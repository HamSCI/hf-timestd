"""HF Time Standard Analysis - Core Module

This package provides core components for recording and analyzing
WWV/WWVH/CHU time station signals for precise timing measurements.

Architecture:
=============
Two-Phase Robust Time-Aligned Data Pipeline:

Phase 1: Immutable Raw Archive (20 kHz IQ DRF)
- Stores raw data with system time only (no UTC corrections)
- Fixed-duration file splitting (1 hour) - NOT event-based
- Lossless compression (Shuffle + ZSTD/gzip)
- NEVER modified based on subsequent analysis
- Key: RawArchiveWriter, CoreRecorder

Phase 2: Analytical Engine (Clock Offset Series)
- Reads from Phase 1 raw archive
- Produces D_clock = t_system - t_UTC
- Uses tone detection, discrimination, propagation modeling
- Output: Separate versionable CSV/JSON files
- Key: AnalyticsService, Phase2TemporalEngine

Note: Phase 3 (decimation, spectrograms, PSWS upload) is in separate grape-recorder package.
See: https://github.com/mijahauan/grape-recorder

Example:
    from hf_timestd.core import create_pipeline
    
    orchestrator = create_pipeline(
        data_dir=Path('/data/grape'),
        channel_name='WWV_10MHz',
        frequency_hz=10e6,
        receiver_grid='EM38ww',
        station_config={'callsign': 'W3PM', 'grid_square': 'EM38ww'}
    )
    orchestrator.start()
    
    # Feed RTP data
    orchestrator.process_samples(iq_samples, rtp_timestamp)
"""

# Tone detection and timing
from .tone_detector import ToneDetector

# Analytics and discrimination
from .analytics_service import AnalyticsService
from .wwvh_discrimination import WWVHDiscriminator
from .wwv_test_signal import WWVTestSignalDetector
from .discrimination_csv_writers import DiscriminationCSVWriters

# Supporting components
from .wwv_geographic_predictor import WWVGeographicPredictor
from .wwv_tone_schedule import schedule as wwv_tone_schedule
from .wwv_bcd_encoder import WWVBCDEncoder
from .quality_metrics import QualityMetricsTracker, MinuteQualityMetrics
from .timing_metrics_writer import TimingMetricsWriter
from .solar_zenith_calculator import calculate_solar_zenith_for_day
from .gap_backfill import find_gaps, backfill_gaps
from .core_recorder import CoreRecorder

# Cross-channel coordination (Station Lock)
from .global_station_voter import GlobalStationVoter, StationAnchor, AnchorQuality
from .station_lock_coordinator import StationLockCoordinator, GuidedDetection, MinuteProcessingResult

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

# Three-Phase Pipeline (New Architecture)
from .pipeline_recorder import (
    PipelineRecorder,
    PipelineRecorderConfig,
    PipelineRecorderState,
    create_pipeline_recorder
)
from .raw_archive_writer import (
    RawArchiveWriter,
    RawArchiveReader,
    RawArchiveConfig,
    SystemTimeReference,
    create_raw_archive_writer
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

# Note: Phase 3 modules (decimation, spectrograms, DRF packaging, upload) moved to grape-recorder
# See: https://github.com/mijahauan/grape-recorder

__all__ = [
    # Core recorder
    "CoreRecorder",
    # Tone detection
    "ToneDetector",
    # Analytics
    "AnalyticsService",
    "WWVHDiscriminator",
    "WWVTestSignalDetector",
    "DiscriminationCSVWriters",
    # Supporting
    "WWVGeographicPredictor",
    "wwv_tone_schedule",
    "WWVBCDEncoder",
    "QualityMetricsTracker",
    "MinuteQualityMetrics",
    "TimingMetricsWriter",
    "calculate_solar_zenith_for_day",
    "find_gaps",
    "backfill_gaps",
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
    # Three-Phase Pipeline (New Architecture)
    "PipelineRecorder",
    "PipelineRecorderConfig",
    "PipelineRecorderState",
    "create_pipeline_recorder",
    "RawArchiveWriter",
    "RawArchiveReader",
    "RawArchiveConfig",
    "SystemTimeReference",
    "create_raw_archive_writer",
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
]
