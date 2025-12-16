#!/usr/bin/env python3
"""
Signal Templates - Matched Filter Templates for Time Signal Detection

================================================================================
PURPOSE
================================================================================
Provide matched filter templates for correlation-based detection of time signal
modulation patterns beyond simple timing ticks:

1. BCD 100 Hz Modulation (WWV/WWVH)
   - IRIG-H format pulse-width encoded time code
   - 200ms (0), 500ms (1), 800ms (marker) pulses on 100 Hz subcarrier
   - Identical on both stations - used for dual-peak delay measurement

2. CHU AFSK (Bell 103)
   - 2225 Hz mark, 2025 Hz space at 300 baud
   - Transmitted seconds 31-39 of each minute
   - Provides precise 500ms timing boundary

3. BPM Patterns (China)
   - 1000 Hz ticks with minute-dependent duration
   - 10ms (UTC minutes) vs 100ms (UT1 minutes)
   - 300ms minute marker
   - BCD time code on 100 Hz subcarrier (similar to WWV)

================================================================================
THEORY: MATCHED FILTERING FOR MODULATION PATTERNS
================================================================================
For complex modulation patterns (BCD, FSK), matched filtering provides:

1. OPTIMAL DETECTION: Maximum SNR for known signal in AWGN
   SNR_out = 2E/N₀ where E = signal energy

2. TIMING PRECISION: Correlation peak locates pattern onset
   Sub-sample interpolation achieves ~5μs precision at 20 kHz

3. PATTERN VERIFICATION: High correlation confirms expected pattern
   Low correlation indicates interference or wrong station

================================================================================
OVERLAPPING WINDOW STRATEGY
================================================================================
For patterns spanning multiple seconds (BCD = 60s, FSK = 9s), use overlapping
windows to track timing drift and handle Doppler decorrelation:

- BCD: 10-second windows with 1-second overlap (within Tc ~10-20s)
- FSK: 1-second windows centered on each FSK second (31-39)
- BPM: 5-second windows with 1-second overlap

================================================================================
Author: HF Time Standard Team
================================================================================
"""

import logging
import numpy as np
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Tuple, Set
from enum import Enum
from datetime import datetime
from scipy import signal as scipy_signal
from scipy.signal import correlate
from scipy.signal.windows import tukey, hann

logger = logging.getLogger(__name__)


# =============================================================================
# BCD 100 Hz TEMPLATE (WWV/WWVH)
# =============================================================================

@dataclass
class BCDCorrelationResult:
    """Result from BCD template correlation"""
    window_start_sec: float
    wwv_delay_ms: float              # WWV peak delay from expected
    wwvh_delay_ms: float             # WWVH peak delay from expected
    differential_delay_ms: float     # WWVH - WWV delay
    wwv_amplitude: float             # WWV correlation peak amplitude
    wwvh_amplitude: float            # WWVH correlation peak amplitude
    amplitude_ratio_db: float        # 20*log10(WWV/WWVH)
    correlation_quality: float       # Peak-to-noise ratio
    detection_type: str              # 'dual_peak', 'single_wwv', 'single_wwvh'


