#!/usr/bin/env python3
"""
WWV/WWVH BCD Time Code Decoder

Decodes the IRIG-H format BCD time code broadcast by WWV and WWVH on the
100 Hz subcarrier. This provides:
- Time verification (decoded UTC time)
- Minute boundary confirmation
- DUT1 correction (UT1-UTC offset)
- Leap second warning

WWV/WWVH BCD Signal Structure:
------------------------------
- Subcarrier: 100 Hz sine wave, double-sideband AM on carrier
- Pulse width encoding:
  - Binary 0: 200ms HIGH
  - Binary 1: 500ms HIGH  
  - Position Marker (P): 800ms HIGH
- Position markers at seconds: 0, 9, 19, 29, 39, 49, 59

Time Code Field Layout (60 seconds):
------------------------------------
Second  | Content          | Notes
--------|------------------|----------------------------------
0       | P (marker)       | Frame reference marker (800ms tone)
1       | DST flag         | Daylight saving time status
2       | DST status       | DST at 00:00 UTC
3       | Leap second      | Leap second pending flag
4-7     | Year (ones)      | BCD, little-endian
8       | Unused           |
9       | P (marker)       | Position marker
10-13   | Minute (ones)    | BCD, little-endian
14      | Unused           |
15-17   | Minute (tens)    | BCD, little-endian
18      | Unused           |
19      | P (marker)       | Position marker
20-23   | Hour (ones)      | BCD, little-endian, 24-hour format
24      | Unused           |
25-26   | Hour (tens)      | BCD, little-endian
27-28   | Unused           |
29      | P (marker)       | Position marker
30-33   | Day (ones)       | BCD, little-endian
34      | Unused           |
35-38   | Day (tens)       | BCD, little-endian
39      | P (marker)       | Position marker
40-42   | Day (hundreds)   | BCD, little-endian
43-48   | Unused           |
49      | P (marker)       | Position marker
50      | UT1 sign         | 0 = positive, 1 = negative
51-54   | Year (tens)      | BCD, little-endian
55      | DST status       | DST at 24:00 UTC
56-58   | UT1 magnitude    | Correction in 0.1s units
59      | P (marker)       | Position marker

Author: HF Time Standard Team
"""

import numpy as np
import logging
from dataclasses import dataclass, field
from typing import Optional, Tuple, List
from scipy.signal import butter, filtfilt, hilbert

logger = logging.getLogger(__name__)

# BCD Constants
SUBCARRIER_FREQ = 100.0  # Hz
POSITION_MARKERS = [0, 9, 19, 29, 39, 49, 59]

# Pulse widths in milliseconds
PULSE_WIDTH_ZERO = 200    # Binary 0
PULSE_WIDTH_ONE = 500     # Binary 1
PULSE_WIDTH_MARKER = 800  # Position marker

# Thresholds for pulse classification
THRESHOLD_ZERO_ONE = 350   # Below = 0, above = 1 or marker
THRESHOLD_ONE_MARKER = 650 # Below = 1, above = marker


