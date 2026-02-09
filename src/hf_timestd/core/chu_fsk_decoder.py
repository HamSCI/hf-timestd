#!/usr/bin/env python3
"""
CHU FSK Time Code Decoder

Decodes the Bell 103 compatible FSK time code broadcast by CHU (Canada)
during seconds 31-39 of each minute. This provides:
- Precise timing reference (500ms boundary)
- Time verification (decoded UTC time)
- DUT1 correction (UT1-UTC offset)
- TAI-UTC offset (leap seconds)
- Channel quality metric (decode success rate)

CHU FSK Signal Structure:
-------------------------
- Frequencies: 2225 Hz (mark), 2025 Hz (space)
- Baud rate: 300 bps (3.333ms per bit)
- Frame format: 1 start + 8 data + 1 parity + 1 stop = 11 bits per byte
- Parity: EVEN parity on 8 data bits
- 10 bytes per second (5 data + 5 redundancy)

Timing per second (31-39):
- 0-10ms: 1000 Hz tick (10 cycles)
- 10-133ms: Mark tone (2225 Hz modem sync)
- 133-500ms: Data stream (110 bits @ 300 baud = 366.67ms)
- Last stop bit ends at EXACTLY 500ms - this is our precise timing reference!

Frame Types:
- Frame A (seconds 32-39): 6d dd hh mm ss (BCD day/time)
- Frame B (second 31): xz yy yy tt aa (DUT1, year, TAI-UTC, DST pattern)

Author: HF Time Standard Team
"""

import numpy as np
import logging
from dataclasses import dataclass, field
from typing import Optional, Tuple, List, Dict
from scipy.signal import butter, filtfilt, hilbert

logger = logging.getLogger(__name__)

# FSK Constants
MARK_FREQ = 2225.0  # Hz - logic 1
SPACE_FREQ = 2025.0  # Hz - logic 0
BAUD_RATE = 300  # bits per second
BIT_DURATION_MS = 1000.0 / BAUD_RATE  # 3.333... ms

# Frame timing (relative to second boundary)
TICK_END_MS = 10.0  # End of 1000 Hz tick
MARK_START_MS = 10.0  # Start of mark sync tone
DATA_START_MS = 100.0  # Start of FSK data (search from here)
DATA_END_MS = 500.0  # End of FSK data (precise timing reference!)
BITS_PER_FRAME = 110  # 10 bytes × 11 bits

# Valid FSK seconds
FSK_SECONDS = [31, 32, 33, 34, 35, 36, 37, 38, 39]


@dataclass
class CHUFrameA:
    """Frame A: Time of day (seconds 32-39)"""
    day_of_year: int  # 1-366
    hour: int  # 0-23
    minute: int  # 0-59
    second: int  # 32-39
    valid: bool = False
    
    def __str__(self):
        return f"Day {self.day_of_year:03d} {self.hour:02d}:{self.minute:02d}:{self.second:02d} UTC"


@dataclass
class CHUFrameB:
    """Frame B: Auxiliary data (second 31)"""
    dut1_tenths: int  # Absolute value of DUT1 in 0.1s
    dut1_negative: bool  # True if DUT1 is negative
    year: int  # Gregorian year (4 digits)
    tai_utc: int  # TAI - UTC in seconds (leap second count)
    dst_pattern: int  # Canadian DST pattern code
    valid: bool = False
    
    @property
    def dut1_seconds(self) -> float:
        """Get DUT1 in seconds (signed)"""
        return -self.dut1_tenths / 10.0 if self.dut1_negative else self.dut1_tenths / 10.0
    
    def __str__(self):
        sign = '-' if self.dut1_negative else '+'
        return f"Year {self.year}, DUT1={sign}{self.dut1_tenths/10:.1f}s, TAI-UTC={self.tai_utc}s"


@dataclass
class CHUFSKResult:
    """Result of CHU FSK decoding for one minute"""
    detected: bool = False
    frames_decoded: int = 0
    frames_total: int = 9  # Seconds 31-39
    
    # Decoded time (from Frame A)
    decoded_day: Optional[int] = None
    decoded_hour: Optional[int] = None
    decoded_minute: Optional[int] = None
    
    # Auxiliary data (from Frame B)
    dut1_seconds: Optional[float] = None
    year: Optional[int] = None
    tai_utc: Optional[int] = None
    
    # Timing precision (2026-01-24 Enhancement: dual timing references)
    timing_offset_ms: Optional[float] = None  # FSK 500ms boundary (secondary, ~1-2ms precision)
    tick_timing_offset_ms: Optional[float] = None  # 1000 Hz tick onset (primary, ~0.05ms precision)
    tick_timing_count: int = 0  # Number of valid tick timing measurements
    
    # Quality metrics
    snr_db: Optional[float] = None  # FSK signal SNR
    bit_error_rate: Optional[float] = None  # Estimated BER from redundancy
    decode_confidence: float = 0.0  # 0-1 based on frame decode success
    
    # Per-second details
    frame_results: List[Dict] = field(default_factory=list)


