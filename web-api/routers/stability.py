"""
Stability analysis API endpoints.
"""

from fastapi import APIRouter, Query, HTTPException
from datetime import datetime, timedelta
from typing import Optional
import logging

from services.stability_service import StabilityService
from config import config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/stability", tags=["stability"])

# Initialize service
stability_service = StabilityService(config.data_root)


@router.get("/adev")
async def get_allan_deviation(
    start: str = Query("-24h", description="Start time (ISO8601 or relative like '-24h')"),
    end: str = Query("now", description="End time (ISO8601 or 'now')"),
):
    """
    Get Allan deviation analysis.
    
    Returns overlapping Allan deviation computed from fusion timing data.
    Includes tau values, ADEV values, and noise type identification.
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
        
        # Compute stability metrics
        metrics = stability_service.compute_stability_metrics(start_time, end_time)
        
        if metrics is None:
            raise HTTPException(
                status_code=404,
                detail="Insufficient data for stability analysis"
            )
        
        return metrics
    
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error getting Allan deviation: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