@dataclass
class WWVBCDResult:
    """Result of WWV/WWVH BCD decoding for one minute"""
    detected: bool = False
    markers_found: int = 0
    markers_expected: int = 7  # Position markers at 0,9,19,29,39,49,59
    
    # Decoded time
    decoded_minute: Optional[int] = None
    decoded_hour: Optional[int] = None
    decoded_day: Optional[int] = None  # Day of year (1-366)
    decoded_year: Optional[int] = None  # 2-digit year
    
    # Auxiliary data
    dut1_tenths: Optional[int] = None  # UT1-UTC in 0.1s units
    dut1_negative: Optional[bool] = None
    leap_second_pending: Optional[bool] = None
    dst_status: Optional[int] = None
    
    # Quality metrics
    snr_db: Optional[float] = None
    decode_confidence: float = 0.0
    pulse_quality: float = 0.0  # How well pulses match expected widths
    
    # Timing offset: samples from file start to actual second boundary
    # This is the coarse_sync offset - critical for correcting system clock error
    coarse_sync_offset_samples: int = 0
    
    # Per-second pulse widths (for debugging)
    pulse_widths_ms: List[float] = field(default_factory=list)
    
    @property
    def dut1_seconds(self) -> Optional[float]:
        """Get DUT1 in seconds (signed)"""
        if self.dut1_tenths is None:
            return None
        sign = -1 if self.dut1_negative else 1
        return sign * self.dut1_tenths / 10.0
    
    def __str__(self):
        if not self.detected:
            return "BCD: Not detected"
        day = self.decoded_day if self.decoded_day is not None else 0
        hour = self.decoded_hour if self.decoded_hour is not None else 0
        minute = self.decoded_minute if self.decoded_minute is not None else 0
        year = self.decoded_year if self.decoded_year is not None else 0
        dut1 = self.dut1_seconds if self.dut1_seconds is not None else 0.0
        return (f"BCD: Day {day:03d} "
                f"{hour:02d}:{minute:02d} "
                f"(year {year:02d}), "
                f"DUT1={dut1:+.1f}s, "
                f"conf={self.decode_confidence:.2f}")


