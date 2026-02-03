#!/usr/bin/env python3
"""
Tick Matched Filter - Station-Specific Per-Second Tick Detection

================================================================================
PURPOSE
================================================================================
Detect per-second timing ticks from WWV/WWVH/CHU/BPM using matched filtering
with station-specific templates. Uses overlapping 5-second windows to balance
SNR improvement against ionospheric Doppler decorrelation.

This complements the minute marker detection (800ms tones) by providing:
- 55+ timing estimates per minute (vs 1 from minute marker)
- Tracking of timing drift within the minute
- Robustness to single-tick dropouts
- Detection of Doppler-induced phase drift

================================================================================
THEORY: MATCHED FILTERING FOR SHORT TICKS
================================================================================
The per-second ticks are much shorter than minute markers:
    WWV/WWVH: 5ms (100 samples at 20 kHz)
    CHU: 10ms (FSK periods) or 300ms (regular)
    BPM: 10ms (UTC) or 100ms (UT1)

Matched filtering remains optimal for detecting known signals in noise:
    SNR_out = 2E/N₀

For a 5ms tick at 20 kHz: E ∝ 100 samples
For an 800ms tone: E ∝ 16000 samples
Ratio: 160× less energy → 22 dB lower SNR per tick

SOLUTION: Integrate multiple ticks coherently
    5 ticks coherent: √5 × SNR improvement (+3.5 dB)
    5 ticks + matched filter: Recovers ~7 dB vs single FFT bin

================================================================================
OVERLAPPING WINDOW STRATEGY
================================================================================
Using 5-second windows with 1-second overlap:

    Window 0: seconds 1-5   (5 ticks)
    Window 1: seconds 2-6   (5 ticks, shares 4 with Window 0)
    Window 2: seconds 3-7   (5 ticks, shares 4 with Window 1)
    ...
    Window 54: seconds 55-59 (5 ticks)

Benefits:
- 55 independent timing estimates per minute
- Adjacent windows highly correlated → smooth tracking
- Robust to single-tick dropouts
- Can detect timing drift (Doppler) across minute

================================================================================
STATION-SPECIFIC PATTERNS
================================================================================
Each station has unique tick characteristics that affect template design:

WWV (Fort Collins, CO):
    - 1000 Hz, 5ms duration
    - Skip: second 0 (800ms marker), 29, 59 (silent)
    
WWVH (Kauai, HI):
    - 1200 Hz, 5ms duration  
    - Skip: second 0 (800ms marker), 29, 59 (silent)

CHU (Ottawa, Canada):
    - 1000 Hz, variable duration
    - Regular seconds: 300ms tones
    - FSK seconds (31-39): 10ms ticks
    - Voice seconds (50-59): 10ms ticks
    - Skip: second 0 (500ms marker), 29 (always silent)

BPM (China):
    - 1000 Hz, minute-dependent duration
    - UTC minutes: 10ms ticks
    - UT1 minutes (25-29, 55-59): 100ms ticks
    - Skip: second 0 (300ms marker)

================================================================================
Author: HF Time Standard Team
================================================================================
"""

import logging
import numpy as np
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Tuple, Set
from enum import Enum
from scipy import signal as scipy_signal
from scipy.signal import correlate
from scipy.signal.windows import tukey

logger = logging.getLogger(__name__)


class StationType(Enum):
    """Time signal station types"""
    WWV = "WWV"
    WWVH = "WWVH"
    CHU = "CHU"
    BPM = "BPM"


@dataclass
class TickTemplate:
    """
    Station-specific tick template configuration.
    
    Defines the characteristics of per-second ticks for matched filtering.
    """
    station: StationType
    frequency_hz: float
    tick_duration_ms: float
    skip_seconds: Set[int] = field(default_factory=set)
    
    # CHU-specific: variable duration by second
    fsk_seconds: Set[int] = field(default_factory=set)
    fsk_duration_ms: float = 10.0
    voice_seconds: Set[int] = field(default_factory=set)
    voice_duration_ms: float = 10.0
    regular_duration_ms: float = 300.0
    
    # BPM-specific: UT1 minutes have different tick duration
    ut1_minutes: Set[int] = field(default_factory=set)
    ut1_tick_duration_ms: float = 100.0


# Pre-defined station templates
WWV_TEMPLATE = TickTemplate(
    station=StationType.WWV,
    frequency_hz=1000.0,
    tick_duration_ms=5.0,
    skip_seconds={0, 29, 59},
)

