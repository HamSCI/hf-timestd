#!/usr/bin/env python3
"""
Transmission Time Solver - Back-Calculate UTC(NIST) from Observed Arrival Times

================================================================================
PURPOSE
================================================================================
This module implements the "Holy Grail" of HF time transfer: turning a passive
receiver into a PRIMARY frequency/time standard by back-calculating the actual
emission time at WWV/WWVH/CHU.

The fundamental equation for HF time transfer is:

    D_clock = T_arrival - T_propagation - T_emission

Where:
    D_clock = system clock offset from UTC(NIST)
    T_arrival = observed tone arrival time (from tone_detector)
    T_propagation = HF signal propagation delay (this module calculates)
    T_emission = 0 (tones transmitted at exact second boundary)

Therefore:
    D_clock = T_arrival - T_propagation

The challenge is that T_propagation depends on the ionospheric propagation
MODE, which must be identified from the signal characteristics.

================================================================================
THEORY: HF IONOSPHERIC PROPAGATION
================================================================================
HF radio waves (3-30 MHz) propagate via reflection from ionospheric layers:

    ┌──────────────────────────────────────────────────────────────────────┐
    │                         F2 Layer (250-400 km)                        │
    │                         ═══════════════════                          │
    │                    F1 Layer (200 km, daytime)                        │
    │                    ─────────────────────────                         │
    │               E Layer (100-120 km)                                   │
    │               ─────────────────────                                  │
    │          D Layer (60-90 km, absorbs)                                 │
    │          ─────────────────────────                                   │
    │                                                                      │
    │    TX ─────────╲        ╱─────────╲        ╱───────── RX             │
    │                 ╲      ╱           ╲      ╱                          │
    │                  ╲    ╱             ╲    ╱                           │
    │     ═════════════════════════════════════════════════               │
    │                        Earth Surface                                 │
    └──────────────────────────────────────────────────────────────────────┘

PROPAGATION MODES:
- Ground Wave (GW): Direct surface wave, range < 200 km
- 1-hop E (1E): Single E-layer reflection, range < 2500 km
- 1-hop F (1F): Single F-layer reflection, range < 4000 km
- 2-hop F (2F): Two F-layer bounces, range < 8000 km
- N-hop F (NF): Multiple bounces for long paths

KEY INSIGHT: Modes are DISCRETE
-------------------------------
For a given transmitter-receiver path, only a finite set of propagation
modes are geometrically possible. Each mode has a characteristic delay:

    τ_mode = (path_length) / c

Where path_length depends on the reflection geometry.

REFERENCE: Davies, K. (1990). "Ionospheric Radio." Peter Peregrinus Ltd.
           Chapter 7: HF Propagation.

================================================================================
THEORY: PATH GEOMETRY CALCULATION
================================================================================
For N-hop propagation through a layer at height h:

    ┌─────────────────────────────────────────────────────────────────────┐
    │                                                                     │
    │                        Layer (height h)                             │
    │                    ────────●────────                                │
    │                          ╱ ╲                                        │
    │                   slant ╱   ╲ slant                                 │
    │                  range ╱     ╲ range                                │
    │                       ╱       ╲                                     │
    │                      ╱ θ       ╲                                    │
    │               TX ───●───────────●─── RX                             │
    │                  ← hop_distance →                                   │
    └─────────────────────────────────────────────────────────────────────┘

For each hop:
    hop_distance = ground_distance / N_hops
    half_hop = hop_distance / 2
    
Elevation angle θ:
    tan(θ) = h / half_hop
    θ = atan(h / half_hop)

Slant range (one way):
    slant = √(half_hop² + h²)

Total path length:
    path_length = 2 × slant × N_hops = 2 × √(half_hop² + h²) × N_hops

Geometric delay:
    τ_geo = path_length / c

Note: This is a flat-Earth approximation, valid for paths < 2000 km.
For longer paths, Earth curvature must be considered.

REFERENCE: McNamara, L.F. (1991). "The Ionosphere: Communications,
           Surveillance, and Direction Finding." Krieger Publishing.

================================================================================
THEORY: IONOSPHERIC DELAY
================================================================================
In addition to geometric delay, the ionosphere slows radio waves:

    v_group = c × √(1 - (f_p/f)²)

Where:
    v_group = group velocity in ionosphere
    c = speed of light in vacuum
    f_p = plasma frequency (~3-12 MHz depending on electron density)
    f = radio frequency

For f >> f_p (typical HF case):
    τ_iono ≈ (40.3 × TEC) / (c × f²)

Where TEC = Total Electron Content along path (electrons/m²)

For HF time transfer, the ionospheric delay is typically:
    2.5 MHz:  0.3-0.5 ms additional delay
    5 MHz:    0.1-0.2 ms additional delay
    10 MHz:   0.03-0.05 ms additional delay
    15+ MHz:  < 0.02 ms additional delay

REFERENCE: Budden, K.G. (1985). "The Propagation of Radio Waves."
           Cambridge University Press. Chapter 13.

================================================================================
THEORY: MODE DISAMBIGUATION
================================================================================
The critical challenge is identifying which mode produced the observed signal.
We use multiple observables:

1. DELAY SPREAD (τ_spread)
   Multi-hop paths have greater delay spread due to multiple reflection points.
   - 1-hop: τ_spread < 0.3 ms (typically)
   - 2-hop: τ_spread ≈ 0.5-1.5 ms
   - 3-hop: τ_spread > 1.5 ms

2. DOPPLER SPREAD (σ_doppler)
   Ionospheric motion causes Doppler shifts. Multi-hop paths sum these effects.
   - Stable path: σ < 0.1 Hz
   - Moderate: σ ≈ 0.1-0.3 Hz
   - Unstable: σ > 0.5 Hz

3. FREQUENCY SELECTIVITY SCORE (FSS)
   The D-layer (60-90 km) absorbs HF energy, particularly at lower frequencies.
   Each hop through the D-layer causes additional absorption.
   - 1-hop: FSS ≈ -0.5 to -1.0 dB
   - 2-hop: FSS ≈ -1.0 to -2.0 dB
   - 3-hop: FSS ≈ -1.5 to -3.0 dB

4. ELEVATION ANGLE
   Low elevation angles (< 5°) are less likely due to ground losses.
   Very high angles (> 80°) are rare except for near-vertical incidence.

MODE SCORING:
-------------
Each candidate mode is scored based on how well it explains the observables:

    score = f(delay_match) × f(spread_consistency) × f(FSS_match) × f(plausibility)

The highest-scoring mode is selected, with confidence based on:
    - Score separation from second-best mode
    - Absolute score value
    - Physical plausibility

REFERENCE: Goodman, J.M. (2005). "Space Weather & Telecommunications."
           Springer. Chapter 4: HF Radio Wave Propagation.

================================================================================
USAGE
================================================================================
    solver = TransmissionTimeSolver(
        receiver_lat=39.0, receiver_lon=-94.5,  # Kansas
        sample_rate=20000
    )
    
    # Solve for transmission time given observed arrival
    result = solver.solve(
        station='WWV',
        frequency_mhz=10.0,
        arrival_rtp=12345678,      # RTP timestamp of detected pulse
        delay_spread_ms=0.5,       # From correlation analysis
        doppler_std_hz=0.1,        # Path stability indicator
        fss_db=-2.0               # Frequency selectivity (D-layer indicator)
    )
    
    print(f"Mode: {result.mode}")           # "1F" 
    print(f"T_emit offset: {result.emission_offset_ms:.2f} ms")  # -14.23
    print(f"Confidence: {result.confidence:.1%}")  # 95%

================================================================================
OUTPUT: D_clock
================================================================================
The solver's output `emission_offset_ms` is the D_clock value:

    D_clock = T_system - T_UTC(NIST) = emission_offset_ms

If the solver correctly identifies the propagation mode and the system clock
is accurate, emission_offset_ms should be close to 0 (within ~1 ms).

Deviations indicate either:
    1. System clock error (the measurement we want!)
    2. Incorrect mode identification
    3. Ionospheric modeling error

================================================================================
REVISION HISTORY
================================================================================
2025-12-13: Upgraded to IRI-2020 ionospheric model (improved accuracy)
2025-12-07: Issue 1.3 FIX - Proper 1/f² ionospheric delay model (replaces linear)
2025-12-07: Issue 1.2 FIX - Dynamic ionospheric model with IRI integration
2025-12-07: Added comprehensive theoretical documentation
2025-11-15: Added multi-station solver for correlated UTC estimation
2025-10-20: Initial implementation with single-station mode disambiguation
"""

