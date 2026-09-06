"""HF Time Standard Analysis - Core Module

This package provides core components for recording and analyzing
WWV/WWVH/BPM time station signals for precise timing measurements.

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

Note: Phase2AnalyticsService and PipelineOrchestrator archived 2026-01-22.
The REST API and web UI left this repo for station-web on 2026-09-06
(split Phase 5); they read these same products from outside.
"""

# Lazy facade (PEP 562) — split plan Phase 1: the eager imports
# dragged the whole engine surface (and, shim-era, half of
# hamsci-dsp) into every `import hf_timestd.core`.  Names resolve
# on first attribute access; `import hf_timestd.core.X` paths are
# unaffected.  Stale __all__ ghosts of archived modules
# (Phase2AnalyticsService, GlobalStationVoter, ...) are dropped —
# they had not been importable for months.

_LAZY = {
    'BCDCorrelationResult': ('.signal_templates', 'BCDCorrelationResult'),
    'BCDTemplateGenerator': ('.signal_templates', 'BCDTemplateGenerator'),
    'BPMCorrelationResult': ('.signal_templates', 'BPMCorrelationResult'),
    'BPMDiscriminationResult': ('.bpm_discriminator', 'BPMDiscriminationResult'),
    'BPMDiscriminator': ('.bpm_discriminator', 'BPMDiscriminator'),
    'BPMTemplateGenerator': ('.signal_templates', 'BPMTemplateGenerator'),
    'BPMTimingMode': ('.bpm_discriminator', 'BPMTimingMode'),
    'BPM_TEMPLATE': ('.tick_matched_filter', 'BPM_TEMPLATE'),
    'BinaryArchiveConfig': ('.binary_archive_writer', 'BinaryArchiveConfig'),
    'BinaryArchiveReader': ('.binary_archive_writer', 'BinaryArchiveReader'),
    'BinaryArchiveWriter': ('.binary_archive_writer', 'BinaryArchiveWriter'),
    'CombinedUTCResult': ('.transmission_time_solver', 'CombinedUTCResult'),
    'ComparisonMetrics': ('.decoder_config', 'ComparisonMetrics'),
    'CoreRecorder': ('.core_recorder_v2', 'CoreRecorderV2'),
    'DecoderConfig': ('.decoder_config', 'DecoderConfig'),
    'DecoderVariant': ('.decoder_config', 'DecoderVariant'),
    'DetectionQuality': ('.multi_station_detector', 'DetectionQuality'),
    'EmissionTimeResult': ('.propagation_mode_solver', 'EmissionTimeResult'),
    'GapInfo': ('.packet_resequencer', 'GapInfo'),
    'HFPropagationModel': ('.propagation_model', 'HFPropagationModel'),
    'MetrologyEngine': ('.metrology_service', 'MetrologyEngine'),
    'MetrologyService': ('.metrology_service', 'MetrologyService'),
    'MinuteDetectionResult': ('.multi_station_detector', 'MinuteDetectionResult'),
    'MinuteTickAnalysis': ('.tick_matched_filter', 'MinuteTickAnalysis'),
    'ModeArrival': ('.propagation_model', 'ModeArrival'),
    'ModeCandidate': ('.propagation_mode_solver', 'ModeCandidate'),
    'ModeIdentificationResult': ('.propagation_mode_solver', 'ModeIdentificationResult'),
    'MultiStationDetector': ('.multi_station_detector', 'MultiStationDetector'),
    'MultiStationSolver': ('.transmission_time_solver', 'MultiStationSolver'),
    'PacketResequencer': ('.packet_resequencer', 'PacketResequencer'),
    'PhysicsPropagationModel': ('.physics_propagation', 'PhysicsPropagationModel'),
    'PropagationMode': ('.transmission_time_solver', 'PropagationMode'),
    'PropagationModeSolver': ('.propagation_mode_solver', 'PropagationModeSolver'),
    'PropagationModelTier': ('.physics_propagation', 'PropagationModelTier'),
    'PropagationPrediction': ('.propagation_model', 'PropagationPrediction'),
    'PropagationResult': ('.physics_propagation', 'PropagationResult'),
    'RTPHeader': ('ka9q', 'RTPHeader'),
    'RTPPacket': ('.packet_resequencer', 'RTPPacket'),
    'RecordingSession': ('.recording_session', 'RecordingSession'),
    'STATION_TEMPLATES': ('.tick_matched_filter', 'STATION_TEMPLATES'),
    'SegmentInfo': ('.recording_session', 'SegmentInfo'),
    'SegmentWriter': ('.recording_session', 'SegmentWriter'),
    'SessionConfig': ('.recording_session', 'SessionConfig'),
    'SessionMetrics': ('.recording_session', 'SessionMetrics'),
    'SessionState': ('.recording_session', 'SessionState'),
    'SignalTemplateCorrelator': ('.signal_templates', 'SignalTemplateCorrelator'),
    'SolverResult': ('.transmission_time_solver', 'SolverResult'),
    'StandardTimeSignalGenerator': ('.standard_signal_generator', 'StandardTimeSignalGenerator'),
    'StationDetection': ('.multi_station_detector', 'StationDetection'),
    'TickDetectionResult': ('.tick_matched_filter', 'TickDetectionResult'),
    'TickMatchedFilter': ('.tick_matched_filter', 'TickMatchedFilter'),
    'TickTemplate': ('.tick_matched_filter', 'TickTemplate'),
    'TieredStorageConfig': ('.tiered_storage', 'TieredStorageConfig'),
    'TieredStorageManager': ('.tiered_storage', 'TieredStorageManager'),
    'ToneDetector': ('.tone_detector', 'ToneDetector'),
    'TransmissionModeCandidate': ('.transmission_time_solver', 'ModeCandidate'),
    'TransmissionTimeSolver': ('.transmission_time_solver', 'TransmissionTimeSolver'),
    'WWVBCDEncoder': ('.wwv_bcd_encoder', 'WWVBCDEncoder'),
    'WWVGeographicPredictor': ('.wwv_geographic_predictor', 'WWVGeographicPredictor'),
    'WWVHDiscriminator': ('.wwvh_discrimination', 'WWVHDiscriminator'),
    'WWVH_TEMPLATE': ('.tick_matched_filter', 'WWVH_TEMPLATE'),
    'WWVTestSignalDetector': ('.wwv_test_signal', 'WWVTestSignalDetector'),
    'WWV_TEMPLATE': ('.tick_matched_filter', 'WWV_TEMPLATE'),
    'calculate_hot_minutes': ('.tiered_storage', 'calculate_hot_minutes'),
    'create_bcd_generator': ('.signal_templates', 'create_bcd_generator'),
    'create_bpm_generator': ('.signal_templates', 'create_bpm_generator'),
    'create_correlator': ('.signal_templates', 'create_correlator'),
    'create_detector': ('.multi_station_detector', 'create_detector'),
    'create_multi_station_solver': ('.transmission_time_solver', 'create_multi_station_solver'),
    'create_solver_from_grid': ('.transmission_time_solver', 'create_solver_from_grid'),
    'create_tick_filter': ('.tick_matched_filter', 'create_tick_filter'),
    'get_available_ram_bytes': ('.tiered_storage', 'get_available_ram_bytes'),
    'get_decoder_config': ('.decoder_config', 'get_decoder_config'),
    'get_tiered_storage_manager': ('.tiered_storage', 'get_tiered_storage_manager'),
    'grid_to_latlon': ('.transmission_time_solver', 'grid_to_latlon'),
    'init_tiered_storage': ('.tiered_storage', 'init_tiered_storage'),
    'wwv_tone_schedule': ('.wwv_tone_schedule', 'schedule'),
}

