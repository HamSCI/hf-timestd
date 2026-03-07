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
import time
import threading
from pathlib import Path
from typing import Dict, Any, Optional, Callable
from dataclasses import dataclass
from enum import Enum

from ka9q import RadiodStream, ChannelInfo, StreamQuality, ManagedStream, RadiodControl

# NOTE (2026-02-03): Bootstrap functionality migrated into MetrologyEngine.
# The recorder now always archives immediately. MetrologyEngine's fusion_state
# handles timing lock internally using wider search windows until locked.

logger = logging.getLogger(__name__)


class RobustManagedStream:
    """
    Drop-in replacement for ka9q.ManagedStream that supports explicit encoding (F32).
    
    The standard ManagedStream (as of ka9q-python 3.2.2) does not support the 'encoding' 
    parameter, defaulting to S16. This causes duplicates when the client enforces F32.
    This class implements the same auto-recovery logic but passes 'encoding' to ensure_channel.
    """
    def __init__(self, control: RadiodControl, frequency_hz: float, preset: str = 'iq',
                 sample_rate: int = 24000, agc_enable: int = 0, gain: float = 0.0,
                 encoding: int = 4, # F32 default
                 destination: Optional[str] = None, ssrc: Optional[int] = None,
                 on_samples=None, on_stream_dropped=None, on_stream_restored=None,
                 drop_timeout_sec: float = 3.0, restore_interval_sec: float = 1.0,
                 samples_per_packet: int = 320, resequence_buffer_size: int = 64,
                 low_edge: Optional[float] = None, high_edge: Optional[float] = None):
        self.control = control
        self.config = {
            'frequency_hz': frequency_hz,
            'preset': preset,
            'sample_rate': sample_rate,
            'agc_enable': agc_enable,
            'gain': gain,
            'encoding': encoding,
            'destination': destination,
            'ssrc': ssrc,
            'low_edge': low_edge,
            'high_edge': high_edge,
        }
        self.callbacks = {
            'on_samples': on_samples,
            'on_stream_dropped': on_stream_dropped,
            'on_stream_restored': on_stream_restored
        }
        self.params = {
            'drop_timeout_sec': drop_timeout_sec,
            'restore_interval_sec': restore_interval_sec,
            'samples_per_packet': samples_per_packet,
            'resequence_buffer_size': resequence_buffer_size
        }
        
        self.stream = None
        self.channel_info = None
        self._running = False
        self._monitor_thread = None
        self._lock = threading.RLock()

    def start(self) -> Optional[ChannelInfo]:
        """Start the stream and monitoring."""
        with self._lock:
            self._running = True
            
            # Initial setup
            if not self._ensure_stream():
                # If failed, background thread will retry
                pass
            
            # self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
            # self._monitor_thread.start()
            logger.info("RobustManagedStream: Monitor loop disabled by manual override")
            
            return self.channel_info

    def stop(self):
        """Stop the stream."""
        with self._lock:
            self._running = False
        
        if self._monitor_thread:
            self._monitor_thread.join(timeout=2.0)
            
        if self.stream:
            self.stream.stop()

    def _ensure_stream(self) -> bool:
        """Attempt to create/find channel and start RadiodStream."""
        try:
            # explicit ensure_channel with ENCODING
            self.channel_info = self.control.ensure_channel(
                frequency_hz=self.config['frequency_hz'],
                preset=self.config['preset'],
                sample_rate=self.config['sample_rate'],
                agc_enable=self.config['agc_enable'],
                gain=self.config['gain'],
                encoding=self.config['encoding'], # CRITICAL FIX
                destination=self.config['destination'],
                # ssrc=self.config['ssrc'] # Let ka9q decide/find
            )
            
            if self.channel_info:
                # Set filter edges if configured (widens passband for FSK etc.)
                self._set_filter_edges(self.channel_info.ssrc)
                # Start RadiodStream
                self.stream = RadiodStream(
                    channel=self.channel_info,
                    on_samples=self.callbacks['on_samples'],
                    samples_per_packet=self.params['samples_per_packet'],
                    resequence_buffer_size=self.params['resequence_buffer_size']
                )
                self.stream.start()
                
                if self.callbacks['on_stream_restored']:
                    self.callbacks['on_stream_restored'](self.channel_info)
                return True
                
        except Exception as e:
            logger.debug(f"RobustManagedStream: ensure failed: {e}")
            
        return False

    def _set_filter_edges(self, ssrc: int):
        """Send filter edge commands to radiod if configured."""
        low = self.config.get('low_edge')
        high = self.config.get('high_edge')
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
            self.control.send_command(cmdbuffer)
            
            logger.info(f"Set filter edges for SSRC {ssrc}: low={low}, high={high}")
        except Exception as e:
            logger.warning(f"Failed to set filter edges: {e}")

    def get_quality(self) -> Optional[StreamQuality]:
        """Get current stream quality metrics."""
        if self.stream:
            return self.stream.get_quality()
        return None


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
        """
        self.config = config
        self.channel_info = channel_info
        self.get_ntp_status = get_ntp_status
        self._control = control
        self._on_stream_dropped = on_stream_dropped
        self._on_stream_restored = on_stream_restored
        # NOTE (2026-02-03): bootstrap_service parameter kept for API compatibility
        # but is no longer used. MetrologyEngine handles timing lock internally.
        
        # State
        self.state = StreamRecorderState.IDLE
        self._lock = threading.Lock()
        
        # RadiodStream instance (created on start)
        self.stream: Optional[RadiodStream] = None
        
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
        )
        
        self.archive_writer = BinaryArchiveWriter(archive_config)
        
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
            'timeout': 10.0,
            'frequency_tolerance': 1.0,
        }
        
        # Check backend capabilities
        caps = {}
        try:
            if hasattr(self._control, 'get_capabilities'):
                caps = self._control.get_capabilities()
        except Exception as e:
            pass
            
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
        
        logger.info(f"{self.config.description}: Channel ready SSRC {self.channel_info.ssrc:08x} "
                   f"at {self.channel_info.multicast_address}:{getattr(self.channel_info, 'port', 5004)}")
        
        # Create RadiodStream to receive data
        # Stop existing stream if any
        if self.stream:
            try:
                self.stream.stop()
            except Exception:
                pass
        
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
            self.archive_writer.add_timing_snapshot(
                gps_time_ns=gps_time,
                rtp_timesnap=rtp_snap
            )
            logger.info(
                f"{self.config.description}: Seeded timing from channel_info — "
                f"GPS_TIME={gps_time}, RTP_TIMESNAP={rtp_snap}"
            )
        else:
            logger.warning(
                f"{self.config.description}: channel_info missing timing — "
                f"gps_time={gps_time}, rtp_timesnap={rtp_snap}"
            )

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
                        # Recreate channel
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
                    logger.debug(f"{self.config.description}: Discovery failed: {e}")
                    continue
                
                if gps_time is not None and rtp_timesnap is not None:
                    # Only store if rtp_timesnap changed (new status received)
                    if rtp_timesnap != last_captured_rtp:
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
            
            # Stop ManagedStream/RadiodStream (returns final quality/stats)
            if self.stream:
                if hasattr(self.stream, 'get_quality'):
                    final_quality = self.stream.get_quality()
                else:
                    final_quality = self.stream.stop()
                
                if hasattr(self.stream, 'stop') and final_quality is not None:
                    # If it's RadiodStream, stop() already returned final_quality
                    # If it's ManagedStream, stop() returns stats, so we call it after get_quality()
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
            self.archive_writer.close()
            
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
            self.archive_writer.write_samples(
                samples=samples,
                rtp_timestamp=quality.last_rtp_timestamp,
                system_time=system_time,
                gap_samples=batch_gap_samples
            )
            
            self.samples_written += len(samples)

            # Forward to tap callbacks
            with self._tap_lock:
                taps = list(self._tap_callbacks)
            for tap in taps:
                try:
                    tap(samples, quality)
                except Exception as tap_err:
                    logger.debug(f"{self.config.description}: tap callback error: {tap_err}")

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
        """Handle stream drop notification from ManagedStream."""
        logger.warning(f"{self.config.description}: Stream DROPPED - {reason}")
        
        # Forward to external callback if provided
        if self._on_stream_dropped:
            try:
                self._on_stream_dropped(reason)
            except Exception as e:
                logger.error(f"Error in stream_dropped callback: {e}")
    
    def _handle_stream_restored(self, channel: ChannelInfo):
        """Handle stream restoration notification from ManagedStream."""
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
            archive_stats = self.archive_writer.get_stats()
            
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
