#!/usr/bin/env python3
"""
Arrival Pattern Matrix - Physics-Based Expected Arrival Predictions

================================================================================
DESIGN PHILOSOPHY
================================================================================

This module implements a fundamental architectural principle: validation against
PHYSICS, not against HISTORY.

Before the radio is turned on, we know:
1. Receiver location (lat/lon)
2. Station locations (WWV, WWVH, CHU, BPM)
3. Great circle distances
4. IRI-2020 ionospheric model predictions
5. Frequency-dependent propagation characteristics

From these, we can compute an EXPECTED ARRIVAL MATRIX that predicts where each
tone should appear (in samples from minute boundary) for any given UTC time.

CRITICAL INSIGHT:
-----------------
The expected arrival pattern is DETERMINISTIC given:
- Geography (fixed)
- Frequency (known per channel)
- UTC time (from GPSDO via NTP at bootstrap)
- Ionospheric model (IRI-2020, updated in real-time)

We do NOT need historical measurements to know where to look for tones.
Historical data is for ARCHIVAL and POST-HOC ANALYSIS, not for operational
decisions.

================================================================================
THE ARRIVAL PATTERN MATRIX
================================================================================

For each (station, frequency) pair, the matrix provides:

    expected_arrival_samples: int   # Samples from minute boundary
    expected_delay_ms: float        # Propagation delay in milliseconds
    uncertainty_3sigma_ms: float    # 3-sigma search window
    min_search_sample: int          # Lower bound of search window
    max_search_sample: int          # Upper bound of search window

The matrix is recomputed:
- At startup (before radio is turned on)
- Every minute (to track diurnal ionospheric changes)
- On demand (if ionospheric conditions change significantly)

================================================================================
VALIDATION FLOW
================================================================================

1. BOOTSTRAP PHASE:
   - NTP provides initial minute identification (one-time orientation)
   - Matrix provides expected tone positions
   - Detections within ±3σ of matrix predictions are accepted
   - Lock when sufficient detections match predictions

2. LOCKED PHASE:
   - Each minute, matrix is updated with current ionospheric model
   - Detections are validated against matrix (not against previous detections)
   - Outliers (outside ±3σ) are rejected immediately
   - Valid detections refine the ionospheric model for next minute

3. NO HISTORICAL CONTAMINATION:
   - Each minute starts fresh from physics
   - No calibration offsets persisted from previous sessions
   - No dependence on L1/L2 data from previous time periods

================================================================================
USAGE
================================================================================

    from hf_timestd.core.arrival_pattern_matrix import ArrivalPatternMatrix
    
    # Initialize before radio starts
    matrix = ArrivalPatternMatrix(
        receiver_lat=38.92,
        receiver_lon=-92.13,
        sample_rate=24000
    )
    
    # Get expected arrivals for current minute
    arrivals = matrix.get_expected_arrivals(utc_time=datetime.now(timezone.utc))
    
    # Validate a detection
    is_valid, confidence = matrix.validate_detection(
        station='WWV',
        frequency_mhz=10.0,
        detected_sample=720,  # samples from minute boundary
        snr_db=25.0
    )

================================================================================
REVISION HISTORY
================================================================================
2026-01-29: Initial implementation - physics-based validation architecture
"""

import logging
import math
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple, NamedTuple
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


# =============================================================================
# CONSTANTS
# =============================================================================

# Speed of light
C_LIGHT_KM_S = 299792.458  # km/s
C_LIGHT_KM_MS = 299.792458  # km/ms

# Sample rate (default, can be overridden)
DEFAULT_SAMPLE_RATE = 24000  # Hz

# Samples per minute at default rate
SAMPLES_PER_MINUTE = DEFAULT_SAMPLE_RATE * 60  # 1,440,000

# Station locations (lat, lon in degrees)
# These are FIXED - geography doesn't change
STATION_LOCATIONS = {
    'WWV': (40.6781, -105.0469),   # Fort Collins, Colorado
    'WWVH': (21.9886, -159.7642),  # Kekaha, Kauai, Hawaii
    'CHU': (45.2950, -75.7533),    # Ottawa, Canada
    'BPM': (34.9500, 109.5500),    # Pucheng, China
}

