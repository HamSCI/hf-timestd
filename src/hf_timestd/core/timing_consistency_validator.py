#!/usr/bin/env python3
"""
Timing Consistency Validator - Multi-Constraint Timing Validation

================================================================================
DESIGN PHILOSOPHY
================================================================================

This module exploits ALL known timing constraints to improve detection confidence
and reject false positives. Given stable underlying timing (GPSDO-locked RTP),
we can validate detections using multiple independent constraints:

INTRA-MINUTE CONSTRAINTS (within a single minute buffer):
---------------------------------------------------------
1. ARRIVAL SEQUENCE: Stations at different distances must arrive in order
   - WWV (~1120 km) arrives before WWVH (~6600 km)
   - CHU (~1520 km) arrives between WWV and WWVH
   - BPM (~11000 km) arrives last

2. CROSS-STATION CONSISTENCY: All stations transmit at UTC second 0
   - T_emission = T_arrival - T_propagation should agree within ±5ms
   - Disagreement indicates wrong station attribution or multipath

3. CROSS-FREQUENCY IONOSPHERIC DISPERSION (same station, multiple frequencies):
   - Ionospheric delay follows 1/f² law: τ(f) = K·TEC/f²
   - Higher frequencies arrive earlier than lower frequencies
   - Delay difference: Δτ = K·TEC·(1/f₁² - 1/f₂²)
   - For WWV 5 MHz vs 15 MHz with TEC=20 TECU: Δτ ≈ 0.5ms

INTER-MINUTE CONSTRAINTS (across consecutive minutes):
------------------------------------------------------
4. SAMPLE INTERVAL STABILITY: Exactly 1,440,000 samples between minutes
   - RTP timestamp difference should be 1,440,000 ± 1 sample
   - Deviation indicates buffer gap or clock anomaly

5. ARRIVAL TIME STABILITY: Same broadcast arrives at consistent offset
   - Ionospheric variation is typically ±5ms over minutes
   - Large jumps (>20ms) indicate detection error or mode change

6. DIFFERENTIAL ARRIVAL STABILITY: Difference between stations is stable
   - (T_wwv - T_wwvh) should be stable to ±2ms over short periods
   - Removes common-mode ionospheric effects

================================================================================
USAGE
================================================================================

    from hf_timestd.core.timing_consistency_validator import TimingConsistencyValidator
    
    validator = TimingConsistencyValidator(
        receiver_lat=38.92,
        receiver_lon=-92.13,
        sample_rate=24000
    )
    
    # Validate a set of detections for one minute
    result = validator.validate_minute(
        minute_boundary=1770088200,
        detections=[
            {'station': 'WWV', 'frequency_mhz': 10.0, 'arrival_ms': 3.8},
            {'station': 'WWVH', 'frequency_mhz': 10.0, 'arrival_ms': 22.1},
        ]
    )
    
    # Check inter-minute consistency
    validator.update_history(minute_boundary, detections)
    stability = validator.get_stability_metrics()

================================================================================
"""

import numpy as np
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
from datetime import datetime, timezone
import logging

logger = logging.getLogger(__name__)

# Physical constants
C_LIGHT_KM_MS = 299.792458  # km/ms
K_IONOSPHERE = 40.3 / C_LIGHT_KM_MS  # ms·MHz² / TECU (for group delay)
TECU_SCALE = 1e16  # 1 TECU = 10^16 el/m²

# Station locations (lat, lon)
STATION_LOCATIONS = {
    'WWV': (40.6781, -105.0469),   # Fort Collins, Colorado
    'WWVH': (21.9886, -159.7642),  # Kauai, Hawaii
    'CHU': (45.2925, -75.7542),    # Ottawa, Canada
    'BPM': (34.9500, 109.5500),    # Xi'an, China
}

# Expected arrival order (by increasing distance from central US)
EXPECTED_ARRIVAL_ORDER = ['WWV', 'CHU', 'WWVH', 'BPM']


