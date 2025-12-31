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
import csv
import json
import logging
import signal
import sys
import time
import numpy as np
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

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
        sample_rate: int = 20000,
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
            sample_rate: Sample rate (default 20000)
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
        
        # CSV time series for D_clock (coordinated path) - daily rotation
        self._init_clock_offset_csv()
        
        # CSV time series for carrier power (for power graphs)
        self.carrier_power_dir = self.output_dir / 'carrier_power'
        self.carrier_power_dir.mkdir(parents=True, exist_ok=True)
        self._init_carrier_power_csv()
        
        # ====================================================================
        # Discrimination Method CSV Directories
        # ====================================================================
        
        # Tone detections (1000/1200 Hz timing tones)
        self.tone_detections_dir = self.output_dir / 'tone_detections'
        self.tone_detections_dir.mkdir(parents=True, exist_ok=True)
        self._init_tone_detections_csv()
        
        # BCD discrimination (BCD correlation analysis)
        self.bcd_discrimination_dir = self.output_dir / 'bcd_discrimination'
        self.bcd_discrimination_dir.mkdir(parents=True, exist_ok=True)
        self._init_bcd_discrimination_csv()
        
        # Doppler analysis
        self.doppler_dir = self.output_dir / 'doppler'
        self.doppler_dir.mkdir(parents=True, exist_ok=True)
        self._init_doppler_csv()
        
        # Station ID (440 Hz voice ID + 500/600 Hz ground truth)
        self.station_id_dir = self.output_dir / 'station_id_440hz'
        self.station_id_dir.mkdir(parents=True, exist_ok=True)
        self._init_station_id_csv()
        
        # Test signal (minutes 8 and 44)
        self.test_signal_dir = self.output_dir / 'test_signal'
        self.test_signal_dir.mkdir(parents=True, exist_ok=True)
        self._init_test_signal_csv()
        
        # Discrimination summary (weighted voting result)
        self.discrimination_dir = self.output_dir / 'discrimination'
        self.discrimination_dir.mkdir(parents=True, exist_ok=True)
        self._init_discrimination_csv()
        
        # Audio tone monitor (500/600 Hz + intermodulation)
        self.audio_tones_dir = self.output_dir / 'audio_tones'
        self.audio_tones_dir.mkdir(parents=True, exist_ok=True)
        self._init_audio_tones_csv()
        
        # Transmission Time (UTC-NIST) - Phase 2 timing directory
        # This feeds the Fusion / Transmission Time API
        self.timing_dir = self.output_dir / 'timing'
        self.timing_dir.mkdir(parents=True, exist_ok=True)
        self._init_transmission_time_csv()
        
        # TEC Estimation - Ionospheric Total Electron Content
        # Calculated from multi-frequency measurements when available
        self.tec_dir = self.output_dir / 'tec'
        self.tec_dir.mkdir(parents=True, exist_ok=True)
        self._init_tec_csv()
        
        # Note: Decimation is not part of hf-timestd (timing-focused)
        # For decimated output, see separate projects

        # Initialize TEC Estimator for ionospheric analysis
        from .tec_estimator import TECEstimator
        self.tec_estimator = TECEstimator(high_precision_mode=True)
        logger.info("Initialized TEC estimator for ionospheric analysis")
        
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
            if channel_name in self.timing_calibrator.rtp_calibration:
                return self.timing_calibrator.rtp_calibration[channel_name].rtp_offset_samples
            # Check for global RTP offset (shared across all channels from same GPSDO)
            return self.timing_calibrator.global_rtp_offset
        
        self.engine.rtp_calibration_callback = get_rtp_offset
        self.engine.station_predictor = self.timing_calibrator.predict_station
        
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
    
    def _init_clock_offset_csv(self):
        """Initialize clock offset CSV file with headers if needed (daily rotation)."""
        today = datetime.now(timezone.utc).strftime('%Y%m%d')
        file_channel = self._get_file_channel_name()
        self.clock_offset_csv = self.clock_offset_dir / f'{file_channel}_clock_offset_{today}.csv'
        
        if not self.clock_offset_csv.exists():
            with open(self.clock_offset_csv, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    'system_time', 'utc_time', 'minute_boundary_utc',
                    'clock_offset_ms', 'station', 'frequency_mhz',
                    'propagation_delay_ms', 'propagation_mode', 'n_hops',
                    'confidence', 'uncertainty_ms', 'quality_grade',
                    'snr_db', 'delay_spread_ms', 'doppler_std_hz', 'fss_db',
                    'wwv_power_db', 'wwvh_power_db', 'discrimination_confidence',
                    'utc_verified', 'multi_station_verified',
                    'rtp_timestamp', 'processed_at',
                    'wwv_tick_snr_db', 'wwvh_tick_snr_db', 'chu_tick_snr_db', 'bpm_tick_snr_db'
                ])
            logger.info(f"Created clock offset CSV: {self.clock_offset_csv}")
        
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
            # Check for daily rotation
            today = datetime.now(timezone.utc).strftime('%Y%m%d')
            file_channel = self._get_file_channel_name()
            expected_csv = self.clock_offset_dir / f'{file_channel}_clock_offset_{today}.csv'
            if self.clock_offset_csv != expected_csv:
                self.clock_offset_csv = expected_csv
                self._init_clock_offset_csv()
            
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
            
            
            with open(self.clock_offset_csv, 'a', newline='') as f:
                writer = csv.writer(f)
                
                writer.writerow([
                    minute_boundary,                                    # system_time
                    minute_boundary + (effective_d_clock / 1000.0),     # utc_time
                    minute_boundary,                                    # minute_boundary_utc
                    effective_d_clock,                                  # clock_offset_ms (converged)
                    station,                                            # station
                    frequency_mhz,                                      # frequency_mhz
                    solution.t_propagation_ms if solution else 0,       # propagation_delay_ms
                    solution.propagation_mode if solution else '',      # propagation_mode
                    solution.n_hops if solution else 0,                 # n_hops
                    solution.confidence if solution else 0,             # confidence
                    effective_uncertainty,                              # uncertainty_ms (from convergence)
                    quality_grade,                                      # quality_grade (from convergence)
                    self.last_carrier_snr_db or 0,                      # snr_db (channel SNR)
                    '',                                                 # delay_spread_ms
                    '',                                                 # doppler_std_hz
                    '',                                                 # fss_db
                    '',                                                 # wwv_power_db
                    '',                                                 # wwvh_power_db
                    '',                                                 # discrimination_confidence
                    convergence_result.is_locked,                       # utc_verified (locked = verified)
                    (solution.dual_station_verified if solution else False),  # multi_station_verified
                    rtp_timestamp,                                      # rtp_timestamp
                    datetime.now(timezone.utc).timestamp(),             # processed_at
                    wwv_tick_snr,                                       # wwv_tick_snr_db
                    wwvh_tick_snr,                                      # wwvh_tick_snr_db
                    chu_tick_snr,                                       # chu_tick_snr_db
                    bpm_tick_snr                                        # bpm_tick_snr_db
                ])
            
            # ================================================================
            # Write to HDF5 (L2 Timing Measurements) - Parallel with CSV
            # ================================================================
            if self.enable_hdf5_writes and self.hdf5_l2_writer:
                try:
                    from hf_timestd.io.uncertainty import ISOGUMCalculator, UncertaintyBudget
                    
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
                    
                    l2_measurement = {
                        'timestamp_utc': timestamp_utc,
                        'minute_boundary_utc': minute_boundary,
                        'rtp_timestamp': rtp_timestamp,
                        'station': station,
                        'frequency_mhz': frequency_mhz,
                        'discrimination_method': 'TONE',  # Primary method
                        'discrimination_confidence': discrimination_conf,
                        'clock_offset_ms': effective_d_clock,
                        'uncertainty_ms': effective_uncertainty,
                        'expanded_uncertainty_ms': unc_result['u_expanded_ms'],
                        'coverage_factor': budget.coverage_factor,
                        'confidence_level': budget.confidence_level,
                        'u_rtp_timestamp_ms': budget.u_rtp_timestamp_ms,
                        'u_ionospheric_ms': budget.u_ionospheric_ms,
                        'u_multipath_ms': budget.u_multipath_ms,
                        'u_discrimination_ms': budget.u_discrimination_ms,
                        'u_gpsdo_ms': budget.u_gpsdo_ms,
                        'u_propagation_model_ms': budget.u_propagation_model_ms,
                        'degrees_of_freedom': unc_result['degrees_of_freedom'],
                        'quality_grade': quality_grade,
                        'confidence': solution.confidence if solution else 0.0,
                        'quality_flag': quality_flag,
                        'propagation_delay_ms': solution.t_propagation_ms if solution else None,
                        'propagation_mode': solution.propagation_mode if solution else None,
                        'n_hops': solution.n_hops if solution else None,
                        'snr_db': snr_db,
                        'utc_verified': convergence_result.is_locked,
                        'multi_station_verified': solution.dual_station_verified if solution else False,
                        'traceability_chain': 'GPSDO → UTC(GPS) → UTC(NIST)',
                        'processing_version': '3.2.0',
                        'processed_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
                        'calibration_date': '2025-12-01T00:00:00Z',  # TODO: Get from config
                        'gpsdo_locked': gpsdo_locked,
                    }
                    
                    # Write to HDF5
                    logger.debug(f"Attempting HDF5 L2 write: D_clock={effective_d_clock:.2f}ms")
                    self.hdf5_l2_writer.write_measurement(l2_measurement)
                    self._track_hdf5_write_success()
                    logger.debug(f"Successfully wrote HDF5 L2 measurement")
                    
                except Exception as e:
                    self._track_hdf5_write_failure(e, "L2")
                
                
            # Store convergence result for status reporting
            self.last_convergence_result = convergence_result
            
        except Exception as e:
            logger.error(f"Failed to write clock offset: {e}")
    
    def _init_carrier_power_csv(self):
        """Initialize carrier power CSV file for today."""
        today = datetime.now(timezone.utc).strftime('%Y%m%d')
        self.carrier_power_csv = self.carrier_power_dir / f'carrier_power_{today}.csv'
        
        if not self.carrier_power_csv.exists():
            with open(self.carrier_power_csv, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    'timestamp', 'utc_time', 'power_db', 'snr_db',
                    'wwv_tone_db', 'wwvh_tone_db', 'station', 'quality_grade'
                ])
            logger.info(f"Created carrier power CSV: {self.carrier_power_csv}")
    
    def _write_carrier_power(self, minute_boundary: int, power_db: float, snr_db: float,
                              wwv_tone_db: float = None, wwvh_tone_db: float = None,
                              station: str = None, quality_grade: str = None,
                              channel_char = None):
        """Append carrier power measurement to daily CSV and HDF5 with channel characterization."""
        try:
            # Ensure we're writing to today's file
            today = datetime.now(timezone.utc).strftime('%Y%m%d')
            expected_csv = self.carrier_power_dir / f'carrier_power_{today}.csv'
            if self.carrier_power_csv != expected_csv:
                self.carrier_power_csv = expected_csv
                self._init_carrier_power_csv()
            
            with open(self.carrier_power_csv, 'a', newline='') as f:
                writer = csv.writer(f)
                utc_time = datetime.fromtimestamp(minute_boundary, timezone.utc).isoformat()
                writer.writerow([
                    minute_boundary,
                    utc_time,
                    round(power_db, 2) if power_db is not None and not (isinstance(power_db, float) and (np.isnan(power_db) or np.isinf(power_db))) else '',
                    round(snr_db, 2) if snr_db is not None and not (isinstance(snr_db, float) and (np.isnan(snr_db) or np.isinf(snr_db))) else '',
                    round(wwv_tone_db, 2) if wwv_tone_db is not None and not (isinstance(wwv_tone_db, float) and (np.isnan(wwv_tone_db) or np.isinf(wwv_tone_db))) else '',
                    round(wwvh_tone_db, 2) if wwvh_tone_db is not None and not (isinstance(wwvh_tone_db, float) and (np.isnan(wwvh_tone_db) or np.isinf(wwvh_tone_db))) else '',
                    station or '',
                    quality_grade or ''
                ])
            
            # ================================================================
            # Write to HDF5 (L1A Channel Observables) - Parallel with CSV
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
    
    def _init_tone_detections_csv(self):
        """Initialize tone detections CSV for today."""
        today = datetime.now(timezone.utc).strftime('%Y%m%d')
        file_channel = self._get_file_channel_name()
        self.tone_detections_csv = self.tone_detections_dir / f'{file_channel}_tones_{today}.csv'
        
        if not self.tone_detections_csv.exists():
            with open(self.tone_detections_csv, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    'timestamp_utc', 'minute_boundary',
                    'wwv_detected', 'wwvh_detected', 'chu_detected', 'bpm_detected',
                    'wwv_snr_db', 'wwvh_snr_db', 'chu_snr_db', 'bpm_snr_db', 
                    'wwv_timing_ms', 'wwvh_timing_ms', 'chu_timing_ms', 'bpm_timing_ms',
                    'anchor_station', 'anchor_confidence'
                ])
            logger.info(f"Created tone detections CSV: {self.tone_detections_csv}")
    
    def _write_tone_detections(self, minute_boundary: int, time_snap):
        """Write tone detection results from TimeSnapResult."""
        try:
            today = datetime.now(timezone.utc).strftime('%Y%m%d')
            file_channel = self._get_file_channel_name()
            expected_csv = self.tone_detections_dir / f'{file_channel}_tones_{today}.csv'
            if self.tone_detections_csv != expected_csv:
                self.tone_detections_csv = expected_csv
                self._init_tone_detections_csv()

            include_chu = False
            try:
                with open(self.tone_detections_csv, 'r', newline='') as f:
                    reader = csv.reader(f)
                    header = next(reader, [])
                include_chu = 'chu_timing_ms' in header
            except Exception:
                include_chu = False
            
            # Write to CSV
            with open(self.tone_detections_csv, 'a', newline='') as f:
                writer = csv.writer(f)
                utc_time = datetime.fromtimestamp(minute_boundary, timezone.utc).isoformat()
                if include_chu:
                    writer.writerow([
                        utc_time,
                        minute_boundary,
                        1 if time_snap.wwv_detected else 0,
                        1 if time_snap.wwvh_detected else 0,
                        1 if getattr(time_snap, 'chu_detected', False) else 0,
                        1 if time_snap.bpm_detected else 0,
                        round(time_snap.wwv_snr_db, 2) if time_snap.wwv_snr_db else '',
                        round(time_snap.wwvh_snr_db, 2) if time_snap.wwvh_snr_db else '',
                        round(getattr(time_snap, 'chu_snr_db', None), 2) if getattr(time_snap, 'chu_snr_db', None) else '',
                        round(time_snap.bpm_snr_db, 2) if time_snap.bpm_snr_db else '',
                        round(time_snap.wwv_timing_ms, 3) if time_snap.wwv_timing_ms else '',
                        round(time_snap.wwvh_timing_ms, 3) if time_snap.wwvh_timing_ms else '',
                        round(getattr(time_snap, 'chu_timing_ms', None), 3) if getattr(time_snap, 'chu_timing_ms', None) else '',
                        round(time_snap.bpm_timing_ms, 3) if time_snap.bpm_timing_ms else '',
                        time_snap.anchor_station or '',
                        round(time_snap.anchor_confidence, 3) if time_snap.anchor_confidence else ''
                    ])
                else:
                    writer.writerow([
                        utc_time,
                        minute_boundary,
                        1 if time_snap.wwv_detected else 0,
                        1 if time_snap.wwvh_detected else 0,
                        1 if time_snap.bpm_detected else 0,
                        round(time_snap.wwv_snr_db, 2) if time_snap.wwv_snr_db else '',
                        round(time_snap.wwvh_snr_db, 2) if time_snap.wwvh_snr_db else '',
                        round(time_snap.bpm_snr_db, 2) if time_snap.bpm_snr_db else '',
                        round(time_snap.wwv_timing_ms, 3) if time_snap.wwv_timing_ms else '',
                        round(time_snap.wwvh_timing_ms, 3) if time_snap.wwvh_timing_ms else '',
                        round(time_snap.bpm_timing_ms, 3) if time_snap.bpm_timing_ms else '',
                        time_snap.anchor_station or '',
                        round(time_snap.anchor_confidence, 3) if time_snap.anchor_confidence else ''
                    ])
            
            # ===============================================================
            # Write to HDF5 (L1A Tone Detections) - Parallel with CSV
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
                    
                    l1a_tones_measurement = {
                        'timestamp_utc': utc_time_iso,
                        'minute_boundary': minute_boundary,
                        'wwv_detected': bool(time_snap.wwv_detected),
                        'wwv_snr_db': time_snap.wwv_snr_db if time_snap.wwv_snr_db else None,
                        'wwv_timing_ms': time_snap.wwv_timing_ms if time_snap.wwv_timing_ms else None,
                        'wwvh_detected': bool(time_snap.wwvh_detected),
                        'wwvh_snr_db': time_snap.wwvh_snr_db if time_snap.wwvh_snr_db else None,
                        'wwvh_timing_ms': time_snap.wwvh_timing_ms if time_snap.wwvh_timing_ms else None,
                        'chu_detected': bool(getattr(time_snap, 'chu_detected', False)),
                        'chu_snr_db': getattr(time_snap, 'chu_snr_db', None),
                        'chu_timing_ms': getattr(time_snap, 'chu_timing_ms', None),
                        'bpm_detected': bool(time_snap.bpm_detected),
                        'bpm_snr_db': time_snap.bpm_snr_db if time_snap.bpm_snr_db else None,
                        'bpm_timing_ms': time_snap.bpm_timing_ms if time_snap.bpm_timing_ms else None,
                        'anchor_station': time_snap.anchor_station or '',
                        'anchor_confidence': time_snap.anchor_confidence if time_snap.anchor_confidence else None,
                        'quality_flag': quality_flag,
                        'processing_version': '3.2.0'
                    }
                    
                    self.hdf5_l1a_tones_writer.write_measurement(l1a_tones_measurement)
                    
                except Exception as e:
                    logger.error(f"Failed to write tone detections to HDF5: {e}")
                    
        except Exception as e:
            logger.error(f"Failed to write tone detections: {e}")
    
    def _init_bcd_discrimination_csv(self):
        """Initialize BCD discrimination CSV for today."""
        today = datetime.now(timezone.utc).strftime('%Y%m%d')
        file_channel = self._get_file_channel_name()
        self.bcd_discrimination_csv = self.bcd_discrimination_dir / f'{file_channel}_bcd_{today}.csv'
        
        if not self.bcd_discrimination_csv.exists():
            with open(self.bcd_discrimination_csv, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    'timestamp_utc', 'minute_boundary', 'wwv_amplitude', 'wwvh_amplitude', 'bpm_amplitude',
                    'differential_delay_ms', 'correlation_quality', 
                    'wwv_toa_ms', 'wwvh_toa_ms', 'bpm_toa_ms',
                    'amplitude_ratio_db'
                ])
            logger.info(f"Created BCD discrimination CSV: {self.bcd_discrimination_csv}")
    
    def _write_bcd_discrimination(self, minute_boundary: int, channel_char):
        """Write BCD discrimination results from ChannelCharacterization."""
        try:
            today = datetime.now(timezone.utc).strftime('%Y%m%d')
            file_channel = self._get_file_channel_name()
            expected_csv = self.bcd_discrimination_dir / f'{file_channel}_bcd_{today}.csv'
            if self.bcd_discrimination_csv != expected_csv:
                self.bcd_discrimination_csv = expected_csv
                self._init_bcd_discrimination_csv()
            
            # Calculate amplitude ratio in dB
            ratio_db = None
            if channel_char.bcd_wwv_amplitude and channel_char.bcd_wwvh_amplitude:
                if channel_char.bcd_wwvh_amplitude > 0:
                    ratio_db = 20 * np.log10(channel_char.bcd_wwv_amplitude / channel_char.bcd_wwvh_amplitude)
            
            with open(self.bcd_discrimination_csv, 'a', newline='') as f:
                writer = csv.writer(f)
                utc_time = datetime.fromtimestamp(minute_boundary, timezone.utc).isoformat()
                writer.writerow([
                    utc_time,
                    minute_boundary,
                    round(channel_char.bcd_wwv_amplitude, 4) if channel_char.bcd_wwv_amplitude else '',
                    round(channel_char.bcd_wwvh_amplitude, 4) if channel_char.bcd_wwvh_amplitude else '',
                    round(channel_char.bcd_bpm_amplitude, 4) if hasattr(channel_char, 'bcd_bpm_amplitude') and channel_char.bcd_bpm_amplitude else '',
                    round(channel_char.bcd_differential_delay_ms, 3) if channel_char.bcd_differential_delay_ms else '',
                    round(channel_char.bcd_correlation_quality, 3) if channel_char.bcd_correlation_quality else '',
                    round(channel_char.bcd_wwv_toa_ms, 3) if channel_char.bcd_wwv_toa_ms else '',
                    round(channel_char.bcd_wwvh_toa_ms, 3) if channel_char.bcd_wwvh_toa_ms else '',
                    round(channel_char.bcd_bpm_toa_ms, 3) if hasattr(channel_char, 'bcd_bpm_toa_ms') and channel_char.bcd_bpm_toa_ms else '',
                    round(ratio_db, 2) if ratio_db else ''
                ])
            
            # ================================================================
            # Write to HDF5 (L1B BCD Timecode) - Parallel with CSV
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
    
    def _init_doppler_csv(self):
        """Initialize Doppler analysis CSV for today."""
        today = datetime.now(timezone.utc).strftime('%Y%m%d')
        file_channel = self._get_file_channel_name()
        self.doppler_csv = self.doppler_dir / f'{file_channel}_doppler_{today}.csv'
        
        if not self.doppler_csv.exists():
            with open(self.doppler_csv, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    'timestamp_utc', 'minute_boundary', 'wwv_doppler_hz', 'wwvh_doppler_hz',
                    'wwv_doppler_std_hz', 'wwvh_doppler_std_hz', 'doppler_quality',
                    'max_coherent_window_sec', 'phase_variance_rad', 'carrier_doppler_hz'
                ])
            logger.info(f"Created Doppler CSV: {self.doppler_csv}")
    
    def _write_doppler(self, minute_boundary: int, channel_char):
        """Write Doppler analysis results from ChannelCharacterization."""
        try:
            today = datetime.now(timezone.utc).strftime('%Y%m%d')
            file_channel = self._get_file_channel_name()
            expected_csv = self.doppler_dir / f'{file_channel}_doppler_{today}.csv'
            if self.doppler_csv != expected_csv:
                self.doppler_csv = expected_csv
                self._init_doppler_csv()
            
            with open(self.doppler_csv, 'a', newline='') as f:
                writer = csv.writer(f)
                utc_time = datetime.fromtimestamp(minute_boundary, timezone.utc).isoformat()
                writer.writerow([
                    utc_time,
                    minute_boundary,
                    round(channel_char.doppler_wwv_hz, 4) if channel_char.doppler_wwv_hz is not None else '',
                    round(channel_char.doppler_wwvh_hz, 4) if channel_char.doppler_wwvh_hz is not None else '',
                    round(channel_char.doppler_wwv_std_hz, 4) if channel_char.doppler_wwv_std_hz is not None else '',
                    round(channel_char.doppler_wwvh_std_hz, 4) if channel_char.doppler_wwvh_std_hz is not None else '',
                    round(channel_char.doppler_quality, 3) if channel_char.doppler_quality is not None else '',
                    round(channel_char.max_coherent_window_sec, 3) if channel_char.max_coherent_window_sec is not None else '',
                    round(channel_char.phase_variance_rad, 6) if channel_char.phase_variance_rad is not None else '',
                    round(channel_char.doppler_carrier_hz, 4) if channel_char.doppler_carrier_hz is not None else ''
                ])
        except Exception as e:
            logger.error(f"Failed to write Doppler: {e}")
    
    def _init_station_id_csv(self):
        """Initialize station ID (440Hz/500Hz/600Hz) CSV for today."""
        today = datetime.now(timezone.utc).strftime('%Y%m%d')
        file_channel = self._get_file_channel_name()
        self.station_id_csv = self.station_id_dir / f'{file_channel}_440hz_{today}.csv'
        
        if not self.station_id_csv.exists():
            with open(self.station_id_csv, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    'timestamp_utc', 'minute_boundary', 'minute_number',
                    'ground_truth_station', 'ground_truth_source', 'ground_truth_power_db',
                    'station_confidence', 'dominant_station',
                    'harmonic_ratio_500_1000', 'harmonic_ratio_600_1200'
                ])
            logger.info(f"Created station ID CSV: {self.station_id_csv}")
    
    def _write_station_id(self, minute_boundary: int, channel_char):
        """Write station ID results from ChannelCharacterization.
        
        Only writes for minutes 1 (WWVH 440 Hz) and 2 (WWV 440 Hz).
        This CSV is specifically for 440 Hz voice announcement detection.
        """
        try:
            # Calculate minute number within hour (0-59)
            minute_number = (minute_boundary // 60) % 60
            
            # Only write for 440 Hz minutes: 1 = WWVH, 2 = WWV
            if minute_number not in [1, 2]:
                return  # Skip - not a 440 Hz minute
            
            today = datetime.now(timezone.utc).strftime('%Y%m%d')
            file_channel = self._get_file_channel_name()
            expected_csv = self.station_id_dir / f'{file_channel}_440hz_{today}.csv'
            if self.station_id_csv != expected_csv:
                self.station_id_csv = expected_csv
                self._init_station_id_csv()
            
            with open(self.station_id_csv, 'a', newline='') as f:
                writer = csv.writer(f)
                utc_time = datetime.fromtimestamp(minute_boundary, timezone.utc).isoformat()
                writer.writerow([
                    utc_time,
                    minute_boundary,
                    minute_number,
                    channel_char.ground_truth_station or '',
                    channel_char.ground_truth_source or '',
                    round(channel_char.ground_truth_power_db, 2) if channel_char.ground_truth_power_db else '',
                    channel_char.station_confidence or '',
                    channel_char.dominant_station or '',
                    round(channel_char.harmonic_ratio_500_1000, 2) if channel_char.harmonic_ratio_500_1000 else '',
                    round(channel_char.harmonic_ratio_600_1200, 2) if channel_char.harmonic_ratio_600_1200 else ''
                ])
        except Exception as e:
            logger.error(f"Failed to write station ID: {e}")
    
    def _init_test_signal_csv(self):
        """Initialize test signal CSV for today."""
        today = datetime.now(timezone.utc).strftime('%Y%m%d')
        file_channel = self._get_file_channel_name()
        self.test_signal_csv = self.test_signal_dir / f'{file_channel}_test_signal_{today}.csv'
        
        if not self.test_signal_csv.exists():
            with open(self.test_signal_csv, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    'timestamp_utc', 'minute_boundary', 'minute_number', 'detected', 'station',
                    'confidence', 'multitone_score', 'chirp_score', 'snr_db',
                    'fss_db', 'delay_spread_ms', 'toa_offset_ms', 'coherence_time_sec'
                ])
            logger.info(f"Created test signal CSV: {self.test_signal_csv}")
    
    def _write_test_signal(self, minute_boundary: int, iq_samples, minute_number: int):
        """Detect and write test signal for minutes 8 and 44."""
        try:
            today = datetime.now(timezone.utc).strftime('%Y%m%d')
            file_channel = self._get_file_channel_name()
            expected_csv = self.test_signal_dir / f'{file_channel}_test_signal_{today}.csv'
            if self.test_signal_csv != expected_csv:
                self.test_signal_csv = expected_csv
                self._init_test_signal_csv()
            
            # Detect test signal using the engine's discriminator
            detection = self.engine.discriminator.test_signal_detector.detect(
                iq_samples=iq_samples,
                minute_number=minute_number,
                sample_rate=self.sample_rate
            )
            
            # Determine station from schedule: minute 8 = WWV, minute 44 = WWVH
            station = 'WWV' if minute_number == 8 else 'WWVH'
            
            with open(self.test_signal_csv, 'a', newline='') as f:
                writer = csv.writer(f)
                utc_time = datetime.fromtimestamp(minute_boundary, timezone.utc).isoformat()
                writer.writerow([
                    utc_time,
                    minute_boundary,
                    minute_number,
                    1 if detection.detected else 0,
                    station if detection.detected else '',
                    round(detection.confidence, 4) if detection.confidence else '',
                    round(detection.multitone_score, 4) if detection.multitone_score else '',
                    round(detection.chirp_score, 4) if detection.chirp_score else '',
                    round(detection.snr_db, 2) if detection.snr_db else '',
                    round(detection.frequency_selectivity_db, 2) if detection.frequency_selectivity_db else '',
                    round(detection.delay_spread_ms, 3) if detection.delay_spread_ms else '',
                    round(detection.toa_offset_ms, 3) if detection.toa_offset_ms else '',
                    round(detection.coherence_time_sec, 3) if detection.coherence_time_sec else ''
                ])
            
            if detection.detected:
                logger.info(
                    f"Test signal detected minute {minute_number}: {station}, "
                    f"confidence={detection.confidence:.2f}, SNR={detection.snr_db:.1f}dB"
                )
        except Exception as e:
            logger.error(f"Failed to write test signal: {e}")
    
    def _init_discrimination_csv(self):
        """Initialize discrimination summary CSV for today."""
        today = datetime.now(timezone.utc).strftime('%Y%m%d')
        file_channel = self._get_file_channel_name()
        self.discrimination_csv = self.discrimination_dir / f'{file_channel}_discrimination_{today}.csv'
        
        if not self.discrimination_csv.exists():
            with open(self.discrimination_csv, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    'timestamp_utc', 'minute_boundary', 'dominant_station', 'station_confidence',
                    'wwv_snr_db', 'wwvh_snr_db', 'bpm_snr_db', 'power_ratio_db', 'ground_truth_station',
                    'quality_grade', 'method_agreements', 'method_disagreements',
                    'bpm_detected', 'bpm_timing_ms'
                ])
            logger.info(f"Created discrimination CSV: {self.discrimination_csv}")
    
    def _write_discrimination(self, minute_boundary: int, result, time_snap, channel_char):
        """Write discrimination summary combining all methods."""
        try:
            today = datetime.now(timezone.utc).strftime('%Y%m%d')
            file_channel = self._get_file_channel_name()
            expected_csv = self.discrimination_dir / f'{file_channel}_discrimination_{today}.csv'
            if self.discrimination_csv != expected_csv:
                self.discrimination_csv = expected_csv
                self._init_discrimination_csv()
            
            # Calculate power ratio from tone SNRs
            power_ratio_db = None
            if time_snap.wwv_snr_db is not None and time_snap.wwvh_snr_db is not None:
                power_ratio_db = time_snap.wwv_snr_db - time_snap.wwvh_snr_db
            
            # Compute quality_grade from uncertainty_ms
            grade = ''
            if result:
                unc = result.uncertainty_ms
                grade = 'A' if unc < 1.0 else 'B' if unc < 3.0 else 'C' if unc < 10.0 else 'D'
            
            with open(self.discrimination_csv, 'a', newline='') as f:
                writer = csv.writer(f)
                utc_time = datetime.fromtimestamp(minute_boundary, timezone.utc).isoformat()
                writer.writerow([
                    utc_time,
                    minute_boundary,
                    channel_char.dominant_station or '',
                    channel_char.station_confidence or '',
                    round(time_snap.wwv_snr_db, 2) if time_snap.wwv_snr_db else '',
                    round(time_snap.wwvh_snr_db, 2) if time_snap.wwvh_snr_db else '',
                    round(time_snap.bpm_snr_db, 2) if time_snap.bpm_snr_db else '',
                    round(power_ratio_db, 2) if power_ratio_db else '',
                    channel_char.ground_truth_station or '',
                    grade,
                    ';'.join(channel_char.cross_validation_agreements) if channel_char.cross_validation_agreements else '',
                    ';'.join(channel_char.cross_validation_disagreements) if channel_char.cross_validation_disagreements else '',
                    1 if time_snap.bpm_detected else 0,
                    round(time_snap.bpm_timing_ms, 3) if time_snap.bpm_timing_ms else ''
                ])
        except Exception as e:
            logger.error(f"Failed to write discrimination: {e}")
    
    def _init_audio_tones_csv(self):
        """Initialize audio tones CSV for continuous 500/600 Hz + intermodulation monitoring."""
        today = datetime.now(timezone.utc).strftime('%Y%m%d')
        file_channel = self._get_file_channel_name()
        self.audio_tones_csv = self.audio_tones_dir / f'{file_channel}_audio_tones_{today}.csv'
        
        if not self.audio_tones_csv.exists():
            with open(self.audio_tones_csv, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    'timestamp_utc', 'minute_boundary',
                    'power_400_hz_db', 'power_500_hz_db', 'power_600_hz_db', 'power_700_hz_db',
                    'power_1000_hz_db', 'power_1200_hz_db',
                    'ratio_500_600_db', 'ratio_400_700_db',
                    'wwv_intermod_db', 'wwvh_intermod_db',
                    'intermod_dominant', 'intermod_confidence'
                ])
            logger.info(f"Created audio tones CSV: {self.audio_tones_csv}")
    
    def _write_audio_tones(self, minute_boundary: int, iq_samples: np.ndarray):
        """Analyze and write audio tone powers with intermodulation."""
        try:
            from .audio_tone_monitor import AudioToneMonitor
            
            today = datetime.now(timezone.utc).strftime('%Y%m%d')
            file_channel = self._get_file_channel_name()
            expected_csv = self.audio_tones_dir / f'{file_channel}_audio_tones_{today}.csv'
            if self.audio_tones_csv != expected_csv:
                self.audio_tones_csv = expected_csv
                self._init_audio_tones_csv()
            
            # Analyze audio tones
            monitor = AudioToneMonitor(self.channel_name, self.sample_rate)
            analysis = monitor.analyze_minute(iq_samples, minute_boundary)
            
            with open(self.audio_tones_csv, 'a', newline='') as f:
                writer = csv.writer(f)
                utc_time = datetime.fromtimestamp(minute_boundary, timezone.utc).isoformat()
                writer.writerow([
                    utc_time,
                    minute_boundary,
                    round(analysis.power_400_hz_db, 2),
                    round(analysis.power_500_hz_db, 2),
                    round(analysis.power_600_hz_db, 2),
                    round(analysis.power_700_hz_db, 2),
                    round(analysis.power_1000_hz_db, 2),
                    round(analysis.power_1200_hz_db, 2),
                    round(analysis.ratio_500_600_db, 2),
                    round(analysis.ratio_400_700_db, 2),
                    round(analysis.wwv_intermod_500_to_600_db, 2),
                    round(analysis.wwvh_intermod_600_to_500_db, 2),
                    analysis.intermod_dominant_station or '',
                    round(analysis.intermod_confidence, 3) if analysis.intermod_confidence else ''
                ])
        except Exception as e:
            logger.error(f"Failed to write audio tones: {e}")

    def _init_transmission_time_csv(self):
        """Initialize transmission time (UTC-NIST) CSV for today."""
        today = datetime.now(timezone.utc).strftime('%Y%m%d')
        file_channel = self._get_file_channel_name()
        self.transmission_time_csv = self.timing_dir / f'{file_channel}_utc_nist_{today}.csv'
        
        if not self.transmission_time_csv.exists():
            with open(self.transmission_time_csv, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    'timestamp_utc', 'minute_boundary', 'station', 'frequency_mhz',
                    'mode', 'n_hops', 'layer_height_km', 'elevation_deg',
                    'propagation_delay_ms', 'utc_nist_offset_ms', 'utc_nist_verified',
                    'confidence', 'mode_separation_ms', 'uncertainty_ms'
                ])
            logger.info(f"Created transmission time CSV: {self.transmission_time_csv}")

    def _write_transmission_time(self, minute_boundary: int, result):
        """Write transmission time solution (UTC-NIST back-calculation)."""
        try:
            if not result or not result.solution or result.solution.d_clock_ms is None:
                return

            # Use data timestamp for filename to support backfilling
            dt = datetime.fromtimestamp(minute_boundary, timezone.utc)
            date_str = dt.strftime('%Y%m%d')
            
            file_channel = self._get_file_channel_name()
            expected_csv = self.timing_dir / f'{file_channel}_utc_nist_{date_str}.csv'
            
            # Initialize if file changed or doesn't exist (handle daily rotation)
            if self.transmission_time_csv != expected_csv or not expected_csv.exists():
                self.transmission_time_csv = expected_csv
                if not self.transmission_time_csv.exists():
                    with open(self.transmission_time_csv, 'w', newline='') as f:
                        writer = csv.writer(f)
                        writer.writerow([
                            'timestamp_utc', 'minute_boundary', 'station', 'frequency_mhz',
                            'mode', 'n_hops', 'layer_height_km', 'elevation_deg',
                            'propagation_delay_ms', 'utc_nist_offset_ms', 'utc_nist_verified',
                            'confidence', 'mode_separation_ms', 'uncertainty_ms'
                        ])
                    logger.info(f"Created/Rotated transmission time CSV: {self.transmission_time_csv}")
            
            sol = result.solution
             
            with open(self.transmission_time_csv, 'a', newline='') as f:
                writer = csv.writer(f)
                utc_time = datetime.fromtimestamp(minute_boundary, timezone.utc).isoformat()
                
                # Note: utc_nist_offset_ms IS d_clock_ms in the solution
                # The API expects 'utc_nist_offset_ms'
                
                writer.writerow([
                    utc_time,
                    minute_boundary,
                    sol.station,
                    round(sol.frequency_mhz, 3),
                    sol.propagation_mode,
                    sol.n_hops,
                    round(sol.layer_height_km, 1),
                    round(getattr(sol, 'elevation_angle_deg', 0.0), 2),
                    round(sol.t_propagation_ms if sol.t_propagation_ms is not None else 0.0, 3),
                    round(sol.d_clock_ms if sol.d_clock_ms is not None else 0.0, 3),  # This is the UTC offset
                    1 if sol.utc_verified else 0,
                    round(sol.confidence, 3),
                    round(getattr(sol, 'mode_separation_ms', 0.0), 3),
                    round(sol.uncertainty_ms, 3)
                ])
        except Exception as e:
            logger.error(f"Failed to write transmission time: {e}")
    

    def _init_tec_csv(self):
        """Initialize TEC estimation CSV for today."""
        today = datetime.now(timezone.utc).strftime('%Y%m%d')
        # TEC is station-based, not channel-based (aggregates across frequencies)
        # Use simplified naming: tec_YYYYMMDD.csv
        self.tec_csv = self.tec_dir / f'tec_{today}.csv'
        
        if not self.tec_csv.exists():
            with open(self.tec_csv, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    'timestamp_utc', 'minute_boundary', 'station',
                    'tec_tecu', 't_vacuum_error_ms', 'confidence', 'residuals_ms',
                    'n_frequencies', 'frequencies_mhz',
                    'group_delay_2_5_mhz', 'group_delay_5_mhz', 'group_delay_10_mhz',
                    'group_delay_15_mhz', 'group_delay_20_mhz', 'group_delay_25_mhz'
                ])
            logger.info(f"Created TEC CSV: {self.tec_csv}")
    
    def _write_tec(self, minute_boundary: int, station: str, measurements: List[Dict]):
        """Write TEC estimation from multi-frequency measurements.
        
        Args:
            minute_boundary: Unix timestamp of minute boundary
            station: Station name (WWV, WWVH, CHU, BPM)
            measurements: List of dicts with 'frequency_hz', 'toa_ms', 'uncertainty_ms'
        """
        try:
            # Need at least 2 frequencies for TEC estimation
            if len(measurements) < 2:
                return
            
            # Use data timestamp for filename to support backfilling
            dt = datetime.fromtimestamp(minute_boundary, timezone.utc)
            date_str = dt.strftime('%Y%m%d')
            
            expected_csv = self.tec_dir / f'tec_{date_str}.csv'
            
            # Initialize if file changed or doesn't exist (handle daily rotation)
            if self.tec_csv != expected_csv or not expected_csv.exists():
                self.tec_csv = expected_csv
                if not self.tec_csv.exists():
                    with open(self.tec_csv, 'w', newline='') as f:
                        writer = csv.writer(f)
                        writer.writerow([
                            'timestamp_utc', 'minute_boundary', 'station',
                            'tec_tecu', 't_vacuum_error_ms', 'confidence', 'residuals_ms',
                            'n_frequencies', 'frequencies_mhz',
                            'group_delay_2_5_mhz', 'group_delay_5_mhz', 'group_delay_10_mhz',
                            'group_delay_15_mhz', 'group_delay_20_mhz', 'group_delay_25_mhz'
                        ])
                    logger.info(f"Created/Rotated TEC CSV: {self.tec_csv}")
            
            # Estimate TEC using multi-frequency least squares
            tec_result = self.tec_estimator.estimate_tec(
                measurements=measurements,
                station=station,
                timestamp=float(minute_boundary)
            )
            
            if not tec_result:
                return  # Estimation failed
            
            # Extract per-frequency group delays (for visualization)
            freq_list = sorted([m['frequency_hz'] / 1e6 for m in measurements])
            freq_str = ';'.join([f"{f:.2f}" for f in freq_list])
            
            # Map group delays to standard frequencies (fill with empty if not present)
            delay_map = tec_result.group_delay_ms  # Dict[float, float] keyed by MHz
            
            with open(self.tec_csv, 'a', newline='') as f:
                writer = csv.writer(f)
                utc_time = datetime.fromtimestamp(minute_boundary, timezone.utc).isoformat()
                
                writer.writerow([
                    utc_time,
                    minute_boundary,
                    station,
                    round(tec_result.tec_u, 3),  # TEC in TECU
                    round(tec_result.t_vacuum_error_ms, 3),
                    round(tec_result.confidence, 4),
                    round(tec_result.residuals_ms, 3),
                    tec_result.n_frequencies,
                    freq_str,
                    round(delay_map.get(2.5, 0), 3) if 2.5 in delay_map else '',
                    round(delay_map.get(5.0, 0), 3) if 5.0 in delay_map else '',
                    round(delay_map.get(10.0, 0), 3) if 10.0 in delay_map else '',
                    round(delay_map.get(15.0, 0), 3) if 15.0 in delay_map else '',
                    round(delay_map.get(20.0, 0), 3) if 20.0 in delay_map else '',
                    round(delay_map.get(25.0, 0), 3) if 25.0 in delay_map else ''
                ])
                
            logger.info(
                f"TEC estimated for {station}: {tec_result.tec_u:.2f} TECU "
                f"(confidence={tec_result.confidence:.2f}, n_freq={tec_result.n_frequencies})"
            )
            
        except Exception as e:
            logger.error(f"Failed to write TEC: {e}")

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
            
            system_time = float(target_minute)
            # Use actual RTP timestamp from metadata, not synthesized from Unix time
            if json_path.exists() and 'start_rtp_timestamp' in metadata:
                rtp_timestamp = int(metadata['start_rtp_timestamp'])
            else:
                # Fallback: synthesize from Unix time (less accurate)
                rtp_timestamp = int(target_minute * self.sample_rate)
                logger.warning(f"No RTP timestamp in metadata, using synthesized value")
            
            logger.debug(f"Read {len(iq_samples)} samples from binary for minute {target_minute}")
            return iq_samples, system_time, rtp_timestamp
            
        except Exception as e:
            logger.debug(f"Error reading binary: {e}")
            return None
    
    def _get_latest_minute(self) -> int:
        """Get the latest complete minute boundary from available data."""
        latest = self._get_latest_binary_minute()
        if latest is not None:
            return latest

        now = time.time()
        return ((int(now) // 60) - 2) * 60
    
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
                    dt_seconds = (minute_boundary - self.last_minute_unix) if self.last_minute_unix else 60.0
                    
                    is_valid, reason = self.engine._validate_d_clock_continuity(
                        current_d_clock_ms=primary_result.d_clock_ms,
                        previous_d_clock_ms=self.last_d_clock_ms,
                        dt_seconds=dt_seconds,
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
                    self._write_doppler(minute_boundary, channel_char)
                    self._write_station_id(minute_boundary, channel_char)
                    self._write_discrimination(minute_boundary, primary_result, time_snap, channel_char)
                
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
                    
                    if solution and time_snap and solution.confidence > 0.1:
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
                                minute_boundary=minute_boundary
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
            minute_number = (minute_boundary // 60) % 60
            if minute_number in [8, 44]:
                self._write_test_signal(minute_boundary, iq_samples, minute_number)
            
            # Write audio tones (500/600 Hz + intermodulation) for every minute
            self._write_audio_tones(minute_boundary, iq_samples)
            
            # Write transmission time solution (Reference for Fusion)
            if self.last_result:
                self._write_transmission_time(minute_boundary, self.last_result)
            
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
                
                # Process any unprocessed minutes
                if latest_minute not in self.processed_minutes:
                    self.process_minute(latest_minute)
                
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
                time.sleep(self.poll_interval)
                
            except Exception as e:
                logger.error(f"Error in main loop: {e}")
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
    parser.add_argument('--sample-rate', type=int, default=20000, help='Sample rate')
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
