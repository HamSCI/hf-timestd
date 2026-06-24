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

from hamsci_dsp.geometry import grid_to_latlon as _grid_to_latlon, midpoint as _midpoint
from hamsci_dsp.ionosphere.solar import solar_position as _solar_position

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
    """Convert Maidenhead grid square to latitude/longitude.

    Delegates the parsing to :func:`hamsci_dsp.geometry.grid_to_latlon`
    (math-identical: square-centre for 4-char, subsquare-centre for 6-char).
    The explicit length check is kept so the historical "too short" error
    message is preserved for callers/tests.
    """
    if len(grid.strip()) < 4:
        raise ValueError(f"Grid square too short: {grid}")
    return _grid_to_latlon(grid)


def calculate_midpoint(lat1: float, lon1: float, lat2: float, lon2: float) -> Tuple[float, float]:
    """Calculate geographic midpoint between two points.

    Delegates to :func:`hamsci_dsp.geometry.midpoint` (geodesic WGS-84
    distance-halfway point). This replaces the former Cartesian-average
    (chord-midpoint) approximation; for HF path lengths the two agree to a
    few hundredths of a degree, and the geodesic point is the more accurate
    ionospheric pierce point.
    """
    return _midpoint(lat1, lon1, lat2, lon2)


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
    # Delegates to hamsci_dsp.ionosphere.solar, the canonical home for this
    # NOAA solar-calculator algorithm (extracted math-identical from here;
    # hamsci additionally clamps the azimuth acos argument and guards the
    # sin(zenith)≈0 singularity, so it is strictly more robust at the poles).
    return _solar_position(dt, lat, lon)


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
