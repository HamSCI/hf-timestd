#!/usr/bin/env python3
"""
Pipeline Recorder - Two-Phase Architecture Integration

This module provides the pipeline recorder interface for the
two-phase time-aligned data pipeline:

- Phase 1: Immutable Raw Archive (20 kHz IQ binary)
- Phase 2: Analytical Engine (Clock Offset Series, D_clock)

Usage:
------
    config = PipelineRecorderConfig(
        ssrc=20100,
        frequency_hz=10e6,
        sample_rate=20000,
        output_dir=Path('/data/timestd'),
        receiver_grid='EM38ww',
        station_config={'callsign': 'W3PM', ...}
    )
    
    recorder = PipelineRecorder(config, rtp_receiver)
    recorder.start()
    # ... runs until stop() called
    recorder.stop()
"""

import numpy as np
import logging
import time
import threading
from pathlib import Path
from typing import Optional, Dict, Any, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from ka9q import ChannelInfo, RTPHeader

from ..core.rtp_receiver_DEPRECATED import RTPReceiver
from ..core.packet_resequencer import PacketResequencer, RTPPacket, GapInfo

logger = logging.getLogger(__name__)


class PipelineRecorderState(Enum):
    """Pipeline recorder states"""
    IDLE = "idle"
    STARTING = "starting"
    RECORDING = "recording"
    STOPPING = "stopping"
    ERROR = "error"


@dataclass
class PipelineRecorderConfig:
    """
    Configuration for pipeline recorder.
    
    Configuration for the two-phase pipeline recorder.
    """
    # Channel identification
    ssrc: int
    frequency_hz: float
    sample_rate: int = 20000
    description: str = ""
    
    # Output directories
    output_dir: Path = Path("data")
    
    # Receiver location (required for propagation calculation)
    receiver_grid: str = ""
    
    # Station metadata
    station_config: Dict[str, Any] = field(default_factory=dict)
    
    # RTP packet timing
    blocktime_ms: float = 20.0  # ka9q-radio default is 20ms = 400 samples @ 20kHz
    max_gap_seconds: float = 60.0
    encoding: str = 'float'  # 'float' (complex64) or 'int16' (complex int16)
    
    # Phase 1 settings
    raw_buffer_compression: str = 'gzip'
    raw_buffer_file_duration_sec: int = 3600
    storage_quota_percent: float = 80.0  # Max disk usage percentage (from config storage_quota)
    use_tiered_storage: bool = False  # Use /dev/shm hot buffer with disk cold storage
    
    # Phase 2 settings
    enable_analysis: bool = True
    analysis_latency_sec: int = 120
    
    # Phase 3 settings
    enable_products: bool = True
    output_sample_rate: int = 10
    streaming_latency_minutes: int = 2
    
    @property
    def samples_per_packet(self) -> int:
        """Calculate samples per RTP packet"""
        return int(self.sample_rate * self.blocktime_ms / 1000)
    
    @property
    def max_gap_samples(self) -> int:
        """Calculate maximum gap samples"""
        return int(self.sample_rate * self.max_gap_seconds)
    
    def __post_init__(self):
        self.output_dir = Path(self.output_dir)
        
        # Derive channel name from description or frequency
        if not self.description:
            freq_mhz = self.frequency_hz / 1e6
            self.description = f"WWV_{freq_mhz:.1f}MHz"


