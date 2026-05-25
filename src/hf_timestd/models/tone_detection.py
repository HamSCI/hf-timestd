from enum import Enum
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field

class ToneQualityFlag(str, Enum):
    GOOD = "GOOD"
    MARGINAL = "MARGINAL"
    BAD = "BAD"
    MISSING = "MISSING"

class AnchorStation(str, Enum):
    WWV = "WWV"
    WWVH = "WWVH"
    CHU = "CHU"
    BPM = "BPM"
    UNKNOWN = "UNKNOWN"
    NONE = ""

class L1ToneDetection(BaseModel):
    """
    Station identification tone timing measurements.
    Corresponds to l1_tone_detections_v1.json.
    """
    timestamp_utc: str = Field(..., description="Measurement timestamp in UTC (ISO 8601)")
    minute_boundary: int = Field(..., description="Unix epoch timestamp of minute boundary")
    
    # WWV
    wwv_detected: bool
    wwv_snr_db: Optional[float] = Field(None, ge=-20, le=60)
    wwv_timing_ms: Optional[float] = Field(None, ge=-1000, le=1000)
    
    # WWVH
    wwvh_detected: bool
    wwvh_snr_db: Optional[float] = Field(None, ge=-20, le=60)
    wwvh_timing_ms: Optional[float] = Field(None, ge=-1000, le=1000)
    
    # CHU
    chu_detected: bool
    chu_snr_db: Optional[float] = Field(None, ge=-20, le=60)
    chu_timing_ms: Optional[float] = Field(None, ge=-1000, le=1000)
    
    # BPM
    bpm_detected: bool
    bpm_snr_db: Optional[float] = Field(None, ge=-20, le=60)
    bpm_timing_ms: Optional[float] = Field(None, ge=-1000, le=1000)
    
    # Anchor
    anchor_station: Optional[AnchorStation] = None
    anchor_confidence: Optional[float] = Field(None, ge=0.0, le=1.0)
    
    # Metadata
    quality_flag: ToneQualityFlag
    processing_version: str

    model_config = ConfigDict(use_enum_values=True)
