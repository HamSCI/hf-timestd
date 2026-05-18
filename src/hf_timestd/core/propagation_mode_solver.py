#!/usr/bin/env python3
"""
Propagation Mode Solver - HF Time Transfer via Mode Identification

This module implements "back calculation" of emission time by identifying
the ionospheric propagation mode (number of hops) and computing the
precise propagation delay.

The Key Insight:
----------------
Propagation modes are DISCRETE. A signal either takes 1 hop, 2 hops, etc.
By calculating the delay for each possible mode and matching to our measured
arrival time, we can identify the mode and subtract the precise delay.

This transforms our receiver from a passive listener into a PRIMARY TIME STANDARD
that can verify UTC(NIST) directly.

The Equation:
-------------
    T_emit = T_arrival - T_prop
    
    T_prop = τ_geo + τ_iono + τ_mode
    
Where:
    τ_geo:  Great circle speed-of-light delay (fixed by geometry)
    τ_iono: Ionospheric refractive delay (electron density dependent)
    τ_mode: Extra path length from N ionospheric reflections

Mode Geometry:
--------------
For an N-hop F2 layer path:

    Total path = N * 2 * sqrt((h_F2)² + (d/(2N))²)
    
Where:
    h_F2: F2 layer virtual height (~250-400 km)
    d:    Great circle ground distance
    N:    Number of hops

Typical Values (WWV to EM38ww, ~1200 km):
    Ground wave: 4.0 ms
    1-hop F2:    4.3 ms  (Δ = 0.3 ms from ground)
    2-hop F2:    4.8 ms  (Δ = 0.5 ms from 1-hop)
    3-hop F2:    5.5 ms  (Δ = 0.7 ms from 2-hop)

Usage:
------
    solver = PropagationModeSolver(receiver_grid='EM38ww')
    
    # Get all possible modes for a path
    modes = solver.calculate_modes('WWV', frequency_mhz=10.0)
    
    # Identify most likely mode from arrival time
    result = solver.identify_mode(
        station='WWV',
        measured_delay_ms=4.5,
        frequency_mhz=10.0,
        channel_metrics=metrics  # delay_spread, doppler_std, fss
    )
    
    # Back-calculate emission time
    emission = solver.back_calculate_emission_time(
        station='WWV',
        arrival_rtp=rtp_timestamp,
        time_snap=time_snap,
        frequency_mhz=10.0
    )
"""

import logging
import numpy as np
from typing import Optional, Dict, List, Tuple, Any
from dataclasses import dataclass, field
from enum import Enum
import math

# Issue 4.1 Fix (2025-12-07): Import coordinates from single source of truth
from .wwv_constants import (
    WWV_LAT, WWV_LON, WWVH_LAT, WWVH_LON, CHU_LAT, CHU_LON, BPM_LAT, BPM_LON
)

logger = logging.getLogger(__name__)

# Physical constants
SPEED_OF_LIGHT_KM_S = 299792.458  # km/s
EARTH_RADIUS_KM = 6371.0  # Mean Earth radius

# Ionospheric layer heights (typical, varies with solar conditions)
D_LAYER_HEIGHT_KM = 70.0   # Daytime only, absorbs HF
E_LAYER_HEIGHT_KM = 110.0  # Sporadic E, 1-3 MHz reflection
F1_LAYER_HEIGHT_KM = 200.0  # Daytime, merges with F2 at night
F2_LAYER_HEIGHT_KM = 300.0  # Primary HF reflection layer

# Transmitter locations (latitude, longitude in degrees)
# Issue 4.1 Fix: Now imported from wwv_constants.py (NIST/NRC verified)
STATION_LOCATIONS = {
    'WWV': (WWV_LAT, WWV_LON),     # Fort Collins, Colorado - NIST verified
    'WWVH': (WWVH_LAT, WWVH_LON),  # Kekaha, Kauai, Hawaii - NIST verified
    'CHU': (CHU_LAT, CHU_LON),     # Ottawa, Canada - NRC verified
    'BPM': (BPM_LAT, BPM_LON),     # Pucheng, China - NTSC verified
}

# Ionospheric group delay coefficient (TEC-dependent)
# Typical midlatitude daytime: ~1-3 µs/MHz² (we use average)
IONO_DELAY_COEFF_US_MHZ2 = 2.0  # µs per MHz² (approximate)


class PropagationMode(Enum):
    """Ionospheric propagation modes"""
    GROUND_WAVE = "ground_wave"
    E_LAYER_1HOP = "1E"
    E_LAYER_2HOP = "2E"
    F1_LAYER_1HOP = "1F1"
    F2_LAYER_1HOP = "1F2"
    F2_LAYER_2HOP = "2F2"
    F2_LAYER_3HOP = "3F2"
    F2_LAYER_4HOP = "4F2"
    MIXED_MODE = "mixed"  # Multiple paths present
    UNKNOWN = "unknown"