class PipelineRecorder:
    """
    HF Time Standard recorder using the two-phase architecture.
    
    1. Writes raw IQ to Phase 1 immutable archive
    2. Queues data for Phase 2 analysis
    
    The architecture ensures:
    - Raw data is never modified
    - Analysis can be re-run with improved algorithms
    - Products can be regenerated without re-recording
    """
    
    def __init__(
        self,
        config: PipelineRecorderConfig,
        rtp_receiver: RTPReceiver,
        get_ntp_status: Optional[Callable[[], Dict[str, Any]]] = None,
        channel_info: Optional[ChannelInfo] = None
    ):
        """
        Initialize pipeline recorder.
        
        Args:
            config: PipelineRecorderConfig
            rtp_receiver: Shared RTP receiver instance
            get_ntp_status: Callable for NTP status
            channel_info: Optional timing info from radiod
        """
        self.config = config
        self.rtp_receiver = rtp_receiver
        self.get_ntp_status = get_ntp_status
        self.channel_info = channel_info
        
        # State
        self.state = PipelineRecorderState.IDLE
        self._lock = threading.Lock()
        
        # Packet resequencer
        self.resequencer = PacketResequencer(
            buffer_size=64,
            samples_per_packet=config.samples_per_packet,
            max_gap_samples=config.max_gap_samples
        )
        
        # Initialize pipeline orchestrator
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
            storage_quota_percent=config.storage_quota_percent,
            use_tiered_storage=config.use_tiered_storage,
            analysis_latency_sec=config.analysis_latency_sec,
            output_sample_rate=config.output_sample_rate,
            streaming_latency_minutes=config.streaming_latency_minutes
        )
        
        self.orchestrator = PipelineOrchestrator(pipeline_config)
        
        # Statistics
        self.packets_received = 0
        self.samples_written = 0
        self.last_packet_time: float = 0.0
        self.session_start_time: Optional[float] = None
        
        # Time reference (for compatibility with legacy code)
        self.time_snap = None  # Not used in new architecture
        
        logger.info(f"PipelineRecorder initialized: {config.description}")
        logger.info(f"  SSRC: {config.ssrc}")
        logger.info(f"  Sample rate: {config.sample_rate} Hz")
        logger.info(f"  Output: {config.output_dir}")
        logger.info(f"  Receiver grid: {config.receiver_grid}")
    
    def start(self):
        """Start the pipeline recorder."""
        with self._lock:
            if self.state != PipelineRecorderState.IDLE:
                logger.warning(f"Cannot start in state {self.state}")
                return
            
            self.state = PipelineRecorderState.STARTING
        
        # Start the orchestrator
        self.orchestrator.start()
        
        # Register RTP callback with expected payload configuration
        self.rtp_receiver.register_callback(
            ssrc=self.config.ssrc,
            callback=self._handle_rtp_packet,
            channel_info=self.channel_info,
            expected_sample_rate=self.config.sample_rate,
            expected_encoding=self.config.encoding,
            blocktime_ms=self.config.blocktime_ms
        )
        
        self.session_start_time = time.time()
        
        with self._lock:
            self.state = PipelineRecorderState.RECORDING
        
        logger.info(f"{self.config.description}: Pipeline recorder started")
    
    def stop(self):
        """Stop the pipeline recorder gracefully."""
        with self._lock:
            if self.state == PipelineRecorderState.IDLE:
                return
            
            self.state = PipelineRecorderState.STOPPING
        
        # Unregister callback
        self.rtp_receiver.unregister_callback(self.config.ssrc)
        
        # Stop the orchestrator (flushes all phases)
        self.orchestrator.stop()
        
        with self._lock:
            self.state = PipelineRecorderState.IDLE
        
        logger.info(f"{self.config.description}: Pipeline recorder stopped")
        logger.info(f"  Packets received: {self.packets_received}")
        logger.info(f"  Samples written: {self.samples_written}")
    
    def flush(self):
        """Flush any buffered data."""
        # The orchestrator handles flushing internally
        pass
    
    def _handle_rtp_packet(
        self,
        header: RTPHeader,
        payload: bytes,
        wallclock: Optional[float] = None
    ):
        """
        Handle incoming RTP packet.
        
        Args:
            header: Parsed RTP header
            payload: Raw payload bytes
            wallclock: Transport timing from radiod
        """
        try:
            with self._lock:
                if self.state != PipelineRecorderState.RECORDING:
                    return
                
                self.packets_received += 1
                self.last_packet_time = time.time()
            
            # Decode payload to IQ samples
            iq_samples = self._decode_payload(header.payload_type, payload)
            if iq_samples is None:
                return
            
            # Resequence
            rtp_pkt = RTPPacket(
                sequence=header.sequence,
                timestamp=header.timestamp,
                ssrc=header.ssrc,
                samples=iq_samples
            )
            
            output_samples, gap_info = self.resequencer.process_packet(rtp_pkt)
            
            if output_samples is None:
                return  # Still buffering
            
            # Get system time
            system_time = wallclock if wallclock else time.time()
            
            # Feed to pipeline orchestrator
            # This writes to Phase 1 and queues for Phase 2/3
            self.orchestrator.process_samples(
                samples=output_samples,
                rtp_timestamp=header.timestamp,
                system_time=system_time
            )
            
            self.samples_written += len(output_samples)
            
            # Log gap if present
            if gap_info and gap_info.gap_samples > 0:
                logger.debug(
                    f"{self.config.description}: Gap detected - "
                    f"{gap_info.gap_samples} samples ({gap_info.gap_packets} packets)"
                )
                
        except Exception as e:
            logger.error(f"{self.config.description}: Packet processing error: {e}", exc_info=True)
    
    def _decode_payload(self, payload_type: int, payload: bytes) -> Optional[np.ndarray]:
        """Decode RTP payload to complex IQ samples."""
        try:
            if payload_type in (120, 97):
                # int16 IQ format
                if len(payload) % 4 != 0:
                    return None
                samples_int16 = np.frombuffer(payload, dtype=np.int16)
                samples = samples_int16.astype(np.float32) / 32768.0
                
            elif payload_type == 11:
                # float32 IQ format
                if len(payload) % 8 != 0:
                    return None
                samples = np.frombuffer(payload, dtype=np.float32)
                
            else:
                # Unknown - try float32
                if len(payload) % 8 != 0:
                    return None
                samples = np.frombuffer(payload, dtype=np.float32)
            
            # Convert interleaved I/Q to complex
            i_samples = samples[0::2]
            q_samples = samples[1::2]
            return (i_samples + 1j * q_samples).astype(np.complex64)
            
        except Exception as e:
            logger.warning(f"Payload decode error: {e}")
            return None
    
    def get_stats(self) -> Dict[str, Any]:
        """Get current statistics."""
        with self._lock:
            pipeline_stats = self.orchestrator.get_stats()
            
            uptime = 0.0
            if self.session_start_time:
                uptime = time.time() - self.session_start_time
            
            return {
                'state': self.state.value,
                'packets_received': self.packets_received,
                'samples_written': self.samples_written,
                'uptime_seconds': uptime,
                'last_packet_time': self.last_packet_time,
                # Pipeline phase stats
                'phase1_samples': pipeline_stats.get('samples_archived', 0),
                'phase2_minutes': pipeline_stats.get('minutes_analyzed', 0),
                'phase3_products': pipeline_stats.get('products_generated', 0),
                # Detailed stats
                'pipeline': pipeline_stats
            }
    
    def get_status(self) -> Dict[str, Any]:
        """Get detailed status for web-ui."""
        stats = self.get_stats()
        
        last_packet_iso = None
        if self.last_packet_time > 0:
            last_packet_iso = datetime.fromtimestamp(
                self.last_packet_time, timezone.utc
            ).isoformat()
        
        return {
            'description': self.config.description,
            'frequency_hz': self.config.frequency_hz,
            'sample_rate': self.config.sample_rate,
            'state': self.state.value,
            'packets_received': self.packets_received,
            'samples_written': self.samples_written,
            'last_packet_time': last_packet_iso,
            'pipeline_state': stats.get('pipeline', {}).get('state', 'unknown'),
            'architecture': 'two_phase_pipeline',
            # Compatibility fields
            'time_snap_source': 'phase2_analysis',  # D_clock from Phase 2
            'time_snap_confidence': 0.0,  # Set from Phase 2 results
        }
    
    def is_healthy(self, timeout_sec: float = 120.0) -> bool:
        """Check if channel is receiving packets."""
        with self._lock:
            if self.last_packet_time == 0:
                return True  # Never received - give it time
            
            silence = time.time() - self.last_packet_time
            return silence < timeout_sec
    
    def get_silence_duration(self) -> float:
        """Get seconds since last packet."""
        with self._lock:
            if self.last_packet_time == 0:
                return 0.0
            return time.time() - self.last_packet_time
    
    def reset_health(self):
        """Reset health timestamp after channel recreation."""
        with self._lock:
            self.last_packet_time = time.time()
        logger.debug(f"{self.config.description}: Health timer reset")


def create_pipeline_recorder(
    ssrc: int,
    frequency_hz: float,
    output_dir: Path,
    receiver_grid: str,
    station_config: Dict[str, Any],
    rtp_receiver: RTPReceiver,
    sample_rate: int = 20000,
    description: Optional[str] = None
) -> PipelineRecorder:
    """
    Factory function to create a pipeline recorder.
    
    Args:
        ssrc: RTP SSRC identifier
        frequency_hz: Center frequency
        output_dir: Output directory
        receiver_grid: Receiver grid square
        station_config: Station metadata
        rtp_receiver: RTP receiver instance
        sample_rate: Sample rate (default 20000)
        description: Channel description
        
    Returns:
        Configured PipelineRecorder
    """
    config = PipelineRecorderConfig(
        ssrc=ssrc,
        frequency_hz=frequency_hz,
        sample_rate=sample_rate,
        description=description or f"WWV_{frequency_hz/1e6:.1f}MHz",
        output_dir=output_dir,
        receiver_grid=receiver_grid,
        station_config=station_config
    )
    
    return PipelineRecorder(config, rtp_receiver)
