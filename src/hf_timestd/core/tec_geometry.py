#!/usr/bin/env python3
"""
TEC Geometric Corrections - Obliquity Factor and Midpoint Calculation

This module provides geometric corrections for converting slant TEC to vertical TEC
and calculating ionospheric pierce points (midpoints).
"""

import math
from typing import Tuple, Dict

# Physical constants
EARTH_RADIUS_KM = 6371.0
DEFAULT_IONO_HEIGHT_KM = 350.0

# Station locations (from wwv_constants.py)
STATIONS = {
    'WWV': {'lat': 40.678, 'lon': -105.039, 'name': 'WWV Fort Collins, CO'},
    'WWVH': {'lat': 21.987, 'lon': -159.763, 'name': 'WWVH Kauai, HI'},
    'CHU': {'lat': 45.295, 'lon': -75.753, 'name': 'CHU Ottawa, ON'},
    'BPM': {'lat': 34.457, 'lon': 109.550, 'name': 'BPM Pucheng, China'}
}


def calculate_midpoint(lat1: float, lon1: float, lat2: float, lon2: float) -> Tuple[float, float]:
    """
    Calculate great circle midpoint between two points.
    
    Args:
        lat1, lon1: First point (degrees)
        lat2, lon2: Second point (degrees)
    
    Returns:
        (midpoint_lat, midpoint_lon) in degrees
    """
    # Convert to radians
    lat1_rad = math.radians(lat1)
    lon1_rad = math.radians(lon1)
    lat2_rad = math.radians(lat2)
    lon2_rad = math.radians(lon2)
    
    # Midpoint calculation
    Bx = math.cos(lat2_rad) * math.cos(lon2_rad - lon1_rad)
    By = math.cos(lat2_rad) * math.sin(lon2_rad - lon1_rad)
    
    lat_mid = math.atan2(
        math.sin(lat1_rad) + math.sin(lat2_rad),
        math.sqrt((math.cos(lat1_rad) + Bx)**2 + By**2)
    )
    lon_mid = lon1_rad + math.atan2(By, math.cos(lat1_rad) + Bx)
    
    return math.degrees(lat_mid), math.degrees(lon_mid)


def calculate_elevation_angle(
    rx_lat: float, rx_lon: float,
    tx_lat: float, tx_lon: float,
    h_iono: float = DEFAULT_IONO_HEIGHT_KM
) -> float:
    """
    Calculate elevation angle from receiver to ionospheric reflection point.
    
    Args:
        rx_lat, rx_lon: Receiver location (degrees)
        tx_lat, tx_lon: Transmitter location (degrees)
        h_iono: Ionospheric height (km)
    
    Returns:
        Elevation angle (degrees)
    """
    # Great circle distance
    distance_km = great_circle_distance(rx_lat, rx_lon, tx_lat, tx_lon)
    
    # For single-hop, half the ground distance
    half_distance = distance_km / 2.0
    
    # Elevation angle from geometry
    # tan(elevation) = h / (half_distance)
    elevation_rad = math.atan2(h_iono, half_distance)
    
    return math.degrees(elevation_rad)


def great_circle_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculate great circle distance between two points.
    
    Args:
        lat1, lon1: First point (degrees)
        lat2, lon2: Second point (degrees)
    
    Returns:
        Distance in kilometers
    """
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    delta_lat = math.radians(lat2 - lat1)
    delta_lon = math.radians(lon2 - lon1)
    
    a = (math.sin(delta_lat / 2) ** 2 +
         math.cos(lat1_rad) * math.cos(lat2_rad) *
         math.sin(delta_lon / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    
    return EARTH_RADIUS_KM * c


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
    
    Args:
        tec_slant: Measured slant TEC (TECU)
        elevation_angle_deg: Elevation angle at receiver (degrees)
        h_iono: Ionospheric height (km, default 350)
    
    Returns:
        (vtec, obliquity_factor) tuple
    """
    theta_rad = math.radians(elevation_angle_deg)
    
    # Obliquity factor calculation
    sin_term = (EARTH_RADIUS_KM * math.cos(theta_rad)) / (EARTH_RADIUS_KM + h_iono)
    
    # Clamp to valid range to avoid math domain errors
    sin_term = max(-1.0, min(1.0, sin_term))
    
    M = 1.0 / math.cos(math.asin(sin_term))
    
    # Convert to vertical
    vtec = tec_slant / M
    
    return vtec, M


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