import logging
import math
from datetime import datetime, timezone
from typing import Optional, List, Dict, Tuple
from dataclasses import dataclass, field
from enum import Enum

import numpy as np

# Import the dynamic ionospheric model (Issue 1.2 fix) and delay calculator (Issue 1.3 fix)
from .ionospheric_model import (
    IonosphericModel,
    LayerHeights,
    IonosphericModelTier,
    IonosphericDelayCalculator,
    IonosphericDelayResult,
    DEFAULT_E_LAYER_HEIGHT_KM,
    DEFAULT_F1_LAYER_HEIGHT_KM,
    DEFAULT_F2_LAYER_HEIGHT_KM
)

logger = logging.getLogger(__name__)

# =============================================================================
# PHYSICAL CONSTANTS
# =============================================================================
# Speed of light in km/s
# Source: CODATA 2018 recommended value (exact by definition)
SPEED_OF_LIGHT_KM_S = 299792.458

# Station-specific expected propagation delay ranges (ms)
# Based on great circle distance and typical ionospheric conditions
# Format: station -> (min_delay_ms, max_delay_ms)
EXPECTED_DELAY_RANGES = {
    'WWV': (4.0, 12.0),     # Fort Collins, CO → typical receiver (~1500km, 1-2 hop F2)
    'WWVH': (15.0, 30.0),   # Kauai, HI → typical receiver (~5500km, 2-3 hop F2)
    'CHU': (6.0, 15.0),     # Ottawa, ON → typical receiver (~2000km, 1-2 hop F2)
    'BPM': (40.0, 70.0),    # Shaanxi, China → typical receiver (~10000km, 3-4 hop F2)
}

# Maximum ionospheric delay per hop for different frequencies (ms)
# Based on 1/f² relationship with typical TEC values (10-40 TECU)
MAX_IONO_DELAY_PER_HOP = {
    2.5: 0.8,   # 2.5 MHz
    3.33: 0.5,  # 3.33 MHz (CHU)
    5.0: 0.3,   # 5 MHz
    7.85: 0.15, # 7.85 MHz (CHU)
    10.0: 0.1,  # 10 MHz
    14.67: 0.08, # 14.67 MHz (CHU)
    15.0: 0.05, # 15 MHz
    20.0: 0.03, # 20 MHz
    25.0: 0.02, # 25 MHz
}

# Mean Earth radius (km)
# Source: WGS84 mean radius = (2a + b) / 3 where a=6378.137, b=6356.752
EARTH_RADIUS_KM = 6371.0

# =============================================================================
# IONOSPHERIC LAYER HEIGHTS (km)
# =============================================================================
# IMPORTANT: These constants are now FALLBACKS only.
#
# Issue 1.2 Fix (2025-12-07), upgraded 2025-12-13:
# The original implementation used these FIXED values, ignoring the significant
# variation of hmF2 (200-400 km) with time, solar activity, and location.
# 
# The new IonosphericModel class provides dynamic heights via:
#   TIER 1: IRI-2020 (International Reference Ionosphere) when available
#           Falls back to IRI-2016 if IRI-2020 not installed
#   TIER 2: Parametric model capturing diurnal/seasonal/solar variations
#   TIER 3: These static fallbacks as last resort
#
# Additionally, the F2_NIGHT_HEIGHT_KM constant was defined but NEVER USED
# in the original code - a bug that is now fixed.
#
# Reference: ITU-R P.1239-3 "ITU-R Reference Ionospheric Characteristics"
# =============================================================================
E_LAYER_HEIGHT_KM = DEFAULT_E_LAYER_HEIGHT_KM     # E-layer: 90-130 km, daytime only
F1_LAYER_HEIGHT_KM = DEFAULT_F1_LAYER_HEIGHT_KM   # F1-layer: 150-200 km
F2_LAYER_HEIGHT_KM = DEFAULT_F2_LAYER_HEIGHT_KM   # F2-layer: 250-400 km (FALLBACK)
F2_NIGHT_HEIGHT_KM = 350.0   # F2-layer at night (now used via IonosphericModel)

# =============================================================================
# TIME SIGNAL STATION LOCATIONS (WGS84 coordinates)
# =============================================================================
# Issue 4.1 Fix (2025-12-07): Coordinates now imported from wwv_constants.py
# (single source of truth). Previous values were inconsistent across files.
#
# WWV: Fort Collins, Colorado, USA - NIST verified coordinates
# WWVH: Kekaha, Kauai, Hawaii, USA - NIST verified coordinates  
# CHU: Ottawa, Ontario, Canada - NRC verified coordinates
# =============================================================================
from .wwv_constants import STATION_LOCATIONS as STATIONS

# =============================================================================
# IONOSPHERIC DELAY FACTOR (frequency-dependent)
# =============================================================================
# The ionosphere is a dispersive medium: group velocity depends on frequency.
# Lower frequencies experience more delay (slower group velocity).
#
# Physical basis: v_group = c × √(1 - (f_p/f)²)
# Where f_p = plasma frequency (~3-12 MHz in F-layer)
#
# These factors are empirical multipliers relative to 10 MHz:
#   delay_at_freq = base_delay × IONO_DELAY_FACTOR[freq]
#
# Values are approximate and vary with solar activity and time of day.
# =============================================================================
IONO_DELAY_FACTOR = {
    2.5: 1.5,    # 2.5 MHz: 50% more delay (close to plasma frequency)
    3.33: 1.3,   # CHU 3.33 MHz
    5.0: 1.1,    # 5 MHz: 10% more delay
    7.85: 1.05,  # CHU 7.85 MHz
    10.0: 1.0,   # 10 MHz: reference frequency
    14.67: 0.95, # CHU 14.67 MHz
    15.0: 0.95,  # 15 MHz: 5% less delay
    20.0: 0.9,   # 20 MHz: 10% less delay
    25.0: 0.85,  # 25 MHz: 15% less delay
}


class PropagationMode(Enum):
    """Discrete propagation modes for HF signals"""
    GROUND_WAVE = "GW"      # Direct ground wave (short range only)
    ONE_HOP_E = "1E"        # Single E-layer reflection
    ONE_HOP_F = "1F"        # Single F-layer reflection
    TWO_HOP_F = "2F"        # Two F-layer reflections
    THREE_HOP_F = "3F"      # Three F-layer reflections
    MIXED_EF = "EF"         # E-layer + F-layer combination
    UNKNOWN = "UNK"


@dataclass
class ModeCandidate:
    """A candidate propagation mode with calculated delay"""
    mode: PropagationMode
    layer_height_km: float
    n_hops: int
    path_length_km: float
    geometric_delay_ms: float
    iono_delay_ms: float
    total_delay_ms: float
    elevation_angle_deg: float
    plausibility: float  # 0-1, based on physics constraints


@dataclass 
class SolverResult:
    """Result of transmission time back-calculation"""
    # Timing results
    arrival_rtp: int
    emission_rtp: int  # Back-calculated emission time in RTP units
    emission_offset_ms: float  # Offset from second boundary (should be ~0 for top-of-second)
    propagation_delay_ms: float
    
    # Mode identification
    mode: PropagationMode
    mode_name: str  # Human readable, e.g., "1-hop F2 layer"
    n_hops: int
    layer_height_km: float
    elevation_angle_deg: float
    
    # Confidence metrics
    confidence: float  # 0-1 overall confidence
    mode_separation_ms: float  # Gap to next-best mode (larger = more confident)
    delay_spread_penalty: float  # Multipath indicator reduces confidence
    doppler_penalty: float  # Unstable path reduces confidence
    fss_consistency: float  # Does FSS match expected for this mode?
    
    # All candidates considered
    candidates: List[ModeCandidate] = field(default_factory=list)
    
    # UTC(NIST) verification
    utc_nist_offset_ms: Optional[float] = None  # Offset from expected UTC second
    utc_nist_verified: bool = False  # True if offset < threshold


