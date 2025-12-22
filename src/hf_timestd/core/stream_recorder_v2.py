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
    sample_rate: int = 20000
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
            control: Optional RadiodControl for auto-recovery via ManagedStream.
                    If provided, uses ManagedStream which auto-restores on radiod restart.
            on_stream_dropped: Optional callback when stream drops (ManagedStream only)
            on_stream_restored: Optional callback when stream restores (ManagedStream only)
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
        
        try:
            # Start the pipeline orchestrator
            self.orchestrator.start()
            
            # Create and start stream
            # radiod splits 20ms blocks (400 samples at 20kHz) into 2 packets:
            #   - Small packet: 20 samples (160 bytes), ts_delta=360  
            #   - Large packet: 180 samples (1440 bytes), ts_delta=40
            # The resequencer uses samples_per_packet to predict next_expected_ts,
            # but with variable packet sizes this causes false gap detection.
            # Set to 200 (average of 360+40)/2 to minimize false gaps.
            samples_per_packet = 200  # Average timestamp delta per packet
            
            if self._control:
                # Use ManagedStream for auto-recovery on radiod restart
                # Let ka9q-python handle SSRC allocation and channel management
                # Do NOT pass destination - let radiod use its configured default
                # This ensures consistent SSRC allocation across restarts
                logger.info(f"{self.config.description}: Using ManagedStream (auto-recovery enabled)")
                self.stream = ManagedStream(
                    control=self._control,
                    frequency_hz=self.config.frequency_hz,
                    preset=self.config.preset,
                    sample_rate=self.config.sample_rate,
                    agc_enable=self.config.agc_enable,
                    gain=self.config.gain,
                    # encoding=self.config.encoding, # Not supported by ManagedStream
                    destination=self.config.destination,
                    on_samples=self._handle_samples,
                    on_stream_dropped=self._handle_stream_dropped,
                    on_stream_restored=self._handle_stream_restored,
                    drop_timeout_sec=5.0,
                    restore_interval_sec=1.0,
                    resequence_buffer_size=128
                )
                self.channel_info = self.stream.start()
                if self.channel_info:
                    self.config.ssrc = self.channel_info.ssrc
                    logger.info(f"{self.config.description}: Latched to SSRC {self.channel_info.ssrc:x}")
            else:
                # Use basic RadiodStream (no auto-recovery)
                self.stream = RadiodStream(
                    channel=self.channel_info,
                    on_samples=self._handle_samples,
                    samples_per_packet=samples_per_packet,
                    resequence_buffer_size=128,
                    deliver_interval_packets=20
                )
                self.stream.start()
            
            self.session_start_time = time.time()
            
            with self._lock:
                self.state = StreamRecorderState.RECORDING
            
            logger.info(f"{self.config.description}: Stream recorder started (using RadiodStream)")
            
        except Exception as e:
            logger.error(f"{self.config.description}: Failed to start: {e}", exc_info=True)
            with self._lock:
                self.state = StreamRecorderState.ERROR
    
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
        
        final_quality = None
        
        try:
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
