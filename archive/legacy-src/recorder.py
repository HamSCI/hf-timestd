"""
Stream recorder module

Records RTP streams from ka9q-radio using pcmrecord or pure Python implementation.
"""

import subprocess
import logging
import signal
import time
from pathlib import Path
from typing import Optional, Dict
from dataclasses import dataclass

from .discovery import StreamMetadata

logger = logging.getLogger(__name__)


@dataclass
class RecorderConfig:
    """Configuration for a stream recorder"""
    output_dir: Path
    file_length: int = 60  # seconds
    use_jt_naming: bool = True  # K1JT format (YYYYMMDDTHHMMSS)
    compress: bool = True
    compression_format: str = "wavpack"  # or "none"
    pcmrecord_path: str = "pcmrecord"  # Path to pcmrecord binary


class StreamRecorder:
    """
    Records a single RTP stream using ka9q-radio's pcmrecord utility
    
    This is a wrapper around pcmrecord that manages the subprocess and
    provides a clean interface for starting/stopping recording.
    """
    
    def __init__(self, metadata: StreamMetadata, config: RecorderConfig, band_name: str):
        """
        Initialize stream recorder
        
        Args:
            metadata: Stream metadata from discovery
            config: Recorder configuration
            band_name: Name of the band (e.g., "WWV_2_5")
        """
        self.metadata = metadata
        self.config = config
        self.band_name = band_name
        self.process: Optional[subprocess.Popen] = None
        self.output_dir = config.output_dir / band_name
        
    def start(self):
        """Start recording the stream"""
        if self.process is not None:
            logger.warning(f"Recorder for {self.band_name} (SSRC 0x{self.metadata.ssrc:08x}) already running")
            return
        
        # Create output directory
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        # Build pcmrecord command
        cmd = self._build_command()
        
        logger.info(f"Starting recorder for {self.band_name} (SSRC 0x{self.metadata.ssrc:08x})")
        logger.debug(f"Command: {' '.join(cmd)}")
        
        try:
            # Start pcmrecord process
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            
            # Give it a moment to start
            time.sleep(0.5)
            
            # Check if it's still running
            if self.process.poll() is not None:
                stdout, stderr = self.process.communicate()
                logger.error(f"pcmrecord failed to start for {self.band_name}")
                logger.error(f"stdout: {stdout}")
                logger.error(f"stderr: {stderr}")
                self.process = None
                raise RuntimeError(f"pcmrecord failed to start: {stderr}")
            
            logger.info(f"Recorder started for {self.band_name} → {self.output_dir}")
            
        except Exception as e:
            logger.error(f"Error starting recorder for {self.band_name}: {e}")
            self.process = None
            raise
    
    def stop(self, timeout: int = 10):
        """
        Stop recording the stream
        
        Args:
            timeout: Maximum time to wait for graceful shutdown (seconds)
        """
        if self.process is None:
            logger.warning(f"Recorder for {self.band_name} not running")
            return
        
        logger.info(f"Stopping recorder for {self.band_name}")
        
        try:
            # Send SIGTERM for graceful shutdown
            self.process.terminate()
            
            # Wait for process to exit
            try:
                self.process.wait(timeout=timeout)
                logger.info(f"Recorder for {self.band_name} stopped gracefully")
            except subprocess.TimeoutExpired:
                logger.warning(f"Recorder for {self.band_name} did not stop gracefully, killing")
                self.process.kill()
                self.process.wait()
        
        except Exception as e:
            logger.error(f"Error stopping recorder for {self.band_name}: {e}")
        
        finally:
            self.process = None
    
    def is_running(self) -> bool:
        """Check if recorder is currently running"""
        if self.process is None:
            return False
        return self.process.poll() is None
    
    def _build_command(self) -> list:
        """Build pcmrecord command line"""
        cmd = [
            self.config.pcmrecord_path,
            "-d", str(self.output_dir),
        ]
        
        # Add K1JT naming format flag
        if self.config.use_jt_naming:
            cmd.append("-j")
        
        # Add verbose flag for debugging
        cmd.append("-v")
        
        # Add max file length if specified (in seconds)
        if self.config.file_length:
            cmd.extend(["-L", str(self.config.file_length)])
        
        # Add multicast address (just the IP, pcmrecord auto-detects all SSRCs)
        cmd.append(self.metadata.multicast_address)
        
        return cmd