__all__ = [
    'BCDCorrelationResult',
    'BCDTemplateGenerator',
    'BPMCorrelationResult',
    'BPMDiscriminationResult',
    'BPMDiscriminator',
    'BPMTemplateGenerator',
    'BPMTimingMode',
    'BPM_TEMPLATE',
    'BinaryArchiveConfig',
    'BinaryArchiveReader',
    'BinaryArchiveWriter',
    'CombinedUTCResult',
    'ComparisonMetrics',
    'CoreRecorder',
    'DecoderConfig',
    'DecoderVariant',
    'DetectionQuality',
    'EmissionTimeResult',
    'GapInfo',
    'HFPropagationModel',
    'MetrologyEngine',
    'MetrologyService',
    'MinuteDetectionResult',
    'MinuteTickAnalysis',
    'ModeArrival',
    'ModeCandidate',
    'ModeIdentificationResult',
    'MultiStationDetector',
    'MultiStationSolver',
    'PacketResequencer',
    'PropagationMode',
    'PropagationModeSolver',
    'PropagationPrediction',
    'RTPHeader',
    'RTPPacket',
    'RecordingSession',
    'STATION_TEMPLATES',
    'SegmentInfo',
    'SegmentWriter',
    'SessionConfig',
    'SessionMetrics',
    'SessionState',
    'SignalTemplateCorrelator',
    'SolverResult',
    'StandardTimeSignalGenerator',
    'StationDetection',
    'TickDetectionResult',
    'TickMatchedFilter',
    'TickTemplate',
    'TieredStorageConfig',
    'TieredStorageManager',
    'ToneDetector',
    'TransmissionModeCandidate',
    'TransmissionTimeSolver',
    'WWVBCDEncoder',
    'WWVGeographicPredictor',
    'WWVHDiscriminator',
    'WWVH_TEMPLATE',
    'WWVTestSignalDetector',
    'WWV_TEMPLATE',
    'calculate_hot_minutes',
    'create_bcd_generator',
    'create_bpm_generator',
    'create_correlator',
    'create_detector',
    'create_multi_station_solver',
    'create_solver_from_grid',
    'create_tick_filter',
    'get_available_ram_bytes',
    'get_decoder_config',
    'get_tiered_storage_manager',
    'grid_to_latlon',
    'init_tiered_storage',
    'wwv_tone_schedule',
]



def __getattr__(name):
    try:
        mod, attr = _LAZY[name]
    except KeyError:
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}") from None
    import importlib
    module = importlib.import_module(mod, __name__) if mod.startswith(".") \
        else importlib.import_module(mod)
    value = getattr(module, attr)
    globals()[name] = value      # cache: next access skips __getattr__
    return value


def __dir__():
    return sorted(set(globals()) | set(_LAZY))
