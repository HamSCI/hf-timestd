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
THREE-LAYER METROLOGICAL ARCHITECTURE
================================================================================
Understanding the fundamental difference between Frequency Stability (Slope)
and Time Accuracy (Offset):

LAYER 1: Single Broadcast — "The Floating Ruler"
    - Measures stability of local clock's tick rate relative to transmitter
    - NOT anchored to UTC — signal always arrives late by propagation delay
    - Result: Perfect slope (frequency), but shifted by ~8ms average delay
    - You know HOW FAST time is passing, but not WHAT TIME IT IS

LAYER 2: Single Station, Multiple Frequencies — "The Dispersion Anchor"
    - Multiple frequencies (5, 10, 15 MHz) unlock dispersion calculation
    - Lower frequencies delayed MORE by ionosphere than higher ones
    - Calculate TEC → compute ionospheric delay → subtract it
    - Result: Characterizes PATH PHYSICS, moves floating ruler toward UTC

LAYER 3: Multiple Stations (17 Broadcasts) — "The Geometry Lock"
    - Geography (WWV vs WWVH vs CHU) sounds ionosphere from different angles
    - Cancels localized anomalies ("Weather") — solar flares affect paths differently
    - Result: "Triangulates" ionosphere globally, provides INTEGRITY (validation)

THE "STEEL RULER" SUMMARY:
    GPSDO                      → Slope (Rate)         → Ruler is straight and rigid
    Multi-Frequency Dispersion → Vertical Shift       → Calibrates zero-point per station
    Multi-Station Fusion       → Integrity            → Zero-point consistent across hemisphere

KEY INSIGHT: Combined regression of 17 broadcasts doesn't just average noise —
it SOLVES THE GEOMETRY of the ionosphere to find the true UTC origin point.

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

Output is written to: phase2/fusion/ (HDF5 L3 products)

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
import os
import time
import re
from contextlib import nullcontext
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

# Disable HDF5 file locking BEFORE importing h5py so the setting takes
# effect at library initialization time.  The writer uses an open-write-close
# pattern (no persistent handles), but HDF5 library-level locking can still
# block concurrent reads on some systems.  Setting the env var after import
# has no effect on HDF5 ≥ 1.14.
os.environ["HDF5_USE_FILE_LOCKING"] = "FALSE"

# HDF5 I/O for reading L1A and L2 data products
try:
    from hf_timestd.io import DataProductReader
    HDF5_AVAILABLE = True
except ImportError:
    HDF5_AVAILABLE = False
    logger.warning("h5py/xarray not available, HDF5 reads will fail")

if not HDF5_AVAILABLE:
    logger.warning("HDF5 storage DISABLED")

# Physics Propagation for GNSS Integration (migrated to HFPropagationModel)
try:
    from hf_timestd.core.propagation_model import HFPropagationModel as _HFPropModel
except ImportError:
    _HFPropModel = None

# Arrival Pattern Matrix for physics-based validation (optional — fusion runs without it)
try:
    from hf_timestd.core.arrival_pattern_matrix import ArrivalPatternMatrix as ArrivalPatternMatrix
    _ARRIVAL_MATRIX_AVAILABLE = True