@dataclass
class ModeCandidate:
    """
    A candidate propagation mode with calculated parameters.
    """
    mode: PropagationMode
    n_hops: int
    layer_height_km: float
    
    # Path geometry
    ground_distance_km: float
    path_length_km: float
    elevation_angle_deg: float  # Launch/arrival angle
    
    # Timing
    propagation_delay_ms: float
    ionospheric_delay_ms: float  # Frequency-dependent group delay
    total_delay_ms: float
    
    # Uncertainty
    delay_uncertainty_ms: float  # Based on layer height variability
    
    # Viability
    viable: bool = True  # False if geometry impossible (e.g., too steep)
    muf_limited: bool = False  # True if frequency > MUF for this mode

    # Confidence of the propagation model that produced this candidate (0-1).
    # Tier-1 sets it from the HFPropagationModel prediction (iono-data quality:
    # wamipe/giro/iri/fallback); the parametric Tier-2 fallback leaves it 0,
    # i.e. treated as low-confidence.
    model_confidence: float = 0.0

    def __str__(self) -> str:
        return (f"{self.mode.value}: {self.total_delay_ms:.2f} ms "
                f"(±{self.delay_uncertainty_ms:.2f} ms), "
                f"elev={self.elevation_angle_deg:.1f}°")


@dataclass
class ModeIdentificationResult:
    """
    Result of mode identification from measured timing.
    """
    # Best-fit mode
    identified_mode: PropagationMode
    n_hops: int
    confidence: float  # 0-1
    
    # Timing
    calculated_delay_ms: float
    measured_delay_ms: float
    residual_ms: float  # measured - calculated
    
    # Back-calculated emission time
    emission_time_utc: Optional[float] = None  # Unix timestamp
    emission_time_accuracy_ms: float = 1.0  # Estimated accuracy
    
    # All candidates considered
    candidates: List[ModeCandidate] = field(default_factory=list)
    
    # Quality indicators from channel metrics
    multipath_detected: bool = False
    path_stability: float = 1.0  # 0=unstable, 1=stable
    
    # Flags
    ambiguous: bool = False  # True if multiple modes fit equally well
    low_confidence: bool = False
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'mode': self.identified_mode.value,
            'n_hops': self.n_hops,
            'confidence': self.confidence,
            'calculated_delay_ms': self.calculated_delay_ms,
            'measured_delay_ms': self.measured_delay_ms,
            'residual_ms': self.residual_ms,
            'emission_time_utc': self.emission_time_utc,
            'emission_time_accuracy_ms': self.emission_time_accuracy_ms,
            'multipath_detected': self.multipath_detected,
            'path_stability': self.path_stability,
            'ambiguous': self.ambiguous
        }


@dataclass
class EmissionTimeResult:
    """
    Back-calculated emission time representing UTC(NIST/NRC).
    
    This is the "Holy Grail" - a precise determination of when
    the transmitter emitted the signal, derived from:
    1. GPS-locked arrival time (RTP timestamp)
    2. Identified propagation mode
    3. Calculated propagation delay
    """
    # The main result
    emission_time_utc: float  # Unix timestamp of emission
    
    # Components
    arrival_time_utc: float  # Measured arrival (from RTP + time_snap)
    propagation_delay_ms: float  # Total calculated delay
    
    # Mode information
    mode: PropagationMode
    n_hops: int
    
    # Quality
    accuracy_ms: float  # Estimated accuracy of emission time
    confidence: float  # 0-1 confidence in mode identification
    
    # Source station
    station: str
    frequency_mhz: float
    
    # Verification
    expected_second_offset_ms: float  # How far from integer second
    second_aligned: bool  # True if within tolerance of second boundary
    
    def utc_nist_offset_ms(self) -> float:
        """
        Offset from UTC(NIST) second boundary in milliseconds.
        
        If our back-calculation is correct, this should be very close
        to 0 (or the known transmitter delay, typically <100 µs).
        """
        fractional_second = self.emission_time_utc % 1.0
        if fractional_second > 0.5:
            fractional_second -= 1.0
        return fractional_second * 1000.0
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'emission_time_utc': self.emission_time_utc,
            'arrival_time_utc': self.arrival_time_utc,
            'propagation_delay_ms': self.propagation_delay_ms,
            'mode': self.mode.value,
            'n_hops': self.n_hops,
            'accuracy_ms': self.accuracy_ms,
            'confidence': self.confidence,
            'station': self.station,
            'frequency_mhz': self.frequency_mhz,
            'utc_nist_offset_ms': self.utc_nist_offset_ms(),
            'second_aligned': self.second_aligned
        }