class BCDTemplateGenerator:
    """
    Generate BCD 100 Hz modulation templates for matched filtering.
    
    The BCD (Binary Coded Decimal) time code is transmitted on a 100 Hz
    subcarrier with pulse-width modulation:
    
    - Binary 0: 200ms HIGH, 800ms LOW
    - Binary 1: 500ms HIGH, 500ms LOW  
    - Marker:   800ms HIGH, 200ms LOW (seconds 0,9,19,29,39,49,59)
    
    Both WWV and WWVH transmit IDENTICAL BCD patterns, making this ideal
    for dual-peak delay measurement.
    """
    
    POSITION_MARKERS = {0, 9, 19, 29, 39, 49, 59}
    
    def __init__(self, sample_rate: int = 20000):
        self.sample_rate = sample_rate
        
    def generate_second_template(
        self,
        bit_value: int,
        is_marker: bool = False,
        with_carrier: bool = True
    ) -> np.ndarray:
        """
        Generate 1-second BCD template for a single bit.
        
        Args:
            bit_value: 0 or 1
            is_marker: True for position marker seconds
            with_carrier: Include 100 Hz carrier modulation
            
        Returns:
            1-second template array
        """
        samples_per_sec = self.sample_rate
        template = np.zeros(samples_per_sec, dtype=np.float32)
        
        # Amplitude levels (from NIST SP 250-67)
        high_amp = 10.0 ** (-6.0 / 20.0)   # -6 dB
        low_amp = high_amp / 10.0           # -20 dB
        
        # Determine pulse width
        if is_marker:
            high_samples = int(0.8 * samples_per_sec)  # 800ms
        elif bit_value == 1:
            high_samples = int(0.5 * samples_per_sec)  # 500ms
        else:
            high_samples = int(0.2 * samples_per_sec)  # 200ms
        
        # Create envelope
        template[:high_samples] = high_amp
        template[high_samples:] = low_amp
        
        if with_carrier:
            # Modulate onto 100 Hz carrier
            t = np.arange(samples_per_sec) / self.sample_rate
            carrier = np.sin(2 * np.pi * 100 * t)
            template = template * carrier
        
        return template
    
    def generate_minute_template(
        self,
        timestamp: float,
        with_carrier: bool = True
    ) -> np.ndarray:
        """
        Generate full 60-second BCD template for a specific UTC minute.
        
        Args:
            timestamp: UTC timestamp at minute boundary
            with_carrier: Include 100 Hz carrier modulation
            
        Returns:
            60-second template array
        """
        dt = datetime.utcfromtimestamp(timestamp)
        
        # Generate BCD bit pattern
        bit_pattern = self._generate_bit_pattern(
            minute=dt.minute,
            hour=dt.hour,
            day_of_year=dt.timetuple().tm_yday,
            year=dt.year % 100
        )
        
        # Build full template
        template = np.zeros(60 * self.sample_rate, dtype=np.float32)
        
        for sec in range(60):
            is_marker = sec in self.POSITION_MARKERS
            bit = bit_pattern[sec]
            
            sec_template = self.generate_second_template(
                bit_value=bit,
                is_marker=is_marker,
                with_carrier=with_carrier
            )
            
            start = sec * self.sample_rate
            end = start + self.sample_rate
            template[start:end] = sec_template
        
        # Normalize to unit energy
        energy = np.sqrt(np.sum(template**2))
        if energy > 0:
            template /= energy
        
        return template
    
    def generate_window_template(
        self,
        timestamp: float,
        start_second: int,
        duration_seconds: int,
        with_carrier: bool = True
    ) -> np.ndarray:
        """
        Generate BCD template for a specific window within the minute.
        
        Args:
            timestamp: UTC timestamp at minute boundary
            start_second: Starting second (0-59)
            duration_seconds: Window duration
            with_carrier: Include 100 Hz carrier
            
        Returns:
            Window template array
        """
        full_template = self.generate_minute_template(timestamp, with_carrier)
        
        start_sample = start_second * self.sample_rate
        end_sample = (start_second + duration_seconds) * self.sample_rate
        
        window_template = full_template[start_sample:end_sample]
        
        # Re-normalize window
        energy = np.sqrt(np.sum(window_template**2))
        if energy > 0:
            window_template /= energy
        
        return window_template
    
    def _generate_bit_pattern(
        self,
        minute: int,
        hour: int,
        day_of_year: int,
        year: int
    ) -> List[int]:
        """Generate 60-element BCD bit pattern (little-endian)."""
        pattern = [0] * 60
        
        def encode_bcd(value: int, start_idx: int, num_bits: int = 4):
            for i in range(num_bits):
                if start_idx + i < 60:
                    pattern[start_idx + i] = (value >> i) & 1
        
        # Year ones (seconds 4-7)
        encode_bcd(year % 10, 4)
        # Year tens (seconds 51-54)
        encode_bcd(year // 10, 51)
        
        # Minute ones (seconds 10-13)
        encode_bcd(minute % 10, 10)
        # Minute tens (seconds 15-17, 3 bits)
        encode_bcd(minute // 10, 15, 3)
        
        # Hour ones (seconds 20-23)
        encode_bcd(hour % 10, 20)
        # Hour tens (seconds 25-26, 2 bits)
        encode_bcd(hour // 10, 25, 2)
        
        # Day ones (seconds 30-33)
        encode_bcd(day_of_year % 10, 30)
        # Day tens (seconds 35-38)
        encode_bcd((day_of_year // 10) % 10, 35)
        # Day hundreds (seconds 40-41, 2 bits)
        encode_bcd(day_of_year // 100, 40, 2)
        
        return pattern


# =============================================================================
# CHU AFSK TEMPLATE (Bell 103)
# =============================================================================

@dataclass
class AFSKCorrelationResult:
    """Result from CHU AFSK template correlation"""
    second: int                      # FSK second (31-39)
    timing_offset_ms: float          # Offset from expected 500ms boundary
    correlation_peak: float          # Normalized correlation peak
    snr_db: float                    # Signal-to-noise ratio
    mark_power_db: float             # 2225 Hz power
    space_power_db: float            # 2025 Hz power
    fsk_quality: float               # FSK signal quality (0-1)


class CHUAFSKTemplateGenerator:
    """
    Generate CHU AFSK (Bell 103) templates for matched filtering.
    
    CHU transmits FSK time code during seconds 31-39:
    - Mark frequency: 2225 Hz (logic 1)
    - Space frequency: 2025 Hz (logic 0)
    - Baud rate: 300 bps
    - Frame: 1 start + 8 data + 2 stop = 11 bits/byte
    
    Timing structure per FSK second:
    - 0-10ms: 1000 Hz tick
    - 10-133ms: Mark tone (sync)
    - 133-500ms: Data stream
    - 500ms: Precise timing boundary
    """
    
    MARK_FREQ = 2225.0   # Hz
    SPACE_FREQ = 2025.0  # Hz
    BAUD_RATE = 300      # bps
    
    FSK_SECONDS = [31, 32, 33, 34, 35, 36, 37, 38, 39]
    
    # Timing within each FSK second (ms)
    TICK_END_MS = 10.0
    MARK_START_MS = 10.0
    DATA_START_MS = 133.33
    DATA_END_MS = 500.0
    
    def __init__(self, sample_rate: int = 20000):
        self.sample_rate = sample_rate
        self.samples_per_bit = int(sample_rate / self.BAUD_RATE)
        
    def generate_mark_template(self, duration_ms: float) -> np.ndarray:
        """Generate mark tone (2225 Hz) template."""
        n_samples = int(duration_ms * self.sample_rate / 1000)
        t = np.arange(n_samples) / self.sample_rate
        template = np.sin(2 * np.pi * self.MARK_FREQ * t)
        return template * tukey(n_samples, alpha=0.1)
    
    def generate_space_template(self, duration_ms: float) -> np.ndarray:
        """Generate space tone (2025 Hz) template."""
        n_samples = int(duration_ms * self.sample_rate / 1000)
        t = np.arange(n_samples) / self.sample_rate
        template = np.sin(2 * np.pi * self.SPACE_FREQ * t)
        return template * tukey(n_samples, alpha=0.1)
    
    def generate_fsk_second_template(
        self,
        second: int,
        include_tick: bool = False
    ) -> np.ndarray:
        """
        Generate template for one FSK second (31-39).
        
        Args:
            second: Second number (31-39)
            include_tick: Include 1000 Hz tick at start
            
        Returns:
            500ms template (up to data end boundary)
        """
        # Template covers 0-500ms (the precise timing boundary)
        duration_ms = self.DATA_END_MS
        n_samples = int(duration_ms * self.sample_rate / 1000)
        template = np.zeros(n_samples, dtype=np.float32)
        
        # 1000 Hz tick (0-10ms) - optional
        if include_tick:
            tick_end = int(self.TICK_END_MS * self.sample_rate / 1000)
            t_tick = np.arange(tick_end) / self.sample_rate
            template[:tick_end] = np.sin(2 * np.pi * 1000 * t_tick)
        
        # Mark sync tone (10-133ms)
        mark_start = int(self.MARK_START_MS * self.sample_rate / 1000)
        data_start = int(self.DATA_START_MS * self.sample_rate / 1000)
        t_mark = np.arange(data_start - mark_start) / self.sample_rate
        template[mark_start:data_start] = np.sin(2 * np.pi * self.MARK_FREQ * t_mark)
        
        # Data stream (133-500ms) - alternating mark/space pattern
        # Use a generic pattern since actual data varies
        data_samples = n_samples - data_start
        t_data = np.arange(data_samples) / self.sample_rate
        
        # Generate alternating FSK pattern (approximation)
        # Real data would need actual frame content
        bit_duration = 1.0 / self.BAUD_RATE
        for i, t in enumerate(t_data):
            bit_idx = int(t / bit_duration)
            if bit_idx % 2 == 0:
                template[data_start + i] = np.sin(2 * np.pi * self.MARK_FREQ * t)
            else:
                template[data_start + i] = np.sin(2 * np.pi * self.SPACE_FREQ * t)
        
        # Normalize
        energy = np.sqrt(np.sum(template**2))
        if energy > 0:
            template /= energy
        
        return template
    
    def generate_fsk_window_template(
        self,
        start_second: int = 31,
        end_second: int = 40
    ) -> np.ndarray:
        """
        Generate template spanning multiple FSK seconds.
        
        Args:
            start_second: First FSK second (31-39)
            end_second: Last FSK second + 1
            
        Returns:
            Multi-second FSK template
        """
        templates = []
        for sec in range(start_second, min(end_second, 40)):
            if sec in self.FSK_SECONDS:
                templates.append(self.generate_fsk_second_template(sec))
        
        if not templates:
            return np.array([])
        
        # Concatenate with 500ms gaps (remaining half of each second)
        gap_samples = int(500 * self.sample_rate / 1000)
        full_template = []
        
        for t in templates:
            full_template.append(t)
            full_template.append(np.zeros(gap_samples))
        
        result = np.concatenate(full_template[:-1])  # Remove last gap
        
        # Normalize
        energy = np.sqrt(np.sum(result**2))
        if energy > 0:
            result /= energy
        
        return result
    
    def generate_quadrature_templates(
        self,
        duration_ms: float,
        frequency: float
    ) -> Tuple[np.ndarray, np.ndarray]:
        """
        Generate quadrature (I/Q) templates for phase-invariant detection.
        
        Args:
            duration_ms: Template duration
            frequency: Tone frequency (MARK_FREQ or SPACE_FREQ)
            
        Returns:
            Tuple of (sin_template, cos_template)
        """
        n_samples = int(duration_ms * self.sample_rate / 1000)
        t = np.arange(n_samples) / self.sample_rate
        window = tukey(n_samples, alpha=0.1)
        
        sin_template = np.sin(2 * np.pi * frequency * t) * window
        cos_template = np.cos(2 * np.pi * frequency * t) * window
        
        # Normalize to unit energy
        energy = np.sqrt(np.sum(sin_template**2))
        if energy > 0:
            sin_template /= energy
            cos_template /= energy
        
        return sin_template, cos_template


# =============================================================================
# BPM PATTERN TEMPLATES (China)
# =============================================================================

@dataclass
class BPMCorrelationResult:
    """Result from BPM pattern correlation"""
    window_start_sec: float
    timing_offset_ms: float          # Offset from expected position
    tick_duration_ms: float          # Measured tick duration
    expected_duration_ms: float      # Expected based on minute
    duration_match: bool             # Does measured match expected?
    correlation_peak: float          # Normalized correlation peak
    snr_db: float
    timing_mode: str                 # 'UTC' or 'UT1'
    is_usable: bool                  # True if UTC minute


class BPMTemplateGenerator:
    """
    Generate BPM (China) time signal templates for matched filtering.
    
    BPM Signal Structure:
    - Frequency: 1000 Hz (same as WWV)
    - UTC minutes (0-24, 30-54): 10ms ticks
    - UT1 minutes (25-29, 55-59): 100ms ticks
    - Minute marker: 300ms
    - BCD time code on 100 Hz subcarrier
    
    Key differences from WWV:
    - Longer tick duration (10ms vs 5ms)
    - UT1/UTC alternation
    - Much longer propagation path (~10,000 km from China)
    """
    
    TICK_FREQ = 1000.0  # Hz
    
    # Tick durations
    UTC_TICK_MS = 10.0
    UT1_TICK_MS = 100.0
    MINUTE_MARKER_MS = 300.0
    
    # UT1 minutes (DO NOT use for UTC timing)
    UT1_MINUTES = {25, 26, 27, 28, 29, 55, 56, 57, 58, 59}
    
    def __init__(self, sample_rate: int = 20000):
        self.sample_rate = sample_rate
        self._build_templates()
    
    def _build_templates(self):
        """Pre-build tick templates for each duration."""
        self.templates = {}
        
        for duration_ms in [self.UTC_TICK_MS, self.UT1_TICK_MS, self.MINUTE_MARKER_MS]:
            n_samples = int(duration_ms * self.sample_rate / 1000)
            t = np.arange(n_samples) / self.sample_rate
            
            # Quadrature templates
            window = tukey(n_samples, alpha=0.1)
            sin_template = np.sin(2 * np.pi * self.TICK_FREQ * t) * window
            cos_template = np.cos(2 * np.pi * self.TICK_FREQ * t) * window
            
            # Normalize
            energy = np.sqrt(np.sum(sin_template**2))
            if energy > 0:
                sin_template /= energy
                cos_template /= energy
            
            self.templates[duration_ms] = (sin_template, cos_template)
    
    def get_tick_duration_ms(self, minute: int, second: int = 1) -> float:
        """
        Get expected tick duration for given minute/second.
        
        Args:
            minute: Minute of hour (0-59)
            second: Second of minute (0-59)
            
        Returns:
            Expected tick duration in ms
        """
        if second == 0:
            return self.MINUTE_MARKER_MS
        elif minute in self.UT1_MINUTES:
            return self.UT1_TICK_MS
        else:
            return self.UTC_TICK_MS
    
    def is_utc_minute(self, minute: int) -> bool:
        """Check if minute uses UTC timing (safe for time transfer)."""
        return minute not in self.UT1_MINUTES
    
    def generate_tick_template(
        self,
        minute: int,
        second: int = 1
    ) -> Tuple[np.ndarray, np.ndarray]:
        """
        Generate quadrature tick template for specific minute/second.
        
        Args:
            minute: Minute of hour
            second: Second of minute
            
        Returns:
            Tuple of (sin_template, cos_template)
        """
        duration_ms = self.get_tick_duration_ms(minute, second)
        return self.templates[duration_ms]
    
    def generate_composite_template(
        self,
        minute: int,
        start_second: int,
        num_seconds: int
    ) -> Tuple[np.ndarray, np.ndarray, List[int]]:
        """
        Generate composite template for multiple ticks.
        
        Args:
            minute: Minute of hour (determines UTC/UT1)
            start_second: First second in window
            num_seconds: Number of seconds to include
            
        Returns:
            Tuple of (sin_template, cos_template, valid_seconds)
        """
        window_samples = num_seconds * self.sample_rate
        template_sin = np.zeros(window_samples, dtype=np.float32)
        template_cos = np.zeros(window_samples, dtype=np.float32)
        valid_seconds = []
        
        for i in range(num_seconds):
            sec = start_second + i
            if sec >= 60:
                break
            if sec == 0:
                continue  # Skip minute marker for tick detection
            
            valid_seconds.append(sec)
            
            tick_sin, tick_cos = self.generate_tick_template(minute, sec)
            
            # Position at start of each second
            tick_start = i * self.sample_rate
            tick_end = tick_start + len(tick_sin)
            
            if tick_end <= window_samples:
                template_sin[tick_start:tick_end] += tick_sin
                template_cos[tick_start:tick_end] += tick_cos
        
        # Normalize
        energy = np.sqrt(np.sum(template_sin**2))
        if energy > 0:
            template_sin /= energy
            template_cos /= energy
        
        return template_sin, template_cos, valid_seconds
    
    def generate_bcd_template(
        self,
        timestamp: float,
        with_carrier: bool = True
    ) -> np.ndarray:
        """
        Generate BPM BCD time code template (100 Hz subcarrier).
        
        BPM uses similar BCD encoding to WWV but with different timing.
        
        Args:
            timestamp: UTC timestamp at minute boundary
            with_carrier: Include 100 Hz carrier
            
        Returns:
            60-second BCD template
        """
        # BPM BCD is similar to WWV - reuse BCDTemplateGenerator logic
        bcd_gen = BCDTemplateGenerator(self.sample_rate)
        return bcd_gen.generate_minute_template(timestamp, with_carrier)


# =============================================================================
# UNIFIED CORRELATION ENGINE
# =============================================================================

class SignalTemplateCorrelator:
    """
    Unified correlation engine for all signal templates.
    
    Provides consistent interface for correlating received signals
    against BCD, AFSK, and BPM templates with overlapping windows.
    """
    
    def __init__(self, sample_rate: int = 20000):
        self.sample_rate = sample_rate
        self.bcd_generator = BCDTemplateGenerator(sample_rate)
        self.afsk_generator = CHUAFSKTemplateGenerator(sample_rate)
        self.bpm_generator = BPMTemplateGenerator(sample_rate)
    
    def correlate_bcd(
        self,
        iq_samples: np.ndarray,
        timestamp: float,
        window_seconds: int = 10,
        overlap_seconds: int = 1
    ) -> List[BCDCorrelationResult]:
        """
        Correlate received signal with BCD template using overlapping windows.
        
        Args:
            iq_samples: Complex IQ samples (60 seconds)
            timestamp: UTC timestamp at minute boundary
            window_seconds: Window duration
            overlap_seconds: Overlap between windows
            
        Returns:
            List of BCDCorrelationResult for each window
        """
        # Extract 100 Hz band
        audio = self._extract_100hz_band(iq_samples)
        
        # Generate full template
        template = self.bcd_generator.generate_minute_template(timestamp, with_carrier=True)
        
        results = []
        step = overlap_seconds
        
        for start_sec in range(0, 60 - window_seconds + 1, step):
            start_sample = start_sec * self.sample_rate
            end_sample = (start_sec + window_seconds) * self.sample_rate
            
            if end_sample > len(audio) or end_sample > len(template):
                break
            
            window_audio = audio[start_sample:end_sample]
            window_template = template[start_sample:end_sample]
            
            # Cross-correlate
            corr = correlate(window_audio, window_template, mode='full')
            corr = np.abs(corr)
            
            # Find peaks (WWV and WWVH arrivals)
            result = self._analyze_bcd_correlation(
                corr, start_sec, window_seconds
            )
            if result:
                results.append(result)
        
        return results
    
    def correlate_afsk(
        self,
        iq_samples: np.ndarray,
        fsk_seconds: List[int] = None
    ) -> List[AFSKCorrelationResult]:
        """
        Correlate received signal with CHU AFSK templates.
        
        Args:
            iq_samples: Complex IQ samples (60 seconds)
            fsk_seconds: Which FSK seconds to analyze (default: 31-39)
            
        Returns:
            List of AFSKCorrelationResult for each FSK second
        """
        if fsk_seconds is None:
            fsk_seconds = self.afsk_generator.FSK_SECONDS
        
        # AM demodulate
        audio = np.abs(iq_samples)
        audio = audio - np.mean(audio)
        
        results = []
        
        for sec in fsk_seconds:
            # Extract second's audio
            start_sample = sec * self.sample_rate
            end_sample = start_sample + int(0.5 * self.sample_rate)  # 500ms
            
            if end_sample > len(audio):
                break
            
            sec_audio = audio[start_sample:end_sample]
            
            # Generate template for this second
            template = self.afsk_generator.generate_fsk_second_template(sec)
            
            if len(template) > len(sec_audio):
                template = template[:len(sec_audio)]
            
            # Correlate
            corr = correlate(sec_audio, template, mode='full')
            
            result = self._analyze_afsk_correlation(corr, sec)
            if result:
                results.append(result)
        
        return results
    
    def correlate_bpm(
        self,
        iq_samples: np.ndarray,
        minute: int,
        window_seconds: int = 5,
        overlap_seconds: int = 1
    ) -> List[BPMCorrelationResult]:
        """
        Correlate received signal with BPM templates.
        
        Args:
            iq_samples: Complex IQ samples (60 seconds)
            minute: Minute of hour (determines UTC/UT1)
            window_seconds: Window duration
            overlap_seconds: Overlap between windows
            
        Returns:
            List of BPMCorrelationResult for each window
        """
        # AM demodulate
        audio = np.abs(iq_samples)
        audio = audio - np.mean(audio)
        
        results = []
        step = overlap_seconds
        
        for start_sec in range(1, 60 - window_seconds + 1, step):
            # Generate composite template for this window
            template_sin, template_cos, valid_secs = self.bpm_generator.generate_composite_template(
                minute, start_sec, window_seconds
            )
            
            if not valid_secs:
                continue
            
            # Extract window
            start_sample = start_sec * self.sample_rate
            end_sample = (start_sec + window_seconds) * self.sample_rate
            
            if end_sample > len(audio):
                break
            
            window_audio = audio[start_sample:end_sample]
            
            # Quadrature correlation
            corr_sin = correlate(window_audio, template_sin, mode='same')
            corr_cos = correlate(window_audio, template_cos, mode='same')
            envelope = np.sqrt(corr_sin**2 + corr_cos**2)
            
            result = self._analyze_bpm_correlation(
                envelope, start_sec, window_seconds, minute
            )
            if result:
                results.append(result)
        
        return results
    
    def _extract_100hz_band(self, iq_samples: np.ndarray) -> np.ndarray:
        """Extract 100 Hz BCD band from IQ samples."""
        nyquist = self.sample_rate / 2
        low = 50 / nyquist
        high = 150 / nyquist
        
        sos = scipy_signal.butter(4, [low, high], 'bandpass', output='sos')
        filtered = scipy_signal.sosfilt(sos, iq_samples)
        
        if np.iscomplexobj(filtered):
            return np.real(filtered)
        return filtered
    
    def _analyze_bcd_correlation(
        self,
        correlation: np.ndarray,
        start_sec: float,
        window_seconds: int
    ) -> Optional[BCDCorrelationResult]:
        """Analyze BCD correlation to find WWV/WWVH peaks."""
        # Find two highest peaks (WWV and WWVH)
        from scipy.signal import find_peaks
        
        peaks, properties = find_peaks(
            correlation,
            height=np.mean(correlation) + np.std(correlation),
            distance=int(0.003 * self.sample_rate)  # 3ms minimum
        )
        
        if len(peaks) < 1:
            return None
        
        # Get top 2 peaks
        if 'peak_heights' in properties:
            heights = properties['peak_heights']
            sorted_idx = np.argsort(heights)[-2:]
            top_peaks = peaks[sorted_idx]
            top_heights = heights[sorted_idx]
        else:
            return None
        
        # Calculate delays and amplitudes
        center = len(correlation) // 2
        
        if len(top_peaks) >= 2:
            delay1_ms = (top_peaks[0] - center) / self.sample_rate * 1000
            delay2_ms = (top_peaks[1] - center) / self.sample_rate * 1000
            
            # Assume earlier arrival is WWV (heuristic for US receivers)
            if delay1_ms < delay2_ms:
                wwv_delay, wwvh_delay = delay1_ms, delay2_ms
                wwv_amp, wwvh_amp = top_heights[0], top_heights[1]
            else:
                wwv_delay, wwvh_delay = delay2_ms, delay1_ms
                wwv_amp, wwvh_amp = top_heights[1], top_heights[0]
            
            detection_type = 'dual_peak'
        else:
            wwv_delay = (top_peaks[0] - center) / self.sample_rate * 1000
            wwvh_delay = 0.0
            wwv_amp = top_heights[0]
            wwvh_amp = 0.0
            detection_type = 'single_wwv'
        
        # Quality metric
        noise_floor = np.median(correlation)
        quality = (wwv_amp + wwvh_amp) / (2 * noise_floor) if noise_floor > 0 else 0
        
        # Amplitude ratio
        if wwvh_amp > 0:
            ratio_db = 20 * np.log10(wwv_amp / wwvh_amp)
        else:
            ratio_db = 40.0  # Large positive = WWV dominant
        
        return BCDCorrelationResult(
            window_start_sec=start_sec,
            wwv_delay_ms=wwv_delay,
            wwvh_delay_ms=wwvh_delay,
            differential_delay_ms=wwvh_delay - wwv_delay,
            wwv_amplitude=float(wwv_amp),
            wwvh_amplitude=float(wwvh_amp),
            amplitude_ratio_db=float(ratio_db),
            correlation_quality=float(quality),
            detection_type=detection_type
        )
    
    def _analyze_afsk_correlation(
        self,
        correlation: np.ndarray,
        second: int
    ) -> Optional[AFSKCorrelationResult]:
        """Analyze AFSK correlation for timing and quality."""
        peak_idx = np.argmax(np.abs(correlation))
        peak_val = np.abs(correlation[peak_idx])
        
        # Expected peak at center
        center = len(correlation) // 2
        offset_samples = peak_idx - center
        offset_ms = offset_samples / self.sample_rate * 1000
        
        # SNR estimate
        noise_region = np.concatenate([
            correlation[:center-100],
            correlation[center+100:]
        ])
        noise_std = np.std(noise_region) if len(noise_region) > 0 else 1.0
        snr_db = 20 * np.log10(peak_val / noise_std) if noise_std > 0 else 0
        
        return AFSKCorrelationResult(
            second=second,
            timing_offset_ms=float(offset_ms),
            correlation_peak=float(peak_val / np.max(np.abs(correlation))),
            snr_db=float(snr_db),
            mark_power_db=0.0,  # Would need separate measurement
            space_power_db=0.0,
            fsk_quality=min(1.0, peak_val / (noise_std * 10)) if noise_std > 0 else 0
        )
    
    def _analyze_bpm_correlation(
        self,
        envelope: np.ndarray,
        start_sec: int,
        window_seconds: int,
        minute: int
    ) -> Optional[BPMCorrelationResult]:
        """Analyze BPM correlation for timing and mode."""
        peak_idx = np.argmax(envelope)
        peak_val = envelope[peak_idx]
        
        center = len(envelope) // 2
        offset_samples = peak_idx - center
        offset_ms = offset_samples / self.sample_rate * 1000
        
        # Expected tick duration
        expected_ms = self.bpm_generator.get_tick_duration_ms(minute, start_sec)
        
        # SNR estimate
        noise_floor = np.median(envelope)
        snr_db = 20 * np.log10(peak_val / noise_floor) if noise_floor > 0 else 0
        
        # Timing mode
        is_utc = self.bpm_generator.is_utc_minute(minute)
        timing_mode = 'UTC' if is_utc else 'UT1'
        
        return BPMCorrelationResult(
            window_start_sec=float(start_sec),
            timing_offset_ms=float(offset_ms),
            tick_duration_ms=expected_ms,  # Would need actual measurement
            expected_duration_ms=expected_ms,
            duration_match=True,  # Placeholder
            correlation_peak=float(peak_val / np.max(envelope)),
            snr_db=float(snr_db),
            timing_mode=timing_mode,
            is_usable=is_utc
        )


# =============================================================================
# FACTORY FUNCTIONS
# =============================================================================

def create_bcd_generator(sample_rate: int = 20000) -> BCDTemplateGenerator:
    """Create BCD template generator for WWV/WWVH."""
    return BCDTemplateGenerator(sample_rate)


def create_afsk_generator(sample_rate: int = 20000) -> CHUAFSKTemplateGenerator:
    """Create AFSK template generator for CHU."""
    return CHUAFSKTemplateGenerator(sample_rate)


def create_bpm_generator(sample_rate: int = 20000) -> BPMTemplateGenerator:
    """Create BPM template generator."""
    return BPMTemplateGenerator(sample_rate)


def create_correlator(sample_rate: int = 20000) -> SignalTemplateCorrelator:
    """Create unified signal template correlator."""
    return SignalTemplateCorrelator(sample_rate)
