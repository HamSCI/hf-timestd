#!/usr/bin/env python3
"""
Stream Recorder V2 - Using ka9q-python RadiodStream

This module provides a simplified recorder that uses ka9q-python's RadiodStream
for RTP reception, resequencing, and sample delivery. This eliminates the need
for custom RTPReceiver and PacketResequencer code.

RadiodStream handles:
- RTP packet reception and parsing
- Packet resequencing (out-of-order handling)
- Gap detection and filling
- Sample decoding (float32 complex IQ)
- Quality metrics (StreamQuality)

This recorder only needs to:
1. Create RadiodStream for each channel
2. Receive decoded numpy arrays via callback
3. Write to Phase 1 archive and queue for Phase 2/3
"""

import numpy as np
import logging
import os
import time
import threading
from pathlib import Path
from typing import Dict, Any, Optional, Callable
from dataclasses import dataclass
from enum import Enum

# radiod auto-destruct timer for our channels (units: radiod main-loop
# frames, ~50 Hz at default 20 ms blocktime → 6000 frames ≈ 120 s).
# Without this, channels we allocated stay live in radiod forever after
# the python process exits — radiod has no way to know we're gone, so
# it keeps streaming bandwidth that nobody consumes.  CoreRecorderV2
# starts a keepalive thread that refreshes this every ~30 s while
# we're running; on clean exit + crash the channel auto-destructs in
# at most LIFETIME / 50 seconds.
RADIOD_LIFETIME_FRAMES = 6000

# Per-channel verify timeout for ensure_channel().  radiod's channel-CREATE
# latency climbs with its existing channel count, so on a busy shared radiod
# a create can take well over the old hard 10 s.  CoreRecorderV2 makes initial
# provisioning non-fatal + retries; a more generous budget here lets more
# channels land on the first pass.  Env-overridable for tuning.
_CHANNEL_VERIFY_TIMEOUT_S = float(os.environ.get("TIMESTD_CHANNEL_VERIFY_TIMEOUT_S", "20"))

from ka9q import RadiodStream, ChannelInfo, StreamQuality, RadiodControl

# NOTE (2026-02-03): Bootstrap functionality migrated into MetrologyEngine.
# The recorder now always archives immediately. MetrologyEngine's fusion_state
# handles timing lock internally using wider search windows until locked.

logger = logging.getLogger(__name__)


class StreamRecorderState(Enum):
    """Stream recorder states"""
    IDLE = "idle"
    STARTING = "starting"
    RECORDING = "recording"
    STOPPING = "stopping"
    ERROR = "error"


@dataclass
class StreamRecorderConfig:
    """Configuration for stream recorder."""
    # Channel identification (from ChannelInfo)
    ssrc: Optional[int]
    frequency_hz: float
    sample_rate: int
    preset: str = 'iq'
    encoding: int = 0  # Encoding type (0=NO_ENCODING, 4=F32, etc.)
    agc_enable: int = 0
    gain: float = 0.0
    description: str = ""
    
    # Output directories
    output_dir: Path = Path("data")
    
    # Receiver location (required for propagation calculation)
    receiver_grid: str = ""
    
    # Station metadata
    station_config: Dict[str, Any] = None
    
    # Phase 1 settings
    raw_buffer_compression: str = 'gzip'
    raw_buffer_file_duration_sec: int = 3600
    compression: str = 'none'  # 'none', 'zstd', or 'lz4'
    compression_level: int = 3  # zstd: 1-22, lz4: 1-12
    file_duration_sec: int = 600  # Raw IQ file chunk duration (seconds)
    
    # RTP Destination
    destination: Optional[str] = None
    
    # Filter edges (Hz) — sent to radiod to control passband width
    # Default None = use radiod's preset defaults
    low_edge: Optional[float] = None
    high_edge: Optional[float] = None
    
    # Phase-engine specific fields
    reception_mode: Optional[str] = None
    target: Optional[str] = None
    null_targets: Optional[list] = None
    combining_method: Optional[str] = None
    
    # Tiered storage: hot buffer in /dev/shm, cold storage on disk
    tiered_storage: bool = False
    hot_buffer_root: Path = None  # e.g., /dev/shm/timestd
    
    # Phase 2 settings
    enable_analysis: bool = True
    analysis_latency_sec: int = 120
    

    
    # Phase 3 settings
    enable_products: bool = True
    output_sample_rate: int = 10
    streaming_latency_minutes: int = 2
    
    # L0 settings
    use_digital_rf: bool = False

    # Archive control: when False, the stream still runs (RTP reception,
    # tap callbacks, timing snapshots) but no IQ data is written to disk.
    # Useful when the channel is consumed only by real-time services
    # (metrology hot-buffer, T6 calibration, etc.) and cold storage is
    # not needed.
    archive: bool = True

    # Shared-memory ring-buffer depth in seconds.  Phase 1 additive path:
    # when > 0 the recorder publishes samples into a per-channel SysV
    # ring segment.  Nothing reads from it yet (Phase 1 is invisible to
    # production); Phase 2 wires metrology workers to it.  ring_seconds
    # == 0 disables the ring entirely.
    ring_seconds: int = 0

    def __post_init__(self):
        self.output_dir = Path(self.output_dir)
        if self.station_config is None:
            self.station_config = {}
        if not self.description:
            freq_mhz = self.frequency_hz / 1e6
            self.description = f"{freq_mhz:.3f} MHz"


