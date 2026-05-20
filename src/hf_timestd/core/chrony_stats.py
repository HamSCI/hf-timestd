"""
Chrony Statistics Collector

Parses chronyc output to collect source comparison data for metrology validation.
Runs periodically from the fusion service to track how TSL1/TSL2 compare against
NTP and GPS sources over time.

Snapshots are written to the ``DIAG_chrony_stats`` SQLite table (one row per
source per snapshot) via :func:`make_data_product_writer` and exposed via the
web API from the in-memory circular buffer. Backend selection is governed by
``[storage] write_hdf5 / write_sqlite`` in ``timestd-config.toml`` — the
factory raises if both are off, so this module no longer carries an
HDF5-specific gate of its own.
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
    in a circular buffer and persisted as one row per chrony source
    per snapshot in the ``DIAG_chrony_stats`` SQLite table (Phase 4
    Step 1 — previously a custom HDF5 group bypassing the schema
    registry).
    """

    def __init__(
        self,
        interval_sec: float = 60.0,
        history_size: int = 1440,  # 24h at 1/min
        data_root: Optional[Path] = None,
        storage_config: Optional[Dict[str, Any]] = None,
    ):
        self.interval_sec = interval_sec
        self._last_collect = 0.0
        self._history: List[ChronySnapshot] = []
        self._history_maxlen = history_size
        self._data_root = data_root
        # Backend selection is now governed by [storage] write_hdf5 /
        # write_sqlite in timestd-config.toml via the writer factory.
        # We hold onto storage_config so the writer can be constructed
        # lazily on the first successful snapshot (delays the SQLite
        # connection until we know chronyd is reachable).
        self._storage_config = storage_config
        self._writer = None  # Lazy — see _ensure_writer().
        self._writer_init_attempted = False
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

        # Persist (SQLite via make_data_product_writer; backend choice
        # comes from [storage] in timestd-config.toml — see __init__).
        if self._data_root is not None:
            self._write_snapshot(snapshot)

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

    def _ensure_writer(self):
        """Lazily construct the SQLite writer on first use.

        Deferred until the first successful chrony snapshot so a missing
        chronyd doesn't leave a stray DB file behind, and so init-time
        failures don't crash the fusion service startup. Returns the
        writer or None — a None return is permanent for this collector
        instance (we don't retry on every snapshot).
        """
        if self._writer is not None or self._writer_init_attempted:
            return self._writer
        self._writer_init_attempted = True
        if self._data_root is None:
            return None
        try:
            from hf_timestd.io.dual_writer import make_data_product_writer
            fusion_dir = self._data_root / 'phase2' / 'fusion'
            fusion_dir.mkdir(parents=True, exist_ok=True)
            self._writer = make_data_product_writer(
                output_dir=fusion_dir,
                product_level='DIAG',
                product_name='chrony_stats',
                # chrony sees the whole system, not per-RF-channel data;
                # tag rows with 'fusion' (the service that owns this
                # collector) so the writer's required channel column
                # carries useful provenance.
                channel='fusion',
                storage_config=self._storage_config,
            )
        except Exception as e:
            logger.warning(f"chrony stats writer init failed: {e}")
            self._writer = None
        return self._writer

    def _write_snapshot(self, snap: ChronySnapshot):
        """Persist one snapshot as one row per chrony source.

        Tracking fields are denormalised across every row of the snapshot
        so per-source queries can read tracking context without a join —
        same shape the HDF5 group used to carry. If tracking parsing
        failed, the corresponding row columns are written as NULL.
        """
        if not snap.sources:
            return
        writer = self._ensure_writer()
        if writer is None:
            return

        if snap.tracking:
            sys_offset_s = snap.tracking.system_time_offset_s
            rms_offset_s = snap.tracking.rms_offset_s
            reference_id = snap.tracking.reference_id
        else:
            sys_offset_s = None
            rms_offset_s = None
            reference_id = None

        rows: List[Dict[str, Any]] = []
        for src in snap.sources:
            row = {
                'timestamp_utc': snap.timestamp_utc,
                'unix_time': snap.unix_time,
                'source_name': src.name,
                'source_mode': src.mode,
                'source_state': src.state,
                'stratum': src.stratum,
                'reach': src.reach,
                'offset_us': src.offset_us,
                'error_us': src.error_us,
                'n_samples': src.n_samples,
                'frequency_ppm': src.frequency_ppm,
                'freq_skew_ppm': src.freq_skew_ppm,
                'std_dev_us': src.std_dev_us,
            }
            # Only include tracking columns when we actually have values;
            # the schema marks them not-required so omission writes NULL.
            if sys_offset_s is not None:
                row['sys_offset_s'] = sys_offset_s
                row['rms_offset_s'] = rms_offset_s
                row['reference_id'] = reference_id
            rows.append(row)

        try:
            writer.write_measurements_batch(rows)
        except Exception as e:
            logger.debug(f"Failed to write chrony stats: {e}")

    def close(self) -> None:
        """Release the SQLite writer (call on service shutdown)."""
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception as e:
                logger.debug(f"Error closing chrony stats writer: {e}")
            self._writer = None

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
