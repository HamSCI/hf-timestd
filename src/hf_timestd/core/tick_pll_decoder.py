"""
Tick PLL Decoder - Flywheel-style timing decoder for WWV/WWVH.

Implements a stiff PLL that locks onto per-second tick frequencies (1000 Hz for WWV,
1200 Hz for WWVH) and maintains lock across fades via flywheel coasting.

This is a parallel implementation to tick_matched_filter.py for A/B testing.
The decoder_variant field in HDF5 output distinguishes results.

Author: AI Assistant
Date: 2026-02-16
"""

import numpy as np
from scipy.signal import butter, filtfilt, hilbert
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Tuple, Any
from enum import Enum
import logging

logger = logging.getLogger(__name__)


class PLLState(Enum):
    """PLL state machine states."""
    HUNT = "HUNT"
    LOCK = "LOCK"
    COAST = "COAST"  # Lost lock but coasting


@dataclass
class PLLTickResult:
    """Result from a single tick detection."""
    tick_index: int              # Sample index of tick (relative to buffer)
    is_minute_mark: bool         # True if 800ms tone (minute marker)
    is_hour_mark: bool           # True if 1500 Hz hour marker
    confidence: float            # 0.0-1.0 based on SNR and lock duration
    station: str                 # "WWV" or "WWVH"
    
    # Timing precision metrics
    phase_error_samples: float   # PLL phase error at detection
    lock_duration_sec: int       # How long we've been locked
    
    # BCD data (if decoded)
    bcd_bit: Optional[str] = None  # '0', '1', 'P', '?', or None
    bcd_confidence: float = 0.0
    collision_suspected: bool = False


@dataclass
class MinutePLLAnalysis:
    """Aggregated PLL results for a full minute."""
    minute_timestamp: float      # Unix time of minute boundary
    station: str                 # "WWV", "WWVH", or "BOTH"
    
    # Tick statistics
    n_ticks_detected: int        # How many of 60/55 ticks found
    n_minute_markers: int        # Should be 1
    n_hour_markers: int          # 0 or 1
    
    # Timing
    mean_timing_offset_ms: float  # Relative to expected
    std_timing_offset_ms: float   # Jitter
    d_clock_ms: Optional[float] = None  # D_clock measurement for comparison
    
    # BCD decode
    bcd_frame: Optional[str] = None  # 60-bit frame if decoded
    bcd_confidence: float = 0.0
    
    # Decoder metadata
    decoder_variant: str = "pll"  # For A/B comparison
    lock_quality: float = 0.0     # Overall lock quality 0-1


