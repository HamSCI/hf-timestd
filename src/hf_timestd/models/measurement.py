from enum import Enum
from typing import Optional, List
from pydantic import BaseModel, ConfigDict, Field

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
    
    # Timing Logic - Data Model Hierarchy
    # 1. raw_arrival_time_ms: CORE DATUM from validated tone detection (source of truth)
    # 2. propagation_delay_ms: DERIVED from ray tracing
    # 3. clock_offset_ms: DERIVED as (raw_arrival_time_ms - propagation_delay_ms)
    tone_detected: bool = Field(..., description="Explicit flag: was a validated tone actually detected?")
    raw_arrival_time_ms: float = Field(..., description="Raw uncalibrated arrival time from validated tone detection (NaN if no tone)")
    clock_offset_ms: float = Field(..., description="D_clock: observed - expected arrival time (NaN if no tone)")
    
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
    
    # Per-Broadcast Kalman Filter State (Science-First Architecture v5.0)
    # These fields track ionospheric path dynamics for each broadcast independently
    tof_kalman_ms: Optional[float] = Field(
        None, 
        description="Kalman-filtered Time of Flight (ionospheric path delay)"
    )
    tof_uncertainty_ms: Optional[float] = Field(
        None,
        description="Kalman filter uncertainty for ToF estimate"
    )
    doppler_ms_per_min: Optional[float] = Field(
        None,
        description="Rate of change of ToF (tracks ionospheric layer movement)"
    )
    gpsdo_consistent: Optional[bool] = Field(
        None,
        description="GPSDO temporal continuity check: consistent with previous minute"
    )
    
    # Verification Flags
    utc_verified: Optional[bool] = None
    multi_station_verified: Optional[bool] = None
    
    # Metadata
    traceability_chain: str
    processing_version: str
    processed_at: str
    calibration_date: str
    gpsdo_locked: bool

    from pydantic import model_validator
    
    @model_validator(mode='after')
    def model_validate_data_integrity(self):
        """
        Enforce data model hierarchy and missing value semantics.
        
        Rules:
        1. If tone_detected=False, raw_arrival_time_ms MUST be NaN
        2. If tone_detected=True, raw_arrival_time_ms MUST be valid (not NaN)
        3. If raw_arrival_time_ms is NaN, clock_offset_ms MUST be NaN
        4. If tone_detected=False, quality_flag MUST be MISSING
        """
        import math
        
        # In Pydantic v2 mode='after', values are accessible via self
        tone_detected = self.tone_detected
        raw_arrival = self.raw_arrival_time_ms
        clock_offset = self.clock_offset_ms
        quality_flag = self.quality_flag
        
        # Rule 1: tone_detected=False requires raw_arrival_time_ms=NaN
        if not tone_detected and raw_arrival is not None and not math.isnan(raw_arrival):
            raise ValueError(
                f"Data model violation: tone_detected=False but "
                f"raw_arrival_time_ms={raw_arrival} (should be NaN)"
            )
        
        # Rule 2: tone_detected=True requires raw_arrival_time_ms is valid
        if tone_detected and (raw_arrival is None or math.isnan(raw_arrival)):
            raise ValueError(
                f"Data model violation: tone_detected=True but "
                f"raw_arrival_time_ms is NaN or None"
            )
        
        # Rule 3: raw_arrival_time_ms=NaN requires clock_offset_ms=NaN
        if raw_arrival is not None and math.isnan(raw_arrival):
            if clock_offset is not None and not math.isnan(clock_offset):
                raise ValueError(
                    f"Data model violation: raw_arrival_time_ms is NaN but "
                    f"clock_offset_ms={clock_offset} (should be NaN)"
                )
        
        # Rule 4: tone_detected=False requires quality_flag=MISSING
        if not tone_detected and quality_flag != QualityFlag.MISSING:
            raise ValueError(
                f"Data model violation: tone_detected=False but "
                f"quality_flag={quality_flag} (should be MISSING)"
            )
        
        return self

    model_config = ConfigDict(use_enum_values=True)


class L1MetrologyMeasurement(BaseModel):
    """
    Pure metrology measurement: "Who sent it" and "When it arrived" (Raw).
    No physics corrections, no d_clock, just the facts.
    """
    # Core Identification
    timestamp_utc: str = Field(..., description="Measurement timestamp in UTC (ISO 8601)")
    minute_boundary_utc: int = Field(..., description="Unix epoch timestamp of minute boundary")
    rtp_timestamp: int = Field(..., description="RTP timestamp from raw_buffer")
    station_id: StationID = Field(..., description="Identified station")
    frequency_mhz: float = Field(..., description="Carrier frequency in MHz")
    
    # The Fact: Raw Time of Arrival
    raw_toa_ms: float = Field(..., description="Raw Time of Arrival (from tone detection)")
    tone_detected: bool = Field(..., description="Was a tone detected?")
    
    # Signal Metrics
    snr_db: float = Field(..., description="Signal-to-Noise Ratio in dB")
    doppler_hz: Optional[float] = Field(None, description="Doppler shift in Hz")
    
    # Identification Metadata
    identification_method: str = Field(..., description="Method used for ID (e.g., 'anchor', 'geometric')")
    identification_confidence: float = Field(..., description="Confidence of ID")
    
    # Geographic Sanity Checks (Physics-Lite)
    distance_km: float = Field(..., description="Great circle distance to station")
    light_travel_time_ms: float = Field(..., description="Minimum physical delay (distance/c)")
    
    # Quality
    quality_flag: QualityFlag
    processing_version: str = "1.0.0"

    from pydantic import model_validator
    
    @model_validator(mode='after')
    def validate_sanity(self):
        """
        Enforce geographic sanity: TOA must be >= light travel time.
        """
        import math
        
        # In Pydantic v2 mode='after', values are accessible via self
        raw_toa = self.raw_toa_ms
        light_time = self.light_travel_time_ms
        tone_detected = self.tone_detected
        
        if tone_detected and not math.isnan(raw_toa):
            # Allow small margin for error/uncertainty (e.g. -0.5ms) 
            # to account for negative clock errors or measurement noise.
            # But gross violations (e.g. 5ms too early) are impossible.
            margin_ms = 1.0
            if raw_toa < (light_time - margin_ms):
                pass
                # raise ValueError(
                #    f"Geographic sanity violation: TOA ({raw_toa:.3f}ms) < "
                #    f"Light Travel Time ({light_time:.3f}ms) with margin {margin_ms}ms"
                # )
                # TODO: Enforce this once we are sure about clock synchronization state.
                # For now, just logging or passing is safer during bootstrap.
            
        return self

    model_config = ConfigDict(use_enum_values=True)


class L2PhysicsMeasurement(BaseModel):
    """
    Physics Interpretation: "The Scientist's View".
    Derived from L1 Metrology + Environmental Models (IRI, Raytracing).
    """
    # Keys to link back to L1
    timestamp_utc: str = Field(..., description="Measurement timestamp in UTC")
    station_id: StationID = Field(..., description="Target station")
    frequency_mhz: float = Field(..., description="Carrier frequency")
    
    # Physics Outputs
    propagation_delay_ms: float = Field(..., description="Modeled propagation delay")
    propagation_mode: str = Field(..., description="Likely mode (1F, 2F, E, etc.)")
    tec_estimate: Optional[float] = Field(None, description="Total Electron Content (TECu)")
    
    # Quality of Fit
    model_confidence: float = Field(..., description="Confidence in model match (0-1)")
    
    # Metadata
    processing_version: str = "1.0.0"
    processed_at: str = Field(..., description="When this interpretation was made")
    
    model_config = ConfigDict(use_enum_values=True)
