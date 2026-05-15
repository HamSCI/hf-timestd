#!/usr/bin/env python3
"""
Metrology Service
=================
Real-time DSP and Timestamping Service.

Responsibility:
1. Ingest raw IQ data from the core-recorder's shared-memory ring buffer.
2. Run MetrologyEngine (Tone Detection, Channel Characterization).
3. Write L1_Metrology data products (HDF5).
4. Do NOT perform physics modeling or clock offset calculation.

Data path
---------
`timestd-core-recorder` publishes each batch of samples into a per-channel
SysV ring buffer (see :mod:`hf_timestd.core.ring_buffer`).  This service
attaches to the ring for its own channel and extracts 60-second windows
aligned to UTC minute boundaries.  There is no file I/O on the hot path.
The archive writer in the recorder handles long-term .bin.zst chunks
independently of metrology latency.
"""

import fcntl
import logging
import os
import time
import json
import signal
import sys
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Dict, List, Any, Tuple
import numpy as np

from hf_timestd.core.metrology_engine import MetrologyEngine
from hf_timestd.models import L1MetrologyMeasurement
from hf_timestd.io.hdf5_writer import DataProductWriter
from hf_timestd.io import make_data_product_writer
from hf_timestd.data_product_registry import DataProductRegistry
from hf_timestd.interfaces.data_models import TimingConfig, TimingAuthority
from hf_timestd.core.ring_buffer import (
    RingBufferError,
    RingBufferOverrunError,
)
from hf_timestd.core.ring_buffer_reader import RingBufferReader

logger = logging.getLogger(__name__)