class CHUFSKDecoder:
    """
    Decode CHU FSK time code for precise timing and time verification.
    
    Usage:
        decoder = CHUFSKDecoder(sample_rate=20000)
        result = decoder.decode_minute(iq_samples, minute_boundary_unix)
        
        if result.detected:
            print(f"Decoded time: Day {result.decoded_day} {result.decoded_hour}:{result.decoded_minute}")
            print(f"Timing offset: {result.timing_offset_ms:.3f} ms")
            print(f"DUT1: {result.dut1_seconds:.1f} s")
    """
    
    def __init__(self, sample_rate: int = 20000, channel_name: str = "CHU"):
        self.sample_rate = sample_rate
        self.channel_name = channel_name
        
        # Samples per bit (keep as int for backward compatibility, use float in critical paths)
        self.samples_per_bit = int(sample_rate / BAUD_RATE)
        
        # Pre-compute mixing oscillator for FSK demodulation (-2125 Hz)
        # Create 1.2 second buffer to handle full second plus margin
        t = np.arange(int(sample_rate * 1.2)) / sample_rate
        self._mixing_oscillator = np.exp(-2j * np.pi * 2125 * t).astype(np.complex64)
        
        # Pre-compute LPF coefficients for FSK filtering
        from scipy.signal import firwin, butter
        nyq = sample_rate / 2
        self._lpf_coeffs = firwin(101, 300 / nyq)
        
        # Pre-compute tick detection filter (bandpass 900-1100 Hz)
        low = 900 / nyq
        high = min(1100 / nyq, 0.99)
        self._tick_b, self._tick_a = butter(4, [low, high], btype='band')
        
        logger.debug(f"CHU FSK Decoder initialized: {sample_rate} Hz, {self.samples_per_bit} samples/bit")
    
    def _fsk_demodulate_audio(self, audio: np.ndarray) -> np.ndarray:
        """
        FSK demodulation from audio using Bell 103 DSP chain.
        
        CHU FSK Signal Structure:
        - Audio tones at 2025 Hz (space/0) and 2225 Hz (mark/1)
        - 300 baud, so each bit is ~3.33ms
        
        DSP Flow:
        1. Frequency translate by -2125 Hz to center FSK at DC
        2. Low-pass filter at 300 Hz to reject ticks and voice
        3. Quadrature demod: Δθ = angle(sample[n] × conj(sample[n-1]))
        
        After translation: mark is at +100 Hz, space is at -100 Hz.
        """
        from scipy.signal import filtfilt
        
        # Convert real audio to analytic signal for frequency translation
        # Use Hilbert transform to create complex signal
        analytic = hilbert(audio.astype(np.float64))
        
        # Step 1: Frequency translate by -2125 Hz using pre-computed oscillator
        n_samples = len(analytic)
        if n_samples <= len(self._mixing_oscillator):
            shift = self._mixing_oscillator[:n_samples]
        else:
            # Fallback for unexpectedly long input
            t = np.arange(n_samples) / self.sample_rate
            shift = np.exp(-2j * np.pi * 2125 * t).astype(np.complex64)
        
        baseband = analytic.astype(np.complex64) * shift
        
        # Step 2: Low-pass filter using pre-computed coefficients
        # Use filtfilt for zero-phase filtering (no group delay)
        filtered = filtfilt(self._lpf_coeffs, 1.0, baseband)
        
        # Step 3: Quadrature demodulation
        # Δθ = angle(sample[n] × conj(sample[n-1]))
        delta_phase = np.angle(filtered[1:] * np.conj(filtered[:-1]))
        delta_phase = np.concatenate([[0], delta_phase])
        
        # Convert to frequency: f = Δθ * sample_rate / (2π)
        inst_freq = delta_phase * self.sample_rate / (2 * np.pi)
        
        # Normalize: +100 Hz (mark) -> +1, -100 Hz (space) -> -1
        # Standard UART: Mark (2225 Hz) = Logic 1 (Idle/Stop)
        #                Space (2025 Hz) = Logic 0 (Start)
        # After mixing by -2125 Hz: Mark = +100 Hz, Space = -100 Hz
        # So: positive inst_freq = Mark = Logic 1, negative = Space = Logic 0
        soft_decision = inst_freq / 100.0
        
        return soft_decision
    
    def _am_demodulate(self, iq_samples: np.ndarray) -> np.ndarray:
        """
        AM demodulate IQ samples to extract audio.
        
        CHU Signal Structure:
        - Carrier is at DC in baseband IQ
        - Audio (including FSK tones) is AM modulated onto the carrier
        - FSK tones are at 2025/2225 Hz in the audio spectrum
        
        Returns audio signal (not FSK soft decisions - that's done in decode_second).
        """
        # AM demodulation - extract envelope from IQ
        audio = np.abs(iq_samples).astype(np.float64)
        
        # Remove DC (carrier level) and normalize
        audio = audio - np.mean(audio)
        
        # Normalize to reasonable amplitude
        max_amp = np.max(np.abs(audio))
        if max_amp > 0:
            audio = audio / max_amp
        
        # Log diagnostic info
        iq_power_db = 10 * np.log10(np.mean(np.abs(iq_samples)**2) + 1e-10)
        logger.info(f"[CHU] AM demod: IQ_power={iq_power_db:.1f}dB, audio_peak={max_amp:.4f}, "
                   f"samples={len(iq_samples)}")
        
        return audio
    
    def _fsk_demodulate(self, audio: np.ndarray) -> np.ndarray:
        """
        FSK demodulate audio to get soft decisions.
        
        Takes AM-demodulated audio and extracts FSK soft decisions.
        """
        return self._fsk_demodulate_audio(audio)
    
    def _find_first_start_bit(self, soft_decision: np.ndarray, search_start: int, search_end: int, expected_byte: Optional[int] = 0x06) -> Optional[int]:
        """
        Find first start bit.
        
        If expected_byte is provided (Frame A), matches full 11-bit pattern.
        If None (Frame B at second 31), looks for simple Mark-to-Space transition.
        
        Standard UART: 0 = negative soft decision (space), 1 = positive (mark)
        """
        samples_per_bit_float = self.sample_rate / BAUD_RATE
        
        # --- STRATEGY 1: Pattern Match (Frame A) ---
        if expected_byte is not None:
            # Construct pattern: Start(0) + 8 data bits (LSB) + Parity + Stop(1)
            parity_bit = bin(expected_byte).count('1') % 2
            
            expected = [0]  # Start bit
            for i in range(8):
                expected.append((expected_byte >> i) & 1)
            expected.append(parity_bit)
            expected.append(1)  # Stop bit
            
            best_match = 0
            best_pos = None
            
            step = max(1, int(samples_per_bit_float / 4))
            
            for i in range(search_start, min(search_end, len(soft_decision) - int(11 * samples_per_bit_float)), step):
                bits = []
                for j in range(11):
                    bit_center = int(i + (j + 0.5) * samples_per_bit_float)
                    if bit_center >= len(soft_decision):
                        break
                    val = soft_decision[bit_center]
                    bits.append(1 if val > 0 else 0)
                
                if len(bits) < 11:
                    continue
                
                match = sum(1 for a, b in zip(bits, expected) if a == b)
                
                if match > best_match:
                    best_match = match
                    best_pos = i
            
            if best_match >= 9:
                return best_pos
            return None
        
        # --- STRATEGY 2: Edge Detection (Frame B) ---
        else:
            # Look for transition from Mark (>0.2) to Space (<-0.2)
            check_len = int(samples_per_bit_float / 2)
            
            for i in range(search_start + 1, min(search_end, len(soft_decision) - check_len)):
                if soft_decision[i-1] > 0.2 and soft_decision[i] < -0.2:
                    # Verify it stays low for at least half a bit
                    if np.mean(soft_decision[i:i+check_len]) < -0.2:
                        return i
            return None
    
    def _extract_bits(self, soft_decision: np.ndarray, start_sample: int, num_bits: int) -> Tuple[List[int], float]:
        """
        Extract bits from soft decision signal using robust timing.
        Avoids cumulative drift by calculating exact float positions.
        """
        bits = []
        confidences = []
        
        # Calculate samples per bit as float
        samples_per_bit_float = self.sample_rate / BAUD_RATE
        
        for i in range(num_bits):
            # Calculate exact start/end for this bit relative to start_sample
            # Rounding to nearest integer for slice indices
            bit_start_exact = start_sample + (i * samples_per_bit_float)
            
            bit_start_idx = int(bit_start_exact)
            bit_end_idx = int(start_sample + ((i + 1) * samples_per_bit_float))
            
            if bit_end_idx > len(soft_decision):
                break
            
            # Sample in middle 50% of the bit window
            # Window length for this specific bit (handles jitter)
            bit_len = bit_end_idx - bit_start_idx
            
            mid_start = bit_start_idx + bit_len // 4
            mid_end = bit_start_idx + 3 * bit_len // 4
            
            # Ensure valid window
            if mid_end <= mid_start:
                 mid_start = bit_start_idx
                 mid_end = bit_end_idx
                 
            bit_value = np.mean(soft_decision[mid_start:mid_end])
            
            bits.append(1 if bit_value > 0 else 0)
            confidences.append(abs(bit_value))
        
        avg_confidence = np.mean(confidences) if confidences else 0.0
        return bits, avg_confidence
    
    def _bits_to_bytes(self, bits: List[int]) -> List[int]:
        """
        Convert bit stream to bytes (1 start + 8 data + 1 parity + 1 stop = 11 bits per byte)
        
        CHU uses EVEN PARITY on the 8 data bits. This is critical for detecting
        frame slips and corrupted data that could cause timing errors.
        
        Returns list of decoded bytes, or empty list if framing/parity error
        """
        bytes_out = []
        
        for byte_num in range(10):  # 10 bytes per frame
            bit_offset = byte_num * 11
            
            if bit_offset + 11 > len(bits):
                break
            
            # Check start bit (should be 0/space)
            start_bit = bits[bit_offset]
            if start_bit != 0:
                logger.debug(f"Framing error: start bit is 1 at byte {byte_num}")
                return []  # Reject entire frame on start bit error
            
            # Extract 8 data bits (LSB first)
            data_byte = 0
            for i in range(8):
                if bits[bit_offset + 1 + i]:
                    data_byte |= (1 << i)
            
            # ===== NEW: Check even parity =====
            parity_bit = bits[bit_offset + 9]  # Bit 9 is parity
            data_parity = bin(data_byte).count('1') % 2  # Count 1s in data (0=even, 1=odd)
            
            # CHU uses EVEN parity: parity bit should be 1 if data has odd number of 1s
            expected_parity = data_parity  # For even parity system
            
            if parity_bit != expected_parity:
                logger.debug(f"Parity error at byte {byte_num}: data=0x{data_byte:02x}, "
                            f"data_parity={data_parity}, received_parity={parity_bit}")
                return []  # Reject entire frame on parity error
            
            # Check stop bit (should be 1/mark)
            stop_bit = bits[bit_offset + 10]
            if stop_bit != 1:
                logger.debug(f"Framing error: stop bit wrong at byte {byte_num}")
                return []  # Reject entire frame on stop bit error
            
            bytes_out.append(data_byte)
        
        return bytes_out
    
    def _swap_nibbles(self, byte_val: int) -> int:
        """Swap least and most significant nibbles in a byte"""
        return ((byte_val & 0x0F) << 4) | ((byte_val & 0xF0) >> 4)
    
    def _decode_frame_a(self, raw_bytes: List[int]) -> Optional[CHUFrameA]:
        """Decode Frame A (time of day) from raw bytes"""
        if len(raw_bytes) < 10:
            return None
        
        # Check redundancy (bytes 5-9 should equal bytes 0-4)
        data_bytes = raw_bytes[:5]
        redundancy = raw_bytes[5:10]
        
        if data_bytes != redundancy:
            logger.debug("Frame A redundancy check failed")
            return None
        
        # Swap nibbles in each byte
        swapped = [self._swap_nibbles(b) for b in data_bytes]
        
        # Parse BCD: 6d dd hh mm ss
        # Byte 0: 0x6d where d is high digit of day
        marker = (swapped[0] >> 4) & 0x0F
        if marker != 6:
            logger.debug(f"Frame A marker invalid: {marker}")
            return None
        
        day_high = swapped[0] & 0x0F
        day_mid = (swapped[1] >> 4) & 0x0F
        day_low = swapped[1] & 0x0F
        day = day_high * 100 + day_mid * 10 + day_low
        
        hour_high = (swapped[2] >> 4) & 0x0F
        hour_low = swapped[2] & 0x0F
        hour = hour_high * 10 + hour_low
        
        min_high = (swapped[3] >> 4) & 0x0F
        min_low = swapped[3] & 0x0F
        minute = min_high * 10 + min_low
        
        sec_high = (swapped[4] >> 4) & 0x0F
        sec_low = swapped[4] & 0x0F
        second = sec_high * 10 + sec_low
        
        # Validate ranges
        if not (1 <= day <= 366 and 0 <= hour <= 23 and 0 <= minute <= 59 and 32 <= second <= 39):
            logger.debug(f"Frame A values out of range: day={day}, hour={hour}, min={minute}, sec={second}")
            return None
        
        return CHUFrameA(
            day_of_year=day,
            hour=hour,
            minute=minute,
            second=second,
            valid=True
        )
    
    def _decode_frame_b(self, raw_bytes: List[int]) -> Optional[CHUFrameB]:
        """Decode Frame B (auxiliary data) from raw bytes"""
        if len(raw_bytes) < 10:
            return None
        
        # Check redundancy (bytes 5-9 should be inverted bytes 0-4)
        data_bytes = raw_bytes[:5]
        redundancy = raw_bytes[5:10]
        
        inverted = [(~b) & 0xFF for b in data_bytes]
        if inverted != redundancy:
            logger.debug("Frame B redundancy check failed")
            return None
        
        # Swap nibbles in each byte
        swapped = [self._swap_nibbles(b) for b in data_bytes]
        
        # Parse: xz yy yy tt aa
        # x: DUT1 sign (even = positive, odd = negative)
        # z: |DUT1| in tenths of seconds
        x_nibble = (swapped[0] >> 4) & 0x0F
        z_nibble = swapped[0] & 0x0F
        
        dut1_negative = (x_nibble % 2) == 1
        dut1_tenths = z_nibble
        
        # Year (4 BCD digits in bytes 1-2)
        year_1000 = (swapped[1] >> 4) & 0x0F
        year_100 = swapped[1] & 0x0F
        year_10 = (swapped[2] >> 4) & 0x0F
        year_1 = swapped[2] & 0x0F
        year = year_1000 * 1000 + year_100 * 100 + year_10 * 10 + year_1
        
        # TAI-UTC (2 BCD digits)
        tai_high = (swapped[3] >> 4) & 0x0F
        tai_low = swapped[3] & 0x0F
        tai_utc = tai_high * 10 + tai_low
        
        # DST pattern (2 BCD digits)
        dst_high = (swapped[4] >> 4) & 0x0F
        dst_low = swapped[4] & 0x0F
        dst_pattern = dst_high * 10 + dst_low
        
        # Validate
        if not (1990 <= year <= 2100 and 0 <= tai_utc <= 99):
            logger.debug(f"Frame B values out of range: year={year}, tai_utc={tai_utc}")
            return None
        
        return CHUFrameB(
            dut1_tenths=dut1_tenths,
            dut1_negative=dut1_negative,
            year=year,
            tai_utc=tai_utc,
            dst_pattern=dst_pattern,
            valid=True
        )
    
    def _find_consensus_time(self, frames: List[CHUFrameA]) -> Optional[Dict]:
        """
        Find consensus time from multiple Frame A decodes.
        
        CHU broadcasts Frame A in seconds 32-39 (8 repetitions). We require
        majority agreement to reject frame slips and corrupted decodes.
        
        Args:
            frames: List of decoded Frame A results
        
        Returns:
            Dictionary with consensus time and confidence, or None if no consensus
        """
        from collections import Counter
        
        # Extract time tuples from valid frames
        time_tuples = [(f.day_of_year, f.hour, f.minute) for f in frames if f.valid]
        
        if not time_tuples:
            return None
        
        # Find most common time
        counter = Counter(time_tuples)
        most_common, count = counter.most_common(1)[0]
        
        # Require at least 50% agreement
        confidence = count / len(time_tuples)
        if confidence < 0.5:
            logger.warning(f"CHU consensus failed: only {count}/{len(time_tuples)} frames agree")
            return None
        
        day, hour, minute = most_common
        
        return {
            'day': day,
            'hour': hour,
            'minute': minute,
            'confidence': confidence,
            'agreement': f"{count}/{len(time_tuples)}"
        }
    
    def _validate_time_consistency(
        self,
        decoded_time: Dict,
        expected_dt: 'datetime'
    ) -> bool:
        """
        Validate decoded time against expected time.
        
        The decoded time should be within ±1 hour of the system time.
        This catches frame slips and major decoding errors.
        
        Args:
            decoded_time: Dictionary with day, hour, minute
            expected_dt: Expected datetime from system
        
        Returns:
            True if times are consistent (within ±1 hour)
        """
        from datetime import datetime, timedelta, timezone
        
        year = expected_dt.year
        day_of_year = decoded_time['day']
        hour = decoded_time['hour']
        minute = decoded_time['minute']
        
        try:
            # Create datetime from day of year
            decoded_dt = datetime(year, 1, 1, tzinfo=timezone.utc) + timedelta(days=day_of_year - 1)
            decoded_dt = decoded_dt.replace(hour=hour, minute=minute, second=0)
        except ValueError as e:
            logger.error(f"Invalid decoded time: day={day_of_year}, hour={hour}, minute={minute}: {e}")
            return False
        
        # Check if within ±1 hour
        delta_seconds = abs((decoded_dt - expected_dt).total_seconds())
        
        if delta_seconds > 3600:  # More than 1 hour off
            logger.warning(f"CHU time inconsistency: decoded={decoded_dt.isoformat()}, "
                          f"expected={expected_dt.isoformat()}, delta={delta_seconds:.0f}s")
            return False
        
        return True
    
    def detect_tick_onset(
        self,
        audio: np.ndarray,
        expected_sample: int,
        search_window_ms: float = 20.0
    ) -> Tuple[float, float, float]:
        """
        Detect precise onset of 1000 Hz tick for high-precision timing.
        
        CHU transmits a 10-cycle (10ms) 1000 Hz tick at the start of each second.
        This tick provides much higher timing precision (~0.05ms) than the FSK
        data boundary (~1-2ms).
        
        METROLOGICAL ENHANCEMENT (2026-01-24):
        --------------------------------------
        The tick is hard-keyed (essentially zero rise time), making edge detection
        optimal for timing. This method uses:
        1. Bandpass filtering to isolate 1000 Hz
        2. Energy envelope computation
        3. Rising edge detection with sub-sample interpolation
        
        Args:
            audio: AM demodulated audio signal
            expected_sample: Expected sample index of tick start
            search_window_ms: Search window half-width (default ±20ms)
            
        Returns:
            Tuple of (tick_onset_sample, timing_offset_ms, confidence):
                - tick_onset_sample: Precise sample index of tick onset
                - timing_offset_ms: Offset from expected position
                - confidence: Detection confidence (0-1)
        """
        search_samples = int(search_window_ms * self.sample_rate / 1000)
        search_start = max(0, expected_sample - search_samples)
        search_end = min(len(audio), expected_sample + search_samples)
        
        if search_end <= search_start + 100:
            return float(expected_sample), 0.0, 0.0
        
        search_region = audio[search_start:search_end]
        
        # Bandpass filter around 1000 Hz using pre-computed coefficients
        try:
            filtered = filtfilt(self._tick_b, self._tick_a, search_region)
        except Exception:
            filtered = search_region
        
        # Compute energy envelope
        energy = filtered ** 2
        window_samples = max(3, int(0.002 * self.sample_rate))  # 2ms window
        kernel = np.ones(window_samples) / window_samples
        envelope = np.convolve(energy, kernel, mode='same')
        
        # Estimate noise floor from first 10% of search region
        noise_region_end = max(10, len(envelope) // 10)
        envelope_noise = np.median(envelope[:noise_region_end])
        envelope_max = np.max(envelope)
        
        # Threshold: geometric mean of noise and max
        if envelope_max > envelope_noise * 2:
            threshold = np.sqrt(envelope_noise * envelope_max) * 0.5
        else:
            threshold = envelope_noise + 2 * np.std(envelope[:noise_region_end])
        
        # Find first crossing above threshold (rising edge)
        above_threshold = envelope > threshold
        onset_candidates = np.where(above_threshold)[0]
        
        if len(onset_candidates) == 0:
            return float(expected_sample), 0.0, 0.3
        
        onset_local = onset_candidates[0]
        
        # Sub-sample interpolation at threshold crossing
        sub_sample_offset = 0.0
        if onset_local > 0:
            y_before = envelope[onset_local - 1]
            y_after = envelope[onset_local]
            if y_after > y_before:
                t = (threshold - y_before) / (y_after - y_before)
                sub_sample_offset = t - 1.0
                sub_sample_offset = max(-1.0, min(0.0, sub_sample_offset))
        
        # Convert to global sample index
        tick_onset_sample = search_start + onset_local + sub_sample_offset
        
        # Calculate timing offset from expected
        timing_offset_ms = (tick_onset_sample - expected_sample) / self.sample_rate * 1000
        
        # Confidence based on edge sharpness
        if onset_local + 5 < len(envelope) and onset_local > 0:
            rise = envelope[onset_local + 5] - envelope[onset_local - 1]
            max_rise = envelope_max - envelope_noise
            if max_rise > 0:
                sharpness = min(1.0, rise / max_rise)
                confidence = 0.5 + 0.5 * sharpness
            else:
                confidence = 0.5
        else:
            confidence = 0.5
        
        return tick_onset_sample, timing_offset_ms, confidence
    
    def decode_second(
        self,
        audio: np.ndarray,
        second_start_sample: int,
        second_number: int,
        time_delay_ms: float = 0.0  # Hilbert demodulator has zero delay
    ) -> Tuple[Optional[object], float, float, Optional[float]]:
        """
        Decode one second of CHU FSK data with enhanced tick timing.
        
        METROLOGICAL ENHANCEMENT (2026-01-24):
        Now returns both FSK timing (500ms boundary) and tick timing (second start).
        The tick timing is more precise (~0.05ms vs ~1-2ms for FSK).
        
        Args:
            audio: AM demodulated audio signal
            second_start_sample: Sample index of second boundary
            second_number: Second within minute (31-39)
            time_delay_ms: Offset to account for filter group delay
            
        Returns:
            Tuple of (frame, fsk_timing_offset_ms, confidence, tick_timing_offset_ms):
                - frame: CHUFrameA or CHUFrameB if decoded, None otherwise
                - fsk_timing_offset_ms: Timing offset from expected 500ms boundary
                - confidence: Decode confidence (0-1)
                - tick_timing_offset_ms: High-precision timing from 1000 Hz tick (NEW)
        """
        # === PRIMARY TIMING: Detect 1000 Hz tick onset ===
        # The tick provides ~0.05ms precision vs ~1-2ms from FSK boundary
        tick_onset, tick_timing_offset_ms, tick_confidence = self.detect_tick_onset(
            audio=audio,
            expected_sample=second_start_sample,
            search_window_ms=20.0
        )
        
        if tick_confidence > 0.5:
            logger.debug(f"CHU tick detected: offset={tick_timing_offset_ms:+.3f}ms, "
                        f"confidence={tick_confidence:.2f}")
        
        # FSK demodulate — only the relevant ~1.1s slice, not the full 60s buffer.
        # hilbert() on 1.44M samples creates ~23MB complex128 temporaries per call;
        # doing that 9× per minute fragments glibc malloc arenas, causing RSS to
        # grow monotonically (~2GB after 12h).  Slicing to ~1.1s reduces peak
        # allocation to ~0.4MB, eliminating the fragmentation pressure.
        slice_start = max(0, second_start_sample - int(0.05 * self.sample_rate))  # 50ms margin before
        slice_end = min(len(audio), second_start_sample + int(1.05 * self.sample_rate))  # 1.05s after
        audio_slice = audio[slice_start:slice_end]
        slice_offset = slice_start  # to convert local indices back to global
        
        soft_decision = self._fsk_demodulate(audio_slice)
        
        # Find the first start bit by searching for expected pattern
        # Search from ~50ms to ~200ms into the second (indices relative to slice)
        search_start = (second_start_sample - slice_offset) + int(50 * self.sample_rate / 1000)
        search_end = (second_start_sample - slice_offset) + int(200 * self.sample_rate / 1000)
        
        # Frame A (seconds 32-39) starts with 0x06, Frame B (second 31) has variable first byte
        expected_byte = 0x06 if second_number != 31 else None
        
        data_start_sample = self._find_first_start_bit(soft_decision, search_start, search_end, expected_byte)
        
        if data_start_sample is None:
            # Fallback to fixed offset if start bit not found
            delay_samples = int(time_delay_ms * self.sample_rate / 1000)
            data_start_sample = (second_start_sample - slice_offset) + int(DATA_START_MS * self.sample_rate / 1000) + delay_samples
        
        # Extract bits
        bits, bit_confidence = self._extract_bits(soft_decision, data_start_sample, BITS_PER_FRAME)
        
        if len(bits) < BITS_PER_FRAME:
            # Return tick timing even if FSK decode fails
            return None, 0.0, 0.0, tick_timing_offset_ms if tick_confidence > 0.5 else None
        
        # Convert to bytes
        raw_bytes = self._bits_to_bytes(bits)
        
        if len(raw_bytes) < 10:
            return None, 0.0, bit_confidence, tick_timing_offset_ms if tick_confidence > 0.5 else None
        
        # Decode based on second number
        if second_number == 31:
            frame = self._decode_frame_b(raw_bytes)
        else:
            frame = self._decode_frame_a(raw_bytes)
        
        # Measure timing offset from 500ms boundary (SECONDARY timing reference)
        # The last stop bit should end at exactly 500ms
        # Find where the mark tone ends (transition to silence)
        expected_end_sample = (second_start_sample - slice_offset) + int(DATA_END_MS * self.sample_rate / 1000)
        
        # Look for mark-to-silence transition near expected end
        search_window = int(10 * self.sample_rate / 1000)  # ±10ms
        window_start = max(0, expected_end_sample - search_window)
        window_end = min(len(soft_decision), expected_end_sample + search_window)
        
        if window_end > window_start:
            window = soft_decision[window_start:window_end]
            # Find where soft decision drops (mark ends)
            threshold = np.mean(window) * 0.5
            transitions = np.where(np.diff(window > threshold))[0]
            
            if len(transitions) > 0:
                actual_end = window_start + transitions[-1]
                fsk_timing_offset_ms = (actual_end - expected_end_sample) / self.sample_rate * 1000
            else:
                fsk_timing_offset_ms = 0.0
        else:
            fsk_timing_offset_ms = 0.0
        
        # Return both FSK timing and tick timing (tick is primary, FSK is secondary)
        return frame, fsk_timing_offset_ms, bit_confidence, tick_timing_offset_ms if tick_confidence > 0.5 else None
    
    def decode_minute(
        self,
        iq_samples: np.ndarray,
        minute_boundary_unix: float,
        is_audio: bool = False
    ) -> CHUFSKResult:
        """
        Decode CHU FSK time code for an entire minute.
        
        Processes seconds 31-39 to extract:
        - Time verification from Frame A
        - DUT1, year, TAI-UTC from Frame B
        - Precise timing reference from 500ms boundaries
        
        Args:
            iq_samples: 60 seconds of IQ data (or Audio if is_audio=True)
            minute_boundary_unix: Unix timestamp of minute start
            is_audio: If True, input is treated as demodulated audio (skip AM demod)
            
        Returns:
            CHUFSKResult with decoded data and quality metrics
        """
        result = CHUFSKResult()
        
        # AM demodulate entire buffer if needed
        if is_audio:
            audio = iq_samples
        else:
            audio = self._am_demodulate(iq_samples)
        
        frame_a_results: List[CHUFrameA] = []
        frame_b_result: Optional[CHUFrameB] = None
        fsk_timing_offsets: List[float] = []
        tick_timing_offsets: List[float] = []  # NEW: High-precision tick timing
        confidences: List[float] = []
        
        # Account for fractional second offset in the input data
        # minute_boundary_unix may have a fractional part indicating when recording started
        fractional_offset = minute_boundary_unix % 60
        
        for second in FSK_SECONDS:
            # Calculate second start sample, accounting for fractional offset
            # If recording started at 0.055s into the minute, CHU second 32 is at
            # (32 - 0.055) seconds into the recording
            second_start_sample = int((second - fractional_offset) * self.sample_rate)
            
            if second_start_sample + int(1.0 * self.sample_rate) > len(audio):
                logger.debug(f"Insufficient data for second {second}")
                continue
            
            try:
                # decode_second now returns 4 values (2026-01-24 enhancement)
                frame, fsk_timing_offset, confidence, tick_timing_offset = self.decode_second(
                    audio, second_start_sample, second
                )
                
                result.frame_results.append({
                    'second': second,
                    'decoded': frame is not None,
                    'fsk_timing_offset_ms': fsk_timing_offset,
                    'tick_timing_offset_ms': tick_timing_offset,
                    'confidence': confidence
                })
                
                # Collect tick timing even if FSK decode fails
                if tick_timing_offset is not None:
                    tick_timing_offsets.append(tick_timing_offset)
                
                if frame is not None:
                    result.frames_decoded += 1
                    
                    if isinstance(frame, CHUFrameA):
                        frame_a_results.append(frame)
                    elif isinstance(frame, CHUFrameB):
                        frame_b_result = frame
                    
                    fsk_timing_offsets.append(fsk_timing_offset)
                    confidences.append(confidence)
                    
            except Exception as e:
                logger.debug(f"Error decoding second {second}: {e}")
        
        # Aggregate results
        if result.frames_decoded > 0:
            result.detected = True
            result.decode_confidence = result.frames_decoded / result.frames_total
            
            # ===== NEW: Multi-second consensus with validation =====
            if len(frame_a_results) >= 3:  # Need at least 3 valid decodes for consensus
                from datetime import datetime, timezone
                
                consensus_time = self._find_consensus_time(frame_a_results)
                
                if consensus_time:
                    # Validate against expected time
                    expected_dt = datetime.fromtimestamp(minute_boundary_unix, tz=timezone.utc)
                    
                    if self._validate_time_consistency(consensus_time, expected_dt):
                        result.decoded_day = consensus_time['day']
                        result.decoded_hour = consensus_time['hour']
                        result.decoded_minute = consensus_time['minute']
                        # Update confidence based on consensus
                        result.decode_confidence = consensus_time['confidence']
                        
                        logger.info(f"{self.channel_name} FSK: Consensus validated - "
                                   f"{consensus_time['agreement']} frames agree, "
                                   f"confidence={consensus_time['confidence']:.2f}")
                    else:
                        logger.warning(f"{self.channel_name} FSK: Time consistency check FAILED - "
                                      f"rejecting decode")
                        result.detected = False
                        result.decode_confidence = 0.0
                else:
                    logger.warning(f"{self.channel_name} FSK: Consensus FAILED - "
                                  f"{len(frame_a_results)} frames decoded but no agreement")
                    result.detected = False
                    result.decode_confidence = 0.0
            elif frame_a_results:
                # Fallback: use most common values (old behavior for <3 frames)
                logger.warning(f"{self.channel_name} FSK: Only {len(frame_a_results)} frames - "
                              f"using fallback consensus (not validated)")
                days = [f.day_of_year for f in frame_a_results]
                hours = [f.hour for f in frame_a_results]
                minutes = [f.minute for f in frame_a_results]
                
                result.decoded_day = max(set(days), key=days.count)
                result.decoded_hour = max(set(hours), key=hours.count)
                result.decoded_minute = max(set(minutes), key=minutes.count)
            
            # Get auxiliary data from Frame B
            if frame_b_result:
                result.dut1_seconds = frame_b_result.dut1_seconds
                result.year = frame_b_result.year
                result.tai_utc = frame_b_result.tai_utc
            
            # Timing precision from 500ms boundaries (secondary)
            if fsk_timing_offsets:
                result.timing_offset_ms = np.mean(fsk_timing_offsets)
            
            # HIGH-PRECISION timing from 1000 Hz tick (primary) - 2026-01-24 Enhancement
            if tick_timing_offsets:
                result.tick_timing_offset_ms = np.mean(tick_timing_offsets)
                result.tick_timing_count = len(tick_timing_offsets)
                logger.debug(f"{self.channel_name} FSK: Tick timing from {result.tick_timing_count} seconds: "
                            f"{result.tick_timing_offset_ms:+.3f}ms (high precision)")
            
            # Estimate BER from redundancy failures
            frames_attempted = len(result.frame_results)
            if frames_attempted > 0:
                result.bit_error_rate = 1.0 - (result.frames_decoded / frames_attempted)
            
            # Log with both timing references
            timing_str = f"FSK={result.timing_offset_ms:.3f}ms" if result.timing_offset_ms else "FSK=N/A"
            if result.tick_timing_offset_ms is not None:
                timing_str += f", tick={result.tick_timing_offset_ms:+.3f}ms (n={result.tick_timing_count})"
            
            logger.info(
                f"{self.channel_name} FSK: Decoded {result.frames_decoded}/{result.frames_total} frames, "
                f"timing=[{timing_str}], confidence={result.decode_confidence:.2f}"
            )
            
            if result.dut1_seconds is not None:
                logger.info(
                    f"{self.channel_name} FSK: Year={result.year}, "
                    f"DUT1={result.dut1_seconds:+.1f}s, TAI-UTC={result.tai_utc}s"
                )
        
        return result