class RecorderManager:
    """Manages multiple stream recorders"""
    
    def __init__(self, config: Dict):
        """
        Initialize recorder manager
        
        Args:
            config: Configuration dictionary
        """
        self.config = config
        self.recorders: Dict[int, StreamRecorder] = {}  # ssrc -> recorder
        
    def start_recorder(self, metadata: StreamMetadata, band_name: str):
        """
        Start a recorder for a specific stream
        
        Args:
            metadata: Stream metadata
            band_name: Name of the band
        """
        if metadata.ssrc in self.recorders:
            logger.warning(f"Recorder for SSRC 0x{metadata.ssrc:08x} already exists")
            return
        
        # Create recorder config
        recorder_config = RecorderConfig(
            output_dir=Path(self.config['recorder']['archive_dir']),
            file_length=self.config['recorder'].get('file_length', 60),
            use_jt_naming=True,
            compress=self.config['recorder'].get('compress', True),
            compression_format=self.config['recorder'].get('compression_format', 'wavpack'),
            pcmrecord_path=self.config['recorder'].get('pcmrecord_path', 'pcmrecord')
        )
        
        # Create and start recorder
        recorder = StreamRecorder(metadata, recorder_config, band_name)
        recorder.start()
        
        self.recorders[metadata.ssrc] = recorder
    
    def stop_recorder(self, ssrc: int):
        """Stop a specific recorder"""
        recorder = self.recorders.get(ssrc)
        if recorder:
            recorder.stop()
            del self.recorders[ssrc]
    
    def stop_all(self):
        """Stop all recorders"""
        logger.info(f"Stopping all {len(self.recorders)} recorders")
        for ssrc in list(self.recorders.keys()):
            self.stop_recorder(ssrc)
    
    def get_status(self) -> Dict[int, Dict]:
        """Get status of all recorders"""
        status = {}
        for ssrc, recorder in self.recorders.items():
            status[ssrc] = {
                'band_name': recorder.band_name,
                'frequency': recorder.metadata.frequency,
                'running': recorder.is_running(),
                'output_dir': str(recorder.output_dir)
            }
        return status


def get_band_name_from_frequency(freq_hz: float, band_mapping: Optional[Dict[float, str]] = None) -> str:
    """
    Determine band name from frequency
    
    Args:
        freq_hz: Frequency in Hz
        band_mapping: Optional custom mapping of frequency to band name
        
    Returns:
        Band name string
    """
    if band_mapping and freq_hz in band_mapping:
        return band_mapping[freq_hz]
    
    # Default mapping for WWV/CHU
    freq_mhz = freq_hz / 1e6
    
    # WWV frequencies
    wwv_freqs = {
        2.5: "WWV_2_5",
        5.0: "WWV_5",
        10.0: "WWV_10",
        15.0: "WWV_15",
        20.0: "WWV_20",
        25.0: "WWV_25",
    }
    
    # CHU frequencies
    chu_freqs = {
        3.33: "CHU_3",
        7.85: "CHU_7",
        14.67: "CHU_14",
    }
    
    # Check WWV frequencies (100 kHz tolerance)
    for wwv_freq, name in wwv_freqs.items():
        if abs(freq_mhz - wwv_freq) < 0.1:
            return name
    
    # Check CHU frequencies (100 kHz tolerance)
    for chu_freq, name in chu_freqs.items():
        if abs(freq_mhz - chu_freq) < 0.1:
            return name
    
    # Fallback to generic name
    return f"FREQ_{freq_mhz:.3f}"

