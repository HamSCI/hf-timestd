#!/usr/bin/env python3
"""
WSPR WAV Writer - SegmentWriter Implementation

Implements the SegmentWriter protocol for WSPR-compatible WAV files.
Outputs 16-bit mono WAV files at 12 kHz for use with wsprd decoder.

Architecture:
    RecordingSession (segmentation) → WsprWAVWriter (storage) → WAV files

The writer handles:
- IQ to real audio conversion (takes real part or magnitude)
- 16-bit PCM encoding
- wsprd-compatible filename format
- Gap filling with silence
"""

import numpy as np
import logging
import wave
import time
import threading
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from dataclasses import dataclass

from ..core.recording_session import SegmentWriter, SegmentInfo
from ..core.packet_resequencer import GapInfo

logger = logging.getLogger(__name__)


@dataclass
class WsprGapRecord:
    """Record of a gap filled with silence"""
    sample_index: int       # Sample index where gap starts
    samples_filled: int     # Number of silent samples inserted


class WsprWAVWriter:
    """
    WSPR-specific WAV writer implementing SegmentWriter protocol.
    
    Writes WAV files compatible with wsprd decoder:
    - 16-bit signed PCM
    - Mono (single channel)
    - 12 kHz sample rate
    - 2-minute segments aligned to even minute boundaries
    
    Filename format: YYMMDD_HHMM_{freq_hz}_usb.wav
    Example: 251130_1200_14095600_usb.wav
    """
    
    def __init__(
        self,
        output_dir: Path,
        frequency_hz: float,
        sample_rate: int = 12000,
        description: str = "",
        use_magnitude: bool = False,
    ):
        """
        Initialize WSPR WAV writer.
        
        Args:
            output_dir: Directory for WAV files
            frequency_hz: WSPR dial frequency (Hz)
            sample_rate: Sample rate (Hz, should be 12000 for WSPR)
            description: Channel description for logging
            use_magnitude: If True, use magnitude of IQ; if False, use real part
        """
        self.output_dir = Path(output_dir)
        self.frequency_hz = frequency_hz
        self.sample_rate = sample_rate
        self.description = description or f"WSPR {frequency_hz/1e6:.4f} MHz"
        self.use_magnitude = use_magnitude
        
        # Create output directory
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        # Thread safety
        self._lock = threading.Lock()
        
        # Current segment state
        self._segment_samples: List[np.ndarray] = []
        self._segment_gaps: List[WsprGapRecord] = []
        self._segment_info: Optional[SegmentInfo] = None
        self._segment_start_time: Optional[float] = None
        self._total_samples_in_segment: int = 0
        
        # Statistics
        self.segments_written = 0
        self.total_samples_written = 0
        self.last_file_written: Optional[Path] = None
        
        logger.info(f"{self.description}: WsprWAVWriter initialized")
        logger.info(f"{self.description}: Output dir: {self.output_dir}")
        logger.info(f"{self.description}: Sample rate: {sample_rate} Hz")
    
    # === SegmentWriter Protocol Implementation ===
    
    def start_segment(self, segment_info: SegmentInfo, metadata: Dict[str, Any]) -> None:
        """Called when a new segment begins"""
        with self._lock:
            # Reset segment state
            self._segment_samples = []
            self._segment_gaps = []
            self._segment_info = segment_info
            self._segment_start_time = segment_info.wallclock_start or time.time()
            self._total_samples_in_segment = 0
            
            logger.debug(
                f"{self.description}: Segment {segment_info.segment_id} started "
                f"at wallclock {self._segment_start_time:.3f}"
            )
    
    def write_samples(
        self,
        samples: np.ndarray,
        rtp_timestamp: int,
        gap_info: Optional[GapInfo] = None
    ) -> None:
        """Called for each batch of samples"""
        with self._lock:
            if self._segment_info is None:
                logger.warning(f"{self.description}: write_samples called without active segment")
                return
            
            # Record gap if present
            if gap_info and gap_info.gap_samples > 0:
                gap_record = WsprGapRecord(
                    sample_index=self._total_samples_in_segment,
                    samples_filled=gap_info.gap_samples
                )
                self._segment_gaps.append(gap_record)
            
            # Convert complex IQ to real audio
            # radiod USB mode already gives us audio-like samples (real part is the signal)
            if np.iscomplexobj(samples):
                if self.use_magnitude:
                    # Use magnitude (envelope detection)
                    audio_samples = np.abs(samples).astype(np.float32)
                else:
                    # Use real part (USB demod output)
                    audio_samples = samples.real.astype(np.float32)
            else:
                audio_samples = samples.astype(np.float32)
            
            # Accumulate samples
            self._segment_samples.append(audio_samples)
            self._total_samples_in_segment += len(audio_samples)
    
    def finish_segment(self, segment_info: SegmentInfo) -> Optional[Path]:
        """Called when segment completes. Returns file path."""
        with self._lock:
            if not self._segment_samples:
                logger.warning(f"{self.description}: finish_segment called with no samples")
                return None
            
            return self._write_wav_file(segment_info)
    
    # === WSPR-Specific Methods ===
    
    def _write_wav_file(self, segment_info: SegmentInfo) -> Path:
        """Write current segment to WAV file"""
        # Concatenate all samples
        audio_data = np.concatenate(self._segment_samples)
        
        # Normalize and convert to 16-bit PCM
        # Find peak and normalize to avoid clipping
        peak = np.max(np.abs(audio_data))
        if peak > 0:
            # Normalize to 90% of full scale to leave headroom
            audio_data = audio_data / peak * 0.9
        
        # Convert to 16-bit signed integers
        audio_int16 = (audio_data * 32767).astype(np.int16)
        
        # Determine filename based on segment start time
        # wsprd expects: YYMMDD_HHMM_freq_usb.wav
        start_dt = datetime.fromtimestamp(self._segment_start_time, tz=timezone.utc)
        
        # Round to even 2-minute boundary for WSPR
        minute = (start_dt.minute // 2) * 2
        start_dt = start_dt.replace(minute=minute, second=0, microsecond=0)
        
        date_str = start_dt.strftime("%y%m%d")
        time_str = start_dt.strftime("%H%M")
        freq_str = f"{int(self.frequency_hz)}"
        
        filename = f"{date_str}_{time_str}_{freq_str}_usb.wav"
        file_path = self.output_dir / filename
        
        # Write WAV file
        with wave.open(str(file_path), 'wb') as wav_file:
            wav_file.setnchannels(1)  # Mono
            wav_file.setsampwidth(2)  # 16-bit = 2 bytes
            wav_file.setframerate(self.sample_rate)
            wav_file.writeframes(audio_int16.tobytes())
        
        # Calculate statistics
        total_gaps = len(self._segment_gaps)
        total_gap_samples = sum(g.samples_filled for g in self._segment_gaps)
        duration_sec = len(audio_int16) / self.sample_rate
        completeness_pct = 100.0 * (len(audio_int16) - total_gap_samples) / len(audio_int16) if len(audio_int16) > 0 else 0.0
        
        # Update statistics
        self.segments_written += 1
        self.total_samples_written += len(audio_int16)
        self.last_file_written = file_path
        
        # Log completion
        file_size_kb = file_path.stat().st_size / 1024
        logger.info(
            f"{self.description}: Wrote {filename} "
            f"({duration_sec:.1f}s, {file_size_kb:.1f} KB, "
            f"{completeness_pct:.1f}% complete, {total_gaps} gaps)"
        )
        
        return file_path
    
    def get_stats(self) -> Dict[str, Any]:
        """Get current writer statistics"""
        with self._lock:
            return {
                'segments_written': self.segments_written,
                'total_samples_written': self.total_samples_written,
                'last_file_written': str(self.last_file_written) if self.last_file_written else None,
                'frequency_hz': self.frequency_hz,
                'sample_rate': self.sample_rate,
            }
