"""
System health models.
"""

from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any


class ChannelStatus(BaseModel):
    """Channel health status."""
    channel_name: str = Field(..., description="Channel name")
    frequency_mhz: float = Field(..., description="Frequency in MHz")
    status: str = Field(..., description="Status (active/inactive/error)")
    last_update: Optional[str] = Field(None, description="Last update timestamp")
    carrier_snr_db: Optional[float] = Field(None, description="Carrier SNR in dB")
    data_quality: Optional[str] = Field(None, description="Data quality flag")
    completeness: Optional[float] = Field(None, description="Data completeness [0-1]")


class ProcessStatus(BaseModel):
    """Process status information."""
    name: str = Field(..., description="Process name")
    status: str = Field(..., description="Status (running/stopped/error)")
    uptime: Optional[str] = Field(None, description="Uptime duration")
    pid: Optional[int] = Field(None, description="Process ID")


class SystemHealth(BaseModel):
    """Overall system health."""
    status: str = Field(..., description="Overall status (healthy/degraded/error)")
    timestamp: str = Field(..., description="Status timestamp (ISO8601)")
    uptime: str = Field(..., description="System uptime")
    channels: List[ChannelStatus] = Field(..., description="Channel status list")
    processes: List[ProcessStatus] = Field(..., description="Process status list")
    disk_usage_percent: Optional[float] = Field(None, description="Disk usage percentage")
    data_completeness: Optional[float] = Field(None, description="Overall data completeness")
    errors: List[str] = Field(default_factory=list, description="Recent errors")