except Exception as _apm_exc:
    ArrivalPatternMatrix = None  # type: ignore[assignment,misc]
    _ARRIVAL_MATRIX_AVAILABLE = False
    logger.warning(f"ArrivalPatternMatrix unavailable ({_apm_exc}); physics-based validation disabled")



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
    
    CALIBRATION vs PHYSICS SEPARATION (2026-02-06):
    -----------------------------------------------
    The total offset is decomposed into two components:
    
    1. hardware_offset_ms: CONSTANT systematic delays from the receiver chain.
       - Matched filter group delay (~0.4ms for 800ms template)
       - ADC/buffer alignment latency
       - Detection threshold bias
       These converge quickly and should NOT change once learned.
       Learning rate: Very slow after convergence (α_hw → 0.01).
    
    2. The RESIDUAL (D_clock - hardware_offset) is the SCIENCE PRODUCT:
       - Real ionospheric variations from climatology
       - Propagation model errors (which should be fixed in the model)
       - Measurement noise
       
    The old approach (offset_ms = -mean(D_clock)) absorbed EVERYTHING,
    hiding physics model errors. The new approach only calibrates hardware.
    
    Issue 3.2 Fix: Calibration is now per-broadcast (station+frequency) rather
    than per-station. This accounts for frequency-dependent ionospheric delays:
    - Different frequencies have different ionospheric delays (1/f²)
    - Same-frequency broadcasts share ionospheric conditions (correlated errors)
    
    BPM Note: BPM calibration must account for UT1/UTC alternation.
    UT1 minutes (25-29, 55-59) are excluded from calibration unless
    DUT1 correction is applied.
    """
    station: str              # WWV, WWVH, CHU, BPM
    frequency_mhz: float      # Broadcast frequency (key for correlation)
    offset_ms: float          # Total calibration offset (backward compat)
    uncertainty_ms: float     # Uncertainty in offset
    n_samples: int            # Number of samples used
    last_updated: float       # Unix time of last update
    reference_station: str    # Station used as reference (CHU)
    hardware_offset_ms: float = 0.0   # Hardware-only component (converges to constant)
    hardware_converged: bool = False   # True once hardware offset has stabilized
    
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
    
    # ========================================================================
    # METROLOGICAL TRACKING FIELDS (v6.2)
    # ========================================================================
    # TSL1 vs TSL2 comparison (for validation and propagation correction quality)
    d_clock_l1_ms: Optional[float] = None        # L1-only fusion (raw metrology)
    d_clock_l2_ms: Optional[float] = None        # L2 fusion (calibrated)
    l1_l2_difference_ms: Optional[float] = None  # L1 - L2 (propagation correction quality)
    
    # Calibration convergence tracking
    calibration_age_hours: Optional[float] = None      # Age of calibration data
    calibration_n_samples: Optional[int] = None        # Total samples in calibration
    calibration_converged: Optional[bool] = None       # True if converged
    
    # Multipath and Doppler aggregates from v6.2 tone detection
    multipath_detected_count: int = 0                  # Measurements with multipath
    multipath_mean_delay_spread_ms: Optional[float] = None  # Mean delay spread
    doppler_mean_hz: Optional[float] = None            # Mean Doppler shift
    doppler_correction_applied_ms: Optional[float] = None   # Total correction applied
    cramer_rao_mean_ms: Optional[float] = None         # Mean Cramér-Rao uncertainty
    
    # Propagation mode identification
    propagation_modes_used: Optional[str] = None       # Comma-separated modes
    dominant_propagation_mode: Optional[str] = None    # Most common mode
    
    # Kalman filter state (UI expects 'LOCKED', 'ACQUIRING', or 'REACQUIRING')
    kalman_state: Optional[str] = None               # Kalman convergence state
    
    # Allan deviation (stability metrics)
    adev_60s: Optional[float] = None                   # ADEV at tau=60s
    adev_1000s: Optional[float] = None                 # ADEV at tau=1000s


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
    
    # Level-aware grade thresholds (uncertainty_ms, inter_station_ms)
    # See METROLOGY.md "Timing Authority Levels" for derivation
    GRADE_THRESHOLDS = {
        'L6': {'A': (1.0, 2.0), 'B': (2.0, 4.0), 'C': (5.0, 10.0)},
        'L5': {'A': (1.0, 2.0), 'B': (2.0, 4.0), 'C': (5.0, 10.0)},
        'L4': {'A': (1.5, 3.0), 'B': (3.0, 6.0), 'C': (7.0, 15.0)},
        'L3': {'A': (2.0, 4.0), 'B': (4.0, 8.0), 'C': (8.0, 20.0)},
        'L2': {'A': (3.0, 5.0), 'B': (5.0, 10.0), 'C': (10.0, 25.0)},
        'L1': {'A': (3.0, 5.0), 'B': (5.0, 10.0), 'C': (10.0, 25.0)},
    }
    
    def __init__(
        self,
        data_root: Path,
        calibration_file: Optional[Path] = None,
        auto_calibrate: bool = True,
        reference_station: str = 'CHU',
        receiver_lat: Optional[float] = None,
        receiver_lon: Optional[float] = None,
        sample_rate: Optional[int] = None,
        timing_authority_level: str = 'L5',
        is_rtp_authority: bool = True
    ):
        """
        Initialize multi-broadcast fusion engine.
        
        Args:
            data_root: Root directory containing phase2/{CHANNEL}/ subdirs
            calibration_file: Optional file to persist calibration
            auto_calibrate: Whether to learn calibration from data
            reference_station: Station to use as timing reference
            timing_authority_level: Hardware timing level (L1-L6), affects grade thresholds
            is_rtp_authority: True when GPS+PPS is available (RTP mode). In RTP mode
                              TEC is a science observable, not a correction to apply.
                              In Fusion mode (False) TEC correction improves D_clock.
        """
        self.data_root = Path(data_root)
        # Optional per-cycle metrics hook installed by run_fusion_service
        # before the main loop starts. When set, fuse() wraps its three
        # named sub-phases (hdf5_read, kalman_apply, calibration_apply)
        # with self.loop_metrics.phase(). When None, _phase() returns a
        # no-op context manager so fuse() is unaffected outside a
        # service context (e.g., unit tests, ad-hoc REPL calls).
        self.loop_metrics = None
        self.timing_authority_level = timing_authority_level.upper()
        if self.timing_authority_level not in self.GRADE_THRESHOLDS:
            logger.warning(f"Unknown timing authority level '{timing_authority_level}', defaulting to L5")
            self.timing_authority_level = 'L5'
        self.is_rtp_authority = is_rtp_authority
        self.phase2_dir = self.data_root / 'phase2'
        self.calibration: Dict[str, BroadcastCalibration] = {}
        self.calibration_update_count = 0  # Track updates for auto-save
        self.calibration_trust_level = 1.0  # Trust level from loaded calibration (1.0 = full trust)
        self.calibration_age_hours = 0.0    # Age of loaded calibration in hours
        self.auto_calibrate = auto_calibrate
        self.reference_station = reference_station
        self.correction_alpha = 0.0  # Gradual ramp-up for Kalman correction (0→1)

        from .wwv_constants import SAMPLE_RATE_FULL
        self.sample_rate = int(sample_rate if sample_rate is not None else SAMPLE_RATE_FULL)

        self.receiver_lat = receiver_lat if receiver_lat is not None else 39.0
        self.receiver_lon = receiver_lon if receiver_lon is not None else -98.0

        # Bootstrap Validator (2026-01-24): Multi-station correlation for offset validation
        # This validates the RTP-to-UTC offset using cross-station agreement
        from .bootstrap_validator import BootstrapValidator
        self.bootstrap_validator = BootstrapValidator(
            receiver_lat=self.receiver_lat,
            receiver_lon=self.receiver_lon
        )
        self._bootstrap_offset_correction: Optional[float] = None

        # Arrival Pattern Matrix (2026-01-29): Physics-based validation
        # Validates L1/L2 measurements against expected arrivals from IRI-2020
        # Advisory only — fusion continues without it.
        self.arrival_matrix = None
        if _ARRIVAL_MATRIX_AVAILABLE:
            try:
                self.arrival_matrix = ArrivalPatternMatrix(
                    receiver_lat=self.receiver_lat,
                    receiver_lon=self.receiver_lon,
                    sample_rate=self.sample_rate,
                    enable_iri=True
                )
                logger.info("ArrivalPatternMatrix initialized for physics-based validation")
            except Exception as e:
                logger.warning(f"ArrivalPatternMatrix init failed ({e}); physics-based validation disabled")

        from .differential_time_solver import GlobalDifferentialSolver
        self.global_solver = GlobalDifferentialSolver(
            receiver_lat=self.receiver_lat,
            receiver_lon=self.receiver_lon
        )
        
        # Initialize TEC Estimator (Coherent Multi-Frequency Physics)
        from .tec_estimator import TECEstimator
        self.tec_estimator = TECEstimator()
        
        # ====================================================================
        # PER-BROADCAST KALMAN FILTERS (v6.0 Hierarchical Architecture)
        # ====================================================================
        # Each broadcast gets its own Kalman filter to track ionospheric path
        # dynamics. This is physically justified because the ionosphere has
        # temporal continuity - it cannot teleport.
        from .broadcast_kalman_filter import BroadcastKalmanFilter
        self.broadcast_kalmans: Dict[str, BroadcastKalmanFilter] = {}
        self.broadcast_kalman_state_dir = self.data_root / 'state' / 'broadcast_kalmans'
        self.broadcast_kalman_state_dir.mkdir(parents=True, exist_ok=True)
        self._load_broadcast_kalman_states()
        
        # Initialize HF Propagation Model (for GNSS VTEC integration + mode scoring)
        if _HFPropModel:
            try:
                self.physics_model = _HFPropModel(
                    receiver_lat=self.receiver_lat,
                    receiver_lon=self.receiver_lon,
                    enable_realtime=True
                )
            except Exception as e:
                self.physics_model = None
                logger.warning(f"HFPropagationModel init failed: {e} - GNSS VTEC integration disabled")
        else:
            self.physics_model = None
            logger.warning("HFPropagationModel not available - GNSS VTEC integration disabled")
        
        # Calibration state
        self.calibration_file = calibration_file or (
            self.data_root / 'state' / 'broadcast_calibration.json'
        )
        self.calibration: Dict[str, StationCalibration] = {}
        
        # CRITICAL FIX (2026-01-20): Initialize Kalman state BEFORE loading calibration
        # so that _load_calibration can restore the persisted Kalman state
        #
        # DUAL KALMAN ARCHITECTURE (2026-02-07):
        # L1 Kalman (kalman_state): Tracks raw L1 metrology D_clock
        # L2 Kalman (kalman_state_l2): Tracks physics-corrected L2 D_clock independently
        # This ensures TSL1 and TSL2 chrony feeds carry genuinely different estimates.
        self.kalman_state = np.array([0.0, 0.0])  # [offset_ms, drift_ms_per_min]
        self.kalman_P = np.array([[100.0, 0.0], [0.0, 1.0]])  # Initial uncertainty
        self.kalman_initialized = False
        self.kalman_n_updates = 0
        self.kalman_converged = False
        self.kalman_convergence_threshold = 50
        
        # Independent L2 Kalman state
        self.kalman_state_l2 = np.array([0.0, 0.0])
        self.kalman_P_l2 = np.array([[100.0, 0.0], [0.0, 1.0]])
        self.kalman_initialized_l2 = False
        self.kalman_n_updates_l2 = 0
        self.kalman_converged_l2 = False
        
        self._load_calibration()
        
        # CHU FSK auxiliary state (populated during FSK timing integration)
        # DUT1: UT1-UTC correction for propagation model solar zenith
        # TAI-UTC: leap second awareness for Kalman hold
        self._fsk_dut1: Optional[float] = None
        self._fsk_tai_utc: Optional[int] = None
        self._fsk_leap_second_hold: bool = False
        
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
            logger.warning("HDF5 fusion writes disabled")
            self.hdf5_fusion_writer = None
            self.enable_hdf5_fusion_writes = False

        
        # History for calibration learning
        self.measurement_history: Dict[str, List[BroadcastMeasurement]] = defaultdict(list)
        self.history_max_size = 100  # Keep last N measurements per station
        
        # Two-tier Kalman approach (2026-01-10)
        # Tier 1: Fast measurements (every 8s) - record variations, don't adjust baseline
        # Tier 2: Slow adjustments (detect persistent drift) - only adjust if GPSDO drifting
        # NOTE: kalman_state, kalman_P, kalman_initialized, kalman_n_updates, kalman_converged,
        # and kalman_convergence_threshold are initialized BEFORE _load_calibration() above
        self.measurement_window = []  # Recent measurements for drift detection
        self.measurement_window_size = 30  # 30 measurements = ~4 minutes
        self.last_baseline_adjustment = 0.0  # Timestamp of last adjustment
        self.baseline_adjustment_interval = 600.0  # Minimum 10 minutes between adjustments
        
        # ====================================================================
        # METROLOGICAL HOLDOVER MODEL (2026-01-16)
        # ====================================================================
        # The GPSDO is our "steel ruler" - it defines the time scale.
        # During signal dropout, the OFFSET remains valid (anchored to GPSDO),
        # but UNCERTAINTY grows at a calculable rate based on:
        # 1. GPSDO holdover drift spec (~1μs/hour when locked, ~1ms/hour unlocked)
        # 2. Time since last validated multi-station fusion
        # 3. Number of stations that contributed to last valid fusion
        #
        # This is the metrologically correct approach: we don't lose our
        # calibration during dropout, we just become less certain of it.
        self.last_valid_fusion_time = 0.0  # Unix timestamp of last multi-station fusion
        self.last_valid_fusion_uncertainty = 1.0  # Uncertainty at that time (ms)
        self.last_valid_n_stations = 0  # Number of stations contributing
        self.gpsdo_holdover_drift_rate = 0.001  # ms/minute (~1μs/min, conservative for locked GPSDO)
        self.holdover_mode = False  # True when in signal dropout
        
        # Station count scaling for systematic uncertainty
        # More stations = better cross-validation = lower systematic error
        # Based on metrological principle: independent measurements reduce systematic bias
        self.station_count_uncertainty_scale = {
            1: 2.0,   # Single station: no cross-validation, 2x systematic uncertainty
            2: 1.0,   # Two stations: basic cross-validation, baseline uncertainty
            3: 0.7,   # Three stations: good cross-validation
            4: 0.5,   # Four stations: excellent cross-validation
        }
        
        # Allan deviation tracker for real-time stability monitoring
        self.adev_tracker = AllanDeviationTracker(max_samples=86400)  # 24h history
        self.adev_tau_values = [10, 100, 1000, 10000]  # Standard tau values (seconds)
        
        # ====================================================================
        # LONG-TERM DRIFT ESTIMATOR (2026-01-16)
        # ====================================================================
        # Key metrological insight: WWV/CHU/BPM transmit EXACTLY on UTC.
        # Ionospheric propagation variations are ZERO-MEAN over long periods.
        # Therefore, the long-term average of ANY single broadcast converges
        # to the true GPSDO drift rate as N → ∞.
        #
        # For each broadcast, we maintain sufficient statistics for online
        # linear regression: D_clock(t) = slope × t + intercept + noise
        #
        # As measurements accumulate:
        # - slope → GPSDO drift rate (what we want to characterize)
        # - intercept → systematic offset (propagation model error)
        # - uncertainty → decreases as 1/√N
        #
        # This exploits the "long view" - every measurement contributes forever,
        # ionospheric noise averages to zero, and GPSDO drift becomes measurable.
        #
        # DISCONTINUITY HANDLING:
        # - Use fixed epoch (Unix epoch) for absolute time reference
        # - Persist sufficient statistics to survive service restarts
        # - Detect step discontinuities (GPSDO unlock, NTP step) and handle gracefully
        # - Segment-based approach: start new segment on discontinuity, merge when stable
        self.long_term_stats: Dict[str, Dict] = {}  # Per-broadcast sufficient statistics
        self.long_term_reference_time = 0.0  # Use Unix epoch (t=0) for absolute reference
        self.long_term_stats_file = self.data_root / 'state' / 'long_term_drift_stats.json'
        self.long_term_last_values: Dict[str, float] = {}  # For discontinuity detection
        self.long_term_discontinuity_threshold = 10.0  # ms - step change detection threshold
        self._load_long_term_stats()
        
        # Channels to aggregate
        self.channels = self._discover_channels()
        
        # Data freshness tracking for upstream starvation detection
        self.upstream_stale_warning_issued = False
        self.max_upstream_age_seconds = 300.0  # 5 minutes - warn if L1/L2 data older than this
        
        # DIAGNOSTIC: Track updates since this restart (separate from kalman_n_updates which may be restored)
        self._updates_since_restart = 0
        
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
    
    def _validate_calibration_data(self, data: dict) -> tuple:
        """
        Validate loaded calibration data and compute trust level based on age.
        
        Prevents loading corrupted or stale calibration files that could
        trap the system in an unrecoverable state.
        
        Validation criteria:
        - Offset magnitude must be < 150ms (realistic max ~120ms for BPM multi-hop)
        - Calibration age must be < 7 days (ionospheric conditions change)
        
        Trust decay model (based on ionospheric variability):
        - < 1 hour: Full trust (ionosphere stable on this timescale)
        - 1-6 hours: High trust (diurnal changes beginning)
        - 6-24 hours: Medium trust (significant diurnal variation)
        - 1-7 days: Low trust (major ionospheric changes possible)
        - > 7 days: Reject (too stale)
        
        Returns:
            (valid: bool, trust_level: float, max_age_hours: float)
            trust_level: 1.0 = full trust, 0.0 = no trust
        """
        MAX_OFFSET_MS = 150.0  # Maximum reasonable offset (BPM from China can be ~120ms)
        MAX_AGE_DAYS = 7       # Maximum calibration age
        
        current_time = time.time()
        max_age_seconds = MAX_AGE_DAYS * 86400
        
        max_age_hours = 0.0
        
        for broadcast_key, cal_data in data.items():
            # Skip metadata keys (Kalman state, etc.)
            if broadcast_key.startswith('_'):
                continue
                
            offset_ms = cal_data.get('offset_ms', 0.0)
            last_updated = cal_data.get('last_updated', 0)
            
            # Check offset magnitude
            if abs(offset_ms) > MAX_OFFSET_MS:
                logger.warning(
                    f"Calibration sanity check FAILED: {broadcast_key} has "
                    f"offset={offset_ms:+.1f}ms (exceeds ±{MAX_OFFSET_MS}ms limit)"
                )
                return (False, 0.0, 0.0)
            
            # Check age - handle both Unix timestamp (float) and ISO string formats
            if isinstance(last_updated, str):
                try:
                    from datetime import datetime, timezone
                    dt = datetime.fromisoformat(last_updated.replace('Z', '+00:00'))
                    last_updated_ts = dt.timestamp()
                except (ValueError, AttributeError):
                    last_updated_ts = 0
            else:
                last_updated_ts = last_updated
            
            age_seconds = current_time - last_updated_ts
            age_hours = age_seconds / 3600.0
            max_age_hours = max(max_age_hours, age_hours)
            
            if age_seconds > max_age_seconds:
                logger.warning(
                    f"Calibration sanity check FAILED: {broadcast_key} is "
                    f"{age_seconds/86400:.1f} days old (exceeds {MAX_AGE_DAYS} day limit)"
                )
                return (False, 0.0, max_age_hours)
        
        # Compute trust level based on maximum age
        # Trust decay: exponential with 12-hour half-life
        # This reflects ionospheric variability timescales
        TRUST_HALFLIFE_HOURS = 12.0
        trust_level = 0.5 ** (max_age_hours / TRUST_HALFLIFE_HOURS)
        trust_level = max(0.1, min(1.0, trust_level))  # Clamp to [0.1, 1.0]
        
        logger.info(
            f"Calibration sanity check PASSED for {len(data) - 1} broadcasts. "
            f"Age: {max_age_hours:.1f}h, trust: {trust_level:.2f}"
        )
        return (True, trust_level, max_age_hours)
    
    def _load_calibration(self):
        """
        Load per-broadcast calibration from file.
        
        STEEL RULER PHILOSOPHY (2026-01-18 - CORRECTED):
        The GPSDO provides a stable FREQUENCY reference (no drift), but broadcast
        calibration offsets represent REAL systematic delays that should persist:
        - Matched filter group delay (~1-2ms)
        - Propagation model errors (station-specific)
        - Tone rise time differences
        
        What SHOULD reset on restart:
        - Kalman filter state (drift rate) - GPSDO doesn't drift
        
        What SHOULD persist:
        - Broadcast calibration offsets - these are real systematic delays
        
        The previous "always bootstrap from zero" approach was incorrect because
        it discarded learned calibration offsets, causing D_clock to jump from
        ~1ms to ~0ms on every restart.
        """
        # Load calibration from file if it exists
        if self.calibration_file.exists():
            try:
                with open(self.calibration_file) as f:
                    data = json.load(f)
                
                # SANITY CHECK: Validate before loading and get trust level
                valid, trust_level, age_hours = self._validate_calibration_data(data)
                if not valid:
                    logger.warning(
                        f"Calibration file {self.calibration_file} failed sanity checks. "
                        "Discarding and starting fresh with bootstrap mode."
                    )
                    self._init_default_calibration()
                    return
                
                # Store trust level for use in calibration updates
                self.calibration_trust_level = trust_level
                self.calibration_age_hours = age_hours
                
                # STEEL RULER (2026-01-18 - REVISED): Restore Kalman OFFSET but not drift
                # The GPSDO doesn't drift, so drift_ms_per_min should stay at zero.
                # But the Kalman offset represents the current D_clock estimate, which is
                # a real physical quantity that should persist across restarts.
                # NOT restoring it causes visible discontinuities in the D_clock trace.
                if '_kalman_state' in data:
                    kalman_state = data['_kalman_state']
                    logger.info(f"Found _kalman_state: converged={kalman_state.get('converged')}, offset={kalman_state.get('offset_ms', 0):.3f}ms")
                    if kalman_state.get('converged', False):
                        # Restore the offset but keep drift at zero (Steel Ruler)
                        restored_offset = kalman_state.get('offset_ms', 0.0)
                        # CRITICAL: Set kalman_state array, not separate variables
                        self.kalman_state[0] = restored_offset  # offset_ms
                        self.kalman_state[1] = 0.0  # drift forced to zero (Steel Ruler)
                        self.kalman_n_updates = kalman_state.get('n_updates', 0)
                        self.kalman_initialized = True
                        self.kalman_converged = True
                        # Restore covariance for proper uncertainty propagation
                        if 'covariance' in kalman_state:
                            self.kalman_P = np.array(kalman_state['covariance'])
                        logger.info(
                            f"Steel Ruler mode: Restored Kalman state[0]={self.kalman_state[0]:.3f}ms "
                            f"(drift forced to 0, n_updates={self.kalman_n_updates})"
                        )
                    else:
                        logger.info(
                            "Steel Ruler mode: Kalman not converged, starting fresh. "
                            "Broadcast calibrations will be loaded."
                        )
                
                # DUAL KALMAN (2026-02-07): Restore independent L2 Kalman state
                if '_kalman_state_l2' in data:
                    ks_l2 = data['_kalman_state_l2']
                    if ks_l2.get('converged', False):
                        self.kalman_state_l2[0] = ks_l2.get('offset_ms', 0.0)
                        self.kalman_state_l2[1] = 0.0
                        self.kalman_n_updates_l2 = ks_l2.get('n_updates', 0)
                        self.kalman_initialized_l2 = True
                        self.kalman_converged_l2 = True
                        if 'covariance' in ks_l2:
                            self.kalman_P_l2 = np.array(ks_l2['covariance'])
                        logger.info(
                            f"Restored L2 Kalman state: offset={self.kalman_state_l2[0]:.3f}ms, "
                            f"n_updates={self.kalman_n_updates_l2}"
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
                        reference_station=cal_data.get('reference_station', 'CHU'),
                        hardware_offset_ms=cal_data.get('hardware_offset_ms', 0.0),
                        hardware_converged=cal_data.get('hardware_converged', False)
                    )
                logger.info(f"✅ Loaded {len(self.calibration)} broadcast calibrations from {self.calibration_file}")
                
                # CRITICAL: Skip warmup penalty AND bootstrap mode if we have valid calibration data
                # This prevents calibration from jumping around on restart
                # Trust level modulates how much we allow calibration to change
                if len(self.calibration) >= 2:
                    self.kalman_n_updates = 200
                    # Scale calibration_update_count by trust level
                    # High trust (1.0) -> 200 (full skip of bootstrap)
                    # Low trust (0.1) -> 20 (partial bootstrap, allows some adjustment)
                    self.calibration_update_count = int(200 * trust_level)
                    
                    # Also scale Kalman covariance by inverse trust
                    # Low trust -> higher uncertainty -> Kalman adapts faster
                    if trust_level < 0.9:
                        uncertainty_scale = 1.0 / trust_level
                        self.kalman_P[0, 0] *= uncertainty_scale
                        logger.info(
                            f"✅ Loaded calibration (age={age_hours:.1f}h, trust={trust_level:.2f}). "
                            f"Bootstrap count={self.calibration_update_count}, "
                            f"Kalman uncertainty scaled by {uncertainty_scale:.1f}x"
                        )
                    else:
                        logger.info(
                            f"✅ Loaded calibration (age={age_hours:.1f}h, trust={trust_level:.2f}). "
                            f"Full trust - skipping warmup and bootstrap"
                        )
                    
                    # DIAGNOSTIC: Log complete restart state for variance investigation
                    logger.info(
                        f"[RESTART_DIAG] Fusion restart state: "
                        f"kalman_state=[{self.kalman_state[0]:.4f}, {self.kalman_state[1]:.6f}], "
                        f"kalman_P_diag=[{self.kalman_P[0,0]:.4f}, {self.kalman_P[1,1]:.8f}], "
                        f"trust={trust_level:.3f}, age_h={age_hours:.2f}, "
                        f"cal_update_count={self.calibration_update_count}, "
                        f"n_broadcasts={len(self.calibration)}"
                    )

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
    
    # ========================================================================
    # PER-BROADCAST KALMAN FILTER METHODS (v6.0 Hierarchical Architecture)
    # ========================================================================
    
    def _load_broadcast_kalman_states(self):
        """
        Load persisted per-broadcast Kalman filter states.
        
        Each broadcast has its own Kalman filter tracking ionospheric path
        dynamics. States are persisted to survive service restarts.
        """
        from .broadcast_kalman_filter import BroadcastKalmanFilter
        
        # Scan for existing state files
        if self.broadcast_kalman_state_dir.exists():
            state_files = list(self.broadcast_kalman_state_dir.glob('*_kalman_state.json'))
            loaded_count = 0
            
            for state_file in state_files:
                try:
                    with open(state_file) as f:
                        state_data = json.load(f)
                    
                    broadcast_id = state_data.get('broadcast_id')
                    station = state_data.get('station')
                    frequency_mhz = state_data.get('frequency_mhz')
                    
                    if broadcast_id and station and frequency_mhz:
                        # Create filter and load state
                        kalman = BroadcastKalmanFilter(broadcast_id, station, frequency_mhz)
                        if kalman.load_state(self.broadcast_kalman_state_dir):
                            self.broadcast_kalmans[broadcast_id] = kalman
                            loaded_count += 1
                except Exception as e:
                    logger.warning(f"Failed to load Kalman state from {state_file}: {e}")
            
            if loaded_count > 0:
                logger.info(f"Loaded {loaded_count} per-broadcast Kalman filter states")
        else:
            logger.info("No per-broadcast Kalman states found - will initialize on first measurements")
    
    def _get_or_create_broadcast_kalman(self, broadcast_id: str, station: str, frequency_mhz: float):
        """
        Get existing Kalman filter for broadcast or create new one.
        
        Args:
            broadcast_id: Unique broadcast identifier (e.g., "WWV_10000")
            station: Station name (WWV, WWVH, CHU, BPM)
            frequency_mhz: Frequency in MHz
            
        Returns:
            BroadcastKalmanFilter instance
        """
        if broadcast_id not in self.broadcast_kalmans:
            from .broadcast_kalman_filter import BroadcastKalmanFilter
            self.broadcast_kalmans[broadcast_id] = BroadcastKalmanFilter(
                broadcast_id, station, frequency_mhz
            )
            logger.info(f"Created new Kalman filter for {broadcast_id}")
        
        return self.broadcast_kalmans[broadcast_id]
    
    def _save_broadcast_kalman_states(self):
        """
        Save all per-broadcast Kalman filter states to disk.
        
        Called periodically and on shutdown to persist state.
        """
        saved_count = 0
        for broadcast_id, kalman in self.broadcast_kalmans.items():
            try:
                kalman.save_state(self.broadcast_kalman_state_dir)
                saved_count += 1
            except Exception as e:
                logger.warning(f"Failed to save Kalman state for {broadcast_id}: {e}")
        
        if saved_count > 0:
            logger.debug(f"Saved {saved_count} per-broadcast Kalman filter states")
    
    def _apply_broadcast_kalmans(self, measurements: List['BroadcastMeasurement']) -> List['BroadcastMeasurement']:
        """
        Apply per-broadcast Kalman filtering to measurements.
        
        This is the core of the v6.0 hierarchical architecture:
        - Each broadcast's d_clock is filtered by its own Kalman
        - The Kalman tracks ionospheric path dynamics (ToF)
        - Glitches are rejected, real dynamics are preserved
        
        Args:
            measurements: Raw measurements from L1/L2
            
        Returns:
            Measurements with Kalman-filtered d_clock values and uncertainties
        """
        filtered_measurements = []
        
        for m in measurements:
            # Construct broadcast ID
            broadcast_id = f"{m.station}_{int(m.frequency_mhz * 1000)}"
            
            # Get or create Kalman filter for this broadcast
            kalman = self._get_or_create_broadcast_kalman(
                broadcast_id, m.station, m.frequency_mhz
            )
            
            # Update Kalman with measurement
            # Note: We're filtering d_clock directly, which includes both
            # ionospheric path and clock offset. The TEC estimator will
            # later separate these.
            snr_db = getattr(m, 'snr_db', 10.0)  # Default SNR if not available
            
            filtered_d_clock, kalman_uncertainty = kalman.update(
                m.d_clock_ms, snr_db
            )
            
            # Create new measurement with filtered values
            # We preserve the original measurement but update d_clock and uncertainty
            filtered_m = BroadcastMeasurement(
                timestamp=m.timestamp,
                station=m.station,
                frequency_mhz=m.frequency_mhz,
                d_clock_ms=filtered_d_clock,
                propagation_delay_ms=m.propagation_delay_ms,
                propagation_mode=m.propagation_mode,
                confidence=m.confidence,
                snr_db=m.snr_db,
                quality_grade=m.quality_grade,
                channel_name=m.channel_name
            )
            
            # Store Kalman uncertainty for weighting
            filtered_m.kalman_uncertainty_ms = kalman_uncertainty
            
            filtered_measurements.append(filtered_m)
        
        return filtered_measurements
    
    def _load_long_term_stats(self):
        """
        Load persisted long-term drift statistics.
        
        This allows the long-term drift estimator to survive service restarts
        without losing accumulated measurement history. The sufficient statistics
        (Σt, Σy, Σt², Σty, Σy²) are additive, so we can seamlessly continue
        accumulating after a restart.
        """
        if not self.long_term_stats_file.exists():
            logger.info("No persisted long-term drift stats found - starting fresh")
            return
        
        try:
            with open(self.long_term_stats_file) as f:
                data = json.load(f)
            
            # Validate and load
            if '_metadata' in data:
                metadata = data.pop('_metadata')
                saved_at = metadata.get('saved_at', 0)
                age_hours = (time.time() - saved_at) / 3600.0
                
                # Only load if reasonably recent (< 7 days old)
                if age_hours > 168:
                    logger.warning(
                        f"Long-term stats are {age_hours:.1f} hours old (> 7 days) - starting fresh"
                    )
                    return
            
            # Load per-broadcast statistics
            for broadcast_key, stats in data.items():
                if broadcast_key.startswith('_'):
                    continue
                
                # Validate required fields
                required = ['n', 'sum_t', 'sum_y', 'sum_tt', 'sum_ty', 'sum_yy']
                if all(k in stats for k in required):
                    self.long_term_stats[broadcast_key] = stats
                    # Initialize last value for discontinuity detection
                    if stats['n'] > 0:
                        # Estimate last value from mean
                        self.long_term_last_values[broadcast_key] = stats['sum_y'] / stats['n']
            
            total_samples = sum(s['n'] for s in self.long_term_stats.values())
            logger.info(
                f"Loaded long-term drift stats: {len(self.long_term_stats)} broadcasts, "
                f"{total_samples} total samples"
            )
            
        except Exception as e:
            logger.warning(f"Failed to load long-term drift stats: {e} - starting fresh")
            self.long_term_stats = {}
    
    def _save_long_term_stats(self):
        """
        Persist long-term drift statistics to file.
        
        Called periodically to ensure we don't lose accumulated history
        on service restart.
        """
        self.long_term_stats_file.parent.mkdir(parents=True, exist_ok=True)
        
        data = dict(self.long_term_stats)
        data['_metadata'] = {
            'saved_at': time.time(),
            'n_broadcasts': len(self.long_term_stats),
            'total_samples': sum(s['n'] for s in self.long_term_stats.values()),
        }
        
        # Atomic write
        temp_file = self.long_term_stats_file.with_suffix('.tmp')
        try:
            with open(temp_file, 'w') as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            temp_file.replace(self.long_term_stats_file)
        except Exception as e:
            logger.error(f"Failed to save long-term drift stats: {e}")
    
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
                'reference_station': cal.reference_station,
                'hardware_offset_ms': cal.hardware_offset_ms,
                'hardware_converged': cal.hardware_converged
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
        
        # DUAL KALMAN (2026-02-07): Persist independent L2 Kalman state
        data['_kalman_state_l2'] = {
            'offset_ms': float(self.kalman_state_l2[0]),
            'drift_ms_per_min': float(self.kalman_state_l2[1]),
            'covariance': self.kalman_P_l2.tolist(),
            'converged': self.kalman_converged_l2,
            'n_updates': self.kalman_n_updates_l2,
            'initialized': self.kalman_initialized_l2,
            'saved_at': time.time()
        }
        
        # v6.0 ARCHITECTURE: Save per-broadcast Kalman states
        # Each broadcast has its own Kalman filter tracking ionospheric path dynamics
        self._save_broadcast_kalman_states()
        
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

    def _read_latest_tone_observations_by_channel(
        self,
        lookback_minutes: int = 10
    ) -> Dict[str, Dict[int, List[Dict]]]:
        """
        Read latest tone observations from all channels via HDF5.
        
        Returns observations from the last N minutes, grouped by channel and minute.
        """
        if not HDF5_AVAILABLE:
            logger.warning("HDF5 not available for tone observations")
            return {}
        
        try:
            return self._read_latest_tone_observations_by_channel_hdf5(lookback_minutes)
        except Exception as e:
            logger.error(f"HDF5 tone detections read failed: {e}")
            return {}
    
    def _read_latest_tone_observations_by_channel_hdf5(
        self,
        lookback_minutes: int = 10
    ) -> Dict[str, Dict[int, List[Dict]]]:
        """
        Read latest tone observations from L2 timing_measurements HDF5 files.
        
        Reads from clock_offset/ subdirectory via DataProductRegistry, providing:
        - Quality filtering from HDF5 metadata
        - Complete metrological provenance chain
        
        Returns observations from the last N minutes, grouped by channel and minute.
        """
        from datetime import datetime, timezone, timedelta
        from .wwv_constants import BPM_UT1_MINUTES
        
        # If HDF5 not available, return empty dict
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
            channel_dir = self.phase2_dir / channel
            if not channel_dir.exists():
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
                logger.debug(f"No HDF5 timing measurements found for {channel}")
            
            except Exception as e:
                logger.warning(f"Error reading HDF5 timing measurements for {channel}: {e}")
        
        if by_channel:
            total_obs = sum(len(per_min) for per_min in by_channel.values() for per_min in per_min.values())
            logger.info(
                f"Read {total_obs} tone observations from HDF5 across {len(by_channel)} channels "
                f"(lookback={lookback_minutes}m)"
            )
        
        return by_channel

    def _read_tick_timing_observations(
        self,
        lookback_minutes: int = 10
    ) -> Dict[str, Dict[int, List[Dict]]]:
        """
        Read per-second tick timing observations from L2 tick_timing HDF5 files.
        
        Tick timing provides 55+ timing estimates per minute, enabling improved
        precision through averaging and drift detection.
        
        Returns observations grouped by channel and minute.
        """
        from datetime import datetime, timezone, timedelta
        
        if not HDF5_AVAILABLE:
            return {}
        
        now = time.time()
        cutoff = now - (lookback_minutes * 60)
        
        start_dt = datetime.fromtimestamp(cutoff, timezone.utc)
        end_dt = datetime.fromtimestamp(now, timezone.utc)
        start_iso = start_dt.isoformat().replace('+00:00', 'Z')
        end_iso = end_dt.isoformat().replace('+00:00', 'Z')
        
        by_channel: Dict[str, Dict[int, List[Dict]]] = {}
        
        for channel in self.channels:
            channel_dir = self.phase2_dir / channel
            if not channel_dir.exists():
                continue
            
            try:
                reader = DataProductReader(
                    data_dir=channel_dir,
                    product_level='L2',
                    product_name='tick_timing',
                    channel=channel
                )
                
                tick_measurements = reader.read_time_range(start=start_iso, end=end_iso)
                
                per_minute: Dict[int, List[Dict]] = defaultdict(list)
                
                for tick_meas in tick_measurements:
                    try:
                        minute_boundary = tick_meas.get('minute_boundary_utc', 0)
                        if minute_boundary < cutoff:
                            continue
                        
                        # Only include high-confidence tick measurements with valid D_clock
                        d_clock = tick_meas.get('d_clock_ms')
                        if tick_meas.get('valid_windows', 0) >= 10 and d_clock is not None:
                            per_minute[minute_boundary].append({
                                'station': tick_meas.get('station'),
                                'frequency_mhz': tick_meas.get('frequency_mhz'),
                                'timing_ms': d_clock,
                                'std_ms': tick_meas.get('std_timing_offset_ms'),
                                'n_windows': tick_meas.get('valid_windows'),
                                'source': 'tick'
                            })
                    except (ValueError, KeyError):
                        continue
                
                if per_minute:
                    by_channel[channel] = per_minute
                    
            except FileNotFoundError:
                pass
            except Exception as e:
                logger.debug(f"Error reading tick timing for {channel}: {e}")
        
        if by_channel:
            total = sum(len(m) for pm in by_channel.values() for m in pm.values())
            logger.info(f"Read {total} tick timing observations from {len(by_channel)} channels")
        
        return by_channel

    def _read_chu_fsk_timing(
        self,
        lookback_minutes: int = 10
    ) -> Dict[int, Dict]:
        """
        Read CHU FSK timing observations from L2 chu_fsk HDF5 files.
        
        CHU FSK provides precise 500ms boundary alignment - the most accurate
        timing reference available from HF broadcasts.
        
        Returns FSK timing keyed by minute boundary.
        """
        from datetime import datetime, timezone
        
        if not HDF5_AVAILABLE:
            return {}
        
        now = time.time()
        cutoff = now - (lookback_minutes * 60)
        
        start_dt = datetime.fromtimestamp(cutoff, timezone.utc)
        end_dt = datetime.fromtimestamp(now, timezone.utc)
        start_iso = start_dt.isoformat().replace('+00:00', 'Z')
        end_iso = end_dt.isoformat().replace('+00:00', 'Z')
        
        fsk_by_minute: Dict[int, Dict] = {}
        
        # Find CHU channels
        chu_channels = [ch for ch in self.channels if 'CHU' in ch.upper()]
        
        for channel in chu_channels:
            channel_dir = self.phase2_dir / channel
            if not channel_dir.exists():
                continue
            
            try:
                reader = DataProductReader(
                    data_dir=channel_dir,
                    product_level='L2',
                    product_name='chu_fsk',
                    channel=channel
                )
                
                fsk_measurements = reader.read_time_range(start=start_iso, end=end_iso)
                
                for fsk_meas in fsk_measurements:
                    try:
                        minute_boundary = fsk_meas.get('minute_boundary_utc', 0)
                        if minute_boundary < cutoff:
                            continue
                        
                        # Only use valid FSK decodes with timing
                        if fsk_meas.get('fsk_valid') and fsk_meas.get('timing_offset_ms') is not None:
                            fsk_by_minute[minute_boundary] = {
                                'channel': channel,
                                'timing_offset_ms': fsk_meas.get('timing_offset_ms'),
                                'decode_confidence': fsk_meas.get('decode_confidence', 0),
                                'frames_decoded': fsk_meas.get('frames_decoded', 0),
                                'dut1_seconds': fsk_meas.get('dut1_seconds'),
                                'tai_utc': fsk_meas.get('tai_utc'),
                                'source': 'chu_fsk'
                            }
                    except (ValueError, KeyError):
                        continue
                        
            except FileNotFoundError:
                pass
            except Exception as e:
                logger.debug(f"Error reading CHU FSK for {channel}: {e}")
        
        if fsk_by_minute:
            logger.info(f"Read {len(fsk_by_minute)} CHU FSK timing observations")
        
        return fsk_by_minute

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
        except (AttributeError, TypeError) as e:
            logger.debug(f"Error logging global solve result: {e}")
        return result, len(observations)
    
    def _check_upstream_freshness(self) -> Tuple[bool, float]:
        """
        Check if upstream L1/L2 data is fresh enough.
        
        Returns:
            Tuple of (is_fresh, newest_age_seconds)
        """
        newest_mtime = 0.0
        
        for channel in self.channels:
            # Check L1 metrology directory
            l1_dir = self.phase2_dir / channel / "metrology"
            if l1_dir.exists():
                h5_files = list(l1_dir.glob("*.h5"))
                if h5_files:
                    channel_mtime = max(f.stat().st_mtime for f in h5_files)
                    newest_mtime = max(newest_mtime, channel_mtime)
            
            # Check L2 clock_offset directory
            l2_dir = self.phase2_dir / channel / "clock_offset"
            if l2_dir.exists():
                h5_files = list(l2_dir.glob("*.h5"))
                if h5_files:
                    channel_mtime = max(f.stat().st_mtime for f in h5_files)
                    newest_mtime = max(newest_mtime, channel_mtime)
        
        if newest_mtime == 0.0:
            return False, float('inf')
        
        age_seconds = time.time() - newest_mtime
        return age_seconds < self.max_upstream_age_seconds, age_seconds
    
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
                logger.warning(f"Error reading L1 for {channel}: {e}")
        
        logger.info(f"L1 metrology read: {len(l1_data)} entries from {len(self.channels)} channels")
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

        # L2 calibrated timing outputs are in clock_offset directory
        for channel in self.channels:
            channel_dir = self.phase2_dir / channel / "clock_offset"
            if not channel_dir.exists():
                continue

            try:
                reader = DataProductReader(
                    data_dir=channel_dir,
                    product_level='L2',
                    product_name='timing_measurements',
                    channel=channel
                )
                
                measurements = reader.read_time_range(
                    start=start_iso, 
                    end=end_iso,
                    min_confidence=0.0
                )

                for m in measurements:
                    ts_str = m.get('timestamp_utc')
                    # L2 timing_measurements uses 'station' field, not 'station_id'
                    station = m.get('station') or m.get('station_id')
                    if not ts_str or not station:
                        continue
                    
                    key = f"{ts_str}|{station}"
                    l2_data[key] = m

            except Exception as e:
                logger.warning(f"Error reading L2 for {channel}: {e}")

        logger.info(f"L2 physics read: {len(l2_data)} entries from {len(self.channels)} channels")
        return l2_data

    def _read_latest_measurements(
        self, 
        lookback_minutes: int = 5,
        force_l1_only: bool = False
    ) -> List[BroadcastMeasurement]:
        """
        Read and join L1 (Metrology) and L2 (Physics) data to form Fusion inputs.
        
        Logic:
           1. Check upstream data freshness (warn if stale)
           2. Read L1 (Raw TOA).
           3. Read L2 (Propagation Delay).
           4. Join on Timestamp + Station.
           5. Calculate D_clock = Raw_TOA - Propagation_Delay.
           6. Fallback: If L2 missing, D_clock = Raw_TOA - (LightTime + 1.5ms).
        """
        from datetime import datetime, timezone
        from .wwv_constants import BPM_UT1_MINUTES
        
        # 0. Check upstream data freshness
        is_fresh, age_seconds = self._check_upstream_freshness()
        if not is_fresh:
            if not self.upstream_stale_warning_issued:
                logger.warning(
                    f"Upstream L1/L2 data is stale ({age_seconds:.0f}s old, "
                    f"threshold={self.max_upstream_age_seconds:.0f}s). "
                    "Metrology or L2 calibration service may have stopped."
                )
                self.upstream_stale_warning_issued = True
            # Continue processing - use whatever data is available
        else:
            if self.upstream_stale_warning_issued:
                logger.info(f"Upstream data is fresh again ({age_seconds:.0f}s old)")
                self.upstream_stale_warning_issued = False
        
        # 1. Read L1 and L2 (skip L2 in L1-only mode)
        l1_map = self._read_l1_metrology(lookback_minutes)
        
        if force_l1_only:
            l2_map = {}  # Skip L2 data in L1-only mode
            logger.debug(f"Fusion Reader (L1-only mode): L1_count={len(l1_map)}")
        else:
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
                
                # CRITICAL FIX (2026-02-07): raw_toa_ms is MISLABELED in L1.
                # It actually stores timing_error_ms (= arrival - expected_delay),
                # computed in metrology_engine.py line 567. This IS the D_clock.
                # Subtracting propagation delay again was a double-subtraction bug
                # that introduced station/frequency-dependent errors of 10-80ms.
                
                if l2_item:
                    # Physics Available — use L2 propagation mode info
                    prop_delay = float(l2_item.get('propagation_delay_ms', 0))
                    mode = l2_item.get('propagation_mode', 'Unknown')
                    model_conf = float(l2_item.get('model_confidence', 0))
                    
                    d_clock = raw_toa  # Already timing_error (arrival - prop_delay)
                    confidence = model_conf
                else:
                    # Fallback (Physics Missing/Failed)
                    prop_delay = light_time + 1.5
                    mode = 'FALLBACK'
                    d_clock = raw_toa  # Already timing_error
                    confidence = 0.5
                    model_conf = 0.0

                # BPM Filtering
                if station == 'BPM':
                    if dt.minute in BPM_UT1_MINUTES:
                        continue

                # Physics-based validation using ArrivalPatternMatrix (2026-01-29)
                # Validate that raw_toa falls within expected bounds from IRI-2020
                # NOTE: We flag but don't reject - the matrix may need calibration
                physics_valid = True
                physics_reason = "no_validation"
                
                if hasattr(self, 'arrival_matrix') and self.arrival_matrix is not None:
                    # Convert raw_toa_ms to sample offset for validation
                    detected_sample = int(raw_toa * self.sample_rate / 1000)
                    snr_db_val = float(l1_item.get('snr_db', 0))
                    
                    is_valid, phys_conf, reason = self.arrival_matrix.validate_detection(
                        station=station,
                        frequency_mhz=freq_mhz,
                        detected_sample=detected_sample,
                        snr_db=snr_db_val,
                        utc_time=dt
                    )
                    
                    physics_valid = is_valid
                    physics_reason = reason
                    
                    if not is_valid:
                        logger.debug(f"Physics validation WARNING: {station}@{freq_mhz}MHz "
                                    f"raw_toa={raw_toa:.1f}ms - {reason}")
                        # Reduce confidence but don't reject
                        confidence = confidence * 0.5

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
        
        # STATION PRIORITY (2026-02-07): Primary timing anchors only
        # CHU, WWV, WWVH are the primary timing anchors - shorter paths, better characterized
        # BPM is EXCLUDED from fusion (weight=0) due to:
        #   - Very long path (~11,000 km) with 18-36 ms cross-station disagreement
        #   - Multi-hop propagation introduces unmodeled uncertainty
        #   - UT1/UTC alternation requires careful handling
        #   - Dominates inter-station spread, preventing grade improvement
        # BPM data is still collected and logged for ionospheric science.
        station_priority = {
            'CHU': 1.0,    # Primary anchor - unique frequencies, FSK verification
            'WWV': 1.0,    # Primary anchor - closest station, well-characterized
            'WWVH': 0.9,   # Primary anchor - longer path but reliable
            'BPM': 0.0,    # EXCLUDED from fusion - kept for ionospheric science only
        }
        
        # BOOTSTRAP PREFERENCE (2026-01-24): Prefer unambiguous channels during bootstrap
        # These channels have only ONE station transmitting, so there's no ambiguity
        # about which station is being detected. This prevents locking onto wrong timing.
        from .wwv_constants import UNAMBIGUOUS_BOOTSTRAP_CHANNELS
        is_bootstrap = self.calibration_update_count < 100
        
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
            # MODE AMBIGUITY PENALTY (2026-02-06): If the physics model reports
            # multiple viable modes, the measurement is less reliable because we
            # don't know which mode the signal actually took. The delay difference
            # between modes (e.g., 1F vs 2E) can be 5-20ms.
            base_mode = m.propagation_mode.split('+')[0] if '+' in m.propagation_mode else m.propagation_mode
            mode_scale_factor = mode_scale.get(base_mode, 0.7)
            
            if hasattr(self, 'physics_model') and self.physics_model is not None and \
               m.station not in ('GLOBAL_DIFF', 'UNKNOWN', 'TICK', 'CHU_FSK'):
                try:
                    from datetime import datetime, timezone as tz
                    prediction = self.physics_model.predict(
                        station=m.station,
                        frequency_mhz=m.frequency_mhz,
                        utc_time=datetime.fromtimestamp(m.timestamp, tz=tz.utc)
                    )
                    feasible = prediction.get_feasible_arrivals()
                    if len(feasible) > 1:
                        # Multiple modes viable - compute ambiguity penalty
                        # Use delay spread between top two feasible modes
                        delay_spread = abs(feasible[0].delay_ms - feasible[1].delay_ms)
                        
                        # Heuristic: if delay spread is small (<2ms), modes are
                        # nearly degenerate and ambiguity is less harmful
                        if delay_spread < 2.0:
                            ambiguity_penalty = 0.9
                        elif delay_spread < 5.0:
                            ambiguity_penalty = 0.7
                        else:
                            ambiguity_penalty = 0.4  # Severe ambiguity
                        
                        mode_scale_factor *= ambiguity_penalty
                except Exception as e:
                    logger.debug(f"Ignored exception: {e}")
                    pass  # Fail silently - mode scoring is an enhancement, not critical
            
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
            
            # Apply station priority (primary anchors vs secondary sources)
            station_priority_factor = station_priority.get(m.station, 0.5)
            
            # Combine: base precision × quality factors × station priority
            w = base_weight * confidence_scale * grade_scale_factor * mode_scale_factor * snr_scale * station_priority_factor
            
            # BOOTSTRAP PREFERENCE (2026-01-24): Boost unambiguous channels during bootstrap
            # WWV 20/25 MHz and all CHU frequencies have no station ambiguity
            # BPM is excluded from bootstrap entirely - too distant for reliable calibration
            if is_bootstrap:
                if m.station == 'BPM':
                    # BPM excluded from bootstrap - use minimal weight
                    w *= 0.1
                    logger.debug(f"Bootstrap: Suppressing BPM weight (too distant for calibration)")
                else:
                    broadcast_key = f"{m.station}_{m.frequency_mhz:.2f}"
                    # Check if this is an unambiguous channel (exact key match)
                    is_unambiguous = any(
                        broadcast_key.startswith(prefix.replace('.', ''))  
                        for prefix in UNAMBIGUOUS_BOOTSTRAP_CHANNELS.keys()
                    ) or m.station == 'CHU' or m.frequency_mhz in (20.0, 25.0)
                    
                    if is_unambiguous:
                        # 3x weight boost for unambiguous channels during bootstrap
                        w *= 3.0
                        logger.debug(f"Bootstrap: Boosting unambiguous channel {broadcast_key} weight 3x")
            
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
        """
        Apply per-broadcast HARDWARE calibration to measurements.
        
        CRITICAL FIX (2026-02-06): Only apply hardware_offset_ms, NOT offset_ms.
        
        The old approach applied offset_ms = -mean(D_clock), which zeroed out
        the entire signal — making it impossible to measure absolute clock offset.
        
        The new approach applies only the hardware constant (matched filter group
        delay, ADC latency, detection bias). The remaining D_clock after hardware
        correction IS the science product: real clock offset + ionospheric residual.
        """
        calibrated = []
        for m in measurements:
            if m.station == 'GLOBAL_DIFF':
                calibrated.append(m.d_clock_ms)
                continue
            # Use per-broadcast calibration (station + frequency)
            broadcast_key = self._get_broadcast_key(m.station, m.frequency_mhz)
            broadcast_cal = self.calibration.get(broadcast_key)
            if broadcast_cal and broadcast_cal.hardware_offset_ms != 0.0:
                # Apply hardware offset (absorbs constant hardware delay + average iono path)
                calibrated.append(m.d_clock_ms + broadcast_cal.hardware_offset_ms)
            else:
                # No calibration yet for this broadcast - use raw value
                calibrated.append(m.d_clock_ms)
        return calibrated
    
    def _validate_bootstrap_with_measurements(
        self,
        measurements: List[BroadcastMeasurement]
    ):
        """
        Feed measurements to bootstrap validator for multi-station correlation.
        
        The bootstrap validator checks:
        1. WWVH > WWV delay ordering (on shared frequencies)
        2. CHU timing consistency with geographic prediction
        3. All delays within geographic bounds
        
        Once validated, logs the offset correction (actual application happens
        at the metrology service level where RTP-to-UTC offset is managed).
        """
        from .bootstrap_validator import BootstrapPhase
        
        if self.bootstrap_validator.phase == BootstrapPhase.LOCKED:
            return  # Already validated
        
        logger.info(f"[BOOTSTRAP] Processing {len(measurements)} measurements, phase={self.bootstrap_validator.phase.name}")
        
        for m in measurements:
            if m.station in ('GLOBAL_DIFF', 'UNKNOWN'):
                continue
            
            # Use d_clock_ms as timing error (it's the offset from expected)
            if m.d_clock_ms is None or np.isnan(m.d_clock_ms):
                continue
            
            # Feed to validator
            offset_correction = self.bootstrap_validator.add_detection(
                channel=m.channel_name,
                station=m.station,
                frequency_mhz=m.frequency_mhz,
                timing_error_ms=m.d_clock_ms,
                confidence=m.confidence,
                snr_db=m.snr_db if m.snr_db is not None else 0.0,
                rtp_timestamp=0,  # Not available at fusion layer
                sample_rate=self.sample_rate,
                minute_boundary=int(m.timestamp),
                tone_frequency_hz=None,
                propagation_delay_ms=m.propagation_delay_ms
            )
            
            if offset_correction is not None:
                self._bootstrap_offset_correction = offset_correction
                logger.info(
                    f"[BOOTSTRAP] Fusion layer detected offset correction: "
                    f"{offset_correction*1000:+.1f}ms"
                )
        
        # Log status periodically
        if hasattr(self, '_bootstrap_log_counter'):
            self._bootstrap_log_counter += 1
        else:
            self._bootstrap_log_counter = 0
        
        if self._bootstrap_log_counter % 10 == 0:
            status = self.bootstrap_validator.get_status()
            logger.info(
                f"[BOOTSTRAP] Fusion status: phase={status['phase']}, "
                f"candidates={status['n_candidates']}, "
                f"stations={status.get('confirming_stations', [])}"
            )
    
    def _update_calibration(
        self,
        measurements: List[BroadcastMeasurement],
        validated: bool = True,
        reference_d_clock: float = 0.0
    ):
        """
        Update HARDWARE calibration offsets per-BROADCAST (station + frequency).
        
        CRITICAL FIX (2026-02-06): HARDWARE-ONLY CALIBRATION
        =====================================================
        The calibration now ONLY learns constant hardware delays:
        - Matched filter group delay (~0.4ms for 800ms template)
        - ADC/buffer alignment latency
        - Detection threshold bias
        
        It does NOT zero out the mean D_clock. The old approach
        (offset_ms = -mean(D_clock)) was circular: it defined "correct"
        as "the mean of what I measured", making it impossible to detect
        real clock offsets.
        
        The hardware offset converges during bootstrap (first ~100 updates)
        then freezes. After convergence, only tiny adjustments are allowed
        (±0.01ms/update) to track thermal drift in the receiver chain.
        
        The RESIDUAL after hardware correction is the science product:
        real clock offset + ionospheric propagation residual.
        
        Args:
            measurements: List of broadcast measurements
            validated: Whether cross-station validation passed (affects update rate)
            reference_d_clock: Unused (kept for API compatibility)
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
        # NOTE (2026-02-07): BPM now included in calibration. The hardware calibration
        # learns a constant offset per broadcast, which works for any distance.
        # Excluding BPM caused uncalibrated D_clock to inflate inter-station spread.
        for m in measurements:
            if m.station == 'GLOBAL_DIFF':
                continue
            broadcast_key = self._get_broadcast_key(m.station, m.frequency_mhz)
            history = self.measurement_history[broadcast_key]
            history.append(m)
            if len(history) > self.history_max_size:
                self.measurement_history[broadcast_key] = history[-self.history_max_size:]
        
        # Update HARDWARE calibration per-BROADCAST
        # HARDWARE-ONLY CALIBRATION (2026-02-06):
        # Learn only the constant hardware delay for each broadcast.
        # The hardware offset is the MEDIAN of recent D_clock values during
        # bootstrap (when the system clock is known-good via GPSDO+chrony).
        # After convergence, it freezes with only tiny thermal drift tracking.
        for broadcast_key, history in self.measurement_history.items():
            if len(history) < 5:
                continue
            
            recent = history[-30:]
            d_clocks = [m.d_clock_ms for m in recent]
            
            broadcast_mean = np.mean(d_clocks)
            broadcast_std = np.std(d_clocks)
            
            # Extract station and frequency from key for logging
            station = recent[0].station
            freq = recent[0].frequency_mhz
            
            # Exponential moving average for smooth updates
            old_cal = self.calibration.get(broadcast_key)
            old_hw_offset = old_cal.hardware_offset_ms if old_cal else 0.0
            hw_converged = old_cal.hardware_converged if old_cal else False
            
            if old_cal and old_cal.n_samples > 0:
                is_bootstrap = self.calibration_update_count < 100
                
                # Hardware offset target: the negative of the mean D_clock
                # This represents the constant hardware delay that should be
                # subtracted from all measurements for this broadcast.
                hw_target = 0.0 - broadcast_mean
                
                if is_bootstrap:
                    # Bootstrap: fast convergence for hardware offset discovery
                    hw_alpha = 0.2
                    max_delta = 5.0  # Allow larger steps during bootstrap
                    if self.calibration_update_count % 10 == 0:
                        logger.info(f"HW Calibration Bootstrap {broadcast_key}: "
                                   f"mean_d_clock={broadcast_mean:+.2f}ms, "
                                   f"hw_offset={old_hw_offset:+.2f}ms, "
                                   f"target={hw_target:+.2f}ms")
                elif hw_converged:
                    # Check if ionospheric conditions have changed significantly.
                    # If calibrated residual (mean D_clock) exceeds 2ms, the
                    # hardware offset is stale — temporarily use standard rate
                    # instead of glacial drift-tracking. (2026-03-04)
                    calibrated_residual = abs(broadcast_mean)
                    if calibrated_residual > 10.0:
                        # Large ionospheric shift (mode change, sunrise/sunset)
                        hw_alpha = 0.15
                        max_delta = 5.0  # Track fast at 5ms/cycle
                        logger.info(
                            f"HW Calibration UN-CONVERGE FAST {broadcast_key}: "
                            f"residual={broadcast_mean:+.2f}ms > 10ms, "
                            f"alpha={hw_alpha}, max_delta={max_delta}ms"
                        )
                    elif calibrated_residual > 2.0:
                        hw_alpha = 0.10
                        max_delta = 1.0  # Moderate tracking
                        logger.info(
                            f"HW Calibration UN-CONVERGE {broadcast_key}: "
                            f"residual={broadcast_mean:+.2f}ms > 2ms, "
                            f"alpha={hw_alpha}, max_delta={max_delta}ms"
                        )
                    else:
                        # Truly converged: near-frozen, only track thermal drift
                        hw_alpha = 0.005
                        max_delta = 0.01  # ±0.01ms per update — glacial
                else:
                    # Standard operation: moderate learning rate
                    base_alpha = max(0.05, min(0.15, 5.0 / old_cal.n_samples))
                    hw_alpha = base_alpha if validated else base_alpha * 0.3
                    max_delta = 0.5

                new_hw_offset = hw_alpha * hw_target + (1 - hw_alpha) * old_hw_offset
                
                # Rate limit hardware offset changes
                delta_hw = new_hw_offset - old_hw_offset
                if abs(delta_hw) > max_delta:
                    new_hw_offset = old_hw_offset + np.sign(delta_hw) * max_delta
                    logger.debug(f"HW Calibration {broadcast_key}: rate-limited Δ={delta_hw:.3f}ms to ±{max_delta}ms")
                
                # Check hardware convergence: if offset hasn't changed >0.05ms in 50+ samples
                if old_cal.n_samples > 50 and abs(new_hw_offset - old_hw_offset) < 0.05:
                    if not hw_converged:
                        hw_converged = True
                        logger.info(f"Hardware calibration CONVERGED for {broadcast_key}: "
                                   f"hw_offset={new_hw_offset:+.2f}ms (n={old_cal.n_samples})")
                
                logger.debug(f"HW Calibration {broadcast_key}: hw_alpha={hw_alpha:.3f}, "
                            f"hw_offset={new_hw_offset:+.3f}ms, hw_converged={hw_converged} "
                            f"(validated={validated}, bootstrap={is_bootstrap})")
            else:
                # First calibration: initialize hardware offset from current mean
                new_hw_offset = 0.0 - broadcast_mean
                logger.info(f"HW Calibration INIT {broadcast_key}: "
                           f"mean_d_clock={broadcast_mean:+.2f}ms, "
                           f"initial_hw_offset={new_hw_offset:+.2f}ms")
            
            # Sanity check hardware offset magnitude
            from .wwv_constants import MAX_CALIBRATION_OFFSET_MS
            
            # Hardware offsets should be small (matched filter delay + ADC latency)
            # Anything larger than the limit indicates a systematic error
            effective_limit = MAX_CALIBRATION_OFFSET_MS if self.kalman_converged else MAX_CALIBRATION_OFFSET_MS * 3
            
            if abs(new_hw_offset) > effective_limit:
                logger.error(
                    f"HW CALIBRATION SANITY FAILURE: {broadcast_key} hw_offset={new_hw_offset:+.1f}ms "
                    f"exceeds ±{effective_limit:.0f}ms limit (converged={self.kalman_converged}). "
                    f"This indicates a systematic error in tone detection. "
                    f"Rejecting this calibration update."
                )
                continue
            
            # offset_ms is kept for backward compatibility but now tracks hardware_offset_ms
            cumulative_n = (old_cal.n_samples if old_cal else 0) + 1
            self.calibration[broadcast_key] = BroadcastCalibration(
                station=station,
                frequency_mhz=freq,
                offset_ms=new_hw_offset,  # Now same as hardware offset (no circular zeroing)
                uncertainty_ms=broadcast_std / np.sqrt(len(d_clocks)),  # Standard error
                n_samples=cumulative_n,
                last_updated=datetime.now(timezone.utc).isoformat(),
                reference_station=self.reference_station,
                hardware_offset_ms=new_hw_offset,
                hardware_converged=hw_converged
            )
        
        # Auto-save calibration every 10 updates
        self.calibration_update_count += 1
        # Only save when Kalman has converged to avoid overwriting good state
        if self.calibration_update_count % 10 == 0:
            try:
                self._save_calibration()
                logger.debug(f"Auto-saved calibration and Kalman state (update #{self.calibration_update_count})")
            except Exception as e:
                logger.error(f"Failed to auto-save calibration: {e}")
    
    def _update_long_term_stats(self, measurements: List[BroadcastMeasurement]) -> None:
        """
        Update long-term sufficient statistics for drift estimation.
        
        Key metrological insight: WWV/CHU/BPM transmit EXACTLY on UTC.
        Ionospheric propagation variations are ZERO-MEAN over long periods.
        Therefore, the long-term linear fit of ANY single broadcast converges
        to the true GPSDO drift rate as N → ∞.
        
        We maintain sufficient statistics for online linear regression:
            D_clock(t) = slope × t + intercept + noise
        
        Sufficient statistics (Welford's online algorithm for regression):
            n: count
            sum_t: Σt
            sum_y: Σy
            sum_tt: Σt²
            sum_ty: Σty
        
        From these, we can compute slope and intercept at any time:
            slope = (n×Σty - Σt×Σy) / (n×Σt² - (Σt)²)
            intercept = (Σy - slope×Σt) / n
        
        DISCONTINUITY HANDLING:
        - Detect step changes > threshold (GPSDO unlock, NTP step, service restart)
        - On discontinuity: log warning but continue accumulating
        - The linear regression is robust to outliers over long periods
        - Severe discontinuities (>50ms) trigger stats reset for that broadcast
        """
        current_time = time.time()
        
        # Periodic save of long-term stats (every ~10 minutes)
        # Use time-based trigger instead of sample count for reliability
        if not hasattr(self, '_last_long_term_save'):
            self._last_long_term_save = 0.0
        
        if current_time - self._last_long_term_save > 600.0:  # Every 10 minutes
            if self.long_term_stats:
                self._save_long_term_stats()
                self._last_long_term_save = current_time
                logger.debug(f"Saved long-term drift stats ({sum(s.get('n', 0) for s in self.long_term_stats.values())} samples)")
        
        for m in measurements:
            if m.station == 'GLOBAL_DIFF':
                continue
            
            broadcast_key = self._get_broadcast_key(m.station, m.frequency_mhz)
            
            # Time in hours since Unix epoch (absolute reference)
            # Using hours since epoch keeps numbers manageable while maintaining precision
            t_hours = current_time / 3600.0
            y = m.d_clock_ms
            
            # ================================================================
            # DISCONTINUITY DETECTION
            # ================================================================
            # Check for step changes that indicate a discontinuity event
            if broadcast_key in self.long_term_last_values:
                last_y = self.long_term_last_values[broadcast_key]
                delta = abs(y - last_y)
                
                if delta > self.long_term_discontinuity_threshold:
                    # Detected a step change
                    if delta > 50.0:
                        # Severe discontinuity (>50ms) - likely GPSDO unlock or major issue
                        # Reset stats for this broadcast to avoid corrupting long-term estimate
                        logger.warning(
                            f"SEVERE DISCONTINUITY detected for {broadcast_key}: "
                            f"Δ={delta:.1f}ms (>{50}ms threshold). "
                            f"Resetting long-term stats for this broadcast."
                        )
                        if broadcast_key in self.long_term_stats:
                            del self.long_term_stats[broadcast_key]
                    else:
                        # Moderate discontinuity - log but continue
                        # Linear regression will handle this as noise over time
                        logger.info(
                            f"Discontinuity detected for {broadcast_key}: "
                            f"Δ={delta:.1f}ms (>{self.long_term_discontinuity_threshold}ms). "
                            f"Continuing accumulation (regression is robust to outliers)."
                        )
            
            # Update last value for next discontinuity check
            self.long_term_last_values[broadcast_key] = y
            
            # ================================================================
            # UPDATE SUFFICIENT STATISTICS
            # ================================================================
            if broadcast_key not in self.long_term_stats:
                self.long_term_stats[broadcast_key] = {
                    'n': 0,
                    'sum_t': 0.0,
                    'sum_y': 0.0,
                    'sum_tt': 0.0,
                    'sum_ty': 0.0,
                    'sum_yy': 0.0,  # For residual variance
                    'first_time': current_time,
                    'last_time': current_time,
                }
            
            stats = self.long_term_stats[broadcast_key]
            stats['n'] += 1
            stats['sum_t'] += t_hours
            stats['sum_y'] += y
            stats['sum_tt'] += t_hours * t_hours
            stats['sum_ty'] += t_hours * y
            stats['sum_yy'] += y * y
            stats['last_time'] = current_time
    
    def get_long_term_drift_estimate(self, broadcast_key: str = None) -> Dict:
        """
        Compute long-term drift estimate from accumulated measurements.
        
        If broadcast_key is None, returns combined estimate from all broadcasts.
        
        Returns:
            Dictionary with:
            - slope_ms_per_hour: GPSDO drift rate estimate (ms/hour)
            - slope_ppb: Drift rate in parts per billion
            - intercept_ms: Systematic offset estimate
            - n_samples: Total measurements used
            - duration_hours: Time span of measurements
            - slope_uncertainty_ms_per_hour: 1-sigma uncertainty on slope
            - intercept_uncertainty_ms: 1-sigma uncertainty on intercept
            - residual_std_ms: RMS of ionospheric variations
        """
        if broadcast_key and broadcast_key in self.long_term_stats:
            stats_list = [(broadcast_key, self.long_term_stats[broadcast_key])]
        else:
            stats_list = list(self.long_term_stats.items())
        
        if not stats_list:
            return {'error': 'No long-term statistics available'}
        
        # Combine sufficient statistics from all broadcasts
        # (This is valid because they all measure the same GPSDO drift)
        n_total = sum(s['n'] for _, s in stats_list)
        if n_total < 10:
            return {'error': f'Insufficient samples ({n_total} < 10)'}
        
        sum_t = sum(s['sum_t'] for _, s in stats_list)
        sum_y = sum(s['sum_y'] for _, s in stats_list)
        sum_tt = sum(s['sum_tt'] for _, s in stats_list)
        sum_ty = sum(s['sum_ty'] for _, s in stats_list)
        sum_yy = sum(s['sum_yy'] for _, s in stats_list)
        
        # Linear regression: y = slope * t + intercept
        denominator = n_total * sum_tt - sum_t * sum_t
        if abs(denominator) < 1e-10:
            return {'error': 'Degenerate regression (all measurements at same time)'}
        
        slope = (n_total * sum_ty - sum_t * sum_y) / denominator
        intercept = (sum_y - slope * sum_t) / n_total
        
        # Residual variance (ionospheric noise)
        # σ² = (Σy² - intercept×Σy - slope×Σty) / (n-2)
        ss_residual = sum_yy - intercept * sum_y - slope * sum_ty
        residual_var = ss_residual / max(1, n_total - 2)
        residual_std = np.sqrt(max(0, residual_var))
        
        # Uncertainty on slope and intercept
        # σ_slope = σ_residual / sqrt(Σ(t-t_mean)²) = σ_residual × sqrt(n / denominator)
        # σ_intercept = σ_residual × sqrt(Σt² / (n × denominator))
        slope_var = residual_var * n_total / denominator
        intercept_var = residual_var * sum_tt / denominator
        slope_uncertainty = np.sqrt(max(0, slope_var))
        intercept_uncertainty = np.sqrt(max(0, intercept_var))
        
        # Duration
        first_time = min(s['first_time'] for _, s in stats_list)
        last_time = max(s['last_time'] for _, s in stats_list)
        duration_hours = (last_time - first_time) / 3600.0
        
        # Convert slope to ppb (parts per billion)
        # slope is in ms/hour
        # 1 ms/hour = 1e-3 s / 3600 s = 2.78e-7 = 278 ppb
        slope_ppb = slope * 277.78
        slope_uncertainty_ppb = slope_uncertainty * 277.78
        
        return {
            'slope_ms_per_hour': slope,
            'slope_ppb': slope_ppb,
            'intercept_ms': intercept,
            'n_samples': n_total,
            'n_broadcasts': len(stats_list),
            'duration_hours': duration_hours,
            'slope_uncertainty_ms_per_hour': slope_uncertainty,
            'slope_uncertainty_ppb': slope_uncertainty_ppb,
            'intercept_uncertainty_ms': intercept_uncertainty,
            'residual_std_ms': residual_std,
        }
    
    def log_long_term_drift_status(self) -> None:
        """Log current long-term drift estimate for monitoring."""
        estimate = self.get_long_term_drift_estimate()
        
        if 'error' in estimate:
            logger.debug(f"Long-term drift: {estimate['error']}")
            return
        
        # Only log periodically (every ~10 minutes worth of samples)
        total_samples = estimate['n_samples']
        if total_samples % 75 != 0:  # ~10 min at 8s cadence
            return
        
        logger.info(
            f"LONG-TERM DRIFT ESTIMATE: "
            f"slope={estimate['slope_ppb']:+.2f}±{estimate['slope_uncertainty_ppb']:.2f} ppb, "
            f"intercept={estimate['intercept_ms']:+.3f}±{estimate['intercept_uncertainty_ms']:.3f} ms, "
            f"residual_std={estimate['residual_std_ms']:.2f} ms, "
            f"n={estimate['n_samples']} samples over {estimate['duration_hours']:.1f} hours"
        )
        
        # Check if drift is significant (> 3σ from zero)
        if abs(estimate['slope_ppb']) > 3 * estimate['slope_uncertainty_ppb']:
            logger.warning(
                f"SIGNIFICANT GPSDO DRIFT DETECTED: {estimate['slope_ppb']:+.2f} ppb "
                f"({estimate['slope_ms_per_hour']:+.4f} ms/hour). "
                f"This exceeds 3σ uncertainty and may indicate GPSDO issue."
            )
    
    def _kalman_update(self, measurement: float, measurement_uncertainty: float, use_l2: bool = False) -> float:
        """
        Two-tier Kalman filter for stable baseline maintenance.
        
        DUAL KALMAN ARCHITECTURE (2026-02-07):
        When use_l2=False (default): operates on L1 state (kalman_state/kalman_P)
        When use_l2=True: operates on independent L2 state (kalman_state_l2/kalman_P_l2)
        This ensures TSL1 and TSL2 chrony feeds carry genuinely different estimates.
        
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
            use_l2: If True, operate on the independent L2 Kalman state
            
        Returns:
            Kalman filter uncertainty (converges over time)
        """
        # Select which Kalman state to operate on
        feed_label = "L2" if use_l2 else "L1"
        if use_l2:
            k_state = self.kalman_state_l2
            k_P = self.kalman_P_l2
            k_init = self.kalman_initialized_l2
            k_n = self.kalman_n_updates_l2
            k_conv = self.kalman_converged_l2
        else:
            k_state = self.kalman_state
            k_P = self.kalman_P
            k_init = self.kalman_initialized
            k_n = self.kalman_n_updates
            k_conv = self.kalman_converged
        
        # DIAGNOSTIC: Log entry to confirm this function is being called
        if not hasattr(self, '_kalman_entry_count'):
            self._kalman_entry_count = 0
        self._kalman_entry_count += 1
        if self._kalman_entry_count <= 10:
            logger.info(f"[KALMAN_ENTRY] _kalman_update #{self._kalman_entry_count} ({feed_label}): meas={measurement:.4f}ms, unc={measurement_uncertainty:.4f}ms, initialized={k_init}")
        
        # Initialize on first measurement
        # CRITICAL FIX (2026-02-06): Initialize from first measurement, not 0.
        # With hardware-only calibration, measurements carry real clock offset.
        # Starting from 0 would take many updates to converge to the real value.
        if not k_init:
            k_state[0] = measurement  # Start from first measurement
            k_state[1] = 0.0  # No drift assumed (GPSDO)
            init_var = max(measurement_uncertainty ** 2, 1.0)
            k_P[:] = np.array([[init_var, 0.0], [0.0, 1e-4]])
            k_n = 1
            if use_l2:
                self.kalman_initialized_l2 = True
                self.kalman_n_updates_l2 = k_n
            else:
                self.kalman_initialized = True
                self.kalman_n_updates = k_n
            logger.info(f"Kalman filter ({feed_label}) initialized from measurement: {measurement:+.3f}ms ± {measurement_uncertainty:.3f}ms")
            return measurement_uncertainty
        
        # State transition matrix (1 minute step)
        dt = 1.0  # 1 minute
        F = np.array([[1.0, dt], [0.0, 1.0]])
        
        # Process noise (clock drift uncertainty)
        q_offset = 0.01  # ms^2 per minute (allows ~0.1ms/min tracking)
        q_drift = 1e-8   # (ms/min)^2 per minute (GPSDO drift is negligible)
        Q = np.array([[q_offset, 0.0], [0.0, q_drift]])
        
        # Predict step
        x_pred = F @ k_state
        P_pred = F @ k_P @ F.T + Q
        
        # Measurement matrix (we only observe offset)
        H = np.array([[1.0, 0.0]])
        
        # Measurement noise
        R = np.array([[measurement_uncertainty ** 2]])
        
        # Kalman gain
        S = H @ P_pred @ H.T + R
        K = P_pred @ H.T @ np.linalg.inv(S)
        
        # Update step
        y = (measurement - (H @ x_pred).item())  # Innovation (scalar)
        k_state[:] = x_pred + K.flatten() * y
        k_P[:] = (np.eye(2) - K @ H) @ P_pred
        
        # Increment update counter and check convergence
        k_n += 1
        
        # DIAGNOSTIC: Log first 20 updates after restart to track settling behavior
        if not use_l2:
            self._updates_since_restart += 1
            if self._updates_since_restart <= 20:
                logger.info(
                    f"[SETTLING_DIAG] Update #{self._updates_since_restart}: "
                    f"meas={measurement:.4f}ms, innov={y:.4f}ms, "
                    f"state=[{k_state[0]:.4f}, {k_state[1]:.6f}], "
                    f"P_diag=[{k_P[0,0]:.4f}, {k_P[1,1]:.8f}]"
                )
        
        if not k_conv and k_n >= self.kalman_convergence_threshold:
            k_conv = True
            k_P[:] = np.array([[1.0, 0.0], [0.0, 1e-6]])
            k_state[1] = 0.0
            
            logger.info(
                f"Kalman filter ({feed_label}) CONVERGED after {k_n} updates. "
                f"Baseline offset: {k_state[0]:.3f}ms. "
                f"Transitioning to operational mode: Covariance clamped, baseline locked, DRIFT FROZEN AT 0."
            )
        
        # OPERATIONAL MODE: Force zero drift to prevent linear "walk away" from 0
        if k_conv:
            k_state[1] = 0.0
        
        # Divergence recovery
        if abs(k_state[0]) > 20.0:
            logger.error(
                f"Kalman filter ({feed_label}) diverged: state={k_state[0]:.3f}ms, "
                f"resetting to measurement value for graceful recovery"
            )
            k_state[:] = np.array([measurement, 0.0])
            k_P[:] = np.array([[10.0, 0.0], [0.0, 1.0]])
            k_n = 1
        
        # Write back counters and flags
        if use_l2:
            self.kalman_n_updates_l2 = k_n
            self.kalman_converged_l2 = k_conv
        else:
            self.kalman_n_updates = k_n
            self.kalman_converged = k_conv
        
        # TIER 2: Operational mode - maintain stable baseline (L1 only)
        # Drift detection window is shared state, only update from L1 feed
        if not use_l2 and self.kalman_converged:
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
        
        # Return uncertainty (sqrt of offset variance) from the active feed
        kalman_uncertainty = np.sqrt(k_P[0, 0])
        
        # Minimum uncertainty floor based on measurement quality
        min_uncertainty = max(0.1, measurement_uncertainty / np.sqrt(max(k_n, 1)))
        
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
        # Group by station (exclude GLOBAL_DIFF synthetic measurements and BPM)
        # BPM is excluded from fusion (weight=0) due to 18-36ms cross-station
        # disagreement from its 11,000km path. Including it in cross-validation
        # would always trigger INTER_ANOMALY.
        station_groups = defaultdict(list)
        for m, cal_val in zip(measurements, calibrated):
            if m.station not in ('GLOBAL_DIFF', 'BPM'):
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
        
        base_threshold = 5.0 if not calibration_converged else 3.5  # ms (raised from 2.5: CHU-WWVH path difference routinely 2.5-5ms)
        
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
    
    def _validate_cross_frequency_d_clock(
        self,
        measurements: List[BroadcastMeasurement]
    ) -> Tuple[bool, str, Dict[str, float]]:
        """
        Cross-validate raw D_clock across frequencies for the same station.
        
        If the physics model is correct, D_clock should be frequency-independent
        (the 1/f² ionospheric term cancels out in D_clock = raw_toa - expected_delay).
        
        Systematic trends with frequency indicate:
        - Propagation model error (wrong mode, wrong TEC)
        - Unmodeled ionospheric dispersion
        - Hardware/frequency-dependent biases
        
        This is a powerful validation of the physics model quality.
        
        Args:
            measurements: List of broadcast measurements
            
        Returns:
            Tuple of (is_valid, reason, station_deviations):
                - is_valid: True if all stations show frequency-independent D_clock
                - reason: Explanation of validation result
                - station_deviations: Dict of station->max_frequency_deviation_ms
        """
        # Group measurements by station
        station_freq_groups = defaultdict(lambda: defaultdict(list))
        for m in measurements:
            if m.station in ('GLOBAL_DIFF', 'BPM', 'UNKNOWN'):
                continue
            if m.d_clock_ms is not None and not np.isnan(m.d_clock_ms):
                station_freq_groups[m.station][m.frequency_mhz].append(m.d_clock_ms)
        
        station_deviations = {}
        all_valid = True
        reasons = []
        
        for station, freq_groups in station_freq_groups.items():
            # Need at least 2 frequencies for comparison
            if len(freq_groups) < 2:
                continue
            
            # Calculate mean D_clock per frequency
            freq_means = {}
            for freq, d_clocks in freq_groups.items():
                freq_means[freq] = np.mean(d_clocks)
            
            # Check if D_clock is frequency-independent
            # The 1/f² ionospheric term should cancel out in D_clock calculation
            # so all frequencies should agree within measurement noise (~2-5ms)
            mean_values = list(freq_means.values())
            if len(mean_values) >= 2:
                max_diff = max(mean_values) - min(mean_values)
                station_deviations[station] = max_diff
                
                # Threshold: 5ms tolerance for frequency independence
                # Physics says D_clock should be identical across frequencies
                # after the 1/f² ionospheric correction
                CROSS_FREQ_THRESHOLD_MS = 5.0
                
                if max_diff > CROSS_FREQ_THRESHOLD_MS:
                    all_valid = False
                    freq_info = ', '.join([f"{f}MHz={v:+.2f}ms" for f, v in sorted(freq_means.items())])
                    reasons.append(
                        f"{station}: {max_diff:.2f}ms spread across frequencies "
                        f"(threshold: {CROSS_FREQ_THRESHOLD_MS}ms). [{freq_info}]"
                    )
                    logger.warning(
                        f"Cross-frequency validation FAILED for {station}: "
                        f"{max_diff:.2f}ms max deviation. Physics model error suspected. "
                        f"Frequencies: {freq_info}"
                    )
                else:
                    logger.debug(
                        f"Cross-frequency validation OK for {station}: "
                        f"{max_diff:.2f}ms max deviation across {len(freq_means)} frequencies"
                    )
        
        if all_valid:
            if station_deviations:
                avg_dev = np.mean(list(station_deviations.values()))
                reason = f"All {len(station_deviations)} stations show frequency-independent D_clock (avg deviation: {avg_dev:.2f}ms)"
            else:
                reason = "No multi-frequency data available for validation"
        else:
            reason = '; '.join(reasons)
        
        return all_valid, reason, station_deviations
    
    def _phase(self, name: str):
        """Return the service-provided phase timer if installed, else a
        no-op context manager so unit tests and ad-hoc callers don't
        need a metrics stub."""
        if self.loop_metrics is None:
            from contextlib import nullcontext
            return nullcontext()
        return self.loop_metrics.phase(name)

    def fuse(self, lookback_minutes: int = 30, force_l1_only: bool = False, skip_write: bool = False) -> Optional[FusedResult]:
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

        # Read latest measurements (L1-only mode skips L2 calibration data)
        with self._phase("hdf5_read"):
            measurements = self._read_latest_measurements(lookback_minutes, force_l1_only=force_l1_only)
        
        # ====================================================================
        # BPM EXCLUSION (2026-02-07): Remove BPM from fusion pipeline
        # ====================================================================
        # BPM's 11,000 km trans-Pacific path introduces 18-36 ms cross-station
        # disagreement that dominates uncertainty and prevents grade improvement.
        # BPM data is still collected by per-broadcast Kalmans and logged for
        # ionospheric science, but excluded from the timing fusion.
        n_bpm = sum(1 for m in measurements if m.station == 'BPM')
        if n_bpm > 0:
            measurements = [m for m in measurements if m.station != 'BPM']
            logger.debug(f"BPM exclusion: removed {n_bpm} BPM measurements from fusion (kept for science)")
        
        # ====================================================================
        # BOOTSTRAP VALIDATION (2026-01-24): Multi-station correlation
        # ====================================================================
        # Feed measurements to bootstrap validator for cross-station agreement.
        # This validates the RTP-to-UTC offset using WWVH > WWV delay ordering,
        # CHU timing consistency, and geographic bounds.
        self._validate_bootstrap_with_measurements(measurements)
        
        # ====================================================================
        # STEP 1: Apply per-broadcast Kalman filtering (v6.0 Architecture)
        # ====================================================================
        # Each broadcast's d_clock is filtered by its own Kalman filter.
        # This rejects detection glitches while preserving real ionospheric dynamics.
        with self._phase("kalman_apply"):
            measurements = self._apply_broadcast_kalmans(measurements)
        
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
        
        # CRITICAL FIX: Pre-fusion outlier rejection using CALIBRATED values
        # Raw d_clock_ms has 30-60ms offsets between broadcasts (propagation model error).
        # Using raw values makes the MAD huge, letting real outliers slip through.
        # Apply calibration first so outlier detection sees the residual scatter.
        if len(measurements) > 2:
            with self._phase("calibration_apply"):
                cal_for_outlier = self._apply_calibration(measurements)
            d_clocks = np.array(cal_for_outlier)
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
                        f"cal_d_clock={d:.2f}ms (median={median_d:.2f}ms, dev={deviations[i]:.2f}ms > {filter_threshold:.1f}ms, "
                        f"raw={measurements[i].d_clock_ms:.2f}ms)"
                    )
            
            if len(keep_indices) == 0:
                # CRITICAL FIX (2026-03-04): Never reject ALL measurements.
                # In single-station mode with stale calibration, the sigma cap
                # (5ms) creates a 17.5ms threshold that rejects every channel
                # when ionospheric conditions have changed since calibration.
                # Starving the Kalman is worse than feeding it noisy data —
                # the Kalman will inflate uncertainty appropriately.
                logger.warning(
                    f"Outlier filter would reject ALL {len(measurements)} measurements "
                    f"(median={median_d:.1f}ms, mad={mad:.1f}ms, threshold={filter_threshold:.1f}ms). "
                    f"Keeping all to prevent Kalman starvation."
                )
            elif len(keep_indices) < len(measurements):
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
        
        # ====================================================================
        # TICK TIMING INTEGRATION (55+ estimates per minute)
        # ====================================================================
        # Per-second tick timing provides many more timing estimates than the
        # single minute marker. The averaged tick timing has lower uncertainty
        # due to √N improvement from averaging.
        try:
            tick_observations = self._read_tick_timing_observations(lookback_minutes)
            if tick_observations:
                # Get the most recent minute's tick data
                all_minutes = set()
                for ch_data in tick_observations.values():
                    all_minutes.update(ch_data.keys())
                
                if all_minutes:
                    latest_minute = max(all_minutes)
                    tick_count = 0
                    
                    for channel, per_minute in tick_observations.items():
                        if latest_minute in per_minute:
                            for tick_obs in per_minute[latest_minute]:
                                # Tick timing provides d_clock directly
                                # Weight by number of valid windows (more windows = lower uncertainty)
                                n_windows = tick_obs.get('n_windows', 10)
                                std_ms = tick_obs.get('std_ms', 5.0)
                                
                                # Confidence based on window count and std
                                confidence = min(0.9, n_windows / 55.0) * min(1.0, 3.0 / max(0.5, std_ms))
                                
                                measurements.append(
                                    BroadcastMeasurement(
                                        timestamp=latest_minute,
                                        station=tick_obs.get('station', 'UNKNOWN'),
                                        frequency_mhz=tick_obs.get('frequency_mhz', 0.0),
                                        d_clock_ms=tick_obs.get('timing_ms', 0.0),
                                        propagation_delay_ms=0.0,
                                        propagation_mode='TICK',
                                        confidence=confidence,
                                        snr_db=15.0,
                                        quality_grade='B',
                                        channel_name=channel
                                    )
                                )
                                tick_count += 1
                    
                    if tick_count > 0:
                        logger.info(f"Integrated {tick_count} tick timing measurements into fusion")
        except Exception as e:
            logger.debug(f"Tick timing integration failed: {e}")
        
        # ====================================================================
        # CHU FSK TIMING INTEGRATION (Precise 500ms boundary)
        # ====================================================================
        # CHU FSK provides the most precise timing reference from HF broadcasts.
        # The 500ms boundary is decoded from the FSK time code with ~0.1ms precision.
        #
        # Confidence is scaled by BER (frames_decoded/9):
        #   9/9 frames → full confidence (clean channel)
        #   2/9 frames → 22% confidence (heavy fading, marginal decode)
        #
        # DUT1 and TAI-UTC are passed through to self for downstream use:
        #   - DUT1: corrects solar zenith in propagation model (UT1 = UTC + DUT1)
        #   - TAI-UTC: leap second awareness (hold Kalman during transition)
        try:
            fsk_timing = self._read_chu_fsk_timing(lookback_minutes)
            if fsk_timing:
                # Get the most recent FSK timing
                latest_fsk_minute = max(fsk_timing.keys())
                fsk_data = fsk_timing[latest_fsk_minute]
                
                # BER-weighted confidence: decode_confidence × (frames/9)
                frames = fsk_data.get('frames_decoded', 0)
                decode_rate = frames / 9.0 if frames > 0 else 0.0
                raw_confidence = fsk_data.get('decode_confidence', 0.5)
                confidence = min(0.95, raw_confidence * decode_rate)
                
                measurements.append(
                    BroadcastMeasurement(
                        timestamp=latest_fsk_minute,
                        station='CHU',
                        frequency_mhz=7.85,  # Primary CHU frequency
                        d_clock_ms=fsk_data.get('timing_offset_ms', 0.0),
                        propagation_delay_ms=0.0,
                        propagation_mode='FSK',
                        confidence=confidence,
                        snr_db=20.0,
                        quality_grade='A' if decode_rate > 0.5 else 'B',
                        channel_name=fsk_data.get('channel', 'CHU')
                    )
                )
                logger.info(f"Integrated CHU FSK timing: {fsk_data.get('timing_offset_ms', 0.0):+.2f}ms "
                           f"(confidence={confidence:.2f}, frames={frames}/9, "
                           f"decode_rate={decode_rate:.2f})")
                
                # Pass through DUT1 and TAI-UTC for downstream use
                dut1 = fsk_data.get('dut1_seconds')
                tai_utc = fsk_data.get('tai_utc')
                if dut1 is not None:
                    self._fsk_dut1 = dut1
                    # Feed DUT1 to propagation model for UT1-corrected solar zenith
                    if self.physics_model is not None and hasattr(self.physics_model, 'set_dut1'):
                        self.physics_model.set_dut1(dut1)
                if tai_utc is not None:
                    if hasattr(self, '_fsk_tai_utc') and self._fsk_tai_utc != tai_utc:
                        logger.warning(f"Fusion: TAI-UTC changed {self._fsk_tai_utc} → {tai_utc} "
                                      f"— leap second transition, holding Kalman")
                        self._fsk_leap_second_hold = True
                    else:
                        self._fsk_leap_second_hold = False
                    self._fsk_tai_utc = tai_utc
        except Exception as e:
            logger.debug(f"CHU FSK timing integration failed: {e}")
        
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
                
                # ================================================================
                # GNSS VTEC IONOSPHERIC CORRECTION (v6.1 - 2026-01-24)
                # ================================================================
                # The propagation model computed D_clock using a MODELED TEC value.
                # GNSS VTEC provides a DIRECT MEASUREMENT of the actual TEC.
                # 
                # Physics: τ_iono = 40.3 × TEC × n_hops / (c × f²) [seconds]
                #        = 40.3 × TEC × n_hops / f² × 1000 [ms, f in Hz]
                #
                # Correction: D_clock_corrected = D_clock + (model_iono - gnss_iono)
                #           = D_clock + 40.3 × (TEC_model - TEC_gnss) × n_hops / f² × 1000
                #
                # This is metrologically justified because:
                # 1. GNSS VTEC is a direct measurement (not a model)
                # 2. The 1/f² physics is well-established
                # 3. We're correcting the model error, not adding new uncertainty
                # ================================================================
                
                corrections_applied = 0
                for m in measurements:
                    if m.station == 'GLOBAL_DIFF' or m.station == 'UNKNOWN':
                        continue
                        
                    # Compute baseline prediction (what the model predicts)
                    prediction = self.physics_model.predict(
                        station=m.station,
                        frequency_mhz=m.frequency_mhz,
                        utc_time=datetime.fromtimestamp(m.timestamp, tz=timezone.utc)
                    )
                    primary = prediction.get_primary_arrival()
                    
                    if primary and primary.mode.n_hops > 0:
                        # Extract model TEC (what was used to compute D_clock)
                        model_tec = primary.slant_tec_tecu if primary.slant_tec_tecu else 20.0
                        n_hops = primary.mode.n_hops
                        
                        # Calculate TEC difference
                        tec_diff = model_tec - vtec_tecu  # Positive if model overestimated
                        
                        # Compute ionospheric delay correction
                        # Using same formula as IonosphericDelayCalculator:
                        # τ_ms = IONO_DELAY_CONSTANT_MS × TEC / f²
                        # where IONO_DELAY_CONSTANT_MS ≈ 0.1345 ms·MHz²/TECU
                        # and f is in MHz, TEC in TECU
                        #
                        # For slant TEC: STEC ≈ VTEC × obliquity_factor
                        # Typical obliquity for HF paths: 1.5-2.0
                        IONO_DELAY_CONSTANT_MS = 40.3 / 299792.458 * 1e16 / 1e12  # ≈ 0.1345
                        obliquity_factor = 1.5  # Typical mapping function for HF paths
                        f_sq = m.frequency_mhz ** 2
                        delta_iono_ms = IONO_DELAY_CONSTANT_MS * tec_diff * n_hops * obliquity_factor / f_sq
                        
                        # Store original for logging
                        original_d_clock = m.d_clock_ms
                        
                        # Apply correction only if significant (> 0.1 ms)
                        if abs(delta_iono_ms) > 0.1:
                            m.d_clock_ms = original_d_clock + delta_iono_ms
                            m.propagation_mode = f"{prediction.primary_mode}+GNSS_TEC"
                            m.confidence = min(1.0, m.confidence * 1.2)  # Boost confidence
                            corrections_applied += 1
                            
                            logger.debug(
                                f"  {m.station} {m.frequency_mhz}MHz: TEC correction "
                                f"model={model_tec:.1f} gnss={vtec_tecu:.1f} ΔTEC={tec_diff:+.1f} TECU, "
                                f"Δiono={delta_iono_ms:+.3f}ms, D_clock {original_d_clock:.3f}->{m.d_clock_ms:.3f}ms"
                            )
                        else:
                            # Small correction - just validate
                            m.propagation_mode = f"{prediction.primary_mode}+GNSS_VALIDATED"
                            m.confidence = min(1.0, m.confidence * 1.1)
                            logger.debug(
                                f"  {m.station} {m.frequency_mhz}MHz: TEC validated "
                                f"(ΔTEC={tec_diff:+.1f} TECU, Δiono={delta_iono_ms:+.3f}ms < 0.1ms threshold)"
                            )
                
                if corrections_applied > 0:
                    logger.info(f"Applied GNSS TEC correction to {corrections_applied} measurements")
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
                    # Gate TEC inputs by dominant propagation mode to avoid
                    # mixing incompatible path families (e.g., 1F and 2F).
                    mode_groups = defaultdict(list)
                    invalid_modes = {
                        '', 'UNKNOWN', 'FALLBACK', 'TICK', 'FSK', 'CHU_FSK',
                        'TEC_VALIDATED', 'TEC_UNREALISTIC', 'TEC_POOR_FIT'
                    }
                    for m in station_meas:
                        base_mode = (m.propagation_mode or '').split('+')[0].strip().upper()
                        if base_mode in invalid_modes:
                            continue
                        mode_groups[base_mode].append(m)

                    if mode_groups:
                        dominant_mode, dominant_meas = max(
                            mode_groups.items(),
                            key=lambda kv: len(kv[1])
                        )
                        if len(dominant_meas) >= 2:
                            station_meas = dominant_meas
                            logger.debug(
                                f"TEC mode gate for {station}: using {len(station_meas)} "
                                f"measurements from dominant mode {dominant_mode}"
                            )
                        else:
                            logger.info(
                                f"Skipping TEC for {station}: no dominant propagation mode "
                                f"with >=2 measurements"
                            )
                            continue

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
                                'uncertainty_ms': getattr(m, 'tof_uncertainty_ms', None) or max(0.1, 1.0 / max(0.001, m.confidence)),
                                'mode_confidence': m.l2_model_confidence,
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
                        # ================================================================
                        # v6.0 ARCHITECTURE: Use TEC to extract ionosphere-free D_clock
                        # ================================================================
                        # The TEC estimator fits: ToA(f) = T_vacuum + k/f²
                        # t_vacuum_error_ms is the ionosphere-free geometric delay
                        # This REMOVES ionospheric bias from timing measurements
                        
                        # Validate TEC result is not NaN
                        if np.isnan(tec_result.tec_u) or np.isnan(tec_result.confidence):
                            logger.warning(f"TEC solver produced NaN for {station} (tec={tec_result.tec_u}, conf={tec_result.confidence}) - skipping")
                        elif tec_result.confidence > 0.5 and 1.0 <= tec_result.tec_u <= 200.0:
                            # TEC is physically reasonable (1-200 TECU) and well-fit
                            logger.info(
                                f"TEC Solved for {station}: {tec_result.tec_u:.1f} TECU "
                                f"(R2={tec_result.confidence:.2f}), "
                                f"t_vacuum={tec_result.t_vacuum_error_ms:.3f}ms"
                            )

                            if not self.is_rtp_authority:
                                # ============================================================
                                # FUSION MODE: Apply ionosphere-free D_clock correction
                                # ============================================================
                                # In Fusion mode (no GPS+PPS) the propagation model's
                                # ionospheric term is the dominant error source.  The TEC
                                # fit solves:
                                #
                                #   D_clock(f) = t_vacuum + K·TEC/f²
                                #
                                # The intercept t_vacuum_error_ms IS the ionosphere-free
                                # D_clock — independent of which propagation mode was used
                                # and independent of what TEC value the model assumed.
                                # Replacing each measurement's d_clock_ms with t_vacuum
                                # removes the ionospheric dispersion entirely.
                                #
                                # Guard: only apply when fit confidence is high enough that
                                # the intercept is well-determined.  With N=3 (CHU) and
                                # R²>0.5 the intercept uncertainty is typically <1ms.
                                # With N=2 the fit is exact (R²=1 always) so we require
                                # a higher confidence threshold to avoid over-fitting noise.
                                n_pts = tec_result.n_frequencies
                                min_conf = 0.7 if n_pts >= 3 else 0.85
                                if tec_result.confidence >= min_conf:
                                    t_vac = tec_result.t_vacuum_error_ms
                                    for m in station_meas:
                                        old_d = m.d_clock_ms
                                        m.d_clock_ms = t_vac
                                        m.propagation_mode = 'TEC_CORRECTED'
                                        m.confidence = min(1.0, m.confidence * 1.2)
                                        logger.debug(
                                            f"  TEC correction {station} {m.frequency_mhz}MHz: "
                                            f"D_clock {old_d:+.3f}ms → {t_vac:+.3f}ms "
                                            f"(Δ={t_vac - old_d:+.3f}ms, "
                                            f"TEC={tec_result.tec_u:.1f} TECU)"
                                        )
                                    logger.info(
                                        f"TEC correction applied to {len(station_meas)} "
                                        f"{station} measurements: "
                                        f"t_vacuum={t_vac:+.3f}ms, "
                                        f"TEC={tec_result.tec_u:.1f} TECU, "
                                        f"conf={tec_result.confidence:.2f}"
                                    )
                                else:
                                    # Fit exists but intercept not well-determined — validate only
                                    for m in station_meas:
                                        m.propagation_mode = 'TEC_VALIDATED'
                                        m.confidence = min(1.0, m.confidence * 1.1)
                                    logger.info(
                                        f"TEC fit for {station} below correction threshold "
                                        f"(conf={tec_result.confidence:.2f} < {min_conf:.2f}): "
                                        f"validated only"
                                    )
                            else:
                                # ============================================================
                                # RTP MODE: TEC is a science observable, not a correction
                                # ============================================================
                                # In RTP mode the GPS+PPS reference is ~50µs accurate.
                                # D_clock is a direct measurement of the propagation path.
                                # Applying TEC correction would remove the ionospheric signal
                                # we want to measure.  Only boost confidence.
                                for m in station_meas:
                                    m.propagation_mode = 'TEC_VALIDATED'
                                    m.confidence = min(1.0, m.confidence * 1.15)
                                logger.debug(
                                    f"  TEC validated {len(station_meas)} measurements from "
                                    f"{station} (RTP mode — not correcting), "
                                    f"t_vacuum={tec_result.t_vacuum_error_ms:.3f}ms, "
                                    f"residuals={tec_result.residuals_ms:.3f}ms"
                                )
                        elif tec_result.confidence > 0.9:
                            # TEC fit is good but value is unrealistic (e.g., 0.0 TECU)
                            logger.warning(f"TEC unrealistic for {station}: {tec_result.tec_u:.1f} TECU (R2={tec_result.confidence:.2f}) - not applying correction")
                            for m in station_meas:
                                m.propagation_mode = 'TEC_UNREALISTIC'
                        else:
                            # TEC fit is poor - reduce confidence slightly
                            logger.warning(f"TEC poor fit for {station}: R2={tec_result.confidence:.2f} (Needs >0.9)")
                            for m in station_meas:
                                m.confidence = max(0.5, m.confidence * 0.95)
                                m.propagation_mode = 'TEC_POOR_FIT'
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
        # APPLY CALIBRATION: Remove constant hardware delays only (2026-02-06)
        # ====================================================================
        # Hardware calibration removes CONSTANT systematic offsets:
        # - Matched filter group delay (~0.4ms for 800ms template)
        # - ADC/buffer alignment latency
        # - Detection threshold bias
        #
        # It does NOT zero out the mean D_clock. The residual after hardware
        # correction is the science product: real clock offset + ionospheric variation.
        
        # Extract raw D_clock values for cross-validation (before calibration)
        raw_d_clocks = [m.d_clock_ms for m in measurements]
        
        # Apply calibration to get calibrated D_clock values for fusion
        with self._phase("calibration_apply"):
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
            measurements, calibrated_d_clocks
        )
        
        if not cross_valid:
            logger.warning(f"Cross-station validation failed: {cross_reason}")
            # Note: We don't reject the fusion, but flag it in the result
            # The consistency_flag will be set to reflect this issue
        
        # ====================================================================
        # CROSS-FREQUENCY VALIDATION (2026-02-16)
        # ====================================================================
        # Validate that raw D_clock is frequency-independent for each station.
        # If the physics model is correct, D_clock should be the same across
        # all frequencies (the 1/f² ionospheric term cancels out).
        # Systematic trends indicate propagation model errors.
        
        freq_valid, freq_reason, freq_deviations = self._validate_cross_frequency_d_clock(
            measurements
        )
        
        if not freq_valid:
            logger.warning(f"Cross-frequency validation failed: {freq_reason}")
            # Flag in consistency if physics model is suspect
            if cross_valid:  # Don't overwrite cross-station failure
                cross_reason = f"PHYSICS_MODEL_SUSPECT: {freq_reason}"
        else:
            logger.debug(f"Cross-frequency validation OK: {freq_reason}")
        
        # Weighted mean of hardware-calibrated D_clock values
        # After hardware correction, this represents real clock offset + iono residual
        w = np.array(weights)
        d_calibrated = np.array(calibrated_d_clocks)
        d_raw = np.array(raw_d_clocks)
        
        # Fuse calibrated measurements
        fused_d_clock_raw = np.sum(w * d_calibrated) / np.sum(w)
        
        # Also track raw fusion for diagnostics
        fused_d_clock_uncalibrated = np.sum(w * d_raw) / np.sum(w)
        
        # ====================================================================
        # KALMAN FILTER UPDATE (2026-02-06: was dead code, now connected)
        # ====================================================================
        # Feed the hardware-calibrated weighted mean into the Kalman filter.
        # The Kalman smooths ionospheric variations while tracking real offsets.
        #
        # DUAL KALMAN (2026-02-07): L1 and L2 feeds use independent Kalman states
        # so that TSL1 (geometric fallback) and TSL2 (physics model) carry
        # genuinely different estimates to chrony.
        use_l2_kalman = not force_l1_only
        measurement_uncertainty = float(np.sqrt(np.sum(w * (d_calibrated - fused_d_clock_raw)**2) / np.sum(w)))
        measurement_uncertainty = max(measurement_uncertainty, 1.0)  # Floor at 1ms
        
        # Leap second hold: skip Kalman update when CHU FSK detects TAI-UTC change.
        # A leap second causes a 1-second UTC jump that would look like a massive
        # Kalman innovation and corrupt the state. Coast on prediction instead.
        if getattr(self, '_fsk_leap_second_hold', False):
            logger.warning("Kalman HELD: leap second transition detected via CHU FSK TAI-UTC change")
            kalman_uncertainty = measurement_uncertainty
        else:
            kalman_uncertainty = self._kalman_update(fused_d_clock_raw, measurement_uncertainty, use_l2=use_l2_kalman)
        
        # Use the Kalman-filtered state as the fused output
        k_state_active = self.kalman_state_l2 if use_l2_kalman else self.kalman_state
        k_init_active = self.kalman_initialized_l2 if use_l2_kalman else self.kalman_initialized
        if k_init_active:
            fused_d_clock = float(k_state_active[0])
            
            # Log the filtering effect
            residual = fused_d_clock_raw - fused_d_clock
            logger.debug(f"Kalman: State={fused_d_clock:+.3f}ms, Raw={fused_d_clock_raw:+.3f}ms, Residual={residual:+.3f}ms")
            
            # Legacy ramp-up variable for logging compatibility (set to max)
            self.correction_alpha = 1.0
        else:
            # During initialization, we have to trust the measurement until the filter starts
            fused_d_clock = fused_d_clock_raw
        
        # ====================================================================
        # UPDATE HARDWARE CALIBRATION (2026-02-06)
        # ====================================================================
        # Hardware calibration learns ONLY constant receiver chain delays.
        # It does NOT zero out the mean D_clock (that was the circular bug).
        # 
        # Metrological separation of concerns:
        # - Hardware calibration: Constant delays (matched filter, ADC, detection bias)
        # - Kalman: Temporal smoothing of the science product (clock offset + iono)
        # - Fusion output: Real D_clock that can be validated against GPS
        self._update_calibration(
            measurements, 
            validated=cross_valid,
            reference_d_clock=0.0  # Target absolute zero (parameter kept for compatibility)
        )
        
        # Update long-term drift statistics (exploits the "long view")
        self._update_long_term_stats(measurements)
        self.log_long_term_drift_status()
        
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
        
        # 1. Statistical uncertainty - standard error of the weighted mean
        # CRITICAL FIX (2026-02-06): Use standard error (σ/√N_eff), not raw std.
        # The raw std measures the SCATTER of individual measurements (which includes
        # real ionospheric path differences between stations). The uncertainty of the
        # FUSED MEAN is much smaller — it decreases as 1/√N by the central limit theorem.
        # Using raw std overstates uncertainty by √N, preventing grade improvement.
        if len(calibrated_d_clocks) > 1:
            n_eff = len(calibrated_d_clocks)
            statistical_uncertainty = np.std(calibrated_d_clocks) / np.sqrt(n_eff)
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
        
        # ====================================================================
        # METROLOGICAL HOLDOVER MODEL (2026-01-16)
        # ====================================================================
        # The GPSDO is our "steel ruler" - it defines the time scale.
        # The offset estimate is ANCHORED to the GPSDO and remains valid.
        # What changes during signal dropout is our UNCERTAINTY, not the offset.
        #
        # Key metrological principles:
        # 1. More stations = better cross-validation = lower systematic uncertainty
        # 2. During dropout, uncertainty grows at GPSDO holdover rate
        # 3. The offset itself does NOT drift (it's anchored to GPSDO)
        # 4. When signals return, uncertainty decreases (not the offset)
        
        n_broadcasts_now = len(measurements)
        n_stations_now = len(set(m.station for m in measurements if m.station != 'GLOBAL_DIFF'))
        current_time = time.time()
        
        # Station count scaling for systematic uncertainty
        # More independent stations = better cross-validation = lower systematic bias
        station_scale = self.station_count_uncertainty_scale.get(
            min(n_stations_now, 4), 
            0.5  # 4+ stations
        )
        
        # Determine if this is a valid multi-station fusion
        # Key insight: station coverage and measurement quality are SEPARATE concerns
        # - Station coverage determines if we can cross-validate (reduces systematic error)
        # - Measurement quality determines the uncertainty of the update
        # With good station coverage, we ALWAYS update the Kalman, just with appropriate uncertainty
        is_valid_multi_station = (n_stations_now >= 2 and n_broadcasts_now >= 2)
        
        # Uncertainty threshold only gates updates during SINGLE-station mode
        # With multi-station coverage, we trust the cross-validation and update with measured uncertainty
        uncertainty_threshold = 10.0 if not self.kalman_converged else 20.0  # Relaxed for multi-station
        
        if is_valid_multi_station:
            # ============================================================
            # v6.0 ARCHITECTURE: Weighted Least Squares Fusion (No Temporal Smoothing)
            # ============================================================
            # The per-broadcast Kalmans have already smoothed the measurements.
            # Here we simply combine them using optimal linear weighting.
            # NO temporal smoothing at this layer - that would be unjustified.
            self.holdover_mode = False
            
            # The fused_d_clock_raw is already the weighted mean from earlier
            # The measurement_uncertainty is the combined uncertainty from ISO GUM
            # We use this directly without additional Kalman filtering
            
            # Apply station count scaling to final uncertainty
            # More stations = more confidence in cross-validation
            wls_uncertainty = measurement_uncertainty * station_scale
            
            # Record this valid fusion for holdover calculations
            self.last_valid_fusion_time = current_time
            self.last_valid_fusion_uncertainty = wls_uncertainty
            self.last_valid_n_stations = n_stations_now
            
            # Track convergence based on measurement quality (not Kalman state)
            # We consider converged when we have good multi-station coverage
            # 2 stations is the normal operating condition (WWV + WWVH)
            if not self.kalman_converged and n_stations_now >= 2 and wls_uncertainty < 3.0:
                self.kalman_converged = True
                logger.info(
                    f"WLS fusion CONVERGED: {n_stations_now} stations, "
                    f"uncertainty={wls_uncertainty:.3f}ms"
                )
            
            uncertainty = wls_uncertainty
            
            logger.debug(
                f"WLS fusion: {n_stations_now} stations, {n_broadcasts_now} broadcasts, "
                f"uncertainty={uncertainty:.3f}ms (scale={station_scale:.1f}x)"
            )
            
        else:
            # ============================================================
            # HOLDOVER MODE: Insufficient stations or poor measurement quality
            # ============================================================
            # The OFFSET remains valid (anchored to GPSDO).
            # The UNCERTAINTY grows at the GPSDO holdover drift rate.
            # This is the metrologically correct approach.
            
            if not self.holdover_mode and self.last_valid_fusion_time > 0:
                logger.info(
                    f"Entering HOLDOVER mode: {n_stations_now} station(s), {n_broadcasts_now} broadcast(s). "
                    f"Offset remains at {self.kalman_state[0]:+.3f}ms, uncertainty will grow."
                )
            self.holdover_mode = True
            
            # Calculate time since last valid multi-station fusion
            if self.last_valid_fusion_time > 0:
                holdover_duration_min = (current_time - self.last_valid_fusion_time) / 60.0
            else:
                # No valid fusion yet - use bootstrap uncertainty
                holdover_duration_min = 0.0
            
            # Uncertainty grows as sqrt(σ²_last + (drift_rate × Δt)²)
            # This is the proper uncertainty propagation for a drifting reference
            drift_uncertainty = self.gpsdo_holdover_drift_rate * holdover_duration_min
            
            base_uncertainty = self.last_valid_fusion_uncertainty if self.last_valid_fusion_time > 0 else 1.0
            holdover_uncertainty = np.sqrt(base_uncertainty**2 + drift_uncertainty**2)
            
            # Apply station count scaling (single station = higher systematic uncertainty)
            # Even in holdover, if we have measurements, they provide some validation
            if n_stations_now >= 1 and n_broadcasts_now >= 1:
                # We have some measurements - use them to bound uncertainty growth
                # but don't update the Kalman state (offset remains anchored)
                holdover_uncertainty = min(holdover_uncertainty, measurement_uncertainty * station_scale)
            
            # Cap holdover uncertainty at reasonable maximum (10ms = ~10 hours of holdover)
            holdover_uncertainty = min(holdover_uncertainty, 10.0)
            
            # DO NOT update Kalman state - offset remains anchored to last valid fusion
            # Only the uncertainty changes
            kalman_uncertainty = holdover_uncertainty
            uncertainty = holdover_uncertainty
            
            # Determine reason for holdover
            if n_stations_now < 2:
                reason = f"single-station ({n_stations_now})"
            elif n_broadcasts_now < 2:
                reason = f"insufficient broadcasts ({n_broadcasts_now})"
            else:
                reason = f"poor measurement quality ({measurement_uncertainty:.2f}ms)"
            
            logger.warning(
                f"HOLDOVER: {reason}. Offset={self.kalman_state[0]:+.3f}ms (stable), "
                f"uncertainty={uncertainty:.3f}ms (growing at {self.gpsdo_holdover_drift_rate:.4f}ms/min), "
                f"holdover_duration={holdover_duration_min:.1f}min"
            )
        
        # Per-station breakdown using CALIBRATED values
        # CRITICAL FIX (2026-02-06): Use calibrated_d_clocks, not raw_d_clocks.
        # The inter-station spread and per-station means must reflect the hardware-
        # corrected values. Using raw values inflates the spread by the full hardware
        # offset difference between stations (~20ms), making the consistency check
        # always fail and the grade always D.
        wwv_cal = [d for m, d in zip(measurements, calibrated_d_clocks) if m.station == 'WWV']
        wwvh_cal = [d for m, d in zip(measurements, calibrated_d_clocks) if m.station == 'WWVH']
        chu_cal = [d for m, d in zip(measurements, calibrated_d_clocks) if m.station == 'CHU']
        bpm_cal = [d for m, d in zip(measurements, calibrated_d_clocks) if m.station == 'BPM']
        
        # Per-station means for reporting (also calibrated)
        wwv_m = wwv_cal
        wwvh_m = wwvh_cal
        chu_m = chu_cal
        bpm_m = bpm_cal
        

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
        # BPM is EXCLUDED from inter-station spread (weight=0 in fusion)
        # but still tracked in station_means_all for reporting
        station_means = {}
        station_means_all = {}  # Including BPM, for reporting only
        if wwv_cal:
            station_means['WWV'] = np.mean(wwv_cal)
            station_means_all['WWV'] = station_means['WWV']
        if wwvh_cal:
            station_means['WWVH'] = np.mean(wwvh_cal)
            station_means_all['WWVH'] = station_means['WWVH']
        if chu_cal:
            station_means['CHU'] = np.mean(chu_cal)
            station_means_all['CHU'] = station_means['CHU']
        if bpm_cal:
            station_means_all['BPM'] = np.mean(bpm_cal)  # Report only, not in spread
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
        
        # ====================================================================
        # v6.0 ARCHITECTURE: WLS Fusion (No L3 Kalman)
        # ====================================================================
        # Per-broadcast Kalmans have already smoothed measurements.
        # We use the measurement uncertainty directly without additional filtering.
        # This preserves ionospheric science signal and avoids false smoothing.
        uncertainty = measurement_uncertainty
        
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
        
        # Level-aware quality grade based on timing authority level
        # Thresholds are (uncertainty_ms, inter_station_ms) per level
        thresholds = self.GRADE_THRESHOLDS[self.timing_authority_level]
        unc_a, inter_a = thresholds['A']
        unc_b, inter_b = thresholds['B']
        unc_c, inter_c = thresholds['C']
        
        # Inter-station check uses the spread we computed (BPM excluded)
        inter_ok_a = (inter_station_spread is not None and inter_station_spread < inter_a) or inter_station_spread is None
        inter_ok_b = (inter_station_spread is not None and inter_station_spread < inter_b) or inter_station_spread is None
        
        if len(measurements) >= 8 and uncertainty < unc_a and inter_ok_a and not single_station_mode:
            grade = 'A'
        elif len(measurements) >= 5 and uncertainty < unc_b and inter_ok_b:
            grade = 'B'
        elif len(measurements) >= 3 and uncertainty < unc_c:
            grade = 'C'
        else:
            grade = 'D'
        
        # ====================================================================
        # PROPAGATION MODE TRACKING (v6.2)
        # ====================================================================
        # Collect propagation modes from all measurements
        prop_modes = [m.propagation_mode for m in measurements if hasattr(m, 'propagation_mode') and m.propagation_mode]
        unique_modes = sorted(set(prop_modes)) if prop_modes else []
        propagation_modes_used = ','.join(unique_modes) if unique_modes else None
        
        # Find dominant mode (most common)
        if prop_modes:
            from collections import Counter
            mode_counts = Counter(prop_modes)
            dominant_propagation_mode = mode_counts.most_common(1)[0][0]
        else:
            dominant_propagation_mode = None
        
        # Compute Kalman state string
        _kalman_state_str = 'LOCKED' if self.kalman_converged else ('ACQUIRING' if self.kalman_n_updates >= 10 else 'REACQUIRING')
        
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
            single_station_mode=single_station_mode,
            # Propagation mode tracking (v6.2)
            propagation_modes_used=propagation_modes_used,
            dominant_propagation_mode=dominant_propagation_mode,
            # Kalman filter state (2026-02-18: fix convergence threshold 3→2 stations)
            kalman_state=_kalman_state_str
        )
        
        # Track measurement for Allan deviation calculation
        self.adev_tracker.add_measurement(result.timestamp, result.d_clock_fused_ms)
        
        # Write to HDF5 (skip if caller will write after populating L1/L2 fields)
        if not skip_write:
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
            
            # Use the kalman_state from the FusedResult (set by fuse() from self.kalman_converged)
            kalman_state = result.kalman_state
            
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
                processing_version='6.2.0',
                single_station_mode=bool(result.single_station_mode),
                
                # Metrological tracking fields (v6.2)
                d_clock_l1_ms=float(result.d_clock_l1_ms) if result.d_clock_l1_ms is not None else None,
                d_clock_l2_ms=float(result.d_clock_l2_ms) if result.d_clock_l2_ms is not None else None,
                l1_l2_difference_ms=float(result.l1_l2_difference_ms) if result.l1_l2_difference_ms is not None else None,
                calibration_age_hours=float(result.calibration_age_hours) if result.calibration_age_hours is not None else None,
                calibration_n_samples=int(result.calibration_n_samples) if result.calibration_n_samples is not None else None,
                calibration_converged=bool(result.calibration_converged) if result.calibration_converged is not None else None,
                multipath_detected_count=int(result.multipath_detected_count) if result.multipath_detected_count else None,
                multipath_mean_delay_spread_ms=float(result.multipath_mean_delay_spread_ms) if result.multipath_mean_delay_spread_ms is not None else None,
                doppler_mean_hz=float(result.doppler_mean_hz) if result.doppler_mean_hz is not None else None,
                doppler_correction_applied_ms=float(result.doppler_correction_applied_ms) if result.doppler_correction_applied_ms is not None else None,
                cramer_rao_mean_ms=float(result.cramer_rao_mean_ms) if result.cramer_rao_mean_ms is not None else None,
                propagation_modes_used=result.propagation_modes_used,
                dominant_propagation_mode=result.dominant_propagation_mode,
                adev_60s=float(result.adev_60s) if result.adev_60s is not None else None,
                adev_1000s=float(result.adev_1000s) if result.adev_1000s is not None else None
            )
            
            # Write to HDF5 with schema validation
            self.hdf5_fusion_writer.write_measurement(l3_measurement.model_dump())
            
        except Exception as e:
            logger.error(f"Failed to write HDF5 fusion result: {e}", exc_info=True)

    
    def _read_gnss_vtec(self) -> Optional[Tuple[float, float]]:
        """
        Read the latest GNSS VTEC from HDF5.
        Returns (vtec_tecu, timestamp) or None.
        
        PERFORMANCE: Reads only the last N rows from the HDF5 file instead of
        loading the entire dataset through the generic reader. The VTEC file
        grows ~560 MB/day at 1 Hz; a full table scan every 8s fusion cycle
        causes intermittent timeouts and VTEC data starvation.
        """
        logger.info(">>> _read_gnss_vtec() called <<<")
        
        if not HDF5_AVAILABLE:
            logger.warning("HDF5 not available for GNSS VTEC reads")
            return None
        
        try:
            from datetime import datetime, timezone
            import h5py as _h5py
            
            # Check both possible locations for GNSS VTEC data
            # Primary: data_root/data/gnss_vtec (where live_vtec.py writes with relative path)
            # Fallback: data_root/gnss_vtec (legacy location)
            vtec_dir = self.data_root / 'data' / 'gnss_vtec'
            if not vtec_dir.exists():
                vtec_dir = self.data_root / 'gnss_vtec'
            
            if not vtec_dir.exists():
                logger.warning(f"GNSS VTEC directory not found: {vtec_dir}")
                return None
            
            # Find today's file (most recent)
            now = datetime.now(timezone.utc)
            date_str = now.strftime('%Y%m%d')
            hdf5_path = vtec_dir / f'GNSS_gnss_vtec_{date_str}.h5'
            
            if not hdf5_path.exists():
                logger.debug(f"Today's VTEC file not found: {hdf5_path}")
                return None
            
            # Fast tail read: only read the last 10 rows instead of entire file
            TAIL_SIZE = 10
            MAX_AGE_SECONDS = 300  # 5 minutes
            
            with _h5py.File(hdf5_path, 'r', libver='latest', swmr=True) as f:
                if 'unix_timestamp' not in f or 'vtec_tecu' not in f:
                    logger.warning(f"Missing required datasets in {hdf5_path}")
                    return None
                
                n_total = len(f['unix_timestamp'])
                if n_total == 0:
                    logger.debug("VTEC file is empty")
                    return None
                
                # Read only the tail
                start_idx = max(0, n_total - TAIL_SIZE)
                timestamps = f['unix_timestamp'][start_idx:]
                vtec_values = f['vtec_tecu'][start_idx:]
                
                # Optional: read quality flags if present
                quality_flags = None
                if 'quality_flag' in f:
                    quality_flags = f['quality_flag'][start_idx:]
                
                n_sats = None
                if 'n_satellites' in f:
                    n_sats = f['n_satellites'][start_idx:]
            
            # Find the most recent GOOD/MARGINAL measurement within age limit
            current_time = time.time()
            best_idx = None
            best_ts = 0
            
            for i in range(len(timestamps) - 1, -1, -1):
                ts = float(timestamps[i])
                age = current_time - ts
                
                if age > MAX_AGE_SECONDS:
                    break  # Timestamps are ordered, older ones follow
                
                # Check quality if available
                if quality_flags is not None:
                    flag = quality_flags[i]
                    if isinstance(flag, bytes):
                        flag = flag.decode('utf-8')
                    if flag not in ('GOOD', 'MARGINAL'):
                        continue
                
                if ts > best_ts:
                    best_idx = i
                    best_ts = ts
                    break  # Most recent valid entry found
            
            if best_idx is not None:
                vtec = float(vtec_values[best_idx])
                sats = int(n_sats[best_idx]) if n_sats is not None else 0
                qflag = ''
                if quality_flags is not None:
                    qflag = quality_flags[best_idx]
                    if isinstance(qflag, bytes):
                        qflag = qflag.decode('utf-8')
                
                age = current_time - best_ts
                logger.info(
                    f"Read VTEC from HDF5: {vtec:.2f} TECU, "
                    f"{sats} sats, quality={qflag} (age: {age:.1f}s, "
                    f"tail read {min(TAIL_SIZE, n_total)} of {n_total} rows)"
                )
                return vtec, best_ts
            else:
                logger.debug(f"No fresh VTEC in last {MAX_AGE_SECONDS}s (file has {n_total} rows)")
                return None
        
        except FileNotFoundError:
            logger.warning("HDF5 VTEC file not found")
            return None
        except Exception as e:
            logger.error(f"HDF5 VTEC read failed: {e}")
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


