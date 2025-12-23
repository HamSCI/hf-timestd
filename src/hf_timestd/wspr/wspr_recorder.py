#!/usr/bin/env python3
"""
WSPR Recorder - Simple Application Recorder

A straightforward recorder for WSPR signals using the generic recording
infrastructure. Unlike hf-timestd (which needs startup tone detection), WSPR
recording is simpler:

- No startup phase needed
- 2-minute segments aligned to even minute boundaries  
- Output: 16-bit mono WAV files at 12 kHz
- Ready for wsprd decoder

Architecture:
    WsprRecorder → RecordingSession → WsprWAVWriter → WAV files

Usage:
    config = WsprConfig(
        ssrc=14095600,
        frequency_hz=14095600,
        output_dir=Path('/tmp/wspr'),
    )
    recorder = WsprRecorder(config, rtp_receiver)
    recorder.start()
"""

import logging
import time
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, Callable
from enum import Enum

from ka9q import ChannelInfo

from ..core.recording_session import RecordingSession, SessionConfig, SegmentInfo
from ..core.rtp_receiver_DEPRECATED import RTPReceiver
from .wspr_wav_writer import WsprWAVWriter

logger = logging.getLogger(__name__)


class WsprState(Enum):
    """WSPR recorder states"""
    IDLE = "idle"
    RECORDING = "recording"
    STOPPED = "stopped"


@dataclass
class WsprConfig:
    """Configuration for WSPR recorder"""
    # Channel identification
    ssrc: int                           # RTP SSRC identifier
    frequency_hz: float                 # WSPR dial frequency (Hz)
    
    # Output
    output_dir: Path = field(default_factory=lambda: Path('/tmp/wspr'))
    
    # Audio parameters (defaults match wsprd requirements)
    sample_rate: int = 12000            # Must be 12 kHz for wsprd
    segment_duration: float = 120.0     # 2 minutes for WSPR cycle
    
    # Processing options
    use_magnitude: bool = False         # False = use real part (USB demod)
    
    # Channel description (for logging)
    description: str = ""
    
    def __post_init__(self):
        if not self.description:
            self.description = f"WSPR {self.frequency_hz/1e6:.4f} MHz"
        if isinstance(self.output_dir, str):
            self.output_dir = Path(self.output_dir)


class WsprRecorder:
    """
    WSPR-specific recorder using generic recording infrastructure.
    
    This is a simple recorder that:
    1. Receives RTP audio stream from radiod
    2. Segments into 2-minute chunks aligned to even minutes
    3. Writes WAV files compatible with wsprd decoder
    
    No startup phase or tone detection needed (unlike hf-timestd).
    
    Example:
        config = WsprConfig(
            ssrc=14095600,
            frequency_hz=14095600,
            output_dir=Path('/tmp/wspr'),
        )
        recorder = WsprRecorder(config, rtp_receiver)
        recorder.start()
        
        # Later...
        recorder.stop()
    """
    
    def __init__(
        self,
        config: WsprConfig,
        rtp_receiver: RTPReceiver,
        channel_info: Optional[ChannelInfo] = None,
        on_file_complete: Optional[Callable[[Path], None]] = None,
    ):
        """
        Initialize WSPR recorder.
        
        Args:
            config: WSPR configuration
            rtp_receiver: Shared RTP receiver instance
            channel_info: Optional timing info from radiod (for GPS timestamps)
            on_file_complete: Callback when WAV file is written (path passed)
        """
        self.config = config
        self.rtp_receiver = rtp_receiver
        self.channel_info = channel_info
        self.on_file_complete = on_file_complete
        
        self.state = WsprState.IDLE
        
        # Create WAV writer
        self.writer = WsprWAVWriter(
            output_dir=config.output_dir,
            frequency_hz=config.frequency_hz,
            sample_rate=config.sample_rate,
            description=config.description,
            use_magnitude=config.use_magnitude,
        )
        
        # Create recording session with 2-minute segments
        session_config = SessionConfig(
            ssrc=config.ssrc,
            sample_rate=config.sample_rate,
            description=config.description,
            segment_duration_sec=config.segment_duration,
            align_to_boundary=True,
            resequencer_buffer_size=64,
            samples_per_packet=240,  # 240 samples @ 12 kHz = 20ms packets
        )
        
        self.session = RecordingSession(
            config=session_config,
            rtp_receiver=rtp_receiver,
            writer=self.writer,
            channel_info=channel_info,
            on_segment_complete=self._on_segment_complete,
        )
        
        logger.info(f"{config.description}: WsprRecorder initialized")
        logger.info(f"{config.description}: SSRC={config.ssrc}, output={config.output_dir}")
    
    def start(self):
        """Start recording"""
        if self.state != WsprState.IDLE:
            logger.warning(f"{self.config.description}: Cannot start - state is {self.state}")
            return
        
        logger.info(f"{self.config.description}: Starting WSPR recording")
        self.session.start()
        self.state = WsprState.RECORDING
    
    def stop(self):
        """Stop recording gracefully"""
        if self.state != WsprState.RECORDING:
            logger.warning(f"{self.config.description}: Cannot stop - state is {self.state}")
            return
        
        logger.info(f"{self.config.description}: Stopping WSPR recording")
        self.session.stop()
        self.state = WsprState.STOPPED
    
    def _on_segment_complete(self, segment_info: SegmentInfo, result: Any):
        """Called when a 2-minute segment completes"""
        if result and self.on_file_complete:
            try:
                self.on_file_complete(result)
            except Exception as e:
                logger.error(f"{self.config.description}: File complete callback error: {e}")
    
    def update_channel_info(self, channel_info: ChannelInfo):
        """Update timing info from radiod status stream"""
        self.channel_info = channel_info
        self.session.update_channel_info(channel_info)
    
    def get_state(self) -> WsprState:
        """Get current recorder state"""
        return self.state
    
    def get_stats(self) -> Dict[str, Any]:
        """Get recorder statistics"""
        writer_stats = self.writer.get_stats()
        session_stats = self.session.get_metrics()
        
        return {
            'state': self.state.value,
            'frequency_hz': self.config.frequency_hz,
            'ssrc': self.config.ssrc,
            **writer_stats,
            **session_stats,
        }


def create_wspr_recorder(
    frequency_hz: float,
    rtp_receiver: RTPReceiver,
    output_dir: Path,
    ssrc: Optional[int] = None,
    channel_info: Optional[ChannelInfo] = None,
    on_file_complete: Optional[Callable[[Path], None]] = None,
) -> WsprRecorder:
    """
    Factory function to create a WSPR recorder with sensible defaults.
    
    Args:
        frequency_hz: WSPR dial frequency (e.g., 14095600 for 20m)
        rtp_receiver: RTP receiver instance
        output_dir: Directory for WAV files
        ssrc: Optional SSRC (defaults to frequency in Hz)
        channel_info: Optional timing info from radiod
        on_file_complete: Optional callback when file is written
    
    Returns:
        Configured WsprRecorder instance
    """
    if ssrc is None:
        ssrc = int(frequency_hz)
    
    config = WsprConfig(
        ssrc=ssrc,
        frequency_hz=frequency_hz,
        output_dir=output_dir,
    )
    
    return WsprRecorder(
        config=config,
        rtp_receiver=rtp_receiver,
        channel_info=channel_info,
        on_file_complete=on_file_complete,
    )
