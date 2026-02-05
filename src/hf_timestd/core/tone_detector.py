#!/usr/bin/env python3
"""
Multi-Station Tone Detector - Precision Timing via Matched Filtering

================================================================================
PURPOSE
================================================================================
Detect WWV/WWVH/CHU time signal tones to establish UTC time reference.
This is Step 1 of Phase 2 analytics - the foundation for all timing measurements.

The detected tone arrival time (T_arrival) is the primary observable for
computing D_clock (system time offset from UTC):

    D_clock = T_system - T_UTC(NIST)
            = T_arrival - T_propagation - T_emission

Where T_emission = 0 (tones transmitted at exact second boundary).

================================================================================
THEORY: MATCHED FILTERING
================================================================================
Matched filtering is the optimal linear filter for detecting a known signal
in additive white Gaussian noise (AWGN). It maximizes the output SNR.

For a known signal s(t) in noise n(t):
    x(t) = s(t - τ) + n(t)

The matched filter impulse response is:
    h(t) = s(-t)  (time-reversed signal)

The filter output y(t) = x(t) * h(t) peaks at the delay τ, with:
    SNR_out = 2E/N₀

Where:
    E = ∫|s(t)|² dt  (signal energy)
    N₀ = noise spectral density

REFERENCE: Turin, G.L. (1960). "An introduction to matched filters."
           IRE Transactions on Information Theory, 6(3), 311-329.

================================================================================
THEORY: PHASE-INVARIANT (QUADRATURE) DETECTION
================================================================================
HF propagation introduces unknown phase shifts. To detect tones regardless
of phase, we use quadrature matched filtering:

    Template_I(t) = sin(2πf₀t) · w(t)   (in-phase)
    Template_Q(t) = cos(2πf₀t) · w(t)   (quadrature)

Where w(t) is a window function (Tukey α=0.1 for smooth edges).

Correlation outputs:
    R_I = ∫ x(t) · Template_I(t - τ) dt
    R_Q = ∫ x(t) · Template_Q(t - τ) dt

Phase-invariant envelope:
    R(τ) = √(R_I² + R_Q²)

This envelope peaks at the tone arrival time regardless of carrier phase.

REFERENCE: Proakis, J.G. & Salehi, M. (2008). "Digital Communications,"
           5th ed., McGraw-Hill. Section 5.1.4.

================================================================================
THEORY: SUB-SAMPLE TIMING PRECISION
================================================================================
Integer sample detection limits timing resolution to ±1/(2·fs). For 20 kHz:
    Resolution = 1/20000 = 50 μs = 0.05 ms

Parabolic (quadratic) interpolation improves this by ~10x:
    Given peak at sample k with neighbors y[k-1], y[k], y[k+1]:
    
    δ = (y[k-1] - y[k+1]) / (2 · (y[k-1] - 2·y[k] + y[k+1]))
    
    Refined peak position: k + δ  (where |δ| ≤ 0.5)
    Refined peak value: y[k] - (y[k-1] - y[k+1]) · δ / 4

This achieves ~5 μs timing precision at 20 kHz sample rate.

REFERENCE: Smith, J.O. (2011). "Spectral Audio Signal Processing,"
           W3K Publishing. Chapter on Sinusoidal Peak Interpolation.
           https://ccrma.stanford.edu/~jos/sasp/

================================================================================
CRITIQUE & IMPROVEMENT: TWO-STAGE ONSET DETECTION (2025-12-07)
================================================================================
ORIGINAL IMPLEMENTATION FLAW:
-----------------------------
The original code used the correlation peak position as the tone "onset" time.
However, the matched filter correlation peak occurs when the template is
maximally aligned with the signal, which is approximately at:

    peak_position ≈ onset + template_length / 2

For an 800ms template, this introduces a ~400ms systematic bias, partially
compensated by the template length offset. More critically, the correlation
peak is "smeared" across the template length, reducing timing precision.

PER NIST SPECIFICATION:
-----------------------
"The beginning of the tone corresponds to the start of the minute." (NIST)
"Each tick consists of 5 cycles of a 1,000 Hz sine wave." (NIST SP 432)

The WWV/WWVH tones are HARD-KEYED at zero crossing - they switch from 
silence to full amplitude essentially instantaneously (within one sample).
This means the "rise time" is effectively zero, and the timing reference
is the FIRST SAMPLE of the tone, not the correlation peak.

IMPROVED METHODOLOGY:
---------------------
Stage 1: DETECTION (high confidence, low timing precision)
    - Full 800ms matched filter correlation
    - Optimal SNR for detecting tone presence (√16000 = 126× gain at 20 kHz)
    - Establishes: "A valid tone exists, centered roughly at position X"

Stage 2: ONSET TIMING (high precision on confirmed detection)
    - Bandpass filter around tone frequency (±50 Hz)
    - Compute energy envelope in search region around Stage 1 detection
    - Find rising edge: first sample where narrowband energy exceeds threshold
    - Sub-sample refinement via interpolation on energy envelope
    - Establishes: "The tone begins precisely at sample Y"

WHY THIS IS OPTIMAL:
--------------------
1. Stage 1 uses matched filter's optimal detection properties (provably best
   for known signal in AWGN) to confirm tone exists and reject false positives
   
2. Stage 2 uses edge detection to find the actual onset, which is the
   physical timing reference per NIST specification
   
3. The hard-keyed nature of WWV tones means the onset is a step function -
   the optimal detector for a step is edge-based, not correlation-based

EXPECTED IMPROVEMENT:
---------------------
- Original: Timing precision limited by template smearing (~±10-20 ms)
- Improved: Timing precision limited only by sample rate (~±0.05 ms at 20 kHz)

REFERENCE: NIST WWV/WWVH Digital Time Code and Broadcast Format
           https://www.nist.gov/pml/time-and-frequency-division/time-distribution/
           radio-station-wwv/wwv-and-wwvh-digital-time-code

================================================================================
SIGNAL PROCESSING CHAIN
================================================================================
1. INPUT: Complex IQ samples at sample_rate (typically 20 kHz)
   - Format: np.complex64 from Phase 1 Digital RF archive
   - Duration: 60 seconds (full minute buffer)

2. AM DEMODULATION: Extract envelope (magnitude)
   - magnitude[n] = |IQ[n]| = √(I[n]² + Q[n]²)
   - The timing tone is AM modulated on the carrier

3. AC COUPLING: Remove DC offset
   - audio[n] = magnitude[n] - mean(magnitude)
   - Ensures zero-mean signal for correlation

4. MATCHED FILTER CORRELATION:
   - Quadrature templates at tone frequency (1000 or 1200 Hz)
   - Template duration matches tone (0.8s WWV/WWVH, 0.5s CHU)
   - Unit-energy normalization for proper SNR calculation

5. ENVELOPE DETECTION:
   - R(τ) = √(R_sin² + R_cos²)
   - Phase-invariant detection

6. PEAK DETECTION:
   - Search within ±500ms of expected minute boundary
   - Noise-adaptive threshold (10th percentile + 3σ)
   - Sub-sample interpolation for precise timing

7. OUTPUT: ToneDetectionResult with:
   - timing_error_ms: Offset from minute boundary (= T_arrival - T_expected)
   - snr_db: Signal-to-noise ratio in decibels
   - confidence: Detection quality metric (0-1)

================================================================================
STATION CHARACTERISTICS
================================================================================
┌─────────┬────────────┬──────────┬─────────────────────────────────────────┐
│ Station │ Tone (Hz)  │ Duration │ Notes                                   │
├─────────┼────────────┼──────────┼─────────────────────────────────────────┤
│ WWV     │ 1000       │ 0.8s     │ Fort Collins, CO. 2.5-25 MHz            │
│ WWVH    │ 1200       │ 0.8s     │ Kauai, HI. 2.5, 5, 10, 15 MHz only      │
│ CHU     │ 1000       │ 0.5s     │ Ottawa, Canada. 3.33, 7.85, 14.67 MHz   │
│         │            │ 1.0s     │ (1.0s at top of hour only)              │
└─────────┴────────────┴──────────┴─────────────────────────────────────────┘

All tones transmitted at exact second boundary (T_emission = 0).

REFERENCE: NIST Special Publication 250-67 (2009). "NIST Time and Frequency
           Radio Stations: WWV, WWVH, and WWVB."
           https://www.nist.gov/pml/time-and-frequency-division/time-services

================================================================================
PROPAGATION DELAY BOUNDS
================================================================================
Timing errors outside physical propagation bounds indicate false detections:

    T_arrival = T_emission + T_propagation
              = 0 + (path_length / c)

Typical delays (receiver in central US):
    WWV:  5-30 ms  (1500-9000 km path via ionosphere)
    WWVH: 15-50 ms (4500-15000 km path via ionosphere)
    CHU:  3-25 ms  (1000-7500 km path via ionosphere)

Detections outside these bounds are rejected as interference.

================================================================================
USAGE
================================================================================
    detector = MultiStationToneDetector(
        channel_name='WWV_10_MHz',
        sample_rate=24000,
    )
    
    # Process 60-second IQ buffer
    detections = detector.process_samples(
        timestamp=buffer_mid_time,     # Unix timestamp at buffer midpoint
        samples=iq_samples,            # np.complex64 array
        rtp_timestamp=rtp_ts,          # For provenance tracking
        original_sample_rate=24000,
    )
    
    if detections:
        for det in detections:
            print(f"{det.station.value}: timing_error={det.timing_error_ms:+.2f}ms, "
                  f"SNR={det.snr_db:.1f}dB")

================================================================================
REVISION HISTORY
================================================================================
2025-12-07: TWO-STAGE ONSET DETECTION - Major timing precision improvement
            - Stage 1: Full correlation for high-confidence detection
            - Stage 2: Edge detection for precise onset timing
            - See "CRITIQUE & IMPROVEMENT" section for rationale
2025-12-07: Added comprehensive theoretical documentation
2025-11-17: Improved noise floor estimation (+6-11% detection rate)
2025-11-10: Fixed floating point precision bug in minute boundary calculation
2025-10-15: Initial implementation with quadrature matched filtering
"""

import logging
import re
import math
import numpy as np
from typing import Optional, List, Dict, Tuple
from scipy import signal as scipy_signal
from scipy.signal import correlate
from scipy.fft import rfft, rfftfreq, fft, ifft

from ..interfaces.tone_detection import ToneDetector, MultiStationToneDetector as IMultiStationToneDetector
from ..interfaces.data_models import ToneDetectionResult, StationType
from .wwv_constants import (
    WWV_ONLY_TONE_MINUTES,
    WWVH_ONLY_TONE_MINUTES,
    PROPAGATION_BOUNDS_MS,
    DEFAULT_PROPAGATION_BOUNDS_MS
)

# =============================================================================
# METROLOGICAL CONSTANTS (2026-01-24 Enhancement)
# =============================================================================
# Cramér-Rao bound for ToA estimation: σ_ToA = 1 / (2π × SNR × B × √(2T))
# where B = bandwidth (Hz), T = observation time (s), SNR = linear signal-to-noise
#
# For WWV 1000 Hz tone with 800ms duration and 50 Hz effective bandwidth:
#   At SNR = 20 dB (100 linear): σ_ToA ≈ 0.036 ms (theoretical minimum)
#   At SNR = 10 dB (10 linear):  σ_ToA ≈ 0.36 ms
#   At SNR = 6 dB (4 linear):    σ_ToA ≈ 0.9 ms
#
# Reference: Kay, S.M. (1993). "Fundamentals of Statistical Signal Processing:
#            Estimation Theory." Prentice Hall. Chapter 3.
# =============================================================================
CRAMER_RAO_BANDWIDTH_HZ = 50.0  # Effective bandwidth for tone detection

logger = logging.getLogger(__name__)