class PropagationModeSolver:
    """
    Solves for ionospheric propagation mode and back-calculates emission time.

    Tier-1 (when available): HFPropagationModel with real-time IonoDataService
    data (WAM-IPE foF2/hmF2, MUF checks, frequency-dependent iono delay).
    Tier-2 (fallback): Fixed F2/E layer heights with parametric iono delay.
    All downstream callers receive the same ModeCandidate interface.
    """

    def __init__(
        self,
        receiver_grid: str,
        f2_height_km: float = F2_LAYER_HEIGHT_KM,
        e_height_km: float = E_LAYER_HEIGHT_KM
    ):
        """
        Initialize solver with receiver location.

        Args:
            receiver_grid: Maidenhead grid square (e.g., 'EM38ww')
            f2_height_km: Fallback F2 layer height when HFPropagationModel unavailable
            e_height_km: Fallback E layer height
        """
        self.receiver_grid = receiver_grid
        self.receiver_lat, self.receiver_lon = self._grid_to_latlon(receiver_grid)

        self.f2_height_km = f2_height_km
        self.e_height_km = e_height_km

        # Cache great-circle distances to stations
        self._distances: Dict[str, float] = {}
        for station, (lat, lon) in STATION_LOCATIONS.items():
            self._distances[station] = self._great_circle_distance(
                self.receiver_lat, self.receiver_lon, lat, lon
            )

        # Tier-1: HFPropagationModel with real-time ionospheric data (P2-A)
        self._hf_model = None
        try:
            from .propagation_model import HFPropagationModel
            self._hf_model = HFPropagationModel(
                receiver_lat=self.receiver_lat,
                receiver_lon=self.receiver_lon,
                enable_realtime=True,
            )
            logger.info(
                f"PropagationModeSolver: HFPropagationModel active "
                f"(real-time foF2/hmF2/MUF)"
            )
        except Exception as e:
            logger.warning(
                f"PropagationModeSolver: HFPropagationModel unavailable "
                f"({e}), using fixed-height fallback"
            )

        logger.info(f"PropagationModeSolver initialized at {receiver_grid}")
        logger.info(
            f"Distances: WWV={self._distances.get('WWV', 0):.0f} km, "
            f"WWVH={self._distances.get('WWVH', 0):.0f} km, "
            f"CHU={self._distances.get('CHU', 0):.0f} km"
        )
    
    def _grid_to_latlon(self, grid: str) -> Tuple[float, float]:
        """Convert Maidenhead grid to lat/lon"""
        grid = grid.upper()
        
        if len(grid) < 4:
            raise ValueError(f"Grid square too short: {grid}")
        
        lon = (ord(grid[0]) - ord('A')) * 20 - 180
        lat = (ord(grid[1]) - ord('A')) * 10 - 90
        lon += (ord(grid[2]) - ord('0')) * 2
        lat += (ord(grid[3]) - ord('0'))
        
        if len(grid) >= 6:
            lon += (ord(grid[4].upper()) - ord('A')) * (2/24)
            lat += (ord(grid[5].upper()) - ord('A')) * (1/24)
        
        # Center of grid square
        lon += 1.0 if len(grid) < 6 else 1/24
        lat += 0.5 if len(grid) < 6 else 1/48
        
        return lat, lon
    
    def _great_circle_distance(
        self,
        lat1: float, lon1: float,
        lat2: float, lon2: float
    ) -> float:
        """Calculate great-circle distance in km using Haversine formula"""
        lat1_rad = math.radians(lat1)
        lat2_rad = math.radians(lat2)
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        
        a = (math.sin(dlat/2)**2 + 
             math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon/2)**2)
        c = 2 * math.asin(math.sqrt(a))
        
        return EARTH_RADIUS_KM * c
    
    def _hop_geometry(
        self,
        ground_distance_km: float,
        layer_height_km: float,
        n_hops: int
    ) -> Tuple[float, float]:
        """
        Calculate path length and elevation angle for N-hop propagation.
        
        Args:
            ground_distance_km: Great-circle ground distance
            layer_height_km: Ionospheric layer height
            n_hops: Number of ionospheric reflections
            
        Returns:
            (path_length_km, elevation_angle_deg)
        """
        if n_hops == 0:
            # Ground wave (follows Earth curvature, approximately)
            return ground_distance_km, 0.0
        
        # Each hop covers ground_distance / n_hops
        hop_ground_distance = ground_distance_km / n_hops
        
        # For a single hop, the path forms a triangle:
        # - Base: hop_ground_distance
        # - Height: layer_height_km
        # - Slant path up + slant path down
        
        # Half the ground distance per hop
        half_hop_distance = hop_ground_distance / 2
        
        # Slant distance (one way, up or down)
        slant_distance = math.sqrt(layer_height_km**2 + half_hop_distance**2)
        
        # Total path for one hop = 2 * slant_distance
        # Total path for N hops = N * 2 * slant_distance
        path_length_km = n_hops * 2 * slant_distance
        
        # Elevation angle (at transmitter/receiver)
        elevation_angle_deg = math.degrees(math.atan(layer_height_km / half_hop_distance))
        
        return path_length_km, elevation_angle_deg
    
    def _ionospheric_group_delay_ms(self, frequency_mhz: float) -> float:
        """
        Calculate ionospheric group delay.
        
        Group delay varies as 1/f² and depends on TEC.
        Higher frequencies have less delay.
        
        Args:
            frequency_mhz: Signal frequency in MHz
            
        Returns:
            Group delay in milliseconds
        """
        # Simple 1/f² model
        # At 10 MHz, typical delay ~20 µs per hop
        # Delay ∝ 1/f²
        delay_us = IONO_DELAY_COEFF_US_MHZ2 / (frequency_mhz**2) * 1000
        return delay_us / 1000.0  # Convert to ms
    
    def _hf_model_candidates(
        self,
        station: str,
        frequency_mhz: float,
    ) -> Optional[List['ModeCandidate']]:
        """
        Tier-1: Get ModeCandidate list from HFPropagationModel.

        Converts HFPropagationModel.ModeArrival objects into ModeCandidate
        objects so all downstream callers are unchanged.  Returns None if
        HFPropagationModel is unavailable or produces no feasible arrivals.
        """
        if self._hf_model is None:
            return None
        try:
            from datetime import datetime, timezone
            from .propagation_model import PROPAGATION_MODES
            prediction = self._hf_model.predict(
                station=station,
                frequency_mhz=frequency_mhz,
                utc_time=datetime.now(timezone.utc),
            )
            feasible = prediction.get_feasible_arrivals()
            if not feasible:
                return None

            # Map layer label → PropagationMode enum used by ModeCandidate
            _mode_map = {
                ('1', 'F'): PropagationMode.F2_LAYER_1HOP,
                ('2', 'F'): PropagationMode.F2_LAYER_2HOP,
                ('3', 'F'): PropagationMode.F2_LAYER_3HOP,
                ('4', 'F'): PropagationMode.F2_LAYER_4HOP,
                ('1', 'E'): PropagationMode.E_LAYER_1HOP,
                ('2', 'E'): PropagationMode.E_LAYER_2HOP,
            }

            candidates = []
            for arr in feasible:
                key = (str(arr.mode.n_hops), arr.mode.layer)
                mode_enum = _mode_map.get(key, PropagationMode.UNKNOWN)
                candidates.append(ModeCandidate(
                    mode=mode_enum,
                    n_hops=arr.mode.n_hops,
                    layer_height_km=arr.reflection_height_km,
                    ground_distance_km=self._distances.get(station, 0.0),
                    path_length_km=arr.path_length_km,
                    elevation_angle_deg=arr.elevation_angle_deg,
                    propagation_delay_ms=arr.geometric_delay_ms,
                    ionospheric_delay_ms=arr.iono_delay_ms,
                    total_delay_ms=arr.delay_ms,
                    delay_uncertainty_ms=arr.uncertainty_1sigma_ms,
                    viable=arr.is_feasible,
                    model_confidence=prediction.model_confidence,
                ))
            candidates.sort(key=lambda c: c.total_delay_ms)
            logger.debug(
                f"HFPropagationModel [{prediction.data_source}] "
                f"{station} {frequency_mhz:.2f} MHz: "
                f"{len(candidates)} feasible modes, "
                f"primary={prediction.primary_mode} "
                f"{prediction.primary_delay_ms:.2f} ms "
                f"(conf={prediction.model_confidence:.2f})"
            )
            return candidates
        except Exception as e:
            logger.debug(f"HFPropagationModel failed for {station}/{frequency_mhz}: {e}")
            return None

    def calculate_modes(
        self,
        station: str,
        frequency_mhz: float,
        max_hops: int = 4,
        include_e_layer: bool = True
    ) -> List[ModeCandidate]:
        """
        Calculate all possible propagation modes for a path.

        Tier-1: HFPropagationModel (real foF2/hmF2, MUF checks) when available.
        Tier-2: Fixed F2/E layer heights with parametric iono delay (fallback).

        Args:
            station: Station name ('WWV', 'WWVH', 'CHU')
            frequency_mhz: Signal frequency in MHz
            max_hops: Maximum number of hops to consider (tier-2 only)
            include_e_layer: Whether to include E-layer modes (tier-2 only)

        Returns:
            List of ModeCandidate objects, sorted by delay
        """
        if station not in self._distances:
            raise ValueError(f"Unknown station: {station}")

        # Tier-1: HFPropagationModel with real ionospheric data
        hf_candidates = self._hf_model_candidates(station, frequency_mhz)
        if hf_candidates:
            return hf_candidates

        # Tier-2: Fixed-height fallback (original implementation)
        ground_distance = self._distances[station]
        candidates = []
        
        # Ground wave (only viable for short distances)
        if ground_distance < 300:  # Ground wave practical < 300 km
            path_length = ground_distance
            delay_ms = path_length / SPEED_OF_LIGHT_KM_S * 1000
            
            candidates.append(ModeCandidate(
                mode=PropagationMode.GROUND_WAVE,
                n_hops=0,
                layer_height_km=0,
                ground_distance_km=ground_distance,
                path_length_km=path_length,
                elevation_angle_deg=0,
                propagation_delay_ms=delay_ms,
                ionospheric_delay_ms=0,
                total_delay_ms=delay_ms,
                delay_uncertainty_ms=0.1,
                viable=True
            ))
        
        # F2 layer modes (1 to max_hops)
        for n in range(1, max_hops + 1):
            path_length, elev_angle = self._hop_geometry(
                ground_distance, self.f2_height_km, n
            )
            
            # Check if geometry is viable (elevation > 0°)
            if elev_angle < 5:  # Too low angle
                continue
            
            # Geometric delay
            geo_delay_ms = path_length / SPEED_OF_LIGHT_KM_S * 1000
            
            # Ionospheric delay (per hop)
            iono_delay_ms = self._ionospheric_group_delay_ms(frequency_mhz) * n
            
            # Total delay
            total_delay_ms = geo_delay_ms + iono_delay_ms
            
            # Uncertainty increases with hops (layer height variability)
            # F2 layer height varies ±50 km typically
            height_uncertainty_km = 50.0
            delay_uncertainty_ms = n * (height_uncertainty_km / SPEED_OF_LIGHT_KM_S * 1000)
            
            mode = {
                1: PropagationMode.F2_LAYER_1HOP,
                2: PropagationMode.F2_LAYER_2HOP,
                3: PropagationMode.F2_LAYER_3HOP,
                4: PropagationMode.F2_LAYER_4HOP
            }.get(n, PropagationMode.UNKNOWN)
            
            candidates.append(ModeCandidate(
                mode=mode,
                n_hops=n,
                layer_height_km=self.f2_height_km,
                ground_distance_km=ground_distance,
                path_length_km=path_length,
                elevation_angle_deg=elev_angle,
                propagation_delay_ms=geo_delay_ms,
                ionospheric_delay_ms=iono_delay_ms,
                total_delay_ms=total_delay_ms,
                delay_uncertainty_ms=delay_uncertainty_ms,
                viable=True
            ))
        
        # E layer modes (usually only 1-hop for short paths)
        if include_e_layer and ground_distance < 2000:
            for n in range(1, min(3, max_hops + 1)):
                path_length, elev_angle = self._hop_geometry(
                    ground_distance, self.e_height_km, n
                )
                
                if elev_angle < 10:  # E layer requires higher angles
                    continue
                
                geo_delay_ms = path_length / SPEED_OF_LIGHT_KM_S * 1000
                iono_delay_ms = self._ionospheric_group_delay_ms(frequency_mhz) * n * 0.5  # E layer less dense
                total_delay_ms = geo_delay_ms + iono_delay_ms
                
                mode = {
                    1: PropagationMode.E_LAYER_1HOP,
                    2: PropagationMode.E_LAYER_2HOP
                }.get(n, PropagationMode.UNKNOWN)
                
                candidates.append(ModeCandidate(
                    mode=mode,
                    n_hops=n,
                    layer_height_km=self.e_height_km,
                    ground_distance_km=ground_distance,
                    path_length_km=path_length,
                    elevation_angle_deg=elev_angle,
                    propagation_delay_ms=geo_delay_ms,
                    ionospheric_delay_ms=iono_delay_ms,
                    total_delay_ms=total_delay_ms,
                    delay_uncertainty_ms=0.2 * n,
                    viable=True
                ))
        
        # Sort by total delay
        candidates.sort(key=lambda c: c.total_delay_ms)
        
        return candidates
    
    def identify_mode(
        self,
        station: str,
        measured_delay_ms: float,
        frequency_mhz: float,
        channel_metrics: Optional[Dict[str, float]] = None
    ) -> ModeIdentificationResult:
        """
        Identify the most likely propagation mode from measured timing.
        
        Uses measured arrival delay and optional channel quality metrics
        to determine which mode best fits the data.
        
        Args:
            station: Station name
            measured_delay_ms: Measured propagation delay in ms
            frequency_mhz: Signal frequency
            channel_metrics: Optional dict with:
                - delay_spread_ms: Multipath delay spread
                - doppler_std_hz: Doppler standard deviation
                - fss_db: Frequency Selectivity Score
                
        Returns:
            ModeIdentificationResult with identified mode and confidence
        """
        # Calculate all candidate modes
        candidates = self.calculate_modes(station, frequency_mhz)
        
        if not candidates:
            return ModeIdentificationResult(
                identified_mode=PropagationMode.UNKNOWN,
                n_hops=0,
                confidence=0.0,
                calculated_delay_ms=0.0,
                measured_delay_ms=measured_delay_ms,
                residual_ms=measured_delay_ms,
                candidates=[],
                low_confidence=True
            )
        
        # Find best-matching candidate
        best_match: Optional[ModeCandidate] = None
        best_residual = float('inf')
        
        for candidate in candidates:
            residual = abs(measured_delay_ms - candidate.total_delay_ms)
            
            # Weight by uncertainty (tighter uncertainty = better match)
            weighted_residual = residual / max(candidate.delay_uncertainty_ms, 0.1)
            
            if weighted_residual < best_residual:
                best_residual = weighted_residual
                best_match = candidate
        
        # Calculate confidence based on residual vs uncertainty
        residual = abs(measured_delay_ms - best_match.total_delay_ms)
        confidence = max(0, 1.0 - (residual / (2 * best_match.delay_uncertainty_ms)))
        
        # Check for ambiguity (multiple modes within uncertainty)
        ambiguous_candidates = [
            c for c in candidates
            if abs(measured_delay_ms - c.total_delay_ms) < c.delay_uncertainty_ms * 2
        ]
        ambiguous = len(ambiguous_candidates) > 1
        
        # Use channel metrics to refine (if available)
        multipath_detected = False
        path_stability = 1.0
        
        if channel_metrics:
            # High delay spread indicates multipath
            delay_spread = channel_metrics.get('delay_spread_ms', 0)
            if delay_spread > 1.0:
                multipath_detected = True
                # Multipath suggests we're seeing the EARLIEST arrival
                # (shortest path, usually lowest hop count)
                confidence *= 0.8  # Reduce confidence
            
            # High Doppler std indicates unstable path
            doppler_std = channel_metrics.get('doppler_std_hz', 0)
            if doppler_std > 1.0:
                path_stability = max(0.3, 1.0 - doppler_std / 5.0)
                confidence *= path_stability
            
            # FSS can help discriminate high vs low hop count
            # High FSS (attenuated highs) = more D-layer transits = more hops
            fss_db = channel_metrics.get('fss_db', 0)
            if ambiguous and fss_db > 5.0:
                # FSS votes for higher hop count
                higher_hop_candidates = [c for c in ambiguous_candidates if c.n_hops > best_match.n_hops]
                if higher_hop_candidates:
                    # Consider upgrading to higher hop
                    logger.debug(f"FSS={fss_db:.1f} dB suggests higher hop count")
        
        return ModeIdentificationResult(
            identified_mode=best_match.mode,
            n_hops=best_match.n_hops,
            confidence=confidence,
            calculated_delay_ms=best_match.total_delay_ms,
            measured_delay_ms=measured_delay_ms,
            residual_ms=residual,
            candidates=candidates,
            multipath_detected=multipath_detected,
            path_stability=path_stability,
            ambiguous=ambiguous,
            low_confidence=confidence < 0.5
        )
    
    def back_calculate_emission_time(
        self,
        station: str,
        arrival_time_utc: float,
        frequency_mhz: float,
        measured_delay_ms: Optional[float] = None,
        channel_metrics: Optional[Dict[str, float]] = None
    ) -> EmissionTimeResult:
        """
        Back-calculate emission time (UTC at transmitter).
        
        This is the "Holy Grail" - determining the precise moment
        the transmitter emitted the signal.
        
        Args:
            station: Station name
            arrival_time_utc: Unix timestamp of arrival (from RTP + time_snap)
            frequency_mhz: Signal frequency
            measured_delay_ms: Optional measured delay (if known from timing analysis)
            channel_metrics: Optional channel quality metrics
            
        Returns:
            EmissionTimeResult with back-calculated emission time
        """
        # Get candidate modes
        candidates = self.calculate_modes(station, frequency_mhz)
        
        if not candidates:
            # No viable modes - return with low confidence
            return EmissionTimeResult(
                emission_time_utc=arrival_time_utc,
                arrival_time_utc=arrival_time_utc,
                propagation_delay_ms=0,
                mode=PropagationMode.UNKNOWN,
                n_hops=0,
                accuracy_ms=100.0,
                confidence=0.0,
                station=station,
                frequency_mhz=frequency_mhz,
                expected_second_offset_ms=0,
                second_aligned=False
            )
        
        # If we have measured delay, use it to identify mode
        if measured_delay_ms is not None:
            mode_result = self.identify_mode(
                station, measured_delay_ms, frequency_mhz, channel_metrics
            )
            propagation_delay_ms = mode_result.calculated_delay_ms
            mode = mode_result.identified_mode
            n_hops = mode_result.n_hops
            confidence = mode_result.confidence
            accuracy_ms = abs(mode_result.residual_ms) + 0.1  # Base accuracy + residual
        else:
            # Use most likely mode (1-hop F2 for typical HF paths)
            # Find the first F2 1-hop mode
            f2_1hop = next(
                (c for c in candidates if c.mode == PropagationMode.F2_LAYER_1HOP),
                candidates[0]
            )
            propagation_delay_ms = f2_1hop.total_delay_ms
            mode = f2_1hop.mode
            n_hops = f2_1hop.n_hops
            confidence = 0.6  # Moderate confidence without measured delay
            accuracy_ms = f2_1hop.delay_uncertainty_ms
        
        # Back-calculate emission time
        emission_time_utc = arrival_time_utc - (propagation_delay_ms / 1000.0)
        
        # Check alignment with second boundary
        # WWV/WWVH ticks occur at exact second boundaries
        fractional_second = emission_time_utc % 1.0
        if fractional_second > 0.5:
            fractional_second -= 1.0
        expected_offset_ms = fractional_second * 1000.0
        
        # Consider aligned if within 2 ms of second boundary
        second_aligned = abs(expected_offset_ms) < 2.0
        
        # Boost confidence if aligned
        if second_aligned:
            confidence = min(1.0, confidence * 1.2)
            accuracy_ms *= 0.8  # Better accuracy when aligned
        
        return EmissionTimeResult(
            emission_time_utc=emission_time_utc,
            arrival_time_utc=arrival_time_utc,
            propagation_delay_ms=propagation_delay_ms,
            mode=mode,
            n_hops=n_hops,
            accuracy_ms=accuracy_ms,
            confidence=confidence,
            station=station,
            frequency_mhz=frequency_mhz,
            expected_second_offset_ms=expected_offset_ms,
            second_aligned=second_aligned
        )
    
    def get_station_distance_km(self, station: str) -> float:
        """Get great-circle distance to station in km"""
        return self._distances.get(station, 0)
    
    def get_expected_delay_range_ms(
        self,
        station: str,
        frequency_mhz: float
    ) -> Tuple[float, float]:
        """
        Get expected delay range (min, max) for a station.
        
        Useful for setting detection windows.
        """
        candidates = self.calculate_modes(station, frequency_mhz)
        if not candidates:
            return (0, 0)
        
        delays = [c.total_delay_ms for c in candidates]
        return (min(delays), max(delays))


