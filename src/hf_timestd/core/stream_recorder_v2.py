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
                 samples_per_packet: int = 320, resequence_buffer_size: int = 64):
        self.control = control
        self.config = {
            'frequency_hz': frequency_hz,
            'preset': preset,
            'sample_rate': sample_rate,
            'agc_enable': agc_enable,
            'gain': gain,
            'encoding': encoding,
            'destination': destination,
            'ssrc': ssrc
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

    def _monitor_loop(self):
        """Monitor stream health and reconnect."""
        while self._running:
            # Simple keep-alive check via re-ensuring channel existence
            # Real ManagedStream uses packet timeouts.
            # Here we just periodic check if stream stopped?
            # RadiodStream runs on its own thread. 
            # If we want pure robustness, we rely on ensure_channel being idempotent.
            try:
                pass 
            except Exception:
                pass
            time.sleep(self.params['restore_interval_sec'])


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
        """
        self.config = config
        self.channel_info = channel_info
        self.get_ntp_status = get_ntp_status
        self._control = control
        self._on_stream_dropped = on_stream_dropped
        self._on_stream_restored = on_stream_restored
        
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
        
        # Initialize pipeline orchestrator for Phase 1/2/3
        from .pipeline_orchestrator import PipelineOrchestrator, PipelineConfig
        
        pipeline_config = PipelineConfig(
            data_dir=config.output_dir,
            channel_name=config.description,
            frequency_hz=config.frequency_hz,
            sample_rate=config.sample_rate,
            receiver_grid=config.receiver_grid,
            station_config=config.station_config,
            raw_buffer_compression=config.raw_buffer_compression,
            raw_buffer_file_duration_sec=config.raw_buffer_file_duration_sec,
            analysis_latency_sec=config.analysis_latency_sec,
            output_sample_rate=config.output_sample_rate,
            streaming_latency_minutes=config.streaming_latency_minutes,

            compression=config.compression,
            compression_level=config.compression_level,
            use_tiered_storage=config.tiered_storage,
            use_digital_rf=config.use_digital_rf,
        )
        
        self.orchestrator = PipelineOrchestrator(pipeline_config)
        
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
            # Start the pipeline orchestrator
            self.orchestrator.start()
            
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
        """Create channel and start RadiodStream (extracted for retry logic)."""
        # WSPR-RECORDER PATTERN: Use create_channel() instead of ensure_channel()
        # But FIRST check if a matching channel already exists to avoid duplicates
        
        logger.info(f"{self.config.description}: Checking for existing channel at {self.config.frequency_hz/1e6:.3f} MHz")
        
        # Step 1: Discover existing channels
        from ka9q import discover_channels
        
        existing_channels = discover_channels(
            self._control.status_address,
            listen_duration=3.0
        )
        
        # Step 2: Look for matching channel (same freq, preset, sample_rate)
        ssrc = None
        for existing_ssrc, channel_info in existing_channels.items():
            if (abs(channel_info.frequency - self.config.frequency_hz) < 1.0 and
                channel_info.preset.lower() == self.config.preset.lower() and
                channel_info.sample_rate == self.config.sample_rate):
                # Found matching channel - reuse it
                ssrc = existing_ssrc
                self.channel_info = channel_info
                logger.info(f"{self.config.description}: Found existing channel SSRC {ssrc:08x}")
                break
        
        # Step 3: Create channel only if it doesn't exist
        if ssrc is None:
            logger.info(f"{self.config.description}: Creating new channel")
            logger.info(f"  Parameters: preset={self.config.preset}, rate={self.config.sample_rate}, "
                       f"agc={self.config.agc_enable}, gain={self.config.gain}, enc={self.config.encoding}")
            
            ssrc = self._control.create_channel(
                frequency_hz=float(self.config.frequency_hz),
                preset=self.config.preset,
                sample_rate=self.config.sample_rate,
                agc_enable=self.config.agc_enable,
                gain=self.config.gain,
                ssrc=None,  # Let radiod assign SSRC
                encoding=self.config.encoding,
                destination=self.config.destination  # None = use radiod default
            )
            
            if ssrc is None:
                raise RuntimeError("Failed to create channel - radiod returned None")
            
            logger.info(f"{self.config.description}: Channel created with SSRC {ssrc:08x}")
            
            # Discover again to get multicast address
            logger.info(f"{self.config.description}: Discovering channel details...")
            channels = discover_channels(
                self._control.status_address,
                listen_duration=3.0
            )
            
            if ssrc not in channels:
                raise RuntimeError(f"Channel SSRC {ssrc:08x} not found in discovery")
            
            self.channel_info = channels[ssrc]
        
        self.config.ssrc = ssrc
        logger.info(f"{self.config.description}: Using channel - "
                   f"Dest: {self.channel_info.multicast_address}, "
                   f"Enc: {getattr(self.channel_info, 'encoding', 'N/A')}")
        
        # Step 4: Create RadiodStream to receive data
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
            
            # Stop the orchestrator (flushes all phases)
            self.orchestrator.stop()
            
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
            
            # Get system time from quality metrics (GPS-derived from ka9q-python)
            if quality.last_packet_utc:
                try:
                    from datetime import datetime
                    if isinstance(quality.last_packet_utc, str):
                        dt = datetime.fromisoformat(quality.last_packet_utc.replace('Z', '+00:00'))
                        system_time = dt.timestamp()
                    else:
                        system_time = float(quality.last_packet_utc)
                    
                    # CRITICAL FIX (2026-01-09): Clock drift protection
                    # If the RTP-derived time is significantly different from our OS clock,
                    # trust the OS clock instead. This prevents 6-day drifts from breaking fusion.
                    os_now = time.time()
                    if abs(system_time - os_now) > 3600: # 1 hour threshold
                        logger.warning(
                            f"{self.config.description}: RTP Clock Drift Detected! "
                            f"RTP={system_time:.0f}, OS={os_now:.0f}, diff={system_time-os_now:.1f}s. "
                            f"Falling back to OS clock."
                        )
                        system_time = os_now
                        
                except (ValueError, TypeError):
                    system_time = time.time()
            else:
                system_time = time.time()
            
            # Feed to pipeline orchestrator
            # This writes to Phase 1 and queues for Phase 2/3
            self.orchestrator.process_samples(
                samples=samples,
                rtp_timestamp=quality.last_rtp_timestamp,
                system_time=system_time
            )
            
            self.samples_written += len(samples)
            
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
        
        # Forward to external callback if provided
        if self._on_stream_restored:
            try:
                self._on_stream_restored(channel)
            except Exception as e:
                logger.error(f"Error in stream_restored callback: {e}")
    
    def get_stats(self) -> Dict[str, Any]:
        """Get current statistics."""
        with self._lock:
            pipeline_stats = self.orchestrator.get_stats()
            
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
                # Pipeline phase stats
                'phase1_samples': pipeline_stats.get('samples_archived', 0),
                'phase2_minutes': pipeline_stats.get('minutes_analyzed', 0),
                'minutes_written': pipeline_stats.get('minutes_written', 0),
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
