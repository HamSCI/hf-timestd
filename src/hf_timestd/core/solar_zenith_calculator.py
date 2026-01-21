"""
Solar Zenith Angle Calculator for WWV/WWVH/CHU Path Midpoints

Calculates solar elevation angles at the midpoint of the propagation path
between a receiver location and time signal transmitters.

Solar elevation at the path midpoint correlates with D-layer absorption
and propagation conditions on HF time signal frequencies.

Issue 4.1 Fix (2025-12-07): Station coordinates now imported from wwv_constants.py
(single source of truth with NIST/NRC verified values).

Usage:
    python -m hf_timestd.solar_zenith_calculator --date 20251127 --grid EM38ww
"""

import math
import json
import argparse
from datetime import datetime, timedelta
from typing import Tuple, List, Dict

# Import transmitter coordinates from single source of truth
# Handle both module import and standalone script execution
try:
    from .wwv_constants import WWV_LAT, WWV_LON, WWVH_LAT, WWVH_LON, CHU_LAT, CHU_LON, BPM_LAT, BPM_LON
except ImportError:
    from wwv_constants import WWV_LAT, WWV_LON, WWVH_LAT, WWVH_LON, CHU_LAT, CHU_LON, BPM_LAT, BPM_LON

# Transmitter coordinates (lat, lon in degrees) - from wwv_constants.py
WWV_LOCATION = (WWV_LAT, WWV_LON)     # Fort Collins, Colorado - NIST verified
WWVH_LOCATION = (WWVH_LAT, WWVH_LON)  # Kekaha, Kauai, Hawaii - NIST verified
CHU_LOCATION = (CHU_LAT, CHU_LON)     # Ottawa, Canada - NRC verified
BPM_LOCATION = (BPM_LAT, BPM_LON)     # Pucheng, Shaanxi, China - NTSC


def grid_to_latlon(grid: str) -> Tuple[float, float]:
    """Convert Maidenhead grid square to latitude/longitude"""
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


def calculate_midpoint(lat1: float, lon1: float, lat2: float, lon2: float) -> Tuple[float, float]:
    """Calculate geographic midpoint between two points"""
    # Convert to radians
    lat1_r = math.radians(lat1)
    lon1_r = math.radians(lon1)
    lat2_r = math.radians(lat2)
    lon2_r = math.radians(lon2)
    
    # Convert to Cartesian
    x1 = math.cos(lat1_r) * math.cos(lon1_r)
    y1 = math.cos(lat1_r) * math.sin(lon1_r)
    z1 = math.sin(lat1_r)
    
    x2 = math.cos(lat2_r) * math.cos(lon2_r)
    y2 = math.cos(lat2_r) * math.sin(lon2_r)
    z2 = math.sin(lat2_r)
    
    # Average
    x = (x1 + x2) / 2
    y = (y1 + y2) / 2
    z = (z1 + z2) / 2
    
    # Convert back to lat/lon
    lon = math.atan2(y, x)
    hyp = math.sqrt(x*x + y*y)
    lat = math.atan2(z, hyp)
    
    return math.degrees(lat), math.degrees(lon)


def solar_position(dt: datetime, lat: float, lon: float) -> Tuple[float, float]:
    """
    Calculate solar azimuth and elevation angle for given time and location.
    
    Based on NOAA solar calculator algorithms.
    
    Args:
        dt: UTC datetime
        lat: Latitude in degrees (positive = North)
        lon: Longitude in degrees (positive = East)
        
    Returns:
        Tuple of (azimuth, elevation) in degrees
    """
    # Julian date
    a = (14 - dt.month) // 12
    y = dt.year + 4800 - a
    m = dt.month + 12 * a - 3
    jd = dt.day + (153 * m + 2) // 5 + 365 * y + y // 4 - y // 100 + y // 400 - 32045
    jd = jd + (dt.hour - 12) / 24 + dt.minute / 1440 + dt.second / 86400
    
    # Julian century
    t = (jd - 2451545.0) / 36525.0
    
    # Geometric mean longitude of sun (degrees)
    L0 = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360
    
    # Geometric mean anomaly of sun (degrees)
    M = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    M_rad = math.radians(M)
    
    # Eccentricity of Earth's orbit
    e = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    
    # Sun's equation of center
    C = (math.sin(M_rad) * (1.914602 - t * (0.004817 + 0.000014 * t)) +
         math.sin(2 * M_rad) * (0.019993 - 0.000101 * t) +
         math.sin(3 * M_rad) * 0.000289)
    
    # Sun's true longitude
    sun_lon = L0 + C
    
    # Sun's apparent longitude
    omega = 125.04 - 1934.136 * t
    apparent_lon = sun_lon - 0.00569 - 0.00478 * math.sin(math.radians(omega))
    
    # Mean obliquity of ecliptic
    obliq_mean = 23 + (26 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60) / 60
    
    # Corrected obliquity
    obliq_corr = obliq_mean + 0.00256 * math.cos(math.radians(omega))
    obliq_corr_rad = math.radians(obliq_corr)
    
    # Sun's declination
    sin_decl = math.sin(obliq_corr_rad) * math.sin(math.radians(apparent_lon))
    decl = math.degrees(math.asin(sin_decl))
    decl_rad = math.radians(decl)
    
    # Equation of time (minutes)
    var_y = math.tan(obliq_corr_rad / 2) ** 2
    L0_rad = math.radians(L0)
    eq_time = 4 * math.degrees(
        var_y * math.sin(2 * L0_rad) -
        2 * e * math.sin(M_rad) +
        4 * e * var_y * math.sin(M_rad) * math.cos(2 * L0_rad) -
        0.5 * var_y * var_y * math.sin(4 * L0_rad) -
        1.25 * e * e * math.sin(2 * M_rad)
    )
    
    # True solar time (minutes)
    time_offset = eq_time + 4 * lon
    true_solar_time = (dt.hour * 60 + dt.minute + dt.second / 60 + time_offset) % 1440
    
    # Hour angle (degrees)
    if true_solar_time < 0:
        hour_angle = true_solar_time / 4 + 180
    else:
        hour_angle = true_solar_time / 4 - 180
    hour_angle_rad = math.radians(hour_angle)
    
    # Solar zenith angle
    lat_rad = math.radians(lat)
    cos_zenith = (math.sin(lat_rad) * math.sin(decl_rad) +
                  math.cos(lat_rad) * math.cos(decl_rad) * math.cos(hour_angle_rad))
    
    # Clamp to valid range
    cos_zenith = max(-1, min(1, cos_zenith))
    zenith = math.degrees(math.acos(cos_zenith))
    elevation = 90 - zenith
    
    # Solar azimuth
    if hour_angle > 0:
        azimuth = (math.degrees(math.acos(
            ((math.sin(lat_rad) * cos_zenith) - math.sin(decl_rad)) /
            (math.cos(lat_rad) * math.sin(math.radians(zenith)))
        )) + 180) % 360
    else:
        azimuth = (540 - math.degrees(math.acos(
            ((math.sin(lat_rad) * cos_zenith) - math.sin(decl_rad)) /
            (math.cos(lat_rad) * math.sin(math.radians(zenith)))
        ))) % 360
    
    return azimuth, elevation


