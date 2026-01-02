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
import threading
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
import numpy as np
from datetime import datetime, timezone

# Systemd watchdog support
try:
    from systemd import daemon as systemd_daemon
    SYSTEMD_AVAILABLE = True
except ImportError:
    SYSTEMD_AVAILABLE = False

# Initialize logger FIRST (before any code that might use it)
logger = logging.getLogger(__name__)

# HDF5 I/O for reading L1A and L2 data products
try:
    from hf_timestd.io import DataProductReader
except ImportError:
    HDF5_AVAILABLE = False
    logger.warning("h5py/xarray not available, using CSV fallback")

# Disable HDF5 file locking to allow SWMR readers
# This is required when the writer has the file locked for SWMR write
os.environ["HDF5_USE_FILE_LOCKING"] = "FALSE"

# HDF5 is now enabled with SWMR support
HDF5_AVAILABLE = True
if not HDF5_AVAILABLE:
    logger.warning("HDF5 storage DISABLED (forced fallback to CSV)")

# Physics Propagation for GNSS Integration
try:
    from hf_timestd.core.physics_propagation import PhysicsPropagationModel
except ImportError:
    PhysicsPropagationModel = None



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
    uncertainty_ms: float        # Combined uncertainty (RSS of components)
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
    
    # Uncertainty budget breakdown (for metrology display)
    statistical_uncertainty_ms: float = 0.0      # Measurement scatter (weighted std)
    systematic_uncertainty_ms: float = 0.0       # Calibration convergence error
    propagation_uncertainty_ms: float = 0.0      # Mode-dependent ionospheric variability