class MetrologyService:
    """
    Metrology Service: The "Instrument" layer.
    """
    
    def __init__(
        self,
        config: Dict[str, Any],
        channel_name: str,
        frequency_hz: float,
        output_dir: Path,
        receiver_grid: str,
        station_config: Dict[str, Any] = None
    ):
        self.config = config
        self.channel_name = channel_name
        self.frequency_hz = frequency_hz
        self.output_dir = Path(output_dir)
        self.receiver_grid = receiver_grid
        self.station_config = station_config or {}

        # Feature flags — read from [metrology] config section.
        # Default both to True so existing deployments without the section are unaffected.
        _metrology_cfg = config.get('metrology', {})
        self._physics_products: bool = bool(_metrology_cfg.get('physics_products', True))
        self._realtime_iono: bool = bool(_metrology_cfg.get('realtime_iono', True))
        if not self._physics_products:
            logger.info(
                f"[{channel_name}] physics_products=false — "
                f"tick_phase / test_signal / detection_attempts / all_arrivals writers disabled"
            )
        if not self._realtime_iono:
            logger.info(
                f"[{channel_name}] realtime_iono=false — "
                f"WAM-IPE/GIRO fetcher disabled; propagation model uses climatological fallback"
            )

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
        
        # Coarse-time producer (METROLOGY.md §4.5 / 2c). Only CHU
        # channels emit, but the config knob is service-level so a
        # single setting covers the whole host.
        _coarse_cfg = (config.get('timing', {}) or {}).get('coarse_time', {}) or {}
        _coarse_enabled = bool(_coarse_cfg.get('enabled', True))
        _coarse_path_str = _coarse_cfg.get('path')
        _coarse_path = Path(_coarse_path_str) if _coarse_path_str else None

        self.engine = MetrologyEngine(
            # raw_buffer_dir is a legacy constructor argument that the
            # engine stores but no longer reads.  Pass a placeholder so
            # we don't have to change the engine signature in Phase 2.
            raw_buffer_dir=Path('/dev/null'),
            output_dir=self.output_dir,
            channel_name=self.channel_name,
            frequency_hz=self.frequency_hz,
            receiver_grid=self.receiver_grid,
            sample_rate=config.get('sample_rate', 24000),
            precise_lat=lat,
            precise_lon=lon,
            is_rtp_authority=self._is_rtp_authority,
            enable_physics_products=self._physics_products,
            enable_coarse_time=_coarse_enabled,
            coarse_time_path=_coarse_path,
        )
        
        # Storage backend selection. Phase 1 of the HDF5 → SQLite
        # migration (see docs/HDF5-TO-SQLITE-MIGRATION.md): each writer
        # is constructed via make_data_product_writer, which returns
        # either the HDF5 writer, the SQLite writer, or a DualWriter
        # forwarding to both — driven by [storage] config knobs.
        # Default config (no [storage] section, or write_sqlite=false)
        # → today's behaviour: HDF5 writer only.
        self._storage_config = config.get('storage', {}) or {}

        # Initialize Writer
        # Resolve correct subdirectory via Registry
        writer_output_dir = DataProductRegistry.get_data_dir(
            channel_dir=self.output_dir,
            product_level="L1",
            product_name="metrology_measurements",
            create=True
        )

        self.writer = make_data_product_writer(
            output_dir=writer_output_dir,
            product_level="L1",
            product_name="metrology_measurements",
            channel=self.channel_name,
            version="v1",
            processing_version="1.0.0",
            station_metadata=self.station_config,
            storage_config=self._storage_config,
        )
        
        # Ring buffer reader — lazily attached in _run_ringbuffer_mode so
        # that a producer restart during our startup does not race us.
        self._ring_reader: Optional[RingBufferReader] = None

        # CHU FSK Writer (for CHU channels only)
        self.fsk_writer = None
        if 'CHU' in channel_name.upper():
            fsk_output_dir = DataProductRegistry.get_data_dir(
                channel_dir=self.output_dir,
                product_level="L2",
                product_name="chu_fsk",
                create=True
            )
            self.fsk_writer = make_data_product_writer(
                output_dir=fsk_output_dir,
                product_level="L2",
                product_name="chu_fsk",
                channel=self.channel_name,
                version="v1",
                processing_version="1.0.0",
                station_metadata=self.station_config,
                storage_config=self._storage_config,
            )
            logger.info(f"CHU FSK writer initialized for {channel_name}")
        
        # Test Signal Writer (for WWV/WWVH channels - minutes 8 and 44)
        # PHYSICS-OPTIONAL: ionospheric sounding product, not needed for Chrony.
        self.test_signal_writer = None
        if self._physics_products and 'CHU' not in channel_name.upper():
            test_signal_output_dir = DataProductRegistry.get_data_dir(
                channel_dir=self.output_dir,
                product_level="L2",
                product_name="test_signal",
                create=True
            )
            self.test_signal_writer = make_data_product_writer(
                output_dir=test_signal_output_dir,
                product_level="L2",
                product_name="test_signal",
                channel=self.channel_name,
                version="v1",
                processing_version="1.0.0",
                station_metadata=self.station_config,
                storage_config=self._storage_config,
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
        self.tick_writer = make_data_product_writer(
            output_dir=tick_output_dir,
            product_level="L2",
            product_name="tick_timing",
            channel=self.channel_name,
            version="v1",
            processing_version="1.0.0",
            station_metadata=self.station_config,
            storage_config=self._storage_config,
        )
        logger.info(f"Tick timing writer initialized for {channel_name}")
        
        # Detection Attempts Writer — PHYSICS-OPTIONAL: threshold-calibration diagnostics.
        # Not consumed by fusion or Chrony; set to None in timing-only mode.
        self.attempts_writer = None
        if self._physics_products:
            attempts_output_dir = DataProductRegistry.get_data_dir(
                channel_dir=self.output_dir,
                product_level="L2",
                product_name="detection_attempts",
                create=True
            )
            self.attempts_writer = make_data_product_writer(
                output_dir=attempts_output_dir,
                product_level="L2",
                product_name="detection_attempts",
                channel=self.channel_name,
                version="v1",
                processing_version="1.0.0",
                station_metadata=self.station_config,
                storage_config=self._storage_config,
            )
            logger.info(f"Detection attempts writer initialized for {channel_name}")

        # Tick Phase Writer — PHYSICS-OPTIONAL: 1 Hz phase time series for ionospheric analysis.
        # Phase drift → Doppler; not consumed by fusion or Chrony.
        self.tick_phase_writer = None
        if self._physics_products:
            tick_phase_output_dir = DataProductRegistry.get_data_dir(
                channel_dir=self.output_dir,
                product_level="L2",
                product_name="tick_phase",
                create=True
            )
            self.tick_phase_writer = make_data_product_writer(
                output_dir=tick_phase_output_dir,
                product_level="L2",
                product_name="tick_phase",
                channel=self.channel_name,
                version="v1",
                processing_version="1.0.0",
                station_metadata=self.station_config,
                storage_config=self._storage_config,
            )
            logger.info(f"Tick phase writer initialized for {channel_name}")

        # All Arrivals Writer — PHYSICS-OPTIONAL: multi-path propagation paths.
        # Records every significant correlation peak — not just the dominant arrival.
        # Explicitly documented: "does not feed the metrology pipeline."
        self.all_arrivals_writer = None
        if self._physics_products:
            all_arrivals_output_dir = DataProductRegistry.get_data_dir(
                channel_dir=self.output_dir,
                product_level="L1",
                product_name="all_arrivals",
                create=True
            )
            self.all_arrivals_writer = make_data_product_writer(
                output_dir=all_arrivals_output_dir,
                product_level="L1",
                product_name="all_arrivals",
                channel=self.channel_name,
                version="v1",
                processing_version="1.0.0",
                station_metadata=self.station_config,
                storage_config=self._storage_config,
            )
            logger.info(f"All-arrivals writer initialized for {channel_name}")

        # IonoDataService — real-time WAM-IPE/GIRO fetcher.
        # Gated on realtime_iono; on false, propagation model uses climatological fallback.
        self._iono_service = None
        if self._realtime_iono and lat is not None and lon is not None:
            try:
                from .iono_data_service import IonoDataService
                self._iono_service = IonoDataService.get_instance()
                self._iono_service.start()
                logger.info("IonoDataService background fetcher started")
            except Exception as e:
                logger.warning(f"IonoDataService not available: {e}")
             
        logger.info(f"MetrologyService initialized for {channel_name}")

    # Poll interval for the ring-buffer consumer loop.
    _RING_POLL_SEC = 0.5
    # How long (seconds) to wait past a minute boundary before extracting.
    # Keeps the producer a little ahead of us on every minute and masks
    # jitter in the first few batches of the next minute.
    _RING_BOUNDARY_SETTLE_SEC = 0.5
    # Fresh bootstrap sits this many minutes behind the head so the very
    # first extract is comfortably inside the ring.
    _RING_BOOTSTRAP_LAG_MIN = 2
    # On RingBufferOverrunError the consumer jumps forward this many
    # minutes past whatever the current head is so the next extract lands
    # in freshly-written territory.
    _RING_OVERRUN_JUMP_MIN = 2
    # How long (seconds) of head stagnation triggers a "recorder wedged"
    # log line.  Real stalls are caught by the systemd watchdog pattern
    # via the pipeline watchdog; this is diagnostics only.
    _RING_STALL_WARN_SEC = 120.0

    def run(self):
        """Main service loop — attach to the ring buffer and consume minutes."""
        self.running = True
        self._resource_guardian = getattr(self, '_resource_guardian', None)
        logger.info("Starting MetrologyService loop (ring-buffer mode)")

        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        try:
            self._run_ringbuffer_mode()
        except Exception as e:
            logger.error(f"MetrologyService crashed: {e}", exc_info=True)
        finally:
            self.stop()

    def _attach_ring(self) -> Optional[RingBufferReader]:
        """Attach to the per-channel ring buffer, retrying until the producer is up."""
        while self.running:
            try:
                reader = RingBufferReader.attach(self.channel_name)
                logger.info(
                    f"[{self.channel_name}] attached to ring buffer "
                    f"(sample_rate={reader._sample_rate}, "
                    f"ring_size={reader._ring_size_samples})"
                )
                return reader
            except RingBufferError as exc:
                logger.info(
                    f"[{self.channel_name}] waiting for producer: {exc}"
                )
                time.sleep(2.0)
            except Exception as exc:  # pragma: no cover - defensive
                logger.error(
                    f"[{self.channel_name}] ring attach failed: {exc}",
                    exc_info=True,
                )
                time.sleep(2.0)
        return None

    def _run_ringbuffer_mode(self):
        """Consume sample windows from the producer's per-channel ring buffer.

        Poll the write cursor every ``_RING_POLL_SEC`` seconds.  When the
        head has advanced past ``next_minute + 60 + settle``, extract 60 s
        of samples starting at ``next_minute`` and hand them to
        :meth:`_process_minute_data`.

        Recovery semantics:
        - No samples yet / anchor not installed → poll.
        - :class:`RingBufferOverrunError` → jump forward to
          ``head_utc - _RING_OVERRUN_JUMP_MIN * 60`` and continue.
        - Producer restart → next ``extract_interval`` call raises overrun
          when the epoch changes; handled as above.
        """
        from hf_timestd.core.buffer_timing import resolve_buffer_timing

        self._ring_reader = self._attach_ring()
        if self._ring_reader is None:
            return
        reader = self._ring_reader

        next_minute: Optional[int] = None
        last_head_utc: Optional[float] = None
        last_head_change = time.monotonic()
        last_guardian_check = 0.0

        while self.running:
            try:
                now_mono = time.monotonic()

                # Resource guardian — cheap, no-op most of the time.
                if (
                    self._resource_guardian
                    and now_mono - last_guardian_check >= 30.0
                ):
                    from hf_timestd.core.resource_guardian import ResourceState
                    rs = self._resource_guardian.watchdog_check()
                    last_guardian_check = now_mono
                    if rs.state in (ResourceState.STOP, ResourceState.EMERGENCY):
                        logger.critical(
                            f"[{self.channel_name}] resource guardian: "
                            f"{rs.message} — stopping"
                        )
                        self.running = False
                        break

                cursor = reader.write_cursor()
                if cursor == 0:
                    time.sleep(self._RING_POLL_SEC)
                    continue
                head_utc = reader.head_utc(cursor)
                if head_utc is None:
                    time.sleep(self._RING_POLL_SEC)
                    continue

                # Bootstrap: seed the first minute to process once the
                # head UTC is known.  Start a couple of minutes back so
                # the very first extract is well inside the ring window.
                if next_minute is None:
                    next_minute = (
                        (int(head_utc) // 60) * 60
                        - self._RING_BOOTSTRAP_LAG_MIN * 60
                    )
                    logger.info(
                        f"[{self.channel_name}] bootstrap: head_utc={head_utc:.3f}, "
                        f"next_minute={next_minute}"
                    )

                # Head stagnation check for diagnostics only.
                if last_head_utc is None or head_utc > last_head_utc:
                    last_head_utc = head_utc
                    last_head_change = now_mono
                elif (now_mono - last_head_change) > self._RING_STALL_WARN_SEC:
                    logger.warning(
                        f"[{self.channel_name}] ring head stagnant for "
                        f"{(now_mono - last_head_change):.0f}s — "
                        f"recorder may be wedged"
                    )
                    last_head_change = now_mono  # rate-limit

                target_end = next_minute + 60.0 + self._RING_BOUNDARY_SETTLE_SEC
                if head_utc < target_end:
                    time.sleep(self._RING_POLL_SEC)
                    continue

                try:
                    samples, metadata = reader.extract_interval(
                        utc_start=float(next_minute),
                        duration_sec=60.0,
                    )
                except RingBufferOverrunError as exc:
                    new_next = (
                        (int(head_utc) // 60) * 60
                        - self._RING_OVERRUN_JUMP_MIN * 60
                    )
                    logger.warning(
                        f"[{self.channel_name}] overrun on minute {next_minute}: "
                        f"{exc}; resyncing next_minute={new_next}"
                    )
                    next_minute = new_next
                    continue
                except RingBufferError as exc:
                    logger.debug(
                        f"[{self.channel_name}] extract_interval failed: {exc}"
                    )
                    time.sleep(self._RING_POLL_SEC)
                    continue

                buffer_timing = resolve_buffer_timing(
                    metadata, sample_rate=self.engine.sample_rate
                )
                if buffer_timing.source == 'no_timing':
                    logger.warning(
                        f"[{self.channel_name}] no RTP timing for minute "
                        f"{next_minute}, skipping"
                    )
                    next_minute += 60
                    continue
                system_time = buffer_timing.sample0_utc
                rtp_timestamp = int(metadata.get('start_rtp_timestamp', 0))

                if next_minute not in self.processed_minutes:
                    logger.info(
                        f"[{self.channel_name}] processing minute {next_minute}"
                    )
                    success = self._process_minute_data(
                        minute_boundary=next_minute,
                        iq_samples=samples,
                        system_time=system_time,
                        rtp_timestamp=rtp_timestamp,
                        metadata=metadata,
                        buffer_timing=buffer_timing,
                    )
                    if success:
                        self.processed_minutes.add(next_minute)
                        self._cleanup_processed_set()
                        logger.info(
                            f"[{self.channel_name}] minute {next_minute} "
                            f"processed successfully"
                        )
                    else:
                        logger.warning(
                            f"[{self.channel_name}] minute {next_minute} "
                            f"processing failed"
                        )

                next_minute += 60

            except Exception as exc:
                logger.error(
                    f"[{self.channel_name}] ring consumer error: {exc}",
                    exc_info=True,
                )
                time.sleep(1.0)

    def _process_minute_data(
        self,
        minute_boundary: int,
        iq_samples: np.ndarray,
        system_time: float,
        rtp_timestamp: int,
        metadata: Dict[str, Any],
        buffer_timing,
    ) -> bool:
        """Run the engine on pre-extracted samples and write data products.

        This is the tail of the old file-mode ``process_minute`` — every
        caller now feeds samples from the ring buffer.
        """
        # Run Engine
        try:
            results = self.engine.process_minute(
                iq_samples=iq_samples,
                system_time=system_time,
                rtp_timestamp=rtp_timestamp,
                buffer_timing=buffer_timing
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
            
            # Write tick timing data from TickEdgeDetector — the single source
            # for all three observables:
            #   - d_clock_ms: front-edge ensemble timing (AM-domain, UTC-referenced)
            #   - doppler_hz: carrier phase slope across the minute (IQ-domain)
            #   - mean_snr_db: per-tick matched filter SNR
            edge_results = getattr(self.engine, '_last_edge_results', None) or {}
            
            if self.tick_writer and edge_results:
                for station_name, edge_result in edge_results.items():
                    if edge_result.ensemble_n_edges < 3:
                        continue
                    
                    # Get expected delay for the HDF5 record (informational)
                    expected_delay_ms = None
                    if hasattr(self.engine, '_predict_geometric_delay'):
                        try:
                            expected_delay_ms, _, _ = self.engine._predict_geometric_delay(
                                station_name, minute_boundary
                            )
                        except Exception as e:
                            logger.debug(f"Ignored exception: {e}")
                            pass
                    
                    d_clock_ms = edge_result.ensemble_timing_error_ms if edge_result.ensemble_n_edges >= 5 else None
                    d_clock_uncertainty_ms = edge_result.ensemble_uncertainty_ms if d_clock_ms is not None else None
                    
                    tick_rec = {
                        'timestamp_utc': datetime.now(timezone.utc).isoformat(),
                        'minute_boundary_utc': minute_boundary,
                        'channel': self.channel_name,
                        'station': station_name,
                        'frequency_mhz': self.frequency_hz / 1e6,
                        'mean_snr_db': edge_result.mean_edge_snr_db,
                        'valid_windows': edge_result.n_detected,
                        'total_windows': edge_result.n_attempted,
                        'overall_confidence': edge_result.confidence,
                        'expected_delay_ms': expected_delay_ms,
                        'd_clock_ms': d_clock_ms,
                        'd_clock_uncertainty_ms': d_clock_uncertainty_ms,
                        'd_clock_source': 'edge_ensemble',
                        'doppler_hz': edge_result.doppler_hz,
                        'doppler_uncertainty_hz': edge_result.doppler_uncertainty_hz,
                        'ensemble_n_edges': edge_result.ensemble_n_edges,
                        'n_clean': edge_result.n_clean,
                        'processed_at': datetime.now(timezone.utc).isoformat(),
                        'processing_version': "5.0.0"
                    }
                    try:
                        self.tick_writer.write_measurement(tick_rec)
                        dc_str = f"d_clock={d_clock_ms:+.2f}ms" if d_clock_ms is not None else "d_clock=None"
                        dop_str = f"doppler={edge_result.doppler_hz:+.4f}Hz" if edge_result.doppler_hz is not None else "doppler=None"
                        logger.info(f"Tick timing written: {station_name} "
                                   f"{dc_str}, {dop_str}, "
                                   f"SNR={edge_result.mean_edge_snr_db:.1f}dB, "
                                   f"{edge_result.ensemble_n_edges} edges")
                    except Exception as tick_err:
                        logger.warning(f"Failed to write tick data for {station_name}: {tick_err}")
                
            # Write per-window tick phase data (~55 rows per station per minute)
            # Each row is one overlapping correlation window with phase_rad, giving
            # a 1 Hz phase time series for ionospheric dynamics analysis.
            if self.tick_phase_writer and hasattr(self.engine, '_last_tick_results'):
                tick_results = self.engine._last_tick_results
                if tick_results:
                    now_iso = datetime.now(timezone.utc).isoformat()
                    phase_batch = []
                    for station_name, tick_analysis in tick_results.items():
                        for wr in tick_analysis.window_results:
                            phase_batch.append({
                                'timestamp_utc': now_iso,
                                'minute_boundary_utc': minute_boundary,
                                'channel': self.channel_name,
                                'station': station_name,
                                'frequency_mhz': self.frequency_hz / 1e6,
                                'window_start_second': wr.window_start_second,
                                'window_end_second': wr.window_end_second,
                                'window_center_second': (wr.window_start_second + wr.window_end_second) / 2.0,
                                'phase_rad': wr.phase_rad,
                                'carrier_phase_rad': getattr(wr, 'carrier_phase_rad', 0.0),
                                'dc_carrier_phase_rad': getattr(wr, 'dc_carrier_phase_rad', 0.0),
                                'timing_offset_ms': wr.timing_offset_ms,
                                'timing_uncertainty_ms': wr.timing_uncertainty_ms,
                                'snr_db': wr.snr_db,
                                'correlation_peak': wr.correlation_peak,
                                'coherence_quality': wr.coherence_quality,
                                'valid_ticks': wr.valid_ticks,
                                'processed_at': now_iso,
                                'processing_version': "1.0.0"
                            })
                    if phase_batch:
                        try:
                            self.tick_phase_writer.write_measurements_batch(phase_batch)
                            logger.debug(f"Tick phase written: {len(phase_batch)} windows")
                        except Exception as ph_err:
                            logger.debug(f"Failed to write tick phase batch: {ph_err}")

            # Write detection attempts (every measurement attempt for threshold calibration)
            if self.attempts_writer and hasattr(self.engine, '_last_rtp_attempts'):
                rtp_attempts = self.engine._last_rtp_attempts
                if rtp_attempts:
                    now_iso = datetime.now(timezone.utc).isoformat()
                    for attempt in rtp_attempts:
                        attempt_rec = {
                            'timestamp_utc': now_iso,
                            'minute_boundary_utc': minute_boundary,
                            'channel': self.channel_name,
                            'station': attempt.get('station', ''),
                            'frequency_hz': attempt.get('frequency_hz', 0),
                            'frequency_mhz': self.frequency_hz / 1e6,
                            'utc_second': attempt.get('utc_second', 0),
                            'tone_duration_sec': attempt.get('tone_duration_sec', 0),
                            'detected': attempt.get('detected', False),
                            'rejection_reason': attempt.get('rejection_reason', ''),
                            'arrival_ms': attempt.get('arrival_ms', 0),
                            'expected_delay_ms': attempt.get('expected_delay_ms', 0),
                            'timing_error_ms': attempt.get('timing_error_ms', 0),
                            'snr_db': attempt.get('snr_db', -99),
                            'corr_snr_db': attempt.get('corr_snr_db', -99),
                            'peak_correlation': attempt.get('peak_correlation', 0),
                            'processed_at': now_iso,
                            'processing_version': "1.0.0"
                        }
                        try:
                            self.attempts_writer.write_measurement(attempt_rec)
                        except Exception as att_err:
                            logger.debug(f"Failed to write attempt record: {att_err}")
                    
                    n_det = sum(1 for a in rtp_attempts if a.get('detected'))
                    logger.debug(f"Detection attempts written: {len(rtp_attempts)} total, "
                                f"{n_det} detected, {len(rtp_attempts) - n_det} rejected")
            
            # Write all-arrivals (multi-path physics product)
            # For each detected attempt that has secondary correlation peaks,
            # write one row per arrival path.  This is purely additive — the
            # metrology pipeline is unaffected.
            if self.all_arrivals_writer and hasattr(self.engine, '_last_rtp_attempts'):
                rtp_attempts = self.engine._last_rtp_attempts
                if rtp_attempts:
                    now_iso = datetime.now(timezone.utc).isoformat()
                    n_multipath = 0
                    for attempt in rtp_attempts:
                        if not attempt.get('detected'):
                            continue
                        arrivals = attempt.get('all_arrivals', [])
                        if not arrivals:
                            continue
                        utc_sec = attempt.get('utc_second', 0)
                        station = attempt.get('station', '')
                        freq_mhz = self.frequency_hz / 1e6
                        expected_ms = attempt.get('expected_delay_ms', 0.0)
                        for arr in arrivals:
                            rec = {
                                'timestamp_utc': now_iso,
                                'minute_boundary_utc': minute_boundary,
                                'channel': self.channel_name,
                                'station': station,
                                'frequency_mhz': freq_mhz,
                                'utc_second': utc_sec,
                                'peak_rank': arr.get('peak_rank', 0),
                                'arrival_ms': arr.get('arrival_ms', 0.0),
                                'timing_error_ms': arr.get('timing_error_ms', 0.0),
                                'corr_snr_db': arr.get('corr_snr_db', -99.0),
                                'peak_value': arr.get('peak_value', 0.0),
                                'model_expected_ms': expected_ms,
                                'carrier_phase_rad': 0.0,
                                'detection_method': 'tone_correlator',
                                'sec_in_minute': int(utc_sec % 60) if utc_sec else 0,
                                'processed_at': now_iso,
                                'processing_version': "2.0.0",
                            }
                            try:
                                self.all_arrivals_writer.write_measurement(rec)
                                if arr.get('peak_rank', 0) > 0:
                                    n_multipath += 1
                            except Exception as arr_err:
                                logger.debug(f"Failed to write all_arrivals record: {arr_err}")
                    if n_multipath > 0:
                        logger.info(f"All-arrivals: {n_multipath} secondary path(s) recorded")

            # Write per-tick edge detections to all_arrivals (Doppler-Delay product).
            # Each detected tick from the TickEdgeDetector becomes one row with
            # timing_error_ms and carrier_phase_rad.  This enables Doppler-Delay
            # scatter plots: phase slope across seconds = Doppler, timing_error =
            # propagation delay residual.  Multipath modes show as distinct
            # clusters in the (delay, phase) plane even when temporally unresolved.
            if self.all_arrivals_writer and edge_results:
                now_iso = datetime.now(timezone.utc).isoformat()
                freq_mhz = self.frequency_hz / 1e6
                n_edge_ticks = 0
                for station_name, edge_result in edge_results.items():
                    if not edge_result.edges:
                        continue
                    expected_delay_ms = None
                    if hasattr(self.engine, '_predict_geometric_delay'):
                        try:
                            expected_delay_ms, _, _ = self.engine._predict_geometric_delay(
                                station_name, minute_boundary
                            )
                        except Exception as e:
                            logger.debug(f"Ignored exception: {e}")
                            pass
                    n_clean_multipath = 0
                    for tick in edge_result.edges:
                        if not tick.detected:
                            continue
                        rec = {
                            'timestamp_utc': now_iso,
                            'minute_boundary_utc': minute_boundary,
                            'channel': self.channel_name,
                            'station': station_name,
                            'frequency_mhz': freq_mhz,
                            'utc_second': tick.utc_second,
                            'peak_rank': 0,
                            'arrival_ms': tick.front_edge_sample * 1000.0 / self.engine.sample_rate,
                            'timing_error_ms': tick.timing_error_ms,
                            'corr_snr_db': tick.corr_snr_db,
                            'peak_value': 0.0,
                            'model_expected_ms': expected_delay_ms or 0.0,
                            'carrier_phase_rad': tick.carrier_phase_rad,
                            'detection_method': 'edge_tick',
                            'sec_in_minute': tick.sec_in_minute,
                            'processed_at': now_iso,
                            'processing_version': "2.0.0",
                        }
                        try:
                            self.all_arrivals_writer.write_measurement(rec)
                            n_edge_ticks += 1
                        except Exception as edge_err:
                            logger.debug(f"Failed to write edge tick record: {edge_err}")
                        
                        # Write CLEAN multipath arrivals (rank >= 1 only;
                        # rank 0 is the same as the edge_tick primary above).
                        for comp in tick.clean_arrivals:
                            if comp.peak_rank == 0:
                                continue
                            clean_rec = {
                                'timestamp_utc': now_iso,
                                'minute_boundary_utc': minute_boundary,
                                'channel': self.channel_name,
                                'station': station_name,
                                'frequency_mhz': freq_mhz,
                                'utc_second': tick.utc_second,
                                'peak_rank': comp.peak_rank,
                                'arrival_ms': 0.0,
                                'timing_error_ms': comp.timing_error_ms,
                                'corr_snr_db': comp.corr_snr_db,
                                'peak_value': comp.relative_amplitude,
                                'model_expected_ms': expected_delay_ms or 0.0,
                                'carrier_phase_rad': comp.carrier_phase_rad,
                                'detection_method': 'clean',
                                'sec_in_minute': tick.sec_in_minute,
                                'processed_at': now_iso,
                                'processing_version': "2.0.0",
                            }
                            try:
                                self.all_arrivals_writer.write_measurement(clean_rec)
                                n_clean_multipath += 1
                            except Exception as clean_err:
                                logger.debug(f"Failed to write CLEAN record: {clean_err}")
                if n_edge_ticks > 0:
                    logger.info(f"All-arrivals: {n_edge_ticks} edge tick(s) written "
                               f"for Doppler-Delay analysis")
                if n_clean_multipath > 0:
                    logger.info(f"All-arrivals: {n_clean_multipath} CLEAN multipath "
                               f"component(s) written")

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
        if self._ring_reader is not None:
            try:
                self._ring_reader.close()
            except Exception as _e:
                logger.debug(f"Ring reader close: {_e}")
            self._ring_reader = None
        for _writer_attr in (
            'writer', 'fsk_writer', 'test_signal_writer',
            'tick_writer', 'attempts_writer', 'tick_phase_writer',
            'all_arrivals_writer',
        ):
            _w = getattr(self, _writer_attr, None)
            if _w is not None:
                try:
                    _w.close()
                except Exception as _e:
                    logger.warning(f"Error closing {_writer_attr}: {_e}")
        if self._iono_service is not None:
            try:
                self._iono_service.stop()
            except Exception as e:
                logger.debug(f"Ignored exception: {e}")
                pass
    
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
            _status_tmp = self.status_file.with_suffix('.tmp')
            with open(_status_tmp, 'w') as f:
                json.dump(status, f, indent=2)
            _status_tmp.replace(self.status_file)
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
    
    parser = argparse.ArgumentParser(description="Metrology Service")

    # Required args
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
    parser.add_argument("--log-level", default="INFO", help="Logging level")
    parser.add_argument("--state-file", type=Path, help="Persistence state file (legacy; unused)")
    parser.add_argument("--config-file", type=Path, default=Path("/etc/hf-timestd/timestd-config.toml"),
                        help="Path to timestd-config.toml for timing authority settings")

    # Legacy args, accepted and ignored for backwards compatibility with
    # older systemd unit files that still pass them through.  Removed
    # from the template unit in Phase 2.
    parser.add_argument("--archive-dir", type=Path, default=None,
                        help="(deprecated) ignored — metrology now reads the ring buffer")
    parser.add_argument("--use-tiered-storage", action="store_true",
                        help="(deprecated) ignored — metrology no longer uses tiered storage")
    parser.add_argument("--poll-interval", type=float, default=10.0,
                        help="(deprecated) ignored — ring poll interval is internal")

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
    
    # Warn once if deprecated args are still being passed by an old
    # systemd unit file.  They are accepted for startup compatibility
    # but have no effect.
    if args.archive_dir is not None:
        logger.warning(
            "--archive-dir is deprecated and ignored; metrology reads "
            "the producer's ring buffer"
        )
    if args.use_tiered_storage:
        logger.warning(
            "--use-tiered-storage is deprecated and ignored"
        )

    # Config dict construction - merge TOML timing + metrology + storage sections.
    # The storage section drives backend selection in
    # hf_timestd.io.make_data_product_writer (Phase 1 of HDF5 → SQLite
    # migration). Without it here, even if [storage] write_sqlite=true is set
    # in the TOML the writer never opts into SQLite — config.get('storage', {})
    # would always be empty.
    config = {
        "sample_rate": 24000,
        "timing": toml_config.get("timing", {}),  # Pass timing section for authority mode
        "metrology": toml_config.get("metrology", {}),
        "storage": toml_config.get("storage", {}),
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
    
    # --- Resource Guardian: preflight check ---
    from hf_timestd.core.resource_guardian import ResourceGuardian
    config_path = str(args.config_file) if args.config_file else '/etc/hf-timestd/timestd-config.toml'
    guardian = ResourceGuardian.from_config(config_path)
    if not guardian.preflight_check():
        logger.critical("Resource preflight failed — exiting")
        sys.exit(1)

    # --- Exclusive output-dir lock: prevent two writers on same HDF5 files ---
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    lock_path = output_dir / '.metrology.lock'
    lock_fd = None
    try:
        lock_fd = open(lock_path, 'w')
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_fd.write(f'{os.getpid()}\n')
        lock_fd.flush()
    except OSError:
        logger.critical(
            f"Another metrology process already owns {output_dir} — "
            f"refusing to start (duplicate writer would corrupt HDF5 files)")
        sys.exit(1)

    try:
        service = MetrologyService(
            config=config,
            channel_name=args.channel_name,
            frequency_hz=args.frequency_hz,
            output_dir=args.output_dir,
            receiver_grid=args.grid_square,
            station_config=station_config,
        )
        service._resource_guardian = guardian
        service.run()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.fatal(f"Service startup failed: {e}", exc_info=True)
        sys.exit(1)
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                lock_fd.close()
                lock_path.unlink(missing_ok=True)
            except Exception:
                pass
