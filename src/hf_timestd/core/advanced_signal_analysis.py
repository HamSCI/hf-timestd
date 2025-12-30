#!/usr/bin/env python3
"""
Advanced Signal Analysis for Phase 2 Analytics

================================================================================
ENHANCEMENTS IMPLEMENTED (Issues 5.1-5.4 from PHASE2_CRITIQUE.md)
================================================================================

ISSUE 5.1: No Use of Phase Information
--------------------------------------
PROBLEM: The matched filter used phase-invariant envelope detection:
    correlation = √(corr_sin² + corr_cos²)
This discards phase information that contains:
- Sub-sample timing (finer than quadratic interpolation)
- Doppler shift (phase rotation rate)
- Multipath (phase distortion)

SOLUTION: Complex correlation preserves phase:
    correlation_complex = corr_cos + j·corr_sin
    magnitude = |correlation_complex|
    phase = arg(correlation_complex)

Phase unwrapping across the correlation peak provides:
- Sub-sample timing via phase slope
- Doppler estimate from phase rotation rate
- Multipath detection from phase discontinuities

ISSUE 5.2: No Multipath Detection
---------------------------------
PROBLEM: HF often has multipath (e.g., 1F + 2F arriving together).
The code measured `delay_spread_ms` but didn't use it to correct timing
or flag unreliable measurements.

SOLUTION: Comprehensive multipath detection:
1. Correlation peak width analysis (broadened = multipath)
2. Amplitude fading analysis across tone duration
3. Phase stability metric (multipath causes phase jumps)
4. Secondary peak detection in correlation

Multipath quality metric allows downstream filtering of unreliable measurements.

ISSUE 5.3: No Cross-Correlation Between WWV and WWVH
----------------------------------------------------
PROBLEM: WWV and WWVH detected separately, missing opportunity for:
- Common-mode noise cancellation (receiver oscillator jitter)
- Higher-precision differential delay
- Confirmation both stations present

SOLUTION: Direct cross-correlation of 1000 Hz and 1200 Hz tones:
    xcorr(tone_1000, tone_1200)
This provides:
- Differential delay measurement
- Coherence metric (both present = high coherence)
- Jitter-cancelled timing

ISSUE 5.4: No Exploitation of CHU FSK Time Code
-----------------------------------------------
PROBLEM: CHU transmits FSK time code with:
- Verified UTC time (not just relative timing)
- DUT1 correction (UT1-UTC)
- Leap second announcements

SOLUTION: CHU FSK decoder:
- 300 baud FSK demodulation (2025/2225 Hz)
- BCD time code extraction
- DUT1 and leap second parsing

================================================================================
THEORY: COMPLEX CORRELATION
================================================================================

Standard matched filter correlation:
    corr_sin(τ) = Σ s(t) × sin(2πft) × template(t-τ)
    corr_cos(τ) = Σ s(t) × cos(2πft) × template(t-τ)
    
    envelope(τ) = √(corr_sin² + corr_cos²)  ← phase discarded!

Complex correlation preserves phase:
    corr_complex(τ) = corr_cos(τ) + j·corr_sin(τ)
    
    magnitude(τ) = |corr_complex(τ)|
    phase(τ) = arg(corr_complex(τ))

PHASE INFORMATION:
    - At correlation peak: phase gives sub-sample offset
    - Phase slope: dφ/dt = 2π·Doppler_shift
    - Phase jumps: indicate multipath or interference

SUB-SAMPLE TIMING:
    If peak is at sample n with phase φ:
    
    sub_sample_offset = -φ / (2π × tone_frequency × sample_period)
    
    This provides ~10× finer resolution than sample-based timing.

================================================================================
USAGE
================================================================================

    # Complex correlation with phase
    analyzer = AdvancedSignalAnalyzer(sample_rate=20000)
    
    result = analyzer.complex_correlation(
        iq_samples, 
        tone_frequency=1000,
        template_duration_ms=100
    )
    print(f"Peak at {result.peak_sample}, phase={result.peak_phase_rad:.3f}")
    print(f"Sub-sample offset: {result.sub_sample_offset:.4f} samples")
    print(f"Doppler estimate: {result.doppler_hz:.2f} Hz")
    
    # Multipath detection
    multipath = analyzer.detect_multipath(iq_samples, tone_frequency=1000)
    print(f"Multipath detected: {multipath.is_multipath}")
    print(f"Delay spread: {multipath.delay_spread_ms:.2f} ms")
    print(f"Quality: {multipath.quality_metric:.3f}")
    
    # WWV/WWVH cross-correlation
    diff_result = analyzer.cross_correlate_stations(iq_samples)
    print(f"Differential delay: {diff_result.differential_delay_ms:.3f} ms")
    print(f"Coherence: {diff_result.coherence:.3f}")
    
    # CHU FSK decoding
    chu_result = analyzer.decode_chu_fsk(iq_samples)
    if chu_result.valid:
        print(f"CHU Time: {chu_result.utc_time}")
        print(f"DUT1: {chu_result.dut1_ms:.1f} ms")

================================================================================
REVISION HISTORY
================================================================================
2025-12-07: Initial implementation addressing Issues 5.1, 5.2, 5.3, 5.4
"""

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple, Any

