"""
Health monitoring API endpoints.
"""

from fastapi import APIRouter, HTTPException
from typing import Dict, Any

from models.health import SystemHealth
from services.health_service import HealthService
from config import config

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/system", response_model=SystemHealth)
async def get_system_health():
    """
    Get overall system health status.
    
    Returns system health including:
    - Overall status (healthy/degraded/error)
    - Channel statuses
    - Process statuses
    - Disk usage
    - Data completeness
    """
    try:
        service = HealthService(
            data_root=config.data_root,
            channels=config.channels
        )
        return service.get_system_health()
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/channels")
async def get_channel_status():
    """
    Get status for all channels.
    
    Returns detailed status for each configured channel including:
    - Channel name and frequency
    - Active/inactive status
    - Last update time
    - Signal quality metrics
    """
    try:
        service = HealthService(
            data_root=config.data_root,
            channels=config.channels
        )
        health = service.get_system_health()
        return {"channels": health['channels']}
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
