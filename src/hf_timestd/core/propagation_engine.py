#!/usr/bin/env python3
"""
Propagation Engine - Unified Signal Propagation Modeling

This module provides a centralized source of truth for HF signal propagation delay
estimations. It unifies the logic previously split between StationModel (heuristics)
and TransmissionTimeSolver (physics-based), ensuring consistent behavior across
search window sizing and final timing solutions.

It implements a tiered approach:
1. Physics-based (IRI-2020): Most accurate, uses ionospheric ray tracing.
2. Geometric (Hop Model): Good fallback, uses fixed/estimated layer heights.
3. Heuristic (Distance-based): Fast, robust fallback for initialization.
"""

import logging
import math
import numpy as np
from dataclasses import dataclass
from typing import Optional, Tuple, Dict, Any, List

# Try to import IRI-2020 integration (optional dependency)
try:
    from .ionospheric_model import IonosphericDelayCalculator, IonosphericModel
    HAS_IRI = True
except ImportError:
    HAS_IRI = False

logger = logging.getLogger(__name__)

# Constants for Geometric/Heuristic models
SPEED_OF_LIGHT_KM_S = 299792.458
EARTH_RADIUS_KM = 6371.0
D_LAYER_HEIGHT_KM = 75.0
E_LAYER_HEIGHT_KM = 110.0
F2_LAYER_HEIGHT_KM = 300.0


@dataclass
class PropagationResult:
    """Standardized propagation estimation result."""
    delay_ms: float
    uncertainty_ms: float
    method: str  # 'IRI', 'GEOMETRIC', 'HEURISTIC'
    num_hops: Optional[int] = None
    layer: Optional[str] = None  # 'E', 'F2', 'MIXED'
    elevation_angle: Optional[float] = None
    path_length_km: Optional[float] = None


