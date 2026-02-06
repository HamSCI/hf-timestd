#!/usr/bin/env python3
"""
Metrology Service (Phase 1)
===========================
Real-time DSP and Timestamping Service.

Responsibility:
1. Ingest raw IQ data streams (from tiered storage).
2. Run MetrologyEngine (Tone Detection, Channel Characterization).
3. Write L1_Metrology data products (HDF5).
4. Do NOT perform physics modeling or clock offset calculation.
"""

import logging
import time
import json
import signal
import sys
import threading
import queue
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Dict, List, Any, Tuple
import numpy as np

# inotify for file watching
try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler, FileCreatedEvent
    WATCHDOG_AVAILABLE = True
except ImportError:
    WATCHDOG_AVAILABLE = False

# Imports
from hf_timestd.core.metrology_engine import MetrologyEngine
from hf_timestd.models import L1MetrologyMeasurement
from hf_timestd.io.hdf5_writer import DataProductWriter
from hf_timestd.data_product_registry import DataProductRegistry
from hf_timestd.interfaces.data_models import TimingConfig, TimingAuthority
# Needed for binary reading
try:
    from hf_timestd.io.tiered_storage import TieredStorageManager
except ImportError:
    TieredStorageManager = None

logger = logging.getLogger(__name__)


class MinuteFileHandler(FileSystemEventHandler):
    """Watchdog handler that detects new minute files (.json sidecars)."""
    
    def __init__(self, channel_name: str, file_queue: queue.Queue):
        super().__init__()
        self.channel_name = channel_name
        self.file_queue = file_queue
    
    def on_created(self, event):
        """Called when a file is created."""
        if event.is_directory:
            return
        
        path = Path(event.src_path)
        # We watch for .json files as they're written after .bin.zst
        # This ensures the binary file is complete
        if path.suffix == '.json' and path.stem.isdigit():
            minute_boundary = int(path.stem)
            logger.debug(f"{self.channel_name}: Detected new minute file: {minute_boundary}")
            self.file_queue.put((minute_boundary, path.parent))


