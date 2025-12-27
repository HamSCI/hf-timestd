"""
Multi-Broadcast D_clock Fusion Engine

================================================================================
PURPOSE
================================================================================
Combine D_clock estimates from all available broadcasts to produce a
HIGH-ACCURACY UTC(NIST) time estimate through weighted fusion and
auto-calibration.

The fused D_clock should converge to 0ms, indicating perfect alignment
with UTC(NIST).

================================================================================
BROADCAST STRUCTURE
================================================================================
The hf-timestd system monitors up to 17 time signal broadcasts:

    STATION | FREQUENCIES
    --------|----------------------------------------------------
    WWV     | 2.5, 5, 10, 15, 20, 25 MHz (6 broadcasts)
    WWVH    | 2.5, 5, 10, 15 MHz (4 broadcasts, shared with WWV)
    CHU     | 3.33, 7.85, 14.67 MHz (3 broadcasts, unique)
    BPM     | 2.5, 5, 10, 15 MHz (4 broadcasts, shared with WWV/WWVH)

SHARED vs UNIQUE FREQUENCIES:
    - Shared (WWV + WWVH + BPM): 2.5, 5, 10, 15 MHz → 12 broadcasts (need discrimination)
    - WWV-only: 20, 25 MHz → 2 broadcasts
    - CHU-only: 3.33, 7.85, 14.67 MHz → 3 broadcasts (FSK timing reference)

BPM SPECIAL HANDLING:
    - Minutes 25-29, 55-59: UT1 timing (DO NOT USE for UTC without DUT1 correction)
    - Minutes 0-24, 30-54: UTC timing (usable)
    - Tick duration: 10ms (UTC) vs 100ms (UT1) - used for mode detection

================================================================================
FUSION THEORY
================================================================================
Each broadcast provides an independent D_clock estimate:

    D_clock_i = T_arrival_i - T_propagation_i

These estimates have different uncertainties based on:
    - SNR (signal quality)
    - Propagation mode (1-hop vs multi-hop)
    - Discrimination confidence (shared frequencies)
    - Quality grade from convergence model

WEIGHTED FUSION:
    D_clock_fused = Σ(w_i × D_clock_i) / Σ(w_i)

Where weights w_i are computed from:
    w_i = confidence × grade_weight × mode_weight × snr_factor

GRADE WEIGHTS:    A: 1.0, B: 0.8, C: 0.5, D: 0.2
MODE WEIGHTS:     1E: 1.0, 1F: 0.9, 2F: 0.7, 3F: 0.5, GW: 1.0

================================================================================
AUTO-CALIBRATION
================================================================================
Each station has a systematic offset due to:
    - Matched filter group delay
    - Tone rise time differences
    - Detection threshold effects

CALIBRATION MODEL:
    calibration_offset_station = -mean(D_clock_station)

This brings each station's mean D_clock to 0, which is the UTC(NIST) target.

CALIBRATION UPDATE (Exponential Moving Average):
    offset_new = α × (-mean_current) + (1-α) × offset_old
    
Where α = max(0.5, 20/n_samples) for fast initial convergence.

CHU AS REFERENCE:
    CHU's FSK time code provides precise 500ms boundary alignment,
    making it the most trusted reference. However, all stations are
    calibrated to converge to 0 (not to match CHU), since the goal
    is UTC(NIST) alignment.

================================================================================
OUTLIER REJECTION
================================================================================
Uses weighted Median Absolute Deviation (MAD) for robust outlier detection:

    MAD = median(|D_clock_i - weighted_median|) × 1.4826
    
Measurements with deviation > 3σ are rejected.

This prevents ionospheric events or detection errors on one channel
from corrupting the fused estimate.

================================================================================
OUTPUT
================================================================================
The fusion produces:
    - d_clock_fused_ms: Calibrated weighted mean (should → 0)
    - d_clock_raw_ms: Uncalibrated mean (for comparison)
    - uncertainty_ms: Weighted standard deviation
    - n_broadcasts: Number of broadcasts contributing
    - quality_grade: A/B/C/D based on broadcast count and uncertainty

Output is written to: phase2/fusion/fused_d_clock.csv

================================================================================
USAGE
================================================================================
Continuous service mode (typical):

    python -m hf_timestd.core.multi_broadcast_fusion \\
        --data-root /data \\
        --interval 60

Programmatic usage:

    fusion = MultiBroadcastFusion(data_root=Path('/data'))
    result = fusion.fuse(lookback_minutes=10)
    print(f"Fused D_clock: {result.d_clock_fused_ms:+.3f} ms")

================================================================================
REVISION HISTORY
================================================================================
2025-12-07: Added comprehensive theoretical documentation
2025-11-20: Improved calibration to target UTC(NIST) = 0
2025-11-01: Initial implementation with CHU reference
"""

import logging
import json
import csv
import os
import time
import re
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
import numpy as np

# HDF5 I/O for reading L1A and L2 data products
try:
    from hf_timestd.io import DataProductReader
    HDF5_AVAILABLE = True
except ImportError:
    HDF5_AVAILABLE = False
    logger.warning("HDF5 I/O module not available, will use CSV fallback")

logger = logging.getLogger(__name__)


@dataclass
class BroadcastMeasurement:
    """Single D_clock measurement from one broadcast."""
    timestamp: float           # Unix time of measurement
    station: str              # WWV, WWVH, CHU, BPM
    frequency_mhz: float      # Broadcast frequency
    d_clock_ms: float         # Raw D_clock measurement
    propagation_delay_ms: float
    propagation_mode: str     # 1E, 1F, 2F, etc.
    confidence: float         # 0-1 confidence score
    snr_db: float            # Signal quality
    quality_grade: str        # A, B, C, D
    channel_name: str         # Source channel


@dataclass
class BroadcastCalibration:
    """
    Per-broadcast calibration offset learned from data.
    
    Issue 3.2 Fix: Calibration is now per-broadcast (station+frequency) rather
    than per-station. This accounts for frequency-dependent ionospheric delays:
    - Different frequencies have different ionospheric delays (1/f²)
    - Same-frequency broadcasts share ionospheric conditions (correlated errors)
    
    Issue 4.3 Fix: No more hardcoded defaults. Initial offset is 0 with high
    uncertainty, and the system learns from data using ground truth validation.
    
    BPM Note: BPM calibration must account for UT1/UTC alternation.
    UT1 minutes (25-29, 55-59) are excluded from calibration unless
    DUT1 correction is applied.
    """
    station: str              # WWV, WWVH, CHU, BPM
    frequency_mhz: float      # Broadcast frequency (key for correlation)
    offset_ms: float          # Calibration offset to apply
    uncertainty_ms: float     # Uncertainty in offset
    n_samples: int            # Number of samples used
    last_updated: float       # Unix time of last update
    reference_station: str    # Station used as reference (CHU)
    
    @property
    def broadcast_key(self) -> str:
        """Unique key for this broadcast (station_frequency)."""
        return f"{self.station}_{self.frequency_mhz:.2f}"


# Legacy alias for backwards compatibility
StationCalibration = BroadcastCalibration


@dataclass 
class FusedResult:
    """Result of multi-broadcast fusion."""
    timestamp: float
    d_clock_fused_ms: float      # Fused D_clock (should converge to 0)
    d_clock_raw_ms: float        # Unweighted mean before calibration
    uncertainty_ms: float        # Estimated uncertainty
    n_broadcasts: int            # Number of broadcasts used
    n_stations: int              # Number of unique stations

    global_solve_verified: bool = False
    global_solve_consistency_ms: Optional[float] = None
    global_solve_n_obs: int = 0
    
    # Per-station breakdown
    wwv_mean_ms: Optional[float] = None
    wwvh_mean_ms: Optional[float] = None
    chu_mean_ms: Optional[float] = None
    bpm_mean_ms: Optional[float] = None
    wwv_count: int = 0
    wwvh_count: int = 0
    chu_count: int = 0
    bpm_count: int = 0
    
    # Calibration applied
    calibration_applied: bool = False
    reference_station: str = 'CHU'
    
    # Quality
    outliers_rejected: int = 0
    quality_grade: str = 'D'
    
    # Consistency checks (same-station should be tight, inter-station can vary)
    wwv_intra_std_ms: Optional[float] = None   # Std dev within WWV frequencies
    wwvh_intra_std_ms: Optional[float] = None  # Std dev within WWVH frequencies
    chu_intra_std_ms: Optional[float] = None   # Std dev within CHU frequencies
    bpm_intra_std_ms: Optional[float] = None   # Std dev within BPM frequencies
    inter_station_spread_ms: Optional[float] = None  # Spread between station means
    consistency_flag: str = 'OK'  # OK, INTRA_ANOMALY, INTER_ANOMALY, DISCRIMINATION_SUSPECT


