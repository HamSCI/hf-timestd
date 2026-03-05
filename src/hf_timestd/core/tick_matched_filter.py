#!/usr/bin/env python3
"""
Tick Matched Filter - Station-Specific Timing and Phase Extraction

================================================================================
PURPOSE
================================================================================
Extract timing and carrier phase from WWV/WWVH/CHU/BPM using IQ-domain
matched filtering with station-specific templates.

Two-tier detection hierarchy:

1. MINUTE MARKER (primary timing):
   - WWV/WWVH: 800ms tone at second 0 (1000/1200 Hz)
   - CHU: 500ms tone at second 0
   - BPM: 300ms tone at second 0
   - 160× more energy than 5ms ticks → robust under fading
   - Single high-SNR timing anchor per minute

2. PER-SECOND TICKS (phase extraction, augments timing when present):
   - WWV/WWVH: 5ms ticks (1000/1200 Hz)
   - CHU: 300ms regular, 10ms FSK/voice
   - BPM: 10ms UTC, 100ms UT1
   - Primary value: carrier phase time series for ionospheric analysis
   - Timing augmentation only when SNR > 8 dB

================================================================================
SIGNAL ENVIRONMENT
================================================================================
WWV/WWVH broadcast multiple simultaneous tones on shared channels:
   - 100 Hz BCD time code modulation (continuous)
   - 440/500/600 Hz audio tones (schedule-dependent, continuous)
   - 1000 Hz per-second ticks (5ms) and minute marker (800ms)
   - 1200 Hz WWVH ticks (5ms) and minute marker (800ms)

The 5ms ticks have only 0.5% duty cycle per second and are buried under
the continuous tones. A bandpass filter in the IQ domain is essential to
isolate the tick frequency before correlation.

All correlation uses complex IQ templates (exp(j2πft)) rather than
AM-demodulated audio. This avoids the timing ambiguity inherent in
envelope detection, where |IQ| creates multiple near-equal correlation
peaks separated by the tone period.

================================================================================
OVERLAPPING WINDOW STRATEGY
================================================================================
Using 5-second windows with 1-second overlap:

    Window 0: seconds 1-5   (5 ticks)
    Window 1: seconds 2-6   (5 ticks, shares 4 with Window 0)
    ...
    Window 54: seconds 55-59 (5 ticks)

Benefits:
- 55 independent phase estimates per minute
- Adjacent windows highly correlated → smooth phase tracking
- Robust to single-tick dropouts
- Can detect Doppler-induced phase drift across minute

================================================================================
STATION-SPECIFIC PATTERNS
================================================================================
WWV (Fort Collins, CO):
    - 1000 Hz, 5ms per-second ticks, 800ms minute marker
    - Silent: seconds 29, 59

WWVH (Kauai, HI):
    - 1200 Hz, 5ms per-second ticks, 800ms minute marker
    - Silent: seconds 29, 59

CHU (Ottawa, Canada):
    - 1000 Hz, variable duration
    - Regular seconds: 300ms tones
    - FSK seconds (31-39): 10ms ticks
    - Voice seconds (50-59): 10ms ticks
    - Second 0: 500ms marker; second 29: silent

BPM (China):
    - 1000 Hz, minute-dependent duration
    - UTC minutes: 10ms ticks
    - UT1 minutes (25-29, 55-59): 100ms ticks
    - Second 0: 300ms marker

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
from scipy.signal import correlate, butter, sosfiltfilt
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
    
    Defines the characteristics of per-second ticks and minute markers.
    """
    station: StationType
    frequency_hz: float
    tick_duration_ms: float
    skip_seconds: Set[int] = field(default_factory=set)
    
    # Minute marker at second 0 (primary timing source)
    minute_marker_duration_ms: float = 800.0
    
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
# NOTE: second 0 is NOT in skip_seconds — it carries the minute marker,
# our primary timing source (800ms for WWV/WWVH, 500ms CHU, 300ms BPM).
# Only truly silent seconds are skipped.
WWV_TEMPLATE = TickTemplate(
    station=StationType.WWV,
    frequency_hz=1000.0,
    tick_duration_ms=5.0,
    skip_seconds={29, 59},
    minute_marker_duration_ms=800.0,
)

WWVH_TEMPLATE = TickTemplate(
    station=StationType.WWVH,
    frequency_hz=1200.0,
    tick_duration_ms=5.0,
    skip_seconds={29, 59},
    minute_marker_duration_ms=800.0,
)