# Broadcast frequencies (MHz) per station
STATION_FREQUENCIES = {
    'WWV': [2.5, 5.0, 10.0, 15.0, 20.0, 25.0],
    'WWVH': [2.5, 5.0, 10.0, 15.0],
    'CHU': [3.33, 7.85, 14.67],
    'BPM': [2.5, 5.0, 10.0, 15.0],
}

# Default ionospheric uncertainty (3-sigma) in milliseconds
# This is the search window half-width
DEFAULT_UNCERTAINTY_3SIGMA_MS = 15.0  # ±15ms covers most ionospheric variation

# Bootstrap window parameters
# Initial window is wide to account for NTP uncertainty at startup
BOOTSTRAP_INITIAL_UNCERTAINTY_MS = 50.0  # ±50ms during initial bootstrap
BOOTSTRAP_MIN_UNCERTAINTY_MS = 5.0       # Minimum window (propagation floor)

# Window narrowing parameters
WINDOW_NARROWING_ALPHA = 0.1  # Exponential smoothing factor for variance tracking
WINDOW_CONFIDENCE_THRESHOLD = 0.8  # Confidence needed to start narrowing


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class BroadcastWindowState:
    """
    Dynamic window state for a single (station, frequency) broadcast.
    
    Tracks observed propagation variance and adjusts window width accordingly.
    Window can NARROW or WIDEN based on observed variance, but NEVER exceeds
    the initial physics-based maximum.
    
    Design rationale:
    - Strong signal with stable path → window narrows → better SNR/sensitivity
    - Strong signal with rapid path changes → window widens → track variations
    - Initial window is physics maximum (accounts for NTP uncertainty at bootstrap)
    - Minimum window is propagation floor (ionospheric jitter limit)
    """
    station: str
    frequency_mhz: float
    
    # Initial (maximum) window from physics - NEVER exceeded
    initial_uncertainty_ms: float
    
    # Current dynamic window (adapts to observed variance, capped at initial)
    current_uncertainty_ms: float
    
    # Tracking statistics
    observed_variance_ms2: float = 0.0  # Running variance estimate
    observation_count: int = 0
    last_deviation_ms: float = 0.0
    confidence: float = 0.0  # 0-1, increases with consistent detections
    
    def update_with_observation(self, deviation_ms: float, snr_db: float):
        """
        Update window state with a new observation.
        
        Window adapts to observed propagation variance:
        - Narrows when variance is low (stable path)
        - Widens when variance is high (rapid path changes)
        - Never exceeds initial physics maximum
        - Never goes below minimum propagation floor
        
        Args:
            deviation_ms: Observed deviation from expected arrival
            snr_db: Signal-to-noise ratio of detection
        """
        self.observation_count += 1
        self.last_deviation_ms = deviation_ms
        
        # Update running variance estimate (exponential smoothing)
        if self.observation_count == 1:
            self.observed_variance_ms2 = deviation_ms ** 2
        else:
            self.observed_variance_ms2 = (
                (1 - WINDOW_NARROWING_ALPHA) * self.observed_variance_ms2 +
                WINDOW_NARROWING_ALPHA * (deviation_ms ** 2)
            )
        
        # Update confidence based on SNR and observation count
        snr_factor = min(1.0, snr_db / 20.0)  # Full confidence at 20dB
        consistency_factor = min(1.0, self.observation_count / 10.0)  # Full after 10 obs
        self.confidence = snr_factor * consistency_factor
        
        # Adapt window based on observed variance
        if self.confidence >= WINDOW_CONFIDENCE_THRESHOLD:
            # Compute window from observed variance: 3σ + margin
            observed_3sigma = 3.0 * math.sqrt(self.observed_variance_ms2)
            proposed_uncertainty = observed_3sigma + BOOTSTRAP_MIN_UNCERTAINTY_MS
            
            # Clamp to [minimum, initial_maximum]
            self.current_uncertainty_ms = max(
                BOOTSTRAP_MIN_UNCERTAINTY_MS,  # Floor: propagation jitter limit
                min(
                    self.initial_uncertainty_ms,  # Ceiling: physics maximum
                    proposed_uncertainty
                )
            )
    
    def get_search_window_ms(self) -> Tuple[float, float]:
        """Get current search window bounds in ms from expected."""
        return (-self.current_uncertainty_ms, self.current_uncertainty_ms)


