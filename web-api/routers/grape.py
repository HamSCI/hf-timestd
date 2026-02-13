"""
GRAPE router - spectrograms, decimation status, upload history.
"""

import logging
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pathlib import Path

from services.grape_service import GrapeService
from config import config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/grape", tags=["grape"])

_service = GrapeService(data_root=str(config.data_root))


@router.get("/summary")
async def grape_summary():
    """Overall GRAPE pipeline summary."""
    return _service.get_summary()


@router.get("/channels")
async def grape_channels():
    """List channels with decimated data."""
    return {"channels": _service.get_channels()}


@router.get("/decimation")
async def decimation_status():
    """Decimation status for all channels."""
    return _service.get_decimation_status()


@router.get("/spectrograms/{channel}")
async def spectrogram_dates(channel: str):
    """List available spectrogram dates for a channel."""
    dates = _service.get_spectrogram_dates(channel)
    return {"channel": channel, "dates": dates}


@router.get("/spectrograms/{channel}/{date_str}")
async def get_spectrogram(channel: str, date_str: str):
    """Serve a spectrogram PNG image."""
    path = _service.get_spectrogram_path(channel, date_str)
    if path is None:
        raise HTTPException(status_code=404, detail=f"No spectrogram for {channel}/{date_str}")
    return FileResponse(path, media_type="image/png")


@router.get("/uploads")
async def upload_history():
    """Upload queue and history."""
    history = _service.get_upload_history()
    dates = _service.get_upload_dates()
    return {
        "packaged_dates": dates,
        "history": history,
    }
