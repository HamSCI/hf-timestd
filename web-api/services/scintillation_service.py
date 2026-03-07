"""
Scintillation Data Service.

Provides access to ionospheric scintillation indices from two independent sources:

1. **test_signal** HDF5 — S4 amplitude scintillation from WWV/WWVH multi-tone
   test signal (seconds 13-23 of minute 8/44).  Per-frequency S4 at 2,3,4,5 kHz
   audio tones plus frequency slope for D-layer vs F-layer discrimination.

2. **tick_phase** HDF5 — σ_φ phase scintillation from per-tick carrier phase
   measurements.  Computed as std(detrended phase) over 60-second sliding windows.

Cross-correlation of S4 (amplitude) and σ_φ (phase) provides validation:
during a real scintillation event both should increase simultaneously.
"""

import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional
import logging
import math
import glob

import numpy as np
import h5py

sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'src'))

logger = logging.getLogger(__name__)

# Channels with tick_phase data (phase scintillation)
TICK_PHASE_CHANNELS = [
    'SHARED_2500', 'SHARED_5000', 'SHARED_10000', 'SHARED_15000',
    'WWV_20000', 'WWV_25000', 'CHU_3330', 'CHU_7850', 'CHU_14670',
]

# Channels with test_signal data (amplitude scintillation S4)
# Only WWV/WWVH have the multi-tone test signal
TEST_SIGNAL_CHANNELS = [
    'SHARED_2500', 'SHARED_5000', 'SHARED_10000', 'SHARED_15000',
    'WWV_20000', 'WWV_25000',
]


def _safe_float(val) -> Optional[float]:
    """Convert to float, returning None for NaN/Inf/None."""
    if val is None:
        return None
    try:
        v = float(val)
        return None if (math.isnan(v) or math.isinf(v)) else v
    except (TypeError, ValueError):
        return None


def _convert_to_native(obj: Any) -> Any:
    """Recursively convert numpy types to native Python for JSON."""
    if isinstance(obj, dict):
        return {k: _convert_to_native(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple)):
        return [_convert_to_native(item) for item in obj]
    elif isinstance(obj, np.ndarray):
        return [_convert_to_native(item) for item in obj.tolist()]
    elif isinstance(obj, (np.integer,)):
        return int(obj)
    elif isinstance(obj, (np.floating,)):
        return _safe_float(obj)
    elif isinstance(obj, np.bool_):
        return bool(obj)
    elif isinstance(obj, float):
        return _safe_float(obj)
    return obj