@dataclass
class ValidationResult:
    """Result of timing consistency validation."""
    minute_boundary: int
    
    # Overall validity
    is_valid: bool
    confidence: float  # 0.0 - 1.0
    
    # Individual constraint results
    arrival_sequence_valid: bool = True
    arrival_sequence_violations: List[str] = field(default_factory=list)
    
    cross_station_valid: bool = True
    cross_station_error_ms: Optional[float] = None
    emission_times_ms: Dict[str, float] = field(default_factory=dict)
    
    cross_frequency_valid: bool = True
    tec_estimate_tecu: Optional[float] = None
    tec_residual_ms: Optional[float] = None
    
    sample_interval_valid: bool = True
    sample_interval_error: Optional[int] = None
    
    arrival_stability_valid: bool = True
    arrival_stability_violations: List[str] = field(default_factory=list)
    
    # Diagnostics
    n_constraints_checked: int = 0
    n_constraints_passed: int = 0
    failure_reasons: List[str] = field(default_factory=list)


@dataclass
class StabilityMetrics:
    """Inter-minute stability metrics."""
    n_minutes: int
    
    # Per-broadcast arrival stability
    arrival_mean_ms: Dict[str, float] = field(default_factory=dict)
    arrival_std_ms: Dict[str, float] = field(default_factory=dict)
    
    # Differential stability (removes common-mode)
    differential_mean_ms: Dict[str, float] = field(default_factory=dict)
    differential_std_ms: Dict[str, float] = field(default_factory=dict)
    
    # Sample interval stability
    sample_interval_mean: float = 1440000.0
    sample_interval_std: float = 0.0
    
    # TEC stability (if multi-frequency available)
    tec_mean_tecu: Optional[float] = None
    tec_std_tecu: Optional[float] = None