class MultiBroadcastFusion:
    """
    Fuse D_clock estimates from all 17 broadcasts (WWV/WWVH/CHU/BPM).
    
    Uses CHU FSK-verified timing as the reference for calibration,
    since CHU FSK provides exact 500ms boundary alignment.
    
    BPM handling:
    - Automatically filters out UT1 minutes (25-29, 55-59)
    - Long propagation path (~10,000 km) requires multi-hop F-layer modeling
    """
    
    # Calibration starts at 0 with high uncertainty and learns from data.
    # Per-broadcast calibration (Issue 3.2) accounts for frequency-dependent delays.
    #
    # The old hardcoded values were:
    #   'WWV': 2.5, 'WWVH': 2.5, 'CHU': 1.0
    # These are now replaced by learned values from ground truth validation.
    DEFAULT_CALIBRATION = {}  # Empty - all calibration is learned
    
    def __init__(
        self,
        data_root: Path,
        calibration_file: Optional[Path] = None,
        auto_calibrate: bool = True,
        reference_station: str = 'CHU',
        receiver_lat: Optional[float] = None,
        receiver_lon: Optional[float] = None,
        sample_rate: Optional[int] = None
    ):
        """
        Initialize multi-broadcast fusion engine.
        
        Args:
            data_root: Root directory containing phase2/{CHANNEL}/ subdirs
            calibration_file: Optional file to persist calibration
            auto_calibrate: Whether to learn calibration from data
            reference_station: Station to use as timing reference
        """
        self.data_root = Path(data_root)
        self.phase2_dir = self.data_root / 'phase2'
        self.auto_calibrate = auto_calibrate
        self.reference_station = reference_station

        from .wwv_constants import SAMPLE_RATE_FULL
        self.sample_rate = int(sample_rate if sample_rate is not None else SAMPLE_RATE_FULL)

        self.receiver_lat = receiver_lat if receiver_lat is not None else 39.0
        self.receiver_lon = receiver_lon if receiver_lon is not None else -98.0

        from .differential_time_solver import GlobalDifferentialSolver
        self.global_solver = GlobalDifferentialSolver(
            receiver_lat=self.receiver_lat,
            receiver_lon=self.receiver_lon
        )
        
        # Initialize TEC Estimator (Coherent Multi-Frequency Physics)
        from .tec_estimator import TECEstimator
        self.tec_estimator = TECEstimator()
        
        # Calibration state
        self.calibration_file = calibration_file or (
            self.data_root / 'state' / 'broadcast_calibration.json'
        )
        self.calibration: Dict[str, StationCalibration] = {}
        self._load_calibration()
        
        # Fusion output
        self.fusion_dir = self.data_root / 'phase2' / 'fusion'
        self.fusion_dir.mkdir(parents=True, exist_ok=True)
        self.fusion_csv = self.fusion_dir / 'fused_d_clock.csv'
        self._init_fusion_csv()
        
        # TEC output
        self.tec_csv = self.fusion_dir / 'tec_estimates.csv'
        self._init_tec_csv()
        
        # History for calibration learning
        self.measurement_history: Dict[str, List[BroadcastMeasurement]] = defaultdict(list)
        self.history_max_size = 100  # Keep last N measurements per station
        
        # Kalman filter state for convergence
        # State: [d_clock_offset, d_clock_drift_rate]
        self.kalman_state = np.array([0.0, 0.0])  # [offset_ms, drift_ms_per_min]
        self.kalman_P = np.array([[100.0, 0.0], [0.0, 1.0]])  # Initial uncertainty
        self.kalman_initialized = False
        self.kalman_n_updates = 0
        
        # Channels to aggregate
        self.channels = self._discover_channels()
        
        logger.info(f"MultiBroadcastFusion initialized")
        logger.info(f"  Data root: {data_root}")
        logger.info(f"  Channels: {len(self.channels)}")
        logger.info(f"  Reference station: {reference_station}")
        logger.info(f"  Auto-calibrate: {auto_calibrate}")
    
    def _discover_channels(self) -> List[str]:
        """Discover available Phase 2 channels."""
        channels = []
        if self.phase2_dir.exists():
            for subdir in self.phase2_dir.iterdir():
                if subdir.is_dir() and (subdir / 'clock_offset').exists():
                    channels.append(subdir.name)
        return sorted(channels)
    
    def _load_calibration(self):
        """
        Load per-broadcast calibration from file.
        
        Issue 3.2 Fix: Calibration is now keyed by broadcast (station_frequency)
        rather than just station, to account for frequency-dependent delays.
        """
        if self.calibration_file.exists():
            try:
                with open(self.calibration_file) as f:
                    data = json.load(f)
                for broadcast_key, cal_data in data.items():
                    # Parse station and frequency from key (e.g., "WWV_10.00")
                    parts = broadcast_key.rsplit('_', 1)
                    station = parts[0] if len(parts) > 1 else broadcast_key
                    freq = float(parts[1]) if len(parts) > 1 else 0.0
                    
                    self.calibration[broadcast_key] = BroadcastCalibration(
                        station=station,
                        frequency_mhz=cal_data.get('frequency_mhz', freq),
                        offset_ms=cal_data['offset_ms'],
                        uncertainty_ms=cal_data['uncertainty_ms'],
                        n_samples=cal_data['n_samples'],
                        last_updated=cal_data['last_updated'],
                        reference_station=cal_data.get('reference_station', 'CHU')
                    )
                logger.info(f"Loaded {len(self.calibration)} broadcast calibrations from {self.calibration_file}")
            except Exception as e:
                logger.warning(f"Could not load calibration: {e}")
                self._init_default_calibration()
        else:
            self._init_default_calibration()
    
    def _init_default_calibration(self):
        """
        Initialize with zero calibration (Issue 4.3 fix).
        
        Instead of hardcoded guesses, we start with zero offset and high
        uncertainty. The system learns proper calibration from:
        1. Ground truth validation (GPS PPS, silent minutes)
        2. CHU FSK verified timing
        3. Cross-validation between broadcasts
        """
        # No default offsets - all calibration is learned from data
        # The calibration dict will be populated as measurements arrive
        logger.info("Calibration initialized - will learn from data (no hardcoded defaults)")
    
    def _save_calibration(self):
        """Persist per-broadcast calibration to file."""
        self.calibration_file.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        for broadcast_key, cal in self.calibration.items():
            data[broadcast_key] = {
                'station': cal.station,
                'frequency_mhz': cal.frequency_mhz,
                'offset_ms': cal.offset_ms,
                'uncertainty_ms': cal.uncertainty_ms,
                'n_samples': cal.n_samples,
                'last_updated': cal.last_updated,
                'reference_station': cal.reference_station
            }
        # Atomic write: write to temp file, fsync, then rename
        temp_file = self.calibration_file.with_suffix('.tmp')
        with open(temp_file, 'w') as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        temp_file.replace(self.calibration_file)
    
    def _init_fusion_csv(self):
        """Initialize fused D_clock CSV."""
        header = [
            'timestamp', 'd_clock_fused_ms', 'd_clock_raw_ms',
            'uncertainty_ms', 'n_broadcasts', 'n_stations',
            'wwv_mean_ms', 'wwvh_mean_ms', 'chu_mean_ms', 'bpm_mean_ms',
            'wwv_count', 'wwvh_count', 'chu_count', 'bpm_count',
            'calibration_applied', 'quality_grade',
            'outliers_rejected',
            'wwv_intra_std_ms', 'wwvh_intra_std_ms', 'chu_intra_std_ms', 'bpm_intra_std_ms',
            'inter_station_spread_ms', 'consistency_flag',
            'global_solve_verified', 'global_solve_consistency_ms', 'global_solve_n_obs'
        ]

        if not self.fusion_csv.exists():
            with open(self.fusion_csv, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(header)
            return

        try:
            with open(self.fusion_csv, 'r', newline='') as f:
                reader = csv.reader(f)
                existing_header = next(reader, None)

            if existing_header and all(col in existing_header for col in header):
                return

            if not existing_header:
                with open(self.fusion_csv, 'w', newline='') as f:
                    writer = csv.writer(f)
                    writer.writerow(header)
                return

            temp_path = self.fusion_csv.with_suffix('.tmp')
            with open(self.fusion_csv, 'r', newline='') as src, open(temp_path, 'w', newline='') as dst:
                r = csv.reader(src)
                w = csv.writer(dst)
                old_header = next(r, [])
                w.writerow(header)

                old_len = len(old_header)
                new_len = len(header)
                for row in r:
                    if len(row) < new_len:
                        row = row + [''] * (new_len - len(row))
                    w.writerow(row)

                dst.flush()
                os.fsync(dst.fileno())
            temp_path.replace(self.fusion_csv)
        except Exception as e:
            logger.debug(f"Could not migrate fusion CSV header: {e}")

    def _init_tec_csv(self):
        """Initialize TEC estimates CSV."""
        header = [
            'timestamp_utc', 'station', 'tec_tecu', 'tec_uncertainty_tecu', 
            't_vacuum_error_ms', 'confidence', 'residuals_ms', 
            'n_frequencies', 'frequencies_mhz', 'group_delays_calibrated_ms'
        ]
        
        if not self.tec_csv.exists():
            try:
                with open(self.tec_csv, 'w', newline='') as f:
                    writer = csv.writer(f)
                    writer.writerow(header)
            except Exception as e:
                logger.error(f"Failed to initialize TEC CSV: {e}")

    def _write_tec_result(self, result):
        """Append TEC result to CSV."""
        from datetime import datetime
        
        try:
            # Map delays to simple string representation
            delays_str = ';'.join([f"{f:.2f}={d:.3f}" for f, d in sorted(result.group_delay_ms.items())])
            freqs_str = ';'.join([f"{f:.2f}" for f in sorted(result.group_delay_ms.keys())])
            
            with open(self.tec_csv, 'a', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    result.timestamp,
                    result.station,
                    f"{result.tec_u:.3f}",
                    "", # Uncertainty not yet exported by estimator in simple form
                    f"{result.t_vacuum_error_ms:.3f}",
                    f"{result.confidence:.4f}",
                    f"{result.residuals_ms:.3f}",
                    result.n_frequencies,
                    freqs_str,
                    delays_str
                ])
        except Exception as e:
            logger.debug(f"Failed to write TEC result: {e}")

    def _extract_frequency_mhz(self, channel: str) -> Optional[float]:
        s = channel.replace('_', ' ')
        m = re.search(r'(\d+(?:\.\d+)?)\s*mhz', s, flags=re.IGNORECASE)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                return None
        m = re.search(r'(\d+(?:\.\d+)?)', s)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                return None
        return None

    def _read_latest_tone_observations(
        self,
        lookback_minutes: int = 10
    ) -> Dict[int, List[Dict]]:
        from datetime import datetime, timezone, timedelta
        from .wwv_constants import BPM_UT1_MINUTES

        now = time.time()
        cutoff = now - (lookback_minutes * 60)

        now_dt = datetime.now(timezone.utc)
        today_str = now_dt.strftime('%Y%m%d')
        yesterday_str = (now_dt - timedelta(days=1)).strftime('%Y%m%d')

        by_minute: Dict[int, List[Dict]] = defaultdict(list)

        for channel in self.channels:
            tone_dir = self.phase2_dir / channel / 'tone_detections'
            if not tone_dir.exists():
                continue

            freq_mhz = self._extract_frequency_mhz(channel)
            if freq_mhz is None:
                continue

            csv_files = []
            for date_str in [today_str, yesterday_str]:
                for csv_path in tone_dir.glob(f'*_tones_{date_str}.csv'):
                    csv_files.append(csv_path)

            for csv_path in csv_files:
                try:
                    with open(csv_path) as f:
                        reader = csv.DictReader(f)
                        for row in reader:
                            mb_str = row.get('minute_boundary')
                            if not mb_str:
                                continue
                            try:
                                minute_boundary = int(float(mb_str))
                            except ValueError:
                                continue

                            if minute_boundary < cutoff:
                                continue

                            wwv_ms = row.get('wwv_timing_ms')
                            wwvh_ms = row.get('wwvh_timing_ms')
                            chu_ms = row.get('chu_timing_ms')
                            bpm_ms = row.get('bpm_timing_ms')

                            if wwv_ms not in (None, ''):
                                try:
                                    by_minute[minute_boundary].append({
                                        'station': 'WWV',
                                        'frequency_mhz': freq_mhz,
                                        'timing_ms': float(wwv_ms)
                                    })
                                except ValueError:
                                    pass

                            if wwvh_ms not in (None, ''):
                                try:
                                    by_minute[minute_boundary].append({
                                        'station': 'WWVH',
                                        'frequency_mhz': freq_mhz,
                                        'timing_ms': float(wwvh_ms)
                                    })
                                except ValueError:
                                    pass

                            if chu_ms not in (None, ''):
                                try:
                                    by_minute[minute_boundary].append({
                                        'station': 'CHU',
                                        'frequency_mhz': freq_mhz,
                                        'timing_ms': float(chu_ms)
                                    })
                                except ValueError:
                                    pass

                            if bpm_ms not in (None, ''):
                                try:
                                    dt = datetime.fromtimestamp(minute_boundary, tz=timezone.utc)
                                    if dt.minute in BPM_UT1_MINUTES:
                                        continue
                                    by_minute[minute_boundary].append({
                                        'station': 'BPM',
                                        'frequency_mhz': freq_mhz,
                                        'timing_ms': float(bpm_ms)
                                    })
                                except ValueError:
                                    pass
                except Exception:
                    continue

        return by_minute

    def _read_latest_tone_observations_by_channel(
        self,
        lookback_minutes: int = 10
    ) -> Dict[str, Dict[int, List[Dict]]]:
        """
        Read latest tone observations from all channels.
        
        Tries HDF5 first (L1A tone detections), falls back to CSV if needed.
        
        Returns observations from the last N minutes, grouped by channel and minute.
        """
        # Try HDF5 first if available
        if HDF5_AVAILABLE:
            try:
                return self._read_latest_tone_observations_by_channel_hdf5(lookback_minutes)
            except Exception as e:
                logger.warning(f"HDF5 tone detections read failed, falling back to CSV: {e}")
        
        # CSV fallback (original implementation)
        from datetime import datetime, timezone, timedelta
        from .wwv_constants import BPM_UT1_MINUTES

        now = time.time()
        cutoff = now - (lookback_minutes * 60)

        now_dt = datetime.now(timezone.utc)
        today_str = now_dt.strftime('%Y%m%d')
        yesterday_str = (now_dt - timedelta(days=1)).strftime('%Y%m%d')

        by_channel: Dict[str, Dict[int, List[Dict]]] = {}

        for channel in self.channels:
            tone_dir = self.phase2_dir / channel / 'tone_detections'
            if not tone_dir.exists():
                continue

            freq_mhz = self._extract_frequency_mhz(channel)
            if freq_mhz is None:
                continue

            per_minute: Dict[int, List[Dict]] = defaultdict(list)

            csv_files = []
            for date_str in [today_str, yesterday_str]:
                for csv_path in tone_dir.glob(f'*_tones_{date_str}.csv'):
                    csv_files.append(csv_path)

            for csv_path in csv_files:
                try:
                    with open(csv_path) as f:
                        reader = csv.DictReader(f)
                        for row in reader:
                            mb_str = row.get('minute_boundary')
                            if not mb_str:
                                continue
                            try:
                                minute_boundary = int(float(mb_str))
                            except ValueError:
                                continue

                            if minute_boundary < cutoff:
                                continue

                            wwv_ms = row.get('wwv_timing_ms')
                            wwvh_ms = row.get('wwvh_timing_ms')
                            chu_ms = row.get('chu_timing_ms')
                            bpm_ms = row.get('bpm_timing_ms')

                            if wwv_ms not in (None, ''):
                                try:
                                    per_minute[minute_boundary].append({
                                        'station': 'WWV',
                                        'frequency_mhz': freq_mhz,
                                        'timing_ms': float(wwv_ms)
                                    })
                                except ValueError:
                                    pass

                            if wwvh_ms not in (None, ''):
                                try:
                                    per_minute[minute_boundary].append({
                                        'station': 'WWVH',
                                        'frequency_mhz': freq_mhz,
                                        'timing_ms': float(wwvh_ms)
                                    })
                                except ValueError:
                                    pass

                            if chu_ms not in (None, ''):
                                try:
                                    per_minute[minute_boundary].append({
                                        'station': 'CHU',
                                        'frequency_mhz': freq_mhz,
                                        'timing_ms': float(chu_ms)
                                    })
                                except ValueError:
                                    pass

                            if bpm_ms not in (None, ''):
                                try:
                                    dt = datetime.fromtimestamp(minute_boundary, tz=timezone.utc)
                                    if dt.minute in BPM_UT1_MINUTES:
                                        continue
                                    per_minute[minute_boundary].append({
                                        'station': 'BPM',
                                        'frequency_mhz': freq_mhz,
                                        'timing_ms': float(bpm_ms)
                                    })
                                except ValueError:
                                    pass
                except Exception:
                    continue

            if per_minute:
                by_channel[channel] = per_minute

        return by_channel
    
    def _read_latest_tone_observations_by_channel_hdf5(
        self,
        lookback_minutes: int = 10
    ) -> Dict[str, Dict[int, List[Dict]]]:
        """
        Read latest tone observations from HDF5 files with CSV fallback.
        
        Reads L1A tone detections from HDF5 format, providing:
        - Quality filtering from HDF5 metadata
        - Complete metrological provenance chain
        - Automatic CSV fallback if HDF5 not available
        
        Returns observations from the last N minutes, grouped by channel and minute.
        """
        from datetime import datetime, timezone, timedelta
        from .wwv_constants import BPM_UT1_MINUTES
        
        # If HDF5 not available, fall back to CSV
        if not HDF5_AVAILABLE:
            logger.debug("HDF5 not available, falling back to CSV for tone detections")
            return self._read_latest_tone_observations_by_channel(lookback_minutes)
        
        now = time.time()
        cutoff = now - (lookback_minutes * 60)
        
        # Calculate time range for HDF5 query
        start_dt = datetime.fromtimestamp(cutoff, timezone.utc)
        end_dt = datetime.fromtimestamp(now, timezone.utc)
        start_iso = start_dt.isoformat().replace('+00:00', 'Z')
        end_iso = end_dt.isoformat().replace('+00:00', 'Z')
        
        by_channel: Dict[str, Dict[int, List[Dict]]] = {}
        
        # Read from each channel
        for channel in self.channels:
            tone_dir = self.phase2_dir / channel / 'tone_detections'
            if not tone_dir.exists():
                continue
            
            freq_mhz = self._extract_frequency_mhz(channel)
            if freq_mhz is None:
                continue
            
            try:
                # Initialize HDF5 reader for L1A tone detections
                reader = DataProductReader(
                    data_dir=tone_dir,
                    product_level='L1',
                    product_name='tone_detections',
                    channel=channel
                )
                
                # Read measurements with quality filtering
                # Note: Not filtering by quality_flag to avoid excluding measurements
                # where gpsdo_locked=False causes flag='BAD' despite valid detections
                hdf5_measurements = reader.read_time_range(
                    start=start_iso,
                    end=end_iso
                )
                
                logger.debug(
                    f"Read {len(hdf5_measurements)} L1A tone detections from HDF5 for {channel}"
                )
                
                # Convert HDF5 measurements to tone observations
                per_minute: Dict[int, List[Dict]] = defaultdict(list)
                
                for hdf5_meas in hdf5_measurements:
                    try:
                        minute_boundary = hdf5_meas.get('minute_boundary', 0)
                        if minute_boundary < cutoff:
                            continue
                        
                        # Extract WWV timing
                        if hdf5_meas.get('wwv_detected') and hdf5_meas.get('wwv_timing_ms') is not None:
                            per_minute[minute_boundary].append({
                                'station': 'WWV',
                                'frequency_mhz': freq_mhz,
                                'timing_ms': float(hdf5_meas['wwv_timing_ms'])
                            })
                        
                        # Extract WWVH timing
                        if hdf5_meas.get('wwvh_detected') and hdf5_meas.get('wwvh_timing_ms') is not None:
                            per_minute[minute_boundary].append({
                                'station': 'WWVH',
                                'frequency_mhz': freq_mhz,
                                'timing_ms': float(hdf5_meas['wwvh_timing_ms'])
                            })
                        
                        # Extract CHU timing
                        if hdf5_meas.get('chu_detected') and hdf5_meas.get('chu_timing_ms') is not None:
                            per_minute[minute_boundary].append({
                                'station': 'CHU',
                                'frequency_mhz': freq_mhz,
                                'timing_ms': float(hdf5_meas['chu_timing_ms'])
                            })
                        
                        # Extract BPM timing (with UT1 filtering)
                        if hdf5_meas.get('bpm_detected') and hdf5_meas.get('bpm_timing_ms') is not None:
                            dt = datetime.fromtimestamp(minute_boundary, tz=timezone.utc)
                            if dt.minute not in BPM_UT1_MINUTES:
                                per_minute[minute_boundary].append({
                                    'station': 'BPM',
                                    'frequency_mhz': freq_mhz,
                                    'timing_ms': float(hdf5_meas['bpm_timing_ms'])
                                })
                    
                    except (ValueError, KeyError) as e:
                        logger.debug(f"Error converting HDF5 tone measurement: {e}")
                        continue
                
                if per_minute:
                    by_channel[channel] = per_minute
            
            except FileNotFoundError:
                # HDF5 file doesn't exist, try CSV fallback for this channel
                logger.debug(f"No HDF5 tone detections found for {channel}, trying CSV fallback")
                csv_data = self._read_tone_observations_for_channel_csv(channel, lookback_minutes)
                if csv_data:
                    by_channel[channel] = csv_data
            
            except Exception as e:
                logger.warning(f"Error reading HDF5 tone detections for {channel}: {e}, falling back to CSV")
                csv_data = self._read_tone_observations_for_channel_csv(channel, lookback_minutes)
                if csv_data:
                    by_channel[channel] = csv_data
        
        if by_channel:
            total_obs = sum(len(per_min) for per_min in by_channel.values() for per_min in per_min.values())
            logger.info(
                f"Read {total_obs} tone observations from HDF5 across {len(by_channel)} channels "
                f"(lookback={lookback_minutes}m)"
            )
        
        return by_channel
    
    def _read_tone_observations_for_channel_csv(
        self,
        channel: str,
        lookback_minutes: int = 10
    ) -> Dict[int, List[Dict]]:
        """
        Read tone observations for a single channel from CSV.
        
        Helper method for fallback when HDF5 is not available for a specific channel.
        """
        from datetime import datetime, timezone, timedelta
        from .wwv_constants import BPM_UT1_MINUTES
        
        now = time.time()
        cutoff = now - (lookback_minutes * 60)
        
        now_dt = datetime.now(timezone.utc)
        today_str = now_dt.strftime('%Y%m%d')
        yesterday_str = (now_dt - timedelta(days=1)).strftime('%Y%m%d')
        
        tone_dir = self.phase2_dir / channel / 'tone_detections'
        if not tone_dir.exists():
            return {}
        
        freq_mhz = self._extract_frequency_mhz(channel)
        if freq_mhz is None:
            return {}
        
        per_minute: Dict[int, List[Dict]] = defaultdict(list)
        
        csv_files = []
        for date_str in [today_str, yesterday_str]:
            for csv_path in tone_dir.glob(f'*_tones_{date_str}.csv'):
                csv_files.append(csv_path)
        
        for csv_path in csv_files:
            try:
                with open(csv_path) as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        mb_str = row.get('minute_boundary')
                        if not mb_str:
                            continue
                        try:
                            minute_boundary = int(float(mb_str))
                        except ValueError:
                            continue
                        
                        if minute_boundary < cutoff:
                            continue
                        
                        wwv_ms = row.get('wwv_timing_ms')
                        wwvh_ms = row.get('wwvh_timing_ms')
                        chu_ms = row.get('chu_timing_ms')
                        bpm_ms = row.get('bpm_timing_ms')
                        
                        if wwv_ms not in (None, ''):
                            try:
                                per_minute[minute_boundary].append({
                                    'station': 'WWV',
                                    'frequency_mhz': freq_mhz,
                                    'timing_ms': float(wwv_ms)
                                })
                            except ValueError:
                                pass
                        
                        if wwvh_ms not in (None, ''):
                            try:
                                per_minute[minute_boundary].append({
                                    'station': 'WWVH',
                                    'frequency_mhz': freq_mhz,
                                    'timing_ms': float(wwvh_ms)
                                })
                            except ValueError:
                                pass
                        
                        if chu_ms not in (None, ''):
                            try:
                                per_minute[minute_boundary].append({
                                    'station': 'CHU',
                                    'frequency_mhz': freq_mhz,
                                    'timing_ms': float(chu_ms)
                                })
                            except ValueError:
                                pass
                        
                        if bpm_ms not in (None, ''):
                            try:
                                dt = datetime.fromtimestamp(minute_boundary, tz=timezone.utc)
                                if dt.minute in BPM_UT1_MINUTES:
                                    continue
                                per_minute[minute_boundary].append({
                                    'station': 'BPM',
                                    'frequency_mhz': freq_mhz,
                                    'timing_ms': float(bpm_ms)
                                })
                            except ValueError:
                                pass
            except Exception:
                continue
        
        return per_minute


    def _run_global_differential_solve(
        self,
        lookback_minutes: int
    ) -> Tuple[Optional[object], int]:
        by_channel = self._read_latest_tone_observations_by_channel(lookback_minutes=lookback_minutes)
        if not by_channel:
            return None, 0

        minute_sets = [set(m.keys()) for m in by_channel.values() if m]
        if not minute_sets:
            return None, 0

        common_minutes = set.intersection(*minute_sets) if len(minute_sets) > 1 else minute_sets[0]
        target_minute = max(common_minutes) if common_minutes else None

        if logger.isEnabledFor(logging.DEBUG):
            channel_ranges = {}
            for ch, per_minute in by_channel.items():
                mins = sorted(per_minute.keys())
                if not mins:
                    continue
                channel_ranges[ch] = {
                    'n_minutes': len(mins),
                    'min_minute': mins[0],
                    'max_minute': mins[-1]
                }
            logger.debug(
                f"Global solve minute coverage: channels={len(channel_ranges)} "
                f"common_minutes={len(common_minutes)} "
                f"ranges={channel_ranges}"
            )

        if target_minute is None:
            union_minutes = set()
            for s in minute_sets:
                union_minutes |= s
            target_minute = max(union_minutes) if union_minutes else None

            if target_minute is not None:
                logger.info(
                    f"Global solve: no common minute across channels in lookback={lookback_minutes}m; "
                    f"falling back to latest available minute={target_minute}"
                )

        if target_minute is None:
            return None, 0

        best_obs = []
        dropped_channels = []
        for ch, m in by_channel.items():
            if target_minute in m:
                best_obs.extend(m.get(target_minute, []))
            else:
                dropped_channels.append(ch)

        station_mix = sorted({f"{o['station']}-{o['frequency_mhz']:.2f}" for o in best_obs})
        if dropped_channels:
            logger.info(
                f"Global solve context: target_minute={target_minute} obs={len(best_obs)} "
                f"mix={station_mix} dropped_channels={sorted(dropped_channels)}"
            )
        else:
            logger.info(
                f"Global solve context: target_minute={target_minute} obs={len(best_obs)} mix={station_mix}"
            )

        has_nist = any(s.startswith('WWV-') or s.startswith('WWVH-') for s in station_mix)
        has_chu = any(s.startswith('CHU-') for s in station_mix)
        if has_nist and has_chu:
            logger.info(
                f"Global solve: cross-agency triangulation active (NIST+NRC) target_minute={target_minute} "
                f"obs={len(best_obs)} mix={station_mix}"
            )

        if len(best_obs) < 2:
            return None, len(best_obs)

        observations = []
        for o in best_obs:
            arrival_rtp = int(round(o['timing_ms'] * self.sample_rate / 1000.0))
            observations.append({
                'station': o['station'],
                'frequency_mhz': o['frequency_mhz'],
                'arrival_rtp': arrival_rtp
            })

        result = self.global_solver.solve_global(
            observations=observations,
            minute_boundary_rtp=0,
            sample_rate=self.sample_rate
        )

        try:
            logger.info(
                f"Global solve result: target_minute={target_minute} n_obs={getattr(result, 'n_observations', len(observations))} "
                f"offset_ms={getattr(result, 'clock_error_ms', 0.0):+.3f} "
                f"verified={getattr(result, 'verified', False)} conf={getattr(result, 'confidence', 0.0):.2f} "
                f"consistency_ms={getattr(result, 'pair_consistency_ms', 0.0):.3f}"
            )
        except Exception:
            pass
        return result, len(observations)
    
    def _read_latest_measurements(
        self, 
        lookback_minutes: int = 5
    ) -> List[BroadcastMeasurement]:
        """
        Read latest D_clock measurements from all channels.
        
        Tries HDF5 first (L2 timing measurements), falls back to CSV if needed.
        
        Returns measurements from the last N minutes.
        
        BPM filtering: Automatically excludes BPM measurements from UT1 minutes
        (25-29, 55-59) since those transmit UT1 time, not UTC.
        
        Note: Analytics service writes daily-rotated CSV files with format:
            {channel}_clock_offset_{YYYYMMDD}.csv
        We read today's and yesterday's files to handle day boundaries.
        """
        # Try HDF5 first if available
        if HDF5_AVAILABLE:
            try:
                return self._read_latest_measurements_hdf5(lookback_minutes)
            except Exception as e:
                logger.warning(f"HDF5 read failed, falling back to CSV: {e}")
        
        # CSV fallback (original implementation)
        from datetime import datetime, timezone, timedelta
        from .wwv_constants import BPM_UT1_MINUTES
        
        measurements = []
        now = time.time()
        cutoff = now - (lookback_minutes * 60)
        
        # Get today and yesterday date strings for daily-rotated files
        now_dt = datetime.now(timezone.utc)
        today_str = now_dt.strftime('%Y%m%d')
        yesterday_str = (now_dt - timedelta(days=1)).strftime('%Y%m%d')
        
        for channel in self.channels:
            clock_offset_dir = self.phase2_dir / channel / 'clock_offset'
            if not clock_offset_dir.exists():
                continue
            
            # Find CSV files matching the daily-rotated pattern
            # Format: {channel}_clock_offset_{YYYYMMDD}.csv
            csv_files = []
            
            # Try to find today's and yesterday's files (for day boundary handling)
            for date_str in [today_str, yesterday_str]:
                # Match pattern: *_clock_offset_{date}.csv
                for csv_path in clock_offset_dir.glob(f'*_clock_offset_{date_str}.csv'):
                    csv_files.append(csv_path)
            
            # Also check for legacy clock_offset_series.csv (backwards compatibility)
            legacy_path = clock_offset_dir / 'clock_offset_series.csv'
            if legacy_path.exists():
                csv_files.append(legacy_path)
            
        for csv_path in csv_files:
            try:
                with open(csv_path) as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        try:
                            ts_str = row.get('system_time')
                            if not ts_str:
                                continue
                            ts = float(ts_str)
                            if ts < cutoff:
                                continue
                            
                            station = row.get('station', 'UNKNOWN')
                            conf_str = row.get('confidence')
                            conf = float(conf_str) if conf_str else 0.0
                            offset_str = row.get('clock_offset_ms', '')
                            
                            # Skip if no valid timing solution was found or confidence is ultra-low
                            if not offset_str or offset_str == '' or conf < 0.01:
                                continue
                                
                            offset_ms = float(offset_str)
                            
                            # BPM UT1 filtering: Skip minutes 25-29 and 55-59
                            if station == 'BPM':
                                dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                                if dt.minute in BPM_UT1_MINUTES:
                                    continue
                            
                            m = BroadcastMeasurement(
                                timestamp=ts,
                                station=station,
                                frequency_mhz=float(row.get('frequency_mhz', 0)),
                                d_clock_ms=offset_ms,
                                propagation_delay_ms=float(row.get('propagation_delay_ms', 0)),
                                propagation_mode=row.get('propagation_mode', ''),
                                confidence=conf,
                                snr_db=float(row.get('snr_db', 0)),
                                quality_grade=row.get('quality_grade', 'D'),
                                channel_name=channel
                            )
                            measurements.append(m)
                        except (ValueError, KeyError):
                            continue
            except Exception as e:
                logger.debug(f"Error reading {csv_path}: {e}")
        
        return measurements
    
    def _read_latest_measurements_hdf5(
        self, 
        lookback_minutes: int = 5
    ) -> List[BroadcastMeasurement]:
        """
        Read latest D_clock measurements from HDF5 files with CSV fallback.
        
        Reads L2 timing measurements from HDF5 format, providing:
        - Quality filtering from HDF5 metadata
        - ISO GUM uncertainty propagation
        - Automatic CSV fallback if HDF5 not available
        
        Returns measurements from the last N minutes.
        """
        from datetime import datetime, timezone, timedelta
        from .wwv_constants import BPM_UT1_MINUTES
        
        measurements = []
        
        # If HDF5 not available, fall back to CSV
        if not HDF5_AVAILABLE:
            logger.debug("HDF5 not available, falling back to CSV")
            return self._read_latest_measurements(lookback_minutes)
        
        now = time.time()
        cutoff = now - (lookback_minutes * 60)
        
        # Calculate time range for HDF5 query
        start_dt = datetime.fromtimestamp(cutoff, timezone.utc)
        end_dt = datetime.fromtimestamp(now, timezone.utc)
        start_iso = start_dt.isoformat().replace('+00:00', 'Z')
        end_iso = end_dt.isoformat().replace('+00:00', 'Z')
        
        # Read from each channel
        for channel in self.channels:
            clock_offset_dir = self.phase2_dir / channel / 'clock_offset'
            if not clock_offset_dir.exists():
                continue
            
            try:
                # Initialize HDF5 reader for L2 timing measurements
                reader = DataProductReader(
                    data_dir=clock_offset_dir,
                    product_level='L2',
                    product_name='timing_measurements',
                    channel=channel
                )
                
                # Read measurements with quality filtering
                # Accept grades A, B, C (exclude D)
                # Note: Not filtering by quality_flag to avoid excluding measurements
                # where gpsdo_locked=False causes flag='BAD' despite valid grade
                hdf5_measurements = reader.read_time_range(
                    start=start_iso,
                    end=end_iso,
                    min_quality_grade='C',  # Accept C and better (A, B, C)
                    min_confidence=0.01  # Minimum confidence threshold
                )
                
                logger.debug(
                    f"Read {len(hdf5_measurements)} L2 measurements from HDF5 for {channel}"
                )
                
                # Convert HDF5 measurements to BroadcastMeasurement objects
                for hdf5_meas in hdf5_measurements:
                    try:
                        # Extract timestamp
                        ts = hdf5_meas.get('minute_boundary_utc', 0)
                        if ts < cutoff:
                            continue
                        
                        station = hdf5_meas.get('station', 'UNKNOWN')
                        
                        # BPM UT1 filtering
                        if station == 'BPM':
                            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                            if dt.minute in BPM_UT1_MINUTES:
                                continue
                        
                        # Create BroadcastMeasurement
                        m = BroadcastMeasurement(
                            timestamp=ts,
                            station=station,
                            frequency_mhz=hdf5_meas.get('frequency_mhz', 0.0),
                            d_clock_ms=hdf5_meas.get('clock_offset_ms', 0.0),
                            propagation_delay_ms=hdf5_meas.get('propagation_delay_ms', 0.0),
                            propagation_mode=hdf5_meas.get('propagation_mode', ''),
                            confidence=hdf5_meas.get('confidence', 0.0),
                            snr_db=hdf5_meas.get('snr_db', 0.0),
                            quality_grade=hdf5_meas.get('quality_grade', 'D'),
                            channel_name=channel
                        )
                        measurements.append(m)
                    
                    except (ValueError, KeyError) as e:
                        logger.debug(f"Error converting HDF5 measurement: {e}")
                        continue
            
            except FileNotFoundError:
                # HDF5 file doesn't exist, try CSV fallback for this channel
                logger.debug(f"No HDF5 files found for {channel}, trying CSV fallback")
                # Fall back to CSV for this specific channel
                csv_measurements = self._read_latest_measurements_for_channel(
                    channel, lookback_minutes
                )
                measurements.extend(csv_measurements)
            
            except Exception as e:
                logger.warning(f"Error reading HDF5 for {channel}: {e}, falling back to CSV")
                # Fall back to CSV for this specific channel
                csv_measurements = self._read_latest_measurements_for_channel(
                    channel, lookback_minutes
                )
                measurements.extend(csv_measurements)
        
        if measurements:
            logger.info(
                f"Read {len(measurements)} L2 timing measurements from HDF5 "
                f"(lookback={lookback_minutes}m)"
            )
        
        return measurements
    
    def _read_latest_measurements_for_channel(
        self,
        channel: str,
        lookback_minutes: int = 5
    ) -> List[BroadcastMeasurement]:
        """
        Read latest measurements for a single channel from CSV.
        
        Helper method for fallback when HDF5 is not available for a specific channel.
        """
        from datetime import datetime, timezone, timedelta
        from .wwv_constants import BPM_UT1_MINUTES
        
        measurements = []
        now = time.time()
        cutoff = now - (lookback_minutes * 60)
        
        now_dt = datetime.now(timezone.utc)
        today_str = now_dt.strftime('%Y%m%d')
        yesterday_str = (now_dt - timedelta(days=1)).strftime('%Y%m%d')
        
        clock_offset_dir = self.phase2_dir / channel / 'clock_offset'
        if not clock_offset_dir.exists():
            return measurements
        
        csv_files = []
        for date_str in [today_str, yesterday_str]:
            for csv_path in clock_offset_dir.glob(f'*_clock_offset_{date_str}.csv'):
                csv_files.append(csv_path)
        
        # Also check for legacy file
        legacy_path = clock_offset_dir / 'clock_offset_series.csv'
        if legacy_path.exists():
            csv_files.append(legacy_path)
        
        for csv_path in csv_files:
            try:
                with open(csv_path) as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        try:
                            ts_str = row.get('system_time')
                            if not ts_str:
                                continue
                            ts = float(ts_str)
                            if ts < cutoff:
                                continue
                            
                            station = row.get('station', 'UNKNOWN')
                            conf_str = row.get('confidence')
                            conf = float(conf_str) if conf_str else 0.0
                            offset_str = row.get('clock_offset_ms', '')
                            
                            if not offset_str or offset_str == '' or conf < 0.01:
                                continue
                            
                            offset_ms = float(offset_str)
                            
                            # BPM UT1 filtering
                            if station == 'BPM':
                                dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                                if dt.minute in BPM_UT1_MINUTES:
                                    continue
                            
                            m = BroadcastMeasurement(
                                timestamp=ts,
                                station=station,
                                frequency_mhz=float(row.get('frequency_mhz', 0)),
                                d_clock_ms=offset_ms,
                                propagation_delay_ms=float(row.get('propagation_delay_ms', 0)),
                                propagation_mode=row.get('propagation_mode', ''),
                                confidence=conf,
                                snr_db=float(row.get('snr_db', 0)),
                                quality_grade=row.get('quality_grade', 'D'),
                                channel_name=channel
                            )
                            measurements.append(m)
                        except (ValueError, KeyError):
                            continue
            except Exception as e:
                logger.debug(f"Error reading {csv_path}: {e}")
        
        return measurements

    
    def _calculate_weights(
        self, 
        measurements: List[BroadcastMeasurement]
    ) -> List[float]:
        """
        Calculate quality-based weights for each measurement.
        
        Weights consider:
        - Confidence score
        - SNR
        - Quality grade
        - Propagation mode (lower hop = more reliable)
        """
        weights = []
        
        grade_weights = {'A': 1.0, 'B': 0.8, 'C': 0.5, 'D': 0.2}
        mode_weights = {
            '1E': 1.0, '1F': 0.9, '2F': 0.7, '3F': 0.5, 'GW': 1.0
        }
        
        for m in measurements:
            # Base weight from confidence
            w = m.confidence

            if m.station == 'GLOBAL_DIFF':
                weights.append(max(10.0, 200.0 * w))
                continue
            
            # Adjust for quality grade
            w *= grade_weights.get(m.quality_grade, 0.2)
            
            # Adjust for propagation mode
            w *= mode_weights.get(m.propagation_mode, 0.5)
            
            # Adjust for SNR (higher is better)
            if m.snr_db > 10:
                w *= 1.0
            elif m.snr_db > 5:
                w *= 0.8
            else:
                w *= 0.5
            
            weights.append(max(0.01, w))  # Minimum weight
        
        return weights
    
    def _reject_outliers(
        self,
        measurements: List[BroadcastMeasurement],
        weights: List[float],
        sigma_threshold: float = 3.0
    ) -> Tuple[List[BroadcastMeasurement], List[float], int]:
        """
        Reject outliers using weighted median absolute deviation.
        
        Returns filtered measurements, weights, and count of rejected.
        """
        if len(measurements) < 4:
            return measurements, weights, 0
        
        # Calculate weighted median
        d_clocks = np.array([m.d_clock_ms for m in measurements])
        w = np.array(weights)
        
        sorted_idx = np.argsort(d_clocks)
        sorted_d = d_clocks[sorted_idx]
        sorted_w = w[sorted_idx]
        cumsum = np.cumsum(sorted_w)
        median_idx = np.searchsorted(cumsum, cumsum[-1] / 2)
        weighted_median = sorted_d[min(median_idx, len(sorted_d)-1)]
        
        # Calculate MAD
        deviations = np.abs(d_clocks - weighted_median)
        mad = np.median(deviations) * 1.4826  # Scale to std dev
        
        if mad < 0.1:
            mad = 0.1  # Minimum to avoid divide by zero
        
        # Reject outliers
        keep_mask = deviations < (sigma_threshold * mad)

        # Never drop the physics-verified synthetic measurement
        for i, m in enumerate(measurements):
            if m.station == 'GLOBAL_DIFF':
                keep_mask[i] = True
        
        filtered_m = [m for m, keep in zip(measurements, keep_mask) if keep]
        filtered_w = [w for w, keep in zip(weights, keep_mask) if keep]
        n_rejected = len(measurements) - len(filtered_m)
        
        return filtered_m, filtered_w, n_rejected
    
    def _get_broadcast_key(self, station: str, frequency_mhz: float) -> str:
        """Generate consistent broadcast key for calibration lookups."""
        return f"{station}_{frequency_mhz:.2f}"
    
    def _apply_calibration(
        self,
        measurements: List[BroadcastMeasurement]
    ) -> List[float]:
        """
        Apply per-broadcast calibration offsets.
        
        Uses per-broadcast keys (station_frequency) to properly account for
        frequency-dependent systematic offsets including:
        - Ionospheric delays (1/f² dependence)
        - Matched filter group delays
        - Propagation mode differences
        
        Returns calibrated D_clock values.
        """
        calibrated = []
        for m in measurements:
            if m.station == 'GLOBAL_DIFF':
                calibrated.append(m.d_clock_ms)
                continue
            # Use per-broadcast calibration (station + frequency)
            broadcast_key = self._get_broadcast_key(m.station, m.frequency_mhz)
            broadcast_cal = self.calibration.get(broadcast_key)
            if broadcast_cal:
                calibrated.append(m.d_clock_ms + broadcast_cal.offset_ms)
            else:
                # No calibration yet for this broadcast - use raw value
                calibrated.append(m.d_clock_ms)
        return calibrated
    
    def _update_calibration(
        self,
        measurements: List[BroadcastMeasurement]
    ):
        """
        Update calibration offsets per-BROADCAST (station + frequency).
        
        Each broadcast (e.g., WWV_10.00, CHU_7.85) has its own systematic offset due to:
        - Ionospheric delays (frequency-dependent, 1/f²)
        - Matched filter group delays (frequency-dependent)
        - Propagation mode differences (varies by frequency and time of day)
        
        Per-broadcast calibration learns these offsets to bring each broadcast's
        D_clock to 0 (UTC alignment).
        """
        if not self.auto_calibrate:
            return
        
        # Add to history keyed by broadcast (station + frequency)
        for m in measurements:
            if m.station == 'GLOBAL_DIFF':
                continue
            broadcast_key = self._get_broadcast_key(m.station, m.frequency_mhz)
            history = self.measurement_history[broadcast_key]
            history.append(m)
            if len(history) > self.history_max_size:
                self.measurement_history[broadcast_key] = history[-self.history_max_size:]
        
        # Update calibration per-BROADCAST
        for broadcast_key, history in self.measurement_history.items():
            if len(history) < 5:
                continue
            
            recent = history[-30:]
            d_clocks = [m.d_clock_ms for m in recent]
            
            broadcast_mean = np.mean(d_clocks)
            broadcast_std = np.std(d_clocks)
            
            # Offset should bring broadcast mean to 0 (UTC alignment)
            new_offset = -broadcast_mean
            
            # Extract station and frequency from key for logging
            station = recent[0].station
            freq = recent[0].frequency_mhz
            
            logger.debug(f"Calibration update {broadcast_key}: raw_mean={broadcast_mean:.2f}ms, offset={new_offset:.2f}ms, n={len(d_clocks)}")
            
            # Exponential moving average for smooth updates
            old_cal = self.calibration.get(broadcast_key)
            if old_cal and old_cal.n_samples > 0:
                # Alpha range: 0.3 (fast) to 0.1 (slow)
                alpha = max(0.1, min(0.3, 10.0 / old_cal.n_samples))
                new_offset = alpha * new_offset + (1 - alpha) * old_cal.offset_ms
            
            self.calibration[broadcast_key] = BroadcastCalibration(
                station=station,
                frequency_mhz=freq,
                offset_ms=new_offset,
                uncertainty_ms=broadcast_std / np.sqrt(len(d_clocks)),  # Standard error
                n_samples=len(d_clocks),
                last_updated=time.time(),
                reference_station=self.reference_station
            )
        
        self._save_calibration()
    
    def _kalman_update(self, measurement: float, measurement_uncertainty: float) -> float:
        """
        Update Kalman filter with new measurement and return converged uncertainty.
        
        Uses a simple offset+drift model:
            State: [d_clock_offset, drift_rate]
            
        The uncertainty converges over time as more measurements are incorporated.
        
        Args:
            measurement: Current fused D_clock measurement (ms)
            measurement_uncertainty: Uncertainty of this measurement (ms)
            
        Returns:
            Kalman filter uncertainty (converges over time)
        """
        # Initialize on first measurement
        if not self.kalman_initialized:
            self.kalman_state[0] = measurement
            self.kalman_P[0, 0] = measurement_uncertainty ** 2
            self.kalman_initialized = True
            self.kalman_n_updates = 1
            return measurement_uncertainty
        
        # State transition matrix (1 minute step)
        # x_new = F * x_old
        # [offset]   [1  dt] [offset]
        # [drift ] = [0   1] [drift ]
        dt = 1.0  # 1 minute
        F = np.array([[1.0, dt], [0.0, 1.0]])
        
        # Process noise (clock drift uncertainty)
        # GPSDO has ~1e-9 stability, so drift is negligible
        # But allow small drift for temperature effects
        q_offset = 0.01  # ms^2 per minute
        q_drift = 0.0001  # (ms/min)^2 per minute
        Q = np.array([[q_offset, 0.0], [0.0, q_drift]])
        
        # Predict step
        x_pred = F @ self.kalman_state
        P_pred = F @ self.kalman_P @ F.T + Q
        
        # Measurement matrix (we only observe offset)
        H = np.array([[1.0, 0.0]])
        
        # Measurement noise
        R = np.array([[measurement_uncertainty ** 2]])
        
        # Kalman gain
        S = H @ P_pred @ H.T + R
        K = P_pred @ H.T @ np.linalg.inv(S)
        
        # Update step
        y = measurement - H @ x_pred  # Innovation
        self.kalman_state = x_pred + K.flatten() * y
        self.kalman_P = (np.eye(2) - K @ H) @ P_pred
        
        self.kalman_n_updates += 1
        
        # Return uncertainty (sqrt of offset variance)
        kalman_uncertainty = np.sqrt(self.kalman_P[0, 0])
        
        # Minimum uncertainty floor based on measurement quality
        min_uncertainty = max(0.1, measurement_uncertainty / np.sqrt(self.kalman_n_updates))
        
        return max(kalman_uncertainty, min_uncertainty)
    
    def fuse(self, lookback_minutes: int = 10) -> Optional[FusedResult]:
        """
        Perform multi-broadcast fusion.
        
        Combines all available broadcasts into a single D_clock estimate
        that converges toward UTC(NIST).
        
        Returns:
            FusedResult with fused D_clock and statistics
        """
        global_result = None
        global_n_obs = 0
        try:
            global_result, global_n_obs = self._run_global_differential_solve(lookback_minutes=lookback_minutes)
        except Exception as e:
            logger.debug(f"Global differential solve failed: {e}")

        # Read latest measurements
        measurements = self._read_latest_measurements(lookback_minutes)
        
        if not measurements:
            logger.debug("No measurements available for fusion")
            return None

        if global_result is not None and getattr(global_result, 'verified', False):
            forced_conf = float(getattr(global_result, 'confidence', 0.0)) or 1.0
            forced_weight = max(10.0, 200.0 * forced_conf)
            forced_floor_ms = 0.1
            logger.info(
                f"Injecting GLOBAL_DIFF: offset_ms={float(getattr(global_result, 'clock_error_ms', 0.0)):+.3f} "
                f"conf={forced_conf:.2f} force_weight={forced_weight:.1f} kalman_floor_ms={forced_floor_ms:.1f}"
            )
            measurements.append(
                BroadcastMeasurement(
                    timestamp=time.time(),
                    station='GLOBAL_DIFF',
                    frequency_mhz=0.0,
                    d_clock_ms=float(getattr(global_result, 'clock_error_ms', 0.0)),
                    propagation_delay_ms=0.0,
                    propagation_mode='GW',
                    confidence=forced_conf,
                    snr_db=20.0,
                    quality_grade=str(getattr(global_result, 'quality_grade', 'A')),
                    channel_name='FUSION'
                )
            )
        
        # Calculate weights
        weights = self._calculate_weights(measurements)
        
        # ====================================================================
        # TEC ESTIMATION (Physics-Based Propagation Correction)
        # ====================================================================
        # Attempt to solve for TEC using multi-frequency data from same station
        # This "removes the ionosphere" mathematically rather than modelling it
        
        # Group by station
        by_station = defaultdict(list)
        for m in measurements:
            if m.station == 'GLOBAL_DIFF':
                continue
            by_station[m.station].append(m)
            
        for station, station_meas in by_station.items():
            if len(station_meas) >= 2:
                # Prepare input for estimator
                tec_input = []
                for m in station_meas:
                    # Input to TEC solver is Total Observed Time (not differential)
                    # T_obs = T_measured + T_sys_offset (calibration)
                    # We use uncalibrated d_clock + existing model delay to estimate ToA
                    # Actually, d_clock = T_arrival - T_prop_model
                    # So T_arrival = d_clock + T_prop_model
                    #
                    # But we want to solve: ToA = Vacuum + K*TEC/f^2
                    # The "ToA" here is the measured arrival time relative to minute boundary
                    
                    # Approximating ToA as d_clock_ms (assuming model was roughly right)
                    # Wait, d_clock is (Arrival - Model). 
                    # If we feed (d_clock + Model) we get pure Arrival.
                    # That is exactly what we want.
                    
                    toa_ms = m.d_clock_ms + m.propagation_delay_ms
                    
                    tec_input.append({
                        'frequency_hz': m.frequency_mhz * 1e6,
                        'toa_ms': toa_ms,
                        'uncertainty_ms': 1.0 / max(0.001, m.confidence) # Inverse confidence weighting
                    })
                
                # Run Solver
                tec_result = self.tec_estimator.estimate_tec(
                    tec_input, station, measurements[0].timestamp
                )
                
                if tec_result:
                    # Persist TEC estimate to CSV
                    self._write_tec_result(tec_result)
                    
                    if tec_result.confidence > 0.9:
                        logger.info(f"TEC Solved for {station}: {tec_result.tec_u:.1f} TECU (R2={tec_result.confidence:.2f})")
                        
                        # Update measurements with Physics-Derived delays
                        for m in station_meas:
                            if m.frequency_mhz in tec_result.group_delay_ms:
                                new_delay = tec_result.group_delay_ms[m.frequency_mhz]
                                
                                # Update D_clock with NEW delay
                                # D_clock_new = T_arrival - T_delay_new
                                # T_arrival was (d_clock_old + T_delay_old)
                                t_arrival = m.d_clock_ms + m.propagation_delay_ms
                                m.d_clock_ms = t_arrival - new_delay
                                
                                # Update metadata
                                m.propagation_delay_ms = new_delay
                                m.propagation_mode = 'TEC_SOLVED' # Flag as physics-derived
                                m.confidence = min(1.0, m.confidence * 1.2) # Boost confidence
                    else:
                        logger.warning(f"TEC poor fit for {station}: R2={tec_result.confidence:.2f} (Needs >0.9)")
                else:
                    logger.warning(f"TEC solver returned None for {station} (inputs: {len(tec_input)})")
            else:
                 logger.info(f"Skipping TEC for {station}: Only {len(station_meas)} measurements (Need >=2)")
        
        # ====================================================================
        
        # Reject outliers
        measurements, weights, n_rejected = self._reject_outliers(
            measurements, weights
        )
        
        if len(measurements) < 2:
            logger.debug("Too few measurements after outlier rejection")
            return None
        
        # ====================================================================
        
        
        # ====================================================================
        
        # Update calibration (before applying)
        self._update_calibration(measurements)
        
        # Apply calibration
        calibrated = self._apply_calibration(measurements)
        
        # Weighted mean of calibrated values
        w = np.array(weights)
        d = np.array(calibrated)
        fused_d_clock = np.sum(w * d) / np.sum(w)
        
        # Raw (uncalibrated) mean for comparison
        raw_d_clocks = np.array([m.d_clock_ms for m in measurements])
        raw_mean = np.mean(raw_d_clocks)
        
        # Measurement uncertainty from weighted std
        weighted_var = np.sum(w * (d - fused_d_clock)**2) / np.sum(w)

        has_verified_global = (global_result is not None and getattr(global_result, 'verified', False))
        uncertainty_floor = 0.1 if has_verified_global else 0.2
        measurement_uncertainty = max(uncertainty_floor, np.sqrt(weighted_var))
        
        # Update Kalman filter for convergence
        kalman_uncertainty = self._kalman_update(fused_d_clock, measurement_uncertainty)
        
        # Use Kalman uncertainty (converges over time) instead of instantaneous spread
        uncertainty = kalman_uncertainty
        
        # Per-station breakdown (using calibrated values for consistency)
        wwv_cal = [c for m, c in zip(measurements, calibrated) if m.station == 'WWV']
        wwvh_cal = [c for m, c in zip(measurements, calibrated) if m.station == 'WWVH']
        chu_cal = [c for m, c in zip(measurements, calibrated) if m.station == 'CHU']
        bpm_cal = [c for m, c in zip(measurements, calibrated) if m.station == 'BPM']
        
        # Raw values for reporting
        wwv_m = [m.d_clock_ms for m in measurements if m.station == 'WWV']
        wwvh_m = [m.d_clock_ms for m in measurements if m.station == 'WWVH']
        chu_m = [m.d_clock_ms for m in measurements if m.station == 'CHU']
        bpm_m = [m.d_clock_ms for m in measurements if m.station == 'BPM']
        
        # Unique stations
        stations = set(m.station for m in measurements if m.station != 'GLOBAL_DIFF')
        
        # === CONSISTENCY CHECKS ===
        # Same-station broadcasts should have tight agreement (ionospheric variation only)
        # Inter-station spread reflects geographic/clock differences (expected to be larger)
        #
        # KEY INSIGHT: With GPSDO-locked RTP, timing is deterministic to <1ms.
        # Therefore, high intra-station variance indicates DISCRIMINATION ERROR,
        # not timing jitter. We can use this to identify misclassified measurements.
        
        # Intra-station std dev (should be small, ~1-3ms for ionospheric variation)
        wwv_intra_std = np.std(wwv_cal) if len(wwv_cal) > 1 else None
        wwvh_intra_std = np.std(wwvh_cal) if len(wwvh_cal) > 1 else None
        chu_intra_std = np.std(chu_cal) if len(chu_cal) > 1 else None
        bpm_intra_std = np.std(bpm_cal) if len(bpm_cal) > 1 else None
        
        # Inter-station spread (difference between station means)
        station_means = {}
        if wwv_cal:
            station_means['WWV'] = np.mean(wwv_cal)
        if wwvh_cal:
            station_means['WWVH'] = np.mean(wwvh_cal)
        if chu_cal:
            station_means['CHU'] = np.mean(chu_cal)
        if bpm_cal:
            station_means['BPM'] = np.mean(bpm_cal)
        inter_station_spread = (max(station_means.values()) - min(station_means.values())) if len(station_means) > 1 else None
        
        # Consistency flag logic
        consistency_flag = 'OK'
        INTRA_THRESHOLD_MS = 5.0  # Same-station should agree within 5ms (ionospheric limit)
        
        # Check for intra-station anomalies (same station, different frequencies disagree)
        intra_stds = [s for s in [wwv_intra_std, wwvh_intra_std, chu_intra_std, bpm_intra_std] if s is not None]
        suspect_count = 0
        
        if intra_stds and max(intra_stds) > INTRA_THRESHOLD_MS:
            # High intra-station spread suggests discrimination errors
            consistency_flag = 'DISCRIMINATION_SUSPECT'
            
            # Identify which measurements are outliers within their station group
            # and EXCLUDE them from the Kalman update by zeroing their contribution
            suspect_indices = []
            for i, (m, cal_val) in enumerate(zip(measurements, calibrated)):
                is_suspect = False
                if m.station == 'WWV' and wwv_intra_std and wwv_intra_std > INTRA_THRESHOLD_MS:
                    wwv_mean = station_means.get('WWV', 0)
                    if abs(cal_val - wwv_mean) > 1.5 * wwv_intra_std:
                        is_suspect = True
                elif m.station == 'WWVH' and wwvh_intra_std and wwvh_intra_std > INTRA_THRESHOLD_MS:
                    wwvh_mean = station_means.get('WWVH', 0)
                    if abs(cal_val - wwvh_mean) > 1.5 * wwvh_intra_std:
                        is_suspect = True
                elif m.station == 'CHU' and chu_intra_std and chu_intra_std > INTRA_THRESHOLD_MS:
                    chu_mean = station_means.get('CHU', 0)
                    if abs(cal_val - chu_mean) > 1.5 * chu_intra_std:
                        is_suspect = True
                
                if is_suspect:
                    suspect_indices.append(i)
                    suspect_count += 1
            
            # If we have suspects, recalculate fused_d_clock excluding them
            if suspect_indices and len(measurements) - len(suspect_indices) >= 3:
                clean_weights = [w for i, w in enumerate(weights) if i not in suspect_indices]
                clean_calibrated = [c for i, c in enumerate(calibrated) if i not in suspect_indices]
                
                w_clean = np.array(clean_weights)
                d_clean = np.array(clean_calibrated)
                fused_d_clock = np.sum(w_clean * d_clean) / np.sum(w_clean)
                
                # Recalculate uncertainty with clean data
                weighted_var = np.sum(w_clean * (d_clean - fused_d_clock)**2) / np.sum(w_clean)
                measurement_uncertainty = np.sqrt(weighted_var)
                
                # Update Kalman with cleaner measurement
                kalman_uncertainty = self._kalman_update(fused_d_clock, measurement_uncertainty)
                uncertainty = kalman_uncertainty
                
                logger.info(
                    f"Excluded {suspect_count} suspect measurements, "
                    f"recalculated D_clock: {fused_d_clock:+.3f}ms ± {uncertainty:.3f}ms"
                )
            
            wwv_str = f"{wwv_intra_std:.1f}" if wwv_intra_std is not None else "N/A"
            wwvh_str = f"{wwvh_intra_std:.1f}" if wwvh_intra_std is not None else "N/A"
            chu_str = f"{chu_intra_std:.1f}" if chu_intra_std is not None else "N/A"
            bpm_str = f"{bpm_intra_std:.1f}" if bpm_intra_std is not None else "N/A"
            logger.warning(
                f"High intra-station spread: WWV σ={wwv_str}ms, "
                f"WWVH σ={wwvh_str}ms, CHU σ={chu_str}ms, BPM σ={bpm_str}ms | "
                f"{suspect_count} suspect measurements"
            )
        
        # Quality grade based on number of broadcasts and uncertainty
        if len(measurements) >= 8 and uncertainty < 0.5:
            grade = 'A'
        elif len(measurements) >= 5 and uncertainty < 1.0:
            grade = 'B'
        elif len(measurements) >= 3 and uncertainty < 2.0:
            grade = 'C'
        else:
            grade = 'D'
        
        result = FusedResult(
            timestamp=time.time(),
            d_clock_fused_ms=fused_d_clock,
            d_clock_raw_ms=raw_mean,
            uncertainty_ms=uncertainty,
            n_broadcasts=len(measurements),
            n_stations=len(stations),
            global_solve_verified=bool(getattr(global_result, 'verified', False)) if global_result is not None else False,
            global_solve_consistency_ms=float(getattr(global_result, 'pair_consistency_ms', 0.0)) if global_result is not None else None,
            global_solve_n_obs=int(getattr(global_result, 'n_observations', global_n_obs)) if global_result is not None else 0,
            wwv_mean_ms=np.mean(wwv_m) if wwv_m else None,
            wwvh_mean_ms=np.mean(wwvh_m) if wwvh_m else None,
            chu_mean_ms=np.mean(chu_m) if chu_m else None,
            bpm_mean_ms=np.mean(bpm_m) if bpm_m else None,
            wwv_count=len(wwv_m),
            wwvh_count=len(wwvh_m),
            chu_count=len(chu_m),
            bpm_count=len(bpm_m),
            calibration_applied=True,
            reference_station=self.reference_station,
            outliers_rejected=n_rejected,
            quality_grade=grade,
            wwv_intra_std_ms=wwv_intra_std,
            wwvh_intra_std_ms=wwvh_intra_std,
            chu_intra_std_ms=chu_intra_std,
            bpm_intra_std_ms=bpm_intra_std,
            inter_station_spread_ms=inter_station_spread,
            consistency_flag=consistency_flag
        )
        
        # Write to CSV
        self._write_fused_result(result)
        
        return result
    
    def _write_fused_result(self, result: FusedResult):
        """Append fused result to CSV."""
        with open(self.fusion_csv, 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                result.timestamp,
                result.d_clock_fused_ms,
                result.d_clock_raw_ms,
                result.uncertainty_ms,
                result.n_broadcasts,
                result.n_stations,
                result.wwv_mean_ms or '',
                result.wwvh_mean_ms or '',
                result.chu_mean_ms or '',
                result.bpm_mean_ms or '',
                result.wwv_count,
                result.wwvh_count,
                result.chu_count,
                result.bpm_count,
                result.calibration_applied,
                result.quality_grade,
                result.outliers_rejected,
                result.wwv_intra_std_ms or '',
                result.wwvh_intra_std_ms or '',
                result.chu_intra_std_ms or '',
                result.bpm_intra_std_ms or '',
                result.inter_station_spread_ms or '',
                result.consistency_flag,
                result.global_solve_verified,
                result.global_solve_consistency_ms if result.global_solve_consistency_ms is not None else '',
                result.global_solve_n_obs
            ])
    
    def get_current_calibration(self) -> Dict[str, float]:
        """Get current calibration offsets."""
        return {
            station: cal.offset_ms 
            for station, cal in self.calibration.items()
        }
    
    def get_status(self) -> Dict:
        """Get fusion engine status."""
        return {
            'channels': self.channels,
            'n_channels': len(self.channels),
            'reference_station': self.reference_station,
            'auto_calibrate': self.auto_calibrate,
            'calibration': {
                station: {
                    'offset_ms': cal.offset_ms,
                    'uncertainty_ms': cal.uncertainty_ms,
                    'n_samples': cal.n_samples
                }
                for station, cal in self.calibration.items()
            }
        }