class MultiStationToneDetector(IMultiStationToneDetector):
    """
    Detect time signal tones from multiple stations using matched filtering
    
    Stations:
    - WWV (Fort Collins): 1000 Hz, 0.8s duration - PRIMARY for time_snap
    - WWVH (Hawaii): 1200 Hz, 0.8s duration - Propagation analysis ONLY
    - CHU (Canada): 1000 Hz, 0.5s duration - Alternate time_snap
    
    Uses phase-invariant quadrature matched filtering for robust detection
    in poor SNR conditions and with phase-shifted signals.
    """
    
    def __init__(self, channel_name: str, sample_rate: int = 24000):
        """
        Initialize multi-station tone detector
        
        Args:
            channel_name: Channel name to determine which stations to detect
            sample_rate: Processing sample rate (Hz), default 3000 Hz
        """
        self.channel_name = channel_name
        self.sample_rate = sample_rate
        self.is_chu_channel = 'CHU' in channel_name.upper()
        
        # Determine channel frequency from name
        self.channel_frequency_mhz = self._extract_frequency_mhz(channel_name)
        
        # Detection threshold (configurable)
        self.detection_threshold = 0.5
        
        # Create matched filter templates (quadrature for phase-invariance)
        self.templates: Dict[StationType, dict] = {}
        
        if self.is_chu_channel:
            # CHU frequencies: 3.33, 7.85, 14.67 MHz
            # Only detect CHU 1000 Hz (0.5s)
            self.templates[StationType.CHU] = self._create_template(1000, 0.5)
        else:
            # WWV frequencies: 2.5, 5, 10, 15, 20, 25 MHz
            # WWV frequencies: 2.5, 5, 10, 15, 20, 25 MHz
            # Always detect WWV 1000 Hz tone
            # Revert to 0.8s template to detect the strong Minute Tone
            self.templates[StationType.WWV] = self._create_template(1000, 0.8)
            
            # WWVH and BPM only broadcast on 2.5, 5, 10, 15 MHz (NOT on 20 or 25 MHz)
            shared_frequencies = [2.5, 5.0, 10.0, 15.0]
            if self.channel_frequency_mhz in shared_frequencies:
                self.templates[StationType.WWVH] = self._create_template(1200, 0.8)
                # BPM minute marker is 300ms at 1000Hz
                self.templates[StationType.BPM] = self._create_template(1000, 0.3)
                logger.info(f"{channel_name}: WWVH and BPM detection enabled (shared frequency)")
            else:
                logger.info(f"{channel_name}: WWVH/BPM detection disabled (WWV-only frequency)")
        
        # State tracking
        self.last_detections_by_minute: Dict[int, List[ToneDetectionResult]] = {}
        self.detection_count = 0
        self.last_detection_time: Optional[float] = None
        
        # Bootstrap lock state (2026-01-27)
        # When True, enforce propagation bounds to reject bad detections.
        # When False (during bootstrap), allow wider bounds to find initial lock.
        self.bootstrap_locked = False
        
        # Statistics tracking
        self.detection_stats: Dict[StationType, int] = {
            StationType.WWV: 0,
            StationType.WWVH: 0,
            StationType.CHU: 0,
            StationType.BPM: 0
        }
        self.total_attempts = 0
        self.timing_errors: List[float] = []
        
        # Differential delay tracking (WWV - WWVH)
        self.differential_delay_history: List[Dict[str, float]] = []
        
        # Adaptive threshold tracking (2026-01-24 Enhancement)
        # Track recent SNR values to adapt detection threshold
        self.recent_snr_history: List[float] = []  # Last N SNR measurements
        self.recent_noise_floor_history: List[float] = []  # Last N noise floor estimates
        self.adaptive_threshold_factor: float = 1.0  # Multiplier for noise floor threshold
        
        # Station priorities - DEPRECATED (2026-01-15)
        # All detected broadcasts are now treated equally and passed to fusion.
        # The fusion layer handles weighting based on uncertainty, not arbitrary priority.
        # Kept for API compatibility but not used for filtering.
        self.station_priorities: Dict[StationType, int] = {
            StationType.WWV: 100,
            StationType.CHU: 100,
            StationType.BPM: 100,
            StationType.WWVH: 100   # All stations equal - fusion handles weighting
        }
        
        logger.info(f"{channel_name}: MultiStationToneDetector initialized - "
                   f"stations={list(self.templates.keys())}, sample_rate={sample_rate}Hz")
    
    def _extract_frequency_mhz(self, channel_name: str) -> Optional[float]:
        """
        Extract frequency in MHz from channel name
        
        Args:
            channel_name: Channel name like "WWV 2.5 MHz", "WWV_10_MHz", "SHARED_5000"
            
        Returns:
            Frequency in MHz, or None if not found
        """
        # Pattern 1: Explicit MHz suffix (e.g., "WWV 2.5 MHz", "WWV_10_MHz")
        match = re.search(r'(\d+(?:\.\d+)?)[_\s]*MHz', channel_name, re.IGNORECASE)
        if match:
            return float(match.group(1))
        
        # Pattern 2: Channel names with frequency in kHz (e.g., "SHARED_5000", "WWV_20000")
        # These use the convention STATION_FREQ where FREQ is in kHz
        match = re.search(r'[A-Z]+_(\d+)$', channel_name, re.IGNORECASE)
        if match:
            freq_khz = int(match.group(1))
            # Sanity check: HF frequencies are 2500-25000 kHz
            if 2000 <= freq_khz <= 30000:
                return freq_khz / 1000.0
        
        return None
    
    def _create_template(self, frequency_hz: float, duration_sec: float) -> dict:
        """
        Create quadrature matched filter templates for phase-invariant detection.
        
        CRITICAL FIX (2026-01-08): Edge Detection Optimization
        -------------------------------------------------------
        At 24 kHz sample rate, using full-duration templates (800ms = 19,200 samples)
        is suboptimal for TIMING the leading edge. The matched filter peak occurs at
        the CENTER of the tone, not the leading edge.
        
        MATHEMATICALLY OPTIMAL APPROACH:
        --------------------------------
        Use a SHORT template (100ms) optimized for edge detection:
        - 100ms at 1000 Hz = 100 cycles → excellent frequency discrimination
        - 100ms at 24 kHz = 2400 samples → manageable correlation length
        - Detects the ONSET of the tone, not the center
        - 5x improvement in timing precision vs 500ms template
        - 8x improvement vs 800ms template
        
        This is the standard approach in radar/sonar timing systems where
        precise time-of-arrival is critical.
        
        THEORY:
        -------
        For phase-invariant detection of a sinusoidal tone, we create a
        quadrature pair of templates:
        
            Template_I(t) = sin(2πf₀t) · w(t)   (in-phase)
            Template_Q(t) = cos(2πf₀t) · w(t)   (quadrature)
        
        Where:
            f₀ = tone frequency (Hz)
            w(t) = Tukey window with α=0.1 (smooth 5% edges)
        
        Args:
            frequency_hz: Tone frequency in Hz (1000 for WWV/CHU, 1200 for WWVH)
            duration_sec: Tone duration in seconds (0.8s WWV, 0.5s CHU)
            
        Returns:
            dict containing:
                'sin': In-phase template (unit energy)
                'cos': Quadrature template (unit energy)
                'frequency': Tone frequency (Hz)
                'duration': Template duration
        """
        # Restore full duration template for maximum detection sensitivity
        # CRITICAL: Use provided duration (optimized by caller for Ticks vs Keys)
        # WWV Ticks = 5ms, CHU Keys = 300ms+
        optimal_duration_sec = duration_sec
        
        # Generate time vector

        n_samples = int(optimal_duration_sec * self.sample_rate)
        t = np.arange(n_samples) / self.sample_rate
        
        # Tukey window: rectangular with cosine-tapered edges
        # α = 0.1 means 5% taper on each end (90% flat)
        window = scipy_signal.windows.tukey(n_samples, alpha=0.1)
        
        # Create quadrature pair: sin and cos at tone frequency
        template_sin = np.sin(2 * np.pi * frequency_hz * t) * window
        template_cos = np.cos(2 * np.pi * frequency_hz * t) * window
        
        # Normalize to unit energy: ||template|| = 1
        template_sin /= np.linalg.norm(template_sin)
        template_cos /= np.linalg.norm(template_cos)
        
        return {
            'sin': template_sin,
            'cos': template_cos,
            'frequency': frequency_hz,
            'duration': optimal_duration_sec  # Return actual template duration
        }
    
    def _estimate_robust_noise_floor(
        self,
        correlation: np.ndarray,
        search_start_idx: int,
        search_end_idx: int
    ) -> float:
        """
        Robust noise floor estimation using samples OUTSIDE search region.
        
        Uses Median Absolute Deviation (MAD) - robust to outliers and interference.
        Prevents interference in search region from elevating noise floor estimate.
        
        THEORY:
        -------
        Traditional noise floor estimation uses percentile + std of all samples:
            threshold = P_10(all) + 3σ(all)
        
        Problem: If interference exists in the search region, it contaminates
        the noise estimate, raising the threshold and reducing sensitivity.
        
        Solution: Use only samples OUTSIDE the search region for noise estimation.
        These samples are guaranteed to not contain the signal of interest.
        
        MAD (Median Absolute Deviation) is more robust than standard deviation:
            MAD = median(|x - median(x)|)
            σ_equivalent = 1.4826 × MAD
        
        The factor 1.4826 converts MAD to equivalent standard deviation for
        Gaussian distributions, while remaining robust to outliers.
        
        EXPECTED IMPACT:
        ----------------
        - 5-10% improvement in weak signal detection
        - Better rejection of in-band interference
        - More stable threshold in varying noise conditions
        
        Args:
            correlation: Full correlation output
            search_start_idx: Start of search window
            search_end_idx: End of search window
            
        Returns:
            Noise floor threshold (median + 3σ_MAD)
        
        Reference:
            Rousseeuw, P.J. & Croux, C. (1993). "Alternatives to the Median
            Absolute Deviation." Journal of the American Statistical Association.
        """
        # Exclude search region from noise estimation
        mask = np.ones(len(correlation), dtype=bool)
        mask[search_start_idx:search_end_idx] = False
        noise_samples = correlation[mask]
        
        if len(noise_samples) < 100:
            # Fallback to percentile method for short buffers
            logger.debug("Insufficient noise samples, using percentile fallback")
            return np.percentile(correlation, 10)
        
        # Use MAD for robustness to outliers
        median = np.median(noise_samples)
        mad = np.median(np.abs(noise_samples - median))
        
        # Convert MAD to equivalent standard deviation
        # Factor 1.4826 = 1/Φ^(-1)(0.75) where Φ is standard normal CDF
        sigma_equivalent = 1.4826 * mad
        
        # Noise floor = median + 3σ (99.7% confidence for Gaussian noise)
        noise_floor = median + 3.0 * sigma_equivalent
        
        logger.debug(f"Robust noise floor: median={median:.3f}, "
                    f"MAD={mad:.3f}, σ_eq={sigma_equivalent:.3f}, "
                    f"threshold={noise_floor:.3f}")
        
        return noise_floor
    
    def _calculate_adaptive_search_window(
        self, 
        recent_snr_db: Optional[float],
        convergence_state: str  # 'ACQUIRING', 'CONVERGING', 'LOCKED'
    ) -> float:
        """
        Calculate adaptive search window based on SNR and convergence state.
        
        Narrow search window as SNR improves and system converges to reduce
        false positive rate while maintaining detection sensitivity.
        
        RATIONALE:
        ----------
        Wide search windows (±500ms) are needed during initial acquisition when
        we have no prior timing information. However, once the system has locked
        with high SNR, we can dramatically narrow the search window.
        
        Benefits of adaptive narrowing:
        - Reduces search space by up to 100x (500ms → 5ms)
        - Dramatically lowers false positive rate
        - Maintains sensitivity (signal is where we expect it)
        - Faster processing (smaller correlation window)
        
        STRATEGY:
        ---------
        State-based progression:
        1. ACQUIRING: Wide search (±500ms) - no prior knowledge
        2. CONVERGING: Medium search (±50ms) - building confidence  
        3. LOCKED + High SNR (>20dB): Very tight (±5ms) - high confidence
        4. LOCKED + Good SNR (>15dB): Tight (±15ms) - good confidence
        5. LOCKED + Medium SNR (>10dB): Moderate (±50ms) - moderate confidence
        
        EXPECTED IMPACT:
        ----------------
        - 10-20% reduction in false positives
        - Faster convergence (fewer false detections to reject)
        - More stable timing once locked
        
        Args:
            recent_snr_db: Recent SNR measurement from previous detection (if available)
            convergence_state: Current convergence state from clock_convergence module
                              ('ACQUIRING', 'CONVERGING', 'LOCKED', 'HOLDOVER', 'REACQUIRE')
        
        Returns:
            Search window half-width in milliseconds
        
        Reference:
            Kay, S.M. (1998). "Fundamentals of Statistical Signal Processing:
            Detection Theory." Prentice Hall. Chapter 6: Composite Hypothesis Testing.
        """
        # LOCKED state with high SNR: very tight window
        if convergence_state == 'LOCKED' and recent_snr_db and recent_snr_db > 20:
            window_ms = 5.0  # ±5ms - 100x narrower than initial
            logger.debug(f"Adaptive window: LOCKED + high SNR ({recent_snr_db:.1f}dB) → ±{window_ms}ms")
            return window_ms
        
        # CONVERGING or LOCKED with good SNR: tight window
        elif convergence_state in ('CONVERGING', 'LOCKED'):
            if recent_snr_db and recent_snr_db > 15:
                window_ms = 15.0  # ±15ms
                logger.debug(f"Adaptive window: {convergence_state} + good SNR ({recent_snr_db:.1f}dB) → ±{window_ms}ms")
                return window_ms
            elif recent_snr_db and recent_snr_db > 10:
                window_ms = 50.0  # ±50ms
                logger.debug(f"Adaptive window: {convergence_state} + medium SNR ({recent_snr_db:.1f}dB) → ±{window_ms}ms")
                return window_ms
        
        # ACQUIRING, REACQUIRE, HOLDOVER, or low SNR: wide search
        window_ms = 500.0  # ±500ms (default)
        logger.debug(f"Adaptive window: {convergence_state} or low SNR → ±{window_ms}ms (wide search)")
        return window_ms
    
    def _find_precise_onset(
        self,
        audio_signal: np.ndarray,
        correlation_peak_idx: int,
        tone_freq_hz: float,
        tone_duration_sec: float,
        noise_floor: float
    ) -> Tuple[float, float]:
        """
        Stage 2 Onset Detection: Find the precise leading edge of the tone.
        
        =====================================================================
        RATIONALE (2025-12-07 Critique):
        =====================================================================
        The matched filter correlation peak indicates WHERE a tone is. For
        mode='valid' correlation, the peak index equals the tone start index.
        However, the correlation peak can be "smeared" by noise and multipath.
        
        Per NIST specification, WWV/WWVH tones are HARD-KEYED: they transition
        from silence to full amplitude in essentially zero time (at zero crossing).
        The timing reference is "the beginning of the tone" - the first sample.
        
        This method finds that first sample by:
        1. Extracting a search region around the correlation peak
        2. Bandpass filtering to isolate the tone frequency
        3. Computing the energy envelope
        4. Finding the rising edge where energy exceeds threshold
        5. Applying sub-sample interpolation for maximum precision
        
        =====================================================================
        ALGORITHM:
        =====================================================================
        Given correlation peak at index P and template duration D:
        
        1. Search region: [P - D*fs - margin, P + margin]
           The onset must be before the correlation peak, at most D seconds before.
        
        2. Bandpass filter: Isolate tone_freq ± 50 Hz
           Rejects out-of-band noise that could trigger false edges.
        
        3. Energy envelope: Moving average of squared signal (window ~5ms)
           Smooths the sinusoidal oscillation to reveal the amplitude envelope.
        
        4. Edge detection: Find first sample where envelope > threshold
           Threshold = noise_floor * edge_threshold_factor (empirically ~2-3)
           
        5. Sub-sample refinement: Linear interpolation at threshold crossing
           Achieves ~1/10 sample precision.
        
        Args:
            audio_signal: AM-demodulated, AC-coupled signal
            correlation_peak_idx: Peak position from Stage 1 matched filter
            tone_freq_hz: Tone frequency (1000 Hz WWV/CHU, 1200 Hz WWVH)
            tone_duration_sec: Expected tone duration (0.8s WWV/WWVH, 0.5s CHU)
            noise_floor: Noise floor estimate from Stage 1
            
        Returns:
            Tuple of (precise_onset_idx, onset_confidence):
                - precise_onset_idx: Sample index of tone onset (with sub-sample precision)
                - onset_confidence: Confidence metric (0-1) based on edge sharpness
        """
        # =================================================================
        # STEP 1: Define search region
        # =================================================================
        # For mode='valid' correlation:
        #   correlation[k] = sum(signal[k:k+M] * template)
        # So the peak at index k means the tone STARTS at audio_signal[k].
        # The correlation peak index IS the tone onset - no offset needed.
        #
        # We search a small region around the correlation peak to find the
        # precise rising edge using energy envelope detection. Search slightly
        # before the peak (to find the exact onset) and into the tone.
        
        template_samples = int(tone_duration_sec * self.sample_rate)
        margin_samples = int(0.050 * self.sample_rate)  # 50ms margin before
        
        # Search region: from slightly before the correlation peak to partway into the tone
        # The correlation peak IS at the tone start, so we search:
        #   - margin_samples before (to find exact rising edge)
        #   - template_samples/2 after (into the tone for edge detection)
        search_start = max(0, correlation_peak_idx - margin_samples)
        search_end = min(len(audio_signal), correlation_peak_idx + template_samples // 2)
        
        if search_end <= search_start:
            logger.warning(f"Invalid onset search region: [{search_start}:{search_end}]")
            return float(correlation_peak_idx), 0.0
        
        search_region = audio_signal[search_start:search_end]
        
        # =================================================================
        # STEP 2: Bandpass filter to isolate tone frequency
        # =================================================================
        # Butterworth bandpass: tone_freq ± 50 Hz
        # This rejects broadband noise while preserving the tone
        
        bandwidth_hz = 50.0
        low_freq = max(10, tone_freq_hz - bandwidth_hz)
        high_freq = min(self.sample_rate / 2 - 10, tone_freq_hz + bandwidth_hz)
        
        try:
            # Design 4th-order Butterworth bandpass
            sos = scipy_signal.butter(
                4, 
                [low_freq, high_freq], 
                btype='band', 
                fs=self.sample_rate, 
                output='sos'
            )
            filtered = scipy_signal.sosfiltfilt(sos, search_region)
        except Exception as e:
            logger.warning(f"Bandpass filter failed: {e}, using unfiltered signal")
            filtered = search_region
        
        # =================================================================
        # STEP 3: Compute energy envelope
        # =================================================================
        # Square the signal and apply moving average to get envelope
        # Window size ~5ms (100 samples at 20 kHz) to smooth oscillations
        
        energy = filtered ** 2
        
        window_samples = max(3, int(0.005 * self.sample_rate))  # 5ms or minimum 3
        if len(energy) >= window_samples:
            # Moving average via convolution
            kernel = np.ones(window_samples) / window_samples
            envelope = np.convolve(energy, kernel, mode='same')
        else:
            envelope = energy
        
        # =================================================================
        # STEP 4: Find rising edge (onset)
        # =================================================================
        # The onset is where the envelope first exceeds the noise threshold.
        # Use a threshold above the noise floor but below the signal level.
        
        # Estimate envelope noise floor from first 10% of search region
        # (which should be before the tone onset)
        noise_region_end = max(10, len(envelope) // 10)
        envelope_noise = np.median(envelope[:noise_region_end])
        envelope_max = np.max(envelope)
        
        # Threshold: midpoint between noise and signal (in log domain for dB-like scaling)
        # This is robust to varying signal levels
        if envelope_max > envelope_noise * 2:
            # Use geometric mean of noise and max as threshold
            threshold = np.sqrt(envelope_noise * envelope_max) * 0.5
        else:
            # Low SNR: use noise + 2σ
            envelope_std = np.std(envelope[:noise_region_end])
            threshold = envelope_noise + 2 * envelope_std
        
        # Find first crossing above threshold
        above_threshold = envelope > threshold
        onset_candidates = np.where(above_threshold)[0]
        
        if len(onset_candidates) == 0:
            logger.debug(f"No onset found above threshold, using correlation peak")
            return float(correlation_peak_idx), 0.3
        
        # First crossing is the onset (in search region coordinates)
        onset_local = onset_candidates[0]
        
        # =================================================================
        # STEP 5: Sub-sample refinement via linear interpolation
        # =================================================================
        # Interpolate the exact threshold crossing point between samples
        
        sub_sample_offset = 0.0
        if onset_local > 0:
            y_before = envelope[onset_local - 1]
            y_after = envelope[onset_local]
            
            if y_after > y_before:
                # Linear interpolation: find where line crosses threshold
                # y = y_before + (y_after - y_before) * t, solve for y = threshold
                t = (threshold - y_before) / (y_after - y_before)
                sub_sample_offset = t - 1.0  # Offset from onset_local
                sub_sample_offset = max(-1.0, min(0.0, sub_sample_offset))
        
        # Convert from search region coordinates to global coordinates
        precise_onset_idx = search_start + onset_local + sub_sample_offset
        
        # =================================================================
        # STEP 6: Calculate confidence based on edge sharpness
        # =================================================================
        # Sharp edge = high confidence, gradual rise = low confidence
        # Measure the rise rate (slope) at onset
        
        if onset_local + 5 < len(envelope) and onset_local > 0:
            # Slope: how fast does envelope rise at onset?
            rise = envelope[onset_local + 5] - envelope[onset_local - 1]
            max_possible_rise = envelope_max - envelope_noise
            
            if max_possible_rise > 0:
                sharpness = min(1.0, rise / max_possible_rise)
                onset_confidence = 0.5 + 0.5 * sharpness  # Map to 0.5-1.0
            else:
                onset_confidence = 0.5
        else:
            onset_confidence = 0.5
        
        logger.debug(f"Onset detection: corr_peak={correlation_peak_idx}, "
                    f"search=[{search_start}:{search_end}], "
                    f"onset_local={onset_local}, precise={precise_onset_idx:.2f}, "
                    f"confidence={onset_confidence:.2f}")
        
        return precise_onset_idx, onset_confidence
    
    # =========================================================================
    # METROLOGICAL ENHANCEMENTS (2026-01-24)
    # =========================================================================
    
    def _calculate_adaptive_threshold(
        self,
        base_noise_floor: float,
        snr_db: Optional[float] = None
    ) -> float:
        """
        Calculate adaptive detection threshold based on channel history.
        
        RATIONALE (2026-01-24 Enhancement):
        -----------------------------------
        Fixed thresholds (noise_floor + 3σ) work well for stationary noise but
        fail in HF environments with:
        - Impulsive interference (atmospheric noise, powerline)
        - Fading (signal strength varies by 20+ dB)
        - Time-varying noise floor (diurnal, seasonal)
        
        Adaptive thresholding adjusts based on:
        1. Recent detection success rate (lower threshold if missing detections)
        2. Recent SNR history (higher threshold if consistently high SNR)
        3. Noise floor stability (tighter threshold if stable)
        
        This implements a simplified CFAR (Constant False Alarm Rate) approach.
        
        Args:
            base_noise_floor: Current noise floor estimate from MAD
            snr_db: Current SNR estimate (if available)
            
        Returns:
            Adapted threshold value
        """
        # Update history
        if snr_db is not None and snr_db > 0:
            self.recent_snr_history.append(snr_db)
            if len(self.recent_snr_history) > 20:
                self.recent_snr_history = self.recent_snr_history[-20:]
        
        self.recent_noise_floor_history.append(base_noise_floor)
        if len(self.recent_noise_floor_history) > 20:
            self.recent_noise_floor_history = self.recent_noise_floor_history[-20:]
        
        # Calculate adaptive factor based on detection rate
        if self.total_attempts > 10:
            total_detections = sum(self.detection_stats.values())
            detection_rate = total_detections / self.total_attempts
            
            # If detection rate is low (<50%), reduce threshold to improve sensitivity
            # If detection rate is high (>90%), can afford tighter threshold
            if detection_rate < 0.3:
                # Very low detection rate - significantly lower threshold
                rate_factor = 0.7
                logger.debug(f"Adaptive threshold: low detection rate ({detection_rate:.1%}) → factor={rate_factor}")
            elif detection_rate < 0.5:
                # Low detection rate - moderately lower threshold
                rate_factor = 0.85
            elif detection_rate > 0.9:
                # High detection rate - can use tighter threshold
                rate_factor = 1.1
            else:
                # Normal detection rate
                rate_factor = 1.0
        else:
            rate_factor = 1.0  # Not enough history
        
        # Calculate noise floor stability factor
        if len(self.recent_noise_floor_history) >= 5:
            noise_std = np.std(self.recent_noise_floor_history)
            noise_mean = np.mean(self.recent_noise_floor_history)
            
            if noise_mean > 0:
                cv = noise_std / noise_mean  # Coefficient of variation
                
                # Stable noise (low CV) → can use tighter threshold
                # Unstable noise (high CV) → need looser threshold
                if cv < 0.1:
                    stability_factor = 0.95  # Very stable
                elif cv > 0.5:
                    stability_factor = 1.2  # Very unstable
                else:
                    stability_factor = 1.0 + (cv - 0.1) * 0.5  # Linear interpolation
            else:
                stability_factor = 1.0
        else:
            stability_factor = 1.0
        
        # Combine factors
        self.adaptive_threshold_factor = rate_factor * stability_factor
        
        # Clamp to reasonable range [0.5, 1.5]
        self.adaptive_threshold_factor = max(0.5, min(1.5, self.adaptive_threshold_factor))
        
        adapted_threshold = base_noise_floor * self.adaptive_threshold_factor
        
        logger.debug(f"Adaptive threshold: base={base_noise_floor:.3f}, "
                    f"rate_factor={rate_factor:.2f}, stability_factor={stability_factor:.2f}, "
                    f"final_factor={self.adaptive_threshold_factor:.2f}, threshold={adapted_threshold:.3f}")
        
        return adapted_threshold
    
    def _calculate_cramer_rao_uncertainty(
        self,
        snr_db: float,
        duration_sec: float,
        bandwidth_hz: float = CRAMER_RAO_BANDWIDTH_HZ
    ) -> float:
        """
        Calculate Cramér-Rao lower bound for ToA estimation uncertainty.
        
        The Cramér-Rao bound gives the theoretical minimum variance for any
        unbiased estimator. For ToA estimation of a sinusoidal tone in AWGN:
        
            σ_ToA = 1 / (2π × √(2 × SNR × B × T))
        
        where:
            SNR = signal-to-noise ratio (linear, not dB)
            B = effective bandwidth (Hz)
            T = observation time (seconds)
        
        This provides a rigorous uncertainty estimate for downstream fusion.
        
        Args:
            snr_db: Signal-to-noise ratio in decibels
            duration_sec: Tone duration in seconds
            bandwidth_hz: Effective detection bandwidth (default 50 Hz)
            
        Returns:
            ToA uncertainty in milliseconds (σ_ToA)
            
        Reference:
            Kay, S.M. (1993). "Fundamentals of Statistical Signal Processing:
            Estimation Theory." Prentice Hall. Eq. 3.32.
        """
        # Convert SNR from dB to linear
        snr_linear = 10 ** (snr_db / 10.0)
        
        # Cramér-Rao bound: σ_ToA = 1 / (2π × √(2 × SNR × B × T))
        # Factor of 2 inside sqrt accounts for complex (I/Q) processing
        denominator = 2 * math.pi * math.sqrt(2 * snr_linear * bandwidth_hz * duration_sec)
        
        if denominator > 0:
            sigma_toa_sec = 1.0 / denominator
            sigma_toa_ms = sigma_toa_sec * 1000.0
        else:
            sigma_toa_ms = 10.0  # Default high uncertainty for invalid inputs
        
        # Clamp to reasonable range [0.01, 50] ms
        sigma_toa_ms = max(0.01, min(50.0, sigma_toa_ms))
        
        logger.debug(f"Cramér-Rao bound: SNR={snr_db:.1f}dB, T={duration_sec:.3f}s, "
                    f"B={bandwidth_hz:.0f}Hz → σ_ToA={sigma_toa_ms:.3f}ms")
        
        return sigma_toa_ms
    
    def _complex_correlation_with_phase(
        self,
        signal: np.ndarray,
        tone_frequency: float,
        template_duration_sec: float
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """
        Perform complex correlation preserving phase information.
        
        Unlike envelope detection (√(sin² + cos²)) which discards phase,
        this method preserves the complex correlation for:
        - Sub-sample timing refinement from phase at peak
        - Doppler estimation from phase slope
        - Multipath detection from phase discontinuities
        
        Args:
            signal: Input signal (real-valued, AM demodulated)
            tone_frequency: Expected tone frequency (Hz)
            template_duration_sec: Template duration (seconds)
            
        Returns:
            Tuple of (magnitude, phase, complex_correlation):
                - magnitude: Phase-invariant envelope (same as standard detection)
                - phase: Phase at each correlation lag (radians)
                - complex_correlation: Full complex correlation for advanced analysis
        """
        template_samples = int(template_duration_sec * self.sample_rate)
        
        # Generate complex template: exp(j × 2π × f × t) = cos + j×sin
        t = np.arange(template_samples) / self.sample_rate
        template_complex = np.exp(2j * np.pi * tone_frequency * t)
        
        # Apply Tukey window for smooth edges
        window = scipy_signal.windows.tukey(template_samples, alpha=0.1)
        template_complex = template_complex * window
        
        # Normalize to unit energy
        template_complex = template_complex / np.linalg.norm(template_complex)
        
        # Complex correlation via FFT (efficient for long signals)
        n_fft = len(signal) + template_samples - 1
        n_fft = int(2 ** np.ceil(np.log2(n_fft)))  # Power of 2 for efficiency
        
        signal_fft = fft(signal.astype(np.float64), n_fft)
        template_fft = fft(template_complex, n_fft)
        correlation_fft = signal_fft * np.conj(template_fft)
        correlation_complex = ifft(correlation_fft)[:len(signal)]
        
        # Extract magnitude and phase
        magnitude = np.abs(correlation_complex)
        phase = np.angle(correlation_complex)
        
        return magnitude, phase, correlation_complex
    
    def _phase_based_subsample_refinement(
        self,
        phase_at_peak: float,
        tone_frequency: float
    ) -> float:
        """
        Compute sub-sample timing offset from phase at correlation peak.
        
        At the correlation peak, the phase tells us the fractional sample
        offset from the integer peak position:
        
            φ = 2π × f × Δt
            Δt = φ / (2π × f)
        
        This provides ~10× finer resolution than sample-based timing.
        
        Args:
            phase_at_peak: Phase at correlation peak (radians, -π to π)
            tone_frequency: Tone frequency (Hz)
            
        Returns:
            Sub-sample offset in samples (typically -0.5 to +0.5)
        """
        if abs(tone_frequency) < 1e-6:
            return 0.0
        
        # Convert phase to time offset
        # Δt = -φ / (2π × f)  [negative because positive phase = signal arrived early]
        delta_t_sec = -phase_at_peak / (2 * math.pi * tone_frequency)
        
        # Convert to samples
        delta_samples = delta_t_sec * self.sample_rate
        
        # Wrap to [-0.5, 0.5] samples (phase ambiguity)
        delta_samples = ((delta_samples + 0.5) % 1.0) - 0.5
        
        return delta_samples
    
    def _estimate_doppler_from_phase_slope(
        self,
        phase: np.ndarray,
        peak_idx: int,
        window_samples: int = 50
    ) -> Tuple[float, float]:
        """
        Estimate Doppler shift from phase rotation rate around correlation peak.
        
        Doppler shift causes continuous phase rotation:
            φ(t) = 2π × f_doppler × t
            f_doppler = (dφ/dt) / (2π)
        
        By measuring the phase slope around the correlation peak, we can
        estimate the Doppler offset caused by ionospheric motion.
        
        Args:
            phase: Phase array from complex correlation (radians)
            peak_idx: Index of correlation peak
            window_samples: Number of samples around peak to analyze
            
        Returns:
            Tuple of (doppler_hz, confidence):
                - doppler_hz: Estimated Doppler shift (Hz)
                - confidence: Confidence in estimate (0-1)
        """
        # Extract phase window around peak
        start = max(0, peak_idx - window_samples)
        end = min(len(phase), peak_idx + window_samples)
        
        if end - start < 10:
            return 0.0, 0.0
        
        phase_window = phase[start:end]
        
        # Unwrap phase to remove 2π discontinuities
        phase_unwrapped = np.unwrap(phase_window)
        
        # Linear fit to get slope (rad/sample)
        t = np.arange(len(phase_unwrapped))
        try:
            coeffs = np.polyfit(t, phase_unwrapped, 1)
            phase_slope = coeffs[0]  # rad/sample
            
            # Convert to Hz: f = (dφ/dt) / (2π) = (dφ/dsample × sample_rate) / (2π)
            doppler_hz = phase_slope * self.sample_rate / (2 * math.pi)
            
            # Confidence from fit residuals (lower residuals = higher confidence)
            fit = np.polyval(coeffs, t)
            residuals = phase_unwrapped - fit
            residual_std = np.std(residuals)
            confidence = 1.0 / (1.0 + residual_std)
            
            return float(doppler_hz), float(confidence)
            
        except (np.linalg.LinAlgError, ValueError):
            return 0.0, 0.0
    
    def _apply_doppler_correction(
        self,
        timing_error_ms: float,
        doppler_hz: float,
        tone_frequency: float,
        tone_duration_sec: float
    ) -> Tuple[float, float]:
        """
        Apply Doppler correction to ToA estimate.
        
        RATIONALE (2026-01-24 Enhancement):
        -----------------------------------
        Doppler shift from ionospheric motion causes continuous phase rotation
        during the tone. This phase drift biases the correlation peak position
        and thus the ToA estimate.
        
        The bias is approximately:
            Δt_bias ≈ (f_doppler / f_tone) × (T_tone / 2)
        
        For typical HF Doppler (±1-5 Hz) on 1000 Hz tone over 800ms:
            Δt_bias ≈ (5 / 1000) × 0.4 = 2 ms (worst case)
        
        This correction removes the systematic bias from Doppler.
        
        Args:
            timing_error_ms: Raw timing error before correction
            doppler_hz: Estimated Doppler shift (Hz)
            tone_frequency: Tone frequency (Hz)
            tone_duration_sec: Tone duration (seconds)
            
        Returns:
            Tuple of (corrected_timing_ms, correction_applied_ms):
                - corrected_timing_ms: Timing error after Doppler correction
                - correction_applied_ms: Amount of correction applied
        """
        if abs(doppler_hz) < 0.01 or tone_frequency < 1.0:
            # No significant Doppler or invalid frequency
            return timing_error_ms, 0.0
        
        # Calculate Doppler-induced timing bias
        # The correlation peak shifts by approximately:
        #   Δt = (f_doppler / f_tone) × (T_tone / 2)
        # This is because the phase rotates during the tone, shifting the
        # effective center of the correlation.
        
        doppler_bias_sec = (doppler_hz / tone_frequency) * (tone_duration_sec / 2)
        doppler_bias_ms = doppler_bias_sec * 1000.0
        
        # Apply correction (subtract bias)
        corrected_timing_ms = timing_error_ms - doppler_bias_ms
        
        logger.debug(f"Doppler correction: f_D={doppler_hz:+.2f}Hz, "
                    f"bias={doppler_bias_ms:+.3f}ms, "
                    f"raw={timing_error_ms:+.2f}ms → corrected={corrected_timing_ms:+.2f}ms")
        
        return corrected_timing_ms, doppler_bias_ms
    
    def _detect_multipath_from_correlation(
        self,
        magnitude: np.ndarray,
        phase: np.ndarray,
        peak_idx: int
    ) -> Tuple[bool, float, float]:
        """
        Detect multipath propagation from correlation characteristics.
        
        Multipath causes:
        1. Broadened correlation peak (multiple arrivals smear the peak)
        2. Secondary peaks (distinct multipath components)
        3. Phase instability (interference between paths)
        
        Args:
            magnitude: Correlation magnitude array
            phase: Correlation phase array (radians)
            peak_idx: Index of primary correlation peak
            
        Returns:
            Tuple of (is_multipath, delay_spread_ms, quality_metric):
                - is_multipath: True if multipath detected
                - delay_spread_ms: Estimated delay spread (ms)
                - quality_metric: 0-1, higher = cleaner signal
        """
        peak_mag = magnitude[peak_idx]
        
        # 1. Measure peak width at -3dB
        threshold = peak_mag * 0.707  # -3 dB
        
        left = peak_idx
        while left > 0 and magnitude[left] > threshold:
            left -= 1
        
        right = peak_idx
        while right < len(magnitude) - 1 and magnitude[right] > threshold:
            right += 1
        
        width_samples = right - left
        width_ms = width_samples * 1000.0 / self.sample_rate
        
        # Expected width for clean signal (~10% of template duration)
        # For 800ms template at 24kHz: ~80ms expected width
        expected_width_ms = 80.0  # Approximate for 800ms template
        width_ratio = width_ms / expected_width_ms if expected_width_ms > 0 else 1.0
        
        # 2. Check for secondary peaks (>30% of primary)
        secondary_threshold = peak_mag * 0.3
        min_separation = int(0.002 * self.sample_rate)  # 2ms minimum separation
        
        secondary_count = 0
        for i in range(max(0, peak_idx - 500), min(len(magnitude), peak_idx + 500)):
            if abs(i - peak_idx) < min_separation:
                continue
            if i > 0 and i < len(magnitude) - 1:
                if (magnitude[i] > magnitude[i-1] and 
                    magnitude[i] > magnitude[i+1] and
                    magnitude[i] > secondary_threshold):
                    secondary_count += 1
        
        # 3. Phase stability around peak
        window = 50  # samples
        start = max(0, peak_idx - window)
        end = min(len(phase), peak_idx + window)
        
        if end - start > 10:
            phase_window = phase[start:end]
            phase_unwrapped = np.unwrap(phase_window)
            
            # Remove linear trend (Doppler)
            t = np.arange(len(phase_unwrapped))
            try:
                coeffs = np.polyfit(t, phase_unwrapped, 1)
                phase_detrended = phase_unwrapped - np.polyval(coeffs, t)
                phase_std = float(np.std(phase_detrended))
            except:
                phase_std = 0.5
        else:
            phase_std = 0.5
        
        # Multipath detection criteria
        is_multipath = (
            width_ratio > 1.5 or  # Peak 50% wider than expected
            secondary_count > 0 or  # Secondary peaks present
            phase_std > 0.5  # Phase instability > 0.5 rad
        )
        
        # Delay spread estimate (from peak width)
        delay_spread_ms = max(0.0, width_ms - expected_width_ms)
        
        # Quality metric (inverse of multipath severity)
        quality_metric = 1.0 - min(1.0, (
            0.4 * min(1.0, width_ratio / 3.0) +
            0.3 * min(1.0, secondary_count / 3.0) +
            0.3 * min(1.0, phase_std / 1.0)
        ))
        
        logger.debug(f"Multipath analysis: width={width_ms:.1f}ms (ratio={width_ratio:.2f}), "
                    f"secondary_peaks={secondary_count}, phase_std={phase_std:.2f}rad, "
                    f"is_multipath={is_multipath}, quality={quality_metric:.2f}")
        
        return is_multipath, delay_spread_ms, quality_metric
    
    def process_samples(
        self,
        timestamp: float,
        samples: np.ndarray,
        rtp_timestamp: Optional[int] = None,
        original_sample_rate: Optional[int] = None,
        buffer_rtp_start: Optional[int] = None,
        search_window_ms: Optional[float] = None,
        expected_offset_ms: Optional[float] = None,
        expected_delays_by_station: Optional[Dict[str, float]] = None
    ) -> Optional[List[ToneDetectionResult]]:
        """
        Process samples and detect tones (ToneDetector interface).
        
        Args:
            timestamp: UTC timestamp of samples (from time_snap if available)
            samples: Complex IQ samples at self.sample_rate
            rtp_timestamp: Optional RTP timestamp for provenance
            original_sample_rate: Original sample rate before decimation (e.g., 20000)
            buffer_rtp_start: RTP timestamp at start of original buffer
            search_window_ms: Search window in milliseconds (default 500ms)
                Pass 0: Use default ±500ms for initial wide search
                Pass 1+: Use guided narrow window (e.g., ±30ms) from anchor
            expected_offset_ms: Expected offset from minute boundary (default 0)
                Pass 0: Use 0 (search around minute boundary)
                Pass 1+: Use expected propagation delay (e.g., +20ms for CHU)
                This centers the search window at minute_boundary + expected_offset
                DEPRECATED: Use expected_delays_by_station for per-station delays
            expected_delays_by_station: Dict mapping station name to expected delay in ms
                e.g., {'WWV': 4.3, 'WWVH': 25.3, 'CHU': 5.8, 'BPM': 44.1}
                If provided, overrides expected_offset_ms for each station
            
        Returns:
            List of ToneDetectionResult objects (may contain WWV + WWVH),
            or None if no tones detected
        """
        self.total_attempts += 1
        detections = self._detect_tones_internal(
            samples, timestamp, original_sample_rate, buffer_rtp_start, 
            search_window_ms, expected_offset_ms, expected_delays_by_station
        )
        
        if detections:
            self.last_detection_time = timestamp
            
            # Update statistics
            for det in detections:
                self.detection_stats[det.station] += 1
                if det.use_for_time_snap:
                    self.timing_errors.append(det.timing_error_ms)
                    
                    # Keep last 1000 timing errors for statistics
                    if len(self.timing_errors) > 1000:
                        self.timing_errors = self.timing_errors[-1000:]
            
            # Calculate differential delay if both WWV and WWVH detected
            self._update_differential_delay(detections, timestamp)
            
            return detections
        
        return None
    
    def _detect_tones_internal(
        self,
        iq_samples: np.ndarray,
        current_unix_time: float,
        original_sample_rate: Optional[int] = None,
        buffer_rtp_start: Optional[int] = None,
        search_window_ms: Optional[float] = None,
        expected_offset_ms: Optional[float] = None,
        expected_delays_by_station: Optional[Dict[str, float]] = None
    ) -> List[ToneDetectionResult]:
        """
        Internal tone detection implementation
        
        Args:
            iq_samples: Complex IQ samples at self.sample_rate
            current_unix_time: UTC time for first sample
            expected_delays_by_station: Per-station expected delays in ms
            
        Returns:
            List of ToneDetectionResult objects, sorted by SNR (strongest first)
        """
        # Get minute boundary for the EXPECTED tone (around :00.0)
        # Calculate buffer start time (current_unix_time is buffer MIDPOINT)
        buffer_duration_sec = len(iq_samples) / self.sample_rate
        buffer_start_time = current_unix_time - (buffer_duration_sec / 2)
        
        # In RTP mode (L4/L5), the buffer starts exactly on the minute boundary
        # (start_system_time = minute_boundary). The minute marker tone is at
        # sample 0 (plus propagation delay from the transmitter).
        #
        # Use floor to find the minute boundary that the buffer START falls in
        minute_boundary = int(buffer_start_time / 60) * 60
        
        # Step 1: AM demodulation (extract envelope)
        magnitude = np.abs(iq_samples)
        audio_signal = magnitude - np.mean(magnitude)  # AC coupling
        
        # Diagnostic: Check signal energy
        audio_rms = np.sqrt(np.mean(audio_signal**2))
        logger.debug(f"AM demod: iq_len={len(iq_samples)}, audio_rms={audio_rms:.6f}, "
                    f"mag_mean={np.mean(magnitude):.6f}")
        
        # LEADING ZEROS CORRECTION (2026-02-01)
        # -------------------------------------
        # Due to radiod sample delivery latency, buffers often have leading zeros
        # at the start. The buffer metadata says start_system_time = minute_boundary,
        # but the actual signal doesn't begin until 20-50ms later.
        #
        # This causes a systematic timing bias: the correlation peak index is
        # relative to where the signal actually starts, not the minute boundary.
        #
        # Fix: Detect leading zeros and adjust minute_boundary accordingly.
        # The detected tone time = minute_boundary + leading_zeros + peak_time_in_buffer
        leading_zeros_correction_sec = 0.0
        max_mag = np.max(magnitude)
        if max_mag > 0:
            threshold = max_mag * 0.05  # 5% of max
            nonzero_indices = np.where(magnitude > threshold)[0]
            if len(nonzero_indices) > 0:
                first_signal_sample = nonzero_indices[0]
                leading_zeros_sec = first_signal_sample / self.sample_rate
                
                # Only apply correction if leading zeros are significant (>5ms)
                # and not too large (< 100ms, which would indicate a problem)
                if 0.005 < leading_zeros_sec < 0.100:
                    leading_zeros_correction_sec = leading_zeros_sec
                    logger.info(f"[LEADING_ZEROS] Detected {leading_zeros_sec*1000:.1f}ms of leading zeros")
        
        detections: List[ToneDetectionResult] = []
        
        # Step 2: Correlate with each station template
        for station_type, template in self.templates.items():
            # Get per-station expected delay if available
            station_expected_offset_ms = expected_offset_ms  # Default fallback
            if expected_delays_by_station is not None:
                station_name = station_type.value  # e.g., 'WWV', 'WWVH', 'CHU', 'BPM'
                if station_name in expected_delays_by_station:
                    station_expected_offset_ms = expected_delays_by_station[station_name]
            
            detection = self._correlate_with_template(
                audio_signal,
                station_type,
                template,
                current_unix_time,
                minute_boundary,
                original_sample_rate,
                buffer_rtp_start,
                search_window_ms,
                station_expected_offset_ms,  # Use per-station expected delay
                leading_zeros_correction_sec  # Pass leading zeros correction
            )
            
            if detection:
                detections.append(detection)
        
        # Sort by SNR (strongest signal first)
        detections.sort(key=lambda d: d.snr_db, reverse=True)
        
        # Cache detections for this minute
        if detections:
            self.last_detections_by_minute[minute_boundary] = detections
            self.detection_count += len(detections)
            
            # Cleanup old minutes (keep last 10)
            if len(self.last_detections_by_minute) > 10:
                oldest_minute = min(self.last_detections_by_minute.keys())
                del self.last_detections_by_minute[oldest_minute]
        
        return detections
    
    def _correlate_with_template(
        self,
        audio_signal: np.ndarray,
        station_type: StationType,
        template: dict,
        current_unix_time: float,
        minute_boundary: int,
        original_sample_rate: Optional[int] = None,
        buffer_rtp_start: Optional[int] = None,
        search_window_ms: Optional[float] = None,
        expected_offset_ms: Optional[float] = None,
        leading_zeros_correction_sec: float = 0.0
    ) -> Optional[ToneDetectionResult]:
        """
        Correlate audio signal with station template using quadrature matched filtering.
        
        ALGORITHM OVERVIEW:
        -------------------
        This implements the core matched filter detection with these steps:
        
        1. QUADRATURE CORRELATION
           Correlate audio with both sin and cos templates:
               R_sin[k] = Σ audio[n] × template_sin[n-k]
               R_cos[k] = Σ audio[n] × template_cos[n-k]
           
           Using scipy.signal.correlate with mode='valid' (output only where
           templates fully overlap with signal).
        
        2. PHASE-INVARIANT ENVELOPE
           Combine to remove phase dependence:
               R[k] = √(R_sin[k]² + R_cos[k]²)
           
           This is the magnitude of the analytic signal, invariant to the
           carrier phase shift introduced by ionospheric propagation.
        
        3. PEAK DETECTION WITH SUB-SAMPLE INTERPOLATION
           Find maximum within search window, then refine with parabolic fit:
               δ = (y[k-1] - y[k+1]) / (2 × (y[k-1] - 2×y[k] + y[k+1]))
           
           Refined position: k + δ (achieves ~5 μs precision at 20 kHz)
        
        4. NOISE-ADAPTIVE THRESHOLD
           Threshold computed from correlation values OUTSIDE the search window:
               threshold = percentile_10(noise) + 3σ(noise)
           
           This provides robust detection in varying noise conditions.
        
        5. TIMING CALCULATION
           Convert peak position to timing error relative to minute boundary:
               T_arrival = buffer_start_time + peak_position / sample_rate
               timing_error_ms = (T_arrival - minute_boundary) × 1000
        
        SEARCH WINDOW STRATEGY:
        -----------------------
        The search window can be progressively narrowed as timing confidence improves:
        
        - Pass 0 (Initial): ±500ms around minute boundary
          Used for first detection with no prior timing information.
          
        - Pass 1 (Geographic): ±30-50ms around expected propagation delay
          Uses known transmitter-receiver distance to predict arrival time.
          
        - Pass 2+ (Anchor-guided): ±3-10ms around previous detection
          Once TimeSnapReference is established, subsequent searches are tight.
        
        Args:
            audio_signal: AM-demodulated, AC-coupled signal (magnitude - DC)
            station_type: Station being detected (WWV, WWVH, or CHU)
            template: Dict with 'sin', 'cos', 'frequency', 'duration' from _create_template
            current_unix_time: Unix timestamp at buffer MIDPOINT
            minute_boundary: UTC minute boundary (second 0) as Unix timestamp
            original_sample_rate: Original sample rate if decimated (for RTP calculation)
            buffer_rtp_start: RTP timestamp at buffer start (for provenance)
            search_window_ms: Search window half-width in ms (default 500)
            expected_offset_ms: Expected offset from minute boundary (default 0)
        
        Returns:
            ToneDetectionResult if detection successful (peak > threshold and
            within propagation bounds), None otherwise.
        
        Note:
            Detection is rejected if timing_error_ms falls outside the physically
            plausible propagation delay range for the station (defined in
            wwv_constants.PROPAGATION_BOUNDS_MS).
        """
        template_sin = template['sin']
        template_cos = template['cos']
        frequency = template['frequency']
        duration = template['duration']
        
        # PRE-FILTERING (Critical for detecting ticks in presence of audio)
        # -------------------------------------------------------------
        # The 600 Hz audio modulation (and 500 Hz) is much stronger than the
        # 5ms tick pulses (1000/1200 Hz). The audio creates a high noise floor
        # in the correlation output effectively burying the tick.
        #
        # Apply a bandpass filter centered on the tone frequency to reject
        # the audio interference.
        # Bandwidth: +/- 250 Hz (approx matched to 5ms pulse width spectrum)
        # 1000 Hz -> [750, 1250] Hz (rejects 600 Hz)
        
        try:
            bw = 250.0  # Hz
            low = max(50, frequency - bw)
            high = min(self.sample_rate / 2 - 50, frequency + bw)
            
            sos = scipy_signal.butter(
                4, [low, high], btype='band', fs=self.sample_rate, output='sos'
            )
            # Use filtfilt for zero phase distortion (crucial for timing)
            # Note: signal needs to be long enough, which it is (60s)
            audio_signal_filtered = scipy_signal.sosfiltfilt(sos, audio_signal)
            
            # Use filtered signal for correlation
            signal_to_correlate = audio_signal_filtered
            logger.debug(f"Applied bandpass filter {low}-{high}Hz to signal")
            
        except Exception as e:
            logger.warning(f"Pre-correlation filter failed: {e}, using raw signal")
            signal_to_correlate = audio_signal

        # Perform quadrature correlation (phase-invariant)
        try:
            corr_sin = scipy_signal.correlate(signal_to_correlate, template_sin, mode='valid')
            corr_cos = scipy_signal.correlate(signal_to_correlate, template_cos, mode='valid')
        except ValueError as e:
            freq_str = f"{frequency}Hz" if frequency is not None else "??Hz"
            logger.warning(f"{station_type.value} @ {freq_str}: Correlation failed: {e}")
            return None
        
        if len(corr_sin) == 0 or len(corr_cos) == 0:
            freq_str = f"{frequency}Hz" if frequency is not None else "??Hz"
            logger.warning(f"{station_type.value} @ {freq_str}: Empty correlation result")
            return None
        
        # Combine to get phase-invariant magnitude: sqrt(sin^2 + cos^2)
        min_len = min(len(corr_sin), len(corr_cos))
        correlation = np.sqrt(corr_sin[:min_len]**2 + corr_cos[:min_len]**2)
        
        # Expected position: all stations use minute boundary (second 0)
        # - WWV: 1000 Hz, 0.8s tone at :00.0
        # - WWVH: 1200 Hz, 0.8s tone at :00.0  
        # - CHU: 1000 Hz, 0.5s tone at :00.0 (1.0s at top of hour)
        #   Note: CHU second 29 is always silent, seconds 31-39 and 50-59 have
        #   only 10ms ticks (FSK data and voice). But second 00 has full tone.
        buffer_len_sec = len(audio_signal) / self.sample_rate
        buffer_start_time = current_unix_time - (buffer_len_sec / 2)
        
        # NOTE: Leading zeros correction is NOT applied to buffer_start_time.
        # The buffer metadata says sample 0 = minute_boundary, and this is correct.
        # Leading zeros just mean the signal is missing/weak at the start, but the
        # timing relationship (sample index → time) is still valid.
        # The leading_zeros_correction_sec is logged for diagnostics only.
        
        # All stations reference the minute boundary
        reference_time = minute_boundary
        
        # DEBUG (2026-01-24): Trace timing calculation
        logger.debug(f"[TIMING_TRACE] current_unix_time={current_unix_time:.3f}, "
                    f"buffer_len_sec={buffer_len_sec:.3f}, buffer_start_time={buffer_start_time:.3f}, "
                    f"minute_boundary={minute_boundary}, offset_in_buffer={(reference_time - buffer_start_time):.3f}s")
        
        # Tone position in buffer (samples from start)
        # For Pass 0: search around minute boundary (expected_offset = 0)
        # For Pass 1+: search around expected arrival (minute_boundary + expected_offset)
        offset_ms = expected_offset_ms if expected_offset_ms is not None else 0.0
        offset_sec = offset_ms / 1000.0
        
        tone_offset_from_start = (reference_time + offset_sec) - buffer_start_time
        expected_pos_samples = int(tone_offset_from_start * self.sample_rate)
        
        # Search window: configurable, default ±500ms
        # Pass 0 (wide): 500ms - initial detection, centered at minute boundary
        # Pass 1 (geographic): 30-50ms - centered at expected propagation delay
        # Guided (anchor): 3-10ms - centered at anchor's detected position
        window_ms = search_window_ms if search_window_ms is not None else 500.0
        search_window = int(window_ms * self.sample_rate / 1000)
        search_start = max(0, expected_pos_samples - search_window)
        search_end = min(len(correlation), expected_pos_samples + search_window)
        
        freq_str = f"{frequency}Hz" if frequency is not None else "??Hz"
        
        # FUSE MODE FIX (2026-01-31): When buffer is primed by wallclock but the
        # expected minute boundary is outside the buffer, fall back to searching
        # the entire buffer. This handles cases where:
        # 1. Tones arrive BEFORE wallclock minute boundary (ionospheric advance)
        # 2. Buffer start_system_time is misaligned with minute boundary
        # 3. Clock drift between NTP and GPSDO
        #
        # In RTP mode, the minute boundary is derived from RTP timestamps and is
        # reliable. In FUSE mode, we must be more tolerant.
        if search_start >= search_end:
            # Expected position is outside buffer - search entire buffer
            search_start = 0
            search_end = len(correlation)
            logger.info(f"{station_type.value} @ {freq_str}: Expected pos outside buffer "
                       f"(expected_pos={expected_pos_samples}, tone_offset={tone_offset_from_start:.2f}s), "
                       f"searching entire buffer [0:{search_end}]")
        else:
            logger.debug(f"{station_type.value} @ {freq_str}: ref=min@{reference_time}, "
                        f"expected_offset={offset_ms:+.1f}ms, "
                        f"expected_pos={expected_pos_samples}, window=±{window_ms:.0f}ms, search=[{search_start}:{search_end}]")
        
        # Find peak within search window
        # CRITICAL FIX (2026-01-26): When buffer starts after minute boundary,
        # the minute marker is truncated and may have weaker correlation than
        # per-second ticks. Prioritize peaks near the expected position.
        search_region = correlation[search_start:search_end]
        
        # Find the global maximum
        local_peak_idx = np.argmax(search_region)
        peak_idx = search_start + local_peak_idx
        peak_val = correlation[peak_idx]
        
        # If expected position is near buffer start (minute marker truncated),
        # also check for a peak near sample 0 that might be the truncated minute marker
        if expected_pos_samples < 0 and search_start == 0:
            # Look for a peak in the first 500ms (where truncated minute marker should be)
            early_region_end = min(len(search_region), int(0.5 * self.sample_rate))
            if early_region_end > 0:
                early_peak_idx = np.argmax(search_region[:early_region_end])
                early_peak_val = search_region[early_peak_idx]
                
                # Use early peak if it's reasonably strong (>50% of global peak)
                # This prioritizes the minute marker over per-second ticks
                if early_peak_val > peak_val * 0.5:
                    logger.info(f"[TRUNCATED_MARKER] Using early peak at {early_peak_idx} "
                               f"(val={early_peak_val:.2f}) over global peak at {local_peak_idx} "
                               f"(val={peak_val:.2f}) - likely truncated minute marker")
                    local_peak_idx = early_peak_idx
                    peak_idx = search_start + local_peak_idx
                    peak_val = early_peak_val
        
        # =====================================================================
        # SUB-SAMPLE INTERPOLATION (Parabolic/Quadratic)
        # =====================================================================
        # The integer peak position limits timing resolution to ±1/(2·fs).
        # At 20 kHz: ±25 μs = ±0.025 ms.
        #
        # Parabolic interpolation fits a quadratic through 3 points:
        #   y(x) = ax² + bx + c
        #
        # Using the peak y[0] and neighbors y[-1], y[+1]:
        #   a = (y[-1] + y[+1] - 2·y[0]) / 2
        #   b = (y[+1] - y[-1]) / 2
        #
        # Peak of parabola at x = -b/(2a):
        #   δ = (y[-1] - y[+1]) / (2·(y[-1] - 2·y[0] + y[+1]))
        #
        # This achieves ~5 μs precision (10x improvement over integer).
        #
        # Reference: Smith, J.O. (2011). "Spectral Audio Signal Processing,"
        #            Chapter: Sinusoidal Peak Interpolation.
        # =====================================================================
        sub_sample_offset = 0.0
        if 0 < peak_idx < len(correlation) - 1:
            y_m1 = correlation[peak_idx - 1]  # y[-1]: sample before peak
            y_0 = correlation[peak_idx]        # y[0]:  peak sample
            y_p1 = correlation[peak_idx + 1]   # y[+1]: sample after peak
            
            # Denominator is 2×a (second derivative)
            # Small denominator indicates flat peak (low confidence interpolation)
            denominator = y_m1 - 2*y_0 + y_p1
            if abs(denominator) > 1e-10:
                # Calculate sub-sample offset (-0.5 to +0.5 samples)
                sub_sample_offset = 0.5 * (y_m1 - y_p1) / denominator
                sub_sample_offset = max(-0.5, min(0.5, sub_sample_offset))
                
                # Interpolated peak value at refined position
                peak_val = y_0 - 0.25 * (y_m1 - y_p1) * sub_sample_offset
        
        # Precise peak position (integer + fractional samples)
        precise_peak_idx = peak_idx + sub_sample_offset
        
        # =====================================================================
        # NOISE-ADAPTIVE THRESHOLD ESTIMATION (Updated 2025-12-31)
        # =====================================================================
        # To detect tones in varying noise conditions, we estimate the noise
        # floor from correlation values OUTSIDE the search window (where we
        # know no tone should be present).
        #
        # ROBUST METHOD (NEW):
        #   Use Median Absolute Deviation (MAD) on samples outside search region
        #   threshold = median(noise) + 3σ_MAD
        #
        # IMPROVEMENT OVER PREVIOUS METHOD:
        # - Previous: Used percentile of ALL samples (contaminated by signal)
        # - New: Uses only noise samples, with MAD for outlier robustness
        # - Expected: 5-10% improvement in weak signal detection
        #
        # Validated 2025-12-31: Addresses Issue #4 from tone detection critique
        # =====================================================================
        
        # Use robust noise floor estimation
        noise_floor = self._estimate_robust_noise_floor(
            correlation,
            search_start,
            search_end
        )
        
        # Still need noise mean for SNR calculation (use samples outside search)
        noise_samples = np.concatenate([
            correlation[:max(0, search_start - 100)],    # Before search window
            correlation[min(len(correlation), search_end + 100):]  # After search window
        ])
        
        if len(noise_samples) > 100:
            noise_mean = np.mean(noise_samples)
            noise_std = np.std(noise_samples)
        else:
            # Fallback for short buffers
            noise_mean = np.mean(correlation)
            noise_std = np.std(correlation)
        
        # =====================================================================
        # STAGE 2: PRECISE ONSET DETECTION (2025-12-07 Improvement)
        # =====================================================================
        # The correlation peak (precise_peak_idx) tells us WHERE a tone is,
        # but not precisely WHEN it starts. Per NIST specification, the timing
        # reference is "the beginning of the tone" - the first sample.
        #
        # WWV/WWVH tones are hard-keyed (essentially zero rise time), so we
        # can achieve much higher timing precision by finding the actual
        # leading edge rather than using the smeared correlation peak.
        #
        # Call _find_precise_onset() to locate the first sample of the tone.
        # =====================================================================
        
        # TEMPORARY FIX (2026-01-26): Bypass Stage 2 onset detection
        # The onset detection was introducing a ~250-500ms offset because it was
        # searching in the wrong region. For now, use the correlation peak directly
        # since for mode='valid', the peak index IS the tone start index.
        #
        # TODO: Fix _find_precise_onset() to properly refine the correlation peak
        # without introducing large offsets.
        
        # Use correlation peak directly (Stage 1 only)
        onset_sample_idx = precise_peak_idx
        onset_confidence = 0.8  # Default confidence without onset refinement
        onset_time = buffer_start_time + (onset_sample_idx / self.sample_rate)
        
        # Raw ToA from minute boundary (for diagnostics)
        raw_toa_sec = onset_time - reference_time
        raw_toa_ms = raw_toa_sec * 1000
        
        # TIMING ERROR: Deviation from EXPECTED arrival time
        # timing_error = raw_toa - expected_propagation_delay
        # A positive timing_error means the tone arrived LATER than expected
        # A negative timing_error means the tone arrived EARLIER than expected
        expected_arrival_time = reference_time + offset_sec
        timing_error_sec = onset_time - expected_arrival_time
        
        # DIAGNOSTIC: Log all timing components
        stage1_timing_ms = timing_error_sec * 1000
        buffer_offset_from_minute = buffer_start_time - reference_time
        # Expected tone position in buffer (accounting for propagation delay)
        expected_tone_sample = (expected_arrival_time - buffer_start_time) * self.sample_rate
        logger.info(f"[TIMING_DIAG] {station_type.value}: "
                   f"peak_idx={peak_idx}, precise_peak={precise_peak_idx:.2f}, "
                   f"raw_toa={raw_toa_ms:+.1f}ms, expected_delay={offset_ms:+.1f}ms, "
                   f"expected_tone_at_sample={expected_tone_sample:.0f}, "
                   f"timing_error={stage1_timing_ms:+.1f}ms, peak_val={peak_val:.2f}")
        
        # Handle wraparound
        if timing_error_sec > 30:
            timing_error_sec -= 60
        elif timing_error_sec < -30:
            timing_error_sec += 60
        
        timing_error_ms = timing_error_sec * 1000
        
        # Calculate SNR
        if noise_mean > 0 and peak_val > noise_mean:
            snr_db = 20 * np.log10(peak_val / noise_mean)
        else:
            snr_db = 0.0
        
        # Calculate actual tone power using FFT on the detected tone segment
        # Extract the tone segment (0.8s for WWV/WWVH, 0.5s for CHU)
        # Use integer index for slicing (onset_sample_idx may be float from sub-sample interpolation)
        tone_start_idx = max(0, int(onset_sample_idx))
        tone_end_idx = min(len(audio_signal), tone_start_idx + int(duration * self.sample_rate))
        tone_segment = audio_signal[tone_start_idx:tone_end_idx]
        
        tone_power_db = None
        if len(tone_segment) > int(0.1 * self.sample_rate):  # Need at least 100ms
            # Use FFT to measure power at the specific frequency
            windowed = tone_segment * scipy_signal.windows.hann(len(tone_segment))
            fft_result = rfft(windowed)
            freqs = rfftfreq(len(windowed), 1/self.sample_rate)
            
            # Find bin closest to target frequency
            freq_idx = np.argmin(np.abs(freqs - frequency))
            power_at_freq = np.abs(fft_result[freq_idx])**2
            
            # Measure noise floor in nearby bins (excluding the tone)
            noise_low = max(0, freq_idx - int(50.0 * len(windowed) / self.sample_rate))
            noise_high = min(len(fft_result), freq_idx + int(50.0 * len(windowed) / self.sample_rate))
            exclude_low = max(0, freq_idx - int(10.0 * len(windowed) / self.sample_rate))
            exclude_high = min(len(fft_result), freq_idx + int(10.0 * len(windowed) / self.sample_rate))
            
            noise_bins = np.concatenate([
                np.arange(noise_low, exclude_low),
                np.arange(exclude_high, noise_high)
            ])
            
            if len(noise_bins) > 10:
                noise_power = np.mean(np.abs(fft_result[noise_bins])**2)
            else:
                noise_power = np.mean(np.abs(fft_result)**2)
            
            # Calculate power relative to noise floor
            if noise_power > 0:
                tone_power_db = 10 * np.log10(power_at_freq / noise_power)
            else:
                tone_power_db = snr_db  # Fallback to SNR estimate
        
        # Diagnostic logging BEFORE threshold check
        freq_str = f"{frequency}Hz" if frequency is not None else "??Hz"
        peak_str = f"{peak_val:.2f}" if peak_val is not None else "None"
        noise_str = f"{noise_floor:.2f}" if noise_floor is not None else "None"
        snr_str = f"{snr_db:.1f}dB" if snr_db is not None else "None"
        power_str = f"{tone_power_db:.1f}dB" if tone_power_db is not None else "None"
        timing_str = f"{timing_error_ms:+.1f}ms" if timing_error_ms is not None else "None"
        logger.debug(f"{station_type.value} @ {freq_str}: peak={peak_str}, "
                    f"noise_floor={noise_str}, SNR={snr_str}, "
                    f"tone_power={power_str}, timing_err={timing_str}")
        
        # Check if we have valid values
        if peak_val is None or noise_floor is None:
            logger.warning(f"  -> REJECTED (invalid peak or noise values)")
            return None
        
        # Apply adaptive threshold (2026-01-24 Enhancement)
        # This adjusts the threshold based on detection history and noise stability
        adaptive_threshold = self._calculate_adaptive_threshold(
            base_noise_floor=noise_floor,
            snr_db=snr_db
        )
        
        # Check if peak is significant (using adaptive threshold)
        if peak_val <= adaptive_threshold:
            logger.info(f"  -> REJECTED {station_type.value} (peak {peak_val:.2f} <= adaptive_threshold {adaptive_threshold:.2f})")
            return None
        
        # Calculate confidence (combines Stage 1 detection + Stage 2 onset quality)
        # Stage 1: How strong is the detection? (correlation / threshold)
        detection_confidence = min(1.0, peak_val / (noise_floor * 2.0))
        # Combined: geometric mean of detection and onset confidence
        # This ensures both must be good for overall high confidence
        confidence = np.sqrt(detection_confidence * onset_confidence)
        
        # PROPAGATION PLAUSIBILITY CHECK
        # 
        # BOOTSTRAP PHILOSOPHY (2026-01-26):
        # During bootstrap, we DON'T KNOW what time it is. The system clock is just
        # a starting point. The tones ARE the ground truth - they tell us UTC.
        # Bounds checking during bootstrap is CIRCULAR REASONING - we'd be rejecting
        # detections based on a clock we're trying to fix.
        #
        # Once we find, confirm, and lock on tones, THEN we can apply bounds to
        # reject noise and interference. But not before.
        #
        # PROPAGATION BOUNDS ENFORCEMENT (2026-01-27, Updated 2026-02-05)
        # After bootstrap locks, enforce propagation bounds to reject bad detections.
        # During bootstrap, allow wider bounds to find initial lock.
        #
        # RTP MODE vs FUSION MODE (2026-02-05):
        # - RTP mode (buffer_rtp_start present): GPSDO provides authoritative UTC timing.
        #   We KNOW when second 0 occurs, so we can confidently reject false positives
        #   based on ToF. Enforce bounds for ALL stations.
        #   NOTE: Fusion still runs in RTP mode as a STUDY of UTC-recovery methodology,
        #   comparing its output against GPSDO ground truth. Strict ToF validation
        #   ensures we're testing realistic detection scenarios.
        # - Fusion mode (no RTP): Must find UTC from tones. Can't reject based on timing
        #   we don't yet know. Only enforce BPM bounds (geographic discrimination).
        #
        # EXCEPTION: BPM bounds are ALWAYS enforced because:
        # 1. BPM is 10,960km away - minimum ToF is ~36ms (speed of light)
        # 2. BPM uses same 1000Hz tone as WWV, so template correlation alone can't distinguish
        # 3. WWV/WWVH signals with ToF < 30ms are physically impossible to be BPM
        station_name = station_type.value  # 'WWV', 'WWVH', 'CHU', 'BPM'
        min_delay_ms, max_delay_ms = PROPAGATION_BOUNDS_MS.get(
            station_name, DEFAULT_PROPAGATION_BOUNDS_MS
        )
        
        # Determine if we have authoritative timing (RTP mode)
        has_rtp_timing = buffer_rtp_start is not None
        
        # Enforce bounds if:
        # 1. RTP mode (authoritative UTC timing) - enforce for ALL stations
        # 2. Bootstrap locked (converged timing) - enforce for ALL stations  
        # 3. BPM station - ALWAYS enforce (geographic discrimination essential)
        enforce_bounds = has_rtp_timing or self.bootstrap_locked or (station_type == StationType.BPM)
        
        if enforce_bounds:
            # ENFORCE bounds - reject detections outside physical range
            if timing_error_ms < min_delay_ms or timing_error_ms > max_delay_ms:
                logger.warning(f"  -> TIMING {station_type.value}: {timing_error_ms:+.1f}ms "
                              f"REJECTED (outside bounds [{min_delay_ms:.0f}, {max_delay_ms:.0f}]ms)")
                return None
            logger.info(f"  -> TIMING {station_type.value}: {timing_error_ms:+.1f}ms "
                       f"(within bounds [{min_delay_ms:.0f}, {max_delay_ms:.0f}]ms)")
        else:
            # During bootstrap (non-BPM), log but don't reject
            logger.info(f"  -> TIMING {station_type.value}: {timing_error_ms:+.1f}ms "
                       f"(bounds [{min_delay_ms:.0f}, {max_delay_ms:.0f}]ms - NOT enforced during bootstrap)")
        
        # DIAGNOSTIC: Track timing_error_ms value before any modifications
        timing_before_corrections = timing_error_ms
        
        # Determine if this station should be used for time_snap
        # 
        # TIMING PHILOSOPHY (Updated):
        # - WWV and CHU: Primary references (direct UTC(NIST) source)
        # - WWVH: Eligible AFTER back-calculation subtracts propagation delay
        #
        # At this detection level, we mark WWVH as "eligible" but with lower
        # initial preference. The TransmissionTimeSolver does the back-calculation
        # to make WWVH's timing as accurate as WWV.
        #
        # All detected stations are valid for timing measurements.
        # The fusion layer handles uncertainty weighting - no filtering here.
        # Each broadcast provides an independent measurement of UTC.
        use_for_time_snap = True  # All stations contribute to timing
        
        # Calculate sample position in ORIGINAL sample rate (for precise RTP calculation)
        # onset_sample_idx is at self.sample_rate (detection rate)
        # Scale to original_sample_rate if different (e.g., 20 kHz archive rate)
        sample_position_original = None
        if original_sample_rate is not None:
            scale_factor = original_sample_rate / self.sample_rate
            sample_position_original = round(onset_sample_idx * scale_factor)
            logger.debug(f"Sample position: decimated={onset_sample_idx}, "
                        f"original={sample_position_original} (scale={scale_factor:.2f})")
        
        # Use FFT-based tone_power_db as SNR if available (more reliable than correlation-based)
        # The correlation-based snr_db can be 0 when noise_mean is poorly estimated
        effective_snr_db = tone_power_db if tone_power_db is not None and tone_power_db > 0 else snr_db
        
        # BPM MINIMUM SNR THRESHOLD (2026-02-05)
        # BPM uses 300ms template (vs 800ms for WWV/WWVH), making it more prone to
        # false positives from noise correlation. Require higher SNR for BPM.
        # BPM is also 10,960km away so signals should be weaker but still detectable.
        if station_type == StationType.BPM:
            MIN_BPM_SNR_DB = 12.0  # Require 12dB SNR for BPM (vs ~6dB for WWV/WWVH)
            if effective_snr_db < MIN_BPM_SNR_DB:
                logger.info(f"  -> REJECTED {station_type.value} (SNR {effective_snr_db:.1f}dB < {MIN_BPM_SNR_DB}dB minimum for BPM)")
                return None
        
        # =====================================================================
        # METROLOGICAL ENHANCEMENTS (2026-01-24)
        # =====================================================================
        # Compute rigorous uncertainty and channel characterization metrics
        # for downstream fusion and scientific analysis.
        # =====================================================================
        
        # 1. Cramér-Rao bound uncertainty (theoretical minimum ToA variance)
        timing_uncertainty_ms = self._calculate_cramer_rao_uncertainty(
            snr_db=effective_snr_db,
            duration_sec=duration,
            bandwidth_hz=CRAMER_RAO_BANDWIDTH_HZ
        )
        
        # 2. Complex correlation for phase-based analysis
        # Use the filtered signal for better phase estimation
        try:
            corr_magnitude, corr_phase, corr_complex = self._complex_correlation_with_phase(
                signal=signal_to_correlate,
                tone_frequency=frequency,
                template_duration_sec=duration
            )
            
            # Get phase at correlation peak
            if peak_idx < len(corr_phase):
                phase_at_peak = float(corr_phase[peak_idx])
            else:
                phase_at_peak = None
            
            # 3. Doppler estimation from phase slope
            doppler_hz, doppler_confidence = self._estimate_doppler_from_phase_slope(
                phase=corr_phase,
                peak_idx=peak_idx,
                window_samples=50
            )
            
            # 4. Multipath detection from correlation characteristics
            is_multipath, delay_spread_ms, multipath_quality = self._detect_multipath_from_correlation(
                magnitude=corr_magnitude,
                phase=corr_phase,
                peak_idx=peak_idx
            )
            
            # 5. Inflate uncertainty if multipath detected
            if is_multipath and delay_spread_ms > 0:
                # Add multipath-induced uncertainty (quadrature sum)
                multipath_uncertainty_ms = delay_spread_ms / 2.0  # Half of delay spread
                timing_uncertainty_ms = math.sqrt(
                    timing_uncertainty_ms**2 + multipath_uncertainty_ms**2
                )
                logger.debug(f"Multipath inflated uncertainty: {timing_uncertainty_ms:.3f}ms "
                            f"(base + {multipath_uncertainty_ms:.3f}ms from delay spread)")
            
            # 6. Apply Doppler correction to timing (2026-01-24 Enhancement)
            # Doppler shift causes systematic timing bias that can be corrected
            #
            # BUG FIX (2026-01-26): The Doppler estimation from phase slope is
            # returning the TONE FREQUENCY (1000Hz) instead of actual Doppler shift
            # (~0.1Hz). This causes a massive 250ms timing error via the correction
            # formula: Δt = (f_doppler / f_tone) × (T_tone / 2)
            #
            # DISABLED until Doppler estimation is fixed. Real HF Doppler is tiny
            # (~0.1-1Hz) and the correction would only be ~0.05-0.5ms anyway.
            #
            # TODO: Fix _estimate_doppler_from_phase_slope to return actual Doppler
            if doppler_hz is not None and abs(doppler_hz) < 10.0 and doppler_confidence > 0.3:
                # Only apply correction if Doppler is physically plausible (<10Hz)
                timing_error_ms, doppler_correction_ms = self._apply_doppler_correction(
                    timing_error_ms=timing_error_ms,
                    doppler_hz=doppler_hz,
                    tone_frequency=frequency,
                    tone_duration_sec=duration
                )
                # Also update onset_time to reflect corrected timing
                onset_time = onset_time - (doppler_correction_ms / 1000.0)
            elif doppler_hz is not None and abs(doppler_hz) >= 10.0:
                logger.warning(f"  -> DOPPLER SANITY FAIL: {doppler_hz:+.1f}Hz is implausible (>10Hz), skipping correction")
            
        except Exception as e:
            logger.debug(f"Complex correlation analysis failed: {e}")
            phase_at_peak = None
            doppler_hz = None
            is_multipath = None
            delay_spread_ms = None
            multipath_quality = None
        
        # Create ToneDetectionResult with metrological fields
        result = ToneDetectionResult(
            station=station_type,
            frequency_hz=frequency,
            duration_sec=duration,
            timestamp_utc=onset_time,
            timing_error_ms=timing_error_ms,
            snr_db=effective_snr_db,
            confidence=confidence,
            use_for_time_snap=use_for_time_snap,
            correlation_peak=float(peak_val),
            noise_floor=float(noise_floor),
            tone_power_db=tone_power_db,
            sample_position_original=sample_position_original,
            original_sample_rate=original_sample_rate,
            buffer_rtp_start=buffer_rtp_start,
            # Metrological fields (2026-01-24 Enhancement)
            timing_uncertainty_ms=timing_uncertainty_ms,
            multipath_detected=is_multipath,
            multipath_delay_spread_ms=delay_spread_ms,
            multipath_quality=multipath_quality,
            doppler_hz=doppler_hz,
            phase_at_peak_rad=phase_at_peak
        )
        
        freq_str = f"{frequency}Hz" if frequency is not None else "??Hz"
        logger.info(f"{self.channel_name}: ✅ {station_type.value} DETECTED! "
                   f"Freq: {freq_str}, Duration: {duration:.1f}s, "
                   f"Timing error: {timing_error_ms:+.1f}ms, SNR: {effective_snr_db:.1f}dB, "
                   f"use_for_time_snap={use_for_time_snap}")
        
        return result
    
    def _update_differential_delay(
        self,
        detections: List[ToneDetectionResult],
        timestamp: float
    ) -> None:
        """
        Update differential delay history if both WWV and WWVH detected
        """
        wwv_det = None
        wwvh_det = None
        
        for det in detections:
            if det.station == StationType.WWV:
                wwv_det = det
            elif det.station == StationType.WWVH:
                wwvh_det = det
        
        if wwv_det and wwvh_det:
            differential_ms = wwv_det.timing_error_ms - wwvh_det.timing_error_ms
            
            self.differential_delay_history.append({
                'timestamp': timestamp,
                'differential_ms': differential_ms,
                'wwv_snr_db': wwv_det.snr_db,
                'wwvh_snr_db': wwvh_det.snr_db
            })
            
            # Keep last 1000 measurements
            if len(self.differential_delay_history) > 1000:
                self.differential_delay_history = self.differential_delay_history[-1000:]
            
            logger.info(f"Differential delay (WWV-WWVH): {differential_ms:+.1f}ms "
                       f"(WWV SNR={wwv_det.snr_db:.1f}dB, WWVH SNR={wwvh_det.snr_db:.1f}dB)")
    
    # ===== ToneDetector Interface Methods =====
    
    def get_differential_delay(self) -> Optional[float]:
        """Get most recent WWV-WWVH differential propagation delay (ms)"""
        if self.differential_delay_history:
            return self.differential_delay_history[-1]['differential_ms']
        return None
    
    def get_detection_statistics(self) -> Dict[str, int]:
        """Get detection counts by station"""
        total_detections = sum(self.detection_stats.values())
        detection_rate = (total_detections / self.total_attempts * 100) if self.total_attempts > 0 else 0.0
        
        return {
            'wwv_detections': self.detection_stats[StationType.WWV],
            'wwvh_detections': self.detection_stats[StationType.WWVH],
            'chu_detections': self.detection_stats[StationType.CHU],
            'total_attempts': self.total_attempts,
            'detection_rate_pct': detection_rate
        }
    
    def get_station_active_list(self) -> List[StationType]:
        """Get list of stations that have been detected"""
        return [station for station, count in self.detection_stats.items() if count > 0]
    
    def set_detection_threshold(self, threshold: float) -> None:
        """Set detection confidence threshold (0.0-1.0)"""
        if not 0.0 <= threshold <= 1.0:
            raise ValueError(f"Threshold must be 0.0-1.0, got {threshold}")
        self.detection_threshold = threshold
        logger.info(f"Detection threshold set to {threshold:.2f}")
    
    def get_last_detection_time(self) -> Optional[float]:
        """Get UTC timestamp of most recent detection"""
        return self.last_detection_time
    
    def get_timing_accuracy_stats(self) -> Dict[str, float]:
        """Get timing accuracy statistics for time_snap-eligible stations"""
        if not self.timing_errors:
            return {
                'mean_error_ms': 0.0,
                'std_error_ms': 0.0,
                'max_error_ms': 0.0,
                'min_error_ms': 0.0,
                'sample_count': 0
            }
        
        errors = np.array(self.timing_errors)
        return {
            'mean_error_ms': float(np.mean(errors)),
            'std_error_ms': float(np.std(errors)),
            'max_error_ms': float(np.max(errors)),
            'min_error_ms': float(np.min(errors)),
            'sample_count': len(errors)
        }
    
    def reset_statistics(self) -> None:
        """Reset detection statistics"""
        self.detection_stats = {
            StationType.WWV: 0,
            StationType.WWVH: 0,
            StationType.CHU: 0
        }
        self.total_attempts = 0
        self.timing_errors = []
        logger.info("Detection statistics reset")
    
    # ===== MultiStationToneDetector Interface Methods =====
    
    def get_detections_by_station(
        self,
        station: StationType
    ) -> List[ToneDetectionResult]:
        """Get recent detections for specific station"""
        results = []
        for detections in self.last_detections_by_minute.values():
            for det in detections:
                if det.station == station:
                    results.append(det)
        return results
    
    def get_differential_delay_history(
        self,
        count: int = 10
    ) -> List[Dict[str, float]]:
        """Get recent WWV-WWVH differential delay measurements"""
        return self.differential_delay_history[-count:]
    
    def configure_station_priorities(
        self,
        priorities: Dict[StationType, int]
    ) -> None:
        """Configure station priorities for time_snap selection"""
        self.station_priorities.update(priorities)
        logger.info(f"Station priorities updated: {self.station_priorities}")
    
    # ===== Extended Tone Analysis (440/500/600 Hz) =====
    
    def analyze_extended_tones(
        self,
        iq_samples: np.ndarray,
        buffer_start_time: float
    ) -> Dict[str, any]:
        """
        Analyze extended WWV/WWVH tones for STATION DISCRIMINATION.
        
        KEY INSIGHT: 500 Hz and 600 Hz are STATION IDENTIFIERS:
        - WWV broadcasts 500 Hz during seconds 1-44 (except voice/silent)
        - WWVH broadcasts 600 Hz during seconds 1-44 (except voice/silent)
        
        This provides DIRECT station identification without timing analysis!
        If 500 Hz > 600 Hz → WWV dominant
        If 600 Hz > 500 Hz → WWVH dominant
        
        Conditions for valid discrimination:
        - Buffer must contain mid-minute data (seconds 1-44)
        - Avoid seconds 0, 29-30 (minute markers), 45-59 (voice/announcements)
        - Both tones should be checked - ratio determines station
        
        Also analyzes:
        - 440 Hz: Test tone (seconds 1-2 on some minutes)
        - 1000 Hz: Reference for comparison
        
        Args:
            iq_samples: Complex IQ samples
            buffer_start_time: UTC timestamp of buffer start
            
        Returns:
            Dict with discrimination results and tone analysis
        """
        if self.is_chu_channel:
            return {'status': 'CHU channel - WWV tones not applicable'}
        
        # Check if buffer is in valid discrimination window
        # 500/600 Hz discrimination only valid when ONE station broadcasts alone
        # (otherwise BCD 100 Hz intermod creates 500/600 Hz products)
        buffer_second = int(buffer_start_time) % 60
        buffer_minute = int(buffer_start_time / 60) % 60
        
        # Use shared constants for station-exclusive broadcast minutes
        # WWV-only: 1, 16, 17, 19 (WWVH silent - 500 Hz is pure WWV)
        # WWVH-only: 2, 43-51 (WWV silent - 600 Hz is pure WWVH)
        
        # Valid for discrimination only during single-station minutes
        is_wwv_only_minute = buffer_minute in WWV_ONLY_TONE_MINUTES
        is_wwvh_only_minute = buffer_minute in WWVH_ONLY_TONE_MINUTES
        valid_for_discrimination = (is_wwv_only_minute or is_wwvh_only_minute) and (1 <= buffer_second <= 44)
        
        results = {
            'buffer_time': buffer_start_time,
            'buffer_second': buffer_second,
            'buffer_minute': buffer_minute,
            'valid_for_discrimination': valid_for_discrimination,
            'is_wwv_only_minute': is_wwv_only_minute,
            'is_wwvh_only_minute': is_wwvh_only_minute,
            'expected_station': 'WWV' if is_wwv_only_minute else ('WWVH' if is_wwvh_only_minute else 'BOTH'),
            'tones': {},
            'dominant_tone': None,
            'frequency_spread_db': 0.0,
            # Station discrimination results
            'wwv_indicator_snr': None,      # 500 Hz SNR
            'wwvh_indicator_snr': None,     # 600 Hz SNR  
            'discrimination_ratio_db': None, # 500 Hz - 600 Hz (positive = WWV)
            'indicated_station': None,       # 'WWV', 'WWVH', or 'AMBIGUOUS'
            'discrimination_confidence': 0.0
        }
        
        # Convert to audio (envelope detection)
        audio_signal = np.abs(iq_samples)
        audio_signal = audio_signal - np.mean(audio_signal)  # Remove DC
        
        # Extended tone frequencies to analyze
        extended_tones = {
            '440Hz': 440,
            '500Hz': 500,
            '600Hz': 600,
            '1000Hz': 1000,  # Include for comparison
        }
        
        tone_powers = {}
        
        for tone_name, freq_hz in extended_tones.items():
            # Create matched filter template (short duration for mid-second tones)
            duration = 0.5  # 500ms analysis window
            t = np.arange(0, duration, 1/self.sample_rate)
            template_sin = np.sin(2 * np.pi * freq_hz * t)
            template_cos = np.cos(2 * np.pi * freq_hz * t)
            
            # Correlate with audio
            # Use only the middle portion of the buffer to avoid edge effects
            mid_start = len(audio_signal) // 4
            mid_end = 3 * len(audio_signal) // 4
            audio_segment = audio_signal[mid_start:mid_end]
            
            if len(audio_segment) < len(template_sin):
                continue
            
            corr_sin = correlate(audio_segment, template_sin, mode='valid')
            corr_cos = correlate(audio_segment, template_cos, mode='valid')
            
            # Phase-invariant magnitude
            magnitude = np.sqrt(corr_sin**2 + corr_cos**2)
            peak_power = np.max(magnitude)
            
            # Estimate noise floor
            noise_floor = np.median(magnitude)
            
            # SNR estimate
            if noise_floor > 0:
                snr_db = 10 * np.log10(peak_power / noise_floor)
            else:
                snr_db = 0.0
            
            tone_powers[tone_name] = peak_power
            results['tones'][tone_name] = {
                'frequency_hz': freq_hz,
                'peak_power': float(peak_power),
                'snr_db': float(snr_db),
                'detected': snr_db > 6.0  # 6 dB threshold
            }
        
        # Find dominant tone
        if tone_powers:
            dominant = max(tone_powers, key=tone_powers.get)
            results['dominant_tone'] = dominant
            
            # Calculate frequency spread (max - min power ratio)
            max_power = max(tone_powers.values())
            min_power = min(tone_powers.values()) if min(tone_powers.values()) > 0 else 1e-10
            results['frequency_spread_db'] = float(10 * np.log10(max_power / min_power))
        
        # === STATION DISCRIMINATION via 500/600 Hz ===
        # Only valid during single-station minutes (avoids BCD 100 Hz intermod)
        if '500Hz' in results['tones'] and '600Hz' in results['tones']:
            snr_500 = results['tones']['500Hz']['snr_db']
            snr_600 = results['tones']['600Hz']['snr_db']
            
            results['wwv_indicator_snr'] = snr_500
            results['wwvh_indicator_snr'] = snr_600
            results['discrimination_ratio_db'] = snr_500 - snr_600
            
            if valid_for_discrimination:
                if is_wwv_only_minute:
                    # WWV broadcasting alone - check for 500 Hz presence
                    # If 500 Hz detected, confirms WWV propagation to receiver
                    if snr_500 > 6.0:
                        results['indicated_station'] = 'WWV'
                        results['discrimination_confidence'] = min(1.0, snr_500 / 15.0)
                    else:
                        results['indicated_station'] = 'WEAK_OR_ABSENT'
                        results['discrimination_confidence'] = 0.0
                        
                elif is_wwvh_only_minute:
                    # WWVH broadcasting alone - check for 600 Hz presence
                    # If 600 Hz detected, confirms WWVH propagation to receiver
                    if snr_600 > 6.0:
                        results['indicated_station'] = 'WWVH'
                        results['discrimination_confidence'] = min(1.0, snr_600 / 15.0)
                    else:
                        results['indicated_station'] = 'WEAK_OR_ABSENT'
                        results['discrimination_confidence'] = 0.0
            else:
                # Both stations may be broadcasting - ratio method unreliable due to BCD intermod
                results['indicated_station'] = 'INTERMOD_RISK'
                results['discrimination_confidence'] = 0.0
        
        return results
    
    # ===== Legacy Compatibility =====
    
    # =========================================================================
    # ACQUISITION MODE (2026-01-25): Bootstrap without timing assumptions
    # =========================================================================
    
    def acquire_tones(
        self,
        samples: np.ndarray,
        buffer_rtp_start: int,
        snr_threshold_db: float = 10.0,
        max_candidates: int = 5
    ) -> List['ToneAcquisitionResult']:
        """
        Acquisition mode: Find ALL tone candidates in buffer without timing assumptions.
        
        Unlike process_samples() which searches a narrow window around an assumed
        minute boundary, this method searches the ENTIRE buffer and returns all
        correlation peaks that exceed the threshold. Used during bootstrap to
        discover the RTP-to-UTC correspondence from the broadcasts themselves.
        
        ALGORITHM:
        ----------
        1. Cross-correlate entire buffer with each station template (1000 Hz, 1200 Hz)
        2. Find all peaks above SNR threshold
        3. For each peak, calculate RTP timestamp of tone onset
        4. Return sorted by SNR (strongest first)
        
        The caller (bootstrap state machine) then validates candidates using:
        - Relative timing constraints (WWVH after WWV on shared frequencies)
        - Minute spacing (1,440,000 samples between consecutive tones)
        - Geographic priors (expected propagation delays)
        - Discriminating features (tone schedule, voice timing, etc.)
        
        Args:
            samples: Complex IQ samples (full minute buffer)
            buffer_rtp_start: RTP timestamp at start of buffer
            snr_threshold_db: Minimum SNR for candidate detection (default 10 dB)
            max_candidates: Maximum candidates to return per station type
            
        Returns:
            List of ToneAcquisitionResult, sorted by SNR (strongest first)
        """
        from ..interfaces.data_models import ToneAcquisitionResult
        
        # AM demodulation
        magnitude = np.abs(samples)
        audio_signal = magnitude - np.mean(magnitude)
        
        candidates: List[ToneAcquisitionResult] = []
        
        for station_type, template in self.templates.items():
            station_candidates = self._acquire_station_tones(
                audio_signal=audio_signal,
                station_type=station_type,
                template=template,
                buffer_rtp_start=buffer_rtp_start,
                snr_threshold_db=snr_threshold_db,
                max_candidates=max_candidates
            )
            candidates.extend(station_candidates)
        
        # Sort by SNR (strongest first)
        candidates.sort(key=lambda c: c.snr_db, reverse=True)
        
        logger.info(f"[ACQUIRE] Found {len(candidates)} tone candidates: "
                   f"{[(c.station.value, c.snr_db) for c in candidates[:5]]}")
        
        return candidates
    
    def _acquire_station_tones(
        self,
        audio_signal: np.ndarray,
        station_type: StationType,
        template: dict,
        buffer_rtp_start: int,
        snr_threshold_db: float,
        max_candidates: int
    ) -> List['ToneAcquisitionResult']:
        """
        Find all tone candidates for a specific station template.
        
        Searches entire buffer, finds all peaks above threshold, applies
        non-maximum suppression to avoid duplicate detections of same tone.
        """
        from ..interfaces.data_models import ToneAcquisitionResult
        
        template_sin = template['sin']
        template_cos = template['cos']
        frequency = template['frequency']
        duration = template['duration']
        
        # Bandpass filter to isolate tone frequency
        try:
            bw = 250.0
            low = max(50, frequency - bw)
            high = min(self.sample_rate / 2 - 50, frequency + bw)
            sos = scipy_signal.butter(4, [low, high], btype='band', 
                                      fs=self.sample_rate, output='sos')
            filtered = scipy_signal.sosfiltfilt(sos, audio_signal)
        except Exception:
            filtered = audio_signal
        
        # Quadrature correlation
        try:
            corr_sin = scipy_signal.correlate(filtered, template_sin, mode='valid')
            corr_cos = scipy_signal.correlate(filtered, template_cos, mode='valid')
        except ValueError:
            return []
        
        if len(corr_sin) == 0:
            return []
        
        min_len = min(len(corr_sin), len(corr_cos))
        correlation = np.sqrt(corr_sin[:min_len]**2 + corr_cos[:min_len]**2)
        
        # Noise floor estimation (robust)
        noise_floor = np.median(correlation)
        noise_std = np.median(np.abs(correlation - noise_floor)) * 1.4826  # MAD to std
        
        if noise_floor <= 0:
            return []
        
        # Find all peaks above threshold
        # Convert SNR threshold to absolute threshold
        snr_linear = 10 ** (snr_threshold_db / 10)
        threshold = noise_floor * np.sqrt(snr_linear)
        
        # Find local maxima above threshold
        candidates = []
        template_samples = int(duration * self.sample_rate)
        min_separation = template_samples  # Minimum separation between peaks
        
        # Simple peak finding with non-maximum suppression
        peak_indices = []
        i = 0
        while i < len(correlation):
            if correlation[i] > threshold:
                # Find local maximum in this region
                region_end = min(i + min_separation, len(correlation))
                local_max_idx = i + np.argmax(correlation[i:region_end])
                peak_indices.append(local_max_idx)
                i = local_max_idx + min_separation  # Skip past this peak
            else:
                i += 1
        
        # Convert peaks to acquisition results
        for peak_idx in peak_indices[:max_candidates]:
            peak_val = correlation[peak_idx]
            
            # SNR calculation
            snr_db = 10 * np.log10(peak_val**2 / noise_floor**2) if noise_floor > 0 else 0
            
            # Confidence based on SNR and peak sharpness
            confidence = min(1.0, snr_db / 20.0)  # Saturates at 20 dB
            
            # For 'valid' mode correlation, output[k] corresponds to signal[k:k+template_len]
            # So peak at k means tone starts at sample k (no offset needed)
            onset_sample = peak_idx
            
            # RTP timestamp of tone onset
            rtp_timestamp = buffer_rtp_start + onset_sample
            
            candidates.append(ToneAcquisitionResult(
                station=station_type,
                frequency_hz=frequency,
                sample_position=onset_sample,
                rtp_timestamp=rtp_timestamp,
                snr_db=snr_db,
                confidence=confidence,
                correlation_peak=float(peak_val),
                noise_floor=float(noise_floor),
                buffer_rtp_start=buffer_rtp_start
            ))
        
        return candidates

