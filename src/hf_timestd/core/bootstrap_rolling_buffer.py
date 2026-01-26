"""
Bootstrap Rolling Buffer: Continuous IQ Buffer for Bootstrap Acquisition

This module implements a rolling circular buffer that accumulates IQ samples
continuously without assuming minute boundaries. During bootstrap, we don't
know where minutes start - the tones will tell us.

Architecture:
------------
                    ┌─────────────────────────────────────────┐
  RTP Stream ──────►│     Rolling Buffer (~2.5 minutes)       │
                    │  [older samples ... newer samples]      │
                    └─────────────────────────────────────────┘
                                      │
                                      ▼
                    ┌─────────────────────────────────────────┐
                    │   Full-Buffer Cross-Correlation         │
                    │   Search for 1000/1200 Hz tones         │
                    └─────────────────────────────────────────┘
                                      │
                                      ▼
                    ┌─────────────────────────────────────────┐
                    │   TimingBootstrap State Machine         │
                    │   ACQUIRING → CORRELATING → LOCKED      │
                    └─────────────────────────────────────────┘

Key Design Decisions:
--------------------
1. Buffer duration: ~150 seconds (2.5 minutes)
   - Guarantees at least one complete minute is always in buffer
   - Handles ±500ms clock uncertainty during bootstrap
   - Captures all arrivals from earliest (WWV ~5ms) to latest (WWVH ~100ms)

2. RTP-based indexing: All positions are in RTP timestamp space
   - No dependency on system time until lock
   - Handles RTP wraparound (32-bit at 24kHz wraps every ~50 hours)

3. No archiving until lock:
   - During bootstrap, we're searching, not recording
   - Once locked, we know minute boundaries and can archive properly

4. Per-channel buffers:
   - Each SDR channel has its own rolling buffer
   - Cross-channel correlation happens in TimingBootstrap

Usage:
------
    buffer = BootstrapRollingBuffer(
        channel_name="CHU_3330",
        sample_rate=24000,
        buffer_duration_sec=150.0
    )
    
    # Feed samples as they arrive
    buffer.add_samples(iq_samples, rtp_timestamp)
    
    # Periodically search for tones
    if buffer.has_enough_data():
        candidates = buffer.search_for_minute_markers(tone_detector)
        for c in candidates:
            bootstrap.add_candidate(...)
"""

import logging
import numpy as np
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from enum import Enum

logger = logging.getLogger(__name__)

# Constants
DEFAULT_SAMPLE_RATE = 24000
SAMPLES_PER_MINUTE = DEFAULT_SAMPLE_RATE * 60  # 1,440,000

# Buffer duration: 2.5 minutes to guarantee capturing full minute + margins
DEFAULT_BUFFER_DURATION_SEC = 150.0  # 2.5 minutes


@dataclass
class ToneCandidate:
    """A tone candidate found during bootstrap search."""
    rtp_timestamp: int           # RTP sample number at tone onset
    sample_position: int         # Position within search buffer
    station: str                 # Identified station (WWV, WWVH, CHU, BPM)
    tone_frequency_hz: float     # Detected tone frequency (1000 or 1200 Hz)
    correlation_peak: float      # Correlation strength
    snr_db: float               # Signal-to-noise ratio
    confidence: float           # Detection confidence (0-1)
    duration_sec: float         # Detected tone duration


