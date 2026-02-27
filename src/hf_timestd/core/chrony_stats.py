"""
Chrony Statistics Collector

Parses chronyc output to collect source comparison data for metrology validation.
Runs periodically from the fusion service to track how TSL1/TSL2 compare against
NTP and GPS sources over time.

Data is written to HDF5 for historical analysis and exposed via the web API.
"""

import logging
import subprocess
import re
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)


@dataclass
class ChronySource:
    """A single chrony source from 'chronyc sources' + 'chronyc sourcestats'."""
    name: str               # Source name/IP (e.g. "TSL1", "192.168.0.203")
    mode: str               # '#' = refclock, '^' = server, '=' = peer
    state: str              # '*' = selected, '+' = combined, '-' = not combined,
                            # 'x' = falseticker, '~' = too variable, '?' = unusable
    stratum: int
    poll: int               # Log2 poll interval
    reach: int              # Reachability register (octal)
    last_rx: Optional[int]  # Seconds since last sample received
    offset_us: float        # Adjusted offset in microseconds
    error_us: float         # Estimated error in microseconds

    # From sourcestats
    n_samples: int = 0
    frequency_ppm: float = 0.0
    freq_skew_ppm: float = 0.0
    std_dev_us: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ChronyTracking:
    """System tracking state from 'chronyc tracking'."""
    reference_id: str       # e.g. "C0A800CB (192.168.0.203)"
    stratum: int
    ref_time_utc: str
    system_time_offset_s: float
    last_offset_s: float
    rms_offset_s: float
    frequency_ppm: float
    residual_freq_ppm: float
    skew_ppm: float
    root_delay_s: float
    root_dispersion_s: float
    update_interval_s: float
    leap_status: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ChronySnapshot:
    """Complete chrony state snapshot at a point in time."""
    timestamp_utc: str
    unix_time: float
    tracking: Optional[ChronyTracking]
    sources: List[ChronySource]

    def to_dict(self) -> Dict[str, Any]:
        return {
            'timestamp_utc': self.timestamp_utc,
            'unix_time': self.unix_time,
            'tracking': self.tracking.to_dict() if self.tracking else None,
            'sources': [s.to_dict() for s in self.sources],
        }


def _run_chronyc(args: List[str], timeout: float = 5.0) -> Optional[str]:
    """Run a chronyc command and return stdout, or None on failure."""
    try:
        result = subprocess.run(
            ['chronyc'] + args,
            capture_output=True, text=True, timeout=timeout
        )
        if result.returncode == 0:
            return result.stdout
        logger.debug(f"chronyc {' '.join(args)} failed: {result.stderr.strip()}")
        return None
    except FileNotFoundError:
        logger.warning("chronyc not found — chrony stats collection disabled")
        return None
    except subprocess.TimeoutExpired:
        logger.warning(f"chronyc {' '.join(args)} timed out after {timeout}s")
        return None
    except Exception as e:
        logger.debug(f"chronyc {' '.join(args)} error: {e}")
        return None


def parse_sources(output: str) -> List[ChronySource]:
    """Parse 'chronyc sources' output into ChronySource objects.

    Example line:
    #? TSL1                          0   4    10    85  +1476us[+1475us] +/- 2000us
    ^* 192.168.0.203                 1   2   377     1  +4441ns[+4565ns] +/-   84us
    """
    sources = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith('=') or line.startswith('.') or line.startswith('/') or line.startswith('|') or line.startswith('MS '):
            continue
        # Match: mode state name  stratum poll reach lastrx  offset[measured] +/- error
        m = re.match(
            r'^([#^=])([*+\-x~?])\s+'     # mode + state
            r'(\S+)\s+'                     # name/IP
            r'(\d+)\s+'                     # stratum
            r'(\d+)\s+'                     # poll
            r'(\d+)\s+'                     # reach (octal)
            r'(\S+)\s+'                     # last rx
            r'([+-]?\S+?)(?:us|ns|ms|s)\[' # adjusted offset (with unit)
            r'[^\]]*\]\s+\+/-\s+'          # [measured offset]
            r'(\S+?)(?:us|ns|ms|s)\s*$',   # estimated error (with unit)
            line
        )
        if not m:
            continue

        mode, state, name = m.group(1), m.group(2), m.group(3)
        stratum = int(m.group(4))
        poll = int(m.group(5))
        reach = int(m.group(6), 8) if m.group(6).isdigit() else 0
        last_rx_str = m.group(7)
        last_rx = None if last_rx_str == '-' else _parse_age(last_rx_str)

        offset_us = _parse_value_to_us(m.group(8), line)
        error_us = _parse_value_to_us(m.group(9), line)

        sources.append(ChronySource(
            name=name, mode=mode, state=state,
            stratum=stratum, poll=poll, reach=reach,
            last_rx=last_rx, offset_us=offset_us, error_us=error_us,
        ))

    return sources