@dataclass
class ExpectedArrival:
    """Expected arrival parameters for a single (station, frequency) pair."""
    station: str
    frequency_mhz: float
    
    # Expected arrival (samples from minute boundary)
    expected_sample: int
    expected_delay_ms: float
    
    # Search window (3-sigma bounds) - may be dynamically adjusted
    uncertainty_3sigma_ms: float
    min_search_sample: int
    max_search_sample: int
    
    # Initial (maximum) window from physics - NEVER exceeded
    initial_uncertainty_ms: float = field(default=BOOTSTRAP_INITIAL_UNCERTAINTY_MS)
    
    # Model metadata
    great_circle_km: float = 0.0
    ionospheric_height_km: float = 0.0
    num_hops: int = 1
    model_tier: str = 'Static'  # 'IRI-2020', 'Parametric', 'Static'
    
    def contains_sample(self, sample: int) -> bool:
        """Check if a sample falls within the search window."""
        return self.min_search_sample <= sample <= self.max_search_sample
    
    def deviation_sigma(self, sample: int, sample_rate: int = DEFAULT_SAMPLE_RATE) -> float:
        """Calculate how many sigma a sample deviates from expected."""
        sample_ms = sample * 1000 / sample_rate
        deviation_ms = abs(sample_ms - self.expected_delay_ms)
        sigma_ms = self.uncertainty_3sigma_ms / 3.0
        return deviation_ms / sigma_ms if sigma_ms > 0 else float('inf')


@dataclass
class ArrivalMatrix:
    """Complete arrival pattern matrix for all stations and frequencies."""
    timestamp: datetime
    receiver_lat: float
    receiver_lon: float
    sample_rate: int
    
    # Arrivals indexed by (station, frequency_mhz)
    arrivals: Dict[Tuple[str, float], ExpectedArrival] = field(default_factory=dict)
    
    # Model metadata
    ionospheric_model_tier: str = 'Static'
    solar_flux_f107: Optional[float] = None
    
    def get_arrival(self, station: str, frequency_mhz: float) -> Optional[ExpectedArrival]:
        """Get expected arrival for a specific station/frequency."""
        return self.arrivals.get((station, frequency_mhz))
    
    def get_station_arrivals(self, station: str) -> List[ExpectedArrival]:
        """Get all expected arrivals for a station (all frequencies)."""
        return [a for (s, f), a in self.arrivals.items() if s == station]
    
    def get_frequency_arrivals(self, frequency_mhz: float, tolerance_mhz: float = 0.1) -> List[ExpectedArrival]:
        """Get all expected arrivals for a frequency (all stations)."""
        return [a for (s, f), a in self.arrivals.items() 
                if abs(f - frequency_mhz) < tolerance_mhz]


# =============================================================================
# ARRIVAL PATTERN MATRIX
# =============================================================================

