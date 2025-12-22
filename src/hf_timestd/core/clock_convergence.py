#!/usr/bin/env python3
"""
Clock Convergence Model - "Set, Monitor, Intervention" Architecture

================================================================================
PURPOSE
================================================================================
Implement a convergence-to-lock model for D_clock estimation that:
    1. Converges to a stable clock offset estimate
    2. Monitors for ionospheric propagation events (scientific data!)
    3. Re-acquires only when persistent anomalies indicate actual change

This is designed for GPSDO-disciplined receivers where the local clock is
a secondary standard with sub-ppm stability.

================================================================================
PHILOSOPHY: "SET, MONITOR, INTERVENTION"
================================================================================
Traditional approach: Constantly recalculate D_clock each minute
    Problem: Propagation variations appear as "noise" in the clock estimate

Our approach: Once clock is characterized, variations ARE the science
    1. SET: Converge to locked D_clock estimate (first 30 minutes)
    2. MONITOR: Track residuals as ionospheric propagation data
    3. INTERVENTION: Re-acquire only if physics violated

KEY INSIGHT:
    With a GPSDO (10⁻⁹ stability), the clock doesn't drift measurably in hours.
    Minute-to-minute D_clock variations are therefore NOT clock error—they
    are IONOSPHERIC PROPAGATION EFFECTS that we want to measure!

================================================================================
STATE MACHINE
================================================================================
    ┌─────────────────────────────────────────────────────────────────────────┐
    │                                                                         │
    │   ┌──────────────┐                                                      │
    │   │  ACQUIRING   │ ──────────────────────────┐                          │
    │   │   N < 10     │                           │                          │
    │   └──────┬───────┘                           │                          │
    │          │ N ≥ 10                            │                          │
    │          ▼                                   │                          │
    │   ┌──────────────┐                           │                          │
    │   │  CONVERGING  │ ◄─────────────────────────┘                          │
    │   │  σ/√N > th   │                                                      │
    │   └──────┬───────┘                                                      │
    │          │ σ/√N < th AND N ≥ 30                                         │
    │          ▼                                                              │
    │   ┌──────────────┐      5 consecutive       ┌──────────────┐            │
    │   │    LOCKED    │ ──── anomalies ─────────►│  REACQUIRE   │            │
    │   │  monitoring  │                          │   reset      │            │
    │   └──────────────┘                          └──────┬───────┘            │
    │          ▲                                         │                    │
    │          │                                         │                    │
    │          └─────────────────────────────────────────┘                    │
    │                         start over                                      │
    └─────────────────────────────────────────────────────────────────────────┘

STATE DESCRIPTIONS:
    ACQUIRING  - First 10 measurements, building initial statistics
    CONVERGING - Uncertainty shrinking toward lock threshold
    LOCKED     - High confidence, now treating variations as propagation data
    HOLDOVER   - (Future) Lock lost but using last good estimate
    REACQUIRE  - Persistent anomalies, resetting statistics

================================================================================
STATISTICAL MODEL - KALMAN FILTER (Issue 3.1 Fix - 2025-12-07)
================================================================================

VULNERABILITY ADDRESSED:
------------------------
The original implementation used Welford's algorithm, which assumes the underlying
value is STATIONARY (constant). But D_clock is affected by:
- Temperature-induced crystal oscillator drift (~1 ppm/°C)
- Ionospheric diurnal variation (systematic bias shift)

This caused the "locked" D_clock value to become stale as conditions changed.

SOLUTION: KALMAN FILTER with offset+drift state model
-----------------------------------------------------
The Kalman filter tracks both the clock offset AND its rate of change:

    STATE: x = [D_clock (ms), D_clock_rate (ms/min)]
    
    STATE TRANSITION (constant velocity model):
        x_k = F × x_{k-1} + process_noise
        F = [[1, Δt],
             [0,  1]]
        
    MEASUREMENT MODEL:
        z = H × x + measurement_noise
        H = [1, 0]  (we only measure D_clock, not rate directly)

PROCESS NOISE (Q matrix):
    Based on oscillator stability specification:
    - GPSDO: ~10⁻¹¹ Allan deviation → very small process noise
    - TCXO:  ~10⁻⁶ Allan deviation → moderate process noise
    
    Q = [[σ_offset², σ_offset×σ_rate],
         [σ_offset×σ_rate, σ_rate²]]

MEASUREMENT NOISE (R scalar):
    Based on propagation mode uncertainty:
    - High SNR, stable path: R ~ (0.1 ms)²
    - Low SNR, multipath: R ~ (2.0 ms)²

ADVANTAGE OVER WELFORD:
    Welford's treats all deviations as "noise" to average out.
    Kalman filter distinguishes between:
    - Slow drift (updates state estimate)
    - Random noise (filtered out)
    - Sudden jumps (detected as anomalies via innovation)

LOCK CRITERION:
    P[0,0] < lock_threshold² (state uncertainty converged)
    AND count ≥ min_samples

ANOMALY DETECTION:
    innovation = measurement - H × x_predicted
    S = H × P × H' + R  (innovation covariance)
    normalized_innovation = |innovation| / √S
    is_anomaly = normalized_innovation > k_sigma

================================================================================
OUTPUT: CONVERGENCE RESULT
================================================================================
Each measurement produces:
    - d_clock_ms: Best estimate (locked or running mean)
    - uncertainty_ms: Current uncertainty
    - residual_ms: Deviation from estimate (THE PROPAGATION SCIENCE!)
    - is_locked: True when locked
    - is_anomaly: True if residual exceeds threshold
    - convergence_progress: 0.0 to 1.0

WHEN LOCKED:
    residual_ms contains the ionospheric delay variation from mean.
    These residuals are the PRIMARY SCIENTIFIC OUTPUT for space weather!

================================================================================
USAGE
================================================================================
    model = ClockConvergenceModel(
        lock_uncertainty_ms=1.0,    # Lock when uncertainty < 1ms
        min_samples_for_lock=30,    # Need 30 minutes of data
        anomaly_sigma=3.0,          # 3σ for anomaly detection
        state_file=Path('/data/state/convergence.json')
    )
    
    # Process each minute's measurement
    result = model.process_measurement(
        station='WWV',
        frequency_mhz=10.0,
        d_clock_ms=15.234,
        timestamp=time.time()
    )
    
    if result.is_locked:
        # Use residual for space weather science!
        ionospheric_variation_ms = result.residual_ms
    else:
        # Still converging, use d_clock_ms for timing
        print(f"Converging: {result.convergence_progress:.0%}")

================================================================================
REVISION HISTORY
================================================================================
2025-12-07: Issue 3.1 FIX - Replaced Welford's algorithm with Kalman filter
2025-12-07: Added comprehensive documentation
2025-12-01: Added state persistence
2025-11-15: Initial "Set, Monitor, Intervention" implementation
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Tuple
import json
import math
import logging
import os
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

# =============================================================================
# STATE FILE VERSION AND VALIDATION (Issues 1.2, 1.3 Fix - 2025-12-08)
# =============================================================================

# Import centralized version and timestamp utilities (Issues 3.1, 3.2 Fix)
try:
    from ..version import STATE_FILE_VERSION, utc_isoformat
except ImportError:
    # Fallback for standalone testing
    STATE_FILE_VERSION = 2
    from datetime import datetime, timezone
    def utc_isoformat() -> str:
        return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

# Sanity thresholds for loaded Kalman state
MAX_REASONABLE_DRIFT_MS_PER_MIN = 0.1  # GPSDO drift < 0.01 PPM = 0.012 ms/min at 20kHz
MAX_REASONABLE_OFFSET_MS = 1000.0  # D_clock should be < 100ms typically
MAX_STATE_AGE_HOURS = 24  # Don't trust state older than 24 hours


def validate_kalman_state(kalman: 'KalmanClockTracker', timestamp: Optional[float] = None) -> Tuple[bool, str]:
    """
    Validate that loaded Kalman state is physically reasonable.
    
    Issue 1.3 Fix (2025-12-08): Prevents corrupted state from poisoning
    fresh measurements. The Dec 8 bug was caused by a drift_rate of
    -6.56 ms/min persisted to state file, which caused all new measurements
    to be rejected as 5-sigma outliers.
    
    Args:
        kalman: KalmanClockTracker instance to validate
        timestamp: Optional ISO timestamp of when state was saved
        
    Returns:
        (is_valid, reason) tuple
    """
    if kalman is None:
        return True, "No Kalman state to validate"
    
    # Check drift rate is physically possible for GPSDO
    drift_rate = kalman.drift_rate_ms_per_min
    if abs(drift_rate) > MAX_REASONABLE_DRIFT_MS_PER_MIN:
        return False, f"Drift rate {drift_rate:.3f} ms/min exceeds GPSDO limit {MAX_REASONABLE_DRIFT_MS_PER_MIN}"
    
    # Check offset is in reasonable range
    offset = kalman.offset_ms
    if abs(offset) > MAX_REASONABLE_OFFSET_MS:
        return False, f"Offset {offset:.1f} ms exceeds reasonable limit {MAX_REASONABLE_OFFSET_MS}"
    
    # Check covariance matrix is positive definite
    try:
        eigenvalues = np.linalg.eigvals(kalman.P)
        if np.min(eigenvalues) < -1e-10:  # Allow small numerical errors
            return False, f"Covariance matrix not positive definite (min eigenvalue: {np.min(eigenvalues):.2e})"
    except np.linalg.LinAlgError:
        return False, "Covariance matrix decomposition failed"
    
    # Check state age if timestamp provided
    if timestamp is not None:
        try:
            from datetime import datetime, timezone
            state_time = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            age_hours = (datetime.now(timezone.utc) - state_time).total_seconds() / 3600
            if age_hours > MAX_STATE_AGE_HOURS:
                return False, f"State is {age_hours:.1f} hours old (limit: {MAX_STATE_AGE_HOURS})"
        except (ValueError, TypeError):
            pass  # Ignore timestamp parsing errors
    
    return True, "State validated"


# =============================================================================
# KALMAN FILTER FOR CLOCK TRACKING (Issue 3.1 Fix)
# =============================================================================

class KalmanClockTracker:
    """
    Kalman filter for tracking clock offset with drift.
    
    This replaces Welford's algorithm to properly handle non-stationary
    clock behavior (temperature drift, ionospheric variations).
    
    State vector: [offset_ms, drift_rate_ms_per_min]
    
    The filter uses a constant-velocity model where:
    - offset(t+dt) = offset(t) + drift_rate * dt
    - drift_rate(t+dt) = drift_rate(t) + noise
    
    This allows the filter to track slow drift while filtering out
    random measurement noise.
    """
    
    def __init__(
        self,
        initial_offset_ms: float = 0.0,
        initial_uncertainty_ms: float = 100.0,
        process_noise_offset_ms: float = 0.01,
        process_noise_drift_ms_per_min: float = 0.001,
        measurement_noise_ms: float = 1.0
    ):
        """
        Initialize Kalman filter.
        
        Args:
            initial_offset_ms: Initial guess for clock offset
            initial_uncertainty_ms: Initial uncertainty (large = uninformed prior)
            process_noise_offset_ms: Process noise for offset (oscillator noise)
            process_noise_drift_ms_per_min: Process noise for drift rate
            measurement_noise_ms: Measurement noise (propagation uncertainty)
        """
        # State vector: [offset, drift_rate]
        self.x = np.array([initial_offset_ms, 0.0])
        
        # State covariance matrix (initially very uncertain)
        self.P = np.array([
            [initial_uncertainty_ms**2, 0.0],
            [0.0, (initial_uncertainty_ms / 10)**2]  # Drift uncertainty
        ])
        
        # Process noise parameters
        self.q_offset = process_noise_offset_ms**2
        self.q_drift = process_noise_drift_ms_per_min**2
        
        # Measurement noise
        self.R = measurement_noise_ms**2
        
        # Measurement matrix: we only observe offset, not drift
        self.H = np.array([[1.0, 0.0]])
        
        # Tracking
        self.count = 0
        self.last_timestamp: Optional[float] = None
        self.innovation_history: List[float] = []
    
    @property
    def offset_ms(self) -> float:
        """Current offset estimate."""
        return float(self.x[0])
    
    @property
    def drift_rate_ms_per_min(self) -> float:
        """Current drift rate estimate (ms/minute)."""
        return float(self.x[1])
    
    @property
    def uncertainty_ms(self) -> float:
        """Current offset uncertainty (1-sigma)."""
        return float(np.sqrt(self.P[0, 0]))
    
    @property
    def drift_uncertainty_ms_per_min(self) -> float:
        """Current drift rate uncertainty (1-sigma)."""
        return float(np.sqrt(self.P[1, 1]))
    
    def predict(self, dt_minutes: float = 1.0) -> None:
        """
        Prediction step: project state forward in time.
        
        Uses constant-velocity model:
            offset(t+dt) = offset(t) + drift * dt
            drift(t+dt) = drift(t)
        
        Args:
            dt_minutes: Time step in minutes (typically 1.0 for per-minute updates)
        """
        # State transition matrix
        F = np.array([
            [1.0, dt_minutes],
            [0.0, 1.0]
        ])
        
        # Process noise covariance (scaled by dt)
        # Using continuous white noise acceleration model
        Q = np.array([
            [self.q_offset + self.q_drift * dt_minutes**2 / 3, self.q_drift * dt_minutes / 2],
            [self.q_drift * dt_minutes / 2, self.q_drift]
        ]) * dt_minutes
        
        # Predict state and covariance
        self.x = F @ self.x
        self.P = F @ self.P @ F.T + Q
    
    def update(
        self,
        measurement_ms: float,
        timestamp: Optional[float] = None,
        measurement_noise_ms: Optional[float] = None
    ) -> Tuple[float, float, bool]:
        """
        Update step: incorporate new measurement.
        
        Args:
            measurement_ms: Measured clock offset
            timestamp: Unix timestamp (for dt calculation)
            measurement_noise_ms: Override measurement noise for this update
            
        Returns:
            (innovation, normalized_innovation, is_outlier)
        """
        # Calculate time delta for prediction
        if timestamp is not None and self.last_timestamp is not None:
            dt_minutes = (timestamp - self.last_timestamp) / 60.0
            dt_minutes = max(0.1, min(60.0, dt_minutes))  # Clamp to reasonable range
        else:
            dt_minutes = 1.0  # Assume 1 minute between updates
        
        if self.last_timestamp is not None:
            self.predict(dt_minutes)
        
        self.last_timestamp = timestamp
        self.count += 1
        
        # Measurement noise (allow per-measurement override)
        R = (measurement_noise_ms**2) if measurement_noise_ms else self.R
        
        # Innovation (measurement residual)
        z = np.array([measurement_ms])
        y = z - self.H @ self.x  # Innovation
        innovation = float(y[0])
        
        # Innovation covariance
        S = self.H @ self.P @ self.H.T + R
        S_scalar = float(S[0, 0])
        
        # Normalized innovation for outlier detection
        normalized_innovation = abs(innovation) / np.sqrt(S_scalar) if S_scalar > 0 else 0.0
        
        # Track innovation history
        self.innovation_history.append(innovation)
        if len(self.innovation_history) > 60:
            self.innovation_history.pop(0)
        
        # Check for outlier (don't update if extreme outlier)
        is_outlier = normalized_innovation > 5.0  # 5-sigma outlier
        
        if not is_outlier:
            # Kalman gain
            K = self.P @ self.H.T / S_scalar
            
            # Update state
            self.x = self.x + K.flatten() * innovation
            
            # Update covariance (Joseph form for numerical stability)
            I_KH = np.eye(2) - K @ self.H
            # K is 2x1, R is scalar -> K @ K.T is 2x2, then scale by R
            self.P = I_KH @ self.P @ I_KH.T + R * (K @ K.T)
        
        return innovation, normalized_innovation, is_outlier
    
    def get_std_dev(self) -> float:
        """
        Get standard deviation of recent innovations.
        
        This is useful for anomaly detection and quality assessment.
        """
        if len(self.innovation_history) < 5:
            return float('inf')
        return float(np.std(self.innovation_history))
    
    def to_dict(self) -> dict:
        """Serialize state for persistence."""
        return {
            'x': self.x.tolist(),
            'P': self.P.tolist(),
            'q_offset': self.q_offset,
            'q_drift': self.q_drift,
            'R': self.R,
            'count': self.count,
            'last_timestamp': self.last_timestamp,
            'innovation_history': self.innovation_history[-10:]  # Keep last 10
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> 'KalmanClockTracker':
        """Deserialize from dictionary."""
        tracker = cls()
        tracker.x = np.array(data.get('x', [0.0, 0.0]))
        tracker.P = np.array(data.get('P', [[100.0, 0.0], [0.0, 10.0]]))
        tracker.q_offset = data.get('q_offset', 0.0001)
        tracker.q_drift = data.get('q_drift', 0.000001)
        tracker.R = data.get('R', 1.0)
        tracker.count = data.get('count', 0)
        tracker.last_timestamp = data.get('last_timestamp')
        tracker.innovation_history = data.get('innovation_history', [])
        return tracker


class ConvergenceState(Enum):
    """State of the clock convergence model."""
    ACQUIRING = "acquiring"      # Building initial estimate
    CONVERGING = "converging"    # Uncertainty shrinking
    LOCKED = "locked"            # High confidence, monitoring mode
    HOLDOVER = "holdover"        # Using last good estimate
    REACQUIRE = "reacquire"      # Rebuilding after anomaly


@dataclass
class StationAccumulator:
    """
    Running statistics for a single station's clock offset measurements.
    
    Issue 3.1 Fix (2025-12-07): Now uses Kalman filter instead of Welford's
    algorithm to properly track clock offset + drift in non-stationary conditions.
    
    The Kalman filter tracks:
    - D_clock offset (ms)
    - D_clock drift rate (ms/minute)
    
    This allows the model to adapt to slow drift while filtering out noise.
    """
    station: str
    frequency_mhz: float
    
    # Kalman filter tracker (replaces Welford's algorithm)
    kalman: Optional[KalmanClockTracker] = field(default=None)
    
    # Legacy fields for backwards compatibility (now derived from Kalman)
    count: int = 0
    mean_ms: float = 0.0
    m2: float = 0.0  # Kept for API compatibility
    
    # Lock state
    state: ConvergenceState = field(default=ConvergenceState.ACQUIRING)
    locked_mean_ms: Optional[float] = None
    locked_uncertainty_ms: Optional[float] = None
    lock_timestamp: Optional[float] = None
    
    # History for anomaly detection
    last_measurements: List[float] = field(default_factory=list)
    max_history: int = 60  # Keep last hour of measurements
    
    # Anomaly tracking
    consecutive_anomalies: int = 0
    total_anomalies: int = 0
    
    # Last innovation from Kalman filter (for anomaly detection)
    last_innovation: float = 0.0
    last_normalized_innovation: float = 0.0
    
    def __post_init__(self):
        """Initialize Kalman filter if not provided."""
        if self.kalman is None:
            self.kalman = KalmanClockTracker(
                initial_offset_ms=0.0,
                initial_uncertainty_ms=100.0,
                process_noise_offset_ms=0.01,      # GPSDO: very stable
                process_noise_drift_ms_per_min=0.0001,  # Nearly zero - GPSDO has no drift
                measurement_noise_ms=20.0          # Ionospheric variations ~10-30ms
            )
    
    @property
    def variance(self) -> float:
        """Variance from Kalman innovation history (for compatibility)."""
        if self.kalman is None or len(self.kalman.innovation_history) < 2:
            return float('inf')
        return float(np.var(self.kalman.innovation_history))
    
    @property
    def std_dev(self) -> float:
        """Standard deviation from Kalman filter."""
        if self.kalman is None:
            return float('inf')
        return self.kalman.get_std_dev()
    
    @property
    def uncertainty_ms(self) -> float:
        """State uncertainty from Kalman filter (replaces σ/√N)."""
        if self.kalman is None:
            return float('inf')
        return self.kalman.uncertainty_ms
    
    @property
    def drift_rate_ms_per_min(self) -> float:
        """Estimated clock drift rate (ms/minute)."""
        if self.kalman is None:
            return 0.0
        return self.kalman.drift_rate_ms_per_min
    
    def update(self, measurement_ms: float, timestamp: Optional[float] = None) -> None:
        """
        Add a new measurement using Kalman filter.
        
        Issue 3.1 Fix: Replaces Welford's algorithm with Kalman filter
        to properly track non-stationary clock behavior.
        """
        if self.kalman is None:
            self.__post_init__()
        
        # Kalman filter update
        innovation, norm_innov, is_outlier = self.kalman.update(
            measurement_ms, timestamp=timestamp
        )
        
        self.last_innovation = innovation
        self.last_normalized_innovation = norm_innov
        
        # Update legacy fields for API compatibility
        self.count = self.kalman.count
        self.mean_ms = self.kalman.offset_ms
        
        # Track recent history
        self.last_measurements.append(measurement_ms)
        if len(self.last_measurements) > self.max_history:
            self.last_measurements.pop(0)
    
    def to_dict(self) -> dict:
        """Serialize state to dictionary."""
        return {
            'station': self.station,
            'frequency_mhz': self.frequency_mhz,
            'count': self.count,
            'mean_ms': self.mean_ms,
            'm2': self.m2,
            'state': self.state.value,
            'locked_mean_ms': self.locked_mean_ms,
            'locked_uncertainty_ms': self.locked_uncertainty_ms,
            'lock_timestamp': self.lock_timestamp,
            'last_measurements': self.last_measurements[-10:],
            'consecutive_anomalies': self.consecutive_anomalies,
            'total_anomalies': self.total_anomalies,
            # Kalman filter state
            'kalman': self.kalman.to_dict() if self.kalman else None
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> 'StationAccumulator':
        """Deserialize from dictionary."""
        # Load Kalman filter if present
        kalman_data = data.get('kalman')
        kalman = KalmanClockTracker.from_dict(kalman_data) if kalman_data else None
        
        acc = cls(
            station=data['station'],
            frequency_mhz=data['frequency_mhz'],
            kalman=kalman,
            count=data.get('count', 0),
            mean_ms=data.get('mean_ms', 0.0),
            m2=data.get('m2', 0.0),
            locked_mean_ms=data.get('locked_mean_ms'),
            locked_uncertainty_ms=data.get('locked_uncertainty_ms'),
            lock_timestamp=data.get('lock_timestamp'),
            consecutive_anomalies=data.get('consecutive_anomalies', 0),
            total_anomalies=data.get('total_anomalies', 0)
        )
        acc.state = ConvergenceState(data.get('state', 'acquiring'))
        acc.last_measurements = data.get('last_measurements', [])
        return acc


@dataclass
class ConvergenceResult:
    """Result of processing a measurement through the convergence model."""
    station: str
    frequency_mhz: float
    
    # Current state
    state: ConvergenceState
    d_clock_ms: float           # Best estimate of clock offset
    uncertainty_ms: float       # Current uncertainty
    
    # Measurement info
    measurement_ms: float       # Raw measurement this minute
    residual_ms: float          # Deviation from converged estimate (propagation effect!)
    
    # Statistics
    sample_count: int
    convergence_progress: float  # 0.0 to 1.0
    
    # Flags
    is_locked: bool
    is_anomaly: bool
    anomaly_sigma: Optional[float] = None


class ClockConvergenceModel:
    """
    Convergence-to-lock model for clock offset estimation.
    
    Implements the "Set, Monitor, Intervention" philosophy:
    1. SET: Converge to a locked clock offset estimate
    2. MONITOR: Track residuals for ionospheric effects
    3. INTERVENTION: Re-acquire only if anomaly detected
    
    Parameters:
    -----------
    lock_uncertainty_ms : float
        Uncertainty threshold for declaring lock (default: 1.0 ms)
    min_samples_for_lock : int
        Minimum samples before lock can be declared (default: 30)
    anomaly_sigma : float
        Number of standard deviations for anomaly detection (default: 3.0)
    max_consecutive_anomalies : int
        Anomalies before forcing re-acquire (default: 5)
    """
    
    def __init__(
        self,
        lock_uncertainty_ms: float = 1.0,
        min_samples_for_lock: int = 30,
        anomaly_sigma: float = 3.0,
        max_consecutive_anomalies: int = 5,
        state_file: Optional[Path] = None
    ):
        self.lock_uncertainty_ms = lock_uncertainty_ms
        self.min_samples_for_lock = min_samples_for_lock
        self.anomaly_sigma = anomaly_sigma
        self.max_consecutive_anomalies = max_consecutive_anomalies
        self.state_file = state_file
        
        # Per-station accumulators: key = "STATION_FREQ" e.g., "WWV_10.0"
        self.accumulators: Dict[str, StationAccumulator] = {}
        
        # Global state
        self.best_d_clock_ms: Optional[float] = None
        self.best_uncertainty_ms: Optional[float] = None
        self.best_source: Optional[str] = None
        
        # Load persisted state if available
        if state_file and state_file.exists():
            self._load_state()
    
    def _get_key(self, station: str, frequency_mhz: float) -> str:
        """Generate accumulator key."""
        return f"{station}_{frequency_mhz}"
    
    def _get_or_create_accumulator(self, station: str, frequency_mhz: float) -> StationAccumulator:
        """Get existing accumulator or create new one."""
        key = self._get_key(station, frequency_mhz)
        if key not in self.accumulators:
            self.accumulators[key] = StationAccumulator(
                station=station,
                frequency_mhz=frequency_mhz
            )
        return self.accumulators[key]
    
    def process_measurement(
        self,
        station: str,
        frequency_mhz: float,
        d_clock_ms: float,
        timestamp: float,
        snr_db: Optional[float] = None,
        quality_grade: Optional[str] = None
    ) -> ConvergenceResult:
        """
        Process a new clock offset measurement.
        
        Parameters:
        -----------
        station : str
            Station identifier (WWV, WWVH, CHU)
        frequency_mhz : float
            Frequency in MHz
        d_clock_ms : float
            Measured clock offset in milliseconds
        timestamp : float
            Unix timestamp of measurement
        snr_db : float, optional
            Signal-to-noise ratio
        quality_grade : str, optional
            Quality grade (A-D, X)
        
        Returns:
        --------
        ConvergenceResult with updated state and estimates
        """
        acc = self._get_or_create_accumulator(station, frequency_mhz)
        
        # Add measurement using Kalman filter (Issue 3.1 fix)
        # Skip update if measurement is None (failed detection)
        if d_clock_ms is not None:
            acc.update(d_clock_ms, timestamp=timestamp)
        
        # Calculate residual from current estimate
        # Issue 3.1: For Kalman filter, use the innovation (predicted vs actual)
        if d_clock_ms is not None:
            if acc.state == ConvergenceState.LOCKED and acc.locked_mean_ms is not None:
                # When locked, residual is relative to locked value
                reference = acc.locked_mean_ms
                residual_ms = d_clock_ms - reference
            else:
                # During convergence, use Kalman innovation as residual
                reference = acc.mean_ms
                residual_ms = acc.last_innovation  # From Kalman filter
        else:
            residual_ms = 0.0
        
        # Anomaly detection using Kalman filter's normalized innovation
        # Issue 3.1: This is statistically more robust than std_dev-based detection
        is_anomaly = False
        anomaly_sigma = None
        if d_clock_ms is not None and acc.count > 10:
            # Use normalized innovation from Kalman filter
            anomaly_sigma = acc.last_normalized_innovation
            is_anomaly = anomaly_sigma > self.anomaly_sigma
        
        # Update anomaly tracking
        if is_anomaly:
            acc.consecutive_anomalies += 1
            acc.total_anomalies += 1
        else:
            acc.consecutive_anomalies = 0
        
        # State machine
        prev_state = acc.state
        
        if acc.state == ConvergenceState.ACQUIRING:
            # Need minimum samples to assess convergence
            if acc.count >= 10:
                acc.state = ConvergenceState.CONVERGING
        
        elif acc.state == ConvergenceState.CONVERGING:
            # Check lock criterion
            if (acc.count >= self.min_samples_for_lock and 
                acc.uncertainty_ms < self.lock_uncertainty_ms):
                acc.state = ConvergenceState.LOCKED
                acc.locked_mean_ms = acc.mean_ms
                acc.locked_uncertainty_ms = acc.uncertainty_ms
                acc.lock_timestamp = timestamp
                logger.info(
                    f"🔒 LOCKED: {station} @ {frequency_mhz} MHz - "
                    f"D_clock = {acc.locked_mean_ms:.3f} ± {acc.locked_uncertainty_ms:.3f} ms "
                    f"after {acc.count} samples"
                )
        
        elif acc.state == ConvergenceState.LOCKED:
            # Monitor for anomalies
            if acc.consecutive_anomalies >= self.max_consecutive_anomalies:
                acc.state = ConvergenceState.REACQUIRE
                logger.warning(
                    f"⚠️ REACQUIRE: {station} @ {frequency_mhz} MHz - "
                    f"{acc.consecutive_anomalies} consecutive anomalies"
                )
        
        elif acc.state == ConvergenceState.REACQUIRE:
            # Wait for valid data before re-initializing
            if d_clock_ms is not None:
                # Reset and start over - INCLUDING the Kalman filter
                acc.count = 1
                acc.mean_ms = d_clock_ms
                acc.m2 = 0.0
                acc.consecutive_anomalies = 0
                acc.locked_mean_ms = None
                acc.locked_uncertainty_ms = None
                # Reset Kalman filter to current measurement (critical fix!)
                acc.kalman = KalmanClockTracker(
                    initial_offset_ms=d_clock_ms,
                    initial_uncertainty_ms=100.0,
                    process_noise_offset_ms=0.01,
                    process_noise_drift_ms_per_min=0.0001,  # Nearly zero - GPSDO has no drift
                    measurement_noise_ms=20.0              # Ionospheric variations ~10-30ms
                )
                acc.state = ConvergenceState.CONVERGING
                logger.info(f"Reset Kalman filter for {station} @ {frequency_mhz} MHz to {d_clock_ms:.2f}ms")
                acc.state = ConvergenceState.ACQUIRING
        
        # Log state transitions
        if acc.state != prev_state:
            logger.info(f"State transition: {station} @ {frequency_mhz} MHz: {prev_state.value} → {acc.state.value}")
        
        # Calculate convergence progress (0 to 1)
        if acc.count < 10:
            progress = acc.count / 10 * 0.2  # 0-20% during acquiring
        elif acc.state == ConvergenceState.CONVERGING:
            # Progress based on uncertainty approaching lock threshold
            if acc.uncertainty_ms > self.lock_uncertainty_ms * 10:
                progress = 0.3
            else:
                progress = 0.3 + 0.6 * (1 - acc.uncertainty_ms / (self.lock_uncertainty_ms * 10))
        elif acc.state == ConvergenceState.LOCKED:
            progress = 1.0
        else:
            progress = 0.5
        
        # Persist state periodically
        if self.state_file and acc.count % 10 == 0:
            self._save_state()
        
        return ConvergenceResult(
            station=station,
            frequency_mhz=frequency_mhz,
            state=acc.state,
            d_clock_ms=acc.locked_mean_ms if acc.state == ConvergenceState.LOCKED else acc.mean_ms,
            uncertainty_ms=acc.locked_uncertainty_ms if acc.state == ConvergenceState.LOCKED else acc.uncertainty_ms,
            measurement_ms=d_clock_ms,
            residual_ms=residual_ms,
            sample_count=acc.count,
            convergence_progress=progress,
            is_locked=acc.state == ConvergenceState.LOCKED,
            is_anomaly=is_anomaly,
            anomaly_sigma=anomaly_sigma
        )
    
    def get_best_estimate(self) -> Tuple[Optional[float], Optional[float], Optional[str]]:
        """
        Get the best current D_clock estimate across all stations.
        
        Returns:
        --------
        (d_clock_ms, uncertainty_ms, source) or (None, None, None)
        """
        best = None
        
        for key, acc in self.accumulators.items():
            if acc.count < 5:
                continue
            
            # Prefer locked estimates
            if acc.state == ConvergenceState.LOCKED:
                if best is None or acc.locked_uncertainty_ms < best[1]:
                    best = (acc.locked_mean_ms, acc.locked_uncertainty_ms, key)
            elif best is None or acc.uncertainty_ms < best[1]:
                if acc.uncertainty_ms != float('inf'):
                    best = (acc.mean_ms, acc.uncertainty_ms, key)
        
        if best:
            self.best_d_clock_ms, self.best_uncertainty_ms, self.best_source = best
            return best
        return (None, None, None)
    
    def get_status(self) -> dict:
        """Get full status of convergence model."""
        d_clock, uncertainty, source = self.get_best_estimate()
        
        locked_count = sum(1 for a in self.accumulators.values() 
                          if a.state == ConvergenceState.LOCKED)
        converging_count = sum(1 for a in self.accumulators.values() 
                              if a.state == ConvergenceState.CONVERGING)
        
        return {
            'best_d_clock_ms': d_clock,
            'best_uncertainty_ms': uncertainty,
            'best_source': source,
            'locked_stations': locked_count,
            'converging_stations': converging_count,
            'total_stations': len(self.accumulators),
            'stations': {
                key: {
                    'state': acc.state.value,
                    'count': acc.count,
                    'mean_ms': acc.mean_ms,
                    'uncertainty_ms': acc.uncertainty_ms if acc.uncertainty_ms != float('inf') else None,
                    'locked_mean_ms': acc.locked_mean_ms,
                    'locked_uncertainty_ms': acc.locked_uncertainty_ms,
                    'convergence_progress': acc.count / self.min_samples_for_lock if acc.count < self.min_samples_for_lock else 1.0
                }
                for key, acc in self.accumulators.items()
            }
        }
    
    def _save_state(self) -> None:
        """
        Persist state to file.
        
        Issue 1.2 Fix (2025-12-08): Now uses STATE_FILE_VERSION constant
        and UTC timestamp for proper version tracking and age validation.
        """
        if not self.state_file:
            return
        
        state = {
            'version': STATE_FILE_VERSION,
            'timestamp': utc_isoformat(),  # Issue 3.2: Use centralized UTC timestamp
            'accumulators': {
                key: acc.to_dict() 
                for key, acc in self.accumulators.items()
            }
        }
        
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            # Atomic write: write to temp file, fsync, then rename
            temp_file = self.state_file.with_suffix('.tmp')
            with open(temp_file, 'w') as f:
                json.dump(state, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            temp_file.replace(self.state_file)
        except Exception as e:
            logger.warning(f"Failed to save convergence state: {e}")
    
    def _load_state(self) -> None:
        """
        Load state from file with version validation and sanity checks.
        
        Issue 1.2 Fix (2025-12-08): Validates state file version before loading.
        Issue 1.3 Fix (2025-12-08): Validates Kalman filter state is physically
        reasonable before using it. Corrupted state is discarded.
        """
        if not self.state_file or not self.state_file.exists():
            return
        
        try:
            with open(self.state_file, 'r') as f:
                state = json.load(f)
            
            # Issue 1.2: Version validation
            file_version = state.get('version', 0)
            if file_version < STATE_FILE_VERSION:
                logger.warning(
                    f"State file version {file_version} < current {STATE_FILE_VERSION}, "
                    "discarding stale state and starting fresh"
                )
                return
            
            timestamp = state.get('timestamp')
            loaded_count = 0
            rejected_count = 0
            
            for key, data in state.get('accumulators', {}).items():
                acc = StationAccumulator.from_dict(data)
                
                # Issue 1.3: Validate Kalman state is physically reasonable
                if acc.kalman is not None:
                    is_valid, reason = validate_kalman_state(acc.kalman, timestamp)
                    if not is_valid:
                        logger.warning(
                            f"Rejecting corrupted Kalman state for {key}: {reason}. "
                            "Starting fresh for this station."
                        )
                        # Reset the Kalman filter but keep legacy stats
                        acc.kalman = None
                        acc.state = ConvergenceState.ACQUIRING
                        acc.locked_mean_ms = None
                        acc.locked_uncertainty_ms = None
                        rejected_count += 1
                    else:
                        loaded_count += 1
                
                self.accumulators[key] = acc
            
            if rejected_count > 0:
                logger.warning(
                    f"Loaded {loaded_count} valid, rejected {rejected_count} corrupted "
                    f"Kalman states from {self.state_file}"
                )
            else:
                logger.info(f"Loaded convergence state: {len(self.accumulators)} stations (all valid)")
                
        except json.JSONDecodeError as e:
            logger.warning(f"Corrupted state file {self.state_file}: {e}. Starting fresh.")
        except Exception as e:
            logger.warning(f"Failed to load convergence state: {e}")