class WWVBCDDecoder:
    """
    Decode WWV/WWVH BCD time code for time verification.
    
    Usage:
        decoder = WWVBCDDecoder(sample_rate=24000)
        result = decoder.decode_minute(iq_samples)
        
        if result.detected:
            print(f"Decoded: Day {result.decoded_day} {result.decoded_hour}:{result.decoded_minute}")
    """
    
    def __init__(self, sample_rate: int = 24000, channel_name: str = "WWV"):
        self.sample_rate = sample_rate
        self.channel_name = channel_name
        self.samples_per_second = sample_rate
        
        # Design FIR bandpass filter for 100 Hz subcarrier
        # Use FIR instead of IIR to avoid numerical issues with narrow bandwidth
        from scipy.signal import firwin
        nyq = sample_rate / 2
        low = 80 / nyq
        high = 120 / nyq
        num_taps = 501  # Long filter for narrow bandwidth
        self._subcarrier_fir = firwin(num_taps, [low, high], pass_zero=False)
        
        logger.debug(f"WWV BCD Decoder initialized: {sample_rate} Hz")
    
    def _design_bandpass(self, center_freq: float, bandwidth: float) -> Tuple[np.ndarray, np.ndarray]:
        """Design a bandpass filter for subcarrier extraction"""
        nyq = self.sample_rate / 2
        low = (center_freq - bandwidth/2) / nyq
        high = (center_freq + bandwidth/2) / nyq
        
        # Ensure valid range
        low = max(0.001, min(low, 0.99))
        high = max(low + 0.001, min(high, 0.99))
        
        b, a = butter(4, [low, high], btype='band')
        return b, a
    
    def _extract_subcarrier(self, iq_samples: np.ndarray) -> np.ndarray:
        """
        Extract 100 Hz subcarrier power using short-time FFT.
        
        This is more robust than envelope detection for weak/noisy signals.
        Returns power values in 20ms windows.
        """
        # Use real part of IQ (the 100 Hz subcarrier is in baseband)
        audio = np.real(iq_samples).astype(np.float64)
        
        # Compute 100 Hz power in 20ms windows
        window_samples = int(0.02 * self.sample_rate)  # 20ms
        powers = []
        
        for i in range(0, len(audio) - window_samples, window_samples):
            window = audio[i:i + window_samples]
            fft_data = np.abs(np.fft.fft(window))
            freqs = np.fft.fftfreq(len(fft_data), 1/self.sample_rate)
            
            # Get power at 100 Hz (±10 Hz)
            mask = (freqs > 90) & (freqs < 110)
            power = fft_data[mask].max() if mask.any() else 0
            powers.append(power)
        
        # Expand to sample-level resolution for compatibility
        # Each power value represents 20ms = 480 samples at 24kHz
        envelope = np.repeat(powers, window_samples)
        
        # Pad to original length if needed
        if len(envelope) < len(audio):
            envelope = np.concatenate([envelope, np.zeros(len(audio) - len(envelope))])
        
        return envelope[:len(audio)]
    
    def _measure_pulse_widths(self, envelope: np.ndarray) -> List[float]:
        """
        Measure pulse width for each second using integration method.
        
        Returns list of 60 pulse widths in milliseconds.
        
        WWV BCD pulses: 100 Hz subcarrier is ON for 200/500/800ms at start of second.
        
        ROBUSTNESS: Uses total energy integration (not contiguous counting) to
        survive HF fading (QSB). A 500ms pulse that fades briefly will still
        measure ~500ms because we count ALL samples above threshold.
        
        Note: The 100 Hz subcarrier begins 30ms after the second boundary.
        """
        pulse_widths = []
        
        # Adaptive threshold: Use 40% of the 95th percentile
        # This ignores static crashes (spikes) while being robust to QSB
        global_max = np.percentile(envelope, 95)
        threshold = global_max * 0.4
        
        for second in range(60):
            start_idx = second * self.samples_per_second
            # Window: WWV pulses are max 800ms. Look at first 900ms.
            # Account for 30ms offset: subcarrier starts at 30ms, ends by 830ms
            window_samples = int(0.9 * self.samples_per_second)
            end_idx = min(start_idx + window_samples, len(envelope))
            
            if start_idx >= len(envelope):
                pulse_widths.append(0)
                continue
            
            second_data = envelope[start_idx:end_idx]
            
            # ROBUSTNESS: Count TOTAL samples above threshold, not contiguous
            # This survives signal dropouts (fading)
            samples_above = np.sum(second_data > threshold)
            
            # Convert to milliseconds
            pulse_ms = samples_above * 1000 / self.sample_rate
            pulse_widths.append(pulse_ms)
        
        return pulse_widths
    
    def coarse_sync(self, envelope: np.ndarray) -> int:
        """
        Find the sample index where second boundaries most likely align.
        
        Uses "folded epoch" averaging: fold the minute into a 1-second profile
        by superposing all 60 seconds. This averages out noise and reveals
        the persistent pulse structure.
        
        The 100 Hz subcarrier rises ~30ms after the second boundary.
        
        Returns: offset in samples to align to second boundaries
        """
        samples_per_sec = self.sample_rate
        num_seconds = len(envelope) // samples_per_sec
        
        if num_seconds < 10:
            return 0
        
        # Fold the minute into a 1-second average profile (Superposed Epoch)
        # This averages out noise and reveals the persistent pulse structure
        folded = np.zeros(samples_per_sec)
        count = 0
        
        for i in range(num_seconds):
            chunk = envelope[i * samples_per_sec : (i + 1) * samples_per_sec]
            if len(chunk) == samples_per_sec:
                folded += chunk
                count += 1
        
        if count > 0:
            folded /= count
        
        # The 100 Hz tone rises ~30ms after the second start.
        # Find the point where energy rises above 50% of the average peak
        threshold = np.max(folded) * 0.5
        
        # Find rising edges
        above_threshold = (folded > threshold).astype(int)
        rising_edges = np.where(np.diff(above_threshold) == 1)[0]
        
        if len(rising_edges) > 0:
            # We found the pulse start within a second. The second boundary is 30ms BEFORE this.
            pulse_start = rising_edges[0]
            sub_second_offset = pulse_start - int(0.030 * self.sample_rate)
            
            # Handle wrap-around if the pulse is right at the start
            if sub_second_offset < 0:
                sub_second_offset += samples_per_sec
            
            logger.debug(f"BCD coarse sync: sub-second offset={sub_second_offset} samples "
                        f"({sub_second_offset/self.sample_rate*1000:.0f}ms)")
            
            # Now find the multi-second offset to align markers to expected positions
            # Markers should be at seconds 0, 9, 19, 29, 39, 49, 59 (mod 10 = 0 or 9)
            # Search 0-9 second offsets to find best marker alignment
            best_multi_sec_offset = 0
            best_marker_score = 0
            
            for sec_offset in range(10):
                total_offset = sub_second_offset + sec_offset * samples_per_sec
                if total_offset >= len(envelope):
                    continue
                
                # Count markers at expected positions with this offset
                marker_score = 0
                for marker_sec in [0, 9, 19, 29, 39, 49]:
                    sec_start = total_offset + marker_sec * samples_per_sec
                    sec_end = sec_start + samples_per_sec
                    
                    if sec_end > len(envelope):
                        continue
                    
                    sec_env = envelope[sec_start:sec_end]
                    
                    # Check if this looks like a marker (high energy in first 800ms)
                    first_800ms = np.mean(sec_env[:int(0.8 * samples_per_sec)])
                    last_200ms = np.mean(sec_env[int(0.8 * samples_per_sec):])
                    
                    if first_800ms > last_200ms * 1.3:
                        marker_score += 1
                
                if marker_score > best_marker_score:
                    best_marker_score = marker_score
                    best_multi_sec_offset = sec_offset
            
            final_offset = sub_second_offset + best_multi_sec_offset * samples_per_sec
            
            logger.debug(f"BCD coarse sync: multi-second offset={best_multi_sec_offset}s, "
                        f"markers={best_marker_score}/6, final={final_offset} samples "
                        f"({final_offset/self.sample_rate*1000:.0f}ms)")
            
            return final_offset
        
        # Fallback: search for best marker alignment
        logger.debug("BCD coarse sync: folded epoch failed, using marker search")
        return self._coarse_sync_marker_search(envelope)
    
    def _coarse_sync_marker_search(self, envelope: np.ndarray) -> int:
        """
        Fallback coarse sync using marker position search.
        
        Searches for the offset that maximizes marker detection (800ms pulses
        at positions 0, 9, 19, 29, 39, 49).
        """
        samples_per_sec = self.sample_rate
        num_seconds = len(envelope) // samples_per_sec
        
        if num_seconds < 10:
            return 0
        
        best_markers = 0
        best_offset = 0
        
        # Search across 10 seconds with 100ms resolution
        search_range_sec = min(10, num_seconds - 50)  # Need 50 seconds for markers
        step_samples = int(0.1 * self.sample_rate)  # 100ms steps
        
        for offset_samples in range(0, search_range_sec * samples_per_sec, step_samples):
            markers_found = 0
            for marker_sec in [0, 9, 19, 29, 39, 49]:
                sec_start = offset_samples + marker_sec * samples_per_sec
                sec_end = sec_start + samples_per_sec
                
                if sec_end > len(envelope):
                    continue
                
                sec_env = envelope[sec_start:sec_end]
                
                # For marker (800ms), first 80% should have higher power than last 20%
                first_800ms = np.mean(sec_env[:int(0.8 * samples_per_sec)])
                last_200ms = np.mean(sec_env[int(0.8 * samples_per_sec):])
                
                if first_800ms > last_200ms * 1.5:
                    markers_found += 1
            
            if markers_found > best_markers:
                best_markers = markers_found
                best_offset = offset_samples
        
        if best_markers >= 3:
            logger.debug(f"BCD coarse sync (marker search): offset={best_offset} samples "
                        f"({best_offset/self.sample_rate*1000:.0f}ms), markers={best_markers}/6")
        
        return best_offset
    
    def _classify_pulse(self, width_ms: float) -> str:
        """Classify pulse as '0', '1', 'P' (marker), or '?' (unknown)"""
        if width_ms < 100:
            return '?'  # Too short
        elif width_ms < THRESHOLD_ZERO_ONE:
            return '0'
        elif width_ms < THRESHOLD_ONE_MARKER:
            return '1'
        elif width_ms <= 900:
            return 'P'
        else:
            return '?'  # Too long
    
    def _decode_bcd_digit(self, bits: List[str], num_bits: int = 4) -> Optional[int]:
        """
        Decode BCD digit from pulse classifications.
        Little-endian: LSB first.
        """
        if len(bits) < num_bits:
            return None
        
        value = 0
        for i in range(num_bits):
            if bits[i] == '1':
                value |= (1 << i)
            elif bits[i] == 'P':
                # Position marker in data field - invalid
                return None
            elif bits[i] == '?':
                # Unknown pulse - can't decode
                return None
            # '0' contributes nothing
        
        return value
    
    def decode_minute(
        self, 
        iq_samples: np.ndarray,
        second_offset_samples: Optional[int] = None
    ) -> WWVBCDResult:
        """
        Decode BCD time code from one minute of IQ samples.
        
        Args:
            iq_samples: Complex IQ samples for one minute (60 seconds)
            second_offset_samples: Optional offset to second boundary (from bootstrap).
                If provided, skips coarse_sync and uses this offset directly.
                This should be the number of samples from file start to the first
                complete second boundary.
            
        Returns:
            WWVBCDResult with decoded time and quality metrics
        """
        result = WWVBCDResult()
        
        # Check we have enough samples
        expected_samples = 60 * self.sample_rate
        if len(iq_samples) < expected_samples * 0.9:
            logger.debug(f"BCD decode: insufficient samples ({len(iq_samples)} < {expected_samples})")
            return result
        
        # Extract 100 Hz subcarrier envelope
        envelope = self._extract_subcarrier(iq_samples)
        
        # Align to second boundaries
        if second_offset_samples is not None:
            # Use bootstrap-provided offset (more reliable than per-file coarse_sync)
            offset = second_offset_samples
            logger.debug(f"BCD using bootstrap offset: {offset} samples ({offset/self.sample_rate*1000:.1f}ms)")
        else:
            # Fall back to coarse_sync (less reliable due to ~30ms variation)
            offset = self.coarse_sync(envelope)
            logger.debug(f"BCD coarse sync: offset={offset} samples ({offset/self.sample_rate*1000:.1f}ms)")
        
        result.coarse_sync_offset_samples = offset
        if offset > 0 and offset < len(envelope):
            envelope = envelope[offset:]
        
        # Measure pulse widths (using integration method for HF robustness)
        pulse_widths = self._measure_pulse_widths(envelope)
        result.pulse_widths_ms = pulse_widths
        
        # Classify each pulse
        classifications = [self._classify_pulse(w) for w in pulse_widths]
        
        # Check position markers
        markers_found = 0
        for marker_sec in POSITION_MARKERS:
            if marker_sec < len(classifications) and classifications[marker_sec] == 'P':
                markers_found += 1
        result.markers_found = markers_found
        
        # Need at least 4 of 7 markers to proceed
        if markers_found < 4:
            logger.debug(f"BCD decode: insufficient markers ({markers_found}/7)")
            return result
        
        # Decode minute (seconds 10-13 ones, 15-17 tens)
        minute_ones = self._decode_bcd_digit(classifications[10:14], 4)
        minute_tens = self._decode_bcd_digit(classifications[15:18], 3)
        if minute_ones is not None and minute_tens is not None:
            result.decoded_minute = minute_tens * 10 + minute_ones
            if result.decoded_minute > 59:
                result.decoded_minute = None
        
        # Decode hour (seconds 20-23 ones, 25-26 tens)
        hour_ones = self._decode_bcd_digit(classifications[20:24], 4)
        hour_tens = self._decode_bcd_digit(classifications[25:27], 2)
        if hour_ones is not None and hour_tens is not None:
            result.decoded_hour = hour_tens * 10 + hour_ones
            if result.decoded_hour > 23:
                result.decoded_hour = None
        
        # Decode day of year (seconds 30-33 ones, 35-38 tens, 40-42 hundreds)
        day_ones = self._decode_bcd_digit(classifications[30:34], 4)
        day_tens = self._decode_bcd_digit(classifications[35:39], 4)
        day_hundreds = self._decode_bcd_digit(classifications[40:43], 3)
        if day_ones is not None and day_tens is not None and day_hundreds is not None:
            result.decoded_day = day_hundreds * 100 + day_tens * 10 + day_ones
            if result.decoded_day < 1 or result.decoded_day > 366:
                result.decoded_day = None
        
        # Decode year (seconds 4-7 ones, 51-54 tens)
        year_ones = self._decode_bcd_digit(classifications[4:8], 4)
        year_tens = self._decode_bcd_digit(classifications[51:55], 4)
        if year_ones is not None and year_tens is not None:
            result.decoded_year = year_tens * 10 + year_ones
        
        # Decode DUT1 (second 50 sign, seconds 56-58 magnitude)
        if classifications[50] in ['0', '1']:
            result.dut1_negative = (classifications[50] == '1')
            dut1_mag = self._decode_bcd_digit(classifications[56:59], 3)
            if dut1_mag is not None:
                result.dut1_tenths = dut1_mag
        
        # Decode leap second pending (second 3)
        if classifications[3] in ['0', '1']:
            result.leap_second_pending = (classifications[3] == '1')
        
        # Calculate confidence
        decoded_fields = sum([
            result.decoded_minute is not None,
            result.decoded_hour is not None,
            result.decoded_day is not None,
            result.decoded_year is not None,
        ])
        
        # Pulse quality: how many pulses are clearly classified
        clear_pulses = sum(1 for c in classifications if c != '?')
        result.pulse_quality = clear_pulses / 60.0
        
        # Overall confidence
        result.decode_confidence = (
            (markers_found / 7.0) * 0.3 +
            (decoded_fields / 4.0) * 0.5 +
            result.pulse_quality * 0.2
        )
        
        # Mark as detected if we have minimum viable decode
        if (result.decoded_minute is not None and 
            result.decoded_hour is not None and
            markers_found >= 4):
            result.detected = True
        
        if result.detected:
            logger.info(f"[BCD] Decoded: {result}")
        else:
            logger.debug(f"[BCD] Partial decode: markers={markers_found}, "
                        f"min={result.decoded_minute}, hr={result.decoded_hour}, "
                        f"day={result.decoded_day}")
        
        return result
    
    def decode_partial(
        self, 
        iq_samples: np.ndarray, 
        start_second: int = 0
    ) -> WWVBCDResult:
        """
        Decode BCD from partial minute (e.g., seconds 10-40 for time fields).
        
        Useful during bootstrap when we don't have a full minute aligned.
        
        Args:
            iq_samples: IQ samples (may be less than 60 seconds)
            start_second: Which second the samples start at
            
        Returns:
            WWVBCDResult with whatever could be decoded
        """
        # For now, just use full decode on whatever we have
        # Could be optimized to focus on specific fields
        return self.decode_minute(iq_samples)


