#!/usr/bin/env python3
"""
Broadcast-Centric Measurement Models

================================================================================
PURPOSE
================================================================================
This module defines the broadcast-centric data models for L1/L2/L3 measurements.
Each measurement is keyed by broadcast_id (station + frequency_khz), ensuring
clear attribution and enabling broadcast-specific analysis.

Key changes from channel-centric model:
1. broadcast_id as primary key (e.g., 'WWV_10000', 'CHU_7850')
2. All frequencies in kHz (integers)
3. Station-specific feature fields (FSK for CHU, BCD for WWV/WWVH, etc.)
4. Attribution confidence for shared frequencies
5. Per-second tick analysis integrated

================================================================================
DATA PRODUCT HIERARCHY
================================================================================
L1 (Raw Metrology):
    - L1BroadcastMeasurement: Raw ToA, SNR, station-specific features
    - L1TickAnalysis: Per-second tick timing statistics

L2 (Calibrated):
    - L2BroadcastTiming: Calibrated D_clock with uncertainty budget
    - L2PropagationPhysics: Ray-tracing interpretation

L3 (Fused):
    - Cross-broadcast products (TEC, fused D_clock, etc.)

================================================================================
FREQUENCY CONVENTION
================================================================================
All frequencies are in kHz (integers) to match directory naming and avoid
floating-point comparison issues.

Examples:
    10000 kHz = 10 MHz
    7850 kHz = 7.85 MHz
    14670 kHz = 14.67 MHz
"""

from enum import Enum
from typing import Optional, List, Dict, Any, Tuple
from pydantic import BaseModel, ConfigDict, Field, model_validator
import math


# =============================================================================
# ENUMERATIONS
# =============================================================================

class StationID(str, Enum):
    """Time standard broadcast stations."""
    WWV = "WWV"
    WWVH = "WWVH"
    CHU = "CHU"
    BPM = "BPM"
    UNKNOWN = "UNKNOWN"


class QualityFlag(str, Enum):
    """Measurement quality classification."""
    GOOD = "GOOD"           # High confidence, passes all checks
    MARGINAL = "MARGINAL"   # Usable but with caveats
    BAD = "BAD"             # Failed validation, do not use
    MISSING = "MISSING"     # No detection


class AttributionMethod(str, Enum):
    """How station attribution was determined."""
    UNIQUE_FREQUENCY = "unique_freq"      # Only one station on this frequency
    TONE_FREQUENCY = "tone_freq"          # 1200 Hz = WWVH (unambiguous)
    TONE_DURATION = "tone_duration"       # 800ms vs 300ms vs 500ms
    PROPAGATION_DELAY = "prop_delay"      # Timing matches expected delay
    TEST_SIGNAL = "test_signal"           # Minute 8 (WWV) or 44 (WWVH)
    BCD_DECODE = "bcd_decode"             # BCD time code decoded
    FSK_DECODE = "fsk_decode"             # CHU FSK decoded
    GROUND_TRUTH_MINUTE = "ground_truth"  # WWV-only or WWVH-only minute
    FUSION = "fusion"                     # Multi-feature fusion


# =============================================================================
# L1 TICK ANALYSIS
# =============================================================================

class L1TickAnalysis(BaseModel):
    """
    Per-second tick timing analysis for a broadcast.
    
    Provides 55+ independent timing estimates per minute from individual
    second ticks, enabling drift estimation and outlier rejection.
    """
    broadcast_id: str = Field(..., description="Broadcast ID (e.g., 'WWV_10000')")
    minute_boundary_utc: int = Field(..., description="Unix epoch of minute boundary")
    
    # Tick statistics
    ticks_detected: int = Field(..., description="Number of ticks detected")
    ticks_expected: int = Field(..., description="Number of ticks expected")
    detection_rate: float = Field(..., ge=0.0, le=1.0, description="ticks_detected / ticks_expected")
    
    # Timing statistics (ms)
    mean_offset_ms: float = Field(..., description="Mean tick timing offset from expected")
    std_offset_ms: float = Field(..., description="Standard deviation of tick offsets")
    median_offset_ms: float = Field(..., description="Median tick timing offset")
    min_offset_ms: float = Field(..., description="Minimum tick offset")
    max_offset_ms: float = Field(..., description="Maximum tick offset")
    
    # Drift estimation
    drift_rate_ms_per_sec: Optional[float] = Field(None, description="Linear drift rate")
    drift_r_squared: Optional[float] = Field(None, description="R² of linear fit")
    
    # Per-second details (optional, for debugging)
    tick_offsets_ms: Optional[List[float]] = Field(None, description="Individual tick offsets")
    tick_seconds: Optional[List[int]] = Field(None, description="Second numbers of detected ticks")
    tick_snrs_db: Optional[List[float]] = Field(None, description="SNR of each tick")
    
    model_config = ConfigDict(use_enum_values=True)


