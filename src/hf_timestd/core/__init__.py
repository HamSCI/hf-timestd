"""HF Time Standard Analysis - Core Module

This package provides core components for recording and analyzing
WWV/WWVH/CHU time station signals for precise timing measurements.

Architecture:
=============
Two-Phase Robust Time-Aligned Data Pipeline:

Phase 1: Immutable raw_buffer (20 kHz IQ binary)
- Stores raw data with system time only (no UTC corrections)
- Fixed-duration file splitting (1 hour) - NOT event-based
- Lossless compression (Shuffle + ZSTD/gzip)
- NEVER modified based on subsequent analysis
- Key: BinaryArchiveWriter, CoreRecorder

Phase 2: Analytical Engine (Clock Offset Series)
- Reads from Phase 1 raw_buffer
- Produces D_clock = t_system - t_UTC
- Uses tone detection, discrimination, propagation modeling
- Output: Separate versionable CSV/JSON files
- Key: Phase2AnalyticsService, Phase2TemporalEngine

Note: hf-timestd does not use DigitalRF or decimation.

Example:
    from hf_timestd.core import create_pipeline
    
    orchestrator = create_pipeline(
        data_dir=Path('/data/timestd'),
        channel_name='WWV_10MHz',
        frequency_hz=10e6,
        receiver_grid='EM38ww',
        station_config={'callsign': 'W3PM', 'grid_square': 'EM38ww'}
    )
    orchestrator.start()
    
    # Feed RTP data
    orchestrator.process_samples(iq_samples, rtp_timestamp)
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
from .physics_propagation import PhysicsPropagationModel, PropagationResult, PropagationModelTier
from .wwv_test_signal import WWVTestSignalDetector
from .discrimination_csv_writers import DiscriminationCSVWriters
from .phase2_analytics_service import Phase2AnalyticsService

# Supporting components
from .wwv_geographic_predictor import WWVGeographicPredictor
from .standard_signal_generator import StandardTimeSignalGenerator
from .wwv_tone_schedule import schedule as wwv_tone_schedule
from .wwv_bcd_encoder import WWVBCDEncoder
from .quality_metrics import QualityMetricsTracker, MinuteQualityMetrics
from .timing_metrics_writer import TimingMetricsWriter
from .solar_zenith_calculator import calculate_solar_zenith_for_day
from .core_recorder import CoreRecorder

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

# Cross-channel coordination (Station Lock) - Legacy
from .global_station_voter import GlobalStationVoter, StationAnchor, AnchorQuality
from .station_lock_coordinator import StationLockCoordinator, GuidedDetection, MinuteProcessingResult

# Multi-Station Detection (Physics-based approach - replaces voting)
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
from .time_standard_csv_writer import TimeStandardCSVWriter, TimeStandardSummaryWriter

# Pipeline
from .pipeline_recorder import (
    PipelineRecorder,
    PipelineRecorderConfig,
    PipelineRecorderState,
    create_pipeline_recorder
)
from .clock_offset_series import (
    ClockOffsetEngine,
    ClockOffsetSeries,
    ClockOffsetMeasurement,
    ClockOffsetQuality,
    ClockOffsetSeriesWriter,
    create_clock_offset_engine
)
from .pipeline_orchestrator import (
    PipelineOrchestrator,
    PipelineConfig,
    PipelineState,
    BatchReprocessor,
    create_pipeline
)

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

# Phase 2: Temporal Analysis Engine (Refined temporal analysis order)
# Phase 2: Temporal Analysis Engine (Refined temporal analysis order)
from .phase2_temporal_engine import (
    Phase2TemporalEngine,
    Phase2Result,
    TimeSnapResult,
    ChannelCharacterization,
    TransmissionTimeSolution,
    create_phase2_engine
)

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
    # Physics Propagation Model
    "PhysicsPropagationModel",
    "PropagationResult",
    "PropagationModelTier",
    # Two-Phase Pipeline
    "PipelineRecorder",
    "PipelineRecorderConfig",
    "PipelineRecorderState",
    "create_pipeline_recorder",
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
    "ClockOffsetEngine",
    "ClockOffsetSeries",
    "ClockOffsetMeasurement",
    "ClockOffsetQuality",
    "ClockOffsetSeriesWriter",
    "create_clock_offset_engine",
    "PipelineOrchestrator",
    "PipelineConfig",
    "PipelineState",
    "BatchReprocessor",
    "create_pipeline",
    # Transmission Time Solver
    "TransmissionTimeSolver",
    "MultiStationSolver",
    "SolverResult",
    "CombinedUTCResult",
    "TransmissionModeCandidate",
    "create_solver_from_grid",
    "create_multi_station_solver",
    "grid_to_latlon",
    # Phase 2: Temporal Analysis Engine
    "Phase2TemporalEngine",
    "Phase2Result",
    "TimeSnapResult",
    "ChannelCharacterization",
    "TransmissionTimeSolution",
    "create_phase2_engine",
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