class TimingConsistencyValidator:
    """
    Multi-constraint timing validation using physics and geometry.
    
    Exploits all known timing relationships to validate detections
    and improve timing characterization.
    """
    
    def __init__(
        self,
        receiver_lat: float,
        receiver_lon: float,
        sample_rate: int = 24000,
        history_minutes: int = 60
    ):
        self.receiver_lat = receiver_lat
        self.receiver_lon = receiver_lon
        self.sample_rate = sample_rate
        self.samples_per_minute = sample_rate * 60
        self.history_minutes = history_minutes
        
        # Pre-compute station distances and expected delays
        self.station_distances_km = {}
        self.station_expected_delays_ms = {}
        for station, (lat, lon) in STATION_LOCATIONS.items():
            dist = self._great_circle_distance(
                self.receiver_lat, self.receiver_lon, lat, lon
            )
            self.station_distances_km[station] = dist
            # Simple ground wave delay (ionospheric path adds more)
            self.station_expected_delays_ms[station] = dist / C_LIGHT_KM_MS
        
        # Sort stations by distance for arrival order validation
        self.stations_by_distance = sorted(
            self.station_distances_km.keys(),
            key=lambda s: self.station_distances_km[s]
        )
        
        # History for inter-minute validation
        self.arrival_history: Dict[str, List[Tuple[int, float]]] = defaultdict(list)
        self.sample_interval_history: List[int] = []
        self.tec_history: List[Tuple[int, float]] = []
        self.last_rtp_timestamp: Optional[int] = None
        self.last_minute_boundary: Optional[int] = None
        
        # Thresholds
        self.cross_station_threshold_ms = 5.0  # Max emission time disagreement
        self.arrival_stability_threshold_ms = 20.0  # Max jump between minutes
        self.sample_interval_tolerance = 10  # samples
        
        logger.info(f"TimingConsistencyValidator initialized")
        logger.info(f"  Receiver: ({receiver_lat:.4f}, {receiver_lon:.4f})")
        logger.info(f"  Station distances: {self.station_distances_km}")
        logger.info(f"  Expected order: {self.stations_by_distance}")
    
    def _great_circle_distance(
        self, lat1: float, lon1: float, lat2: float, lon2: float
    ) -> float:
        """Calculate great circle distance in km using Haversine formula."""
        lat1, lon1, lat2, lon2 = map(np.radians, [lat1, lon1, lat2, lon2])
        dlat = lat2 - lat1
        dlon = lon2 - lon1
        a = np.sin(dlat/2)**2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon/2)**2
        c = 2 * np.arctan2(np.sqrt(a), np.sqrt(1-a))
        return 6371.0 * c  # Earth radius in km
    
    def validate_minute(
        self,
        minute_boundary: int,
        detections: List[Dict],
        rtp_timestamp: Optional[int] = None
    ) -> ValidationResult:
        """
        Validate all detections for a single minute using multiple constraints.
        
        Args:
            minute_boundary: Unix timestamp of minute start
            detections: List of dicts with keys:
                - station: str ('WWV', 'WWVH', 'CHU', 'BPM')
                - frequency_mhz: float
                - arrival_ms: float (measured arrival time from minute boundary)
                - snr_db: float (optional)
            rtp_timestamp: RTP timestamp at minute boundary (for sample interval check)
        
        Returns:
            ValidationResult with all constraint checks
        """
        result = ValidationResult(
            minute_boundary=minute_boundary,
            is_valid=True,
            confidence=1.0
        )
        
        if not detections:
            result.is_valid = False
            result.confidence = 0.0
            result.failure_reasons.append("No detections")
            return result
        
        # Group detections by station and frequency
        by_station: Dict[str, List[Dict]] = defaultdict(list)
        by_frequency: Dict[float, List[Dict]] = defaultdict(list)
        
        for det in detections:
            station = det.get('station')
            freq = det.get('frequency_mhz')
            if station and freq:
                by_station[station].append(det)
                by_frequency[freq].append(det)
        
        # 1. Check arrival sequence
        self._check_arrival_sequence(result, by_station)
        
        # 2. Check cross-station consistency
        self._check_cross_station_consistency(result, by_station)
        
        # 3. Check cross-frequency ionospheric dispersion
        self._check_cross_frequency_dispersion(result, by_station)
        
        # 4. Check sample interval (if RTP timestamp provided)
        if rtp_timestamp is not None:
            self._check_sample_interval(result, minute_boundary, rtp_timestamp)
        
        # 5. Check arrival stability (against history)
        self._check_arrival_stability(result, by_station)
        
        # Compute overall validity and confidence
        result.n_constraints_passed = sum([
            result.arrival_sequence_valid,
            result.cross_station_valid,
            result.cross_frequency_valid,
            result.sample_interval_valid,
            result.arrival_stability_valid
        ])
        
        result.is_valid = result.n_constraints_passed >= 3  # Majority must pass
        result.confidence = result.n_constraints_passed / max(result.n_constraints_checked, 1)
        
        return result
    
    def _check_arrival_sequence(
        self,
        result: ValidationResult,
        by_station: Dict[str, List[Dict]]
    ):
        """Check that stations arrive in expected order based on distance."""
        result.n_constraints_checked += 1
        
        detected_stations = list(by_station.keys())
        if len(detected_stations) < 2:
            return  # Can't check sequence with < 2 stations
        
        # Get arrival times for detected stations
        arrivals = {}
        for station, dets in by_station.items():
            # Use mean arrival if multiple frequencies
            arrivals[station] = np.mean([d['arrival_ms'] for d in dets])
        
        # Check pairwise ordering
        violations = []
        for i, s1 in enumerate(self.stations_by_distance):
            if s1 not in arrivals:
                continue
            for s2 in self.stations_by_distance[i+1:]:
                if s2 not in arrivals:
                    continue
                # s1 should arrive before s2 (s1 is closer)
                if arrivals[s1] > arrivals[s2] + 2.0:  # 2ms tolerance
                    violations.append(
                        f"{s1} ({arrivals[s1]:.1f}ms) arrived after "
                        f"{s2} ({arrivals[s2]:.1f}ms)"
                    )
        
        if violations:
            result.arrival_sequence_valid = False
            result.arrival_sequence_violations = violations
            result.failure_reasons.extend(violations)
    
    def _check_cross_station_consistency(
        self,
        result: ValidationResult,
        by_station: Dict[str, List[Dict]]
    ):
        """Check that all stations agree on emission time (T_arrival - T_propagation)."""
        result.n_constraints_checked += 1
        
        if len(by_station) < 2:
            return  # Can't cross-validate with < 2 stations
        
        emission_times = {}
        for station, dets in by_station.items():
            # Use mean arrival across frequencies
            mean_arrival = np.mean([d['arrival_ms'] for d in dets])
            expected_delay = self.station_expected_delays_ms.get(station, 0)
            
            # Add ionospheric path estimate (~1.1x ground wave for F-layer)
            iono_factor = 1.1
            expected_delay *= iono_factor
            
            emission_times[station] = mean_arrival - expected_delay
        
        result.emission_times_ms = emission_times
        
        # Check max disagreement
        if len(emission_times) >= 2:
            values = list(emission_times.values())
            max_error = max(values) - min(values)
            result.cross_station_error_ms = max_error
            
            if max_error > self.cross_station_threshold_ms:
                result.cross_station_valid = False
                result.failure_reasons.append(
                    f"Cross-station error {max_error:.1f}ms exceeds {self.cross_station_threshold_ms}ms"
                )
    
    def _check_cross_frequency_dispersion(
        self,
        result: ValidationResult,
        by_station: Dict[str, List[Dict]]
    ):
        """Check that multi-frequency arrivals follow 1/f² ionospheric dispersion."""
        result.n_constraints_checked += 1
        
        # Find stations with multiple frequencies
        for station, dets in by_station.items():
            frequencies = set(d['frequency_mhz'] for d in dets)
            if len(frequencies) < 2:
                continue
            
            # Build measurements for TEC estimation
            measurements = []
            for det in dets:
                measurements.append({
                    'frequency_hz': det['frequency_mhz'] * 1e6,
                    'toa_ms': det['arrival_ms']
                })
            
            # Fit TEC using 1/f² model
            # T_obs = T_vacuum + K·TEC/f²
            # Linear regression: y = T_obs, x = 1/f², slope = K·TEC
            x = np.array([1.0 / (m['frequency_hz']**2) for m in measurements])
            y = np.array([m['toa_ms'] for m in measurements])
            
            if len(x) >= 2:
                # Least squares fit
                A = np.vstack([x, np.ones(len(x))]).T
                try:
                    coeffs, residuals, _, _ = np.linalg.lstsq(A, y, rcond=None)
                    slope, intercept = coeffs
                    
                    # slope = K·TEC, solve for TEC
                    # K = 40.3 / c in ms·Hz² units
                    K_ms_hz2 = 40.3 / (C_LIGHT_KM_MS * 1e3)  # Convert to ms·Hz²
                    tec_electrons = slope / K_ms_hz2
                    tec_tecu = tec_electrons / TECU_SCALE
                    
                    result.tec_estimate_tecu = tec_tecu
                    
                    # Check residuals
                    y_pred = slope * x + intercept
                    rms_residual = np.sqrt(np.mean((y - y_pred)**2))
                    result.tec_residual_ms = rms_residual
                    
                    # Valid if residual is small and TEC is reasonable
                    if rms_residual > 2.0:  # > 2ms residual
                        result.cross_frequency_valid = False
                        result.failure_reasons.append(
                            f"Cross-frequency residual {rms_residual:.1f}ms too large"
                        )
                    elif tec_tecu < 0 or tec_tecu > 200:  # Unreasonable TEC
                        result.cross_frequency_valid = False
                        result.failure_reasons.append(
                            f"TEC estimate {tec_tecu:.1f} TECU out of range"
                        )
                except np.linalg.LinAlgError:
                    pass  # Can't fit, skip this check
    
    def _check_sample_interval(
        self,
        result: ValidationResult,
        minute_boundary: int,
        rtp_timestamp: int
    ):
        """Check that exactly 1,440,000 samples elapsed since last minute."""
        result.n_constraints_checked += 1
        
        if self.last_rtp_timestamp is None or self.last_minute_boundary is None:
            self.last_rtp_timestamp = rtp_timestamp
            self.last_minute_boundary = minute_boundary
            return
        
        # Check if this is a consecutive minute
        minute_diff = minute_boundary - self.last_minute_boundary
        if minute_diff != 60:
            # Not consecutive, reset
            self.last_rtp_timestamp = rtp_timestamp
            self.last_minute_boundary = minute_boundary
            return
        
        # Calculate sample interval
        # Handle RTP timestamp wraparound (32-bit)
        rtp_diff = (rtp_timestamp - self.last_rtp_timestamp) & 0xFFFFFFFF
        
        expected_samples = self.samples_per_minute
        sample_error = rtp_diff - expected_samples
        
        result.sample_interval_error = sample_error
        
        if abs(sample_error) > self.sample_interval_tolerance:
            result.sample_interval_valid = False
            result.failure_reasons.append(
                f"Sample interval error: {sample_error} samples "
                f"(expected {expected_samples}, got {rtp_diff})"
            )
        
        # Update history
        self.sample_interval_history.append(rtp_diff)
        if len(self.sample_interval_history) > self.history_minutes:
            self.sample_interval_history.pop(0)
        
        self.last_rtp_timestamp = rtp_timestamp
        self.last_minute_boundary = minute_boundary
    
    def _check_arrival_stability(
        self,
        result: ValidationResult,
        by_station: Dict[str, List[Dict]]
    ):
        """Check that arrival times are stable compared to recent history."""
        result.n_constraints_checked += 1
        
        violations = []
        
        for station, dets in by_station.items():
            mean_arrival = np.mean([d['arrival_ms'] for d in dets])
            
            # Check against history
            history = self.arrival_history.get(station, [])
            if len(history) >= 3:
                recent_arrivals = [arr for _, arr in history[-10:]]
                recent_mean = np.mean(recent_arrivals)
                
                jump = abs(mean_arrival - recent_mean)
                if jump > self.arrival_stability_threshold_ms:
                    violations.append(
                        f"{station} jumped {jump:.1f}ms from recent mean {recent_mean:.1f}ms"
                    )
        
        if violations:
            result.arrival_stability_valid = False
            result.arrival_stability_violations = violations
            result.failure_reasons.extend(violations)
    
    def update_history(
        self,
        minute_boundary: int,
        detections: List[Dict]
    ):
        """Update history with validated detections for inter-minute tracking."""
        for det in detections:
            station = det.get('station')
            arrival = det.get('arrival_ms')
            if station and arrival is not None:
                self.arrival_history[station].append((minute_boundary, arrival))
                # Trim history
                if len(self.arrival_history[station]) > self.history_minutes:
                    self.arrival_history[station].pop(0)
    
    def get_stability_metrics(self) -> StabilityMetrics:
        """Compute stability metrics from accumulated history."""
        metrics = StabilityMetrics(n_minutes=0)
        
        # Arrival stability per station
        for station, history in self.arrival_history.items():
            if len(history) >= 5:
                arrivals = [arr for _, arr in history]
                metrics.arrival_mean_ms[station] = np.mean(arrivals)
                metrics.arrival_std_ms[station] = np.std(arrivals)
                metrics.n_minutes = max(metrics.n_minutes, len(history))
        
        # Differential stability (e.g., WWV - WWVH)
        if 'WWV' in self.arrival_history and 'WWVH' in self.arrival_history:
            wwv_hist = dict(self.arrival_history['WWV'])
            wwvh_hist = dict(self.arrival_history['WWVH'])
            
            common_minutes = set(wwv_hist.keys()) & set(wwvh_hist.keys())
            if len(common_minutes) >= 5:
                diffs = [wwv_hist[m] - wwvh_hist[m] for m in common_minutes]
                metrics.differential_mean_ms['WWV-WWVH'] = np.mean(diffs)
                metrics.differential_std_ms['WWV-WWVH'] = np.std(diffs)
        
        # Sample interval stability
        if len(self.sample_interval_history) >= 5:
            metrics.sample_interval_mean = np.mean(self.sample_interval_history)
            metrics.sample_interval_std = np.std(self.sample_interval_history)
        
        # TEC stability
        if len(self.tec_history) >= 5:
            tecs = [tec for _, tec in self.tec_history]
            metrics.tec_mean_tecu = np.mean(tecs)
            metrics.tec_std_tecu = np.std(tecs)
        
        return metrics
    
    def log_validation_summary(self, result: ValidationResult):
        """Log a summary of validation results."""
        status = "✅ VALID" if result.is_valid else "❌ INVALID"
        logger.info(
            f"Timing validation {status} "
            f"({result.n_constraints_passed}/{result.n_constraints_checked} constraints, "
            f"confidence={result.confidence:.2f})"
        )
        
        if result.cross_station_error_ms is not None:
            logger.debug(f"  Cross-station error: {result.cross_station_error_ms:.2f}ms")
        
        if result.tec_estimate_tecu is not None:
            logger.debug(f"  TEC estimate: {result.tec_estimate_tecu:.1f} TECU")
        
        if result.sample_interval_error is not None:
            logger.debug(f"  Sample interval error: {result.sample_interval_error} samples")
        
        if result.failure_reasons:
            for reason in result.failure_reasons:
                logger.warning(f"  ⚠️ {reason}")
