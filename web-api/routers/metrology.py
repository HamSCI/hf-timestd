"""
Metrology API endpoints.
"""

from fastapi import APIRouter, Query, HTTPException
from datetime import datetime, timedelta
from typing import Optional

from models.timing import FusionResponse, FusionHistoryResponse
from services.fusion_service import FusionService
from config import config

router = APIRouter(prefix="/metrology", tags=["metrology"])


def parse_time_param(time_str: str) -> datetime:
    """
    Parse time parameter (ISO8601 or relative).
    
    Args:
        time_str: Time string (e.g., "2026-01-03T12:00:00Z", "-6h", "now")
        
    Returns:
        Datetime object
    """
    if time_str == "now":
        return datetime.utcnow()
    
    # Relative time (e.g., "-6h", "-1d")
    if time_str.startswith("-"):
        value = int(time_str[1:-1])
        unit = time_str[-1]
        
        if unit == 'h':
            return datetime.utcnow() - timedelta(hours=value)
        elif unit == 'd':
            return datetime.utcnow() - timedelta(days=value)
        elif unit == 'm':
            return datetime.utcnow() - timedelta(minutes=value)
        elif unit == 's':
            return datetime.utcnow() - timedelta(seconds=value)
        else:
            raise ValueError(f"Unknown time unit: {unit}")
    
    # ISO8601
    return datetime.fromisoformat(time_str.replace('Z', '+00:00'))


@router.get("/fusion/latest", response_model=FusionResponse)
async def get_latest_fusion():
    """
    Get latest fusion timing estimate.
    
    Returns the most recent multi-station fusion estimate including:
    - D_clock offset with uncertainty
    - Quality grade
    - Contributing stations
    - Number of broadcasts
    """
    try:
        service = FusionService(fusion_dir=config.fusion_dir)
        result = service.get_latest()
        
        if result is None:
            raise HTTPException(
                status_code=404,
                detail="No recent fusion data available"
            )
        
        return result
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/fusion/history", response_model=FusionHistoryResponse)
async def get_fusion_history(
    start: str = Query("-6h", description="Start time (ISO8601 or relative, e.g., '-6h')"),
    end: str = Query("now", description="End time (ISO8601 or 'now')"),
    min_quality: Optional[str] = Query(None, description="Minimum quality grade (A/B/C/D)")
):
    """
    Get fusion timing history.
    
    Returns time series of fusion estimates for the specified time range.
    Supports quality filtering and relative time specifications.
    
    Examples:
    - Last 6 hours: ?start=-6h&end=now
    - Last day: ?start=-1d&end=now
    - Specific range: ?start=2026-01-03T00:00:00Z&end=2026-01-03T12:00:00Z
    - Quality filtered: ?start=-6h&end=now&min_quality=B
    """
    try:
        # Parse time parameters
        start_dt = parse_time_param(start)
        end_dt = parse_time_param(end)
        
        # Validate time range
        if start_dt > end_dt:
            raise HTTPException(
                status_code=400,
                detail="Start time must be before end time"
            )
        
        service = FusionService(fusion_dir=config.fusion_dir)
        return service.get_history(
            start=start_dt,
            end=end_dt,
            min_quality_grade=min_quality
        )
    
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