import numpy as np
from scipy import signal as scipy_signal
from scipy.fft import fft, ifft, fftfreq

logger = logging.getLogger(__name__)


# =============================================================================
# CONSTANTS
# =============================================================================

# WWV/WWVH tone frequencies
WWV_TONE_HZ = 1000
WWVH_TONE_HZ = 1200

# CHU FSK parameters
CHU_FSK_MARK_HZ = 2225   # Mark frequency (binary 1)
CHU_FSK_SPACE_HZ = 2025  # Space frequency (binary 0)
CHU_FSK_BAUD = 300       # 300 baud
CHU_FSK_CARRIER_HZ = 1000  # CHU carrier (also used for timing)

# Multipath detection thresholds
MULTIPATH_PEAK_WIDTH_THRESHOLD_MS = 2.0    # Peak wider than this = multipath
MULTIPATH_SECONDARY_PEAK_THRESHOLD = 0.3   # Secondary peak > 30% of primary
MULTIPATH_PHASE_STABILITY_THRESHOLD = 0.5  # Phase std > 0.5 rad = unstable


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class ComplexCorrelationResult:
    """
    Result of complex correlation preserving phase information.
    
    Issue 5.1 Fix: Phase information is now available for:
    - Sub-sample timing refinement
    - Doppler estimation
    - Multipath detection
    """
    # Peak location
    peak_sample: int
    peak_magnitude: float
    peak_phase_rad: float
    
    # Sub-sample refinement from phase
    sub_sample_offset: float      # Fractional sample offset from phase
    refined_sample: float         # peak_sample + sub_sample_offset
    
    # Doppler estimation from phase slope
    doppler_hz: float
    doppler_confidence: float     # 0-1, higher = more reliable
    
    # Full correlation data (optional)
    correlation_magnitude: Optional[np.ndarray] = None
    correlation_phase: Optional[np.ndarray] = None
    
    # SNR estimate
    snr_db: float = 0.0


@dataclass
class MultipathResult:
    """
    Result of multipath detection analysis.
    
    Issue 5.2 Fix: Multipath is now detected and quantified.
    """
    # Detection result
    is_multipath: bool
    confidence: float             # 0-1, higher = more confident multipath
    
    # Multipath characteristics
    delay_spread_ms: float        # Spread of correlation peak
    secondary_peaks: List[Tuple[int, float]]  # (sample, magnitude) of secondary peaks
    peak_width_ms: float          # Width of main peak at -3dB
    
    # Phase stability
    phase_std_rad: float          # Phase standard deviation
    phase_rate_stability: float   # Consistency of phase slope
    
    # Amplitude fading
    amplitude_fading_db: float    # Peak-to-trough variation
    fading_rate_hz: float         # Fading frequency (if periodic)
    
    # Quality metric (higher = cleaner signal, lower = more multipath)
    quality_metric: float         # 0-1 composite quality score
    
    # Timing correction (if multipath detected)
    timing_correction_ms: float   # Suggested correction
    timing_uncertainty_ms: float  # Uncertainty due to multipath


@dataclass
class CrossCorrelationResult:
    """
    Result of WWV/WWVH cross-correlation.
    
    Issue 5.3 Fix: Direct cross-correlation provides differential delay.
    """
    # Differential timing
    differential_delay_ms: float   # WWV - WWVH arrival time
    differential_delay_samples: int
    
    # Coherence (both stations present)
    coherence: float              # 0-1, higher = both present
    wwv_power_db: float
    wwvh_power_db: float
    
    # Cross-correlation peak
    xcorr_peak_magnitude: float
    xcorr_snr_db: float
    
    # Quality assessment
    is_reliable: bool             # True if both stations clearly present
    dominant_station: str         # 'WWV', 'WWVH', 'BALANCED', or 'NONE'


@dataclass
class CHUFSKResult:
    """
    Result of CHU FSK time code decoding.
    
    Issue 5.4 Fix: CHU FSK provides verified UTC time.
    """
    # Validity
    valid: bool
    decode_confidence: float      # 0-1
    
    # Decoded time
    utc_time: Optional[datetime] = None
    year: Optional[int] = None
    day_of_year: Optional[int] = None
    hour: Optional[int] = None
    minute: Optional[int] = None
    second: Optional[int] = None
    
    # Time corrections
    dut1_ms: float = 0.0          # UT1 - UTC in milliseconds
    leap_second_pending: bool = False
    
    # Raw data
    raw_bits: Optional[List[int]] = None
    bit_error_rate: float = 0.0


