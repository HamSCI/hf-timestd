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
from typing import Any, Dict, List, Optional, Tuple
from enum import Enum

logger = logging.getLogger(__name__)

# Constants
DEFAULT_SAMPLE_RATE = 24000
SAMPLES_PER_MINUTE = DEFAULT_SAMPLE_RATE * 60  # 1,440,000

# Buffer duration: 2.5 minutes to guarantee capturing full minute + margins
DEFAULT_BUFFER_DURATION_SEC = 150.0  # 2.5 minutes

# GPS/RTP timing constants (from ka9q-python)
GPS_UTC_OFFSET = 315964800  # GPS epoch (1980-01-06) - Unix epoch (1970-01-01)
GPS_LEAP_SECONDS = 18       # GPS time is ahead of UTC by 18 seconds (as of 2025)
BILLION = 1_000_000_000


def rtp_to_wallclock(rtp_timestamp: int, channel_info: Any, sample_rate: int) -> Optional[float]:
    """
    Convert RTP timestamp to Unix wall-clock time using GPS timing info.
    
    Each SSRC has its own RTP clock space. The channel_info provides:
    - gps_time: GPS timestamp in nanoseconds at the snapshot
    - rtp_timesnap: The RTP timestamp at that GPS time
    
    Formula: wall_time = gps_time + (packet_timestamp - rtp_timesnap) / sample_rate
    
    Args:
        rtp_timestamp: RTP timestamp from packet header
        channel_info: ChannelInfo with gps_time and rtp_timesnap
        sample_rate: Sample rate in Hz
    
    Returns:
        Unix timestamp (seconds) or None if timing info unavailable
    """
    if channel_info is None:
        return None
    
    gps_time = getattr(channel_info, 'gps_time', None)
    rtp_timesnap = getattr(channel_info, 'rtp_timesnap', None)
    
    if gps_time is None or rtp_timesnap is None:
        return None
    
    # Convert GPS nanoseconds to Unix time
    # GPS epoch is Jan 6, 1980; Unix epoch is Jan 1, 1970
    # Subtract GPS_LEAP_SECONDS to align with UTC
    sender_time_ns = gps_time + BILLION * (GPS_UTC_OFFSET - GPS_LEAP_SECONDS)
    
    # Add offset from RTP timestamp difference (handle 32-bit wrap)
    rtp_delta = int((rtp_timestamp - rtp_timesnap) & 0xFFFFFFFF)
    if rtp_delta > 0x7FFFFFFF:
        rtp_delta -= 0x100000000
    
    time_offset_ns = BILLION * rtp_delta // sample_rate
    wall_time_ns = sender_time_ns + time_offset_ns
    
    return wall_time_ns / BILLION


