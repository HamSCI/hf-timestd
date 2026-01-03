"""
Station metadata API endpoints.
"""

from fastapi import APIRouter, HTTPException

from models.station import StationMetadata
from config import config

router = APIRouter(prefix="/station", tags=["station"])


@router.get("/metadata", response_model=StationMetadata)
async def get_station_metadata():
    """
    Get station metadata and configuration.
    
    Returns:
    - Station identification (callsign, grid square, IDs)
    - Geographic location
    - Operating mode
    - Configured channels
    - Data paths
    """
    try:
        metadata = config.station_metadata
        metadata['channels'] = config.channels
        metadata['data_root'] = str(config.data_root)
        
        return metadata
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
