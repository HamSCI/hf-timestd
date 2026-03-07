"""
Health monitoring API endpoints.
"""

import sys
from pathlib import Path
from fastapi import APIRouter, HTTPException
from typing import Dict, Any

from models.health import SystemHealth
from services.health_service import HealthService
from config import config

# Ensure hf_timestd is importable
sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'src'))
from hf_timestd.quota_manager import QuotaManager

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


@router.get("/storage")
async def get_storage_status():
    """
    Data storage inventory: days in storage, quota usage, per-category breakdown.

    Useful for determining which days of complete raw IQ data are available
    for retrieval (e.g. ionospheric event analysis).
    """
    try:
        mgr = QuotaManager(
            data_root=config.data_root,
            threshold_percent=75.0,
            min_days_to_keep=7,
        )
        return mgr.get_storage_inventory()
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
