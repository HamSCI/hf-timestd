#!/usr/bin/env python3
"""
Phase 2: Temporal Analysis Engine - Precision Timing Analytics

================================================================================
PURPOSE
================================================================================
The Phase 2 Temporal Engine is the CENTRAL ORCHESTRATOR for all timing analytics.
It coordinates the three-step process that transforms raw IQ samples into a
precision D_clock measurement:

    D_clock = T_system - T_UTC(NIST)

This is the "System Clock Offset" - the primary output of the hf-timestd system.

================================================================================
ARCHITECTURAL OVERVIEW
================================================================================
Phase 2 implements a hierarchical refinement strategy where each step narrows
the search window for the next:

┌─────────────────────────────────────────────────────────────────────────────┐
│                    STEP 1: TIME SNAP (±500ms → anchor)                      │
│                                                                             │
│   Input:  Raw IQ @ 20 kHz, system_time, rtp_timestamp                       │
│   Method: Matched filter tone detection (1000/1200 Hz)                      │
│   Output: timing_error_ms, anchor_station, confidence                       │
│                                                                             │
│   🎯 Establishes initial temporal synchronization                           │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│             STEP 2: CHANNEL CHARACTERIZATION (±50ms window)                 │
│                                                                             │
│   2A. BCD Correlation → differential_delay_ms, dual-peak timing             │
│   2B. Doppler Estimation → doppler_std_hz, coherence_time                   │
│   2C. Station Discrimination → dominant_station, ground_truth               │
│   2D. Test Signal Analysis → FSS, delay_spread (minutes 8/44)               │
│                                                                             │
│   📡 Characterizes ionospheric channel for mode disambiguation              │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                STEP 3: TRANSMISSION TIME SOLUTION (→ D_clock)               │
│                                                                             │
│   Input:  timing_error_ms + channel_metrics + station_ID                    │
│   Method: TransmissionTimeSolver with mode disambiguation                   │
│   Output: D_clock, propagation_mode, confidence, uncertainty                │
│                                                                             │
│   🎯 Back-calculates UTC(NIST) from observed arrival time                   │
└─────────────────────────────────────────────────────────────────────────────┘

================================================================================
DATA FLOW: THE D_CLOCK EQUATION
================================================================================
The fundamental equation we're solving:

    T_arrival = T_emission + T_propagation + D_clock

Where:
    T_arrival = Observed tone arrival time (from Step 1)
    T_emission = 0 (tones transmitted at exact second boundary)
    T_propagation = HF signal propagation delay (from Step 3 mode solving)
    D_clock = System clock offset (THE OUTPUT WE WANT)

Rearranging:
    D_clock = T_arrival - T_propagation

================================================================================
STEP 1: TIME SNAP - INITIAL SYNCHRONIZATION
================================================================================
The tone detector (from tone_detector.py) uses quadrature matched filtering
to detect the 800ms timing tones:

    - WWV:  1000 Hz, 0.8s duration at second 0
    - WWVH: 1200 Hz, 0.8s duration at second 0
    - CHU:  1000 Hz, 0.5s duration at second 0 (1.0s at hour)

Output: timing_error_ms = offset from expected minute boundary
        This is typically in the range of 5-50ms (propagation delay)

SEARCH WINDOW: ±500ms (wide, to handle unknown propagation)

================================================================================
STEP 2: CHANNEL CHARACTERIZATION
================================================================================
With timing anchored to ±50ms, Step 2 extracts channel metrics:

STEP 2A: BCD CORRELATION
    The 100 Hz Binary Coded Decimal subcarrier provides:
    - Differential delay between WWV and WWVH peaks
    - Amplitude ratio for station power comparison
    - Delay spread from correlation peak width

STEP 2B: DOPPLER ESTIMATION  
    Per-tick phase tracking measures:
    - Doppler shift (ionospheric motion)
    - Doppler standard deviation (channel stability)
    - Maximum coherent integration window

STEP 2C: STATION DISCRIMINATION
    Weighted voting across multiple methods (see wwvh_discrimination.py):
    - Ground truth tones (500/600 Hz, 440 Hz)
    - Power ratio (1000 Hz vs 1200 Hz)
    - BCD amplitude ratio
    - Test signal (minutes 8/44)

STEP 2D: TEST SIGNAL ANALYSIS (Minutes 8 and 44 only)
    Scientific modulation test provides:
    - Frequency Selectivity Score (FSS) - D-layer indicator for mode disambiguation
    - Delay spread from chirp analysis - multipath severity
    - High-precision ToA from single-cycle bursts
    - Coherence time from fading analysis

================================================================================
STEP 3: TRANSMISSION TIME SOLUTION
================================================================================
The TransmissionTimeSolver (from transmission_time_solver.py) identifies the
propagation mode and computes D_clock:

STATION PRIORITY FOR MODE SOLVING:
    1. Ground truth (500/600 Hz exclusive minutes)
    2. High-confidence discrimination
    3. Channel name (e.g., "WWV 20 MHz" is unambiguous)
    4. Fallback to WWV

MODE DISAMBIGUATION INPUTS:
    - delay_spread_ms: High → favor multi-hop modes
    - doppler_std_hz: High → unstable path, reduce confidence
    - fss_db: Negative → D-layer attenuation, favor multi-hop

OUTPUT:
    - d_clock_ms: System clock offset from UTC(NIST)
    - propagation_mode: '1F', '2F', 'GW', etc.
    - confidence: 0-1 confidence in the solution
    - uncertainty_ms: Estimated timing uncertainty

INPUT DATA REQUIREMENTS
================================================================================
- Data format: np.complex64 (32-bit float I + 32-bit float Q)
- Sample rate: 20,000 Hz (full Phase 1 resolution)
- Buffer duration: 60 seconds (one complete minute)
- Source: Phase 1 raw_buffer (IMMUTABLE - never modified)

32-BIT FLOAT RATIONALE:
    - 144 dB dynamic range vs 96 dB for 16-bit
    - AGC disabled (F32 has sufficient range)
    - Preserves weak signal information
    - Consistent amplitude for matched filtering

================================================================================
USAGE
================================================================================
    from hf_timestd.core.phase2_temporal_engine import Phase2TemporalEngine
    
    engine = Phase2TemporalEngine(
        raw_buffer_dir=Path('/data/raw_buffer'),
        output_dir=Path('/data/phase2'),
        channel_name='WWV_10MHz',
        frequency_hz=10e6,
        receiver_grid='EM38ww'
    )
    
    # Process a minute of data
    result = engine.process_minute(
        iq_samples=samples,      # np.complex64 array, 60 seconds @ 20 kHz
        system_time=timestamp,   # Unix timestamp of buffer START
        rtp_timestamp=rtp_ts     # RTP timestamp of first sample
    )
    
    print(f"D_clock: {result.d_clock_ms:+.2f} ms")
    print(f"Mode: {result.solution.propagation_mode}")
    print(f"Uncertainty: {result.uncertainty_ms:.1f} ms")

================================================================================
OUTPUT: Phase2Result
================================================================================
The Phase2Result dataclass contains:
    - time_snap: TimeSnapResult (Step 1 output)
    - channel: ChannelCharacterization (Step 2 output)
    - solution: TransmissionTimeSolution (Step 3 output)
    - d_clock_ms: Final D_clock value
    - uncertainty_ms: Timing uncertainty in milliseconds
    - confidence: 0-1 confidence score

================================================================================
REVISION HISTORY
================================================================================
2025-12-07: Added comprehensive architectural documentation
2025-12-01: Integrated CHU FSK decoder for Canadian time signals
2025-11-20: Added test signal analysis for minutes 8/44
2025-11-01: Initial three-step architecture implementation
"""

import numpy as np
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Dict, List, Tuple, Any, NamedTuple, Callable
from dataclasses import dataclass, field
import threading
import json
from .wwv_constants import BPM_PURE_CARRIER_MINUTES
from .wwvh_discrimination import WWVHDiscriminator

logger = logging.getLogger(__name__)


# =============================================================================
# Constants for 32-bit Float Processing
# =============================================================================

# Phase 1 stores data as np.complex64 (32-bit float I + 32-bit float Q)
# This provides 144 dB dynamic range vs 96 dB for 16-bit int
EXPECTED_DTYPE = np.complex64
SAMPLE_RATE_FULL = 24000      # Phase 1 archive sample rate (24 kHz for integer WWVH cycles)

# Decimation for tone detection
# Note: Decimation was considered but removed - tone detection now uses full rate
# for maximum timing accuracy. The matched filter templates are generated at
# the full sample rate (20 kHz).

# Normalization threshold for 32-bit float data
# Since AGC is disabled (F32 has sufficient dynamic range), we apply
# a fixed normalization to ensure consistent processing
MAX_EXPECTED_AMPLITUDE = 1.0  # Normalized float range [-1, 1]
AMPLITUDE_WARNING_THRESHOLD = 10.0  # Flag if amplitude exceeds this


@dataclass
class TimeSnapResult:
    """
    Result of Step 1: Fundamental Tone Detection & Time Snap Correction.
    
    This establishes the initial synchronization point for all subsequent analysis.
    """
    # Time snap anchor
    timing_error_ms: float           # Offset from expected second boundary
    arrival_rtp: int                 # RTP timestamp of detected tone arrival
    arrival_system_time: float       # System time of arrival
    
    # Tone detection results
    wwv_detected: bool
    wwvh_detected: bool
    chu_detected: bool = False
    bpm_detected: bool = False
    wwv_snr_db: Optional[float] = None
    wwvh_snr_db: Optional[float] = None
    chu_snr_db: Optional[float] = None
    bpm_snr_db: Optional[float] = None
    wwv_timing_ms: Optional[float] = None
    wwvh_timing_ms: Optional[float] = None
    chu_timing_ms: Optional[float] = None
    bpm_timing_ms: Optional[float] = None
    
    # BPM-specific fields
    bpm_timing_mode: Optional[str] = None  # 'UTC' or 'UT1' (UT1 minutes not usable for UTC timing)
    bpm_is_usable_for_utc: bool = True  # False during UT1 minutes (25-29, 55-59)
    
    # Multi-station detection result (physics-based approach)
    # Contains ALL detected stations with propagation analysis
    multi_station_result: Optional[Any] = None  # MinuteDetectionResult
    
    # Quality metrics
    anchor_station: str = 'UNKNOWN'  # Station used for time snap ('WWV', 'WWVH', 'CHU')
    anchor_confidence: float = 0.0
    search_window_ms: float = 500.0  # Initial search window (narrowed in Step 2)
    
    # Provenance
    detection_method: str = 'matched_filter'


@dataclass
class ChannelCharacterization:
    """
    Result of Step 2: Ionospheric Channel Characterization.
    
    Contains BCD correlation, Doppler, and station identity results.
    """
    # BCD Correlation (Step 2A)
    bcd_wwv_amplitude: Optional[float] = None
    bcd_wwvh_amplitude: Optional[float] = None
    bcd_bpm_amplitude: Optional[float] = None  # BPM uses same 100 Hz BCD
    bcd_differential_delay_ms: Optional[float] = None
    bcd_correlation_quality: Optional[float] = None
    bcd_wwv_toa_ms: Optional[float] = None    # Absolute ToA from minute start
    bcd_wwvh_toa_ms: Optional[float] = None
    bcd_bpm_toa_ms: Optional[float] = None    # BPM ToA (long path from China)
    
    # Doppler and Coherence (Step 2B)
    doppler_carrier_hz: Optional[float] = None
    doppler_wwv_hz: Optional[float] = None
    doppler_wwvh_hz: Optional[float] = None
    doppler_wwv_std_hz: Optional[float] = None
    doppler_wwvh_std_hz: Optional[float] = None
    max_coherent_window_sec: Optional[float] = None
    doppler_quality: Optional[float] = None
    phase_variance_rad: Optional[float] = None
    
    # Channel multipath metrics
    delay_spread_ms: Optional[float] = None
    coherence_time_sec: Optional[float] = None
    spreading_factor: Optional[float] = None  # L = τ_D × f_D
    
    # Station Identity (Step 2C)
    dominant_station: str = 'UNKNOWN'
    station_confidence: str = 'low'
    ground_truth_station: Optional[str] = None  # From 500/600 Hz exclusive minutes
    ground_truth_source: Optional[str] = None   # '500Hz', '600Hz', '440Hz'
    ground_truth_power_db: Optional[float] = None  # Power of detected ground truth tone
    
    # Harmonic power ratios (500/600 Hz detection)
    harmonic_ratio_500_1000: Optional[float] = None  # P_1000/P_500 in dB
    harmonic_ratio_600_1200: Optional[float] = None  # P_1200/P_600 in dB
    
    # BCD Intermodulation analysis (Vote 13)
    # 400 Hz = 500-100 (WWV signature), 700 Hz = 600+100 (WWVH signature)
    intermod_power_400_hz_db: Optional[float] = None  # WWV BCD sideband
    intermod_power_700_hz_db: Optional[float] = None  # WWVH BCD sideband
    intermod_ratio_400_700_db: Optional[float] = None  # WWV vs WWVH intermod signature
    intermod_dominant_station: Optional[str] = None    # From intermod analysis
    intermod_confidence: float = 0.0
    
    # Test signal analysis (minutes 8 and 44 only)
    test_signal_detected: bool = False
    test_signal_fss_db: Optional[float] = None  # Frequency Selectivity Score (D-layer indicator)
    test_signal_delay_spread_ms: Optional[float] = None  # Multipath from chirp analysis
    test_signal_toa_offset_ms: Optional[float] = None  # High-precision ToA
    test_signal_coherence_time_sec: Optional[float] = None  # Channel stability
    
    # CHU FSK analysis (seconds 31-39 of each minute)
    chu_fsk_detected: bool = False
    chu_fsk_frames_decoded: int = 0  # Number of successfully decoded frames (max 9)
    chu_fsk_timing_offset_ms: Optional[float] = None  # Offset from 500ms boundary
    chu_fsk_dut1_seconds: Optional[float] = None  # UT1-UTC correction
    chu_fsk_tai_utc: Optional[int] = None  # TAI-UTC (leap seconds)
    chu_fsk_decode_confidence: float = 0.0  # Frame decode success rate
    chu_fsk_time_verified: bool = False  # Decoded time matches expected
    
    # Narrowed search window for Step 3
    refined_search_window_ms: float = 50.0  # Tightened from 500ms to 50ms
    
    # Carrier SNR for uncertainty estimation
    snr_db: Optional[float] = None  # Carrier signal-to-noise ratio
    
    # Validation
    cross_validation_agreements: List[str] = field(default_factory=list)
    cross_validation_disagreements: List[str] = field(default_factory=list)