CHU_TEMPLATE = TickTemplate(
    station=StationType.CHU,
    frequency_hz=1000.0,
    tick_duration_ms=300.0,  # Default for regular seconds
    skip_seconds={29},
    minute_marker_duration_ms=500.0,
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
    skip_seconds=set(),
    minute_marker_duration_ms=300.0,
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
    phase_rad: float                 # Audio-domain modulation phase (from AM envelope correlator)
    carrier_phase_rad: float         # RF carrier phase at tone freq (from IQ mix-down)
    dc_carrier_phase_rad: float      # Bare carrier phase from mean(IQ) DC phasor
    coherence_quality: float         # Phase stability metric (0-1)
    valid_ticks: int                 # Number of ticks in window
    station: StationType


@dataclass 
class MinuteTickAnalysis:
    """Complete tick analysis for one minute of data.
    
    Two-tier structure:
    - Minute marker (primary timing): single high-SNR detection at second 0
    - Per-second ticks (phase extraction): overlapping windows for Doppler/phase
    
    When the minute marker is detected, mean_timing_offset_ms uses the marker
    value. Per-second tick windows augment timing only when the marker is absent.
    """
    station: StationType
    minute_number: int
    window_results: List[TickDetectionResult]
    
    # Aggregate statistics (marker-primary when available)
    mean_timing_offset_ms: float
    std_timing_offset_ms: float
    mean_snr_db: float
    drift_rate_ms_per_sec: Optional[float]  # Linear drift if detected
    
    # Quality metrics
    valid_windows: int
    total_windows: int
    overall_confidence: float
    
    # Minute marker (primary timing source)
    marker_detected: bool = False
    marker_timing_offset_ms: float = 0.0
    marker_snr_db: float = -100.0
    marker_uncertainty_ms: float = 999.0
    
    # Per-second tick aggregate (phase extraction, secondary timing)
    tick_mean_offset_ms: float = 0.0
    tick_std_offset_ms: float = 0.0
    tick_valid_windows: int = 0
    
    # D_clock: proper timing error computed via timing authority (buffer_timing).
    # None if buffer_timing was not provided (no timing authority available).
    # When buffer_timing IS provided, mean_timing_offset_ms is already a true
    # ToA residual from the UTC second boundary, and d_clock_ms equals it.
    d_clock_ms: Optional[float] = None
    d_clock_uncertainty_ms: Optional[float] = None
    
    # Doppler shift derived from carrier phase slope across per-second ticks.
    # Positive = approaching (frequency increase), negative = receding.
    # None if insufficient phase data or poor fit quality.
    doppler_hz: Optional[float] = None
    doppler_uncertainty_hz: Optional[float] = None


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
        
        # Pre-compute base templates (quadrature pair for AM, complex for IQ)
        self._templates: Dict[float, Tuple[np.ndarray, np.ndarray]] = {}
        self._iq_templates: Dict[float, np.ndarray] = {}  # Complex IQ templates
        self._am_templates: Dict[float, np.ndarray] = {}  # AM envelope templates
        
        # Pre-allocated buffers for zero-allocation DSP
        self._max_samples = 65 * self.sample_rate
        self._envelope_buffer = np.empty(self._max_samples, dtype=np.float32)
        self._build_templates()
        self._build_iq_templates()
        self._build_am_templates()
        
        # IQ-domain bandpass filter to isolate tick frequency.
        # Rejects continuous 100 Hz BCD, 440/500/600 Hz audio tones.
        # ±100 Hz bandwidth around the station tone frequency.
        freq = self.template_config.frequency_hz
        bw = 100.0  # ±100 Hz
        low = freq - bw
        high = freq + bw
        nyquist = self.sample_rate / 2
        if low > 0 and high < nyquist:
            self._bandpass_sos = butter(
                4, [low, high], btype='band', fs=self.sample_rate, output='sos'
            )
        else:
            self._bandpass_sos = None
    
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
    
    def _build_am_templates(self) -> None:
        """Build AM envelope templates for timing correlation.
        
        After AM demodulation (|IQ| - DC), the tick signal is a rectified
        tone burst: a Tukey-windowed pulse at the tone frequency.  The
        correlation template is the *envelope* of that burst — a smooth
        rectangular pulse with tapered edges.
        
        Unlike IQ-domain correlation (which is flat on a continuous carrier),
        AM-domain correlation produces a sharp peak at the tone onset because
        the information is in the amplitude keying, not the carrier phase.
        """
        durations_ms = {self.template_config.tick_duration_ms}
        durations_ms.add(self.template_config.minute_marker_duration_ms)
        
        if self.station == StationType.CHU:
            durations_ms.add(self.template_config.fsk_duration_ms)
            durations_ms.add(self.template_config.voice_duration_ms)
            durations_ms.add(self.template_config.regular_duration_ms)
        
        if self.station == StationType.BPM:
            durations_ms.add(self.template_config.ut1_tick_duration_ms)
        
        freq = self.template_config.frequency_hz
        
        for duration_ms in durations_ms:
            duration_sec = duration_ms / 1000.0
            n_samples = int(duration_sec * self.sample_rate)
            
            if n_samples < 2:
                continue
            
            t = np.arange(n_samples) / self.sample_rate
            alpha = 0.05 if duration_ms >= 100.0 else 0.1
            window = tukey(n_samples, alpha=alpha)
            
            # AM envelope template: what |IQ| looks like during a tone burst.
            # After AM demod, the tone burst is a rectified sinusoid shaped
            # by the Tukey window.  We use the RMS envelope (smooth pulse).
            tone = np.sin(2 * np.pi * freq * t) * window
            # Rectified envelope (half-wave, then smooth)
            # For matched filtering, the envelope shape is what matters.
            # A simple Tukey pulse works well as the template.
            template = window.copy()
            
            # Normalize to unit energy
            energy = np.sqrt(np.sum(template**2))
            if energy > 0:
                template /= energy
            
            self._am_templates[duration_ms] = template
    
    def _build_iq_templates(self) -> None:
        """Build complex IQ-domain templates for carrier phase extraction.
        
        Templates are complex exponentials at the tick frequency, windowed
        with a Tukey window. Used for carrier phase measurement (Doppler),
        NOT for timing — IQ correlation is flat on a continuous AM carrier.
        """
        durations_ms = {self.template_config.tick_duration_ms}
        
        # Minute marker (primary timing source)
        durations_ms.add(self.template_config.minute_marker_duration_ms)
        
        if self.station == StationType.CHU:
            durations_ms.add(self.template_config.fsk_duration_ms)
            durations_ms.add(self.template_config.voice_duration_ms)
            durations_ms.add(self.template_config.regular_duration_ms)
        
        if self.station == StationType.BPM:
            durations_ms.add(self.template_config.ut1_tick_duration_ms)
        
        freq = self.template_config.frequency_hz
        
        for duration_ms in durations_ms:
            duration_sec = duration_ms / 1000.0
            n_samples = int(duration_sec * self.sample_rate)
            
            if n_samples < 2:
                continue
            
            t = np.arange(n_samples) / self.sample_rate
            # Shorter taper for long templates (minute marker), wider for short ticks
            alpha = 0.05 if duration_ms >= 100.0 else 0.1
            window = tukey(n_samples, alpha=alpha)
            
            # Complex exponential at tone frequency
            template = np.exp(1j * 2 * np.pi * freq * t) * window
            
            # Normalize to unit energy
            energy = np.sqrt(np.sum(np.abs(template)**2))
            if energy > 0:
                template /= energy
            
            self._iq_templates[duration_ms] = template
    
    def _get_tick_duration_ms(self, second: int, minute: int = 0) -> float:
        """
        Get tick/marker duration for a specific second (station-dependent).
        
        Second 0 returns the minute marker duration (primary timing source).
        
        Args:
            second: Second within minute (0-59)
            minute: Minute within hour (for BPM UT1/UTC)
            
        Returns:
            Tick or marker duration in milliseconds
        """
        # Second 0: minute marker (primary timing source)
        if second == 0:
            return self.template_config.minute_marker_duration_ms
        
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
    
    def _correlate_tick_iq(
        self,
        iq_slice: np.ndarray,
        template: np.ndarray,
        search_samples: int
    ) -> Tuple[float, float, float]:
        """
        Correlate a single tick's IQ template against an IQ slice.
        
        Uses complex IQ-domain correlation which produces a single sharp
        peak, unlike AM-domain correlation which has multiple ambiguous
        peaks separated by the tone period.
        
        Args:
            iq_slice: Complex IQ samples around expected tick position.
                      Length = search_samples + tick_len + search_samples.
            template: Complex IQ template (from _iq_templates).
            search_samples: Number of samples in the search margin on each side.
            
        Returns:
            Tuple of (offset_samples, peak_value, snr_db) where offset_samples
            is the sub-sample refined offset from the expected tick position.
        """
        corr = correlate(iq_slice, template, mode='valid')
        envelope = np.abs(corr)
        
        if len(envelope) == 0:
            return 0.0, 0.0, -100.0
        
        peak_idx = int(np.argmax(envelope))
        peak_value = envelope[peak_idx]
        
        # Sub-sample interpolation (parabolic on magnitude envelope)
        if 0 < peak_idx < len(envelope) - 1:
            y0 = envelope[peak_idx - 1]
            y1 = envelope[peak_idx]
            y2 = envelope[peak_idx + 1]
            
            denom = 2 * (y0 - 2*y1 + y2)
            if abs(denom) > 1e-10:
                delta = float((y0 - y2) / denom)
                delta = np.clip(delta, -0.5, 0.5)
                peak_idx_refined = peak_idx + delta
            else:
                peak_idx_refined = float(peak_idx)
        else:
            peak_idx_refined = float(peak_idx)
        
        # Offset from expected position (center of search region)
        offset_samples = peak_idx_refined - search_samples
        
        # SNR: peak vs noise floor.
        # Exclude a zone around the peak for noise estimation.
        # For short templates (5ms tick), exclude ±tick_len.
        # For long templates (800ms marker), the correlation output may be
        # shorter than 2×tick_len, so cap the exclusion to ≤25% of the
        # envelope on each side (guaranteeing ≥50% for noise).
        tick_len = len(template)
        max_exclusion = max(10, len(envelope) // 4)
        exclusion = min(tick_len, max_exclusion)
        peak_start = max(0, peak_idx - exclusion)
        peak_end = min(len(envelope), peak_idx + exclusion)
        noise_region = np.concatenate([
            envelope[:peak_start],
            envelope[peak_end:]
        ])
        if len(noise_region) > 10:
            noise_std = np.std(noise_region)
            if noise_std > 0:
                snr_linear = peak_value / noise_std
                snr_db = 20 * np.log10(max(snr_linear, 1e-10))
            else:
                snr_db = 40.0 if peak_value > 1e-6 else 0.0
        else:
            snr_db = 0.0
        
        return offset_samples, float(peak_value), snr_db
    
    def _bandpass_iq(self, iq_samples: np.ndarray) -> np.ndarray:
        """Apply IQ-domain bandpass filter to isolate tick frequency.
        
        Rejects continuous 100 Hz BCD modulation, 440/500/600 Hz audio tones,
        and other out-of-band energy that would otherwise dominate the
        correlation for short (5ms) ticks.
        
        Args:
            iq_samples: Complex IQ samples
            
        Returns:
            Bandpass-filtered complex IQ samples
        """
        if self._bandpass_sos is None:
            return iq_samples
        return sosfiltfilt(self._bandpass_sos, iq_samples)
    
    def _am_demodulate(self, iq_samples: np.ndarray) -> np.ndarray:
        """AM demodulation bridge: IQ → audio envelope.
        
        WWV/WWVH/CHU/BPM use standard AM (DSB).  The timing information
        (tick on/off keying) is encoded in the magnitude of the carrier.
        
        Two-stage envelope process:
          1. RF envelope (this method): |IQ| - DC → real-valued audio
          2. Audio envelope (done by correlation): template match on pulse shape
        
        Args:
            iq_samples: Complex IQ samples (bandpass-filtered or raw)
            
        Returns:
            Real-valued AM-demodulated audio (DC-blocked)
        """
        # Magnitude extraction: sqrt(I² + Q²)
        rf_envelope = np.abs(iq_samples)
        # DC block: remove the static carrier power, leaving AC audio
        audio = rf_envelope - np.mean(rf_envelope)
        return audio
    
    def _correlate_tick_am(
        self,
        audio_slice: np.ndarray,
        template: np.ndarray,
        search_samples: int
    ) -> Tuple[float, float, float]:
        """Correlate an AM envelope template against demodulated audio.
        
        Unlike IQ-domain correlation (which is flat on a continuous carrier),
        AM-domain correlation produces a sharp peak at the tone onset because
        the information is in the amplitude keying.
        
        Args:
            audio_slice: Real-valued AM-demodulated audio around expected tick.
                         Length ≈ search_samples + tick_len + search_samples.
            template: Real-valued AM envelope template (from _am_templates).
            search_samples: Samples in the search margin on each side.
            
        Returns:
            Tuple of (offset_samples, peak_value, snr_db) where offset_samples
            is the sub-sample refined offset from the expected tick position.
        """
        corr = correlate(audio_slice, template, mode='valid')
        
        if len(corr) == 0:
            return 0.0, 0.0, -100.0
        
        peak_idx = int(np.argmax(corr))
        peak_value = corr[peak_idx]
        
        # Sub-sample interpolation (parabolic)
        if 0 < peak_idx < len(corr) - 1:
            y0 = corr[peak_idx - 1]
            y1 = corr[peak_idx]
            y2 = corr[peak_idx + 1]
            
            denom = 2 * (y0 - 2*y1 + y2)
            if abs(denom) > 1e-10:
                delta = float((y0 - y2) / denom)
                delta = np.clip(delta, -0.5, 0.5)
                peak_idx_refined = peak_idx + delta
            else:
                peak_idx_refined = float(peak_idx)
        else:
            peak_idx_refined = float(peak_idx)
        
        # Offset from expected position (center of search region)
        offset_samples = peak_idx_refined - search_samples
        
        # SNR: peak vs noise floor
        tick_len = len(template)
        max_exclusion = max(10, len(corr) // 4)
        exclusion = min(tick_len, max_exclusion)
        peak_start = max(0, peak_idx - exclusion)
        peak_end = min(len(corr), peak_idx + exclusion)
        noise_region = np.concatenate([
            corr[:peak_start],
            corr[peak_end:]
        ])
        if len(noise_region) > 10:
            noise_std = np.std(noise_region)
            if noise_std > 0:
                snr_linear = peak_value / noise_std
                snr_db = 20 * np.log10(max(snr_linear, 1e-10))
            else:
                snr_db = 40.0 if peak_value > 1e-6 else 0.0
        else:
            snr_db = 0.0
        
        return offset_samples, float(peak_value), snr_db
    
    def process_window(
        self,
        iq_samples: np.ndarray,
        start_second: int,
        end_second: int,
        minute: int = 0,
        iq_filtered: np.ndarray = None,
        iq_unfiltered: np.ndarray = None,
        buffer_timing=None,
        minute_boundary: int = 0,
    ) -> Optional[TickDetectionResult]:
        """
        Process a single window of IQ samples for timing and phase.
        
        Timing: AM-demodulated envelope correlated against pulse templates.
        Phase:  Unfiltered IQ mixed down at tone frequency for Doppler.
        
        The AM demodulation bridge (|IQ| - DC) is essential because WWV/WWVH
        use standard AM: timing is in the amplitude keying, not the carrier
        phase.  IQ-domain correlation is flat on a continuous carrier and
        cannot resolve tick onset times.
        
        Per-tick offsets are combined with the median for robustness against
        individual tick dropouts or interference.
        
        Args:
            iq_samples: Complex IQ samples for the window (used if filtered/unfiltered not provided)
            start_second: First second in window
            end_second: Last second in window (exclusive)
            minute: Minute number (for BPM)
            iq_filtered: Pre-filtered IQ for correlation (optional, avoids re-filtering)
            iq_unfiltered: Unfiltered IQ for phase extraction (optional)
            buffer_timing: BufferTiming object for UTC↔sample conversion.
                          When provided, timing_offset_ms is a true ToA residual
                          from the UTC second boundary. Without it, falls back to
                          buffer-relative offsets.
            minute_boundary: Unix timestamp of the minute boundary (integer seconds).
            
        Returns:
            TickDetectionResult or None if detection failed
        """
        tone_freq_hz = self.template_config.frequency_hz
        search_range_ms = 100.0  # ±100ms search per tick
        search_samples = int(search_range_ms * self.sample_rate / 1000.0)
        
        # Use pre-filtered IQ if provided, otherwise filter now
        if iq_filtered is not None:
            iq_for_corr = iq_filtered
        else:
            iq_for_corr = self._bandpass_iq(iq_samples)
        
        # AM demodulation bridge: IQ → audio envelope for timing
        # The bandpass filter isolates the tick frequency; AM demod then
        # extracts the pulse shape (on/off keying) that carries timing.
        am_audio = self._am_demodulate(iq_for_corr)
        
        # Unfiltered IQ for phase extraction
        if iq_unfiltered is not None:
            iq_for_phase = iq_unfiltered
        else:
            iq_for_phase = iq_samples
        
        # Determine which seconds have ticks in this window
        valid_seconds = []
        for sec in range(start_second, end_second):
            if sec in self.template_config.skip_seconds:
                continue
            valid_seconds.append(sec)
        
        if len(valid_seconds) == 0:
            return None
        
        # Per-tick AM-domain correlation for timing, IQ for phase
        tick_offsets = []      # offset in samples from expected position
        tick_snrs = []         # SNR per tick
        tick_peaks = []        # correlation peak value per tick
        carrier_phasors = []   # carrier phase phasors for coherent combination
        dc_phasors = []        # DC carrier phasors
        
        for sec in valid_seconds:
            duration_ms = self._get_tick_duration_ms(sec, minute)
            if duration_ms not in self._am_templates:
                continue
            
            am_template = self._am_templates[duration_ms]
            tick_len = len(am_template)
            
            # Tick expected position within this window.
            # When buffer_timing is available, compute from absolute UTC so
            # that timing_offset_ms is a true ToA residual from the UTC
            # second boundary — independent of where the buffer starts.
            # Without buffer_timing, fall back to buffer-relative position.
            if buffer_timing is not None and minute_boundary > 0:
                # Absolute UTC time of this tick's second boundary
                tick_utc = float(minute_boundary + sec)
                # Expected sample in the FULL buffer
                tick_sample_full = buffer_timing.utc_to_sample(tick_utc)
                # Window starts at start_second in the full buffer
                window_start_sample_full = buffer_timing.utc_to_sample(
                    float(minute_boundary + start_second)
                )
                tick_offset_in_window = int(round(
                    tick_sample_full - window_start_sample_full
                ))
            else:
                tick_offset_in_window = (sec - start_second) * self.sample_rate
            
            # Search range scales with template duration:
            # 800ms marker needs wider search (±500ms for ionospheric variation)
            # 5ms tick uses ±100ms
            if duration_ms >= 100.0:
                tick_search_ms = min(500.0, max(100.0, duration_ms * 0.625))
            else:
                tick_search_ms = search_range_ms
            tick_search_samples = int(tick_search_ms * self.sample_rate / 1000.0)
            
            # Extract AM audio slice: search before + tick + search after
            slice_start = tick_offset_in_window - tick_search_samples
            slice_end = tick_offset_in_window + tick_len + tick_search_samples
            
            if slice_start < 0 or slice_end > len(am_audio):
                continue
            
            audio_slice = am_audio[slice_start:slice_end]
            
            # AM-domain correlation for timing
            offset_samp, peak_val, snr_db = self._correlate_tick_am(
                audio_slice, am_template, tick_search_samples
            )
            
            tick_offsets.append(offset_samp)
            tick_snrs.append(snr_db)
            tick_peaks.append(peak_val)
            
            # Extract carrier phase at the detected tick position using
            # BUFFER-RELATIVE time for phase continuity across windows.
            # Use UNFILTERED IQ for phase (bandpass distorts phase).
            adjusted_start = int(tick_offset_in_window + offset_samp)
            adjusted_end = adjusted_start + tick_len
            
            if 0 <= adjusted_start and adjusted_end <= len(iq_for_phase):
                iq_tick = iq_for_phase[adjusted_start:adjusted_end]
                n_tick = len(iq_tick)
                if n_tick > 0:
                    # Buffer-relative time (seconds from start of 60s buffer)
                    t_abs = (start_second + adjusted_start / self.sample_rate) + np.arange(n_tick) / self.sample_rate
                    
                    # Mix down to baseband at tone frequency
                    mixer = np.exp(-1j * 2 * np.pi * tone_freq_hz * t_abs)
                    mixed = iq_tick * mixer
                    carrier_phasors.append(np.mean(mixed))
                    
                    # DC carrier phasor (mean IQ over tick — no mixer needed)
                    dc_phasors.append(np.mean(iq_tick))
        
        if len(tick_offsets) == 0:
            return None
        
        tick_offsets = np.array(tick_offsets)
        tick_snrs = np.array(tick_snrs)
        tick_peaks = np.array(tick_peaks)
        
        # Combine per-tick offsets: use median for robustness
        median_offset_samples = float(np.median(tick_offsets))
        offset_ms = (median_offset_samples / self.sample_rate) * 1000.0
        
        # Aggregate SNR (mean of per-tick SNRs in dB)
        mean_snr_db = float(np.mean(tick_snrs))
        
        # Aggregate peak value (mean, normalized to 0-1 range)
        mean_peak = float(np.mean(tick_peaks))
        max_peak = float(np.max(tick_peaks)) if len(tick_peaks) > 0 else 0.0
        peak_normalized = min(1.0, mean_peak / (max_peak + 1e-10)) if max_peak > 0 else 0.0
        
        # Audio-domain phase from the IQ correlation at the median offset
        phase_rad = 0.0
        
        # Carrier phase: coherent combination of per-tick phasors
        carrier_phase_rad = 0.0
        dc_carrier_phase_rad = 0.0
        if carrier_phasors:
            combined_carrier = np.sum(carrier_phasors)
            carrier_phase_rad = float(np.angle(combined_carrier))
            
            combined_dc = np.sum(dc_phasors)
            dc_carrier_phase_rad = float(np.angle(combined_dc))
        
        # Estimate uncertainty based on SNR
        if mean_snr_db > 20:
            uncertainty_ms = 0.05
        elif mean_snr_db > 10:
            uncertainty_ms = 0.2
        elif mean_snr_db > 6:
            uncertainty_ms = 0.5
        else:
            uncertainty_ms = 1.0
        
        return TickDetectionResult(
            window_start_second=start_second,
            window_end_second=end_second,
            timing_offset_ms=offset_ms,
            timing_uncertainty_ms=uncertainty_ms,
            snr_db=mean_snr_db,
            correlation_peak=peak_normalized,
            phase_rad=phase_rad,
            carrier_phase_rad=carrier_phase_rad,
            dc_carrier_phase_rad=dc_carrier_phase_rad,
            coherence_quality=min(1.0, peak_normalized * 2),
            valid_ticks=len(tick_offsets),
            station=self.station,
        )
    
    def _detect_minute_marker(
        self,
        iq_filtered: np.ndarray,
        iq_unfiltered: np.ndarray,
        buffer_timing=None,
        minute_boundary: int = 0,
    ) -> Optional[TickDetectionResult]:
        """
        Detect the minute marker at second 0 (primary timing source).
        
        The minute marker has 160× more energy than a 5ms tick, making it
        detectable under fading conditions where per-second ticks are lost.
        
        The marker tone arrives at second_0_UTC + geometric_delay (a few ms).
        We use buffer_timing to find where second 0 falls in the buffer,
        then search forward from there.
        
        Args:
            iq_filtered: Bandpass-filtered IQ (full minute)
            iq_unfiltered: Unfiltered IQ (full minute, for phase extraction)
            buffer_timing: BufferTiming mapping sample indices to UTC.
            minute_boundary: Unix timestamp of the minute boundary.
            
        Returns:
            TickDetectionResult for the minute marker, or None
        """
        marker_ms = self.template_config.minute_marker_duration_ms
        if marker_ms not in self._am_templates:
            return None
        
        am_template = self._am_templates[marker_ms]
        tick_len = len(am_template)
        
        # AM demodulation bridge: IQ → audio envelope for timing
        am_audio = self._am_demodulate(iq_filtered)
        
        # Find where second 0 falls in the buffer using the timing authority.
        # The marker tone arrives at sec0 + geometric_delay (HF skywave:
        # typically 3–40ms, up to ~80ms for long multi-hop nighttime paths).
        if buffer_timing is not None and minute_boundary > 0:
            sec0_sample = buffer_timing.utc_to_sample(float(minute_boundary))
        else:
            # Fallback: assume buffer starts at minute boundary
            sec0_sample = 0.0
        
        expected_sample = int(max(0, sec0_sample))
        
        # Search window: from a few ms before sec0 (buffer jitter) through
        # 100ms after (covers all realistic HF propagation delays).
        search_before_ms = 5.0
        search_after_ms = 100.0
        search_before_samp = int(search_before_ms * self.sample_rate / 1000.0)
        search_after_samp = int(search_after_ms * self.sample_rate / 1000.0)
        
        slice_start = max(0, expected_sample - search_before_samp)
        slice_end = expected_sample + search_after_samp + tick_len
        
        if slice_end > len(am_audio):
            return None
        
        audio_slice = am_audio[slice_start:slice_end]
        
        # The expected position within the slice is where sec0 falls.
        # _correlate_tick_am returns offset relative to this position.
        expected_pos_in_slice = expected_sample - slice_start
        
        offset_samp, peak_val, snr_db = self._correlate_tick_am(
            audio_slice, am_template, expected_pos_in_slice
        )
        
        # offset_samp is relative to expected_sample.  The detected sample
        # in the full buffer is expected_sample + offset_samp.
        detected_sample = expected_sample + offset_samp
        offset_ms = (offset_samp / self.sample_rate) * 1000.0
        
        # Extract carrier phase from unfiltered IQ at detected position
        carrier_phase_rad = 0.0
        dc_carrier_phase_rad = 0.0
        det_start = int(detected_sample)
        det_end = det_start + tick_len
        
        if 0 <= det_start and det_end <= len(iq_unfiltered):
            iq_tick = iq_unfiltered[det_start:det_end]
            n_tick = len(iq_tick)
            if n_tick > 0:
                tone_freq_hz = self.template_config.frequency_hz
                t_abs = (det_start / self.sample_rate) + np.arange(n_tick) / self.sample_rate
                mixer = np.exp(-1j * 2 * np.pi * tone_freq_hz * t_abs)
                mixed = iq_tick * mixer
                carrier_phase_rad = float(np.angle(np.mean(mixed)))
                dc_carrier_phase_rad = float(np.angle(np.mean(iq_tick)))
        
        # Uncertainty scales with marker duration (longer = more precise)
        if snr_db > 20:
            uncertainty_ms = 0.02
        elif snr_db > 10:
            uncertainty_ms = 0.1
        elif snr_db > 6:
            uncertainty_ms = 0.3
        else:
            uncertainty_ms = 1.0
        
        # timing_offset_ms: offset from expected position (sec0) in ms.
        # This is now a proper arrival offset, not a buffer-relative position.
        return TickDetectionResult(
            window_start_second=0,
            window_end_second=1,
            timing_offset_ms=offset_ms,
            timing_uncertainty_ms=uncertainty_ms,
            snr_db=snr_db,
            correlation_peak=min(1.0, peak_val),
            phase_rad=0.0,
            carrier_phase_rad=carrier_phase_rad,
            dc_carrier_phase_rad=dc_carrier_phase_rad,
            coherence_quality=min(1.0, snr_db / 20.0),
            valid_ticks=1,
            station=self.station,
        )
    
    def process_minute(
        self,
        iq_samples: np.ndarray,
        minute_number: int = 0,
        min_snr_db: float = 8.0,
        buffer_timing=None,
        minute_boundary: int = 0
    ) -> MinuteTickAnalysis:
        """
        Process a full minute of IQ samples.
        
        Two-tier detection:
        1. Minute marker at second 0 (primary timing, 800ms, high SNR)
        2. Per-second ticks in overlapping windows (phase extraction, timing augmentation)
        
        The IQ is bandpass-filtered around the station tone frequency to reject
        continuous 100 Hz BCD, 440/500/600 Hz audio tones before correlation.
        
        Args:
            iq_samples: Complex IQ samples (60 seconds at sample_rate)
            minute_number: Minute within hour (for BPM UT1/UTC)
            min_snr_db: Minimum SNR to accept a detection (default 8.0 dB)
            buffer_timing: BufferTiming object mapping sample indices to UTC.
                          Required for proper D_clock computation. Without it,
                          d_clock_ms will be None.
            minute_boundary: Unix timestamp of the minute boundary (integer seconds).
                            Required for D_clock computation.
            
        Returns:
            MinuteTickAnalysis with all window results
        """
        expected_samples = 60 * self.sample_rate
        if len(iq_samples) < expected_samples:
            logger.warning(f"Incomplete minute: {len(iq_samples)} < {expected_samples} samples")
        
        # Bandpass filter the full minute once (avoids per-window re-filtering)
        iq_filtered = self._bandpass_iq(iq_samples)
        
        window_results: List[TickDetectionResult] = []
        
        # === Tier 1: Minute marker at second 0 (primary timing) ===
        marker_result = self._detect_minute_marker(
            iq_filtered, iq_samples,
            buffer_timing=buffer_timing,
            minute_boundary=minute_boundary
        )
        if marker_result is not None and marker_result.snr_db >= min_snr_db:
            window_results.append(marker_result)
            logger.info(f"{self.station.value} minute marker DETECTED: "
                       f"offset={marker_result.timing_offset_ms:+.3f}ms, "
                       f"SNR={marker_result.snr_db:.1f}dB, "
                       f"uncertainty={marker_result.timing_uncertainty_ms:.3f}ms")
        elif marker_result is not None:
            logger.info(f"{self.station.value} minute marker below SNR gate: "
                       f"SNR={marker_result.snr_db:.1f}dB < {min_snr_db:.1f}dB")
        
        # === Tier 2: Per-second ticks in overlapping windows ===
        step = self.overlap_seconds
        
        for start_sec in range(1, 60 - self.window_seconds + 1, step):
            end_sec = start_sec + self.window_seconds
            
            # Extract window samples from both filtered and unfiltered
            start_sample = start_sec * self.sample_rate
            end_sample = end_sec * self.sample_rate
            
            if end_sample > len(iq_samples):
                break
            
            window_filtered = iq_filtered[start_sample:end_sample]
            window_unfiltered = iq_samples[start_sample:end_sample]
            
            result = self.process_window(
                iq_samples=window_unfiltered,
                start_second=start_sec,
                end_second=end_sec,
                minute=minute_number,
                iq_filtered=window_filtered,
                iq_unfiltered=window_unfiltered,
                buffer_timing=buffer_timing,
                minute_boundary=minute_boundary,
            )
            
            if result is not None and result.snr_db >= min_snr_db:
                window_results.append(result)
                logger.info(f"{self.station.value} tick window {start_sec}-{end_sec}: DETECTED, "
                           f"SNR={result.snr_db:.1f}dB, offset={result.timing_offset_ms:+.3f}ms")
            elif result is not None:
                logger.debug(f"{self.station.value} tick window {start_sec}-{end_sec}: below SNR gate "
                            f"({result.snr_db:.1f}dB < {min_snr_db:.1f}dB)")
            else:
                logger.debug(f"{self.station.value} tick window {start_sec}-{end_sec}: no result")
        
        # === Separate marker and tick results ===
        marker_ok = (marker_result is not None and marker_result.snr_db >= min_snr_db)
        
        # Tick-only results (exclude minute marker)
        tick_results = [r for r in window_results if r.window_start_second > 0]
        
        # Per-second tick aggregate
        tick_mean = 0.0
        tick_std = 0.0
        tick_valid = len(tick_results)
        if tick_results:
            tick_offsets = [r.timing_offset_ms for r in tick_results]
            tick_mean = float(np.mean(tick_offsets))
            tick_std = float(np.std(tick_offsets))
        
        # Estimate linear drift from tick windows (need temporal spread)
        drift_rate = None
        if len(tick_results) >= 5:
            times = [(r.window_start_second + r.window_end_second) / 2
                    for r in tick_results]
            tick_offsets_list = [r.timing_offset_ms for r in tick_results]
            if np.std(times) > 0:
                slope, _ = np.polyfit(times, tick_offsets_list, 1)
                drift_rate = float(slope)  # ms per second
        
        # === Primary timing: marker when available, tick median as fallback ===
        if marker_ok:
            mean_offset = marker_result.timing_offset_ms
            std_offset = marker_result.timing_uncertainty_ms
            mean_snr = marker_result.snr_db
        elif tick_results:
            # Fallback: use median of tick windows (more robust than mean)
            tick_offsets_arr = np.array([r.timing_offset_ms for r in tick_results])
            mean_offset = float(np.median(tick_offsets_arr))
            std_offset = tick_std
            mean_snr = float(np.mean([r.snr_db for r in tick_results]))
        else:
            mean_offset = 0.0
            std_offset = 0.0
            mean_snr = -100.0
        
        # Overall confidence
        if window_results:
            all_snrs = [r.snr_db for r in window_results]
            snr_factor = min(1.0, float(np.mean(all_snrs)) / 20.0)
            coverage = len(window_results) / 56.0
            if marker_ok:
                # High confidence when marker detected
                consistency = 1.0 / (1.0 + marker_result.timing_uncertainty_ms)
            else:
                consistency = 1.0 / (1.0 + tick_std)
            overall_confidence = consistency * snr_factor * coverage
        else:
            overall_confidence = 0.0
        
        # +1 for minute marker window
        total_windows = 1 + (60 - self.window_seconds) // self.overlap_seconds
        
        # === Compute D_clock ===
        # With buffer_timing, both the minute marker and per-second tick
        # timing_offset_ms are true ToA residuals from the UTC second
        # boundary (computed via buffer_timing.utc_to_sample in both
        # _detect_minute_marker and process_window).  D_clock is simply
        # the primary timing estimate — no conversion needed.
        #
        # Without buffer_timing, timing_offset_ms is buffer-relative and
        # D_clock is meaningless, so we leave it None.
        d_clock_val = None
        d_clock_unc = None
        
        if buffer_timing is not None and minute_boundary > 0:
            # timing_offset_ms is already a true ToA residual
            d_clock_val = mean_offset
            d_clock_unc = std_offset
        
        # === Derive Doppler from carrier phase slope ===
        # Each per-second tick window has a carrier_phase_rad measured by
        # mixing the IQ signal at the tone frequency.  The phase progression
        # across the minute reflects the Doppler shift:
        #   Δφ/Δt = 2π × f_doppler
        #   f_doppler = (1/2π) × dφ/dt
        #
        # We fit a linear slope to the unwrapped carrier phase vs time
        # across all detected tick windows.
        doppler_val = None
        doppler_unc = None
        
        if len(tick_results) >= 5:
            # Use the center time of each window (seconds from minute start)
            phase_times = []
            phase_values = []
            for r in tick_results:
                t_center = (r.window_start_second + r.window_end_second) / 2.0
                phase_times.append(t_center)
                phase_values.append(r.carrier_phase_rad)
            
            phase_times = np.array(phase_times)
            phase_values = np.array(phase_values)
            
            # Unwrap phase to remove 2π discontinuities
            phase_unwrapped = np.unwrap(phase_values)
            
            # Linear fit: phase = slope * t + intercept
            # slope = dφ/dt in rad/s → Doppler = slope / (2π) Hz
            if np.std(phase_times) > 0:
                try:
                    coeffs = np.polyfit(phase_times, phase_unwrapped, 1)
                    slope_rad_per_sec = coeffs[0]
                    doppler_val = float(slope_rad_per_sec / (2.0 * np.pi))
                    
                    # Residuals for uncertainty estimate
                    fitted = np.polyval(coeffs, phase_times)
                    residuals = phase_unwrapped - fitted
                    phase_std = float(np.std(residuals))
                    # Uncertainty in slope → uncertainty in Doppler
                    t_range = float(np.max(phase_times) - np.min(phase_times))
                    if t_range > 0:
                        slope_unc = phase_std / (t_range * np.sqrt(len(phase_times) / 12.0))
                        doppler_unc = float(slope_unc / (2.0 * np.pi))
                    
                    # Sanity check: HF Doppler should be < 5 Hz typically
                    # (ionospheric motion ~50 m/s at 10 MHz → ~1.7 Hz)
                    if abs(doppler_val) > 10.0:
                        logger.warning(f"{self.station.value}: Doppler {doppler_val:.3f} Hz "
                                      f"exceeds 10 Hz sanity limit, discarding")
                        doppler_val = None
                        doppler_unc = None
                    else:
                        logger.info(f"{self.station.value}: Doppler from phase slope: "
                                   f"{doppler_val:+.4f} Hz "
                                   f"(±{doppler_unc:.4f} Hz, {len(tick_results)} windows)")
                except (np.linalg.LinAlgError, ValueError) as e:
                    logger.debug(f"{self.station.value}: Doppler fit failed: {e}")
        
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
            marker_detected=marker_ok,
            marker_timing_offset_ms=marker_result.timing_offset_ms if marker_ok else 0.0,
            marker_snr_db=marker_result.snr_db if marker_ok else -100.0,
            marker_uncertainty_ms=marker_result.timing_uncertainty_ms if marker_ok else 999.0,
            tick_mean_offset_ms=tick_mean,
            tick_std_offset_ms=tick_std,
            tick_valid_windows=tick_valid,
            d_clock_ms=d_clock_val,
            d_clock_uncertainty_ms=d_clock_unc,
            doppler_hz=doppler_val,
            doppler_uncertainty_hz=doppler_unc,
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