class AllanDeviationTracker:
    """
    Efficient overlapping Allan deviation calculator for real-time stability monitoring.
    
    Maintains a rolling window of timing measurements and computes Allan deviation
    at multiple tau values to characterize oscillator stability.
    
    Allan deviation σ_y(τ) measures fractional frequency stability:
        σ_y(τ) = sqrt(1/(2(M-1)) * Σ(y_{i+1} - y_i)²)
    
    For timing applications, we track clock offset and convert to frequency stability.
    """
    
    def __init__(self, max_samples: int = 86400):
        """
        Initialize tracker with rolling window.
        
        Args:
            max_samples: Maximum samples to retain (default 86400 = 24h at 1min cadence)
        """
        from collections import deque
        self.timestamps = deque(maxlen=max_samples)
        self.values = deque(maxlen=max_samples)
        self.max_samples = max_samples
    
    def add_measurement(self, timestamp: float, value_ms: float):
        """
        Add new timing measurement to history.
        
        Args:
            timestamp: Unix timestamp
            value_ms: Clock offset in milliseconds
        """
        self.timestamps.append(timestamp)
        self.values.append(value_ms)
    
    def compute_adev(self, tau_seconds: int) -> Optional[float]:
        """
        Compute overlapping Allan deviation for given tau.
        
        Args:
            tau_seconds: Averaging time (tau) in seconds
            
        Returns:
            Allan deviation σ_y(τ) or None if insufficient data
        """
        if len(self.values) < 2:
            return None
        
        # Estimate sample interval (assume ~60s cadence)
        if len(self.timestamps) >= 2:
            dt_avg = (self.timestamps[-1] - self.timestamps[0]) / (len(self.timestamps) - 1)
        else:
            dt_avg = 60.0  # Default to 1 minute
        
        # Number of samples per tau
        n_tau = max(1, int(tau_seconds / dt_avg))
        
        # Need at least 2*n_tau samples for overlapping ADEV
        if len(self.values) < 2 * n_tau:
            return None
        
        # Compute overlapping differences
        diffs = []
        values_arr = np.array(self.values)
        for i in range(len(values_arr) - n_tau):
            diff = values_arr[i + n_tau] - values_arr[i]
            diffs.append(diff)
        
        if len(diffs) < 2:
            return None
        
        # Allan variance (overlapping)
        # σ²_y(τ) = 1/(2(M-1)) * Σ(y_{i+1} - y_i)²
        diffs_arr = np.array(diffs)
        second_diffs = np.diff(diffs_arr)
        allan_var = np.mean(second_diffs**2) / 2.0
        
        # Allan deviation
        allan_dev = np.sqrt(allan_var)
        
        # Convert from time deviation (ms) to fractional frequency
        # σ_y ≈ σ_time / tau
        # For ms units: σ_y = (allan_dev_ms / 1000) / tau_seconds
        sigma_y = (allan_dev / 1000.0) / tau_seconds
        
        return sigma_y
    
    def compute_all_adev(self, tau_values: List[int]) -> Dict[str, Optional[float]]:
        """
        Compute ADEV for multiple tau values.
        
        Args:
            tau_values: List of tau values in seconds
            
        Returns:
            Dictionary mapping tau to ADEV value
        """
        results = {}
        for tau in tau_values:
            results[f'adev_{tau}s'] = self.compute_adev(tau)
        return results


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
        
        # Initialize Physics Propagation Model (for GNSS VTEC integration)
        if PhysicsPropagationModel:
            self.physics_model = PhysicsPropagationModel(
                receiver_lat=self.receiver_lat,
                receiver_lon=self.receiver_lon,
                enable_pylap=False, # We just want Tier 2/3 for geometric/empirical baseline
                enable_iri=False,   # We just want the empirical baseline to correct against
                ionex_dir=Path('/var/lib/timestd/ionex')  # Enable IONEX VTEC
            )
        else:
            self.physics_model = None
            logger.warning("PhysicsPropagationModel not available - GNSS VTEC integration disabled")
        
        # Calibration state
        self.calibration_file = calibration_file or (
            self.data_root / 'state' / 'broadcast_calibration.json'
        )
        self.calibration: Dict[str, StationCalibration] = {}
        self._load_calibration()
        
        # Fusion output
        self.fusion_dir = self.data_root / 'phase2' / 'fusion'
        self.fusion_dir.mkdir(parents=True, exist_ok=True)
        
        # ====================================================================
        # HDF5 Data Product Writer (HDF5-Only Output)
        # ====================================================================
        # Initialize HDF5 writer for schema-validated fusion results
        try:
            from hf_timestd.io import DataProductWriter
            
            self.hdf5_fusion_writer = DataProductWriter(
                output_dir=self.fusion_dir,
                product_level='L3',
                product_name='fusion_timing',
                channel='fusion',  # Fusion is multi-channel aggregate
                processing_version='3.2.0',
                station_metadata={'description': 'Multi-broadcast fusion estimate'}
            )
            self.enable_hdf5_fusion_writes = True
            logger.info("Initialized HDF5 L3 fusion writer")
        except Exception as e:
            logger.warning(f"Failed to initialize HDF5 fusion writer: {e}")
            logger.warning("Continuing with CSV-only writes")
            self.hdf5_fusion_writer = None
            self.enable_hdf5_fusion_writes = False

        
        # History for calibration learning
        self.measurement_history: Dict[str, List[BroadcastMeasurement]] = defaultdict(list)
        self.history_max_size = 100  # Keep last N measurements per station
        
        # Kalman filter state for convergence
        # State: [d_clock_offset, d_clock_drift_rate]
        self.kalman_state = np.array([0.0, 0.0])  # [offset_ms, drift_ms_per_min]
        self.kalman_P = np.array([[100.0, 0.0], [0.0, 1.0]])  # Initial uncertainty
        self.kalman_initialized = False
        self.kalman_n_updates = 0
        
        # Allan deviation tracker for real-time stability monitoring
        self.adev_tracker = AllanDeviationTracker(max_samples=86400)  # 24h history
        self.adev_tau_values = [10, 100, 1000, 10000]  # Standard tau values (seconds)
        
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
    
    def _validate_calibration_data(self, data: dict) -> bool:
        """
        Validate loaded calibration data for sanity.
        
        Prevents loading corrupted or stale calibration files that could
        trap the system in an unrecoverable state.
        
        Validation criteria:
        - Offset magnitude must be < 100ms (realistic max ~60ms for multi-hop)
        - Calibration age must be < 7 days (ionospheric conditions change)
        
        Returns:
            True if calibration is safe to use, False otherwise.
        """
        MAX_OFFSET_MS = 100.0  # Maximum reasonable offset
        MAX_AGE_DAYS = 7       # Maximum calibration age
        
        current_time = time.time()
        max_age_seconds = MAX_AGE_DAYS * 86400
        
        for broadcast_key, cal_data in data.items():
            offset_ms = cal_data.get('offset_ms', 0.0)
            last_updated = cal_data.get('last_updated', 0)
            
            # Check offset magnitude
            if abs(offset_ms) > MAX_OFFSET_MS:
                logger.warning(
                    f"Calibration sanity check FAILED: {broadcast_key} has "
                    f"offset={offset_ms:+.1f}ms (exceeds ±{MAX_OFFSET_MS}ms limit)"
                )
                return False
            
            # Check age
            age_seconds = current_time - last_updated
            if age_seconds > max_age_seconds:
                logger.warning(
                    f"Calibration sanity check FAILED: {broadcast_key} is "
                    f"{age_seconds/86400:.1f} days old (exceeds {MAX_AGE_DAYS} day limit)"
                )
                return False
        
        logger.info(f"Calibration sanity check PASSED for {len(data)} broadcasts")
        return True
    
    def _load_calibration(self):
        """
        Load per-broadcast calibration from file.
        
        Issue 3.2 Fix: Calibration is now keyed by broadcast (station_frequency)
        rather than just station, to account for frequency-dependent delays.
        
        Issue 3.8.2 Fix: Added sanity checks to prevent loading corrupted
        calibration files with unreasonably large offsets.
        """
        if self.calibration_file.exists():
            try:
                with open(self.calibration_file) as f:
                    data = json.load(f)
                
                # SANITY CHECK: Validate before loading
                if not self._validate_calibration_data(data):
                    logger.warning(
                        f"Calibration file {self.calibration_file} failed sanity checks. "
                        "Discarding and starting fresh with bootstrap mode."
                    )
                    self._init_default_calibration()
                    return
                
                # Load validated calibration
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
                
                # FIX 4: Skip warmup penalty if we have valid calibration data
                if len(self.calibration) >= 2:
                    self.kalman_n_updates = 200
                    logger.info("Skipping warmup penalty (calibration loaded)")

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
        
        # If HDF5 not available, return empty dict (CSV fallback handled at top level)
        if not HDF5_AVAILABLE:
            logger.debug("HDF5 not available for tone detections")
            return {}
        
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
                hdf5_measurements = self._read_latest_measurements_hdf5(lookback_minutes)
                if hdf5_measurements:
                    return hdf5_measurements
                else:
                    logger.info("HDF5 returned 0 measurements, falling back to CSV")
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
            
            # Read CSV files for this channel
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
                    min_quality_grade='D',  # FIX 3: Accept D to match CSV utility
                    min_confidence=0.0  # Accept all (trust Flags/Grades)
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
                # HDF5 file doesn't exist - skip this channel
                logger.debug(f"No HDF5 files found for {channel}")
            
            except Exception as e:
                logger.warning(f"Error reading HDF5 for {channel}: {e}")
        
        # After trying all channels, log results
        if measurements:
            logger.info(
                f"Read {len(measurements)} L2 timing measurements from HDF5 "
                f"(lookback={lookback_minutes}m)"
            )
        
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

        # FIX 2: Removed "God Mode" immunity for GLOBAL_DIFF
        # It must survive the same statistical scrutiny as other measurements
        
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
    
    def _cross_validate_stations(
        self,
        measurements: List[BroadcastMeasurement],
        calibrated: List[float]
    ) -> Tuple[bool, str, int]:
        """
        Cross-validate measurements from different stations.
        
        Detects systematic errors in any single station by requiring agreement
        between multiple stations. If only one station is available, validation
        passes (no cross-check possible). If multiple stations disagree by >200µs,
        flags a potential systematic error.
        
        RATIONALE:
        ----------
        Different stations (WWV, WWVH, CHU, BPM) should agree on UTC time within
        ±200µs after accounting for:
        - Propagation delays (already corrected in D_clock)
        - Calibration offsets (already applied)
        - Ionospheric variations (~50-100µs typical)
        
        Large disagreements (>200µs) indicate:
        - Systematic error in one station's propagation model
        - Discrimination error (wrong station identified)
        - Frame slip or decoder error
        - Ionospheric storm (rare, but possible)
        
        STRATEGY:
        ---------
        1. Group measurements by station
        2. Calculate mean D_clock for each station (using calibrated values)
        3. Check if all station means agree within ±200µs
        4. If disagreement >200µs, identify outlier station
        5. Return validation status and outlier count
        
        Args:
            measurements: List of broadcast measurements
            calibrated: Calibrated D_clock values (same length as measurements)
            
        Returns:
            Tuple of (is_valid, reason, n_outliers):
                - is_valid: True if stations agree or only 1 station
                - reason: Explanation of validation result
                - n_outliers: Number of outlier stations detected
        """
        # Group by station (exclude GLOBAL_DIFF synthetic measurements)
        station_groups = defaultdict(list)
        for m, cal_val in zip(measurements, calibrated):
            if m.station != 'GLOBAL_DIFF':
                station_groups[m.station].append(cal_val)
        
        # Need at least 2 stations for cross-validation
        if len(station_groups) < 2:
            return True, f"Only {len(station_groups)} station (no cross-check possible)", 0
        
        # Calculate mean D_clock for each station
        station_means = {}
        for station, values in station_groups.items():
            station_means[station] = np.mean(values)
        
        # Check agreement between all station pairs
        stations = list(station_means.keys())
        max_disagreement = 0.0
        disagreeing_pair = None
        
        for i in range(len(stations)):
            for j in range(i + 1, len(stations)):
                station_a = stations[i]
                station_b = stations[j]
                disagreement = abs(station_means[station_a] - station_means[station_b])
                
                if disagreement > max_disagreement:
                    max_disagreement = disagreement
                    disagreeing_pair = (station_a, station_b)
        
        # Threshold: ±200µs = 0.2ms
        CROSS_STATION_THRESHOLD_MS = 0.2
        
        if max_disagreement > CROSS_STATION_THRESHOLD_MS:
            # Stations disagree - identify outlier
            # The outlier is the station furthest from the median
            all_means = list(station_means.values())
            median_d_clock = np.median(all_means)
            
            # Find station(s) furthest from median
            outliers = []
            for station, mean_val in station_means.items():
                if abs(mean_val - median_d_clock) > CROSS_STATION_THRESHOLD_MS:
                    outliers.append(station)
            
            reason = (f"Cross-station disagreement: {max_disagreement:.3f}ms "
                     f"(threshold: {CROSS_STATION_THRESHOLD_MS:.3f}ms). "
                     f"Disagreeing pair: {disagreeing_pair[0]} vs {disagreeing_pair[1]}. "
                     f"Outlier stations: {', '.join(outliers)}")
            
            logger.warning(f"Cross-station validation FAILED: {reason}")
            logger.warning(f"  Station means: {', '.join([f'{s}={v:+.3f}ms' for s, v in station_means.items()])}")
            
            return False, reason, len(outliers)
        
        # All stations agree
        reason = (f"{len(station_groups)} stations agree within ±{CROSS_STATION_THRESHOLD_MS:.3f}ms "
                 f"(max disagreement: {max_disagreement:.3f}ms)")
        logger.debug(f"Cross-station validation OK: {reason}")
        
        return True, reason, 0
    
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
        # GNSS VTEC INTEGRATION (Real-Time Physics Correction)
        # ====================================================================
        # If available, use local GNSS VTEC to refine ionospheric delays.
        # This replaces the empirical/model delay with one derived from live data.
        
        logger.info(">>> VTEC INTEGRATION: Starting VTEC check <<<")
        
        gnss_vtec_data = self._read_gnss_vtec()
        used_gnss_vtec = False
        
        logger.debug(f"VTEC check: data={gnss_vtec_data is not None}, physics_model={self.physics_model is not None}")
        
        if gnss_vtec_data and self.physics_model:
            vtec_tecu, vtec_ts = gnss_vtec_data
            
            # Only use if fresh (< 5 minutes old)
            age_seconds = time.time() - vtec_ts
            logger.debug(f"VTEC data: {vtec_tecu:.2f} TECU, age={age_seconds:.1f}s")
            
            if age_seconds < 300:
                logger.info(f"Applying GNSS VTEC correction: {vtec_tecu:.2f} TECU (age: {age_seconds:.1f}s)")
                used_gnss_vtec = True
                
                for m in measurements:
                    if m.station == 'GLOBAL_DIFF' or m.station == 'UNKNOWN':
                        continue
                        
                    # 1. Compute baseline delay (what the system likely used)
                    # We assume the measurement used Tier 2/3 (IRI or Empirical)
                    baseline = self.physics_model.compute_delay(
                        station=m.station,
                        frequency_mhz=m.frequency_mhz,
                        observed_arrival_ms=0, # Not needed for prediction
                        timestamp=datetime.fromtimestamp(m.timestamp, tz=timezone.utc).replace(tzinfo=None)
                    )
                    
                    if baseline and baseline.n_hops > 0:
                        # 2. Extract Implicit TEC from baseline
                        # baseline.tec_tecu is what the model thought TEC was (e.g. 20-50 TECU)
                        model_tec = baseline.tec_tecu if baseline.tec_tecu else 20.0
                        
                        # 3. Calculate Ionospheric Delays
                        # Delay_meters = 40.3 * TEC * 10^16 / f^2
                        # Delay_sec = Delay_meters / c
                        # Delay_ms = Delay_sec * 1000
                        
                        C_LIGHT = 299792458.0
                        freq_hz = m.frequency_mhz * 1e6
                        
                        # Factor to convert TECU (1e16 el/m2) to Delay (ms)
                        # factor = (40.3 * 1e16 * 1000) / (freq_hz^2 * c)
                        factor = (40.3 * 1e16 * baseline.n_hops * 1000.0) / ((freq_hz**2) * C_LIGHT)
                        
                        old_iono_delay_ms = model_tec * factor
                        new_iono_delay_ms = vtec_tecu * factor
                        
                        # 4. Apply Correction
                        # We want to REMOVE old iono and ADD new iono
                        # d_clock = (Arrival - vac - old_iono)
                        # d_clock_new = (Arrival - vac - new_iono)
                        #             = d_clock + old_iono - new_iono
                        
                        correction_ms = old_iono_delay_ms - new_iono_delay_ms
                        
                        # FIX 1: VTEC Safety Check
                        # Only apply correction if it moves the measurement closer to the group median
                        # (or 0 if we are the only one, but strict check is better)
                        
                        other_clocks = [om.d_clock_ms for om in measurements if om != m and om.station != 'GLOBAL_DIFF']
                        if other_clocks:
                            median_ref = np.median(other_clocks)
                            dist_before = abs(m.d_clock_ms - median_ref)
                            dist_after = abs((m.d_clock_ms + correction_ms) - median_ref)
                            is_better = dist_after < dist_before
                        else:
                            # If no others, compare to UTC target (0)
                            is_better = abs(m.d_clock_ms + correction_ms) < abs(m.d_clock_ms)
                        
                        if is_better:
                            logger.debug(
                                f"  {m.station} {m.frequency_mhz}MHz: Model={model_tec:.1f}TECU Iono={old_iono_delay_ms:.3f}ms -> "
                                f"GNSS={vtec_tecu:.1f}TECU Iono={new_iono_delay_ms:.3f}ms | "
                                f"Corr={correction_ms:+.3f}ms (Improved)"
                            )
                            
                            m.d_clock_ms += correction_ms
                            m.propagation_delay_ms += (new_iono_delay_ms - old_iono_delay_ms)
                            m.propagation_mode = f"{baseline.propagation_mode}+GNSS"
                            m.confidence = min(0.95, m.confidence * 1.5) # Boost confidence
                        else:
                            logger.debug(
                                f"  {m.station} {m.frequency_mhz}MHz: VTEC correction rejected (worsened consistency)"
                            )
            else:
                logger.debug(f"GNSS VTEC stale (age: {time.time()-vtec_ts:.1f}s), skipping")

        # ====================================================================
        # TEC ESTIMATION (Physics-Based Propagation Correction)
        # ====================================================================
        # Only run the HF TEC solver if we DIDN'T use GNSS VTEC.
        # GNSS VTEC is generally superior to HF-derived TEC.
        
        if not used_gnss_vtec:
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
                        
                        # Approximating ToA as d_clock_ms + prop_delay
                        
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
                        # TEC writing is handled by science_aggregator service
                        # We use TEC here only to improve propagation delay estimates
                        
                        if tec_result.confidence > 0.9:
                            logger.info(f"TEC Solved for {station}: {tec_result.tec_u:.1f} TECU (R2={tec_result.confidence:.2f})")
                            
                            # Update measurements with Physics-Derived delays
                            for m in station_meas:
                                if m.frequency_mhz in tec_result.group_delay_ms:
                                    new_delay = tec_result.group_delay_ms[m.frequency_mhz]
                                    
                                    # Update D_clock with NEW delay
                                    # D_clock_new = T_arrival - T_delay_new
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
        
        # ====================================================================
        
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
        
        # ====================================================================
        # CROSS-STATION VALIDATION (Priority 1C - 2025-12-31)
        # ====================================================================
        # Validate that different stations agree on UTC time within ±200µs.
        # This detects systematic errors in any single station.
        
        cross_valid, cross_reason, n_cross_outliers = self._cross_validate_stations(
            measurements, calibrated
        )
        
        if not cross_valid:
            logger.warning(f"Cross-station validation failed: {cross_reason}")
            # Note: We don't reject the fusion, but flag it in the result
            # The consistency_flag will be set to reflect this issue
        
        # Weighted mean of calibrated values
        w = np.array(weights)
        d = np.array(calibrated)
        fused_d_clock = np.sum(w * d) / np.sum(w)
        
        # Raw (uncalibrated) mean for comparison
        raw_d_clocks = np.array([m.d_clock_ms for m in measurements])
        raw_mean = np.mean(raw_d_clocks)
        
        # ====================================================================
        # ENHANCED UNCERTAINTY CALCULATION
        # ====================================================================
        # Proper uncertainty budget with three components:
        # 1. Statistical: Measurement scatter (weighted std)
        # 2. Systematic: Calibration convergence error
        # 3. Propagation: Mode-dependent ionospheric variability
        
        # 1. Statistical uncertainty from weighted std
        weighted_var = np.sum(w * (d - fused_d_clock)**2) / np.sum(w)
        statistical_uncertainty = np.sqrt(weighted_var)
        
        # 2. Systematic uncertainty from calibration convergence
        # Estimate based on Kalman filter convergence state
        # Early in convergence: higher systematic error
        # After convergence: residual calibration uncertainty ~0.3-0.5ms
        if self.kalman_n_updates < 50:
            # Still converging
            systematic_uncertainty = 1.0 * (1.0 - self.kalman_n_updates / 50.0)
        elif self.kalman_n_updates < 200:
            # Partially converged
            systematic_uncertainty = 0.5 * (1.0 - (self.kalman_n_updates - 50) / 150.0)
        else:
            # Fully converged - residual systematic error
            systematic_uncertainty = 0.3 if has_verified_global else 0.4
        
        # 3. Propagation uncertainty - mode-dependent ionospheric variability
        # Different propagation modes have different inherent uncertainties
        mode_uncertainties = {
            'GW': 0.1,    # Ground wave (very stable)
            '1E': 0.3,    # Single-hop E-layer (stable)
            '1F': 0.5,    # Single-hop F-layer (moderate)
            '2E': 1.0,    # Two-hop E-layer
            '2F': 2.0,    # Two-hop F-layer (variable)
            '3F': 3.0,    # Three-hop (highly variable)
            'TEC_SOLVED': 0.2,  # Physics-derived (very good)
        }
        
        # Weighted average of mode uncertainties
        mode_unc_list = []
        for m in measurements:
            mode = getattr(m, 'propagation_mode', '1F')
            mode_unc_list.append(mode_uncertainties.get(mode, 1.0))
        
        if mode_unc_list:
            # Weight by measurement weights
            propagation_uncertainty = np.sum(w * np.array(mode_unc_list)) / np.sum(w)
        else:
            propagation_uncertainty = 1.0  # Conservative default
        
        # Combined uncertainty (Root Sum of Squares)
        # RSS is appropriate for independent uncertainty sources
        combined_uncertainty = np.sqrt(
            statistical_uncertainty**2 +
            systematic_uncertainty**2 +
            propagation_uncertainty**2
        )
        
        # Apply uncertainty floor
        has_verified_global = (global_result is not None and getattr(global_result, 'verified', False))
        uncertainty_floor = 0.1 if has_verified_global else 0.2
        measurement_uncertainty = max(uncertainty_floor, combined_uncertainty)
        
        # Update Kalman filter for convergence tracking
        kalman_uncertainty = self._kalman_update(fused_d_clock, measurement_uncertainty)
        
        # Final uncertainty is the Kalman-filtered combined uncertainty
        # This provides temporal smoothing while preserving the uncertainty budget
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
        # Priority: Cross-station > Intra-station
        if not cross_valid:
            consistency_flag = 'CROSS_STATION_DISAGREE'
        else:
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
            consistency_flag=consistency_flag,
            # Uncertainty budget components
            statistical_uncertainty_ms=statistical_uncertainty,
            systematic_uncertainty_ms=systematic_uncertainty,
            propagation_uncertainty_ms=propagation_uncertainty
        )
        
        # Track measurement for Allan deviation calculation
        self.adev_tracker.add_measurement(result.timestamp, result.d_clock_fused_ms)
        
        # Write to CSV
        self._write_fused_result(result)
        
        return result
    
    def get_current_adev(self) -> Dict[str, Optional[float]]:
        """
        Get current Allan deviation values at standard tau values.
        
        Returns:
            Dictionary with ADEV at 10s, 100s, 1000s, 10000s tau values
        """
        return self.adev_tracker.compute_all_adev(self.adev_tau_values)
    
    def _write_fused_result(self, result: FusedResult):
        """Write fused result to HDF5."""
        # HDF5 write (HDF5-only output)
        self._write_fused_result_hdf5(result)
    
    def _write_fused_result_hdf5(self, result: FusedResult):
        """Write fused result to HDF5 with schema validation."""
        if not self.enable_hdf5_fusion_writes or not self.hdf5_fusion_writer:
            return
        
        try:
            from datetime import datetime, timezone
            
            # Convert timestamp to ISO 8601
            timestamp_utc = datetime.fromtimestamp(
                result.timestamp, 
                timezone.utc
            ).isoformat().replace('+00:00', 'Z')
            
            # Determine quality flag from quality grade
            if result.quality_grade == 'A':
                quality_flag = 'GOOD'
            elif result.quality_grade == 'B':
                quality_flag = 'MARGINAL'
            else:
                quality_flag = 'BAD'
            
            # Build stations_used string
            stations = []
            if result.wwv_count > 0:
                stations.append('WWV')
            if result.wwvh_count > 0:
                stations.append('WWVH')
            if result.chu_count > 0:
                stations.append('CHU')
            if result.bpm_count > 0:
                stations.append('BPM')
            stations_used = ','.join(stations) if stations else 'NONE'
            
            # Determine Kalman state (simplified - fusion uses weighted averaging, not Kalman)
            # Map convergence state to Kalman-like states for schema compatibility
            if result.n_broadcasts >= 10 and result.uncertainty_ms < 1.0:
                kalman_state = 'LOCKED'
            elif result.n_broadcasts >= 5:
                kalman_state = 'ACQUIRING'
            else:
                kalman_state = 'REACQUIRING'
            
            # Build measurement dictionary
            measurement = {
                'timestamp_utc': timestamp_utc,
                'minute_boundary': int(result.timestamp),
                'd_clock_fused_ms': result.d_clock_fused_ms,
                'd_clock_raw_ms': result.d_clock_raw_ms,
                'uncertainty_ms': result.uncertainty_ms,
                'n_broadcasts': result.n_broadcasts,
                'n_stations': result.n_stations,
                'stations_used': stations_used,
                'kalman_state': kalman_state,
                'quality_flag': quality_flag,
                'processing_version': '3.2.0',
                
                # Uncertainty budget
                'statistical_uncertainty_ms': result.statistical_uncertainty_ms,
                'systematic_uncertainty_ms': result.systematic_uncertainty_ms,
                'propagation_uncertainty_ms': result.propagation_uncertainty_ms,
                
                # Per-station breakdown
                'wwv_mean_ms': result.wwv_mean_ms,
                'wwvh_mean_ms': result.wwvh_mean_ms,
                'chu_mean_ms': result.chu_mean_ms,
                'bpm_mean_ms': result.bpm_mean_ms,
                'wwv_count': result.wwv_count,
                'wwvh_count': result.wwvh_count,
                'chu_count': result.chu_count,
                'bpm_count': result.bpm_count,
                'wwv_intra_std_ms': result.wwv_intra_std_ms,
                'wwvh_intra_std_ms': result.wwvh_intra_std_ms,
                'chu_intra_std_ms': result.chu_intra_std_ms,
                
                # Consistency metrics
                'inter_station_spread_ms': result.inter_station_spread_ms,
                'consistency_flag': 'INTER_ANOMALY' if result.consistency_flag == 'CROSS_STATION_DISAGREE' else result.consistency_flag,
                
                # Global solve
                'global_solve_verified': result.global_solve_verified,
                'global_solve_consistency_ms': result.global_solve_consistency_ms,
                'global_solve_n_obs': result.global_solve_n_obs,
                
                # Calibration metadata
                'calibration_applied': result.calibration_applied,
                'reference_station': result.reference_station,
                'outliers_rejected': result.outliers_rejected,
                'quality_grade': result.quality_grade,
            }
            
            # Write to HDF5 with schema validation
            self.hdf5_fusion_writer.write_measurement(measurement)
            
        except Exception as e:
            logger.error(f"Failed to write HDF5 fusion result: {e}", exc_info=True)

    
    def _read_gnss_vtec(self) -> Optional[Tuple[float, float]]:
        """
        Read the latest GNSS VTEC from HDF5 or CSV fallback.
        Returns (vtec_tecu, timestamp) or None.
        """
        logger.info(">>> _read_gnss_vtec() called <<<")
        
        # Try HDF5 first
        if HDF5_AVAILABLE:
            try:
                from datetime import datetime, timezone, timedelta
                
                vtec_dir = self.data_root / 'gnss_vtec'
                if vtec_dir.exists():
                    reader = DataProductReader(
                        data_dir=vtec_dir,
                        product_level='L3',
                        product_name='gnss_vtec',
                        channel='GNSS'
                    )
                    
                    # Read last 5 minutes of data
                    now = datetime.now(timezone.utc)
                    start = now - timedelta(minutes=5)
                    
                    measurements = reader.read_time_range(
                        start=start.isoformat().replace('+00:00', 'Z'),
                        end=now.isoformat().replace('+00:00', 'Z'),
                        quality_flags=['GOOD', 'MARGINAL']  # Accept GOOD and MARGINAL
                    )
                    
                    if measurements:
                        # Get most recent measurement
                        latest = max(measurements, key=lambda m: m['unix_timestamp'])
                        logger.info(
                            f"Read VTEC from HDF5: {latest['vtec_tecu']:.2f} TECU, "
                            f"{latest['n_satellites']} sats, quality={latest['quality_flag']}"
                        )
                        return latest['vtec_tecu'], latest['unix_timestamp']
                    else:
                        logger.debug("HDF5 VTEC query returned no measurements")
            
            except FileNotFoundError:
                logger.debug("HDF5 VTEC directory not found, trying CSV fallback")
            except Exception as e:
                logger.debug(f"HDF5 VTEC read failed, trying CSV fallback: {e}")
        
        # CSV fallback (original implementation)
        vtec_path = self.data_root / 'gnss_vtec.csv'
        if not vtec_path.exists():
            logger.info(f"VTEC file does not exist: {vtec_path}")
            return None
            
        try:
            # Efficiently read last line using seek
            with open(vtec_path, 'rb') as f:
                try:
                    f.seek(-1024, os.SEEK_END)
                except OSError:
                    # File too small, read from beginning
                    pass
                lines = f.readlines()
                
            if not lines:
                return None
                
            last_line = lines[-1].decode('utf-8').strip()
            # CSV Format: timestamp,vtec_tecu,nsats
            parts = last_line.split(',')
            if len(parts) >= 2:
                # Handle potential header
                if parts[0] == 'timestamp':
                    return None
                    
                ts = float(parts[0])
                vtec = float(parts[1])
                logger.info(f"Read VTEC from CSV fallback: {vtec:.2f} TECU")
                return vtec, ts
                
        except Exception as e:
            logger.debug(f"Error reading GNSS VTEC: {e}")
            return None

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