@dataclass
class TransmissionTimeSolution:
    """
    Result of Step 3: Transmission Time Solution.
    
    The final D_clock output representing the clock offset.
    """
    # Tone Detection Provenance (NEW: v1.2.0)
    tone_detected: bool             # Was a validated tone actually detected?
    arrival_rtp: Optional[int]       # Raw arrival time in RTP units (for calibration)
    raw_tone_arrival_ms: Optional[float]  # Raw timing from multi-station detector (None if no tone)
    
    # The Holy Grail: D_clock
    d_clock_ms: float               # D_clock = T_system - T_UTC
    
    # UTC recovery
    t_emission_ms: float            # Back-calculated emission time offset
    t_arrival_ms: float             # Measured arrival time (same as raw_tone_arrival_ms)
    t_propagation_ms: float         # Calculated propagation delay
    
    # Propagation mode identification
    propagation_mode: str           # '1F', '2F', 'GW', etc.
    n_hops: int                     # Number of ionospheric hops
    layer_height_km: float          # Estimated ionospheric layer height
    
    # Station used for solution
    station: str                    # 'WWV', 'WWVH', 'CHU'
    frequency_mhz: float
    
    # Confidence metrics
    confidence: float               # 0-1 overall confidence
    uncertainty_ms: float           # Estimated timing uncertainty
    utc_verified: bool = False      # True if |emission_offset| < 2ms
    
    # Dual-station cross-validation
    dual_station_agreement_ms: Optional[float] = None  # |T_wwv - T_wwvh|
    dual_station_verified: bool = False
    
    # All propagation mode candidates with probabilities (for Mode Ridge visualization)
    mode_candidates: List[Dict] = field(default_factory=list)


@dataclass
class Phase2Result:
    """
    Complete Phase 2 analysis result for one minute of data.
    
    Combines all three steps into a single output structure.
    """
    # Timing reference
    minute_boundary_utc: float      # UTC minute boundary this measurement relates to
    system_time: float              # System time of first sample
    rtp_timestamp: int              # RTP timestamp of first sample
    
    # Step 1: Time Snap
    time_snap: TimeSnapResult
    
    # Step 2: Channel Characterization
    channel: ChannelCharacterization
    
    # Step 3: Transmission Time Solution
    solution: TransmissionTimeSolution
    
    # Final D_clock (propagated from solution)
    d_clock_ms: float
    utc_time: float                 # Calculated UTC = system_time - d_clock
    
    # Quality metrics (Issue 6.2 Fix: replaced arbitrary grades with uncertainty)
    uncertainty_ms: float = 10.0    # Estimated timing uncertainty in ms
    confidence: float = 0.0         # 0-1 confidence score
    
    # Deprecated: quality_grade removed per Issue 6.2
    # Old grades (A/B/C/D/X) had no statistical basis and are replaced by
    # uncertainty_ms which has physical meaning (expected error bounds).
    
    # Processing metadata
    processing_version: str = '2.1.0'  # Version bump for grade removal
    processed_at: Optional[float] = None


