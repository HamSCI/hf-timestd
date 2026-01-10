#!/usr/bin/env python3
"""
Phase 2 Analytics Service - Continuous Processing of Phase 1 raw_buffer

================================================================================
PURPOSE
================================================================================
The Phase 2 Analytics Service is the RUNTIME WRAPPER that continuously monitors
the Phase 1 raw_buffer archive and processes new data through the Phase 2
Temporal Analysis Engine.

This service can be run as a daemon and:
    1. Polls for new minute-aligned data in the raw archive
    2. Invokes Phase2TemporalEngine.process_minute() for each new minute
    3. Writes results to CSV time series files
    4. Updates status JSON for web-ui monitoring

================================================================================
ARCHITECTURE: SERVICE vs ENGINE
================================================================================
┌─────────────────────────────────────────────────────────────────────────────┐
│                     Phase2AnalyticsService (THIS FILE)                      │
│                                                                             │
│   RESPONSIBILITIES:                                                         │
│   - Daemon lifecycle (start, stop, signal handling)                         │
│   - Archive polling and data retrieval                                      │
│   - CSV time series management (per-method files)                           │
│   - Status file updates for web-ui                                          │
│   - Clock convergence model integration                                     │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                 Phase2TemporalEngine (phase2_temporal_engine.py)            │
│                                                                             │
│   RESPONSIBILITIES:                                                         │
│   - Tone detection (Step 1)                                                 │
│   - Channel characterization (Step 2)                                       │
│   - Transmission time solution (Step 3)                                     │
│   - D_clock computation                                                     │
└─────────────────────────────────────────────────────────────────────────────┘

================================================================================
DATA FLOW
================================================================================
                        Phase 1 Archive
                              │
    raw_buffer/{CHANNEL}/     │   (binary complex64 + JSON sidecars)
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                     Phase2AnalyticsService.run()                            │
│                                                                             │
│   1. Poll for new minute-aligned data                                       │
│   2. Read IQ samples from raw_buffer                                        │
│   3. Call engine.process_minute(iq_samples, system_time, rtp_timestamp)     │
│   4. Write results to CSV files                                             │
│   5. Update status JSON                                                     │
└─────────────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
                     Phase 2 Output Directory
                              │
    phase2/{CHANNEL}/         │
    ├── clock_offset/         │   clock_offset_series.csv
    ├── carrier_power/        │   carrier_power_{date}.csv
    ├── tone_detections/      │   {channel}_tones_{date}.csv
    ├── bcd_discrimination/   │   {channel}_bcd_{date}.csv
    ├── doppler/              │   {channel}_doppler_{date}.csv
    ├── station_id_440hz/     │   {channel}_440hz_{date}.csv
    ├── test_signal/          │   {channel}_test_{date}.csv
    ├── discrimination/       │   {channel}_discrimination_{date}.csv
    ├── audio_tones/          │   {channel}_audio_{date}.csv
    └── status/               │   analytics-service-status.json

================================================================================
CSV TIME SERIES FILES
================================================================================
Each discrimination method produces its own CSV for visualization:

FILE                          | DESCRIPTION
------------------------------|---------------------------------------------
clock_offset_series.csv       | D_clock, propagation mode, quality grade
carrier_power_{date}.csv      | Power/SNR measurements
{channel}_tones_{date}.csv    | 1000/1200 Hz detection results
{channel}_bcd_{date}.csv      | BCD correlation amplitudes and delays
{channel}_doppler_{date}.csv  | Doppler shift and stability
{channel}_440hz_{date}.csv    | 440 Hz tone and ground truth detection
{channel}_test_{date}.csv     | Test signal analysis (minutes 8/44)
{channel}_discrimination.csv  | Final weighted voting result

================================================================================
CLOCK CONVERGENCE MODEL
================================================================================
The service integrates a ClockConvergenceModel that implements:

    "SET, MONITOR, INTERVENTION"

1. SET (Acquisition): Collect D_clock measurements, compute running mean/std
2. MONITOR (Lock): When uncertainty < 1ms, lock and flag anomalies
3. INTERVENTION (Reacquire): Force reacquisition after consecutive anomalies

This provides:
    - Stable D_clock output (smooth over short-term ionospheric variations)
    - Anomaly detection (flag propagation mode changes, ionospheric events)
    - Quality grading based on convergence state

================================================================================
USAGE
================================================================================
Or directly for testing:

    python -m hf_timestd.core.phase2_analytics_service \
        --archive-dir /data/raw_buffer/WWV_10_MHz \
        --output /data/phase2/WWV_10_MHz \
        --channel "WWV 10 MHz" \
        --frequency 10e6 \
        --grid EM38ww

================================================================================
REVISION HISTORY
================================================================================
2025-12-07: Added comprehensive service architecture documentation
2025-12-01: Added clock convergence model integration
2025-11-20: Added per-method CSV files for web-ui graphs
2025-10-15: Initial implementation with Phase2TemporalEngine integration
"""

import argparse
import json
import logging
import signal
import time
import numpy as np
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List
from hf_timestd.models import (
    L2TimingMeasurement,
    L1ToneDetection,
    StationID,
    AnchorStation,
    DiscriminationMethod,
    QualityGrade,
    QualityFlag,
    ToneQualityFlag
)
from hf_timestd.core.broadcast_kalman_filter import BroadcastKalmanFilter

logger = logging.getLogger(__name__)

# ============================================================================
# CONFIGURATION CONSTANTS
# ============================================================================

# HDF5 Write Failure Thresholds
HDF5_FAILURE_ALERT_THRESHOLD = 10  # Critical alert after N consecutive failures
HDF5_FAILURE_RESET_INTERVAL = 300  # Reset counter after N seconds of success

# Input Validation
REQUIRE_ARCHIVE_DIR_EXISTS = True  # Fail if archive_dir doesn't exist


