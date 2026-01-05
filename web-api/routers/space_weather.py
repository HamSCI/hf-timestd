"""
Space Weather API endpoints.

Provides solar and geomagnetic data for correlation with HF propagation.
"""

from fastapi import APIRouter, Query, HTTPException
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
import logging

from services.space_weather_service import SpaceWeatherService

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
