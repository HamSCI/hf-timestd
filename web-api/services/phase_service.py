"""
Phase and Doppler analysis service.

Reads L2/tick_phase HDF5 data and computes:
- Phase time series (unwrapped)
- Doppler shift from phase rate
- Phase scintillation index (sigma_phi)
- Mode transition detection
- Summary across all channels
"""

import numpy as np
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any
import logging
import sqlite3

try:
    from config import config as _web_config
except Exception:
    _web_config = None

from hf_timestd.io import make_data_product_reader
from hf_timestd.io.sqlite_writer import DEFAULT_DB_PATH

logger = logging.getLogger(__name__)

# Station colors for consistent visualization
STATION_COLORS = {
    'WWV': '#3b82f6',    # blue
    'WWVH': '#f59e0b',   # amber
    'CHU': '#10b981',    # green
    'BPM': '#ef4444',    # red
}

# Channels where DC carrier phase is meaningful (single-station)
UNAMBIGUOUS_CHANNELS = {
    'CHU_3330', 'CHU_7850', 'CHU_14670',
    'WWV_20000', 'WWV_25000',
}


class PhaseService:
    """
    Service for phase and Doppler analysis from L2/tick_phase HDF5 data.
    """

    def __init__(self, data_root: Path):
        self.data_root = Path(data_root)
        self.phase2_dir = self.data_root / 'phase2'

    def _list_channels(self) -> List[str]:
        """List channels visible in L2_tick_phase."""
        db_path = self._db_path()
        if not db_path.exists():
            return []
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
        except sqlite3.Error:
            return []
        try:
            rows = conn.execute(
                "SELECT DISTINCT channel FROM L2_tick_phase ORDER BY channel"
            ).fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            conn.close()
        return [r[0] for r in rows]

    def _db_path(self) -> Path:
        storage = getattr(_web_config, 'storage', {}) if _web_config else {}
        return Path(storage.get('sqlite_path', DEFAULT_DB_PATH))

    def _read_phase_data(
        self,
        channels: List[str],
        start_epoch: float,
        end_epoch: float,
        station: Optional[str] = None,
        max_rows: int = 50000,
    ) -> List[Dict[str, Any]]:
        """Read L2_tick_phase records for the given channels in the window."""
        records: List[Dict[str, Any]] = []
        start_iso = datetime.fromtimestamp(
            start_epoch, tz=timezone.utc
        ).isoformat().replace('+00:00', 'Z')
        end_iso = datetime.fromtimestamp(
            end_epoch, tz=timezone.utc
        ).isoformat().replace('+00:00', 'Z')
        storage_config = getattr(_web_config, 'storage', {}) if _web_config else {}

        for ch in channels:
            try:
                reader = make_data_product_reader(
                    data_dir=self.phase2_dir / ch / 'tick_phase',
                    product_level='L2',
                    product_name='tick_phase',
                    channel=ch,
                    storage_config=storage_config,
                )
            except Exception as e:
                logger.warning(f"L2_tick_phase reader init failed for {ch}: {e}")
                continue
            try:
                try:
                    rows = reader.read_time_range(start=start_iso, end=end_iso)
                except Exception as e:
                    logger.warning(f"L2_tick_phase read failed for {ch}: {e}")
                    rows = []
            finally:
                close_fn = getattr(reader, 'close', None)
                if close_fn is not None:
                    try:
                        close_fn()
                    except Exception:
                        pass

            for row in rows:
                mb = row.get('minute_boundary_utc')
                wc = row.get('window_center_second')
                if mb is None or wc is None:
                    continue
                utc_epoch = float(mb) + float(wc)
                if utc_epoch < start_epoch or utc_epoch > end_epoch:
                    continue
                stn = row.get('station')
                if station and stn != station:
                    continue
                rec = {
                    'utc_epoch': utc_epoch,
                    'minute_boundary_utc': int(mb),
                    'channel': ch,
                }
                for name in (
                    'window_center_second', 'phase_rad',
                    'carrier_phase_rad', 'dc_carrier_phase_rad',
                    'snr_db', 'coherence_quality', 'timing_offset_ms',
                    'station', 'frequency_mhz',
                    'window_start_second', 'window_end_second',
                ):
                    val = row.get(name)
                    if val is None:
                        continue
                    rec[name] = val
                records.append(rec)
                if len(records) >= max_rows:
                    break
            if len(records) >= max_rows:
                break

        records.sort(key=lambda r: r['utc_epoch'])
        return records[:max_rows]

    def get_phase_timeseries(
        self,
        start: datetime,
        end: datetime,
        channel: Optional[str] = None,
        station: Optional[str] = None,
        phase_type: str = 'carrier_phase_rad',
        unwrap: bool = True,
    ) -> Dict[str, Any]:
        """
        Get phase time series, optionally unwrapped.

        Args:
            start, end: Time range
            channel: Filter by channel name
            station: Filter by station name
            phase_type: Which phase field (phase_rad, carrier_phase_rad, dc_carrier_phase_rad)
            unwrap: If True, unwrap phase for continuous tracking

        Returns:
            Dict with timestamps, phase values, metadata
        """
        start_epoch = start.timestamp()
        end_epoch = end.timestamp()

        if channel:
            channels = [channel]
        else:
            channels = self._list_channels()

        records = self._read_phase_data(
            channels, start_epoch, end_epoch, station=station
        )

        if not records:
            return {'series': [], 'metadata': {'n_points': 0}}

        # Group by (channel, station) for separate traces
        groups = {}
        for rec in records:
            key = (rec.get('channel', ''), rec.get('station', ''))
            if key not in groups:
                groups[key] = []
            groups[key].append(rec)

        series = []
        for (ch, stn), recs in sorted(groups.items()):
            times = [r['utc_epoch'] for r in recs]
            phases = [r.get(phase_type, 0.0) for r in recs]
            snrs = [r.get('snr_db', 0.0) for r in recs]

            phases_arr = np.array(phases)
            if unwrap and len(phases_arr) > 1:
                phases_arr = np.unwrap(phases_arr)

            series.append({
                'channel': ch,
                'station': stn,
                'color': STATION_COLORS.get(stn, '#94a3b8'),
                'timestamps': [datetime.fromtimestamp(t, tz=timezone.utc).isoformat() for t in times],
                'epochs': times,
                'phase_rad': phases_arr.tolist(),
                'snr_db': snrs,
                'n_points': len(times),
                'dc_meaningful': ch in UNAMBIGUOUS_CHANNELS,
            })

        return {
            'series': series,
            'metadata': {
                'phase_type': phase_type,
                'unwrapped': unwrap,
                'start': start.isoformat(),
                'end': end.isoformat(),
                'n_points': sum(s['n_points'] for s in series),
                'n_traces': len(series),
            }
        }

    def get_doppler(
        self,
        start: datetime,
        end: datetime,
        channel: Optional[str] = None,
        station: Optional[str] = None,
        smoothing_seconds: float = 30.0,
    ) -> Dict[str, Any]:
        """
        Compute Doppler shift from phase rate: f_D = -(1/2pi) * dphi/dt.

        Args:
            smoothing_seconds: Window for Savitzky-Golay or moving average smoothing
        """
        # Get unwrapped carrier phase
        phase_data = self.get_phase_timeseries(
            start, end, channel=channel, station=station,
            phase_type='carrier_phase_rad', unwrap=True
        )

        series = []
        for trace in phase_data.get('series', []):
            epochs = np.array(trace['epochs'])
            phases = np.array(trace['phase_rad'])

            if len(epochs) < 3:
                continue

            # Compute phase rate via finite differences
            dt = np.diff(epochs)
            dphi = np.diff(phases)

            # Avoid division by zero
            valid = dt > 0
            doppler_hz = np.zeros(len(dt))
            doppler_hz[valid] = -(1.0 / (2 * np.pi)) * dphi[valid] / dt[valid]

            # Timestamps at midpoints
            mid_epochs = (epochs[:-1] + epochs[1:]) / 2.0

            # Smooth if requested
            if smoothing_seconds > 0 and len(doppler_hz) > 5:
                median_dt = float(np.median(dt[valid])) if np.any(valid) else 1.0
                window = max(3, int(smoothing_seconds / median_dt))
                if window % 2 == 0:
                    window += 1
                if window <= len(doppler_hz):
                    # Simple moving average
                    kernel = np.ones(window) / window
                    doppler_smoothed = np.convolve(doppler_hz, kernel, mode='same')
                else:
                    doppler_smoothed = doppler_hz
            else:
                doppler_smoothed = doppler_hz

            series.append({
                'channel': trace['channel'],
                'station': trace['station'],
                'color': trace['color'],
                'timestamps': [
                    datetime.fromtimestamp(t, tz=timezone.utc).isoformat()
                    for t in mid_epochs
                ],
                'epochs': mid_epochs.tolist(),
                'doppler_hz': doppler_smoothed.tolist(),
                'doppler_raw_hz': doppler_hz.tolist(),
                'n_points': len(mid_epochs),
            })

        return {
            'series': series,
            'metadata': {
                'smoothing_seconds': smoothing_seconds,
                'start': start.isoformat(),
                'end': end.isoformat(),
                'n_traces': len(series),
            }
        }

    def get_scintillation(
        self,
        start: datetime,
        end: datetime,
        channel: Optional[str] = None,
        station: Optional[str] = None,
        window_seconds: float = 60.0,
    ) -> Dict[str, Any]:
        """
        Compute phase scintillation index (sigma_phi) over sliding windows.

        sigma_phi = std(detrended phase) over each window.
        """
        phase_data = self.get_phase_timeseries(
            start, end, channel=channel, station=station,
            phase_type='carrier_phase_rad', unwrap=True
        )

        series = []
        for trace in phase_data.get('series', []):
            epochs = np.array(trace['epochs'])
            phases = np.array(trace['phase_rad'])

            if len(epochs) < 10:
                continue

            median_dt = float(np.median(np.diff(epochs)))
            if median_dt <= 0:
                continue
            window_samples = max(5, int(window_seconds / median_dt))

            sigma_phi = []
            sigma_epochs = []

            # P3-C: Extract SNR to compute S4 proxy
            has_snr = 'snr_db' in trace and len(trace['snr_db']) == len(epochs)
            snrs = np.array(trace['snr_db']) if has_snr else None
            
            s4_vals = []

            for i in range(0, len(epochs) - window_samples + 1, max(1, window_samples // 2)):
                chunk = phases[i:i + window_samples]
                t_chunk = epochs[i:i + window_samples]

                # Detrend: remove linear fit
                if len(chunk) >= 3:
                    coeffs = np.polyfit(t_chunk - t_chunk[0], chunk, 1)
                    trend = np.polyval(coeffs, t_chunk - t_chunk[0])
                    detrended = chunk - trend
                    sigma_phi.append(float(np.std(detrended)))
                    sigma_epochs.append(float(np.mean(t_chunk)))
                    
                    if has_snr:
                        snr_chunk = snrs[i:i + window_samples]
                        intensity = 10.0 ** (snr_chunk / 10.0)
                        mean_i = np.mean(intensity)
                        if mean_i > 1e-10:
                            s4_vals.append(float(np.sqrt(np.var(intensity)) / mean_i))
                        else:
                            s4_vals.append(None)
                    else:
                        s4_vals.append(None)

            if not sigma_phi:
                continue

            series.append({
                'channel': trace['channel'],
                'station': trace['station'],
                'color': trace['color'],
                'timestamps': [
                    datetime.fromtimestamp(t, tz=timezone.utc).isoformat()
                    for t in sigma_epochs
                ],
                'epochs': sigma_epochs,
                'sigma_phi_rad': sigma_phi,
                's4': s4_vals if has_snr else None,
                'n_points': len(sigma_phi),
            })

        return {
            'series': series,
            'metadata': {
                'window_seconds': window_seconds,
                'start': start.isoformat(),
                'end': end.isoformat(),
                'n_traces': len(series),
            }
        }

    # Known broadcast station call-signs
    VALID_STATIONS = {'WWV', 'WWVH', 'CHU', 'BPM'}

    def get_available_channels(self) -> Dict[str, Any]:
        """List all channels that have tick_phase data in SQLite."""
        channels = []
        stations: set = set()

        db_path = self._db_path()
        if not db_path.exists():
            return {'channels': [], 'stations': []}

        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
        except sqlite3.Error as e:
            logger.warning(f"Could not open L2_tick_phase for channel listing: {e}")
            return {'channels': [], 'stations': []}

        try:
            rows = conn.execute(
                "SELECT channel, COUNT(*) AS n FROM L2_tick_phase GROUP BY channel"
            ).fetchall()
        except sqlite3.Error as e:
            logger.warning(f"L2_tick_phase listing failed: {e}")
            rows = []

        try:
            for ch, n in rows:
                if ch.startswith('SHARED_'):
                    try:
                        st_rows = conn.execute(
                            "SELECT DISTINCT station FROM L2_tick_phase WHERE channel = ?",
                            (ch,),
                        ).fetchall()
                    except sqlite3.Error:
                        st_rows = []
                    unique_st = sorted(
                        s for (s,) in st_rows if s in self.VALID_STATIONS
                    )
                    stations.update(unique_st)
                    station = unique_st
                else:
                    parts = ch.split('_')
                    s = parts[0] if parts else ch
                    if s in self.VALID_STATIONS:
                        stations.add(s)
                        station = [s]
                    else:
                        station = []
                channels.append({
                    'channel': ch,
                    'stations': station,
                    'n_rows': int(n),
                    'dc_meaningful': ch in UNAMBIGUOUS_CHANNELS,
                })
        finally:
            conn.close()

        return {
            'channels': sorted(channels, key=lambda c: c['channel']),
            'stations': sorted(stations),
        }

    def get_phase_summary(self) -> Dict[str, Any]:
        """
        Get current phase/Doppler state across all channels.
        Returns the latest ~5 minutes of data summarized per channel/station.
        """
        end = datetime.now(timezone.utc)
        start = end - timedelta(minutes=5)

        phase_data = self.get_phase_timeseries(
            start, end, phase_type='carrier_phase_rad', unwrap=True
        )

        channels = []
        for trace in phase_data.get('series', []):
            epochs = np.array(trace['epochs'])
            phases = np.array(trace['phase_rad'])
            snrs = np.array(trace['snr_db'])

            if len(epochs) < 2:
                continue

            # Compute instantaneous Doppler from last few points
            if len(epochs) >= 3:
                dt = epochs[-1] - epochs[-3]
                dphi = phases[-1] - phases[-3]
                if dt > 0:
                    doppler_hz = -(1.0 / (2 * np.pi)) * dphi / dt
                else:
                    doppler_hz = 0.0
            else:
                doppler_hz = 0.0

            # Phase scintillation over the window
            if len(phases) >= 5:
                coeffs = np.polyfit(epochs - epochs[0], phases, 1)
                trend = np.polyval(coeffs, epochs - epochs[0])
                sigma_phi = float(np.std(phases - trend))
            else:
                sigma_phi = 0.0

            channels.append({
                'channel': trace['channel'],
                'station': trace['station'],
                'color': trace['color'],
                'dc_meaningful': trace['dc_meaningful'],
                'latest_phase_rad': float(phases[-1]) if len(phases) > 0 else 0.0,
                'doppler_hz': float(doppler_hz),
                'sigma_phi_rad': sigma_phi,
                'mean_snr_db': float(np.mean(snrs)) if len(snrs) > 0 else 0.0,
                'n_points': len(epochs),
                'last_update': trace['timestamps'][-1] if trace['timestamps'] else None,
            })

        return {
            'channels': channels,
            'timestamp': end.isoformat(),
            'window_minutes': 5,
        }
