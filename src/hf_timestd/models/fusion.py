from enum import Enum
from typing import Optional, List
from pydantic import BaseModel, Field

class FusionQualityGrade(str, Enum):
    A = "A"
    B = "B"
    C = "C"
    D = "D"

class FusionQualityFlag(str, Enum):
    GOOD = "GOOD"
    MARGINAL = "MARGINAL"
    BAD = "BAD"
    MISSING = "MISSING"

class FusionConsistencyFlag(str, Enum):
    OK = "OK"
    INTRA_ANOMALY = "INTRA_ANOMALY"
    INTER_ANOMALY = "INTER_ANOMALY"
    DISCRIMINATION_SUSPECT = "DISCRIMINATION_SUSPECT"

class FusionKalmanState(str, Enum):
    ACQUIRING = "ACQUIRING"
    LOCKED = "LOCKED"
    REACQUIRING = "REACQUIRING"

class ReferenceStation(str, Enum):
    WWV = "WWV"
    WWVH = "WWVH"
    CHU = "CHU"
    BPM = "BPM"

class L3FusionTiming(BaseModel):
    """
    Multi-broadcast fusion timing estimate.
    Corresponds to l3_fusion_timing_v1.json.
    """
    # Core Timing
    timestamp_utc: str = Field(..., description="Measurement timestamp in UTC (ISO 8601)")
    minute_boundary: int = Field(..., description="Unix epoch timestamp of minute boundary")
    d_clock_fused_ms: float = Field(..., description="Fused D_clock estimate (calibrated, weighted mean)")
    d_clock_raw_ms: float = Field(..., description="Raw D_clock estimate (unweighted mean)")
    
    # Uncertainty
    uncertainty_ms: float = Field(..., description="Combined uncertainty (RSS)")
    statistical_uncertainty_ms: float = Field(..., description="Statistical uncertainty")
    systematic_uncertainty_ms: float = Field(..., description="Systematic uncertainty")
    propagation_uncertainty_ms: float = Field(..., description="Propagation uncertainty")
    
    # Composition
    n_broadcasts: int = Field(..., description="Number of broadcast measurements")
    n_stations: int = Field(..., description="Number of unique stations")
    stations_used: str = Field(..., description="Comma-separated list of stations")
    
    # Station Statistics
    wwv_mean_ms: Optional[float] = None
    wwvh_mean_ms: Optional[float] = None
    chu_mean_ms: Optional[float] = None
    bpm_mean_ms: Optional[float] = None
    
    wwv_count: int
    wwvh_count: int
    chu_count: int
    bpm_count: int
    
    wwv_intra_std_ms: Optional[float] = None
    wwvh_intra_std_ms: Optional[float] = None
    chu_intra_std_ms: Optional[float] = None
    bpm_intra_std_ms: Optional[float] = None
    
    inter_station_spread_ms: Optional[float] = None
    consistency_flag: FusionConsistencyFlag
    
    # Global Solve
    global_solve_verified: bool
    global_solve_consistency_ms: Optional[float] = None
    global_solve_n_obs: int
    
    # Metadata & Quality
    calibration_applied: bool
    reference_station: ReferenceStation
    outliers_rejected: int
    quality_grade: FusionQualityGrade
    kalman_state: FusionKalmanState
    quality_flag: FusionQualityFlag
    processing_version: str

    class Config:
        use_enum_values = True
