"""
Space Weather API endpoints.

Provides solar and geomagnetic data for correlation with HF propagation.
"""

from fastapi import APIRouter, Query, HTTPException
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from pathlib import Path
import logging

from services.space_weather_service import SpaceWeatherService
from config import config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/space-weather", tags=["space-weather"])

# Initialize service
space_weather_service = SpaceWeatherService()


@router.get("/current")
async def get_current_conditions():
    """
    Get current space weather conditions.
    
    Returns:
        Current X-ray flux, Kp index, proton flux, and active alerts
    """
    try:
        conditions = space_weather_service.get_current_conditions()
        return conditions
    except Exception as e:
        logger.error(f"Error getting current space weather: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/xray")
async def get_xray_flux(
    hours: int = Query(24, ge=1, le=168, description="Hours of history (max 168)")
):
    """
    Get X-ray flux time series from GOES satellites.
    
    Returns:
        List of X-ray flux measurements with classification (A/B/C/M/X)
    """
    try:
        data = space_weather_service.get_xray_flux(hours=hours)
        
        # Convert to JSON-serializable format
        result = {
            'timestamps': [x.timestamp for x in data],
            'flux': [x.flux_long for x in data],
            'classes': [x.get_class() for x in data],
            'satellites': [x.satellite for x in data],
            'count': len(data)
        }
        
        return result
    except Exception as e:
        logger.error(f"Error getting X-ray flux: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/kp")
async def get_kp_index(
    hours: int = Query(24, ge=1, le=168, description="Hours of history")
):
    """
    Get planetary Kp index time series.
    
    Returns:
        List of Kp index measurements (geomagnetic activity)
    """
    try:
        data = space_weather_service.get_kp_index(hours=hours)
        
        # Convert to JSON-serializable format
        result = {
            'timestamps': [k.timestamp for k in data],
            'kp': [k.kp for k in data],
            'kp_index': [k.kp_index for k in data],
            'observed': [k.observed for k in data],
            'count': len(data)
        }
        
        return result
    except Exception as e:
        logger.error(f"Error getting Kp index: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/protons")
async def get_proton_flux(
    hours: int = Query(24, ge=1, le=168, description="Hours of history")
):
    """
    Get proton flux time series from GOES satellites.
    
    Returns:
        List of proton flux measurements (>10 MeV)
    """
    try:
        data = space_weather_service.get_proton_flux(hours=hours)
        
        # Filter for >=10 MeV channel
        data_10mev = [p for p in data if '>=10' in p.energy]
        
        # Convert to JSON-serializable format
        result = {
            'timestamps': [p.timestamp for p in data_10mev],
            'flux': [p.flux for p in data_10mev],
            'energy': '>=10 MeV',
            'satellites': [p.satellite for p in data_10mev],
            'count': len(data_10mev)
        }
        
        return result
    except Exception as e:
        logger.error(f"Error getting proton flux: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/events/sid")
async def get_sid_events(
    hours: int = Query(24, ge=1, le=168, description="Hours of history")
):
    """
    Get detected Sudden Ionospheric Disturbance (SID) events.
    
    SID events are caused by solar flares and result in increased D-layer
    absorption, particularly on lower HF frequencies.
    
    Returns:
        List of detected SID events with timestamps and X-ray classes
    """
    try:
        events = space_weather_service.detect_sid_events(hours=hours)
        return {
            'events': events,
            'count': len(events)
        }
    except Exception as e:
        logger.error(f"Error detecting SID events: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/summary")
async def get_summary(
    hours: int = Query(24, ge=1, le=168, description="Hours of history")
):
    """
    Get comprehensive space weather summary.
    
    Combines X-ray, Kp, proton flux, and detected events into a single response.
    Useful for dashboard displays.
    
    Returns:
        Comprehensive space weather data package
    """
    try:
        # Get all data types
        current = space_weather_service.get_current_conditions()
        xray_data = space_weather_service.get_xray_flux(hours=hours)
        kp_data = space_weather_service.get_kp_index(hours=hours)
        proton_data = space_weather_service.get_proton_flux(hours=hours)
        sid_events = space_weather_service.detect_sid_events(hours=hours)
        
        # Calculate statistics
        xray_max = max([x.flux_long for x in xray_data]) if xray_data else 0
        xray_max_class = max([x.get_class() for x in xray_data], key=lambda c: c[0]) if xray_data else "A0.0"
        
        kp_max = max([k.kp_index for k in kp_data]) if kp_data else 0
        kp_avg = sum([k.kp_index for k in kp_data]) / len(kp_data) if kp_data else 0
        
        proton_10mev = [p for p in proton_data if '>=10' in p.energy]
        proton_max = max([p.flux for p in proton_10mev]) if proton_10mev else 0
        
        summary = {
            'timestamp': datetime.utcnow().isoformat() + 'Z',
            'period_hours': hours,
            'current': current,
            'statistics': {
                'xray': {
                    'max_flux': xray_max,
                    'max_class': xray_max_class,
                    'flare_count': len([x for x in xray_data if x.flux_long >= 1e-6])
                },
                'geomagnetic': {
                    'max_kp': kp_max,
                    'avg_kp': round(kp_avg, 1),
                    'storm_hours': len([k for k in kp_data if k.kp_index >= 5])
                },
                'protons': {
                    'max_flux': proton_max,
                    'pca_risk': 'HIGH' if proton_max >= 100 else 'MEDIUM' if proton_max >= 10 else 'LOW'
                }
            },
            'events': {
                'sid_count': len(sid_events),
                'sid_events': sid_events[:10]  # Limit to 10 most recent
            },
            'data_availability': {
                'xray_points': len(xray_data),
                'kp_points': len(kp_data),
                'proton_points': len(proton_10mev)
            }
        }
        
        return summary
    except Exception as e:
        logger.error(f"Error getting space weather summary: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/solar/elevation")
