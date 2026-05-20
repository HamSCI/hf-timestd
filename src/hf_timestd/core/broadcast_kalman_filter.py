#!/usr/bin/env python3
"""
Broadcast Kalman Filter - Per-Broadcast Ionospheric Path Tracking

================================================================================
PURPOSE
================================================================================
Track ionospheric path dynamics for a single broadcast using a Kalman filter.

Each of the 17 broadcasts (WWV, WWVH, CHU, BPM across multiple frequencies)
is treated as an independent ionospheric probe with unique characteristics.

STATE VECTOR:
    x = [tof_ms, doppler_ms_per_min]
    
    tof_ms: Time of Flight (ionospheric path delay in milliseconds)
    doppler_ms_per_min: Rate of change of ToF (tracks layer movement)

SCIENCE FOCUS:
    This is NOT a clock recovery filter. It tracks ionospheric path dynamics.
    The goal is to study propagation, not smooth it out.

================================================================================
DESIGN PRINCIPLES
================================================================================
1. **Per-Probe Tuning**: Each broadcast has unique characteristics
   - Frequency-dependent ionospheric properties
   - Station-specific path characteristics
   - Modulation-specific detection properties

2. **GPSDO Temporal Continuity**: Leverage GPSDO stability
   - Minute-to-minute consistency is the primary constraint
   - Not Kalman prediction - direct comparison using "steel ruler"

3. **Fading Handling**: Graceful degradation during signal loss
   - Predict-only mode when SNR drops
   - Maintain state continuity through fading periods

4. **Dynamic Measurement Noise**: SNR-based uncertainty
   - High SNR → trust measurement
   - Low SNR → trust prediction

================================================================================
USAGE
================================================================================
    # Create filter for WWV 10 MHz
    filter = BroadcastKalmanFilter(
        broadcast_id="WWV_10000",
        station="WWV",
        frequency_mhz=10.0
    )
    
    # Update with measurement
    tof, uncertainty = filter.update(
        measurement_ms=34.5,
        snr_db=15.0
    )
    
    # During fading (low SNR)
    tof, uncertainty = filter.predict()  # Coast on last known state
    
    # Get current state
    state = filter.get_state()
    # Returns: {'tof_ms': 34.5, 'doppler_ms_per_min': 0.02, ...}

================================================================================
REVISION HISTORY
================================================================================
2026-01-07: Initial implementation for science-first architecture (v5.0.0)
"""

import time

import numpy as np
import json
import logging
from pathlib import Path
from typing import Dict, Tuple, Optional
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


@dataclass
class BroadcastCharacteristics:
    """Physical characteristics of a broadcast that inform filter tuning."""
    
    broadcast_id: str
    station: str
    frequency_mhz: float
    
    # Ionospheric properties
    typical_layer: str  # 'E', 'F', 'E/F'
    typical_height_km: float  # Refraction height
    
    # Path properties
    path_length_km: float
    typical_hops: int
    
    # Process noise (ionospheric volatility)
    q_tof: float  # ms²/min
    q_doppler: float  # (ms/min)²/min
    
    # Measurement noise baseline
    base_measurement_noise_ms: float
    
    # Modulation type
    modulation: str  # 'AM', 'FSK', 'AM+BCD'
    
    # Special features
    has_bcd: bool = False
    has_test_signal: bool = False
    is_anchor: bool = False  # Unambiguous station ID