def test_decoder():
    """Test BCD decoder with synthetic signal"""
    from .wwv_bcd_encoder import WWVBCDEncoder
    from datetime import datetime
    
    sample_rate = 24000
    encoder = WWVBCDEncoder(sample_rate=sample_rate)
    decoder = WWVBCDDecoder(sample_rate=sample_rate)
    
    # Generate test signal for known time
    test_time = datetime(2026, 1, 26, 16, 30, 0)
    timestamp = test_time.timestamp()
    
    # Generate BCD envelope (not modulated for this test)
    envelope = encoder.encode_minute(timestamp, envelope_only=True)
    
    # Convert to "IQ" by making it complex (real signal)
    iq_samples = envelope.astype(np.complex64)
    
    # Decode
    result = decoder.decode_minute(iq_samples)
    
    print(f"Test time: {test_time.isoformat()} UTC")
    print(f"Decoded: {result}")
    print(f"Expected: minute={test_time.minute}, hour={test_time.hour}, "
          f"day={test_time.timetuple().tm_yday}")
    
    # Verify
    assert result.decoded_minute == test_time.minute, f"Minute mismatch"
    assert result.decoded_hour == test_time.hour, f"Hour mismatch"
    assert result.decoded_day == test_time.timetuple().tm_yday, f"Day mismatch"
    print("PASS: All fields decoded correctly")


if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG)
    test_decoder()
