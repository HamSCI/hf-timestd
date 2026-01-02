#!/usr/bin/env python3
"""
Simple Audio Buffer for Web UI Playback

Takes IQ samples, AM demodulates, downsamples to 8 kHz, and writes to
a circular buffer file that the Node.js server can stream to browsers.

Much simpler than the previous radiod-based RTP/multicast approach.
"""

import numpy as np
from scipy import signal
from pathlib import Path
import struct
import logging
import threading
import time

logger = logging.getLogger(__name__)

# Audio parameters
AUDIO_SAMPLE_RATE = 8000  # 8 kHz output
BUFFER_SECONDS = 5  # 5 seconds of circular buffer
BUFFER_SAMPLES = AUDIO_SAMPLE_RATE * BUFFER_SECONDS


class AudioBuffer:
    """
    Circular audio buffer for a single channel.
    
    Writes AM-demodulated, downsampled audio to a memory-mapped-style file
    that the web server can read and stream.
    """
    
    def __init__(self, channel_name: str, data_root: str, input_sample_rate: int):
        self.channel_name = channel_name
        self.input_sample_rate = input_sample_rate
        self.output_sample_rate = AUDIO_SAMPLE_RATE
        
        # Resampling ratio
        self.downsample_ratio = input_sample_rate // AUDIO_SAMPLE_RATE
        
        # Buffer file path
        self.buffer_dir = Path(data_root) / "audio_buffers"
        self.buffer_dir.mkdir(parents=True, exist_ok=True)
        self.buffer_file = self.buffer_dir / f"{channel_name}.pcm"
        self.meta_file = self.buffer_dir / f"{channel_name}.meta"
        
        # Circular buffer state
        self.write_pos = 0
        self.buffer = np.zeros(BUFFER_SAMPLES, dtype=np.int16)
        self._lock = threading.Lock()
        
        # Initialize files
        self._init_buffer_file()
        
        logger.info(f"{channel_name}: AudioBuffer initialized @ {AUDIO_SAMPLE_RATE} Hz (input: {input_sample_rate} Hz)")
    
    def _init_buffer_file(self):
        """Initialize the buffer file with zeros."""
        with open(self.buffer_file, 'wb') as f:
            self.buffer.tofile(f)
        self._write_meta()
    
    def _write_meta(self):
        """Write metadata (write position, sample rate)."""
        with open(self.meta_file, 'wb') as f:
            # Format: write_pos (uint32), sample_rate (uint32), buffer_samples (uint32), timestamp (float64)
            f.write(struct.pack('<IIId', self.write_pos, self.output_sample_rate, BUFFER_SAMPLES, time.time()))
    
    def write_iq(self, iq_samples: np.ndarray):
        """
        Process IQ samples and write to audio buffer.
        
        Args:
            iq_samples: Complex IQ samples at input_sample_rate
        """
        if len(iq_samples) == 0:
            return
            
        # Clean inputs (handle NaNs/Infs from gaps)
        iq_samples = np.nan_to_num(iq_samples, nan=0.0, posinf=0.0, neginf=0.0)
        
        # AM demodulation (envelope detection)
        audio = np.abs(iq_samples).astype(np.float32)
        
        # Remove DC offset
        audio = audio - np.mean(audio)
        
        # Normalize to use full 16-bit range
        max_val = np.max(np.abs(audio))
        if max_val > 1e-6:  # Prevent divide by zero on silence
            audio = audio / max_val * 32000
        
        # Dynamic downsampling using polyphase filter
        # Calculate rational resampling ratio: input_rate / output_rate = down / up
        gcd_val = np.gcd(self.input_sample_rate, self.output_sample_rate)
        up = int(self.output_sample_rate // gcd_val)
        down = int(self.input_sample_rate // gcd_val)

        audio_8k = signal.resample_poly(audio, up, down)
        
        # Safe cast to int16 (handle any residual overflows)
        audio_int16 = np.clip(np.nan_to_num(audio_8k), -32767, 32767).astype(np.int16)
        
        # Write to circular buffer
        with self._lock:
            n_samples = len(audio_int16)
            
            # Handle wraparound
            space_to_end = BUFFER_SAMPLES - self.write_pos
            if n_samples <= space_to_end:
                self.buffer[self.write_pos:self.write_pos + n_samples] = audio_int16
            else:
                # Split write
                self.buffer[self.write_pos:] = audio_int16[:space_to_end]
                self.buffer[:n_samples - space_to_end] = audio_int16[space_to_end:]
            
            self.write_pos = (self.write_pos + n_samples) % BUFFER_SAMPLES
            
            # Write to file
            with open(self.buffer_file, 'r+b') as f:
                self.buffer.tofile(f)
            self._write_meta()


class AudioBufferManager:
    """
    Manages audio buffers for all channels.
    """
    
    def __init__(self, data_root: str, sample_rate: int):
        self.data_root = data_root
        self.sample_rate = sample_rate
        self.buffers: dict[str, AudioBuffer] = {}
        self._lock = threading.Lock()
        
        logger.info(f"AudioBufferManager initialized, data_root={data_root}")
    
    def get_buffer(self, channel_name: str) -> AudioBuffer:
        """Get or create audio buffer for a channel."""
        with self._lock:
            if channel_name not in self.buffers:
                self.buffers[channel_name] = AudioBuffer(
                    channel_name, self.data_root, self.sample_rate
                )
            return self.buffers[channel_name]
    
    def write_iq(self, channel_name: str, iq_samples: np.ndarray):
        """Write IQ samples to a channel's audio buffer."""
        buf = self.get_buffer(channel_name)
        buf.write_iq(iq_samples)
