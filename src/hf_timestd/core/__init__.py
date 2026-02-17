"""HF Time Standard Analysis - Core Module

This package provides core components for recording and analyzing
WWV/WWVH/CHU time station signals for precise timing measurements.

Architecture (v5.4.0):
======================
Six-service systemd pipeline:

1. CoreRecorder (timestd-core-recorder)
   - Consumes RTP multicast from ka9q-radio
   - Writes raw IQ to /var/lib/timestd/raw_buffer/
   - Key: CoreRecorderV2, BinaryArchiveWriter

2. MetrologyService (timestd-metrology)
   - Reads raw_buffer, produces L1 metrology measurements
   - Tone detection, discrimination, test signal analysis
   - Output: /var/lib/timestd/phase2/{CHANNEL}/L1/
   - Key: MetrologyService, MetrologyEngine

3. L2CalibrationService (timestd-l2-calibration)
   - Converts L1 metrology to L2 timing measurements
   - Applies propagation corrections
   - Output: /var/lib/timestd/phase2/{CHANNEL}/L2/

4. FusionService (timestd-fusion)
   - Multi-broadcast weighted fusion
   - Kalman filtering, Chrony SHM feed
   - Output: /var/lib/timestd/phase2/fusion/

5. PhysicsService (timestd-physics)
   - Ionospheric modeling, TEC estimation
   - Output: /var/lib/timestd/phase2/{CHANNEL}/L3/

6. WebAPI (timestd-web-api)
   - REST API and web UI
   - Reads HDF5 data products

Note: Phase2AnalyticsService and PipelineOrchestrator archived 2026-01-22.
"""

# RTP infrastructure
# from .rtp_receiver import RTPReceiver  # DEPRECATED - using ka9q-python instead
from .packet_resequencer import PacketResequencer, RTPPacket, GapInfo
from .recording_session import (
    RecordingSession,
    SessionConfig,
    SessionState,
    SegmentInfo,
    SessionMetrics,
    SegmentWriter,
)

# Re-export ka9q types for convenience
from ka9q import RTPHeader

# Tone detection and timing
from .tone_detector import ToneDetector

# Analytics and discrimination
from .wwvh_discrimination import WWVHDiscriminator
from .bpm_discriminator import BPMDiscriminator, BPMTimingMode, BPMDiscriminationResult
from .physics_propagation import PhysicsPropagationModel, PropagationResult, PropagationModelTier  # deprecated
from .propagation_model import HFPropagationModel, PropagationPrediction, ModeArrival
from .wwv_test_signal import WWVTestSignalDetector
from .metrology_service import MetrologyService, MetrologyEngine
from .decoder_config import DecoderConfig, DecoderVariant, ComparisonMetrics, get_decoder_config
# Phase2AnalyticsService archived 2026-01-22 - replaced by MetrologyService

# Supporting components
from .wwv_geographic_predictor import WWVGeographicPredictor
from .standard_signal_generator import StandardTimeSignalGenerator
from .wwv_tone_schedule import schedule as wwv_tone_schedule
from .wwv_bcd_encoder import WWVBCDEncoder
from .quality_metrics import QualityMetricsTracker, MinuteQualityMetrics
from .timing_metrics_writer import TimingMetricsWriter
from .solar_zenith_calculator import calculate_solar_zenith_for_day
from .core_recorder_v2 import CoreRecorderV2 as CoreRecorder  # V2 is active implementation

# Phase 1 raw_buffer (binary archive)
from .binary_archive_writer import (
    BinaryArchiveWriter,
    BinaryArchiveConfig,
    BinaryArchiveReader,
)

# Tiered Storage (RAM hot buffer + disk cold storage)
from .tiered_storage import (
    TieredStorageManager,
    TieredStorageConfig,
    get_tiered_storage_manager,
    init_tiered_storage,
    calculate_hot_minutes,
    get_available_ram_bytes,
)

# Multi-Station Detection (Physics-based approach)
# Note: GlobalStationVoter and StationLockCoordinator archived 2026-01-16
# Backward-compat aliases available in multi_station_detector.py
from .multi_station_detector import (
    MultiStationDetector,
    StationDetection,
    MinuteDetectionResult,
    DetectionQuality,
    create_detector,
)

# Clock Convergence Model ("Set, Monitor, Intervention" for GPSDO)
from .clock_convergence import (
    ClockConvergenceModel,
    ConvergenceState,
    ConvergenceResult,
    StationAccumulator
)

# Primary Time Standard (HF Time Transfer)
from .propagation_mode_solver import (
    PropagationModeSolver, 
    PropagationMode, 
    ModeCandidate,
    ModeIdentificationResult,
    EmissionTimeResult
)
from .primary_time_standard import (
    PrimaryTimeStandard,
    ChannelTimeResult,
    StationConsensus,
    MinuteTimeStandardResult
)

# Pipeline
# Note: PipelineRecorder archived 2026-01-16 (used deprecated RTPReceiver)
# Note: PipelineOrchestrator archived 2026-01-22 (replaced by MetrologyService)
# Use StreamRecorderV2 (stream_recorder_v2.py) with ka9q.RadiodStream instead

# Transmission Time Solver (UTC back-calculation)
from .transmission_time_solver import (
    TransmissionTimeSolver,
    MultiStationSolver,
    SolverResult,
    CombinedUTCResult,
    PropagationMode,
    ModeCandidate as TransmissionModeCandidate,
    create_solver_from_grid,
    create_multi_station_solver,
    grid_to_latlon
)

# Phase 2: Temporal Analysis Engine
# Note: Phase2TemporalEngine archived 2026-01-22 (replaced by MetrologyService)

