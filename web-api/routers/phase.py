"""
Phase and Doppler analysis API endpoints.

Provides:
- Phase time series (unwrapped carrier phase)
- Doppler shift (from phase rate)
- Phase scintillation index (sigma_phi)
- Summary of current phase/Doppler state
"""

from fastapi import APIRouter, Query, HTTPException
from datetime import datetime, timedelta, timezone
from typing import Optional
import logging

from services.phase_service import PhaseService
from config import config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/phase", tags=["phase"])

# Initialize service
phase_service = PhaseService(config.data_root)


def _parse_time_range(start: str, end: str):
    """Parse start/end time strings into datetime objects."""
    if end == "now":
        end_time = datetime.now(timezone.utc)
    else:
        end_time = datetime.fromisoformat(end.replace('Z', '+00:00'))

    if start.startswith('-'):
        duration_str = start[1:]
        if duration_str.endswith('h'):
            hours = int(duration_str[:-1])
            start_time = end_time - timedelta(hours=hours)
        elif duration_str.endswith('d'):
            days = int(duration_str[:-1])
            start_time = end_time - timedelta(days=days)
        elif duration_str.endswith('m'):
            minutes = int(duration_str[:-1])
            start_time = end_time - timedelta(minutes=minutes)
        elif duration_str.endswith('s'):
            seconds = int(duration_str[:-1])
            start_time = end_time - timedelta(seconds=seconds)
        else:
            raise ValueError(f"Invalid duration format: {start}")
    else:
        start_time = datetime.fromisoformat(start.replace('Z', '+00:00'))

    return start_time, end_time


@router.get("/timeseries")
async def get_phase_timeseries(
    start: str = Query("-1h", description="Start time (ISO8601 or relative like '-1h', '-24h')"),
    end: str = Query("now", description="End time (ISO8601 or 'now')"),
    channel: Optional[str] = Query(None, description="Channel name (e.g., SHARED_10000, CHU_14670)"),
    station: Optional[str] = Query(None, description="Station name (WWV, WWVH, CHU, BPM)"),
    phase_type: str = Query("carrier_phase_rad", description="Phase field: phase_rad, carrier_phase_rad, dc_carrier_phase_rad"),
    unwrap: bool = Query(True, description="Unwrap phase for continuous tracking"),
):
    """
    Get phase time series from L2/tick_phase data.

    Returns per-channel, per-station phase traces suitable for plotting.
    Unwrapped carrier_phase_rad is the primary ionospheric observable.
    """
    try:
        start_time, end_time = _parse_time_range(start, end)

        if phase_type not in ('phase_rad', 'carrier_phase_rad', 'dc_carrier_phase_rad'):
            raise ValueError(f"Invalid phase_type: {phase_type}")

        result = phase_service.get_phase_timeseries(
            start_time, end_time,
            channel=channel, station=station,
            phase_type=phase_type, unwrap=unwrap
        )
        return result

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error getting phase timeseries: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/doppler")
async def get_doppler(
    start: str = Query("-1h", description="Start time"),
    end: str = Query("now", description="End time"),
    channel: Optional[str] = Query(None, description="Channel name"),
    station: Optional[str] = Query(None, description="Station name"),
    smoothing: float = Query(30.0, description="Smoothing window in seconds"),
):
    """
    Get Doppler shift derived from carrier phase rate.

    f_Doppler = -(1/2pi) * dphi/dt

    For 10 MHz carrier: 1 Hz Doppler = ~30 m/s ionospheric motion.
    """
    try:
        start_time, end_time = _parse_time_range(start, end)
        result = phase_service.get_doppler(
            start_time, end_time,
            channel=channel, station=station,
            smoothing_seconds=smoothing
        )
        return result

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error getting Doppler: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/scintillation")
async def get_scintillation(
    start: str = Query("-1h", description="Start time"),
    end: str = Query("now", description="End time"),
    channel: Optional[str] = Query(None, description="Channel name"),
    station: Optional[str] = Query(None, description="Station name"),
    window: float = Query(60.0, description="Scintillation window in seconds"),
):
    """
    Get phase scintillation index (sigma_phi) time series.

    sigma_phi = std(detrended phase) over sliding windows.
    High sigma_phi (> 0.3 rad) indicates ionospheric irregularities.
    """
    try:
        start_time, end_time = _parse_time_range(start, end)
        result = phase_service.get_scintillation(
            start_time, end_time,
            channel=channel, station=station,
            window_seconds=window
        )
        return result

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error getting scintillation: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/channels")
async def get_available_channels():
    """
    List all channels and stations that have tick_phase data on disk.

    Used by the frontend to populate channel/station filter dropdowns.
    """
    try:
        result = phase_service.get_available_channels()
        return result

    except Exception as e:
        logger.error(f"Error listing channels: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/summary")
async def get_phase_summary():
    """
    Get current phase/Doppler state across all channels.

    Returns latest Doppler, scintillation, and SNR for each active channel/station pair.
    """
    try:
        result = phase_service.get_phase_summary()
        return result

    except Exception as e:
        logger.error(f"Error getting phase summary: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
