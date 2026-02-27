"""
Chrony source comparison API endpoints.

Provides live and historical chrony source statistics for metrology validation.
"""

from fastapi import APIRouter, HTTPException, Query
from typing import Dict, Any

from services.chrony_service import ChronyService
from config import config

router = APIRouter(prefix="/chrony", tags=["chrony"])


@router.get("/snapshot")
async def get_live_snapshot() -> Dict[str, Any]:
    """Get a live chrony snapshot (sources + sourcestats + tracking)."""
    try:
        service = ChronyService(data_root=config.data_root)
        result = service.get_live_snapshot()
        if result is None:
            raise HTTPException(status_code=503, detail="chronyc not available")
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/comparison")
async def get_source_comparison() -> Dict[str, Any]:
    """Get a formatted source comparison table.

    Returns all chrony sources with their offsets, errors, states,
    and the current system tracking info.
    """
    try:
        service = ChronyService(data_root=config.data_root)
        return service.get_source_comparison()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/history")
async def get_source_history(
    hours: int = Query(default=24, ge=1, le=168, description="Hours of history to return"),
) -> Dict[str, Any]:
    """Get historical chrony source statistics from HDF5.

    Returns per-source time series of offset and std_dev over the
    requested time window (default 24h, max 7 days).
    """
    try:
        service = ChronyService(data_root=config.data_root)
        return service.get_history(hours=hours)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