# GPSDO Monitoring
from .gpsdo_monitor import (
    GPSDOMonitor,
    AnchorState,
    GPSDOMonitorState
)

# Sliding Window Monitor (10-second real-time quality tracking)
from .sliding_window_monitor import (
    SlidingWindowMonitor,
    WindowMetrics,
    MinuteSummary,
    SignalQuality
)

# Tick Matched Filter (per-second tick detection with overlapping windows)
from .tick_matched_filter import (
    TickMatchedFilter,
    TickTemplate,
    TickDetectionResult,
    MinuteTickAnalysis,
    create_tick_filter,
    WWV_TEMPLATE,
    WWVH_TEMPLATE,
    CHU_TEMPLATE,
    BPM_TEMPLATE,
    STATION_TEMPLATES,
)

# Signal Templates (BCD, AFSK, BPM modulation patterns)
from .signal_templates import (
    BCDTemplateGenerator,
    BCDCorrelationResult,
    CHUAFSKTemplateGenerator,
    AFSKCorrelationResult,
    BPMTemplateGenerator,
    BPMCorrelationResult,
    SignalTemplateCorrelator,
    create_bcd_generator,
    create_afsk_generator,
    create_bpm_generator,
    create_correlator,
)

__all__ = [
    # RTP infrastructure
    # "RTPReceiver",  # DEPRECATED
    "RTPHeader",
    "PacketResequencer",
    "RTPPacket",
    "GapInfo",
    "RecordingSession",
    "SessionConfig",
    "SessionState",
    "SegmentInfo",
    "SessionMetrics",
    "SegmentWriter",
    # Core recorder
    "CoreRecorder",
    # Tone detection
    "ToneDetector",
    # Analytics
    "Phase2AnalyticsService",
    "WWVHDiscriminator",
    "WWVTestSignalDetector",
    "DiscriminationCSVWriters",
    # Supporting
    "WWVGeographicPredictor",
    "StandardTimeSignalGenerator",
    "wwv_tone_schedule",
    "WWVBCDEncoder",
    "QualityMetricsTracker",
    "MinuteQualityMetrics",
    "TimingMetricsWriter",
    "calculate_solar_zenith_for_day",
    # Cross-channel coordination
    "GlobalStationVoter",
    "StationAnchor",
    "AnchorQuality",
    "StationLockCoordinator",
    "GuidedDetection",
    "MinuteProcessingResult",
    # Clock Convergence Model
    "ClockConvergenceModel",
    "ConvergenceState",
    "ConvergenceResult",
    "StationAccumulator",
    # Primary Time Standard
    "PropagationModeSolver",
    "PropagationMode",
    "ModeCandidate",
    "ModeIdentificationResult",
    "EmissionTimeResult",
    "PrimaryTimeStandard",
    "ChannelTimeResult",
    "StationConsensus",
    "MinuteTimeStandardResult",
    "TimeStandardCSVWriter",
    "TimeStandardSummaryWriter",
    # BPM Discrimination
    "BPMDiscriminator",
    "BPMTimingMode",
    "BPMDiscriminationResult",
    # Metrology Service
    "MetrologyService",
    "MetrologyEngine",
    # Decoder Config
    "DecoderConfig",
    "DecoderVariant",
    "ComparisonMetrics",
    "get_decoder_config",
    "PhysicsPropagationModel",
    "PropagationResult",
    "PropagationModelTier",
    # Two-Phase Pipeline (PipelineRecorder archived 2026-01-16)
    "BinaryArchiveWriter",
    "BinaryArchiveConfig",
    "BinaryArchiveReader",
    # Tiered Storage
    "TieredStorageManager",
    "TieredStorageConfig",
    "get_tiered_storage_manager",
    "init_tiered_storage",
    "calculate_hot_minutes",
    "get_available_ram_bytes",
    # ClockOffsetEngine removed - redundant legacy code
    # PipelineOrchestrator archived 2026-01-22
    "BatchReprocessor",
    # Transmission Time Solver
    "TransmissionTimeSolver",
    "MultiStationSolver",
    "SolverResult",
    "CombinedUTCResult",
    "TransmissionModeCandidate",
    "create_solver_from_grid",
    "create_multi_station_solver",
    "grid_to_latlon",
    # Phase 2: Temporal Analysis Engine - archived 2026-01-22
    # GPSDO Monitoring
    "GPSDOMonitor",
    "AnchorState",
    "GPSDOMonitorState",
    # Sliding Window Monitor
    "SlidingWindowMonitor",
    "WindowMetrics",
    "MinuteSummary",
    "SignalQuality",
    # Tick Matched Filter
    "TickMatchedFilter",
    "TickTemplate",
    "TickDetectionResult",
    "MinuteTickAnalysis",
    "create_tick_filter",
    "WWV_TEMPLATE",
    "WWVH_TEMPLATE",
    "CHU_TEMPLATE",
    "BPM_TEMPLATE",
    "STATION_TEMPLATES",
    # Multi-Station Detection (Physics-based)
    "MultiStationDetector",
    "StationDetection",
    "MinuteDetectionResult",
    "DetectionQuality",
    "create_detector",
    # Signal Templates (BCD, AFSK, BPM)
    "BCDTemplateGenerator",
    "BCDCorrelationResult",
    "CHUAFSKTemplateGenerator",
    "AFSKCorrelationResult",
    "BPMTemplateGenerator",
    "BPMCorrelationResult",
    "SignalTemplateCorrelator",
    "create_bcd_generator",
    "create_afsk_generator",
    "create_bpm_generator",
    "create_correlator",
]