class TickPLL:
    """
    Stiff PLL that locks onto a specific tick frequency (1000 or 1200 Hz).
    
    The PLL maintains an internal "flywheel" clock that predicts when the next
tick should arrive. Once locked, it uses a narrow gating window (±20ms) to
    reject noise and crosstalk from other stations.
    
    Key features:
    - Narrow aperture reduces noise pickup vs wide matched filter
    - Flywheel coasting across fades (up to 5 missed ticks)
    - Sub-sample precision through continuous phase tracking
    - Automatic minute/hour marker discrimination
    """
    
    def __init__(
        self,
        station_name: str,
        tick_freq: float,
        fs: int = 24000,
        window_ms: float = 40.0,
        alpha: float = 0.1,
        max_missed_ticks: int = 5
    ):
        """
        Initialize PLL for a specific station.
        
        Args:
            station_name: "WWV" or "WWVH"
            tick_freq: 1000.0 (WWV) or 1200.0 (WWVH)
            fs: Sample rate (default 24000 Hz)
            window_ms: Gating window width (±window_ms/2)
            alpha: Loop filter gain (0.0-1.0)
            max_missed_ticks: Ticks to coast before declaring lost lock
        """
        self.name = station_name
        self.target_freq = tick_freq
        self.fs = fs
        
        # State machine
        self.state = PLLState.HUNT
        self.samples_per_period = int(fs / 1.0)  # 1 second period
        self.next_expected_tick = 0
        self.missed_ticks = 0
        self.locked_for_ticks = 0
        
        # Tuning
        self.window_samples = int(window_ms / 1000.0 * fs / 2)
        self.alpha = alpha
        self.max_missed_ticks = max_missed_ticks
        
        # Filters
        nyq = 0.5 * fs
        
        # Tick frequency bandpass (±50 Hz)
        self.b_tick, self.a_tick = butter(
            2,
            [(tick_freq - 50) / nyq, (tick_freq + 50) / nyq],
            btype='band'
        )
        
        # Hour marker filter (1500 Hz ±50 Hz) - shared by both stations
        self.b_hour, self.a_hour = butter(
            2,
            [1450 / nyq, 1550 / nyq],
            btype='band'
        )
        
        # 100 Hz BCD data filter
        self.b_100, self.a_100 = butter(
            2,
            [80 / nyq, 120 / nyq],
            btype='band'
        )
        
        # Tracking
        self.last_tick_sample = None
        self.period_estimate = float(fs)  # Current period estimate
        
        logger.info(f"TickPLL initialized for {station_name} at {tick_freq} Hz")
    
    def reset(self):
        """Reset PLL to hunt state."""
        self.state = PLLState.HUNT
        self.missed_ticks = 0
        self.locked_for_ticks = 0
        self.next_expected_tick = 0
        logger.debug(f"[{self.name}] PLL reset to HUNT")
    
    def process_buffer(
        self,
        audio: np.ndarray,
        buffer_start_sample: int,
        buffer_timing=None
    ) -> List[PLLTickResult]:
        """
        Process a buffer of audio and detect ticks.
        
        Args:
            audio: Audio samples (mono, 24kHz)
            buffer_start_sample: Absolute sample index of buffer start
            buffer_timing: Optional BufferTiming for UTC conversion
            
        Returns:
            List of detected ticks with timing information
        """
        results = []
        
        # Pre-compute filters
        filt_tick = filtfilt(self.b_tick, self.a_tick, audio)
        env_tick = np.abs(hilbert(filt_tick))
        
        filt_hour = filtfilt(self.b_hour, self.a_hour, audio)
        env_hour = np.abs(hilbert(filt_hour))
        
        # Dynamic threshold based on noise floor
        noise_floor = np.median(env_tick)
        threshold = noise_floor * 5.0
        
        # State machine processing
        if self.state == PLLState.HUNT:
            result = self._process_hunt(
                audio, env_tick, env_hour, threshold,
                buffer_start_sample
            )
            if result:
                results.append(result)
                
        elif self.state in (PLLState.LOCK, PLLState.COAST):
            # Look for expected tick within gating window
            result = self._process_lock(
                audio, env_tick, env_hour, threshold,
                buffer_start_sample
            )
            if result:
                results.append(result)
        
        return results
    
    def _process_hunt(
        self,
        audio: np.ndarray,
        env_tick: np.ndarray,
        env_hour: np.ndarray,
        threshold: float,
        buffer_start: int
    ) -> Optional[PLLTickResult]:
        """HUNT state: Find initial tick to lock onto."""
        # Simple peak detection
        peak_idx = np.argmax(env_tick)
        
        if env_tick[peak_idx] > threshold:
            # Found signal - transition to LOCK
            abs_peak = buffer_start + peak_idx
            
            # Check if it's a minute marker (long pulse)
            is_minute = self._check_minute_marker(env_tick, peak_idx)
            
            # Check for hour marker (1500 Hz)
            is_hour = self._check_hour_marker(env_hour, peak_idx)
            
            # Initialize flywheel
            self.next_expected_tick = abs_peak + self.samples_per_period
            self.period_estimate = float(self.samples_per_period)
            self.state = PLLState.LOCK
            self.missed_ticks = 0
            self.locked_for_ticks = 1
            
            logger.info(
                f"[{self.name}] HUNT→LOCK: Found tick at sample {abs_peak}, "
                f"is_minute={is_minute}, is_hour={is_hour}"
            )
            
            return PLLTickResult(
                tick_index=peak_idx,
                is_minute_mark=is_minute,
                is_hour_mark=is_hour,
                confidence=min(1.0, env_tick[peak_idx] / (threshold * 2)),
                station=self.name,
                phase_error_samples=0.0,  # First lock, no error yet
                lock_duration_sec=0
            )
        
        return None
    
    def _process_lock(
        self,
        audio: np.ndarray,
        env_tick: np.ndarray,
        env_hour: np.ndarray,
        threshold: float,
        buffer_start: int
    ) -> Optional[PLLTickResult]:
        """LOCK state: Use narrow gating window."""
        # Calculate expected tick position relative to this buffer
        rel_expected = int(self.next_expected_tick - buffer_start)
        
        # Define gating window
        win_start = rel_expected - self.window_samples
        win_end = rel_expected + self.window_samples
        
        # Check if window is within buffer
        if win_start < 0 or win_end >= len(audio):
            # Expected tick not in this buffer - coast
            return None
        
        # Extract window
        window_tick = env_tick[win_start:win_end]
        window_hour = env_hour[win_start:win_end]
        
        # Find peak in window
        peak_rel_idx = np.argmax(window_tick)
        peak_val = window_tick[peak_rel_idx]
        
        hit_idx = -1
        is_minute = False
        is_hour = False
        confidence = 0.0
        
        if peak_val > threshold:
            # Found our tick
            hit_idx = win_start + peak_rel_idx
            is_minute = self._check_minute_marker(env_tick, hit_idx)
            
            # Update PLL
            actual_pos = buffer_start + hit_idx
            phase_error = actual_pos - self.next_expected_tick
            
            # Loop filter: adjust period estimate
            self.period_estimate += self.alpha * phase_error
            self.next_expected_tick = actual_pos + self.period_estimate
            
            # Reset coast counter
            self.missed_ticks = 0
            self.locked_for_ticks += 1
            
            confidence = min(1.0, peak_val / (threshold * 2))
            
            return PLLTickResult(
                tick_index=hit_idx,
                is_minute_mark=is_minute,
                is_hour_mark=False,
                confidence=confidence,
                station=self.name,
                phase_error_samples=phase_error,
                lock_duration_sec=self.locked_for_ticks
            )
        
        else:
            # No tick found - check for hour marker
            hour_peak_idx = np.argmax(window_hour)
            if window_hour[hour_peak_idx] > np.median(env_hour) * 5.0:
                # Hour marker detected
                hit_idx = win_start + hour_peak_idx
                is_hour = True
                actual_pos = buffer_start + hit_idx
                phase_error = actual_pos - self.next_expected_tick
                
                # Update PLL using hour mark
                self.period_estimate += self.alpha * phase_error
                self.next_expected_tick = actual_pos + self.period_estimate
                self.missed_ticks = 0
                self.locked_for_ticks += 1
                
                return PLLTickResult(
                    tick_index=hit_idx,
                    is_minute_mark=False,
                    is_hour_mark=True,
                    confidence=0.8,
                    station=self.name,
                    phase_error_samples=phase_error,
                    lock_duration_sec=self.locked_for_ticks
                )
            
            else:
                # Complete miss - coast
                self.missed_ticks += 1
                self.next_expected_tick += self.period_estimate
                
                if self.missed_ticks > self.max_missed_ticks:
                    logger.warning(
                        f"[{self.name}] Lost lock after {self.missed_ticks} misses"
                    )
                    self.state = PLLState.HUNT
                    self.locked_for_ticks = 0
                else:
                    self.state = PLLState.COAST
                
                return None
    
    def _check_minute_marker(self, env: np.ndarray, tick_idx: int) -> bool:
        """
        Check if tick is a minute marker (800ms) vs regular tick (5ms).
        
        Returns True if energy persists beyond 200ms (indicating long tone).
        """
        lookahead_samples = int(0.2 * self.fs)
        
        if tick_idx + lookahead_samples >= len(env):
            return False
        
        # Measure energy in 200ms following tick
        tail_energy = np.mean(env[tick_idx:tick_idx + lookahead_samples])
        peak_energy = env[tick_idx]
        
        # Minute marker: tail energy is significant fraction of peak
        # Regular tick: tail drops to noise floor quickly
        return tail_energy > (peak_energy * 0.2)
    
    def _check_hour_marker(self, env_hour: np.ndarray, tick_idx: int) -> bool:
        """Check for 1500 Hz hour marker at tick position."""
        window = 100  # samples
        start = max(0, tick_idx - window)
        end = min(len(env_hour), tick_idx + window)
        
        return np.max(env_hour[start:end]) > np.median(env_hour) * 3.0