class ChronySHMUpdater:
    """
    Threaded Chrony SHM updater that runs independently of fusion loop.
    
    This ensures chrony receives updates at its poll interval (8s) even if
    fusion runs at a different cadence (e.g., 60s).
    """
    
    def __init__(self, chrony_shm, poll_interval: float = 8.0):
        self.chrony_shm = chrony_shm
        self.poll_interval = poll_interval
        self.latest_result = None
        self.result_lock = threading.Lock()
        self.running = False
        self.thread = None
        self.consecutive_failures = 0
        self.total_writes = 0
        self.failed_writes = 0
        
    def update_result(self, result):
        """Update the latest fusion result (called by main fusion loop)."""
        with self.result_lock:
            self.latest_result = result
    
    def _updater_thread(self):
        """Background thread that writes to Chrony SHM at poll interval."""
        logger.info(f"Chrony SHM updater thread started (poll interval: {self.poll_interval}s)")
        
        while self.running:
            try:
                with self.result_lock:
                    result = self.latest_result
                
                if result and result.quality_grade in ('A', 'B', 'C', 'D'):
                    now = time.time()
                    system_time = now
                    reference_time = system_time - (result.d_clock_fused_ms / 1000.0)
                    
                    # Precision based on uncertainty (log2 of seconds)
                    precision = max(-13, min(-4, int(-10 - np.log2(max(0.1, result.uncertainty_ms)))))
                    
                    try:
                        update_success = self.chrony_shm.update(reference_time, system_time, precision)
                        
                        if update_success:
                            self.consecutive_failures = 0
                            self.total_writes += 1
                            if self.total_writes <= 5 or self.total_writes % 60 == 0:
                                logger.info(
                                    f"Chrony SHM updated: D_clock={result.d_clock_fused_ms:+.3f}ms, "
                                    f"offset={(system_time-reference_time)*1000:+.3f}ms, "
                                    f"precision={precision} (write #{self.total_writes})"
                                )
                        else:
                            self.consecutive_failures += 1
                            self.failed_writes += 1
                            logger.error(
                                f"Chrony SHM write failed (consecutive: {self.consecutive_failures}, "
                                f"total: {self.failed_writes}/{self.total_writes + self.failed_writes})"
                            )
                            
                            # Try to reconnect after multiple failures
                            if self.consecutive_failures >= 3:
                                try:
                                    logger.warning("Attempting Chrony SHM reconnect...")
                                    self.chrony_shm.disconnect()
                                    if self.chrony_shm.connect():
                                        logger.info("Chrony SHM reconnected successfully")
                                        self.consecutive_failures = 0
                                except Exception as e:
                                    logger.error(f"Failed to reconnect Chrony SHM: {e}")
                    
                    except Exception as e:
                        logger.error(f"Chrony SHM update exception: {e}", exc_info=True)
                        self.consecutive_failures += 1
                        self.failed_writes += 1
                else:
                    if result:
                        logger.debug(f"Skipping SHM write: quality grade {result.quality_grade} not acceptable")
                    else:
                        logger.debug("Skipping SHM write: no fusion result available yet")
                
            except Exception as e:
                logger.error(f"Chrony SHM updater thread error: {e}", exc_info=True)
            
            # Sleep for poll interval
            time.sleep(self.poll_interval)
    
    def start(self):
        """Start the background updater thread."""
        if self.running:
            logger.warning("Chrony SHM updater already running")
            return
        
        self.running = True
        self.thread = threading.Thread(target=self._updater_thread, daemon=True, name="ChronySHMUpdater")
        self.thread.start()
        logger.info("Chrony SHM updater thread started")
    
    def stop(self):
        """Stop the background updater thread."""
        if not self.running:
            return
        
        self.running = False
        if self.thread:
            self.thread.join(timeout=2.0)
        logger.info("Chrony SHM updater thread stopped")


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
    
    The Chrony SHM updates run in a separate thread at 8-second intervals,
    independent of the fusion loop interval. This ensures chrony receives
    fresh updates at its poll rate even if fusion runs less frequently.
    
    Args:
        data_root: Base data directory
        interval_sec: Fusion interval in seconds
        enable_chrony: If True, write fused time to Chrony SHM refclock
        lookback_minutes: Number of minutes to look back for measurements
    """
    fusion = MultiBroadcastFusion(data_root)
    
    # Initialize Chrony SHM if enabled
    chrony_shm = None
    chrony_updater = None
    if enable_chrony:
        try:
            from hf_timestd.core.chrony_shm import ChronySHM
            chrony_shm = ChronySHM(unit=0)
            if chrony_shm.connect():
                logger.info("Chrony SHM refclock enabled (unit=0, refid=TMGR)")
                # Start threaded updater at 8-second intervals
                chrony_updater = ChronySHMUpdater(chrony_shm, poll_interval=8.0)
                chrony_updater.start()
            else:
                logger.warning("Failed to connect to Chrony SHM - continuing without")
                chrony_shm = None
        except Exception as e:
            logger.warning(f"Chrony SHM not available: {e}")
            chrony_shm = None
    
    logger.info("Starting Multi-Broadcast Fusion Service")
    logger.info(f"  Interval: {interval_sec} seconds")
    logger.info(f"  Output: {fusion.fusion_dir / 'fusion_fusion_timing_YYYYMMDD.h5'}")
    logger.info(f"  Chrony SHM: {'enabled (threaded updater at 8s)' if chrony_updater else 'disabled'}")
    
    logger.info("Starting Multi-Broadcast Fusion Dashboard Service...")
    logger.info(f"Fusion interval: {interval_sec}s")
    
    # Notify systemd we're ready
    if SYSTEMD_AVAILABLE:
        systemd_daemon.notify('READY=1')
        logger.info("Notified systemd: READY")
    
    while True:
        try:
            # BREADCRUMB: Loop start
            loop_start_time = time.time()
            logger.debug(f"--- FUSION LOOP START (t={loop_start_time:.3f}) ---")
            
            # Notify watchdog we are alive
            if SYSTEMD_AVAILABLE:
                systemd_daemon.notify('WATCHDOG=1')
            
            # BREADCRUMB: Calling fuse
            logger.debug("Calling fusion.fuse()...")
            
            # Run fusion update
            try:
                result = fusion.fuse(lookback_minutes=lookback_minutes)
            except Exception as e_fuse:
                logger.error(f"Fusion calculation CRASHED: {e_fuse}", exc_info=True)
                result = None
            
            # BREADCRUMB: Fusion returned
            logger.debug(f"Fusion returned: {result is not None}")
            
            if result:
                # Log summary
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
                
                # Update Chrony SHM updater thread with latest result
                if chrony_updater:
                    chrony_updater.update_result(result)
                    logger.debug(f"Updated Chrony SHM thread with latest fusion result (grade {result.quality_grade})")

            # BREADCRUMB: Sleeping
            loop_duration = time.time() - loop_start_time
            logger.debug(f"Loop finished in {loop_duration:.3f}s. Sleeping {interval_sec}s...")
            
            time.sleep(interval_sec)
            
        except KeyboardInterrupt:
            logger.info("Fusion service stopped")
            if chrony_updater:
                chrony_updater.stop()
            break
        except Exception as e:
            logger.error(f"Fusion error: {e}", exc_info=True)
            time.sleep(interval_sec)




if __name__ == '__main__':
    import argparse
    
    parser = argparse.ArgumentParser(description='Multi-Broadcast D_clock Fusion')
    parser.add_argument('--data-root', type=Path, default=Path('data'), required=False) # Configured default for simpler running
    parser.add_argument('--config', type=Path, help='Configuration file') # Added config support
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