class TransmissionTimeSolver:
    """
    Solve for transmission time by identifying propagation mode.
    
    This turns a passive receiver into a primary time standard by
    back-calculating when the signal was actually transmitted at
    WWV/WWVH/CHU, recovering UTC(NIST) with ~1ms accuracy.
    
    Issue 1.2 Fix (2025-12-07), upgraded 2025-12-13:
    ------------------------------------------------
    Now uses dynamic ionospheric layer heights via IonosphericModel:
    - TIER 1: IRI-2020 when available (best accuracy, ~20-25 km)
              Falls back to IRI-2016 if IRI-2020 not installed
    - TIER 2: Parametric model (captures diurnal/seasonal variation)
    - TIER 3: Static fallback (original fixed constants)
    
    IRI-2020 improvements over IRI-2016:
    - Better topside electron density model
    - Improved storm-time corrections
    - Updated CCIR/URSI coefficients
    - Better high-latitude coverage
    
    The model also learns calibration offsets from actual measurements
    to track ionospheric "weather" vs "climate".
    """
    
    def __init__(
        self,
        receiver_lat: float,
        receiver_lon: float,
        sample_rate: int = 20000,
        f_layer_height_km: float = F2_LAYER_HEIGHT_KM,
        enable_dynamic_ionosphere: bool = True
    ):
        """
        Initialize solver with receiver location.
        
        Args:
            receiver_lat: Receiver latitude (degrees, WGS84)
            receiver_lon: Receiver longitude (degrees, WGS84)
            sample_rate: Audio sample rate (Hz), used for RTP conversion
            f_layer_height_km: DEPRECATED - Static F-layer height fallback (km).
                              Now only used if enable_dynamic_ionosphere=False.
            enable_dynamic_ionosphere: Use IonosphericModel for dynamic heights.
                                      Set False to revert to original fixed-height behavior.
        """
        self.receiver_lat = receiver_lat
        self.receiver_lon = receiver_lon
        self.sample_rate = sample_rate
        self.f_layer_height_km = f_layer_height_km  # Fallback value
        self.enable_dynamic_ionosphere = enable_dynamic_ionosphere
        
        # Initialize the dynamic ionospheric model (Issue 1.2 fix)
        if enable_dynamic_ionosphere:
            self.iono_model = IonosphericModel(
                enable_iri=True,
                enable_calibration=True,
                calibration_window_hours=24.0
            )
            # Initialize ionospheric delay calculator (Issue 1.3 fix)
            self.delay_calculator = IonosphericDelayCalculator(iono_model=self.iono_model)
            logger.info("Dynamic ionospheric model enabled (IRI-2020/2016 + calibration + 1/f² delay)")
        else:
            self.iono_model = None
            self.delay_calculator = None
            logger.info(f"Using static F-layer height: {f_layer_height_km} km (and linear delay model)")
        
        # Track the last layer heights used (for logging/debugging)
        self._last_layer_heights: Optional[LayerHeights] = None
        
        # Pre-calculate distances to each station
        self.station_distances = {}
        for station, loc in STATIONS.items():
            dist = self._great_circle_distance(
                receiver_lat, receiver_lon,
                loc['lat'], loc['lon']
            )
            self.station_distances[station] = dist
            logger.info(f"Distance to {station}: {dist:.1f} km")
    
    def _great_circle_distance(
        self, lat1: float, lon1: float, lat2: float, lon2: float
    ) -> float:
        """Calculate great circle distance in km using Haversine formula."""
        lat1_rad = math.radians(lat1)
        lat2_rad = math.radians(lat2)
        delta_lat = math.radians(lat2 - lat1)
        delta_lon = math.radians(lon2 - lon1)
        
        a = (math.sin(delta_lat / 2) ** 2 +
             math.cos(lat1_rad) * math.cos(lat2_rad) * 
             math.sin(delta_lon / 2) ** 2)
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        
        return EARTH_RADIUS_KM * c
    
    def _calculate_hop_path(
        self,
        ground_distance_km: float,
        layer_height_km: float,
        n_hops: int
    ) -> Tuple[float, float]:
        """
        Calculate path length and elevation angle for N-hop ionospheric propagation.
        
        GEOMETRY:
        ---------
        For N-hop propagation, the ground distance is divided into N equal segments.
        Each segment involves an upward slant to the ionospheric layer and a
        downward slant to the next ground reflection point.
        
                            Layer (h km)
                        ────────●────────
                              ╱   ╲
                       slant ╱     ╲ slant
                            ╱       ╲
                           ╱ θ       ╲
                    TX ───●───────────●─── (next hop or RX)
                       ← hop_distance →
        
        EQUATIONS:
        ----------
        1. hop_distance = ground_distance / N_hops
        2. half_hop = hop_distance / 2
        3. elevation angle: θ = atan(h / half_hop)
        4. slant_range = √(half_hop² + h²)
        5. path_per_hop = 2 × slant_range (up and down)
        6. total_path = path_per_hop × N_hops
        
        ASSUMPTIONS:
        ------------
        - Flat Earth approximation (valid for hops < 2000 km)
        - Specular reflection (mirror-like) from ionospheric layer
        - Layer height is constant (reality: varies with location/time)
        
        For paths > 2000 km, Earth curvature causes the actual path to be
        slightly longer than this calculation. Error is ~1% at 2000 km.
        
        Args:
            ground_distance_km: Great circle distance between TX and RX (km)
            layer_height_km: Ionospheric layer reflection height (km)
            n_hops: Number of ionospheric reflections (0 for ground wave)
            
        Returns:
            Tuple of (path_length_km, elevation_angle_deg)
            - path_length_km: Total signal path length through atmosphere
            - elevation_angle_deg: Takeoff angle from horizon at TX
        
        Example:
            For WWV to Kansas (1500 km), 1-hop F2 at 300 km:
            >>> path, elev = self._calculate_hop_path(1500, 300, 1)
            >>> path   # ~1618 km (longer than ground distance)
            >>> elev   # ~21.8° elevation angle
        """
        if n_hops == 0:
            # Ground wave: signal follows Earth's surface
            # Elevation angle is essentially 0° (grazing)
            return ground_distance_km, 0.0
        
        # Divide ground distance equally among hops
        hop_distance = ground_distance_km / n_hops
        
        # Half the hop distance (horizontal distance to reflection point)
        half_hop = hop_distance / 2
        
        # Elevation angle θ from horizon
        # tan(θ) = opposite/adjacent = layer_height/half_hop
        elevation_rad = math.atan2(layer_height_km, half_hop)
        elevation_deg = math.degrees(elevation_rad)
        
        # Slant range: hypotenuse of right triangle
        # slant = √(half_hop² + h²)
        slant_range = math.sqrt(half_hop ** 2 + layer_height_km ** 2)
        
        # Each hop goes UP to layer and DOWN to ground: 2 × slant
        path_per_hop = 2 * slant_range
        
        # Total path is sum of all hops
        total_path = path_per_hop * n_hops
        
        return total_path, elevation_deg
    
    def _get_layer_heights(
        self,
        timestamp: Optional[datetime] = None,
        station_lat: Optional[float] = None,
        station_lon: Optional[float] = None
    ) -> Tuple[float, float, float]:
        """
        Get ionospheric layer heights for the current conditions.
        
        Issue 1.2 Fix: Uses IonosphericModel when enabled, otherwise
        falls back to static constants.
        
        For propagation calculations, we use the layer height at the
        midpoint between transmitter and receiver (approximation).
        
        Args:
            timestamp: UTC time for height calculation
            station_lat: Transmitter latitude (for midpoint calculation)
            station_lon: Transmitter longitude (for midpoint calculation)
            
        Returns:
            Tuple of (hmE, hmF1, hmF2) layer heights in km
        """
        if self.iono_model is None:
            # Original behavior: use fixed constants
            return (E_LAYER_HEIGHT_KM, F1_LAYER_HEIGHT_KM, self.f_layer_height_km)
        
        # Calculate midpoint for ionospheric height lookup
        if station_lat is not None and station_lon is not None:
            mid_lat = (self.receiver_lat + station_lat) / 2
            mid_lon = (self.receiver_lon + station_lon) / 2
        else:
            mid_lat = self.receiver_lat
            mid_lon = self.receiver_lon
        
        # Get dynamic heights from ionospheric model
        heights = self.iono_model.get_layer_heights(
            timestamp=timestamp,
            latitude=mid_lat,
            longitude=mid_lon
        )
        
        # Store for later reference (debugging, calibration)
        self._last_layer_heights = heights
        
        logger.debug(f"Layer heights via {heights.tier.value}: "
                    f"hmF2={heights.hmF2:.1f}±{heights.hmF2_uncertainty_km:.0f} km, "
                    f"hmF1={heights.hmF1:.1f} km, hmE={heights.hmE:.1f} km")
        
        return (heights.hmE, heights.hmF1, heights.hmF2)
    
    def _calculate_mode_delay(
        self,
        mode: PropagationMode,
        ground_distance_km: float,
        frequency_mhz: float,
        timestamp: Optional[datetime] = None,
        station_lat: Optional[float] = None,
        station_lon: Optional[float] = None
    ) -> Optional[ModeCandidate]:
        """
        Calculate propagation delay for a specific mode.
        
        Returns ModeCandidate with all timing details, or None if mode
        is physically implausible for this path.
        
        Issue 1.2 Fix (2025-12-07):
        Now uses dynamic layer heights from IonosphericModel when available,
        capturing diurnal, seasonal, and solar activity variations.
        """
        # Get current layer heights (dynamic if model enabled, static otherwise)
        hmE, hmF1, hmF2 = self._get_layer_heights(
            timestamp=timestamp,
            station_lat=station_lat,
            station_lon=station_lon
        )
        
        # Determine layer height and hop count for this mode
        if mode == PropagationMode.GROUND_WAVE:
            if ground_distance_km > 200:  # Ground wave limited range
                return None
            layer_height = 0
            n_hops = 0
        elif mode == PropagationMode.ONE_HOP_E:
            # CRITICAL FIX: E-layer plausibility check
            # E-layer only exists during daytime (solar ionization required)
            if timestamp is not None:
                # Calculate local solar time at midpoint
                from datetime import timezone
                hour_utc = timestamp.hour + timestamp.minute / 60.0
                if station_lon is not None:
                    local_hour = (hour_utc + station_lon / 15.0) % 24.0
                else:
                    local_hour = hour_utc
                
                # E-layer exists roughly 06:00-18:00 local time
                is_daytime = 6.0 <= local_hour <= 18.0
                if not is_daytime:
                    logger.debug(f"E-layer mode rejected: nighttime (local hour {local_hour:.1f})")
                    return None  # E-layer doesn't exist at night
            
            layer_height = hmE  # Dynamic E-layer height
            n_hops = 1
            # E-layer only works for shorter paths
            if ground_distance_km > 2500:
                return None
        elif mode == PropagationMode.ONE_HOP_F:
            layer_height = hmF2  # Dynamic F2-layer height
            n_hops = 1
            # Check if single hop can reach (max ~4000 km)
            if ground_distance_km > 4000:
                return None
        elif mode == PropagationMode.TWO_HOP_F:
            layer_height = hmF2  # Dynamic F2-layer height
            n_hops = 2
        elif mode == PropagationMode.THREE_HOP_F:
            layer_height = hmF2  # Dynamic F2-layer height
            n_hops = 3
        elif mode == PropagationMode.MIXED_EF:
            # Approximate as 1.5 hops at intermediate height (E + F2)
            layer_height = (hmE + hmF2) / 2
            n_hops = 2
        else:
            return None
        
        # Calculate path geometry
        path_length_km, elevation_deg = self._calculate_hop_path(
            ground_distance_km, layer_height, n_hops
        )
        
        # CRITICAL FIX: Enhanced plausibility checks
        # Check elevation angle plausibility (< 5° is very low, may not propagate)
        plausibility = 1.0
        
        if n_hops > 0:
            if elevation_deg < 3:
                plausibility *= 0.3  # Very low angle, unlikely
            elif elevation_deg < 10:
                plausibility *= 0.7  # Low angle, possible but not ideal
        
        # Ground wave beyond 500km is highly implausible
        if mode == PropagationMode.GROUND_WAVE and ground_distance_km > 500:
            logger.warning(f"Ground wave mode beyond realistic range: {ground_distance_km:.0f}km")
            plausibility *= 0.1
        
        # Multi-hop modes at short distances are implausible
        if n_hops >= 2 and ground_distance_km < 1000:
            logger.debug(f"{n_hops}-hop mode implausible for short distance {ground_distance_km:.0f}km")
            plausibility *= 0.3
        
        # 3+ hops are rare and only for very long distances
        if n_hops >= 3 and ground_distance_km < 5000:
            plausibility *= 0.5
        
        # Geometric delay (speed of light)
        geometric_delay_ms = (path_length_km / SPEED_OF_LIGHT_KM_S) * 1000
        
        # CRITICAL FIX: Propagation delay bounds check
        # Prevent sign errors and physically impossible delays
        if geometric_delay_ms < 0:
            logger.error(f"CRITICAL: Negative geometric delay {geometric_delay_ms:.3f}ms - sign error in calculation!")
            return None
        if geometric_delay_ms < 3.0:
            logger.warning(f"Suspiciously low geometric delay {geometric_delay_ms:.3f}ms for {ground_distance_km:.0f}km")
        if geometric_delay_ms > 100.0:
            logger.error(f"CRITICAL: Geometric delay {geometric_delay_ms:.3f}ms exceeds physical bounds (>100ms)")
            return None
        
        # =================================================================
        # IONOSPHERIC DELAY (Issue 1.3 Fix - 2025-12-07)
        # =================================================================
        # OLD CODE (linear approximation - WRONG):
        #     iono_factor = IONO_DELAY_FACTOR.get(frequency_mhz, 1.0)
        #     iono_delay_ms = n_hops * 0.15 * iono_factor
        #
        # NEW: Use proper 1/f² physics via IonosphericDelayCalculator
        #     τ_iono = 40.3 × TEC / (c × f²)
        #
        # The 1/f² relationship means 2.5 MHz has 16× more delay than 10 MHz,
        # not the 1.5× that the old linear model assumed.
        # =================================================================
        
        if self.delay_calculator is not None and n_hops > 0:
            # Use physically correct 1/f² model
            # Calculate midpoint latitude/longitude for TEC lookup
            if station_lat is not None and station_lon is not None:
                mid_lat = (self.receiver_lat + station_lat) / 2
                mid_lon = (self.receiver_lon + station_lon) / 2
            else:
                mid_lat = self.receiver_lat
                mid_lon = self.receiver_lon
            
            delay_result = self.delay_calculator.calculate_delay(
                frequency_mhz=frequency_mhz,
                n_hops=n_hops,
                elevation_deg=elevation_deg,
                timestamp=timestamp,
                latitude=mid_lat,
                longitude=mid_lon
            )
            iono_delay_ms = delay_result.delay_ms
        else:
            # Fallback: Use proper 1/f² physics with parametric TEC estimate
            # This ensures frequency-dependent delays even without IRI/IONEX
            # 
            # Ionospheric group delay: τ = 40.3 × TEC / f² (seconds)
            # where TEC is in TECU (10^16 el/m²), f is in Hz
            #
            # Parametric TEC estimate (moderate conditions):
            # - Day: ~30 TECU, Night: ~10 TECU
            # - Simple model based on local solar time
            
            if timestamp is not None:
                # Calculate local solar time for diurnal TEC variation
                hour_utc = timestamp.hour + timestamp.minute / 60.0
                if station_lon is not None:
                    local_hour = (hour_utc + station_lon / 15.0) % 24.0
                else:
                    local_hour = hour_utc
                
                # Diurnal TEC model: peak around 14:00 local time
                # TEC varies from ~10 TECU (night) to ~40 TECU (day)
                import math
                diurnal_phase = 2 * math.pi * (local_hour - 14.0) / 24.0
                tec_tecu = 25.0 * (1.0 + 0.6 * math.cos(diurnal_phase))  # 10-40 TECU range
            else:
                # No timestamp: use moderate default
                tec_tecu = 25.0
            
            # Calculate frequency-dependent ionospheric delay
            # Formula: delay_ms = (40.3 × TEC × n_hops) / (f_Hz²) × 1000
            frequency_hz = frequency_mhz * 1e6
            iono_delay_ms = (40.3 * tec_tecu * n_hops * 1000.0) / (frequency_hz ** 2)
        
        # Total delay
        total_delay_ms = geometric_delay_ms + iono_delay_ms
        
        # CRITICAL FIX: Enhanced propagation delay validation (2026-01-04)
        # Issue: Previous validation was too weak, allowing physically impossible delays
        # Fix: Add station-specific bounds and ionospheric delay validation
        
        # 1. Basic sanity checks
        if total_delay_ms < 0:
            logger.error(f"CRITICAL: Negative total delay {total_delay_ms:.3f}ms - sign error!")
            return None
        if total_delay_ms > 120.0:
            logger.error(f"CRITICAL: Total delay {total_delay_ms:.3f}ms exceeds physical bounds (>120ms)")
            return None
        
        # 2. Validate ionospheric delay component
        if n_hops > 0:
            # Get expected max ionospheric delay for this frequency
            max_iono_expected = MAX_IONO_DELAY_PER_HOP.get(frequency_mhz, 0.1) * n_hops * 6.0  # 6x safety factor for variable ionospheric conditions
            if iono_delay_ms < 0:
                logger.error(f"CRITICAL: Negative ionospheric delay {iono_delay_ms:.3f}ms")
                return None
            if iono_delay_ms > max_iono_expected:
                # Don't reject - the measurement is ground truth. Reduce plausibility instead.
                logger.warning(f"UNUSUAL: Ionospheric delay {iono_delay_ms:.3f}ms exceeds model prediction "
                           f"{max_iono_expected:.3f}ms for {frequency_mhz}MHz, {n_hops} hops - accepting with reduced confidence")
                plausibility *= 0.5  # Reduce confidence but don't reject
        
        # 3. Station-specific validation (if station is known)
        # This catches systematic errors in propagation mode selection
        if hasattr(self, '_current_station') and self._current_station:
            station = self._current_station
            if station in EXPECTED_DELAY_RANGES:
                min_delay, max_delay = EXPECTED_DELAY_RANGES[station]
                if total_delay_ms < min_delay or total_delay_ms > max_delay:
                    logger.warning(
                        f"SUSPECT: {station} total delay {total_delay_ms:.1f}ms outside typical range "
                        f"{min_delay:.1f}-{max_delay:.1f}ms for mode={mode.value}, distance={ground_distance_km:.0f}km"
                    )
                    # Reduce plausibility but don't reject outright (ionosphere can be unusual)
                    plausibility *= 0.3
        
        return ModeCandidate(
            mode=mode,
            layer_height_km=layer_height,
            n_hops=n_hops,
            path_length_km=path_length_km,
            geometric_delay_ms=geometric_delay_ms,
            iono_delay_ms=iono_delay_ms,
            total_delay_ms=total_delay_ms,
            elevation_angle_deg=elevation_deg,
            plausibility=plausibility
        )
    
    def _evaluate_mode_fit(
        self,
        candidate: ModeCandidate,
        observed_delay_ms: float,
        delay_spread_ms: float,
        doppler_std_hz: float,
        fss_db: Optional[float]
    ) -> float:
        """
        Evaluate how well a candidate propagation mode fits the observed signal.
        
        MODE DISAMBIGUATION THEORY:
        ---------------------------
        Multiple propagation modes may have similar geometric delays, making
        mode identification ambiguous from timing alone. We use secondary
        observables to disambiguate:
        
        1. DELAY MATCH
           Primary criterion: how close is predicted delay to observed?
           - Error < 0.5 ms: Excellent match (score = 1.0)
           - Error < 1.0 ms: Good match (score = 0.8)
           - Error > 2.0 ms: Poor match (score = 0.1)
        
        2. DELAY SPREAD → HOP COUNT
           Multipath causes delay spread (different ray paths arrive at
           different times). More hops = more multipath = more spread.
           
           Model: τ_spread ∝ N_hops
           - 1-hop: expect < 0.5 ms spread
           - 2-hop: expect 0.5-1.5 ms spread
           - 3-hop: expect > 1.5 ms spread
           
           If observed spread is high but candidate is 1-hop: PENALIZE
           If observed spread is high and candidate is multi-hop: BONUS
        
        3. FSS → D-LAYER ABSORPTION
           The D-layer (60-90 km) absorbs HF, especially at lower frequencies.
           Each hop passes through the D-layer twice (up and down).
           
           Model: FSS ≈ -0.8 dB × N_hops
           - Negative FSS with 1-hop candidate: PENALIZE
           - Negative FSS with multi-hop candidate: BONUS
        
        4. DOPPLER STABILITY
           Ionospheric motion causes Doppler shifts. Multi-hop paths
           accumulate Doppler from multiple reflection points.
           - High Doppler std (> 0.5 Hz): path is unstable, reduce confidence
           
        SCORING FORMULA:
        ----------------
        score = delay_score × plausibility × spread_penalty × doppler_penalty × fss_score
                + multipath_bonus + fss_bonus
        
        Where multiplicative factors penalize poor matches and additive
        bonuses reward consistent multi-hop indicators.
        
        Args:
            candidate: ModeCandidate with predicted delay and hop count
            observed_delay_ms: Measured propagation delay from tone detector
            delay_spread_ms: Observed multipath delay spread
            doppler_std_hz: Doppler standard deviation (path stability)
            fss_db: Frequency Selectivity Score (D-layer indicator)
            
        Returns:
            Score from 0.0 (poor fit) to 1.0 (excellent fit)
            
        Note:
            This scoring is empirically tuned for WWV/WWVH paths. Different
            paths may require adjusted thresholds.
        """
        # Base score: how close is predicted delay to observed?
        delay_error_ms = abs(candidate.total_delay_ms - observed_delay_ms)
        
        # Delay errors > 2ms are very unlikely to be correct mode
        if delay_error_ms > 2.0:
            delay_score = 0.1
        elif delay_error_ms > 1.0:
            delay_score = 0.5
        elif delay_error_ms > 0.5:
            delay_score = 0.8
        else:
            delay_score = 1.0
        
        # === CRITICAL: Delay spread as multipath indicator ===
        # High delay spread strongly suggests multi-hop propagation
        # This is a TIE-BREAKER when two modes have similar delays
        spread_penalty = 1.0
        multipath_bonus = 0.0
        
        if delay_spread_ms > 1.5:
            # Very high spread: almost certainly multi-hop
            if candidate.n_hops >= 2:
                multipath_bonus = 0.15  # Boost multi-hop modes
            elif candidate.n_hops == 1:
                spread_penalty = 0.6  # Heavily penalize single-hop
        elif delay_spread_ms > 1.0:
            # High spread: likely multi-hop
            if candidate.n_hops >= 2:
                multipath_bonus = 0.10
            elif candidate.n_hops == 1:
                spread_penalty = 0.7
        elif delay_spread_ms > 0.5:
            # Moderate spread: slight preference for multi-hop
            if candidate.n_hops >= 2:
                multipath_bonus = 0.05
            else:
                spread_penalty = 0.9
        
        # Doppler penalty: high Doppler std means unstable path
        if doppler_std_hz > 0.5:
            doppler_penalty = 0.7
        elif doppler_std_hz > 0.2:
            doppler_penalty = 0.9
        else:
            doppler_penalty = 1.0
        
        # === CRITICAL: FSS integration for D-layer detection ===
        # Negative FSS (high frequencies attenuated) indicates D-layer traversal
        # More hops = more D-layer transits = more negative FSS expected
        fss_score = 1.0
        fss_bonus = 0.0
        
        if fss_db is not None:
            # Model: Each hop through D-layer attenuates highs by ~0.8 dB
            expected_fss = -0.8 * candidate.n_hops
            fss_error = abs(fss_db - expected_fss)
            
            # If FSS is very negative (strong D-layer attenuation)
            if fss_db < -2.0:
                # Should be multi-hop; penalize single-hop
                if candidate.n_hops >= 2:
                    fss_bonus = 0.10
                elif candidate.n_hops == 1:
                    fss_score = 0.7
            elif fss_db < -1.0:
                # Moderate D-layer effect
                if candidate.n_hops >= 2:
                    fss_bonus = 0.05
            
            # Also penalize if FSS doesn't match expectation
            if fss_error > 3:
                fss_score *= 0.8
            elif fss_error > 1.5:
                fss_score *= 0.9
        
        # Combine scores
        # Note: bonuses are additive, penalties are multiplicative
        total_score = (
            delay_score * 
            candidate.plausibility * 
            spread_penalty * 
            doppler_penalty * 
            fss_score
        ) + multipath_bonus + fss_bonus
        
        # Clamp to [0, 1]
        total_score = min(1.0, max(0.0, total_score))
        
        return total_score
    
    def solve(
        self,
        station: str,
        frequency_mhz: float,
        arrival_rtp: int,
        delay_spread_ms: float = 0.0,
        doppler_std_hz: float = 0.0,
        fss_db: Optional[float] = None,
        expected_second_rtp: Optional[int] = None,
        timestamp: Optional[datetime] = None
    ) -> SolverResult:
        """
        Solve for transmission time by identifying propagation mode.
        
        Args:
            station: 'WWV', 'WWVH', or 'CHU'
            frequency_mhz: Carrier frequency
            arrival_rtp: RTP timestamp of detected signal arrival
            delay_spread_ms: Observed delay spread (multipath indicator)
            doppler_std_hz: Doppler standard deviation (path stability)
            fss_db: Frequency Selectivity Strength (D-layer indicator)
            expected_second_rtp: RTP timestamp of expected second boundary
            timestamp: UTC datetime of observation (for dynamic ionosphere model)
            
        Returns:
            SolverResult with mode identification and back-calculated time
        """
        if station not in self.station_distances:
            raise ValueError(f"Unknown station: {station}")
        
        # Store station for validation in _calculate_mode_delay (2026-01-04 fix)
        self._current_station = station
        
        ground_distance = self.station_distances[station]
        
        # Get station coordinates for ionospheric model (Issue 1.2 fix)
        station_info = STATIONS.get(station, {})
        station_lat = station_info.get('lat')
        station_lon = station_info.get('lon')
        
        # Use current time if not provided
        if timestamp is None:
            timestamp = datetime.now(timezone.utc)
        
        # Calculate all plausible mode candidates with dynamic layer heights
        candidates = []
        for mode in PropagationMode:
            if mode == PropagationMode.UNKNOWN:
                continue
            candidate = self._calculate_mode_delay(
                mode, ground_distance, frequency_mhz,
                timestamp=timestamp,
                station_lat=station_lat,
                station_lon=station_lon
            )
            if candidate:
                candidates.append(candidate)
        
        if not candidates:
            logger.warning(f"No valid propagation modes for {station} at {ground_distance:.0f} km")
            return self._no_solution(arrival_rtp)
        
        # Calculate observed delay (from expected second boundary)
        if expected_second_rtp is not None:
            observed_delay_samples = arrival_rtp - expected_second_rtp
            observed_delay_ms = (observed_delay_samples / self.sample_rate) * 1000
        else:
            # Estimate from minimum plausible delay
            min_delay = min(c.total_delay_ms for c in candidates)
            observed_delay_ms = min_delay  # Assume we're close to minimum
        
        # Score each candidate
        scored_candidates = []
        for candidate in candidates:
            score = self._evaluate_mode_fit(
                candidate, observed_delay_ms,
                delay_spread_ms, doppler_std_hz, fss_db
            )
            scored_candidates.append((score, candidate))
        
        # Sort by score (best first)
        scored_candidates.sort(key=lambda x: x[0], reverse=True)
        
        best_score, best_candidate = scored_candidates[0]
        
        # Calculate mode separation (confidence indicator)
        if len(scored_candidates) > 1:
            second_score = scored_candidates[1][0]
            mode_separation = best_score - second_score
            # Also calculate delay separation
            delay_separation_ms = abs(
                best_candidate.total_delay_ms - 
                scored_candidates[1][1].total_delay_ms
            )
        else:
            mode_separation = 1.0
            delay_separation_ms = 10.0
        
        # Back-calculate emission time
        # Use round() not int() for proper rounding (at 20kHz, 1 sample = 0.05ms)
        propagation_samples = round(
            (best_candidate.total_delay_ms / 1000) * self.sample_rate
        )
        emission_rtp = arrival_rtp - propagation_samples
        
        # Calculate offset from second boundary
        if expected_second_rtp is not None:
            emission_offset_samples = emission_rtp - expected_second_rtp
            emission_offset_ms = (emission_offset_samples / self.sample_rate) * 1000
            
            # STATION-BASED SANITY CHECK: D_clock must be within physical bounds
            # A properly synchronized system should have D_clock within ~100ms
            # Even a badly drifted clock should be within ~1000ms
            # Values like -3700ms indicate corrupted data or wrong second detection
            MAX_PLAUSIBLE_D_CLOCK_MS = 1000.0
            if abs(emission_offset_ms) > MAX_PLAUSIBLE_D_CLOCK_MS:
                logger.warning(
                    f"D_clock {emission_offset_ms:.1f}ms exceeds plausible bounds "
                    f"(±{MAX_PLAUSIBLE_D_CLOCK_MS}ms) for {station} - rejecting"
                )
                return self._no_solution(arrival_rtp)
            
            # Check if this looks like valid UTC(NIST)
            # WWV transmits at exact second boundaries, so offset should be ~0
            utc_verified = abs(emission_offset_ms) < 2.0  # Within 2ms of second
        else:
            emission_offset_ms = 0.0
            utc_verified = False
        
        # Build human-readable mode name
        mode_names = {
            PropagationMode.GROUND_WAVE: "Ground wave",
            PropagationMode.ONE_HOP_E: "1-hop E-layer",
            PropagationMode.ONE_HOP_F: "1-hop F-layer",
            PropagationMode.TWO_HOP_F: "2-hop F-layer",
            PropagationMode.THREE_HOP_F: "3-hop F-layer",
            PropagationMode.MIXED_EF: "Mixed E/F-layer",
        }
        
        # Calculate confidence
        confidence = best_score * min(1.0, delay_separation_ms / 0.5)
        
        # Apply penalties
        delay_spread_penalty = 1.0 if delay_spread_ms < 0.5 else 0.8
        doppler_penalty = 1.0 if doppler_std_hz < 0.2 else 0.8
        
        # Update calibration if we have a confident solution (Issue 1.2 fix)
        # This allows the ionospheric model to learn from actual measurements
        if self.iono_model is not None and utc_verified and confidence > 0.7:
            self.iono_model.update_calibration(
                latitude=self.receiver_lat,
                longitude=self.receiver_lon,
                timestamp=timestamp,
                observed_delay_ms=observed_delay_ms,
                predicted_delay_ms=best_candidate.total_delay_ms,
                ground_distance_km=ground_distance,
                n_hops=best_candidate.n_hops,
                confidence=confidence
            )
            logger.debug(f"Calibration updated: observed={observed_delay_ms:.2f}ms, "
                        f"predicted={best_candidate.total_delay_ms:.2f}ms")
        
        return SolverResult(
            arrival_rtp=arrival_rtp,
            emission_rtp=emission_rtp,
            emission_offset_ms=emission_offset_ms,
            propagation_delay_ms=best_candidate.total_delay_ms,
            mode=best_candidate.mode,
            mode_name=mode_names.get(best_candidate.mode, "Unknown"),
            n_hops=best_candidate.n_hops,
            layer_height_km=best_candidate.layer_height_km,
            elevation_angle_deg=best_candidate.elevation_angle_deg,
            confidence=confidence,
            mode_separation_ms=delay_separation_ms,
            delay_spread_penalty=delay_spread_penalty,
            doppler_penalty=doppler_penalty,
            fss_consistency=1.0,  # TODO: Implement
            candidates=[c for _, c in scored_candidates],
            utc_nist_offset_ms=emission_offset_ms if expected_second_rtp is not None else None,
            utc_nist_verified=utc_verified
        )
    
    def _no_solution(self, arrival_rtp: int) -> SolverResult:
        """Return a result indicating no valid solution."""
        return SolverResult(
            arrival_rtp=arrival_rtp,
            emission_rtp=arrival_rtp,
            emission_offset_ms=None,     # Changed from 0.0
            propagation_delay_ms=None,   # Changed from 0.0
            mode=PropagationMode.UNKNOWN,
            mode_name="Unknown",
            n_hops=0,
            layer_height_km=0.0,
            elevation_angle_deg=0.0,
            confidence=0.0,
            mode_separation_ms=0.0,
            delay_spread_penalty=1.0,
            doppler_penalty=1.0,
            fss_consistency=0.0,
            candidates=[],
            utc_nist_offset_ms=None,
            utc_nist_verified=False
        )
    
    def solve_multi_frequency(
        self,
        station: str,
        observations: List[Dict],
        expected_second_rtp: int
    ) -> SolverResult:
        """
        Solve using observations from multiple frequencies.
        
        This provides higher confidence by requiring mode consistency
        across frequencies. Different frequencies may use different modes
        but should all point to the same emission time.
        
        Args:
            station: 'WWV', 'WWVH', or 'CHU'
            observations: List of dicts with keys:
                - frequency_mhz
                - arrival_rtp
                - delay_spread_ms (optional)
                - doppler_std_hz (optional)
                - fss_db (optional)
                - snr_db (optional, for weighting)
            expected_second_rtp: RTP timestamp of expected second boundary
            
        Returns:
            Combined SolverResult with multi-frequency confidence
        """
        if not observations:
            raise ValueError("No observations provided")
        
        # Solve each frequency independently
        results = []
        for obs in observations:
            result = self.solve(
                station=station,
                frequency_mhz=obs['frequency_mhz'],
                arrival_rtp=obs['arrival_rtp'],
                delay_spread_ms=obs.get('delay_spread_ms', 0.0),
                doppler_std_hz=obs.get('doppler_std_hz', 0.0),
                fss_db=obs.get('fss_db'),
                expected_second_rtp=expected_second_rtp
            )
            snr = obs.get('snr_db', 10.0)
            results.append((result, snr))
        
        # Weight by SNR and confidence
        weighted_emission_offset = 0.0
        total_weight = 0.0
        
        for result, snr in results:
            if result.confidence > 0.3:  # Only use confident results
                weight = result.confidence * max(1.0, snr / 10.0)
                weighted_emission_offset += result.emission_offset_ms * weight
                total_weight += weight
        
        if total_weight > 0:
            combined_offset_ms = weighted_emission_offset / total_weight
        else:
            combined_offset_ms = results[0][0].emission_offset_ms
        
        # Check consistency: all frequencies should give similar emission times
        offsets = [r.emission_offset_ms for r, _ in results if r.confidence > 0.3]
        if offsets:
            offset_spread = max(offsets) - min(offsets)
            consistency_bonus = 1.0 if offset_spread < 1.0 else 0.7
        else:
            consistency_bonus = 0.5
        
        # Use the highest-confidence single result as base
        best_result, best_snr = max(results, key=lambda x: x[0].confidence * x[1])
        
        # Boost confidence based on multi-frequency agreement
        combined_confidence = min(1.0, best_result.confidence * consistency_bonus * 1.2)
        
        # Verify UTC(NIST) with combined offset
        utc_verified = abs(combined_offset_ms) < 1.5  # Tighter threshold for multi-freq
        
        return SolverResult(
            arrival_rtp=best_result.arrival_rtp,
            emission_rtp=best_result.emission_rtp,
            emission_offset_ms=combined_offset_ms,
            propagation_delay_ms=best_result.propagation_delay_ms,
            mode=best_result.mode,
            mode_name=best_result.mode_name + " (multi-freq)",
            n_hops=best_result.n_hops,
            layer_height_km=best_result.layer_height_km,
            elevation_angle_deg=best_result.elevation_angle_deg,
            confidence=combined_confidence,
            mode_separation_ms=best_result.mode_separation_ms,
            delay_spread_penalty=best_result.delay_spread_penalty,
            doppler_penalty=best_result.doppler_penalty,
            fss_consistency=consistency_bonus,
            candidates=best_result.candidates,
            utc_nist_offset_ms=combined_offset_ms,
            utc_nist_verified=utc_verified
        )