WWVH_TEMPLATE = TickTemplate(
    station=StationType.WWVH,
    frequency_hz=1200.0,
    tick_duration_ms=5.0,
    skip_seconds={0, 29, 59},
)

CHU_TEMPLATE = TickTemplate(
    station=StationType.CHU,
    frequency_hz=1000.0,
    tick_duration_ms=300.0,  # Default for regular seconds
    skip_seconds={0, 29},
    fsk_seconds=set(range(31, 40)),
    fsk_duration_ms=10.0,
    voice_seconds=set(range(50, 60)),
    voice_duration_ms=10.0,
    regular_duration_ms=300.0,
)

BPM_TEMPLATE = TickTemplate(
    station=StationType.BPM,
    frequency_hz=1000.0,
    tick_duration_ms=10.0,  # UTC minutes
    skip_seconds={0},
    ut1_minutes={25, 26, 27, 28, 29, 55, 56, 57, 58, 59},
    ut1_tick_duration_ms=100.0,
)

STATION_TEMPLATES = {
    StationType.WWV: WWV_TEMPLATE,
    StationType.WWVH: WWVH_TEMPLATE,
    StationType.CHU: CHU_TEMPLATE,
    StationType.BPM: BPM_TEMPLATE,
}


@dataclass
class TickDetectionResult:
    """Result from tick matched filter detection"""
    window_start_second: int
    window_end_second: int
    timing_offset_ms: float          # Offset from expected position
    timing_uncertainty_ms: float     # Estimated uncertainty
    snr_db: float                    # Signal-to-noise ratio
    correlation_peak: float          # Normalized correlation peak (0-1)
    phase_rad: float                 # Carrier phase at detection
    coherence_quality: float         # Phase stability metric (0-1)
    valid_ticks: int                 # Number of ticks in window
    station: StationType


@dataclass 
class MinuteTickAnalysis:
    """Complete tick analysis for one minute of data"""
    station: StationType
    minute_number: int
    window_results: List[TickDetectionResult]
    
    # Aggregate statistics
    mean_timing_offset_ms: float
    std_timing_offset_ms: float
    mean_snr_db: float
    drift_rate_ms_per_sec: Optional[float]  # Linear drift if detected
    
    # Quality metrics
    valid_windows: int
    total_windows: int
    overall_confidence: float