def run_fusion_service(
    data_root: Path, 
    interval_sec: float = 60.0, 
    enable_chrony: bool = True,
    lookback_minutes: int = 30,
    receiver_lat: Optional[float] = None,
    receiver_lon: Optional[float] = None,
    timing_authority_level: str = 'L5',
    calib_file: Optional[str] = None
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
        timing_authority_level: Hardware timing level (L1-L6)
        calib_file: Path to JSON calibration file for wsprdaemon integration.
            When set, an atomic JSON file is written after each fusion cycle
            containing offset_ms, uncertainty_ms, convergence_state, and
            quality diagnostics.  Consumed by wd-ka9q-record to align wav
            start times.
    """
    # Determine timing authority from config (same logic as bootstrap gate below)
    _is_rtp_authority = True
    try:
        import toml as _toml_fs
        _cfg_path = Path('/etc/hf-timestd/timestd-config.toml')
        if _cfg_path.exists():
            _cfg_fs = _toml_fs.load(_cfg_path)
            _auth = _cfg_fs.get('timing', {}).get('authority', 'rtp')
            _is_rtp_authority = (_auth == 'rtp')
            logger.info(f"[FUSION] Timing authority: {_auth} → is_rtp_authority={_is_rtp_authority}")
    except Exception as _e:
        logger.warning(f"[FUSION] Could not read config for authority: {_e}")

    fusion = MultiBroadcastFusion(
        data_root,
        receiver_lat=receiver_lat,
        receiver_lon=receiver_lon,
        timing_authority_level=timing_authority_level,
        is_rtp_authority=_is_rtp_authority
    )
    
    # Initialize dual Chrony SHM outputs if enabled
    chrony_shm_l1 = None  # SHM 0: timestd.L1 (raw L1 metrology fusion)
    chrony_shm_l2 = None  # SHM 1: timestd.L2 (calibrated L2 timing fusion)
    _chrony_shm_available = False  # True if sysv_ipc is importable

    if enable_chrony:
        try:
            from hf_timestd.core.chrony_shm import ChronySHM
            _chrony_shm_available = True

            # Initialize L1 feed (SHM unit 0)
            chrony_shm_l1 = ChronySHM(unit=0)
            if chrony_shm_l1.connect():
                logger.info("Chrony SHM L1 feed enabled (unit=0, refid=TSL1)")
            else:
                logger.warning(
                    "Failed to connect to Chrony SHM unit 0 - L1 feed disabled "
                    "(will retry periodically)"
                )
                chrony_shm_l1.connected = False  # Keep object for reconnect

            # Initialize L2 feed (SHM unit 1)
            chrony_shm_l2 = ChronySHM(unit=1)
            if chrony_shm_l2.connect():
                logger.info("Chrony SHM L2 feed enabled (unit=1, refid=TSL2)")
            else:
                logger.warning(
                    "Failed to connect to Chrony SHM unit 1 - L2 feed disabled "
                    "(will retry periodically)"
                )
                chrony_shm_l2.connected = False  # Keep object for reconnect

        except Exception as e:
            logger.warning(f"Chrony SHM not available: {e}")
            chrony_shm_l1 = None
            chrony_shm_l2 = None
    
    # Initialize chrony stats collector (runs alongside fusion, logs source comparison)
    chrony_stats_collector = None
    if enable_chrony:
        try:
            from hf_timestd.core.chrony_stats import ChronyStatsCollector
            chrony_stats_collector = ChronyStatsCollector(
                interval_sec=60.0,  # Collect once per minute
                data_root=data_root,
            )
            logger.info("Chrony stats collector initialized (60s interval)")
        except Exception as e:
            logger.warning(f"Chrony stats collector not available: {e}")
    
    logger.info("Starting Multi-Broadcast Fusion Service")
    logger.info(f"  Interval: {interval_sec} seconds")
    logger.info(f"  Output: {fusion.fusion_dir / 'fusion_fusion_timing_YYYYMMDD.h5'}")
    logger.info(f"  Chrony SHM L1: {'enabled' if chrony_shm_l1 and chrony_shm_l1.connected else 'disabled (will retry)' if chrony_shm_l1 else 'disabled'}")
    logger.info(f"  Chrony SHM L2: {'enabled' if chrony_shm_l2 and chrony_shm_l2.connected else 'disabled (will retry)' if chrony_shm_l2 else 'disabled'}")
    # Initialize calibration file writer (wsprdaemon integration)
    calib_writer = None
    if calib_file:
        try:
            from hf_timestd.io.calibration_file import CalibrationFileWriter
            calib_writer = CalibrationFileWriter(calib_file)
            logger.info(f"Calibration file writer enabled: {calib_file}")
        except Exception as e:
            logger.error(f"Failed to initialize calibration file writer: {e}")
    
    logger.info(f"  Chrony stats: {'enabled' if chrony_stats_collector else 'disabled'}")
    logger.info(f"  Calibration file: {calib_file or 'disabled'}")

    # Fusion status writer for authority manager (METROLOGY.md §4.5).
    # Publishes /run/hf-timestd/fusion_status.json atomically every cycle so
    # the authority manager can probe T3 availability without reading HDF5.
    fusion_status_writer = None
    try:
        from hf_timestd.core.fusion_status_writer import FusionStatusWriter
        fusion_status_writer = FusionStatusWriter(
            path=Path('/run/hf-timestd/fusion_status.json'),
            cycle_interval_sec=interval_sec,
        )
        logger.info("Fusion status writer enabled: /run/hf-timestd/fusion_status.json")
    except Exception as e:
        logger.warning(f"Fusion status writer not available: {e}")

    # Authority manager — METROLOGY.md §4.5 / §4.6. Publishes
    # /run/hf-timestd/authority.json on a 30s cadence from its own thread.
    # Per the single-writer / coupling rule, this runs INSIDE the fusion
    # service so a fusion hang decays authority.json, chrony SHM reach,
    # and mDNS advertisement together.
    authority_runner = None
    try:
        from hf_timestd.core.authority_runner import build_authority_runner_from_config
        # Re-read config for the authority block (config was loaded earlier
        # for station coords and timing_authority; the authority.* subtree
        # may only be present in fresh configs, so handle absence gracefully).
        _auth_config: dict = {}
        try:
            import toml as _toml_auth
            _auth_cfg_path = Path('/etc/hf-timestd/timestd-config.toml')
            if _auth_cfg_path.exists():
                _auth_config = _toml_auth.load(_auth_cfg_path)
        except Exception as _e:
            logger.warning(f"Could not re-read config for authority manager: {_e}")
        authority_runner = build_authority_runner_from_config(config=_auth_config)
        authority_runner.start()
        probe_levels = [p.t_level for p in authority_runner.manager.probes]
        logger.info(
            "Authority manager started: probes=%s interval=%.1fs hysteresis=%d",
            probe_levels, authority_runner.interval_sec,
            authority_runner.manager.upgrade_hysteresis,
        )
    except Exception as e:
        logger.warning(f"Authority manager not available: {e}")

    # Fusion loop metrics (fusion audit measurement phase). Enabled by
    # default; gate via [timing.fusion_metrics] enabled=false if we ever
    # want to turn it off. Watchdog budget is hardcoded to 120.0 s to
    # match systemd/timestd-fusion.service WatchdogSec; adjust both
    # together if the unit file changes.
    loop_metrics = None
    try:
        _fm_cfg = (_auth_config.get('timing', {}) or {}).get('fusion_metrics', {}) or {}
        _fm_enabled = bool(_fm_cfg.get('enabled', True))
        if _fm_enabled:
            from hf_timestd.core.fusion_loop_metrics import FusionLoopMetrics
            _fm_path = _fm_cfg.get('path')
            _fm_kwargs = {'watchdog_sec': 120.0}
            if _fm_path:
                _fm_kwargs['path'] = Path(_fm_path)
            loop_metrics = FusionLoopMetrics(**_fm_kwargs)
            fusion.loop_metrics = loop_metrics
            logger.info(
                "Fusion loop metrics enabled: %s (watchdog_budget=%.0fs)",
                loop_metrics.path, loop_metrics.watchdog_sec,
            )
    except Exception as e:
        logger.warning(f"Fusion loop metrics not available: {e}")

    logger.info("Starting Multi-Broadcast Fusion Dashboard Service...")
    logger.info(f"Fusion interval: {interval_sec}s, lookback: {lookback_minutes}m")
    logger.info(f"Timing authority level: {fusion.timing_authority_level}")
    thresholds = fusion.GRADE_THRESHOLDS[fusion.timing_authority_level]
    logger.info(f"Grade thresholds: A=<{thresholds['A'][0]}ms, B=<{thresholds['B'][0]}ms, C=<{thresholds['C'][0]}ms")
    
    # CRITICAL FIX (2026-01-20): Handle SIGTERM for clean shutdown with calibration save
    running = True
    def handle_shutdown(signum, frame):
        nonlocal running
        logger.info(f"Received signal {signum}, initiating clean shutdown...")
        running = False
    
    import signal
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)
    
    # Notify systemd we're ready
    if SYSTEMD_AVAILABLE:
        systemd_daemon.notify('READY=1')
        logger.info("Notified systemd: READY")
    
    # ================================================================
    # BOOTSTRAP LOCK GATE (v6.2) - Wait for bootstrap before fusion
    # ================================================================
    # Fusion should not run until bootstrap has established the RTP-to-UTC
    # correspondence. Without this, D_clock calculations are meaningless.
    #
    # In RTP authority mode (GPS+PPS), timing is already authoritative
    # so bootstrap lock is not needed - skip the gate entirely.
    #
    # In fusion authority mode, MetrologyEngine writes bootstrap_state.json
    # when FusionTimingState achieves PROVISIONAL or REFINED lock.
    _skip_bootstrap_gate = True  # Default: skip gate (RTP authority assumed)
    try:
        import toml as _toml
        _config_path = Path('/etc/hf-timestd/timestd-config.toml')
        if _config_path.exists():
            with open(_config_path, 'r') as _f:
                _cfg = _toml.load(_f)
            _authority = _cfg.get('timing', {}).get('authority', 'rtp')
            if _authority == 'rtp':
                logger.info("[BOOTSTRAP] RTP authority mode (GPS+PPS) - skipping bootstrap gate")
            else:
                logger.info(f"[BOOTSTRAP] Authority mode '{_authority}' - bootstrap gate enabled")
                _skip_bootstrap_gate = False
        else:
            logger.info("[BOOTSTRAP] No config file found - defaulting to RTP authority (skip gate)")
    except Exception as e:
        logger.warning(f"[BOOTSTRAP] Could not read config to check authority: {e} - defaulting to skip gate")
    
    if not _skip_bootstrap_gate:
        try:
            from .bootstrap_state import BootstrapStateWatcher
            bootstrap_watcher = BootstrapStateWatcher()
            
            # Check if already locked (e.g., service restart after lock)
            if not bootstrap_watcher.is_locked():
                logger.info("[BOOTSTRAP] Waiting for bootstrap lock before starting fusion...")
                # Wait indefinitely for bootstrap lock (with watchdog keepalive)
                while running and not bootstrap_watcher.is_locked():
                    if SYSTEMD_AVAILABLE:
                        systemd_daemon.notify('WATCHDOG=1')
                    time.sleep(1.0)
                
                if not running:
                    logger.info("[BOOTSTRAP] Shutdown requested while waiting for lock")
                    return
            
            state = bootstrap_watcher.get_state()
            if state:
                logger.info(
                    f"[BOOTSTRAP] Lock confirmed: {state.lock_tier}, "
                    f"D_clock={state.d_clock_ms:+.1f}ms ± {state.uncertainty_ms:.1f}ms"
                )
            else:
                logger.info("[BOOTSTRAP] Lock detected (state file exists)")
                
        except ImportError as e:
            logger.warning(f"[BOOTSTRAP] State watcher not available: {e}. Proceeding without gate.")
        except Exception as e:
            logger.warning(f"[BOOTSTRAP] Error checking lock state: {e}. Proceeding without gate.")
    
    # Data freshness alerting - track consecutive cycles with no measurements
    consecutive_empty_cycles = 0
    EMPTY_CYCLE_WARNING_THRESHOLD = 5  # Warn after 5 consecutive empty cycles
    EMPTY_CYCLE_ERROR_THRESHOLD = 15   # Error after 15 consecutive empty cycles (~2 min at 8s interval)
    last_successful_fusion_time = time.time()
    
    # Track chrony updates for discontinuity filtering
    last_chrony_d_clock = None
    last_chrony_update_time = None

    # SHM reconnection state — retry every 30s if initial connect failed
    _shm_reconnect_interval = 30.0
    _shm_last_reconnect_attempt = 0.0

    while running:
        try:
            # BREADCRUMB: Loop start
            loop_start_time = time.time()
            logger.debug(f"--- FUSION LOOP START (t={loop_start_time:.3f}) ---")

            # Reset per-cycle phase accumulator (fusion audit measurement
            # phase). No-op when loop_metrics is None.
            if loop_metrics is not None:
                loop_metrics.start_cycle()

            # Per-cycle authority/status bookkeeping (consumed by
            # FusionStatusWriter before the watchdog notify at end of loop).
            chrony_fed_this_cycle = False
            chrony_skip_reasons: List[str] = []

            # Notify watchdog we are alive
            if SYSTEMD_AVAILABLE:
                systemd_daemon.notify('WATCHDOG=1')

            # Periodic SHM reconnection: if either feed is disconnected,
            # retry connect every 30s.  This recovers from the race where
            # chrony recreates SHM as root:0600 between ExecStartPre and
            # fusion's connect().
            if enable_chrony and _chrony_shm_available:
                _need_reconnect = (
                    (chrony_shm_l1 and not chrony_shm_l1.connected) or
                    (chrony_shm_l2 and not chrony_shm_l2.connected)
                )
                if _need_reconnect and (loop_start_time - _shm_last_reconnect_attempt) >= _shm_reconnect_interval:
                    with (loop_metrics.phase("shm_reconnect") if loop_metrics else nullcontext()):
                        _shm_last_reconnect_attempt = loop_start_time
                        if chrony_shm_l1 and not chrony_shm_l1.connected:
                            logger.info("Attempting Chrony SHM L1 reconnect...")
                            if chrony_shm_l1.connect():
                                logger.info("Chrony SHM L1 reconnected (unit=0, refid=TSL1)")
                                if loop_metrics:
                                    loop_metrics.mark_event("shm_reconnect_l1")
                        if chrony_shm_l2 and not chrony_shm_l2.connected:
                            logger.info("Attempting Chrony SHM L2 reconnect...")
                            if chrony_shm_l2.connect():
                                logger.info("Chrony SHM L2 reconnected (unit=1, refid=TSL2)")
                                if loop_metrics:
                                    loop_metrics.mark_event("shm_reconnect_l2")

            # BREADCRUMB: Calling fuse
            logger.debug("Calling fusion.fuse()...")

            # DUAL FEED ARCHITECTURE: Run fusion twice for L1 and L2 feeds
            # L1 feed: Force L1-only mode (raw metrology, no L2 calibration)
            # L2 feed: Use L2 calibrated data when available
            result_l1 = None
            result_l2 = None

            try:
                # L1-only fusion: Force use of raw L1 metrology only
                # skip_write=True because we write once after L1/L2 fields are populated
                with (loop_metrics.phase("fuse_l1") if loop_metrics else nullcontext()):
                    result_l1 = fusion.fuse(lookback_minutes=lookback_minutes, force_l1_only=True, skip_write=True)
            except Exception as e_fuse:
                logger.error(f"L1 fusion calculation CRASHED: {e_fuse}", exc_info=True)

            # Pet watchdog between heavy fuse() calls to avoid 120s timeout
            if SYSTEMD_AVAILABLE:
                systemd_daemon.notify('WATCHDOG=1')

            try:
                # L2 fusion: Use L2 calibrated data (current behavior)
                # skip_write=True because we write once after L1/L2 fields are populated
                with (loop_metrics.phase("fuse_l2") if loop_metrics else nullcontext()):
                    result_l2 = fusion.fuse(lookback_minutes=lookback_minutes, force_l1_only=False, skip_write=True)
            except Exception as e_fuse:
                logger.error(f"L2 fusion calculation CRASHED: {e_fuse}", exc_info=True)
            
            # BREADCRUMB: Fusion returned (INFO level for visibility)
            logger.info(f"Dual fusion: L1={result_l1 is not None}, L2={result_l2 is not None}")
            
            # Use L2 result for logging (primary feed)
            result = result_l2 if result_l2 else result_l1
            
            # ================================================================
            # DATA FRESHNESS ALERTING (2026-02-06)
            # ================================================================
            # Track consecutive cycles with no measurements to detect data flow issues
            if result is None or (result.n_broadcasts == 0):
                consecutive_empty_cycles += 1
                stale_duration = time.time() - last_successful_fusion_time
                
                if consecutive_empty_cycles == EMPTY_CYCLE_WARNING_THRESHOLD:
                    logger.warning(
                        f"[DATA FRESHNESS] No measurements for {consecutive_empty_cycles} consecutive cycles "
                        f"({stale_duration:.0f}s). Check metrology service and L1/L2 HDF5 files."
                    )
                elif consecutive_empty_cycles == EMPTY_CYCLE_ERROR_THRESHOLD:
                    logger.error(
                        f"[DATA FRESHNESS] CRITICAL: No measurements for {consecutive_empty_cycles} consecutive cycles "
                        f"({stale_duration:.0f}s). Fusion is starved - upstream data flow may be broken!"
                    )
                elif consecutive_empty_cycles > EMPTY_CYCLE_ERROR_THRESHOLD and consecutive_empty_cycles % 30 == 0:
                    # Periodic reminder every ~4 minutes
                    logger.error(
                        f"[DATA FRESHNESS] Still no measurements after {consecutive_empty_cycles} cycles "
                        f"({stale_duration/60:.1f} minutes). Manual intervention required."
                    )
            else:
                if consecutive_empty_cycles >= EMPTY_CYCLE_WARNING_THRESHOLD:
                    logger.info(
                        f"[DATA FRESHNESS] Data flow restored after {consecutive_empty_cycles} empty cycles"
                    )
                consecutive_empty_cycles = 0
                last_successful_fusion_time = time.time()
            
            # ================================================================
            # METROLOGICAL TRACKING: Populate L1/L2 comparison fields (v6.2)
            # ================================================================
            # CRITICAL: Use d_clock_raw_ms (weighted mean before Kalman), NOT d_clock_fused_ms
            # 
            # d_clock_fused_ms = self.kalman_state[0] (same for both L1 and L2!)
            # d_clock_raw_ms = weighted mean of measurements (different for L1 vs L2)
            #
            # The L1-L2 difference reveals propagation correction quality:
            #   L1: D_clock = raw_toa - (light_time + 1.5ms)  [geometric fallback]
            #   L2: D_clock = raw_toa - propagation_delay     [full physics model]
            #   L1-L2 = ionospheric_delay + mode_correction - 1.5ms_fallback
            #
            # This is the metrologically meaningful comparison.
            if result:
                # Record L1 vs L2 comparison for propagation correction validation
                if result_l1 is not None:
                    result.d_clock_l1_ms = result_l1.d_clock_raw_ms
                if result_l2 is not None:
                    result.d_clock_l2_ms = result_l2.d_clock_raw_ms
                if result_l1 is not None and result_l2 is not None:
                    result.l1_l2_difference_ms = result_l1.d_clock_raw_ms - result_l2.d_clock_raw_ms
                    logger.info(
                        f"L1-L2 difference: {result.l1_l2_difference_ms:+.3f} ms "
                        f"(L1_raw={result_l1.d_clock_raw_ms:+.3f}, L2_raw={result_l2.d_clock_raw_ms:+.3f}) "
                        f"[propagation correction quality]"
                    )
                
                # Record calibration convergence metrics
                if hasattr(fusion, 'calibration_age_hours'):
                    result.calibration_age_hours = fusion.calibration_age_hours
                total_cal_samples = sum(
                    cal.n_samples for cal in fusion.calibration.values()
                ) if fusion.calibration else 0
                result.calibration_n_samples = total_cal_samples
                if hasattr(fusion, 'recent_validations') and len(fusion.recent_validations) >= 10:
                    result.calibration_converged = sum(fusion.recent_validations) / len(fusion.recent_validations) > 0.8
                else:
                    result.calibration_converged = False
                
                # Record Allan deviation from tracker
                adev_values = fusion.adev_tracker.compute_all_adev([60, 1000])
                result.adev_60s = adev_values.get('adev_60s')
                result.adev_1000s = adev_values.get('adev_1000s')
                
                # CRITICAL: Re-write HDF5 with L1/L2 fields now populated
                # The initial write in fuse() happened before L1/L2 were set
                fusion._write_fused_result(result)
            
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
                # DUAL FEED ARCHITECTURE: Write both L1 (raw) and L2 (calibrated) feeds
                # L1 feed: Uses raw L1 metrology fusion (fallback, fast)
                # L2 feed: Uses calibrated L2 timing fusion (primary, accurate)
                if (chrony_shm_l1 and chrony_shm_l1.connected) or (chrony_shm_l2 and chrony_shm_l2.connected):
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
                        # Operational: prefer A/B/C but accept D with reasonable uncertainty.
                        # Raised from 10ms to 25ms (2026-03-04): single-station mode
                        # structurally inflates uncertainty to 17-20ms even when D_clock
                        # is accurate to <2ms. Chrony has its own outlier rejection.
                        quality_ok = result.quality_grade in ('A', 'B', 'C') or \
                                    (result.quality_grade == 'D' and result.uncertainty_ms < 25.0)
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
                        # Operational: accept disagreement with reasonable uncertainty.
                        # Raised from 2.0ms to 5.0ms (2026-03-04): stale calibration +
                        # ionospheric variability routinely produces 3ms uncertainty with
                        # CROSS_STATION_DISAGREE, which is still far better than no feed.
                        # 5ms still rejects truly degraded data.
                        if result.consistency_flag in ('INTER_ANOMALY', 'CROSS_STATION_DISAGREE') and result.uncertainty_ms < 5.0:
                            consistent = True
                            logger.debug(
                                f"Chrony feed: Accepting {result.consistency_flag} with low uncertainty "
                                f"({result.uncertainty_ms:.3f}ms < 5.0ms threshold)"
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
                    discontinuity_ok = True
                    
                    # Reset discontinuity check if no update for >5 minutes (allows recovery)
                    if last_chrony_update_time is not None:
                        time_since_update = time.time() - last_chrony_update_time
                        if time_since_update > 300:  # 5 minutes
                            logger.info(
                                f"Chrony feed: Resetting discontinuity check after {time_since_update:.0f}s "
                                f"without updates (allows recovery from stuck state)"
                            )
                            last_chrony_d_clock = None
                    
                    if last_chrony_d_clock is not None:
                        delta = abs(result.d_clock_fused_ms - last_chrony_d_clock)
                        # Scale discontinuity threshold with measurement uncertainty.
                        # HF timing has 5-30ms uncertainty; cycle-to-cycle variation of
                        # 2-3σ is normal ionospheric behavior, not a discontinuity.
                        # Fixed 10ms threshold latches permanently when uncertainty > ~5ms
                        # because rejected updates never advance last_chrony_d_clock.
                        if fusion.kalman_converged:
                            discontinuity_threshold = max(10.0, 3.0 * result.uncertainty_ms)
                        else:
                            discontinuity_threshold = 100.0
                        if delta > discontinuity_threshold:
                            logger.warning(
                                f"Chrony feed: Discontinuity detected ({delta:.1f}ms jump > {discontinuity_threshold:.0f}ms), "
                                f"skipping update to prevent clock instability"
                            )
                            discontinuity_ok = False
                    # else: First measurement after restart, allow it
                    
                    # DIAGNOSTIC: Log gating status for debugging chrony feed issues
                    if not (quality_ok and multi_station and consistent and discontinuity_ok):
                        gate_reasons = []
                        if not quality_ok:
                            gate_reasons.append(f"quality(grade={result.quality_grade},unc={result.uncertainty_ms:.1f}ms)")
                        if not multi_station:
                            gate_reasons.append(f"multi_station(n={result.n_stations})")
                        if not consistent:
                            gate_reasons.append(f"consistent(flag={result.consistency_flag},unc={result.uncertainty_ms:.1f}ms,thresh=5.0ms)")
                        if not discontinuity_ok:
                            gate_reasons.append("discontinuity")
                        logger.info(
                            f"Chrony feed GATED: {' + '.join(gate_reasons)} "
                            f"[grade={result.quality_grade}, n_sta={result.n_stations}, "
                            f"flag={result.consistency_flag}, unc={result.uncertainty_ms:.1f}ms, "
                            f"cal_converged={calibration_converged}]"
                        )
                        # Expose the same reasons to the status writer.
                        chrony_skip_reasons = list(gate_reasons)
                    
                    if quality_ok and multi_station and consistent and discontinuity_ok:
                        now = time.time()
                        system_time = now

                        _shm_write_t0 = time.monotonic()
                        try:
                            # Update L1 feed (SHM 0) - raw L1 metrology fusion only
                            if chrony_shm_l1 and chrony_shm_l1.connected and result_l1:
                                reference_time_l1 = system_time - (result_l1.d_clock_fused_ms / 1000.0)
                                uncertainty_sec_l1 = max(0.1, result_l1.uncertainty_ms) / 1000.0
                                # Precision = log2(seconds), more negative = better
                                # Clamp to [-20, -4] range (1us to 62ms)
                                raw_precision_l1 = int(np.log2(uncertainty_sec_l1))
                                precision_l1 = max(-20, min(-4, raw_precision_l1))
                                
                                update_success_l1 = chrony_shm_l1.update(reference_time_l1, system_time, precision_l1)
                                if update_success_l1:
                                    chrony_fed_this_cycle = True
                                    logger.debug(
                                        f"Chrony SHM L1 (unit=0) updated: D_clock={result_l1.d_clock_fused_ms:+.3f}ms, "
                                        f"uncertainty={result_l1.uncertainty_ms:.1f}ms, precision={precision_l1} "
                                        f"[{result_l1.n_stations}sta, {result_l1.quality_grade}]"
                                    )
                                else:
                                    logger.warning("Chrony SHM L1 write failed")
                            
                            # Update L2 feed (SHM 1) - calibrated L2 timing fusion
                            if chrony_shm_l2 and chrony_shm_l2.connected and result_l2:
                                reference_time_l2 = system_time - (result_l2.d_clock_fused_ms / 1000.0)
                                uncertainty_sec_l2 = max(0.1, result_l2.uncertainty_ms) / 1000.0
                                # Precision = log2(seconds), more negative = better
                                # Clamp to [-20, -4] range (1us to 62ms)
                                raw_precision_l2 = int(np.log2(uncertainty_sec_l2))
                                precision_l2 = max(-20, min(-4, raw_precision_l2))
                                
                                update_success_l2 = chrony_shm_l2.update(reference_time_l2, system_time, precision_l2)
                                if update_success_l2:
                                    chrony_fed_this_cycle = True
                                    logger.debug(
                                        f"Chrony SHM L2 (unit=1) updated: D_clock={result_l2.d_clock_fused_ms:+.3f}ms, "
                                        f"uncertainty={result_l2.uncertainty_ms:.1f}ms, precision={precision_l2} "
                                        f"[{result_l2.n_stations}sta, {result_l2.quality_grade}]"
                                    )
                                else:
                                    logger.warning("Chrony SHM L2 write failed")
                            
                            # Update last value and timestamp for discontinuity check (use L2 as primary)
                            if result_l2:
                                last_chrony_d_clock = result_l2.d_clock_fused_ms
                                last_chrony_update_time = time.time()
                            elif result_l1:
                                last_chrony_d_clock = result_l1.d_clock_fused_ms
                                last_chrony_update_time = time.time()
                                
                        except Exception as e:
                            logger.error(f"Chrony SHM update exception: {e}")
                        if loop_metrics is not None:
                            loop_metrics.record_phase("shm_write", time.monotonic() - _shm_write_t0)
                    # Always advance the discontinuity reference to track the
                    # current signal.  Without this, a rejected update freezes
                    # last_chrony_d_clock at a stale value and every subsequent
                    # delta grows larger, permanently latching the filter.
                    if result_l2:
                        last_chrony_d_clock = result_l2.d_clock_fused_ms
                    elif result_l1:
                        last_chrony_d_clock = result_l1.d_clock_fused_ms
                    
                    if not (quality_ok and multi_station and consistent and discontinuity_ok):
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

            # Write calibration file (wsprdaemon integration)
            if calib_writer:
                _cal_t0 = time.monotonic()
                try:
                    calib_writer.update(result)
                except Exception as e:
                    logger.error(f"Calibration file write failed: {e}")
                if loop_metrics is not None:
                    loop_metrics.record_phase("calib_file_write", time.monotonic() - _cal_t0)

            # Publish fusion status for the authority manager (§4.5).
            # Always called, even when result is None, so utc_published stays
            # fresh and consumers can distinguish "service alive, no data"
            # from "service dead."
            if fusion_status_writer:
                _fsw_t0 = time.monotonic()
                try:
                    fusion_status_writer.update(
                        result=result,
                        chrony_fed=chrony_fed_this_cycle,
                        skip_reasons=chrony_skip_reasons,
                    )
                except Exception as e:
                    logger.warning(f"Fusion status write failed: {e}")
                if loop_metrics is not None:
                    loop_metrics.record_phase("fusion_status_write", time.monotonic() - _fsw_t0)

            # Pet watchdog after heavy chrony/fusion processing
            if SYSTEMD_AVAILABLE:
                systemd_daemon.notify('WATCHDOG=1')

            # Collect chrony source statistics (rate-limited to once per minute)
            if chrony_stats_collector:
                _cs_t0 = time.monotonic()
                try:
                    chrony_stats_collector.collect()
                except Exception as e:
                    logger.debug(f"Chrony stats collection error: {e}")
                if loop_metrics is not None:
                    loop_metrics.record_phase("chrony_stats", time.monotonic() - _cs_t0)

            # Emit per-cycle metrics (fusion audit measurement phase).
            # After all other per-cycle work, before sleep, so the
            # recorded loop_duration captures everything.
            if loop_metrics is not None:
                try:
                    loop_metrics.finalize_and_emit()
                except Exception as e:
                    logger.warning(f"Fusion loop metrics emit failed: {e}")

            # BREADCRUMB: Sleeping
            loop_duration = time.time() - loop_start_time
            logger.debug(f"Loop finished in {loop_duration:.3f}s. Sleeping {interval_sec}s...")

            time.sleep(interval_sec)
            
        except KeyboardInterrupt:
            logger.info("Fusion service stopped by keyboard interrupt")
            break
        except Exception as e:
            logger.error(f"Fusion error: {e}", exc_info=True)
            time.sleep(interval_sec)
    
    # CRITICAL FIX (2026-01-20): Save calibration on clean shutdown
    # This ensures the converged Kalman state is preserved for the next restart
    logger.info("Saving calibration before shutdown...")
    try:
        if fusion.kalman_converged:
            fusion._save_calibration()
            logger.info(f"Calibration saved: offset={fusion.kalman_state[0]:.3f}ms, converged=True")
        else:
            logger.warning(f"Kalman not converged (n_updates={fusion.kalman_n_updates}), skipping calibration save to preserve previous state")
    except Exception as e:
        logger.error(f"Failed to save calibration on shutdown: {e}")
    
    # Remove calibration file on shutdown so stale data is not consumed
    if calib_writer:
        calib_writer.remove()

    # Stop authority manager thread cleanly (publishes its final state
    # before exiting if it is in the middle of a tick).
    if authority_runner is not None:
        try:
            authority_runner.stop(timeout=5.0)
        except Exception as e:
            logger.warning(f"Authority manager stop failed: {e}")

    logger.info("Fusion service shutdown complete")


if __name__ == '__main__':
    import argparse
    
    parser = argparse.ArgumentParser(description='Multi-Broadcast D_clock Fusion')
    parser.add_argument('--data-root', type=Path, default=Path('data'), required=False) # Configured default for simpler running
    parser.add_argument('--config', type=Path, help='Configuration file') # Added config support
    parser.add_argument('--interval', type=float, default=60.0)
    parser.add_argument('--lookback', type=int, default=30, help='Lookback window in minutes')
    parser.add_argument('--timing-level', default='L5', choices=['L1', 'L2', 'L3', 'L4', 'L5', 'L6'],
                        help='Timing authority level (L1-L6, default: L5)')
    parser.add_argument('--log-level', default='INFO')
    parser.add_argument('--enable-chrony', action='store_true', default=True,
                        help='Enable Chrony SHM refclock output (default: enabled)')
    parser.add_argument('--disable-chrony', action='store_true',
                        help='Disable Chrony SHM refclock output')
    parser.add_argument('--calib-file', type=str, default=None,
                        help='Path to JSON calibration file (wsprdaemon integration)')
    
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
    
    # Read timing level from config if not overridden on CLI
    timing_level = args.timing_level
    if args.config and args.config.exists():
        try:
            cfg_level = config.get('fusion', {}).get('timing_authority_level')
            if cfg_level and args.timing_level == 'L5':  # Only use config if CLI is default
                timing_level = cfg_level
                logger.info(f"Using timing authority level from config: {timing_level}")
        except Exception as e:
            logger.debug(f"Ignored exception: {e}")
            pass
    
    enable_chrony = args.enable_chrony and not args.disable_chrony
    run_fusion_service(
        args.data_root, 
        args.interval, 
        enable_chrony=enable_chrony,
        lookback_minutes=args.lookback,
        receiver_lat=receiver_lat,
        receiver_lon=receiver_lon,
        timing_authority_level=timing_level,
        calib_file=args.calib_file
    )
