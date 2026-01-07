"""
Physics API endpoints.
"""

from fastapi import APIRouter, Query, HTTPException
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

from services.physics_service import PhysicsService
from config import config

router = APIRouter(prefix="/physics", tags=["physics"])


def parse_time_param(time_str: str) -> datetime:
    """Parse time parameter (ISO8601 or relative)."""
    if time_str == "now":
        return datetime.utcnow()
    
    if time_str.startswith("-"):
        value = int(time_str[1:-1])
        unit = time_str[-1]
        
        if unit == 'h':
            return datetime.utcnow() - timedelta(hours=value)
        elif unit == 'd':
            return datetime.utcnow() - timedelta(days=value)
        elif unit == 'm':
            return datetime.utcnow() - timedelta(minutes=value)
        else:
            raise ValueError(f"Unknown time unit: {unit}")
    
    return datetime.fromisoformat(time_str.replace('Z', '+00:00'))


@router.get("/latest")
async def get_latest_physics():
    """Get latest physics data (TEC, UTC consistency)."""
    try:
        service = PhysicsService(data_root=config.data_root)
        result = service.get_latest()
        
        return result
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/history")
async def get_physics_history(
    start: str = Query("-6h", description="Start time"),
    end: str = Query("now", description="End time")
):
    """Get physics data history."""
    try:
        start_dt = parse_time_param(start)
        end_dt = parse_time_param(end)
        
        if start_dt > end_dt:
            raise HTTPException(status_code=400, detail="Start time must be before end time")
        
        service = PhysicsService(data_root=config.data_root)
        return service.get_history(start=start_dt, end=end_dt)
    
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