class BCDIntegrator:
    """
    Decodes WWV/WWVH BCD time code using PLL-gated integration.
    
    The BCD data is a 100 Hz tone with varying duty cycle:
    - Logic 0: 30ms-170ms active (170ms total)
    - Logic 1: 30ms-500ms active (500ms total)  
    - Position marker: 30ms-800ms active (800ms total)
    
    Integration windows are relative to the tick start (T+0).
    """
    
    def __init__(self, fs: int = 24000):
        self.fs = fs
        
        # BCD timing (relative to tick start)
        self.t_start = int(0.030 * fs)      # BCD starts at T+30ms
        self.t_logic0 = int(0.170 * fs)     # End of logic 0 (170ms total)
        self.t_logic1 = int(0.500 * fs)     # End of logic 1 (500ms total)
        self.t_marker = int(0.800 * fs)     # End of position marker (800ms total)
        
    def decode(
        self,
        env_100hz: np.ndarray,
        tick_idx: int,
        station: str
    ) -> Tuple[str, float, bool]:
        """
        Decode BCD bit at given tick position.
        
        Args:
            env_100hz: 100 Hz envelope
            tick_idx: Sample index of tick start
            station: Station name for logging
            
        Returns:
            (bit, confidence, collision_suspected)
            bit: '0', '1', 'P', or '?'
        """
        start_idx = tick_idx + self.t_start
        end_idx = start_idx + self.t_marker + int(0.1 * self.fs)
        
        if end_idx >= len(env_100hz):
            return '?', 0.0, False
        
        # Measure energy in three zones
        zone_a = env_100hz[start_idx:start_idx + self.t_logic0]
        zone_b = env_100hz[start_idx + self.t_logic0:start_idx + self.t_logic1]
        zone_c = env_100hz[start_idx + self.t_logic1:start_idx + self.t_marker]
        
        # Dynamic threshold
        noise_floor = np.percentile(env_100hz[start_idx:end_idx], 10)
        threshold = noise_floor * 3.0
        
        # Boolean presence detection
        has_a = np.mean(zone_a) > threshold
        has_b = np.mean(zone_b) > threshold
        has_c = np.mean(zone_c) > threshold
        
        # Decode logic
        if has_c:
            bit = 'P'  # Position marker
            collision = False
        elif has_b:
            bit = '1'
            # Suspect collision if our signal might be weak
            collision = np.mean(zone_a) < threshold * 1.5
        elif has_a:
            bit = '0'
            collision = False
        else:
            bit = '?'  # Miss/no signal
            collision = False
        
        # Confidence based on signal-to-noise
        peak = np.max(env_100hz[start_idx:end_idx])
        if peak > 0:
            total_energy = np.sum(zone_a) + np.sum(zone_b) + np.sum(zone_c)
            confidence = (total_energy / peak) / len(zone_c)
            confidence = min(1.0, confidence)
        else:
            confidence = 0.0
        
        return bit, confidence, collision


