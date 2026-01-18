"""
Physics API endpoints.
"""

from fastapi import APIRouter, Query, HTTPException
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

from services.physics_service import PhysicsService
from services.scintillation_service import ScintillationService
from services.test_signal_service import TestSignalService
from services.event_service import EventService
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


# ============================================================================
# SCINTILLATION ENDPOINTS
# ============================================================================

@router.get("/scintillation/paths")
async def get_scintillation_by_path():
    """
    Get scintillation indices organized by propagation path.
    
    Returns S4 (amplitude) and σ_φ (phase) scintillation indices
    for each broadcast station path.
    """
    try:
        service = ScintillationService(data_root=config.data_root)
        return service.get_latest_by_path()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/scintillation/history")
async def get_scintillation_history(
    start: str = Query("-6h", description="Start time"),
    end: str = Query("now", description="End time"),
    station: Optional[str] = Query(None, description="Station filter (WWV, WWVH, CHU, BPM)")
):
    """Get scintillation history."""
    try:
        start_dt = parse_time_param(start)
        end_dt = parse_time_param(end)
        
        service = ScintillationService(data_root=config.data_root)
        return service.get_history(start=start_dt, end=end_dt, station=station)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# TEST SIGNAL / CHANNEL CHARACTERIZATION ENDPOINTS
# ============================================================================

@router.get("/channels/latest")
async def get_channel_characterization():
    """
    Get latest test signal analysis results.
    
    Returns channel characterization from WWV/WWVH scientific test signals
    including delay spread, coherence time, and frequency selectivity.
    """
    try:
        service = TestSignalService(data_root=config.data_root)
        return service.get_latest()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/channels/summary")
async def get_channel_summary():
    """
    Get channel quality summary across all frequencies.
    """
    try:
        service = TestSignalService(data_root=config.data_root)
        return service.get_channel_summary()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/channels/history")
async def get_channel_history(
    start: str = Query("-24h", description="Start time"),
    end: str = Query("now", description="End time"),
    frequency_mhz: Optional[float] = Query(None, description="Frequency filter")
):
    """Get test signal history."""
    try:
        start_dt = parse_time_param(start)
        end_dt = parse_time_param(end)
        
        service = TestSignalService(data_root=config.data_root)
        return service.get_history(start=start_dt, end=end_dt, frequency_mhz=frequency_mhz)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# EVENT DETECTION ENDPOINTS
# ============================================================================

@router.get("/events/recent")
async def get_recent_events(
    hours: int = Query(24, description="Hours to look back", ge=1, le=168)
):
    """
    Get recent ionospheric events.
    
    Returns detected events including:
    - Sunrise/sunset transitions
    - Signal loss/recovery events
    - Propagation mode changes
    - Anomalous conditions
    """
    try:
        service = EventService(data_root=config.data_root)
        return service.get_recent_events(hours=hours)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/events/conditions")
async def get_current_conditions():
    """
    Get current ionospheric conditions summary.
    
    Returns day/night status, sun times, and recent event counts.
    """
    try:
        service = EventService(data_root=config.data_root)
        return service.get_current_conditions()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