@dataclass
class CombinedUTCResult:
    """
    Combined UTC(NIST) estimate from multiple stations/frequencies.
    
    This is the "Holy Grail" result - a primary time standard from
    passive HF reception by correlating multiple independent measurements.
    """
    # Combined estimate
    utc_offset_ms: float  # Best estimate of UTC(NIST) offset from local clock
    uncertainty_ms: float  # 1-sigma uncertainty
    
    # Confidence and quality
    confidence: float  # 0-1 overall confidence
    consistency: float  # 0-1 how well measurements agree
    n_measurements: int  # Number of independent measurements used
    n_stations: int  # Number of distinct stations
    
    # Individual measurements
    individual_results: List[Dict]  # Per-station/freq results
    
    # Outlier info
    outliers_rejected: int
    
    # Verification
    verified: bool  # True if uncertainty < 2ms and consistency > 0.7
    quality_grade: str  # "A" (sub-ms), "B" (1-2ms), "C" (2-5ms), "D" (>5ms)


class MultiStationSolver:
    """
    Correlate UTC(NIST) estimates from multiple stations and frequencies.
    
    By combining WWV, WWVH, and CHU observations, we can:
    1. Reject outliers from incorrect mode identification
    2. Reduce uncertainty through averaging
    3. Detect systematic errors (e.g., wrong ionospheric model)
    4. Achieve sub-millisecond timing accuracy
    """
    
    def __init__(self, solver: TransmissionTimeSolver):
        """
        Initialize with a base solver (already has receiver location).
        """
        self.solver = solver
        self.pending_observations: List[Dict] = []
    
    def add_observation(
        self,
        station: str,
        frequency_mhz: float,
        arrival_rtp: int,
        expected_second_rtp: int,
        snr_db: float = 10.0,
        delay_spread_ms: float = 0.0,
        doppler_std_hz: float = 0.0,
        fss_db: Optional[float] = None
    ):
        """
        Add an observation to the pending set.
        
        Call this for each detected station/frequency, then call solve_combined().
        """
        self.pending_observations.append({
            'station': station,
            'frequency_mhz': frequency_mhz,
            'arrival_rtp': arrival_rtp,
            'expected_second_rtp': expected_second_rtp,
            'snr_db': snr_db,
            'delay_spread_ms': delay_spread_ms,
            'doppler_std_hz': doppler_std_hz,
            'fss_db': fss_db
        })
    
    def clear_observations(self):
        """Clear pending observations for new minute."""
        self.pending_observations = []
    
    def solve_combined(self) -> CombinedUTCResult:
        """
        Solve for combined UTC(NIST) using all pending observations.
        
        Uses weighted least-squares to find the UTC offset that best
        explains all observations, accounting for different propagation
        modes at each station/frequency.
        """
        if not self.pending_observations:
            return self._no_combined_solution()
        
        # Solve each observation independently
        individual_results = []
        for obs in self.pending_observations:
            try:
                result = self.solver.solve(
                    station=obs['station'],
                    frequency_mhz=obs['frequency_mhz'],
                    arrival_rtp=obs['arrival_rtp'],
                    delay_spread_ms=obs['delay_spread_ms'],
                    doppler_std_hz=obs['doppler_std_hz'],
                    fss_db=obs['fss_db'],
                    expected_second_rtp=obs['expected_second_rtp']
                )
                
                individual_results.append({
                    'station': obs['station'],
                    'frequency_mhz': obs['frequency_mhz'],
                    'snr_db': obs['snr_db'],
                    'mode': result.mode.value,
                    'mode_name': result.mode_name,
                    'n_hops': result.n_hops,
                    'propagation_delay_ms': result.propagation_delay_ms,
                    'utc_offset_ms': result.utc_nist_offset_ms,
                    'confidence': result.confidence,
                    'elevation_deg': result.elevation_angle_deg
                })
            except Exception as e:
                logger.warning(f"Failed to solve {obs['station']} {obs['frequency_mhz']} MHz: {e}")
        
        if not individual_results:
            return self._no_combined_solution()
        
        # Filter to confident results
        confident_results = [r for r in individual_results 
                           if r['confidence'] > 0.3 and r['utc_offset_ms'] is not None]
        
        if not confident_results:
            # Fall back to best individual result
            best = max(individual_results, key=lambda r: r['confidence'])
            return CombinedUTCResult(
                utc_offset_ms=best['utc_offset_ms'] or 0.0,
                uncertainty_ms=5.0,
                confidence=best['confidence'] * 0.5,
                consistency=0.0,
                n_measurements=1,
                n_stations=1,
                individual_results=individual_results,
                outliers_rejected=0,
                verified=False,
                quality_grade='D'
            )
        
        # Weighted average with outlier rejection
        offsets = [r['utc_offset_ms'] for r in confident_results]
        weights = [r['confidence'] * max(1.0, r['snr_db'] / 10.0) for r in confident_results]
        
        # Iterative outlier rejection (2-sigma)
        outliers_rejected = 0
        for _ in range(3):  # Max 3 iterations
            if len(offsets) < 2:
                break
            
            weighted_mean = sum(o * w for o, w in zip(offsets, weights)) / sum(weights)
            residuals = [abs(o - weighted_mean) for o in offsets]
            
            # Estimate std from median absolute deviation (robust)
            mad = sorted(residuals)[len(residuals) // 2]
            sigma = mad * 1.4826  # MAD to std conversion
            
            if sigma < 0.1:
                sigma = 0.1  # Minimum std to avoid over-rejection
            
            # Reject outliers > 2 sigma
            new_offsets = []
            new_weights = []
            for o, w, r in zip(offsets, weights, residuals):
                if r < 2 * sigma:
                    new_offsets.append(o)
                    new_weights.append(w)
                else:
                    outliers_rejected += 1
            
            if len(new_offsets) == len(offsets):
                break  # No more outliers
            offsets = new_offsets
            weights = new_weights
        
        # Final weighted average
        if offsets:
            total_weight = sum(weights)
            combined_offset = sum(o * w for o, w in zip(offsets, weights)) / total_weight
            
            # Uncertainty from weighted std
            if len(offsets) > 1:
                variance = sum(w * (o - combined_offset)**2 for o, w in zip(offsets, weights)) / total_weight
                uncertainty = math.sqrt(variance) / math.sqrt(len(offsets))  # Standard error
            else:
                uncertainty = 2.0  # Single measurement, conservative
        else:
            combined_offset = 0.0
            uncertainty = 10.0
        
        # Calculate consistency (how well measurements agree)
        if len(offsets) > 1:
            spread = max(offsets) - min(offsets)
            consistency = max(0.0, 1.0 - spread / 3.0)  # 0 spread = 1.0, 3ms spread = 0.0
        else:
            consistency = 0.5
        
        # Count distinct stations
        stations_used = set(r['station'] for r in confident_results 
                          if r['utc_offset_ms'] in offsets)
        n_stations = len(stations_used)
        
        # Calculate combined confidence
        avg_individual_conf = sum(r['confidence'] for r in confident_results) / len(confident_results)
        multi_station_bonus = 1.0 + 0.1 * (n_stations - 1)  # Bonus for multiple stations
        combined_confidence = min(1.0, avg_individual_conf * consistency * multi_station_bonus)
        
        # Determine quality grade
        if uncertainty < 0.5 and consistency > 0.8:
            quality_grade = 'A'  # Excellent - sub-millisecond
        elif uncertainty < 1.5 and consistency > 0.6:
            quality_grade = 'B'  # Good
        elif uncertainty < 3.0:
            quality_grade = 'C'  # Fair
        else:
            quality_grade = 'D'  # Poor
        
        # Verify
        verified = uncertainty < 2.0 and consistency > 0.5 and n_stations >= 1
        
        return CombinedUTCResult(
            utc_offset_ms=combined_offset,
            uncertainty_ms=uncertainty,
            confidence=combined_confidence,
            consistency=consistency,
            n_measurements=len(offsets),
            n_stations=n_stations,
            individual_results=individual_results,
            outliers_rejected=outliers_rejected,
            verified=verified,
            quality_grade=quality_grade
        )
    
    def _no_combined_solution(self) -> CombinedUTCResult:
        """Return empty result when no observations available."""
        return CombinedUTCResult(
            utc_offset_ms=0.0,
            uncertainty_ms=999.0,
            confidence=0.0,
            consistency=0.0,
            n_measurements=0,
            n_stations=0,
            individual_results=[],
            outliers_rejected=0,
            verified=False,
            quality_grade='D'
        )


# Convenience function for quick use
def create_solver_from_grid(
    grid_square: str, 
    sample_rate: int = 20000,
    precise_lat: Optional[float] = None,
    precise_lon: Optional[float] = None
) -> TransmissionTimeSolver:
    """
    Create a TransmissionTimeSolver from a Maidenhead grid square.
    
    Args:
        grid_square: 4 or 6 character grid square (e.g., "EM38" or "EM38ww")
        sample_rate: Audio sample rate
        precise_lat: Optional precise latitude (overrides grid square)
        precise_lon: Optional precise longitude (overrides grid square)
        
    Returns:
        Configured TransmissionTimeSolver
        
    Note:
        Using precise coordinates improves timing accuracy by ~16μs for
        6-character grid squares. The grid square center can be up to
        4.3 km from your actual position.
    """
    if precise_lat is not None and precise_lon is not None:
        # Use precise coordinates for better timing accuracy
        lat, lon = precise_lat, precise_lon
        logger.info(f"Using precise coordinates: {lat:.6f}°N, {lon:.6f}°W")
    else:
        # Fall back to grid square center
        lat, lon = grid_to_latlon(grid_square)
        logger.info(f"Using grid square {grid_square} center: {lat:.4f}°N, {lon:.4f}°W")
    
    return TransmissionTimeSolver(lat, lon, sample_rate)


def grid_to_latlon(grid: str) -> Tuple[float, float]:
    """Convert Maidenhead grid square to latitude/longitude."""
    grid = grid.upper()
    
    lon = (ord(grid[0]) - ord('A')) * 20 - 180
    lat = (ord(grid[1]) - ord('A')) * 10 - 90
    
    lon += (ord(grid[2]) - ord('0')) * 2
    lat += (ord(grid[3]) - ord('0')) * 1
    
    if len(grid) >= 6:
        lon += (ord(grid[4].lower()) - ord('a')) * (2/24) + (1/24)
        lat += (ord(grid[5].lower()) - ord('a')) * (1/24) + (1/48)
    else:
        lon += 1  # Center of grid
        lat += 0.5
    
    return lat, lon


def create_multi_station_solver(grid_square: str, sample_rate: int = 20000) -> MultiStationSolver:
    """
    Create a MultiStationSolver for correlating WWV/WWVH/CHU.
    
    Args:
        grid_square: Receiver location (e.g., "EM38ww")
        sample_rate: Audio sample rate
        
    Returns:
        Configured MultiStationSolver ready to accept observations
    """
    base_solver = create_solver_from_grid(grid_square, sample_rate)
    return MultiStationSolver(base_solver)
