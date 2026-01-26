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
        return (f"BCD: Day {self.decoded_day:03d} "
                f"{self.decoded_hour:02d}:{self.decoded_minute:02d} "
                f"(year {self.decoded_year:02d}), "
                f"DUT1={self.dut1_seconds:+.1f}s, "
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
        
        # Design bandpass filter for 100 Hz subcarrier
        # Narrow bandwidth since it's a pure tone
        self.subcarrier_filter = self._design_bandpass(SUBCARRIER_FREQ, bandwidth=20)
        
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
        Extract 100 Hz subcarrier envelope from IQ samples.
        
        The 100 Hz subcarrier appears as AM sidebands at carrier ± 100 Hz.
        We AM demodulate first, then extract the 100 Hz component.
        """
        # AM demodulate (magnitude of IQ)
        audio = np.abs(iq_samples)
        audio = audio - np.mean(audio)  # Remove DC
        
        # Bandpass filter around 100 Hz
        b, a = self.subcarrier_filter
        try:
            filtered = filtfilt(b, a, audio)
        except ValueError:
            # Signal too short for filter
            return np.zeros(len(audio))
        
        # Get envelope of 100 Hz signal
        analytic = hilbert(filtered)
        envelope = np.abs(analytic)
        
        return envelope
    
    def _measure_pulse_widths(self, envelope: np.ndarray) -> List[float]:
        """
        Measure pulse width for each second.
        
        Returns list of 60 pulse widths in milliseconds.
        """
        pulse_widths = []
        
        # Normalize envelope
        if envelope.max() > 0:
            envelope = envelope / envelope.max()
        
        # Threshold for pulse detection (adaptive)
        threshold = 0.3
        
        for second in range(60):
            start_idx = second * self.samples_per_second
            end_idx = (second + 1) * self.samples_per_second
            
            if end_idx > len(envelope):
                pulse_widths.append(0)
                continue
            
            second_data = envelope[start_idx:end_idx]
            
            # Find pulse width by counting samples above threshold
            above_threshold = second_data > threshold
            
            # Find first and last sample above threshold
            indices = np.where(above_threshold)[0]
            if len(indices) > 0:
                # Use the contiguous region from the start
                # (pulse should start at beginning of second)
                pulse_samples = 0
                for i, idx in enumerate(indices):
                    if i == 0 or indices[i] == indices[i-1] + 1:
                        pulse_samples = idx + 1
                    else:
                        break
                
                pulse_ms = pulse_samples * 1000 / self.sample_rate
            else:
                pulse_ms = 0
            
            pulse_widths.append(pulse_ms)
        
        return pulse_widths
    
    def _classify_pulse(self, width_ms: float) -> str:
        """Classify pulse as '0', '1', 'P' (marker), or '?' (unknown)"""
        if width_ms < 100:
            return '?'  # Too short
        elif width_ms < THRESHOLD_ZERO_ONE:
            return '0'
        elif width_ms < THRESHOLD_ONE_MARKER:
            return '1'
        elif width_ms < 900:
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
    
    def decode_minute(self, iq_samples: np.ndarray) -> WWVBCDResult:
        """
        Decode BCD time code from one minute of IQ samples.
        
        Args:
            iq_samples: Complex IQ samples for one minute (60 seconds)
            
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
        
        # Measure pulse widths
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
