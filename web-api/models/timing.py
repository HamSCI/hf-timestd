"""
Timing measurement models.
"""

from pydantic import BaseModel, Field
from typing import List, Optional


class FusionResponse(BaseModel):
    """Latest fusion timing estimate with full expert metrics."""
    timestamp: str = Field(..., description="Measurement timestamp (ISO8601)")
    d_clock_ms: float = Field(..., description="D_clock offset in milliseconds")
    d_clock_raw_ms: Optional[float] = Field(None, description="Raw D_clock before Kalman filtering")
    uncertainty_ms: float = Field(..., description="Combined uncertainty in milliseconds")
    statistical_uncertainty_ms: Optional[float] = Field(None, description="Statistical uncertainty (Type A)")
    systematic_uncertainty_ms: Optional[float] = Field(None, description="Systematic uncertainty (Type B)")
    propagation_uncertainty_ms: Optional[float] = Field(None, description="Propagation uncertainty")
    quality_grade: str = Field(..., description="Quality grade (A/B/C/D)")
    quality_flag: Optional[str] = Field(None, description="Quality flag")
    n_broadcasts: int = Field(..., description="Number of broadcasts in fusion")
    n_stations: int = Field(..., description="Number of unique stations")
    stations_used: List[str] = Field(..., description="List of stations contributing")
    inter_station_spread_ms: Optional[float] = Field(None, description="Inter-station spread")
    consistency_flag: Optional[str] = Field(None, description="Consistency flag")
    outliers_rejected: Optional[int] = Field(None, description="Number of outliers rejected")
    kalman_state: Optional[str] = Field(None, description="Kalman filter state")
    reference_station: Optional[str] = Field(None, description="Reference station")
    calibration_applied: Optional[bool] = Field(None, description="Calibration applied flag")
    processing_version: Optional[str] = Field(None, description="Processing version")
    global_solve_verified: Optional[bool] = Field(None, description="Global solve verified")
    global_solve_consistency_ms: Optional[float] = Field(None, description="Global solve consistency")
    global_solve_n_obs: Optional[int] = Field(None, description="Global solve observations")
    wwv_mean_ms: Optional[float] = Field(None, description="WWV mean offset")
    wwvh_mean_ms: Optional[float] = Field(None, description="WWVH mean offset")
    chu_mean_ms: Optional[float] = Field(None, description="CHU mean offset")
    bpm_mean_ms: Optional[float] = Field(None, description="BPM mean offset")
    wwv_count: Optional[int] = Field(None, description="WWV broadcast count")
    wwvh_count: Optional[int] = Field(None, description="WWVH broadcast count")
    chu_count: Optional[int] = Field(None, description="CHU broadcast count")
    bpm_count: Optional[int] = Field(None, description="BPM broadcast count")
    wwv_intra_std_ms: Optional[float] = Field(None, description="WWV intra-station std")
    wwvh_intra_std_ms: Optional[float] = Field(None, description="WWVH intra-station std")
    chu_intra_std_ms: Optional[float] = Field(None, description="CHU intra-station std")


class FusionHistoryResponse(BaseModel):
    """Fusion timing history."""
    timestamps: List[str] = Field(..., description="Measurement timestamps (ISO8601)")
    d_clock_ms: List[float] = Field(..., description="D_clock offsets in milliseconds")
    uncertainty_ms: List[float] = Field(..., description="Uncertainties in milliseconds")
    quality_grade: List[str] = Field(..., description="Quality grades")
    n_broadcasts: List[int] = Field(..., description="Broadcast counts")
    count: int = Field(..., description="Number of measurements returned")


class TimingMeasurement(BaseModel):
    """Single timing measurement (L2)."""
    timestamp: str = Field(..., description="Measurement timestamp (ISO8601)")
    station: str = Field(..., description="Station (WWV/WWVH/CHU/BPM)")
    frequency_mhz: float = Field(..., description="Frequency in MHz")
    clock_offset_ms: float = Field(..., description="Clock offset in milliseconds")
    uncertainty_ms: float = Field(..., description="Combined uncertainty in milliseconds")
    quality_grade: str = Field(..., description="Quality grade (A/B/C/D)")
    quality_flag: str = Field(..., description="Quality flag")
    confidence: float = Field(..., description="Measurement confidence [0-1]")
    snr_db: Optional[float] = Field(None, description="Signal-to-noise ratio in dB")
    propagation_mode: Optional[str] = Field(None, description="Propagation mode")