class StreamRecorderV2:
    """
    HF Time Standard recorder using ka9q-python RadiodStream.
    
    This is a simplified replacement for PipelineRecorder that delegates
    RTP handling entirely to ka9q-python's RadiodStream.
    
    Benefits:
    - No custom RTP receiver code
    - No custom packet resequencer
    - Automatic gap detection and filling
    - Built-in quality metrics
    - Simpler, more maintainable code
    - Automatic recovery from radiod restarts
    """
    
    def __init__(
        self,
        config: StreamRecorderConfig,
        channel_info: Optional[ChannelInfo] = None,
        get_ntp_status: Optional[Callable[[], Dict[str, Any]]] = None,
        control: Optional[RadiodControl] = None,
        on_stream_dropped: Optional[Callable[[str], None]] = None,
        on_stream_restored: Optional[Callable[[ChannelInfo], None]] = None,
        bootstrap_service: Optional[Any] = None,  # DEPRECATED: kept for API compat
        offset_judge: Optional[Any] = None,
        status_stream: Optional[str] = None,
    ):
        """
        Initialize stream recorder.

        Args:
            config: StreamRecorderConfig
            channel_info: Optional ChannelInfo (can be None if control is provided)
            get_ntp_status: Optional callable for NTP status
            control: Optional RadiodControl for channel creation and recovery
            on_stream_dropped: Optional callback when stream drops
            on_stream_restored: Optional callback when stream restores
            bootstrap_service: DEPRECATED - bootstrap now handled by MetrologyEngine
            offset_judge: Optional OffsetJudge (docs/OFFSET-JUDGE-SPEC-2026-08-05.md).
                Wired to the archive writer once the SSRC is known.
            status_stream: The radiod status stream this channel belongs to
                (CoreRecorderV2.status_address) — first half of the judge's
                per-source key (spec §7 scoping; never global).
        """
        self.config = config
        self.channel_info = channel_info
        self.get_ntp_status = get_ntp_status
        self._control = control
        self._on_stream_dropped = on_stream_dropped
        self._on_stream_restored = on_stream_restored
        self._offset_judge = offset_judge
        self._judge_status_stream = status_stream
        self._judge_source_key: Optional[tuple] = None
        # Ring-anchor provenance (P2 item 4 / audit G6): the raw radiod
        # pair + the judge offset last written into the ring, so the
        # revalidation tick can re-anchor when the judge's correction
        # moves.  (gps_time_ns, rtp_timesnap, offset_ns) or None.
        self._ring_anchor_state: Optional[tuple] = None
        # NOTE (2026-02-03): bootstrap_service parameter kept for API compatibility
        # but is no longer used. MetrologyEngine handles timing lock internally.
        
        # State
        self.state = StreamRecorderState.IDLE
        self._lock = threading.Lock()
        
        # RadiodStream instance (created on start in legacy per-channel mode).
        # When register_with(multi) is used instead of start(), this stays None
        # and ``_parent_multi`` holds the shared MultiStream that owns the socket.
        self.stream: Optional[RadiodStream] = None
        self._parent_multi = None

        # Health monitoring
        self._health_monitor_thread: Optional[threading.Thread] = None
        self._running = False
        self._last_sample_time = 0.0
        self._health_check_interval = 5.0  # Check every 5 seconds (fast detection)
        self._silence_threshold = 10.0  # Recreate if silent for 10 seconds
        
        # NOTE (2026-03-04): Timing poll thread removed — see start() comment.
        # GPS/RTP mapping is now seeded once from channel_info in _create_channel().
        self._timing_poll_thread: Optional[threading.Thread] = None  # kept for stop() compat
        
        # Initialize BinaryArchiveWriter for Phase 1 raw IQ storage
        # Phase 2/3 are handled by separate systemd services (6-service architecture)
        # When archive=False, the stream still runs but no IQ data is written.
        if config.archive:
            from .binary_archive_writer import BinaryArchiveWriter, BinaryArchiveConfig

            archive_config = BinaryArchiveConfig(
                output_dir=config.output_dir,
                channel_name=config.description,
                frequency_hz=config.frequency_hz,
                sample_rate=config.sample_rate,
                station_config=config.station_config,
                compression=config.compression,
                compression_level=config.compression_level,
                use_tiered_storage=config.tiered_storage,
                file_duration_sec=config.file_duration_sec,
            )

            self.archive_writer = BinaryArchiveWriter(archive_config)
        else:
            self.archive_writer = None
            logger.info(f"{config.description}: archive=False, stream-only (no IQ storage)")

        # Phase 1 ring buffer (additive).  When ring_seconds > 0, the
        # recorder also publishes samples into a SysV shared-memory
        # segment keyed by channel name.  Nothing reads from it until
        # Phase 2 wires metrology workers onto the ring.
        self.ring_buffer = None
        # Ring health: None == OK.  A non-None string is surfaced in the
        # recorder status file so the guard can alarm instead of the metrology
        # consumer silently starving — the hot ring IS the metrology feed since
        # Phase 2, so a ring failure is no longer benign.
        self.ring_error: Optional[str] = None
        if config.ring_seconds > 0:
            try:
                from .ring_buffer import RingBuffer, RingBufferOwnershipError
                self.ring_buffer = RingBuffer.create(
                    channel_name=config.description,
                    sample_rate=config.sample_rate,
                    ring_seconds=config.ring_seconds,
                )
            except RingBufferOwnershipError as exc:
                self.ring_error = f"foreign-owned-shm: {exc}"
                self.ring_buffer = None
                logger.critical(
                    f"{config.description}: HOT RING UNAVAILABLE — {exc} "
                    f"Metrology for this channel will STARVE (stale L1). The "
                    f"recorder's root ExecStartPre `clean-stale-rings` should "
                    f"clear this on restart."
                )
            except Exception as exc:
                self.ring_error = f"create-failed: {exc}"
                self.ring_buffer = None
                logger.critical(
                    f"{config.description}: HOT RING create failed ({exc}); "
                    f"metrology for this channel will starve until resolved."
                )

        # Tap callbacks: additional on_samples consumers (e.g. FSK listener)
        self._tap_callbacks: list = []
        self._tap_lock = threading.Lock()

        # Statistics
        self.samples_received = 0
        self.samples_written = 0
        self.batches_received = 0
        self.last_sample_time: float = 0.0
        self.session_start_time: Optional[float] = None
        self.last_quality: Optional[StreamQuality] = None
        
        logger.info(f"StreamRecorderV2 initialized: {config.description}")
        logger.info(f"  SSRC: {config.ssrc}")
        logger.info(f"  Sample rate: {config.sample_rate} Hz")
        logger.info(f"  Output: {config.output_dir}")
    
    def start(self):
        """Start the stream recorder."""
        with self._lock:
            if self.state != StreamRecorderState.IDLE:
                logger.warning(f"Cannot start in state {self.state}")
                return
            
            self.state = StreamRecorderState.STARTING
            self._running = True
        
        try:
            # BinaryArchiveWriter doesn't need explicit start - it's ready on init
            
            # Create channel and start stream
            self._create_channel()
            
            # Start health monitoring thread
            self._health_monitor_thread = threading.Thread(
                target=self._health_monitor_loop,
                name=f"HealthMonitor-{self.config.description}",
                daemon=True
            )
            self._health_monitor_thread.start()
            logger.info(f"{self.config.description}: Health monitoring started")
            
            # NOTE (2026-03-04): Timing poll thread REMOVED.
            # discover_channels() listens to the GLOBAL status multicast which
            # mixes status from ALL radiod decoders.  Different decoders for the
            # same SSRC have different RTP counter spaces, so the poll frequently
            # returned the wrong rtp_timesnap — corrupting the GPS/RTP mapping
            # and pushing minute boundaries ~4500s into the future.
            # The archive writer is now seeded once from channel_info (per-client,
            # authoritative) in _create_channel(), and re-seeded on radiod restart
            # via the health monitor's _create_channel() call.
            
            self.session_start_time = time.time()
            
            with self._lock:
                self.state = StreamRecorderState.RECORDING
            
            logger.info(f"{self.config.description}: Stream recorder started successfully")
            
        except Exception as e:
            logger.error(f"{self.config.description}: Failed to start: {e}", exc_info=True)
            with self._lock:
                self.state = StreamRecorderState.ERROR
                self._running = False
            raise
    
    def _create_channel(self):
        """Create channel and start RadiodStream.
        
        Uses ka9q-python's ensure_channel which handles all SSRC management,
        channel reuse, and verification internally.
        """
        logger.info(f"{self.config.description}: Requesting channel at {self.config.frequency_hz/1e6:.3f} MHz")
        logger.info(f"  Parameters: preset={self.config.preset}, rate={self.config.sample_rate}, "
                   f"agc={self.config.agc_enable}, gain={self.config.gain}, enc={self.config.encoding}")
        
        # Let ka9q-python handle all channel management
        kwargs = {
            'frequency_hz': float(self.config.frequency_hz),
            'preset': self.config.preset,
            'sample_rate': self.config.sample_rate,
            'agc_enable': self.config.agc_enable,
            'gain': self.config.gain,
            'destination': self.config.destination,
            'encoding': self.config.encoding,
            'timeout': _CHANNEL_VERIFY_TIMEOUT_S,
            'frequency_tolerance': 1.0,
            # Self-destruct timer; CoreRecorderV2 keeps it refreshed.
            'lifetime': RADIOD_LIFETIME_FRAMES,
        }
        
        # Check backend capabilities
        caps = {}
        try:
            if hasattr(self._control, 'get_capabilities'):
                caps = self._control.get_capabilities()
        except Exception as e:
            logger.debug(f"{self.config.description}: get_capabilities failed: {e}")
            
        # Add phase-engine extensions if supported
        if caps.get("backend") == "phase-engine":
            if getattr(self.config, 'reception_mode', None):
                kwargs['reception_mode'] = self.config.reception_mode
            if getattr(self.config, 'target', None):
                kwargs['target'] = self.config.target
            if getattr(self.config, 'null_targets', None):
                kwargs['null_targets'] = self.config.null_targets
            if getattr(self.config, 'combining_method', None):
                kwargs['combining_method'] = self.config.combining_method
                
        self.channel_info = self._control.ensure_channel(**kwargs)
        
        # Update config with SSRC from ka9q-python
        self.config.ssrc = self.channel_info.ssrc

        # Set filter edges if configured (widens passband for FSK etc.)
        self._set_filter_edges(self.channel_info.ssrc)

        # Wire the Offset Judge now that the per-source key exists.
        self._wire_offset_judge()

        # P2: keep this channel's radiod pair observable during steady
        # state (own-stream STATUS listener feeding the 60 s
        # revalidation tick).  Spec §7-scoped; see the method docs.
        self._register_with_status_listener()
        
        logger.info(f"{self.config.description}: Channel ready SSRC {self.channel_info.ssrc:08x} "
                   f"at {self.channel_info.multicast_address}:{getattr(self.channel_info, 'port', 5004)}")
        
        # Create RadiodStream to receive data
        # Stop existing stream if any
        if self.stream:
            try:
                self.stream.stop()
            except Exception as e:
                logger.debug(f"{self.config.description}: Failed to stop previous stream: {e}")
        
        samples_per_packet = 200  # Average timestamp delta per packet
        
        self.stream = RadiodStream(
            channel=self.channel_info,
            on_samples=self._handle_samples,
            samples_per_packet=samples_per_packet,
            resequence_buffer_size=128
        )
        
        self.stream.start()
        self._last_sample_time = time.time()  # Reset silence timer
        logger.info(f"{self.config.description}: RadiodStream started")

        # Seed archive writer with GPS/RTP mapping from the channel's own
        # ChannelInfo.  ensure_channel() returns timing from our dedicated
        # multicast group — not the global status multicast, which mixes
        # status packets from ALL decoders and can return the wrong
        # rtp_timesnap for our SSRC.
        gps_time = getattr(self.channel_info, 'gps_time', None)
        rtp_snap = getattr(self.channel_info, 'rtp_timesnap', None)
        if gps_time is not None and rtp_snap is not None:
            if self.archive_writer is not None:
                # The writer relays the pair to the Offset Judge at this
                # anchor-adoption point (see BinaryArchiveWriter.add_timing_snapshot).
                self.archive_writer.add_timing_snapshot(
                    gps_time_ns=gps_time,
                    rtp_timesnap=rtp_snap
                )
            elif self._offset_judge is not None and self._judge_source_key:
                # archive=False channels have no writer to relay the pair;
                # register directly so the judge still tracks this source.
                try:
                    self._offset_judge.register_radiod_pair(
                        self._judge_source_key, gps_time, rtp_snap,
                        self.config.sample_rate,
                    )
                except Exception as exc:
                    logger.debug(
                        f"{self.config.description}: judge pair "
                        f"registration failed: {exc}"
                    )
            # Ring anchor follows the same judged/corrected mapping the
            # writer uses (P2 item 4 — audit G6: archive and ring must
            # not silently diverge).
            self._update_ring_anchor(gps_time, rtp_snap)
            logger.info(
                f"{self.config.description}: Seeded timing from channel_info — "
                f"GPS_TIME={gps_time}, RTP_TIMESNAP={rtp_snap}"
            )
        else:
            logger.warning(
                f"{self.config.description}: channel_info missing timing — "
                f"gps_time={gps_time}, rtp_timesnap={rtp_snap}"
            )

    def register_with(self, multi):
        """Register this channel on a shared ``MultiStream`` instead of opening a per-channel ``RadiodStream``.

        Performs ``ensure_channel()`` with the same kwargs as
        :meth:`_create_channel` (so phase-engine extensions and
        filter-edge commands behave identically), seeds the archive
        writer + ring buffer with GPS/RTP timing from the returned
        ``ChannelInfo``, and registers ``self._handle_samples`` as the
        per-SSRC callback on the parent ``MultiStream``. The parent
        owns the receive socket; this recorder remains responsible for
        archive writing, ring-buffer hot data, and its own health
        monitor.

        ``MultiStream.add_channel`` re-runs ``ensure_channel`` internally —
        idempotent for a SSRC we just provisioned, so the second call
        is a cheap status probe and does not disturb the channel's
        phase-engine state.

        This method is the ``CoreRecorderV2``-driven entry point for
        the shared-socket architecture. The legacy :meth:`start` /
        :meth:`_create_channel` path remains in place so the change
        can be rolled back via configuration without code edits.
        """
        logger.info(
            f"{self.config.description}: Requesting channel at "
            f"{self.config.frequency_hz/1e6:.3f} MHz (shared MultiStream mode)"
        )
        logger.info(
            f"  Parameters: preset={self.config.preset}, "
            f"rate={self.config.sample_rate}, agc={self.config.agc_enable}, "
            f"gain={self.config.gain}, enc={self.config.encoding}"
        )

        kwargs = {
            'frequency_hz': float(self.config.frequency_hz),
            'preset': self.config.preset,
            'sample_rate': self.config.sample_rate,
            'agc_enable': self.config.agc_enable,
            'gain': self.config.gain,
            'destination': self.config.destination,
            'encoding': self.config.encoding,
            'timeout': _CHANNEL_VERIFY_TIMEOUT_S,
            'frequency_tolerance': 1.0,
            # Self-destruct timer; CoreRecorderV2 keeps it refreshed.
            'lifetime': RADIOD_LIFETIME_FRAMES,
        }

        caps = {}
        try:
            if hasattr(self._control, 'get_capabilities'):
                caps = self._control.get_capabilities()
        except Exception as e:
            logger.debug(f"{self.config.description}: get_capabilities failed: {e}")

        if caps.get("backend") == "phase-engine":
            if getattr(self.config, 'reception_mode', None):
                kwargs['reception_mode'] = self.config.reception_mode
            if getattr(self.config, 'target', None):
                kwargs['target'] = self.config.target
            if getattr(self.config, 'null_targets', None):
                kwargs['null_targets'] = self.config.null_targets
            if getattr(self.config, 'combining_method', None):
                kwargs['combining_method'] = self.config.combining_method

        self.channel_info = self._control.ensure_channel(**kwargs)
        self.config.ssrc = self.channel_info.ssrc
        self._set_filter_edges(self.channel_info.ssrc)

        # Wire the Offset Judge now that the per-source key exists.
        self._wire_offset_judge()

        # P2: keep this channel's radiod pair observable during steady
        # state (own-stream STATUS listener feeding the 60 s
        # revalidation tick).  Spec §7-scoped; see the method docs.
        self._register_with_status_listener()

        logger.info(
            f"{self.config.description}: Channel ready SSRC "
            f"{self.channel_info.ssrc:08x} at "
            f"{self.channel_info.multicast_address}:"
            f"{getattr(self.channel_info, 'port', 5004)}"
        )

        # Register on the shared MultiStream — its on_samples is wired here,
        # the parent owns the socket and the receive thread.  We forward the
        # operator-supplied on_stream_dropped/on_stream_restored callbacks
        # through to MultiStream's per-slot drop detector
        # (ka9q.MultiStream._handle_drop/_attempt_restore), which replaces
        # this recorder's own _health_monitor_loop in shared mode.  The
        # legacy per-channel health monitor's job (silence detection +
        # ensure_channel recovery on radiod restart) is now done centrally
        # by MultiStream against the shared socket — so register_with does
        # NOT spawn _health_monitor_thread and stop() tolerates it being
        # None.
        multi.add_channel(
            frequency_hz=float(self.config.frequency_hz),
            preset=self.config.preset,
            sample_rate=self.config.sample_rate,
            encoding=self.config.encoding,
            agc_enable=self.config.agc_enable,
            gain=self.config.gain,
            on_samples=self._handle_samples,
            on_stream_dropped=self._on_stream_dropped,
            on_stream_restored=self._on_stream_restored,
            # Self-destruct timer; CoreRecorderV2 keeps it refreshed.
            lifetime=RADIOD_LIFETIME_FRAMES,
        )
        self._parent_multi = multi
        self._last_sample_time = time.time()
        # Transition to RECORDING so _handle_samples accepts the first
        # batch the parent MultiStream will deliver — start() does this
        # for the legacy path; register_with is the analogue for shared
        # mode.  session_start_time is also set here so disk-budget /
        # uptime accounting is sane.
        self.session_start_time = time.time()
        with self._lock:
            self.state = StreamRecorderState.RECORDING
        logger.info(
            f"{self.config.description}: Registered on shared MultiStream"
        )

        # Seed archive writer with GPS/RTP mapping from the channel's own
        # ChannelInfo.  ensure_channel() returns timing from our dedicated
        # multicast group — not the global status multicast, which mixes
        # status packets from ALL decoders and can return the wrong
        # rtp_timesnap for our SSRC.  This block is intentionally identical
        # to the seeding at the end of _create_channel().
        gps_time = getattr(self.channel_info, 'gps_time', None)
        rtp_snap = getattr(self.channel_info, 'rtp_timesnap', None)
        if gps_time is not None and rtp_snap is not None:
            if self.archive_writer is not None:
                # The writer relays the pair to the Offset Judge at this
                # anchor-adoption point (see BinaryArchiveWriter.add_timing_snapshot).
                self.archive_writer.add_timing_snapshot(
                    gps_time_ns=gps_time,
                    rtp_timesnap=rtp_snap
                )
            elif self._offset_judge is not None and self._judge_source_key:
                # archive=False channels have no writer to relay the pair;
                # register directly so the judge still tracks this source.
                try:
                    self._offset_judge.register_radiod_pair(
                        self._judge_source_key, gps_time, rtp_snap,
                        self.config.sample_rate,
                    )
                except Exception as exc:
                    logger.debug(
                        f"{self.config.description}: judge pair "
                        f"registration failed: {exc}"
                    )
            # Ring anchor follows the same judged/corrected mapping the
            # writer uses (P2 item 4 — audit G6: archive and ring must
            # not silently diverge).
            self._update_ring_anchor(gps_time, rtp_snap)
            logger.info(
                f"{self.config.description}: Seeded timing from channel_info — "
                f"GPS_TIME={gps_time}, RTP_TIMESNAP={rtp_snap}"
            )
        else:
            logger.warning(
                f"{self.config.description}: channel_info missing timing — "
                f"gps_time={gps_time}, rtp_timesnap={rtp_snap}"
            )

    def _wire_offset_judge(self) -> None:
        """Bind the Offset Judge to this channel's (status_stream, ssrc).

        Called from _create_channel() / register_with() once
        ensure_channel() has produced the SSRC — the judge's per-source
        key (spec §7) cannot exist earlier.  Safe to re-run on channel
        recreation (radiod restart): the key is stable, and the writer's
        set_offset_judge re-registers the current pair with the judge.
        """
        if self._offset_judge is None or not self.config.ssrc:
            return
        self._judge_source_key = (
            str(self._judge_status_stream or ''),
            int(self.config.ssrc),
        )
        if self.archive_writer is not None:
            try:
                self.archive_writer.set_offset_judge(
                    self._offset_judge, self._judge_source_key
                )
            except Exception as exc:
                logger.warning(
                    f"{self.config.description}: offset-judge wiring failed "
                    f"(recording continues uncorrected): {exc}"
                )

    def _register_with_status_listener(self) -> None:
        """Wire this channel into ka9q-python's continuous STATUS listener.

        The listener is bound to THIS client's own radiod status stream
        (RadiodControl's status group) and updates only the registered
        SSRC's ChannelInfo — the spec §7 scoping (per status-stream,
        per-SSRC; never ``discover_channels()`` on a mixed multicast,
        which was the March 2026 ``2d54c9c`` corruption vector).  With
        the listener wired, ``channel_info.gps_time``/``rtp_timesnap``
        refresh in place per radiod STATUS broadcast, and the P2
        revalidation tick (:meth:`revalidate_radiod_pair`) re-observes
        radiod's current claim from those fields.

        Silent no-op on old ka9q-python (<3.16) — revalidation then
        degrades to re-reading the creation-time snapshot (harmless:
        it dedupes), and discontinuity recovery falls back to the
        health monitor's silence-triggered channel recreation.
        Safe to re-run on channel recreation: the new ChannelInfo
        object must be (re-)registered so the listener mutates the
        currently-used reference.
        """
        ci = self.channel_info
        if ci is None or not getattr(ci, 'ssrc', 0) or self._control is None:
            return
        start_fn = getattr(self._control, 'start_status_listener', None)
        if start_fn is None:
            if not getattr(self, '_listener_absent_logged', False):
                logger.info(
                    f"{self.config.description}: ka9q-python STATUS listener "
                    f"unavailable (<3.16) — radiod-pair revalidation limited "
                    f"to the creation-time snapshot"
                )
                self._listener_absent_logged = True
            return
        try:
            start_fn().register_channel(ci)
            logger.info(
                f"{self.config.description}: SSRC "
                f"{getattr(ci, 'ssrc', 0):08x} wired to own-stream STATUS "
                f"listener (gps_time/rtp_timesnap refresh per broadcast)"
            )
        except Exception as exc:
            logger.warning(
                f"{self.config.description}: STATUS-listener wiring failed "
                f"(revalidation degraded to creation-time snapshot): {exc}"
            )

    # Ring re-anchor hysteresis: the ring's anchor is refreshed when the
    # judge's correction has moved at least this far since the last ring
    # write.  Keeps steady-state epoch bumps rare (each bump risks one
    # reader-retry if it lands mid-copy) while bounding ring-vs-writer
    # label divergence to ~this threshold + one revalidation tick.
    RING_REANCHOR_MIN_DELTA_NS = 5_000_000.0  # 5 ms

    def _current_judge_offset_ns(self, rtp_snap: int) -> float:
        """The judge's current label correction for this source (0 when
        no judge / no verdict — the raw-radiod pre-judge behavior)."""
        if self._offset_judge is None or not self._judge_source_key:
            return 0.0
        try:
            v = self._offset_judge.offset_for(self._judge_source_key, rtp_snap)
        except Exception:  # noqa: BLE001 — judge trouble never disturbs the ring
            return 0.0
        return float(v.offset_ns) if v is not None else 0.0

    def _update_ring_anchor(self, gps_time_ns: int, rtp_timesnap: int) -> None:
        """Anchor the ring with the JUDGED mapping (P2 item 4, audit G6).

        The ring stores a (gps_time_ns, rtp_timesnap) pair that readers
        (metrology) resolve exactly like the writer's sidecar mapping —
        but the ring format has no per-chunk "timing" block, so the
        judge's correction is folded into the pair itself:

            ring_gps_time_ns = radiod_gps_time_ns + offset_ns

        which makes ring-resolved UTC == writer's corrected labels.
        Called at every radiod pair adoption (seed + revalidation) and
        re-invoked by the revalidation tick when the judge's offset has
        moved beyond RING_REANCHOR_MIN_DELTA_NS.  Judge absent ⇒ raw
        pair, byte-identical to the pre-judge ring.
        """
        if self.ring_buffer is None:
            return
        offset_ns = self._current_judge_offset_ns(int(rtp_timesnap))
        try:
            self.ring_buffer.update_anchor(
                gps_time_ns=int(gps_time_ns + round(offset_ns)),
                rtp_timesnap=int(rtp_timesnap),
            )
            self._ring_anchor_state = (
                int(gps_time_ns), int(rtp_timesnap), float(offset_ns)
            )
            if offset_ns:
                logger.info(
                    f"{self.config.description}: ring anchor updated with "
                    f"judged correction {offset_ns / 1e9:+.6f}s "
                    f"(rtp_timesnap={int(rtp_timesnap)})"
                )
        except Exception as exc:
            logger.error(
                f"{self.config.description}: ring update_anchor failed: {exc}"
            )

    def _reanchor_ring_if_offset_drifted(self) -> None:
        """Re-anchor the ring when the judge's correction has moved.

        The writer picks up a fresh verdict at every chunk start; the
        ring's anchor is a one-shot value, so without this the two
        paths diverge by however far the offset walks (audit G6).  Runs
        on the revalidation tick; the RING_REANCHOR_MIN_DELTA_NS
        hysteresis keeps healthy steady state (offset flat) write-free.
        """
        if self.ring_buffer is None or self._ring_anchor_state is None:
            return
        gps_time_ns, rtp_timesnap, applied_ns = self._ring_anchor_state
        current_ns = self._current_judge_offset_ns(rtp_timesnap)
        if abs(current_ns - applied_ns) > self.RING_REANCHOR_MIN_DELTA_NS:
            self._update_ring_anchor(gps_time_ns, rtp_timesnap)

    # A re-observed radiod pair that disagrees with the adopted mapping
    # by more than this is a genuine discontinuity (restart/re-snap) and
    # is adopted; below it, the disagreement is radiod status jitter /
    # host-clock slew and the steel-ruler mapping is left alone (the
    # judge measures + corrects the residual against its benches on its
    # own 10 s tick).  0.75 s matches the fleet re-anchor threshold and
    # the judge's pair_step_threshold_s.
    REVALIDATE_ADOPT_THRESHOLD_S = 0.75

    def revalidate_radiod_pair(self) -> None:
        """P2 per-source revalidation tick (called ~60 s by CoreRecorderV2).

        Re-observes radiod's current (GPS_TIME, RTP_TIMESNAP) claim from
        this channel's own listener-refreshed ChannelInfo (spec §7 scope)
        and:

        * adopts it (writer flush-and-adopt + judge segment fracture)
          when it disagrees with the current mapping beyond
          REVALIDATE_ADOPT_THRESHOLD_S — catches radiod restarts /
          counter re-snaps that the silence-based health monitor never
          sees because the stream keeps flowing;
        * leaves the mapping alone on steady sub-threshold agreement —
          a wrong-but-STEADY pair needs no re-adoption to be re-judged:
          the judge measures the registered pair against its benches
          every tick already (P1);
        * never raises (recording must not be disturbed).
        """
        try:
            self._revalidate_radiod_pair_inner()
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                f"{self.config.description}: revalidation tick failed: {exc}"
            )

    def _revalidate_radiod_pair_inner(self) -> None:
        ci = self.channel_info
        if ci is None:
            return
        gps_time = getattr(ci, 'gps_time', None)
        rtp_snap = getattr(ci, 'rtp_timesnap', None)
        if gps_time is None or rtp_snap is None:
            return

        adopted = False
        if self.archive_writer is not None:
            diff = self.archive_writer.evaluate_pair(gps_time, rtp_snap)
            if diff is None:
                # No mapping adopted yet (writer still waiting for
                # timing) — seed it now.
                self.archive_writer.add_timing_snapshot(
                    gps_time_ns=gps_time, rtp_timesnap=rtp_snap
                )
                adopted = True
            elif abs(diff) > self.REVALIDATE_ADOPT_THRESHOLD_S:
                logger.warning(
                    f"{self.config.description}: revalidation found radiod's "
                    f"advertised pair {diff:+.3f}s away from the adopted "
                    f"mapping (threshold "
                    f"{self.REVALIDATE_ADOPT_THRESHOLD_S}s) — adopting "
                    f"(radiod restart / counter re-snap signature)"
                )
                # add_timing_snapshot flushes the in-progress chunk and
                # relays the new pair to the judge (segment fracture).
                self.archive_writer.add_timing_snapshot(
                    gps_time_ns=gps_time, rtp_timesnap=rtp_snap
                )
                adopted = True
            else:
                logger.debug(
                    f"{self.config.description}: revalidation — pair steady "
                    f"({diff:+.4f}s vs adopted mapping); judge continues "
                    f"judging the adopted pair on its own tick"
                )
        elif self._offset_judge is not None and self._judge_source_key:
            # archive=False: the judge holds the only mapping for this
            # source.  Its own dedupe / pair_step_threshold machinery
            # filters status jitter vs genuine fractures.
            self._offset_judge.register_radiod_pair(
                self._judge_source_key, gps_time, rtp_snap,
                self.config.sample_rate,
            )

        # Ring/metrology unification (P2 item 4): the ring re-anchors at
        # every pair adoption, and otherwise follows the judge's moving
        # correction so ring-resolved UTC and the writer's corrected
        # labels cannot silently diverge (audit G6).
        if adopted:
            self._update_ring_anchor(gps_time, rtp_snap)
        else:
            self._reanchor_ring_if_offset_drifted()

    def _set_filter_edges(self, ssrc: int):
        """Send filter edge commands to radiod if configured."""
        low = self.config.low_edge
        high = self.config.high_edge
        if low is None and high is None:
            return
        
        try:
            import secrets
            from ka9q.types import StatusType
            from ka9q.control import encode_int, encode_double, encode_eol, CMD
            
            cmdbuffer = bytearray()
            cmdbuffer.append(CMD)
            encode_int(cmdbuffer, StatusType.OUTPUT_SSRC, ssrc)
            encode_int(cmdbuffer, StatusType.COMMAND_TAG, secrets.randbits(31))
            
            if low is not None:
                encode_double(cmdbuffer, StatusType.LOW_EDGE, float(low))
            if high is not None:
                encode_double(cmdbuffer, StatusType.HIGH_EDGE, float(high))
            
            encode_eol(cmdbuffer)
            self._control.send_command(cmdbuffer)
            
            logger.info(f"{self.config.description}: Set filter edges: low={low}, high={high}")
        except Exception as e:
            logger.warning(f"{self.config.description}: Failed to set filter edges: {e}")

    def _health_monitor_loop(self):
        """Monitor stream health and recreate channel if needed (e.g., after radiod restart)."""
        while self._running:
            try:
                time.sleep(self._health_check_interval)
                
                if not self._running:
                    break
                
                # Check if we're receiving data
                silence_duration = time.time() - self._last_sample_time
                
                if silence_duration > self._silence_threshold:
                    logger.warning(
                        f"{self.config.description}: No data for {silence_duration:.0f}s - "
                        f"attempting channel recreation (radiod may have restarted)"
                    )
                    
                    try:
                        # Recreate channel — acquire lock to prevent concurrent
                        # access to self.stream / self.channel_info / self.config.ssrc
                        with self._lock:
                            self._create_channel()
                        logger.info(f"{self.config.description}: Channel recreated successfully")
                        
                        if self._on_stream_restored and self.channel_info:
                            self._on_stream_restored(self.channel_info)
                            
                    except Exception as e:
                        logger.error(f"{self.config.description}: Failed to recreate channel: {e}")
                        # Will retry on next health check
                        
            except Exception as e:
                logger.error(f"{self.config.description}: Health monitor error: {e}")

    def _timing_poll_loop(self):
        """
        Capture GPS_TIME/RTP_TIMESNAP pairs by re-discovering channel status.
        
        IMPORTANT: ChannelInfo from ka9q-python is a SNAPSHOT from discovery time.
        The gps_time/rtp_timesnap values do NOT update dynamically. We must
        re-discover the channel to get fresh timing values from radiod status.
        
        Metrological justification:
        - With GPSDO, the RTP-to-UTC relationship is stable (sub-ppm drift)
        - We capture periodically to document the relationship
        - Fresh discovery ensures we get current GPS_TIME/RTP_TIMESNAP
        
        In L4/L5 (GPS+PPS): The relationship is stable to ±1μs
        In L3/L2/L1 (NTP): Captures the NTP-derived relationship
        
        Storage overhead: ~120 snapshots/minute × ~50 bytes = ~6 KB/minute (negligible)
        """
        from ka9q import discover_channels
        
        last_captured_rtp = None
        status_address = getattr(self._control, 'status_address', None) if self._control else None
        
        if not status_address:
            logger.warning(f"{self.config.description}: No status_address for timing poll")
            return
        
        logger.info(f"{self.config.description}: Timing poll using status_address={status_address}")
        
        while self._running:
            try:
                time.sleep(self._timing_poll_interval)
                
                if not self._running:
                    break
                
                if self.channel_info is None:
                    continue
                
                # Re-discover to get fresh gps_time/rtp_timesnap from radiod status
                # This is necessary because ChannelInfo is a snapshot, not live
                try:
                    channels = discover_channels(status_address, listen_duration=0.5)
                    
                    # Find our channel by SSRC
                    our_ssrc = self.channel_info.ssrc
                    fresh_info = channels.get(our_ssrc)
                    
                    if fresh_info is None:
                        # Try finding by SSRC as string key
                        for ssrc, info in channels.items():
                            if ssrc == our_ssrc:
                                fresh_info = info
                                break
                    
                    if fresh_info is None:
                        logger.debug(f"{self.config.description}: Channel SSRC {our_ssrc} not found in discovery")
                        continue
                    
                    gps_time = fresh_info.gps_time
                    rtp_timesnap = fresh_info.rtp_timesnap
                    
                except Exception as e:
                    logger.debug(f"{self.config.description}: Discovery failed: {e}")  # Expected during radiod restart
                    continue
                
                if gps_time is not None and rtp_timesnap is not None:
                    # Only store if rtp_timesnap changed (new status received)
                    if rtp_timesnap != last_captured_rtp and self.archive_writer is not None:
                        stored = self.archive_writer.add_timing_snapshot(
                            gps_time_ns=gps_time,
                            rtp_timesnap=rtp_timesnap
                        )
                        if stored:
                            last_captured_rtp = rtp_timesnap
                            logger.info(
                                f"{self.config.description}: Timing snapshot captured - "
                                f"GPS_TIME={gps_time}, RTP_TIMESNAP={rtp_timesnap}"
                            )
                else:
                    logger.debug(f"{self.config.description}: No timing data - gps_time={gps_time}, rtp_timesnap={rtp_timesnap}")
                    
            except Exception as e:
                logger.error(f"{self.config.description}: Timing poll loop error: {e}")

    def stop(self) -> Optional[StreamQuality]:
        """
        Stop the stream recorder gracefully.
        
        Returns:
            Final StreamQuality metrics, or None if not recording
        """
        with self._lock:
            if self.state == StreamRecorderState.IDLE:
                return None
            
            self.state = StreamRecorderState.STOPPING
            self._running = False  # Stop health monitor
        
        final_quality = None
        
        try:
            # Stop health monitor
            if self._health_monitor_thread:
                self._health_monitor_thread.join(timeout=2.0)
                self._health_monitor_thread = None
            
            # Stop timing poll thread
            if self._timing_poll_thread:
                self._timing_poll_thread.join(timeout=2.0)
                self._timing_poll_thread = None
            
            # In shared-MultiStream mode the parent CoreRecorderV2 owns
            # the socket and stops the MultiStream itself; this recorder
            # never opened a per-channel RadiodStream.  Use the most
            # recent StreamQuality observed via _handle_samples as the
            # final stats — it's continually updated and persists after
            # callbacks stop firing.
            if self._parent_multi is not None:
                final_quality = self.last_quality
            elif self.stream:
                # Legacy: stop the per-channel RadiodStream.
                if hasattr(self.stream, 'get_quality'):
                    final_quality = self.stream.get_quality()
                else:
                    final_quality = self.stream.stop()

                if hasattr(self.stream, 'stop') and final_quality is not None:
                    # If it's RadiodStream, stop() already returned final_quality
                    # Defensive: stop() after get_quality() in case of unexpected type
                    if not isinstance(final_quality, StreamQuality):
                        # This shouldn't happen with RadiodStream, but just in case
                        self.stream.stop()
                    else:
                        # RadiodStream already stopped if final_quality is StreamQuality
                        pass
                else:
                    # Ensure it's stopped
                    self.stream.stop()

                self.stream = None
            
            # Close the archive writer (flushes pending data)
            if self.archive_writer is not None:
                self.archive_writer.close()

            # Destroy the ring buffer LAST so any late callback from the
            # stream has already been drained.  Consumers attached to the
            # segment will see the next attach fail or get overrun on
            # their next read and should resync.
            if self.ring_buffer is not None:
                try:
                    self.ring_buffer.destroy()
                except Exception as exc:
                    logger.warning(
                        f"{self.config.description}: ring destroy failed: {exc}"
                    )
                self.ring_buffer = None

        except Exception as e:
            logger.error(f"{self.config.description}: Error during stop: {e}")
        
        with self._lock:
            self.state = StreamRecorderState.IDLE
        
        logger.info(f"{self.config.description}: Stream recorder stopped")
        logger.info(f"  Samples received: {self.samples_received}")
        logger.info(f"  Samples written: {self.samples_written}")
        logger.info(f"  Batches: {self.batches_received}")
        
        if final_quality:
            logger.info(f"  Completeness: {final_quality.completeness_pct:.2f}%")
            logger.info(f"  Gaps filled: {final_quality.total_gaps_filled}")
            logger.info(f"  Packets lost: {final_quality.rtp_packets_lost}")
        
        return final_quality
    
    def add_tap(self, callback) -> None:
        """Register an additional on_samples consumer.

        The callback receives the same (samples, quality) arguments as the
        RadiodStream on_samples callback.  Taps are called after the archive
        write so they never block recording.
        """
        with self._tap_lock:
            self._tap_callbacks.append(callback)

    def _handle_samples(self, samples: np.ndarray, quality: StreamQuality):
        """
        Handle incoming samples from RadiodStream.
        
        Args:
            samples: Complex64 IQ samples (already decoded by RadiodStream)
            quality: StreamQuality with timing and metrics
        """
        try:
            with self._lock:
                if self.state != StreamRecorderState.RECORDING:
                    return
                
                self.batches_received += 1
                self.samples_received += len(samples)
                self.last_sample_time = time.time()
                self._last_sample_time = self.last_sample_time  # For health monitor
                self.last_quality = quality
            
            # system_time is a startup hint only — the archive writer uses
            # GPS_TIME/RTP_TIMESNAP as its authoritative source once locked.
            # last_packet_utc comes from rtp_to_wallclock() which is derived
            # from the 32-bit RTP counter; that counter wraps every ~49.7 hours
            # at 24 kHz, making any comparison against time.time() unreliable.
            # Always use the OS clock here; the archive writer will correct it.
            system_time = time.time()
            
            # Calculate gap samples for this batch
            # ka9q-python fills gaps with zeros which breaks phase continuity
            batch_gap_samples = 0
            if quality.batch_gaps:
                batch_gap_samples = sum(gap.duration_samples for gap in quality.batch_gaps)
            
            # NOTE (2026-02-03): Bootstrap gating removed. We now always archive immediately.
            # MetrologyEngine's fusion_state handles timing lock internally using wider
            # search windows until locked. The archive writer uses RTP-derived minute
            # boundaries from GPS_TIME/RTP_TIMESNAP, which works in both RTP and Fusion modes.
            
            # Write to Phase 1 archive (Phase 2/3 handled by separate services)
            if self.archive_writer is not None:
                self.archive_writer.write_samples(
                    samples=samples,
                    rtp_timestamp=quality.last_rtp_timestamp,
                    system_time=system_time,
                    gap_samples=batch_gap_samples
                )

            # Publish the batch into the ring buffer AFTER the archive
            # write so any ring-buffer bug during Phase 1 rollout cannot
            # affect disk writes.  RTP of the first sample in this batch
            # is (total_delivered - len) relative to first_rtp_timestamp.
            if self.ring_buffer is not None:
                try:
                    batch_first_rtp = (
                        quality.first_rtp_timestamp
                        + quality.total_samples_delivered
                        - len(samples)
                    ) & 0xFFFFFFFF
                    self.ring_buffer.write_samples(samples, batch_first_rtp)
                    if batch_gap_samples:
                        self.ring_buffer.record_gap(batch_gap_samples)
                except Exception as exc:
                    logger.error(
                        f"{self.config.description}: ring write_samples failed: {exc}"
                    )

            self.samples_written += len(samples)

            # Forward to tap callbacks
            with self._tap_lock:
                taps = list(self._tap_callbacks)
            for tap in taps:
                try:
                    tap(samples, quality)
                except Exception as tap_err:
                    logger.warning(f"{self.config.description}: tap callback error: {tap_err}")

            # Log gaps if present
            if quality.has_gaps:
                logger.debug(
                    f"{self.config.description}: Batch with gaps - "
                    f"{quality.total_gaps_filled} samples filled, "
                    f"completeness={quality.completeness_pct:.1f}%"
                )
                
        except Exception as e:
            logger.error(f"{self.config.description}: Sample processing error: {e}", exc_info=True)
    
    def _handle_stream_dropped(self, reason: str):
        """Handle stream drop notification."""
        logger.warning(f"{self.config.description}: Stream DROPPED - {reason}")
        
        # Forward to external callback if provided
        if self._on_stream_dropped:
            try:
                self._on_stream_dropped(reason)
            except Exception as e:
                logger.error(f"Error in stream_dropped callback: {e}")
    
    def _handle_stream_restored(self, channel: ChannelInfo):
        """Handle stream restoration notification."""
        logger.info(f"{self.config.description}: Stream RESTORED - SSRC={channel.ssrc}")
        
        # Update channel info with new values
        self.channel_info = channel
        self.config.ssrc = channel.ssrc
        
        # Forward to external callback if provided
        if self._on_stream_restored:
            try:
                self._on_stream_restored(channel)
            except Exception as e:
                logger.error(f"Error in stream_restored callback: {e}")
    
    def get_stats(self) -> Dict[str, Any]:
        """Get current statistics."""
        with self._lock:
            archive_stats = self.archive_writer.get_stats() if self.archive_writer else {}
            
            uptime = 0.0
            if self.session_start_time:
                uptime = time.time() - self.session_start_time
            
            stats = {
                'state': self.state.value,
                'samples_received': self.samples_received,
                'samples_written': self.samples_written,
                'batches_received': self.batches_received,
                'uptime_seconds': uptime,
                'last_sample_time': self.last_sample_time,
                # Archive stats
                'phase1_samples': archive_stats.get('samples_written', 0),
                'minutes_written': archive_stats.get('minutes_written', 0),
            }
            
            # Add quality metrics if available
            if self.last_quality:
                stats.update({
                    'completeness_pct': self.last_quality.completeness_pct,
                    'packets_received': self.last_quality.rtp_packets_received,
                    'packets_lost': self.last_quality.rtp_packets_lost,
                    'packets_resequenced': self.last_quality.rtp_packets_resequenced,
                    'total_gaps_filled': self.last_quality.total_gaps_filled,
                })
            
            return stats
    
    def get_status(self) -> Dict[str, Any]:
        """Get status for web-ui monitoring."""
        stats = self.get_stats()
        return {
            'description': self.config.description,
            'frequency_hz': self.config.frequency_hz,
            'sample_rate': self.config.sample_rate,
            'ssrc': self.config.ssrc,
            # None when the hot ring is healthy; a string (e.g. foreign-owned
            # shm) when the metrology feed for this channel is broken.
            'ring_error': self.ring_error,
            **stats
        }
    
    def is_healthy(self, timeout_sec: float = 30.0) -> bool:
        """Check if recorder is receiving data."""
        if self.state != StreamRecorderState.RECORDING:
            return False
        
        if self.last_sample_time == 0:
            return False
        
        return (time.time() - self.last_sample_time) < timeout_sec
    
    def get_silence_duration(self) -> float:
        """Get seconds since last sample received."""
        if self.last_sample_time == 0:
            return float('inf')
        return time.time() - self.last_sample_time
    
    def get_quality(self) -> Optional[StreamQuality]:
        """Get current stream quality metrics."""
        if self.stream:
            return self.stream.get_quality()
        return self.last_quality