@dataclass
class SporadicEEvent:
    """
    Result of Sporadic-E (Es) detection.
    
    Sporadic-E is thin, dense ionization at E-layer heights (~100-120 km) that
    can reflect frequencies normally above the E-layer MUF. Es events are
    characterized by:
    - Sudden SNR increases at higher frequencies (10-15 MHz)
    - Mode change from F-layer to E-layer propagation
    - Shorter propagation delays (E-layer is lower than F-layer)
    
    Physics:
    --------
    Es layers form from wind shear concentrating metallic ions (primarily Fe+, Mg+)
    at ~100-120 km altitude. They can have critical frequencies (foEs) up to 10+ MHz,
    allowing reflection of signals that would normally penetrate the regular E-layer.
    """
    detected: bool
    confidence: float  # 0-1
    
    # Event characteristics
    event_start_time: Optional[float] = None  # Unix timestamp
    event_duration_sec: Optional[float] = None
    
    # Layer parameters
    estimated_foEs_mhz: Optional[float] = None  # Critical frequency
    estimated_height_km: float = 110.0  # Typical Es height
    
    # Detection evidence
    snr_increase_db: Optional[float] = None  # SNR jump at detection
    mode_changed_to_e: bool = False  # True if mode switched to 1E
    highest_freq_reflected_mhz: Optional[float] = None  # Highest freq showing Es
    
    # Quality
    detection_method: str = 'snr_anomaly'  # 'snr_anomaly', 'mode_change', 'combined'