def _parse_age(s: str) -> Optional[int]:
    """Parse a chronyc age string like '85', '2m', '1h' into seconds."""
    try:
        if s.endswith('m'):
            return int(s[:-1]) * 60
        elif s.endswith('h'):
            return int(s[:-1]) * 3600
        elif s.endswith('d'):
            return int(s[:-1]) * 86400
        elif s.endswith('y'):
            return int(s[:-1]) * 365 * 86400
        else:
            return int(s)
    except (ValueError, IndexError):
        return None


def _parse_value_to_us(num_str: str, full_line: str) -> float:
    """Parse a numeric value and its unit from the source line into microseconds.

    The regex captures the number but the unit is still in the line.
    We search the line for the number+unit pattern to determine the unit.
    """
    try:
        val = float(num_str)
    except ValueError:
        return 0.0

    # Find the unit that follows this number in the line
    # Look for the pattern number followed by unit
    escaped = re.escape(num_str)
    unit_match = re.search(escaped + r'(ns|us|ms|s)', full_line)
    if unit_match:
        unit = unit_match.group(1)
        if unit == 'ns':
            return val / 1000.0
        elif unit == 'us':
            return val
        elif unit == 'ms':
            return val * 1000.0
        elif unit == 's':
            return val * 1_000_000.0
    return val  # assume microseconds


def parse_sourcestats(output: str) -> Dict[str, Dict[str, float]]:
    """Parse 'chronyc sourcestats' output into per-source stats.

    Example line:
    TSL1                        6   3   399     -0.778      0.649  +1413us    25us
    192.168.0.203              48  25   191     +0.001      0.052     +0ns  5632ns
    """
    stats = {}
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith('=') or line.startswith('.') or line.startswith('/') or line.startswith('|') or line.startswith('Name'):
            continue
        parts = line.split()
        if len(parts) < 8:
            continue

        name = parts[0]
        try:
            n_samples = int(parts[1])
            frequency_ppm = float(parts[4])
            freq_skew_ppm = float(parts[5])
            # offset and std_dev have units attached
            offset_str = parts[6]
            stddev_str = parts[7]

            stats[name] = {
                'n_samples': n_samples,
                'frequency_ppm': frequency_ppm,
                'freq_skew_ppm': freq_skew_ppm,
                'offset_us': _parse_chronyc_value_us(offset_str),
                'std_dev_us': _parse_chronyc_value_us(stddev_str),
            }
        except (ValueError, IndexError):
            continue

    return stats


def _parse_chronyc_value_us(s: str) -> float:
    """Parse a chronyc value+unit string like '+1413us', '5632ns', '-0.001ms'."""
    m = re.match(r'^([+-]?\d+\.?\d*(?:e[+-]?\d+)?)(ns|us|ms|s)$', s)
    if not m:
        return 0.0
    val = float(m.group(1))
    unit = m.group(2)
    if unit == 'ns':
        return val / 1000.0
    elif unit == 'us':
        return val
    elif unit == 'ms':
        return val * 1000.0
    elif unit == 's':
        return val * 1_000_000.0
    return val


def parse_tracking(output: str) -> Optional[ChronyTracking]:
    """Parse 'chronyc tracking' output."""
    fields = {}
    for line in output.splitlines():
        if ':' not in line:
            continue
        key, _, val = line.partition(':')
        fields[key.strip()] = val.strip()

    if 'Reference ID' not in fields:
        return None

    def _float(key: str) -> float:
        s = fields.get(key, '0')
        # Extract first numeric value: "0.000000869 seconds slow of NTP time" -> 0.000000869
        m = re.match(r'^([+-]?\d+\.?\d*(?:e[+-]?\d+)?)', s)
        if m:
            val = float(m.group(1))
            # Handle "slow" (negative) and "fast" (positive)
            if 'slow' in s:
                val = -val
            return val
        return 0.0

    return ChronyTracking(
        reference_id=fields.get('Reference ID', ''),
        stratum=int(fields.get('Stratum', '0')),
        ref_time_utc=fields.get('Ref time (UTC)', ''),
        system_time_offset_s=_float('System time'),
        last_offset_s=_float('Last offset'),
        rms_offset_s=_float('RMS offset'),
        frequency_ppm=_float('Frequency'),
        residual_freq_ppm=_float('Residual freq'),
        skew_ppm=_float('Skew'),
        root_delay_s=_float('Root delay'),
        root_dispersion_s=_float('Root dispersion'),
        update_interval_s=_float('Update interval'),
        leap_status=fields.get('Leap status', 'Unknown'),
    )