class DualStationPLL:
    """
    Dual-station PLL decoder for simultaneous WWV + WWVH reception.
    
    Runs independent PLLs for each station on the same audio stream.
    Handles "wired-OR" collision case where both stations transmit
    simultaneously (different frequencies, same 100 Hz BCD).
    """
    
    def __init__(self, fs: int = 24000):
        self.fs = fs
        
        # Independent PLLs
        self.pll_wwv = TickPLL("WWV", 1000.0, fs)
        self.pll_wwvh = TickPLL("WWVH", 1200.0, fs)
        
        # Shared BCD decoder
        self.bcd = BCDIntegrator(fs)
        
        # Pre-compute 100 Hz filter for BCD
        nyq = 0.5 * fs
        self.b_100, self.a_100 = butter(2, [80/nyq, 120/nyq], btype='band')
        
        logger.info("DualStationPLL initialized")
    
    def process_minute(
        self,
        audio: np.ndarray,
        minute_boundary_sample: int,
        buffer_timing=None,
        station_filter: Optional[List[str]] = None
    ) -> List[MinutePLLAnalysis]:
        """
        Process a full minute of audio (60 seconds for WWV, 55 for WWVH).
        
        Args:
            audio: Full minute of audio (should be ~60s * 24000 = 1.44M samples)
            minute_boundary_sample: Sample index of minute start (second 0)
            buffer_timing: BufferTiming for UTC conversion
            station_filter: Optional list of stations to process ["WWV", "WWVH"]
            
        Returns:
            List of MinutePLLAnalysis (one per station successfully decoded)
        """
        results = []
        
        # Pre-compute 100 Hz envelope for BCD decoding
        filt_100 = filtfilt(self.b_100, self.a_100, audio)
        env_100 = np.abs(hilbert(filt_100))
        
        # Process each station
        stations_to_process = station_filter or ["WWV", "WWVH"]
        
        for station in stations_to_process:
            pll = self.pll_wwv if station == "WWV" else self.pll_wwvh
            
            # Reset PLL for fresh minute
            pll.reset()
            
            # Expected ticks per minute
            expected_ticks = 60 if station == "WWV" else 55
            
            # Collect tick results
            tick_results = []
            bcd_bits = []
            
            # Process audio in 1-second chunks
            chunk_size = self.fs
            for sec in range(70):  # Slightly more than minute to catch trailing ticks
                chunk_start = sec * chunk_size
                chunk_end = chunk_start + chunk_size
                
                if chunk_end > len(audio):
                    break
                
                chunk = audio[chunk_start:chunk_end]
                abs_start = minute_boundary_sample + chunk_start
                
                # Process this second
                ticks = pll.process_buffer(chunk, abs_start, buffer_timing)
                
                for tick in ticks:
                    tick_results.append(tick)
                    
                    # Decode BCD at this tick
                    if tick.tick_index < len(env_100) - int(0.9 * self.fs):
                        abs_tick_idx = chunk_start + tick.tick_index
                        bit, conf, coll = self.bcd.decode(
                            env_100, abs_tick_idx, station
                        )
                        tick.bcd_bit = bit
                        tick.bcd_confidence = conf
                        tick.collision_suspected = coll
                        bcd_bits.append(bit)
            
            # Compile minute analysis
            if tick_results:
                analysis = self._compile_analysis(
                    station, tick_results, bcd_bits, minute_boundary_sample
                )
                results.append(analysis)
        
        return results
    
    def _compile_analysis(
        self,
        station: str,
        tick_results: List[PLLTickResult],
        bcd_bits: List[str],
        minute_boundary: int
    ) -> MinutePLLAnalysis:
        """Compile tick results into minute-level analysis."""
        
        # Count statistics
        n_ticks = len(tick_results)
        n_minute = sum(1 for t in tick_results if t.is_minute_mark)
        n_hour = sum(1 for t in tick_results if t.is_hour_mark)
        
        # Timing statistics
        timing_offsets = [t.phase_error_samples for t in tick_results]
        mean_offset = np.mean(timing_offsets) if timing_offsets else 0.0
        std_offset = np.std(timing_offsets) if len(timing_offsets) > 1 else 0.0
        
        # Convert to ms
        mean_offset_ms = mean_offset / self.fs * 1000.0
        std_offset_ms = std_offset / self.fs * 1000.0
        
        # Lock quality
        lock_durations = [t.lock_duration_sec for t in tick_results]
        avg_lock = np.mean(lock_durations) if lock_durations else 0.0
        lock_quality = min(1.0, avg_lock / 30.0)  # Normalize to 30 seconds
        
        # BCD frame
        bcd_frame = ''.join(bcd_bits) if bcd_bits else None
        bcd_conf = np.mean([t.bcd_confidence for t in tick_results if t.bcd_confidence > 0])
        
        return MinutePLLAnalysis(
            minute_timestamp=minute_boundary / self.fs,  # Convert to Unix time
            station=station,
            n_ticks_detected=n_ticks,
            n_minute_markers=n_minute,
            n_hour_markers=n_hour,
            mean_timing_offset_ms=mean_offset_ms,
            std_timing_offset_ms=std_offset_ms,
            d_clock_ms=mean_offset_ms,
            bcd_frame=bcd_frame,
            bcd_confidence=bcd_conf,
            decoder_variant="pll",
            lock_quality=lock_quality
        )


