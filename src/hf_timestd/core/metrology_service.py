#!/usr/bin/env python3
"""
Metrology Service
=================
Real-time DSP and Timestamping Service.

Responsibility:
1. Ingest raw IQ data from the core-recorder's shared-memory ring buffer.
2. Run MetrologyEngine (Tone Detection, Channel Characterization).
3. Write L1 and L2 metrology data products (HDF5 + SQLite).
4. Compute the per-channel edge-ensemble timing residual
   (``d_clock_ms`` on L2 tick_timing) -- this IS a clock-offset
   measurement, but it is **per-channel** and **not** yet fused
   across stations and **not** wired to chrony.  Fusion (the
   `timestd-fusion` service) is what takes these per-channel
   residuals and produces the chrony SHM update.

(§3.4 Low: docstring line 11 used to say "Do NOT perform ... clock
offset calculation"; that was stale -- this service has computed
edge-ensemble d_clock_ms since TickEdgeDetector landed.  Reconciled
2026-05-20.)

Data path
---------
`timestd-core-recorder` publishes each batch of samples into a per-channel
SysV ring buffer (see :mod:`hf_timestd.core.ring_buffer`).  This service
attaches to the ring for its own channel and extracts 60-second windows
aligned to UTC minute boundaries.  There is no file I/O on the hot path.
The archive writer in the recorder handles long-term .bin.zst chunks
independently of metrology latency.
"""

import dataclasses
import fcntl
import logging
import math
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
from hf_timestd.io import make_data_product_writer
from hf_timestd.data_product_registry import DataProductRegistry
from hf_timestd.core.ring_buffer import (
    RingBufferError,
    RingBufferOverrunError,
)
from hf_timestd.core.ring_buffer_reader import RingBufferReader
from hf_timestd.core.counter_epoch_tracker import CounterEpochTracker
from hf_timestd.core.buffer_timing import resolve_buffer_timing
from hf_timestd.core.registration_acquirer import (
    NON_REGISTERING_STATIONS,
    ORIGIN_SIGMA_FLOOR_MS,
    MinuteArrival,
    Registration,
    RegistrationAcquirer,
    residuals_for_verify,
)
from hf_timestd.core.registration_store import (
    FUSE_OUTLIER_MS,
    RegistrationStore,
    fuse_registrations,
    fuse_registrations_with_members,
)

from hf_timestd.core.wwv_constants import (
    TEST_SIGNAL_MINUTES, station_for_test_minute,
)

logger = logging.getLogger(__name__)