class BroadcastKalmanFilter:
    """
    Kalman filter for tracking ionospheric path dynamics of a single broadcast.
    
    This filter tracks the Time of Flight (ToF) and its rate of change (Doppler)
    for one specific broadcast. Each broadcast is an independent ionospheric probe.
    """
    
    def __init__(self, broadcast_id: str, station: str, frequency_mhz: float):
        """
        Initialize Kalman filter for a specific broadcast.
        
        Args:
            broadcast_id: Unique identifier (e.g., "WWV_10000")
            station: Station name (WWV, WWVH, CHU, BPM)
            frequency_mhz: Frequency in MHz
        """
        self.broadcast_id = broadcast_id
        self.station = station
        self.frequency_mhz = frequency_mhz
        
        # Get broadcast-specific characteristics
        self.characteristics = self._get_broadcast_characteristics()
        
        # State vector: [tof_ms, doppler_ms_per_min]
        self.state = np.array([0.0, 0.0])
        
        # State covariance matrix
        self.P = np.eye(2) * 100.0  # High initial uncertainty
        
        # Process noise (ionospheric volatility)
        self.Q = np.array([
            [self.characteristics.q_tof, 0.0],
            [0.0, self.characteristics.q_doppler]
        ])
        
        # State transition matrix (dt = 1 minute)
        self.F = np.array([
            [1.0, 1.0],  # tof_new = tof_old + doppler * dt
            [0.0, 1.0]   # doppler_new = doppler_old (constant velocity model)
        ])
        
        # Measurement matrix (we observe tof, not doppler)
        self.H = np.array([[1.0, 0.0]])
        
        # Filter state
        self.initialized = False
        self.n_updates = 0
        self.last_update_time = None
        
        # History for GPSDO temporal continuity
        self.previous_tof = None
        self.previous_time = None
        
        # ADAPTIVE KALMAN ENHANCEMENTS (2026-01-08)
        # Mode-transition timing — a single wall-clock base (M-H20). "Time
        # since the last transition" is always derived from this timestamp via
        # _minutes_since_mode_change(); there is no separate update-counter.
        # Initialised far in the past so the filter starts in the stable regime.
        self.last_mode_status = 'STABLE'
        self.last_innovation = 0.0
        self.last_mode_transition_time = time.time() - 10000.0
        
        logger.info(
            f"Initialized {self.broadcast_id} Kalman filter: "
            f"layer={self.characteristics.typical_layer}, "
            f"q_tof={self.characteristics.q_tof:.3f}, "
            f"modulation={self.characteristics.modulation}"
        )
    
    def _get_broadcast_characteristics(self) -> BroadcastCharacteristics:
        """
        Get physical characteristics for this broadcast.
        
        Returns tuned parameters based on:
        - Frequency (ionospheric layer, volatility)
        - Station (path length, complexity)
        - Modulation (detection robustness)
        """
        # Frequency-dependent ionospheric properties
        if self.frequency_mhz < 5.0:
            # Low freq: E-layer, high volatility
            layer = 'E'
            height_km = 110.0
            q_tof = 1.5
            q_doppler = 0.1
        elif self.frequency_mhz < 10.0:
            # Mid freq: E/F transition, moderate volatility
            layer = 'E/F'
            height_km = 150.0
            q_tof = 0.8
            q_doppler = 0.05
        else:
            # High freq: F-layer, lower volatility (but solar-sensitive)
            layer = 'F'
            height_km = 250.0
            q_tof = 0.3
            q_doppler = 0.02
        
        # Station-dependent path properties
        if self.station == 'WWV':
            path_km = 1800.0
            hops = 1
            base_noise = 0.3
        elif self.station == 'WWVH':
            path_km = 4500.0
            hops = 2
            base_noise = 0.8
            # Increase volatility for long path
            q_tof *= 1.5
        elif self.station == 'CHU':
            path_km = 2200.0
            hops = 1
            base_noise = 0.4
            # Increase volatility for auroral effects
            q_tof *= 1.2
        elif self.station == 'BPM':
            path_km = 10000.0
            hops = 3
            base_noise = 1.5
            # Very high volatility for very long path
            q_tof *= 2.0
        else:
            # Default
            path_km = 2000.0
            hops = 1
            base_noise = 0.5
        
        # Modulation-specific properties
        if self.station in ['WWV', 'WWVH']:
            modulation = 'AM+BCD'
            has_bcd = True
            has_test_signal = True
        elif self.station == 'CHU':
            modulation = 'FSK'
            has_bcd = False
            has_test_signal = False
            # FSK is more robust - reduce measurement noise
            base_noise *= 0.7
        else:  # BPM
            modulation = 'AM'
            has_bcd = False
            has_test_signal = False
        
        # Anchor channels (unambiguous station ID)
        is_anchor = (
            (self.station == 'WWV' and self.frequency_mhz in [20.0, 25.0]) or
            (self.station == 'CHU' and self.frequency_mhz in [3.33, 7.85, 14.67])
        )
        
        return BroadcastCharacteristics(
            broadcast_id=self.broadcast_id,
            station=self.station,
            frequency_mhz=self.frequency_mhz,
            typical_layer=layer,
            typical_height_km=height_km,
            path_length_km=path_km,
            typical_hops=hops,
            q_tof=q_tof,
            q_doppler=q_doppler,
            base_measurement_noise_ms=base_noise,
            modulation=modulation,
            has_bcd=has_bcd,
            has_test_signal=has_test_signal,
            is_anchor=is_anchor
        )
    
    def predict(self, dt: float = 1.0) -> Tuple[float, float]:
        """
        Predict next state (coast during fading).
        
        Args:
            dt: Time step in minutes (default 1.0)
            
        Returns:
            (tof_ms, uncertainty_ms)
        """
        if not self.initialized:
            # Can't predict without initialization
            return 0.0, 100.0
        
        # Update transition matrix for this dt
        F = np.array([
            [1.0, dt],
            [0.0, 1.0]
        ])
        
        # Predict state
        self.state = F @ self.state
        
        # Predict covariance
        self.P = F @ self.P @ F.T + self.Q * dt
        
        # Extract uncertainty
        uncertainty = np.sqrt(self.P[0, 0])
        
        return self.state[0], uncertainty
    
    def update(self, measurement_ms: float, snr_db: float) -> Tuple[float, float]:
        """
        Update filter with new measurement.

        Args:
            measurement_ms: Measured ToF in milliseconds
            snr_db: Signal-to-noise ratio in dB

        Returns:
            (tof_ms, uncertainty_ms)
        """
        # M-M15: NaN/Inf guard.  Either input being non-finite poisons
        # the state covariance instantly (the L3 NaN filter in the fusion
        # service runs downstream, so it couldn't catch corruption that
        # had already landed in `self.P` here).  Refuse the update and
        # return the current state/uncertainty so the caller sees a
        # well-defined "no-op" answer rather than NaN.  ~10⁶ updates/week
        # means we cannot rely on upstream cleanliness for safety.
        if not (np.isfinite(measurement_ms) and np.isfinite(snr_db)):
            logger.warning(
                f"{self.broadcast_id}: non-finite Kalman input rejected "
                f"(measurement_ms={measurement_ms!r}, snr_db={snr_db!r})"
            )
            if not self.initialized:
                return float('nan'), float('nan')
            return float(self.state[0]), float(np.sqrt(self.P[0, 0]))

        # Initialize on first measurement
        if not self.initialized:
            self.state[0] = measurement_ms
            self.state[1] = 0.0  # Unknown doppler initially
            self.initialized = True
            self.n_updates = 1
            self.last_update_time = datetime.now(timezone.utc)

            uncertainty = np.sqrt(self.P[0, 0])
            logger.info(
                f"{self.broadcast_id} initialized: "
                f"tof={measurement_ms:.3f}ms, uncertainty={uncertainty:.3f}ms"
            )
            return measurement_ms, uncertainty

        # PREDICT (state) — F advances tof by doppler·dt. The covariance
        # predict is deferred until the adaptive Q is known (below).
        self.state = self.F @ self.state

        # ONE innovation, derived AFTER the predict — the true residual
        # (M-H19). The filter's defences (mode-transition detection, adaptive
        # process noise) and the measurement update all key off this same
        # value. Previously they were fed `measurement - state[0]` computed
        # BEFORE the predict, which differs from the true innovation by
        # doppler·dt — so the defences keyed off the wrong residual.
        innovation = measurement_ms - (self.H @ self.state)[0]
        self.last_innovation = innovation

        # Mode-transition detection (adaptive Kalman enhancement)
        mode_status = self.detect_mode_transition(innovation)
        if mode_status == 'MODE_CHANGE':
            self.last_mode_transition_time = time.time()
            logger.info(f"{self.broadcast_id}: Mode transition detected")
        # POSSIBLE_CHANGE / STABLE: no transition — leave the timestamp as is.

        # Adaptive process noise, then the covariance predict (dt=1.0 minute)
        Q_adaptive = self._adaptive_process_noise(
            innovation_ms=innovation,
            snr_db=snr_db,
            time_since_mode_change=self._minutes_since_mode_change(),
        )
        self.P = self.F @ self.P @ self.F.T + Q_adaptive

        # Measurement noise (dynamic, based on SNR)
        R = self._get_measurement_noise(snr_db)

        # Kalman gain
        S = self.H @ self.P @ self.H.T + R
        K = self.P @ self.H.T / S

        # Update step (same innovation derived above)
        self.state = self.state + K.flatten() * innovation

        # M-M14: Joseph-form covariance update.
        #
        #     P = (I − K H) P (I − K H)ᵀ + K R Kᵀ
        #
        # The short form `P = (I − K H) P` is mathematically equivalent
        # in exact arithmetic, but at finite precision it neither
        # preserves symmetry nor guarantees positive-definiteness — both
        # of which the Kalman gain on the *next* cycle needs.  With ~10⁶
        # updates per week per broadcast bank, asymmetry and tiny
        # negative eigenvalues accumulate; this filter would eventually
        # produce NaN gains or negative-variance states.  The Joseph
        # form is symmetric by construction (a congruence transform of a
        # symmetric P plus a symmetric outer product) and remains PD as
        # long as P was PD and R > 0.  A belt-and-braces explicit
        # symmetrisation (½(P + Pᵀ)) cleans up the last bits of
        # asymmetry the asymmetric subtraction `I − KH` can shed.
        IKH = np.eye(2) - K @ self.H
        self.P = IKH @ self.P @ IKH.T + (K @ K.T) * float(R)
        self.P = 0.5 * (self.P + self.P.T)

        # Increment counter
        self.n_updates += 1
        self.last_update_time = datetime.now(timezone.utc)

        # Extract uncertainty
        uncertainty = np.sqrt(self.P[0, 0])

        return self.state[0], uncertainty
    
    def _get_measurement_noise(self, snr_db: float) -> float:
        """
        Get measurement noise variance based on SNR.
        
        Higher SNR → lower noise (trust measurement)
        Lower SNR → higher noise (trust prediction)
        
        Args:
            snr_db: Signal-to-noise ratio in dB
            
        Returns:
            Measurement noise variance (ms²)
        """
        # Convert SNR from dB to linear
        snr_linear = 10 ** (snr_db / 10)
        
        # Noise factor: high SNR → low factor, low SNR → high factor
        # At SNR=20dB (100:1), factor ≈ 0.1
        # At SNR=10dB (10:1), factor ≈ 0.3
        # At SNR=0dB (1:1), factor ≈ 1.0
        noise_factor = 1.0 / np.sqrt(snr_linear)
        
        # Base noise from broadcast characteristics
        base_noise = self.characteristics.base_measurement_noise_ms
        
        # Total measurement noise
        noise_ms = base_noise * noise_factor
        
        # Return variance
        return noise_ms ** 2
    
    def _minutes_since_mode_change(self) -> float:
        """Minutes elapsed since the last detected mode transition.

        Single wall-clock time base (M-H20): the adaptive process noise, the
        search-window widening, and is_converged() all derive "time since the
        last transition" from this one call, which is persisted across restarts.
        """
        return (time.time() - self.last_mode_transition_time) / 60.0

    def _adaptive_process_noise(
        self,
        innovation_ms: float, 
        snr_db: float,
        time_since_mode_change: float
    ) -> np.ndarray:
        """
        Calculate adaptive process noise based on current conditions.
        
        ADAPTIVE KALMAN ENHANCEMENT (2026-01-08):
        Adjusts Q (process noise) based on:
        - SNR: Low SNR → expect more jitter
        - Innovation: Large residual → increase Q to track changes
        - Mode stability: Recent transition → higher uncertainty
        
        Args:
            innovation_ms: Current innovation (measurement - prediction)
            snr_db: Signal-to-noise ratio
            time_since_mode_change: Minutes since last mode transition
            
        Returns:
            Adaptive Q matrix
        """
        Q_base = self.Q.copy()
        
        # SNR scaling (low SNR → more jitter expected)
        snr_scale = max(1.0, 20.0 / max(snr_db, 5.0))
        
        # Innovation scaling (large residual → increase Q)
        if abs(innovation_ms) > 2.0:
            innovation_scale = abs(innovation_ms) / 2.0
        else:
            innovation_scale = 1.0
        
        # Mode change scaling (recent change → higher uncertainty)
        if time_since_mode_change < 5.0:
            mode_scale = 3.0
        else:
            mode_scale = 1.0
        
        # Combined scaling
        Q_adaptive = Q_base * snr_scale * innovation_scale * mode_scale
        
        return Q_adaptive
    
    def detect_mode_transition(self, innovation_ms: float) -> str:
        """
        Detect sudden propagation changes via innovation analysis.
        
        ADAPTIVE KALMAN ENHANCEMENT (2026-01-08):
        Uses Mahalanobis distance (normalized innovation) to detect:
        - MODE_CHANGE: Very large innovation (>5σ) - likely propagation mode change
        - POSSIBLE_CHANGE: Moderate innovation (>3σ) - possible change
        - STABLE: Small innovation (<3σ) - tracking well
        
        Args:
            innovation_ms: Innovation (measurement - prediction)
            
        Returns:
            Mode status: 'MODE_CHANGE', 'POSSIBLE_CHANGE', or 'STABLE'
        """
        # Calculate innovation covariance (predicted measurement uncertainty)
        S = self.H @ self.P @ self.H.T + self._get_measurement_noise(10.0)  # Use nominal SNR
        sigma_innovation = np.sqrt(S[0, 0] if S.ndim > 1 else S)
        
        # Normalized innovation (Mahalanobis distance)
        if sigma_innovation > 0:
            normalized = abs(innovation_ms) / sigma_innovation
        else:
            normalized = 0.0
        
        if normalized > 5.0:
            return 'MODE_CHANGE'
        elif normalized > 3.0:
            return 'POSSIBLE_CHANGE'
        else:
            return 'STABLE'
    
    def get_search_window(self, snr_db: float) -> float:
        """
        Calculate adaptive search window based on uncertainty and SNR.
        
        ADAPTIVE KALMAN ENHANCEMENT (2026-01-08):
        Implements ROC tradeoff:
        - Narrow window → high specificity (reject noise)
        - Wide window → high sensitivity (find weak signals)
        
        Window adapts based on:
        - Filter uncertainty (3σ confidence interval)
        - SNR (low SNR → widen for sensitivity)
        - Mode stability (recent transition → widen temporarily)
        
        Args:
            snr_db: Current signal-to-noise ratio
            
        Returns:
            Search window half-width in milliseconds
        """
        # Extract ToF uncertainty from covariance
        sigma_tof = np.sqrt(self.P[0, 0])
        
        # Base: 3σ window (99.7% confidence)
        window = 3.0 * sigma_tof
        
        # SNR adjustment (low SNR → widen for sensitivity)
        if snr_db < 10:
            snr_factor = 1.5
        else:
            snr_factor = 1.0
        
        # Mode stability (recent transition → widen)
        if self._minutes_since_mode_change() < 5.0:
            mode_factor = 2.0
        else:
            mode_factor = 1.0
        
        # Calculate final window
        window = window * snr_factor * mode_factor
        
        # Physical constraints
        MIN_WINDOW = 3.0   # ms (ionospheric jitter minimum)
        MAX_WINDOW = 50.0  # ms (mode transition maximum)
        
        return max(MIN_WINDOW, min(MAX_WINDOW, window))
    
    def is_converged(self) -> bool:
        """
        Detect convergence using innovation-based criteria.
        
        INNOVATION-BASED CONVERGENCE (2026-01-08):
        Enables fast convergence (5-15 min vs 30 min fixed) by detecting when:
        - Uncertainty is low (< 2ms)
        - Innovation is small (< 1ms) - measurements match predictions
        - Mode is stable (> 3 minutes since last transition)
        - Sufficient data (>= 5 updates)
        
        This allows narrowing search windows earlier for strong signals while
        maintaining conservative approach for weak/unstable signals.
        
        Returns:
            True if converged, False otherwise
        """
        # Need minimum data
        if self.n_updates < 5:
            return False
        
        # Check uncertainty (confident in state estimate)
        uncertainty_ms = np.sqrt(self.P[0, 0])
        uncertainty_ok = uncertainty_ms < 2.0
        
        # Check innovation (measurements match predictions)
        innovation_ok = abs(self.last_innovation) < 1.0
        
        # Check mode stability (no recent transitions) — same wall-clock base
        # as the adaptive Q and search window (M-H20).
        mode_stable = self._minutes_since_mode_change() > 3.0  # minutes
        
        return uncertainty_ok and innovation_ok and mode_stable
    
    def check_gpsdo_continuity(self, current_tof: float) -> Tuple[bool, float]:
        """
        Check consistency with previous measurement using GPSDO constraint.
        
        This is NOT Kalman prediction - it's direct comparison using the
        GPSDO "steel ruler" to validate temporal continuity.
        
        Args:
            current_tof: Current ToF measurement
            
        Returns:
            (is_consistent, residual_ms)
        """
        if self.previous_tof is None:
            # First measurement - can't check continuity
            self.previous_tof = current_tof
            self.previous_time = datetime.now(timezone.utc)
            return True, 0.0
        
        # Expected ToF based on GPSDO temporal stability
        # GPSDO hasn't drifted significantly in 1 minute
        expected_tof = self.previous_tof
        
        # Residual
        residual = abs(current_tof - expected_tof)
        
        # Update history
        self.previous_tof = current_tof
        self.previous_time = datetime.now(timezone.utc)
        
        # Consistency threshold (1 ms for good GPSDO stability)
        is_consistent = residual < 1.0
        
        return is_consistent, residual
    
    def get_state(self) -> Dict[str, float]:
        """
        Get current filter state.
        
        Returns:
            Dictionary with state information
        """
        return {
            'broadcast_id': self.broadcast_id,
            'station': self.station,
            'frequency_mhz': self.frequency_mhz,
            'tof_ms': self.state[0],
            'doppler_ms_per_min': self.state[1],
            'tof_uncertainty_ms': np.sqrt(self.P[0, 0]),
            'doppler_uncertainty': np.sqrt(self.P[1, 1]),
            'n_updates': self.n_updates,
            'initialized': self.initialized,
            'last_update': self.last_update_time.isoformat() if self.last_update_time else None
        }
    
    def save_state(self, state_dir: Path):
        """
        Save filter state to disk for persistence across restarts.
        
        Args:
            state_dir: Directory to save state file
        """
        state_dir.mkdir(parents=True, exist_ok=True)
        
        state_file = state_dir / f"{self.broadcast_id}_kalman_state.json"
        
        state_data = {
            'broadcast_id': self.broadcast_id,
            'station': self.station,
            'frequency_mhz': self.frequency_mhz,
            'state': self.state.tolist(),
            'covariance': self.P.tolist(),
            'n_updates': self.n_updates,
            'initialized': self.initialized,
            'last_update': self.last_update_time.isoformat() if self.last_update_time else None,
            'last_mode_transition_time': self.last_mode_transition_time,
            'saved_at': datetime.now(timezone.utc).isoformat()
        }
        
        with open(state_file, 'w') as f:
            json.dump(state_data, f, indent=2)
        
        logger.debug(f"Saved {self.broadcast_id} state to {state_file}")
    
    def load_state(self, state_dir: Path) -> bool:
        """
        Load filter state from disk.
        
        Args:
            state_dir: Directory containing state file
            
        Returns:
            True if state loaded successfully, False otherwise
        """
        state_file = state_dir / f"{self.broadcast_id}_kalman_state.json"
        
        if not state_file.exists():
            logger.debug(f"No saved state for {self.broadcast_id}")
            return False
        
        try:
            with open(state_file, 'r') as f:
                state_data = json.load(f)
            
            # Restore state
            self.state = np.array(state_data['state'])
            self.P = np.array(state_data['covariance'])
            self.n_updates = state_data['n_updates']
            self.initialized = state_data['initialized']
            
            if state_data['last_update']:
                self.last_update_time = datetime.fromisoformat(state_data['last_update'])

            # Restore mode-transition timing (M-H20). Pre-M-H20 state files
            # lack this key — keep the constructor default (stable) for those.
            if 'last_mode_transition_time' in state_data:
                self.last_mode_transition_time = state_data['last_mode_transition_time']

            logger.info(
                f"Loaded {self.broadcast_id} state: "
                f"tof={self.state[0]:.3f}ms, n_updates={self.n_updates}"
            )
            return True
            
        except Exception as e:
            logger.error(f"Failed to load state for {self.broadcast_id}: {e}")
            return False