# =============================================================================
# ADVANCED SIGNAL ANALYZER
# =============================================================================

class AdvancedSignalAnalyzer:
    """
    Advanced signal analysis for HF time signal reception.
    
    Implements enhancements from PHASE2_CRITIQUE.md Section 5:
    - 5.1: Complex correlation with phase information
    - 5.2: Multipath detection
    - 5.3: WWV/WWVH cross-correlation
    - 5.4: CHU FSK decoding
    """
    
    def __init__(
        self,
        sample_rate: int = 20000,
        default_template_ms: float = 100.0
    ):
        """
        Initialize the analyzer.
        
        Args:
            sample_rate: Sample rate in Hz
            default_template_ms: Default template duration for correlation
        """
        self.sample_rate = sample_rate
        self.default_template_ms = default_template_ms
        
        # Precompute templates
        self._templates: Dict[int, np.ndarray] = {}
        
        logger.info(f"Advanced signal analyzer initialized: {sample_rate} Hz")
    
    # =========================================================================
    # 5.1: COMPLEX CORRELATION WITH PHASE
    # =========================================================================
    
    def complex_correlation(
        self,
        samples: np.ndarray,
        tone_frequency: float,
        template_duration_ms: Optional[float] = None,
        return_full_correlation: bool = False
    ) -> ComplexCorrelationResult:
        """
        Perform complex correlation preserving phase information.
        
        Issue 5.1 Fix: Instead of discarding phase with envelope detection,
        we preserve the complex correlation to extract:
        - Sub-sample timing from phase at peak
        - Doppler shift from phase slope
        - Multipath indicators from phase stability
        
        Args:
            samples: Input samples (real or complex)
            tone_frequency: Expected tone frequency in Hz
            template_duration_ms: Template length (default: 100ms)
            return_full_correlation: Return full correlation arrays
            
        Returns:
            ComplexCorrelationResult with phase information
        """
        if template_duration_ms is None:
            template_duration_ms = self.default_template_ms
        
        template_samples = int(template_duration_ms * self.sample_rate / 1000)
        
        # Generate complex template: cos + j*sin
        t = np.arange(template_samples) / self.sample_rate
        template_complex = np.exp(2j * np.pi * tone_frequency * t)
        
        # Ensure input is real (take real part if complex)
        if np.iscomplexobj(samples):
            samples_real = np.real(samples)
        else:
            samples_real = samples
        
        # Complex correlation via FFT (efficient for long signals)
        n_fft = len(samples_real) + template_samples - 1
        n_fft = int(2 ** np.ceil(np.log2(n_fft)))  # Power of 2 for efficiency
        
        # Correlate with complex template
        samples_fft = fft(samples_real, n_fft)
        template_fft = fft(template_complex, n_fft)
        correlation_fft = samples_fft * np.conj(template_fft)
        correlation_complex = ifft(correlation_fft)[:len(samples_real)]
        
        # Extract magnitude and phase
        correlation_magnitude = np.abs(correlation_complex)
        correlation_phase = np.angle(correlation_complex)
        
        # Find peak
        peak_sample = int(np.argmax(correlation_magnitude))
        peak_magnitude = float(correlation_magnitude[peak_sample])
        peak_phase = float(correlation_phase[peak_sample])
        
        # Sub-sample refinement from phase
        # Phase at peak tells us fractional sample offset
        # φ = 2π × f × Δt → Δt = φ / (2π × f)
        if abs(tone_frequency) > 1e-6:
            sub_sample_offset = -peak_phase / (2 * np.pi * tone_frequency / self.sample_rate)
            # Wrap to [-0.5, 0.5] samples
            sub_sample_offset = ((sub_sample_offset + 0.5) % 1.0) - 0.5
            refined_sample = peak_sample + sub_sample_offset
        else:
            # Cannot use phase for timing refinement at DC (period is infinite)
            sub_sample_offset = 0.0
            refined_sample = float(peak_sample)
        
        # Doppler estimation from phase slope around peak
        doppler_hz, doppler_confidence = self._estimate_doppler_from_phase(
            correlation_phase, peak_sample, tone_frequency
        )
        
        # SNR estimate
        noise_region = np.concatenate([
            correlation_magnitude[:max(0, peak_sample - 100)],
            correlation_magnitude[min(len(correlation_magnitude), peak_sample + 100):]
        ])
        if len(noise_region) > 0:
            noise_power = np.mean(noise_region ** 2)
            signal_power = peak_magnitude ** 2
            snr_db = 10 * np.log10(signal_power / noise_power) if noise_power > 0 else 40.0
        else:
            snr_db = 20.0
        
        result = ComplexCorrelationResult(
            peak_sample=peak_sample,
            peak_magnitude=peak_magnitude,
            peak_phase_rad=peak_phase,
            sub_sample_offset=sub_sample_offset,
            refined_sample=peak_sample + sub_sample_offset,
            doppler_hz=doppler_hz,
            doppler_confidence=doppler_confidence,
            snr_db=snr_db
        )
        
        if return_full_correlation:
            result.correlation_magnitude = correlation_magnitude
            result.correlation_phase = correlation_phase
        
        return result
    
    def _estimate_doppler_from_phase(
        self,
        phase: np.ndarray,
        peak_sample: int,
        tone_frequency: float,
        window_samples: int = 50
    ) -> Tuple[float, float]:
        """
        Estimate Doppler shift from phase rotation rate.
        
        Doppler shift causes continuous phase rotation:
            φ(t) = 2π × (f + f_doppler) × t
            dφ/dt = 2π × (f + f_doppler)
        
        By measuring phase slope, we get Doppler offset.
        """
        # Extract phase around peak
        start = max(0, peak_sample - window_samples)
        end = min(len(phase), peak_sample + window_samples)
        
        if end - start < 10:
            return 0.0, 0.0
        
        phase_window = phase[start:end]
        
        # Unwrap phase
        phase_unwrapped = np.unwrap(phase_window)
        
        # Linear fit to get slope
        t = np.arange(len(phase_unwrapped))
        try:
            coeffs = np.polyfit(t, phase_unwrapped, 1)
            phase_slope = coeffs[0]  # rad/sample
            
            # Convert to Hz: f_doppler = (dφ/dt) / (2π) - f_carrier
            # But we want just the Doppler offset
            phase_rate_hz = phase_slope * self.sample_rate / (2 * np.pi)
            doppler_hz = phase_rate_hz - tone_frequency
            
            # Confidence from fit residuals
            fit = np.polyval(coeffs, t)
            residuals = phase_unwrapped - fit
            confidence = 1.0 / (1.0 + np.std(residuals))
            
            return float(doppler_hz), float(confidence)
            
        except (np.linalg.LinAlgError, ValueError):
            return 0.0, 0.0
    
    # =========================================================================
    # 5.2: MULTIPATH DETECTION
    # =========================================================================
    
    def detect_multipath(
        self,
        samples: np.ndarray,
        tone_frequency: float,
        template_duration_ms: float = 100.0
    ) -> MultipathResult:
        """
        Detect and characterize multipath propagation.
        
        Issue 5.2 Fix: Multipath detection from:
        1. Correlation peak width (broadened = multipath)
        2. Secondary peaks in correlation
        3. Phase stability analysis
        4. Amplitude fading across tone
        
        Args:
            samples: Input samples
            tone_frequency: Tone frequency in Hz
            template_duration_ms: Template duration
            
        Returns:
            MultipathResult with multipath characteristics
        """
        # Get complex correlation
        corr_result = self.complex_correlation(
            samples, tone_frequency, template_duration_ms,
            return_full_correlation=True
        )
        
        magnitude = corr_result.correlation_magnitude
        phase = corr_result.correlation_phase
        peak_sample = corr_result.peak_sample
        peak_mag = corr_result.peak_magnitude
        
        # 1. Peak width analysis
        peak_width_samples, peak_width_ms = self._measure_peak_width(
            magnitude, peak_sample
        )
        
        # Expected width for clean signal (approximately template length)
        expected_width_ms = template_duration_ms * 0.1  # 10% of template
        width_ratio = peak_width_ms / expected_width_ms if expected_width_ms > 0 else 1.0
        
        # 2. Secondary peak detection
        secondary_peaks = self._find_secondary_peaks(
            magnitude, peak_sample, peak_mag
        )
        
        # 3. Phase stability analysis
        phase_std, phase_rate_stability = self._analyze_phase_stability(
            phase, peak_sample
        )
        
        # 4. Amplitude fading analysis
        fading_db, fading_rate = self._analyze_amplitude_fading(
            samples, tone_frequency
        )
        
        # Compute delay spread from peak width and secondary peaks
        delay_spread_ms = peak_width_ms
        if secondary_peaks:
            max_secondary_delay = max(abs(s[0] - peak_sample) for s in secondary_peaks)
            delay_spread_ms = max(delay_spread_ms, max_secondary_delay * 1000 / self.sample_rate)
        
        # Determine if multipath is present
        is_multipath = (
            peak_width_ms > MULTIPATH_PEAK_WIDTH_THRESHOLD_MS or
            len(secondary_peaks) > 0 or
            phase_std > MULTIPATH_PHASE_STABILITY_THRESHOLD or
            fading_db > 6.0  # More than 6 dB fading
        )
        
        # Confidence in multipath detection
        multipath_indicators = [
            width_ratio > 1.5,
            len(secondary_peaks) > 0,
            phase_std > MULTIPATH_PHASE_STABILITY_THRESHOLD,
            fading_db > 3.0
        ]
        confidence = sum(multipath_indicators) / len(multipath_indicators)
        
        # Quality metric (inverse of multipath severity)
        quality_metric = 1.0 - min(1.0, (
            0.3 * min(1.0, width_ratio / 3.0) +
            0.3 * min(1.0, len(secondary_peaks) / 3) +
            0.2 * min(1.0, phase_std / 1.0) +
            0.2 * min(1.0, fading_db / 10.0)
        ))
        
        # Timing correction estimate (center of mass of correlation peak)
        timing_correction_ms = 0.0
        timing_uncertainty_ms = delay_spread_ms / 2
        
        if is_multipath and magnitude is not None:
            # Use center of mass for timing correction
            window = 50  # samples
            start = max(0, peak_sample - window)
            end = min(len(magnitude), peak_sample + window)
            local_mag = magnitude[start:end]
            indices = np.arange(start, end)
            if np.sum(local_mag) > 0:
                com = np.sum(indices * local_mag) / np.sum(local_mag)
                timing_correction_ms = (com - peak_sample) * 1000 / self.sample_rate
        
        return MultipathResult(
            is_multipath=is_multipath,
            confidence=confidence,
            delay_spread_ms=delay_spread_ms,
            secondary_peaks=secondary_peaks,
            peak_width_ms=peak_width_ms,
            phase_std_rad=phase_std,
            phase_rate_stability=phase_rate_stability,
            amplitude_fading_db=fading_db,
            fading_rate_hz=fading_rate,
            quality_metric=quality_metric,
            timing_correction_ms=timing_correction_ms,
            timing_uncertainty_ms=timing_uncertainty_ms
        )
    
    def _measure_peak_width(
        self,
        magnitude: np.ndarray,
        peak_sample: int
    ) -> Tuple[int, float]:
        """Measure correlation peak width at -3dB."""
        if magnitude is None:
            return 0, 0.0
        
        peak_mag = magnitude[peak_sample]
        threshold = peak_mag * 0.707  # -3 dB
        
        # Find where magnitude drops below threshold
        left = peak_sample
        while left > 0 and magnitude[left] > threshold:
            left -= 1
        
        right = peak_sample
        while right < len(magnitude) - 1 and magnitude[right] > threshold:
            right += 1
        
        width_samples = right - left
        width_ms = width_samples * 1000 / self.sample_rate
        
        return width_samples, width_ms
    
    def _find_secondary_peaks(
        self,
        magnitude: np.ndarray,
        peak_sample: int,
        peak_mag: float,
        min_separation_samples: int = 20
    ) -> List[Tuple[int, float]]:
        """Find secondary peaks in correlation that might indicate multipath."""
        if magnitude is None:
            return []
        
        threshold = peak_mag * MULTIPATH_SECONDARY_PEAK_THRESHOLD
        
        secondary_peaks = []
        
        # Find local maxima
        for i in range(1, len(magnitude) - 1):
            if abs(i - peak_sample) < min_separation_samples:
                continue
            
            if (magnitude[i] > magnitude[i-1] and 
                magnitude[i] > magnitude[i+1] and
                magnitude[i] > threshold):
                secondary_peaks.append((i, float(magnitude[i])))
        
        # Sort by magnitude (strongest first)
        secondary_peaks.sort(key=lambda x: x[1], reverse=True)
        
        return secondary_peaks[:5]  # Return top 5
    
    def _analyze_phase_stability(
        self,
        phase: np.ndarray,
        peak_sample: int,
        window_samples: int = 100
    ) -> Tuple[float, float]:
        """Analyze phase stability around correlation peak."""
        if phase is None:
            return 0.0, 1.0
        
        start = max(0, peak_sample - window_samples)
        end = min(len(phase), peak_sample + window_samples)
        
        if end - start < 10:
            return 0.0, 1.0
        
        phase_window = phase[start:end]
        phase_unwrapped = np.unwrap(phase_window)
        
        # Remove linear trend (Doppler)
        t = np.arange(len(phase_unwrapped))
        try:
            coeffs = np.polyfit(t, phase_unwrapped, 1)
            phase_detrended = phase_unwrapped - np.polyval(coeffs, t)
            
            phase_std = float(np.std(phase_detrended))
            
            # Rate stability: how consistent is the phase slope
            # Compute local slopes
            local_slopes = np.diff(phase_unwrapped)
            slope_std = np.std(local_slopes)
            rate_stability = 1.0 / (1.0 + slope_std)
            
            return phase_std, float(rate_stability)
            
        except (np.linalg.LinAlgError, ValueError):
            return 0.0, 1.0
    
    def _analyze_amplitude_fading(
        self,
        samples: np.ndarray,
        tone_frequency: float,
        window_ms: float = 50.0
    ) -> Tuple[float, float]:
        """Analyze amplitude fading across the signal."""
        window_samples = int(window_ms * self.sample_rate / 1000)
        
        if len(samples) < window_samples * 3:
            return 0.0, 0.0
        
        # Compute envelope in sliding windows
        n_windows = len(samples) // window_samples
        envelopes = []
        
        for i in range(n_windows):
            start = i * window_samples
            end = start + window_samples
            window = samples[start:end]
            
            # Compute envelope via Hilbert transform
            analytic = scipy_signal.hilbert(window)
            envelope = np.mean(np.abs(analytic))
            envelopes.append(envelope)
        
        envelopes = np.array(envelopes)
        
        if len(envelopes) < 2 or np.max(envelopes) == 0:
            return 0.0, 0.0
        
        # Fading depth (peak-to-trough in dB)
        fading_db = 20 * np.log10(np.max(envelopes) / max(np.min(envelopes), 1e-10))
        
        # Fading rate (from FFT of envelope)
        if len(envelopes) > 4:
            envelope_fft = np.abs(fft(envelopes - np.mean(envelopes)))
            freqs = fftfreq(len(envelopes), window_ms / 1000)
            
            # Find dominant fading frequency (excluding DC)
            positive_freqs = freqs[:len(freqs)//2]
            positive_fft = envelope_fft[:len(freqs)//2]
            if len(positive_fft) > 1:
                peak_idx = np.argmax(positive_fft[1:]) + 1
                fading_rate = abs(positive_freqs[peak_idx])
            else:
                fading_rate = 0.0
        else:
            fading_rate = 0.0
        
        return float(fading_db), float(fading_rate)
    
    # =========================================================================
    # 5.3: WWV/WWVH CROSS-CORRELATION
    # =========================================================================
    
    def cross_correlate_stations(
        self,
        samples: np.ndarray,
        wwv_freq: float = WWV_TONE_HZ,
        wwvh_freq: float = WWVH_TONE_HZ,
        filter_bandwidth: float = 50.0
    ) -> CrossCorrelationResult:
        """
        Cross-correlate WWV and WWVH tones for differential timing.
        
        Issue 5.3 Fix: Direct cross-correlation of 1000 Hz and 1200 Hz tones
        cancels common-mode noise and provides precise differential delay.
        
        Args:
            samples: Input samples
            wwv_freq: WWV tone frequency (default 1000 Hz)
            wwvh_freq: WWVH tone frequency (default 1200 Hz)
            filter_bandwidth: Bandpass filter bandwidth
            
        Returns:
            CrossCorrelationResult with differential timing
        """
        # Bandpass filter to extract each tone
        wwv_tone = self._bandpass_filter(samples, wwv_freq, filter_bandwidth)
        wwvh_tone = self._bandpass_filter(samples, wwvh_freq, filter_bandwidth)
        
        # Compute analytic signals (complex envelope)
        wwv_analytic = scipy_signal.hilbert(wwv_tone)
        wwvh_analytic = scipy_signal.hilbert(wwvh_tone)
        
        # Power estimates
        wwv_power = np.mean(np.abs(wwv_analytic) ** 2)
        wwvh_power = np.mean(np.abs(wwvh_analytic) ** 2)
        
        noise_floor = 1e-10  # Prevent log(0)
        wwv_power_db = 10 * np.log10(max(wwv_power, noise_floor))
        wwvh_power_db = 10 * np.log10(max(wwvh_power, noise_floor))
        
        # Cross-correlation of envelopes
        wwv_env = np.abs(wwv_analytic)
        wwvh_env = np.abs(wwvh_analytic)
        
        # Normalize
        wwv_norm = (wwv_env - np.mean(wwv_env)) / max(np.std(wwv_env), 1e-10)
        wwvh_norm = (wwvh_env - np.mean(wwvh_env)) / max(np.std(wwvh_env), 1e-10)
        
        # Cross-correlation
        xcorr = np.correlate(wwv_norm, wwvh_norm, mode='full')
        xcorr_center = len(wwv_norm) - 1
        
        # Find peak
        peak_idx = np.argmax(np.abs(xcorr))
        peak_magnitude = float(np.abs(xcorr[peak_idx]))
        
        # Differential delay
        differential_delay_samples = peak_idx - xcorr_center
        differential_delay_ms = differential_delay_samples * 1000 / self.sample_rate
        
        # Coherence metric (normalized peak)
        xcorr_snr_db = 10 * np.log10(peak_magnitude / max(np.std(xcorr), 1e-10))
        coherence = min(1.0, peak_magnitude / len(wwv_norm))
        
        # Reliability assessment
        power_diff_db = abs(wwv_power_db - wwvh_power_db)
        is_reliable = (
            wwv_power_db > -30 and 
            wwvh_power_db > -30 and 
            coherence > 0.3
        )
        
        # Determine dominant station
        if wwv_power_db < -30 and wwvh_power_db < -30:
            dominant_station = 'NONE'
        elif power_diff_db < 3:
            dominant_station = 'BALANCED'
        elif wwv_power_db > wwvh_power_db:
            dominant_station = 'WWV'
        else:
            dominant_station = 'WWVH'
        
        return CrossCorrelationResult(
            differential_delay_ms=differential_delay_ms,
            differential_delay_samples=differential_delay_samples,
            coherence=coherence,
            wwv_power_db=wwv_power_db,
            wwvh_power_db=wwvh_power_db,
            xcorr_peak_magnitude=peak_magnitude,
            xcorr_snr_db=xcorr_snr_db,
            is_reliable=is_reliable,
            dominant_station=dominant_station
        )
    
    def _bandpass_filter(
        self,
        samples: np.ndarray,
        center_freq: float,
        bandwidth: float
    ) -> np.ndarray:
        """Apply bandpass filter centered on frequency."""
        nyquist = self.sample_rate / 2
        low = (center_freq - bandwidth / 2) / nyquist
        high = (center_freq + bandwidth / 2) / nyquist
        
        # Clamp to valid range
        low = max(0.001, min(0.999, low))
        high = max(0.001, min(0.999, high))
        
        if low >= high:
            return samples
        
        try:
            b, a = scipy_signal.butter(4, [low, high], btype='band')
            return scipy_signal.filtfilt(b, a, samples)
        except ValueError:
            return samples
    
    # =========================================================================
    # 5.4: CHU FSK TIME CODE DECODER
    # =========================================================================
    
    def decode_chu_fsk(
        self,
        samples: np.ndarray,
        expected_second: Optional[int] = None
    ) -> CHUFSKResult:
        """
        Decode CHU FSK time code.
        
        Issue 5.4 Fix: Decode CHU's 300 baud FSK to extract:
        - Verified UTC time
        - DUT1 correction (UT1 - UTC)
        - Leap second announcements
        
        CHU FSK Format (per NRC):
        - 300 baud FSK
        - Mark: 2225 Hz, Space: 2025 Hz
        - 10 characters per second
        - Each character: start bit + 8 data bits + 2 stop bits
        
        Args:
            samples: Input samples (should contain ~1 second of data)
            expected_second: Expected second for validation
            
        Returns:
            CHUFSKResult with decoded time code
        """
        # Demodulate FSK
        bits, bit_confidence = self._demodulate_fsk(samples)
        
        if len(bits) < 80:  # Need at least some bits
            return CHUFSKResult(
                valid=False,
                decode_confidence=0.0,
                bit_error_rate=1.0
            )
        
        # Parse time code from bits
        try:
            time_data = self._parse_chu_time_code(bits)
        except Exception as e:
            logger.debug(f"CHU decode error: {e}")
            return CHUFSKResult(
                valid=False,
                decode_confidence=bit_confidence,
                raw_bits=bits,
                bit_error_rate=1.0 - bit_confidence
            )
        
        # Validate decoded time
        valid = self._validate_chu_time(time_data, expected_second)
        
        # Build result
        result = CHUFSKResult(
            valid=valid,
            decode_confidence=bit_confidence,
            raw_bits=bits,
            bit_error_rate=1.0 - bit_confidence
        )
        
        if time_data:
            result.year = time_data.get('year')
            result.day_of_year = time_data.get('day')
            result.hour = time_data.get('hour')
            result.minute = time_data.get('minute')
            result.second = time_data.get('second')
            result.dut1_ms = time_data.get('dut1', 0.0)
            result.leap_second_pending = time_data.get('leap_pending', False)
            
            # Construct datetime
            try:
                if result.year and result.day_of_year:
                    base = datetime(result.year, 1, 1, tzinfo=timezone.utc)
                    from datetime import timedelta
                    result.utc_time = base + timedelta(
                        days=result.day_of_year - 1,
                        hours=result.hour or 0,
                        minutes=result.minute or 0,
                        seconds=result.second or 0
                    )
            except (ValueError, TypeError):
                pass
        
        return result
    
    def _demodulate_fsk(
        self,
        samples: np.ndarray
    ) -> Tuple[List[int], float]:
        """
        Demodulate FSK signal to bits.
        
        Uses frequency discrimination: compare power at mark vs space frequencies.
        """
        bit_duration = self.sample_rate / CHU_FSK_BAUD
        n_bits = int(len(samples) / bit_duration)
        
        bits = []
        confidences = []
        
        for i in range(n_bits):
            start = int(i * bit_duration)
            end = int(start + bit_duration)
            
            if end > len(samples):
                break
            
            bit_samples = samples[start:end]
            
            # Measure power at mark and space frequencies
            mark_power = self._measure_frequency_power(bit_samples, CHU_FSK_MARK_HZ)
            space_power = self._measure_frequency_power(bit_samples, CHU_FSK_SPACE_HZ)
            
            # Decide bit value
            if mark_power > space_power:
                bits.append(1)
                confidence = mark_power / (mark_power + space_power + 1e-10)
            else:
                bits.append(0)
                confidence = space_power / (mark_power + space_power + 1e-10)
            
            confidences.append(confidence)
        
        avg_confidence = np.mean(confidences) if confidences else 0.0
        
        return bits, float(avg_confidence)
    
    def _measure_frequency_power(
        self,
        samples: np.ndarray,
        frequency: float,
        bandwidth: float = 50.0
    ) -> float:
        """Measure power at a specific frequency using Goertzel algorithm."""
        n = len(samples)
        k = int(0.5 + (n * frequency / self.sample_rate))
        w = 2 * np.pi * k / n
        coeff = 2 * np.cos(w)
        
        s0 = 0.0
        s1 = 0.0
        s2 = 0.0
        
        for sample in samples:
            s0 = sample + coeff * s1 - s2
            s2 = s1
            s1 = s0
        
        power = s1 * s1 + s2 * s2 - coeff * s1 * s2
        return max(0.0, power)
    
    def _parse_chu_time_code(self, bits: List[int]) -> Dict[str, Any]:
        """
        Parse CHU time code from demodulated bits.
        
        CHU transmits 10 BCD characters per second, each 11 bits:
        - 1 start bit (0)
        - 8 data bits (LSB first)
        - 2 stop bits (1)
        
        Format varies by second, but typical structure:
        - Characters 1-2: Identification
        - Characters 3-4: Day of year (hundreds, tens+units)
        - Characters 5-6: Hours
        - Characters 7-8: Minutes
        - Characters 9-10: Seconds
        """
        time_data = {}
        
        # Try to extract characters (skip start, extract 8 data bits, skip 2 stop)
        characters = []
        bit_idx = 0
        
        while bit_idx + 11 <= len(bits):
            # Look for start bit (0)
            if bits[bit_idx] != 0:
                bit_idx += 1
                continue
            
            # Extract 8 data bits
            data_bits = bits[bit_idx + 1:bit_idx + 9]
            
            # Convert LSB-first to value
            value = sum(b << i for i, b in enumerate(data_bits))
            characters.append(value)
            
            bit_idx += 11
        
        # Parse based on number of characters
        if len(characters) >= 10:
            try:
                # Day of year (characters 3-4)
                day_hundreds = characters[2] & 0x0F
                day_tens = (characters[3] >> 4) & 0x0F
                day_units = characters[3] & 0x0F
                time_data['day'] = day_hundreds * 100 + day_tens * 10 + day_units
                
                # Hours (characters 5-6)
                hour_tens = (characters[4] >> 4) & 0x0F
                hour_units = characters[4] & 0x0F
                time_data['hour'] = hour_tens * 10 + hour_units
                
                # Minutes (characters 7-8)
                min_tens = (characters[6] >> 4) & 0x0F
                min_units = characters[6] & 0x0F
                time_data['minute'] = min_tens * 10 + min_units
                
                # Seconds (characters 9-10)
                sec_tens = (characters[8] >> 4) & 0x0F
                sec_units = characters[8] & 0x0F
                time_data['second'] = sec_tens * 10 + sec_units
                
                # Year (assumed current year)
                time_data['year'] = datetime.now(timezone.utc).year
                
                # DUT1 is typically encoded in specific bits
                # Simplified: extract from character 1
                dut1_sign = 1 if (characters[0] & 0x80) else -1
                dut1_value = (characters[0] & 0x0F) * 0.1
                time_data['dut1'] = dut1_sign * dut1_value * 1000  # Convert to ms
                
                # Leap second warning in character 2
                time_data['leap_pending'] = bool(characters[1] & 0x40)
                
            except (IndexError, ValueError):
                pass
        
        return time_data
    
    def _validate_chu_time(
        self,
        time_data: Dict[str, Any],
        expected_second: Optional[int] = None
    ) -> bool:
        """Validate decoded CHU time for sanity."""
        if not time_data:
            return False
        
        # Check ranges
        day = time_data.get('day', 0)
        hour = time_data.get('hour', -1)
        minute = time_data.get('minute', -1)
        second = time_data.get('second', -1)
        
        if not (1 <= day <= 366):
            return False
        if not (0 <= hour <= 23):
            return False
        if not (0 <= minute <= 59):
            return False
        if not (0 <= second <= 60):  # Allow leap second
            return False
        
        # Check against expected second if provided
        if expected_second is not None:
            if abs(second - expected_second) > 2:  # Allow 2s tolerance
                return False
        
        return True


# =============================================================================
# CONVENIENCE FUNCTIONS
# =============================================================================

_default_analyzer: Optional[AdvancedSignalAnalyzer] = None


def get_analyzer(sample_rate: int = 20000) -> AdvancedSignalAnalyzer:
    """Get or create the default signal analyzer."""
    global _default_analyzer
    if _default_analyzer is None or _default_analyzer.sample_rate != sample_rate:
        _default_analyzer = AdvancedSignalAnalyzer(sample_rate=sample_rate)
    return _default_analyzer
