"""
Propagation and ionospheric analysis API endpoints.
"""

from fastapi import APIRouter, Query, HTTPException
from datetime import datetime, timedelta
from typing import Optional
import logging

from services.propagation_service import PropagationService
from config import config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/propagation", tags=["propagation"])

# Initialize service
propagation_service = PropagationService(config.data_root)


@router.get("/conditions")
async def get_current_conditions():
    """
    Get current propagation conditions.
    
    Returns summary of propagation modes, MUF estimates, and signal quality
    from the last hour of measurements.
    """
    try:
        conditions = propagation_service.get_current_conditions()
        
        if conditions is None:
            raise HTTPException(
                status_code=404,
                detail="No recent propagation data available"
            )
        
        return conditions
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting propagation conditions: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/timeline")
async def get_mode_timeline(
    start: str = Query("-6h", description="Start time (ISO8601 or relative like '-6h')"),
    end: str = Query("now", description="End time (ISO8601 or 'now')"),
    station: Optional[str] = Query(None, description="Filter by station (WWV, WWVH, CHU, BPM)")
):
    """
    Get propagation mode timeline.
    
    Returns time series of propagation modes observed across all channels.
    """
    try:
        # Parse time range
        if end == "now":
            end_time = datetime.utcnow()
        else:
            end_time = datetime.fromisoformat(end.replace('Z', ''))
        
        if start.startswith('-'):
            # Relative time
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
        
        timeline = propagation_service.get_mode_timeline(start_time, end_time, station)
        
        if timeline is None:
            raise HTTPException(
                status_code=404,
                detail="No propagation timeline data available"
            )
        
        return timeline
    
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting propagation timeline: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/tec")
async def get_tec_summary(
    start: str = Query("-24h", description="Start time (ISO8601 or relative like '-24h')"),
    end: str = Query("now", description="End time (ISO8601 or 'now')")
):
    """
    Get TEC (Total Electron Content) summary.
    
    Returns HF-derived TEC measurements and trends.
    """
    try:
        # Parse time range
        if end == "now":
            end_time = datetime.utcnow()
        else:
            end_time = datetime.fromisoformat(end.replace('Z', ''))
        
        if start.startswith('-'):
            # Relative time
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
        
        tec_data = propagation_service.get_tec_summary(start_time, end_time)
        
        if tec_data is None:
            raise HTTPException(
                status_code=404,
                detail="No TEC data available"
            )
        
        return tec_data
    
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting TEC summary: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/test-signals")
async def get_test_signals(
    start: str = Query("-24h", description="Start time (ISO8601 or relative like '-24h')"),
    end: str = Query("now", description="End time (ISO8601 or 'now')")
):
    """
    Get WWV/WWVH test signal analysis data.
    
    Returns test signal detections from minutes 8 (WWV) and 44 (WWVH) with
    ionospheric metrics including field strength, delay spread, and scintillation.
    """
    try:
        # Parse time range
        if end == "now":
            end_time = datetime.utcnow()
        else:
            end_time = datetime.fromisoformat(end.replace('Z', ''))
        
        if start.startswith('-'):
            # Relative time
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
        
        test_signal_data = propagation_service.get_test_signal_summary(start_time, end_time)
        
        if test_signal_data is None:
            raise HTTPException(
                status_code=404,
                detail="No test signal data available"
            )
        
        return test_signal_data
    
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting test signal data: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