class SporadicEDetector:
    """
    Detect Sporadic-E (Es) events from multi-frequency observations.
    
    Detection Strategy:
    -------------------
    1. Monitor SNR at higher frequencies (10, 15 MHz) for sudden increases
    2. Track propagation mode changes from F-layer to E-layer
    3. Compare arrival times - Es gives shorter delays than F-layer
    4. Correlate across multiple frequencies for confirmation
    
    Usage:
    ------
        detector = SporadicEDetector()
        
        # Add observations over time
        detector.add_observation(timestamp, freq_mhz=10.0, snr_db=25.0, mode='1F')
        detector.add_observation(timestamp, freq_mhz=10.0, snr_db=35.0, mode='1E')  # Es!
        
        # Check for Es event
        event = detector.detect_event()
        if event.detected:
            print(f"Es detected! foEs ≈ {event.estimated_foEs_mhz} MHz")
    """
    
    # Detection thresholds
    SNR_INCREASE_THRESHOLD_DB = 10.0  # Sudden SNR increase indicating Es
    MIN_OBSERVATIONS = 5  # Minimum observations before detection
    ES_FREQUENCIES_MHZ = [10.0, 15.0]  # Frequencies where Es is most visible
    
    def __init__(self, history_minutes: int = 30):
        """
        Initialize Es detector.
        
        Args:
            history_minutes: How long to keep observation history
        """
        self.history_minutes = history_minutes
        self.observations: List[Dict[str, Any]] = []
        self.current_event: Optional[SporadicEEvent] = None
        
        logger.info(f"SporadicEDetector initialized (history={history_minutes} min)")
    
    def add_observation(
        self,
        timestamp: float,
        freq_mhz: float,
        snr_db: float,
        mode: Optional[str] = None,
        delay_ms: Optional[float] = None
    ) -> None:
        """
        Add a new observation to the history.
        
        Args:
            timestamp: Unix timestamp
            freq_mhz: Frequency in MHz
            snr_db: Signal-to-noise ratio in dB
            mode: Propagation mode if known ('1E', '1F', '2F', etc.)
            delay_ms: Propagation delay in ms
        """
        self.observations.append({
            'timestamp': timestamp,
            'freq_mhz': freq_mhz,
            'snr_db': snr_db,
            'mode': mode,
            'delay_ms': delay_ms
        })
        
        # Prune old observations
        cutoff = timestamp - (self.history_minutes * 60)
        self.observations = [o for o in self.observations if o['timestamp'] > cutoff]
    
    def detect_event(self, current_timestamp: Optional[float] = None) -> SporadicEEvent:
        """
        Analyze observations to detect Sporadic-E event.
        
        Returns:
            SporadicEEvent with detection results
        """
        if len(self.observations) < self.MIN_OBSERVATIONS:
            return SporadicEEvent(detected=False, confidence=0.0)
        
        if current_timestamp is None:
            current_timestamp = self.observations[-1]['timestamp']
        
        # Analyze each Es-sensitive frequency
        es_evidence = []
        highest_freq_with_es = 0.0
        
        for freq in self.ES_FREQUENCIES_MHZ:
            freq_obs = [o for o in self.observations if abs(o['freq_mhz'] - freq) < 0.5]
            
            if len(freq_obs) < 3:
                continue
            
            # Check for SNR anomaly (sudden increase)
            snr_anomaly = self._detect_snr_anomaly(freq_obs)
            
            # Check for mode change to E-layer
            mode_change = self._detect_mode_change(freq_obs)
            
            if snr_anomaly['detected'] or mode_change['detected']:
                es_evidence.append({
                    'freq_mhz': freq,
                    'snr_anomaly': snr_anomaly,
                    'mode_change': mode_change
                })
                if freq > highest_freq_with_es:
                    highest_freq_with_es = freq
        
        if not es_evidence:
            return SporadicEEvent(detected=False, confidence=0.0)
        
        # Calculate overall confidence
        confidence = self._calculate_confidence(es_evidence)
        
        # Estimate foEs from highest frequency showing Es
        # foEs ≈ highest_freq / sec(elevation_angle)
        # For typical elevation angles, foEs ≈ highest_freq * 0.8
        estimated_foEs = highest_freq_with_es * 0.8 if highest_freq_with_es > 0 else None
        
        # Get SNR increase from best evidence
        snr_increase = max(
            (e['snr_anomaly'].get('increase_db', 0) for e in es_evidence),
            default=0
        )
        
        # Check if mode changed
        mode_changed = any(e['mode_change']['detected'] for e in es_evidence)
        
        # Determine detection method
        if mode_changed and snr_increase > self.SNR_INCREASE_THRESHOLD_DB:
            detection_method = 'combined'
        elif mode_changed:
            detection_method = 'mode_change'
        else:
            detection_method = 'snr_anomaly'
        
        event = SporadicEEvent(
            detected=True,
            confidence=confidence,
            event_start_time=current_timestamp,
            estimated_foEs_mhz=estimated_foEs,
            snr_increase_db=snr_increase if snr_increase > 0 else None,
            mode_changed_to_e=mode_changed,
            highest_freq_reflected_mhz=highest_freq_with_es if highest_freq_with_es > 0 else None,
            detection_method=detection_method
        )
        
        logger.info(f"Sporadic-E detected: foEs≈{estimated_foEs:.1f} MHz, "
                   f"confidence={confidence:.2f}, method={detection_method}")
        
        return event
    
    def _detect_snr_anomaly(self, observations: List[Dict]) -> Dict[str, Any]:
        """Detect sudden SNR increase indicating Es onset."""
        if len(observations) < 3:
            return {'detected': False}
        
        # Sort by timestamp
        sorted_obs = sorted(observations, key=lambda x: x['timestamp'])
        
        # Calculate baseline SNR (first half of observations)
        mid = len(sorted_obs) // 2
        baseline_snr = np.mean([o['snr_db'] for o in sorted_obs[:mid]])
        
        # Check recent SNR
        recent_snr = np.mean([o['snr_db'] for o in sorted_obs[mid:]])
        
        increase_db = recent_snr - baseline_snr
        
        if increase_db >= self.SNR_INCREASE_THRESHOLD_DB:
            return {
                'detected': True,
                'increase_db': float(increase_db),
                'baseline_snr': float(baseline_snr),
                'recent_snr': float(recent_snr)
            }
        
        return {'detected': False, 'increase_db': float(increase_db)}
    
    def _detect_mode_change(self, observations: List[Dict]) -> Dict[str, Any]:
        """Detect mode change from F-layer to E-layer."""
        # Get observations with mode information
        mode_obs = [o for o in observations if o.get('mode')]
        
        if len(mode_obs) < 2:
            return {'detected': False}
        
        # Sort by timestamp
        sorted_obs = sorted(mode_obs, key=lambda x: x['timestamp'])
        
        # Look for F→E transition
        for i in range(1, len(sorted_obs)):
            prev_mode = sorted_obs[i-1]['mode']
            curr_mode = sorted_obs[i]['mode']
            
            # Check for transition to E-layer mode
            if prev_mode and curr_mode:
                if 'F' in prev_mode.upper() and 'E' in curr_mode.upper():
                    return {
                        'detected': True,
                        'from_mode': prev_mode,
                        'to_mode': curr_mode,
                        'transition_time': sorted_obs[i]['timestamp']
                    }
        
        return {'detected': False}
    
    def _calculate_confidence(self, evidence: List[Dict]) -> float:
        """Calculate overall confidence from multiple evidence sources."""
        if not evidence:
            return 0.0
        
        # Weight factors
        snr_weight = 0.4
        mode_weight = 0.4
        multi_freq_weight = 0.2
        
        # SNR evidence
        snr_scores = []
        for e in evidence:
            if e['snr_anomaly']['detected']:
                increase = e['snr_anomaly'].get('increase_db', 0)
                # Normalize: 10 dB = 0.5, 20 dB = 1.0
                snr_scores.append(min(1.0, increase / 20.0))
        snr_confidence = np.mean(snr_scores) if snr_scores else 0.0
        
        # Mode change evidence
        mode_confidence = 1.0 if any(e['mode_change']['detected'] for e in evidence) else 0.0
        
        # Multi-frequency confirmation
        multi_freq_confidence = min(1.0, len(evidence) / 2.0)
        
        total = (snr_weight * snr_confidence + 
                 mode_weight * mode_confidence + 
                 multi_freq_weight * multi_freq_confidence)
        
        return float(total)


# Convenience function for quick testing
def create_test_solver():
    """Create solver for AC0G station (EM38ww)"""
    return PropagationModeSolver(receiver_grid='EM38ww')