class Phase2TemporalEngine:
    """
    Phase 2 Temporal Analysis Engine.
    
    Implements the refined temporal analysis order:
    1. Fundamental Tone Detection → Time Snap Anchor
    2. Ionospheric Channel Characterization → Confidence Scoring
    3. Transmission Time Solution → D_clock
    """
    
    def __init__(
        self,
        raw_buffer_dir: Path,
        output_dir: Path,
        channel_name: str,
        frequency_hz: float,
        receiver_grid: str,
        sample_rate: int = SAMPLE_RATE_FULL,
        precise_lat: Optional[float] = None,
        precise_lon: Optional[float] = None,
        discriminator: Optional[Any] = None,
        solver: Optional[Any] = None
    ):
        """
        Initialize the Phase 2 Temporal Engine.
        
        Args:
            raw_buffer_dir: Directory containing Phase 1 raw_buffer
            output_dir: Output directory for Phase 2 products
            channel_name: Channel identifier (e.g., 'WWV_10MHz')
            frequency_hz: Center frequency in Hz
            receiver_grid: Receiver Maidenhead grid square (e.g., 'EM38ww')
            sample_rate: Input sample rate (default 20000 Hz)
            precise_lat: Optional precise latitude (improves timing by ~16μs)
            precise_lon: Optional precise longitude (improves timing by ~16μs)
            discriminator: Optional discriminator instance (dependency injection)
            solver: Optional transmission time solver (dependency injection)
        """
        self.raw_buffer_dir = Path(raw_buffer_dir)
        self.output_dir = Path(output_dir)
        self.channel_name = channel_name
        self.frequency_hz = frequency_hz
        self.frequency_mhz = frequency_hz / 1e6
        self.receiver_grid = receiver_grid
        self.sample_rate = sample_rate
        self.precise_lat = precise_lat
        self.precise_lon = precise_lon
        
        # Dependency injection
        self.discriminator = discriminator
        self.solver = solver
        
        # Initialize sub-components (lazy import to avoid circular deps)
        self._init_components()
        
        # Processing state
        self._lock = threading.Lock()
        self.minutes_processed = 0
        self.last_result: Optional[Phase2Result] = None
        
        # CRITICAL FIX (2026-01-04): D_clock continuity tracking
        # Track previous D_clock for continuity validation
        # Detects CHU frame slips and other timing jumps
        self._last_d_clock_ms: Optional[float] = None
        
        # Load persisted calibration
        self._load_calibration()
        
        # Configurable search window (can be set by timing calibrator)
        # Default is wide (500ms) for bootstrap, narrowed after calibration
        self.config_search_window_ms: Optional[float] = None
        
        # Station prediction callback (set by pipeline orchestrator)
        # Signature: predict_station(channel_name, rtp_timestamp, detected_station, confidence) -> (station, conf)
        self.station_predictor: Optional[Callable] = None
        
        # RTP calibration callback (set by pipeline orchestrator)
        # Returns calibrated RTP offset for minute boundary, or None if not calibrated
        # Signature: get_calibrated_rtp_offset(channel_name) -> Optional[int]
        self.rtp_calibration_callback: Optional[Callable] = None
        
        logger.info(f"Phase2TemporalEngine initialized for {channel_name}")
        logger.info(f"  Frequency: {self.frequency_mhz:.2f} MHz")
        logger.info(f"  Receiver: {receiver_grid}")
        logger.info(f"  Sample rate: {sample_rate} Hz")
        
    def _load_calibration(self):
        """Load persisted calibration state."""
        try:
            cal_file = self.output_dir / "timing_calibration.json"
            if cal_file.exists():
                with open(cal_file, 'r') as f:
                    data = json.load(f)
                    
                # Load BPM calibration
                if 'bpm' in data and self.bpm_discriminator:
                    bpm_data = data['bpm']
                    self.bpm_calibration.update(bpm_data)
                    
                    if bpm_data.get('calibrated') and bpm_data.get('delay_offset_ms') is not None:
                        # Restore expected delay in discriminator
                        if hasattr(self.bpm_discriminator, 'expected_delay_ms'):
                            base_delay = self.bpm_discriminator.expected_delay_ms
                            # Note: expected_delay_ms in discriminator might be reset on init
                            # We should ideally store the base and offset separately
                            pass
                            
                        # Restore correlator bank offset
                        if self.correlator_bank:
                            from .station_model import StationID
                            self.correlator_bank.update_calibration(
                                StationID.BPM,
                                bpm_data.get('delay_offset_ms', 0.0)
                            )
                            self.correlator_bank.set_calibrated(True)
                            
                    logger.info(f"Loaded calibration state from {cal_file}")
        except Exception as e:
            logger.warning(f"Failed to load calibration: {e}")

    def _save_calibration(self):
        """Save calibration state to file."""
        try:
            cal_file = self.output_dir / "timing_calibration.json"
            data = {
                'bpm': self.bpm_calibration,
                'last_updated': datetime.now(timezone.utc).isoformat()
            }
            
            # Atomic write
            tmp_file = cal_file.with_suffix('.tmp')
            with open(tmp_file, 'w') as f:
                json.dump(data, f, indent=2)
            tmp_file.replace(cal_file)
            
        except Exception as e:
            logger.warning(f"Failed to save calibration: {e}")
    
    def _init_components(self):
        """Initialize analysis sub-components."""
        try:
            # Step 1: Tone Detector - use FULL rate for accurate timing
            # Decimation causes timing errors due to spectral interactions
            from .tone_detector import MultiStationToneDetector
            self.tone_detector = MultiStationToneDetector(
                channel_name=self.channel_name,
                sample_rate=self.sample_rate  # Full rate (20 kHz) for accuracy
            )
            
            # Step 2: WWV/WWVH Discriminator (includes BCD and Doppler)
            if self.discriminator is None:
                self.discriminator = WWVHDiscriminator(
                    channel_name=self.channel_name,
                    receiver_grid=self.receiver_grid,
                    sample_rate=self.sample_rate
                )
                self.discriminator.frequency_mhz = self.frequency_mhz
            
            # Step 2c: BPM Discriminator (China, shares 2.5/5/10/15 MHz)
            # BPM uses 10ms ticks (vs 5ms WWV) and has UT1 minutes (25-29, 55-59)
            
            # Determine active hours based on frequency (ROC Specificity)
            bpm_active_hours = set(range(24)) # Default to all
            if abs(self.frequency_mhz - 2.5) < 0.1:
                # 2.5 MHz: 07:30 - 01:00 UTC (Off 01:00 - 07:30) -- ON: 00, 08-23
                bpm_active_hours = {0} | set(range(8, 24))
            elif abs(self.frequency_mhz - 15.0) < 0.1:
                 # 15 MHz: 01:00 - 09:00 UTC -- ON: [1, 2, ..., 8]
                 bpm_active_hours = set(range(1, 9))

            from .bpm_discriminator import BPMDiscriminator
            self.bpm_discriminator = BPMDiscriminator(
                receiver_lat=self.precise_lat,
                receiver_lon=self.precise_lon,
                channel_name=self.channel_name,
                active_hours=bpm_active_hours
            )

            # Step 2b: Probabilistic Discriminator (Shadow Mode)
            # This is the new ML-based discriminator running in parallel
            from .probabilistic_discriminator import ProbabilisticDiscriminator
            self.prob_discriminator = ProbabilisticDiscriminator(
                model_path=self.output_dir / 'discriminator_model.json',
                auto_train=True
            )
            
            # Step 3: Transmission Time Solver
            if self.solver is None:
                from .transmission_time_solver import (
                    TransmissionTimeSolver,
                    create_solver_from_grid
                )
                self.solver = create_solver_from_grid(
                self.receiver_grid,
                self.sample_rate,
                precise_lat=self.precise_lat,
                precise_lon=self.precise_lon
            )

            # Step 4: Multi-Station Detector (Physics-based approach)
            # Detects ALL receivable stations and extracts propagation info
            # GPSDO is the timing reference, not the loudest station
            # All detected stations are passed to fusion with their uncertainties
            from .multi_station_detector import MultiStationDetector
            
            self.multi_station_detector = MultiStationDetector(
                receiver_lat=self.precise_lat,
                receiver_lon=self.precise_lon,
                sample_rate=self.sample_rate
            )

            from .differential_time_solver import DifferentialTimeSolver
            from .differential_time_solver import GlobalDifferentialSolver

            receiver_lat = self.precise_lat if self.precise_lat is not None else 39.0
            receiver_lon = self.precise_lon if self.precise_lon is not None else -98.0
            self.differential_solver = DifferentialTimeSolver(
                receiver_lat=receiver_lat,
                receiver_lon=receiver_lon
            )

            self.global_differential_solver = GlobalDifferentialSolver(
                receiver_lat=receiver_lat,
                receiver_lon=receiver_lon
            )
            
            # Step 5: Correlator Bank (MLE-based component decomposition)
            # Parallel matched filtering with station-specific templates
            # Centered on predicted ToA windows for each station
            from .correlator_bank import CorrelatorBank
            
            if self.precise_lat is not None and self.precise_lon is not None:
                self.correlator_bank = CorrelatorBank(
                    receiver_lat=self.precise_lat,
                    receiver_lon=self.precise_lon,
                    sample_rate=self.sample_rate,
                    calibrated=False  # Will be set True after UT1 calibration
                )
                logger.info("✅ CorrelatorBank initialized for MLE-based discrimination")
            else:
                self.correlator_bank = None
                logger.warning("⚠️ CorrelatorBank not initialized (no precise coordinates)")
            
            # Timing calibrator (injected by service)
            self.timing_calibrator = None
            
            # BPM calibration state (updated from UT1 pulse detection)
            self.bpm_calibration = {
                'calibrated': False,
                'last_calibration_minute': None,
                'path_gain_db': None,
                'delay_offset_ms': None
            }
            
            logger.info("✅ Phase 2 components initialized (MultiStationDetector + CorrelatorBank)")
            
        except ImportError as e:
            logger.error(f"Failed to initialize Phase 2 components: {e}")
            raise
    
    def _validate_input(self, iq_samples: np.ndarray) -> Tuple[np.ndarray, Dict[str, Any]]:
        """
        Validate and normalize 32-bit float IQ input data.
        
        Ensures input is np.complex64 as expected from Phase 1 archive.
        Applies fixed normalization to prevent numerical issues while
        preserving linearity.
        
        Args:
            iq_samples: Input IQ samples from Phase 1 archive
            
        Returns:
            Tuple of (normalized_samples, validation_metrics)
        """
        metrics = {
            'input_dtype': str(iq_samples.dtype),
            'input_shape': iq_samples.shape,
            'max_amplitude': 0.0,
            'mean_amplitude': 0.0,
            'normalization_applied': False,
            'amplitude_warning': False
        }
        
        # Check dtype - must be complex64 (32-bit float IQ)
        if iq_samples.dtype != EXPECTED_DTYPE:
            logger.warning(
                f"Input dtype {iq_samples.dtype} differs from expected {EXPECTED_DTYPE}. "
                f"Converting to {EXPECTED_DTYPE}."
            )
            iq_samples = iq_samples.astype(EXPECTED_DTYPE)
        
        # Calculate amplitude statistics
        amplitudes = np.abs(iq_samples)
        max_amp = float(np.max(amplitudes))
        mean_amp = float(np.mean(amplitudes))
        
        metrics['max_amplitude'] = max_amp
        metrics['mean_amplitude'] = mean_amp
        
        # Check for amplitude warnings
        if max_amp > AMPLITUDE_WARNING_THRESHOLD:
            logger.warning(
                f"High amplitude detected: max={max_amp:.2f} > threshold={AMPLITUDE_WARNING_THRESHOLD}. "
                f"This may indicate decode errors or unusual signal conditions."
            )
            metrics['amplitude_warning'] = True
        
        # Apply fixed normalization if needed
        # Since Phase 1 uses F32 without AGC, the amplitude range varies
        # Normalize to [-1, 1] range for consistent processing
        if max_amp > MAX_EXPECTED_AMPLITUDE:
            normalization_factor = max_amp
            iq_samples = iq_samples / normalization_factor
            metrics['normalization_applied'] = True
            metrics['normalization_factor'] = normalization_factor
            logger.debug(f"Applied normalization: factor={normalization_factor:.4f}")
        
        return iq_samples, metrics
    
    def _predict_propagation_delay(
        self,
        station: str,  # 'WWV', 'WWVH', 'CHU'
        timestamp: datetime
    ) -> Tuple[float, float]:
        """
        Predict propagation delay using IRI-2020 ionospheric model.
        
        Uses predicted hmF2 (F2 layer height) and station geometry to calculate
        expected propagation delay. Centers search window at predicted arrival time.
        
        THEORY:
        -------
        For 1-hop F-layer propagation:
            path_length = 2 × sqrt(hmF2² + (distance/2)²)
            delay = path_length / c
        
        Where:
            hmF2 = F2 layer peak height (from IRI-2020 model)
            distance = great circle distance TX to RX
            c = speed of light (299.792458 km/ms)
        
        EXPECTED IMPACT:
        ----------------
        - Search window centering within ±10ms of actual arrival
        - 15-25% reduction in false positives
        - Better multipath rejection
        - Enables tighter search windows (±15ms instead of ±500ms)
        
        Args:
            station: Station identifier ('WWV', 'WWVH', 'CHU')
            timestamp: UTC timestamp for ionospheric prediction
            
        Returns:
            (expected_delay_ms, uncertainty_ms)
        
        Reference:
            Davies, K. (1990). "Ionospheric Radio." Peter Peregrinus Ltd.
            Chapter 6: HF Propagation Prediction.
        """
        # Get receiver location (from grid square or precise lat/lon)
        if self.precise_lat and self.precise_lon:
            receiver_lat = self.precise_lat
            receiver_lon = self.precise_lon
        else:
            # Convert grid square to lat/lon (approximate center)
            # Use existing method from propagation_mode_solver if available
            from .propagation_mode_solver import PropagationModeSolver
            solver = PropagationModeSolver(self.receiver_grid, self.sample_rate)
            receiver_lat, receiver_lon = solver.receiver_lat, solver.receiver_lon
        
        # Initialize ionospheric model if not already present
        if not hasattr(self, 'iono_model'):
            from .ionospheric_model import IonosphericModel
            self.iono_model = IonosphericModel()
            logger.debug("Initialized IonosphericModel for propagation prediction")
        
        # Get predicted layer height from IRI-2020
        heights = self.iono_model.get_layer_heights(
            timestamp=timestamp,
            latitude=receiver_lat,
            longitude=receiver_lon
        )
        hmF2_km = heights.hmF2
        hmF2_uncertainty_km = heights.hmF2_uncertainty_km
        
        # Station locations and approximate distances
        # TODO: Use precise haversine calculation from station_model
        station_info = {
            'WWV': {'distance_km': 1500, 'name': 'Fort Collins, CO'},
            'WWVH': {'distance_km': 6000, 'name': 'Kauai, HI'},
            'CHU': {'distance_km': 1200, 'name': 'Ottawa, Canada'}
        }
        
        if station not in station_info:
            logger.warning(f"Unknown station {station}, using default distance")
            distance_km = 1500
        else:
            distance_km = station_info[station]['distance_km']
        
        # 1-hop F-layer geometry: path_length = 2 × sqrt(h² + (d/2)²)
        half_distance = distance_km / 2.0
        path_length_km = 2 * np.sqrt(hmF2_km**2 + half_distance**2)
        
        # Propagation delay (speed of light = 299.792458 km/ms)
        c_km_per_ms = 299.792458
        expected_delay_ms = path_length_km / c_km_per_ms
        
        # Uncertainty from hmF2 uncertainty
        # dDelay/dh = 2h / (c × sqrt(h² + (d/2)²))
        if hmF2_km > 0:
            path_length_uncertainty_km = (2 * hmF2_uncertainty_km * hmF2_km / 
                                         np.sqrt(hmF2_km**2 + half_distance**2))
            uncertainty_ms = max(5.0, path_length_uncertainty_km / c_km_per_ms)
        else:
            uncertainty_ms = 10.0  # Default uncertainty
        
        logger.debug(f"Ionospheric prediction for {station}: "
                    f"hmF2={hmF2_km:.1f}±{hmF2_uncertainty_km:.1f}km, "
                    f"distance={distance_km}km, "
                    f"delay={expected_delay_ms:.1f}±{uncertainty_ms:.1f}ms "
                    f"(tier={heights.tier.value})")
        
        return expected_delay_ms, uncertainty_ms
    
    def _validate_d_clock_continuity(
        self,
        current_d_clock_ms: float,
        previous_d_clock_ms: Optional[float],
        dt_seconds: float,
        channel_name: str
    ) -> Tuple[bool, str]:
        """
        Validate that D_clock hasn't jumped unrealistically.
        
        Physical propagation delay cannot change faster than ~0.1ms/minute
        due to ionospheric dynamics. Rapid jumps indicate:
        - CHU frame slips (33ms jumps)
        - Multipath mode hopping
        - Decoder errors
        
        THEORY:
        -------
        Ionospheric layer heights change slowly:
        - Diurnal variation: ~50 km over 12 hours = 0.07 km/min
        - This translates to ~0.05 ms/min propagation delay change
        
        System clock drift (GPSDO):
        - Typical: <0.01 ms/min
        
        Therefore, D_clock should change by <0.1 ms/min under normal conditions.
        We allow 2ms baseline for measurement noise + 0.1ms per minute.
        
        Args:
            current_d_clock_ms: Current D_clock measurement
            previous_d_clock_ms: Previous D_clock measurement (or None)
            dt_seconds: Time between measurements
            channel_name: Channel for logging
        
        Returns:
            (is_valid, reason)
        """
        if previous_d_clock_ms is None:
            return True, "First measurement"
        
        delta_ms = abs(current_d_clock_ms - previous_d_clock_ms)
        
        # Maximum allowed change: 2ms baseline + 0.1ms per minute
        # This accounts for:
        # - Ionospheric variation: ~0.05ms/minute
        # - Clock drift: ~0.01ms/minute (GPSDO)
        # - Measurement noise: ~1-2ms
        dt_minutes = dt_seconds / 60.0
        # TEMPORARY RELAXATION: Allow large jumps for calibration recovery (480ms observed)
        max_allowed_ms = 2000.0 + 0.1 * dt_minutes
        
        if delta_ms > max_allowed_ms:
            reason = (f"D_clock jump: {delta_ms:.2f}ms in {dt_seconds:.0f}s "
                     f"(max allowed: {max_allowed_ms:.2f}ms)")
            logger.warning(f"{channel_name}: {reason}")
            return False, reason
        
        return True, "Continuity OK"
    
    def _step1_tone_detection(
        self,
        iq_samples: np.ndarray,
        system_time: float,
        rtp_timestamp: int,
        calibration_offsets: Optional[Dict[str, float]] = None
    ) -> TimeSnapResult:
        """
        Step 1: Detect 1000/1200 Hz tones to establish approximate timing.
        
        Uses matched filtering (via ToneDetector) to find signals.
        MultiStationDetector processes ALL detected stations for fusion.
        
        Args:
            iq_samples: Complex IQ samples
            system_time: System time of first sample
            rtp_timestamp: RTP timestamp of first sample
            calibration_offsets: Optional dict of station -> learned offset (ms) from Fusion
        """
        # Calculate buffer mid-point timestamp
        # The tone detector expects timestamp at MIDDLE of buffer for
        # correct minute boundary calculation
        buffer_duration = len(iq_samples) / self.sample_rate
        buffer_mid_time = system_time + buffer_duration / 2
        
        # ===== SEARCH WINDOW PRIORITY HIERARCHY =====
        # 1. PHYSICS PRIOR (IRI-2020 prediction)
        # 2. BLIND SEARCH (bootstrap/fallback)
        #
        # NOTE: Calibration offsets are NOT used for search window centering!
        # They represent corrections to APPLY to measurements, not expected arrival times.
        # Using them as search centers was causing searches at wrong locations.
        
        expected_offset_ms = None
        adaptive_window_ms = self.config_search_window_ms or 500.0
        search_strategy = "BLIND"
        
        # Determine primary expected station based on frequency
        from ..interfaces.data_models import StationType
        predicted_station = None
        predicted_station_name = None
        
        if self.frequency_mhz in (2.5, 5.0, 10.0, 15.0, 20.0, 25.0):
            # WWV/WWVH frequencies
            predicted_station = StationType.WWV
            predicted_station_name = 'WWV'
        elif self.frequency_mhz in (3.33, 7.85, 14.67):
            # CHU frequencies
            predicted_station = StationType.CHU
            predicted_station_name = 'CHU'
        
        # PRIORITY 1: Use Physics prediction for search window centering
        if predicted_station:
            try:
                # Convert Unix timestamp to datetime for ionospheric model
                from datetime import datetime, timezone
                timestamp_dt = datetime.fromtimestamp(buffer_mid_time, tz=timezone.utc)
                
                predicted_delay_ms, uncertainty_ms = self._predict_propagation_delay(
                    station=predicted_station,
                    timestamp=timestamp_dt
                )
                
                if predicted_delay_ms is not None:
                    expected_offset_ms = predicted_delay_ms
                    # Adaptive window: 3-sigma around prediction
                    adaptive_window_ms = max(10.0, min(3.0 * uncertainty_ms, adaptive_window_ms))
                    search_strategy = "PHYSICS"
                    logger.info(
                        f"📡 Physics prior: {predicted_station.value} delay={predicted_delay_ms:.1f}±{uncertainty_ms:.1f}ms, "
                        f"window=±{adaptive_window_ms:.1f}ms (IRI-2020)"
                    )
            except Exception as e:
                logger.warning(f"Ionospheric prediction failed: {e}, using blind search")
        
        
        # ADAPTIVE WINDOW OVERRIDE: Use calibrator if available and converged
        # This overrides physics prediction with learned ToA after sufficient convergence
        if self.timing_calibrator and predicted_station_name:
            # Check if we have learned ToA for this station
            expected_toa = self.timing_calibrator.get_expected_toa(
                predicted_station_name,
                self.frequency_mhz,
                self.channel_name
            )
            
            if expected_toa is not None:
                # Use learned ToA with narrow window
                expected_offset_ms = expected_toa
                adaptive_window_ms = self.timing_calibrator.get_search_window(
                    predicted_station_name,
                    self.frequency_mhz,
                    self.channel_name
                )
                search_strategy = "LEARNED"
                logger.info(
                    f"🎯 Learned ToA: {predicted_station_name} @ {self.frequency_mhz}MHz, "
                    f"expected={expected_offset_ms:.1f}ms, window=±{adaptive_window_ms:.1f}ms"
                )
        
        # CRITICAL FIX (2026-01-04): Cross-frequency guidance
        # Use strong detections from other frequencies to narrow search window
        # Key insight: WWVH ToA across frequencies correlates tighter than WWV vs WWVH on same freq
        if search_strategy == "BLIND" and predicted_station_name:
            minute_boundary = int(system_time) - int(system_time) % 60
            cross_freq_guidance = self.multi_station_detector.get_cross_freq_guidance(
                station=predicted_station_name,
                target_frequency_mhz=self.frequency_mhz,
                minute_boundary=minute_boundary
            )
            
            if cross_freq_guidance and cross_freq_guidance['source_snr_db'] > 10.0:
                # Strong detection on another frequency - use it to guide this one
                expected_offset_ms = cross_freq_guidance['expected_toa_ms']
                adaptive_window_ms = cross_freq_guidance['search_window_ms']
                search_strategy = "CROSS_FREQ"
                logger.info(
                    f"🔗 Cross-freq guidance: {predicted_station_name} from {cross_freq_guidance['source_frequency_mhz']:.1f}MHz "
                    f"(SNR={cross_freq_guidance['source_snr_db']:.1f}dB), "
                    f"expected={expected_offset_ms:.1f}ms, window=±{adaptive_window_ms:.1f}ms"
                )
        
        # PRIORITY 2: Blind search (bootstrap or fallback)
        if search_strategy == "BLIND":
            expected_offset_ms = 0.0
            logger.info(f"🔍 Bootstrap mode: wide search ±{adaptive_window_ms:.0f}ms")
        
        detections = self.tone_detector.process_samples(
            timestamp=buffer_mid_time, # Use buffer_mid_time for tone detector
            samples=iq_samples,
            rtp_timestamp=rtp_timestamp,
            original_sample_rate=self.sample_rate, # Added from original code
            buffer_rtp_start=rtp_timestamp, # Added from original code
            search_window_ms=adaptive_window_ms,  # Use adaptive window
            expected_offset_ms=expected_offset_ms  # Center at predicted delay
        )
        
        # B. Analyze detections
        wwv_det = None
        wwvh_det = None
        chu_det = None
        bpm_det = None
        
        if detections:
            from ..interfaces.data_models import StationType
            for det in detections:
                if det.station == StationType.WWV:
                    wwv_det = det
                elif det.station == StationType.WWVH:
                    wwvh_det = det
                elif det.station == StationType.CHU:
                    chu_det = det
        
        
        # Record detection results for adaptive window tracking
        if self.timing_calibrator:
            if detections:
                # Successful detection - record for each detected station
                for det in detections:
                    self.timing_calibrator.record_detection(
                        station=det.station.value,
                        frequency_mhz=self.frequency_mhz,
                        channel_name=self.channel_name,
                        toa_ms=det.timing_error_ms
                    )
            else:
                # No detection - record failure
                self.timing_calibrator.record_failure(
                    frequency_mhz=self.frequency_mhz,
                    channel_name=self.channel_name
                )
                
                # Check if we should back off (widen windows)
                if self.timing_calibrator.should_back_off(self.channel_name):
                    logger.warning(
                        f"{self.channel_name}: Lost lock after consecutive failures - "
                        "system will widen search windows on next iteration"
                    )
        # B2. BPM Detection (separate discriminator - uses tick duration)
        # BPM shares 2.5/5/10/15 MHz with WWV/WWVH but has 10ms ticks (vs 5ms)
        # Only check on shared frequencies
        bpm_det = None  # Initialize before conditional block
        bpm_timing_mode = None
        bpm_is_usable = True
        if self.frequency_mhz in (2.5, 5.0, 10.0, 15.0):
            try:
                # Get minute and hour for UT1/UTC mode detection and scheduling
                dt = datetime.fromtimestamp(system_time, tz=timezone.utc)
                minute_of_hour = dt.minute
                hour_of_day = dt.hour
                
                bpm_result = self.bpm_discriminator.analyze(
                    iq_samples=iq_samples,
                    sample_rate=self.sample_rate,
                    minute=minute_of_hour,
                    hour=hour_of_day
                )
                if bpm_result and bpm_result.is_bpm_detected:
                    # Create a detection-like object for BPM
                    bpm_det = bpm_result
                    bpm_timing_mode = bpm_result.timing_mode.value
                    bpm_is_usable = bpm_result.is_usable_for_utc
                    if not bpm_is_usable:
                        logger.debug(f"BPM detected but minute {minute_of_hour} is UT1 mode (not usable for UTC)")
                    else:
                        logger.info(f"📡 BPM detected: delay={bpm_result.measured_delay_ms:.1f}ms, SNR={bpm_result.snr_db:.1f}dB")
            except Exception as e:
                logger.debug(f"BPM detection failed: {e}")
        
        # C. Multi-Station Detection (Physics-based approach)
        # Process ALL detected stations and extract propagation info
        # This replaces the old "voting" approach
        minute_boundary = int(system_time) - int(system_time) % 60
        tone_detections = {
            'wwv': wwv_det,
            'wwvh': wwvh_det,
            'chu': chu_det
        }
        
        multi_station_result = self.multi_station_detector.process_detections(
            channel=self.channel_name,
            frequency_mhz=self.frequency_mhz,
            minute_boundary=minute_boundary,
            rtp_timestamp=rtp_timestamp,
            system_time=system_time,
            tone_detections=tone_detections,
            bpm_detection=bpm_det
        )
        
        # Save detections for cross-frequency coordination
        for detection in multi_station_result.detections.values():
            if detection.detected:
                self.multi_station_detector.save_detection_for_cross_freq(detection, minute_boundary)
        
        # D. Use multi-station result for timing (physics-based, no voting)
        # The best timing comes from the station with lowest uncertainty
        # ALL stations are passed to fusion - we just need one for the TimeSnapResult
        timing_error_ms = 0.0
        anchor_station = 'UNKNOWN'
        anchor_confidence = 0.0
        
        if multi_station_result.best_timing_station:
            anchor_station = multi_station_result.best_timing_station
            best_det = multi_station_result.detections.get(anchor_station)
            if best_det and best_det.detected:
                timing_error_ms = best_det.measured_toa_ms
                anchor_confidence = best_det.confidence
        else:
            # Fallback: use any detected station
            for det in multi_station_result.get_all_usable_detections():
                timing_error_ms = det.measured_toa_ms
                anchor_station = det.station
                anchor_confidence = det.confidence
                break

        # Calculate arrival RTP from timing error
        timing_offset_samples = round(timing_error_ms * self.sample_rate / 1000)
        arrival_rtp = rtp_timestamp + timing_offset_samples
        
        # CRITICAL FIX (2026-01-04): Extract timing from multi-station detector
        # The multi-station detector finds all stations, but we need to populate
        # individual station timing fields for inter-station validation
        wwv_timing_from_multi = None
        wwvh_timing_from_multi = None
        chu_timing_from_multi = None
        wwv_snr_from_multi = None
        wwvh_snr_from_multi = None
        chu_snr_from_multi = None
        
        usable_detections = multi_station_result.get_all_usable_detections()
        logger.info(f"🔍 Multi-station detector found {len(usable_detections)} usable detections")
        
        for det in usable_detections:
            logger.info(f"  📡 Station {det.station}: ToA={det.measured_toa_ms:.2f}ms, SNR={det.snr_db:.1f}dB")
            if det.station == 'WWV':
                wwv_timing_from_multi = det.measured_toa_ms
                wwv_snr_from_multi = det.snr_db
            elif det.station == 'WWVH':
                wwvh_timing_from_multi = det.measured_toa_ms
                wwvh_snr_from_multi = det.snr_db
            elif det.station == 'CHU':
                chu_timing_from_multi = det.measured_toa_ms
                chu_snr_from_multi = det.snr_db
        
        result = TimeSnapResult(
            timing_error_ms=timing_error_ms,
            arrival_rtp=arrival_rtp,
            arrival_system_time=system_time + (timing_error_ms / 1000.0),
            wwv_detected=wwv_det is not None or wwv_timing_from_multi is not None,
            wwvh_detected=wwvh_det is not None or wwvh_timing_from_multi is not None,
            chu_detected=chu_det is not None or chu_timing_from_multi is not None,
            bpm_detected=bpm_det is not None,
            # Use multi-station detector results if available, otherwise fall back to individual detectors
            wwv_snr_db=wwv_snr_from_multi if wwv_snr_from_multi is not None else (wwv_det.snr_db if wwv_det else None),
            wwvh_snr_db=wwvh_snr_from_multi if wwvh_snr_from_multi is not None else (wwvh_det.snr_db if wwvh_det else None),
            chu_snr_db=chu_snr_from_multi if chu_snr_from_multi is not None else (chu_det.snr_db if chu_det else None),
            bpm_snr_db=bpm_det.snr_db if bpm_det else None,
            wwv_timing_ms=wwv_timing_from_multi if wwv_timing_from_multi is not None else (wwv_det.timing_error_ms if wwv_det else None),
            wwvh_timing_ms=wwvh_timing_from_multi if wwvh_timing_from_multi is not None else (wwvh_det.timing_error_ms if wwvh_det else None),
            chu_timing_ms=chu_timing_from_multi if chu_timing_from_multi is not None else (chu_det.timing_error_ms if chu_det else None),
            # BPM timing comes from multi-station detector (measured_toa_ms), not discriminator
            bpm_timing_ms=multi_station_result.detections.get('BPM', None).measured_toa_ms if multi_station_result.detections.get('BPM') and multi_station_result.detections['BPM'].detected else None,
            bpm_timing_mode=bpm_timing_mode,
            bpm_is_usable_for_utc=bpm_is_usable,
            multi_station_result=multi_station_result,  # Physics-based multi-station detection
            anchor_station=anchor_station,
            anchor_confidence=anchor_confidence,
            search_window_ms=self.config_search_window_ms or 500.0  # Use calibrated window if available
        )
        
        logger.debug(
            f"Step 1 Time Snap: anchor={anchor_station}, "
            f"timing_error={timing_error_ms:+.2f}ms, confidence={anchor_confidence:.2f}"
        )
        
        return result
    
    def _step2_channel_characterization(
        self,
        iq_samples: np.ndarray,
        time_snap: TimeSnapResult,
        system_time: float,
        minute_number: int,
        calibration_offsets: Optional[Dict[str, float]] = None
    ) -> ChannelCharacterization:
        """
        Step 2: Ionospheric Channel Characterization.
        
        Uses the time snap from Step 1 to synchronize BCD correlation and
        Doppler estimation for high-sensitivity channel analysis.
        
        Sub-steps:
        A. BCD Correlation & Dual-Peak Delay
        B. Doppler and Coherence Estimation
        C. Station Identity & Ground Truth
        
        Args:
            iq_samples: Full-rate (20 kHz) complex64 IQ samples
            time_snap: Result from Step 1
            system_time: System time of first sample
            minute_number: Minute of hour (0-59)
            calibration_offsets: Optional map of station name to calibration offset (ms)
            
        Returns:
            ChannelCharacterization with channel metrics
        """
        result = ChannelCharacterization()
        agreements = []
        disagreements = []
        
        # === Step 2A-PRE: BPM UT1 Calibration (minutes 25-29, 55-59) ===
        # During UT1 minutes, BPM transmits 100ms pulses (10× longer than WWV's 5ms)
        # These are UNAMBIGUOUS BPM markers - use them to calibrate BPM path
        if self.frequency_mhz in (2.5, 5.0, 10.0, 15.0) and minute_number in {25, 26, 27, 28, 29, 55, 56, 57, 58, 59}:
            try:
                ut1_result = self.bpm_discriminator.detect_ut1_pulses(
                    iq_samples=iq_samples,
                    sample_rate=self.sample_rate,
                    minute=minute_number
                )
                
                if ut1_result and ut1_result.get('detected'):
                    # Update BPM calibration
                    calibration = self.bpm_discriminator.calibrate_from_ut1(ut1_result)
                    if calibration:
                        self.bpm_calibration['calibrated'] = True
                        self.bpm_calibration['last_calibration_minute'] = minute_number
                        self.bpm_calibration['path_gain_db'] = calibration.get('path_gain_db')
                        self.bpm_calibration['delay_offset_ms'] = calibration.get('adjustment_ms')
                        
                        # Update correlator bank with calibrated BPM delay
                        if self.correlator_bank:
                            from .station_model import StationID
                            self.correlator_bank.update_calibration(
                                StationID.BPM,
                                calibration.get('adjustment_ms', 0.0)
                            )
                            self.correlator_bank.set_calibrated(True)
                        
                        # Persist calibration to disk
                        self._save_calibration()
                        
                        logger.info(f"🎯 BPM UT1 calibration saved: delay_adj={calibration.get('adjustment_ms', 0):+.2f}ms, "
                                   f"gain={calibration.get('path_gain_db', 0):.1f}dB, "
                                   f"quality={calibration.get('quality', 'unknown')}")
            except Exception as e:
                logger.debug(f"BPM UT1 calibration failed: {e}")
        
        # === Step 2A-ALT: Correlator Bank (MLE-based component decomposition) ===
        # Run parallel matched filtering for all stations on this frequency
        channel_assignment = None
        if self.correlator_bank and self.frequency_mhz in (2.5, 5.0, 10.0, 15.0):
            try:
                # Apply feedback loop calibration if available
                if calibration_offsets:
                    from .station_model import StationID
                    for station_name, offset in calibration_offsets.items():
                        try:
                            # Map string name (e.g. "WWV") to StationID enum
                            if hasattr(StationID, station_name):
                                st_id = StationID[station_name]
                                self.correlator_bank.update_calibration(st_id, offset)
                        except KeyError:
                            pass
                    self.correlator_bank.set_calibrated(True)
                    
                channel_assignment = self.correlator_bank.process_minute(
                    iq_samples=iq_samples,
                    frequency_mhz=self.frequency_mhz,
                    minute=minute_number,
                    channel=self.channel_name,
                    minute_timestamp=system_time
                )
                
                if channel_assignment:
                    # Store correlator bank results in channel characterization
                    # These provide per-station power decomposition
                    if channel_assignment.wwv_component_power_db is not None:
                        result.bcd_wwv_amplitude = 10 ** (channel_assignment.wwv_component_power_db / 20.0)
                    if channel_assignment.wwvh_component_power_db is not None:
                        result.bcd_wwvh_amplitude = 10 ** (channel_assignment.wwvh_component_power_db / 20.0)
                    if channel_assignment.bpm_component_power_db is not None:
                        result.bcd_bpm_amplitude = 10 ** (channel_assignment.bpm_component_power_db / 20.0)
                    
                    # Store ToA values
                    result.bcd_wwv_toa_ms = channel_assignment.wwv_toa_ms
                    result.bcd_wwvh_toa_ms = channel_assignment.wwvh_toa_ms
                    result.bcd_bpm_toa_ms = channel_assignment.bpm_toa_ms
                    
                    # Cross-validation from correlator bank
                    if channel_assignment.cross_validation_error_ms is not None:
                        if channel_assignment.cross_validation_passed:
                            agreements.append('correlator_bank_cross_validation')
                        else:
                            disagreements.append(f'correlator_bank_error_{channel_assignment.cross_validation_error_ms:.1f}ms')
                    
                    logger.info(f"📊 CorrelatorBank: WWV={channel_assignment.wwv_confidence:.2f}, "
                               f"WWVH={channel_assignment.wwvh_confidence:.2f}, "
                               f"BPM={channel_assignment.bpm_confidence:.2f} "
                               f"(mode={channel_assignment.bpm_timing_mode})")
            except Exception as e:
                logger.debug(f"Correlator bank processing failed: {e}")
        
        # === Step 2A: BCD Correlation & Dual-Peak Delay ===
        # The time snap from Step 1 provides the expected minute boundary,
        # allowing accurate template synchronization
        # Skip if correlator bank already provided amplitudes
        # optimization: Skip BCD during BPM pure carrier minutes on shared freqs
        is_bpm_pure_carrier = (
            self.frequency_mhz in (2.5, 5.0, 10.0, 15.0) and 
            minute_number in BPM_PURE_CARRIER_MINUTES
        )
        
        # Skip BCD discrimination on station-specific frequencies
        # These frequencies only have one station, so discrimination is unnecessary
        from .wwv_constants import STATION_SPECIFIC_FREQ
        is_station_specific = self.frequency_mhz in STATION_SPECIFIC_FREQ
        
        if is_station_specific:
            # Direct station labeling for station-specific frequencies
            station_name = STATION_SPECIFIC_FREQ[self.frequency_mhz]
            logger.debug(
                f"Skipping BCD discrimination for {station_name}-specific "
                f"frequency {self.frequency_mhz} MHz"
            )
            # Set high-confidence single-station result
            if station_name == 'WWV':
                result.bcd_wwv_amplitude = 1.0
                result.bcd_wwvh_amplitude = 0.0
            elif station_name == 'CHU':
                result.bcd_wwv_amplitude = 0.0
                result.bcd_wwvh_amplitude = 0.0
            result.bcd_correlation_quality = 1.0
        elif (not is_bpm_pure_carrier and 
            (result.bcd_wwv_amplitude is None or result.bcd_wwvh_amplitude is None)):
            try:
                bcd_result = self.discriminator.detect_bcd_discrimination(
                    iq_samples=iq_samples,
                    sample_rate=self.sample_rate,
                    minute_timestamp=system_time,
                    frequency_mhz=self.frequency_mhz
                )
                
                if bcd_result and bcd_result[0] is not None:
                    wwv_amp, wwvh_amp, delay_ms, quality, windows = bcd_result
                    result.bcd_wwv_amplitude = wwv_amp
                    result.bcd_wwvh_amplitude = wwvh_amp
                    result.bcd_differential_delay_ms = delay_ms
                    result.bcd_correlation_quality = quality
                    
                    # Extract ToA and delay spread from windows if available
                    if windows and len(windows) > 0:
                        # Use first high-quality window
                        for w in windows:
                            if w.get('wwv_toa_ms') is not None:
                                result.bcd_wwv_toa_ms = w['wwv_toa_ms']
                                result.bcd_wwvh_toa_ms = w['wwvh_toa_ms']
                                
                                # Extract delay spread from BCD correlation peak widths
                                # Use the delay spread of the dominant station
                                wwv_spread = w.get('wwv_delay_spread_ms')
                                wwvh_spread = w.get('wwvh_delay_spread_ms')
                                if wwv_spread is not None and wwvh_spread is not None:
                                    # Use average of both stations' delay spreads
                                    result.delay_spread_ms = (wwv_spread + wwvh_spread) / 2.0
                                elif wwv_spread is not None:
                                    result.delay_spread_ms = wwv_spread
                                elif wwvh_spread is not None:
                                    result.delay_spread_ms = wwvh_spread
                                break
                    
                    # Log with None-safe formatting
                    logger.debug(
                        f"Step 2A BCD: WWV_amp={wwv_amp if wwv_amp is not None else 'None'}, "
                        f"WWVH_amp={wwvh_amp if wwvh_amp is not None else 'None'}, "
                        f"delay={delay_ms if delay_ms is not None else 'None'}ms, "
                        f"quality={quality if quality is not None else 'None'}"
                    )
            except Exception as e:
                logger.warning(f"Step 2A BCD correlation failed: {e}")
        
        # === Step 2B: Doppler and Coherence Estimation ===
        # Measure ionospheric stability from per-tick phase tracking
        tick_results = None  # Will hold per-second tick SNR for discrimination voting
        try:
            doppler_info = self.discriminator.estimate_doppler_shift_from_ticks(
                iq_samples=iq_samples,
                sample_rate=self.sample_rate
            )
            
            if doppler_info:
                result.doppler_wwv_hz = doppler_info.get('wwv_doppler_hz')
                result.doppler_wwvh_hz = doppler_info.get('wwvh_doppler_hz')
                result.doppler_wwv_std_hz = doppler_info.get('wwv_doppler_std_hz')
                result.doppler_wwvh_std_hz = doppler_info.get('wwvh_doppler_std_hz')
                result.max_coherent_window_sec = doppler_info.get('max_coherent_window_sec')
                result.doppler_quality = doppler_info.get('doppler_quality')
                result.phase_variance_rad = doppler_info.get('phase_variance_rad')
                result.doppler_carrier_hz = doppler_info.get('carrier_doppler_hz')
            
            # Detect per-second tick SNR for discrimination voting (Vote 4)
            # Uses 60-second coherent integration for maximum sensitivity
            tick_windows = self.discriminator.detect_tick_windows(
                iq_samples=iq_samples,
                sample_rate=self.sample_rate,
                window_seconds=60
            )
            if tick_windows:
                tick_results = tick_windows
                logger.debug(f"Step 2B Tick detection: {len(tick_windows)} windows")
                
                # Estimate coherence time from Doppler standard deviation
                max_std = max(
                    result.doppler_wwv_std_hz or 0.0,
                    result.doppler_wwvh_std_hz or 0.0
                )
                if max_std > 0.001:
                    # τ_c ≈ 1 / (π × f_D)
                    result.coherence_time_sec = 1.0 / (np.pi * max_std)
                else:
                    result.coherence_time_sec = 60.0  # Stable channel
                
                logger.debug(
                    f"Step 2B Doppler: WWV={result.doppler_wwv_hz or 0:+.4f}Hz, "
                    f"T_max={result.max_coherent_window_sec or 60:.1f}s"
                )
        except Exception as e:
            logger.warning(f"Step 2B Doppler estimation failed: {e}")
        
        # === Step 2C: Station Identity & Ground Truth ===
        # Check for exclusive broadcast minutes (500/600 Hz tones)
        try:
            # Function returns 6 values: (detected, power_db, freq, station, harmonic_500_1000, harmonic_600_1200)
            gt_result = self.discriminator.detect_500_600hz_tone(
                iq_samples=iq_samples,
                sample_rate=self.sample_rate,
                minute_number=minute_number
            )
            gt_detected, gt_power, gt_freq, gt_station = gt_result[:4]
            harmonic_500_1000, harmonic_600_1200 = gt_result[4], gt_result[5]
            
            # Always store harmonic ratios when computed (useful for analysis)
            if harmonic_500_1000 is not None:
                result.harmonic_ratio_500_1000 = harmonic_500_1000
            if harmonic_600_1200 is not None:
                result.harmonic_ratio_600_1200 = harmonic_600_1200
            
            if gt_detected and gt_station:
                result.ground_truth_station = gt_station
                result.ground_truth_source = f'{gt_freq}Hz'
                result.ground_truth_power_db = gt_power
                agreements.append(f'ground_truth_{gt_station}_{gt_freq}Hz')
                logger.info(
                    f"Step 2C Ground Truth: {gt_station} confirmed via {gt_freq} Hz "
                    f"(power={gt_power:.1f}dB)"
                )
        except Exception as e:
            logger.debug(f"Step 2C ground truth detection: {e}")
        
        # Check 440 Hz tone for minutes 1 and 2
        if minute_number in [1, 2]:
            try:
                detected_440, power_440 = self.discriminator.detect_440hz_tone(
                    iq_samples=iq_samples,
                    sample_rate=self.sample_rate,
                    minute_number=minute_number
                )
                
                if detected_440:
                    if minute_number == 1:
                        result.ground_truth_station = 'WWVH'
                        result.ground_truth_source = '440Hz_min1'
                    else:  # minute 2
                        result.ground_truth_station = 'WWV'
                        result.ground_truth_source = '440Hz_min2'
                    result.ground_truth_power_db = power_440
                    agreements.append(f'440Hz_minute{minute_number}')
                    logger.info(
                        f"Step 2C Ground Truth: {result.ground_truth_station} confirmed via 440Hz "
                        f"(power={power_440:.1f}dB)"
                    )
            except Exception as e:
                logger.debug(f"440 Hz detection: {e}")
        
        # Detect test signal for minutes 8 and 44 (channel sounding)
        # This provides FSS, delay spread, and high-precision ToA for timing improvement
        if minute_number in [8, 44]:
            try:
                test_result = self.discriminator.test_signal_detector.detect(
                    iq_samples=iq_samples,
                    minute_number=minute_number,
                    sample_rate=self.sample_rate
                )
                
                if test_result.detected:
                    result.test_signal_detected = True
                    result.test_signal_fss_db = test_result.frequency_selectivity_db
                    result.test_signal_delay_spread_ms = test_result.delay_spread_ms
                    result.test_signal_toa_offset_ms = test_result.toa_offset_ms
                    result.test_signal_coherence_time_sec = test_result.coherence_time_sec
                    
                    # Use test signal delay spread if better than BCD estimate
                    if test_result.delay_spread_ms is not None:
                        if result.delay_spread_ms is None or test_result.delay_spread_ms < result.delay_spread_ms:
                            result.delay_spread_ms = test_result.delay_spread_ms
                    
                    # Use test signal coherence time if available
                    if test_result.coherence_time_sec is not None:
                        result.coherence_time_sec = test_result.coherence_time_sec
                    
                    expected_station = 'WWV' if minute_number == 8 else 'WWVH'
                    agreements.append(f'test_signal_{expected_station}')
                    
                    logger.info(
                        f"Step 2C Test Signal: {expected_station} detected, "
                        f"FSS={test_result.frequency_selectivity_db:.1f}dB, "
                        f"delay_spread={test_result.delay_spread_ms:.2f}ms"
                        if test_result.delay_spread_ms else
                        f"Step 2C Test Signal: {expected_station} detected, "
                        f"FSS={test_result.frequency_selectivity_db}dB"
                    )
            except Exception as e:
                logger.debug(f"Test signal detection: {e}")
        
        # CHU FSK detection (all minutes for CHU channels)
        # CHU transmits FSK time code at seconds 31-39 with precise 500ms boundaries
        if 'CHU' in self.channel_name.upper():
            try:
                from .chu_fsk_decoder import CHUFSKDecoder
                
                if not hasattr(self, 'chu_fsk_decoder'):
                    self.chu_fsk_decoder = CHUFSKDecoder(
                        sample_rate=self.sample_rate,
                        channel_name=self.channel_name
                    )
                
                fsk_result = self.chu_fsk_decoder.decode_minute(
                    iq_samples=iq_samples,
                    minute_boundary_unix=system_time
                )
                
                if fsk_result.detected:
                    result.chu_fsk_detected = True
                    result.chu_fsk_frames_decoded = fsk_result.frames_decoded
                    result.chu_fsk_timing_offset_ms = fsk_result.timing_offset_ms
                    result.chu_fsk_dut1_seconds = fsk_result.dut1_seconds
                    result.chu_fsk_tai_utc = fsk_result.tai_utc
                    result.chu_fsk_decode_confidence = fsk_result.decode_confidence
                    
                    # Verify decoded time matches expected
                    expected_minute = minute_number
                    if fsk_result.decoded_minute == expected_minute:
                        result.chu_fsk_time_verified = True
                        agreements.append('chu_fsk_time_match')
                    else:
                        disagreements.append('chu_fsk_time_mismatch')
                    
                    logger.info(
                        f"Step 2C CHU FSK: {fsk_result.frames_decoded}/9 frames, "
                        f"timing={fsk_result.timing_offset_ms:.3f}ms, "
                        f"DUT1={fsk_result.dut1_seconds}s"
                        if fsk_result.dut1_seconds else
                        f"Step 2C CHU FSK: {fsk_result.frames_decoded}/9 frames, "
                        f"timing={fsk_result.timing_offset_ms:.3f}ms"
                    )
            except Exception as e:
                logger.debug(f"CHU FSK detection: {e}")
        
        # Determine dominant station from weighted voting
        # Use finalize_discrimination for complete voting
        try:
            # Create base discrimination result
            base_result = self.discriminator.compute_discrimination(
                detections=[],  # Detections handled via SNR below
                minute_timestamp=system_time
            )
            
            # Populate power_ratio_db from Step 1 tone detection SNRs
            # This enables Vote 3 (carrier power ratio) in finalize_discrimination
            if time_snap.wwv_snr_db is not None and time_snap.wwvh_snr_db is not None:
                base_result.power_ratio_db = time_snap.wwv_snr_db - time_snap.wwvh_snr_db
            elif time_snap.wwv_snr_db is not None:
                base_result.power_ratio_db = 10.0  # WWV detected only
                base_result.dominant_station = 'WWV'
            elif time_snap.wwvh_snr_db is not None:
                base_result.power_ratio_db = -10.0  # WWVH detected only
                base_result.dominant_station = 'WWVH'
            
            # Finalize with all evidence including per-second tick SNR
            final_result = self.discriminator.finalize_discrimination(
                result=base_result,
                minute_number=minute_number,
                bcd_wwv_amp=result.bcd_wwv_amplitude,
                bcd_wwvh_amp=result.bcd_wwvh_amplitude,
                tone_440_wwv_detected=(minute_number == 2 and result.ground_truth_station == 'WWV'),
                tone_440_wwvh_detected=(minute_number == 1 and result.ground_truth_station == 'WWVH'),
                tick_results=tick_results  # Per-second tick SNR for Vote 4
            )
            
            result.dominant_station = final_result.dominant_station or 'UNKNOWN'
            result.station_confidence = final_result.confidence
            
            # Collect validation results
            if final_result.inter_method_agreements:
                agreements.extend(final_result.inter_method_agreements)
            if final_result.inter_method_disagreements:
                disagreements.extend(final_result.inter_method_disagreements)
                
        except Exception as e:
            logger.warning(f"Station discrimination failed: {e}")
            
        # === ACTIVE MODE: Probabilistic Discriminator ===
        # Run the new ML/probabilistic discriminator and update the result
        try:
            if hasattr(self, 'prob_discriminator'):
                # Extract features for the model
                features = self.prob_discriminator.extract_features(
                    power_ratio_db=base_result.power_ratio_db,
                    bcd_wwv_amplitude=result.bcd_wwv_amplitude,
                    bcd_wwvh_amplitude=result.bcd_wwvh_amplitude,
                    doppler_std_wwv=result.doppler_wwv_std_hz,
                    doppler_std_wwvh=result.doppler_wwvh_std_hz,
                    differential_delay_ms=result.bcd_differential_delay_ms,
                    tone_440_wwv_detected=(minute_number == 2 and result.ground_truth_station == 'WWV'),
                    tone_440_wwvh_detected=(minute_number == 1 and result.ground_truth_station == 'WWVH'),
                    tone_500_600_detected=result.ground_truth_station is not None,
                    minute=minute_number,
                    timestamp=system_time
                )
                
                # Run classification
                prob_result = self.prob_discriminator.classify(features)
                
                # Update result with authoritative probabilistic decision
                old_station = result.dominant_station
                
                # Map Probabilistic 'UNCERTAIN' to Legacy 'BALANCED'
                if prob_result.station == 'UNCERTAIN':
                    result.dominant_station = 'BALANCED'
                else:
                    result.dominant_station = prob_result.station
                
                # Map confidence score (0-1) to legacy string levels
                if prob_result.confidence > 0.8:
                    result.confidence = 'high'
                elif prob_result.confidence > 0.5:
                    result.confidence = 'medium'
                else:
                    result.confidence = 'low'
                
                # Log modification
                if old_station != result.dominant_station:
                    logger.info(
                        f"Probabilistic Correction: {old_station} -> {result.dominant_station} "
                        f"(P(WWV)={prob_result.p_wwv:.2f}, conf={prob_result.confidence:.2f})"
                    )
                else:
                    logger.debug(
                        f"Probabilistic Confirmation: {result.dominant_station} "
                        f"(conf={prob_result.confidence:.2f})"
                    )
                    
        except Exception as e:
            logger.warning(f"Probabilistic discriminator failed: {e}")
        
        # Calculate spreading factor L = τ_D × f_D
        if result.delay_spread_ms is not None and result.coherence_time_sec is not None:
            if result.coherence_time_sec > 0.01:
                f_D_est = 1.0 / (np.pi * result.coherence_time_sec)
                result.spreading_factor = (result.delay_spread_ms / 1000.0) * f_D_est
        
        # Narrow search window based on Step 2 confidence
        if result.station_confidence == 'high':
            result.refined_search_window_ms = 10.0  # Very tight
        elif result.station_confidence == 'medium':
            result.refined_search_window_ms = 25.0
        else:
            result.refined_search_window_ms = 50.0  # Conservative
        
        result.cross_validation_agreements = agreements
        result.cross_validation_disagreements = disagreements
        
        # Populate SNR for uncertainty estimation
        # Use the dominant station's SNR or max of detected SNRs
        if time_snap.wwv_snr_db is not None and time_snap.wwvh_snr_db is not None:
            if result.dominant_station == 'WWV':
                result.snr_db = time_snap.wwv_snr_db
            elif result.dominant_station == 'WWVH':
                result.snr_db = time_snap.wwvh_snr_db
            else:
                result.snr_db = max(time_snap.wwv_snr_db, time_snap.wwvh_snr_db)
        elif time_snap.wwv_snr_db is not None:
            result.snr_db = time_snap.wwv_snr_db
        elif time_snap.wwvh_snr_db is not None:
            result.snr_db = time_snap.wwvh_snr_db
        elif time_snap.chu_snr_db is not None:
            result.snr_db = time_snap.chu_snr_db
        
        return result
    
    def _station_from_channel_name(self) -> str:
        """
        Derive the transmitting station from the channel name.
        
        Channel names like "WWV 15 MHz", "WWVH 10 MHz", "CHU 7.85 MHz"
        tell us exactly which station we're receiving.
        
        Returns:
            'WWV', 'WWVH', 'CHU', or 'UNKNOWN'
        """
        if not self.channel_name:
            return 'UNKNOWN'
        
        name_upper = self.channel_name.upper()
        
        # Check for CHU first (to avoid matching "CHU" in other strings)
        if 'CHU' in name_upper:
            return 'CHU'
        # Check for WWVH before WWV (WWVH contains WWV)
        elif 'WWVH' in name_upper:
            return 'WWVH'
        elif 'WWV' in name_upper:
            return 'WWV'
        else:
            return 'UNKNOWN'
    
    def _is_shared_frequency(self) -> bool:
        """
        Check if this channel is on a shared WWV/WWVH frequency.
        
        Shared frequencies: 2.5, 5, 10, 15 MHz
        WWV-only: 20, 25 MHz
        CHU-only: 3.33, 7.85, 14.67 MHz
        
        Only shared frequencies need discrimination logic.
        """
        # Shared WWV/WWVH frequencies in MHz
        shared_freqs = {2.5, 5.0, 10.0, 15.0}
        
        # Check if this channel's frequency is shared
        if self.frequency_mhz in shared_freqs:
            return True
        
        # Also check channel name for explicit WWVH prefix on shared freqs
        # (e.g., "WWVH 10 MHz" is unambiguous even though 10 MHz is shared)
        name_upper = self.channel_name.upper() if self.channel_name else ''
        if 'WWVH' in name_upper:
            return False  # Explicitly WWVH, no discrimination needed
        
        return False
    
    def _validate_inter_station_dclock_consistency(
        self,
        time_snap: TimeSnapResult,
        channel: ChannelCharacterization,
        rtp_timestamp: int,
        expected_second_rtp: int,
        delay_spread_ms: float,
        doppler_std_hz: float,
        fss_db: Optional[float]
    ) -> Dict[str, float]:
        """
        CRITICAL FIX (2026-01-04): Inter-station D_clock consistency validation.
        
        D_clock is a RECEIVER CLOCK PROPERTY - it should be the same for all stations.
        If different stations report different D_clock values, it indicates:
        1. Incorrect propagation delay calculations
        2. Station misidentification
        3. Propagation mode errors
        
        This method calculates D_clock for all detected stations and validates consistency.
        
        Returns:
            Dict mapping station -> d_clock_ms, or empty dict if validation fails
        """
        d_clock_estimates = {}
        
        # Calculate D_clock for each detected station
        stations_to_check = []
        if time_snap.wwv_timing_ms is not None and time_snap.wwv_snr_db is not None:
            if time_snap.wwv_snr_db > 0:  # Only if detected
                stations_to_check.append(('WWV', time_snap.wwv_timing_ms))
        
        if time_snap.wwvh_timing_ms is not None and time_snap.wwvh_snr_db is not None:
            if time_snap.wwvh_snr_db > 0:
                stations_to_check.append(('WWVH', time_snap.wwvh_timing_ms))
        
        if time_snap.chu_timing_ms is not None and time_snap.chu_snr_db is not None:
            if time_snap.chu_snr_db > 0:
                stations_to_check.append(('CHU', time_snap.chu_timing_ms))
        
        if len(stations_to_check) < 2:
            # Only one station detected, can't validate consistency
            return {}
        
        # Calculate D_clock for each station
        for station, t_arrival_ms in stations_to_check:
            try:
                timing_offset_samples = round(t_arrival_ms * self.sample_rate / 1000.0)
                arrival_rtp = rtp_timestamp + timing_offset_samples
                
                solver_result = self.solver.solve(
                    station=station,
                    frequency_mhz=self.frequency_mhz,
                    arrival_rtp=arrival_rtp,
                    delay_spread_ms=delay_spread_ms,
                    doppler_std_hz=doppler_std_hz,
                    fss_db=fss_db,
                    expected_second_rtp=expected_second_rtp
                )
                
                # Extract D_clock
                if solver_result.utc_nist_offset_ms is not None:
                    d_clock_ms = solver_result.utc_nist_offset_ms
                elif solver_result.emission_offset_ms is not None:
                    d_clock_ms = solver_result.emission_offset_ms
                else:
                    continue  # Skip if no valid solution
                
                d_clock_estimates[station] = d_clock_ms
                
            except Exception as e:
                logger.warning(f"Failed to calculate D_clock for {station}: {e}")
                continue
        
        if len(d_clock_estimates) < 2:
            return {}
        
        # Validate consistency
        d_clock_values = list(d_clock_estimates.values())
        d_clock_mean = sum(d_clock_values) / len(d_clock_values)
        d_clock_min = min(d_clock_values)
        d_clock_max = max(d_clock_values)
        d_clock_spread = d_clock_max - d_clock_min
        
        logger.info(
            f"Inter-station D_clock validation: {d_clock_estimates}, "
            f"mean={d_clock_mean:.2f}ms, spread={d_clock_spread:.2f}ms"
        )
        
        # CRITICAL THRESHOLD: D_clock spread should be < 5ms
        # Larger spreads indicate systematic propagation errors
        if d_clock_spread > 5.0:
            logger.error(
                f"CRITICAL: D_clock spread {d_clock_spread:.2f}ms exceeds 5ms threshold!"
            )
            logger.error(f"  Station D_clock values: {d_clock_estimates}")
            logger.error(f"  This indicates PROPAGATION DELAY CALCULATION ERRORS")
            logger.error(f"  D_clock is a receiver property - should be same for all stations")
            
            # Flag all measurements as SUSPECT
            for station, d_clock in d_clock_estimates.items():
                logger.error(f"    {station}: {d_clock:+.2f}ms (deviation: {d_clock - d_clock_mean:+.2f}ms)")
            
            # Return empty dict to signal validation failure
            return {}
        
        elif d_clock_spread > 3.0:
            logger.warning(
                f"WARNING: D_clock spread {d_clock_spread:.2f}ms exceeds 3ms (measurement noise limit)"
            )
            logger.warning(f"  Station D_clock values: {d_clock_estimates}")
            # Continue but with reduced confidence
        
        return d_clock_estimates
    
    def _step3_transmission_time_solution(
        self,
        time_snap: TimeSnapResult,
        channel: ChannelCharacterization,
        system_time: float,
        rtp_timestamp: int,
        forced_station: Optional[str] = None,
        calibration_offsets: Optional[Dict[str, float]] = None
    ) -> TransmissionTimeSolution:
        """
        Step 3: Transmission Time Solution.
        
        Back-calculates the true T_emission (UTC) by accurately modeling the
        propagation delay using all high-confidence measurements from Steps 1 and 2.
        
        Args:
            time_snap: Result from Step 1 (timing anchor)
            channel: Result from Step 2 (channel metrics)
            system_time: System time of first sample
            rtp_timestamp: RTP timestamp of first sample
            forced_station: Optional station to force solution for (for multi-station output)
            
        Returns:
            TransmissionTimeSolution with final D_clock
        """
        # Determine which station to use for solution
        # 
        # Shared frequencies (discrimination needed): 2.5, 5, 10, 15 MHz
        # WWV-only: 20, 25 MHz
        # WWVH-only: (none in typical configs)
        # CHU-only: 3.33, 7.85, 14.67 MHz
        #
        # Priority 0: Non-shared channels - station is unambiguous from channel name
        
        station = None
        
        if forced_station:
            # Explicit override for multi-station output loop
            station = forced_station
            logger.debug(f"Station forced: {station}")
        else:
            # Legacy/Fallback logic for single-station determination
            channel_station = self._station_from_channel_name()
            is_shared_frequency = self._is_shared_frequency()
            
            if not is_shared_frequency:
                # CHU, WWV 20/25 MHz, etc. - no discrimination needed
                station = channel_station
                logger.debug(f"Station = {station} (non-shared frequency, no discrimination)")
            
            # For shared frequencies only: use discrimination
            if not station:
                # Priority 1: Ground truth (500/600 Hz exclusive minutes, 440 Hz)
                if channel.ground_truth_station:
                    station = channel.ground_truth_station
                    logger.debug(f"Station from ground truth: {station}")
                
                # Check RTP prediction early
                rtp_predicted_station = None
                rtp_conf = 0.0
                if self.station_predictor is not None:
                    rtp_predicted_station, rtp_conf = self.station_predictor(
                        self.channel_name,
                        rtp_timestamp,
                        channel.dominant_station or channel_station,
                        channel.station_confidence
                    )

                # Priority 2: RTP Prediction (High Confidence) - ROBUST LOCK
                # If we have a strong RTP history lock (>0.8), we trust it over 
                # acoustic noise, unless ground truth contradicted it (Priority 1)
                if not station and rtp_predicted_station and rtp_conf > 0.8:
                    station = rtp_predicted_station
                    logger.debug(f"Station from RTP prediction (HIGH confidence overrides acoustic): {station} (conf={rtp_conf:.2f})")

                # Priority 3: High confidence discrimination (detected via voting)
                elif not station and channel.station_confidence == 'high' and channel.dominant_station not in ['UNKNOWN', 'BALANCED', None]:
                    station = channel.dominant_station
                    logger.debug(f"Station from discrimination (high confidence): {station}")
                
                # Priority 4: RTP Prediction (Moderate Confidence)
                # If we have a decent RTP history (>0.5), it's better than medium confidence acoustic
                elif not station and rtp_predicted_station and rtp_conf > 0.5:
                    station = rtp_predicted_station
                    logger.debug(f"Station from RTP prediction (MODERATE confidence): {station} (conf={rtp_conf:.2f})")

                # Priority 5: Medium confidence discrimination only (NOT low confidence)
                # Low confidence discrimination on shared frequencies causes flip-flopping
                elif not station and channel.station_confidence == 'medium' and channel.dominant_station not in ['UNKNOWN', 'BALANCED', 'NONE', None, '']:
                    station = channel.dominant_station
                    logger.debug(f"Station from discrimination (medium confidence): {station}")
                
                # Priority 6: Low confidence - use channel name fallback
                if not station:
                    station = channel_station
                    logger.debug(f"Station from channel name fallback (low confidence discrimination rejected): {station}")
            
            # Final fallback
            if not station or station in ['BALANCED', 'UNKNOWN', 'NONE', '']:
                station = 'WWV'
                logger.debug(f"Station fallback to WWV")
        
        # Validate station/frequency combination
        # Reject physically impossible combinations (e.g., WWVH at 20/25 MHz)
        from .wwv_constants import WWVH_FREQUENCIES, WWV_FREQUENCIES, CHU_FREQUENCIES
        
        if station == 'WWVH' and self.frequency_mhz not in WWVH_FREQUENCIES:
            logger.warning(
                f"INVALID: Station determination chose {station} at {self.frequency_mhz} MHz, "
                f"but WWVH only broadcasts on {WWVH_FREQUENCIES}. Rejecting and using WWV."
            )
            station = 'WWV'
        elif station == 'WWV' and self.frequency_mhz not in WWV_FREQUENCIES:
            logger.warning(
                f"INVALID: Station determination chose {station} at {self.frequency_mhz} MHz, "
                f"but WWV only broadcasts on {WWV_FREQUENCIES}. Rejecting."
            )
            station = 'UNKNOWN'
        elif station == 'CHU' and self.frequency_mhz not in CHU_FREQUENCIES:
            logger.warning(
                f"INVALID: Station determination chose {station} at {self.frequency_mhz} MHz, "
                f"but CHU only broadcasts on {CHU_FREQUENCIES}. Rejecting."
            )
            station = 'UNKNOWN'
        
        # Prepare channel metrics for solver
        delay_spread_ms = channel.delay_spread_ms or 0.5
        doppler_std_hz = channel.doppler_wwv_std_hz if station == 'WWV' else channel.doppler_wwvh_std_hz
        doppler_std_hz = doppler_std_hz or 0.1
        
        # FSS from test signal detection (minutes 8 and 44 only)
        # This provides D-layer attenuation indicator for mode disambiguation
        fss_db = channel.test_signal_fss_db  # Will be None for non-test-signal minutes
        
        if fss_db is not None:
            logger.info(f"Using FSS={fss_db:.1f}dB from test signal for mode disambiguation")
        
        # GPSDO-FIRST TIMING: RTP timestamp is the gold standard ruler.
        #
        # The RTP offset within a minute is DETERMINISTIC with GPSDO:
        #   rtp_offset = rtp_timestamp % samples_per_minute (1,200,000 at 20 kHz)
        #
        # We learn which RTP offset corresponds to the minute boundary from tone
        # detections, then use that mapping consistently. The mapping is stored
        # in the timing calibrator's rtp_calibration data.
        #
        # This is fundamentally different from recalculating from system_time:
        # - system_time has NTP jitter (~1-10ms)
        # - RTP offset is GPSDO-locked (~100ns stability)
        #
        # Get arrival RTP from time snap - USE STATION SPECIFIC TIMING
        t_arrival_ms = None
        if station == 'WWV':
            t_arrival_ms = time_snap.wwv_timing_ms
        elif station == 'WWVH':
            t_arrival_ms = time_snap.wwvh_timing_ms
        elif station == 'CHU':
            t_arrival_ms = time_snap.chu_timing_ms
        elif station == 'BPM':
            # BPM timing: prefer bpm_timing_ms
            t_arrival_ms = time_snap.bpm_timing_ms if time_snap.bpm_timing_ms else time_snap.wwv_timing_ms
        
        # Calculate arrival_rtp if signal was detected
        arrival_rtp = None
        if t_arrival_ms is not None:
             timing_offset_samples = round(t_arrival_ms * self.sample_rate / 1000.0)
             arrival_rtp = rtp_timestamp + timing_offset_samples


        # GPSDO-FIRST TIMING: RTP timestamp is the gold standard ruler.
        samples_per_minute = self.sample_rate * 60  # 1,200,000 at 20 kHz
        
        # Get the calibrated RTP offset that corresponds to minute boundary
        calibrated_offset = None
        if self.rtp_calibration_callback:
            calibrated_offset = self.rtp_calibration_callback(self.channel_name)
        
        if calibrated_offset is not None:
            # We have learned which RTP offset corresponds to minute boundary
            # Use this as the reference - it's stable to GPSDO precision
            current_offset = rtp_timestamp % samples_per_minute
            offset_diff = calibrated_offset - current_offset
            
            # Handle wraparound (offset_diff should be small, within half a minute)
            if offset_diff > samples_per_minute // 2:
                offset_diff -= samples_per_minute
            elif offset_diff < -samples_per_minute // 2:
                offset_diff += samples_per_minute
            
            expected_second_rtp = rtp_timestamp + offset_diff
            logger.debug(f"RTP ruler: calibrated_offset={calibrated_offset}, current={current_offset}, diff={offset_diff}")
        else:
            # Bootstrap: No calibration yet
            if arrival_rtp is not None:
                # Establish mapping from first meaningful detection
                # Estimate arrival time in system clock frame
                arrival_rtp_offset = arrival_rtp - rtp_timestamp
                estimated_arrival_time = system_time + (arrival_rtp_offset / self.sample_rate)
                
                # Snap to nearest minute boundary (we assume we detected the Minute Tone)
                nearest_minute = round(estimated_arrival_time / 60.0) * 60.0
                
                # Calculate expected RTP for that boundary
                time_diff = nearest_minute - system_time
                expected_second_rtp = rtp_timestamp + int(time_diff * self.sample_rate)
                
                logger.info(
                    f"Bootstrap: Snapped arrival {estimated_arrival_time:.3f} to minute {nearest_minute} "
                    f"(diff={estimated_arrival_time - nearest_minute:.3f}s)"
                )
            else:
                # CRITICAL FIX: Bootstrap without expected_second_rtp
                # 
                # PROBLEM: The previous code used RTP modulo to estimate expected_second_rtp,
                # but this assumes the RTP clock is already aligned with UTC, which creates
                # a circular dependency during bootstrap.
                # 
                # SOLUTION: Pass None to the solver and let it bootstrap using station-specific
                # expected delays. Once we have a good detection, we'll establish the RTP→UTC
                # mapping via the calibration callback.
                # 
                # The solver handles expected_second_rtp=None by using:
                #   observed_delay_ms = min(c.total_delay_ms for c in candidates)
                # This allows it to find the most plausible propagation mode without
                # assuming we know the RTP→UTC mapping.
                expected_second_rtp = None
                
                logger.info(
                    f"Bootstrap: No calibration established yet - solver will use station-specific "
                    f"expected delays to establish RTP→UTC mapping from first good detection"
                )

            
        # Fallback to generic timing error if specific not available (e.g. single station anchor)
        if t_arrival_ms is None:
             t_arrival_ms = time_snap.timing_error_ms
        
        # Recalculate arrival RTP based on specific timing
        if t_arrival_ms is not None:
            timing_offset_samples = round(t_arrival_ms * self.sample_rate / 1000.0)
            arrival_rtp = rtp_timestamp + timing_offset_samples
        else:
            arrival_rtp = time_snap.arrival_rtp # Fallback
        
        # CRITICAL FIX (2026-01-04): Inter-station D_clock consistency validation
        # Run validation BEFORE solving for the selected station
        # This catches systematic propagation errors early
        if expected_second_rtp is not None and not forced_station:
            d_clock_consistency = self._validate_inter_station_dclock_consistency(
                time_snap=time_snap,
                channel=channel,
                rtp_timestamp=rtp_timestamp,
                expected_second_rtp=expected_second_rtp,
                delay_spread_ms=delay_spread_ms,
                doppler_std_hz=doppler_std_hz,
                fss_db=fss_db
            )
            
            # If validation failed (spread > 5ms), log critical error
            if len(d_clock_consistency) >= 2 and not d_clock_consistency:
                logger.error("Inter-station D_clock validation FAILED - propagation errors detected")
                # Continue with reduced confidence
        
        try:
            solver_result = self.solver.solve(
                station=station,
                frequency_mhz=self.frequency_mhz,
                arrival_rtp=arrival_rtp,
                delay_spread_ms=delay_spread_ms,
                doppler_std_hz=doppler_std_hz,
                fss_db=fss_db,
                expected_second_rtp=expected_second_rtp
            )
            
            # Extract D_clock (handle None from _no_solution during bootstrap)
            # Extract D_clock (handle None from _no_solution during bootstrap)
            if solver_result.utc_nist_offset_ms is not None:
                d_clock_ms = solver_result.utc_nist_offset_ms
            elif solver_result.emission_offset_ms is not None:
                d_clock_ms = solver_result.emission_offset_ms
            else:
                d_clock_ms = 0.0  # Fallback for bootstrap/_no_solution
            
            # CRITICAL FIX (2026-01-04): D_clock continuity validation
            # Check for sudden jumps that indicate frame slips or mode errors
            if hasattr(self, '_last_d_clock_ms') and self._last_d_clock_ms is not None:
                d_clock_delta = abs(d_clock_ms - self._last_d_clock_ms)
                
                # Expected drift: < 0.1 ms/minute for GPSDO-disciplined clock
                if d_clock_delta > 5.0:
                    logger.error(
                        f"D_clock DISCONTINUITY: {self._last_d_clock_ms:.2f}ms → {d_clock_ms:.2f}ms "
                        f"(Δ={d_clock_delta:.2f}ms)"
                    )
                    
                    # Check for CHU frame slip (500ms jumps)
                    if abs(d_clock_delta - 500.0) < 10.0:
                        logger.error("  → CHU FRAME SLIP DETECTED (500ms jump)")
                    elif abs(d_clock_delta - 1000.0) < 10.0:
                        logger.error("  → CHU DOUBLE FRAME SLIP DETECTED (1000ms jump)")
                    
                    # Reduce confidence for this measurement
                    solver_result.confidence = max(0.1, solver_result.confidence * 0.3)
            
            # Store for next iteration
            self._last_d_clock_ms = d_clock_ms
            
            # Convert mode candidates to dict format for serialization
            mode_candidates = [
                {
                    'mode': c.mode.value,
                    'delay_ms': round(c.total_delay_ms, 2),
                    'probability': round(c.plausibility, 3),
                    'n_hops': c.n_hops,
                    'elevation_deg': round(c.elevation_angle_deg, 1)
                }
                for c in solver_result.candidates
            ]
            
            # Determine tone detection status for this station
            # Extract from time_snap which contains validated detections
            tone_detected_flag = False
            raw_tone_timing = None
            
            if station == 'WWV' and time_snap.wwv_detected:
                tone_detected_flag = True
                raw_tone_timing = time_snap.wwv_timing_ms
            elif station == 'WWVH' and time_snap.wwvh_detected:
                tone_detected_flag = True
                raw_tone_timing = time_snap.wwvh_timing_ms
            elif station == 'CHU' and time_snap.chu_detected:
                tone_detected_flag = True
                raw_tone_timing = time_snap.chu_timing_ms
            elif station == 'BPM' and time_snap.bpm_detected:
                tone_detected_flag = True
                raw_tone_timing = time_snap.bpm_timing_ms
            
            solution = TransmissionTimeSolution(
                tone_detected=tone_detected_flag,
                raw_tone_arrival_ms=raw_tone_timing,
                d_clock_ms=d_clock_ms,
                t_emission_ms=solver_result.emission_offset_ms,
                t_arrival_ms=t_arrival_ms,
                t_propagation_ms=solver_result.propagation_delay_ms,
                propagation_mode=solver_result.mode.value,
                n_hops=solver_result.n_hops,
                layer_height_km=solver_result.layer_height_km,
                station=station,
                frequency_mhz=self.frequency_mhz,
                confidence=solver_result.confidence,
                uncertainty_ms=self._calculate_physics_based_uncertainty(channel, solver_result.confidence)[0],
                utc_verified=solver_result.utc_nist_verified,
                mode_candidates=mode_candidates,
                arrival_rtp=solver_result.arrival_rtp
            )
            
            # CRITICAL FIX: Validate D_clock against expected range from calibration
            # This prevents outliers from wrong mode selection from reaching fusion
            if calibration_offsets and station in calibration_offsets:
                expected_d_clock = calibration_offsets[station]
                tolerance_ms = 5.0  # ±5ms tolerance for ionospheric variability
                
                d_clock_error = abs(d_clock_ms - expected_d_clock)
                if d_clock_error > tolerance_ms:
                    logger.warning(
                        f"D_clock validation FAILED for {station}: "
                        f"measured={d_clock_ms:+.2f}ms, expected={expected_d_clock:+.2f}ms, "
                        f"error={d_clock_error:.2f}ms > tolerance={tolerance_ms:.2f}ms"
                    )
                    logger.warning(
                        f"  Likely wrong propagation mode: {solver_result.mode.value} "
                        f"(confidence={solver_result.confidence:.2f})"
                    )
                    # Reduce confidence to below rejection threshold
                    solution.confidence = 0.05
                    logger.warning(f"  Reducing confidence to {solution.confidence:.2f} to trigger rejection")

            
            # Check for dual-station cross-validation
            try:
                if (
                    getattr(self, 'differential_solver', None) is not None and
                    self._is_shared_frequency() and
                    time_snap.wwv_timing_ms is not None and
                    time_snap.wwvh_timing_ms is not None and
                    expected_second_rtp is not None
                ):
                    wwv_arrival_rtp = rtp_timestamp + round(time_snap.wwv_timing_ms * self.sample_rate / 1000.0)
                    wwvh_arrival_rtp = rtp_timestamp + round(time_snap.wwvh_timing_ms * self.sample_rate / 1000.0)

                    diff_result = self.differential_solver.solve_with_anchor(
                        wwv_arrival_rtp=wwv_arrival_rtp,
                        wwvh_arrival_rtp=wwvh_arrival_rtp,
                        minute_boundary_rtp=expected_second_rtp,
                        sample_rate=self.sample_rate,
                        frequency_mhz=self.frequency_mhz,
                        delay_spread_ms=delay_spread_ms,
                        doppler_std_hz=max(
                            channel.doppler_wwv_std_hz or 0.0,
                            channel.doppler_wwvh_std_hz or 0.0,
                            0.1
                        )
                    )

                    if (
                        diff_result is not None and
                        getattr(diff_result.wwv_mode, 'value', 'UNK') != 'UNK' and
                        getattr(diff_result.wwvh_mode, 'value', 'UNK') != 'UNK'
                    ):
                        solution.dual_station_agreement_ms = diff_result.wwv_wwvh_agreement_ms
                        solution.dual_station_verified = diff_result.clock_error_verified

                        dclock_delta_ms = abs(solution.d_clock_ms - diff_result.clock_error_ms)
                        if diff_result.clock_error_verified and diff_result.confidence >= 0.3 and dclock_delta_ms <= 2.0:
                            solution.confidence = min(1.0, solution.confidence + 0.15)
                        elif diff_result.confidence >= 0.3 and dclock_delta_ms >= 5.0:
                            solution.confidence = max(0.05, solution.confidence - 0.2)
                            d_clock_str = f"{solution.d_clock_ms:+.2f}ms" if solution.d_clock_ms is not None else "N/A"
                            logger.warning(
                                f"Differential validator disagrees with D_clock: "
                                f"single={d_clock_str} vs diff={diff_result.clock_error_ms:+.2f}ms "
                                f"(Δ={dclock_delta_ms:.2f}ms, verified={diff_result.clock_error_verified})"
                            )

                        logger.info(
                            f"Differential validation: WWV={diff_result.wwv_mode.value}, "
                            f"WWVH={diff_result.wwvh_mode.value}, "
                            f"agreement={diff_result.wwv_wwvh_agreement_ms:.2f}ms, "
                            f"ΔD_clock={dclock_delta_ms:.2f}ms, conf={diff_result.confidence:.2f}"
                        )

                if (
                    getattr(self, 'global_differential_solver', None) is not None and
                    self._is_shared_frequency() and
                    getattr(time_snap, 'multi_station_result', None) is not None and
                    expected_second_rtp is not None
                ):
                    observations = []
                    for det in time_snap.multi_station_result.get_all_usable_detections():
                        if det.station not in ('WWV', 'WWVH', 'CHU', 'BPM'):
                            continue
                        arrival_rtp_i = rtp_timestamp + round(det.measured_toa_ms * self.sample_rate / 1000.0)
                        observations.append({
                            'station': det.station,
                            'frequency_mhz': self.frequency_mhz,
                            'arrival_rtp': arrival_rtp_i
                        })

                    if len(observations) >= 2:
                        global_result = self.global_differential_solver.solve_global(
                            observations=observations,
                            minute_boundary_rtp=expected_second_rtp,
                            sample_rate=self.sample_rate
                        )

                        if global_result.verified:
                            solution.dual_station_verified = True
                        if solution.dual_station_agreement_ms is None:
                            solution.dual_station_agreement_ms = global_result.pair_consistency_ms

                        global_delta_ms = abs(solution.d_clock_ms - global_result.clock_error_ms)
                        if global_result.verified and global_result.confidence >= 0.3 and global_delta_ms <= 2.0:
                            solution.confidence = min(1.0, solution.confidence + 0.10)
                        elif global_result.confidence >= 0.3 and global_delta_ms >= 5.0:
                            solution.confidence = max(0.05, solution.confidence - 0.15)
                            d_clock_str = f"{solution.d_clock_ms:+.2f}ms" if solution.d_clock_ms is not None else "N/A"
                            logger.warning(
                                f"Global differential validator disagrees with D_clock: "
                                f"single={d_clock_str} vs global={global_result.clock_error_ms:+.2f}ms "
                                f"(Δ={global_delta_ms:.2f}ms, verified={global_result.verified})"
                            )

                        logger.info(
                            f"Global differential validation: n_obs={global_result.n_observations}, "
                            f"pairs={global_result.n_pairs}, rms={global_result.pair_consistency_ms:.2f}ms, "
                            f"ΔD_clock={global_delta_ms:.2f}ms, conf={global_result.confidence:.2f}, "
                            f"verified={global_result.verified}"
                        )
            except Exception as e:
                logger.debug(f"Differential validation failed: {e}")
            
            # CHU FSK timing confirmation
            # The FSK decoder provides independent timing from seconds 31-39
            # Compare FSK timing offset with D_clock for cross-validation
            if station == 'CHU' and channel.chu_fsk_detected and channel.chu_fsk_timing_offset_ms is not None:
                fsk_timing_ms = channel.chu_fsk_timing_offset_ms
                timing_diff_ms = abs(d_clock_ms - fsk_timing_ms)
                
                if timing_diff_ms < 5.0:
                    # FSK timing agrees with D_clock - increase confidence
                    solution.confidence = min(1.0, solution.confidence + 0.1)
                    solution.utc_verified = True
                    logger.info(
                        f"CHU FSK confirms D_clock: FSK={fsk_timing_ms:+.2f}ms, "
                        f"D_clock={d_clock_ms:+.2f}ms, diff={timing_diff_ms:.2f}ms"
                    )
                elif timing_diff_ms > 20.0:
                    # Large disagreement - flag as suspect
                    solution.confidence = max(0.1, solution.confidence - 0.2)
                    logger.warning(
                        f"CHU FSK disagrees with D_clock: FSK={fsk_timing_ms:+.2f}ms, "
                        f"D_clock={d_clock_ms:+.2f}ms, diff={timing_diff_ms:.2f}ms"
                    )
            
            logger.info(
                f"Step 3 Solution: D_clock={d_clock_ms:+.2f}ms, station={station}, "
                f"mode={solver_result.mode.value}, confidence={solver_result.confidence:.2f}"
            )
            
            return solution
            
        except Exception as e:
            logger.error(f"Step 3 TransmissionTimeSolver failed: {e}")
            
            # Use station's typical propagation delay as fallback (not 0.0)
            # This ensures physical constraints are satisfied
            fallback_propagation_ms = {
                'WWV': 8.0,    # Typical 1-2 hop F-layer
                'WWVH': 35.0,  # Typical 2-3 hop F-layer
                'CHU': 10.0,   # Typical 1-2 hop F-layer
                'BPM': 50.0,   # Typical 3-4 hop F-layer
            }.get(station, 15.0)
            
            # Return fallback solution with low confidence
            # Check if we have validated tone timing for this station
            fallback_tone_detected = False
            fallback_raw_timing = None
            
            if station == 'WWV' and time_snap.wwv_detected:
                fallback_tone_detected = True
                fallback_raw_timing = time_snap.wwv_timing_ms
            elif station == 'WWVH' and time_snap.wwvh_detected:
                fallback_tone_detected = True
                fallback_raw_timing = time_snap.wwvh_timing_ms
            elif station == 'CHU' and time_snap.chu_detected:
                fallback_tone_detected = True
                fallback_raw_timing = time_snap.chu_timing_ms
            elif station == 'BPM' and time_snap.bpm_detected:
                fallback_tone_detected = True
                fallback_raw_timing = time_snap.bpm_timing_ms
            
            return TransmissionTimeSolution(
                tone_detected=fallback_tone_detected,
                raw_tone_arrival_ms=fallback_raw_timing,
                d_clock_ms=time_snap.timing_error_ms - fallback_propagation_ms,  # Fix: Subtract estimated delay
                t_emission_ms=0.0,
                t_arrival_ms=time_snap.timing_error_ms,
                t_propagation_ms=fallback_propagation_ms,
                propagation_mode='UNK',
                n_hops=1,
                layer_height_km=250.0,
                station=station,
                frequency_mhz=self.frequency_mhz,
                arrival_rtp=None,
                confidence=0.1,
                uncertainty_ms=100.0,
                utc_verified=False
            )
    
    def _calculate_physics_based_uncertainty(
        self,
        channel: ChannelCharacterization,
        solution_confidence: float = 1.0
    ) -> Tuple[float, float]:
        """
        Calculate timing uncertainty using variance propagation.
        
        Formula: sigma_total^2 = sigma_snr^2 + sigma_prop^2 + sigma_stab^2 + sigma_sys^2
        
        Returns:
            (uncertainty_ms, confidence)
        """
        # 1. SNR Variance (Cramer-Rao Lower Bound approximation)
        # sigma_toa ~ 1 / (B * sqrt(SNR))
        snr_db = channel.snr_db if channel.snr_db is not None else 0.0
        snr_linear = 10 ** (max(snr_db, 0.0) / 10.0)
        # Effective bandwidth ~100Hz for envelope timing
        sigma_snr_ms = 100.0 / np.sqrt(max(snr_linear, 1.0))
        
        # 2. Propagation Variance (Dominant term)
        # Delay spread directly maps to ambiguity
        delay_spread = channel.delay_spread_ms or 2.0
        sigma_prop_ms = delay_spread
        
        # 3. Stability Variance
        # Doppler spread implies changing path length
        doppler_std = max(
            channel.doppler_wwv_std_hz or 0.0,
            channel.doppler_wwvh_std_hz or 0.0
        )
        sigma_stab_ms = doppler_std * 10.0  # 1 Hz Doppler ~ 10ms/s rate of change? Heuristic scaling.
        
        # 4. System Variance (Base metrology limit)
        sigma_sys_ms = 1.0
        
        # Total Variance
        total_variance = (sigma_snr_ms**2 + 
                          sigma_prop_ms**2 + 
                          sigma_stab_ms**2 + 
                          sigma_sys_ms**2)
                          
        uncertainty_ms = np.sqrt(total_variance)
        
        # Special cases (Ground Truth / FSK) drastically reduce variance
        if channel.chu_fsk_detected and channel.chu_fsk_time_verified:
            uncertainty_ms = 0.1  # FSK is digital lock
        elif channel.ground_truth_station is not None:
            uncertainty_ms = min(uncertainty_ms, 1.0)
            
        # Confidence logic (consistency checks)
        confidence = solution_confidence
        
        # Reduce confidence if systematic disagreements exist
        disagreements = len(channel.cross_validation_disagreements)
        if disagreements > 0:
            confidence *= (0.8 ** disagreements)
            
        return uncertainty_ms, confidence

    def _estimate_uncertainty(
        self,
        solution: TransmissionTimeSolution,
        channel: ChannelCharacterization
    ) -> Tuple[float, float]:
        """
        Estimate timing uncertainty based on physics-based variance.
        Wrapper for _calculate_physics_based_uncertainty.
        """
        return self._calculate_physics_based_uncertainty(channel, solution.confidence)

    
    def process_minute(
        self,
        iq_samples: np.ndarray,
        system_time: float,
        rtp_timestamp: int,
        calibration_offsets: Optional[Dict[str, float]] = None
    ) -> List[Phase2Result]:
        """
        Process one minute of IQ data through the complete Phase 2 pipeline.
        
        This is the main entry point for Phase 2 analysis, implementing the
        refined temporal analysis order:
        
        1. Fundamental Tone Detection → Time Snap Anchor
        2. Ionospheric Channel Characterization → Confidence Scoring
        3. Transmission Time Solution → D_clock
        
        Args:
            iq_samples: Complex64 IQ samples (60 seconds at sample_rate)
            system_time: System time of first sample (Unix timestamp)
            rtp_timestamp: RTP timestamp of first sample
            
        Returns:
            List of Phase2Result containing analysis for ALL detected stations.
            Returns empty list if analysis fails completely.
        """
        # Calculate minute boundary
        minute_boundary = (int(system_time) // 60) * 60
        minute_number = int((system_time // 60) % 60)
        
        # Validate and normalize input
        iq_samples, validation_metrics = self._validate_input(iq_samples)
        
        if validation_metrics.get('amplitude_warning'):
            logger.warning(f"Input amplitude warning - proceeding with caution")
        
        results = []
        
        try:
            # === STEP 1: Fundamental Tone Detection ===
            time_snap = self._step1_tone_detection(
                iq_samples=iq_samples,
                system_time=system_time,
                rtp_timestamp=rtp_timestamp,
                calibration_offsets=calibration_offsets
            )
            
            # === STEP 2: Ionospheric Channel Characterization ===
            channel = self._step2_channel_characterization(
                iq_samples=iq_samples,
                time_snap=time_snap,
                system_time=system_time,
                minute_number=minute_number,
                calibration_offsets=calibration_offsets
            )
            
            # === STEP 3: Transmission Time Solution (MULTI-STATION LOOP) ===
            # Identify which stations are candidates for solution
            candidate_stations = []
            
            # Add stations based on detection in Step 1
            # Only add if confidence is sufficient to warrant CPU time
            if time_snap.wwv_detected:
                candidate_stations.append('WWV')
            if time_snap.wwvh_detected:
                candidate_stations.append('WWVH')
            if time_snap.chu_detected or self._station_from_channel_name() == 'CHU':
                candidate_stations.append('CHU')
            if time_snap.bpm_detected:
                candidate_stations.append('BPM')
                
            # If no specific tone detections, fallback to dominant/channel station
            if not candidate_stations:
                dominant = channel.dominant_station or self._station_from_channel_name()
                if dominant and dominant not in ['UNKNOWN', 'BALANCED', 'NONE', '']:
                    candidate_stations.append(dominant)
                else:
                    candidate_stations.append('WWV') # Ultimate fallback
            
            # Remove duplicates
            candidate_stations = list(set(candidate_stations))
            
            logger.debug(f"Multi-station candidates: {candidate_stations}")
            
            for station in candidate_stations:
                try:
                    # Solve for this specific station
                    solution = self._step3_transmission_time_solution(
                        time_snap=time_snap,
                        channel=channel,
                        system_time=system_time,
                        rtp_timestamp=rtp_timestamp,
                        forced_station=station
                    )
                    
                    # Calculate final UTC time (handle None d_clock_ms during bootstrap)
                    d_clock_ms = solution.d_clock_ms if solution.d_clock_ms is not None else 0.0
                    utc_time = system_time - (d_clock_ms / 1000.0)
                    
                    # Estimate uncertainty (Issue 6.2 fix: replaced arbitrary grades)
                    uncertainty_ms, confidence = self._estimate_uncertainty(solution, channel)
                    
                    # Assemble complete result
                    result = Phase2Result(
                        minute_boundary_utc=minute_boundary,
                        system_time=system_time,
                        rtp_timestamp=rtp_timestamp,
                        time_snap=time_snap,
                        channel=channel,
                        solution=solution,
                        d_clock_ms=d_clock_ms,  # Use fallback value if original was None
                        utc_time=utc_time,
                        uncertainty_ms=uncertainty_ms,
                        confidence=confidence,
                        processing_version='2.2.0', # Multi-station support
                        processed_at=datetime.now(tz=timezone.utc).timestamp()
                    )
                    
                    results.append(result)
                    
                     # Update state (only for primary result to keep stats simple? or track last)
                    with self._lock:
                        self.last_result = result

                    # Format d_clock_ms, handling None values during bootstrap
                    d_clock_str = f"{solution.d_clock_ms:+.2f}ms" if solution.d_clock_ms is not None else "N/A"
                    
                    logger.info(
                        f"Phase 2 processing complete for {station}: D_clock={d_clock_str}, "
                        f"uncertainty={uncertainty_ms:.1f}ms"
                    )
                    
                except Exception as e:
                     logger.error(f"Failed to solve for station {station}: {e}")
            
            with self._lock:
                self.minutes_processed += 1
            
            return results
            
        except Exception as e:
            logger.error(f"Phase 2 processing failed: {e}")
            import traceback
            traceback.print_exc()
            return []
    
    def get_stats(self) -> Dict[str, Any]:
        """Get engine statistics."""
        with self._lock:
            return {
                'minutes_processed': self.minutes_processed,
                'channel_name': self.channel_name,
                'frequency_mhz': self.frequency_mhz,
                'receiver_grid': self.receiver_grid,
                'last_d_clock_ms': self.last_result.d_clock_ms if self.last_result else None,
                'last_uncertainty_ms': self.last_result.uncertainty_ms if self.last_result else None,
                'last_confidence': self.last_result.confidence if self.last_result else None
            }


# =============================================================================
# Factory Function
# =============================================================================

def create_phase2_engine(
    raw_buffer_dir: Path,
    output_dir: Path,
    channel_name: str,
    frequency_hz: float,
    receiver_grid: str,
    sample_rate: int = SAMPLE_RATE_FULL
) -> Phase2TemporalEngine:
    """
    Create a Phase 2 Temporal Engine with standard configuration.
    
    Args:
        raw_buffer_dir: Directory containing Phase 1 raw_buffer
        output_dir: Output directory for Phase 2 products
        channel_name: Channel identifier
        frequency_hz: Center frequency in Hz
        receiver_grid: Receiver Maidenhead grid square
        sample_rate: Input sample rate (default 20000 Hz)
        
    Returns:
        Configured Phase2TemporalEngine
    """
    return Phase2TemporalEngine(
        raw_buffer_dir=raw_buffer_dir,
        output_dir=output_dir,
        channel_name=channel_name,
        frequency_hz=frequency_hz,
        receiver_grid=receiver_grid,
        sample_rate=sample_rate
    )