def resync_minute_after(exc: Exception, head_utc: float, jump_min: int) -> int:
    """The next minute to process after the ring refused a window.

    An overwritten window: jump ``jump_min`` minutes back from the head and
    continue.  A window that predates the current RTP numbering
    (``RingBufferBeforeBaseError``): the ring names where readable history
    starts, so go to the first whole minute at or after it.  Two minutes back
    from the head could still sit before that base for a while after a radiod
    restart, and every such attempt would raise again.
    """
    first_valid = getattr(exc, "first_valid_utc", None)
    if first_valid is not None:
        return int(math.ceil(float(first_valid) / 60.0)) * 60
    return (int(head_utc) // 60) * 60 - int(jump_min) * 60


class MetrologyService:
    """
    Metrology Service: The "Instrument" layer.
    """
    
    # A tick measurement is only admissible if the channel can show it
    # actually received the station.  hf-timestd#24: WWV 20/25 MHz
    # produce a confident measurement every minute of the day (1435 of
    # 1440) at a constant ~9 dB SNR, including hours when those bands
    # are closed over a 1,100 km path -- and nothing downstream could
    # tell those minutes from real ones.
    #
    # Measured over ~8,780 minutes per channel on AC0G-B4, the other
    # per-minute statistics CANNOT separate the cases:
    #   * ensemble_n_edges is ~56.6 on every channel, signal or not;
    #   * overall_confidence is INVERTED (0.93 on the suspect channels
    #     against 0.56 on the well-received ones);
    #   * doppler uncertainty is ~0.0022 Hz everywhere.
    # SNR is the field that separates, because the bands really do open:
    # 20 MHz reaches 37.4 dB and 25 MHz 33.3 dB when they work, against
    # a ~9 dB floor when they do not.
    #
    # The default is CHOSEN FROM COVERAGE DATA, not borrowed.  Over
    # 8,772 minutes on B4, requiring at least one admissible measurement
    # somewhere in the fleet:
    #
    #   floor    coverage    mean measurements/min
    #    0 dB     100.0%          15.09
    #   10 dB      94.6%           6.05
    #   12 dB      65.8%           5.24
    #   16 dB      62.0%           4.08
    #
    # 10 dB discards ~60% of measurements as noise while keeping 94.6%
    # of minutes covered.  12 dB (the bar already applied to BPM) buys
    # almost no further quality and costs a THIRD of all minutes -- and
    # a fusion input that goes silent for long stretches is the failure
    # this project spent 2026-08-17 recovering from (hf-timestd#16).
    #
    # ⚠ A single global floor is a compromise: the per-channel noise
    # floor spans ~7 dB (25 MHz sits at 9.4 dB mean, 10 MHz at 16.0), so
    # 10 dB still admits some noise on 20/25 MHz -- the very channels
    # that motivated this.  Per-channel floors, set from each channel's
    # own observed floor, are the real answer; this is configurable via
    # ``min_tick_snr_db`` in the meantime.
    DEFAULT_MIN_TICK_SNR_DB = 10.0

    @staticmethod
    def tick_measurement_admissible(n_edges, mean_snr_db, min_snr_db,
                                    anchor_source=None, sigma_single_ms=None):
        """(admissible, reason) for a per-minute tick measurement.

        Refusing is how a channel reports BLINDNESS: it publishes no
        d_clock rather than a confident noise-floor value.  A source
        that cannot say "I have nothing" cannot participate in a suite
        of mutually-supporting discriminators -- it contributes
        confident noise the other members must out-vote.

        Missing inputs refuse rather than assume good.
        """
        if n_edges is None or int(n_edges) < 5:
            return False, "too few edges (%s)" % (n_edges,)
        if mean_snr_db is None:
            return False, "no SNR reported"
        if float(mean_snr_db) < float(min_snr_db):
            return False, ("SNR %.1f dB below the %.1f dB floor — treating "
                           "as BLIND, not as a measurement"
                           % (float(mean_snr_db), float(min_snr_db)))
        # Step 0.5(b): an ensemble found where the host clock said to look
        # must look like ticks, not like the search window, before its
        # d_clock may be published (HOST_CLOCK_INTEGRITY.md).
        if anchor_source is not None and anchor_source != 'minute_marker':
            from hf_timestd.core.tick_edge_detector import TickEdgeDetector
            if sigma_single_ms is None:
                return False, "host-label anchor with no per-tick scatter reported"
            if float(sigma_single_ms) > TickEdgeDetector.LABEL_ANCHOR_MAX_SIGMA_MS:
                return False, ("host-label anchor, per-tick σ %.1f ms > %.0f ms — "
                               "the search window, not the ticks"
                               % (float(sigma_single_ms),
                                  TickEdgeDetector.LABEL_ANCHOR_MAX_SIGMA_MS))
        return True, "ok"

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

        # M-M19: rate-limited per-product warning timestamps for data-
        # product write failures.  The contract requires WARNING (not
        # DEBUG) for these; the rate-limit keeps a stuck backend from
        # spamming the journal at 50+ records/min/channel.
        self._last_write_warn_ts: Dict[str, float] = {}
        self._WRITE_WARN_INTERVAL_SEC = 60.0
        
        # RTP Offset Learning
        self._rtp_to_unix_offset = None
        self._offset_samples = []
        
        # The engine registers each buffer against radiod's start_system_time
        # (the GPS_TIME / RTP_TIMESNAP pair); the Offset Judge supplies the
        # correction.  The `[timing] authority` switch that once selected a
        # FUSION alternative retired 2026-09-04 (RESIDUE_AUDIT §3.4-3.5).
        
        # Initialize Engine
        # Extract precise coords if available
        lat = self.station_config.get('latitude')
        lon = self.station_config.get('longitude')
        
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
            enable_physics_products=self._physics_products,
            bcd_leap_notice=bool(_metrology_cfg.get('bcd_leap_notice', True)),
        )

        # T3 self-registration (spec 2026-09-06): the ticks place the second.
        self.acquirer = RegistrationAcquirer(self.channel_name, self.engine.sample_rate)
        self.reg_store = RegistrationStore()
        self.epoch_tracker = CounterEpochTracker()
        # Task 10: this minute's self-measurement of the held plane
        # (RegistrationAcquirer.measure_minute), kept for the shadow record
        # (Task 13).  {} when nothing was measured.
        self._last_arrivals: Dict[str, MinuteArrival] = {}
        # Task 10: inside an episode with no raw radiod pair in the ring
        # (one WARNING per episode).
        self._raw_pair_missing = False
        # Fix round 1 (I-2): the donor plane this channel's own ticks
        # condemned (see _refuse_donors); refused until it comes onto the
        # own-implied plane, the counter epoch changes, or 30 minutes pass.
        self._refused_donors = None
        self._refused_kept: List[str] = []
        self._minute_now: Optional[int] = None
        # Fix round 1: the involved set of the CONFLICT episode already
        # logged, so a persisting conflict logs one WARNING.
        self._conflict_logged = None
        # Fix round 3 (M1): the named siblings' planes in this episode.
        self._conflict_planes: Dict[str, Any] = {}

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

        # Test Signal Writer (for WWV/WWVH channels - minutes 8 and 44)
        # PHYSICS-OPTIONAL: ionospheric sounding product, not needed for Chrony.
        self.test_signal_writer = None
        if self._physics_products:
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
                # Pass the receiver location so GIRO polls the nearest
                # ionosondes (full GIRO weight is within ~555 km).
                self._iono_service = IonoDataService.get_instance(
                    home_lat=lat, home_lon=lon,
                )
                self._iono_service.start()
                logger.info("IonoDataService background fetcher started")
            except Exception as e:
                logger.warning(f"IonoDataService not available: {e}")

            # Near-real-time space weather (F10.7 / Kp / Ap) feeds the
            # parametric ionosphere path for the current day.
            try:
                from .space_weather import SpaceWeatherService
                self._space_weather = SpaceWeatherService.get_instance()
                self._space_weather.start()
                logger.info("SpaceWeatherService background fetcher started")
            except Exception as e:
                logger.warning(f"SpaceWeatherService not available: {e}")

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

    # Wall-clock sanity gate on the ring head UTC.  head_utc is the newest
    # buffered sample's timestamp, so it can never legitimately be more than
    # clock jitter AHEAD of now().  A head in the future means the ring's
    # RTP->UTC mapping is corrupt (radiod RTP-timing glitch / stepped
    # timesnap): observed 2026-07-20 when 3 of 6 channels ran ~22 min ahead
    # and silently emitted future-dated measurements (HamSCI/hf-timestd#5).
    # 120s is far beyond any real RTP-vs-OS jitter yet well under the
    # minutes-scale offsets the corruption produces.
    _RING_FUTURE_HEAD_SEC = 120.0

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
        last_future_warn = 0.0
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

                # Wall-clock sanity gate (HamSCI/hf-timestd#5): head_utc is
                # the newest buffered sample's UTC and can never be materially
                # ahead of now().  If it is, the ring's RTP->UTC mapping is
                # corrupt (future-dated); refuse to seed/advance the cursor
                # from it so we never emit future-timestamped measurements.
                # Loud but rate-limited; self-heals when the mapping recovers.
                now_wall = time.time()
                if head_utc > now_wall + self._RING_FUTURE_HEAD_SEC:
                    if now_mono - last_future_warn > 60.0:
                        logger.error(
                            f"[{self.channel_name}] ring head_utc={head_utc:.1f} "
                            f"is {head_utc - now_wall:.0f}s in the FUTURE vs "
                            f"wall-clock {now_wall:.1f} — RTP->UTC mapping "
                            f"corrupt; NOT processing future-dated minutes "
                            f"(INVESTIGATE radiod RTP timing / timesnap)"
                        )
                        last_future_warn = now_mono
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
                    new_next = resync_minute_after(
                        exc, head_utc=head_utc, jump_min=self._RING_OVERRUN_JUMP_MIN
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

                buffer_timing = self.apply_registration(
                    buffer_timing, samples, metadata.get("start_rtp_timestamp", 0),
                    next_minute, metadata)
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
                        # M-M20: prune the processed-minute set against
                        # ring-derived UTC, not the OS clock.
                        self._cleanup_processed_set(now_utc=head_utc)
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

    # Where AuthorityManager publishes the active timing tier (T_LEVELS_RANKED
    # in authority_manager.py, key "t_level_active").  The live ring path
    # never populates BufferTiming.judge_tier -- ring_buffer_reader's
    # extract_interval metadata carries no "timing" block, so
    # resolve_buffer_timing only ever fills judge_tier from a sidecar's
    # Offset Judge provenance (Task 8 fix round 1, C3: confirmed by reading
    # both files; the live path's tier is this file, not the BufferTiming).
    _AUTHORITY_JSON_PATH = Path("/run/hf-timestd/authority.json")

    def apply_registration(self, buffer_timing, iq_samples: np.ndarray, start_rtp: int,
                           minute_utc: int, metadata: Dict[str, Any]):
        """Replace the label ORIGIN with the acquired one (rate untouched).
        Returns the BufferTiming to hand the engine.

        Never raises: registration is an improvement to the plane, never a
        precondition for measuring it (review I1) -- any failure below
        logs and falls back to the plane this method was handed."""
        if buffer_timing is None or buffer_timing.source == 'no_timing':
            return buffer_timing
        try:
            return self._apply_registration_unsafe(
                buffer_timing, iq_samples, start_rtp, minute_utc, metadata)
        except Exception:
            logger.error(
                f"[{self.channel_name}] apply_registration failed for minute "
                f"{minute_utc}; metrology continues on the label plane",
                exc_info=True)
            return buffer_timing

    def _t6_authoritative(self, buffer_timing) -> bool:
        """Is T6 the active timing authority right now?

        Preferred: ``buffer_timing.judge_tier`` (populated from a sidecar's
        Offset Judge block -- Task 11's replay path, and any future direct
        caller).  The live ring path never sets it, so fall back to reading
        AuthorityManager's own publication."""
        tier = getattr(buffer_timing, "judge_tier", None)
        if tier is not None:
            return tier == "T6"
        try:
            with open(self._AUTHORITY_JSON_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("t_level_active") == "T6"
        except Exception:
            return False

    def _raw_label(self, metadata: Dict[str, Any], start_rtp: int):
        """The label radiod's own (GPS_TIME, RTP_TIMESNAP) pair gives this
        minute's first sample, or None when the ring carries no raw pair.

        Task 10 ("T3 stands alone"): T3 starts here on every station, never
        from the ring anchor.  The ring anchor carries whatever plane
        governs -- T6's correction on a T6 station, T3's own adopted plane
        on a T3-only one -- and starting from it would make T3 check a
        higher tier, or itself.  The raw pair names the whole second when
        no minute marker decides it; the fraction comes from the ticks."""
        raw_gps = metadata.get("raw_gps_time_ns")
        raw_rtp = metadata.get("raw_rtp_timesnap")
        if raw_gps is None or raw_rtp is None:
            return None
        bt = resolve_buffer_timing(
            {
                **metadata,
                "start_rtp_timestamp": int(start_rtp),
                "gps_time_ns": int(raw_gps),
                "rtp_timesnap": int(raw_rtp),
                # No Offset Judge correction: the raw pair as radiod sent it.
                "timing": None,
                # The writer's start_system_time follows the ring anchor,
                # not this pair; comparing the two would only warn.
                "start_system_time": 0,
            },
            sample_rate=self.engine.sample_rate,
        )
        return None if bt.source == "no_timing" else bt

    def _wait_for_raw_pair(self, buffer_timing, t6_authoritative: bool):
        """No raw pair this minute (an old producer, or one that never set
        it): T3 has nothing to start from, so it waits in BOOTSTRAP and
        says why.  One WARNING per episode."""
        if not getattr(self, "_raw_pair_missing", False):
            logger.warning(
                f"[{self.channel_name}] ring carries no raw radiod pair "
                f"(raw_gps_time_ns / raw_rtp_timesnap); T3 waits in "
                f"BOOTSTRAP until the recorder publishes one"
            )
            self._raw_pair_missing = True
        self._publish_registration(
            None, [], None, None, self.epoch_tracker.epoch_id,
            state_override="BOOTSTRAP",
            standalone_reason="raw_pair_unavailable",
        )
        if t6_authoritative:
            return buffer_timing
        return dataclasses.replace(buffer_timing, origin_source="label")

    def _fuse_minute(self, own, convertible, resolved_via_sibling_this_minute: bool,
                     start_rtp: int, at_map):
        """Fuse this channel's plane with its convertible siblings, and
        adopt a sibling-only plane in place of an untrusted own one.
        Returns ``(own, own_is_adopted, fused, kept, waiting, refused)``;
        ``refused`` says the siblings' plane was set aside because this
        channel's own ticks condemned it (fix round 1, I-2).

        Called twice a minute: before the self-measurement (so a sibling
        plane is adopted before the held plane is measured) and after it
        (so the fused plane carries what verify or corroborate just did
        to the held one)."""
        # An ADOPTED `own` is a derived ECHO of a prior sibling fusion, not
        # independent evidence -- folding it back into fuse_registrations
        # alongside the very siblings it was copied from understates sigma
        # by sqrt(n+1) every minute after the first (review C1).  A
        # genuinely self-acquired `own` (method != "adopted") IS independent
        # and belongs in the fusion as before.
        own_is_adopted = own is not None and getattr(own, "method", None) == "adopted"
        # task-11b fix round 1 (I1): an UNVERIFIED own (CANDIDATE) plane is
        # not independent evidence either.  Folding it into
        # fuse_registrations alongside real (hence already-verified, per
        # the CANDIDATE invariant) siblings let a phantom knock a good
        # sibling plane straight to CONFLICT (review, C1/❌1).  Exclude it
        # -- but NOT when it was resolved via THIS minute's sibling already
        # (I3), and ONLY when there are siblings to fuse instead: with none,
        # excluding it would make `fused` None, the plane would never be
        # applied, and the candidate could never be verified (deadlock).
        own_unverified_with_siblings = (
            own is not None
            and not own_is_adopted
            and not own.verified
            and not resolved_via_sibling_this_minute
            and bool(convertible)
        )
        if own_is_adopted or own_unverified_with_siblings:
            fusion_inputs = convertible
        else:
            fusion_inputs = ([own] if own else []) + convertible
        # C1: `kept` is what fusion ACTUALLY combined.  task 16c: `waiting`
        # names the members the corroboration floor held back.
        fused, kept, waiting = fuse_registrations_with_members(
            fusion_inputs, at_rtp=int(start_rtp), at_map=at_map,
            domain=self.channel_name,
        )
        # Fix round 1 (I-2): a sibling-only plane whose donors this
        # channel's own ticks already condemned is neither adopted nor
        # applied again until the donors' planes move.  Fix round 3 (I2):
        # only while this channel has no verified plane of its own.  Once
        # its own ticks verify a plane, ordinary fusion and the CONFLICT
        # rules govern ("T3 stands alone": verified own evidence outranks
        # a remembered refusal).
        refused = (
            fused is not None
            and self.channel_name not in kept
            and (own is None or not own.verified)
            and self._donors_refused(fused, kept, int(start_rtp))
        )
        if refused:
            self._refused_kept = list(kept)
            fused, kept, waiting = None, [], []
        elif (fused is not None and len(kept) > 1 and self.channel_name in kept
                and getattr(self, "_refused_donors", None) is not None):
            # This channel's own plane now fuses WITH siblings (fusion
            # drops a member more than FUSE_OUTLIER_MS out), so the
            # disagreement is over: the same test lifts the refusal.
            self._donors_refused(fused, kept, int(start_rtp))
        if fused is not None and (own is None or own_unverified_with_siblings):
            # `fused` here is built purely from siblings.  read_siblings
            # returns ACQUIRED and WITNESS files only, and only a VERIFIED
            # registration is ever published in either state (task-11b;
            # Task 10: verified by its own channel's ticks).  So a plane
            # fused entirely from them is itself verified; it is stamped
            # here rather than inside fuse_registrations.  The unverified
            # candidate (if any) is dropped in favour of it.
            self.acquirer.adopt(dataclasses.replace(fused, verified=True))
            own = self.acquirer.registration
            own_is_adopted = True
        return own, own_is_adopted, fused, kept, waiting, refused

    # Fix round 2 (MINOR 3): a refused donor plane is let back in only
    # once it sits within CORRECT_K_SIGMA x ORIGIN_SIGMA_FLOOR_MS (3 ms) of
    # the plane this channel's own residuals implied when they condemned
    # it -- the threshold corroborate itself condemns on.  A move of a
    # millisecond or so (a young donor still converging) cures nothing;
    # it would only buy one more condemnation.
    DONOR_REFUSAL_LIFT_MS = (
        RegistrationAcquirer.CORRECT_K_SIGMA * ORIGIN_SIGMA_FLOOR_MS)
    # ... and a refusal older than this lifts anyway, as a backstop.
    DONOR_REFUSAL_MAX_S = 30 * 60

    def _refuse_donors(self, kept, fused, epoch, minute_utc: int) -> None:
        """Record the plane this channel's own ticks just condemned, and
        the plane its own residuals implied instead: the condemned plane
        less the median residual (a positive residual means the plane
        labels late).  Without a residual only the backstop lifts it."""
        res = sorted(a.residual_ms for st, a in self._last_arrivals.items()
                     if st not in NON_REGISTERING_STATIONS)
        residual_s = None if not res else res[len(res) // 2] / 1000.0
        self._refused_donors = {
            "names": frozenset(kept), "plane": fused, "residual_s": residual_s,
            "epoch": epoch, "minute": int(minute_utc),
        }

    def _renew_refusal(self, start_rtp: int) -> None:
        """Fix round 3 (I1): restart the backstop's clock when this
        channel's OWN evidence still stands against the refused plane --
        its held (not adopted) plane, corrected by this minute's own
        residuals, or a verified plane when nothing was measured, sits
        more than DONOR_REFUSAL_LIFT_MS from it.  The backstop then means
        "30 minutes without own evidence".  A CONFLICT publish alone
        renews nothing: the refused path publishes one every minute, and
        the backstop would never fire."""
        r = getattr(self, "_refused_donors", None)
        reg = self.acquirer.registration
        if r is None or reg is None or getattr(reg, "method", None) == "adopted":
            return
        res = sorted(a.residual_ms for st, a in self._last_arrivals.items()
                     if st not in NON_REGISTERING_STATIONS)
        own_s0 = reg.sample0_utc_for(start_rtp)
        if res:
            own_s0 -= res[len(res) // 2] / 1000.0
        elif not reg.verified:
            return
        off_ms = abs(own_s0 - r["plane"].sample0_utc_for(start_rtp)) * 1000.0
        if off_ms > self.DONOR_REFUSAL_LIFT_MS and self._minute_now is not None:
            r["minute"] = int(self._minute_now)

    def _donors_refused(self, fused, kept, start_rtp: int) -> bool:
        """Is ``fused`` (a plane from siblings alone) still the plane this
        channel's own ticks condemned?  Which siblings carry it does not
        matter: the plane does.  Lifts, with one INFO line, when the
        plane has come within DONOR_REFUSAL_LIFT_MS of the own-implied
        plane, or when the refusal is DONOR_REFUSAL_MAX_S old."""
        r = getattr(self, "_refused_donors", None)
        if r is None:
            return False
        now = getattr(self, "_minute_now", None)
        if now is not None and now - r["minute"] >= self.DONOR_REFUSAL_MAX_S:
            logger.info(
                f"[{self.channel_name}] refusal of {sorted(r['names'])}'s plane "
                f"lifted after 30 minutes without this channel's own evidence "
                f"against it; adoption allowed again")
            self._refused_donors = None
            return False
        if r["residual_s"] is None:
            return True
        implied = r["plane"].sample0_utc_for(start_rtp) - r["residual_s"]
        off_ms = abs(fused.sample0_utc_for(start_rtp) - implied) * 1000.0
        if off_ms > self.DONOR_REFUSAL_LIFT_MS:
            return True
        logger.info(
            f"[{self.channel_name}] the siblings' plane ({sorted(kept)}) now sits "
            f"{off_ms:.1f} ms from the plane this channel's own ticks implied; "
            f"adoption allowed again")
        self._refused_donors = None
        return False

    def _publish_conflict(self, others, label_s0, epoch, message: str,
                          extra_extra: Optional[Dict[str, Any]],
                          planes: Optional[Dict[str, Any]] = None,
                          start_rtp: Optional[int] = None) -> None:
        """Publish a per-channel CONFLICT naming ``others`` beside this
        channel, with one WARNING per episode.

        Fix round 2 (MINOR 2): the episode is the CONDITION -- this
        channel in conflict, minute after minute -- not the member list.
        The paths name different sets (a refusal names the donors fusion
        kept; the own-vs-siblings CONFLICT names every convertible
        sibling), and a donor clearing the corroboration floor changes
        the set mid-episode; keying on the set logged a second WARNING
        for one disagreement.  A changed set logs at INFO.  The episode
        ends at the first publish that names no conflict.

        Fix round 3 (M1): ``planes`` maps the named siblings to their
        planes in this channel's counter.  A member joining mid-episode
        more than FUSE_OUTLIER_MS from every member already named is a
        new disagreement, not a re-labelling, and logs its own WARNING;
        a joiner on a plane already named stays at INFO."""
        involved = sorted({self.channel_name, *others})
        logged = getattr(self, "_conflict_logged", None)
        known = getattr(self, "_conflict_planes", None) or {}
        planes = planes or {}
        if logged is None:
            logger.warning(message)
        elif logged != tuple(involved):
            far = {}
            if start_rtp is not None and known:
                for n in involved:
                    if n in logged or n not in planes:
                        continue
                    s0 = planes[n].sample0_utc_for(start_rtp)
                    nearest = min(abs(s0 - k.sample0_utc_for(start_rtp)) * 1000.0
                                  for k in known.values())
                    if nearest > FUSE_OUTLIER_MS:
                        far[n] = nearest
            if far:
                logger.warning(
                    f"[{self.channel_name}] the conflict gains a new plane: "
                    + ", ".join(f"{n} sits {d:.1f} ms from every channel already named"
                                for n, d in sorted(far.items()))
                    + f"; now {involved}")
            else:
                logger.info(
                    f"[{self.channel_name}] the conflict now names {involved} "
                    f"(was {list(logged)})")
        self._conflict_logged = tuple(involved)
        self._conflict_planes = {**known, **planes}
        # I6: CONFLICT is a PER-CHANNEL verdict.  It belongs in this
        # channel's file; the station summary carries the channels
        # involved under `conflicts` and keeps a state derived from the
        # fused result, so one channel's disagreement no longer
        # overwrites the station's state for every reader.
        self._publish_registration(None, [], label_s0, None, epoch,
                                   channel_state_override="CONFLICT",
                                   conflicts=involved, extra_extra=extra_extra)

    def _route_arrivals(self, arrivals) -> None:
        """Hand this minute's self-measurement to ``verify`` (an unverified
        plane) or ``corroborate`` (a verified one).  Either may reset the
        plane; the reset logs the event's one WARNING (Task 10, ruling 3),
        so nothing more is logged here.

        A minute that measured nothing ({}: the acquisition minute, no
        plane, or no peak) routes nothing, as an empty detector minute
        routed nothing before."""
        if not arrivals:
            return
        res = residuals_for_verify(arrivals)
        reg = self.acquirer.registration
        if reg is None:
            return
        if not reg.verified:
            self.acquirer.verify(res)
        else:
            self.acquirer.corroborate(res)

    def _apply_registration_unsafe(self, buffer_timing, iq_samples: np.ndarray,
                                   start_rtp: int, minute_utc: int,
                                   metadata: Dict[str, Any]):
        # Task 10: "T3 stands alone".  The same code runs on every station.
        # T3 starts from radiod's raw pair, verifies and corroborates on
        # this channel's own fold and marker, and treats T6, where T6
        # governs, as a recorded diagnostic only.
        self._last_arrivals = {}
        t6_authoritative = self._t6_authoritative(buffer_timing)
        raw = self._raw_label(metadata, int(start_rtp))
        if raw is None:
            return self._wait_for_raw_pair(buffer_timing, t6_authoritative)
        if getattr(self, "_raw_pair_missing", False):
            logger.info(f"[{self.channel_name}] raw radiod pair present again; T3 resumes")
            self._raw_pair_missing = False
        epoch = self.epoch_tracker.observe(
            metadata.get("raw_gps_time_ns"), metadata.get("raw_rtp_timesnap"),
            metadata.get("sample_rate", self.engine.sample_rate))
        self._minute_now = int(minute_utc)
        refusal = getattr(self, "_refused_donors", None)
        if refusal is not None and refusal["epoch"] != epoch:
            # Fix round 2 (MINOR 3): the refused plane was measured in
            # another counter; nothing it says carries over.
            logger.info(
                f"[{self.channel_name}] counter epoch {refusal['epoch']} -> "
                f"{epoch}: refusal of {sorted(refusal['names'])}'s plane cleared")
            self._refused_donors = None
        label_s0 = float(raw.sample0_utc)
        audio = self.engine.prepare_audio(iq_samples)
        delays = self.engine.expected_delays_s(label_s0, int(minute_utc))
        # C1: hand the acquirer the epoch as a MEASURED offset as well as a
        # name.  The name is this process's own; the offset is what the
        # other channels' processes agree on.
        epoch_offset_s = self.epoch_tracker.epoch_offset_s
        # Task 7: since ka9q-radio e5f790dd each channel starts its own RTP
        # counter, so start_rtp means nothing to a sibling without this
        # channel's map to radiod's input-sample index.  The acquirer
        # stamps the map on every plane it holds or makes, and resets a
        # plane whose counter no longer continues (a map step), before it
        # is offered this minute.  `at_map` names start_rtp's counter for
        # every fusion call below; None (an old producer, a withdrawn map,
        # a map out of reach) leaves fusion to this channel's own planes.
        self.acquirer.note_counter_map(metadata.get("counter_map"))
        at_map = self.acquirer.map_at(int(start_rtp))
        own = self.acquirer.offer_minute(audio, raw, int(start_rtp), int(minute_utc),
                                         delays, epoch, epoch_offset_s=epoch_offset_s)
        # Task 10 (deferred from Task 6): WITNESS too, so the channels of a
        # T6 station, which publish WITNESS for a verified plane, see each
        # other as siblings.
        sibs = self.reg_store.read_siblings(
            exclude_channel=self.channel_name, states=("ACQUIRED", "WITNESS"))
        # Task 7 fix round 1 (I-3): a sibling that cannot convert into this
        # channel's counter (no map, another radiod run, no map of our own)
        # is no sibling at all.  Every rule below that asks "are there
        # siblings?" -- the deadlock guard, the orphaned-adoption reset and
        # CONFLICT -- asks it of these, never of the raw files.
        convertible = [
            r for r in sibs
            if r.in_domain(int(start_rtp), at_map, self.channel_name) is not None
        ]
        # Fix round 3 (M1): each convertible sibling's plane in this
        # channel's counter, for naming what a CONFLICT is about.
        sib_planes = {
            r.channel: r.in_domain(int(start_rtp), at_map, self.channel_name)
            for r in convertible
        }
        # I3 (task-11 review): resolve-before-adopt precedence -- a plane
        # `resolve_ambiguity` just named THIS minute already used the
        # sibling to pick the station and supplied the whole second; it is
        # independent, this-channel evidence, not a candidate to be dropped
        # in favour of that same sibling below.
        resolved_via_sibling_this_minute = False
        if own is None and convertible:
            # a shared channel's open hypotheses: let the siblings' plane
            # name the station.  Task 7: every sibling converts into this
            # channel's counter through both counter maps.
            sib_plane = fuse_registrations(convertible, at_rtp=int(start_rtp),
                                           at_map=at_map, domain=self.channel_name)
            # Fix round 2: a plane this channel's ticks condemned names
            # no hypothesis either.
            if sib_plane is not None and not self._donors_refused(
                    sib_plane, [], int(start_rtp)):
                own = self.acquirer.resolve_ambiguity(sib_plane, int(start_rtp), label_s0,
                                                      epoch_offset_s=epoch_offset_s)
                resolved_via_sibling_this_minute = own is not None
        own, own_is_adopted, fused, kept, waiting, refused = self._fuse_minute(
            own, convertible, resolved_via_sibling_this_minute, int(start_rtp), at_map)

        # Task 10: measure the held plane on this channel's own fold and
        # marker, every minute (the acquisition minute included: that call
        # keeps the audio tail the next minute's marker needs), and let the
        # measurement verify or corroborate it.  Kept for Task 13.
        held = self.acquirer.registration
        self._last_arrivals = self.acquirer.measure_minute(
            audio, int(start_rtp), int(minute_utc), delays)
        self._route_arrivals(self._last_arrivals)
        self._renew_refusal(int(start_rtp))

        # Fix round 1 (I-1): every station runs the same orphan, CONFLICT
        # and stand-down logic below; T6 changes only which plane the
        # engine gets back and how a surviving plane is named.
        t6_extra = ({"witness_of": "T6", "residual_vs_t6_ms": None}
                    if t6_authoritative else None)
        if t6_authoritative:
            fallback = dataclasses.replace(buffer_timing, counter_epoch_id=epoch)
        else:
            fallback = dataclasses.replace(buffer_timing, origin_source="label",
                                           counter_epoch_id=epoch)

        if self.acquirer.state != self.acquirer.STATE_ACQUIRED:
            if (held is not None and getattr(held, "method", None) == "adopted"
                    and fused is not None and self.channel_name not in kept):
                # Fix round 1 (I-2): this channel's own ticks condemned the
                # plane it adopted.  That is a disagreement with its
                # donors, not a bootstrap: name them, once per episode,
                # and refuse the same plane until it moves, or the next
                # minute re-adopts it and the cycle repeats.
                self._refuse_donors(kept, fused, epoch, minute_utc)
                own_ms = ", ".join(
                    f"{st} {a.residual_ms:+.1f} ms"
                    for st, a in sorted(self._last_arrivals.items()))
                self._publish_conflict(
                    kept, label_s0, epoch,
                    f"[{self.channel_name}] this channel's own ticks disagree "
                    f"with the plane adopted from {sorted(kept)} ({own_ms}); "
                    f"not adopting it again until the donors' plane moves",
                    t6_extra, planes={n: sib_planes[n] for n in kept if n in sib_planes},
                    start_rtp=int(start_rtp))
                return fallback
            if refused:
                # Fix round 2 (IMPORTANT 1): no own plane this minute (a
                # slow re-acquisition, a candidate verify rejected), but the
                # refusal holds and its donors are still on offer.  The
                # conflict goes on; a plain BOOTSTRAP here ended the
                # episode and the next refused minute logged a second
                # WARNING.
                self._publish_conflict(
                    self._refused_kept, label_s0, epoch,
                    f"[{self.channel_name}] still refusing the plane of "
                    f"{sorted(self._refused_kept)}", t6_extra,
                    planes={n: sib_planes[n] for n in self._refused_kept
                            if n in sib_planes},
                    start_rtp=int(start_rtp))
                return fallback
            # task 16b: the measurement just gave the plane up.  Stand
            # every surface down in this minute's publish rather than
            # label from an abandoned plane.
            self._publish_registration(None, [], label_s0, None, epoch,
                                       extra_extra=t6_extra)
            return fallback
        if own is not None:
            # Re-fuse on the plane as the measurement left it.  A
            # self-acquired plane enters fusion as verify or corroborate
            # left it.  An ADOPTED plane never enters fusion (the echo
            # rule in _fuse_minute), so whatever corroborate did to the
            # adopted copy does not reach the published plane, which
            # follows the donors; for an adopted plane the measurement's
            # job is to condemn it (I-2), not to steer it.  No adoption
            # follows here: a sibling-only plane was adopted in the first
            # pass.
            own, own_is_adopted, fused, kept, waiting, refused = self._fuse_minute(
                self.acquirer.registration, convertible,
                resolved_via_sibling_this_minute, int(start_rtp), at_map)

        if refused:
            # The donors this channel's ticks condemned are still the only
            # plane on offer: the conflict goes on (no new WARNING).
            self._publish_conflict(
                self._refused_kept, label_s0, epoch,
                f"[{self.channel_name}] still refusing the plane of "
                f"{sorted(self._refused_kept)}", t6_extra,
                planes={n: sib_planes[n] for n in self._refused_kept
                        if n in sib_planes},
                start_rtp=int(start_rtp))
            return fallback

        # I2: this channel already has its own ACQUIRED plane, but nothing
        # in the sibling fusion agrees with it within FUSE_OUTLIER_MS of the
        # combined median -- a real disagreement between channels sharing
        # one origin, not a bootstrap.  Surface it; do not silently revert
        # with contradictory provenance (channel file ACQUIRED, summary
        # BOOTSTRAP) as before.
        if own is not None and fused is None:
            if own_is_adopted and not convertible:
                # The donor's evidence is gone (its file expired past
                # RegistrationStore.stale_s, simply vanished, or no longer
                # converts into this counter -- Task 7 fix round 1) -- an
                # adopted plane with nothing left to corroborate it is an
                # ORPHAN, not a disagreement between channels.  CONFLICT
                # would misreport "channels disagree" when nothing does,
                # and would never recover on its own (ACQUIRED short-
                # circuits offer_minute).  Reset to BOOTSTRAP instead so
                # this channel tries its own signal, or re-adopts a fresh
                # sibling, next minute (review, fix round 2).
                self.acquirer.reset("adopted plane's donor expired")
                self._publish_registration(None, [], label_s0, None, epoch,
                                           extra_extra=t6_extra)
                return fallback
            elif convertible:
                # A genuine disagreement between channels on one radiod
                # run -- CONFLICT requires siblings to disagree WITH.
                # Task 7: each sibling's delta is read in THIS channel's
                # counter (through both maps); at raw RTP a sibling on
                # another start block reads 20 ms per block off.  A
                # sibling that cannot convert took no part in fusion, so
                # it is named as such and not among the channels involved.
                own_s0 = own.sample0_utc_for(int(start_rtp))
                parts = []
                involved_sibs = []
                for r in sibs:
                    c = r.in_domain(int(start_rtp), at_map, self.channel_name)
                    if c is None:
                        parts.append(f"{r.channel}=not in this counter")
                        continue
                    involved_sibs.append(r.channel)
                    parts.append(
                        f"{r.channel}={(c.sample0_utc_for(int(start_rtp)) - own_s0) * 1000.0:+.1f}ms")
                deltas = ", ".join(parts)
                plane = "T6's" if t6_authoritative else "the label"
                self._publish_conflict(
                    involved_sibs, label_s0, epoch,
                    f"[{self.channel_name}] sibling registrations disagree with this "
                    f"channel's own plane ({deltas}); handing back {plane} plane "
                    f"while they do",
                    t6_extra,
                    planes={n: sib_planes[n] for n in involved_sibs if n in sib_planes},
                    start_rtp=int(start_rtp))
                return fallback
            # else: a genuinely self-acquired `own` with zero siblings and
            # `fused is None` is mathematically unreachable here --
            # fuse_registrations of a single entry is an identity and
            # never returns None -- so no case falls through unhandled;
            # continue to the generic BOOTSTRAP publish below.

        # C3: T6 wins.  The ring anchor already carries T6's correction
        # (offset_judge -> stream_recorder_v2 -> resolve_buffer_timing), so
        # replacing sample0_utc here would fight it, not defer to it.
        # Publish what the acquirer measured as a WITNESS, and hand the
        # engine T6's plane back untouched.  Task 10: T6's plane never
        # verifies the registration and never resets it; the residual
        # against it is recorded, nothing more.
        if t6_authoritative:
            if fused is None:
                residual_vs_t6_ms = raw_residual_ms = None
            else:
                fused_s0 = fused.sample0_utc_for(int(start_rtp))
                residual_vs_t6_ms = (fused_s0 - float(buffer_timing.sample0_utc)) * 1000.0
                raw_residual_ms = (fused_s0 - label_s0) * 1000.0
            current = self.acquirer.registration
            if fused is None:
                witness_state = "BOOTSTRAP"
            elif current is not None and current.verified:
                witness_state = "WITNESS"
            else:
                witness_state = "CANDIDATE"
            self._publish_registration(
                fused, [] if fused is None else list(kept), label_s0, raw_residual_ms,
                epoch, state_override=witness_state,
                waiting=[] if fused is None else list(waiting),
                extra_extra={
                    "witness_of": "T6",
                    "residual_vs_t6_ms": (
                        None if residual_vs_t6_ms is None else round(residual_vs_t6_ms, 3)
                    ),
                },
            )
            return fallback

        if fused is None:
            self._publish_registration(None, [], label_s0, None, epoch)
            return dataclasses.replace(buffer_timing, origin_source="label",
                                       counter_epoch_id=epoch)
        s0 = fused.sample0_utc_for(int(start_rtp))
        residual_ms = (s0 - label_s0) * 1000.0
        contributing = list(kept)
        self._publish_registration(fused, contributing, label_s0, residual_ms, epoch,
                                   waiting=list(waiting))
        # I4: the label carried T6's (or whatever incumbent's) provenance;
        # once the origin is replaced the provenance must move with it --
        # one measurand, one ruler, one registration.
        return dataclasses.replace(buffer_timing, sample0_utc=s0, origin_source="acquired",
                                   origin_sigma_ms=fused.sigma_ms, counter_epoch_id=epoch,
                                   judge_tier="T3", offset_sigma_ns=fused.sigma_ms * 1e6)

    def _publish_registration(self, fused, contributing, label_s0, residual_ms, epoch,
                              *, state_override: Optional[str] = None,
                              channel_state_override: Optional[str] = None,
                              conflicts: Optional[List[str]] = None,
                              waiting: Optional[List[str]] = None,
                              extra_extra: Optional[Dict[str, Any]] = None,
                              standalone_reason: Optional[str] = None):
        """``state_override`` names BOTH the channel file's state and the
        summary's -- use it only for a state the FUSED result justifies
        (WITNESS / CANDIDATE / BOOTSTRAP on the T6 path, BOOTSTRAP without
        a raw pair).  ``channel_state_override`` names a PER-CHANNEL state
        that must not reach the summary (CONFLICT); pass ``conflicts``
        alongside it so the summary can name the channels involved (final
        review, I6).  ``standalone_reason`` says why T3 cannot run this
        minute (Task 10: ``"raw_pair_unavailable"``); None when it runs.
        ``label_s0`` is the raw pair's label, None without one."""
        # NOTE: this "current" registration is the ACQUIRER's own state as of
        # right now (post adopt/acquire this minute) -- a distinct quantity
        # from the caller's "own" (this minute's fresh offer_minute /
        # resolve_ambiguity return), which is None exactly in the adopt case.
        # Conflating the two names previously masked that adopt rewrites
        # `self.acquirer.registration` out from under the caller's `own`
        # (review M5).
        current = self.acquirer.registration
        # task-11b: an ACQUIRED-state channel file is a CANDIDATE until this
        # channel's own registration is verified on its own ticks
        # (RegistrationAcquirer.verify, fed by measure_minute since Task 10)
        # -- publishing it as ACQUIRED before
        # that let a single-station channel self-register on a fold-lattice
        # phantom and offer it to siblings as trustworthy evidence.
        if channel_state_override is not None:
            state = channel_state_override
        elif state_override is not None:
            state = state_override
        elif self.acquirer.state == self.acquirer.STATE_ACQUIRED:
            state = "ACQUIRED" if (current is not None and current.verified) else "CANDIDATE"
        else:
            state = self.acquirer.state
        if current is not None:
            self.reg_store.write_channel(current, state, {
                "label_sample0_utc": label_s0,
                "correction_ms": None if residual_ms is None else round(residual_ms, 3)})
        else:
            self.reg_store.write_channel(
                Registration(counter_epoch_id=epoch, rtp_ref=0, utc_ref=0.0,
                             sample_rate=self.engine.sample_rate, sigma_ms=float("inf"),
                             channel=self.channel_name), state, {"label_sample0_utc": label_s0})
        # The summary is the network-wide view: CANDIDATE only when the
        # SOLE contributor is this channel's own not-yet-verified plane --
        # real corroboration from an ACQUIRED sibling (which is, by the
        # same invariant, itself verified) is trusted immediately, whether
        # or not this channel has independently verified its own signal.
        only_own_unverified = (
            fused is not None
            and contributing == [self.channel_name]
            and current is not None
            and not current.verified
        )
        summary_state = (
            state_override if state_override is not None
            else ("CANDIDATE" if only_own_unverified
                  else ("ACQUIRED" if fused is not None else "BOOTSTRAP"))
        )
        extra = {"raw_pair_residual_ms": None if residual_ms is None else round(residual_ms, 3),
                 "counter_epoch_id": epoch,
                 "minutes_since_acquisition": 0 if fused is None else fused.n_minutes,
                 # I6: always present, so a reader can rely on the key.
                 "conflicts": sorted(set(conflicts)) if conflicts else [],
                 # task 16c: the channels fusion held back on the
                 # corroboration floor -- always present, empty when
                 # nobody waits.
                 "waiting": sorted(set(waiting)) if waiting else [],
                 # Task 10: always present; None while T3 runs.
                 "standalone_reason": standalone_reason}
        if extra_extra:
            extra.update(extra_extra)
        if not conflicts:
            # Fix round 1: a publish naming no conflict ends the episode.
            self._conflict_logged = None
            self._conflict_planes = {}
        self.reg_store.write_summary(fused, sorted(set(contributing)), summary_state, extra)

    def feed_back_ensembles(self, results) -> None:
        """Retired as a route to the registration (Task 10, "T3 stands
        alone").  Kept, with its call site in ``_process_minute_data``, so
        the seam stays visible; it never touches the plane.

        The tick detector measures against whatever plane the engine was
        handed: T6's on a T6 station, so its ensembles would verify T3
        against a higher tier.  Its marker-anchored ensembles also report
        an unwrapped whole-second error without the acquirer's SNR and
        two-consecutive-minute bars.  The registration now verifies and
        corroborates only on its own fold and marker
        (``RegistrationAcquirer.measure_minute``, routed in
        ``_apply_registration_unsafe``)."""
        return None

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

            edge_results = getattr(self.engine, "edge_results_this_minute", None)
            if edge_results:
                self.feed_back_ensembles(edge_results)

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

                # Leap-second advance notice decoded from this station's
                # time code this minute (WWV BCD second 3 on dedicated WWV
                # channels; WWVB carries its own on the recorder side).
                notices = getattr(self.engine, '_last_leap_second_notice', None) or {}
                if rec.get('leap_second_notice') is None and rec.get('station_id') in notices:
                    rec['leap_second_notice'] = notices[rec['station_id']]
                
                # Schema expects 'processed_at', 'processing_version'
                rec['processed_at'] = datetime.now(timezone.utc).isoformat()
                rec['processing_version'] = "1.0.0"
                
                self.writer.write_measurement(rec)
                
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
                    
                    # hf-timestd#24: publish NOTHING rather than a
                    # confident noise-floor value.  Refusing here is how
                    # this channel reports blindness.
                    _min_snr = float(getattr(
                        self, 'min_tick_snr_db',
                        self.DEFAULT_MIN_TICK_SNR_DB))
                    _ok, _why = self.tick_measurement_admissible(
                        n_edges=edge_result.ensemble_n_edges,
                        mean_snr_db=edge_result.mean_edge_snr_db,
                        min_snr_db=_min_snr,
                        anchor_source=getattr(edge_result, 'anchor_source', None),
                        sigma_single_ms=getattr(edge_result, 'sigma_single_ms', None),
                    )
                    # Loud on TRANSITIONS only — this runs every minute
                    # and a blind channel would otherwise log forever.
                    _key = f"{self.channel_name}:{station_name}"
                    _seen = getattr(self, '_tick_admissible_last', None)
                    if _seen is None:
                        _seen = self._tick_admissible_last = {}
                    if _seen.get(_key) is not _ok:
                        _seen[_key] = _ok
                        if _ok:
                            logger.info(
                                f"{station_name} @ "
                                f"{self.frequency_hz/1e6:.1f}MHz: "
                                f"RECEIVING again "
                                f"({edge_result.mean_edge_snr_db:.1f}dB)")
                        else:
                            logger.warning(
                                f"{station_name} @ "
                                f"{self.frequency_hz/1e6:.1f}MHz: BLIND — "
                                f"{_why}; publishing no d_clock "
                                f"(hf-timestd#24)")
                    d_clock_ms = (
                        edge_result.ensemble_timing_error_ms if _ok else None)
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
                        'd_clock_source': 'edge_ensemble:' + str(
                            getattr(edge_result, 'anchor_source', 'host_label')),
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
                            self._warn_write_failure("tick_phase", ph_err)

            # Write detection attempts (every measurement attempt for threshold calibration).
            # M-M18: batched.  Each minute generates ~50+ attempt rows per
            # channel; per-record write_measurement() calls were the same
            # heap-corruption risk the data contract already calls out
            # for tick_phase.
            if self.attempts_writer and hasattr(self.engine, '_last_rtp_attempts'):
                rtp_attempts = self.engine._last_rtp_attempts
                if rtp_attempts:
                    now_iso = datetime.now(timezone.utc).isoformat()
                    attempt_batch = [
                        {
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
                            'processing_version': "1.0.0",
                        }
                        for attempt in rtp_attempts
                    ]
                    if attempt_batch:
                        try:
                            self.attempts_writer.write_measurements_batch(attempt_batch)
                        except Exception as att_err:
                            self._warn_write_failure("detection_attempts", att_err)

                    n_det = sum(1 for a in rtp_attempts if a.get('detected'))
                    logger.debug(f"Detection attempts written: {len(rtp_attempts)} total, "
                                f"{n_det} detected, {len(rtp_attempts) - n_det} rejected")
            
            # Write all-arrivals (multi-path physics product).
            # For each detected attempt that has secondary correlation
            # peaks, build one row per arrival path; batch them under
            # M-M18 to avoid the per-record write pattern flagged in
            # the data contract for tick_phase.  This is purely additive —
            # the metrology pipeline is unaffected.
            if self.all_arrivals_writer and hasattr(self.engine, '_last_rtp_attempts'):
                rtp_attempts = self.engine._last_rtp_attempts
                if rtp_attempts:
                    now_iso = datetime.now(timezone.utc).isoformat()
                    freq_mhz = self.frequency_hz / 1e6
                    arrival_batch: List[Dict[str, Any]] = []
                    n_multipath = 0
                    for attempt in rtp_attempts:
                        if not attempt.get('detected'):
                            continue
                        arrivals = attempt.get('all_arrivals', [])
                        if not arrivals:
                            continue
                        utc_sec = attempt.get('utc_second', 0)
                        station = attempt.get('station', '')
                        expected_ms = attempt.get('expected_delay_ms', 0.0)
                        for arr in arrivals:
                            arrival_batch.append({
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
                            })
                            if arr.get('peak_rank', 0) > 0:
                                n_multipath += 1
                    if arrival_batch:
                        try:
                            self.all_arrivals_writer.write_measurements_batch(arrival_batch)
                        except Exception as arr_err:
                            self._warn_write_failure("all_arrivals", arr_err)
                    if n_multipath > 0:
                        logger.info(f"All-arrivals: {n_multipath} secondary path(s) recorded")

            # Write per-tick edge detections to all_arrivals (Doppler-Delay product).
            # Each detected tick from the TickEdgeDetector becomes one row with
            # timing_error_ms and carrier_phase_rad.  This enables Doppler-Delay
            # scatter plots: phase slope across seconds = Doppler, timing_error =
            # propagation delay residual.  Multipath modes show as distinct
            # clusters in the (delay, phase) plane even when temporally unresolved.
            if self.all_arrivals_writer and edge_results:
                # M-M18: edge ticks + CLEAN multipath share one batch.
                # The old code emitted one HDF5 write per detected tick
                # *and* per CLEAN component — 50+ rows/min/channel — the
                # same per-record pattern the data contract calls out as
                # heap-corruption-risky for tick_phase.
                now_iso = datetime.now(timezone.utc).isoformat()
                freq_mhz = self.frequency_hz / 1e6
                edge_batch: List[Dict[str, Any]] = []
                n_edge_ticks = 0
                n_clean_multipath = 0
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
                    for tick in edge_result.edges:
                        if not tick.detected:
                            continue
                        edge_batch.append({
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
                        })
                        n_edge_ticks += 1

                        # Append CLEAN multipath arrivals (rank >= 1
                        # only; rank 0 is the edge_tick primary above).
                        for comp in tick.clean_arrivals:
                            if comp.peak_rank == 0:
                                continue
                            edge_batch.append({
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
                            })
                            n_clean_multipath += 1
                if edge_batch:
                    try:
                        self.all_arrivals_writer.write_measurements_batch(edge_batch)
                    except Exception as edge_err:
                        self._warn_write_failure("all_arrivals", edge_err)
                if n_edge_ticks > 0:
                    logger.info(f"All-arrivals: {n_edge_ticks} edge tick(s) written "
                               f"for Doppler-Delay analysis")
                if n_clean_multipath > 0:
                    logger.info(f"All-arrivals: {n_clean_multipath} CLEAN multipath "
                               f"component(s) written")

            # Write test signal for minutes 8 and 44 (WWV/WWVH channel sounding)
            minute_number = (minute_boundary // 60) % 60
            if (minute_number in TEST_SIGNAL_MINUTES
                    and self.test_signal_writer):
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
            'writer', 'test_signal_writer',
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
            
            # The station is a property of the SCHEDULE, not of whether we
            # heard anything: minute 8 is WWV, minute 48 is WWVH.
            station = station_for_test_minute(minute_number)
            
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
                # Recorded whichever way the detection went.  Blanking it
                # on non-detection made every MISSING record fail schema
                # validation and be discarded -- so the detector could only
                # ever record its successes, and a closed path was
                # indistinguishable from a healthy one.  `quality_flag`
                # already carries 'MISSING'; that is where absence belongs.
                'station': station,
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
        """Write the per-channel status.json summary.

        §3.4 Low: this used to dump every L1 measurement object via
        ``[r.model_dump(mode='json') for r in results]`` every minute
        -- a JSON status file should be small, quick to read, and free
        of high-cadence payload churn.  Now we summarise: count +
        first/last station + count of detected tones.  Full
        measurement records live in the HDF5/SQLite data products.
        """
        try:
            detected = [r for r in results if getattr(r, 'tone_detected', False)]
            stations = sorted({
                str(getattr(r, 'station_id', '?')) for r in results
            })
            status = {
                "service": "metrology",
                "last_update": datetime.now(timezone.utc).isoformat(),
                "channel": self.channel_name,
                "last_minute_processed": minute,
                "minutes_processed": self.minutes_processed,
                "last_results_summary": {
                    "n_results": len(results),
                    "n_detected": len(detected),
                    "stations": stations,
                },
            }
            _status_tmp = self.status_file.with_suffix('.tmp')
            with open(_status_tmp, 'w') as f:
                json.dump(status, f, indent=2)
            _status_tmp.replace(self.status_file)
        except Exception as e:
            logger.error(f"Status write failed: {e}")
    
    def _warn_write_failure(self, product: str, exc: Exception) -> None:
        """Emit a rate-limited WARNING for a data-product write failure (M-M19).

        The contract calls for these to be WARNING-level (so a stuck
        backend is visible in operations), not DEBUG.  Rate-limiting
        keeps the journal sane if a backend goes hard down: one
        WARNING per product per ``_WRITE_WARN_INTERVAL_SEC``, with the
        suppressed count surfaced on the next emitted line so nothing
        is silently hidden.
        """
        now = time.time()
        suppressed_key = f"{product}::suppressed"
        last = self._last_write_warn_ts.get(product, 0.0)
        if now - last >= self._WRITE_WARN_INTERVAL_SEC:
            suppressed = int(self._last_write_warn_ts.get(suppressed_key, 0))
            extra = f" (suppressed {suppressed} similar in last interval)" if suppressed else ""
            logger.warning(
                f"[{self.channel_name}] Failed to write {product}: {exc}{extra}"
            )
            self._last_write_warn_ts[product] = now
            self._last_write_warn_ts[suppressed_key] = 0
        else:
            self._last_write_warn_ts[suppressed_key] = (
                int(self._last_write_warn_ts.get(suppressed_key, 0)) + 1
            )

    def _cleanup_processed_set(self, now_utc: Optional[float] = None) -> None:
        """Drop minutes older than 1 h from ``self.processed_minutes``.

        M-M20: the horizon is computed from ``now_utc`` — the caller's
        ring-derived UTC (``head_utc``) — *not* ``time.time()``.  In
        Fusion mode the OS clock can be hours off the RTP-derived UTC
        that the minutes are keyed by; using ``time.time()`` would
        either prune live minutes (causing them to be reprocessed) or
        let the set grow unbounded.  Falls back to ``time.time()`` only
        when the caller didn't pass an authoritative value (legacy
        paths and tests).
        """
        if now_utc is None:
            now_utc = time.time()
        now_min = (int(now_utc) // 60) * 60
        horizon = now_min - 3600
        old_mins = [m for m in self.processed_minutes if m < horizon]
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
    # sigmond#2: distinguish a PERMISSIONS failure (dir/lock not writable by the
    # metrology user) from a genuine DUPLICATE WRITER (lock held).  The old code
    # caught every OSError and reported "already owns", sending operators to hunt
    # a phantom second process when the real problem was ownership/permissions.
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except PermissionError as e:
        logger.critical(
            f"Cannot create metrology output dir {output_dir}: {e}. "
            f"Permissions problem — the dir (or its parent) must be writable by "
            f"the metrology user (timestd); this is NOT a duplicate writer.")
        sys.exit(1)
    lock_path = output_dir / '.metrology.lock'
    lock_fd = None
    try:
        lock_fd = open(lock_path, 'w')
    except PermissionError as e:
        logger.critical(
            f"Cannot write metrology lock {lock_path}: {e}. "
            f"Permissions problem — {output_dir} must be owned/writable by the "
            f"metrology user (timestd); this is NOT a duplicate writer.")
        sys.exit(1)
    except OSError as e:
        logger.critical(f"Cannot open metrology lock {lock_path}: {e}")
        sys.exit(1)
    # ONLY a BlockingIOError (LOCK_NB on a held lock) means another writer owns it.
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_fd.write(f'{os.getpid()}\n')
        lock_fd.flush()
    except BlockingIOError:
        logger.critical(
            f"Another metrology process already owns {output_dir} — "
            f"refusing to start (duplicate writer would corrupt HDF5 files)")
        sys.exit(1)
    except OSError as e:
        logger.critical(
            f"Failed to acquire metrology lock on {output_dir}: {e}")
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
