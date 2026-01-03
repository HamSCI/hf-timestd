"""
Station metadata models.
"""

from pydantic import BaseModel, Field
from typing import List, Optional


class ChannelInfo(BaseModel):
    """Channel configuration information."""
    frequency_hz: int = Field(..., description="Frequency in Hz")
    frequency_mhz: float = Field(..., description="Frequency in MHz")
    description: str = Field(..., description="Channel description")
    channel_name: str = Field(..., description="Channel name")


class StationMetadata(BaseModel):
    """Station metadata and configuration."""
    callsign: str = Field(..., description="Station callsign")
    grid_square: str = Field(..., description="Maidenhead grid square")
    station_id: str = Field(..., description="Station ID")
    instrument_id: str = Field(..., description="Instrument ID")
    description: str = Field(..., description="Station description")
    latitude: float = Field(..., description="Latitude in degrees")
    longitude: float = Field(..., description="Longitude in degrees")
    mode: str = Field(..., description="Operating mode (production/test)")
    channels: List[ChannelInfo] = Field(..., description="Configured channels")
    data_root: str = Field(..., description="Data root directory")