def wallclock_to_rtp(wallclock: float, channel_info: Any, sample_rate: int) -> Optional[int]:
    """
    Convert Unix wall-clock time back to RTP timestamp for a specific channel.
    
    Inverse of rtp_to_wallclock - used to find the RTP timestamp in a channel's
    buffer that corresponds to a specific wallclock time.
    
    Args:
        wallclock: Unix timestamp (seconds)
        channel_info: ChannelInfo with gps_time and rtp_timesnap
        sample_rate: Sample rate in Hz
    
    Returns:
        RTP timestamp or None if timing info unavailable
    """
    if channel_info is None:
        return None
    
    gps_time = getattr(channel_info, 'gps_time', None)
    rtp_timesnap = getattr(channel_info, 'rtp_timesnap', None)
    
    if gps_time is None or rtp_timesnap is None:
        return None
    
    # Convert wallclock to GPS nanoseconds
    wall_time_ns = int(wallclock * BILLION)
    sender_time_ns = gps_time + BILLION * (GPS_UTC_OFFSET - GPS_LEAP_SECONDS)
    
    # Calculate RTP offset from time difference
    time_offset_ns = wall_time_ns - sender_time_ns
    rtp_delta = time_offset_ns * sample_rate // BILLION
    
    # Add to rtp_timesnap (handle 32-bit wrap)
    rtp_timestamp = (rtp_timesnap + rtp_delta) & 0xFFFFFFFF
    
    return rtp_timestamp


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
    
    def get_samples_at_rtp(
        self,
        start_rtp: int,
        num_samples: int
    ) -> Optional[np.ndarray]:
        """
        Get samples starting at a specific RTP timestamp.
        
        Args:
            start_rtp: RTP timestamp of first sample to retrieve
            num_samples: Number of samples to retrieve
            
        Returns:
            Array of samples, or None if requested range not in buffer
        """
        if self.buffer_start_rtp is None:
            return None
        
        # Check if requested range is in buffer
        buffer_end_rtp = self.buffer_start_rtp + min(self.total_samples_written, self.buffer_size)
        
        if start_rtp < self.buffer_start_rtp or start_rtp >= buffer_end_rtp:
            return None
        
        if start_rtp + num_samples > buffer_end_rtp:
            # Partial data available - return what we have
            num_samples = buffer_end_rtp - start_rtp
        
        # Calculate buffer offset
        offset_from_start = start_rtp - self.buffer_start_rtp
        
        # Get contiguous buffer and slice
        contiguous, _ = self.get_contiguous_buffer()
        
        if offset_from_start + num_samples <= len(contiguous):
            return contiguous[offset_from_start:offset_from_start + num_samples].copy()
        
        return None
    
    def search_for_minute_markers(
        self,
        tone_detector,
        search_all_stations: bool = True
    ) -> List[ToneCandidate]:
        """
        Search the entire buffer for minute marker tones.
        
        Uses the ToneDetector's acquire_tones() method which has proper matched
        filtering with duration-specific templates (800ms for WWV/WWVH, 500ms for CHU).
        
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
        
        # Use the ToneDetector's acquire_tones method which has proper matched filtering
        # with duration-specific templates (800ms WWV/WWVH, 500ms CHU)
        try:
            acquisition_results = tone_detector.acquire_tones(
                samples=samples,
                buffer_rtp_start=start_rtp,
                snr_threshold_db=15.0,  # Require strong match to template
                max_candidates=10  # Per station type
            )
        except Exception as e:
            logger.error(f"[BOOTSTRAP_BUFFER] {self.channel_name}: acquire_tones failed: {e}")
            return []
        
        # Convert ToneAcquisitionResult to ToneCandidate
        candidates = []
        for result in acquisition_results:
            candidate = ToneCandidate(
                rtp_timestamp=result.rtp_timestamp,
                sample_position=result.sample_position,
                station=result.station.value,
                tone_frequency_hz=int(result.frequency_hz),
                correlation_peak=result.correlation_peak,
                snr_db=result.snr_db,
                confidence=result.confidence,
                duration_sec=0.8 if result.station.value in ('WWV', 'WWVH') else 0.5
            )
            candidates.append(candidate)
        
        logger.info(f"[BOOTSTRAP_BUFFER] {self.channel_name}: Found {len(candidates)} candidates")
        
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
        
        # Per-channel ChannelInfo for GPS-aligned wallclock conversion
        self.channel_infos: Dict[str, Any] = {}
        
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
        rtp_timestamp: int,
        channel_info: Optional[Any] = None
    ) -> None:
        """
        Add samples to a channel's buffer.
        
        Args:
            channel_name: Channel identifier
            samples: IQ samples
            rtp_timestamp: RTP timestamp of first sample
            channel_info: Optional ChannelInfo with gps_time/rtp_timesnap for
                         GPS-aligned wallclock conversion (per-SSRC RTP alignment)
        """
        if self.is_locked:
            # Once locked, we don't need the rolling buffers anymore
            return
        
        buffer = self.get_or_create_buffer(channel_name)
        buffer.add_samples(samples, rtp_timestamp)
        
        # Store channel_info for wallclock alignment
        if channel_info is not None:
            self.channel_infos[channel_name] = channel_info
    
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
        from hf_timestd.core.timing_bootstrap import BootstrapState
        logger.info(f"[BOOTSTRAP_MANAGER] Processing {len(candidates)} candidates from {channel_name}, "
                   f"bootstrap state={self.bootstrap.state.value if self.bootstrap else 'None'}")
        for candidate in candidates:
            # Extract frequency from channel name
            try:
                freq_khz = int(channel_name.split('_')[1])
            except (IndexError, ValueError):
                freq_khz = 0
            
            # Compute wallclock time from RTP using GPS timing info (for cross-SSRC comparison)
            channel_info = self.channel_infos.get(channel_name)
            wallclock_time = rtp_to_wallclock(candidate.rtp_timestamp, channel_info, self.sample_rate)
            
            wc_str = f"{wallclock_time:.3f}" if wallclock_time is not None else "N/A"
            logger.debug(f"[BOOTSTRAP_MANAGER] Candidate: {candidate.station} RTP={candidate.rtp_timestamp} "
                        f"wallclock={wc_str} SNR={candidate.snr_db:.1f}dB")
            result = self.bootstrap.add_candidate(
                channel=channel_name,
                station=candidate.station,
                frequency_khz=freq_khz,
                tone_frequency_hz=candidate.tone_frequency_hz,
                rtp_timestamp=candidate.rtp_timestamp,
                sample_position=candidate.sample_position,
                snr_db=candidate.snr_db,
                confidence=candidate.confidence,
                buffer_rtp_start=buffer.buffer_start_rtp or 0,
                wallclock_time=wallclock_time
            )
            
            if result:
                status = result
                
                # Check if we've locked
                from hf_timestd.core.timing_bootstrap import BootstrapState
                if self.bootstrap.state == BootstrapState.LOCKED:
                    self.is_locked = True
                    logger.info("[BOOTSTRAP_MANAGER] Bootstrap LOCKED - transitioning to operational mode")
                    break
                
                # If in TRACKING state, attempt time confirmation
                if self.bootstrap.state == BootstrapState.TRACKING:
                    confirm_result = self._attempt_time_confirmation()
                    if confirm_result:
                        status = confirm_result
                        if self.bootstrap.state == BootstrapState.LOCKED:
                            self.is_locked = True
                            break
        
        return status
    
    def _attempt_time_confirmation(self) -> Optional[str]:
        """
        Attempt to confirm time by decoding BCD/FSK from station broadcasts.
        
        Called when bootstrap reaches TRACKING state to get decoded time
        confirmation before final LOCKED state.
        
        Architecture:
        ------------
        Each SSRC has its own RTP clock space. We use GPS timing info (gps_time,
        rtp_timesnap) from ChannelInfo to convert the reference minute boundary
        to each channel's RTP space.
        
        1. Get reference_rtp from the anchor channel (where tone was detected)
        2. Convert to wallclock time using anchor channel's GPS timing
        3. For each target channel, convert wallclock back to that channel's RTP
        4. Retrieve samples at the converted RTP position
        """
        logger.info("[BOOTSTRAP_MANAGER] Attempting time confirmation via BCD/FSK decode")
        
        if self.bootstrap is None or self.bootstrap.reference_rtp is None:
            logger.warning("[BOOTSTRAP_MANAGER] Cannot confirm time: no bootstrap or reference_rtp")
            return None
        
        import time
        ntp_time = time.time()  # Current system time as NTP hypothesis
        
        samples_per_minute = 60 * self.sample_rate
        reference_rtp = self.bootstrap.reference_rtp
        reference_channel = getattr(self.bootstrap, 'reference_channel', None)
        
        # Convert reference_rtp to wallclock using the anchor channel's timing info
        minute_boundary_wallclock = None
        if reference_channel and reference_channel in self.channel_infos:
            anchor_info = self.channel_infos[reference_channel]
            minute_boundary_wallclock = rtp_to_wallclock(reference_rtp, anchor_info, self.sample_rate)
            if minute_boundary_wallclock:
                logger.info(f"[BOOTSTRAP_MANAGER] Minute boundary: wallclock={minute_boundary_wallclock:.3f} "
                           f"(from {reference_channel} RTP={reference_rtp})")
        
        if minute_boundary_wallclock is None:
            # Fallback: try to find any channel with timing info
            for ch_name, ch_info in self.channel_infos.items():
                if ch_name in self.buffers:
                    buffer = self.buffers[ch_name]
                    if buffer.buffer_start_rtp is not None:
                        # Use buffer midpoint as reference
                        mid_rtp = buffer.buffer_start_rtp + buffer.buffer_size // 2
                        test_wallclock = rtp_to_wallclock(mid_rtp, ch_info, self.sample_rate)
                        if test_wallclock:
                            # Estimate minute boundary from reference_rtp offset
                            # This is approximate but better than nothing
                            logger.info(f"[BOOTSTRAP_MANAGER] Using {ch_name} for wallclock alignment")
                            minute_boundary_wallclock = test_wallclock
                            break
        
        chu_samples = None
        wwv_samples = None
        wwvh_samples = None
        
        for channel_name, buffer in self.buffers.items():
            channel_upper = channel_name.upper()
            
            # Get this channel's timing info
            channel_info = self.channel_infos.get(channel_name)
            
            # Convert minute boundary wallclock to this channel's RTP space
            if minute_boundary_wallclock and channel_info:
                channel_rtp = wallclock_to_rtp(minute_boundary_wallclock, channel_info, self.sample_rate)
                if channel_rtp is None:
                    logger.debug(f"[BOOTSTRAP_MANAGER] {channel_name}: cannot convert wallclock to RTP")
                    continue
            else:
                # No wallclock alignment available - use reference_rtp directly (may fail for different SSRCs)
                channel_rtp = reference_rtp
            
            # Log buffer range for debugging
            buffer_end = buffer.buffer_start_rtp + buffer.buffer_size if buffer.buffer_start_rtp else None
            
            samples = buffer.get_samples_at_rtp(channel_rtp, samples_per_minute)
            if samples is None:
                logger.debug(f"[BOOTSTRAP_MANAGER] {channel_name}: no samples at RTP {channel_rtp} "
                            f"(buffer: {buffer.buffer_start_rtp} to {buffer_end})")
                continue
            if len(samples) < samples_per_minute * 0.5:
                logger.debug(f"[BOOTSTRAP_MANAGER] {channel_name}: only {len(samples)} samples (need {samples_per_minute})")
                continue
            
            if 'CHU' in channel_upper:
                if chu_samples is None or len(samples) > len(chu_samples):
                    chu_samples = samples
                    logger.info(f"[BOOTSTRAP_MANAGER] Got {len(samples)} CHU samples for time confirmation")
            elif 'WWVH' in channel_upper:
                # Explicit WWVH channel
                if wwvh_samples is None or len(samples) > len(wwvh_samples):
                    wwvh_samples = samples
                    logger.info(f"[BOOTSTRAP_MANAGER] Got {len(samples)} WWVH samples for time confirmation")
            elif 'WWV' in channel_upper:
                # WWV channel (not WWVH)
                if wwv_samples is None or len(samples) > len(wwv_samples):
                    wwv_samples = samples
                    logger.info(f"[BOOTSTRAP_MANAGER] Got {len(samples)} WWV samples for time confirmation")
            elif 'SHARED' in channel_upper:
                # SHARED channels contain WWV/WWVH - use for WWV/WWVH decode
                # The BCD decoder handles both WWV and WWVH (same 100 Hz subcarrier)
                if wwv_samples is None or len(samples) > len(wwv_samples):
                    wwv_samples = samples
                    logger.info(f"[BOOTSTRAP_MANAGER] Got {len(samples)} SHARED samples for WWV/WWVH time confirmation")
        
        # Log what we're passing to confirmation
        logger.info(f"[BOOTSTRAP_MANAGER] Time confirmation: CHU={len(chu_samples) if chu_samples is not None else 0}, "
                   f"WWV={len(wwv_samples) if wwv_samples is not None else 0}, "
                   f"WWVH={len(wwvh_samples) if wwvh_samples is not None else 0}")
        
        # Attempt confirmation
        result = self.bootstrap.attempt_time_confirmation(
            ntp_time=ntp_time,
            chu_samples=chu_samples,
            wwv_samples=wwv_samples,
            wwvh_samples=wwvh_samples,
        )
        
        if result:
            logger.info(f"[BOOTSTRAP_MANAGER] Time confirmation result: {result}")
        else:
            logger.info("[BOOTSTRAP_MANAGER] Time confirmation: no result (decoders may have failed)")
        
        return result
    
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
