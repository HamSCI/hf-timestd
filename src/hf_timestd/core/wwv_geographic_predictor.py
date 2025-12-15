"""
Geographic Time-of-Arrival Predictor for WWV/WWVH Discrimination

================================================================================
PURPOSE
================================================================================
Predict expected propagation delays from geographic path geometry, enabling:
    1. Single-peak classification when only one station is propagating
    2. Dual-peak assignment (which peak is WWV, which is WWVH)
    3. Cross-validation of measured delays against expected ranges
    4. Empirical refinement through historical ToA tracking

================================================================================
GEOGRAPHIC PATH GEOMETRY
================================================================================
HF radio signals travel via ionospheric reflection, following a curved path
that depends on transmitter/receiver locations and ionospheric conditions.

GREAT CIRCLE DISTANCE:
    The shortest path on Earth's surface between two points. Calculated using
    the Haversine formula:
    
    a = sin²(Δφ/2) + cos(φ₁) × cos(φ₂) × sin²(Δλ/2)
    c = 2 × arcsin(√a)
    d = R × c
    
    Where:
        φ = latitude (radians)
        λ = longitude (radians)
        R = Earth radius (6371 km)

IONOSPHERIC PATH LENGTH:
    The actual signal path is longer due to ionospheric reflection:
    
    Single-hop geometry (simplified):
    ┌─────────────────────────────────────────────────────────────────────────┐
    │                            Ionosphere                                   │
    │                        ~~~~~~~~~~~~~~~~~~~                              │
    │                      /                    \                             │
    │  TX  ─────────────  /   h = layer height   \  ─────────────  RX        │
    │     A            B                          C            D             │
    │                                                                         │
    │     └───────────── ground distance ──────────────┘                     │
    └─────────────────────────────────────────────────────────────────────────┘
    
    Path length = 2 × √((d/2)² + h²)
    
    For multi-hop: path = n_hops × single_hop_path

================================================================================
PROPAGATION DELAY MODEL
================================================================================
The propagation delay is:

    τ = L_path / c

Where:
    L_path = Total path length (km)
    c = Speed of light (299,792.458 km/s)

FREQUENCY-DEPENDENT LAYER HEIGHT:
    Lower frequencies reflect from higher layers (more ionized):
    
    Frequency   │ Layer   │ Height (km)
    ────────────┼─────────┼────────────
    ≤ 5 MHz     │ F2      │ ~320
    5-10 MHz    │ F2      │ ~300
    10-15 MHz   │ F2      │ ~280
    > 15 MHz    │ F2/F1   │ ~260

================================================================================
SINGLE-PEAK vs DUAL-PEAK CLASSIFICATION
================================================================================
DUAL-PEAK (both stations propagating):
    Δτ_geo = τ_WWV - τ_WWVH
    
    - If Δτ_geo < 0: WWV closer → early peak = WWV, late peak = WWVH
    - If Δτ_geo > 0: WWVH closer → early peak = WWVH, late peak = WWV

SINGLE-PEAK (one station propagating):
    Compare measured delay against expected ranges:
    
    - If delay ∈ [τ_WWV ± σ_WWV] AND delay ∉ [τ_WWVH ± σ_WWVH]: Station = WWV
    - If delay ∈ [τ_WWVH ± σ_WWVH] AND delay ∉ [τ_WWV ± σ_WWV]: Station = WWVH
    - If delay in both ranges: Ambiguous (need other discrimination)
    - If delay in neither range: Outlier (detection error)

================================================================================
EMPIRICAL REFINEMENT
================================================================================
The predictor maintains a history of ToA measurements to refine predictions:

    1. Initially use geometric model for expected delays
    2. As measurements accumulate, compute empirical mean and variance
    3. Use tighter ranges based on observed variance
    4. Confidence score increases with more measurements

This handles:
    - Systematic model errors (layer height assumptions)
    - Local ionospheric conditions
    - Seasonal/diurnal variations (by tracking recent history)

================================================================================
MAIDENHEAD GRID CONVERSION
================================================================================
Grid squares encode location with increasing precision:

    Characters │ Resolution   │ Example
    ───────────┼──────────────┼─────────
    2 (field)  │ 20° × 10°    │ EM
    4 (square) │ 2° × 1°      │ EM38
    6 (subsq)  │ 5' × 2.5'    │ EM38ww

Conversion formula (for 6-char grid):
    lon = (field[0] - 'A') × 20° - 180° + (square[2]) × 2° + (subsq[4] - 'A') × 5'
    lat = (field[1] - 'A') × 10° - 90° + (square[3]) × 1° + (subsq[5] - 'A') × 2.5'

================================================================================
USAGE
================================================================================
    predictor = WWVGeographicPredictor(
        receiver_grid='EM38ww',
        history_file=Path('/data/state/toa_history.json')
    )
    
    # Get expected delays
    expected = predictor.calculate_expected_delays(frequency_mhz=10.0)
    print(f"WWV expected: {expected['wwv_delay_ms']:.1f} ms")
    print(f"WWVH expected: {expected['wwvh_delay_ms']:.1f} ms")
    
    # Classify a single peak
    station = predictor.classify_single_peak(
        peak_delay_ms=25.0,
        peak_amplitude=0.8,
        frequency_mhz=10.0,
        correlation_quality=5.0
    )

================================================================================
REVISION HISTORY
================================================================================
2025-12-07: Added comprehensive documentation
2025-11-20: Added dual-peak classification
2025-10-15: Initial implementation with Haversine formula
"""