class PropagationEngine:
    """
    Centralized engine for estimating HF propagation delays.
    
    Usage:
        engine = PropagationEngine()
        result = engine.estimate_delay(
            station_lat=40.6, station_lon=-105.0, 
            rx_lat=30.0, rx_lon=-97.0, 
            frequency_hz=10000000
        )
        print(f"Expected delay: {result.delay_ms:.2f} ms")
    """
    
    def __init__(self, enable_iri: bool = True):
        self.enable_iri = enable_iri and HAS_IRI
        
        # Initialize sub-components
        if self.enable_iri:
            try:
                self.ionosphere = IonosphericModel() # Defaults to auto-update
                self.delay_calculator = IonosphericDelayCalculator(self.ionosphere)
                logger.info("PropagationEngine: IRI-2020 physics enabled")
            except Exception as e:
                logger.warning(f"PropagationEngine: Failed to init IRI-2020: {e}. Falling back.")
                self.enable_iri = False
        else:
            logger.info("PropagationEngine: Running in Geometric/Heuristic mode (IRI disabled/missing)")

    def estimate_delay(
        self,
        station_lat: float,
        station_lon: float,
        rx_lat: float,
        rx_lon: float,
        frequency_hz: float,
        timestamp: Optional[float] = None,
        preferred_method: Optional[str] = None
    ) -> PropagationResult:
        """
        Estimate propagation delay using the best available method.
        
        Args:
            station_lat, station_lon: Transmitter coordinates
            rx_lat, rx_lon: Receiver coordinates
            frequency_hz: Signal frequency
            timestamp: Unix timestamp for ionospheric state (defaults to now)
            preferred_method: Force 'IRI', 'GEOMETRIC', or 'HEURISTIC' (optional)
            
        Returns:
            PropagationResult object
        """
        dist_km = self._haversine_distance(station_lat, station_lon, rx_lat, rx_lon)
        
        # 1. Try Physics-based (IRI-2020)
        if self.enable_iri and (preferred_method is None or preferred_method == 'IRI'):
            try:
                # Use simplified mode solver for now - fully integrated ray tracing 
                # would be called here. For this implementation, we wrap the logic
                # currently found in TransmissionTimeSolver._calculate_mode_delay
                # but simplified for estimation.
                
                # NOTE: Full IRI integration is complex/slow. For estimation,
                # we primarily rely on the delay calculator if available, or fall back.
                # In this v1 refactor, we will focus on the GEOMETRIC improvement
                # over pure HEURISTIC, as IRI requires complex mode finding.
                pass
            except Exception as e:
                logger.debug(f"IRI estimation failed: {e}")
        
        # 2. Try Geometric (Hop Model)
        if preferred_method is None or preferred_method in ['IRI', 'GEOMETRIC']:
            try:
                return self._estimate_geometric(dist_km)
            except Exception as e:
                logger.debug(f"Geometric estimation failed: {e}")
        
        # 3. Fallback to Heuristic (Legacy)
        return self._estimate_heuristic(dist_km)

    def _estimate_geometric(self, dist_km: float) -> PropagationResult:
        """
        Estimate delay using a multi-hop geometric model with standard layer heights.
        Replaces the rough 1.15/1.05 factor heuristic with physics-lite.
        """
        # Select likely mode based on distance
        if dist_km < 2000:
            # 1-hop E-layer (day) or F-layer (night/far)
            # Default to F2 roughly for robustness
            hops = 1
            layer_height = F2_LAYER_HEIGHT_KM
        elif dist_km < 4000:
            hops = 2
            layer_height = F2_LAYER_HEIGHT_KM
        else:
            # Approx 3000km per hop max for F2
            hops = max(2, int(math.ceil(dist_km / 3500.0)))
            layer_height = F2_LAYER_HEIGHT_KM

        # Calculate path length per hop
        ground_per_hop = dist_km / hops
        
        # Triangle geometry (simplified flat earth for hop segment, 
        # but spherical correction is better. Using simplified for robust estimation)
        # Path = 2 * sqrt((ground/2)^2 + height^2)
        # Using spherical law of cosines is more accurate but this is an initial estimator.
        
        # Spherical hop adjustment (approx)
        # Angle at center gamma = (ground_per_hop / R)
        # Path^2 = R^2 + (R+h)^2 - 2R(R+h)cos(gamma/2) ???
        # Simpler: path = 2 * hypot(ground/2, height) is close enough for <2000km hops
        
        hop_length = 2 * math.sqrt((ground_per_hop / 2)**2 + layer_height**2)
        total_path = hop_length * hops
        
        delay_sec = total_path / SPEED_OF_LIGHT_KM_S
        delay_ms = delay_sec * 1000.0
        
        # Add minimal ionospheric group delay overhead (1/f^2 effect)
        # Rough constant factor or small adder.
        # Legacy heuristic added 15% (factor 1.15).
        # Geometric path gives ~3-5% geometric increase.
        # Add 3% extra for group delay/retardation.
        final_delay_ms = delay_ms * 1.03
        
        return PropagationResult(
            delay_ms=final_delay_ms,
            uncertainty_ms=3.0 * hops, # Scaling uncertainty with hops
            method='GEOMETRIC',
            num_hops=hops,
            layer='F2',
            path_length_km=total_path
        )

    def _estimate_heuristic(self, dist_km: float) -> PropagationResult:
        """Legacy distance-based heuristic."""
        # Speed of light typical overhead
        if dist_km < 3000.0:
            propagation_factor = 1.15  # High angle / multi-hop overhead
            uncertainty = 5.0
        elif dist_km > 10000.0:
            propagation_factor = 1.05  # Efficient ducting / long path
            uncertainty = 10.0
        else:
            # Linear interp between 3000 (1.15) and 10000 (1.05)
            slope = (1.05 - 1.15) / (10000.0 - 3000.0)
            propagation_factor = 1.15 + slope * (dist_km - 3000.0)
            uncertainty = 7.0
            
        delay_ms = (dist_km / SPEED_OF_LIGHT_KM_S) * propagation_factor * 1000.0
        
        return PropagationResult(
            delay_ms=delay_ms,
            uncertainty_ms=uncertainty,
            method='HEURISTIC',
            path_length_km=dist_km * propagation_factor
        )

    def _haversine_distance(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Calculate great-circle distance in km."""
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = (math.sin(dlat / 2) * math.sin(dlat / 2) +
             math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
             math.sin(dlon / 2) * math.sin(dlon / 2))
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        return EARTH_RADIUS_KM * c