async def get_solar_elevation(
    start: str = Query("-6h", description="Start time (relative like '-6h' or ISO8601)"),
    end: str = Query("now", description="End time"),
    interval_minutes: int = Query(5, ge=1, le=60, description="Sample interval in minutes"),
):
    """
    Solar elevation angle at the path midpoint for each station (WWV, WWVH, CHU, BPM).

    Uses the receiver grid square from station config and the existing
    solar_zenith_calculator to compute elevation at the geographic midpoint
    of each propagation path. Elevation correlates with D-layer absorption.
    """
    try:
        import sys, os
        import math
        from datetime import datetime, timedelta

        # Resolve receiver grid from station config
        meta = config.station_metadata
        grid = meta.get("grid_square", "EM38ww")

        # Parse time window
        now = datetime.utcnow()
        def _parse(s):
            s = s.strip()
            if s == "now":
                return now
            if s.startswith("-"):
                val = float(s[1:-1])
                u = s[-1]
                if u == "h": return now - timedelta(hours=val)
                if u == "d": return now - timedelta(days=val)
                if u == "m": return now - timedelta(minutes=val)
            return datetime.fromisoformat(s.replace("Z", ""))

        t0 = _parse(start)
        t1 = _parse(end)

        # Import calculator (installed as part of hf_timestd package)
        from hf_timestd.core.solar_zenith_calculator import (
            grid_to_latlon, calculate_midpoint, solar_position,
            WWV_LOCATION, WWVH_LOCATION, CHU_LOCATION, BPM_LOCATION,
        )

        import math as _math

        rx_lat, rx_lon = grid_to_latlon(grid)

        def _gc_point(lat1, lon1, lat2, lon2, f=0.5):
            """Point at fraction f along the great-circle from (lat1,lon1) to (lat2,lon2)."""
            lat1r, lon1r = _math.radians(lat1), _math.radians(lon1)
            lat2r, lon2r = _math.radians(lat2), _math.radians(lon2)
            d = _math.acos(max(-1.0, min(1.0,
                _math.sin(lat1r)*_math.sin(lat2r) +
                _math.cos(lat1r)*_math.cos(lat2r)*_math.cos(lon2r - lon1r)
            )))
            if d < 1e-10:
                return lat1, lon1
            A = _math.sin((1 - f) * d) / _math.sin(d)
            B = _math.sin(f * d) / _math.sin(d)
            x = A*_math.cos(lat1r)*_math.cos(lon1r) + B*_math.cos(lat2r)*_math.cos(lon2r)
            y = A*_math.cos(lat1r)*_math.sin(lon1r) + B*_math.cos(lat2r)*_math.sin(lon2r)
            z = A*_math.sin(lat1r) + B*_math.sin(lat2r)
            lat = _math.degrees(_math.atan2(z, _math.sqrt(x*x + y*y)))
            lon = _math.degrees(_math.atan2(y, x))
            return lat, lon

        def _path_midpoint(rx_lat, rx_lon, tx_lat, tx_lon):
            """
            Return the ionospheric path midpoint.
            For paths > 60 deg GC distance (e.g. BPM at ~103 deg), the true
            geographic midpoint passes over the Arctic and is not representative
            of the D-layer above the propagation path near the receiver.
            Use the 1/4-path point instead so the solar elevation reflects the
            ionosphere in the receiver's hemisphere.
            """
            lat1r, lon1r = _math.radians(rx_lat), _math.radians(rx_lon)
            lat2r, lon2r = _math.radians(tx_lat), _math.radians(tx_lon)
            d_deg = _math.degrees(_math.acos(max(-1.0, min(1.0,
                _math.sin(lat1r)*_math.sin(lat2r) +
                _math.cos(lat1r)*_math.cos(lat2r)*_math.cos(lon2r - lon1r)
            ))))
            f = 0.25 if d_deg > 60 else 0.5
            return _gc_point(rx_lat, rx_lon, tx_lat, tx_lon, f)

        midpoints = {
            "WWV":  _path_midpoint(rx_lat, rx_lon, *WWV_LOCATION),
            "WWVH": _path_midpoint(rx_lat, rx_lon, *WWVH_LOCATION),
            "CHU":  _path_midpoint(rx_lat, rx_lon, *CHU_LOCATION),
            "BPM":  _path_midpoint(rx_lat, rx_lon, *BPM_LOCATION),
        }

        timestamps = []
        elevations = {s: [] for s in midpoints}

        current = t0
        dt = timedelta(minutes=interval_minutes)
        while current <= t1:
            timestamps.append(int(current.timestamp()))
            for station, (mlat, mlon) in midpoints.items():
                _, el = solar_position(current, mlat, mlon)
                elevations[station].append(round(el, 2))
            current += dt

        return {
            "status": "ok",
            "receiver_grid": grid,
            "receiver_lat": round(rx_lat, 4),
            "receiver_lon": round(rx_lon, 4),
            "interval_minutes": interval_minutes,
            "timestamps": timestamps,
            "elevations": elevations,
            "midpoints": {s: {"lat": round(v[0], 4), "lon": round(v[1], 4)}
                          for s, v in midpoints.items()},
        }
    except Exception as e:
        logger.error(f"Error computing solar elevation: {e}")
        raise HTTPException(status_code=500, detail=str(e))
