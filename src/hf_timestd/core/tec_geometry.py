#!/usr/bin/env python3
"""
TEC Geometric Corrections - Obliquity Factor and Midpoint Calculation

This module provides geometric corrections for converting slant TEC to vertical TEC
and calculating ionospheric pierce points (midpoints).
"""

import math
from typing import Tuple, Dict

from hamsci_dsp.geometry import (
    great_circle_km as _great_circle_km,
    midpoint as _midpoint,
    elevation_angle_deg as _elevation_angle_deg,
)
from hamsci_dsp.propagation.oblique import slant_to_vertical_tec as _slant_to_vertical_tec

from hf_timestd.core.wwv_constants import STATION_LOCATIONS as STATIONS

# Physical constants
# NOTE: the great-circle/midpoint/elevation helpers below now delegate to
# hamsci_dsp.geometry, which uses geodesic (WGS-84 ellipsoid) distance — more
# accurate than the previous spherical haversine. EARTH_RADIUS_KM is retained
# for the spherical thin-shell obliquity model in convert_slant_to_vertical.
EARTH_RADIUS_KM = 6371.0
DEFAULT_IONO_HEIGHT_KM = 350.0

# Station locations: the single source of truth is wwv_constants.STATION_LOCATIONS
# (P-M6). A local copy here had drifted — BPM sat at 34.457°N, ~55 km from the
# real Pucheng transmitter — so it is now imported, not duplicated.


def _validate_latlon(lat: float, lon: float, label: str) -> None:
    """§4.4 Low: validate lat/lon are finite and within physical range.

    Raises ``ValueError`` on invalid input -- catching swapped lat/lon
    or unit-confusion bugs at the boundary instead of silently
    propagating nonsense through the great-circle / obliquity math.
    """
    if not (math.isfinite(lat) and math.isfinite(lon)):
        raise ValueError(f"{label}: non-finite coordinate (lat={lat!r}, lon={lon!r})")
    if not (-90.0 <= lat <= 90.0):
        raise ValueError(f"{label}: lat {lat} out of [-90, 90] (deg)")
    if not (-180.0 <= lon <= 180.0):
        raise ValueError(f"{label}: lon {lon} out of [-180, 180] (deg)")


def calculate_midpoint(lat1: float, lon1: float, lat2: float, lon2: float) -> Tuple[float, float]:
    """
    Calculate great circle midpoint between two points.

    Args:
        lat1, lon1: First point (degrees)
        lat2, lon2: Second point (degrees)

    Returns:
        (midpoint_lat, midpoint_lon) in degrees

    Raises:
        ValueError: if either endpoint is out of range or non-finite.
    """
    _validate_latlon(lat1, lon1, "calculate_midpoint p1")
    _validate_latlon(lat2, lon2, "calculate_midpoint p2")

    # Geodesic midpoint (WGS-84) — the distance-halfway sub-point of the path.
    return _midpoint(lat1, lon1, lat2, lon2)


def calculate_elevation_angle(
    rx_lat: float, rx_lon: float,
    tx_lat: float, tx_lon: float,
    h_iono: float = DEFAULT_IONO_HEIGHT_KM
) -> float:
    """
    Elevation angle from the receiver to the single-hop ionospheric
    reflection point, on a spherical Earth (P-H8).

    The reflection point sits at height ``h_iono`` above the ground midpoint.
    A flat-Earth triangle, ``atan2(h_iono, d/2)``, ignores curvature: over
    1000-3000 km paths the ground curves away from the receiver's local
    horizontal, so the flat triangle overestimates the elevation (≈2x at
    2000 km), underestimates the obliquity factor, and biases VTEC high.

    Spherical geometry — with central angle ``gamma = (d/2)/R_E`` between the
    receiver and the sub-reflection point, and reflection-point geocentric
    radius ``r_p = R_E + h_iono``:

        tan(elevation) = (r_p*cos(gamma) - R_E) / (r_p*sin(gamma))

    This reduces to the flat-Earth triangle as ``gamma -> 0``. A negative
    result means ``h_iono`` is too low to support a single hop at this range
    (reflection point below the local horizon); it is returned as-is so the
    caller can gate on it.

    Args:
        rx_lat, rx_lon: Receiver location (degrees)
        tx_lat, tx_lon: Transmitter location (degrees)
        h_iono: Ionospheric height (km)

    Returns:
        Elevation angle (degrees)
    """
    # Great circle distance; a single hop reflects above the ground midpoint.
    # Delegates the spherical-Earth elevation formula to hamsci_dsp.geometry
    # (identical math: atan2(cos(gamma) - Re/(Re+h), sin(gamma))).
    distance_km = great_circle_distance(rx_lat, rx_lon, tx_lat, tx_lon)
    return _elevation_angle_deg(distance_km, h_iono)