def calculate_solar_zenith_for_day(
    date_str: str,
    receiver_grid: str,
    interval_minutes: int = 5
) -> Dict:
    """
    Calculate solar zenith angles for WWV, WWVH, and CHU path midpoints over 24 hours.
    
    Args:
        date_str: Date in YYYYMMDD format
        receiver_grid: Maidenhead grid square (e.g., "EM38ww")
        interval_minutes: Time interval between samples
        
    Returns:
        Dictionary with solar zenith data for all paths
    """
    # Parse date
    year = int(date_str[0:4])
    month = int(date_str[4:6])
    day = int(date_str[6:8])
    
    # Get receiver location
    rx_lat, rx_lon = grid_to_latlon(receiver_grid)
    
    # Calculate midpoints for all transmitters
    wwv_mid_lat, wwv_mid_lon = calculate_midpoint(rx_lat, rx_lon, *WWV_LOCATION)
    wwvh_mid_lat, wwvh_mid_lon = calculate_midpoint(rx_lat, rx_lon, *WWVH_LOCATION)
    chu_mid_lat, chu_mid_lon = calculate_midpoint(rx_lat, rx_lon, *CHU_LOCATION)
    bpm_mid_lat, bpm_mid_lon = calculate_midpoint(rx_lat, rx_lon, *BPM_LOCATION)
    
    # Generate time series
    start_time = datetime(year, month, day, 0, 0, 0)
    times = []
    wwv_elevations = []
    wwvh_elevations = []
    chu_elevations = []
    bpm_elevations = []
    
    current_time = start_time
    end_time = start_time + timedelta(days=1)
    
    while current_time < end_time:
        times.append(current_time.strftime("%Y-%m-%dT%H:%M:%SZ"))
        
        # WWV path midpoint
        _, wwv_el = solar_position(current_time, wwv_mid_lat, wwv_mid_lon)
        wwv_elevations.append(round(wwv_el, 2))
        
        # WWVH path midpoint
        _, wwvh_el = solar_position(current_time, wwvh_mid_lat, wwvh_mid_lon)
        wwvh_elevations.append(round(wwvh_el, 2))
        
        # CHU path midpoint
        _, chu_el = solar_position(current_time, chu_mid_lat, chu_mid_lon)
        chu_elevations.append(round(chu_el, 2))
        
        # BPM path midpoint
        _, bpm_el = solar_position(current_time, bpm_mid_lat, bpm_mid_lon)
        bpm_elevations.append(round(bpm_el, 2))
        
        current_time += timedelta(minutes=interval_minutes)
    
    return {
        "date": date_str,
        "receiver_grid": receiver_grid,
        "receiver_location": {"lat": round(rx_lat, 4), "lon": round(rx_lon, 4)},
        "wwv_midpoint": {"lat": round(wwv_mid_lat, 4), "lon": round(wwv_mid_lon, 4)},
        "wwvh_midpoint": {"lat": round(wwvh_mid_lat, 4), "lon": round(wwvh_mid_lon, 4)},
        "chu_midpoint": {"lat": round(chu_mid_lat, 4), "lon": round(chu_mid_lon, 4)},
        "bpm_midpoint": {"lat": round(bpm_mid_lat, 4), "lon": round(bpm_mid_lon, 4)},
        "interval_minutes": interval_minutes,
        "timestamps": times,
        "wwv_solar_elevation": wwv_elevations,
        "wwvh_solar_elevation": wwvh_elevations,
        "chu_solar_elevation": chu_elevations,
        "bpm_solar_elevation": bpm_elevations
    }


def main():
    parser = argparse.ArgumentParser(description="Calculate solar zenith angles for WWV/WWVH/CHU paths")
    parser.add_argument("--date", required=True, help="Date in YYYYMMDD format")
    parser.add_argument("--grid", required=True, help="Maidenhead grid square (e.g., EM38ww)")
    parser.add_argument("--interval", type=int, default=5, help="Interval in minutes (default: 5)")
    
    args = parser.parse_args()
    
    result = calculate_solar_zenith_for_day(args.date, args.grid, args.interval)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