class MetrologyService:
    """
    Metrology Service: The "Instrument" layer.
    """
    
    def __init__(
        self,
        config: Dict[str, Any],
        channel_name: str,
        frequency_hz: float,
        archive_dir: Path,
        output_dir: Path,
        receiver_grid: str,
        station_config: Dict[str, Any] = None
    ):
        self.config = config
        self.channel_name = channel_name
        self.frequency_hz = frequency_hz
        self.archive_dir = Path(archive_dir)
        self.output_dir = Path(output_dir)
        self.receiver_grid = receiver_grid
        self.station_config = station_config or {}
        
        # State
        self.running = False
        self.minutes_processed = 0
        self.processed_minutes = set()
        self.last_minute_unix = None
        self.start_time = time.time()
        self.status_file = self.output_dir / "status.json"
        
        # RTP Offset Learning
        self._rtp_to_unix_offset = None
        self._offset_samples = []
        
        # Timing Authority Configuration (2026-02-01)
        # In RTP mode: start_system_time from metadata IS authoritative (GPS+PPS)
        # In FUSION mode: bootstrap reference provides RTP-to-UTC mapping
        self._timing_config = TimingConfig.from_config(config)
        self._is_rtp_authority = self._timing_config.authority == TimingAuthority.RTP
        if self._is_rtp_authority:
            logger.info(f"[TIMING] RTP authority mode - using metadata start_system_time directly")
        else:
            logger.info(f"[TIMING] FUSION authority mode - engine handles timing lock internally")
        
        # NOTE (2026-02-03): Bootstrap functionality migrated into MetrologyEngine.
        # The engine's fusion_state handles timing lock - no external bootstrap needed.
        
        # Initialize Engine
        # Extract precise coords if available
        lat = self.station_config.get('latitude')
        lon = self.station_config.get('longitude')
        
        self.engine = MetrologyEngine(
            raw_buffer_dir=self.archive_dir,
            output_dir=self.output_dir,
            channel_name=self.channel_name,
            frequency_hz=self.frequency_hz,
            receiver_grid=self.receiver_grid,
            sample_rate=config.get('sample_rate', 24000),
            precise_lat=lat,
            precise_lon=lon,
            is_rtp_authority=self._is_rtp_authority
        )
        
        # Initialize Writer
        # Resolve correct subdirectory via Registry
        writer_output_dir = DataProductRegistry.get_data_dir(
            channel_dir=self.output_dir,
            product_level="L1",
            product_name="metrology_measurements",
            create=True
        )
        
        self.writer = DataProductWriter(
            output_dir=writer_output_dir,
            product_level="L1",
            product_name="metrology_measurements",
            channel=self.channel_name,
            version="v1",
            processing_version="1.0.0",
            station_metadata=self.station_config
        )
        
        # Tiered Storage (Hot/Cold buffer)
        self._tiered_manager = None
        if TieredStorageManager:
            # Import config class
            from hf_timestd.core.tiered_storage import TieredStorageConfig
            # archive_dir is typically /dev/shm/timestd/raw_buffer/CHANNEL
            # We need hot_buffer_root = /dev/shm/timestd and cold_buffer_root = /var/lib/timestd
            hot_root = self.archive_dir.parent.parent  # /dev/shm/timestd
            cold_root = Path('/var/lib/timestd')
            tiered_config = TieredStorageConfig(
                hot_buffer_root=hot_root,
                cold_buffer_root=cold_root,
                auto_configure=False,
                hot_minutes=5
            )
            self._tiered_manager = TieredStorageManager(tiered_config)
            logger.info(f"Tiered storage manager initialized: hot={hot_root}, cold={cold_root}")
        
        # File watcher (inotify-based) for immediate processing
        self._file_queue = queue.Queue()
        self._observer = None
        self._use_file_watcher = WATCHDOG_AVAILABLE
        if self._use_file_watcher:
            self._setup_file_watcher()
        else:
            logger.warning("watchdog not available, falling back to polling mode")
        
        # CHU FSK Writer (for CHU channels only)
        self.fsk_writer = None
        if 'CHU' in channel_name.upper():
            fsk_output_dir = DataProductRegistry.get_data_dir(
                channel_dir=self.output_dir,
                product_level="L2",
                product_name="chu_fsk",
                create=True
            )
            self.fsk_writer = DataProductWriter(
                output_dir=fsk_output_dir,
                product_level="L2",
                product_name="chu_fsk",
                channel=self.channel_name,
                version="v1",
                processing_version="1.0.0",
                station_metadata=self.station_config
            )
            logger.info(f"CHU FSK writer initialized for {channel_name}")
        
        # Test Signal Writer (for WWV/WWVH channels - minutes 8 and 44)
        self.test_signal_writer = None
        if 'CHU' not in channel_name.upper():  # WWV/WWVH channels only
            test_signal_output_dir = DataProductRegistry.get_data_dir(
                channel_dir=self.output_dir,
                product_level="L2",
                product_name="test_signal",
                create=True
            )
            self.test_signal_writer = DataProductWriter(
                output_dir=test_signal_output_dir,
                product_level="L2",
                product_name="test_signal",
                channel=self.channel_name,
                version="v1",
                processing_version="1.0.0",
                station_metadata=self.station_config
            )
            logger.info(f"Test signal writer initialized for {channel_name}")
        
        # Tick Timing Writer (for per-second timing estimates)
        # Provides 55+ timing estimates per minute for improved precision
        tick_output_dir = DataProductRegistry.get_data_dir(
            channel_dir=self.output_dir,
            product_level="L2",
            product_name="tick_timing",
            create=True
        )
        self.tick_writer = DataProductWriter(
            output_dir=tick_output_dir,
            product_level="L2",
            product_name="tick_timing",
            channel=self.channel_name,
            version="v1",
            processing_version="1.0.0",
            station_metadata=self.station_config
        )
        logger.info(f"Tick timing writer initialized for {channel_name}")
             
        logger.info(f"MetrologyService initialized for {channel_name}")

    def _setup_file_watcher(self):
        """Set up inotify-based file watcher for immediate processing."""
        if not WATCHDOG_AVAILABLE:
            return
        
        # Watch today's date directory specifically (more reliable than recursive)
        today = datetime.now(timezone.utc).strftime('%Y%m%d')
        watch_dir = self.archive_dir / today
        
        # Ensure directory exists
        watch_dir.mkdir(parents=True, exist_ok=True)
        
        # Create observer and handler
        self._observer = Observer()
        self._file_handler = MinuteFileHandler(self.channel_name, self._file_queue)
        self._current_watch_date = today
        
        # Watch the specific date directory (non-recursive is more reliable on tmpfs)
        self._observer.schedule(self._file_handler, str(watch_dir), recursive=False)
        self._observer.start()
        logger.info(f"File watcher started on {watch_dir}")
    
    def _stop_file_watcher(self):
        """Stop the file watcher."""
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5.0)
            self._observer = None
            logger.info("File watcher stopped")

    def run(self):
        """Main service loop with inotify-based file watching."""
        self.running = True
        logger.info("Starting MetrologyService loop")
        
        # Handle signals
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)
        
        try:
            if self._use_file_watcher:
                logger.info("Entering main loop (inotify mode)")
                self._run_inotify_mode()
            else:
                logger.info("Entering main loop (polling mode)")
                self._run_polling_mode()
                
        except Exception as e:
            logger.error(f"MetrologyService crashed: {e}", exc_info=True)
        finally:
            self.stop()
    
    def _run_inotify_mode(self):
        """Run using inotify-based file watching with polling fallback."""
        # First, catch up on any missed minutes (files already in directory)
        self._process_backlog()
        
        last_poll_time = time.time()
        poll_interval = 5.0  # Poll every 5 seconds as fallback
        
        while self.running:
            try:
                # Wait for new file notification (with short timeout)
                minute_boundary, file_dir = self._file_queue.get(timeout=2.0)
                
                # Small delay to ensure file is fully written
                time.sleep(0.5)
                
                if minute_boundary not in self.processed_minutes:
                    logger.info(f"Processing minute {minute_boundary} (inotify triggered)")
                    success = self.process_minute(minute_boundary)
                    if success:
                        self.processed_minutes.add(minute_boundary)
                        self._cleanup_processed_set()
                        logger.info(f"Minute {minute_boundary} processed successfully")
                    else:
                        logger.warning(f"Failed to process minute {minute_boundary}")
                        
            except queue.Empty:
                # Fallback: poll for new files periodically (inotify may miss events on tmpfs)
                now = time.time()
                if now - last_poll_time >= poll_interval:
                    last_poll_time = now
                    self._poll_for_new_files()
    
    def _run_polling_mode(self):
        """Fallback polling mode when watchdog is not available."""
        while self.running:
            # Determine next minute to process
            target_minute = self._get_latest_minute()
            logger.info(f"Target minute: {target_minute}")
            
            # Process
            if target_minute not in self.processed_minutes:
                logger.info(f"Processing minute {target_minute}")
                success = self.process_minute(target_minute)
                if success:
                    self.processed_minutes.add(target_minute)
                    self._cleanup_processed_set()
                    logger.info(f"Minute {target_minute} processed successfully")
                else:
                    logger.debug(f"Minute {target_minute} not ready yet")
            
            # Wait until next minute boundary (with 5s margin)
            now = time.time()
            seconds_into_minute = now % 60
            wait_time = max(5.0, 65.0 - seconds_into_minute)
            logger.debug(f"Waiting {wait_time:.1f}s for next minute")
            time.sleep(wait_time)
    
    def _poll_for_new_files(self):
        """Poll for new files (fallback when inotify misses events)."""
        # Check the last few minutes for any unprocessed files
        now = time.time()
        current_minute = (int(now) // 60) * 60
        
        # Check last 3 minutes (files are written at end of minute)
        for minutes_ago in range(3, 0, -1):
            target_minute = current_minute - (minutes_ago * 60)
            if target_minute in self.processed_minutes:
                continue
            
            # Check if file exists
            data = self._read_binary_minute(target_minute)
            if data is not None:
                logger.info(f"Processing minute {target_minute} (poll detected)")
                success = self.process_minute(target_minute)
                if success:
                    self.processed_minutes.add(target_minute)
                    self._cleanup_processed_set()
                    logger.info(f"Minute {target_minute} processed successfully")
    
    def _process_backlog(self):
        """Process any files that exist but haven't been processed yet."""
        logger.info("Checking for backlog files...")
        
        # Look for existing files in the archive directory
        now = time.time()
        current_minute = (int(now) // 60) * 60
        
        # Check last 10 minutes for unprocessed files
        for minutes_ago in range(10, 0, -1):
            target_minute = current_minute - (minutes_ago * 60)
            if target_minute in self.processed_minutes:
                continue
            
            # Check if file exists
            data = self._read_binary_minute(target_minute)
            if data is not None:
                logger.info(f"Processing backlog minute {target_minute}")
                success = self.process_minute(target_minute)
                if success:
                    self.processed_minutes.add(target_minute)
                    logger.info(f"Backlog minute {target_minute} processed successfully")

    def process_minute(self, minute_boundary: int) -> bool:
        """Process a single minute."""
        # Read IQ Data
        data = self._read_binary_minute(minute_boundary)
        if data is None:
            logger.debug(f"No data for minute {minute_boundary}")
            return False
            
        iq_samples, system_time, rtp_timestamp = data
        
        # Run Engine
        try:
            results = self.engine.process_minute(
                iq_samples=iq_samples,
                system_time=system_time,
                rtp_timestamp=rtp_timestamp
            )
            
            # NOTE (2026-02-03): Bootstrap functionality migrated into MetrologyEngine.
            # The engine's fusion_state handles timing refinement internally.
            
            # Write Results
            for res in results:
                # Convert Pydantic model to dict for writer
                # HDF5 writer expects dict matching schema
                # We can use model_dump(mode='json')
                
                # IMPORTANT: DataProductWriter expects specific schema fields.
                # L1MetrologyMeasurement model has fields like 'station_id' which is Enum.
                # model_dump() handles enum -> int/str conversion if configured?
                # Pydantic v2 model_dump(mode='json') converts Enums to values.
                
                rec = res.model_dump(mode='json')
                
                # Schema expects 'processed_at', 'processing_version'
                rec['processed_at'] = datetime.now(timezone.utc).isoformat()
                rec['processing_version'] = "1.0.0"
                
                self.writer.write_measurement(rec)
                
            # Write CHU FSK data if available
            if self.fsk_writer and hasattr(self.engine, '_last_chu_fsk_data'):
                fsk_data = self.engine._last_chu_fsk_data
                if fsk_data and fsk_data.get('fsk_valid'):
                    fsk_rec = {
                        'timestamp_utc': datetime.now(timezone.utc).isoformat(),
                        'minute_boundary_utc': minute_boundary,
                        'channel': self.channel_name,
                        'fsk_valid': fsk_data.get('fsk_valid', False),
                        'frames_decoded': fsk_data.get('fsk_frames_decoded', 0),
                        'decode_confidence': fsk_data.get('fsk_confidence', 0.0),
                        'decoded_day': fsk_data.get('decoded_day'),
                        'decoded_hour': fsk_data.get('decoded_hour'),
                        'decoded_minute': fsk_data.get('decoded_minute'),
                        'dut1_seconds': fsk_data.get('dut1_seconds'),
                        'tai_utc': fsk_data.get('tai_utc'),
                        'year': fsk_data.get('year'),
                        'timing_offset_ms': fsk_data.get('timing_offset_ms'),
                        'processed_at': datetime.now(timezone.utc).isoformat(),
                        'processing_version': "1.0.0"
                    }
                    try:
                        self.fsk_writer.write_measurement(fsk_rec)
                        logger.info(f"CHU FSK data written: DUT1={fsk_data.get('dut1_seconds')}s, TAI-UTC={fsk_data.get('tai_utc')}s")
                    except Exception as fsk_err:
                        logger.warning(f"Failed to write FSK data: {fsk_err}")
            
            # Write tick timing data (55+ estimates per minute)
            if self.tick_writer and hasattr(self.engine, '_last_tick_results'):
                tick_results = self.engine._last_tick_results
                if tick_results:
                    for station_name, tick_analysis in tick_results.items():
                        # Get expected delay for D_clock calculation
                        expected_delay_ms = None
                        d_clock_ms = None
                        if hasattr(self.engine, '_predict_geometric_delay'):
                            try:
                                expected_delay_ms, _, _ = self.engine._predict_geometric_delay(
                                    station_name, minute_boundary
                                )
                                # D_clock = measured_offset (already relative to expected tick positions)
                                # The tick filter returns offset from expected positions within the buffer
                                d_clock_ms = tick_analysis.mean_timing_offset_ms
                            except Exception:
                                pass
                        
                        tick_rec = {
                            'timestamp_utc': datetime.now(timezone.utc).isoformat(),
                            'minute_boundary_utc': minute_boundary,
                            'channel': self.channel_name,
                            'station': station_name,
                            'frequency_mhz': self.frequency_hz / 1e6,
                            'mean_timing_offset_ms': tick_analysis.mean_timing_offset_ms,
                            'std_timing_offset_ms': tick_analysis.std_timing_offset_ms,
                            'mean_snr_db': tick_analysis.mean_snr_db,
                            'drift_rate_ms_per_sec': tick_analysis.drift_rate_ms_per_sec,
                            'valid_windows': tick_analysis.valid_windows,
                            'total_windows': tick_analysis.total_windows,
                            'overall_confidence': tick_analysis.overall_confidence,
                            'expected_delay_ms': expected_delay_ms,
                            'd_clock_ms': d_clock_ms,
                            'processed_at': datetime.now(timezone.utc).isoformat(),
                            'processing_version': "1.0.0"
                        }
                        try:
                            self.tick_writer.write_measurement(tick_rec)
                            logger.info(f"Tick timing written: {station_name} "
                                       f"offset={tick_analysis.mean_timing_offset_ms:+.2f}ms "
                                       f"±{tick_analysis.std_timing_offset_ms:.2f}ms "
                                       f"({tick_analysis.valid_windows}/{tick_analysis.total_windows} windows)")
                        except Exception as tick_err:
                            logger.warning(f"Failed to write tick data for {station_name}: {tick_err}")
                
            # Write test signal for minutes 8 and 44 (WWV/WWVH channel sounding)
            minute_number = (minute_boundary // 60) % 60
            if minute_number in [8, 44] and self.test_signal_writer:
                self._write_test_signal(minute_boundary, iq_samples, minute_number)
                
            self.minutes_processed += 1
            self._write_status(minute_boundary, results)
            
            logger.info(f"Processed minute {minute_boundary}: {len(results)} measurements")
            return True
            
        except Exception as e:
            logger.error(f"Error processing minute {minute_boundary}: {e}", exc_info=True)
            return False
    
    def stop(self):
        """Stop service."""
        logger.info("Stopping MetrologyService...")
        self.running = False
        self._stop_file_watcher()
        if self.writer:
            self.writer.close()
        if self.fsk_writer:
            self.fsk_writer.close()
        if self.test_signal_writer:
            self.test_signal_writer.close()
        if self.tick_writer:
            self.tick_writer.close()
    
    def _write_test_signal(self, minute_boundary: int, iq_samples: np.ndarray, minute_number: int):
        """
        Detect and write test signal for minutes 8 and 44.
        
        Minute 8: WWV test signal (WWVH silent)
        Minute 44: WWVH test signal (WWV silent)
        """
        try:
            logger.info(f"{self.channel_name}: Processing test signal for minute {minute_number}")
            
            # Detect test signal using the engine's discriminator
            detection = self.engine.discriminator.test_signal_detector.detect(
                iq_samples=iq_samples,
                minute_number=minute_number,
                sample_rate=self.engine.sample_rate
            )
            
            # Determine station from schedule: minute 8 = WWV, minute 44 = WWVH
            station = 'WWV' if minute_number == 8 else 'WWVH'
            
            conf = detection.confidence if detection.confidence is not None else 0.0
            logger.info(
                f"{self.channel_name}: Test signal detection: detected={detection.detected}, "
                f"confidence={conf:.2f}, station={station}"
            )
            
            # Build measurement record
            timestamp_utc = datetime.fromtimestamp(minute_boundary, timezone.utc).isoformat().replace('+00:00', 'Z')
            
            # Determine quality flag
            if not detection.detected:
                quality_flag = 'MISSING'
            elif detection.confidence and detection.confidence >= 0.8:
                quality_flag = 'GOOD'
            elif detection.confidence and detection.confidence >= 0.5:
                quality_flag = 'MARGINAL'
            else:
                quality_flag = 'BAD'
            
            measurement = {
                'timestamp_utc': timestamp_utc,
                'minute_boundary_utc': minute_boundary,
                'minute_number': minute_number,
                'station': station if detection.detected else '',
                'frequency_mhz': self.frequency_hz / 1e6,
                'detected': bool(detection.detected),
                'detection_confidence': detection.confidence if detection.confidence is not None else 0.0,
                'snr_db': detection.snr_db,
                'effective_snr_db': detection.effective_snr_db,
                'multitone_score': detection.multitone_score,
                'chirp_score': detection.chirp_score,
                'burst_score': None,
                'noise_correlation': detection.noise_correlation,
                'toa_offset_ms': detection.toa_offset_ms,
                'toa_source': detection.toa_source or '',
                'burst_toa_offset_ms': detection.burst_toa_offset_ms,
                'delay_spread_ms': detection.delay_spread_ms,
                'coherence_time_sec': detection.coherence_time_sec,
                'frequency_selectivity_db': detection.frequency_selectivity_db,
                'tone_power_2khz_db': detection.tone_powers_db.get(2000) if detection.tone_powers_db else None,
                'tone_power_3khz_db': detection.tone_powers_db.get(3000) if detection.tone_powers_db else None,
                'tone_power_4khz_db': detection.tone_powers_db.get(4000) if detection.tone_powers_db else None,
                'tone_power_5khz_db': detection.tone_powers_db.get(5000) if detection.tone_powers_db else None,
                'fading_variance': detection.fading_variance,
                'scintillation_index': detection.scintillation_index,
                's4_2khz': detection.s4_by_frequency.get(2000) if detection.s4_by_frequency else None,
                's4_3khz': detection.s4_by_frequency.get(3000) if detection.s4_by_frequency else None,
                's4_4khz': detection.s4_by_frequency.get(4000) if detection.s4_by_frequency else None,
                's4_5khz': detection.s4_by_frequency.get(5000) if detection.s4_by_frequency else None,
                's4_frequency_slope': detection.s4_frequency_slope,
                'noise_toa_offset_ms': detection.noise_toa_offset_ms,
                'noise_correlation_peak': detection.noise_correlation_peak,
                'anomaly_detected': bool(detection.anomaly_detected) if detection.anomaly_detected is not None else False,
                'anomaly_type': detection.anomaly_type or 'none',
                'anomaly_confidence': detection.anomaly_confidence,
                'field_strength_db': detection.field_strength_db,
                'field_strength_stability': detection.field_strength_stability,
                'multipath_detected': bool(detection.multipath_detected) if detection.multipath_detected is not None else False,
                'channel_quality': detection.channel_quality or '',
                'quality_flag': quality_flag,
                'processing_version': '1.0.0',
                'processed_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
            }
            
            self.test_signal_writer.write_measurement(measurement)
            logger.info(f"{self.channel_name}: Wrote test signal to HDF5: detected={detection.detected}, station={station}")
            
        except Exception as e:
            logger.error(f"{self.channel_name}: Failed to write test signal: {e}", exc_info=True)

    def _handle_signal(self, signum, frame):
        logger.info(f"Received signal {signum}")
        self.stop()

    def _get_latest_minute(self) -> int:
        """Get latest complete minute (wall clock - 3 min).
        
        Files are written at the END of each minute by the core recorder,
        then may be moved to cold storage. A 3-minute delay ensures:
        - The minute has fully elapsed
        - The file has been written (~1 min after minute start)
        - Any tiered storage archiving has completed
        """
        now = time.time()
        # 3 minute delay for safety/completion
        return ((int(now) // 60) - 3) * 60

    def _read_binary_minute(self, target_minute: int) -> Optional[Tuple[np.ndarray, float, int]]:
        """Read binary IQ data (Tiered Storage aware)."""
        # Re-using logic from Phase2AnalyticsService
        # Simplified for clarity
        
        from datetime import datetime
        dt = datetime.fromtimestamp(target_minute, tz=timezone.utc)
        date_str = dt.strftime('%Y%m%d')
        
        # 1. Locate file
        bin_path = None
        json_path = None
        
        if self._tiered_manager:
            bin_path = self._tiered_manager.find_minute_file(
                self.channel_name, target_minute, date_str
            )
            if bin_path:
                json_path = bin_path.parent / f"{target_minute}.json"
        
        if not bin_path:
            # Check archive
            base = self.archive_dir / date_str / str(target_minute)
            for ext in ['.bin', '.bin.zst', '.bin.lz4']:
                p = Path(str(base) + ext)
                if p.exists():
                    bin_path = p
                    json_path = Path(str(base) + ".json")
                    break
        
        if not bin_path:
            logger.info(f"No binary file found for minute {target_minute}")
            return None
            
        # 2. Read Metadata
        metadata = {}
        if json_path and json_path.exists():
            try:
                with open(json_path) as f:
                    metadata = json.load(f)
            except (OSError, IOError, json.JSONDecodeError) as e:
                logger.debug(f"Could not load metadata file {json_path}: {e}")
                
        # 3. Read Data
        try:
            # Decompression logic...
            # For brevity, implementing basic read. 
            # In production, ensure zstd/lz4 imports.
            # I will assume standard .bin for now or use library if available.
             
            iq_samples = self._load_iq_file(bin_path)
            if iq_samples is None:
                return None
                
            # 4. Determine Time
            # -------------------------------------------------------------
            # ARCHITECTURE (2026-01-29):
            # The raw buffer metadata already contains start_system_time which is
            # NTP-derived wallclock time from the GPSDO "steel ruler". Use this
            # directly - it's already per-channel and doesn't require RTP conversion.
            #
            # The bootstrap reference is SSRC-specific and cannot be used across
            # channels (each channel has its own RTP epoch).
            #
            # Timing Authority Check (2026-02-01):
            # - RTP mode: start_system_time from metadata IS authoritative (GPS+PPS)
            #   No need to wait for bootstrap BCD/FSK confirmation
            # - FUSION mode: Process immediately with wide search window, engine handles lock
            #
            # NOTE (2026-02-03): Bootstrap functionality migrated into MetrologyEngine.
            # The engine's fusion_state handles timing lock internally - no external
            # bootstrap service or reference file needed. We always process; the engine
            # uses wider search windows until lock is achieved.
            if not self._is_rtp_authority:
                # Log fusion state for diagnostics
                if self.engine.fusion_state is not None:
                    fs = self.engine.fusion_state
                    if self.minutes_processed % 5 == 0:
                        logger.info(f"[FUSION] lock_tier={fs.lock_tier.name}, "
                                   f"stations={list(fs._stations_seen)}, "
                                   f"measurements={len(fs.measurements)}")
            
            # Use start_system_time from metadata (NTP-derived, per-channel)
            if 'start_system_time' in metadata:
                system_time = float(metadata['start_system_time'])
                rtp_timestamp = int(metadata.get('start_rtp_timestamp', 0))
                timing_source = "metadata"
                logger.info(
                    f"[TIMING_DIAG] Minute {target_minute}: source={timing_source}, "
                    f"system_time={system_time:.6f}"
                )
            elif 'start_rtp_timestamp' in metadata:
                # Fallback: no system_time, skip (shouldn't happen with current recorder)
                logger.warning(f"No start_system_time in metadata for {target_minute}, skipping")
                return None
            else:
                logger.warning(f"No timing info in metadata for {target_minute}, skipping")
                return None

            # Pad/Clip
            expected_len = self.engine.sample_rate * 60
            if len(iq_samples) < expected_len:
                padded = np.zeros(expected_len, dtype=np.complex64)
                padded[:len(iq_samples)] = iq_samples
                iq_samples = padded
                 
            return iq_samples, system_time, rtp_timestamp
            
        except Exception as e:
            logger.error(f"Read error: {e}")
            return None

    def _load_iq_file(self, path: Path) -> Optional[np.ndarray]:
        """Helper to load IQ file."""
        try:
            if path.suffix == '.zst':
                import zstandard as zstd
                with open(path, 'rb') as f:
                    dctx = zstd.ZstdDecompressor()
                    data = dctx.decompress(f.read())
                    return np.frombuffer(data, dtype=np.complex64)
            elif path.suffix == '.lz4':
                import lz4.frame
                with open(path, 'rb') as f:
                    data = lz4.frame.decompress(f.read())
                    return np.frombuffer(data, dtype=np.complex64)
            else:
                return np.memmap(path, dtype=np.complex64, mode='r')
        except ImportError:
            logger.error("Compression library missing")
            return None
        except (OSError, IOError, ValueError) as e:
            logger.debug(f"Error loading IQ file {path}: {e}")
            return None

    def _write_status(self, minute: int, results: List[L1MetrologyMeasurement]):
        """Write status.json."""
        try:
            status = {
                "service": "metrology",
                "last_update": datetime.now(timezone.utc).isoformat(),
                "channel": self.channel_name,
                "last_minute_processed": minute,
                "minutes_processed": self.minutes_processed,
                "last_results": [r.model_dump(mode='json') for r in results]
            }
            with open(self.status_file, 'w') as f:
                json.dump(status, f, indent=2)
        except Exception as e:
            logger.error(f"Status write failed: {e}")
    
    def _cleanup_processed_set(self):
        """Keep processed set small."""
        now_min = (int(time.time()) // 60) * 60
        old_mins = [m for m in self.processed_minutes if m < now_min - 3600]
        for m in old_mins:
            self.processed_minutes.remove(m)

if __name__ == "__main__":
    import argparse
    import sys
    
    parser = argparse.ArgumentParser(description="Metrology Service (Phase 1)")
    
    # Required args
    parser.add_argument("--archive-dir", required=True, type=Path, help="Input raw buffer directory")
    parser.add_argument("--output-dir", required=True, type=Path, help="Output directory for L1 products")
    parser.add_argument("--channel-name", required=True, help="Channel name (e.g. WWV_15000)")
    parser.add_argument("--frequency-hz", required=True, type=float, help="Center frequency in Hz")
    
    # Optional Station Metadata
    parser.add_argument("--callsign", default="UNKNOWN", help="Receiver callsign")
    parser.add_argument("--grid-square", default="XX00xx", help="Receiver grid square")
    parser.add_argument("--receiver-name", default="HF-TimeStd", help="Receiver name")
    parser.add_argument("--station-id", default="UNKNOWN", help="Station ID")
    parser.add_argument("--instrument-id", default="UNKNOWN", help="Instrument ID")
    
    # Optional Precise Coordinates
    parser.add_argument("--latitude", type=float, help="Receiver latitude")
    parser.add_argument("--longitude", type=float, help="Receiver longitude")
    
    # Service Config
    parser.add_argument("--poll-interval", type=float, default=10.0, help="Polling interval (not used in current loop logic but kept for compat)")
    parser.add_argument("--log-level", default="INFO", help="Logging level")
    parser.add_argument("--state-file", type=Path, help="Persistence state file")
    parser.add_argument("--use-tiered-storage", action="store_true", help="Enable tiered storage manager")
    parser.add_argument("--config-file", type=Path, default=Path("/opt/hf-timestd/config/timestd-config.toml"),
                        help="Path to timestd-config.toml for timing authority settings")
    
    args = parser.parse_args()
    
    # Setup Logging - force level on root logger since basicConfig may be ignored
    # if handlers were already configured by imports
    log_level = getattr(logging, args.log_level.upper())
    logging.basicConfig(
        level=log_level,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%Y-%m-%dT%H:%M:%S%z'
    )
    # Force level on root and our module logger
    logging.getLogger().setLevel(log_level)
    logger.setLevel(log_level)
    
    # Load TOML config for timing authority settings
    toml_config = {}
    if args.config_file and args.config_file.exists():
        try:
            import tomllib
            with open(args.config_file, 'rb') as f:
                toml_config = tomllib.load(f)
            logger.info(f"Loaded config from {args.config_file}")
        except ImportError:
            import tomli as tomllib
            with open(args.config_file, 'rb') as f:
                toml_config = tomllib.load(f)
            logger.info(f"Loaded config from {args.config_file}")
        except Exception as e:
            logger.warning(f"Could not load config file {args.config_file}: {e}")
    
    # Config dict construction - merge TOML timing section
    config = {
        "sample_rate": 24000, # Hardcoded for now, or could be arg/config
        "tiered_storage": args.use_tiered_storage,
        "timing": toml_config.get("timing", {})  # Pass timing section for authority mode
    }
    
    station_config = {
        "callsign": args.callsign,
        "grid_square": args.grid_square,
        "receiver_name": args.receiver_name,
        "station_id": args.station_id,
        "instrument_id": args.instrument_id,
        "latitude": args.latitude,
        "longitude": args.longitude
    }
    
    try:
        service = MetrologyService(
            config=config,
            channel_name=args.channel_name,
            frequency_hz=args.frequency_hz,
            archive_dir=args.archive_dir,
            output_dir=args.output_dir,
            receiver_grid=args.grid_square,
            station_config=station_config
        )
        service.run()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.fatal(f"Service startup failed: {e}", exc_info=True)
        sys.exit(1)