class Phase2AnalyticsService:
    """
    Phase 2 Analytics Service - reads raw_buffer, produces timing analysis.
    
    Monitors raw_buffer/{CHANNEL}/ for new binary minute files and
    processes each minute through Phase2TemporalEngine.
    """
    
    def __init__(
        self,
        archive_dir: Path,
        output_dir: Path,
        channel_name: str,
        frequency_hz: float,
        sample_rate: int = 24000,
        receiver_grid: str = '',
        station_config: Optional[Dict] = None,
        poll_interval: float = 10.0,
        use_tiered_storage: bool = False,
        backfill_gaps: bool = False,
        max_backfill: int = 100
    ):
        """
        Initialize Phase 2 analytics service.
        
        Args:
            archive_dir: Directory containing raw_buffer for Phase 1 (raw_buffer/{CHANNEL})
            output_dir: Output directory for Phase 2 products
            channel_name: Channel identifier
            frequency_hz: Center frequency in Hz
            sample_rate: Sample rate (default 24000)
            receiver_grid: Receiver grid square for propagation calculations
            station_config: Station metadata
            poll_interval: Seconds between polling for new data
        """
        self.archive_dir = Path(archive_dir)
        self.output_dir = Path(output_dir)
        self.channel_name = channel_name
        self.frequency_hz = frequency_hz
        self.sample_rate = sample_rate
        self.receiver_grid = receiver_grid
        self.station_config = station_config or {}
        self.poll_interval = poll_interval
        self.use_tiered_storage = use_tiered_storage
        self.backfill_gaps = backfill_gaps
        self.max_backfill = max_backfill
        
        # ====================================================================
        # Input Validation (Issue 1.1 - Analytics Review 2025-12-30)
        # ====================================================================
        if REQUIRE_ARCHIVE_DIR_EXISTS:
            if not self.archive_dir.exists():
                raise FileNotFoundError(
                    f"Archive directory does not exist: {self.archive_dir}. "
                    f"Set REQUIRE_ARCHIVE_DIR_EXISTS=False to disable this check."
                )
            if not self.archive_dir.is_dir():
                raise NotADirectoryError(
                    f"Archive path is not a directory: {self.archive_dir}"
                )
            logger.info(f"✅ Archive directory validated: {self.archive_dir}")
        
        # Initialize tiered storage manager if enabled
        # This allows reading from hot buffer (/dev/shm) first, then cold (disk)
        self._tiered_manager = None
        self._tiered_storage_enabled = False
        if use_tiered_storage:
            try:
                from .tiered_storage import get_tiered_storage_manager
                self._tiered_manager = get_tiered_storage_manager()
                self._tiered_storage_enabled = True
                logger.info(f"✅ Tiered storage enabled: hot={self._tiered_manager.hot_root}, cold={self._tiered_manager.cold_root}")
            except Exception as e:
                logger.warning(f"⚠️  Tiered storage initialization failed: {e}")
                logger.info("Continuing with single-tier storage (cold only)")
        
        # Create output directories using coordinated path structure
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.status_dir = self.output_dir / 'status'
        self.status_dir.mkdir(parents=True, exist_ok=True)
        
        # Clock offset series directory: {data_root}/phase2/{CHANNEL}/clock_offset/
        self.clock_offset_dir = self.output_dir / 'clock_offset'
        self.clock_offset_dir.mkdir(parents=True, exist_ok=True)
        
        # Status file for web-ui
        self.status_file = self.status_dir / 'analytics-service-status.json'
        
        
        # CSV time series for carrier power (for power graphs)
        # CSV time series for carrier power (for power graphs)
        self.carrier_power_dir = self.output_dir / 'carrier_power'
        self.carrier_power_dir.mkdir(parents=True, exist_ok=True)
        
        # ====================================================================
        # Discrimination Method Output Directories
        # ====================================================================
        
        # Tone detections (1000/1200 Hz timing tones)
        self.tone_detections_dir = self.output_dir / 'tone_detections'
        self.tone_detections_dir.mkdir(parents=True, exist_ok=True)
        
        # BCD discrimination (BCD correlation analysis)
        self.bcd_discrimination_dir = self.output_dir / 'bcd_discrimination'
        self.bcd_discrimination_dir.mkdir(parents=True, exist_ok=True)
        
        # Test signal (minutes 8 and 44)
        self.test_signal_dir = self.output_dir / 'test_signal'
        self.test_signal_dir.mkdir(parents=True, exist_ok=True)
        
        # Note: Decimation is not part of hf-timestd (timing-focused)
        # For decimated output, see separate projects

        # Initialize TEC Estimator for ionospheric analysis
        from .tec_estimator import TECEstimator
        self.tec_estimator = TECEstimator(high_precision_mode=True)
        logger.info("Initialized TEC estimator for ionospheric analysis")
        
        # RTP-to-Unix time offset (learned from metadata)
        # This is critical for accurate timing - converts RTP timestamps to actual UTC
        self._rtp_to_unix_offset = None
        self._offset_samples = []
        
        # ====================================================================
        # HDF5 Data Product Writers (Parallel with CSV)
        # ====================================================================
        # Initialize HDF5 writers for schema-validated data products
        # These write in parallel with CSV files during transition period
        try:
            from hf_timestd.io import DataProductWriter
            
            # Get channel name for HDF5 files (e.g., "WWV_10000")
            file_channel = self._get_file_channel_name()
            
            # L1A: Channel Observables (carrier power, SNR, Doppler, tones)
            self.hdf5_l1a_writer = DataProductWriter(
                output_dir=self.carrier_power_dir,
                product_level='L1',
                product_name='channel_observables',
                channel=file_channel,
                processing_version='3.2.0',
                station_metadata=station_config or {}
            )
            logger.info(f"Initialized HDF5 L1A channel observables writer for {file_channel}")
            
            # L1A: Tone Detections (station ID tone timing - critical for fusion provenance)
            self.hdf5_l1a_tones_writer = DataProductWriter(
                output_dir=self.tone_detections_dir,
                product_level='L1',
                product_name='tone_detections',
                channel=file_channel,
                processing_version='3.2.0',
                station_metadata=station_config or {}
            )
            logger.info(f"Initialized HDF5 L1A tone detections writer for {file_channel}")
            
            # L1B: BCD Timecode (BCD discrimination results)
            self.hdf5_l1b_writer = DataProductWriter(
                output_dir=self.bcd_discrimination_dir,
                product_level='L1',
                product_name='bcd_timecode',
                channel=file_channel,
                processing_version='3.2.0',
                station_metadata=station_config or {}
            )
            logger.info(f"Initialized HDF5 L1B BCD timecode writer for {file_channel}")
            
            # L2: Timing Measurements (clock offset with ISO GUM uncertainty)
            self.hdf5_l2_writer = DataProductWriter(
                output_dir=self.clock_offset_dir,
                product_level='L2',
                product_name='timing_measurements',
                channel=file_channel,
                processing_version='3.2.0',
                station_metadata=station_config or {}
            )
            logger.info(f"Initialized HDF5 L2 timing measurements writer for {file_channel}")
            
            # L2: Test Signal Analysis (WWV/WWVH scientific test signals at minutes 8/44)
            self.hdf5_l2_test_signal_writer = DataProductWriter(
                output_dir=self.test_signal_dir,
                product_level='L2',
                product_name='test_signal',
                channel=file_channel,
                version='v1',
                processing_version='3.9.0',
                station_metadata=station_config or {}
            )
            logger.info(f"Initialized HDF5 L2 test signal writer for {file_channel}")
            
            # Flag to enable/disable HDF5 writes (for testing)
            self.enable_hdf5_writes = True

            
            # ================================================================
            # HDF5 Startup Health Check (Analytics Review 2025-12-30)
            # ================================================================
            self._verify_hdf5_writers_healthy()
            
        except Exception as e:
            logger.warning(f"Failed to initialize HDF5 writers: {e}")
            logger.warning("Continuing with CSV-only writes")
            self.hdf5_l1a_writer = None
            self.hdf5_l1a_tones_writer = None
            self.hdf5_l1b_writer = None
            self.hdf5_l2_writer = None
            self.hdf5_l2_test_signal_writer = None
            self.enable_hdf5_writes = False

        
        # Initialize Timing Calibrator (shared across all channel instances)
        # This manages the bootstrap→calibrated→measurement mode progression
        from .timing_calibrator import TimingCalibrator
        
        timing_calibrator_state_file = self.archive_dir.parent.parent / 'state' / 'timing_calibration.json'
        self.timing_calibrator = TimingCalibrator(
            data_root=self.archive_dir.parent.parent,  # /var/lib/timestd
            sample_rate=sample_rate,
            state_file=timing_calibrator_state_file
        )
        logger.info(
            f"Initialized timing calibrator (phase={self.timing_calibrator.phase.value}, "
            f"state_file={timing_calibrator_state_file})"
        )
        
        # Initialize Phase 2 engine
        from .phase2_temporal_engine import Phase2TemporalEngine
        
        # Extract precise coordinates from station_config if available
        # Precise coordinates improve timing accuracy by ~16μs over grid square center
        precise_lat = self.station_config.get('latitude')
        precise_lon = self.station_config.get('longitude')
        
        if precise_lat is not None and precise_lon is not None:
            logger.info(f"Using precise coordinates: {precise_lat:.6f}°N, {precise_lon:.6f}°W")
        
        self.engine = Phase2TemporalEngine(
            raw_buffer_dir=self.archive_dir.parent,  # parent contains all channels
            output_dir=self.output_dir,
            channel_name=channel_name,
            frequency_hz=frequency_hz,
            receiver_grid=receiver_grid,
            sample_rate=sample_rate,
            precise_lat=precise_lat,
            precise_lon=precise_lon
        )
        
        # Wire up timing calibrator callbacks to engine
        # This enables bootstrap→calibrated→measurement mode progression
        def get_rtp_offset(channel_name: str) -> Optional[int]:
            """Get calibrated RTP offset for minute boundary."""
            # CRITICAL FIX (2026-01-10): Always use global RTP offset for all channels
            # All channels share the same GPSDO-disciplined RTP stream, so they MUST
            # use the same RTP-to-UTC mapping. Per-channel offsets create systematic
            # cross-station disagreements (e.g., CHU vs WWV differ by 7.5ms).
            # The global offset is established from anchor channels (CHU, WWV 20/25 MHz)
            # and provides the single source of truth for RTP timestamp alignment.
            if self.timing_calibrator.global_rtp_offset is not None:
                return self.timing_calibrator.global_rtp_offset
            
            # Fallback during bootstrap: use per-channel offset if global not yet established
            if channel_name in self.timing_calibrator.rtp_calibration:
                return self.timing_calibrator.rtp_calibration[channel_name].rtp_offset_samples
            
            return None
        
        self.engine.rtp_calibration_callback = get_rtp_offset
        self.engine.station_predictor = self.timing_calibrator.predict_station
        self.engine.timing_calibrator = self.timing_calibrator
        
        logger.info("Wired timing calibrator callbacks to Phase2TemporalEngine")
        
        # Initialize Clock Convergence Model
        # "Set, Monitor, Intervention" architecture for GPSDO-disciplined timing
        from .clock_convergence import ClockConvergenceModel
        
        convergence_state_file = self.status_dir / 'convergence_state.json'
        self.convergence_model = ClockConvergenceModel(
            lock_uncertainty_ms=1.0,      # Lock when uncertainty < 1ms
            min_samples_for_lock=30,      # Need 30 minutes of data
            anomaly_sigma=3.0,            # 3σ for anomaly detection
            max_consecutive_anomalies=5,  # Force reacquire after 5 anomalies
            state_file=convergence_state_file
        )
        logger.info(f"Initialized clock convergence model (state file: {convergence_state_file})")
        
        # ====================================================================
        # Per-Broadcast Kalman Filters (Science-First Architecture v5.0)
        # ====================================================================
        # Instantiate Kalman filters for ionospheric path tracking
        # Each broadcast (station + frequency) gets its own independent filter
        
        # State directory for Kalman filter persistence
        self.kalman_state_dir = self.archive_dir.parent.parent / 'state' / 'broadcast_kalman_states'
        self.kalman_state_dir.mkdir(parents=True, exist_ok=True)
        
        # Initialize filters for all possible broadcasts on this channel
        self.broadcast_filters = {}
        
        # Determine possible stations for this frequency
        possible_stations = self._get_possible_stations_for_frequency(frequency_hz)
        
        for station in possible_stations:
            broadcast_id = f"{station}_{int(frequency_hz/1000)}"
            
            filter = BroadcastKalmanFilter(
                broadcast_id=broadcast_id,
                station=station,
                frequency_mhz=frequency_hz / 1e6
            )
            
            # Try to load saved state
            filter.load_state(self.kalman_state_dir)
            
            self.broadcast_filters[broadcast_id] = filter
            
            logger.info(
                f"Initialized Kalman filter for {broadcast_id}: "
                f"layer={filter.characteristics.typical_layer}, "
                f"modulation={filter.characteristics.modulation}"
            )
        
        logger.info(f"Initialized {len(self.broadcast_filters)} per-broadcast Kalman filters")
        
        # State tracking
        self.running = False
        self.start_time = time.time()
        self.minutes_processed = 0
        self.last_processed_minute = 0
        self.last_result = None
        self.last_carrier_snr_db = None  # Carrier SNR from IQ data
        self.last_carrier_power_db = None  # Carrier power from IQ data
        self.last_radiod_snr_db = None  # SNR from radiod (base channel SNR)
        
        # Track which minutes we've processed
        self.processed_minutes = set()
        
        # ====================================================================
        # D_clock Continuity Validation State (Critical Fix - 2025-12-31)
        # ====================================================================
        # Track previous D_clock for continuity validation
        # Detects CHU frame slips and other timing jumps
        self.last_d_clock_ms = None
        self.last_minute_unix = None
        
        logger.debug("Initialized D_clock continuity validation state")
        
        # ====================================================================
        # HDF5 Write Failure Tracking (Issue 4.1 - Analytics Review 2025-12-30)
        # ====================================================================
        self.hdf5_write_failures = 0
        self.hdf5_write_successes = 0
        self.last_hdf5_success_time = time.time()
        self.hdf5_failure_alerted = False  # Prevent spam
        
        logger.info(f"Phase2AnalyticsService initialized for {channel_name}")
        logger.info(f"  Archive: {archive_dir}")
        logger.info(f"  Output: {output_dir}")
        logger.info(f"  Frequency: {frequency_hz/1e6:.3f} MHz")
        logger.info(f"  Grid: {receiver_grid}")
        logger.info(f"  Tiered Storage: {'enabled' if self._tiered_storage_enabled else 'disabled'}")
    
    def _get_possible_stations_for_frequency(self, frequency_hz: float) -> List[str]:
        """
        Get list of possible stations for this frequency.
        
        Args:
            frequency_hz: Frequency in Hz
            
        Returns:
            List of station names that broadcast on this frequency
        """
        freq_mhz = frequency_hz / 1e6
        
        # Shared frequencies (WWV + WWVH + BPM)
        if freq_mhz in [2.5, 5.0, 10.0, 15.0]:
            return ['WWV', 'WWVH', 'BPM']
        
        # WWV-only frequencies
        elif freq_mhz in [20.0, 25.0]:
            return ['WWV']
        
        # CHU-only frequencies
        elif freq_mhz in [3.33, 7.85, 14.67]:
            return ['CHU']
        
        # Unknown frequency - try all stations
        else:
            logger.warning(f"Unknown frequency {freq_mhz} MHz - trying all stations")
            return ['WWV', 'WWVH', 'CHU', 'BPM']
    
    # ========================================================================
    # HDF5 Write Tracking & Validation (Analytics Review 2025-12-30)
    # ========================================================================
    
    def _track_hdf5_write_success(self):
        """Track successful HDF5 write and reset failure counter if needed."""
        self.hdf5_write_successes += 1
        self.last_hdf5_success_time = time.time()
        
        # Reset failure counter after sustained success
        if self.hdf5_write_failures > 0:
            self.hdf5_write_failures = 0
            self.hdf5_failure_alerted = False
            logger.info("✅ HDF5 write failure counter reset after sustained success")
    
    def _track_hdf5_write_failure(self, error: Exception, data_product: str):
        """
        Track HDF5 write failure and alert if threshold exceeded.
        
        Args:
            error: The exception that occurred
            data_product: Name of data product (L1A, L1B, L2, etc.)
        """
        self.hdf5_write_failures += 1
        
        logger.error(
            f"HDF5 write failed for {data_product}: {error} "
            f"(failure count: {self.hdf5_write_failures})",
            exc_info=True
        )
        
        # Critical alert if threshold exceeded
        if self.hdf5_write_failures >= HDF5_FAILURE_ALERT_THRESHOLD and not self.hdf5_failure_alerted:
            logger.critical(
                f"🚨 HDF5 WRITE FAILURES CRITICAL: {self.hdf5_write_failures} consecutive failures! "
                f"Fusion service may be starving. Check disk space, permissions, and HDF5 library. "
                f"Channel: {self.channel_name}"
            )
            self.hdf5_failure_alerted = True
            # TODO: Trigger email alert via service-alert.sh
    
    def _validate_required_fields(self, measurement: Dict[str, Any], required_fields: List[str], data_product: str) -> bool:
        """
        Validate that required fields are present and non-None in measurement dict.
        
        Args:
            measurement: Measurement dictionary to validate
            required_fields: List of required field names
            data_product: Name of data product for error messages
            
        Returns:
            True if all required fields present and non-None, False otherwise
        """
        missing_fields = []
        for field in required_fields:
            if field not in measurement or measurement[field] is None:
                missing_fields.append(field)
        
        if missing_fields:
            logger.error(
                f"Cannot write {data_product}: missing required fields: {missing_fields}. "
                f"Measurement: {measurement.keys()}"
            )
            return False
        
        return True
    
    def _verify_hdf5_writers_healthy(self):
        """
        Verify all HDF5 writers can write and read on startup.
        
        Performs a health check by writing a test measurement to each writer
        and verifying it can be read back. Fails fast if any writer is not
        operational.
        
        Raises:
            RuntimeError: If any HDF5 writer fails health check
        """
        writers_to_test = []
        
        if self.hdf5_l1a_writer:
            writers_to_test.append(('L1A Channel Observables', self.hdf5_l1a_writer))
        if self.hdf5_l1a_tones_writer:
            writers_to_test.append(('L1A Tone Detections', self.hdf5_l1a_tones_writer))
        if self.hdf5_l1b_writer:
            writers_to_test.append(('L1B BCD Timecode', self.hdf5_l1b_writer))
        if self.hdf5_l2_writer:
            writers_to_test.append(('L2 Timing Measurements', self.hdf5_l2_writer))
        
        if not writers_to_test:
            logger.info("No HDF5 writers to test (HDF5 disabled)")
            return
        
        logger.info(f"Running HDF5 startup health check for {len(writers_to_test)} writers...")
        
        for writer_name, writer in writers_to_test:
            try:
                if writer.write_test_measurement():
                    logger.info(f"✅ {writer_name} HDF5 writer healthy")
                else:
                    raise RuntimeError(f"{writer_name} HDF5 writer test failed")
            except Exception as e:
                logger.error(f"❌ {writer_name} HDF5 writer FAILED startup test: {e}")
                raise RuntimeError(f"HDF5 writer {writer_name} not operational: {e}")
        
        logger.info(f"✅ All {len(writers_to_test)} HDF5 writers passed startup health check")
    

    def _write_clock_offset(self, result, minute_boundary: int, rtp_timestamp: int):
        """Append D_clock measurement to CSV time series with convergence tracking."""
        try:
            # Skip if no valid timing solution was found
            if result.d_clock_ms is None:
                logger.debug(f"Skipping clock offset write: d_clock_ms is None")
                return
            
            logger.debug(f"Writing clock offset: d_clock_ms={result.d_clock_ms:.2f}ms, enable_hdf5={self.enable_hdf5_writes}, hdf5_l2_writer={self.hdf5_l2_writer is not None}")
            
            # Extract values from Phase2Result
            solution = result.solution if hasattr(result, 'solution') else None
            station = solution.station if solution else 'UNKNOWN'
            frequency_mhz = self.frequency_hz / 1e6
            
            # ================================================================
            # Process through Clock Convergence Model
            # "Set, Monitor, Intervention" - converge to lock, then monitor
            # ================================================================
            # Derive quality_grade from uncertainty_ms for convergence model
            unc = result.uncertainty_ms
            input_grade = 'A' if unc < 1.0 else 'B' if unc < 3.0 else 'C' if unc < 10.0 else 'D'
            
            convergence_result = self.convergence_model.process_measurement(
                station=station,
                frequency_mhz=frequency_mhz,
                d_clock_ms=result.d_clock_ms,
                timestamp=float(minute_boundary),
                snr_db=self.last_carrier_snr_db,
                quality_grade=input_grade
            )
            
            
            
            # Use converged values when locked, raw values otherwise
            if convergence_result.is_locked:
                effective_d_clock = convergence_result.d_clock_ms
                effective_uncertainty = convergence_result.uncertainty_ms
                quality_grade = 'A' if convergence_result.uncertainty_ms < 0.5 else 'B'
            else:
                effective_d_clock = convergence_result.d_clock_ms  # Running mean
                effective_uncertainty = convergence_result.uncertainty_ms
                # Grade based on convergence progress
                progress = convergence_result.convergence_progress
                if progress >= 0.9:
                    quality_grade = 'B'
                elif progress >= 0.5:
                    quality_grade = 'C'
                else:
                    quality_grade = 'D'
            
            # Log convergence state changes
            if convergence_result.is_locked and convergence_result.sample_count == 30:
                logger.info(
                    f"🔒 LOCKED: {self.channel_name} D_clock = "
                    f"{effective_d_clock:.3f} ± {effective_uncertainty:.3f} ms"
                )
            
            # Log anomalies (propagation events!)
            if convergence_result.is_anomaly:
                logger.info(
                    f"📡 PROPAGATION EVENT: {self.channel_name} residual = "
                    f"{convergence_result.residual_ms:.2f} ms "
                    f"({convergence_result.anomaly_sigma:.1f}σ)"
                )
            
            # Extract per-station tick SNR values from time_snap
            # These are the SNR of the detected timing tick for each station
            # Report 0 if no tick was detected for that station
            ts = result.time_snap if hasattr(result, 'time_snap') else None
            wwv_tick_snr = ts.wwv_snr_db if ts and ts.wwv_snr_db is not None else 0.0
            wwvh_tick_snr = ts.wwvh_snr_db if ts and ts.wwvh_snr_db is not None else 0.0
            chu_tick_snr = ts.chu_snr_db if ts and ts.chu_snr_db is not None else 0.0
            bpm_tick_snr = ts.bpm_snr_db if ts and ts.bpm_snr_db is not None else 0.0
            
            # ================================================================
            # Write to HDF5 (L2 Timing Measurements) - HDF5-Only Output
            # ================================================================
            if self.enable_hdf5_writes and self.hdf5_l2_writer:
                try:
                    from hf_timestd.io.uncertainty import ISOGUMCalculator
                    
                    # Early validation: Check required fields before building full dict
                    required_l2_fields = ['timestamp_utc', 'station', 'clock_offset_ms']
                    if station is None or effective_d_clock is None:
                        logger.error(f"Cannot write L2: missing critical data (station={station}, d_clock={effective_d_clock})")
                        self._track_hdf5_write_failure(ValueError("Missing critical L2 fields"), "L2")
                        return  # Skip HDF5 write but continue with CSV
                    
                    # Create ISO GUM uncertainty budget
                    # Use default values scaled by SNR and convergence state
                    snr_db = self.last_carrier_snr_db or 10.0
                    
                    # GPSDO lock status: Assume locked since we have hardware GPSDO
                    # The convergence model's is_locked represents statistical convergence
                    # (requires 30 samples), not actual GPSDO hardware lock.
                    # A hardware GPSDO is GPS-disciplined and maintains lock to satellites.
                    gpsdo_locked = True
                    
                    discrimination_conf = solution.confidence if solution else 0.5
                    
                    budget = ISOGUMCalculator.create_default_budget(
                        snr_db=snr_db,
                        gpsdo_locked=gpsdo_locked,
                        discrimination_confidence=discrimination_conf
                    )
                    
                    # Calculate combined uncertainty
                    unc_result = ISOGUMCalculator.calculate_combined_uncertainty(budget)
                    
                    # Determine quality flag
                    quality_flag = ISOGUMCalculator.assign_quality_flag(
                        quality_grade=quality_grade,
                        discrimination_confidence=discrimination_conf,
                        gpsdo_locked=gpsdo_locked
                    )
                    
                    # Build L2 measurement dict
                    timestamp_utc = datetime.fromtimestamp(minute_boundary, timezone.utc).isoformat().replace('+00:00', 'Z')
                    
                    
                    # Create typed measurement object
                    # SAFEGUARD: Ensure all float fields handle None correctly
                    # effective_d_clock should not be None (checked above), but safeguard anyway
                    ck_off = float(effective_d_clock) if effective_d_clock is not None else float(result.d_clock_ms)
                    
                    # ================================================================
                    # CORE DATUM: Raw Tone Arrival Time (Source of Truth)
                    # ================================================================
                    # Extract from solution which contains validated tone detection.
                    # Data Model Hierarchy:
                    # 1. raw_tone_arrival_ms: from validated tone detection (source of truth)
                    # 2. propagation_delay_ms: from ray tracing (derived)
                    # 3. clock_offset_ms: calculated as (raw - propagation) (derived)
                    #
                    # Missing Value Semantics:
                    # - If no tone detected: raw_tone_arrival_ms = NaN, quality_flag = MISSING
                    # - If ray tracing failed: propagation_delay_ms = NaN
                    # - If either is NaN: clock_offset_ms = NaN
                    # ================================================================
                    
                    import math
                    
                    # Extract tone detection status and raw timing from solution
                    if solution and hasattr(solution, 'tone_detected') and hasattr(solution, 'raw_tone_arrival_ms'):
                        # New schema v1.2.0 with explicit tone_detected field
                        tone_detected = solution.tone_detected
                        raw_tone_timing = solution.raw_tone_arrival_ms
                    elif solution and hasattr(solution, 't_arrival_ms'):
                        # Backward compatibility: old solution format
                        # Assume tone detected if t_arrival_ms is not None
                        raw_tone_timing = solution.t_arrival_ms
                        tone_detected = (raw_tone_timing is not None)
                    else:
                        # No solution - tone not detected
                        tone_detected = False
                        raw_tone_timing = None
                    
                    # Convert None to NaN for HDF5 (explicit missing value)
                    if raw_tone_timing is None or not tone_detected:
                        raw_arr = float('nan')
                        tone_detected_flag = False
                        logger.debug(
                            f"No validated tone for {station} at {frequency_mhz:.2f} MHz - "
                            f"setting raw_arrival_time_ms=NaN, quality_flag=MISSING"
                        )
                    else:
                        raw_arr = float(raw_tone_timing)
                        tone_detected_flag = True
                        logger.debug(
                            f"Validated tone for {station} at {frequency_mhz:.2f} MHz: "
                            f"raw_arrival_time_ms={raw_arr:.3f} ms"
                        )
                    
                    # Extract propagation delay (derived value)
                    if solution and solution.t_propagation_ms is not None:
                        prop_delay = float(solution.t_propagation_ms)
                    else:
                        prop_delay = float('nan')
                        logger.debug(
                            f"No propagation delay for {station} at {frequency_mhz:.2f} MHz - "
                            f"setting propagation_delay_ms=NaN"
                        )
                    
                    # ================================================================
                    # Per-Broadcast Kalman Filter Update (Science-First v5.0)
                    # ================================================================
                    # Update Kalman filter for this specific broadcast
                    # This tracks ionospheric path dynamics [ToF, Doppler]
                    
                    tof_kalman_ms = None
                    tof_uncertainty_ms = None
                    doppler_ms_per_min = None
                    gpsdo_consistent = None
                    
                    if tone_detected_flag and not math.isnan(raw_arr):
                        # Get filter for this broadcast
                        broadcast_id = f"{station}_{int(frequency_mhz)}"
                        
                        if broadcast_id in self.broadcast_filters:
                            filter = self.broadcast_filters[broadcast_id]
                            
                            # Compute ToF from raw arrival time
                            tof_measurement = raw_arr
                            
                            # Get SNR for dynamic measurement noise
                            snr = snr_db if snr_db > 0 else 10.0
                            
                            # Check GPSDO temporal continuity
                            is_consistent, residual = filter.check_gpsdo_continuity(tof_measurement)
                            gpsdo_consistent = is_consistent
                            
                            if not is_consistent:
                                logger.info(
                                    f"GPSDO continuity check: {broadcast_id} residual = {residual:.3f} ms "
                                    f"(propagation change or anomaly)"
                                )
                            
                            # Update Kalman filter
                            tof_kalman_ms, tof_uncertainty_ms = filter.update(
                                measurement_ms=tof_measurement,
                                snr_db=snr
                            )
                            
                            # Get Doppler (rate of change)
                            state = filter.get_state()
                            doppler_ms_per_min = state['doppler_ms_per_min']
                            
                            logger.debug(
                                f"Kalman update {broadcast_id}: "
                                f"ToF={tof_kalman_ms:.3f}±{tof_uncertainty_ms:.3f} ms, "
                                f"Doppler={doppler_ms_per_min:.4f} ms/min"
                            )
                            
                            # ADAPTIVE WINDOW ENHANCEMENT (2026-01-08)
                            # Check convergence and log adaptive window
                            if filter.is_converged():
                                window_ms = filter.get_search_window(snr)
                                if window_ms < 20:  # Only log when significantly narrowed
                                    logger.info(
                                        f"🎯 {broadcast_id} converged: "
                                        f"window={window_ms:.1f}ms, unc={tof_uncertainty_ms:.2f}ms, "
                                        f"innovation={filter.last_innovation:.2f}ms"
                                    )
                            
                            # Save state periodically (every 10 minutes)
                            if self.minutes_processed % 10 == 0:
                                filter.save_state(self.kalman_state_dir)
                        else:
                            logger.warning(f"No Kalman filter for broadcast {broadcast_id}")
                    else:
                        # No tone detected - predict only (coast)
                        broadcast_id = f"{station}_{int(frequency_mhz)}"
                        
                        if broadcast_id in self.broadcast_filters:
                            filter = self.broadcast_filters[broadcast_id]
                            
                            # Predict (coast during fading)
                            tof_kalman_ms, tof_uncertainty_ms = filter.predict()
                            
                            state = filter.get_state()
                            doppler_ms_per_min = state['doppler_ms_per_min']
                            gpsdo_consistent = False  # No measurement to check
                            
                            logger.debug(
                                f"Kalman predict {broadcast_id}: "
                                f"ToF={tof_kalman_ms:.3f}±{tof_uncertainty_ms:.3f} ms (coasting)"
                            )
                            
                            # Save state periodically (every 10 minutes) - Persistence during fading!
                            if self.minutes_processed % 10 == 0:
                                filter.save_state(self.kalman_state_dir)
                    

                    # Clock offset should already be correct from solution
                    # But validate data model hierarchy: clock_offset = raw_arrival - propagation
                    if not math.isnan(raw_arr) and not math.isnan(prop_delay):
                        # Both inputs valid - verify clock_offset is consistent
                        expected_clock_offset = raw_arr - prop_delay
                        if abs(ck_off - expected_clock_offset) > 0.01:
                            logger.warning(
                                f"Data model inconsistency: clock_offset={ck_off:.3f} ms "
                                f"but raw_arrival - propagation = {expected_clock_offset:.3f} ms "
                                f"(diff={abs(ck_off - expected_clock_offset):.3f} ms)"
                            )
                    elif math.isnan(raw_arr) or math.isnan(prop_delay):
                        # If either input is NaN, clock_offset should be NaN
                        ck_off = float('nan')
                    
                    # Determine quality flag based on tone detection
                    if not tone_detected_flag:
                        quality_flag_final = 'MISSING'  # No validated tone detected
                    else:
                        # Use existing quality flag logic
                        quality_flag_final = quality_flag
                        
                    l2_measurement = L2TimingMeasurement(
                        timestamp_utc=timestamp_utc,
                        minute_boundary_utc=minute_boundary,
                        rtp_timestamp=rtp_timestamp,
                        station=StationID(station) if station else StationID.WWV,
                        frequency_mhz=float(frequency_mhz),
                        discrimination_method=DiscriminationMethod.TONE,
                        discrimination_confidence=float(discrimination_conf) if discrimination_conf is not None else 0.0,
                        
                        # === CORE DATUM ===
                        tone_detected=tone_detected_flag,
                        raw_arrival_time_ms=raw_arr,  # NaN if no tone detected
                        
                        # === DERIVED VALUES ===
                        clock_offset_ms=ck_off,  # NaN if inputs are NaN
                        
                        uncertainty_ms=float(effective_uncertainty) if effective_uncertainty is not None else 1.0,
                        expanded_uncertainty_ms=float(unc_result['u_expanded_ms']) if unc_result.get('u_expanded_ms') is not None else 2.0,
                        coverage_factor=float(budget.coverage_factor) if budget.coverage_factor is not None else 2.0,
                        confidence_level=float(budget.confidence_level) if budget.confidence_level is not None else 0.95,
                        u_rtp_timestamp_ms=float(budget.u_rtp_timestamp_ms),
                        u_ionospheric_ms=float(budget.u_ionospheric_ms),
                        u_multipath_ms=float(budget.u_multipath_ms),
                        u_discrimination_ms=float(budget.u_discrimination_ms),
                        u_gpsdo_ms=float(budget.u_gpsdo_ms),
                        u_propagation_model_ms=float(budget.u_propagation_model_ms),
                        degrees_of_freedom=int(unc_result['degrees_of_freedom']),
                        quality_grade=QualityGrade(quality_grade),
                        confidence=float(solution.confidence) if solution and solution.confidence is not None else 0.0,
                        quality_flag=QualityFlag(quality_flag_final),
                        propagation_delay_ms=prop_delay if not math.isnan(prop_delay) else None,
                        propagation_mode=str(solution.propagation_mode) if solution and solution.propagation_mode else None,
                        n_hops=int(solution.n_hops) if solution and solution.n_hops is not None else None,
                        snr_db=float(snr_db) if snr_db is not None else None,
                        
                        # Per-Broadcast Kalman Filter State (Science-First v5.0)
                        tof_kalman_ms=tof_kalman_ms,
                        tof_uncertainty_ms=tof_uncertainty_ms,
                        doppler_ms_per_min=doppler_ms_per_min,
                        gpsdo_consistent=gpsdo_consistent,
                        
                        utc_verified=bool(convergence_result.is_locked),
                        multi_station_verified=bool(solution.dual_station_verified) if solution else False,
                        traceability_chain='GPSDO → UTC(GPS) → UTC(NIST)',
                        processing_version='3.2.0',
                        processed_at=datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
                        calibration_date='2025-12-01T00:00:00Z',
                        gpsdo_locked=bool(gpsdo_locked)
                    )
                    
                    # Log data model validation
                    if tone_detected_flag:
                        logger.info(
                            f"✓ Validated tone: station={station}, freq={frequency_mhz:.2f} MHz, "
                            f"raw_arrival={raw_arr:.3f} ms, quality={quality_flag_final}"
                        )
                    else:
                        logger.debug(
                            f"✗ No validated tone: station={station}, freq={frequency_mhz:.2f} MHz, "
                            f"quality=MISSING"
                        )
                    
                    # Write to HDF5
                    logger.debug(f"Attempting HDF5 L2 write: D_clock={effective_d_clock:.2f}ms")
                    
                    # DEBUG: Verify tone_detected field is present
                    measurement_dict = l2_measurement.model_dump()
                    if 'tone_detected' not in measurement_dict:
                        logger.error(f"CRITICAL: tone_detected missing from model_dump()! Keys: {list(measurement_dict.keys())}")
                    else:
                        logger.debug(f"tone_detected={measurement_dict['tone_detected']}")
                    
                    self.hdf5_l2_writer.write_measurement(measurement_dict)
                    self._track_hdf5_write_success()
                    logger.debug(f"Successfully wrote HDF5 L2 measurement")
                    
                except Exception as e:
                    self._track_hdf5_write_failure(e, "L2")
                
                
            # Store convergence result for status reporting
            self.last_convergence_result = convergence_result
            
        except Exception as e:
            logger.error(f"Failed to write clock offset: {e}")
    

    
    def _write_carrier_power(self, minute_boundary: int, power_db: float, snr_db: float,
                              wwv_tone_db: float = None, wwvh_tone_db: float = None,
                              station: str = None, quality_grade: str = None,
                              channel_char = None):
        """Append carrier power measurement to daily CSV and HDF5 with channel characterization."""
        try:
            # ================================================================
            # Write to HDF5 (L1A Channel Observables) only
            # ================================================================
            if self.enable_hdf5_writes and self.hdf5_l1a_writer:
                try:
                    timestamp_utc = datetime.fromtimestamp(minute_boundary, timezone.utc).isoformat().replace('+00:00', 'Z')
                    
                    # Determine quality flag based on data completeness
                    quality_flag = 'GOOD' if snr_db and snr_db > 10 else 'MARGINAL' if snr_db and snr_db > 0 else 'BAD'
                    data_completeness = 1.0  # Assume full minute of data
                    
                    # Extract channel characterization fields if available
                    carrier_doppler = None
                    doppler_std = None
                    phase_variance = None
                    coherence_time = None
                    
                    if channel_char:
                        # Carrier Doppler shift (Hz)
                        if hasattr(channel_char, 'doppler_carrier_hz') and channel_char.doppler_carrier_hz is not None:
                            carrier_doppler = channel_char.doppler_carrier_hz
                        
                        # Doppler spread - use WWV std dev, or average if both available
                        if hasattr(channel_char, 'doppler_wwv_std_hz') and channel_char.doppler_wwv_std_hz is not None:
                            doppler_std = channel_char.doppler_wwv_std_hz
                        elif hasattr(channel_char, 'doppler_wwvh_std_hz') and channel_char.doppler_wwvh_std_hz is not None:
                            doppler_std = channel_char.doppler_wwvh_std_hz
                        
                        # Phase variance (radians)
                        if hasattr(channel_char, 'phase_variance_rad') and channel_char.phase_variance_rad is not None:
                            phase_variance = channel_char.phase_variance_rad
                        
                        # Coherence time (seconds)
                        if hasattr(channel_char, 'max_coherent_window_sec') and channel_char.max_coherent_window_sec is not None:
                            coherence_time = channel_char.max_coherent_window_sec
                    
                    l1a_measurement = {
                        'timestamp_utc': timestamp_utc,
                        'minute_boundary': minute_boundary,
                        'rtp_timestamp': 0,  # Not available in this context
                        'carrier_power_db': power_db if power_db is not None and not np.isnan(power_db) and not np.isinf(power_db) else None,
                        'carrier_snr_db': snr_db if snr_db is not None and not np.isnan(snr_db) and not np.isinf(snr_db) else None,
                        'carrier_doppler_hz': carrier_doppler if carrier_doppler is not None and not np.isnan(carrier_doppler) and not np.isinf(carrier_doppler) else None,
                        'doppler_std_hz': doppler_std if doppler_std is not None and not np.isnan(doppler_std) and not np.isinf(doppler_std) else None,
                        'phase_variance_rad': phase_variance if phase_variance is not None and not np.isnan(phase_variance) and not np.isinf(phase_variance) else None,
                        'coherence_time_sec': coherence_time if coherence_time is not None and not np.isnan(coherence_time) and not np.isinf(coherence_time) else None,
                        'wwv_tone_500hz_db': wwv_tone_db if wwv_tone_db is not None and not np.isnan(wwv_tone_db) and not np.isinf(wwv_tone_db) else None,
                        'wwvh_tone_1200hz_db': wwvh_tone_db if wwvh_tone_db is not None and not np.isnan(wwvh_tone_db) and not np.isinf(wwvh_tone_db) else None,
                        'quality_flag': quality_flag,
                        'data_completeness': data_completeness,
                        'processing_version': '3.2.0'
                    }
                    
                    self.hdf5_l1a_writer.write_measurement(l1a_measurement)
                    self._track_hdf5_write_success()
                    
                except Exception as e:
                    self._track_hdf5_write_failure(e, "L1A")
                    
        except Exception as e:
            logger.error(f"Failed to write carrier power: {e}")
    
    # ========================================================================
    # Discrimination Method CSV Writers
    # ========================================================================
    
    def _get_file_channel_name(self) -> str:
        """Get filename-safe channel name (Station_KHz e.g. WWV_10000)."""
        # User requested simplified "Station_Freq" format to avoid "MHz" and dots.
        khz = int(self.frequency_hz / 1000)
        
        # Extract station name (handle "WWV 10 MHz" or "CHU_3330" formats)
        name_part = self.channel_name.split(' ')[0]
        station = name_part.split('_')[0].replace('/', '')
        
        return f"{station}_{khz}"
    

    
    def _write_tone_detections(self, minute_boundary: int, time_snap):
        """Write tone detection results from TimeSnapResult."""
        try:
            # ===============================================================
            # Write to HDF5 (L1A Tone Detections) only
            # ===============================================================
            if self.enable_hdf5_writes and self.hdf5_l1a_tones_writer:
                try:
                    # Determine quality flag based on detections and SNR
                    detected_count = sum([
                        time_snap.wwv_detected,
                        time_snap.wwvh_detected,
                        getattr(time_snap, 'chu_detected', False),
                        time_snap.bpm_detected
                    ])
                    
                    max_snr = max([
                        time_snap.wwv_snr_db or -999,
                        time_snap.wwvh_snr_db or -999,
                        getattr(time_snap, 'chu_snr_db', None) or -999,
                        time_snap.bpm_snr_db or -999
                    ])
                    
                    if detected_count == 0:
                        quality_flag = 'MISSING'
                    elif max_snr > 20:
                        quality_flag = 'GOOD'
                    elif max_snr > 10:
                        quality_flag = 'MARGINAL'
                    else:
                        quality_flag = 'BAD'
                    
                    utc_time_iso = datetime.fromtimestamp(minute_boundary, timezone.utc).isoformat().replace('+00:00', 'Z')
                    
                    
                    # Create typed L1A measurement object
                    l1a_tones_measurement = L1ToneDetection(
                        timestamp_utc=utc_time_iso,
                        minute_boundary=minute_boundary,
                        
                        # WWV
                        wwv_detected=bool(time_snap.wwv_detected),
                        wwv_snr_db=float(time_snap.wwv_snr_db) if time_snap.wwv_snr_db is not None else None,
                        wwv_timing_ms=float(time_snap.wwv_timing_ms) if time_snap.wwv_timing_ms is not None else None,
                        
                        # WWVH
                        wwvh_detected=bool(time_snap.wwvh_detected),
                        wwvh_snr_db=float(time_snap.wwvh_snr_db) if time_snap.wwvh_snr_db is not None else None,
                        wwvh_timing_ms=float(time_snap.wwvh_timing_ms) if time_snap.wwvh_timing_ms is not None else None,
                        
                        # CHU (handle potential missing attributes)
                        chu_detected=bool(getattr(time_snap, 'chu_detected', False)),
                        chu_snr_db=float(getattr(time_snap, 'chu_snr_db', None)) if getattr(time_snap, 'chu_snr_db', None) is not None else None,
                        chu_timing_ms=float(getattr(time_snap, 'chu_timing_ms', None)) if getattr(time_snap, 'chu_timing_ms', None) is not None else None,
                        
                        # BPM
                        bpm_detected=bool(time_snap.bpm_detected),
                        bpm_snr_db=float(time_snap.bpm_snr_db) if time_snap.bpm_snr_db is not None else None,
                        bpm_timing_ms=float(time_snap.bpm_timing_ms) if time_snap.bpm_timing_ms is not None else None,
                        
                        # Anchor
                        anchor_station=AnchorStation(time_snap.anchor_station if time_snap.anchor_station else ""),
                        anchor_confidence=float(time_snap.anchor_confidence) if time_snap.anchor_confidence is not None else None,
                        
                        # Metadata
                        quality_flag=ToneQualityFlag(quality_flag),
                        processing_version='3.2.0'
                    )
                    
                    self.hdf5_l1a_tones_writer.write_measurement(l1a_tones_measurement.model_dump())
                    
                except Exception as e:
                    logger.error(f"Failed to write tone detections to HDF5: {e}")
                    
        except Exception as e:
            logger.error(f"Failed to write tone detections: {e}")
    

    
    def _get_frequency_mhz(self) -> float:
        """Get channel frequency in MHz."""
        return self.frequency_hz / 1_000_000
    
    def _should_discriminate(self) -> bool:
        """Check if this frequency requires BCD discrimination.
        
        Returns:
            True if frequency is shared (2.5, 5, 10, 15 MHz) and requires discrimination
            False if frequency is station-specific (20, 25, 3.33, 7.85, 14.67 MHz)
        """
        from .wwv_constants import SHARED_FREQUENCIES
        freq_mhz = self._get_frequency_mhz()
        return freq_mhz in SHARED_FREQUENCIES
    
    def _get_station_from_frequency(self) -> Optional[str]:
        """Get station name from frequency for station-specific frequencies.
        
        Returns:
            Station name ('WWV', 'CHU') if frequency is station-specific
            None if frequency is shared and requires discrimination
        """
        from .wwv_constants import STATION_SPECIFIC_FREQ
        freq_mhz = self._get_frequency_mhz()
        return STATION_SPECIFIC_FREQ.get(freq_mhz)
    
    def _write_bcd_discrimination(self, minute_boundary: int, channel_char):
        """Write BCD discrimination results from ChannelCharacterization.
        
        For station-specific frequencies (20, 25, 3.33, 7.85, 14.67 MHz),
        skip BCD discrimination and directly label the station.
        """
        try:
            # Check if this frequency requires discrimination
            station_from_freq = self._get_station_from_frequency()
            
            if station_from_freq:
                # Station-specific frequency - skip BCD discrimination
                # Log that we're skipping discrimination for this frequency
                freq_mhz = self._get_frequency_mhz()
                logger.debug(
                    f"Skipping BCD discrimination for {station_from_freq}-specific "
                    f"frequency {freq_mhz} MHz"
                )
                
                # Write to HDF5 with direct station labeling (no discrimination needed)
                if self.enable_hdf5_writes and self.hdf5_l1b_writer:
                    try:
                        timestamp_utc = datetime.fromtimestamp(minute_boundary, timezone.utc).isoformat().replace('+00:00', 'Z')
                        
                        # Direct station labeling from frequency
                        l1b_measurement = {
                            'timestamp_utc': timestamp_utc,
                            'minute_boundary': minute_boundary,
                            'bcd_station': station_from_freq,
                            'bcd_confidence': 1.0,  # High confidence - frequency is station-specific
                            'quality_flag': 'GOOD',
                        }
                        
                        self.hdf5_l1b_writer.write_measurement(l1b_measurement)
                        
                    except Exception as e:
                        logger.error(f"Failed to write HDF5 L1B measurement: {e}", exc_info=True)
                
                return  # Skip CSV writing for station-specific frequencies
            
            # Shared frequency - perform normal BCD discrimination
            
            # ================================================================
            # Write to HDF5 (L1B BCD Timecode) only
            # ================================================================
            if self.enable_hdf5_writes and self.hdf5_l1b_writer:
                try:
                    timestamp_utc = datetime.fromtimestamp(minute_boundary, timezone.utc).isoformat().replace('+00:00', 'Z')
                    
                    # Determine BCD station from amplitudes
                    bcd_station = 'UNKNOWN'
                    bcd_confidence = 0.0
                    
                    if channel_char.bcd_wwv_amplitude and channel_char.bcd_wwvh_amplitude:
                        if channel_char.bcd_wwv_amplitude > channel_char.bcd_wwvh_amplitude:
                            bcd_station = 'WWV'
                            bcd_confidence = min(channel_char.bcd_correlation_quality or 0.0, 1.0)
                        else:
                            bcd_station = 'WWVH'
                            bcd_confidence = min(channel_char.bcd_correlation_quality or 0.0, 1.0)
                    elif channel_char.bcd_wwv_amplitude:
                        bcd_station = 'WWV'
                        bcd_confidence = min(channel_char.bcd_correlation_quality or 0.0, 1.0)
                    elif channel_char.bcd_wwvh_amplitude:
                        bcd_station = 'WWVH'
                        bcd_confidence = min(channel_char.bcd_correlation_quality or 0.0, 1.0)
                    
                    # Validate station/frequency combination
                    from .wwv_constants import WWVH_FREQUENCIES
                    freq_mhz = self._get_frequency_mhz()
                    if bcd_station == 'WWVH' and freq_mhz not in WWVH_FREQUENCIES:
                        logger.warning(
                            f"INVALID: BCD discrimination detected {bcd_station} at {freq_mhz} MHz "
                            f"(WWVH only broadcasts on {WWVH_FREQUENCIES}). Rejecting measurement."
                        )
                        bcd_station = 'UNKNOWN'
                        bcd_confidence = 0.0
                    
                    # Determine quality flag
                    if bcd_confidence > 0.8:
                        quality_flag = 'GOOD'
                    elif bcd_confidence > 0.5:
                        quality_flag = 'MARGINAL'
                    elif bcd_confidence > 0:
                        quality_flag = 'BAD'
                    else:
                        quality_flag = 'MISSING'
                    
                    l1b_measurement = {
                        'timestamp_utc': timestamp_utc,
                        'minute_boundary': minute_boundary,
                        'bcd_station': bcd_station,
                        'bcd_confidence': bcd_confidence,
                        'quality_flag': quality_flag,
                    }
                    
                    self.hdf5_l1b_writer.write_measurement(l1b_measurement)
                    
                except Exception as e:
                    logger.error(f"Failed to write HDF5 L1B measurement: {e}", exc_info=True)
                    
        except Exception as e:
            logger.error(f"Failed to write BCD discrimination: {e}")
    

    
    def _is_chu_channel(self) -> bool:
        """Check if this is a CHU channel (3.33, 7.85, or 14.67 MHz)."""
        chu_frequencies = [3.33, 7.85, 14.67]
        return self._get_frequency_mhz() in chu_frequencies
    
    def _write_test_signal(self, minute_boundary: int, iq_samples: np.ndarray, minute_number: int):
        """
        Detect and write test signal for minutes 8 and 44.
        
        Minute 8: WWV test signal (WWVH silent)
        Minute 44: WWVH test signal (WWV silent)
        
        Note: This should only be called for WWV/WWVH channels, not CHU.
        """
        try:
            # Detect test signal using the engine's discriminator
            detection = self.engine.discriminator.test_signal_detector.detect(
                iq_samples=iq_samples,
                minute_number=minute_number,
                sample_rate=self.sample_rate
            )
            
            # Determine station from schedule: minute 8 = WWV, minute 44 = WWVH
            station = 'WWV' if minute_number == 8 else 'WWVH'
            
            # ================================================================
            # Write to HDF5 (L2 Test Signal) only
            # ================================================================
            if self.enable_hdf5_writes and self.hdf5_l2_test_signal_writer:
                try:
                    timestamp_utc = datetime.fromtimestamp(minute_boundary, timezone.utc).isoformat().replace('+00:00', 'Z')
                    
                    # Determine quality flag based on detection confidence
                    if not detection.detected:
                        quality_flag = 'MISSING'
                    elif detection.confidence >= 0.8:
                        quality_flag = 'GOOD'
                    elif detection.confidence >= 0.5:
                        quality_flag = 'MARGINAL'
                    else:
                        quality_flag = 'BAD'
                    
                    # Build comprehensive HDF5 measurement with all schema fields
                    l2_test_signal_measurement = {
                        # Basic metadata
                        'timestamp_utc': timestamp_utc,
                        'minute_boundary_utc': minute_boundary,
                        'minute_number': minute_number,
                        'station': station if detection.detected else '',
                        'frequency_mhz': self._get_frequency_mhz(),
                        
                        # Detection results
                        'detected': bool(detection.detected),
                        'detection_confidence': detection.confidence if detection.confidence is not None else 0.0,
                        
                        # SNR measurements
                        'snr_db': detection.snr_db,
                        'effective_snr_db': detection.effective_snr_db,
                        
                        # Detection scores
                        'multitone_score': detection.multitone_score if detection.multitone_score is not None else None,
                        'chirp_score': detection.chirp_score if detection.chirp_score is not None else None,
                        'burst_score': None,  # Not in current CSV, but in schema
                        'noise_correlation': detection.noise_correlation if detection.noise_correlation is not None else None,
                        
                        # Timing measurements
                        'toa_offset_ms': detection.toa_offset_ms,
                        'toa_source': detection.toa_source or '',
                        'burst_toa_offset_ms': detection.burst_toa_offset_ms,
                        
                        # Channel characterization
                        'delay_spread_ms': detection.delay_spread_ms,
                        'coherence_time_sec': detection.coherence_time_sec,
                        'frequency_selectivity_db': detection.frequency_selectivity_db,
                        
                        # Individual tone powers
                        'tone_power_2khz_db': detection.tone_powers_db.get(2000) if detection.tone_powers_db else None,
                        'tone_power_3khz_db': detection.tone_powers_db.get(3000) if detection.tone_powers_db else None,
                        'tone_power_4khz_db': detection.tone_powers_db.get(4000) if detection.tone_powers_db else None,
                        'tone_power_5khz_db': detection.tone_powers_db.get(5000) if detection.tone_powers_db else None,
                        
                        # Time-series data (10-second windows)
                        'tone_power_timeseries_2khz': detection.tone_power_timeseries.get(2000) if detection.tone_power_timeseries else None,
                        'tone_power_timeseries_3khz': detection.tone_power_timeseries.get(3000) if detection.tone_power_timeseries else None,
                        'tone_power_timeseries_4khz': detection.tone_power_timeseries.get(4000) if detection.tone_power_timeseries else None,
                        'tone_power_timeseries_5khz': detection.tone_power_timeseries.get(5000) if detection.tone_power_timeseries else None,
                        
                        # Fading and scintillation
                        'fading_variance': detection.fading_variance,
                        'scintillation_index': detection.scintillation_index,
                        
                        # Noise segment analysis
                        'noise1_score': detection.noise1_score if detection.noise1_score is not None else None,
                        'noise2_score': detection.noise2_score if detection.noise2_score is not None else None,
                        'noise_coherence_diff': detection.noise_coherence_diff,
                        'transient_detected': bool(detection.transient_detected) if detection.transient_detected is not None else False,
                        
                        # Anomaly detection
                        'anomaly_detected': bool(detection.anomaly_detected) if detection.anomaly_detected is not None else False,
                        'anomaly_type': detection.anomaly_type or 'none',
                        'anomaly_confidence': detection.anomaly_confidence,
                        
                        # Field strength metrics
                        'field_strength_db': detection.field_strength_db,
                        'field_strength_stability': detection.field_strength_stability,
                        
                        # Channel quality
                        'multipath_detected': bool(detection.multipath_detected) if detection.multipath_detected is not None else False,
                        'channel_quality': detection.channel_quality or '',
                        
                        # Quality flag and processing metadata
                        'quality_flag': quality_flag,
                        'processing_version': '3.9.0',
                        'processed_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
                    }
                    
                    self.hdf5_l2_test_signal_writer.write_measurement(l2_test_signal_measurement)
                    
                    if detection.detected:
                        logger.debug(f"Wrote test signal to HDF5: {station}, confidence={detection.confidence:.2f}")
                    
                except Exception as e:
                    logger.error(f"Failed to write test signal to HDF5: {e}", exc_info=True)
            
            if detection.detected:
                logger.info(
                    f"Test signal detected minute {minute_number}: {station}, "
                    f"confidence={detection.confidence:.2f}, SNR={detection.snr_db:.1f}dB"
                )
        except Exception as e:
            logger.error(f"Failed to write test signal: {e}")
    


    def _read_drf_minute(self, target_minute: int):
        """
        Read one minute of data from the binary archive.
        
        Args:
            target_minute: Unix timestamp of minute boundary
            
        Returns:
            Tuple of (iq_samples, system_time, rtp_timestamp) or None if not available
        """
        return self._read_binary_minute(target_minute)
    
    def _read_binary_minute(self, target_minute: int):
        """Read from binary archive format.
        
        When tiered storage is enabled, checks hot buffer (/dev/shm) first,
        then falls back to cold buffer (disk). This ensures analytics reads
        from wherever core recorder wrote the data.
        """
        from datetime import datetime, timezone
        
        dt = datetime.fromtimestamp(target_minute, tz=timezone.utc)
        date_str = dt.strftime('%Y%m%d')
        
        # Use tiered storage manager if available (checks hot buffer first, then cold)
        if self._tiered_manager is not None:
            bin_path = self._tiered_manager.find_minute_file(
                self.channel_name, target_minute, date_str
            )
            if bin_path is not None:
                # Derive json_path from bin_path location
                json_path = bin_path.parent / f"{target_minute}.json"
            else:
                logger.debug(f"Tiered storage: file not found for minute {target_minute}")
                return None
        else:
            # Fallback: use archive_dir directly
            channel_dir = self.archive_dir
            base_path = channel_dir / date_str / f"{target_minute}"
            json_path = channel_dir / date_str / f"{target_minute}.json"
            
            # Try to find the binary file (uncompressed or compressed)
            bin_path = None
            for ext in ['.bin', '.bin.zst', '.bin.lz4']:
                candidate = Path(f"{base_path}{ext}")
                if candidate.exists():
                    bin_path = candidate
                    break
            
            if bin_path is None:
                logger.debug(f"Binary file not found: {base_path}.bin[.zst|.lz4]")
                return None
        
        # Determine compression from file extension
        compression = None
        if bin_path.suffix == '.zst' or str(bin_path).endswith('.bin.zst'):
            compression = 'zstd'
        elif bin_path.suffix == '.lz4' or str(bin_path).endswith('.bin.lz4'):
            compression = 'lz4'
        
        logger.debug(f"Found binary file: {bin_path}, compression={compression}")
        
        try:
            # Read metadata
            metadata = {}
            if json_path.exists():
                try:
                    with open(json_path) as f:
                        metadata = json.load(f)
                    samples_written = metadata.get('samples_written', 0)
                except json.JSONDecodeError as e:
                    logger.warning(f"Corrupted metadata file {json_path}: {e}")
                    samples_written = bin_path.stat().st_size // 8  # Fallback
            else:
                samples_written = bin_path.stat().st_size // 8  # complex64 = 8 bytes
            
            # Read binary file (handle compression)
            if compression == 'zstd':
                try:
                    import zstandard as zstd
                    with open(bin_path, 'rb') as f:
                        dctx = zstd.ZstdDecompressor()
                        decompressed = dctx.decompress(f.read())
                    iq_samples = np.frombuffer(decompressed, dtype=np.complex64)
                except ImportError:
                    logger.warning("zstandard not installed, cannot read .bin.zst files")
                    return None
            elif compression == 'lz4':
                try:
                    import lz4.frame
                    with open(bin_path, 'rb') as f:
                        decompressed = lz4.frame.decompress(f.read())
                    iq_samples = np.frombuffer(decompressed, dtype=np.complex64)
                except ImportError:
                    logger.warning("lz4 not installed, cannot read .bin.lz4 files")
                    return None
            else:
                # Memory-map for zero-copy reading (uncompressed)
                iq_samples = np.memmap(bin_path, dtype=np.complex64, mode='r')
            
            samples_per_minute = self.sample_rate * 60
            completeness = 100 * len(iq_samples) / samples_per_minute
            logger.debug(f"Read {len(iq_samples)} samples ({completeness:.1f}% of {samples_per_minute})")
            if len(iq_samples) < samples_per_minute * 0.4:  # Need at least 40% (relaxed for partial RTP streams)
                logger.warning(f"Incomplete minute: {len(iq_samples)}/{samples_per_minute} ({completeness:.1f}%)")
                return None
            
            # Pad if slightly short
            if len(iq_samples) < samples_per_minute:
                padded = np.zeros(samples_per_minute, dtype=np.complex64)
                padded[:len(iq_samples)] = iq_samples
                iq_samples = padded
            
            # CRITICAL FIX (2026-01-10): Calculate actual Unix time from RTP timestamp
            # Previous code used minute boundary (target_minute), which created 20-30ms systematic offset
            # because buffers don't start exactly at :00.000
            logger.debug(f"RTP offset check: json_path.exists()={json_path.exists()}, has_rtp={'start_rtp_timestamp' in metadata}, metadata_keys={list(metadata.keys())[:5]}")
            if json_path.exists() and 'start_rtp_timestamp' in metadata:
                rtp_timestamp = int(metadata['start_rtp_timestamp'])
                logger.debug(f"RTP metadata found: rtp_timestamp={rtp_timestamp}, json_path={json_path}")
                
                # Learn RTP-to-Unix offset from metadata if available
                # CRITICAL FIX: Validate offset isn't stale (e.g., from before recorder restart)
                inst_offset = target_minute - (rtp_timestamp / self.sample_rate)
                
                # Check if current offset is stale (differs by >1 second from expected)
                if self._rtp_to_unix_offset is not None:
                    offset_drift = abs(inst_offset - self._rtp_to_unix_offset)
                    if offset_drift > 1.0:
                        logger.warning(
                            f"Stale RTP offset detected! Current: {self._rtp_to_unix_offset:.3f}s, "
                            f"Expected: {inst_offset:.3f}s, Drift: {offset_drift:.3f}s. "
                            f"Resetting offset learning (likely recorder restart)."
                        )
                        self._rtp_to_unix_offset = None
                        self._offset_samples = []
                
                if self._rtp_to_unix_offset is None and len(self._offset_samples) < 10:
                    # Calculate offset: unix_time = rtp_timestamp / sample_rate + offset
                    # We know the buffer is for target_minute, so use that as reference
                    self._offset_samples.append(inst_offset)
                    
                    if len(self._offset_samples) >= 10:
                        # Average over 10 samples to reduce jitter
                        self._rtp_to_unix_offset = sum(self._offset_samples) / len(self._offset_samples)
                        logger.info(f"RTP-to-Unix offset established: {self._rtp_to_unix_offset:.6f}s")
                    else:
                        # Use first sample immediately for processing
                        self._rtp_to_unix_offset = inst_offset
                
                # Convert RTP timestamp to Unix time using the established offset
                # This gives us the ACTUAL time of the first sample, not the idealized minute boundary
                if self._rtp_to_unix_offset is not None:
                    system_time = rtp_timestamp / self.sample_rate + self._rtp_to_unix_offset
                else:
                    # Fallback during initialization
                    system_time = float(target_minute)
            else:
                # Fallback: use minute boundary (less accurate, but better than nothing)
                system_time = float(target_minute)
                rtp_timestamp = int(target_minute * self.sample_rate)
                logger.warning(f"No RTP timestamp in metadata, using minute boundary as fallback")
            
            logger.debug(f"Read {len(iq_samples)} samples from binary for minute {target_minute}")
            return iq_samples, system_time, rtp_timestamp
            
        except Exception as e:
            logger.debug(f"Error reading binary: {e}")
            return None
    
    def _get_latest_minute(self) -> int:
        """Get the latest complete minute boundary from wall clock time.
        
        Uses system time as the authoritative source for continuous operation.
        Maintains a 2-minute safety buffer to ensure data files are complete.
        
        This is the correct approach for a real-time system:
        - Wall clock advances continuously
        - Service processes each new minute as it becomes available
        - Binary file discovery is used only for validation/backfill
        """
        now = time.time()
        # Go back 2 minutes for safety (data completeness)
        latest_minute = ((int(now) // 60) - 2) * 60
        
        # Optional: Verify data exists (but don't block on it)
        binary_latest = self._get_latest_binary_minute()
        if binary_latest is not None and binary_latest > latest_minute:
            # Binary data is ahead of our safety buffer - use it
            return binary_latest
        
        return latest_minute
    
    def _get_latest_binary_minute(self) -> Optional[int]:
        """Get latest minute from binary archive (checks hot and cold storage)."""
        from datetime import datetime, timezone
        from ..paths import channel_name_to_dir
        
        minutes = []
        channel_dir_name = channel_name_to_dir(self.channel_name)
        today = datetime.now(timezone.utc).strftime('%Y%m%d')
        
        # Check hot buffer (RAM)
        if self._tiered_manager is not None:
            hot_channel_dir = self._tiered_manager.hot_root / channel_dir_name
            day_dir = hot_channel_dir / today
            if day_dir.exists():
                bin_files = list(day_dir.glob('*.bin'))
                for f in bin_files:
                    try:
                        minutes.append(int(f.stem))
                    except ValueError:
                        pass
        
        # Check cold storage (disk)
        if 'raw_buffer' in str(self.archive_dir):
            channel_dir = self.archive_dir
        else:
            binary_dir = self.archive_dir.parent.parent / 'raw_buffer'
            channel_dir = binary_dir / channel_dir_name
        
        day_dir = channel_dir / today
        if day_dir.exists():
            bin_files = list(day_dir.glob('*.bin'))
            for f in bin_files:
                try:
                    minutes.append(int(f.stem))
                except ValueError:
                    pass
        
        if not minutes:
            return None
        
        # Return second-to-last (last might be incomplete)
        # Go back 2 minutes for safety margin
        latest = max(minutes)
        return latest - 120  # 2 minutes behind
    
    def _calculate_carrier_snr(self, iq_samples: np.ndarray) -> float:
        """
        Calculate base channel SNR from IQ samples.
        
        This measures the signal-to-noise ratio of the carrier using
        the ratio of mean amplitude to standard deviation of amplitude.
        This is the base channel SNR before station discrimination.
        
        Args:
            iq_samples: Complex IQ samples
            
        Returns:
            SNR in dB, or None if calculation fails
        """
        try:
            # Filter out zero-padded samples (gaps in data)
            non_zero_mask = (iq_samples.real != 0) | (iq_samples.imag != 0)
            valid_samples = iq_samples[non_zero_mask]
            
            if len(valid_samples) < 1000:  # Need minimum samples
                return None
            
            # Calculate amplitude (magnitude)
            amplitude = np.abs(valid_samples)
            
            # Signal is mean amplitude, noise is std dev of amplitude
            signal = np.mean(amplitude)
            noise = np.std(amplitude)
            
            # Avoid division by zero
            if noise < 1e-10 or signal < 1e-10:
                return None
            
            # SNR in dB = 20 * log10(signal/noise) for amplitude ratio
            snr_db = 20 * np.log10(signal / noise)
            
            # Clamp to reasonable range for HF signals
            if np.isnan(snr_db) or np.isinf(snr_db):
                return None
            
            return float(np.clip(snr_db, -20, 60))
        except Exception:
            return None

    def _query_radiod_snr(self) -> Optional[float]:
        """
        Query radiod for current channel SNR.
        
        This is the base channel SNR before any station discrimination.
        Returns SNR in dB or None if unavailable.
        """
        try:
            from ka9q import discover_channels
            
            # Use configured status address instead of default
            # Phase2AnalyticsService init doesn't explicitly take status_address,
            # but we can try to get it from station_config (which is full config often)
            # or default to FQDN if not present.
            # BEST PRACTICE: Use same hardcoded FQDN default as we assume in core
            # if we can't get it from config easily here without refactoring __init__.
            # Ideally passthrough from __init__, but for hotfix:
            status_address = 'bee1-hf-status.local'
            
            # If self.station_config has a parent config context? No. 
            # But the service is launched with --config usually?
            # Actually Phase2AnalyticsService is usually launched by CLI which parses config.
            # Let's check __init__ for config passing.
            # It has `station_config`.
            # Let's see if we can get it from there. Not typical.
            # Safe bet: Use 'bee1-hf-status.local' as primary default over IP.
            
            channels = {}
            # Retry discovery (3 attempts)
            for attempt in range(3):
                found = discover_channels(status_address, listen_duration=2.5)
                if found:
                    channels = found
                    break
                time.sleep(1.0)
            
            # Find our channel by frequency
            for ssrc, ch_info in channels.items():
                if abs(ch_info.frequency - self.frequency_hz) < 100:  # Within 100 Hz
                    snr = ch_info.snr
                    # Handle -inf (no signal)
                    if snr is not None and not np.isinf(snr):
                        return float(snr)
                    return None
            return None
        except Exception as e:
            logger.debug(f"Failed to query radiod SNR: {e}")
            return None

    def _write_status(self):
        """Write status file for web-ui monitoring."""
        try:
            # Query radiod for current channel SNR (base channel SNR)
            self.last_radiod_snr_db = self._query_radiod_snr()
            
            # Build time_snap info from last result
            time_snap_dict = None
            if self.last_result and self.last_result.time_snap:
                ts = self.last_result.time_snap
                time_snap_dict = {
                    'established': True,
                    'utc_timestamp': time.time(),
                    'source': ts.anchor_station or 'unknown',
                    'confidence': ts.anchor_confidence
                }
            
            status = {
                'service': 'phase2_analytics_service',
                'version': '2.0',
                'timestamp': datetime.now(timezone.utc).isoformat(),
                'uptime_seconds': int(time.time() - self.start_time),
                'pid': None,
                'channels': {
                    self.channel_name: {
                        'channel_name': self.channel_name,
                        'frequency_hz': self.frequency_hz,
                        'minutes_processed': self.minutes_processed,
                        'last_processed_time': datetime.fromtimestamp(
                            self.last_processed_minute, timezone.utc
                        ).isoformat() if self.last_processed_minute else None,
                        'time_snap': time_snap_dict,
                        'quality_metrics': {
                            'last_completeness_pct': 100.0 if self.last_result else 0.0,
                            'last_packet_loss_pct': 0.0,
                            'last_snr_db': self.last_radiod_snr_db,  # Base channel SNR from radiod
                            'carrier_snr_db': self.last_carrier_snr_db  # Carrier SNR from IQ calculation
                        }
                    }
                },
                'overall': {
                    'channels_processing': 1,
                    'total_minutes_processed': self.minutes_processed
                }
            }
            
            # Add D_clock result if available
            if self.last_result:
                status['channels'][self.channel_name]['d_clock_ms'] = self.last_result.d_clock_ms
                # Issue 6.2: quality_grade replaced with uncertainty_ms
                # Compute backwards-compatible grade from uncertainty for web UI
                unc = self.last_result.uncertainty_ms
                if unc < 1.0:
                    quality_grade = 'A'
                elif unc < 3.0:
                    quality_grade = 'B'
                elif unc < 10.0:
                    quality_grade = 'C'
                else:
                    quality_grade = 'D'
                status['channels'][self.channel_name]['quality_grade'] = quality_grade
                status['channels'][self.channel_name]['uncertainty_ms'] = unc
                status['channels'][self.channel_name]['confidence'] = self.last_result.confidence
                
                # Add per-station tick SNR values (from multi-station detector)
                # These are the SNR of the detected timing tick for each station
                # Report 0 if no tick was detected for that station
                if self.last_result.time_snap:
                    ts = self.last_result.time_snap
                    status['channels'][self.channel_name]['station_snr'] = {
                        'wwv_snr_db': ts.wwv_snr_db if ts.wwv_snr_db is not None else 0.0,
                        'wwvh_snr_db': ts.wwvh_snr_db if ts.wwvh_snr_db is not None else 0.0,
                        'chu_snr_db': ts.chu_snr_db if ts.chu_snr_db is not None else 0.0,
                        'bpm_snr_db': ts.bpm_snr_db if ts.bpm_snr_db is not None else 0.0,
                    }
                
                if self.last_result.solution:
                    sol = self.last_result.solution
                    status['channels'][self.channel_name]['station'] = sol.station
                    status['channels'][self.channel_name]['propagation_mode'] = sol.propagation_mode
                    status['channels'][self.channel_name]['propagation_delay_ms'] = getattr(sol, 't_propagation_ms', 0)
                    status['channels'][self.channel_name]['n_hops'] = sol.n_hops
                    # Mode candidates for Mode Ridge visualization
                    status['channels'][self.channel_name]['mode_candidates'] = getattr(sol, 'mode_candidates', [])
            
            # Add convergence model state - shows convergence progress and lock status
            if hasattr(self, 'last_convergence_result') and self.last_convergence_result:
                conv = self.last_convergence_result
                status['channels'][self.channel_name]['convergence'] = {
                    'state': conv.state.value,
                    'is_locked': bool(conv.is_locked),  # Convert numpy bool to Python bool
                    'sample_count': int(conv.sample_count),
                    'uncertainty_ms': float(conv.uncertainty_ms) if conv.uncertainty_ms != float('inf') else None,
                    'convergence_progress': float(conv.convergence_progress),
                    'residual_ms': float(conv.residual_ms) if conv.residual_ms is not None else None,
                    'is_anomaly': bool(conv.is_anomaly)  # Convert numpy bool to Python bool
                }
                # Also expose uncertainty at channel level for consensus weighting
                status['channels'][self.channel_name]['uncertainty_ms'] = (
                    conv.uncertainty_ms if conv.uncertainty_ms != float('inf') else 100.0
                )
            
            # Write atomically
            temp_file = self.status_file.with_suffix('.tmp')
            with open(temp_file, 'w') as f:
                json.dump(status, f, indent=2)
            temp_file.replace(self.status_file)
            
        except Exception as e:
            logger.error(f"Failed to write status: {e}")
    
    def _load_broadcast_calibration(self) -> Dict[str, float]:
        """
        Load learned calibration offsets for this channel.
        
        Reads from state/broadcast_calibration.json produced by MultiBroadcastFusion.
        Returns a dictionary mapping Station Name -> Offset (ms).
        """
        # Assume data standard layout: output_dir is phase2/{CHANNEL}
        # Fusion writes to {data_root}/state/broadcast_calibration.json
        # So we look in output_dir/../../state/broadcast_calibration.json
        try:
            # self.output_dir = .../phase2/WWV_10_MHz
            # desired = .../state/broadcast_calibration.json
            cal_file = self.output_dir.parent.parent / 'state' / 'broadcast_calibration.json'
            
            if not cal_file.exists():
                return {}
            
            with open(cal_file) as f:
                data = json.load(f)
            
            offsets = {}
            target_freq_mhz = self.frequency_hz / 1e6
            
            # Key format: "STATION_FREQ" (e.g. "WWV_10.00")
            for key, cal_data in data.items():
                parts = key.rsplit('_', 1)
                if len(parts) != 2:
                    continue
                station, freq_str = parts[0], parts[1]
                
                try:
                    cal_freq = float(freq_str)
                    # Check if this calibration applies to our frequency
                    if abs(cal_freq - target_freq_mhz) < 0.01:
                        offsets[station] = cal_data['offset_ms']
                except ValueError:
                    continue
            
            if offsets:
                logger.debug(f"Loaded calibration offsets: {offsets}")
            
            return offsets
            
        except Exception as e:
            logger.debug(f"Failed to load calibration: {e}")
            return {}

    def process_minute(self, minute_boundary: int) -> bool:
        """
        Process one minute of data.
        
        Args:
            minute_boundary: Unix timestamp of minute start
            
        Returns:
            True if processed successfully
        """
        if minute_boundary in self.processed_minutes:
            return False
        
        # Read binary data for this minute
        data = self._read_binary_minute(minute_boundary)
        if data is None:
            logger.debug(f"No data available for minute {minute_boundary}")
            return False
        
        iq_samples, system_time, rtp_timestamp = data
        
        # Calculate base channel SNR (before station discrimination)
        self.last_carrier_snr_db = self._calculate_carrier_snr(iq_samples)
        
        # Calculate carrier power in dB (for power graphs)
        # Validate IQ samples to prevent NaN propagation
        if len(iq_samples) > 0 and not np.any(np.isnan(iq_samples)):
            power_linear = np.mean(np.abs(iq_samples) ** 2)
            # Check for valid power before log
            if power_linear > 0 and not np.isnan(power_linear) and not np.isinf(power_linear):
                self.last_carrier_power_db = 10 * np.log10(power_linear)
            else:
                self.last_carrier_power_db = None
                logger.debug(f"Invalid power_linear: {power_linear}, setting carrier_power_db to None")
        else:
            self.last_carrier_power_db = None
            logger.debug("IQ samples contain NaN or are empty, setting carrier_power_db to None")
        
        # Detect gaps in source data (zeros indicate gaps from Phase 1)
        zero_mask = (iq_samples.real == 0) & (iq_samples.imag == 0)
        gap_samples = int(np.sum(zero_mask))
        
        try:
            # Load latest calibration offsets (feedback from fusion)
            calibration_offsets = self._load_broadcast_calibration()
            
            # Process through Phase 2 engine
            # Returns LIST of results (one per detected station)
            results = self.engine.process_minute(
                iq_samples=iq_samples,
                system_time=system_time,
                rtp_timestamp=rtp_timestamp,
                calibration_offsets=calibration_offsets
            )
            
            self.minutes_processed += 1
            self.last_processed_minute = minute_boundary
            self.processed_minutes.add(minute_boundary)
            
            if results:
                # Identify the "primary" result for status reporting and single-row CSVs
                # Sort by confidence so the "best" one is last keys
                # (We want last_convergence_result to be the best one)
                results.sort(key=lambda r: r.confidence)
                primary_result = results[-1]
                
                self.last_result = primary_result
                
                # ===== NEW: D_clock Continuity Validation (Critical Fix - 2025-12-31) =====
                # Validate that D_clock hasn't jumped unrealistically
                # This catches CHU frame slips (33ms jumps) and other timing errors
                if primary_result.d_clock_ms is not None:
                   # D_clock continuity validation (GPSDO temporal stability check)
                # CRITICAL: Skip during bootstrap (no previous measurement)
                    if self.last_d_clock_ms is not None:
                        is_valid, reason = self.engine._validate_d_clock_continuity(
                            current_d_clock_ms=primary_result.d_clock_ms,
                            previous_d_clock_ms=self.last_d_clock_ms,
                            dt_seconds=(minute_boundary - self.last_minute_unix) if self.last_minute_unix else 60,
                            channel_name=self.channel_name
                        )
                        
                        if not is_valid:
                            logger.error(f"{self.channel_name}: D_clock continuity check FAILED - {reason}")
                            logger.error(f"  Current: {primary_result.d_clock_ms:.2f}ms, "
                                        f"Previous: {self.last_d_clock_ms:.2f}ms, "
                                        f"Delta: {abs(primary_result.d_clock_ms - self.last_d_clock_ms):.2f}ms")
                            
                            # Mark result as invalid
                            primary_result.confidence = 0.0
                            if hasattr(primary_result, 'solution') and primary_result.solution:
                                primary_result.solution.confidence = 0.0
                            
                            # Don't update last_d_clock_ms - keep previous value
                            logger.warning(f"{self.channel_name}: Rejecting measurement due to continuity failure")
                        else:
                            # Valid measurement - update state
                            self.last_d_clock_ms = primary_result.d_clock_ms
                            self.last_minute_unix = minute_boundary
                            logger.debug(f"{self.channel_name}: Continuity OK - {reason}")
                    else:
                        # BOOTSTRAP MODE: Accept first measurement regardless of confidence
                        # This allows the Kalman filter to initialize
                        self.last_d_clock_ms = primary_result.d_clock_ms
                        self.last_minute_unix = minute_boundary
                        primary_solution = primary_result.solution # Ensure primary_solution is defined for logging
                        logger.info(f"🔓 {self.channel_name}: BOOTSTRAP - accepting initial measurement "
                                   f"(D_clock={primary_result.d_clock_ms:.2f}ms, "
                                   f"confidence={primary_solution.confidence if primary_solution else 0:.2f})")
                
                # Write to CSV time series (coordinated path)
                # Loop through ALL results to capture multi-station data
                for res in results:
                    self._write_clock_offset(res, minute_boundary, rtp_timestamp)
                
                # Write carrier power for power graphs
                # Loop through ALL results to capture per-station power metadata
                for res in results:
                    solution = res.solution if hasattr(res, 'solution') else None
                    time_snap = res.time_snap if hasattr(res, 'time_snap') else None
                    
                    # Compute quality_grade from uncertainty for logging/CSV
                    result_unc = res.uncertainty_ms
                    result_grade = 'A' if result_unc < 1.0 else 'B' if result_unc < 3.0 else 'C' if result_unc < 10.0 else 'D'
                    
                    self._write_carrier_power(
                        minute_boundary=minute_boundary,
                        power_db=self.last_carrier_power_db,
                        snr_db=self.last_carrier_snr_db,
                        wwv_tone_db=time_snap.wwv_snr_db if time_snap else None,
                        wwvh_tone_db=time_snap.wwvh_snr_db if time_snap else None,
                        station=solution.station if solution else None,
                        quality_grade=result_grade,
                        channel_char=res.channel if hasattr(res, 'channel') else None
                    )
                
                # Write discrimination method CSVs
                # These are "per-minute" analyses, generally shared across detections
                # We use the PRIMARY result to avoid duplicate rows
                time_snap = primary_result.time_snap
                channel_char = primary_result.channel
                primary_solution = primary_result.solution
                primary_unc = primary_result.uncertainty_ms
                
                if time_snap:
                    self._write_tone_detections(minute_boundary, time_snap)
                
                if channel_char:
                    self._write_bcd_discrimination(minute_boundary, channel_char)
                
                logger.info(
                    f"Processed minute {minute_boundary}: {len(results)} stations detected. "
                    f"Primary: {primary_solution.station if primary_solution else '?'}, "
                    f"D_clock={primary_result.d_clock_ms:+.2f}ms, "
                    f"uncertainty={primary_unc:.1f}ms"
                )
                
                # Update timing calibrator from successful detections
                # This enables bootstrap→calibrated→measurement mode progression
                for res in results:
                    solution = res.solution if hasattr(res, 'solution') else None
                    time_snap = res.time_snap if hasattr(res, 'time_snap') else None
                    
                    if solution and time_snap and solution.confidence > 0.0:
                        # Extract detection parameters
                        station = solution.station
                        propagation_delay_ms = solution.t_propagation_ms if solution.t_propagation_ms else 0.0
                        d_clock_ms = res.d_clock_ms
                        
                        # Get SNR from time_snap (station-specific)
                        snr_db = 0.0
                        if station == 'WWV' and time_snap.wwv_snr_db is not None:
                            snr_db = time_snap.wwv_snr_db
                        elif station == 'WWVH' and time_snap.wwvh_snr_db is not None:
                            snr_db = time_snap.wwvh_snr_db
                        elif station == 'CHU' and time_snap.chu_snr_db is not None:
                            snr_db = time_snap.chu_snr_db
                        
                        # Update calibrator
                        try:
                            self.timing_calibrator.update_from_detection(
                                station=station,
                                frequency_mhz=self.frequency_hz / 1e6,
                                channel_name=self.channel_name,
                                d_clock_ms=d_clock_ms,
                                propagation_delay_ms=propagation_delay_ms,
                                snr_db=snr_db,
                                confidence=solution.confidence,
                                rtp_timestamp=rtp_timestamp,
                                minute_boundary=minute_boundary,
                                arrival_rtp=res.solution.arrival_rtp if res.solution else None
                            )
                            # Format values, handling None during bootstrap
                            prop_str = f"{propagation_delay_ms:.1f}ms" if propagation_delay_ms is not None else "N/A"
                            snr_str = f"{snr_db:.1f}dB" if snr_db is not None else "N/A"
                            
                            logger.debug(
                                f"Updated timing calibrator: {station} @ {self.frequency_hz/1e6:.2f}MHz, "
                                f"prop_delay={prop_str}, SNR={snr_str}"
                            )
                        except Exception as e:
                            logger.warning(f"Failed to update timing calibrator: {e}")

            else:
                snr_str = f"{self.last_carrier_snr_db:.1f}" if self.last_carrier_snr_db is not None else "N/A"
                logger.debug(
                    f"Processed minute {minute_boundary}: no timing result, "
                    f"carrier_snr={snr_str}dB"
                )
                self.last_result = None
            
            # Write test signal for minutes 8 and 44 (channel sounding minutes)
            # Run OUTSIDE of if result: block since test signal detection doesn't need timing lock
            # Skip CHU channels - they don't broadcast WWV/WWVH test signals
            minute_number = (minute_boundary // 60) % 60
            if minute_number in [8, 44] and not self._is_chu_channel():
                self._write_test_signal(minute_boundary, iq_samples, minute_number)
            
            # Write audio tones removed (CSV legacy)
            
            # Write transmission time solution removed (CSV legacy)
            
            return True
                
        except Exception as e:
            logger.error(f"Error processing minute {minute_boundary}: {e}", exc_info=True)
            return False
    
    def run(self):
        """Main service loop."""
        self.running = True
        logger.info(f"Starting Phase 2 analytics service for {self.channel_name}")
        
        while self.running:
            try:
                # Get latest complete minute
                latest_minute = self._get_latest_minute()
                logger.debug(f"Main loop: latest_minute={latest_minute}, running={self.running}")
                
                # Always attempt to process latest minute
                # New data may have arrived since last poll
                logger.info(f"Calling process_minute for {latest_minute}")
                self.process_minute(latest_minute)
                logger.info(f"Completed process_minute for {latest_minute}")
                
                # Backfill gaps if enabled
                if self.backfill_gaps and latest_minute > 0:
                    lookback_count = 0
                    # Standard minute is 60 seconds
                    back_minute = latest_minute - 60
                    
                    while self.running and lookback_count < self.max_backfill:
                        if back_minute not in self.processed_minutes:
                            if self.process_minute(back_minute):
                                logger.info(f"Backfilled missing minute {back_minute} for {self.channel_name}")
                            else:
                                # Data might be missing for this minute. 
                                # Continue backfilling unless we hit a very large gap?
                                pass
                        
                        back_minute -= 60
                        lookback_count += 1
                
                # Write status
                self._write_status()
                
                # Sleep until next poll
                logger.debug(f"Sleeping for {self.poll_interval}s")
                time.sleep(self.poll_interval)
                
            except Exception as e:
                logger.error(f"Error in main loop: {e}", exc_info=True)
                time.sleep(self.poll_interval)
        
        logger.info("Phase 2 analytics service stopped")
    
    def stop(self):
        """Stop the service."""
        self.running = False


def main():
    """Command-line entry point."""
    parser = argparse.ArgumentParser(
        description='Phase 2 Analytics Service - Process raw_buffer to timing products'
    )
    parser.add_argument('--archive-dir', required=True, help='raw_buffer/{CHANNEL} directory')
    parser.add_argument('--output-dir', required=True, help='Output directory')
    parser.add_argument('--channel-name', required=True, help='Channel name')
    parser.add_argument('--frequency-hz', type=float, required=True, help='Center frequency')
    parser.add_argument('--sample-rate', type=int, default=24000, help='Sample rate')
    parser.add_argument('--grid-square', default='', help='Receiver grid square')
    parser.add_argument('--poll-interval', type=float, default=10.0, help='Poll interval')
    parser.add_argument('--log-level', default='INFO', help='Log level')
    
    # Additional args
    parser.add_argument('--state-file', help='State file (not used)')
    parser.add_argument('--backfill-gaps', action='store_true', help='Backfill gaps (not used)')
    parser.add_argument('--max-backfill', type=int, help='Max backfill (not used)')
    parser.add_argument('--callsign', help='Callsign')
    parser.add_argument('--receiver-name', help='Receiver name')
    parser.add_argument('--station-id', help='Station ID')
    parser.add_argument('--instrument-id', help='Instrument ID')
    parser.add_argument('--latitude', type=float, help='Precise latitude (improves timing ~16μs)')
    parser.add_argument('--longitude', type=float, help='Precise longitude (improves timing ~16μs)')
    parser.add_argument('--use-tiered-storage', action='store_true',
                        help='Use tiered storage (read from /dev/shm hot buffer first, then disk)')
    
    args = parser.parse_args()
    
    # Setup logging
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format='%(asctime)s %(levelname)s:%(name)s:%(message)s'
    )
    
    station_id = args.station_id
    instrument_id = args.instrument_id

    # Build station config
    station_config = {
        'callsign': args.callsign,
        'grid_square': args.grid_square,
        'receiver_name': args.receiver_name,
        'station_id': station_id,
        'instrument_id': instrument_id,
        'latitude': args.latitude,
        'longitude': args.longitude
    }
    
    # Create service
    service = Phase2AnalyticsService(
        archive_dir=Path(args.archive_dir),
        output_dir=Path(args.output_dir),
        channel_name=args.channel_name,
        frequency_hz=args.frequency_hz,
        sample_rate=args.sample_rate,
        receiver_grid=args.grid_square,
        station_config=station_config,
        poll_interval=args.poll_interval,
        use_tiered_storage=args.use_tiered_storage,
        backfill_gaps=args.backfill_gaps or False,
        max_backfill=args.max_backfill or 100
    )
    
    # Handle signals
    def signal_handler(signum, frame):
        logger.info(f"Received signal {signum}, stopping...")
        service.stop()
    
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    # Run
    service.run()


if __name__ == '__main__':
    main()