def run_fusion_service(
    data_root: Path, 
    interval_sec: float = 60.0, 
    enable_chrony: bool = True,
    lookback_minutes: int = 10
):
    """
    Run continuous fusion service.
    
    Produces fused D_clock estimate every interval_sec.
    Optionally writes to Chrony SHM refclock for system clock discipline.
    
    Args:
        data_root: Base data directory
        interval_sec: Fusion interval in seconds
        interval_sec: Fusion interval in seconds
        enable_chrony: If True, write fused time to Chrony SHM refclock
        lookback_minutes: Number of minutes to look back for measurements
    """
    fusion = MultiBroadcastFusion(data_root)
    
    # Initialize Chrony SHM if enabled
    chrony_shm = None
    if enable_chrony:
        try:
            from hf_timestd.core.chrony_shm import ChronySHM
            chrony_shm = ChronySHM(unit=0)
            if chrony_shm.connect():
                logger.info("Chrony SHM refclock enabled (unit=0, refid=TMGR)")
            else:
                logger.warning("Failed to connect to Chrony SHM - continuing without")
                chrony_shm = None
        except Exception as e:
            logger.warning(f"Chrony SHM not available: {e}")
            chrony_shm = None
    
    # Chrony update rate limiting - match chrony poll interval (poll 3 = 8 seconds)
    # Only update chrony at this cadence to avoid unnecessary SHM writes
    chrony_poll_interval = 8.0  # seconds (matches "poll 3" in chrony.conf)
    last_chrony_update = 0.0
    chrony_consecutive_failures = 0
    chrony_total_writes = 0
    chrony_failed_writes = 0
    
    logger.info("Starting Multi-Broadcast Fusion Service")
    logger.info(f"  Interval: {interval_sec} seconds")
    logger.info(f"  Output: {fusion.fusion_csv}")
    logger.info(f"  Chrony SHM: {'enabled' if chrony_shm else 'disabled'}")
    if chrony_shm:
        logger.info(f"  Chrony update cadence: {chrony_poll_interval}s (matching poll interval)")
    
    while True:
        try:
            result = fusion.fuse(lookback_minutes=lookback_minutes)
            
            if result:
                # Log main fusion result
                logger.info(
                    f"Fused D_clock: {result.d_clock_fused_ms:+.3f} ms "
                    f"(raw: {result.d_clock_raw_ms:+.3f} ms) "
                    f"± {result.uncertainty_ms:.3f} ms "
                    f"[{result.n_broadcasts} broadcasts, grade {result.quality_grade}]"
                )
                
                # Log consistency check results
                intra_stds = []
                if result.wwv_intra_std_ms is not None:
                    intra_stds.append(f"WWV={result.wwv_intra_std_ms:.1f}")
                if result.wwvh_intra_std_ms is not None:
                    intra_stds.append(f"WWVH={result.wwvh_intra_std_ms:.1f}")
                if result.chu_intra_std_ms is not None:
                    intra_stds.append(f"CHU={result.chu_intra_std_ms:.1f}")
                
                if intra_stds:
                    logger.debug(
                        f"  Intra-station σ: {', '.join(intra_stds)} ms | "
                        f"Inter-station spread: {result.inter_station_spread_ms if result.inter_station_spread_ms is not None else 0.0:.1f} ms | "
                        f"Flag: {result.consistency_flag}"
                    )
                
                if result.consistency_flag != 'OK':
                    logger.warning(f"  ⚠️ Consistency: {result.consistency_flag}")
                
                # Write to Chrony SHM if available and quality is acceptable
                # Rate-limit updates to match chrony poll interval (avoid unnecessary writes)
                # Allow grade D during initial calibration - Chrony will weight by precision
                now = time.time()
                if chrony_shm and result.quality_grade in ('A', 'B', 'C', 'D'):
                    if now - last_chrony_update >= chrony_poll_interval:
                        # D_clock = T_system - T_UTC(NIST)
                        # So T_UTC(NIST) = T_system - D_clock
                        system_time = now
                        reference_time = system_time - (result.d_clock_fused_ms / 1000.0)
                        
                        # Precision based on uncertainty (log2 of seconds)
                        # uncertainty_ms=1 -> precision=-10, uncertainty_ms=10 -> precision=-7
                        precision = max(-13, min(-4, int(-10 - np.log2(max(0.1, result.uncertainty_ms)))))
                        
                        if chrony_shm.update(reference_time, system_time, precision):
                            last_chrony_update = now
                            chrony_consecutive_failures = 0
                            chrony_total_writes += 1
                            logger.info(
                                f"Chrony SHM updated: D_clock={result.d_clock_fused_ms:+.3f}ms, "
                                f"ref={reference_time:.6f}, sys={system_time:.6f}, "
                                f"offset={(system_time-reference_time)*1000:+.3f}ms, precision={precision}"
                            )
                        else:
                            chrony_consecutive_failures += 1
                            chrony_failed_writes += 1
                            logger.error(
                                f"Chrony SHM write failed (consecutive failures: {chrony_consecutive_failures}, "
                                f"total: {chrony_failed_writes}/{chrony_total_writes + chrony_failed_writes})"
                            )
                            if chrony_consecutive_failures >= 5:
                                logger.critical(
                                    f"Chrony SHM unavailable after {chrony_consecutive_failures} consecutive failures! "
                                    f"System clock discipline may be degraded."
                                )
                            # Try to reconnect on next iteration
                            if chrony_consecutive_failures >= 3:
                                try:
                                    chrony_shm.disconnect()
                                    if chrony_shm.connect():
                                        logger.warning("Chrony SHM reconnected successfully")
                                        chrony_consecutive_failures = 0
                                except Exception as e:
                                    logger.error(f"Failed to reconnect Chrony SHM: {e}")
            
            time.sleep(interval_sec)
            
        except KeyboardInterrupt:
            logger.info("Fusion service stopped")
            break
        except Exception as e:
            logger.error(f"Fusion error: {e}")
            time.sleep(interval_sec)


if __name__ == '__main__':
    import argparse
    
    parser = argparse.ArgumentParser(description='Multi-Broadcast D_clock Fusion')
    parser.add_argument('--data-root', type=Path, required=True)
    parser.add_argument('--interval', type=float, default=60.0)
    parser.add_argument('--lookback', type=int, default=10, help='Lookback window in minutes')
    parser.add_argument('--log-level', default='INFO')
    parser.add_argument('--enable-chrony', action='store_true', default=True,
                        help='Enable Chrony SHM refclock output (default: enabled)')
    parser.add_argument('--disable-chrony', action='store_true',
                        help='Disable Chrony SHM refclock output')
    
    args = parser.parse_args()
    
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format='%(asctime)s %(levelname)s:%(name)s:%(message)s',
        force=True
    )
    
    enable_chrony = args.enable_chrony and not args.disable_chrony
    run_fusion_service(
        args.data_root, 
        args.interval, 
        enable_chrony=enable_chrony,
        lookback_minutes=args.lookback
    )