class BootstrapRollingBuffer:
    """
    Rolling circular buffer for bootstrap acquisition.
    
    Maintains a continuous buffer of IQ samples indexed by RTP timestamp.
    Provides methods to search the entire buffer for tone candidates
    without assuming minute boundaries.
    """
    
    def __init__(
        self,
        channel_name: str,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        buffer_duration_sec: float = DEFAULT_BUFFER_DURATION_SEC
    ):
        """
        Initialize the rolling buffer.
        
        Args:
            channel_name: Channel identifier (e.g., "CHU_3330")
            sample_rate: Sample rate in Hz
            buffer_duration_sec: Buffer duration in seconds
        """
        self.channel_name = channel_name
        self.sample_rate = sample_rate
        self.buffer_duration_sec = buffer_duration_sec
        
        # Buffer size in samples
        self.buffer_size = int(buffer_duration_sec * sample_rate)
        
        # Circular buffer for IQ samples (complex64)
        self.buffer = np.zeros(self.buffer_size, dtype=np.complex64)
        
        # RTP timestamp of the OLDEST sample in buffer (buffer[0])
        # None until first samples are added
        self.buffer_start_rtp: Optional[int] = None
        
        # Write position (next sample goes here)
        self.write_pos: int = 0
        
        # Total samples written (may exceed buffer_size due to wraparound)
        self.total_samples_written: int = 0
        
        # Track gaps in RTP sequence
        self.gap_count: int = 0
        self.gap_samples: int = 0
        
        # Last RTP timestamp seen (for continuity checking)
        self.last_rtp: Optional[int] = None
        
        logger.info(f"[BOOTSTRAP_BUFFER] {channel_name}: Initialized rolling buffer "
                   f"({buffer_duration_sec:.1f}s = {self.buffer_size} samples)")
    
    def add_samples(
        self,
        samples: np.ndarray,
        rtp_timestamp: int
    ) -> None:
        """
        Add samples to the rolling buffer.
        
        Args:
            samples: IQ samples (complex64)
            rtp_timestamp: RTP timestamp of first sample
        """
        n_samples = len(samples)
        
        if n_samples == 0:
            return
        
        # Initialize buffer start RTP on first write
        if self.buffer_start_rtp is None:
            self.buffer_start_rtp = rtp_timestamp
            self.last_rtp = rtp_timestamp
            logger.debug(f"[BOOTSTRAP_BUFFER] {self.channel_name}: First samples at RTP={rtp_timestamp}")
        
        # Check for RTP discontinuity
        if self.last_rtp is not None:
            expected_rtp = self.last_rtp + (self.write_pos if self.total_samples_written == 0 
                                           else n_samples)
            # Handle wraparound (32-bit RTP)
            rtp_diff = (rtp_timestamp - self.last_rtp) & 0xFFFFFFFF
            if rtp_diff > 0x7FFFFFFF:
                rtp_diff -= 0x100000000
            
            # Check for gap (more than 1 second of missing samples)
            if rtp_diff > self.sample_rate:
                gap_samples = rtp_diff - n_samples
                self.gap_count += 1
                self.gap_samples += gap_samples
                logger.warning(f"[BOOTSTRAP_BUFFER] {self.channel_name}: RTP gap detected "
                              f"({gap_samples} samples = {gap_samples/self.sample_rate:.2f}s)")
        
        # Write samples to circular buffer
        if n_samples <= self.buffer_size:
            # Samples fit in buffer
            end_pos = self.write_pos + n_samples
            
            if end_pos <= self.buffer_size:
                # No wraparound needed
                self.buffer[self.write_pos:end_pos] = samples
            else:
                # Wraparound
                first_part = self.buffer_size - self.write_pos
                self.buffer[self.write_pos:] = samples[:first_part]
                self.buffer[:end_pos - self.buffer_size] = samples[first_part:]
            
            self.write_pos = end_pos % self.buffer_size
        else:
            # Samples larger than buffer - only keep last buffer_size samples
            self.buffer[:] = samples[-self.buffer_size:]
            self.write_pos = 0
            logger.warning(f"[BOOTSTRAP_BUFFER] {self.channel_name}: Samples larger than buffer, "
                          f"keeping last {self.buffer_size}")
        
        self.total_samples_written += n_samples
        self.last_rtp = rtp_timestamp + n_samples - 1
        
        # Update buffer_start_rtp if buffer has wrapped
        if self.total_samples_written > self.buffer_size:
            # Oldest sample is at write_pos, its RTP is:
            samples_in_buffer = min(self.total_samples_written, self.buffer_size)
            self.buffer_start_rtp = self.last_rtp - samples_in_buffer + 1
    
    def has_enough_data(self, min_duration_sec: float = 65.0) -> bool:
        """
        Check if buffer has enough data for meaningful search.
        
        Args:
            min_duration_sec: Minimum duration required (default: 65s for full minute + margin)
            
        Returns:
            True if buffer has at least min_duration_sec of data
        """
        samples_available = min(self.total_samples_written, self.buffer_size)
        duration_available = samples_available / self.sample_rate
        return duration_available >= min_duration_sec
    
    def get_contiguous_buffer(self) -> Tuple[np.ndarray, int]:
        """
        Get a contiguous copy of the buffer contents.
        
        Returns:
            Tuple of (samples, start_rtp):
                - samples: Contiguous array of samples (oldest to newest)
                - start_rtp: RTP timestamp of first sample
        """
        if self.buffer_start_rtp is None:
            return np.array([], dtype=np.complex64), 0
        
        samples_available = min(self.total_samples_written, self.buffer_size)
        
        if samples_available == 0:
            return np.array([], dtype=np.complex64), self.buffer_start_rtp
        
        # If buffer hasn't wrapped yet, simple slice
        if self.total_samples_written <= self.buffer_size:
            return self.buffer[:samples_available].copy(), self.buffer_start_rtp
        
        # Buffer has wrapped - need to reorder
        # Oldest samples are at write_pos, newest are at write_pos-1
        result = np.empty(samples_available, dtype=np.complex64)
        
        # First part: from write_pos to end
        first_part_len = self.buffer_size - self.write_pos
        result[:first_part_len] = self.buffer[self.write_pos:]
        
        # Second part: from start to write_pos
        result[first_part_len:] = self.buffer[:self.write_pos]
        
        return result, self.buffer_start_rtp
    
    def search_for_minute_markers(
        self,
        tone_detector,
        search_all_stations: bool = True
    ) -> List[ToneCandidate]:
        """
        Search the entire buffer for minute marker tones.
        
        This performs a full-buffer cross-correlation search for 1000 Hz (WWV/CHU)
        and 1200 Hz (WWVH) tones. Unlike operational mode, we don't assume where
        the minute boundary is - we search everywhere.
        
        Args:
            tone_detector: ToneDetector instance for correlation
            search_all_stations: If True, search for all station types
            
        Returns:
            List of ToneCandidate objects found
        """
        if not self.has_enough_data(min_duration_sec=30.0):
            logger.debug(f"[BOOTSTRAP_BUFFER] {self.channel_name}: Not enough data for search")
            return []
        
        # Get contiguous buffer
        samples, start_rtp = self.get_contiguous_buffer()
        
        if len(samples) < self.sample_rate * 10:  # Need at least 10 seconds
            return []
        
        logger.info(f"[BOOTSTRAP_BUFFER] {self.channel_name}: Searching {len(samples)/self.sample_rate:.1f}s "
                   f"buffer for minute markers (RTP {start_rtp} to {start_rtp + len(samples)})")
        
        candidates = []
        
        # Search for each station type
        # Import here to avoid circular dependency
        from hf_timestd.core.tone_detector import StationType
        
        station_configs = []
        
        # Determine which stations to search based on channel
        channel_upper = self.channel_name.upper()
        
        if 'CHU' in channel_upper:
            # CHU-only channel
            station_configs.append((StationType.CHU, 1000, 0.5))
        elif any(f in channel_upper for f in ['20000', '25000']):
            # WWV-only frequencies
            station_configs.append((StationType.WWV, 1000, 0.8))
        else:
            # Shared frequency - search for all
            station_configs.append((StationType.WWV, 1000, 0.8))
            station_configs.append((StationType.WWVH, 1200, 0.8))
            station_configs.append((StationType.CHU, 1000, 0.5))
        
        for station_type, tone_freq, duration in station_configs:
            # Use tone detector's correlation method
            # We need to search the ENTIRE buffer, not just around expected position
            try:
                found = self._search_buffer_for_tone(
                    samples=samples,
                    start_rtp=start_rtp,
                    tone_detector=tone_detector,
                    station_type=station_type,
                    tone_frequency=tone_freq,
                    duration=duration
                )
                candidates.extend(found)
            except Exception as e:
                logger.error(f"[BOOTSTRAP_BUFFER] {self.channel_name}: Error searching for "
                            f"{station_type.value}: {e}")
        
        logger.info(f"[BOOTSTRAP_BUFFER] {self.channel_name}: Found {len(candidates)} candidates")
        
        return candidates
    
    def _search_buffer_for_tone(
        self,
        samples: np.ndarray,
        start_rtp: int,
        tone_detector,
        station_type,
        tone_frequency: int,
        duration: float
    ) -> List[ToneCandidate]:
        """
        Search buffer for a specific tone type.
        
        Uses sliding window correlation to find all instances of the tone
        in the buffer, not just the strongest one.
        """
        from hf_timestd.core.tone_detector import StationType
        import scipy.signal as scipy_signal
        
        candidates = []
        
        # Create matched filter template
        template_samples = int(duration * self.sample_rate)
        t = np.arange(template_samples) / self.sample_rate
        
        # Quadrature template for phase-invariant detection
        template_sin = np.sin(2 * np.pi * tone_frequency * t)
        template_cos = np.cos(2 * np.pi * tone_frequency * t)
        
        # Apply window
        window = scipy_signal.windows.tukey(template_samples, alpha=0.1)
        template_sin *= window
        template_cos *= window
        
        # Normalize
        template_sin /= np.linalg.norm(template_sin)
        template_cos /= np.linalg.norm(template_cos)
        
        # Demodulate to baseband (extract audio)
        # For now, just use magnitude of complex samples as proxy
        audio = np.abs(samples)
        
        # Correlate with both templates
        corr_sin = scipy_signal.correlate(audio, template_sin, mode='valid')
        corr_cos = scipy_signal.correlate(audio, template_cos, mode='valid')
        
        # Quadrature combination (phase-invariant)
        correlation = np.sqrt(corr_sin**2 + corr_cos**2)
        
        # Estimate noise floor using median
        noise_floor = np.median(correlation)
        noise_std = np.median(np.abs(correlation - noise_floor)) * 1.4826  # MAD to std
        
        # Adaptive threshold
        threshold = noise_floor + 4.0 * noise_std
        
        if threshold <= 0:
            return candidates
        
        # Find peaks above threshold
        # Minimum distance between peaks: 0.9 seconds (to separate per-second ticks)
        min_distance = int(0.9 * self.sample_rate)
        
        peaks, properties = scipy_signal.find_peaks(
            correlation,
            height=threshold,
            distance=min_distance
        )
        
        logger.debug(f"[BOOTSTRAP_BUFFER] {self.channel_name}: {station_type.value} "
                    f"found {len(peaks)} peaks above threshold {threshold:.4f}")
        
        # Convert peaks to candidates
        for peak_idx in peaks:
            peak_val = correlation[peak_idx]
            
            # Calculate SNR
            snr_db = 20 * np.log10(peak_val / noise_floor) if noise_floor > 0 else 0
            
            # Confidence based on SNR
            confidence = min(1.0, snr_db / 20.0)  # Saturates at 20 dB
            
            # RTP timestamp of this peak
            # For mode='valid', peak_idx corresponds to where template starts
            rtp_timestamp = start_rtp + peak_idx
            
            candidate = ToneCandidate(
                rtp_timestamp=rtp_timestamp,
                sample_position=peak_idx,
                station=station_type.value,
                tone_frequency_hz=tone_frequency,
                correlation_peak=float(peak_val),
                snr_db=snr_db,
                confidence=confidence,
                duration_sec=duration
            )
            
            candidates.append(candidate)
        
        return candidates
    
    def get_status(self) -> dict:
        """Get current buffer status."""
        samples_available = min(self.total_samples_written, self.buffer_size)
        duration_available = samples_available / self.sample_rate
        
        return {
            'channel': self.channel_name,
            'buffer_size_samples': self.buffer_size,
            'buffer_duration_sec': self.buffer_duration_sec,
            'samples_written': self.total_samples_written,
            'samples_available': samples_available,
            'duration_available_sec': duration_available,
            'buffer_start_rtp': self.buffer_start_rtp,
            'write_pos': self.write_pos,
            'gap_count': self.gap_count,
            'gap_samples': self.gap_samples,
            'has_enough_data': self.has_enough_data(),
        }
    
    def clear(self) -> None:
        """Clear the buffer and reset state."""
        self.buffer.fill(0)
        self.buffer_start_rtp = None
        self.write_pos = 0
        self.total_samples_written = 0
        self.gap_count = 0
        self.gap_samples = 0
        self.last_rtp = None
        
        logger.info(f"[BOOTSTRAP_BUFFER] {self.channel_name}: Buffer cleared")