class TickMatchedFilter:
    """
    Station-specific matched filter for per-second tick detection.
    
    Uses overlapping 5-second windows with quadrature matched filtering
    for phase-invariant detection of timing ticks.
    
    Example:
        filter = TickMatchedFilter(StationType.WWV, sample_rate=20000)
        results = filter.process_minute(iq_samples, minute_number=15)
        
        for window in results.window_results:
            print(f"Seconds {window.window_start_second}-{window.window_end_second}: "
                  f"offset={window.timing_offset_ms:+.3f}ms, SNR={window.snr_db:.1f}dB")
    """
    
    def __init__(
        self,
        station: StationType,
        sample_rate: int = 20000,
        window_seconds: int = 5,
        overlap_seconds: int = 1,
    ):
        """
        Initialize tick matched filter.
        
        Args:
            station: Station type (WWV, WWVH, CHU, BPM)
            sample_rate: Sample rate in Hz
            window_seconds: Number of seconds per detection window
            overlap_seconds: Overlap between adjacent windows (1 = max overlap)
        """
        self.station = station
        self.template_config = STATION_TEMPLATES[station]
        self.sample_rate = sample_rate
        self.window_seconds = window_seconds
        self.overlap_seconds = overlap_seconds
        
        # Pre-compute base templates (quadrature pair)
        self._templates: Dict[float, Tuple[np.ndarray, np.ndarray]] = {}
        self._build_templates()
    
    def _build_templates(self) -> None:
        """Build quadrature matched filter templates for each tick duration."""
        durations_ms = {self.template_config.tick_duration_ms}
        
        # CHU has multiple durations
        if self.station == StationType.CHU:
            durations_ms.add(self.template_config.fsk_duration_ms)
            durations_ms.add(self.template_config.voice_duration_ms)
            durations_ms.add(self.template_config.regular_duration_ms)
        
        # BPM has UT1 duration
        if self.station == StationType.BPM:
            durations_ms.add(self.template_config.ut1_tick_duration_ms)
        
        freq = self.template_config.frequency_hz
        
        for duration_ms in durations_ms:
            duration_sec = duration_ms / 1000.0
            n_samples = int(duration_sec * self.sample_rate)
            
            if n_samples < 2:
                continue
            
            t = np.arange(n_samples) / self.sample_rate
            
            # Tukey window for smooth edges (reduces spectral leakage)
            window = tukey(n_samples, alpha=0.1)
            
            # Quadrature templates (phase-invariant detection)
            template_sin = np.sin(2 * np.pi * freq * t) * window
            template_cos = np.cos(2 * np.pi * freq * t) * window
            
            # Normalize to unit energy
            energy = np.sqrt(np.sum(template_sin**2))
            if energy > 0:
                template_sin /= energy
                template_cos /= energy
            
            self._templates[duration_ms] = (template_sin, template_cos)
    
    def _get_tick_duration_ms(self, second: int, minute: int = 0) -> float:
        """
        Get tick duration for a specific second (station-dependent).
        
        Args:
            second: Second within minute (0-59)
            minute: Minute within hour (for BPM UT1/UTC)
            
        Returns:
            Tick duration in milliseconds
        """
        if self.station == StationType.CHU:
            if second in self.template_config.fsk_seconds:
                return self.template_config.fsk_duration_ms
            elif second in self.template_config.voice_seconds:
                return self.template_config.voice_duration_ms
            else:
                return self.template_config.regular_duration_ms
        
        elif self.station == StationType.BPM:
            if minute in self.template_config.ut1_minutes:
                return self.template_config.ut1_tick_duration_ms
            else:
                return self.template_config.tick_duration_ms
        
        else:
            return self.template_config.tick_duration_ms
    
    def _build_composite_template(
        self,
        start_second: int,
        end_second: int,
        minute: int = 0
    ) -> Tuple[np.ndarray, np.ndarray, List[int]]:
        """
        Build composite template for multiple ticks in a window.
        
        Args:
            start_second: First second in window
            end_second: Last second in window (exclusive)
            minute: Minute number (for BPM UT1/UTC)
            
        Returns:
            Tuple of (template_sin, template_cos, valid_seconds)
        """
        window_samples = (end_second - start_second) * self.sample_rate
        template_sin = np.zeros(window_samples)
        template_cos = np.zeros(window_samples)
        valid_seconds = []
        
        for sec in range(start_second, end_second):
            if sec in self.template_config.skip_seconds:
                continue
            
            valid_seconds.append(sec)
            
            # Get appropriate template for this second
            duration_ms = self._get_tick_duration_ms(sec, minute)
            if duration_ms not in self._templates:
                continue
            
            tick_sin, tick_cos = self._templates[duration_ms]
            
            # Position within window (tick at start of each second)
            tick_start = (sec - start_second) * self.sample_rate
            tick_end = tick_start + len(tick_sin)
            
            if tick_end <= window_samples:
                template_sin[tick_start:tick_end] += tick_sin
                template_cos[tick_start:tick_end] += tick_cos
        
        # Normalize composite template
        energy = np.sqrt(np.sum(template_sin**2))
        if energy > 0:
            template_sin /= energy
            template_cos /= energy
        
        return template_sin, template_cos, valid_seconds
    
    def _correlate_window(
        self,
        audio: np.ndarray,
        template_sin: np.ndarray,
        template_cos: np.ndarray,
        search_range_ms: float = 100.0
    ) -> Tuple[float, float, float, float]:
        """
        Perform quadrature correlation and find peak.
        
        Args:
            audio: Audio signal (AM demodulated)
            template_sin: In-phase template
            template_cos: Quadrature template
            search_range_ms: Search range around expected position (±ms)
            
        Returns:
            Tuple of (offset_ms, snr_db, peak_value, phase_rad)
        """
        # Correlate with both templates
        corr_sin = correlate(audio, template_sin, mode='same')
        corr_cos = correlate(audio, template_cos, mode='same')
        
        # Phase-invariant envelope
        envelope = np.sqrt(corr_sin**2 + corr_cos**2)
        
        # Search around center (expected position)
        center = len(envelope) // 2
        search_samples = int(search_range_ms * self.sample_rate / 1000.0)
        search_start = max(0, center - search_samples)
        search_end = min(len(envelope), center + search_samples)
        
        search_region = envelope[search_start:search_end]
        
        if len(search_region) == 0:
            return 0.0, -100.0, 0.0, 0.0
        
        # Find peak
        peak_idx_local = np.argmax(search_region)
        peak_idx = search_start + peak_idx_local
        peak_value = envelope[peak_idx]
        
        # Sub-sample interpolation (parabolic)
        if 0 < peak_idx_local < len(search_region) - 1:
            y0 = search_region[peak_idx_local - 1]
            y1 = search_region[peak_idx_local]
            y2 = search_region[peak_idx_local + 1]
            
            denom = 2 * (y0 - 2*y1 + y2)
            if abs(denom) > 1e-10:
                delta = (y0 - y2) / denom
                delta = np.clip(delta, -0.5, 0.5)
                peak_idx_refined = peak_idx + delta
            else:
                peak_idx_refined = float(peak_idx)
        else:
            peak_idx_refined = float(peak_idx)
        
        # Convert to timing offset (ms from center)
        offset_samples = peak_idx_refined - center
        offset_ms = (offset_samples / self.sample_rate) * 1000.0
        
        # Estimate SNR
        noise_region = np.concatenate([
            envelope[:search_start],
            envelope[search_end:]
        ])
        if len(noise_region) > 0:
            noise_std = np.std(noise_region)
            if noise_std > 0:
                snr_linear = peak_value / noise_std
                snr_db = 20 * np.log10(snr_linear) if snr_linear > 0 else -100.0
            else:
                snr_db = 40.0  # Very clean signal
        else:
            snr_db = 0.0
        
        # Extract phase at peak
        if peak_idx < len(corr_sin):
            phase_rad = np.arctan2(corr_sin[peak_idx], corr_cos[peak_idx])
        else:
            phase_rad = 0.0
        
        # Normalize peak value (0-1)
        max_possible = np.sqrt(np.sum(audio**2)) * np.sqrt(np.sum(template_sin**2))
        if max_possible > 0:
            peak_normalized = peak_value / max_possible
        else:
            peak_normalized = 0.0
        
        return offset_ms, snr_db, peak_normalized, phase_rad
    
    def process_window(
        self,
        iq_samples: np.ndarray,
        start_second: int,
        end_second: int,
        minute: int = 0
    ) -> Optional[TickDetectionResult]:
        """
        Process a single window of IQ samples.
        
        Args:
            iq_samples: Complex IQ samples for the window
            start_second: First second in window
            end_second: Last second in window (exclusive)
            minute: Minute number (for BPM)
            
        Returns:
            TickDetectionResult or None if detection failed
        """
        # AM demodulation
        magnitude = np.abs(iq_samples)
        audio = magnitude - np.mean(magnitude)  # AC coupling
        
        # Bandpass filter around station-specific tick frequency
        # This is critical for WWV/WWVH discrimination on shared channels:
        # - WWV uses 1000 Hz ticks
        # - WWVH uses 1200 Hz ticks
        # Without filtering, the matched filter can respond to the wrong station
        tick_freq = self.template_config.frequency_hz
        bandwidth = 100.0  # ±100 Hz bandwidth
        low_freq = tick_freq - bandwidth
        high_freq = tick_freq + bandwidth
        
        # Ensure frequencies are valid for the sample rate
        nyquist = self.sample_rate / 2
        if high_freq < nyquist and low_freq > 0:
            from scipy.signal import butter, sosfiltfilt
            sos = butter(4, [low_freq, high_freq], btype='band', fs=self.sample_rate, output='sos')
            audio = sosfiltfilt(sos, audio)
        
        # Build composite template for this window
        template_sin, template_cos, valid_seconds = self._build_composite_template(
            start_second, end_second, minute
        )
        
        if len(valid_seconds) == 0:
            return None
        
        # Ensure audio and template are same length
        expected_samples = (end_second - start_second) * self.sample_rate
        if len(audio) < expected_samples:
            # Pad if needed
            audio = np.pad(audio, (0, expected_samples - len(audio)))
        elif len(audio) > expected_samples:
            audio = audio[:expected_samples]
        
        # Correlate
        offset_ms, snr_db, peak_value, phase_rad = self._correlate_window(
            audio, template_sin, template_cos
        )
        
        # Estimate uncertainty based on SNR
        # Higher SNR → lower uncertainty
        if snr_db > 20:
            uncertainty_ms = 0.05  # ~1 sample at 20 kHz
        elif snr_db > 10:
            uncertainty_ms = 0.2
        elif snr_db > 6:
            uncertainty_ms = 0.5
        else:
            uncertainty_ms = 1.0
        
        return TickDetectionResult(
            window_start_second=start_second,
            window_end_second=end_second,
            timing_offset_ms=offset_ms,
            timing_uncertainty_ms=uncertainty_ms,
            snr_db=snr_db,
            correlation_peak=peak_value,
            phase_rad=phase_rad,
            coherence_quality=min(1.0, peak_value * 2),  # Rough estimate
            valid_ticks=len(valid_seconds),
            station=self.station,
        )
    
    def process_minute(
        self,
        iq_samples: np.ndarray,
        minute_number: int = 0,
        min_snr_db: float = 3.0
    ) -> MinuteTickAnalysis:
        """
        Process a full minute of IQ samples with overlapping windows.
        
        Args:
            iq_samples: Complex IQ samples (60 seconds at sample_rate)
            minute_number: Minute within hour (for BPM UT1/UTC)
            min_snr_db: Minimum SNR to consider a valid detection
            
        Returns:
            MinuteTickAnalysis with all window results
        """
        expected_samples = 60 * self.sample_rate
        if len(iq_samples) < expected_samples:
            logger.warning(f"Incomplete minute: {len(iq_samples)} < {expected_samples} samples")
        
        window_results: List[TickDetectionResult] = []
        
        # Generate overlapping windows
        # Start at second 1 (skip minute marker at second 0)
        step = self.overlap_seconds
        
        for start_sec in range(1, 60 - self.window_seconds + 1, step):
            end_sec = start_sec + self.window_seconds
            
            # Extract window samples
            start_sample = start_sec * self.sample_rate
            end_sample = end_sec * self.sample_rate
            
            if end_sample > len(iq_samples):
                break
            
            window_iq = iq_samples[start_sample:end_sample]
            
            result = self.process_window(window_iq, start_sec, end_sec, minute_number)
            
            if result is not None and result.snr_db >= min_snr_db:
                window_results.append(result)
        
        # Compute aggregate statistics
        if window_results:
            offsets = [r.timing_offset_ms for r in window_results]
            snrs = [r.snr_db for r in window_results]
            
            mean_offset = float(np.mean(offsets))
            std_offset = float(np.std(offsets))
            mean_snr = float(np.mean(snrs))
            
            # Estimate linear drift if enough windows
            drift_rate = None
            if len(window_results) >= 5:
                # Linear regression: offset vs window center time
                times = [(r.window_start_second + r.window_end_second) / 2 
                        for r in window_results]
                if np.std(times) > 0:
                    slope, _ = np.polyfit(times, offsets, 1)
                    drift_rate = float(slope)  # ms per second
            
            # Overall confidence based on consistency and SNR
            consistency = 1.0 / (1.0 + std_offset)  # Lower std → higher confidence
            snr_factor = min(1.0, mean_snr / 20.0)
            coverage = len(window_results) / 55.0  # Expected ~55 windows
            overall_confidence = consistency * snr_factor * coverage
        else:
            mean_offset = 0.0
            std_offset = 0.0
            mean_snr = -100.0
            drift_rate = None
            overall_confidence = 0.0
        
        total_windows = (60 - self.window_seconds) // self.overlap_seconds
        
        return MinuteTickAnalysis(
            station=self.station,
            minute_number=minute_number,
            window_results=window_results,
            mean_timing_offset_ms=mean_offset,
            std_timing_offset_ms=std_offset,
            mean_snr_db=mean_snr,
            drift_rate_ms_per_sec=drift_rate,
            valid_windows=len(window_results),
            total_windows=total_windows,
            overall_confidence=overall_confidence,
        )


def create_tick_filter(
    station: str,
    sample_rate: int = 20000,
    window_seconds: int = 5,
    overlap_seconds: int = 1
) -> TickMatchedFilter:
    """
    Factory function to create a tick matched filter.
    
    Args:
        station: Station name ('WWV', 'WWVH', 'CHU', 'BPM')
        sample_rate: Sample rate in Hz
        window_seconds: Seconds per detection window
        overlap_seconds: Overlap between windows
        
    Returns:
        TickMatchedFilter configured for the station
    """
    station_type = StationType(station.upper())
    return TickMatchedFilter(
        station=station_type,
        sample_rate=sample_rate,
        window_seconds=window_seconds,
        overlap_seconds=overlap_seconds,
    )
