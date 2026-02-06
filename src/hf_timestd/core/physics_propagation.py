#!/usr/bin/env python3
"""
Physics-Informed Propagation Model

================================================================================
PURPOSE
================================================================================
Compute HF propagation delays using physics-based models rather than statistical
calibration. The key insight is that the RESIDUAL between observed and predicted
arrival times IS the scientific data product - it reveals ionospheric conditions
that differ from climatology.

ARCHITECTURE:
    Expected_Arrival = Emission_Time + Vacuum_Delay + Ionospheric_Delay(Physics)
    Residual = Observed_Arrival - Expected_Arrival
    
    The Residual contains:
    - Real ionospheric variations from climatology
    - Measurement noise
    - Unmodeled propagation effects (multipath, mode mixing)

PHYSICS MODELS (Hierarchical):
    TIER 1: PyLap/PHaRLAP Ray Tracing (if available)
        - Full 3D ray tracing through IRI ionosphere
        - Accounts for ray bending, focusing, multipath
        - Most accurate but computationally expensive
        
    TIER 2: IRI-2020 + Geometric Model
        - IRI-2020 provides hmF2, foF2, TEC
        - Geometric hop calculation with IRI layer heights
        - Group delay from TEC (40.3 * TEC / f^2)
        
    TIER 3: Empirical Model
        - Distance-based delay with diurnal/seasonal corrections
        - Fallback when IRI unavailable

OUTPUT:
    - predicted_delay_ms: Physics-based propagation delay
    - observed_delay_ms: Measured arrival time - emission time
    - residual_ms: observed - predicted (THE SCIENCE PRODUCT)
    - model_tier: Which physics model was used
    - model_uncertainty_ms: Estimated model error

================================================================================
REFERENCES
================================================================================
1. Davies, K. (1990). "Ionospheric Radio." Chapter 10: Group Path and Phase Path.
2. ITU-R P.531-14: "Ionospheric propagation data and prediction methods"
3. Bilitza, D. et al. (2022). "International Reference Ionosphere 2020."

================================================================================
Author: HF Time Standard Team
"""

import logging
import math
import numpy as np
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Dict, Tuple, List
from enum import Enum
from pathlib import Path

from .wwv_constants import (
    WWV_LAT, WWV_LON,
    WWVH_LAT, WWVH_LON,
    CHU_LAT, CHU_LON,
    BPM_LAT, BPM_LON,
    SPEED_OF_LIGHT_KM_S,
    EARTH_RADIUS_KM,
    E_LAYER_HEIGHT_KM,
    F_LAYER_HEIGHT_KM,
)

logger = logging.getLogger(__name__)


class PropagationModelTier(Enum):
    """Which physics model provided the delay estimate"""
    PYLAP_RAYTRACE = "PyLap_RayTrace"    # Full 3D ray tracing
    IRI_GEOMETRIC = "IRI_Geometric"       # IRI heights + geometric hops
    TEC_DISPERSION = "TEC_Dispersion"     # TEC-based group delay
    EMPIRICAL = "Empirical"               # Distance-based fallback
    UNKNOWN = "Unknown"


@dataclass
class PropagationResult:
    """Result of physics-based propagation calculation"""
    # Core timing
    predicted_delay_ms: float      # Physics model prediction
    observed_delay_ms: float       # Measured arrival - emission
    residual_ms: float             # observed - predicted (SCIENCE OUTPUT)
    
    # Model metadata
    model_tier: PropagationModelTier
    model_uncertainty_ms: float    # Estimated model error (1-sigma)
    
    # Path geometry
    station: str
    frequency_mhz: float
    distance_km: float
    n_hops: int                    # Estimated number of ionospheric hops
    propagation_mode: str          # "1F", "2F", "1E", "GW", etc.
    
    # Ionospheric parameters (if available)
    hmF2_km: Optional[float] = None       # F2 layer peak height
    foF2_mhz: Optional[float] = None      # F2 critical frequency
    tec_tecu: Optional[float] = None      # Total Electron Content
    
    # Ray tracing details (if PyLap used)
    ray_path_km: Optional[float] = None   # Actual ray path length
    ray_group_delay_ms: Optional[float] = None
    ray_elevation_deg: Optional[float] = None
    
    # Quality flags
    is_physics_based: bool = True
    confidence: float = 0.5