class ArrivalPatternMatrix:
    """
    Physics-based expected arrival predictions.
    
    Computes where each tone should appear based on:
    - Geography (receiver and station locations)
    - Frequency (affects ionospheric reflection height)
    - UTC time (affects ionospheric conditions via IRI-2020)
    
    NO historical data is used for predictions. Each computation is fresh
    from physics.
    """
    
    def __init__(
        self,
        receiver_lat: float,
        receiver_lon: float,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        enable_iri: bool = True,
        default_uncertainty_3sigma_ms: float = DEFAULT_UNCERTAINTY_3SIGMA_MS
    ):
        """
        Initialize the arrival pattern matrix.
        
        Args:
            receiver_lat: Receiver latitude (degrees)
            receiver_lon: Receiver longitude (degrees)
            sample_rate: Sample rate in Hz
            enable_iri: Whether to attempt IRI-2020 model (falls back to parametric)
            default_uncertainty_3sigma_ms: Default 3-sigma uncertainty for search window
        """
        self.receiver_lat = receiver_lat
        self.receiver_lon = receiver_lon
        self.sample_rate = sample_rate
        self.enable_iri = enable_iri
        self.default_uncertainty_3sigma_ms = default_uncertainty_3sigma_ms
        
        # Bootstrap state - starts wide, narrows with confidence
        self._is_locked = False
        self._lock_confidence = 0.0  # 0-1, increases as GPSDO stability is confirmed
        
        # Per-broadcast dynamic window states
        # Key: (station, frequency_mhz)
        # These track observed variance and allow windows to narrow
        self._broadcast_windows: Dict[Tuple[str, float], BroadcastWindowState] = {}
        
        # Pre-compute great circle distances (these never change)
        self.great_circle_distances: Dict[str, float] = {}
        for station, (lat, lon) in STATION_LOCATIONS.items():
            self.great_circle_distances[station] = self._haversine_km(
                self.receiver_lat, self.receiver_lon, lat, lon
            )
        
        # Try to import ionospheric model
        self._iono_model = None
        if enable_iri:
            try:
                from .ionospheric_model import IonosphericModel
                self._iono_model = IonosphericModel(enable_iri=True)
                logger.info("ArrivalPatternMatrix: IRI-2020 ionospheric model available")
            except ImportError:
                logger.warning("ArrivalPatternMatrix: IonosphericModel not available, using parametric fallback")
        
        # Current matrix (recomputed each minute)
        self._current_matrix: Optional[ArrivalMatrix] = None
        
        # Initialize broadcast window states for all station/frequency pairs
        self._init_broadcast_windows()
        
        logger.info(f"ArrivalPatternMatrix initialized for receiver at ({receiver_lat:.4f}, {receiver_lon:.4f})")
        for station, dist in self.great_circle_distances.items():
            logger.info(f"  {station}: {dist:.0f} km great circle")
    
    @staticmethod
    def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Calculate great circle distance using Haversine formula."""
        lat1_rad = math.radians(lat1)
        lat2_rad = math.radians(lat2)
        delta_lat = math.radians(lat2 - lat1)
        delta_lon = math.radians(lon2 - lon1)
        
        a = (math.sin(delta_lat / 2) ** 2 +
             math.cos(lat1_rad) * math.cos(lat2_rad) *
             math.sin(delta_lon / 2) ** 2)
        c = 2 * math.asin(math.sqrt(a))
        
        earth_radius_km = 6371.0
        return earth_radius_km * c
    
    def _init_broadcast_windows(self):
        """Initialize per-broadcast window states with wide bootstrap windows."""
        for station, frequencies in STATION_FREQUENCIES.items():
            for freq_mhz in frequencies:
                key = (station, freq_mhz)
                self._broadcast_windows[key] = BroadcastWindowState(
                    station=station,
                    frequency_mhz=freq_mhz,
                    initial_uncertainty_ms=BOOTSTRAP_INITIAL_UNCERTAINTY_MS,
                    current_uncertainty_ms=BOOTSTRAP_INITIAL_UNCERTAINTY_MS
                )
        logger.info(f"Initialized {len(self._broadcast_windows)} broadcast windows "
                   f"(initial ±{BOOTSTRAP_INITIAL_UNCERTAINTY_MS}ms)")
    
    def set_locked(self, locked: bool, confidence: float = 1.0):
        """
        Set bootstrap lock state.
        
        Args:
            locked: Whether bootstrap has achieved lock
            confidence: Lock confidence (0-1)
        """
        was_locked = self._is_locked
        self._is_locked = locked
        self._lock_confidence = confidence
        
        if locked and not was_locked:
            logger.info(f"ArrivalPatternMatrix: Bootstrap LOCKED (confidence={confidence:.2f})")
        elif not locked and was_locked:
            logger.info("ArrivalPatternMatrix: Bootstrap UNLOCKED - resetting windows to initial")
            # Reset all windows to initial width on unlock
            for state in self._broadcast_windows.values():
                state.current_uncertainty_ms = state.initial_uncertainty_ms
                state.observation_count = 0
                state.confidence = 0.0
    
    def get_current_uncertainty_ms(self, station: str, frequency_mhz: float) -> float:
        """Get current dynamic uncertainty for a broadcast."""
        key = (station, frequency_mhz)
        if key in self._broadcast_windows:
            return self._broadcast_windows[key].current_uncertainty_ms
        return BOOTSTRAP_INITIAL_UNCERTAINTY_MS
    
    def record_detection(
        self,
        station: str,
        frequency_mhz: float,
        detected_ms: float,
        expected_ms: float,
        snr_db: float
    ) -> float:
        """
        Record a valid detection to update window tracking.
        
        Call this after a detection passes validation to allow the window
        to adapt based on observed propagation variance.
        
        Args:
            station: Station name
            frequency_mhz: Frequency in MHz
            detected_ms: Detected arrival time in ms from minute boundary
            expected_ms: Expected arrival time in ms from minute boundary
            snr_db: Signal-to-noise ratio
            
        Returns:
            Current window uncertainty after update
        """
        key = (station, frequency_mhz)
        if key not in self._broadcast_windows:
            return BOOTSTRAP_INITIAL_UNCERTAINTY_MS
        
        deviation_ms = detected_ms - expected_ms
        state = self._broadcast_windows[key]
        old_uncertainty = state.current_uncertainty_ms
        
        state.update_with_observation(deviation_ms, snr_db)
        
        # Log significant window changes
        if abs(state.current_uncertainty_ms - old_uncertainty) > 1.0:
            logger.info(f"Window {station}@{frequency_mhz}MHz: "
                       f"±{old_uncertainty:.1f}ms → ±{state.current_uncertainty_ms:.1f}ms "
                       f"(obs={state.observation_count}, var={state.observed_variance_ms2:.1f}ms²)")
        
        return state.current_uncertainty_ms
    
    def get_window_stats(self) -> Dict[str, Dict]:
        """Get current window statistics for all broadcasts."""
        stats = {}
        for (station, freq), state in self._broadcast_windows.items():
            key = f"{station}@{freq}MHz"
            stats[key] = {
                'initial_ms': state.initial_uncertainty_ms,
                'current_ms': state.current_uncertainty_ms,
                'variance_ms2': state.observed_variance_ms2,
                'observations': state.observation_count,
                'confidence': state.confidence
            }
        return stats
    
    def _get_ionospheric_height_km(
        self,
        frequency_mhz: float,
        utc_time: datetime,
        midpoint_lat: float,
        midpoint_lon: float
    ) -> Tuple[float, str]:
        """
        Get ionospheric reflection height for a frequency at a given time/location.
        
        Returns:
            Tuple of (height_km, model_tier)
        """
        # Try IRI-2020 first
        if self._iono_model is not None:
            try:
                heights = self._iono_model.get_layer_heights(
                    timestamp=utc_time,
                    latitude=midpoint_lat,
                    longitude=midpoint_lon
                )
                # Use F2 layer for HF propagation
                return heights.hmF2, heights.tier.value
            except Exception as e:
                logger.debug(f"IRI-2020 failed, using parametric: {e}")
        
        # Parametric fallback based on frequency and time of day
        hour = utc_time.hour + utc_time.minute / 60.0
        
        # Base height varies with frequency (lower freq → higher reflection)
        if frequency_mhz <= 5:
            base_height = 320.0
        elif frequency_mhz <= 10:
            base_height = 300.0
        elif frequency_mhz <= 15:
            base_height = 280.0
        else:
            base_height = 260.0
        
        # Diurnal variation: higher at night (ionization decays)
        # Simple sinusoidal model centered on local noon
        local_hour = (hour + midpoint_lon / 15.0) % 24
        diurnal_phase = (local_hour - 14.0) / 24.0 * 2 * math.pi  # Peak at 14:00 local
        diurnal_variation = 30.0 * math.cos(diurnal_phase)  # ±30 km
        
        height_km = base_height + diurnal_variation
        return height_km, 'Parametric'
    
    def _compute_propagation_delay_ms(
        self,
        distance_km: float,
        height_km: float
    ) -> Tuple[float, int]:
        """
        Compute propagation delay for ionospheric path.
        
        Returns:
            Tuple of (delay_ms, num_hops)
        """
        # Determine number of hops based on distance
        # Single hop typically covers up to ~2000 km
        if distance_km < 500:
            # Ground wave or single very short hop
            path_length = distance_km
            num_hops = 1
        elif distance_km < 2500:
            # Single hop
            hop_distance = distance_km
            path_length = 2 * math.sqrt((hop_distance / 2) ** 2 + height_km ** 2)
            num_hops = 1
        else:
            # Multi-hop
            num_hops = max(2, int(distance_km / 2000))
            hop_distance = distance_km / num_hops
            single_hop_path = 2 * math.sqrt((hop_distance / 2) ** 2 + height_km ** 2)
            path_length = num_hops * single_hop_path
        
        delay_ms = path_length / C_LIGHT_KM_MS
        return delay_ms, num_hops
    
    def compute_matrix(self, utc_time: datetime) -> ArrivalMatrix:
        """
        Compute the arrival pattern matrix for a given UTC time.
        
        This is the core method that produces physics-based predictions
        for where each tone should appear.
        
        Args:
            utc_time: UTC time for ionospheric model
            
        Returns:
            ArrivalMatrix with expected arrivals for all station/frequency pairs
        """
        matrix = ArrivalMatrix(
            timestamp=utc_time,
            receiver_lat=self.receiver_lat,
            receiver_lon=self.receiver_lon,
            sample_rate=self.sample_rate
        )
        
        model_tiers_used = set()
        
        for station, frequencies in STATION_FREQUENCIES.items():
            station_lat, station_lon = STATION_LOCATIONS[station]
            distance_km = self.great_circle_distances[station]
            
            # Midpoint for ionospheric model
            midpoint_lat = (self.receiver_lat + station_lat) / 2
            midpoint_lon = (self.receiver_lon + station_lon) / 2
            
            for freq_mhz in frequencies:
                # Get ionospheric height
                height_km, model_tier = self._get_ionospheric_height_km(
                    freq_mhz, utc_time, midpoint_lat, midpoint_lon
                )
                model_tiers_used.add(model_tier)
                
                # Compute propagation delay
                delay_ms, num_hops = self._compute_propagation_delay_ms(distance_km, height_km)
                
                # Convert to samples
                expected_sample = int(delay_ms * self.sample_rate / 1000)
                
                # Get dynamic window width for this broadcast
                # Uses tracked variance if available, otherwise initial width
                current_uncertainty_ms = self.get_current_uncertainty_ms(station, freq_mhz)
                
                # Search window (dynamic bounds based on observed variance)
                uncertainty_samples = int(current_uncertainty_ms * self.sample_rate / 1000)
                min_sample = max(0, expected_sample - uncertainty_samples)
                max_sample = expected_sample + uncertainty_samples
                
                arrival = ExpectedArrival(
                    station=station,
                    frequency_mhz=freq_mhz,
                    expected_sample=expected_sample,
                    expected_delay_ms=delay_ms,
                    uncertainty_3sigma_ms=current_uncertainty_ms,
                    min_search_sample=min_sample,
                    max_search_sample=max_sample,
                    initial_uncertainty_ms=BOOTSTRAP_INITIAL_UNCERTAINTY_MS,
                    great_circle_km=distance_km,
                    ionospheric_height_km=height_km,
                    num_hops=num_hops,
                    model_tier=model_tier
                )
                
                matrix.arrivals[(station, freq_mhz)] = arrival
        
        # Set overall model tier (use highest available)
        if 'IRI-2020' in model_tiers_used:
            matrix.ionospheric_model_tier = 'IRI-2020'
        elif 'Parametric' in model_tiers_used:
            matrix.ionospheric_model_tier = 'Parametric'
        else:
            matrix.ionospheric_model_tier = 'Static'
        
        self._current_matrix = matrix
        return matrix
    
    def get_expected_arrivals(self, utc_time: Optional[datetime] = None) -> ArrivalMatrix:
        """
        Get expected arrivals for a given time, computing if necessary.
        
        Args:
            utc_time: UTC time (default: now)
            
        Returns:
            ArrivalMatrix with expected arrivals
        """
        if utc_time is None:
            utc_time = datetime.now(timezone.utc)
        
        # Recompute if no matrix or if time has changed significantly (>1 minute)
        if (self._current_matrix is None or 
            abs((utc_time - self._current_matrix.timestamp).total_seconds()) > 60):
            return self.compute_matrix(utc_time)
        
        return self._current_matrix
    
    def validate_detection(
        self,
        station: str,
        frequency_mhz: float,
        detected_sample: int,
        snr_db: float,
        utc_time: Optional[datetime] = None
    ) -> Tuple[bool, float, str]:
        """
        Validate a detection against the expected arrival matrix.
        
        Args:
            station: Station name (WWV, WWVH, CHU, BPM)
            frequency_mhz: Frequency in MHz
            detected_sample: Detected sample offset from minute boundary
            snr_db: Signal-to-noise ratio in dB
            utc_time: UTC time for matrix (default: now)
            
        Returns:
            Tuple of (is_valid, confidence, reason)
            - is_valid: True if detection is within 3-sigma of expected
            - confidence: 0.0-1.0 based on deviation and SNR
            - reason: Human-readable explanation
        """
        matrix = self.get_expected_arrivals(utc_time)
        arrival = matrix.get_arrival(station, frequency_mhz)
        
        if arrival is None:
            return False, 0.0, f"No expected arrival for {station} @ {frequency_mhz} MHz"
        
        detected_ms = detected_sample * 1000 / self.sample_rate
        
        # Check if within search window (using current dynamic window)
        if not arrival.contains_sample(detected_sample):
            deviation = arrival.deviation_sigma(detected_sample, self.sample_rate)
            return False, 0.0, f"Outside ±{arrival.uncertainty_3sigma_ms:.0f}ms window: {deviation:.1f}σ deviation"
        
        # Calculate confidence based on deviation and SNR
        deviation_sigma = arrival.deviation_sigma(detected_sample, self.sample_rate)
        
        # Deviation factor: 1.0 at 0σ, 0.0 at 3σ
        deviation_factor = max(0.0, 1.0 - deviation_sigma / 3.0)
        
        # SNR factor: sigmoid centered at 10 dB
        snr_factor = 1.0 / (1.0 + math.exp(-(snr_db - 10.0) / 5.0))
        
        confidence = deviation_factor * snr_factor
        
        # Record this valid detection to update window tracking
        # This allows windows to adapt based on observed propagation variance
        self.record_detection(
            station=station,
            frequency_mhz=frequency_mhz,
            detected_ms=detected_ms,
            expected_ms=arrival.expected_delay_ms,
            snr_db=snr_db
        )
        
        reason = (f"Valid: {detected_ms:.1f}ms vs expected {arrival.expected_delay_ms:.1f}ms "
                 f"({deviation_sigma:.1f}σ, SNR={snr_db:.1f}dB, window=±{arrival.uncertainty_3sigma_ms:.0f}ms)")
        
        return True, confidence, reason
    
    def get_search_windows(
        self,
        frequency_mhz: float,
        utc_time: Optional[datetime] = None,
        tolerance_mhz: float = 0.1
    ) -> Dict[str, Tuple[int, int]]:
        """
        Get search windows for all stations at a given frequency.
        
        Useful for tone detection: search only within these windows.
        
        Args:
            frequency_mhz: Frequency in MHz
            utc_time: UTC time for matrix
            tolerance_mhz: Frequency matching tolerance
            
        Returns:
            Dict mapping station name to (min_sample, max_sample) search window
        """
        matrix = self.get_expected_arrivals(utc_time)
        arrivals = matrix.get_frequency_arrivals(frequency_mhz, tolerance_mhz)
        
        return {
            a.station: (a.min_search_sample, a.max_search_sample)
            for a in arrivals
        }
    
    def log_matrix_summary(self, utc_time: Optional[datetime] = None):
        """Log a summary of the current arrival matrix."""
        matrix = self.get_expected_arrivals(utc_time)
        
        logger.info(f"Arrival Pattern Matrix @ {matrix.timestamp.strftime('%Y-%m-%d %H:%M:%S')} UTC")
        logger.info(f"  Receiver: ({matrix.receiver_lat:.4f}, {matrix.receiver_lon:.4f})")
        logger.info(f"  Model: {matrix.ionospheric_model_tier}")
        
        for station in ['WWV', 'WWVH', 'CHU', 'BPM']:
            arrivals = matrix.get_station_arrivals(station)
            if arrivals:
                delays = [f"{a.frequency_mhz:.1f}MHz:{a.expected_delay_ms:.1f}ms" for a in arrivals]
                logger.info(f"  {station}: {', '.join(delays)}")