class BootstrapBufferManager:
    """
    Manages rolling buffers for multiple channels during bootstrap.
    
    Coordinates the bootstrap process across all channels:
    1. Accumulates samples into per-channel rolling buffers
    2. Periodically searches for tone candidates
    3. Feeds candidates to TimingBootstrap state machine
    4. Transitions to operational mode once locked
    """
    
    def __init__(
        self,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        buffer_duration_sec: float = DEFAULT_BUFFER_DURATION_SEC
    ):
        """
        Initialize the buffer manager.
        
        Args:
            sample_rate: Sample rate in Hz
            buffer_duration_sec: Buffer duration per channel
        """
        self.sample_rate = sample_rate
        self.buffer_duration_sec = buffer_duration_sec
        
        # Per-channel buffers
        self.buffers: Dict[str, BootstrapRollingBuffer] = {}
        
        # Bootstrap state machine (shared across channels)
        self.bootstrap = None  # Will be set externally
        
        # Search interval (don't search every sample batch)
        self.search_interval_sec = 5.0
        self.last_search_time: Dict[str, float] = {}
        
        # Lock status
        self.is_locked = False
        
        logger.info(f"[BOOTSTRAP_MANAGER] Initialized with {buffer_duration_sec:.1f}s buffers")
    
    def get_or_create_buffer(self, channel_name: str) -> BootstrapRollingBuffer:
        """Get existing buffer or create new one for channel."""
        if channel_name not in self.buffers:
            self.buffers[channel_name] = BootstrapRollingBuffer(
                channel_name=channel_name,
                sample_rate=self.sample_rate,
                buffer_duration_sec=self.buffer_duration_sec
            )
            self.last_search_time[channel_name] = 0.0
        
        return self.buffers[channel_name]
    
    def add_samples(
        self,
        channel_name: str,
        samples: np.ndarray,
        rtp_timestamp: int
    ) -> None:
        """
        Add samples to a channel's buffer.
        
        Args:
            channel_name: Channel identifier
            samples: IQ samples
            rtp_timestamp: RTP timestamp of first sample
        """
        if self.is_locked:
            # Once locked, we don't need the rolling buffers anymore
            return
        
        buffer = self.get_or_create_buffer(channel_name)
        buffer.add_samples(samples, rtp_timestamp)
    
    def search_and_process(
        self,
        channel_name: str,
        tone_detector,
        current_time: float
    ) -> Optional[str]:
        """
        Search channel buffer for tones and process through bootstrap.
        
        Args:
            channel_name: Channel to search
            tone_detector: ToneDetector instance
            current_time: Current time (for rate limiting)
            
        Returns:
            Status message or None
        """
        if self.is_locked:
            return "LOCKED"
        
        if channel_name not in self.buffers:
            return None
        
        # Rate limit searches
        last_search = self.last_search_time.get(channel_name, 0.0)
        if current_time - last_search < self.search_interval_sec:
            return None
        
        self.last_search_time[channel_name] = current_time
        
        buffer = self.buffers[channel_name]
        
        if not buffer.has_enough_data():
            return None
        
        # Search for candidates
        candidates = buffer.search_for_minute_markers(tone_detector)
        
        if not candidates:
            return None
        
        # Process through bootstrap state machine
        if self.bootstrap is None:
            logger.warning("[BOOTSTRAP_MANAGER] No bootstrap state machine configured")
            return None
        
        status = None
        for candidate in candidates:
            # Extract frequency from channel name
            try:
                freq_khz = int(channel_name.split('_')[1])
            except (IndexError, ValueError):
                freq_khz = 0
            
            result = self.bootstrap.add_candidate(
                channel=channel_name,
                station=candidate.station,
                frequency_khz=freq_khz,
                tone_frequency_hz=candidate.tone_frequency_hz,
                rtp_timestamp=candidate.rtp_timestamp,
                sample_position=candidate.sample_position,
                snr_db=candidate.snr_db,
                confidence=candidate.confidence,
                buffer_rtp_start=buffer.buffer_start_rtp or 0
            )
            
            if result:
                status = result
                
                # Check if we've locked
                from hf_timestd.core.timing_bootstrap import BootstrapState
                if self.bootstrap.state == BootstrapState.LOCKED:
                    self.is_locked = True
                    logger.info("[BOOTSTRAP_MANAGER] Bootstrap LOCKED - transitioning to operational mode")
                    break
        
        return status
    
    def get_status(self) -> dict:
        """Get overall bootstrap status."""
        buffer_statuses = {
            name: buf.get_status() 
            for name, buf in self.buffers.items()
        }
        
        bootstrap_status = self.bootstrap.get_status() if self.bootstrap else {}
        
        return {
            'is_locked': self.is_locked,
            'num_channels': len(self.buffers),
            'buffers': buffer_statuses,
            'bootstrap': bootstrap_status,
        }
    
    def clear_all(self) -> None:
        """Clear all buffers and reset state."""
        for buffer in self.buffers.values():
            buffer.clear()
        self.is_locked = False
        logger.info("[BOOTSTRAP_MANAGER] All buffers cleared")