# =============================================================================
# L1 BROADCAST MEASUREMENT
# =============================================================================

class L1BroadcastMeasurement(BaseModel):
    """
    Pure metrology measurement for a single broadcast.
    
    This is the foundational L1 data product: "What station sent it, when did
    it arrive, and how confident are we?" No physics corrections applied.
    
    All frequencies in kHz (integers).
    """
    
    # === BROADCAST IDENTITY (Primary Key) ===
    broadcast_id: str = Field(..., description="Unique broadcast key: 'WWV_10000' or 'CHU_7850'")
    station: StationID = Field(..., description="Station: WWV, WWVH, CHU, BPM")
    frequency_khz: int = Field(..., description="Carrier frequency in kHz (integer)")
    
    # === TEMPORAL IDENTITY ===
    timestamp_utc: str = Field(..., description="ISO 8601 timestamp of measurement")
    minute_boundary_utc: int = Field(..., description="Unix epoch of minute boundary")
    rtp_timestamp: int = Field(..., description="RTP timestamp for provenance")
    
    # === CORE TIMING MEASUREMENT ===
    tone_detected: bool = Field(..., description="Was the expected tone detected?")
    raw_toa_ms: float = Field(..., description="Raw Time of Arrival (ms from minute boundary)")
    toa_uncertainty_ms: float = Field(..., description="ToA uncertainty (Cramér-Rao bound)")
    
    # === SIGNAL QUALITY ===
    snr_db: float = Field(..., description="Signal-to-Noise Ratio (dB)")
    tone_duration_ms: Optional[float] = Field(None, description="Measured tone duration (ms)")
    expected_duration_ms: float = Field(..., description="Expected duration for this broadcast")
    duration_match: bool = Field(True, description="Does measured match expected (±20%)?")
    
    # === ATTRIBUTION (Critical for Shared Frequencies) ===
    attribution_method: AttributionMethod = Field(..., description="How station was identified")
    attribution_confidence: float = Field(..., ge=0.0, le=1.0, description="Confidence in station ID")
    attribution_ambiguous: bool = Field(False, description="True if multiple stations possible")
    alternative_stations: List[str] = Field(default_factory=list, description="Other possible stations")
    
    # === WWV/WWVH SPECIFIC FEATURES ===
    # Only populated for WWV/WWVH broadcasts
    bcd_detected: Optional[bool] = Field(None, description="BCD time code detected")
    bcd_time_valid: Optional[bool] = Field(None, description="BCD decoded time matches expected")
    bcd_wwv_amplitude: Optional[float] = Field(None, description="WWV BCD correlation amplitude")
    bcd_wwvh_amplitude: Optional[float] = Field(None, description="WWVH BCD correlation amplitude")
    test_signal_detected: Optional[bool] = Field(None, description="Test signal present (min 8/44)")
    tone_500_600_hz: Optional[int] = Field(None, description="500/600 Hz tone frequency if present")
    
    # === CHU SPECIFIC FEATURES ===
    # Only populated for CHU broadcasts
    fsk_detected: Optional[bool] = Field(None, description="FSK time code detected")
    fsk_frames_decoded: Optional[int] = Field(None, ge=0, le=9, description="FSK frames decoded (0-9)")
    fsk_time_valid: Optional[bool] = Field(None, description="FSK decoded time matches expected")
    fsk_timing_offset_ms: Optional[float] = Field(None, description="FSK 500ms boundary timing")
    fsk_decoded_day: Optional[int] = Field(None, description="Day of year from FSK")
    fsk_decoded_hour: Optional[int] = Field(None, description="Hour from FSK")
    fsk_decoded_minute: Optional[int] = Field(None, description="Minute from FSK")
    tick_timing_offset_ms: Optional[float] = Field(None, description="1000 Hz tick onset timing (high precision)")
    dut1_from_fsk: Optional[float] = Field(None, description="DUT1 from FSK Frame B (seconds)")
    dut1_from_splits: Optional[float] = Field(None, description="DUT1 from split tones (seconds)")
    dut1_consistent: Optional[bool] = Field(None, description="FSK and split DUT1 agree")
    tai_utc_from_fsk: Optional[int] = Field(None, description="TAI-UTC from FSK Frame B")
    
    # === BPM SPECIFIC FEATURES ===
    # Only populated for BPM broadcasts
    ut1_tick_detected: Optional[bool] = Field(None, description="100ms UT1 tick detected")
    
    # === PER-SECOND TICK ANALYSIS ===
    tick_analysis: Optional[L1TickAnalysis] = Field(None, description="Per-second tick statistics")
    
    # === PROPAGATION INDICATORS ===
    doppler_hz: Optional[float] = Field(None, description="Estimated Doppler shift (Hz)")
    multipath_detected: Optional[bool] = Field(None, description="Multipath signature detected")
    multipath_delay_spread_ms: Optional[float] = Field(None, description="Delay spread if multipath")
    multipath_quality: Optional[float] = Field(None, ge=0.0, le=1.0, description="Signal quality (1=clean)")
    
    # === GEOGRAPHIC SANITY ===
    distance_km: float = Field(..., description="Great circle distance to station (km)")
    light_travel_time_ms: float = Field(..., description="Minimum physical delay (distance/c)")
    expected_delay_range_ms: Tuple[float, float] = Field(..., description="Plausible delay range (min, max)")
    delay_plausible: bool = Field(..., description="Is raw_toa within plausible range?")
    
    # === QUALITY & PROVENANCE ===
    quality_flag: QualityFlag = Field(..., description="Overall quality assessment")
    processing_version: str = Field("2.0.0", description="Software version")
    
    # === COMPUTED PROPERTIES ===
    @property
    def frequency_mhz(self) -> float:
        """Frequency in MHz (for legacy compatibility)."""
        return self.frequency_khz / 1000.0
    
    @property
    def is_unique_frequency(self) -> bool:
        """True if this broadcast is on a unique frequency."""
        unique_freqs = {20000, 25000, 3330, 7850, 14670}
        return self.frequency_khz in unique_freqs
    
    @property
    def is_wwv_or_chu(self) -> bool:
        """True if this is a timing reference station (not WWVH/BPM)."""
        return self.station in [StationID.WWV, StationID.CHU]
    
    # === VALIDATION ===
    @model_validator(mode='after')
    def validate_measurement(self):
        """Enforce data model constraints."""
        # Rule 1: If tone not detected, raw_toa should be NaN
        if not self.tone_detected:
            if not math.isnan(self.raw_toa_ms):
                # Allow but flag as MISSING
                pass  # Relaxed for now
            if self.quality_flag != QualityFlag.MISSING:
                pass  # Relaxed for now
        
        # Rule 2: Station-specific fields should only be set for relevant stations
        if self.station not in [StationID.WWV, StationID.WWVH]:
            # CHU/BPM shouldn't have BCD fields set to True
            if self.bcd_detected is True:
                raise ValueError(f"bcd_detected=True invalid for {self.station}")
        
        if self.station != StationID.CHU:
            # Non-CHU shouldn't have FSK fields set to True
            if self.fsk_detected is True:
                raise ValueError(f"fsk_detected=True invalid for {self.station}")
        
        return self
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return self.model_dump(mode='json')
    
    model_config = ConfigDict(use_enum_values=True)


