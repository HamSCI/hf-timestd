"""
TID (Traveling Ionospheric Disturbance) API endpoints for v6.5.0.
"""

from fastapi import APIRouter, Query, HTTPException
from datetime import datetime, timedelta
from typing import Optional
import logging

from services.tid_service import TIDService
from config import config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tid", tags=["ionosphere"])

# Initialize service
tid_service = TIDService(config.data_root)


@router.get("/events")
async def get_tid_events(
    hours: int = Query(24, description="Number of hours to look back")
):
    """
    Get recent TID events.
    
    Returns list of detected Traveling Ionospheric Disturbances.
    """
    try:
        events = tid_service.get_recent_events(hours)
        
        return {
            "status": "ok",
            "n_events": len(events),
            "hours": hours,
            "events": events
        }
    
    except Exception as e:
        logger.error(f"Error getting TID events: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/events/{event_id}")
async def get_tid_event_details(event_id: str):
    """
    Get detailed information about a specific TID event.
    
    Returns full event data including correlation and residual time series.
    """
    try:
        event = tid_service.get_event_details(event_id)
        
        if event is None:
            raise HTTPException(
                status_code=404,
                detail=f"TID event not found: {event_id}"
            )
        
        return {
            "status": "ok",
            "event": event
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting TID event details: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/statistics")
async def get_tid_statistics(
    days: int = Query(7, description="Number of days to analyze")
):
    """
    Get TID detection statistics.
    
    Returns summary statistics including event rate, velocity distribution,
    and propagation direction histogram.
    """
    try:
        stats = tid_service.get_statistics(days)
        
        return {
            "status": "ok",
            **stats
        }
    
    except Exception as e:
        logger.error(f"Error getting TID statistics: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/history")
async def get_tid_history(
    start: str = Query("-7d", description="Start time (ISO8601 or relative like '-7d')"),
    end: str = Query("now", description="End time (ISO8601 or 'now')")
):
    """
    Get TID events within a time range.
    
    Returns all TID events detected between start and end times.
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
        
        events = tid_service.get_events_in_range(start_time, end_time)
        
        return {
            "status": "ok",
            "n_events": len(events),
            "time_range": {
                "start": start_time.isoformat() + 'Z',
                "end": end_time.isoformat() + 'Z'
            },
            "events": events
        }
    
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error getting TID history: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