import math
import logging
from typing import Optional, Tuple, Dict, List
from datetime import datetime, timedelta
from collections import deque
import json
from pathlib import Path

# Issue 4.1 Fix (2025-12-07): Import coordinates from single source of truth
from .wwv_constants import WWV_LAT, WWV_LON, WWVH_LAT, WWVH_LON, BPM_LAT, BPM_LON


logger = logging.getLogger(__name__)


class WWVGeographicPredictor:
    """Predicts WWV/WWVH time-of-arrival based on geographic locations"""
    
    # Transmitter coordinates (lat, lon in degrees)
    # Issue 4.1 Fix: Now imported from wwv_constants.py (NIST verified)
    WWV_LOCATION = (WWV_LAT, WWV_LON)     # Fort Collins, Colorado - NIST verified
    WWVH_LOCATION = (WWVH_LAT, WWVH_LON)  # Kekaha, Kauai, Hawaii - NIST verified
    BPM_LOCATION = (BPM_LAT, BPM_LON)     # Pucheng, China - NTSC verified

    
    # Speed of light
    C_LIGHT_KM_PER_MS = 299.792458  # km/ms
    
    def __init__(
        self,
        receiver_grid: str,
        history_file: Optional[Path] = None,
        max_history: int = 1000
    ):
        """
        Initialize geographic predictor
        
        Args:
            receiver_grid: Maidenhead grid square (e.g., "EM38ww")
            history_file: Optional path to persist historical ToA measurements
            max_history: Maximum number of historical measurements to retain
        """
        self.receiver_grid = receiver_grid
        self.receiver_lat, self.receiver_lon = self.grid_to_latlon(receiver_grid)
        self.history_file = history_file
        self.max_history = max_history
        
        # Historical ToA measurements for empirical refinement
        # Structure: {frequency_mhz: deque([{time, peak_delay_ms, station}, ...])}
        self.toa_history: Dict[float, Dict[str, deque]] = {}
        
        # Load history if available
        if history_file and history_file.exists():
            self._load_history()
        
        logger.info(f"Geographic predictor initialized: {receiver_grid} "
                   f"({self.receiver_lat:.4f}°N, {self.receiver_lon:.4f}°E)")
        
        # Calculate and log baseline distances
        wwv_dist = self._haversine_distance(
            self.receiver_lat, self.receiver_lon, *self.WWV_LOCATION
        )
        wwvh_dist = self._haversine_distance(
            self.receiver_lat, self.receiver_lon, *self.WWVH_LOCATION
        )
        bpm_dist = self._haversine_distance(
            self.receiver_lat, self.receiver_lon, *self.BPM_LOCATION
        )
        logger.info(f"Great circle distances: WWV={wwv_dist:.0f}km, WWVH={wwvh_dist:.0f}km, BPM={bpm_dist:.0f}km")

    
    @staticmethod
    def grid_to_latlon(grid: str) -> Tuple[float, float]:
        """
        Convert Maidenhead grid square to latitude/longitude
        
        Supports 4-character (e.g., "EM38") or 6-character (e.g., "EM38ww") grids.
        
        Args:
            grid: Maidenhead grid square string
            
        Returns:
            Tuple of (latitude, longitude) in decimal degrees
        """
        grid = grid.upper()
        
        if len(grid) < 4:
            raise ValueError(f"Grid square too short: {grid}")
        
        # Field (first 2 chars): 20° longitude x 10° latitude
        lon = (ord(grid[0]) - ord('A')) * 20 - 180
        lat = (ord(grid[1]) - ord('A')) * 10 - 90
        
        # Square (next 2 chars): 2° longitude x 1° latitude
        lon += int(grid[2]) * 2
        lat += int(grid[3]) * 1
        
        # Subsquare (optional next 2 chars): 5' longitude x 2.5' latitude
        if len(grid) >= 6:
            lon += (ord(grid[4]) - ord('A')) * (2/24)
            lat += (ord(grid[5]) - ord('A')) * (1/24)
            # Center of subsquare
            lon += 1/24
            lat += 1/48
        else:
            # Center of square
            lon += 1
            lat += 0.5
        
        return lat, lon
    
    @staticmethod
    def _haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """
        Calculate great circle distance between two points using Haversine formula
        
        Args:
            lat1, lon1: First point (decimal degrees)
            lat2, lon2: Second point (decimal degrees)
            
        Returns:
            Distance in kilometers
        """
        # Convert to radians
        lat1_rad = math.radians(lat1)
        lat2_rad = math.radians(lat2)
        delta_lat = math.radians(lat2 - lat1)
        delta_lon = math.radians(lon2 - lon1)
        
        # Haversine formula
        a = (math.sin(delta_lat / 2) ** 2 +
             math.cos(lat1_rad) * math.cos(lat2_rad) *
             math.sin(delta_lon / 2) ** 2)
        c = 2 * math.asin(math.sqrt(a))
        
        # Earth radius in km
        earth_radius_km = 6371.0
        
        return earth_radius_km * c
    
    def _estimate_propagation_delay(
        self,
        distance_km: float,
        frequency_mhz: float
    ) -> float:
        """
        Estimate ionospheric propagation delay
        
        Uses simplified ionospheric model:
        - HF signals reflect from F2 layer (~300 km altitude typical)
        - Lower frequencies penetrate higher (longer path)
        - Path length ≈ geometric path with ionospheric reflection
        
        Args:
            distance_km: Great circle distance
            frequency_mhz: Operating frequency
            
        Returns:
            Estimated propagation delay in milliseconds
        """
        # Ionospheric reflection height (frequency-dependent)
        # Lower freq → higher reflection → longer path
        # Rough model: h = 250 + (20 - freq_mhz) * 5 km
        if frequency_mhz <= 5:
            reflection_height_km = 320
        elif frequency_mhz <= 10:
            reflection_height_km = 300
        elif frequency_mhz <= 15:
            reflection_height_km = 280
        else:  # 20-25 MHz
            reflection_height_km = 260
        
        # Single-hop path length (simplified triangulation)
        # For short distances: essentially straight line at altitude
        # For long distances: arc approximation
        if distance_km < 2000:
            # Single hop path length using Pythagorean theorem
            hop_distance = distance_km / 2
            path_length = 2 * math.sqrt(hop_distance**2 + reflection_height_km**2)
        else:
            # Multi-hop or longer paths - use approximation
            # Path ≈ great circle + altitude overhead
            num_hops = max(1, int(distance_km / 2000))
            hop_distance = distance_km / num_hops
            single_hop_path = 2 * math.sqrt((hop_distance/2)**2 + reflection_height_km**2)
            path_length = num_hops * single_hop_path
        
        # Propagation delay
        delay_ms = path_length / self.C_LIGHT_KM_PER_MS
        
        return delay_ms
    
    def calculate_expected_delays(
        self,
        frequency_mhz: float,
        use_history: bool = True
    ) -> Dict:
        """
        Calculate expected propagation delays for WWV and WWVH
        
        Args:
            frequency_mhz: Operating frequency
            use_history: Whether to refine predictions using historical data
            
        Returns:
            Dictionary with delay ranges and confidence:
            {
                'wwv_delay_ms': float,
                'wwvh_delay_ms': float,
                'wwv_range': (min_ms, max_ms),
                'wwvh_range': (min_ms, max_ms),
                'differential_delay_ms': float,
                'differential_range': (min_ms, max_ms),
                'confidence': float,  # 0-1
                'history_count': int
            }
        """
        # Calculate baseline geometric delays
        wwv_dist_km = self._haversine_distance(
            self.receiver_lat, self.receiver_lon, *self.WWV_LOCATION
        )
        wwvh_dist_km = self._haversine_distance(
            self.receiver_lat, self.receiver_lon, *self.WWVH_LOCATION
        )
        
        wwv_delay_ms = self._estimate_propagation_delay(wwv_dist_km, frequency_mhz)
        wwvh_delay_ms = self._estimate_propagation_delay(wwvh_dist_km, frequency_mhz)
        
        # Refine with historical data if available
        if use_history and frequency_mhz in self.toa_history:
            history = self.toa_history[frequency_mhz]
            wwv_delays = [m['peak_delay_ms'] for m in history.get('WWV', []) if m['peak_delay_ms'] is not None]
            wwvh_delays = [m['peak_delay_ms'] for m in history.get('WWVH', []) if m['peak_delay_ms'] is not None]
            bpm_delays = [m['peak_delay_ms'] for m in history.get('BPM', []) if m['peak_delay_ms'] is not None]
        
            if wwv_delays:
                wwv_delay_ms = sum(wwv_delays) / len(wwv_delays)
            if wwvh_delays:
                wwvh_delay_ms = sum(wwvh_delays) / len(wwvh_delays)
            # Currently ignoring BPM history for simple average until requested

        
        # Determine variance (empirical or default)
        wwv_variance, wwvh_variance, confidence = self._calculate_variance(frequency_mhz)
        
        # Build result
        differential_delay = abs(wwv_delay_ms - wwvh_delay_ms)
        differential_variance = math.sqrt(wwv_variance**2 + wwvh_variance**2)
        
        history_count = 0
        if frequency_mhz in self.toa_history:
            history_count = sum(
                len(self.toa_history[frequency_mhz].get(station, []))
                for station in ['WWV', 'WWVH']
            )
        
        return {
            'wwv_delay_ms': wwv_delay_ms,
            'wwvh_delay_ms': wwvh_delay_ms,
            'wwv_range': (wwv_delay_ms - wwv_variance, wwv_delay_ms + wwv_variance),
            'wwvh_range': (wwvh_delay_ms - wwvh_variance, wwvh_delay_ms + wwvh_variance),
            'differential_delay_ms': differential_delay,
            'differential_range': (
                max(0, differential_delay - differential_variance),
                differential_delay + differential_variance
            ),
            'confidence': confidence,
            'history_count': history_count
        }
    
    def _calculate_variance(self, frequency_mhz: float) -> Tuple[float, float, float]:
        """
        Calculate expected variance in ToA measurements
        
        Returns:
            Tuple of (wwv_variance_ms, wwvh_variance_ms, confidence)
        """
        if frequency_mhz not in self.toa_history:
            # No history - use conservative default (±5 ms)
            return 5.0, 5.0, 0.3
        
        history = self.toa_history[frequency_mhz]
        
        # Calculate empirical standard deviations
        wwv_delays = [m['peak_delay_ms'] for m in history.get('WWV', []) if m['peak_delay_ms'] is not None]
        wwvh_delays = [m['peak_delay_ms'] for m in history.get('WWVH', []) if m['peak_delay_ms'] is not None]
        
        if len(wwv_delays) < 10:
            wwv_var = 5.0
            wwv_conf = len(wwv_delays) / 10
        else:
            wwv_mean = sum(wwv_delays) / len(wwv_delays)
            wwv_var = math.sqrt(sum((d - wwv_mean)**2 for d in wwv_delays) / len(wwv_delays))
            wwv_var = max(2.0, min(wwv_var * 2, 10.0))  # 2σ, clamp to 2-10 ms
            wwv_conf = min(1.0, len(wwv_delays) / 100)
        
        if len(wwvh_delays) < 10:
            wwvh_var = 5.0
            wwvh_conf = len(wwvh_delays) / 10
        else:
            wwvh_mean = sum(wwvh_delays) / len(wwvh_delays)
            wwvh_var = math.sqrt(sum((d - wwvh_mean)**2 for d in wwvh_delays) / len(wwvh_delays))
            wwvh_var = max(2.0, min(wwvh_var * 2, 10.0))
            wwvh_conf = min(1.0, len(wwvh_delays) / 100)
        
        overall_conf = (wwv_conf + wwvh_conf) / 2
        
        return wwv_var, wwvh_var, overall_conf

    def calculate_expected_delay_bpm(self, frequency_mhz: float) -> float:
        """Calculate expected delay for BPM"""
        dist_km = self._haversine_distance(
            self.receiver_lat, self.receiver_lon, *self.BPM_LOCATION
        )
        return self._estimate_propagation_delay(dist_km, frequency_mhz)

    
    def classify_single_peak(
        self,
        peak_delay_ms: float,
        peak_amplitude: float,
        frequency_mhz: float,
        correlation_quality: float
    ) -> Optional[str]:
        """
        Classify a single correlation peak as WWV, WWVH, or unknown
        
        Args:
            peak_delay_ms: Measured peak delay from correlation zero
            peak_amplitude: Correlation peak amplitude
            frequency_mhz: Operating frequency
            correlation_quality: Quality metric from correlation
            
        Returns:
            'WWV', 'WWVH', or None if ambiguous/unclassifiable
        """
        expected = self.calculate_expected_delays(frequency_mhz)
        
        wwv_range = expected['wwv_range']
        wwvh_range = expected['wwvh_range']
        
        # Check if peak falls within expected ranges
        in_wwv_range = wwv_range[0] <= peak_delay_ms <= wwv_range[1]
        in_wwvh_range = wwvh_range[0] <= peak_delay_ms <= wwvh_range[1]
        
        # Quality threshold - only classify if correlation is strong
        if correlation_quality < 2.0:
            logger.debug(f"Peak quality too low ({correlation_quality:.1f}) for classification")
            return None
        
        # Unambiguous classification
        if in_wwv_range and not in_wwvh_range:
            self._update_history(frequency_mhz, 'WWV', peak_delay_ms, peak_amplitude)
            logger.info(f"Single peak classified as WWV: {peak_delay_ms:.2f}ms "
                       f"(expected {expected['wwv_delay_ms']:.2f}±{wwv_range[1]-expected['wwv_delay_ms']:.2f}ms)")
            return 'WWV'
        
        elif in_wwvh_range and not in_wwv_range:
            self._update_history(frequency_mhz, 'WWVH', peak_delay_ms, peak_amplitude)
            logger.info(f"Single peak classified as WWVH: {peak_delay_ms:.2f}ms "
                       f"(expected {expected['wwvh_delay_ms']:.2f}±{wwvh_range[1]-expected['wwvh_delay_ms']:.2f}ms)")
            return 'WWVH'
        
        elif in_wwv_range and in_wwvh_range:
            logger.debug(f"Peak ambiguous: {peak_delay_ms:.2f}ms matches both ranges")
            return None
        
        else:
            logger.debug(f"Peak outside expected ranges: {peak_delay_ms:.2f}ms "
                        f"(WWV: {wwv_range[0]:.1f}-{wwv_range[1]:.1f}ms, "
                        f"WWVH: {wwvh_range[0]:.1f}-{wwvh_range[1]:.1f}ms)")
            return None
    
    def classify_dual_peaks(
        self,
        peak_early_delay_ms: float,
        peak_late_delay_ms: float,
        peak_early_amplitude: float,
        peak_late_amplitude: float,
        frequency_mhz: float
    ) -> Tuple[str, str]:
        """
        Classify two correlation peaks as WWV and WWVH based on geographic ToA prediction.
        
        The differential delay Δτ_geo = ToA_WWV - ToA_WWVH determines which station
        arrives first:
        - If Δτ_geo < 0: WWV is closer → peak_early = WWV, peak_late = WWVH
        - If Δτ_geo > 0: WWVH is closer → peak_early = WWVH, peak_late = WWV
        
        This uses the receiver's location to calculate the geometric propagation
        delay to each transmitter, accounting for ionospheric reflection height.
        
        Args:
            peak_early_delay_ms: Delay of earlier-arriving peak (ms from correlation zero)
            peak_late_delay_ms: Delay of later-arriving peak (ms from correlation zero)
            peak_early_amplitude: Amplitude of earlier peak
            peak_late_amplitude: Amplitude of later peak
            frequency_mhz: Operating frequency for propagation estimation
            
        Returns:
            Tuple of (early_station, late_station) where each is 'WWV' or 'WWVH'
        """
        expected = self.calculate_expected_delays(frequency_mhz)
        
        wwv_delay = expected['wwv_delay_ms']
        wwvh_delay = expected['wwvh_delay_ms']
        
        # Δτ_geo = ToA_WWV - ToA_WWVH
        # Negative means WWV arrives first (is closer)
        delta_geo = wwv_delay - wwvh_delay
        
        if delta_geo < 0:
            # WWV is closer → arrives first (early peak)
            early_station = 'WWV'
            late_station = 'WWVH'
        else:
            # WWVH is closer → arrives first (early peak)
            early_station = 'WWVH'
            late_station = 'WWV'
        
        # Log the assignment with confidence information
        measured_diff = peak_late_delay_ms - peak_early_delay_ms
        expected_diff = abs(delta_geo)
        
        logger.debug(f"Geographic peak assignment: early={early_station}, late={late_station} "
                    f"(Δτ_geo={delta_geo:+.2f}ms, measured_diff={measured_diff:.2f}ms, "
                    f"expected_diff={expected_diff:.2f}ms)")
        
        # Validate: measured differential should be close to expected
        if expected_diff > 0:
            diff_error = abs(measured_diff - expected_diff) / expected_diff
            if diff_error > 0.5:  # >50% error
                logger.warning(f"Measured delay diff ({measured_diff:.2f}ms) differs "
                              f"significantly from expected ({expected_diff:.2f}ms)")
        
        # Update history with the assigned classifications
        self._update_history(frequency_mhz, early_station, peak_early_delay_ms, peak_early_amplitude)
        self._update_history(frequency_mhz, late_station, peak_late_delay_ms, peak_late_amplitude)
        
        return early_station, late_station
    
    def _update_history(
        self,
        frequency_mhz: float,
        station: str,
        peak_delay_ms: float,
        amplitude: float
    ):
        """Update historical ToA measurements"""
        if frequency_mhz not in self.toa_history:
            self.toa_history[frequency_mhz] = {'WWV': deque(maxlen=self.max_history),
                                                'WWVH': deque(maxlen=self.max_history)}
        
        self.toa_history[frequency_mhz][station].append({
            'timestamp': datetime.utcnow().isoformat(),
            'peak_delay_ms': peak_delay_ms,
            'amplitude': amplitude
        })
        
        # Persist if configured
        if self.history_file:
            self._save_history()
    
    def update_dual_peak_history(
        self,
        frequency_mhz: float,
        wwv_delay_ms: float,
        wwvh_delay_ms: float,
        wwv_amplitude: float,
        wwvh_amplitude: float
    ):
        """Update history from dual-peak measurements (existing BCD path)"""
        self._update_history(frequency_mhz, 'WWV', wwv_delay_ms, wwv_amplitude)
        self._update_history(frequency_mhz, 'WWVH', wwvh_delay_ms, wwv_amplitude)
    
    def _save_history(self):
        """Persist ToA history to file"""
        if not self.history_file:
            return
        
        try:
            # Convert deques to lists for JSON serialization
            history_data = {}
            for freq, stations in self.toa_history.items():
                history_data[str(freq)] = {
                    station: list(measurements)
                    for station, measurements in stations.items()
                }
            
            data = {
                'receiver_grid': self.receiver_grid,
                'last_updated': datetime.utcnow().isoformat(),
                'history': history_data
            }
            
            self.history_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.history_file, 'w') as f:
                json.dump(data, f, indent=2)
                
        except Exception as e:
            logger.error(f"Failed to save ToA history: {e}")
    
    def _load_history(self):
        """Load ToA history from file"""
        try:
            with open(self.history_file, 'r') as f:
                data = json.load(f)
            
            # Verify grid square matches
            if data.get('receiver_grid') != self.receiver_grid:
                logger.warning(f"Grid square mismatch in history file: "
                             f"{data.get('receiver_grid')} vs {self.receiver_grid}")
                return
            
            # Load history
            for freq_str, stations in data.get('history', {}).items():
                freq = float(freq_str)
                self.toa_history[freq] = {
                    'WWV': deque(stations.get('WWV', []), maxlen=self.max_history),
                    'WWVH': deque(stations.get('WWVH', []), maxlen=self.max_history)
                }
            
            logger.info(f"Loaded ToA history: {sum(len(s['WWV']) + len(s['WWVH']) for s in self.toa_history.values())} measurements")
            
        except Exception as e:
            logger.error(f"Failed to load ToA history: {e}")
