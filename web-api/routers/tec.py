"""
TEC (Total Electron Content) API endpoints for v6.5.0.
"""

from fastapi import APIRouter, Query, HTTPException
from datetime import datetime, timedelta
from typing import Optional
import logging

from services.tec_service import TECService
from config import config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tec", tags=["ionosphere"])

# Initialize service
tec_service = TECService(config.data_root)


@router.get("/current")
async def get_current_tec():
    """
    Get the most recent TEC estimates.
    
    Returns current TEC values for all monitored propagation paths.
    """
    try:
        data = tec_service.get_current_tec()
        
        if data is None:
            return {
                "status": "no_data",
                "message": "No TEC data available",
                "timestamp": datetime.utcnow().isoformat() + 'Z'
            }
        
        return {
            "status": "ok",
            "timestamp": data.get('timestamp', ''),
            "paths": data.get('paths', {}),
            "n_paths": len(data.get('paths', {}))
        }
    
    except Exception as e:
        logger.error(f"Error getting current TEC: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/history")
async def get_tec_history(
    start: str = Query("-24h", description="Start time (ISO8601 or relative like '-24h')"),
    end: str = Query("now", description="End time (ISO8601 or 'now')"),
    station: Optional[str] = Query(None, description="Filter by station (WWV, WWVH, CHU, BPM)")
):
    """
    Get TEC history for a time range.
    
    Returns TEC values over time for all paths or filtered by station.
    """
    try:
        # Parse time range
        if end == "now":
            end_time = datetime.utcnow()
        else:
            end_time = datetime.fromisoformat(end.replace('Z', ''))
        
        if start.startswith('-'):
            duration_str = start[1:]
            if duration_str.endswith('h'):
                hours = int(duration_str[:-1])
                start_time = end_time - timedelta(hours=hours)
            elif duration_str.endswith('d'):
                days = int(duration_str[:-1])
                start_time = end_time - timedelta(days=days)
            else:
                raise ValueError(f"Invalid duration format: {start}")
        else:
            start_time = datetime.fromisoformat(start.replace('Z', ''))
        
        data = tec_service.get_tec_history(start_time, end_time, station=station)
        
        return {
            "status": "ok",
            **data
        }
    
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error getting TEC history: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/station/{station}")
async def get_tec_by_station(
    station: str,
    hours: int = Query(24, description="Number of hours of history")
):
    """
    Get TEC data for a specific station.
    
    Returns TEC values for all frequencies from the specified station.
    """
    try:
        station = station.upper()
        if station not in ['WWV', 'WWVH', 'CHU', 'BPM']:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid station: {station}. Must be WWV, WWVH, CHU, or BPM"
            )
        
        data = tec_service.get_tec_by_station(station, hours)
        
        return {
            "status": "ok",
            "station": station,
            **data
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting TEC for station {station}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