def collect_chrony_snapshot() -> Optional[ChronySnapshot]:
    """Collect a complete chrony state snapshot.

    Runs chronyc sources, sourcestats, and tracking, merges
    the results into a single ChronySnapshot.
    """
    sources_out = _run_chronyc(['sources'])
    stats_out = _run_chronyc(['sourcestats'])
    tracking_out = _run_chronyc(['tracking'])

    if sources_out is None:
        return None

    now = datetime.now(timezone.utc)

    sources = parse_sources(sources_out)
    if stats_out:
        stats = parse_sourcestats(stats_out)
        for src in sources:
            if src.name in stats:
                s = stats[src.name]
                src.n_samples = s.get('n_samples', 0)
                src.frequency_ppm = s.get('frequency_ppm', 0.0)
                src.freq_skew_ppm = s.get('freq_skew_ppm', 0.0)
                src.std_dev_us = s.get('std_dev_us', 0.0)

    tracking = parse_tracking(tracking_out) if tracking_out else None

    return ChronySnapshot(
        timestamp_utc=now.strftime('%Y-%m-%dT%H:%M:%SZ'),
        unix_time=now.timestamp(),
        tracking=tracking,
        sources=sources,
    )


class ChronyStatsCollector:
    """Periodic chrony statistics collector.

    Call collect() from the fusion main loop. It rate-limits to
    at most once per `interval_sec` seconds. Results are stored
    in a circular buffer and optionally written to HDF5.
    """

    def __init__(
        self,
        interval_sec: float = 60.0,
        history_size: int = 1440,  # 24h at 1/min
        data_root: Optional[Path] = None,
    ):
        self.interval_sec = interval_sec
        self._last_collect = 0.0
        self._history: List[ChronySnapshot] = []
        self._history_maxlen = history_size
        self._data_root = data_root
        self._available = None  # None = not checked yet

    def collect(self, force: bool = False) -> Optional[ChronySnapshot]:
        """Collect chrony stats if enough time has elapsed.

        Returns the snapshot if collected, None if skipped or failed.
        """
        now = time.time()
        if not force and (now - self._last_collect) < self.interval_sec:
            return None

        self._last_collect = now

        snapshot = collect_chrony_snapshot()
        if snapshot is None:
            if self._available is None:
                self._available = False
                logger.info("chronyc not available — chrony stats collection disabled")
            return None

        if self._available is None:
            self._available = True
            logger.info(
                f"Chrony stats collection enabled: "
                f"{len(snapshot.sources)} sources, "
                f"ref={snapshot.tracking.reference_id if snapshot.tracking else 'unknown'}"
            )

        # Store in circular buffer
        self._history.append(snapshot)
        if len(self._history) > self._history_maxlen:
            self._history = self._history[-self._history_maxlen:]

        # Log summary
        self._log_snapshot(snapshot)

        # Write to HDF5
        if self._data_root:
            self._write_hdf5(snapshot)

        return snapshot

    def _log_snapshot(self, snap: ChronySnapshot):
        """Log a concise summary of the chrony state."""
        parts = []
        for src in snap.sources:
            offset_ms = src.offset_us / 1000.0
            stddev_ms = src.std_dev_us / 1000.0
            parts.append(
                f"{src.name}({src.state}): {offset_ms:+.3f}±{stddev_ms:.3f}ms "
                f"[n={src.n_samples},reach={src.reach:03o}]"
            )
        if snap.tracking:
            sys_offset_us = snap.tracking.system_time_offset_s * 1e6
            rms_us = snap.tracking.rms_offset_s * 1e6
            parts.append(
                f"sys={sys_offset_us:+.1f}µs rms={rms_us:.1f}µs "
                f"ref={snap.tracking.reference_id}"
            )
        logger.info(f"[CHRONY] {' | '.join(parts)}")

    def _write_hdf5(self, snap: ChronySnapshot):
        """Append chrony snapshot to daily HDF5 file."""
        try:
            import h5py
            import numpy as np

            date_str = datetime.now(timezone.utc).strftime('%Y%m%d')
            fusion_dir = self._data_root / 'phase2' / 'fusion'
            fusion_dir.mkdir(parents=True, exist_ok=True)
            h5_path = fusion_dir / f'chrony_stats_{date_str}.h5'

            with h5py.File(h5_path, 'a') as f:
                # Flat table: one row per source per snapshot
                grp = f.require_group('chrony_sources')

                # Determine next row index
                if 'timestamp_utc' in grp:
                    n_existing = len(grp['timestamp_utc'])
                else:
                    n_existing = 0

                n_new = len(snap.sources)
                if n_new == 0:
                    return

                # Column arrays for this batch
                timestamps = [snap.timestamp_utc] * n_new
                unix_times = [snap.unix_time] * n_new
                names = [s.name for s in snap.sources]
                modes = [s.mode for s in snap.sources]
                states = [s.state for s in snap.sources]
                stratums = [s.stratum for s in snap.sources]
                offsets_us = [s.offset_us for s in snap.sources]
                errors_us = [s.error_us for s in snap.sources]
                n_samples = [s.n_samples for s in snap.sources]
                freq_ppms = [s.frequency_ppm for s in snap.sources]
                freq_skews = [s.freq_skew_ppm for s in snap.sources]
                std_devs = [s.std_dev_us for s in snap.sources]
                reaches = [s.reach for s in snap.sources]

                # Tracking fields (repeated per source row for easy joins)
                if snap.tracking:
                    sys_offsets = [snap.tracking.system_time_offset_s] * n_new
                    rms_offsets = [snap.tracking.rms_offset_s] * n_new
                    ref_ids = [snap.tracking.reference_id] * n_new
                else:
                    sys_offsets = [float('nan')] * n_new
                    rms_offsets = [float('nan')] * n_new
                    ref_ids = [''] * n_new

                # Write columns (create or append)
                str_dt = h5py.string_dtype()
                columns = {
                    'timestamp_utc': (timestamps, str_dt),
                    'unix_time': (unix_times, 'f8'),
                    'source_name': (names, str_dt),
                    'source_mode': (modes, str_dt),
                    'source_state': (states, str_dt),
                    'stratum': (stratums, 'i4'),
                    'offset_us': (offsets_us, 'f8'),
                    'error_us': (errors_us, 'f8'),
                    'n_samples': (n_samples, 'i4'),
                    'frequency_ppm': (freq_ppms, 'f8'),
                    'freq_skew_ppm': (freq_skews, 'f8'),
                    'std_dev_us': (std_devs, 'f8'),
                    'reach': (reaches, 'i4'),
                    'sys_offset_s': (sys_offsets, 'f8'),
                    'rms_offset_s': (rms_offsets, 'f8'),
                    'reference_id': (ref_ids, str_dt),
                }

                for col_name, (data, dtype) in columns.items():
                    arr = np.array(data, dtype=dtype) if dtype != str_dt else np.array(data, dtype=object)
                    if col_name in grp:
                        ds = grp[col_name]
                        ds.resize(n_existing + n_new, axis=0)
                        ds[n_existing:] = arr
                    else:
                        maxshape = (None,)
                        grp.create_dataset(
                            col_name, data=arr,
                            maxshape=maxshape, chunks=True,
                            dtype=dtype if dtype != str_dt else str_dt,
                        )

        except ImportError:
            pass  # h5py not available
        except Exception as e:
            logger.debug(f"Failed to write chrony stats HDF5: {e}")

    @property
    def available(self) -> bool:
        """Whether chronyc was found and stats collection is active."""
        return self._available is True

    @property
    def latest(self) -> Optional[ChronySnapshot]:
        """Most recent snapshot."""
        return self._history[-1] if self._history else None

    @property
    def history(self) -> List[ChronySnapshot]:
        """All stored snapshots."""
        return list(self._history)

    def get_source_history(self, source_name: str) -> List[Dict[str, Any]]:
        """Get offset/stddev time series for a specific source."""
        points = []
        for snap in self._history:
            for src in snap.sources:
                if src.name == source_name:
                    points.append({
                        'timestamp_utc': snap.timestamp_utc,
                        'unix_time': snap.unix_time,
                        'offset_us': src.offset_us,
                        'std_dev_us': src.std_dev_us,
                        'state': src.state,
                        'reach': src.reach,
                        'n_samples': src.n_samples,
                    })
                    break
        return points