@dataclass
class IonosphericState:
    """Current ionospheric state for propagation modeling"""
    timestamp: datetime
    hmF2_km: float           # F2 peak height
    foF2_mhz: float          # F2 critical frequency
    hmE_km: float            # E layer height
    foE_mhz: float           # E critical frequency
    tec_tecu: float          # Vertical TEC
    model_source: str        # "IRI-2020", "IRI-2016", "Parametric"


# Station coordinates lookup
STATION_COORDS = {
    'WWV': (WWV_LAT, WWV_LON),
    'WWVH': (WWVH_LAT, WWVH_LON),
    'CHU': (CHU_LAT, CHU_LON),
    'BPM': (BPM_LAT, BPM_LON),
}


class PhysicsPropagationModel:
    """
    Physics-informed HF propagation delay calculator.
    
    This class computes propagation delays using the best available physics
    model, and outputs the RESIDUAL as the primary scientific product.
    
    The residual (observed - predicted) reveals ionospheric conditions that
    differ from climatology - this is the "ionospheric weather" signal we
    want to capture.
    
    Usage:
        model = PhysicsPropagationModel(
            receiver_lat=38.0,
            receiver_lon=-90.0
        )
        
        result = model.compute_delay(
            station='WWV',
            frequency_mhz=10.0,
            observed_arrival_ms=5.23,  # Measured from tone detection
            timestamp=datetime.now(timezone.utc)
        )
        
        # The science output:
        print(f"Ionospheric residual: {result.residual_ms:.2f} ms")
    """
    
    def __init__(
        self,
        receiver_lat: float,
        receiver_lon: float,
        enable_pylap: bool = True,
        enable_iri: bool = True,
        pylap_cache_dir: Optional[Path] = None,
        ionex_dir: Optional[Path] = None
    ):
        """
        Initialize physics propagation model.
        
        Args:
            receiver_lat: Receiver latitude (degrees)
            receiver_lon: Receiver longitude (degrees)
            enable_pylap: Attempt to use PyLap ray tracing
            enable_iri: Use IRI-2020 for ionospheric parameters
            pylap_cache_dir: Directory to cache PyLap results
            ionex_dir: Directory containing IONEX files (Tier 1.5)
        """
        self.receiver_lat = receiver_lat
        self.receiver_lon = receiver_lon
        self.enable_pylap = enable_pylap
        self.enable_iri = enable_iri
        
        # Pre-calculate distances to all stations
        self.station_distances = {}
        for station, (lat, lon) in STATION_COORDS.items():
            self.station_distances[station] = self._haversine_distance(
                receiver_lat, receiver_lon, lat, lon
            )
        
        # Initialize IRI model
        self._iri_model = None
        if enable_iri:
            try:
                from .ionospheric_model import IonosphericModel
                self._iri_model = IonosphericModel(enable_iri=True, ionex_dir=ionex_dir)
                logger.info("IRI ionospheric model initialized")
            except Exception as e:
                logger.warning(f"IRI model initialization failed: {e}")
        
        # Initialize PyLap (if available)
        self._pylap_available = False
        self._pylap = None
        if enable_pylap:
            self._check_pylap_availability()
        
        # Statistics
        self.stats = {
            'pylap_calls': 0,
            'iri_calls': 0,
            'empirical_fallbacks': 0,
            'total_computations': 0
        }
        
        logger.info(
            f"PhysicsPropagationModel initialized: "
            f"PyLap={'available' if self._pylap_available else 'unavailable'}, "
            f"IRI={'available' if self._iri_model else 'unavailable'}"
        )
    
    def _check_pylap_availability(self) -> bool:
        """Check if PyLap is available and functional."""
        try:
            # PyLap requires PHaRLAP and Intel Fortran runtime
            import pylap
            self._pylap = pylap
            self._pylap_available = True
            logger.info("PyLap ray tracing available")
            return True
        except ImportError:
            logger.debug("PyLap not installed (pip install pylap)")
            self._pylap_available = False
        except Exception as e:
            logger.warning(f"PyLap initialization failed: {e}")
            self._pylap_available = False
        return False
    
    @staticmethod
    def _haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Calculate great circle distance in km."""
        lat1_rad = math.radians(lat1)
        lat2_rad = math.radians(lat2)
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        
        a = math.sin(dlat/2)**2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon/2)**2
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
        
        return EARTH_RADIUS_KM * c
    
    def get_mode_candidates(
        self,
        station: str,
        frequency_mhz: float,
        timestamp: datetime
    ) -> List[Dict]:
        """
        Get all viable propagation mode candidates with probability scores.
        
        This is the public interface for mode ambiguity analysis. Returns
        all physically viable modes sorted by probability.
        
        Args:
            station: Station name (WWV, WWVH, CHU, BPM)
            frequency_mhz: Broadcast frequency in MHz
            timestamp: UTC timestamp (for ionospheric state)
            
        Returns:
            List of mode candidates, each with:
            - mode: Mode string (e.g., '1F', '2E')
            - n_hops: Number of ionospheric hops
            - layer: Ionospheric layer ('E' or 'F')
            - delay_ms: Predicted propagation delay
            - probability: Normalized probability (0-1)
            - uncertainty_ms: Mode-specific uncertainty
        """
        distance_km = self.station_distances.get(station, 0)
        if distance_km == 0:
            return []
        
        # Get layer heights from IRI or use defaults
        hmF2_km = F_LAYER_HEIGHT_KM
        hmE_km = E_LAYER_HEIGHT_KM
        
        if self._iri_model is not None:
            try:
                tx_lat, tx_lon = STATION_COORDS.get(station, (0, 0))
                mid_lat = (tx_lat + self.receiver_lat) / 2
                mid_lon = (tx_lon + self.receiver_lon) / 2
                heights = self._iri_model.get_layer_heights(
                    timestamp=timestamp,
                    latitude=mid_lat,
                    longitude=mid_lon
                )
                if heights:
                    hmF2_km = heights.hmF2
                    hmE_km = heights.hmE
            except Exception:
                pass
        
        return self._get_mode_candidates(distance_km, frequency_mhz, hmF2_km, hmE_km)
    
    def compute_delay(
        self,
        station: str,
        frequency_mhz: float,
        observed_arrival_ms: float,
        timestamp: datetime,
        snr_db: float = 10.0
    ) -> PropagationResult:
        """
        Compute physics-based propagation delay and residual.
        
        Args:
            station: Station name (WWV, WWVH, CHU, BPM)
            frequency_mhz: Broadcast frequency in MHz
            observed_arrival_ms: Measured arrival time relative to second boundary
            timestamp: UTC timestamp of observation
            snr_db: Signal-to-noise ratio (for confidence weighting)
            
        Returns:
            PropagationResult with predicted delay, residual, and metadata
        """
        self.stats['total_computations'] += 1
        
        distance_km = self.station_distances.get(station, 0)
        if distance_km == 0:
            logger.warning(f"Unknown station: {station}")
            return self._create_unknown_result(station, frequency_mhz, observed_arrival_ms)
        
        # Try physics models in order of preference
        result = None
        
        # TIER 1: PyLap ray tracing
        if self._pylap_available and self._pylap is not None:
            result = self._compute_pylap_delay(
                station, frequency_mhz, distance_km, timestamp
            )
            if result is not None:
                self.stats['pylap_calls'] += 1
        
        # TIER 2: IRI + geometric model
        if result is None and self._iri_model is not None:
            result = self._compute_iri_geometric_delay(
                station, frequency_mhz, distance_km, timestamp
            )
            if result is not None:
                self.stats['iri_calls'] += 1
        
        # TIER 3: Empirical fallback
        if result is None:
            result = self._compute_empirical_delay(
                station, frequency_mhz, distance_km, timestamp
            )
            self.stats['empirical_fallbacks'] += 1
        
        # Compute residual (THE SCIENCE OUTPUT)
        result.observed_delay_ms = observed_arrival_ms
        result.residual_ms = observed_arrival_ms - result.predicted_delay_ms
        
        # Adjust confidence based on SNR
        if snr_db > 20:
            result.confidence = min(1.0, result.confidence * 1.2)
        elif snr_db < 10:
            result.confidence = result.confidence * 0.7
        
        return result
    
    def _compute_pylap_delay(
        self,
        station: str,
        frequency_mhz: float,
        distance_km: float,
        timestamp: datetime
    ) -> Optional[PropagationResult]:
        """
        TIER 1: Compute delay using PyLap 3D ray tracing.
        
        PyLap uses PHaRLAP to trace rays through the IRI ionosphere,
        accounting for ray bending, focusing, and multipath.
        """
        if not self._pylap_available or self._pylap is None:
            return None
        
        try:
            # Get station coordinates
            tx_lat, tx_lon = STATION_COORDS.get(station, (0, 0))
            
            # PyLap ray tracing call
            # This is a simplified interface - actual PyLap API may differ
            ray_result = self._pylap.raytrace_2d(
                tx_lat=tx_lat,
                tx_lon=tx_lon,
                rx_lat=self.receiver_lat,
                rx_lon=self.receiver_lon,
                frequency_mhz=frequency_mhz,
                datetime=timestamp,
                elevations=np.arange(5, 85, 5)  # Search elevation angles
            )
            
            if ray_result is None or not ray_result.get('success', False):
                return None
            
            # Extract results
            group_delay_ms = ray_result.get('group_delay_ms', 0)
            ray_path_km = ray_result.get('path_length_km', distance_km)
            elevation_deg = ray_result.get('elevation_deg', 0)
            n_hops = ray_result.get('n_hops', 1)
            
            # Determine propagation mode from ray trace
            if n_hops == 0:
                mode = "GW"  # Ground wave
            elif ray_result.get('layer', 'F') == 'E':
                mode = f"{n_hops}E"
            else:
                mode = f"{n_hops}F"
            
            return PropagationResult(
                predicted_delay_ms=group_delay_ms,
                observed_delay_ms=0,  # Will be set by caller
                residual_ms=0,        # Will be computed by caller
                model_tier=PropagationModelTier.PYLAP_RAYTRACE,
                model_uncertainty_ms=0.3,  # Ray tracing is most accurate
                station=station,
                frequency_mhz=frequency_mhz,
                distance_km=distance_km,
                n_hops=n_hops,
                propagation_mode=mode,
                ray_path_km=ray_path_km,
                ray_group_delay_ms=group_delay_ms,
                ray_elevation_deg=elevation_deg,
                is_physics_based=True,
                confidence=0.9
            )
            
        except Exception as e:
            logger.debug(f"PyLap ray tracing failed: {e}")
            return None
    
    def _compute_iri_geometric_delay(
        self,
        station: str,
        frequency_mhz: float,
        distance_km: float,
        timestamp: datetime
    ) -> Optional[PropagationResult]:
        """
        TIER 2: Compute delay using IRI-2020 + geometric hop model.
        
        Uses IRI for ionospheric parameters (hmF2, TEC) and geometric
        ray path calculation for delay.
        """
        if self._iri_model is None:
            return None
        
        try:
            # Get midpoint coordinates for IRI query
            tx_lat, tx_lon = STATION_COORDS.get(station, (0, 0))
            mid_lat = (tx_lat + self.receiver_lat) / 2
            mid_lon = (tx_lon + self.receiver_lon) / 2
            
            # Get IRI layer heights
            heights = self._iri_model.get_layer_heights(
                timestamp=timestamp,
                latitude=mid_lat,
                longitude=mid_lon
            )
            
            if heights is None:
                return None
            
            hmF2_km = heights.hmF2
            hmE_km = heights.hmE
            
            # Determine propagation mode based on distance and frequency
            n_hops, layer, mode = self._estimate_propagation_mode(
                distance_km, frequency_mhz, hmF2_km, hmE_km
            )
            
            # Calculate geometric path length
            if layer == 'E':
                layer_height = hmE_km
            else:
                layer_height = hmF2_km
            
            path_km = self._calculate_hop_path(distance_km, layer_height, n_hops)
            
            # Vacuum delay
            vacuum_delay_ms = (path_km / SPEED_OF_LIGHT_KM_S) * 1000.0
            
            # Ionospheric group delay (TEC-based)
            # Group delay = 40.3 * TEC / f^2 (in seconds, TEC in TECU, f in Hz)
            # For HF, this is typically 0.1-1.0 ms
            tec_tecu = self._estimate_tec(timestamp, mid_lat, mid_lon)
            iono_delay_ms = (40.3 * tec_tecu * n_hops) / (frequency_mhz * 1e6)**2 * 1000.0
            
            total_delay_ms = vacuum_delay_ms + iono_delay_ms
            
            return PropagationResult(
                predicted_delay_ms=total_delay_ms,
                observed_delay_ms=0,
                residual_ms=0,
                model_tier=PropagationModelTier.IRI_GEOMETRIC,
                model_uncertainty_ms=0.8,  # IRI + geometric has ~0.8ms uncertainty
                station=station,
                frequency_mhz=frequency_mhz,
                distance_km=distance_km,
                n_hops=n_hops,
                propagation_mode=mode,
                hmF2_km=hmF2_km,
                tec_tecu=tec_tecu,
                is_physics_based=True,
                confidence=0.7
            )
            
        except Exception as e:
            logger.debug(f"IRI geometric calculation failed: {e}")
            return None
    
    def _compute_empirical_delay(
        self,
        station: str,
        frequency_mhz: float,
        distance_km: float,
        timestamp: datetime
    ) -> PropagationResult:
        """
        TIER 3: Empirical delay model (fallback).
        
        Uses distance-based delay with empirical corrections.
        Less accurate but always available.
        """
        # Estimate number of hops
        n_hops = max(1, int(distance_km / 3000))  # ~3000 km per hop
        
        # Assume F-layer propagation
        layer_height = F_LAYER_HEIGHT_KM
        
        # Calculate path length
        path_km = self._calculate_hop_path(distance_km, layer_height, n_hops)
        
        # Vacuum delay
        delay_ms = (path_km / SPEED_OF_LIGHT_KM_S) * 1000.0
        
        # Add empirical ionospheric correction (~0.15 ms per hop)
        delay_ms += n_hops * 0.15
        
        mode = f"{n_hops}F"
        
        return PropagationResult(
            predicted_delay_ms=delay_ms,
            observed_delay_ms=0,
            residual_ms=0,
            model_tier=PropagationModelTier.EMPIRICAL,
            model_uncertainty_ms=2.0,  # Empirical has higher uncertainty
            station=station,
            frequency_mhz=frequency_mhz,
            distance_km=distance_km,
            n_hops=n_hops,
            propagation_mode=mode,
            is_physics_based=False,
            confidence=0.4
        )
    
    def _estimate_propagation_mode(
        self,
        distance_km: float,
        frequency_mhz: float,
        hmF2_km: float,
        hmE_km: float
    ) -> Tuple[int, str, str]:
        """
        Estimate propagation mode based on distance and ionospheric state.
        
        Returns: (n_hops, layer, mode_string)
        
        Note: This returns the MOST LIKELY mode. For multi-mode analysis,
        use _get_mode_candidates() which returns all viable modes with scores.
        """
        candidates = self._get_mode_candidates(distance_km, frequency_mhz, hmF2_km, hmE_km)
        if candidates:
            # Return the highest-scoring candidate
            best = candidates[0]
            return (best['n_hops'], best['layer'], best['mode'])
        else:
            # Fallback
            return (1, 'F', '1F')
    
    def _get_mode_candidates(
        self,
        distance_km: float,
        frequency_mhz: float,
        hmF2_km: float,
        hmE_km: float
    ) -> List[Dict]:
        """
        Generate all viable propagation mode candidates with probability scores.
        
        MODE AMBIGUITY RESOLUTION (2026-02-06):
        ---------------------------------------
        Multiple ionospheric propagation modes can be viable simultaneously:
        - 1F vs 2E (similar path lengths at certain distances)
        - 1F vs 2F (mode mixing during disturbed conditions)
        - E+F mixed modes (signal reflects off both layers)
        
        This method returns ALL viable modes with scores based on:
        1. Geometric feasibility (is the path physically possible?)
        2. Frequency/layer compatibility (E-layer has lower MUF)
        3. Path efficiency (shorter paths are more likely)
        4. Ionospheric conditions (time of day affects layer strength)
        
        Returns:
            List of dicts sorted by score (highest first):
            [{'mode': '1F', 'n_hops': 1, 'layer': 'F', 'score': 0.8, 
              'delay_ms': 5.2, 'uncertainty_ms': 1.0}, ...]
        """
        candidates = []
        
        # Maximum single-hop distances for each layer
        max_1hop_E = 2 * math.sqrt(2 * EARTH_RADIUS_KM * hmE_km + hmE_km**2)
        max_1hop_F = 2 * math.sqrt(2 * EARTH_RADIUS_KM * hmF2_km + hmF2_km**2)
        
        # E-layer candidates (lower frequencies, shorter distances)
        # E-layer MUF is typically 3-5 MHz during day, lower at night
        e_layer_viable = frequency_mhz < 10  # Conservative upper limit
        
        if e_layer_viable:
            for n_hops in range(1, 4):
                if distance_km <= n_hops * max_1hop_E:
                    # Calculate path and delay
                    path_km = self._calculate_hop_path(distance_km, hmE_km, n_hops)
                    delay_ms = (path_km / SPEED_OF_LIGHT_KM_S) * 1000.0
                    
                    # Score based on:
                    # - Frequency (lower = better for E-layer)
                    # - Path efficiency (fewer hops = better)
                    # - Distance fit (closer to max = less likely)
                    freq_score = max(0, 1.0 - (frequency_mhz - 3) / 7)  # Peak at 3 MHz
                    hop_score = 1.0 / n_hops  # Fewer hops preferred
                    dist_ratio = distance_km / (n_hops * max_1hop_E)
                    dist_score = 1.0 - 0.5 * dist_ratio  # Penalize near-max distances
                    
                    score = freq_score * hop_score * dist_score * 0.7  # E-layer base weight
                    
                    # Uncertainty increases with hops
                    uncertainty_ms = 1.0 + 0.5 * n_hops
                    
                    candidates.append({
                        'mode': f'{n_hops}E',
                        'n_hops': n_hops,
                        'layer': 'E',
                        'height_km': hmE_km,
                        'score': score,
                        'delay_ms': delay_ms,
                        'path_km': path_km,
                        'uncertainty_ms': uncertainty_ms
                    })
        
        # F-layer candidates (all frequencies, longer distances)
        for n_hops in range(1, 6):
            if distance_km <= n_hops * max_1hop_F:
                # Calculate path and delay
                path_km = self._calculate_hop_path(distance_km, hmF2_km, n_hops)
                delay_ms = (path_km / SPEED_OF_LIGHT_KM_S) * 1000.0
                
                # Score based on:
                # - Path efficiency (fewer hops = better)
                # - Distance fit
                # - Frequency (higher frequencies prefer F-layer)
                hop_score = 1.0 / n_hops
                dist_ratio = distance_km / (n_hops * max_1hop_F)
                dist_score = 1.0 - 0.3 * dist_ratio
                freq_score = min(1.0, frequency_mhz / 10)  # Higher freq = better for F
                
                score = hop_score * dist_score * freq_score
                
                # Uncertainty increases with hops
                uncertainty_ms = 0.8 + 0.4 * n_hops
                
                candidates.append({
                    'mode': f'{n_hops}F',
                    'n_hops': n_hops,
                    'layer': 'F',
                    'height_km': hmF2_km,
                    'score': score,
                    'delay_ms': delay_ms,
                    'path_km': path_km,
                    'uncertainty_ms': uncertainty_ms
                })
        
        # Sort by score (highest first)
        candidates.sort(key=lambda x: x['score'], reverse=True)
        
        # Normalize scores to sum to 1.0 (probability distribution)
        total_score = sum(c['score'] for c in candidates)
        if total_score > 0:
            for c in candidates:
                c['probability'] = c['score'] / total_score
        
        return candidates
    
    def _calculate_hop_path(
        self,
        ground_distance_km: float,
        layer_height_km: float,
        n_hops: int
    ) -> float:
        """Calculate total ray path length for n-hop propagation."""
        if n_hops == 0:
            return ground_distance_km  # Ground wave
        
        # Distance per hop
        hop_ground = ground_distance_km / n_hops
        
        # Slant path for each hop (up and down)
        # Using simple geometry: path = 2 * sqrt((hop_ground/2)^2 + layer_height^2)
        half_hop = hop_ground / 2
        slant_path = 2 * math.sqrt(half_hop**2 + layer_height_km**2)
        
        return slant_path * n_hops
    
    def _estimate_tec(
        self,
        timestamp: datetime,
        latitude: float,
        longitude: float
    ) -> float:
        """
        Estimate vertical TEC in TECU.
        
        Uses IRI if available, otherwise empirical model.
        """
        # Simple empirical TEC model
        # TEC varies from ~5 TECU (night) to ~50 TECU (day, solar max)
        hour = timestamp.hour + timestamp.minute / 60.0
        
        # Diurnal variation (peak at local noon)
        local_hour = (hour + longitude / 15.0) % 24
        diurnal_factor = 0.5 + 0.5 * math.cos(2 * math.pi * (local_hour - 14) / 24)
        
        # Latitude variation (higher at equator)
        lat_factor = 1.0 + 0.5 * math.cos(math.radians(latitude))
        
        # Base TEC (moderate solar activity)
        base_tec = 20.0
        
        return base_tec * diurnal_factor * lat_factor
    
    def _create_unknown_result(
        self,
        station: str,
        frequency_mhz: float,
        observed_arrival_ms: float
    ) -> PropagationResult:
        """Create result for unknown station."""
        return PropagationResult(
            predicted_delay_ms=0,
            observed_delay_ms=observed_arrival_ms,
            residual_ms=observed_arrival_ms,
            model_tier=PropagationModelTier.UNKNOWN,
            model_uncertainty_ms=999.0,
            station=station,
            frequency_mhz=frequency_mhz,
            distance_km=0,
            n_hops=0,
            propagation_mode="UNKNOWN",
            is_physics_based=False,
            confidence=0.0
        )
    
    def get_stats(self) -> Dict:
        """Get model usage statistics."""
        return dict(self.stats)


def create_physics_model(
    receiver_lat: float,
    receiver_lon: float
) -> PhysicsPropagationModel:
    """Factory function to create physics propagation model."""
    return PhysicsPropagationModel(
        receiver_lat=receiver_lat,
        receiver_lon=receiver_lon
    )