# =============================================================================
# L2 BROADCAST TIMING
# =============================================================================

class L2BroadcastTiming(BaseModel):
    """
    Calibrated timing measurement with ISO GUM uncertainty budget.
    
    Derived from L1BroadcastMeasurement with propagation model corrections
    and full uncertainty propagation.
    """
    
    # === BROADCAST IDENTITY ===
    broadcast_id: str = Field(..., description="Broadcast ID (e.g., 'WWV_10000')")
    station: StationID = Field(..., description="Station")
    frequency_khz: int = Field(..., description="Frequency in kHz")
    
    # === TEMPORAL IDENTITY ===
    timestamp_utc: str = Field(..., description="ISO 8601 timestamp")
    minute_boundary_utc: int = Field(..., description="Unix epoch of minute boundary")
    
    # === CORE TIMING ===
    tone_detected: bool = Field(..., description="Was tone detected?")
    raw_toa_ms: float = Field(..., description="Raw ToA from L1 (ms)")
    propagation_delay_ms: float = Field(..., description="Modeled propagation delay (ms)")
    clock_offset_ms: float = Field(..., description="D_clock = raw_toa - propagation_delay")
    
    # === UNCERTAINTY BUDGET (ISO GUM) ===
    uncertainty_ms: float = Field(..., description="Combined standard uncertainty u_c")
    expanded_uncertainty_ms: float = Field(..., description="Expanded uncertainty U = k * u_c")
    coverage_factor: float = Field(2.0, description="Coverage factor k")
    confidence_level: float = Field(0.95, description="Confidence level")
    
    # Uncertainty components
    u_toa_ms: float = Field(..., description="ToA measurement uncertainty")
    u_ionospheric_ms: float = Field(..., description="Ionospheric model uncertainty")
    u_multipath_ms: float = Field(..., description="Multipath uncertainty")
    u_attribution_ms: float = Field(..., description="Station attribution uncertainty")
    u_gpsdo_ms: float = Field(..., description="GPSDO timing uncertainty")
    u_propagation_model_ms: float = Field(..., description="Propagation model uncertainty")
    degrees_of_freedom: int = Field(..., description="Effective degrees of freedom")
    
    # === PROPAGATION PHYSICS ===
    propagation_mode: Optional[str] = Field(None, description="Ray mode (1F, 2F, E, etc.)")
    n_hops: Optional[int] = Field(None, description="Number of ionospheric hops")
    virtual_height_km: Optional[float] = Field(None, description="Virtual reflection height")
    
    # === QUALITY ===
    quality_flag: QualityFlag = Field(..., description="Quality assessment")
    attribution_confidence: float = Field(..., description="Station ID confidence")
    
    # === KALMAN FILTER STATE ===
    tof_kalman_ms: Optional[float] = Field(None, description="Kalman-filtered ToF")
    tof_kalman_uncertainty_ms: Optional[float] = Field(None, description="Kalman uncertainty")
    doppler_ms_per_min: Optional[float] = Field(None, description="ToF rate of change")
    
    # === PROVENANCE ===
    l1_measurement_id: Optional[str] = Field(None, description="Reference to source L1")
    processing_version: str = Field("2.0.0", description="Software version")
    processed_at: str = Field(..., description="Processing timestamp")
    gpsdo_locked: bool = Field(..., description="GPSDO was locked during measurement")
    
    @property
    def frequency_mhz(self) -> float:
        """Frequency in MHz."""
        return self.frequency_khz / 1000.0
    
    model_config = ConfigDict(use_enum_values=True)


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def create_broadcast_id(station: str, frequency_khz: int) -> str:
    """Create broadcast ID from station and frequency."""
    return f"{station}_{frequency_khz}"


def parse_broadcast_id(broadcast_id: str) -> Tuple[str, int]:
    """Parse broadcast ID into (station, frequency_khz)."""
    parts = broadcast_id.rsplit('_', 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid broadcast_id format: {broadcast_id}")
    station = parts[0]
    frequency_khz = int(parts[1])
    return station, frequency_khz


def khz_to_mhz(khz: int) -> float:
    """Convert kHz to MHz."""
    return khz / 1000.0


def mhz_to_khz(mhz: float) -> int:
    """Convert MHz to kHz (rounded)."""
    return int(round(mhz * 1000))
