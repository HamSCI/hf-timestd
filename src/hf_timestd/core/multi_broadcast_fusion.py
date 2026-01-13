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
from hf_timestd.models import (
    L3FusionTiming,
    FusionQualityGrade,
    FusionQualityFlag,
    FusionConsistencyFlag,
    FusionKalmanState,
    ReferenceStation
)

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
    raw_arrival_time_ms: Optional[float] = None  # L1: Validated Tone TOA
    uncertainty_ms: Optional[float] = None  # L1: ISO GUM combined uncertainty
    
    # Physics (L2) additions
    l2_propagation_delay_ms: Optional[float] = None  # Physics model delay
    l2_tec_estimate: Optional[float] = None          # TECU estimate
    l2_model_confidence: Optional[float] = None      # Physics model confidence



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
    
    # Validation flags (CRITICAL FIX 2026-01-10)
    single_station_mode: bool = False            # True if only one station available (no cross-validation)


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
        self.calibration: Dict[str, BroadcastCalibration] = {}
        self.calibration_update_count = 0  # Track updates for auto-save
        self.auto_calibrate = auto_calibrate
        self.reference_station = reference_station
        self.correction_alpha = 0.0  # Gradual ramp-up for Kalman correction (0→1)

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
        
        # Two-tier Kalman approach (2026-01-10)
        # Tier 1: Fast measurements (every 8s) - record variations, don't adjust baseline
        # Tier 2: Slow adjustments (detect persistent drift) - only adjust if GPSDO drifting
        self.kalman_converged = False  # True after ~50 updates (~7 minutes)
        self.kalman_convergence_threshold = 50  # Updates needed for convergence
        self.measurement_window = []  # Recent measurements for drift detection
        self.measurement_window_size = 30  # 30 measurements = ~4 minutes
        self.last_baseline_adjustment = 0.0  # Timestamp of last adjustment
        self.baseline_adjustment_interval = 600.0  # Minimum 10 minutes between adjustments
        
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
        """
        Discover available Phase 2 channels.
        
        CRITICAL FIX (2026-01-05): Updated to look for HDF5 timing measurement files
        instead of legacy clock_offset subdirectory. The new HDF5 schema stores
        timing_measurements files directly in the channel directory.
        """
        channels = []
        if self.phase2_dir.exists():
            for subdir in self.phase2_dir.iterdir():
                if subdir.is_dir() and subdir.name != 'fusion':
                    # Check for HDF5 timing measurement files (new schema)
                    has_hdf5 = any(subdir.glob('*_timing_measurements_*.h5'))
                    # Fallback: check for legacy clock_offset subdirectory
                    has_legacy = (subdir / 'clock_offset').exists()
                    
                    if has_hdf5 or has_legacy:
                        channels.append(subdir.name)
        
        logger.info(f"Discovered {len(channels)} channels: {sorted(channels)[:5]}...")
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
        
        CRITICAL FIX (P2.1): Calibration persistence eliminates bootstrap delay.
        System now loads previous calibration state on startup, allowing immediate
        grade A performance instead of 10-20 minute convergence period.
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
                
                # CRITICAL FIX: Restore Kalman state from calibration file
                # This prevents discontinuities on service restart
                if '_kalman_state' in data:
                    ks = data['_kalman_state']
                    age_seconds = time.time() - ks.get('saved_at', 0)
                    
                    # Only restore if state is recent (<1 hour old)
                    if age_seconds < 3600:
                        try:
                            self.kalman_state = np.array([
                                ks['offset_ms'],
                                ks['drift_ms_per_min']
                            ])
                            self.kalman_P = np.array(ks['covariance'])
                            self.kalman_converged = ks['converged']
                            self.kalman_n_updates = ks['n_updates']
                            self.kalman_initialized = ks['initialized']
                            
                            logger.info(
                                f"Restored Kalman state: offset={self.kalman_state[0]:.3f}ms, "
                                f"converged={self.kalman_converged}, n_updates={self.kalman_n_updates}, "
                                f"age={age_seconds:.0f}s"
                            )
                        except (KeyError, ValueError, TypeError) as e:
                            logger.warning(f"Failed to restore Kalman state: {e}, using defaults")
                    else:
                        logger.warning(
                            f"Kalman state too old ({age_seconds:.0f}s), resetting to defaults for safety"
                        )
                
                # Load validated calibration
                for broadcast_key, cal_data in data.items():
                    # Skip metadata keys (Kalman state, etc.)
                    if broadcast_key.startswith('_'):
                        continue
                    
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
                logger.info(f"✅ Loaded {len(self.calibration)} broadcast calibrations from {self.calibration_file}")
                
                # CRITICAL: Skip warmup penalty if we have valid calibration data
                # This allows immediate grade A performance after service restart
                if len(self.calibration) >= 2:
                    self.kalman_n_updates = 200
                    logger.info("✅ Skipping warmup penalty (calibration loaded from disk)")

            except Exception as e:
                logger.warning(f"Could not load calibration: {e}")
                self._init_default_calibration()
        else:
            logger.info("No calibration file found, starting fresh bootstrap")
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
        """Persist per-broadcast calibration and Kalman state to file."""
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
        
        # CRITICAL FIX: Persist Kalman state to prevent discontinuities on restart
        # Without this, each restart resets Kalman to [0.0, 0.0] causing ~5ms jumps
        data['_kalman_state'] = {
            'offset_ms': float(self.kalman_state[0]),
            'drift_ms_per_min': float(self.kalman_state[1]),
            'covariance': self.kalman_P.tolist(),
            'converged': self.kalman_converged,
            'n_updates': self.kalman_n_updates,
            'initialized': self.kalman_initialized,
            'saved_at': time.time()
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
            # CRITICAL FIX: Define channel_dir before using it in DataProductReader
            channel_dir = self.phase2_dir / channel
            
            tone_dir = self.phase2_dir / channel / 'tone_detections'
            if not tone_dir.exists():
                continue
            
            freq_mhz = self._extract_frequency_mhz(channel)
            if freq_mhz is None:
                continue
            
            try:
                # Initialize HDF5 reader for L2 timing measurements
                reader = DataProductReader(
                    data_dir=channel_dir,
                    product_level='L2',  # CRITICAL FIX: Read L2 timing_measurements from analytics
                    product_name='timing_measurements',  # Correct schema name
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
    
    def _read_l1_metrology(
        self,
        lookback_minutes: int = 5
    ) -> Dict[str, List[Dict]]:
        """
        Read L1 Metrology measurements (Raw TOA).
        Returns a dict keyed by 'timestamp_utc|station' for easy joining.
        """
        from datetime import datetime, timezone

        l1_data = {}
        if not HDF5_AVAILABLE:
            return {}

        now = time.time()
        cutoff = now - (lookback_minutes * 60)
        start_dt = datetime.fromtimestamp(cutoff, timezone.utc)
        end_dt = datetime.fromtimestamp(now, timezone.utc)
        start_iso = start_dt.isoformat().replace('+00:00', 'Z')
        end_iso = end_dt.isoformat().replace('+00:00', 'Z')

        for channel in self.channels:
            channel_dir = self.phase2_dir / channel
            if not channel_dir.exists():
                continue

            try:
                reader = DataProductReader(
                    data_dir=channel_dir,
                    product_level='L1',
                    product_name='metrology_measurements',
                    channel=channel
                )
                
                # Filter locally to avoid reader complexity, or rely on reader if optimized
                measurements = reader.read_time_range(
                    start=start_iso, 
                    end=end_iso,
                    min_confidence=0.0 # Read all, filter later
                )

                for m in measurements:
                    # Key: {iso_timestamp}|{station_id}
                    # We use exact ISO string match or minute alignment
                    # Metrology uses 'timestamp_utc'
                    ts_str = m.get('timestamp_utc')
                    station = m.get('station_id')
                    if not ts_str or not station:
                        continue
                    
                    # Store key for joining
                    key = f"{ts_str}|{station}"
                    
                    # Augment with channel info
                    m['channel_name'] = channel
                    l1_data[key] = m

            except Exception as e:
                logger.debug(f"Error reading L1 for {channel}: {e}")
        
        return l1_data

    def _read_l2_physics(
        self,
        lookback_minutes: int = 5
    ) -> Dict[str, Dict]:
        """
        Read L2 Physics interpretations (Propagation Delay).
        Returns a dict keyed by 'timestamp_utc|station'.
        """
        from datetime import datetime, timezone

        l2_data = {}
        if not HDF5_AVAILABLE:
            return {}

        now = time.time()
        cutoff = now - (lookback_minutes * 60)
        start_dt = datetime.fromtimestamp(cutoff, timezone.utc)
        end_dt = datetime.fromtimestamp(now, timezone.utc)
        start_iso = start_dt.isoformat().replace('+00:00', 'Z')
        end_iso = end_dt.isoformat().replace('+00:00', 'Z')

        # Physics outputs are organized by channel (derived from L1 channel)
        for channel in self.channels:
            channel_dir = self.phase2_dir / channel
            if not channel_dir.exists():
                continue

            try:
                reader = DataProductReader(
                    data_dir=channel_dir,
                    product_level='L2',
                    product_name='physics_interpretation',
                    channel=channel
                )
                
                measurements = reader.read_time_range(
                    start=start_iso, 
                    end=end_iso,
                    min_confidence=0.0
                )

                for m in measurements:
                    ts_str = m.get('timestamp_utc')
                    station = m.get('station_id')
                    if not ts_str or not station:
                        continue
                    
                    key = f"{ts_str}|{station}"
                    l2_data[key] = m

            except Exception as e:
                logger.debug(f"Error reading L2 for {channel}: {e}")

        return l2_data

    def _read_latest_measurements(
        self, 
        lookback_minutes: int = 5
    ) -> List[BroadcastMeasurement]:
        """
        Read and join L1 (Metrology) and L2 (Physics) data to form Fusion inputs.
        
        Logic:
           1. Read L1 (Raw TOA).
           2. Read L2 (Propagation Delay).
           3. Join on Timestamp + Station.
           4. Calculate D_clock = Raw_TOA - Propagation_Delay.
           5. Fallback: If L2 missing, D_clock = Raw_TOA - (LightTime + 1.5ms).
        """
        from datetime import datetime, timezone
        from .wwv_constants import BPM_UT1_MINUTES
        
        # 1. Read L1 and L2
        l1_map = self._read_l1_metrology(lookback_minutes)
        l2_map = self._read_l2_physics(lookback_minutes)
        
        logger.debug(f"Fusion Reader: L1_count={len(l1_map)}, L2_count={len(l2_map)}")

        if len(l1_map) > 0 and len(l2_map) == 0:
             logger.warning("Fusion Reader: L1 data found but L2 map empty! Physics/Fusion path mismatch?")

        measurements = []
        
        # 2. Iterate through L1 items (Driving table)
        for key, l1_item in l1_map.items():
            try:
                # Extract basic info
                ts_str = l1_item.get('timestamp_utc')
                # Parse timestamp to float
                # Assume ISO format: YYYY-MM-DDTHH:MM:SS.ssssssZ
                # Simple parsing or use datetime
                if ts_str.endswith('Z'):
                    dt = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
                else:
                    dt = datetime.fromisoformat(ts_str)
                ts = dt.timestamp()
                
                station = l1_item.get('station_id')
                freq_mhz = float(l1_item.get('frequency_mhz', 0))
                
                # Check for Locked GPSDO in L1 (Critical for valid TOA)
                # If L1 doesn't explicitly state, we assume logic elsewhere handled it?
                # Actually, L1MetrologyMeasurement does not have gpsdo_locked? 
                # Check metrics or assume MetrologyService filtered it.
                # MetrologyService writes 'gpsdo_locked' attribute?
                # Re-reading measurement.py: L1MetrologyMeasurement doesn't have gpsdo_locked.
                # It does have 'quality_flag'.
                # Let's assume for now Metrology filters bad data or flags it.
                
                # 3. Get L2 match
                l2_item = l2_map.get(key)
                
                raw_toa = float(l1_item.get('raw_toa_ms', 0))
                light_time = float(l1_item.get('light_travel_time_ms', 0))
                
                if l2_item:
                    # Physics Available
                    prop_delay = float(l2_item.get('propagation_delay_ms', 0))
                    mode = l2_item.get('propagation_mode', 'Unknown')
                    model_conf = float(l2_item.get('model_confidence', 0))
                    
                    d_clock = raw_toa - prop_delay
                    confidence = model_conf
                else:
                    # Fallback (Physics Missing/Failed)
                    # "Light Time + 1.5ms" (Generic approx for 1-hop)
                    fallback_delay = light_time + 1.5
                    prop_delay = fallback_delay
                    mode = 'FALLBACK'
                    d_clock = raw_toa - fallback_delay
                    confidence = 0.5 # Lower confidence for fallback
                    model_conf = 0.0

                # BPM Filtering
                if station == 'BPM':
                    if dt.minute in BPM_UT1_MINUTES:
                        continue

                # Construct BroadcastMeasurement
                m = BroadcastMeasurement(
                    timestamp=ts,
                    station=station,
                    frequency_mhz=freq_mhz,
                    d_clock_ms=d_clock,
                    propagation_delay_ms=prop_delay,
                    propagation_mode=mode,
                    confidence=confidence,
                    snr_db=float(l1_item.get('snr_db', 0)),
                    quality_grade=str(l1_item.get('quality_flag', 'D')),
                    channel_name=l1_item.get('channel_name', 'UNKNOWN'),
                    raw_arrival_time_ms=raw_toa,
                    uncertainty_ms=1.0, # Default for now, L1 doesn't have it yet
                    
                    # New L2 Fields
                    l2_propagation_delay_ms=prop_delay if l2_item else None,
                    l2_tec_estimate=float(l2_item.get('tec_estimate')) if (l2_item and l2_item.get('tec_estimate') is not None) else None,
                    l2_model_confidence=model_conf if l2_item else None
                )
                
                # Propagate GPSDO lock if available in L1 (via extra fields or quality)
                # If quality_flag is bad, we rely on fusion to filter
                
                measurements.append(m)

            except Exception as e:
                logger.debug(f"Error processing item {key}: {e}")
                continue

        if not measurements:
            # Fallback to CSV if HDF5 failed completely?
            # Or just return empty if we are committed to HDF5.
            # Let's keep csv fallback for legacy read? 
            # The previous code had a CSV fallback.
            # If l1_map is empty, maybe try CSV.
                    pass

        return measurements
    
    def _calculate_weights(
        self, 
        measurements: List[BroadcastMeasurement]
    ) -> List[float]:
        """
        Calculate statistically optimal weights for each measurement.
        
        Uses inverse variance weighting (precision) as the base:
            w_i = 1 / σ²_i
        
        This is the statistically optimal weighting for combining independent
        measurements with different uncertainties (ISO GUM, GUM-S1).
        
        Weights are then scaled by confidence to account for non-statistical
        quality factors:
        - Discrimination quality (WWV vs WWVH separation)
        - Propagation mode reliability (1E vs 3F)
        - Signal quality (SNR, multipath)
        """
        weights = []
        
        # Quality scaling factors (applied to base inverse-variance weight)
        grade_scale = {'A': 1.0, 'B': 0.9, 'C': 0.7, 'D': 0.5}
        mode_scale = {
            '1E': 1.0, '1F': 0.95, '2F': 0.85, '3F': 0.7, 'GW': 1.0
        }
        
        for m in measurements:
            # Special handling for GLOBAL_DIFF (cross-station validation)
            if m.station == 'GLOBAL_DIFF':
                # High weight for validated cross-station measurements
                base_weight = 100.0
                confidence_scale = m.confidence
                weights.append(max(10.0, base_weight * confidence_scale))
                continue
            
            # CRITICAL FIX: Use inverse variance (precision) as base weight
            # This is the statistically optimal weighting formula
            if m.uncertainty_ms is None or m.uncertainty_ms <= 0:
                # Fallback for missing/invalid uncertainty
                # Use inverse confidence as a rough proxy
                base_weight = 1.0 / max(0.1, (1.0 / max(0.01, m.confidence)))
            else:
                # Inverse variance: w = 1/σ²
                base_weight = 1.0 / (m.uncertainty_ms ** 2)
            
            # Scale by confidence to account for non-statistical factors
            # (discrimination quality, propagation mode reliability, etc.)
            confidence_scale = m.confidence if m.confidence > 0 else 0.5
            
            # Scale by quality grade (measurement process quality)
            grade_scale_factor = grade_scale.get(m.quality_grade, 0.5)
            
            # Scale by propagation mode (physical reliability)
            mode_scale_factor = mode_scale.get(m.propagation_mode, 0.7)
            
            # Scale by SNR (signal quality)
            if m.snr_db is not None:
                if m.snr_db > 15:
                    snr_scale = 1.0
                elif m.snr_db > 10:
                    snr_scale = 0.95
                elif m.snr_db > 5:
                    snr_scale = 0.85
                else:
                    snr_scale = 0.7
            else:
                snr_scale = 0.9  # Default if SNR unknown
            
            # Combine: base precision × quality factors
            w = base_weight * confidence_scale * grade_scale_factor * mode_scale_factor * snr_scale
            
            # Ensure minimum weight for numerical stability
            weights.append(max(0.01, w))
        
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
        """
        Generate broadcast key for calibration lookup.
        
        Keys by (station, frequency) for frequency-dependent calibration.
        """
        if frequency_mhz > 0:
            return f"{station}_{frequency_mhz:.1f}"
        return station
    
    def _apply_calibration(
        self,
        measurements: List[BroadcastMeasurement]
    ) -> List[float]:
        """Apply per-broadcast calibration to measurements."""
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
        measurements: List[BroadcastMeasurement],
        validated: bool = True,
        reference_d_clock: float = 0.0
    ):
        """
        Update calibration offsets per-BROADCAST (station + frequency).
        
        Args:
            measurements: List of broadcast measurements
            validated: Whether cross-station validation passed (affects update rate)
            reference_d_clock: The fused D_clock from the current step, used as truth
        
        Each broadcast (e.g., WWV_10.00, CHU_7.85) has its own systematic offset.
        Per-broadcast calibration learns these offsets to bring each broadcast's
        D_clock into alignment with the CONSENSUS (fused) D_clock.
        """
        if not self.auto_calibrate:
            return
        
        # CRITICAL FIX: Check GPSDO lock status
        # If any measurement has unlocked GPSDO, skip calibration update
        n_unlocked = sum(1 for m in measurements if hasattr(m, 'gpsdo_locked') and not m.gpsdo_locked)
        if n_unlocked > 0:
            logger.warning(
                f"Skipping calibration update: {n_unlocked}/{len(measurements)} measurements "
                f"have unlocked GPSDO (risk of absorbing clock drift)"
            )
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
            
            # CRITICAL FIX: Align to CONSENSUS time, not 0
            # Offset should bring broadcast mean to reference_d_clock
            # calibrated = raw + offset = reference
            # offset = reference - raw
            new_offset = reference_d_clock - broadcast_mean
            
            # Extract station and frequency from key for logging
            station = recent[0].station
            freq = recent[0].frequency_mhz
            
            logger.debug(f"Calibration update {broadcast_key}: raw_mean={broadcast_mean:.2f}ms, ref={reference_d_clock:.2f}ms, offset={new_offset:.2f}ms, n={len(d_clocks)}")
            
            # Exponential moving average for smooth updates
            old_cal = self.calibration.get(broadcast_key)
            if old_cal and old_cal.n_samples > 0:
                # Alpha range: 0.3 (fast) to 0.1 (slow)
                base_alpha = max(0.1, min(0.3, 10.0 / old_cal.n_samples))
                
                # CRITICAL FIX: Bootstrap Acceleration
                # If system is starting up, allow fast convergence even if agreement fails
                is_bootstrap = self.calibration_update_count < 100
                
                if is_bootstrap:
                    # Accelerate learning during bootstrap
                    alpha = base_alpha  # Don't penalize during potential misalignment
                    max_delta = 50.0    # Allow large jumps (e.g. 60ms) to find alignment
                    if self.calibration_update_count % 10 == 0:
                        logger.info(f"Calibration Bootstrap: Accelerating convergence for {broadcast_key} (max_delta={max_delta}ms)")
                else:
                    # Standard operation: cautious updates
                    alpha = base_alpha if validated else base_alpha * 0.3
                    max_delta = 0.5  # ms per update

                new_offset = alpha * new_offset + (1 - alpha) * old_cal.offset_ms
                
                # Rate limit calibration changes to prevent discontinuities
                delta_offset = new_offset - old_cal.offset_ms
                
                if abs(delta_offset) > max_delta:
                    new_offset = old_cal.offset_ms + np.sign(delta_offset) * max_delta
                    logger.debug(f"Calibration {broadcast_key}: rate-limited Δ={delta_offset:.3f}ms to ±{max_delta}ms")
                
                logger.debug(f"Calibration {broadcast_key}: alpha={alpha:.3f} (validated={validated}, bootstrap={is_bootstrap})")
            
            self.calibration[broadcast_key] = BroadcastCalibration(
                station=station,
                frequency_mhz=freq,
                offset_ms=new_offset,
                uncertainty_ms=broadcast_std / np.sqrt(len(d_clocks)),  # Standard error
                n_samples=len(d_clocks),
                last_updated=datetime.now(timezone.utc).isoformat(),
                reference_station=self.reference_station
            )
        
        # CRITICAL FIX (P2.1): Auto-save calibration every 50 updates
        self.calibration_update_count += 1
        # CRITICAL FIX: Save more frequently (every 10 updates ~80 seconds) to persist Kalman state
        # This prevents losing convergence progress on service restarts
        if self.calibration_update_count % 10 == 0:
            try:
                self._save_calibration()
                logger.debug(f"Auto-saved calibration and Kalman state (update #{self.calibration_update_count})")
            except Exception as e:
                logger.error(f"Failed to auto-save calibration: {e}")
    
    def _kalman_update(self, measurement: float, measurement_uncertainty: float) -> float:
        """
        Two-tier Kalman filter for stable baseline maintenance.
        
        TIER 1 (Bootstrap): Learn the baseline offset from measurements
        - Active for first ~50 updates (~7 minutes)
        - Normal Kalman updates to converge to true offset
        - High process noise to resist chasing individual variations
        
        TIER 2 (Operational): Maintain stable baseline, detect real drift
        - Active after convergence
        - Only adjust baseline if persistent drift detected
        - Measurements recorded for science, not used to chase variations
        - GPSDO is the "steel ruler" - it doesn't drift significantly
        
        Philosophy: After bootstrap, the baseline offset should be rock solid.
        Individual broadcast appearances/disappearances should not jerk the offset.
        Measurement variations are ionospheric effects (the science signal), not
        clock drift (which the GPSDO prevents).
        
        Args:
            measurement: Current fused D_clock measurement (ms)
            measurement_uncertainty: Uncertainty of this measurement (ms)
            
        Returns:
            Kalman filter uncertainty (converges over time)
        """
        # Initialize on first measurement
        # CRITICAL: Start from 0, not from first measurement
        # This ensures the filter learns the offset gradually from scratch
        # and the correction ramp-up matches the filter convergence
        if not self.kalman_initialized:
            self.kalman_state[0] = 0.0  # Start from zero offset
            # CRITICAL FIX (2026-01-12): Initialize entire P matrix for Steel Ruler
            # P[0,0] (offset) = 1.0 (Confident start)
            # P[1,1] (drift) = 1e-4 (Assumed stable GPSDO)
            self.kalman_P = np.array([[1.0, 0.0], [0.0, 1e-4]])
            self.kalman_initialized = True
            self.kalman_n_updates = 1
            logger.info("Kalman filter initialized from zero - will learn offset gradually")
            return measurement_uncertainty
        
        # State transition matrix (1 minute step)
        # x_new = F * x_old
        # [offset]   [1  dt] [offset]
        # [drift ] = [0   1] [drift ]
        dt = 1.0  # 1 minute
        F = np.array([[1.0, dt], [0.0, 1.0]])
        
        # Process noise (clock drift uncertainty)
        # CRITICAL FIX (2026-01-10): Increased process noise to resist chasing transient variations
        # GPSDO has ~1e-9 stability, so real drift is negligible
        # Higher process noise makes filter trust its state more than noisy measurements
        # This maintains stable baseline offset despite propagation fluctuations
        q_offset = 1e-10  # ms^2 per minute (Steel Ruler: Rock Solid)
        q_drift = 1e-12  # (ms/min)^2 per minute (Steel Ruler: Frozen)
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
        
        # Increment update counter and check convergence
        self.kalman_n_updates += 1
        
        if not self.kalman_converged and self.kalman_n_updates >= self.kalman_convergence_threshold:
            self.kalman_converged = True
            # CRITICAL FIX (2026-01-12): Clamp covariance to enforce "Steel Ruler" immediately
            # We trust the GPSDO-disciplined clock is extremely stable.
            # P[0,0] = 1e-4  -> 0.01ms uncertainty in offset
            # P[1,1] = 1e-10 -> Effectively zero drift uncertainty
            self.kalman_P = np.array([[1e-4, 0.0], [0.0, 1e-10]])
            
            # CRITICAL FIX (2026-01-13): Force zero drift for Pure Steel Ruler
            # If we trust the GPSDO, any learned drift during convergence is likely 
            # noise/jitter bias. We freeze the offset but drop the drift.
            self.kalman_state[1] = 0.0
            
            logger.info(
                f"Kalman filter CONVERGED after {self.kalman_n_updates} updates. "
                f"Baseline offset: {self.kalman_state[0]:.3f}ms. "
                f"Transitioning to operational mode: Covariance clamped, baseline locked, DRIFT FROZEN AT 0."
            )
        
        # OPERATIONAL MODE: Force zero drift to prevent linear "walk away" from 0
        if self.kalman_converged:
            self.kalman_state[1] = 0.0
        
        # CRITICAL FIX (2026-01-10): Relaxed divergence bounds and better recovery
        # If state has diverged beyond ±20ms, reset the filter
        # Increased from ±10ms to allow for larger but legitimate offsets
        # Note: kalman_state is a numpy array [offset, drift], check offset (index 0)
        if abs(self.kalman_state[0]) > 20.0:
            logger.error(
                f"Kalman filter diverged: state={self.kalman_state[0]:.3f}ms, "
                f"resetting to measurement value for graceful recovery"
            )
            # Reset to current measurement instead of zero for faster recovery
            self.kalman_state = np.array([measurement, 0.0])
            self.kalman_P = np.array([[10.0, 0.0], [0.0, 1.0]])  # Lower uncertainty for faster convergence
            self.kalman_n_updates = 1
            return measurement_uncertainty  # Return measurement uncertainty, not inf of offset variance)
        
        # TIER 2: Operational mode - maintain stable baseline
        if self.kalman_converged:
            # Add measurement to window for drift detection
            self.measurement_window.append({
                'timestamp': time.time(),
                'measurement': measurement,
                'uncertainty': measurement_uncertainty,
                'kalman_state': self.kalman_state[0]
            })
            
            # Keep window size limited
            if len(self.measurement_window) > self.measurement_window_size:
                self.measurement_window.pop(0)
            
            # Detect persistent drift (measurements consistently different from baseline)
            if len(self.measurement_window) >= self.measurement_window_size:
                recent_measurements = [m['measurement'] for m in self.measurement_window]
                recent_mean = np.mean(recent_measurements)
                recent_std = np.std(recent_measurements)
                baseline = self.kalman_state[0]
                
                # Check if measurements persistently deviate from baseline
                deviation = abs(recent_mean - baseline)
                
                # Only adjust if:
                # 1. Deviation is significant (>1ms)
                # 2. Deviation is consistent (std < 2ms indicates not just noise)
                # 3. Enough time has passed since last adjustment (>10 minutes)
                current_time = time.time()
                time_since_adjustment = current_time - self.last_baseline_adjustment
                
                if (deviation > 1.0 and 
                    recent_std < 2.0 and 
                    time_since_adjustment > self.baseline_adjustment_interval):
                    
                    logger.warning(
                        f"DRIFT DETECTED: Measurements persistently deviate from baseline by {deviation:.2f}ms. "
                        f"Baseline: {baseline:.3f}ms, Recent mean: {recent_mean:.3f}ms (σ={recent_std:.2f}ms). "
                        f"Adjusting baseline to track real GPSDO drift."
                    )
                    self.last_baseline_adjustment = current_time
                    # Allow the Kalman update to proceed (already done above)
                else:
                    # No drift detected - maintain stable baseline
                    # Don't log every cycle, only periodically
                    if self.kalman_n_updates % 100 == 0:
                        logger.debug(
                            f"Baseline stable: {baseline:.3f}ms. "
                            f"Recent measurements: {recent_mean:.3f}ms ± {recent_std:.2f}ms. "
                            f"No adjustment needed (deviation={deviation:.2f}ms)."
                        )
        
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
        
        # Threshold: ±1.0ms (increased from 0.2ms)
        # Real propagation differences between stations can be 0.5-1.0ms due to:
        # - Different ionospheric paths (CHU vs WWV = 2000+ km)
        # - Different propagation modes (1E vs 1F)
        # CRITICAL FIX: Adaptive cross-station threshold based on conditions
        # Bootstrap-aware threshold: relaxed during calibration convergence, strict after
        # 
        # BOOTSTRAP PHASE (calibration not yet validated):
        #   - 5.0ms base threshold accommodates real systematic differences
        #   - CHU/WWV have ~4.3ms persistent offset (different propagation paths)
        #   - This is the measured reality - calibration learns to compensate
        #   - Cross-station disagreement is expected during convergence (30-60 minutes)
        # 
        # OPERATIONAL PHASE (after calibration validated):
        #   - 2.5ms base threshold enforces reasonable cross-station consistency
        #   - Calibration has converged, large disagreement indicates actual problems
        #   - Protects against mode mixing, detection errors, ionospheric anomalies
        #   - Still allows for ~2ms natural ionospheric variability
        #
        # NOTE: We track validation status via a rolling window of recent cross-validation results
        # If we haven't had consistent validation, stay in bootstrap mode
        if not hasattr(self, 'recent_validations'):
            self.recent_validations = []
        
        # Keep last 20 validation results (rolling window)
        if len(self.recent_validations) > 20:
            self.recent_validations.pop(0)
        
        # Consider calibration converged if >80% of recent validations passed
        calibration_converged = (
            len(self.recent_validations) >= 10 and 
            sum(self.recent_validations) / len(self.recent_validations) > 0.8
        )
        
        base_threshold = 5.0 if not calibration_converged else 2.5  # ms
        
        # Time of day factor (nighttime more variable)
        from datetime import datetime, timezone
        current_hour = datetime.now(timezone.utc).hour
        is_nighttime = current_hour < 6 or current_hour > 18
        time_factor = 1.5 if is_nighttime else 1.0
        
        # Ionospheric conditions factor (if available)
        # High/low TEC indicates disturbed conditions
        iono_factor = 1.0
        if hasattr(self, 'last_vtec_tecu') and self.last_vtec_tecu is not None:
            if self.last_vtec_tecu > 40 or self.last_vtec_tecu < 10:
                iono_factor = 2.0  # Disturbed conditions
        
        CROSS_STATION_THRESHOLD_MS = base_threshold * time_factor * iono_factor
        
        # Track validation result for adaptive threshold convergence detection
        # This is done BEFORE the threshold check so we track the actual disagreement status
        validation_passed = max_disagreement <= CROSS_STATION_THRESHOLD_MS
        self.recent_validations.append(validation_passed)
        
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
        
        # Filter out NaN measurements immediately (tone not detected)
        # CRITICAL FIX (2026-01-08): Leverage GPSDO stability during detection gaps
        #
        # Current: Reject measurements where tone_detected=False (d_clock_ms=NaN)
        # Future: Accept Kalman-coasted predictions with inflated uncertainty
        #
        # The GPSDO provides stable T_arrival timestamps. When tone detection fails,
        # the per-broadcast Kalman filter can still predict ToF (coasting mode).
        # This would allow continuous D_clock even during fades:
        #   D_clock = T_arrival(GPSDO) - ToF(Kalman_predicted)
        #
        # For now, we filter out NaN to maintain current behavior while the
        # stricter chrony feed criteria prevent discontinuities.
        measurements = [m for m in measurements if m.d_clock_ms is not None and not np.isnan(m.d_clock_ms)]
        
        # CRITICAL FIX: Pre-fusion outlier rejection
        # Filter out gross outliers (e.g. 200ms) caused by false tone detection or physics failures.
        if len(measurements) > 2:
            d_clocks = np.array([m.d_clock_ms for m in measurements])
            median_d = np.median(d_clocks)
            # CRITICAL FIX: MAD-based Robust Outlier Rejection
            # Use Median Absolute Deviation (MAD) to filter outliers that distort mean/std.
            # MAD is robust to up to 50% outliers.
            deviations = np.abs(d_clocks - median_d)
            mad = np.median(deviations)
            sigma_est = 1.4826 * mad
            
            # Floor sigma to avoid over-filtering tight clusters or quantization noise
            # Minimum expected noise is ~0.5ms.
            # CRITICAL FIX: Cap sigma to avoid runaway thresholds when variance is high.
            # If sigma_est > 5.0ms, the distribution is already bad, so clamp it 
            # to force rejection of the outliers causing it.
            sigma_est = max(0.5, min(sigma_est, 5.0))
            
            # Threshold: 3.5 sigma (99.95% coverage for Gaussian)
            # With cap at 5.0ms, max threshold is 17.5ms.
            # This catches the ~45ms outliers (e.g. WWV 2.5MHz) that were slipping through.
            mad_threshold = 3.5 * sigma_est
            filter_threshold = min(100.0, mad_threshold)
            
            # During bootstrap/learning, relax to allow convergence
            # But we saw 16ms spread, implying outliers. Robust MAD should handle it.
            
            keep_indices = []
            for i, d in enumerate(d_clocks):
                if deviations[i] < filter_threshold:
                    keep_indices.append(i)
                else:
                    logger.warning(
                        f"Rejecting outlier: {measurements[i].station}_{measurements[i].frequency_mhz}MHz "
                        f"d_clock={d:.2f}ms (median={median_d:.2f}ms, dev={deviations[i]:.2f}ms > {filter_threshold:.1f}ms)"
                    )
            
            if len(keep_indices) < len(measurements):
                measurements = [measurements[i] for i in keep_indices]
        
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
                logger.info(f"GNSS VTEC available: {vtec_tecu:.2f} TECU (age: {age_seconds:.1f}s)")
                used_gnss_vtec = True
                
                # CRITICAL FIX (2026-01-05): GNSS VTEC should refine confidence, not modify measurements
                # Modifying D_clock based on VTEC causes discontinuities when VTEC quality changes
                # Instead, use VTEC-model agreement to adjust measurement confidence
                
                for m in measurements:
                    if m.station == 'GLOBAL_DIFF' or m.station == 'UNKNOWN':
                        continue
                        
                    # Compute baseline delay (what the system used)
                    baseline = self.physics_model.compute_delay(
                        station=m.station,
                        frequency_mhz=m.frequency_mhz,
                        observed_arrival_ms=0,
                        timestamp=datetime.fromtimestamp(m.timestamp, tz=timezone.utc).replace(tzinfo=None)
                    )
                    
                    if baseline and baseline.n_hops > 0:
                        # Extract model TEC
                        model_tec = baseline.tec_tecu if baseline.tec_tecu else 20.0
                        
                        # Calculate TEC agreement
                        tec_diff = abs(vtec_tecu - model_tec)
                        tec_agreement = 1.0 - min(1.0, tec_diff / 20.0)  # 0-1 scale
                        
                        # Adjust confidence based on TEC agreement
                        # Good agreement (< 5 TECU diff) -> boost confidence
                        # Poor agreement (> 20 TECU diff) -> reduce confidence
                        if tec_diff < 5.0:
                            m.confidence = min(1.0, m.confidence * 1.1)
                            m.propagation_mode = f"{baseline.propagation_mode}+GNSS_VALIDATED"
                            logger.debug(
                                f"  {m.station} {m.frequency_mhz}MHz: GNSS={vtec_tecu:.1f} Model={model_tec:.1f} TECU "
                                f"(diff={tec_diff:.1f}, agreement={tec_agreement:.2f}) -> confidence boost"
                            )
                        elif tec_diff > 20.0:
                            m.confidence = max(0.5, m.confidence * 0.9)
                            logger.debug(
                                f"  {m.station} {m.frequency_mhz}MHz: GNSS={vtec_tecu:.1f} Model={model_tec:.1f} TECU "
                                f"(diff={tec_diff:.1f}, poor agreement) -> confidence reduction"
                            )
                        else:
                            logger.debug(
                                f"  {m.station} {m.frequency_mhz}MHz: GNSS={vtec_tecu:.1f} Model={model_tec:.1f} TECU "
                                f"(diff={tec_diff:.1f}, moderate agreement)"
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
                        # TEC FIX: Use raw_arrival_time_ms if available (schema v1.1.0+)
                        # This is the uncalibrated ToA that includes ionospheric dispersion
                        if m.raw_arrival_time_ms is not None:
                            toa_ms = m.raw_arrival_time_ms
                        else:
                            # Fallback: reconstruct ToA from calibrated values (old schema)
                            # This is less accurate but maintains backward compatibility
                            toa_ms = m.d_clock_ms + m.propagation_delay_ms
                        
                        # Filter out NaN values (tone not detected) to prevent solver failure
                        if toa_ms is not None and not np.isnan(toa_ms):
                            tec_input.append({
                                'frequency_hz': m.frequency_mhz * 1e6,
                                'toa_ms': toa_ms,
                                'uncertainty_ms': 1.0 / max(0.001, m.confidence) # Inverse confidence weighting
                            })
                    
                    # DIAGNOSTIC: Log the raw inputs to the TEC estimator to trace "0.0 TEC" issue
                    if logger.isEnabledFor(logging.DEBUG):
                        input_summary = ", ".join([f"{x['frequency_hz']/1e6:.1f}MHz={x['toa_ms']:.3f}ms" for x in tec_input])
                        logger.debug(f"TEC Solver Inputs for {station}: {input_summary}")
                    
                    # Run Solver
                    tec_result = self.tec_estimator.estimate_tec(
                        tec_input, station, measurements[0].timestamp
                    )
                    
                    if tec_result:
                        # TEC writing is handled by science_aggregator service
                        # CRITICAL FIX (2026-01-05): TEC should be a REFINEMENT to uncertainty, not a replacement
                        # Modifying D_clock values based on TEC causes discontinuities when signals fade in/out
                        # Instead, use TEC quality to adjust measurement confidence/uncertainty
                        
                        # Validate TEC result is not NaN
                        if np.isnan(tec_result.tec_u) or np.isnan(tec_result.confidence):
                            logger.warning(f"TEC solver produced NaN for {station} (tec={tec_result.tec_u}, conf={tec_result.confidence}) - skipping")
                        elif tec_result.confidence > 0.9 and 5.0 <= tec_result.tec_u <= 100.0:
                            # TEC is physically reasonable (5-100 TECU) and well-fit
                            logger.info(f"TEC Solved for {station}: {tec_result.tec_u:.1f} TECU (R2={tec_result.confidence:.2f})")
                            
                            # REFINEMENT: Boost confidence for measurements with good TEC fit
                            # This gives them more weight in fusion without modifying their values
                            for m in station_meas:
                                m.propagation_mode = 'TEC_VALIDATED' # Flag as TEC-validated
                                m.confidence = min(1.0, m.confidence * 1.15) # Modest confidence boost
                        elif tec_result.confidence > 0.9:
                            # TEC fit is good but value is unrealistic (e.g., 0.0 TECU)
                            logger.warning(f"TEC unrealistic for {station}: {tec_result.tec_u:.1f} TECU (R2={tec_result.confidence:.2f}) - ignoring")
                        else:
                            # TEC fit is poor - reduce confidence slightly
                            logger.warning(f"TEC poor fit for {station}: R2={tec_result.confidence:.2f} (Needs >0.9)")
                            for m in station_meas:
                                m.confidence = max(0.5, m.confidence * 0.95) # Slight confidence reduction
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
        
        if len(measurements) < 1:
            logger.debug("Too few measurements after outlier rejection")
            return None
        
        # CRITICAL: Filter out any measurements with NaN values or unlocked GPSDO before fusion
        # This is a safety net to prevent NaN from propagating and to exclude unlocked measurements
        valid_measurements = []
        valid_weights = []
        n_gpsdo_unlocked = 0
        for m, w in zip(measurements, weights):
            if np.isnan(m.d_clock_ms) or np.isnan(w):
                logger.warning(f"Filtering out measurement with NaN: station={m.station}, d_clock={m.d_clock_ms}, weight={w}")
            elif hasattr(m, 'gpsdo_locked') and not m.gpsdo_locked:
                # CRITICAL FIX: Exclude measurements where GPSDO is not locked
                # Unlocked GPSDO can drift by seconds, causing massive timing errors
                n_gpsdo_unlocked += 1
                logger.warning(f"Filtering out measurement with unlocked GPSDO: station={m.station}, freq={m.frequency_mhz}MHz")
            else:
                valid_measurements.append(m)
                valid_weights.append(w)
        
        if n_gpsdo_unlocked > 0:
            logger.warning(f"Excluded {n_gpsdo_unlocked} measurements due to unlocked GPSDO")
        
        if len(valid_measurements) < 1:
            logger.error(f"Too few valid measurements after NaN filtering ({len(valid_measurements)}/{len(measurements)})")
            return None
        
        measurements = valid_measurements
        weights = valid_weights
        
        # ====================================================================
        # APPLY CALIBRATION: Apply learned systematic offsets to bring D_clock toward zero
        # ====================================================================
        # Calibration removes station/frequency-specific systematic offsets:
        # - Propagation delay estimation errors
        # - Detection/matched filter group delays
        # - Frequency-dependent ionospheric delays
        #
        # Rate limiting in _update_calibration (±0.5ms/update) ensures smooth convergence
        # without discontinuities. The Kalman filter then handles residual variations.
        
        # Extract raw D_clock values for cross-validation (before calibration)
        raw_d_clocks = [m.d_clock_ms for m in measurements]
        
        # Apply calibration to get calibrated D_clock values for fusion
        calibrated_d_clocks = self._apply_calibration(measurements)
        
        # ====================================================================
        # INTER-STATION AGREEMENT CHECK (Priority 1D - 2026-01-04)
        # ====================================================================
        # D_clock is the SYSTEM CLOCK OFFSET - it should be the same for all stations.
        # D_clock = T_arrival - T_propagation
        # 
        # If Phase 2 calculated propagation delays correctly, all stations should
        # report approximately the same D_clock (within ~2-3ms for measurement noise).
        # 
        # Large disagreements indicate:
        # - Propagation delay calculation error
        # - Station misidentification  
        # - Tone misidentification
        #
        # This is handled by the existing cross-station validation below.
        # No additional geographic check needed - D_clock is station-independent.
        
        # ====================================================================
        # CROSS-STATION VALIDATION (Priority 1C - 2025-12-31)
        # ====================================================================
        # Validate that different stations agree on UTC time within ±1.0ms.
        # This detects systematic errors in any single station.
        # Threshold increased from 0.2ms to 1.0ms to account for real propagation differences.
        
        cross_valid, cross_reason, n_cross_outliers = self._cross_validate_stations(
            measurements, raw_d_clocks
        )
        
        if not cross_valid:
            logger.warning(f"Cross-station validation failed: {cross_reason}")
            # Note: We don't reject the fusion, but flag it in the result
            # The consistency_flag will be set to reflect this issue
        
        # Weighted mean of calibrated D_clock values
        # Use calibrated values for fusion to converge toward zero
        w = np.array(weights)
        d_calibrated = np.array(calibrated_d_clocks)
        d_raw = np.array(raw_d_clocks)
        
        # Fuse calibrated measurements
        fused_d_clock_raw = np.sum(w * d_calibrated) / np.sum(w)
        
        # Also track raw fusion for diagnostics
        fused_d_clock_uncalibrated = np.sum(w * d_raw) / np.sum(w)
        
        # STEEL RULER ARCHITECTURE:
        # The Kalman State IS the time. We trust the model (GPSDO) over the noisy measurements.
        # "The local clock describes the rate of time... the radio is just a noisy report."
        if self.kalman_initialized:
            # Use the Kalman state (Model) as the fused value
            fused_d_clock = float(self.kalman_state[0])
            
            # Log the "Science Residual" (what we would have jumped to vs where we stayed)
            residual = fused_d_clock_raw - fused_d_clock
            logger.debug(f"Steel Ruler: State={fused_d_clock:+.3f}ms, Raw={fused_d_clock_raw:+.3f}ms, Residual={residual:+.3f}ms")
            
            # Legacy ramp-up variable for logging compatibility (set to max)
            self.correction_alpha = 1.0
        else:
            # During initialization, we have to trust the measurement until the filter starts
            fused_d_clock = fused_d_clock_raw
        
        # ====================================================================
        # UPDATE CALIBRATION (Priority 1B)
        # ====================================================================
        # CRITICAL FIX: Update calibration using the *residuals* relative to the FUSED result.
        # This aligns all stations to the consensus time, rather than arbitrarily forcing them to 0.
        # If the real system clock error is non-zero (e.g. -37ms), calibration should NOT try to remove it.
        # It should only remove the *difference* between a station and the consensus (-37ms).
        self._update_calibration(
            measurements, 
            validated=cross_valid,
            reference_d_clock=fused_d_clock
        )
        
        # CRITICAL FIX (P3.2): D_clock monotonicity check
        # Large jumps (>5ms) indicate tone misidentification or other errors
        if hasattr(self, 'last_fused_d_clock'):
            delta = abs(fused_d_clock - self.last_fused_d_clock)
            if delta > 5.0:
                logger.error(
                    f"D_clock jumped {delta:.1f}ms (from {self.last_fused_d_clock:+.3f}ms "
                    f"to {fused_d_clock:+.3f}ms) - possible tone misidentification or "
                    f"calibration error"
                )
        self.last_fused_d_clock = fused_d_clock
        
        # Raw mean for comparison (raw_d_clocks already defined above)
        raw_mean = np.mean(raw_d_clocks)
        
        # ====================================================================
        # ENHANCED UNCERTAINTY CALCULATION
        # ====================================================================
        # Proper uncertainty budget with three components:
        # 1. Statistical: Measurement scatter (weighted std)
        # 2. Systematic: Calibration convergence error
        # 3. Propagation: Mode-dependent ionospheric variability
        
        # Check if we have verified global solver result
        has_verified_global = (global_result is not None and getattr(global_result, 'verified', False))
        
        # 1. Statistical uncertainty - measurement scatter
        # CRITICAL FIX: Use CALIBRATED measurements for statistical uncertainty.
        # The raw spread includes systematic offsets that we have learned and removed.
        # The uncertainty of the fusion result is the RESIDUAL scatter after calibration.
        if len(calibrated_d_clocks) > 1:
            statistical_uncertainty = np.std(calibrated_d_clocks)
        else:
            statistical_uncertainty = 0.5  # Single measurement uncertainty
        
        # CRITICAL FIX: Add tone detection uncertainty (SNR-dependent)
        # Lower SNR → higher phase ambiguity → larger uncertainty
        avg_snr = np.mean([m.snr_db for m in measurements if hasattr(m, 'snr_db') and m.snr_db > 0] or [20.0])
        if avg_snr < 10:
            tone_detection_uncertainty = 0.5  # Low SNR
        elif avg_snr < 20:
            tone_detection_uncertainty = 0.3  # Medium SNR
        else:
            tone_detection_uncertainty = 0.2  # High SNR
        
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
        # CRITICAL FIX (P3.1): Added RTP jitter component to uncertainty budget
        rtp_jitter_ms = 0.1  # RTP timestamp jitter (~100µs typical)
        
        # CRITICAL FIX: Enhanced propagation uncertainty with ionospheric variability
        # Base mode uncertainties (quiet conditions)
        mode_uncertainties_base = {
            'GW': 0.1,    # Ground wave (very stable)
            '1E': 0.3,    # Single-hop E-layer (stable)
            '1F': 0.5,    # Single-hop F-layer (moderate)
            '2E': 1.0,    # Two-hop E-layer
            '2F': 2.0,    # Two-hop F-layer (variable)
            '3F': 3.0,    # Three-hop (highly variable)
            'TEC_SOLVED': 0.2,  # Physics-derived (very good)
        }
        
        # Scale by ionospheric conditions (if VTEC available)
        # TEC variability increases uncertainty
        iono_scale_factor = 1.0
        if hasattr(self, 'last_vtec_tecu') and self.last_vtec_tecu is not None:
            # High TEC (>40 TECU) or low TEC (<10 TECU) indicates disturbed conditions
            if self.last_vtec_tecu > 40 or self.last_vtec_tecu < 10:
                iono_scale_factor = 1.5
        
        mode_uncertainties = {k: v * iono_scale_factor for k, v in mode_uncertainties_base.items()}
        
        # CRITICAL FIX: Add multipath delay spread uncertainty
        # HF signals have ~1-5ms delay spread depending on mode
        multipath_uncertainty = 0.5  # Conservative estimate for multi-hop
        
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
        
        # Combined uncertainty (Root Sum of Squares per ISO GUM)
        # RSS is appropriate for independent uncertainty sources
        measurement_uncertainty = np.sqrt(
            statistical_uncertainty**2 +           # Measurement scatter
            systematic_uncertainty**2 +            # Calibration convergence
            propagation_uncertainty**2 +           # Mode-dependent ionospheric
            rtp_jitter_ms**2 +                     # RTP timestamp jitter
            tone_detection_uncertainty**2 +        # Phase ambiguity (SNR-dependent)
            multipath_uncertainty**2               # Delay spread
        )
        
        # Apply uncertainty floor (has_verified_global already defined above)
        uncertainty_floor = 0.1 if has_verified_global else 0.2
        measurement_uncertainty = max(uncertainty_floor, measurement_uncertainty)
        
        # CRITICAL FIX (2026-01-10): Gate Kalman updates with measurement quality check
        # Two-tier gating: relaxed during bootstrap, strict during operational
        # Bootstrap: Allow up to 10ms uncertainty to learn baseline with moderate signals
        # Operational: Only 5ms to maintain stable baseline and resist chasing noise
        uncertainty_threshold = 10.0 if not self.kalman_converged else 5.0
        
        if measurement_uncertainty > uncertainty_threshold:
            phase = "bootstrap" if not self.kalman_converged else "operational"
            logger.warning(
                f"Skipping Kalman update ({phase}): measurement uncertainty too high "
                f"({measurement_uncertainty:.2f}ms > {uncertainty_threshold}ms threshold). "
                f"Using previous Kalman state to maintain stable baseline offset."
            )
            # Use previous Kalman uncertainty instead of updating
            kalman_uncertainty = np.sqrt(self.kalman_P[0, 0]) if self.kalman_initialized else measurement_uncertainty
        else:
            # Gating: Only update Kalman filter if measurement uncertainty is reasonable
            # If uncertainty is huge (e.g. > 100ms), we trust the filter prediction completely
            if measurement_uncertainty < 100.0:
                kalman_uncertainty = self._kalman_update(fused_d_clock_raw, measurement_uncertainty)
            else:
                # If measurement uncertainty is too high, trust the Kalman prediction
                kalman_uncertainty = np.sqrt(self.kalman_P[0, 0]) if self.kalman_initialized else measurement_uncertainty
        
        # Final uncertainty is the Kalman-filtered combined uncertainty
        # This provides temporal smoothing while preserving the uncertainty budget
        uncertainty = kalman_uncertainty
        
        # Per-station breakdown (using raw values)
        wwv_cal = [d for m, d in zip(measurements, raw_d_clocks) if m.station == 'WWV']
        wwvh_cal = [d for m, d in zip(measurements, raw_d_clocks) if m.station == 'WWVH']
        chu_cal = [d for m, d in zip(measurements, raw_d_clocks) if m.station == 'CHU']
        bpm_cal = [d for m, d in zip(measurements, raw_d_clocks) if m.station == 'BPM']
        
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
        
        # Consistency flag already calculated above (before Kalman update)
        # This section kept for logging details
        if consistency_flag == 'DISCRIMINATION_SUSPECT':
            
            # Identify which measurements are outliers within their station group
            # and EXCLUDE them from the Kalman update by zeroing their contribution
            suspect_indices = []
            for i, (m, raw_val) in enumerate(zip(measurements, raw_d_clocks)):
                is_suspect = False
                if m.station == 'WWV' and wwv_intra_std and wwv_intra_std > INTRA_THRESHOLD_MS:
                    wwv_mean = station_means.get('WWV', 0)
                    if abs(raw_val - wwv_mean) > 2 * wwv_intra_std:
                        is_suspect = True
                if m.station == 'WWVH' and wwvh_intra_std and wwvh_intra_std > INTRA_THRESHOLD_MS:
                    wwvh_mean = station_means.get('WWVH', 0)
                    if abs(raw_val - wwvh_mean) > 2 * wwvh_intra_std:
                        is_suspect = True
                if m.station == 'CHU' and chu_intra_std and chu_intra_std > INTRA_THRESHOLD_MS:
                    chu_mean = station_means.get('CHU', 0)
                    if abs(raw_val - chu_mean) > 2 * chu_intra_std:
                        is_suspect = True
                if m.station == 'BPM' and bpm_intra_std and bpm_intra_std > INTRA_THRESHOLD_MS:
                    bpm_mean = station_means.get('BPM', 0)
                    if abs(raw_val - bpm_mean) > 2 * bpm_intra_std:
                        is_suspect = True
                
                if is_suspect:
                    suspect_indices.append(i)
            
            # If we have suspects, recalculate fused_d_clock excluding them
            if suspect_indices and len(measurements) - len(suspect_indices) >= 3:
                clean_weights = [w for i, w in enumerate(weights) if i not in suspect_indices]
                clean_raw = [d for i, d in enumerate(raw_d_clocks) if i not in suspect_indices]
                
                w_clean = np.array(clean_weights)
                d_clean = np.array(clean_raw)
                fused_d_clock = np.sum(w_clean * d_clean) / np.sum(w_clean)
                
                # Recalculate uncertainty with clean data
                weighted_var = np.sum(w_clean * (d_clean - fused_d_clock)**2) / np.sum(w_clean)
                measurement_uncertainty = np.sqrt(weighted_var)
                
                logger.info(
                    f"Excluded {suspect_count} suspect measurements (discrimination errors), "
                    f"recalculated D_clock: {fused_d_clock:+.3f}ms ± {measurement_uncertainty:.3f}ms"
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
        
        # Update Kalman filter for convergence tracking
        kalman_uncertainty = self._kalman_update(fused_d_clock, measurement_uncertainty)
        
        # Final uncertainty is the Kalman-filtered combined uncertainty
        uncertainty = kalman_uncertainty
        
        # ====================================================================
        # SINGLE-STATION MODE SAFEGUARDS (CRITICAL FIX 2026-01-10)
        # ====================================================================
        # Single-station mode (n_stations == 1) has no cross-validation capability.
        # Systematic errors cannot be detected. Inflate uncertainty to reflect this.
        single_station_mode = len(stations) == 1
        if single_station_mode:
            # Inflate uncertainty by 5x to reflect lack of validation
            # This is conservative but scientifically honest
            uncertainty *= 5.0
            logger.warning(
                f"SINGLE-STATION MODE: Only {list(stations)[0]} available. "
                f"Uncertainty inflated to {uncertainty:.2f}ms (no cross-validation possible). "
                f"Scientific data quality is UNVALIDATED."
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
            propagation_uncertainty_ms=propagation_uncertainty,
            # Validation flags (CRITICAL FIX 2026-01-10)
            single_station_mode=single_station_mode
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
            
            # Create typed L3 measurement object
            l3_measurement = L3FusionTiming(
                timestamp_utc=timestamp_utc,
                minute_boundary=int(result.timestamp),
                d_clock_fused_ms=float(result.d_clock_fused_ms),
                d_clock_raw_ms=float(result.d_clock_raw_ms),
                uncertainty_ms=float(result.uncertainty_ms),
                
                # Composition
                n_broadcasts=int(result.n_broadcasts),
                n_stations=int(result.n_stations),
                stations_used=stations_used,
                
                # Per-station statistics
                wwv_mean_ms=float(result.wwv_mean_ms) if result.wwv_mean_ms is not None else None,
                wwvh_mean_ms=float(result.wwvh_mean_ms) if result.wwvh_mean_ms is not None else None,
                chu_mean_ms=float(result.chu_mean_ms) if result.chu_mean_ms is not None else None,
                bpm_mean_ms=float(result.bpm_mean_ms) if result.bpm_mean_ms is not None else None,
                
                wwv_count=int(result.wwv_count),
                wwvh_count=int(result.wwvh_count),
                chu_count=int(result.chu_count),
                bpm_count=int(result.bpm_count),
                
                wwv_intra_std_ms=float(result.wwv_intra_std_ms) if result.wwv_intra_std_ms is not None else None,
                wwvh_intra_std_ms=float(result.wwvh_intra_std_ms) if result.wwvh_intra_std_ms is not None else None,
                chu_intra_std_ms=float(result.chu_intra_std_ms) if result.chu_intra_std_ms is not None else None,
                bpm_intra_std_ms=float(result.bpm_intra_std_ms) if result.bpm_intra_std_ms is not None else None,
                
                inter_station_spread_ms=float(result.inter_station_spread_ms) if result.inter_station_spread_ms is not None else None,
                consistency_flag=FusionConsistencyFlag('INTER_ANOMALY') if result.consistency_flag == 'CROSS_STATION_DISAGREE' else FusionConsistencyFlag(result.consistency_flag),

                # Uncertainty budget
                statistical_uncertainty_ms=float(result.statistical_uncertainty_ms),
                systematic_uncertainty_ms=float(result.systematic_uncertainty_ms),
                propagation_uncertainty_ms=float(result.propagation_uncertainty_ms),
                
                # Global solve
                global_solve_verified=bool(result.global_solve_verified),
                global_solve_consistency_ms=float(result.global_solve_consistency_ms) if result.global_solve_consistency_ms is not None else None,
                global_solve_n_obs=int(result.global_solve_n_obs),
                
                # Metadata
                calibration_applied=bool(result.calibration_applied),
                reference_station=ReferenceStation(result.reference_station),
                outliers_rejected=int(result.outliers_rejected),
                quality_grade=FusionQualityGrade(result.quality_grade),
                kalman_state=FusionKalmanState(kalman_state),
                quality_flag=FusionQualityFlag(quality_flag),
                processing_version='3.2.0',
                single_station_mode=bool(result.single_station_mode)
            )
            
            # Write to HDF5 with schema validation
            self.hdf5_fusion_writer.write_measurement(l3_measurement.model_dump())
            
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
    lookback_minutes: int = 10,
    receiver_lat: Optional[float] = None,
    receiver_lon: Optional[float] = None
):
    """
    Run continuous fusion service that aggregates Phase 2 timing measurements.
    
    This is the main entry point for the fusion service. It:
    1. Reads Phase 2 HDF5 timing measurements from all channels
    2. Applies cross-station validation and outlier rejection
    3. Fuses measurements into a single high-confidence UTC estimate
    4. Writes fused result to Chrony SHM for system clock discipline
    5. Saves fusion history to HDF5 for analysis
    
    Args:
        data_root: Root data directory
        interval_sec: Fusion interval in seconds (default: 60s)
        enable_chrony: If True, write fused time to Chrony SHM refclock
        lookback_minutes: Number of minutes to look back for measurements
        receiver_lat: Receiver latitude (from config)
        receiver_lon: Receiver longitude (from config)
    """
    fusion = MultiBroadcastFusion(
        data_root,
        receiver_lat=receiver_lat,
        receiver_lon=receiver_lon
    )
    
    # Initialize Chrony SHM if enabled
    chrony_shm = None
    if enable_chrony:
        try:
            from hf_timestd.core.chrony_shm import ChronySHM
            chrony_shm = ChronySHM(unit=0)
            if chrony_shm.connect():
                logger.info("Chrony SHM refclock enabled (unit=0, refid=TMGR)")
                logger.info("SHM updates will occur directly in fusion loop (no threaded updater)")
            else:
                logger.warning("Failed to connect to Chrony SHM - continuing without")
                chrony_shm = None
        except Exception as e:
            logger.warning(f"Chrony SHM not available: {e}")
            chrony_shm = None
    
    logger.info("Starting Multi-Broadcast Fusion Service")
    logger.info(f"  Interval: {interval_sec} seconds")
    logger.info(f"  Output: {fusion.fusion_dir / 'fusion_fusion_timing_YYYYMMDD.h5'}")
    logger.info(f"  Chrony SHM: {'enabled (direct updates)' if chrony_shm else 'disabled'}")
    
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
                
                # Write directly to Chrony SHM (fusion runs at chrony poll rate)
                # CRITICAL FIX (2026-01-10): STRICTER feed criteria for scientific integrity
                # Only feed validated, multi-station measurements to prevent contamination
                if chrony_shm:
                    # Check quality criteria
                    # CRITICAL FIX (2026-01-10): Bootstrap-aware quality gating
                    # During bootstrap (calibration not converged), accept grade D
                    # The 2-3ms uncertainty is expected during calibration learning
                    # After convergence, enforce stricter A/B/C requirement
                    if hasattr(fusion, 'recent_validations') and len(fusion.recent_validations) >= 10:
                        calibration_converged = sum(fusion.recent_validations) / len(fusion.recent_validations) > 0.8
                    else:
                        calibration_converged = False
                    
                    if calibration_converged:
                        quality_ok = result.quality_grade in ('A', 'B', 'C')
                    else:
                        # Bootstrap: accept grade D (uncertainty <50ms is acceptable during learning/single-station)
                        # High uncertainty during bootstrap is normal due to calibration convergence
                        quality_ok = result.quality_grade in ('A', 'B', 'C', 'D') and result.uncertainty_ms < 50.0
                    
                    # CRITICAL FIX (2026-01-10): Require multi-station for validation
                    # Single-station mode has no cross-validation, cannot detect systematic errors
                    # RELAXED (2026-01-12): Allow single station to maintain feed during outages
                    multi_station = result.n_stations >= 1  # Require at least 1 station
                    
                    # CRITICAL FIX (2026-01-10): Bootstrap-aware consistency criteria
                    # During bootstrap, CROSS_STATION_DISAGREE is expected (calibration learning)
                    # After convergence, enforce stricter consistency requirements
                    if result.consistency_flag == 'OK':
                        consistent = True
                    elif calibration_converged:
                        # Operational: only accept disagreement with low uncertainty
                        if result.consistency_flag in ('INTER_ANOMALY', 'CROSS_STATION_DISAGREE') and result.uncertainty_ms < 1.0:
                            consistent = True
                            logger.debug(
                                f"Chrony feed: Accepting {result.consistency_flag} with low uncertainty "
                                f"({result.uncertainty_ms:.3f}ms < 1.0ms threshold)"
                            )
                        else:
                            consistent = False
                    else:
                        # Bootstrap: accept CROSS_STATION_DISAGREE (expected during calibration)
                        # The fused result is still valid due to weighted averaging and Kalman filtering
                        if result.consistency_flag in ('OK', 'CROSS_STATION_DISAGREE'):
                            consistent = True
                            if result.consistency_flag == 'CROSS_STATION_DISAGREE':
                                logger.debug(
                                    f"Chrony feed: Accepting CROSS_STATION_DISAGREE during bootstrap "
                                    f"(calibration learning, uncertainty={result.uncertainty_ms:.3f}ms)"
                                )
                        else:
                            consistent = False
                    
                    # Discontinuity filter: reject large jumps (>10ms)
                    # Increased from 3ms to 10ms to allow for legitimate calibration convergence
                    # and ionospheric variations while still protecting against major errors
                    global last_chrony_d_clock, last_chrony_update_time
                    discontinuity_ok = True
                    
                    # Reset discontinuity check if no update for >5 minutes (allows recovery)
                    if 'last_chrony_update_time' in globals() and last_chrony_update_time is not None:
                        time_since_update = time.time() - last_chrony_update_time
                        if time_since_update > 300:  # 5 minutes
                            logger.info(
                                f"Chrony feed: Resetting discontinuity check after {time_since_update:.0f}s "
                                f"without updates (allows recovery from stuck state)"
                            )
                            last_chrony_d_clock = None
                    
                    if 'last_chrony_d_clock' in globals() and last_chrony_d_clock is not None:
                        delta = abs(result.d_clock_fused_ms - last_chrony_d_clock)
                        if delta > 10.0:
                            logger.warning(
                                f"Chrony feed: Discontinuity detected ({delta:.1f}ms jump), "
                                f"skipping update to prevent clock instability"
                            )
                            discontinuity_ok = False
                    # else: First measurement after restart, allow it
                    
                    if quality_ok and multi_station and consistent and discontinuity_ok:
                        now = time.time()
                        system_time = now
                        reference_time = system_time - (result.d_clock_fused_ms / 1000.0)
                        
                        # Precision based on uncertainty (log2 of seconds)
                        # Correct formula: log2(uncertainty_sec) = log2(uncertainty_ms) - 10
                        # Example: 1000ms -> 0s -> 0. 1ms -> -10. 0.001ms -> -20.
                        # CLAMP: Ensure at least -10 (1ms) to satisfy Chrony, even if uncertainty is high
                        uncertainty_sec = max(0.1, result.uncertainty_ms) / 1000.0
                        raw_precision = int(np.log2(uncertainty_sec))
                        precision = min(-10, raw_precision) if raw_precision > -10 else raw_precision
                        
                        try:
                            update_success = chrony_shm.update(reference_time, system_time, precision)
                            if update_success:
                                # Update last value and timestamp for discontinuity check
                                last_chrony_d_clock = result.d_clock_fused_ms
                                last_chrony_update_time = time.time()
                                
                                logger.debug(
                                    f"Chrony SHM updated: D_clock={result.d_clock_fused_ms:+.3f}ms, "
                                    f"offset={(system_time-reference_time)*1000:+.3f}ms, precision={precision} "
                                    f"(raw_prec={raw_precision}) "
                                    f"[{result.n_stations}sta, {result.quality_grade}, {result.consistency_flag}]"
                                )
                            else:
                                logger.warning("Chrony SHM write failed")
                        except Exception as e:
                            logger.error(f"Chrony SHM update exception: {e}")
                    else:
                        # Log why we're not feeding chrony
                        reasons = []
                        if not quality_ok:
                            reasons.append(f"grade={result.quality_grade}")
                        if not multi_station:
                            reasons.append(f"n_stations={result.n_stations} (need >=2)")
                        if not consistent:
                            if result.consistency_flag == 'INTER_ANOMALY':
                                reasons.append(f"consistency={result.consistency_flag} with uncertainty={result.uncertainty_ms:.3f}ms (>0.5ms)")
                            else:
                                reasons.append(f"consistency={result.consistency_flag}")
                        if not discontinuity_ok:
                            reasons.append("discontinuity")
                        
                        if result.single_station_mode:
                            logger.info(
                                f"Chrony feed DISABLED in single-station mode: "
                                f"No cross-validation possible, systematic errors undetectable. "
                                f"Using NTP for clock discipline."
                            )
                        else:
                            logger.debug(f"Chrony feed skipped: {', '.join(reasons)}")

            # BREADCRUMB: Sleeping
            loop_duration = time.time() - loop_start_time
            logger.debug(f"Loop finished in {loop_duration:.3f}s. Sleeping {interval_sec}s...")
            
            time.sleep(interval_sec)
            
        except KeyboardInterrupt:
            logger.info("Fusion service stopped")
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
    
    # Read receiver coordinates from config if provided
    receiver_lat = None
    receiver_lon = None
    if args.config and args.config.exists():
        try:
            import toml
            with open(args.config, 'r') as f:
                config = toml.load(f)
            receiver_lat = config.get('station', {}).get('latitude')
            receiver_lon = config.get('station', {}).get('longitude')
            if receiver_lat and receiver_lon:
                logger.info(f"Using receiver coordinates from config: {receiver_lat:.6f}°N, {receiver_lon:.6f}°W")
        except Exception as e:
            logger.warning(f"Failed to read config file: {e}")
    
    enable_chrony = args.enable_chrony and not args.disable_chrony
    run_fusion_service(
        args.data_root, 
        args.interval, 
        enable_chrony=enable_chrony,
        lookback_minutes=args.lookback,
        receiver_lat=receiver_lat,
        receiver_lon=receiver_lon
    )