class TickPLLDecoder:
    """
    Wrapper class for PLL decoder to match metrology_engine interface.
    
    This provides a compatible interface for A/B comparison with TickMatchedFilter.
    """
    
    def __init__(
        self,
        sample_rate: int,
        station_type: str,
        window_ms: float = 40.0,
        alpha: float = 0.1,
        max_missed: int = 5
    ):
        """
        Initialize PLL decoder.
        
        Args:
            sample_rate: Audio sample rate
            station_type: Station name ("WWV", "WWVH", etc.)
            window_ms: PLL gating window width
            alpha: Loop filter gain (0.0-1.0)
            max_missed: Maximum missed ticks before losing lock
        """
        self.sample_rate = sample_rate
        self.station_type = station_type
        
        # Map station to tick frequency
        freq_map = {
            "WWV": 1000.0,
            "WWVH": 1200.0,
        }
        
        if station_type not in freq_map:
            logger.warning(f"PLL decoder not supported for {station_type}, using stub")
            self.pll = None
        else:
            self.pll = TickPLL(
                station_name=station_type,
                tick_freq=freq_map[station_type],
                fs=sample_rate,
                window_ms=window_ms,
                alpha=alpha,
                max_missed_ticks=max_missed
            )
    
    def process_minute(
        self,
        iq_samples: np.ndarray,
        minute_number: int = 0,
        buffer_timing=None,
        minute_boundary: int = 0
    ):
        """
        Process a minute of IQ samples.
        
        Args:
            iq_samples: Complex IQ samples (60 seconds)
            minute_number: Minute within hour
            buffer_timing: BufferTiming object
            minute_boundary: Unix timestamp of minute boundary
            
        Returns:
            MinutePLLAnalysis or None if not supported
        """
        if self.pll is None:
            # Stub for unsupported stations
            return MinutePLLAnalysis(
                minute_timestamp=float(minute_boundary),
                station=self.station_type,
                n_ticks_detected=0,
                n_minute_markers=0,
                n_hour_markers=0,
                mean_timing_offset_ms=0.0,
                std_timing_offset_ms=0.0,
                bcd_frame=None,
                bcd_confidence=0.0,
                decoder_variant="pll",
                lock_quality=0.0
            )
        
        # Convert IQ to audio (magnitude)
        audio = np.abs(iq_samples)
        
        # Reset PLL for fresh minute
        self.pll.reset()
        
        # Collect tick results
        tick_results = []
        
        # Process audio in 1-second chunks
        chunk_size = self.sample_rate
        for sec in range(60):
            chunk_start = sec * chunk_size
            chunk_end = chunk_start + chunk_size
            
            if chunk_end > len(audio):
                break
            
            chunk = audio[chunk_start:chunk_end]
            abs_start = minute_boundary * self.sample_rate + chunk_start
            
            # Process this second
            ticks = self.pll.process_buffer(chunk, abs_start, buffer_timing)
            tick_results.extend(ticks)
        
        # Compile results
        if not tick_results:
            return MinutePLLAnalysis(
                minute_timestamp=float(minute_boundary),
                station=self.station_type,
                n_ticks_detected=0,
                n_minute_markers=0,
                n_hour_markers=0,
                mean_timing_offset_ms=0.0,
                std_timing_offset_ms=0.0,
                d_clock_ms=None,
                bcd_frame=None,
                bcd_confidence=0.0,
                decoder_variant="pll",
                lock_quality=0.0
            )
        
        # Count statistics
        n_ticks = len(tick_results)
        n_minute = sum(1 for t in tick_results if t.is_minute_mark)
        n_hour = sum(1 for t in tick_results if t.is_hour_mark)
        
        # Timing statistics
        timing_offsets = [t.phase_error_samples for t in tick_results]
        mean_offset = np.mean(timing_offsets) if timing_offsets else 0.0
        std_offset = np.std(timing_offsets) if len(timing_offsets) > 1 else 0.0
        
        # Convert to ms
        mean_offset_ms = mean_offset / self.sample_rate * 1000.0
        std_offset_ms = std_offset / self.sample_rate * 1000.0
        
        # Lock quality
        lock_durations = [t.lock_duration_sec for t in tick_results]
        avg_lock = np.mean(lock_durations) if lock_durations else 0.0
        lock_quality = min(1.0, avg_lock / 30.0)
        
        return MinutePLLAnalysis(
            minute_timestamp=float(minute_boundary),
            station=self.station_type,
            n_ticks_detected=n_ticks,
            n_minute_markers=n_minute,
            n_hour_markers=n_hour,
            mean_timing_offset_ms=mean_offset_ms,
            std_timing_offset_ms=std_offset_ms,
            d_clock_ms=mean_offset_ms,
            bcd_frame=None,
            bcd_confidence=0.0,
            decoder_variant="pll",
            lock_quality=lock_quality
        )


def create_pll_decoder(station: str, fs: int = 24000) -> TickPLL:
    """
    Factory function to create appropriate PLL for station.
    
    Args:
        station: "WWV", "WWVH", "CHU", "BPM"
        fs: Sample rate
        
    Returns:
        Configured TickPLL instance
    """
    if station == "WWV":
        return TickPLL("WWV", 1000.0, fs)
    elif station == "WWVH":
        return TickPLL("WWVH", 1200.0, fs)
    else:
        raise ValueError(f"PLL decoder not supported for station: {station}")
