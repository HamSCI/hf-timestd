#!/usr/bin/env python3
"""
Propagation Engine - Unified Signal Propagation Modeling

This module provides a centralized source of truth for HF signal propagation delay
estimations. It unifies the logic previously split between StationModel (heuristics)
and TransmissionTimeSolver (physics-based), ensuring consistent behavior across
search window sizing and final timing solutions.

It implements a tiered approach:
1. Geometric (Hop Model): multi-hop spherical-segment geometry with
   standard layer heights — the primary estimator.
2. Heuristic (Distance-based): fast, robust fallback for initialization.

An IRI-2020 ray-tracing tier was previously advertised here but never
implemented — the dead branch and its unused IonosphericModel /
IonosphericDelayCalculator construction have been removed (P-H23).
HFPropagationModel provides the physics-based path; this engine is the
lightweight geometric estimator that StationModel and search-window
sizing rely on.
"""

import logging
import math
from dataclasses import dataclass
from typing import Optional

from hamsci_dsp.geometry import great_circle_km

from .hop_geometry import hop_geometry

logger = logging.getLogger(__name__)

# Constants for Geometric/Heuristic models
SPEED_OF_LIGHT_KM_S = 299792.458
EARTH_RADIUS_KM = 6371.0
D_LAYER_HEIGHT_KM = 75.0
E_LAYER_HEIGHT_KM = 110.0
F2_LAYER_HEIGHT_KM = 300.0

# Ionospheric group-delay excess: Δt(ms) = K · TEC(TECU) / f(MHz)².
# K = 40.3·10⁴ / c, the standard 40.3/f² dispersion constant — see e.g.
# ionospheric_model.IONO_DELAY_CONSTANT_MS. At 10 MHz, 30 TECU → 0.40 ms.
IONO_DELAY_CONSTANT_MS = 40.3 / SPEED_OF_LIGHT_KM_S * 1e16 / 1e12

# Nominal slant TEC per hop for this lightweight estimator. PropagationEngine
# has no ionospheric model (the dead IRI tier was removed — P-H23), so it
# uses one coarse climatological figure: ~30 TECU is a mid-latitude,
# moderate-solar-activity slant value. The estimate carries this term's full
# magnitude as added uncertainty (TEC swings by its own size day-to-night).
NOMINAL_SLANT_TEC_PER_HOP_TECU = 30.0


@dataclass
class PropagationResult:
    """Standardized propagation estimation result."""
    delay_ms: float
    uncertainty_ms: float
    method: str  # 'GEOMETRIC', 'HEURISTIC'
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
            preferred_method: Force 'GEOMETRIC' or 'HEURISTIC' (optional).
                A request for the retired 'IRI' tier falls back to
                'GEOMETRIC' — the best available here (P-H23).

        Returns:
            PropagationResult object
        """
        dist_km = self._haversine_distance(station_lat, station_lon, rx_lat, rx_lon)

        # 1. Geometric (Hop Model) — the primary estimator.  A legacy
        #    'IRI' request is honoured as 'GEOMETRIC' (the retired IRI
        #    tier never had an implementation; see the module docstring).
        if preferred_method is None or preferred_method in ('IRI', 'GEOMETRIC'):
            try:
                return self._estimate_geometric(dist_km, frequency_hz)
            except Exception as e:
                logger.debug(f"Geometric estimation failed: {e}")
        
        # 3. Fallback to Heuristic (Legacy)
        return self._estimate_heuristic(dist_km)

    def _estimate_geometric(
        self, dist_km: float, frequency_hz: float
    ) -> PropagationResult:
        """
        Multi-hop geometric delay estimate with standard layer heights.

        Geometry is the shared spherical law-of-cosines hop model (review
        items S2, P-M19) — the old flat-Earth triangle here understated
        the path on long routes. The ionospheric term is a proper
        frequency-dependent 40.3/f² group delay (P-M19): the previous flat
        ×1.03 ignored ``frequency_hz`` entirely, yet ionospheric delay
        scales as 1/f² — wrong by a factor of ~25 across the 2.5–25 MHz
        broadcast bands.
        """
        # Select likely hop count from distance (coarse mode heuristic).
        if dist_km < 2000:
            hops = 1
        elif dist_km < 4000:
            hops = 2
        else:
            # Roughly 3500 km per hop for F2.
            hops = max(2, int(math.ceil(dist_km / 3500.0)))
        layer_height = F2_LAYER_HEIGHT_KM

        # Spherical-Earth hop geometry — shared module.
        geom = hop_geometry(dist_km, layer_height, hops)
        geometric_delay_ms = geom.path_length_km / SPEED_OF_LIGHT_KM_S * 1000.0

        # Ionospheric group delay: proper 40.3/f² term, climatological TEC.
        f_mhz = frequency_hz / 1e6
        if f_mhz > 0:
            iono_delay_ms = (
                IONO_DELAY_CONSTANT_MS
                * NOMINAL_SLANT_TEC_PER_HOP_TECU * hops
                / (f_mhz ** 2)
            )
        else:
            iono_delay_ms = 0.0

        total_delay_ms = geometric_delay_ms + iono_delay_ms

        # The climatological iono term is itself uncertain to ~its own
        # magnitude (TEC varies by its own size day-to-night), so carry it
        # as uncertainty on top of the per-hop geometric uncertainty.
        uncertainty_ms = 3.0 * hops + iono_delay_ms

        return PropagationResult(
            delay_ms=total_delay_ms,
            uncertainty_ms=uncertainty_ms,
            method='GEOMETRIC',
            num_hops=hops,
            layer='F2',
            elevation_angle=geom.elevation_deg,
            path_length_km=geom.path_length_km,
        )

    def _estimate_heuristic(self, dist_km: float) -> PropagationResult:
        """Legacy distance-based heuristic.

        Returns a delay of ``(dist_km / c) × propagation_factor``, where
        ``propagation_factor`` is the combined geometric-slant + iono
        overhead expressed as a path-length multiplier over the great-
        circle ground distance.

        §4.4 Low: the factors below are bounded **> 1** (the radio
        path is always longer than the great-circle ground distance --
        it goes up to the ionosphere and back), so a "superluminal"
        reading of the 1.05 long-path factor is not actually possible:
        the implied propagation speed is ``c / propagation_factor < c``.
        The values are calibrated from the older single-hop /
        multi-hop empirical fit:

          dist_km   factor   notes
          --------  -------  -------------------------------------------
          < 3000    1.15     1-hop F2 -- steep elevation, large geometric
                             zig-zag overhead, iono delay carries weight
          > 10000   1.05     multi-hop F2 -- shallow elevations, small
                             per-hop overhead, iono averages out across
                             many independent ionospheric columns
          interp    1.15 -> 1.05 linear between

        For real geometry use the `_estimate_geometric` path
        (`hop_geometry` + 40.3/f² iono); this heuristic is a fallback
        when no model is available and no frequency is in scope.
        """
        # Combined geometric + iono path-length multiplier; see docstring.
        if dist_km < 3000.0:
            propagation_factor = 1.15  # High angle / multi-hop overhead
            uncertainty = 5.0
        elif dist_km > 10000.0:
            propagation_factor = 1.05  # Long path -- per-hop overhead small
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
        """Delegates to hamsci_dsp.geometry.great_circle_km (geodesic WGS-84)."""
        return great_circle_km(lat1, lon1, lat2, lon2)