class ScintillationService:
    """Service for accessing scintillation data from test_signal and tick_phase."""

    def __init__(self, data_root: Path):
        self.data_root = Path(data_root)
        self.phase2_dir = self.data_root / 'phase2'

    # ------------------------------------------------------------------
    # S4 from test_signal HDF5
    # ------------------------------------------------------------------

    def _read_test_signal_s4(
        self,
        start: datetime,
        end: datetime,
    ) -> List[Dict[str, Any]]:
        """Read S4 amplitude scintillation from test_signal HDF5 files."""
        records = []
        start_iso = start.strftime('%Y-%m-%dT%H:%M:%SZ')
        end_iso = end.strftime('%Y-%m-%dT%H:%M:%SZ')

        for channel in TEST_SIGNAL_CHANNELS:
            ts_dir = self.phase2_dir / channel / 'test_signal'
            if not ts_dir.exists():
                continue

            # Find HDF5 files covering the date range
            for dt in self._date_range(start, end):
                date_str = dt.strftime('%Y%m%d')
                pattern = str(ts_dir / f'*{date_str}*.h5')
                for fpath in sorted(glob.glob(pattern)):
                    try:
                        with h5py.File(fpath, 'r', libver='latest', swmr=True) as f:
                            if 'timestamp_utc' not in f:
                                continue
                            timestamps = [
                                t.decode() if isinstance(t, bytes) else t
                                for t in f['timestamp_utc'][:]
                            ]
                            stations = [
                                s.decode() if isinstance(s, bytes) else s
                                for s in f['station'][:]
                            ] if 'station' in f else ['WWV'] * len(timestamps)

                            freq_mhz = f['frequency_mhz'][:] if 'frequency_mhz' in f else [None] * len(timestamps)

                            # Read S4 fields
                            s4_idx = f['scintillation_index'][:] if 'scintillation_index' in f else [None] * len(timestamps)
                            s4_2k = f['s4_2khz'][:] if 's4_2khz' in f else [None] * len(timestamps)
                            s4_3k = f['s4_3khz'][:] if 's4_3khz' in f else [None] * len(timestamps)
                            s4_4k = f['s4_4khz'][:] if 's4_4khz' in f else [None] * len(timestamps)
                            s4_5k = f['s4_5khz'][:] if 's4_5khz' in f else [None] * len(timestamps)
                            s4_slope = f['s4_frequency_slope'][:] if 's4_frequency_slope' in f else [None] * len(timestamps)
                            fading_var = f['fading_variance'][:] if 'fading_variance' in f else [None] * len(timestamps)

                            for i, ts in enumerate(timestamps):
                                if ts < start_iso or ts > end_iso:
                                    continue
                                s4_val = _safe_float(s4_idx[i])
                                records.append({
                                    'timestamp': ts,
                                    'channel': channel,
                                    'station': stations[i] if i < len(stations) else 'WWV',
                                    'frequency_mhz': _safe_float(freq_mhz[i]) if i < len(freq_mhz) else None,
                                    's4': s4_val,
                                    's4_2khz': _safe_float(s4_2k[i]),
                                    's4_3khz': _safe_float(s4_3k[i]),
                                    's4_4khz': _safe_float(s4_4k[i]),
                                    's4_5khz': _safe_float(s4_5k[i]),
                                    's4_frequency_slope': _safe_float(s4_slope[i]),
                                    'fading_variance': _safe_float(fading_var[i]),
                                    'source': 'test_signal',
                                })
                    except Exception as e:
                        logger.warning(f"Could not read test_signal from {fpath}: {e}")
        return records

    # ------------------------------------------------------------------
    # σ_φ from tick_phase HDF5
    # ------------------------------------------------------------------

    def _read_tick_phase_sigma_phi(
        self,
        start: datetime,
        end: datetime,
        window_seconds: float = 60.0,
    ) -> List[Dict[str, Any]]:
        """
        Compute σ_φ phase scintillation from tick_phase HDF5 data.

        Groups per-tick carrier phase by (channel, station, minute), detrends
        with a linear fit to remove Doppler, and reports std(residual) as σ_φ.
        """
        records = []
        start_epoch = start.timestamp()
        end_epoch = end.timestamp()

        for channel in TICK_PHASE_CHANNELS:
            tp_dir = self.phase2_dir / channel / 'tick_phase'
            if not tp_dir.exists():
                continue

            for dt in self._date_range(start, end):
                date_str = dt.strftime('%Y%m%d')
                pattern = str(tp_dir / f'*{date_str}*.h5')
                for fpath in sorted(glob.glob(pattern)):
                    try:
                        with h5py.File(fpath, 'r', libver='latest', swmr=True) as f:
                            if 'carrier_phase_rad' not in f or 'window_center_second' not in f:
                                continue

                            phases = f['carrier_phase_rad'][:]
                            seconds = f['window_center_second'][:]
                            snrs = f['snr_db'][:] if 'snr_db' in f else np.zeros(len(phases))
                            stations = [
                                s.decode() if isinstance(s, bytes) else s
                                for s in f['station'][:]
                            ] if 'station' in f else ['UNKNOWN'] * len(phases)
                            freq_mhz = f['frequency_mhz'][:] if 'frequency_mhz' in f else [None] * len(phases)
                            raw_mb = f['minute_boundary_utc'][:] if 'minute_boundary_utc' in f else np.array([])
                            if len(raw_mb) == 0:
                                continue

                            # minute_boundary_utc may be Unix epoch (int64/float)
                            # or ISO string — handle both
                            if np.issubdtype(raw_mb.dtype, np.integer) or np.issubdtype(raw_mb.dtype, np.floating):
                                mb_epochs = raw_mb.astype(float)
                            else:
                                # String timestamps — parse to epoch
                                mb_epochs = np.zeros(len(raw_mb))
                                for _i, _v in enumerate(raw_mb):
                                    _s = _v.decode() if isinstance(_v, bytes) else str(_v)
                                    try:
                                        mb_epochs[_i] = datetime.fromisoformat(_s.replace('Z', '+00:00')).timestamp()
                                    except (ValueError, AttributeError):
                                        mb_epochs[_i] = 0.0

                            # Group by (station, minute_boundary_epoch)
                            groups: Dict[tuple, List[int]] = {}
                            for idx in range(len(phases)):
                                key = (stations[idx], float(mb_epochs[idx]))
                                if key not in groups:
                                    groups[key] = []
                                groups[key].append(idx)

                            for (stn, mb_epoch), indices in groups.items():
                                if mb_epoch <= 0:
                                    continue
                                if mb_epoch < start_epoch or mb_epoch > end_epoch:
                                    continue

                                idx_arr = np.array(indices)
                                ph = phases[idx_arr]
                                sec = seconds[idx_arr]
                                snr = snrs[idx_arr]

                                # Filter by SNR >= 3 dB
                                good = snr >= 3.0
                                if np.sum(good) < 5:
                                    continue
                                ph = ph[good]
                                sec = sec[good]
                                snr_good = snr[good]

                                # Sort by time
                                order = np.argsort(sec)
                                ph = np.unwrap(ph[order])
                                sec = sec[order]

                                # Detrend: linear fit removes Doppler
                                try:
                                    coeffs = np.polyfit(sec - sec[0], ph, 1)
                                    trend = np.polyval(coeffs, sec - sec[0])
                                    detrended = ph - trend
                                    doppler_hz = coeffs[0] / (2 * np.pi)
                                except (np.linalg.LinAlgError, ValueError):
                                    detrended = ph - np.mean(ph)
                                    doppler_hz = 0.0

                                sigma_phi = float(np.std(detrended))

                                # Classify severity
                                if sigma_phi < 0.2:
                                    severity = 'weak'
                                elif sigma_phi < 0.5:
                                    severity = 'moderate'
                                else:
                                    severity = 'strong'

                                # Also compute amplitude S4 from correlation_peak if available
                                amp_s4 = None
                                if 'correlation_peak' in f:
                                    amps = f['correlation_peak'][:][idx_arr][good][order]
                                    if len(amps) >= 5:
                                        intensity = amps ** 2
                                        mean_i = np.mean(intensity)
                                        if mean_i > 1e-10:
                                            amp_s4 = float(np.sqrt(np.var(intensity)) / mean_i)
                                            
                                # P3-C: Compute S4 opportunistically from SNR if correlation peak isn't available
                                if amp_s4 is None and len(snr_good) >= 5:
                                    # Convert SNR (dB) to linear power proxy
                                    intensity = 10.0 ** (snr_good / 10.0)
                                    mean_i = np.mean(intensity)
                                    if mean_i > 1e-10:
                                        amp_s4 = float(np.sqrt(np.var(intensity)) / mean_i)

                                mb_iso = datetime.fromtimestamp(mb_epoch, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

                                records.append({
                                    'timestamp': mb_iso,
                                    'channel': channel,
                                    'station': stn,
                                    'frequency_mhz': _safe_float(freq_mhz[indices[0]]) if freq_mhz[indices[0]] is not None else None,
                                    'sigma_phi_rad': _safe_float(sigma_phi),
                                    'sigma_phi_severity': severity,
                                    'doppler_hz': _safe_float(doppler_hz),
                                    'tick_s4': _safe_float(amp_s4),
                                    'n_ticks': int(np.sum(good)),
                                    'mean_snr_db': _safe_float(np.mean(snr_good)),
                                    'source': 'tick_phase',
                                })
                    except Exception as e:
                        logger.warning(f"Could not read tick_phase from {fpath}: {e}")
        return records

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_latest_by_path(self) -> Dict[str, Any]:
        """
        Get latest scintillation data organized by propagation path.

        Combines S4 from test_signal and σ_φ from tick_phase.
        """
        try:
            end_time = datetime.now(timezone.utc)
            start_time = end_time - timedelta(hours=2)

            s4_records = self._read_test_signal_s4(start_time, end_time)
            sigma_records = self._read_tick_phase_sigma_phi(start_time, end_time)

            # Aggregate by station
            path_data: Dict[str, Dict] = {}

            for rec in s4_records:
                stn = rec['station']
                if stn not in path_data:
                    path_data[stn] = {'s4_values': [], 'sigma_phi_values': [], 'fading_variances': [], 'frequencies': set()}
                s4_val = rec.get('s4')
                if s4_val is not None:
                    path_data[stn]['s4_values'].append(s4_val)
                fv = rec.get('fading_variance')
                if fv is not None:
                    path_data[stn]['fading_variances'].append(fv)
                if rec.get('frequency_mhz'):
                    path_data[stn]['frequencies'].add(rec['frequency_mhz'])

            for rec in sigma_records:
                stn = rec['station']
                if stn not in path_data:
                    path_data[stn] = {'s4_values': [], 'sigma_phi_values': [], 'fading_variances': [], 'frequencies': set()}
                sp = rec.get('sigma_phi_rad')
                if sp is not None:
                    path_data[stn]['sigma_phi_values'].append(sp)
                if rec.get('frequency_mhz'):
                    path_data[stn]['frequencies'].add(rec['frequency_mhz'])

            result = {'paths': {}, 'timestamp': end_time.isoformat()}

            for station, data in path_data.items():
                s4_vals = [v for v in data['s4_values'] if v is not None]
                sigma_vals = [v for v in data['sigma_phi_values'] if v is not None]

                avg_s4 = sum(s4_vals) / len(s4_vals) if s4_vals else None
                avg_sigma = sum(sigma_vals) / len(sigma_vals) if sigma_vals else None

                # Severity from S4 (amplitude) if available, else from σ_φ
                if avg_s4 is not None:
                    if avg_s4 < 0.3:
                        severity = 'weak'
                    elif avg_s4 < 0.6:
                        severity = 'moderate'
                    else:
                        severity = 'strong'
                elif avg_sigma is not None:
                    if avg_sigma < 0.2:
                        severity = 'weak'
                    elif avg_sigma < 0.5:
                        severity = 'moderate'
                    else:
                        severity = 'strong'
                else:
                    severity = 'unknown'

                result['paths'][station] = {
                    'station': station,
                    's4_mean': _safe_float(avg_s4),
                    's4_max': _safe_float(max(s4_vals)) if s4_vals else None,
                    'sigma_phi_mean': _safe_float(avg_sigma),
                    'sigma_phi_max': _safe_float(max(sigma_vals)) if sigma_vals else None,
                    'fading_variance_mean': _safe_float(sum(data['fading_variances']) / len(data['fading_variances'])) if data['fading_variances'] else None,
                    'n_s4_measurements': len(s4_vals),
                    'n_sigma_phi_measurements': len(sigma_vals),
                    'severity': severity,
                    'frequencies_observed': sorted(data['frequencies']),
                }

            result['n_paths'] = len(result['paths'])
            return _convert_to_native(result)

        except Exception as e:
            logger.error(f"Error getting scintillation data: {e}")
            return {'paths': {}, 'n_paths': 0, 'error': str(e)}

    def get_history(
        self,
        start: datetime,
        end: datetime,
        station: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Get scintillation history combining S4 and σ_φ time series.

        Returns both test_signal S4 and tick_phase σ_φ records, sorted by time.
        """
        try:
            s4_records = self._read_test_signal_s4(start, end)
            sigma_records = self._read_tick_phase_sigma_phi(start, end)

            all_data = []

            for rec in s4_records:
                if station and rec.get('station') != station:
                    continue
                all_data.append(rec)

            for rec in sigma_records:
                if station and rec.get('station') != station:
                    continue
                all_data.append(rec)

            all_data.sort(key=lambda x: x.get('timestamp', ''))

            return _convert_to_native({
                'measurements': all_data,
                'count': len(all_data),
                'n_s4': sum(1 for r in all_data if r.get('source') == 'test_signal'),
                'n_sigma_phi': sum(1 for r in all_data if r.get('source') == 'tick_phase'),
            })

        except Exception as e:
            logger.error(f"Error getting scintillation history: {e}")
            return {'measurements': [], 'count': 0, 'error': str(e)}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _date_range(start: datetime, end: datetime) -> List[datetime]:
        """Generate list of dates (midnight) covering start..end."""
        dates = []
        current = start.replace(hour=0, minute=0, second=0, microsecond=0)
        end_day = end.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        while current <= end_day:
            dates.append(current)
            current += timedelta(days=1)
        return dates