def great_circle_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Geodesic (WGS-84 ellipsoidal) distance between two points, in kilometers.

    Delegates to :func:`hamsci_dsp.geometry.great_circle_km` (geographiclib,
    Karney geodesics). This replaces the former spherical haversine
    (``R·c`` with ``R=6371.0``); results differ by up to ~0.5% on long HF
    paths, which is the more accurate value.

    Args:
        lat1, lon1: First point (degrees)
        lat2, lon2: Second point (degrees)

    Returns:
        Distance in kilometers
    """
    return _great_circle_km(lat1, lon1, lat2, lon2)


#: Maximum obliquity factor (§4.4 Low).  Matches the sibling cap in
#: ``propagation_model._integrate_layer_delay`` (``max(0.01, sin_sq)``
#: at the denominator → ``M ≤ 10``).  Without this cap, near-horizon
#: elevations push ``M`` past 30, well past the regime where the
#: thin-shell approximation is meaningful; the cap keeps the
#: slant-to-vertical mapping inside the model's validity envelope.
MAX_OBLIQUITY_FACTOR = 10.0


def convert_slant_to_vertical(
    tec_slant: float,
    elevation_angle_deg: float,
    h_iono: float = DEFAULT_IONO_HEIGHT_KM
) -> Tuple[float, float]:
    """
    Convert slant TEC to vertical TEC using obliquity factor.

    The obliquity factor M accounts for the longer path length through
    the ionosphere at oblique angles.

    Formula:
        M = 1 / cos(arcsin((R_E * cos(θ)) / (R_E + h_m)))
        VTEC = TEC_slant / M

    The returned ``M`` is capped at :data:`MAX_OBLIQUITY_FACTOR`
    (=10) to match the sibling cap in ``propagation_model`` -- without
    it, sub-5° elevations push the obliquity past the regime where
    the thin-shell approximation is meaningful.

    Args:
        tec_slant: Measured slant TEC (TECU)
        elevation_angle_deg: Elevation angle at receiver (degrees)
        h_iono: Ionospheric height (km, default 350)

    Returns:
        (vtec, obliquity_factor) tuple
    """
    # Delegates the thin-shell obliquity model and the M≤10 cap to
    # hamsci_dsp.propagation.oblique (math-identical; it uses the WGS-84 mean
    # radius R_EARTH_KM=6371.0088 rather than the local 6371.0, a ~1e-6 shift).
    return _slant_to_vertical_tec(tec_slant, elevation_angle_deg, h_iono_km=h_iono)


def calculate_geometry_for_station(
    station: str,
    rx_lat: float,
    rx_lon: float,
    h_iono: float = DEFAULT_IONO_HEIGHT_KM
) -> Dict:
    """
    Calculate all geometric parameters for a given station.
    
    Args:
        station: Station code ('WWV', 'WWVH', 'CHU', 'BPM')
        rx_lat, rx_lon: Receiver location (degrees)
        h_iono: Ionospheric height (km)
    
    Returns:
        Dict with midpoint_lat, midpoint_lon, elevation_deg, distance_km
    """
    if station not in STATIONS:
        raise ValueError(f"Unknown station: {station}")
    
    tx_lat = STATIONS[station]['lat']
    tx_lon = STATIONS[station]['lon']
    
    # Calculate midpoint
    mid_lat, mid_lon = calculate_midpoint(rx_lat, rx_lon, tx_lat, tx_lon)
    
    # Calculate elevation angle
    elevation_deg = calculate_elevation_angle(rx_lat, rx_lon, tx_lat, tx_lon, h_iono)
    
    # Calculate distance
    distance_km = great_circle_distance(rx_lat, rx_lon, tx_lat, tx_lon)
    
    return {
        'midpoint_lat': mid_lat,
        'midpoint_lon': mid_lon,
        'elevation_deg': elevation_deg,
        'distance_km': distance_km,
        'tx_lat': tx_lat,
        'tx_lon': tx_lon
    }


if __name__ == '__main__':
    # Example usage
    rx_lat, rx_lon = 38.918461, -92.127974  # AC0G location
    
    print("TEC Geometric Corrections Example")
    print("=" * 50)
    print(f"Receiver: {rx_lat:.6f}°N, {rx_lon:.6f}°W\n")
    
    for station in ['WWV', 'WWVH', 'CHU']:
        print(f"\n{station} ({STATIONS[station]['name']}):")
        geom = calculate_geometry_for_station(station, rx_lat, rx_lon)
        
        print(f"  Distance: {geom['distance_km']:.1f} km")
        print(f"  Midpoint: {geom['midpoint_lat']:.4f}°N, {geom['midpoint_lon']:.4f}°W")
        print(f"  Elevation: {geom['elevation_deg']:.2f}°")
        
        # Example TEC conversion
        tec_slant = 30.0  # TECU
        vtec, M = convert_slant_to_vertical(tec_slant, geom['elevation_deg'])
        print(f"  Obliquity factor: {M:.3f}")
        print(f"  TEC conversion: {tec_slant:.1f} TECU (slant) → {vtec:.1f} TECU (vertical)")
