from enum import Enum
from typing import Optional, List
from pydantic import BaseModel, Field

class QualityGrade(str, Enum):
    A = "A"
    B = "B"
    C = "C"
    D = "D"

class QualityFlag(str, Enum):
    GOOD = "GOOD"
    MARGINAL = "MARGINAL"
    BAD = "BAD"
    MISSING = "MISSING"

class StationID(str, Enum):
    WWV = "WWV"
    WWVH = "WWVH"
    CHU = "CHU"
    BPM = "BPM"

class DiscriminationMethod(str, Enum):
    TONE = "TONE"
    BCD = "BCD"
    ID_440HZ = "440HZ"
    FUSION = "FUSION"

class L2TimingMeasurement(BaseModel):
    """
    Station-assigned timing measurements with ISO GUM-compliant uncertainty budget.
    Corresponds to l2_timing_measurements_v1.json.
    """
    # Core Identification
    timestamp_utc: str = Field(..., description="Measurement timestamp in UTC (ISO 8601)")
    minute_boundary_utc: int = Field(..., description="Unix epoch timestamp of minute boundary")
    rtp_timestamp: int = Field(..., description="RTP timestamp from raw_buffer")
    station: StationID = Field(..., description="Broadcast station assignment")
    frequency_mhz: float = Field(..., description="Carrier frequency in MHz")
    
    # Discrimination
    discrimination_method: DiscriminationMethod = Field(..., description="Station discrimination method used")
    discrimination_confidence: float = Field(..., ge=0.0, le=1.0, description="Station ID confidence score")
    
    # Timing Logic
    clock_offset_ms: float = Field(..., description="D_clock: observed - expected arrival time")
    raw_arrival_time_ms: Optional[float] = Field(None, description="Raw uncalibrated arrival time relative to minute boundary")
    
    # Uncertainty (ISO GUM)
    uncertainty_ms: float = Field(..., description="Combined standard uncertainty u_c")
    expanded_uncertainty_ms: float = Field(..., description="Expanded uncertainty U = k * u_c")
    coverage_factor: float = Field(2.0, description="Coverage factor k")
    confidence_level: float = Field(0.95, description="Confidence level")
    
    # Uncertainty Components
    u_rtp_timestamp_ms: float
    u_ionospheric_ms: float
    u_multipath_ms: float
    u_discrimination_ms: float
    u_gpsdo_ms: float
    u_propagation_model_ms: float
    degrees_of_freedom: int
    
    # Quality & Confidence
    quality_grade: QualityGrade
    confidence: float = Field(..., ge=0.0, le=1.0, description="Overall measurement confidence")
    quality_flag: QualityFlag
    
    # Propagation Physics (Optional)
    propagation_delay_ms: Optional[float] = Field(None, description="Estimated propagation delay")
    propagation_mode: Optional[str] = Field(None, description="Ray-tracing classification (1F, 2F, etc.)")
    n_hops: Optional[int] = None
    delay_spread_ms: Optional[float] = None
    fss_db: Optional[float] = None
    
    # Signal Metrics (Optional)
    snr_db: Optional[float] = None
    doppler_hz: Optional[float] = None
    
    # Verification Flags
    utc_verified: Optional[bool] = None
    multi_station_verified: Optional[bool] = None
    
    # Metadata
    traceability_chain: str
    processing_version: str
    processed_at: str
    calibration_date: str
    gpsdo_locked: bool

    class Config:
        use_enum_values = True
