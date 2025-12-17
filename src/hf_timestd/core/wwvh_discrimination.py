#!/usr/bin/env python3
"""
WWV/WWVH Station Discrimination - Multi-Method Weighted Voting

================================================================================
PURPOSE
================================================================================
Distinguish between WWV (Fort Collins, CO) and WWVH (Kauai, HI) signals on
shared frequencies where both stations broadcast simultaneously.

SHARED FREQUENCIES: 2.5, 5, 10, 15 MHz
    - Both stations transmit identical time codes
    - Both use similar modulation formats
    - Signals arrive superimposed at the receiver
    - Without discrimination, timing is ambiguous

This is Goal #2 of Phase 2 analytics: establish the best possible distinction
between WWV and WWVH on shared frequencies.

================================================================================
THE DISCRIMINATION CHALLENGE
================================================================================
WWV and WWVH are intentionally similar for redundancy, making discrimination
difficult. Key differences we can exploit:

┌────────────────────┬──────────────────────┬──────────────────────┐
│ Characteristic     │ WWV (Colorado)       │ WWVH (Hawaii)        │
├────────────────────┼──────────────────────┼──────────────────────┤
│ Timing Tone        │ 1000 Hz, 0.8s        │ 1200 Hz, 0.8s        │
│ Voice Announcement │ Male voice           │ Female voice         │
│ 440 Hz Tone        │ Minute 2             │ Minute 1             │
│ 500/600 Hz Tone    │ Minutes 1,16,17,19   │ Minutes 2,43-51      │
│ Test Signal        │ Minute 8             │ Minute 44            │
│ BCD Phase          │ Leading edge         │ Lagging edge         │
│ Propagation Delay  │ Path-dependent       │ Path-dependent       │
└────────────────────┴──────────────────────┴──────────────────────┘

REFERENCE: NIST Special Publication 250-67 (2009). "NIST Time and Frequency
           Radio Stations: WWV, WWVH, and WWVB."

================================================================================
MULTI-METHOD DISCRIMINATION APPROACH
================================================================================
No single method provides reliable discrimination under all conditions.
We use WEIGHTED VOTING across multiple independent methods:

METHOD 1: 1000/1200 Hz Tone Power Ratio
-----------------------------------------
    power_ratio_db = P_1000Hz - P_1200Hz
    - Positive → WWV dominant
    - Negative → WWVH dominant
    - |ratio| < 3 dB → Balanced (both present)

    Weight: 10.0 (standard minutes), reduced when other methods available

METHOD 2: Differential Propagation Delay
-----------------------------------------
    Δτ = τ_WWV - τ_WWVH
    
    WWV and WWVH have different geographic locations, so their signals
    arrive at different times. This difference is receiver-location dependent.
    
    Weight: Used for cross-validation, not direct voting

METHOD 3: 440 Hz Tone (Minutes 1 & 2 only)
------------------------------------------
    - Minute 1: WWVH broadcasts 440 Hz
    - Minute 2: WWV broadcasts 440 Hz
    
    Detection provides DEFINITIVE identification during these minutes.
    
    Weight: 10.0 (highest in minutes 1/2)

METHOD 4: 500/600 Hz Ground Truth Tones
----------------------------------------
    During exclusive broadcast minutes, only one station transmits:
    - WWV-only: Minutes 1, 16, 17, 19 (500/600 Hz, WWVH silent)
    - WWVH-only: Minutes 2, 43-51 (500/600 Hz, WWV silent)
    
    This provides 14 GROUND TRUTH minutes per hour!
    
    Weight: 15.0 (highest confidence - scheduled exclusivity)

METHOD 5: BCD Time Code Correlation
------------------------------------
    Binary Coded Decimal (BCD) time code is 100 Hz amplitude modulation.
    WWV and WWVH encode identical time but with timing offset.
    Cross-correlation reveals amplitude ratio.
    
    Weight: 8.0-10.0 (varies by minute)

METHOD 6: Test Signal Analysis (Minutes 8 & 44)
-----------------------------------------------
    Scientific modulation test provides rich channel characterization:
    - Multi-tone detection (1-5 kHz)
    - Chirp delay spread measurement
    - Frequency Selectivity Score (FSS)
    - High-precision ToA from single-cycle bursts
    
    Minute 8: WWV only (WWVH silent)
    Minute 44: WWVH only (WWV silent)
    
    Weight: 15.0 (highest - scheduled exclusivity with channel metrics)

METHOD 7: Doppler Stability
---------------------------
    Lower Doppler standard deviation indicates cleaner ionospheric path.
    The more stable signal is likely the dominant one.
    
    Weight: 2.0 (confirmatory only - avoids feedback loops)

METHOD 8: Harmonic Power Ratio
------------------------------
    Receiver nonlinearity generates harmonics proportional to fundamental:
    - 500 Hz → 1000 Hz (WWV marker contribution)
    - 600 Hz → 1200 Hz (WWVH marker contribution)
    
    Weight: 1.5 (confirmatory only)

================================================================================
WEIGHTED VOTING ALGORITHM
================================================================================
Each method contributes a vote with method-specific weight. The weights are
adjusted based on minute number to favor the most reliable methods:

    score_WWV = Σ(w_i × vote_WWV_i)
    score_WWVH = Σ(w_i × vote_WWVH_i)
    
    norm_WWV = score_WWV / Σ(w_i)
    norm_WWVH = score_WWVH / Σ(w_i)
    
    DECISION:
    - |norm_WWV - norm_WWVH| < 0.15 → BALANCED
    - norm_WWV > norm_WWVH → WWV dominant
    - norm_WWVH > norm_WWV → WWVH dominant
    
    CONFIDENCE:
    - margin > 0.7 → high
    - margin > 0.4 → medium
    - margin ≤ 0.4 → low

MINUTE-SPECIFIC WEIGHT ADJUSTMENTS:
-----------------------------------
    Minutes 8, 44:  Test signal (15) > BCD (8) > Tick (5) > Carrier (2)
    Minutes 1, 2:   440 Hz (10) > 500/600 Hz (10) > Tick (5) > BCD (2)
    Minutes 16,17,19: 500/600 Hz (15) > Carrier (5) > Tick (5)
    Minutes 43-51:  500/600 Hz (15) > Carrier (5) > Tick (5)
    Other minutes:  Carrier (10) > Tick (5) > BCD (2)

================================================================================
INTER-METHOD CROSS-VALIDATION
================================================================================
Methods are cross-validated to detect anomalies:

AGREEMENTS (increase confidence):
    - TS_FSS_WWV: Test signal FSS confirms WWV path
    - TS_FSS_WWVH: Test signal FSS confirms WWVH path
    - TS_timing_high_precision: Low delay spread confirms accurate ToA
    - channel_underspread_clean: L < 0.05 confirms stable channel

DISAGREEMENTS (flag for investigation):
    - TS_FSS_geographic_mismatch: FSS doesn't match expected path
    - transient_noise_event: Noise floor changed during measurement
    - channel_overspread: L > 1.0 indicates severely degraded channel

================================================================================
OUTPUT: DiscriminationResult
================================================================================
The result includes:
    - dominant_station: 'WWV', 'WWVH', or 'BALANCED'
    - confidence: 'high', 'medium', or 'low'
    - power_ratio_db: 1000 Hz - 1200 Hz power difference
    - differential_delay_ms: Propagation time difference
    - Per-method detection flags and measurements
    - Inter-method agreement/disagreement lists

================================================================================
USAGE
================================================================================
    discriminator = WWVHDiscriminator(
        channel_name='WWV_10_MHz',
        receiver_grid='EM38ww',
        sample_rate=20000
    )
    
    # Process detections from tone_detector
    result = discriminator.compute_discrimination(
        detections=tone_detections,
        minute_timestamp=minute_boundary
    )
    
    print(f"Dominant: {result.dominant_station}, Confidence: {result.confidence}")
    print(f"Power ratio: {result.power_ratio_db:+.1f} dB")

================================================================================
REVISION HISTORY
================================================================================
2025-12-07: Added comprehensive theoretical documentation
2025-12-01: Added dual-station time recovery for UTC cross-validation
2025-11-20: Added test signal analysis for minutes 8/44
2025-11-15: Added 500/600 Hz ground truth detection
2025-10-20: Initial implementation with tone power ratio and BCD correlation
"""

import logging
import numpy as np
from typing import Optional, Dict, List, Tuple, Any
from dataclasses import dataclass
from datetime import datetime
from scipy import signal as scipy_signal
from scipy.fft import rfft, rfftfreq
from scipy.signal import iirnotch, filtfilt

from ..interfaces.data_models import ToneDetectionResult, StationType
from .tone_detector import MultiStationToneDetector
from .wwv_bcd_encoder import WWVBCDEncoder
from .wwv_geographic_predictor import WWVGeographicPredictor
from .wwv_test_signal import WWVTestSignalDetector, TestSignalDetection
from .wwv_constants import (
    TONE_SCHEDULE_500_600,
    WWV_ONLY_TONE_MINUTES,
    WWVH_ONLY_TONE_MINUTES
)

logger = logging.getLogger(__name__)

# Note: TONE_SCHEDULE_500_600, WWV_ONLY_TONE_MINUTES, WWVH_ONLY_TONE_MINUTES
# are now imported from wwv_constants.py (single source of truth)


@dataclass
class DiscriminationResult:
    """
    Result of WWV/WWVH discrimination analysis
    
    Attributes:
        minute_timestamp: UTC timestamp of minute boundary
        wwv_detected: Whether WWV (1000 Hz) was detected
        wwvh_detected: Whether WWVH (1200 Hz) was detected
        wwv_power_db: Power of WWV 1000 Hz tone (dB relative to noise)
        wwvh_power_db: Power of WWVH 1200 Hz tone (dB relative to noise)
        power_ratio_db: WWV power - WWVH power (positive = WWV stronger)
        differential_delay_ms: WWV arrival time - WWVH arrival time (ms)
        dominant_station: 'WWV', 'WWVH', or 'BALANCED'
        confidence: 'high', 'medium', 'low' based on SNR and power difference
        tone_440hz_wwv_detected: Whether 440 Hz tone detected in minute 2
        tone_440hz_wwvh_detected: Whether 440 Hz tone detected in minute 1
        tone_440hz_wwv_power_db: Power of 440 Hz tone in minute 2 (if detected)
        tone_440hz_wwvh_power_db: Power of 440 Hz tone in minute 1 (if detected)
        tick_windows_10sec: High-resolution 10-second windowed tick analysis (6 windows per minute)
            Each window contains both coherent and incoherent integration results:
            - coherent_wwv_snr_db, coherent_wwvh_snr_db: Phase-aligned amplitude sum (10 dB gain)
            - incoherent_wwv_snr_db, incoherent_wwvh_snr_db: Power sum (5 dB gain)
            - coherence_quality: 0-1 metric indicating phase stability
            - integration_method: 'coherent' or 'incoherent' (chosen based on quality)
    """
    minute_timestamp: float
    wwv_detected: bool
    wwvh_detected: bool
    wwv_power_db: Optional[float] = None
    wwvh_power_db: Optional[float] = None
    power_ratio_db: Optional[float] = None
    differential_delay_ms: Optional[float] = None
    dominant_station: Optional[str] = None
    confidence: str = 'low'
    tone_440hz_wwv_detected: bool = False
    tone_440hz_wwvh_detected: bool = False
    tone_440hz_wwv_power_db: Optional[float] = None
    tone_440hz_wwvh_power_db: Optional[float] = None
    tick_windows_10sec: Optional[List[Dict[str, float]]] = None
    # BCD-based discrimination (100 Hz cross-correlation method)
    bcd_wwv_amplitude: Optional[float] = None
    bcd_wwvh_amplitude: Optional[float] = None
    bcd_differential_delay_ms: Optional[float] = None
    bcd_correlation_quality: Optional[float] = None
    bcd_windows: Optional[List[Dict[str, float]]] = None  # Time-series data from sliding windows
    # Test signal discrimination (minute 8/44 scientific modulation test)
    # Note: Test signal is IDENTICAL for WWV/WWVH - discrimination from SCHEDULE
    # Value is high-gain ToA and SNR measurement for ionospheric characterization
    test_signal_detected: bool = False
    test_signal_station: Optional[str] = None  # 'WWV' or 'WWVH' (from schedule)
    test_signal_confidence: Optional[float] = None
    test_signal_multitone_score: Optional[float] = None
    test_signal_chirp_score: Optional[float] = None
    test_signal_noise_correlation: Optional[float] = None  # Average noise score (N1+N2)/2
    test_signal_snr_db: Optional[float] = None
    test_signal_toa_offset_ms: Optional[float] = None  # Time of arrival offset from expected
    test_signal_burst_toa_offset_ms: Optional[float] = None  # High-precision ToA from single-cycle bursts
    test_signal_delay_spread_ms: Optional[float] = None  # Multipath delay spread from chirp
    test_signal_coherence_time_sec: Optional[float] = None  # Channel coherence time from fading
    # Frequency Selectivity Score (FSS) - path-specific fingerprint
    # FSS = 10*log10((P_2kHz + P_3kHz) / (P_4kHz + P_5kHz))
    test_signal_frequency_selectivity_db: Optional[float] = None
    # Dual noise segment analysis for transient interference detection
    test_signal_noise1_score: Optional[float] = None  # Noise segment at 10-12s
    test_signal_noise2_score: Optional[float] = None  # Noise segment at 37-39s
    test_signal_noise_coherence_diff: Optional[float] = None  # |N1-N2|, high = transient event
    # Doppler estimation (ionospheric channel characterization)
    doppler_wwv_hz: Optional[float] = None
    doppler_wwvh_hz: Optional[float] = None
    doppler_wwv_std_hz: Optional[float] = None
    doppler_wwvh_std_hz: Optional[float] = None
    doppler_max_coherent_window_sec: Optional[float] = None
    doppler_quality: Optional[float] = None
    doppler_phase_variance_rad: Optional[float] = None
    doppler_valid_tick_count: Optional[int] = None
    # Inter-method cross-validation (Phase 6)
    inter_method_agreements: Optional[List[str]] = None
    inter_method_disagreements: Optional[List[str]] = None
    # 500/600 Hz tone ground truth (exclusive broadcast minutes)
    tone_500_600_detected: bool = False
    tone_500_600_power_db: Optional[float] = None
    tone_500_600_freq_hz: Optional[int] = None  # 500 or 600
    tone_500_600_ground_truth_station: Optional[str] = None  # Which station should be broadcasting
    # Harmonic Power Ratio (500→1000 Hz, 600→1200 Hz)
    # 2nd harmonic of 500 Hz is 1000 Hz (WWV timing marker), 2nd harmonic of 600 Hz is 1200 Hz (WWVH timing marker)
    # Receiver nonlinearity causes harmonic content proportional to fundamental power
    harmonic_ratio_500_1000: Optional[float] = None  # P_1000/P_500 ratio (dB)
    harmonic_ratio_600_1200: Optional[float] = None  # P_1200/P_600 ratio (dB)
    # BCD time code validation
    bcd_minute_validated: bool = False  # True if BCD correlation confirms expected minute
    bcd_correlation_peak_quality: Optional[float] = None  # Peak sharpness indicates timing lock
    
    # Dual-station time recovery (2025-12-01)
    # Uses both WWV and WWVH as redundant time servers for cross-validation
    # Back-calculates emission time: T_utc = T_arrival - propagation_delay
    wwv_toa_ms: Optional[float] = None        # Absolute ToA of WWV BCD peak (ms from minute start)
    wwvh_toa_ms: Optional[float] = None       # Absolute ToA of WWVH BCD peak (ms from minute start)
    wwv_expected_delay_ms: Optional[float] = None   # Expected WWV propagation delay
    wwvh_expected_delay_ms: Optional[float] = None  # Expected WWVH propagation delay
    t_emission_from_wwv_ms: Optional[float] = None  # ToA - delay_wwv (should be ~0 at minute boundary)
    t_emission_from_wwvh_ms: Optional[float] = None # ToA - delay_wwvh (should match WWV result)
    cross_validation_error_ms: Optional[float] = None  # |T_wwv - T_wwvh| - agreement metric
    dual_station_confidence: str = 'none'     # 'excellent'(<1ms)/'good'(<2ms)/'fair'(<5ms)/'investigate'
    
    def to_dict(self) -> Dict:
        """Convert to dictionary for logging/storage"""
        return {
            'minute_timestamp': self.minute_timestamp,
            'wwv_detected': self.wwv_detected,
            'wwvh_detected': self.wwvh_detected,
            'wwv_power_db': self.wwv_power_db,
            'wwvh_power_db': self.wwvh_power_db,
            'power_ratio_db': self.power_ratio_db,
            'differential_delay_ms': self.differential_delay_ms,
            'dominant_station': self.dominant_station,
            'confidence': self.confidence,
            'tone_440hz_wwv_detected': self.tone_440hz_wwv_detected,
            'tone_440hz_wwvh_detected': self.tone_440hz_wwvh_detected,
            'tone_440hz_wwv_power_db': self.tone_440hz_wwv_power_db,
            'tone_440hz_wwvh_power_db': self.tone_440hz_wwvh_power_db,
            'tick_windows_10sec': self.tick_windows_10sec,
            # Dual-station time recovery
            'wwv_toa_ms': self.wwv_toa_ms,
            'wwvh_toa_ms': self.wwvh_toa_ms,
            'wwv_expected_delay_ms': self.wwv_expected_delay_ms,
            'wwvh_expected_delay_ms': self.wwvh_expected_delay_ms,
            't_emission_from_wwv_ms': self.t_emission_from_wwv_ms,
            't_emission_from_wwvh_ms': self.t_emission_from_wwvh_ms,
            'cross_validation_error_ms': self.cross_validation_error_ms,
            'dual_station_confidence': self.dual_station_confidence
        }


class WWVHDiscriminator:
    """
    Discriminate between WWV and WWVH using multiple signal characteristics
    
    Combines:
    1. Per-minute 1000 Hz vs 1200 Hz power ratio
    2. Arrival time difference (differential propagation delay)
    3. 440 Hz tone presence in minutes 1 and 2
    """
    
    def __init__(
        self,
        channel_name: str,
        receiver_grid: Optional[str] = None,
        history_dir: Optional[str] = None,
        sample_rate: int = 20000
    ):
        """
        Initialize discriminator
        
        Args:
            channel_name: Channel name for logging
            receiver_grid: Maidenhead grid square (e.g., "EM38ww") for geographic ToA prediction
            history_dir: Directory for persisting ToA history (optional)
            sample_rate: Sample rate in Hz (20000 default, 16000 for legacy)
        """
        self.channel_name = channel_name
        self.sample_rate = sample_rate
        self.measurements: List[DiscriminationResult] = []
        
        # Keep last 1000 measurements
        self.max_history = 1000
        
        # Initialize BCD encoder for template generation
        self.bcd_encoder = WWVBCDEncoder(sample_rate=sample_rate)
        
        # Initialize test signal detector for minute 8/44 discrimination
        self.test_signal_detector = WWVTestSignalDetector(sample_rate=sample_rate)
        logger.info(f"{channel_name}: Test signal detector initialized for minutes 8/44 @ {sample_rate} Hz")
        
        # Initialize geographic predictor if grid square provided
        self.geo_predictor: Optional[WWVGeographicPredictor] = None
        if receiver_grid:
            from pathlib import Path
            history_file = None
            if history_dir:
                history_file = Path(history_dir) / f"toa_history_{channel_name.replace(' ', '_')}.json"
            
            self.geo_predictor = WWVGeographicPredictor(
                receiver_grid=receiver_grid,
                history_file=history_file,
                max_history=1000
            )
            logger.info(f"{channel_name}: Geographic ToA prediction enabled for {receiver_grid}")
        else:
            logger.info(f"{channel_name}: Geographic ToA prediction disabled (no grid square configured)")
        
        logger.info(f"{channel_name}: WWVHDiscriminator initialized")
    
    def measure_tone_powers_fft(
        self,
        iq_samples: np.ndarray,
        sample_rate: int
    ) -> Tuple[float, float]:
        """
        Measure actual tone powers using FFT (not matched filter).
        
        This provides accurate RELATIVE power comparison between 1000 Hz and 1200 Hz
        tones, unlike matched filter SNR which measures detection confidence.
        
        Args:
            iq_samples: Complex IQ samples
            sample_rate: Sample rate in Hz
            
        Returns:
            Tuple of (wwv_power_db, wwvh_power_db) - absolute power in dB
        """
        # AM demodulation
        envelope = np.abs(iq_samples)
        audio = envelope - np.mean(envelope)
        
        # FFT
        fft_result = np.abs(rfft(audio))
        freqs = rfftfreq(len(audio), 1/sample_rate)
        
        # Measure power at 1000 Hz (WWV) and 1200 Hz (WWVH)
        # Use small window around target frequency
        def get_peak_power(target_freq: float) -> float:
            idx = np.argmin(np.abs(freqs - target_freq))
            # Take max in ±5 bins to handle slight frequency offsets
            band_start = max(0, idx - 5)
            band_end = min(len(fft_result), idx + 5)
            peak = np.max(fft_result[band_start:band_end])
            return 20 * np.log10(peak + 1e-12)
        
        wwv_power_db = get_peak_power(1000.0)
        wwvh_power_db = get_peak_power(1200.0)
        
        return wwv_power_db, wwvh_power_db
    
    def compute_discrimination(
        self,
        detections: List[ToneDetectionResult],
        minute_timestamp: float
    ) -> DiscriminationResult:
        """
        Compute base discrimination from 1000/1200 Hz tone detection results.
        
        This is the FIRST STAGE of discrimination, using only the timing tone
        power ratio. The result is later enhanced by finalize_discrimination()
        which adds weighted voting from multiple methods.
        
        ALGORITHM:
        ----------
        1. Extract WWV (1000 Hz) and WWVH (1200 Hz) detections
        2. Calculate power ratio: P_WWV - P_WWVH (dB)
        3. Calculate differential delay: τ_WWV - τ_WWVH (ms)
        4. Determine initial dominant station based on power ratio
        5. Assign confidence based on SNR and power difference
        
        POWER RATIO INTERPRETATION:
        ---------------------------
            power_ratio_db > +3 dB  → WWV dominant
            power_ratio_db < -3 dB  → WWVH dominant
            |power_ratio_db| ≤ 3 dB → BALANCED (both present)
        
        DIFFERENTIAL DELAY:
        -------------------
        Only computed when BOTH tones are detected. This is the propagation
        time difference between the two signals:
        
            Δτ = timing_error_WWV - timing_error_WWVH
        
        Typical values depend on receiver location:
            - Central US: WWV closer → Δτ negative
            - Pacific: WWVH closer → Δτ positive
        
        Values outside ±1000 ms are rejected as detection errors.
        
        ALWAYS RETURNS A RESULT:
        ------------------------
        Even when no tones are detected, a result is returned with:
            - wwv_power_db = 0.0 (noise floor)
            - wwvh_power_db = 0.0 (noise floor)
            - power_ratio_db = 0.0
            - differential_delay_ms = None
            - dominant_station = 'NONE'
            - confidence = 'low'
        
        This ensures continuous time-series data for visualization.
        
        Args:
            detections: List of ToneDetectionResult objects from same minute
                        (typically from MultiStationToneDetector.process_samples)
            minute_timestamp: UTC timestamp of minute boundary (Unix time)
            
        Returns:
            DiscriminationResult with base discrimination metrics.
            This result should be passed to finalize_discrimination() for
            weighted voting with additional methods.
        """
        wwv_det = None
        wwvh_det = None
        
        # Handle None or empty detections
        if not detections:
            detections = []
        
        for det in detections:
            if det.station == StationType.WWV:
                wwv_det = det
            elif det.station == StationType.WWVH:
                wwvh_det = det
        
        # Extract power/SNR measurements (use noise floor if not detected)
        wwv_detected = wwv_det is not None
        wwvh_detected = wwvh_det is not None
        
        if wwv_detected:
            wwv_power_db = getattr(wwv_det, 'tone_power_db', wwv_det.snr_db)
            # Ensure we have a valid number
            if wwv_power_db is None:
                wwv_power_db = wwv_det.snr_db if wwv_det.snr_db is not None else 0.0
        else:
            # No WWV detection - record noise floor (assume ~0 dB SNR = noise)
            wwv_power_db = 0.0
        
        if wwvh_detected:
            wwvh_power_db = getattr(wwvh_det, 'tone_power_db', wwvh_det.snr_db)
            # Ensure we have a valid number
            if wwvh_power_db is None:
                wwvh_power_db = wwvh_det.snr_db if wwvh_det.snr_db is not None else 0.0
        else:
            # No WWVH detection - record noise floor
            wwvh_power_db = 0.0
        
        # Calculate power ratio (always computed, even with noise floor)
        # Safety check for None values
        if wwv_power_db is None or wwvh_power_db is None:
            power_ratio_db = 0.0
        else:
            power_ratio_db = wwv_power_db - wwvh_power_db
        
        # Calculate differential delay ONLY if BOTH detected
        # Otherwise null (creates gap in time-series graph)
        differential_delay_ms = None
        if wwv_detected and wwvh_detected:
            # Safety check for None timing errors
            if wwv_det.timing_error_ms is not None and wwvh_det.timing_error_ms is not None:
                differential_delay_ms = wwv_det.timing_error_ms - wwvh_det.timing_error_ms
                
                # Reject outliers: Ionospheric differential delay should be < ±1 second
                # Values outside this range indicate detection errors
                if abs(differential_delay_ms) > 1000:
                    logger.warning(f"{self.channel_name}: Rejecting outlier differential delay: {differential_delay_ms:.1f}ms "
                                  f"(WWV: {wwv_det.timing_error_ms:.1f}ms, WWVH: {wwvh_det.timing_error_ms:.1f}ms)")
                    differential_delay_ms = None
        
        # Determine dominant station
        if not wwv_detected and not wwvh_detected:
            dominant_station = 'NONE'
        elif not wwv_detected:
            dominant_station = 'WWVH'  # Only WWVH detected
        elif not wwvh_detected:
            dominant_station = 'WWV'  # Only WWV detected
        elif abs(power_ratio_db) < 3.0:  # Within 3 dB = balanced
            dominant_station = 'BALANCED'
        elif power_ratio_db > 0:
            dominant_station = 'WWV'
        else:
            dominant_station = 'WWVH'
        
        # Determine confidence based on actual detections
        if wwv_detected and wwvh_detected:
            min_snr = min(wwv_det.snr_db, wwvh_det.snr_db)
            max_snr = max(wwv_det.snr_db, wwvh_det.snr_db)
            power_diff = abs(power_ratio_db)
            
            # Improved confidence logic:
            # High confidence: Strong dominant station OR both stations strong with clear separation
            if (max_snr > 25 and power_diff > 15):  # One very strong, other clearly weaker
                confidence = 'high'
            elif (min_snr > 20 and power_diff > 6.0):  # Both strong with good separation
                confidence = 'high'
            elif (max_snr > 15 and power_diff > 10):  # One strong with clear dominance
                confidence = 'medium'
            elif (min_snr > 10 and power_diff > 3.0):  # Both moderate with separation
                confidence = 'medium'
            else:
                confidence = 'low'
        elif wwv_detected or wwvh_detected:
            # Single station detected - confidence based on SNR of detected station
            detected_snr = wwv_det.snr_db if wwv_detected else wwvh_det.snr_db
            if detected_snr > 20:
                confidence = 'high'  # Strong single station is high confidence
            elif detected_snr > 10:
                confidence = 'medium'
            else:
                confidence = 'low'
        else:
            # Neither detected - low confidence
            confidence = 'low'
        
        result = DiscriminationResult(
            minute_timestamp=minute_timestamp,
            wwv_detected=wwv_detected,
            wwvh_detected=wwvh_detected,
            wwv_power_db=wwv_power_db,
            wwvh_power_db=wwvh_power_db,
            power_ratio_db=power_ratio_db,
            differential_delay_ms=differential_delay_ms,  # None if either missing
            dominant_station=dominant_station,
            confidence=confidence
        )
        
        # Store measurement
        self.measurements.append(result)
        if len(self.measurements) > self.max_history:
            self.measurements = self.measurements[-self.max_history:]
        
        # Log appropriately based on detection status
        if wwv_detected and wwvh_detected:
            # Safely format values that might be None
            wwv_str = f"{wwv_power_db:.1f}dB" if wwv_power_db is not None else "N/A"
            wwvh_str = f"{wwvh_power_db:.1f}dB" if wwvh_power_db is not None else "N/A"
            ratio_str = f"{power_ratio_db:+.1f}dB" if power_ratio_db is not None else "N/A"
            delay_str = f"{differential_delay_ms:+.1f}ms" if differential_delay_ms is not None else "N/A"
            
            logger.info(f"{self.channel_name}: Discrimination - "
                       f"WWV: {wwv_str}, WWVH: {wwvh_str}, "
                       f"Ratio: {ratio_str}, Delay: {delay_str}, "
                       f"Dominant: {dominant_station or 'N/A'}, Confidence: {confidence or 'N/A'}")
        else:
            wwv_str = f"{wwv_power_db:.1f}dB" if wwv_power_db is not None else "N/A"
            wwvh_str = f"{wwvh_power_db:.1f}dB" if wwvh_power_db is not None else "N/A"
            logger.debug(f"{self.channel_name}: Discrimination (partial) - "
                        f"WWV: {'detected' if wwv_detected else 'noise'} ({wwv_str}), "
                        f"WWVH: {'detected' if wwvh_detected else 'noise'} ({wwvh_str})")
        
        return result
    
    def detect_timing_tones(
        self,
        iq_samples: np.ndarray,
        sample_rate: int,
        minute_timestamp: float
    ) -> Tuple[Optional[float], Optional[float], Optional[float], List[ToneDetectionResult]]:
        """
        Detect 800ms timing tones from IQ samples - INDEPENDENT METHOD
        
        This method integrates ToneDetector to make tone detection independent
        and reprocessable from archived IQ data. It extracts WWV/WWVH power
        measurements and differential delay for discrimination.
        
        Args:
            iq_samples: Complex IQ samples at sample_rate (typically 16 kHz, 60 seconds)
            sample_rate: Sample rate in Hz
            minute_timestamp: UTC timestamp of minute boundary
            
        Returns:
            Tuple of:
            - wwv_power_db: WWV 1000 Hz tone power (dB), or None if not detected
            - wwvh_power_db: WWVH 1200 Hz tone power (dB), or None if not detected
            - differential_delay_ms: Propagation delay difference (ms), or None if not both detected
            - detections: List of ToneDetectionResult objects (for full provenance)
        """
        # Initialize tone detector if not already present
        if not hasattr(self, 'tone_detector'):
            self.tone_detector = MultiStationToneDetector(
                channel_name=self.channel_name,
                sample_rate=sample_rate
            )
        elif self.tone_detector.sample_rate != sample_rate:
            # Reinitialize if sample rate changed
            self.tone_detector = MultiStationToneDetector(
                channel_name=self.channel_name,
                sample_rate=sample_rate
            )
        
        # Detect tones using process_samples() method
        # IMPORTANT: tone_detector.process_samples() expects timestamp to be buffer MIDPOINT
        # minute_timestamp is the minute boundary (buffer START), so add half the buffer duration
        buffer_duration_sec = len(iq_samples) / sample_rate
        buffer_midpoint = minute_timestamp + (buffer_duration_sec / 2)
        
        try:
            detections = self.tone_detector.process_samples(
                timestamp=buffer_midpoint,
                samples=iq_samples
            )
            if detections is None:
                detections = []
        except Exception as e:
            logger.warning(f"{self.channel_name}: Tone detection failed: {e}")
            return None, None, None, []
        
        # Extract WWV and WWVH detections
        wwv_det = None
        wwvh_det = None
        
        for det in detections:
            if det.station == StationType.WWV:
                wwv_det = det
            elif det.station == StationType.WWVH:
                wwvh_det = det
        
        # Extract power measurements
        wwv_power_db = None
        wwvh_power_db = None
        differential_delay_ms = None
        
        if wwv_det:
            # Prefer tone_power_db, fall back to snr_db
            wwv_power_db = getattr(wwv_det, 'tone_power_db', None)
            if wwv_power_db is None:
                wwv_power_db = wwv_det.snr_db if wwv_det.snr_db is not None else 0.0
        
        if wwvh_det:
            wwvh_power_db = getattr(wwvh_det, 'tone_power_db', None)
            if wwvh_power_db is None:
                wwvh_power_db = wwvh_det.snr_db if wwvh_det.snr_db is not None else 0.0
        
        # Calculate differential delay only if both detected
        if wwv_det and wwvh_det:
            if wwv_det.timing_error_ms is not None and wwvh_det.timing_error_ms is not None:
                differential_delay_ms = wwv_det.timing_error_ms - wwvh_det.timing_error_ms
                
                # Sanity check: reject outliers beyond ±1 second
                if abs(differential_delay_ms) > 1000:
                    logger.warning(
                        f"{self.channel_name}: Rejecting outlier differential delay: "
                        f"{differential_delay_ms:.1f}ms (WWV: {wwv_det.timing_error_ms:.1f}ms, "
                        f"WWVH: {wwvh_det.timing_error_ms:.1f}ms)"
                    )
                    differential_delay_ms = None
        
        return wwv_power_db, wwvh_power_db, differential_delay_ms, detections
    
    def finalize_discrimination(
        self,
        result: DiscriminationResult,
        minute_number: int,
        bcd_wwv_amp: Optional[float],
        bcd_wwvh_amp: Optional[float],
        tone_440_wwv_detected: bool,
        tone_440_wwvh_detected: bool,
        tick_results: Optional[List[dict]] = None
    ) -> DiscriminationResult:
        """
        Finalize discrimination using weighted voting based on minute-specific confidence
        
        Weighting hierarchy:
        - Minutes 8/44: Test signal (highest weight when detected) → BCD → Tick SNR
        - Minutes 1/2: 440 Hz tone (highest weight) + 500/600 Hz ground truth → Tick SNR
        - Minutes 16,17,19: 500/600 Hz ground truth (WWV-only) → 1000/1200 Hz → Tick SNR
        - Minutes 43-51: 500/600 Hz ground truth (WWVH-only) → 1000/1200 Hz → Tick SNR
        - Minutes 0/8-10/29-30: BCD amplitude (highest weight) → Tick SNR → 1000/1200 Hz
        - All other minutes: 1000/1200 Hz power (highest weight) → Tick SNR
        
        500/600 Hz Ground Truth Minutes (14 per hour):
        - WWV-only: 1, 16, 17, 19 (WWV broadcasts 600/500 Hz, WWVH is silent)
        - WWVH-only: 2, 43-51 (WWVH broadcasts 600/500 Hz, WWV is silent)
        
        Args:
            result: Base discrimination result from 1000/1200 Hz tones
            minute_number: Minute of hour (0-59)
            bcd_wwv_amp: WWV amplitude from BCD correlation
            bcd_wwvh_amp: WWVH amplitude from BCD correlation
            tone_440_wwv_detected: 440 Hz detected in minute 2
            tone_440_wwvh_detected: 440 Hz detected in minute 1
            tick_results: Per-second tick discrimination results
            
        Returns:
            Enhanced DiscriminationResult with weighted voting
        """
        # Test signal minutes (scientific modulation test)
        test_signal_minutes = [8, 44]
        # BCD-dominant minutes (0, 8-10, 29-30)
        bcd_minutes = [0, 8, 9, 10, 29, 30]
        # 440 Hz tone minutes
        tone_440_minutes = [1, 2]
        # 500/600 Hz ground truth minutes (14 total per hour!)
        # WWV-only: 1, 16, 17, 19 (WWV broadcasts 500/600 Hz, WWVH silent)
        # WWVH-only: 2, 43, 44, 45, 46, 47, 48, 49, 50, 51 (WWVH broadcasts, WWV silent)
        ground_truth_500_600_minutes = WWV_ONLY_TONE_MINUTES | WWVH_ONLY_TONE_MINUTES
        
        # Initialize voting scores
        wwv_score = 0.0
        wwvh_score = 0.0
        total_weight = 0.0
        
        # Initialize inter-method agreement/disagreement tracking
        agreements = []
        disagreements = []
        
        # Weight factors - test signal gets highest weight when available
        # 500/600 Hz ground truth gets high weight when applicable (overlaps with some other categories)
        # Exclusive minutes (16,17,19 WWV-only; 43-51 WWVH-only) get highest weight (15.0)
        # Mixed minutes (1,2) share with 440 Hz so get standard weight (10.0)
        high_confidence_500_600_minutes = [m for m in ground_truth_500_600_minutes if m not in [1, 2]]
        if minute_number in high_confidence_500_600_minutes:
            w_500_600 = 15.0  # Highest confidence - scheduled to be alone
        elif minute_number in [1, 2]:
            w_500_600 = 10.0  # High confidence, but shares minute with 440 Hz
        else:
            w_500_600 = 0.0
        
        if minute_number in test_signal_minutes:
            w_test = 15.0  # Highest weight for test signal (when detected)
            w_bcd = 8.0
            w_tick = 5.0
            w_carrier = 2.0 if w_500_600 == 0 else 1.0  # Reduce carrier weight when ground truth available
            w_440 = 0.0
        elif minute_number in tone_440_minutes:
            w_test = 0.0
            w_440 = 10.0  # Highest weight for 440 Hz
            w_tick = 5.0
            w_bcd = 2.0
            w_carrier = 1.0  # Already reduced for 440 Hz minutes
        elif minute_number in bcd_minutes:
            w_test = 0.0
            w_bcd = 10.0  # Highest weight for BCD
            w_tick = 5.0
            w_carrier = 2.0 if w_500_600 == 0 else 1.0
            w_440 = 0.0
        else:
            w_test = 0.0
            w_carrier = 10.0 if w_500_600 == 0 else 5.0  # Reduce when ground truth available
            w_tick = 5.0
            w_bcd = 2.0
            w_440 = 0.0
        
        # === VOTE 0: Test Signal Detection (minutes 8/44 only) ===
        if w_test > 0 and result.test_signal_detected:
            # Test signal provides definitive station identification
            if result.test_signal_station == 'WWV':
                wwv_score += w_test
            elif result.test_signal_station == 'WWVH':
                wwvh_score += w_test
            total_weight += w_test
            
            logger.debug(f"Test signal vote: station={result.test_signal_station}, "
                        f"confidence={result.test_signal_confidence:.3f}")
        
        # === VOTE 1: 440 Hz Tone Detection ===
        if w_440 > 0:
            if tone_440_wwv_detected and not tone_440_wwvh_detected:
                wwv_score += w_440
                total_weight += w_440
            elif tone_440_wwvh_detected and not tone_440_wwv_detected:
                wwvh_score += w_440
                total_weight += w_440
            elif tone_440_wwv_detected and tone_440_wwvh_detected:
                # Both detected (shouldn't happen) - ignore
                pass
        
        # === VOTE 2: BCD Amplitude Ratio ===
        if w_bcd > 0 and bcd_wwv_amp is not None and bcd_wwvh_amp is not None:
            if bcd_wwv_amp > 0 and bcd_wwvh_amp > 0:
                bcd_ratio_db = 20 * np.log10(bcd_wwv_amp / bcd_wwvh_amp)
                
                if abs(bcd_ratio_db) >= 3.0:  # Significant difference
                    if bcd_ratio_db > 0:
                        wwv_score += w_bcd
                    else:
                        wwvh_score += w_bcd
                    total_weight += w_bcd
        
        # === VOTE 3: 1000/1200 Hz Carrier Power Ratio ===
        if w_carrier > 0 and result.power_ratio_db is not None:
            if abs(result.power_ratio_db) >= 3.0:  # Significant difference
                if result.power_ratio_db > 0:
                    wwv_score += w_carrier
                else:
                    wwvh_score += w_carrier
                total_weight += w_carrier
        
        # === VOTE 4: Per-Second Tick SNR Average ===
        if w_tick > 0 and tick_results:
            wwv_tick_snr = []
            wwvh_tick_snr = []
            
            for tick in tick_results:
                if 'wwv_snr_db' in tick and 'wwvh_snr_db' in tick:
                    wwv_tick_snr.append(tick['wwv_snr_db'])
                    wwvh_tick_snr.append(tick['wwvh_snr_db'])
            
            if wwv_tick_snr and wwvh_tick_snr:
                avg_wwv_tick = np.mean(wwv_tick_snr)
                avg_wwvh_tick = np.mean(wwvh_tick_snr)
                tick_ratio_db = avg_wwv_tick - avg_wwvh_tick
                
                if abs(tick_ratio_db) >= 3.0:
                    if tick_ratio_db > 0:
                        wwv_score += w_tick
                    else:
                        wwvh_score += w_tick
                    total_weight += w_tick
        
        # === VOTE 5: 500/600 Hz Ground Truth Tone Detection ===
        # During exclusive broadcast minutes, only one station transmits 500/600 Hz
        # This provides absolute ground truth for station identification
        if w_500_600 > 0 and result.tone_500_600_detected:
            gt_station = result.tone_500_600_ground_truth_station
            if gt_station == 'WWV':
                wwv_score += w_500_600
                total_weight += w_500_600
                logger.debug(f"{self.channel_name}: 500/600 Hz ground truth vote: WWV "
                           f"(minute {minute_number}, power={result.tone_500_600_power_db:.1f}dB)")
            elif gt_station == 'WWVH':
                wwvh_score += w_500_600
                total_weight += w_500_600
                logger.debug(f"{self.channel_name}: 500/600 Hz ground truth vote: WWVH "
                           f"(minute {minute_number}, power={result.tone_500_600_power_db:.1f}dB)")
        
        # === VOTE 6: Doppler Stability Vote ===
        # Uses Doppler standard deviation as an INDEPENDENT measure of channel stability.
        # A more stable path (lower std) indicates a cleaner, more direct ionospheric reflection.
        # This avoids subtle reinforcement loops with the power ratio by using std, not mean ΔfD.
        w_doppler = 2.0  # Lower weight - confirmatory rather than primary
        if (result.doppler_wwv_std_hz is not None and 
            result.doppler_wwvh_std_hz is not None and
            result.doppler_quality is not None and result.doppler_quality > 0.3):
            # Calculate ratio of Doppler standard deviations in dB
            # Negative = WWV more stable, Positive = WWVH more stable
            std_ratio_db = 10 * np.log10(
                (result.doppler_wwv_std_hz + 1e-12) / 
                (result.doppler_wwvh_std_hz + 1e-12)
            )
            
            # If one station's Doppler is significantly more stable (>3 dB difference)
            if std_ratio_db < -3.0:
                # WWV is much more stable (cleaner path)
                wwv_score += w_doppler
                total_weight += w_doppler
                logger.debug(f"{self.channel_name}: Doppler stability vote: WWV "
                           f"(WWV std < WWVH std by {-std_ratio_db:.1f} dB)")
            elif std_ratio_db > 3.0:
                # WWVH is much more stable (cleaner path)
                wwvh_score += w_doppler
                total_weight += w_doppler
                logger.debug(f"{self.channel_name}: Doppler stability vote: WWVH "
                           f"(WWVH std < WWV std by {std_ratio_db:.1f} dB)")
        
        # === VOTE 7: Test Signal ToA vs BCD ToA Consistency (minutes 8/44) ===
        # Both Test Signal and BCD are modulated on the carrier simultaneously.
        # The Test Signal matched filter has superior BT product gain for timing.
        # If Test Signal ToA aligns with BCD early peak arrival, boost confidence.
        if minute_number in [8, 44] and result.test_signal_detected and result.test_signal_toa_offset_ms is not None:
            # Check if BCD also detected dual peaks with timing
            if result.bcd_differential_delay_ms is not None and result.bcd_differential_delay_ms > 0:
                # Test signal ToA should be close to the dominant station's BCD arrival
                # If ToA offset is small (<5ms), the timing is coherent
                if abs(result.test_signal_toa_offset_ms) < 5.0:
                    # Boost the test signal vote confidence
                    w_timing_coherence = 3.0
                    if result.test_signal_station == 'WWV':
                        wwv_score += w_timing_coherence
                    elif result.test_signal_station == 'WWVH':
                        wwvh_score += w_timing_coherence
                    total_weight += w_timing_coherence
                    logger.debug(f"{self.channel_name}: Timing coherence vote: {result.test_signal_station} "
                               f"(ToA offset={result.test_signal_toa_offset_ms:.2f}ms, BCD delay={result.bcd_differential_delay_ms:.1f}ms)")
        
        # === VOTE 7b: Chirp Delay Spread Channel Quality Assessment ===
        # When chirps are detected, delay spread indicates multipath severity.
        # Low delay spread (<2ms) = clean channel = high confidence in timing
        # High delay spread (>5ms) = multipath = reduce confidence in all timing methods
        if minute_number in [8, 44] and result.test_signal_delay_spread_ms is not None:
            delay_spread = result.test_signal_delay_spread_ms
            if delay_spread < 2.0:
                # Clean channel - boost confidence in dominant station
                w_channel_quality = 2.0
                if result.test_signal_station:
                    if result.test_signal_station == 'WWV':
                        wwv_score += w_channel_quality
                    else:
                        wwvh_score += w_channel_quality
                    total_weight += w_channel_quality
                    logger.debug(f"{self.channel_name}: Channel quality vote: {result.test_signal_station} "
                               f"(delay_spread={delay_spread:.1f}ms - clean channel)")
            elif delay_spread > 5.0:
                # Significant multipath - log warning, don't add weight (reduces overall confidence)
                logger.warning(f"{self.channel_name}: High delay spread ({delay_spread:.1f}ms) indicates multipath - "
                              "timing measurements may be unreliable")
        
        # === VOTE 7c: Coherence Time Quality Gating ===
        # Short coherence time (<0.5s) indicates fast fading channel.
        # This affects reliability of all timing methods during that minute.
        if minute_number in [8, 44] and result.test_signal_coherence_time_sec is not None:
            coherence_time = result.test_signal_coherence_time_sec
            if coherence_time < 0.5:
                logger.info(f"{self.channel_name}: Short coherence time ({coherence_time:.2f}s) - "
                           "fast fading channel, timing precision may be degraded")
            elif coherence_time > 2.0:
                # Stable channel - boost confidence slightly
                w_stability = 1.0
                if result.test_signal_station:
                    if result.test_signal_station == 'WWV':
                        wwv_score += w_stability
                    else:
                        wwvh_score += w_stability
                    total_weight += w_stability
                    logger.debug(f"{self.channel_name}: Channel stability vote: {result.test_signal_station} "
                               f"(coherence_time={coherence_time:.1f}s - stable channel)")
        
        # === VOTE 8: Harmonic Power Ratio Cross-Validation ===
        # The 2nd harmonic of 500 Hz is 1000 Hz (WWV timing marker)
        # The 2nd harmonic of 600 Hz is 1200 Hz (WWVH timing marker)
        # In exclusive tone minutes, the harmonic ratio should correlate with station presence
        w_harmonic = 1.5  # Lower weight - confirmatory
        if result.harmonic_ratio_500_1000 is not None and result.harmonic_ratio_600_1200 is not None:
            # If 500 Hz is present, its harmonic boosts 1000 Hz (WWV marker)
            # If 600 Hz is present, its harmonic boosts 1200 Hz (WWVH marker)
            # A higher harmonic ratio indicates stronger fundamental tone presence
            ratio_diff = result.harmonic_ratio_500_1000 - result.harmonic_ratio_600_1200
            
            # Significant difference (>3 dB) indicates one station's tone is dominant
            if abs(ratio_diff) > 3.0:
                if ratio_diff > 0:
                    # 500→1000 Hz harmonic stronger - correlates with WWV 500 Hz tone
                    if result.power_ratio_db and result.power_ratio_db > 0:
                        wwv_score += w_harmonic
                        total_weight += w_harmonic
                        logger.debug(f"{self.channel_name}: Harmonic ratio vote: WWV (500→1000 stronger by {ratio_diff:.1f}dB)")
                else:
                    # 600→1200 Hz harmonic stronger - correlates with WWVH 600 Hz tone
                    if result.power_ratio_db and result.power_ratio_db < 0:
                        wwvh_score += w_harmonic
                        total_weight += w_harmonic
                        logger.debug(f"{self.channel_name}: Harmonic ratio vote: WWVH (600→1200 stronger by {-ratio_diff:.1f}dB)")
        
        # === VOTE 9: Frequency Selectivity Score (FSS) - Geographic Path Validator ===
        # The ionosphere's frequency-dependent attenuation creates a path "fingerprint"
        # FSS = 10*log10((P_2kHz + P_3kHz) / (P_4kHz + P_5kHz))
        # 
        # This vote confirms that the measured path characteristic matches the EXPECTED
        # geographic path for the scheduled station:
        # - WWV (continental): Short-to-medium path, relatively flat response → FSS < 3.0 dB
        # - WWVH (trans-oceanic): Long path, heavy high-freq attenuation → FSS > 5.0 dB
        #
        # This is a GEOGRAPHIC VALIDATOR: it only adds weight when FSS confirms the schedule
        w_fss = 2.0  # Strong weight - independent geographic confirmation
        if result.test_signal_frequency_selectivity_db is not None:
            fss = result.test_signal_frequency_selectivity_db
            scheduled_station = result.test_signal_station  # 'WWV' (min 8) or 'WWVH' (min 44)
            
            if scheduled_station == 'WWV':
                # WWV path expected to be flatter (< 3.0 dB)
                if fss < 3.0:
                    wwv_score += w_fss
                    total_weight += w_fss
                    agreements.append('TS_FSS_WWV')
                    logger.debug(f"{self.channel_name}: FSS Vote: WWV confirmed "
                                f"(FSS={fss:.1f}dB < 3.0dB, matches continental path)")
                elif fss > 5.0:
                    # Dispersive path when WWV expected - geographic mismatch
                    disagreements.append('TS_FSS_geographic_mismatch')
                    logger.warning(f"{self.channel_name}: FSS geographic mismatch: "
                                  f"FSS={fss:.1f}dB > 5.0dB but WWV (continental) scheduled")
                else:
                    logger.debug(f"{self.channel_name}: FSS={fss:.1f}dB (ambiguous, no vote)")
                    
            elif scheduled_station == 'WWVH':
                # WWVH path expected to be more dispersive (> 5.0 dB)
                if fss > 5.0:
                    wwvh_score += w_fss
                    total_weight += w_fss
                    agreements.append('TS_FSS_WWVH')
                    logger.debug(f"{self.channel_name}: FSS Vote: WWVH confirmed "
                                f"(FSS={fss:.1f}dB > 5.0dB, matches trans-oceanic path)")
                elif fss < 3.0:
                    # Clean path when WWVH expected - geographic mismatch
                    disagreements.append('TS_FSS_geographic_mismatch')
                    logger.warning(f"{self.channel_name}: FSS geographic mismatch: "
                                  f"FSS={fss:.1f}dB < 3.0dB but WWVH (trans-oceanic) scheduled")
                else:
                    logger.debug(f"{self.channel_name}: FSS={fss:.1f}dB (ambiguous, no vote)")
        
        # === VOTE 10: Noise Coherence Transient Detection ===
        # If |Noise1 - Noise2| is large, a transient event occurred during the test signal
        # This flags potential interference that could corrupt other measurements
        if result.test_signal_noise_coherence_diff is not None:
            noise_diff = result.test_signal_noise_coherence_diff
            if noise_diff > 0.2:  # >20% difference between noise segments
                disagreements.append('transient_noise_event')
                n1 = result.test_signal_noise1_score
                n2 = result.test_signal_noise2_score
                logger.warning(f"{self.channel_name}: ⚠️ Transient noise event detected "
                              f"(N1={n1:.2f if n1 else 0}, N2={n2:.2f if n2 else 0}, "
                              f"diff={noise_diff:.2f})")
            elif noise_diff < 0.05:
                # Very stable noise floor - boost confidence in all test signal metrics
                agreements.append('TS_noise_stable')
                logger.debug(f"{self.channel_name}: Noise coherence stable (diff={noise_diff:.3f})")
        
        # === VOTE 11: High-Precision Timing Cross-Validation ===
        # Burst ToA is the HIGHEST AUTHORITY for timing (single-cycle = best resolution)
        # Cross-validate against chirp delay spread to detect multipath issues
        if result.test_signal_burst_toa_offset_ms is not None:
            burst_toa = result.test_signal_burst_toa_offset_ms
            delay_spread = result.test_signal_delay_spread_ms
            
            # If delay spread is low, burst ToA is highly reliable
            if delay_spread is not None and delay_spread < 2.0:
                agreements.append('TS_timing_high_precision')
                logger.debug(f"{self.channel_name}: High-precision burst ToA={burst_toa:.2f}ms "
                            f"(delay_spread={delay_spread:.1f}ms - clean channel)")
            elif delay_spread is not None and delay_spread > 5.0:
                # High delay spread means multipath - timing less reliable
                disagreements.append('TS_timing_multipath')
                logger.warning(f"{self.channel_name}: Burst ToA={burst_toa:.2f}ms may be degraded "
                              f"(delay_spread={delay_spread:.1f}ms - multipath)")
        
        # === VOTE 12: Spreading Factor - Channel Physics Validation ===
        # The Spreading Factor L = τ_D × f_D combines delay spread and Doppler spread
        # to create a single channel quality metric based on fundamental physics.
        # 
        # Doppler spread f_D ≈ 1/(π × τ_c) where τ_c is coherence time
        # L < 0.05: Underspread channel (clean, timing reliable)
        # L > 1.0:  Overspread channel (severely degraded, timing unreliable)
        #
        # This uses two independent test signal measurements for cross-validation
        if (result.test_signal_delay_spread_ms is not None and 
            result.test_signal_coherence_time_sec is not None):
            
            tau_c = result.test_signal_coherence_time_sec
            tau_D_ms = result.test_signal_delay_spread_ms
            
            # Estimate Doppler spread from coherence time
            if tau_c > 0.01:  # Avoid division by very small values
                f_D_est = 1.0 / (np.pi * tau_c)  # Hz
                
                # Calculate Spreading Factor (dimensionless)
                # L = τ_D (seconds) × f_D (Hz)
                L = (tau_D_ms / 1000.0) * f_D_est
                
                if L > 1.0:
                    # Overspread channel - timing severely unreliable
                    disagreements.append('channel_overspread')
                    logger.warning(f"{self.channel_name}: ❌ Channel overspread: L={L:.3f} (>1.0), "
                                  f"timing severely unreliable (τ_D={tau_D_ms:.1f}ms, τ_c={tau_c:.2f}s)")
                elif L > 0.3:
                    # Moderately spread - timing degraded
                    logger.debug(f"{self.channel_name}: Channel moderately spread: L={L:.3f} "
                                f"(τ_D={tau_D_ms:.1f}ms, τ_c={tau_c:.2f}s)")
                elif L < 0.05:
                    # Underspread - clean channel, high confidence
                    agreements.append('channel_underspread_clean')
                    logger.debug(f"{self.channel_name}: ✓ Channel clean: L={L:.3f} (<0.05), "
                                f"confirms stable channel (τ_D={tau_D_ms:.1f}ms, τ_c={tau_c:.2f}s)")
        
        # === FINAL DECISION ===
        if total_weight > 0:
            # Normalize scores
            wwv_norm = wwv_score / total_weight
            wwvh_norm = wwvh_score / total_weight
            
            # Determine dominant station
            if abs(wwv_norm - wwvh_norm) < 0.15:  # Within ~15% of each other
                dominant_station = 'BALANCED'
                confidence = 'medium'
            elif wwv_norm > wwvh_norm:
                dominant_station = 'WWV'
                # Confidence based on score margin
                margin = wwv_norm - wwvh_norm
                if margin > 0.7:
                    confidence = 'high'
                elif margin > 0.4:
                    confidence = 'medium'
                else:
                    confidence = 'low'
            else:
                dominant_station = 'WWVH'
                margin = wwvh_norm - wwv_norm
                if margin > 0.7:
                    confidence = 'high'
                elif margin > 0.4:
                    confidence = 'medium'
                else:
                    confidence = 'low'
            
            # Update result
            result.dominant_station = dominant_station
            result.confidence = confidence
        
        # Store inter-method agreement/disagreement tracking
        result.inter_method_agreements = agreements if agreements else None
        result.inter_method_disagreements = disagreements if disagreements else None
        
        return result
    
    def detect_440hz_tone(
        self,
        iq_samples: np.ndarray,
        sample_rate: int,
        minute_number: int
    ) -> Tuple[bool, Optional[float]]:
        """
        Detect 440 Hz tone in AM-demodulated signal using coherent integration.
        
        Uses quadrature matched filtering with 1-second segments for ~16 dB 
        processing gain over simple FFT (coherent sum of 44 segments).
        
        Args:
            iq_samples: Complex IQ samples at sample_rate
            sample_rate: Sample rate in Hz (typically 16000)
            minute_number: Minute number (0-59), should be 1 or 2 for 440 Hz
            
        Returns:
            (detected: bool, power_db: Optional[float])
        """
        # 440 Hz tone is only in minutes 1 (WWVH) and 2 (WWV)
        if minute_number not in [1, 2]:
            return False, None
        
        # AM demodulation
        magnitude = np.abs(iq_samples)
        audio_signal = magnitude - np.mean(magnitude)  # AC coupling
        
        # Extract window :15-:59 (44 seconds) where 440 Hz tone is present
        start_sample = int(15.0 * sample_rate)
        end_sample = int(59.0 * sample_rate)
        
        if len(audio_signal) < end_sample:
            end_sample = len(audio_signal)
            if end_sample < start_sample + int(10.0 * sample_rate):
                return False, None
        
        tone_window = audio_signal[start_sample:end_sample]
        
        # === Coherent Integration with Quadrature Matched Filter ===
        # Process in 1-second segments for phase-invariant detection
        # This gives ~16 dB gain: 10*log10(44 segments) ≈ 16.4 dB
        segment_samples = sample_rate  # 1 second per segment
        num_segments = len(tone_window) // segment_samples
        
        if num_segments < 5:  # Need at least 5 seconds
            return False, None
        
        # Create 440 Hz quadrature templates (1 second duration)
        t = np.arange(segment_samples) / sample_rate
        template_i = np.cos(2 * np.pi * 440.0 * t)  # In-phase
        template_q = np.sin(2 * np.pi * 440.0 * t)  # Quadrature
        
        # Apply Hann window to templates
        window = scipy_signal.windows.hann(segment_samples)
        template_i = template_i * window
        template_q = template_q * window
        
        # Coherent integration across segments
        power_sum = 0.0
        noise_sum = 0.0
        
        for i in range(num_segments):
            seg_start = i * segment_samples
            seg_end = seg_start + segment_samples
            segment = tone_window[seg_start:seg_end]
            
            # Quadrature correlation (phase-invariant)
            corr_i = np.sum(segment * template_i)
            corr_q = np.sum(segment * template_q)
            power = corr_i**2 + corr_q**2
            power_sum += np.sqrt(power)  # Amplitude sum for coherent integration
            
            # Estimate noise from segment variance in guard band
            seg_fft = rfft(segment * window)
            seg_freqs = rfftfreq(segment_samples, 1/sample_rate)
            
            # Guard band for noise estimate (away from 440 Hz and harmonics)
            guard_mask = ((seg_freqs >= 300) & (seg_freqs <= 400)) | \
                        ((seg_freqs >= 825) & (seg_freqs <= 875))
            if np.any(guard_mask):
                noise_sum += np.mean(np.abs(seg_fft[guard_mask])**2)
        
        # Calculate SNR
        coherent_power = power_sum**2
        avg_noise = noise_sum / num_segments if num_segments > 0 else 1e-12
        total_noise = avg_noise * num_segments
        
        if total_noise > 0:
            snr_linear = coherent_power / total_noise
            snr_db = 10 * np.log10(snr_linear + 1e-12)
            power_db = 10 * np.log10(coherent_power + 1e-12)
        else:
            snr_db = 0.0
            power_db = -np.inf
        
        # Detection threshold: SNR > 3 dB (lower than before due to better integration)
        detected = snr_db > 3.0
        
        if detected:
            logger.info(f"{self.channel_name}: 440 Hz tone detected in minute {minute_number} - "
                       f"Power: {power_db:.1f}dB, SNR: {snr_db:.1f}dB, segments: {num_segments}")
        else:
            logger.debug(f"{self.channel_name}: 440 Hz NOT detected in minute {minute_number} - "
                        f"SNR: {snr_db:.1f}dB < 3.0 dB threshold")
        
        return detected, power_db if detected else None
    
    def detect_500_600hz_tone(
        self,
        iq_samples: np.ndarray,
        sample_rate: int,
        minute_number: int
    ) -> Tuple[bool, Optional[float], Optional[int], Optional[str]]:
        """
        Detect 500/600 Hz tones for ground truth validation.
        
        During certain minutes, only one station broadcasts these tones:
        - Minute 1: WWV=600 Hz, WWVH=440 Hz (WWV 600 Hz exclusive)
        - Minute 2: WWV=440 Hz, WWVH=600 Hz (WWVH 600 Hz exclusive)
        - Minutes 16,17,19: WWV broadcasts 500/600 Hz, WWVH does NOT (3 minutes)
        - Minutes 43-51: WWVH broadcasts 500/600 Hz, WWV does NOT (9 minutes)
        
        Total: 14 ground truth minutes per hour!
        
        If we detect a 500/600 Hz tone during these exclusive minutes,
        we have ground truth for which station is being received.
        
        Args:
            iq_samples: Complex IQ samples
            sample_rate: Sample rate in Hz
            minute_number: Minute number (0-59)
            
        Returns:
            (detected, power_db, freq_hz, ground_truth_station)
            - detected: Whether 500 or 600 Hz tone was found
            - power_db: Power of detected tone
            - freq_hz: 500 or 600 (which tone was detected)
            - ground_truth_station: 'WWV' or 'WWVH' (which station should be broadcasting)
        """
        # Get schedule for this minute
        schedule = TONE_SCHEDULE_500_600.get(minute_number, {'WWV': None, 'WWVH': None})
        
        wwv_tone = schedule.get('WWV')
        wwvh_tone = schedule.get('WWVH')
        
        # Determine if this is a ground truth minute (only one station broadcasting)
        ground_truth_station = None
        expected_tone = None
        
        # Check if this is a ground truth minute (exclusive broadcast)
        is_ground_truth_minute = False
        if minute_number in WWVH_ONLY_TONE_MINUTES:
            ground_truth_station = 'WWVH'
            expected_tone = wwvh_tone
            is_ground_truth_minute = True
        elif minute_number in WWV_ONLY_TONE_MINUTES:
            ground_truth_station = 'WWV'
            expected_tone = wwv_tone
            is_ground_truth_minute = True
        
        # Always compute harmonic ratios (useful for all minutes), 
        # but only set ground truth for exclusive minutes
        
        # AM demodulation
        magnitude = np.abs(iq_samples)
        audio_signal = magnitude - np.mean(magnitude)
        
        # The 500/600 Hz tone is broadcast throughout the minute (:00 to :45)
        # NOT just the first 800ms like the timing marker
        # Use seconds 15-45 for best SNR (avoid voice announcements at start/end)
        start_sample = int(15.0 * sample_rate)
        end_sample = min(int(45.0 * sample_rate), len(audio_signal))
        
        if end_sample <= start_sample:
            # Not enough data
            return False, None, None, ground_truth_station
        
        tone_window = audio_signal[start_sample:end_sample]
        
        # Window and FFT
        windowed = tone_window * scipy_signal.windows.hann(len(tone_window))
        fft_result = rfft(windowed)
        freqs = rfftfreq(len(windowed), 1/sample_rate)
        
        # Measure power at 500 Hz, 600 Hz, and their 2nd harmonics (1000 Hz, 1200 Hz)
        power_500 = power_600 = power_1000 = power_1200 = 0.0
        
        for target_freq in [500.0, 600.0, 1000.0, 1200.0]:
            freq_idx = np.argmin(np.abs(freqs - target_freq))
            power = np.abs(fft_result[freq_idx])**2
            if target_freq == 500.0:
                power_500 = power
            elif target_freq == 600.0:
                power_600 = power
            elif target_freq == 1000.0:
                power_1000 = power
            else:  # 1200 Hz
                power_1200 = power
        
        # Measure noise floor in guard band (825-875 Hz)
        guard_low_idx = np.argmin(np.abs(freqs - 825.0))
        guard_high_idx = np.argmin(np.abs(freqs - 875.0))
        
        if guard_high_idx > guard_low_idx:
            guard_band_power = np.abs(fft_result[guard_low_idx:guard_high_idx])**2
            noise_power = np.mean(guard_band_power) * 1.5  # ENBW for Hann window
        else:
            noise_power = np.mean(np.abs(fft_result)**2)
        
        # Determine which tone is stronger
        detected_freq = 500 if power_500 > power_600 else 600
        detected_power = max(power_500, power_600)
        
        # Calculate SNR
        if noise_power > 0:
            snr_db = 10 * np.log10(detected_power / noise_power)
            power_db = 10 * np.log10(detected_power + 1e-12)
        else:
            snr_db = 0.0
            power_db = -np.inf
        
        # Detection threshold: SNR > 6 dB
        detected = snr_db > 6.0
        
        # Calculate harmonic power ratios (in dB)
        # P_1000/P_500 and P_1200/P_600 - measures 2nd harmonic contribution
        # Higher ratio when that fundamental is present (due to receiver nonlinearity)
        harmonic_ratio_500_1000 = None
        harmonic_ratio_600_1200 = None
        
        if power_500 > 0:
            harmonic_ratio_500_1000 = 10 * np.log10((power_1000 + 1e-12) / power_500)
        if power_600 > 0:
            harmonic_ratio_600_1200 = 10 * np.log10((power_1200 + 1e-12) / power_600)
        
        # Validate that detected tone matches expected (only for ground truth minutes)
        if is_ground_truth_minute and detected and expected_tone and detected_freq != expected_tone:
            # Detected wrong tone - could be interference or misidentification
            logger.warning(f"{self.channel_name}: Minute {minute_number} - "
                          f"Expected {expected_tone} Hz ({ground_truth_station}) but detected {detected_freq} Hz")
        
        # Only count as "ground truth detected" if it's an exclusive minute
        ground_truth_detected = detected and is_ground_truth_minute
        
        if ground_truth_detected:
            logger.info(f"{self.channel_name}: ✨ {detected_freq} Hz tone detected in minute {minute_number} - "
                       f"Ground truth: {ground_truth_station}, Power: {power_db:.1f}dB, SNR: {snr_db:.1f}dB")
        
        # Always log harmonic ratios if computed
        if harmonic_ratio_500_1000 is not None or harmonic_ratio_600_1200 is not None:
            logger.debug(f"{self.channel_name}: min {minute_number} Harmonic ratios - "
                        f"500→1000: {harmonic_ratio_500_1000:.1f}dB, 600→1200: {harmonic_ratio_600_1200:.1f}dB")
        
        return (ground_truth_detected, power_db if ground_truth_detected else None, 
                detected_freq if ground_truth_detected else None, 
                ground_truth_station if is_ground_truth_minute else None,
                harmonic_ratio_500_1000, harmonic_ratio_600_1200)
    
    def detect_tick_windows(
        self,
        iq_samples: np.ndarray,
        sample_rate: int,
        window_seconds: int = 60
    ) -> List[Dict[str, float]]:
        """
        Detect 5ms tick tones with coherent integration
        
        DISCRIMINATION-FIRST PHILOSOPHY:
        Default uses 60-second window (full minute) for maximum tick stacking.
        This provides √59 ≈ 7.7x SNR improvement over 10-second windows (+8.9 dB).
        
        Implements true coherent integration with phase tracking:
        - Coherent: Phase-aligned amplitude sum → 10*log10(N) dB gain
        - Incoherent: Power sum → 5*log10(N) dB gain
        - Automatically selects best method based on phase stability
        
        Args:
            iq_samples: Full minute of complex IQ samples at sample_rate
            sample_rate: Sample rate in Hz (typically 16000)
            window_seconds: Integration window (60=full minute baseline, 10=legacy)
            
        Returns:
            List of dictionaries (1 for 60s, 6 for 10s windows):
            {
                'second': start second in minute (1 for 60s, varies for 10s),
                    NOTE: Second 0 is EXCLUDED (contains 800ms tone marker)
                'coherent_wwv_snr_db': Coherent integration SNR,
                'coherent_wwvh_snr_db': Coherent integration SNR,
                'incoherent_wwv_snr_db': Incoherent integration SNR,
                'incoherent_wwvh_snr_db': Incoherent integration SNR,
                'coherence_quality_wwv': Phase stability metric (0-1),
                'coherence_quality_wwvh': Phase stability metric (0-1),
                'integration_method': 'coherent' or 'incoherent' (chosen),
                'wwv_snr_db': Best SNR (from chosen method),
                'wwvh_snr_db': Best SNR (from chosen method),
                'ratio_db': wwv_snr - wwvh_snr,
                'tick_count': number of ticks analyzed (59 for 60s, 10 or 9 for 10s)
            }
        """
        # AM demodulation for entire minute
        magnitude = np.abs(iq_samples)
        audio_signal = magnitude - np.mean(magnitude)  # AC coupling
        
        # CRITICAL: Remove station ID tones before tick detection to prevent harmonic contamination
        # WWV/WWVH broadcast 440/500/600 Hz tones throughout each minute per schedule.
        # Receiver 2nd/3rd order nonlinearity creates spurious signals at tick frequencies:
        #   500 Hz × 2 = 1000 Hz (contaminates WWV ticks)
        #   600 Hz × 2 = 1200 Hz (contaminates WWVH ticks)
        #   440 Hz × 3 = 1320 Hz (near WWVH 1200 Hz)
        # Must remove fundamentals to ensure clean, unbiased power measurements.
        
        # 440 Hz notch (Q=20, ~22 Hz width) - prevents 3rd harmonic at 1320 Hz
        b_440, a_440 = iirnotch(440, 20, sample_rate)
        audio_signal = filtfilt(b_440, a_440, audio_signal)
        
        # 500 Hz notch (Q=20, ~25 Hz width) - prevents 2nd harmonic at 1000 Hz
        b_500, a_500 = iirnotch(500, 20, sample_rate)
        audio_signal = filtfilt(b_500, a_500, audio_signal)
        
        # 600 Hz notch (Q=20, ~30 Hz width) - prevents 2nd harmonic at 1200 Hz
        b_600, a_600 = iirnotch(600, 20, sample_rate)
        audio_signal = filtfilt(b_600, a_600, audio_signal)
        
        samples_per_window = window_seconds * sample_rate
        
        # CRITICAL: Skip second 0 (contains 800ms tone marker)
        # For 60s: Single window covering seconds 1-59 (59 ticks)
        # For 10s: Six windows 1-10, 11-20, 21-30, 31-40, 41-50, 51-59
        if window_seconds >= 60:
            num_windows = 1
        else:
            num_windows = 6  # Legacy 10-second windows
        
        results = []
        
        for window_idx in range(num_windows):
            # Start at second 1 (not 0) to avoid 800ms tone marker
            if window_seconds >= 60:
                # Full minute: seconds 1-59 (59 ticks)
                window_start_second = 1
                window_end_second = 60
                actual_window_seconds = 59
            else:
                # Legacy 10-second windows
                window_start_second = 1 + (window_idx * window_seconds)
                # Last window is only 9 seconds (51-59)
                if window_idx == 5:
                    window_end_second = 60
                    actual_window_seconds = 9
                else:
                    window_end_second = window_start_second + window_seconds
                    actual_window_seconds = window_seconds
                    
            window_start_sample = window_start_second * sample_rate
            
            window_end_sample = window_end_second * sample_rate
            
            # Check if we have enough data
            if window_end_sample > len(audio_signal):
                logger.debug(f"{self.channel_name}: Tick window {window_idx} incomplete "
                            f"({len(audio_signal)} < {window_end_sample} samples)")
                break
            
            window_data = audio_signal[window_start_sample:window_end_sample]
            
            # Track both coherent (complex amplitude) and incoherent (power) integration
            wwv_complex_sum = 0.0 + 0.0j  # Coherent sum
            wwvh_complex_sum = 0.0 + 0.0j
            wwv_energy_sum = 0.0  # Incoherent sum
            wwvh_energy_sum = 0.0
            noise_estimate_sum = 0.0
            
            # Track phase for coherence quality measurement
            wwv_phases = []
            wwvh_phases = []
            
            valid_ticks = 0
            
            for tick_idx in range(actual_window_seconds):
                # Extract 100ms window around each tick (±50ms)
                # Ticks occur at :XX.0 seconds within the window
                tick_center_sample = tick_idx * sample_rate
                tick_window_start = max(0, tick_center_sample - int(0.05 * sample_rate))
                tick_window_end = min(len(window_data), tick_center_sample + int(0.05 * sample_rate))
                
                if tick_window_end - tick_window_start < int(0.08 * sample_rate):
                    continue  # Need at least 80ms
                
                tick_window = window_data[tick_window_start:tick_window_end]
                
                # Apply Hann window to reduce spectral leakage
                windowed_tick = tick_window * scipy_signal.windows.hann(len(tick_window))
                
                # Zero-pad to achieve 1 Hz frequency resolution
                # 1 second at sample_rate = 1 Hz bins
                padded_length = sample_rate  # 16000 samples → 1 Hz resolution
                padded_tick = np.pad(windowed_tick, (0, padded_length - len(windowed_tick)), mode='constant')
                
                # FFT to extract complex amplitudes with fine frequency resolution
                fft_result = rfft(padded_tick)
                freqs = rfftfreq(padded_length, 1/sample_rate)
                
                # Extract complex values at WWV (1000 Hz) and WWVH (1200 Hz)
                wwv_freq_idx = np.argmin(np.abs(freqs - 1000.0))
                wwvh_freq_idx = np.argmin(np.abs(freqs - 1200.0))
                
                wwv_complex = fft_result[wwv_freq_idx]  # Complex amplitude
                wwvh_complex = fft_result[wwvh_freq_idx]
                
                # Phase tracking for coherence quality
                wwv_phase = np.angle(wwv_complex)
                wwvh_phase = np.angle(wwvh_complex)
                
                # Phase correction: align to first tick's phase
                if valid_ticks == 0:
                    # Reference phase from first tick
                    wwv_ref_phase = wwv_phase
                    wwvh_ref_phase = wwvh_phase
                else:
                    # Correct phase drift (simple first-order correction)
                    wwv_phase_correction = np.exp(-1j * (wwv_phase - wwv_ref_phase))
                    wwvh_phase_correction = np.exp(-1j * (wwvh_phase - wwvh_ref_phase))
                    
                    wwv_complex *= wwv_phase_correction
                    wwvh_complex *= wwvh_phase_correction
                
                # Coherent integration: sum complex amplitudes
                wwv_complex_sum += wwv_complex
                wwvh_complex_sum += wwvh_complex
                
                # Incoherent integration: sum power
                wwv_energy = np.abs(wwv_complex)**2
                wwvh_energy = np.abs(wwvh_complex)**2
                wwv_energy_sum += wwv_energy
                wwvh_energy_sum += wwvh_energy
                
                # Track phases for coherence quality
                wwv_phases.append(wwv_phase)
                wwvh_phases.append(wwvh_phase)
                
                # Measure noise power density in clean guard band
                # Use 825-875 Hz (50 Hz band, below both signals and modulation sidebands)
                # Avoids:
                #   WWV: 1000 ± 100 Hz = 900-1100 Hz
                #   WWVH: 1200 ± 100 Hz = 1100-1300 Hz
                noise_low_idx = np.argmin(np.abs(freqs - 825.0))
                noise_high_idx = np.argmin(np.abs(freqs - 875.0))
                noise_bins = fft_result[noise_low_idx:noise_high_idx]
                
                if len(noise_bins) > 0:
                    # Total noise power in 50 Hz band
                    total_noise_power = np.mean(np.abs(noise_bins)**2)
                    # Normalize to power spectral density (W/Hz)
                    noise_bandwidth_hz = 50.0
                    noise_power_density = total_noise_power / noise_bandwidth_hz
                    noise_estimate_sum += noise_power_density
                else:
                    noise_estimate_sum += 1e-12
                
                valid_ticks += 1
            
            # Calculate average noise power density per tick
            if valid_ticks > 0 and noise_estimate_sum > 0:
                N0 = noise_estimate_sum / valid_ticks  # Average noise power density (W/Hz)
                
                # Signal filter bandwidth (effective)
                # Hann window ENBW = 1.5 × frequency resolution
                # With 1 Hz FFT bins (1 second zero-padding), ENBW = 1.5 Hz
                B_signal = 1.5  # Hz (Hann window ENBW)
                
                # ===== COHERENT INTEGRATION (10 dB gain) =====
                # Power from coherent sum of complex amplitudes
                wwv_coherent_power = np.abs(wwv_complex_sum)**2
                wwvh_coherent_power = np.abs(wwvh_complex_sum)**2
                
                # SNR with proper bandwidth normalization
                # SNR = S / (N₀ × B_signal × N_ticks)
                noise_power_coherent = N0 * B_signal * valid_ticks
                coherent_wwv_snr = 10 * np.log10(wwv_coherent_power / noise_power_coherent) if wwv_coherent_power > 0 else -100
                coherent_wwvh_snr = 10 * np.log10(wwvh_coherent_power / noise_power_coherent) if wwvh_coherent_power > 0 else -100
                
                # ===== INCOHERENT INTEGRATION (5 dB gain) =====
                # Sum of power (energy)
                noise_power_incoherent = N0 * B_signal * valid_ticks
                incoherent_wwv_snr = 10 * np.log10(wwv_energy_sum / noise_power_incoherent) if wwv_energy_sum > 0 else -100
                incoherent_wwvh_snr = 10 * np.log10(wwvh_energy_sum / noise_power_incoherent) if wwvh_energy_sum > 0 else -100
                
                # ===== COHERENCE QUALITY METRIC =====
                # Measure phase stability: low variance = high coherence
                # Quality = 1 - (phase_variance / π²)  → ranges 0 (random) to 1 (stable)
                wwv_coherence_quality = 0.0
                wwvh_coherence_quality = 0.0
                
                if len(wwv_phases) > 1:
                    # Unwrap phases to handle 2π discontinuities
                    wwv_phases_unwrapped = np.unwrap(wwv_phases)
                    wwv_phase_variance = np.var(wwv_phases_unwrapped)
                    # Normalize: perfect coherence = 0 variance, random = π²/3 variance
                    wwv_coherence_quality = max(0.0, min(1.0, 1.0 - (wwv_phase_variance / (np.pi**2 / 3))))
                
                if len(wwvh_phases) > 1:
                    wwvh_phases_unwrapped = np.unwrap(wwvh_phases)
                    wwvh_phase_variance = np.var(wwvh_phases_unwrapped)
                    wwvh_coherence_quality = max(0.0, min(1.0, 1.0 - (wwvh_phase_variance / (np.pi**2 / 3))))
                
                # ===== CHOOSE INTEGRATION METHOD =====
                # Use coherent integration if it yields significantly higher SNR
                # If coherent SNR > incoherent SNR + threshold, coherence is real
                # Otherwise fall back to incoherent (more robust)
                coherent_snr_advantage_threshold = 3.0  # dB
                
                # Check if coherent method provides real SNR improvement
                wwv_coherent_advantage = coherent_wwv_snr - incoherent_wwv_snr
                wwvh_coherent_advantage = coherent_wwvh_snr - incoherent_wwvh_snr
                
                # Use coherent if BOTH stations show coherent advantage
                if (wwv_coherent_advantage >= coherent_snr_advantage_threshold and 
                    wwvh_coherent_advantage >= coherent_snr_advantage_threshold):
                    integration_method = 'coherent'
                    wwv_snr = coherent_wwv_snr
                    wwvh_snr = coherent_wwvh_snr
                else:
                    integration_method = 'incoherent'
                    wwv_snr = incoherent_wwv_snr
                    wwvh_snr = incoherent_wwvh_snr
                
                ratio_db = wwv_snr - wwvh_snr
                
                # ABSOLUTE POWER (for inter-method agreement)
                # Use incoherent energy sum for absolute power comparison
                # This is comparable to FFT-based power measurement
                wwv_power_db = 10 * np.log10(wwv_energy_sum) if wwv_energy_sum > 0 else -100
                wwvh_power_db = 10 * np.log10(wwvh_energy_sum) if wwvh_energy_sum > 0 else -100
                power_ratio_db = wwv_power_db - wwvh_power_db
                
                # Convert noise power density to dB (relative to 1.0 = 0 dB)
                # This is N₀ in dBW/Hz
                noise_power_density_db = 10 * np.log10(N0) if N0 > 0 else -100
                
                # Calculate mean phase for Doppler estimation
                wwv_mean_phase = float(np.mean(wwv_phases_unwrapped)) if len(wwv_phases) > 1 else 0.0
                wwvh_mean_phase = float(np.mean(wwvh_phases_unwrapped)) if len(wwvh_phases) > 1 else 0.0
                
                results.append({
                    'second': window_start_second,  # Actual start second (skips second 0)
                    # Best SNR (chosen method) - relative to noise floor
                    'wwv_snr_db': float(wwv_snr),
                    'wwvh_snr_db': float(wwvh_snr),
                    'ratio_db': float(ratio_db),  # SNR ratio (legacy)
                    # ABSOLUTE POWER (for inter-method agreement)
                    'wwv_power_db': float(wwv_power_db),
                    'wwvh_power_db': float(wwvh_power_db),
                    'power_ratio_db': float(power_ratio_db),  # Absolute power ratio
                    # Coherent results
                    'coherent_wwv_snr_db': float(coherent_wwv_snr),
                    'coherent_wwvh_snr_db': float(coherent_wwvh_snr),
                    # Incoherent results
                    'incoherent_wwv_snr_db': float(incoherent_wwv_snr),
                    'incoherent_wwvh_snr_db': float(incoherent_wwvh_snr),
                    # Coherence quality
                    'coherence_quality_wwv': float(wwv_coherence_quality),
                    'coherence_quality_wwvh': float(wwvh_coherence_quality),
                    # Phase for Doppler tracking (unwrapped mean)
                    'wwv_phase_rad': wwv_mean_phase,
                    'wwvh_phase_rad': wwvh_mean_phase,
                    # Integration method selection
                    'integration_method': integration_method,
                    'tick_count': valid_ticks,
                    # Noise floor for this window
                    'noise_power_density_db': float(noise_power_density_db),
                    # Signal filter bandwidth for diagnostics
                    'signal_bandwidth_hz': float(B_signal)
                })
                
                logger.info(f"{self.channel_name}: Tick window {window_idx} (sec {window_start_second}-{window_end_second-1}): "
                           f"{integration_method.upper()} - WWV={wwv_snr:.1f}dB, WWVH={wwvh_snr:.1f}dB, Ratio={ratio_db:+.1f}dB "
                           f"(coherence: WWV={wwv_coherence_quality:.2f}, WWVH={wwvh_coherence_quality:.2f}, {valid_ticks} ticks)")
            else:
                # No valid ticks in this window
                results.append({
                    'second': window_start_second,  # Actual start second (skips second 0)
                    'wwv_snr_db': -100.0,
                    'wwvh_snr_db': -100.0,
                    'ratio_db': 0.0,
                    # Absolute power (for inter-method agreement)
                    'wwv_power_db': -100.0,
                    'wwvh_power_db': -100.0,
                    'power_ratio_db': 0.0,
                    'coherent_wwv_snr_db': -100.0,
                    'coherent_wwvh_snr_db': -100.0,
                    'incoherent_wwv_snr_db': -100.0,
                    'incoherent_wwvh_snr_db': -100.0,
                    'coherence_quality_wwv': 0.0,
                    'coherence_quality_wwvh': 0.0,
                    'integration_method': 'none',
                    'tick_count': 0,
                    'noise_power_density_db': -100.0
                })
        
        return results
    
    def extract_per_tick_phases(
        self,
        iq_samples: np.ndarray,
        sample_rate: int,
        snr_threshold_db: float = 0.0  # Lowered from 10 dB - uses narrow noise band reference
    ) -> Dict:
        """
        Extract per-second tick phases for Doppler estimation.
        
        This method extracts the complex amplitude (phasor) of the 1000 Hz (WWV) and
        1200 Hz (WWVH) tones from each 5ms tick, providing 58 phase measurements per
        minute (seconds 1-58, skipping second 0 which has the 800ms marker tone).
        
        Args:
            iq_samples: Full minute of complex IQ samples (16 kHz, 60 seconds)
            sample_rate: Sample rate in Hz (typically 16000)
            snr_threshold_db: Minimum SNR for reliable phase measurement (default 0 dB)
            
        Returns:
            Dictionary with per-tick phase measurements for WWV and WWVH
        """
        # AM demodulation
        magnitude = np.abs(iq_samples)
        audio_signal = magnitude - np.mean(magnitude)  # AC coupling
        
        # Remove harmonic-generating tones (440/500/600 Hz)
        b_440, a_440 = iirnotch(440, 20, sample_rate)
        audio_signal = filtfilt(b_440, a_440, audio_signal)
        b_500, a_500 = iirnotch(500, 20, sample_rate)
        audio_signal = filtfilt(b_500, a_500, audio_signal)
        b_600, a_600 = iirnotch(600, 20, sample_rate)
        audio_signal = filtfilt(b_600, a_600, audio_signal)
        
        samples_per_second = sample_rate
        tick_duration_samples = int(0.005 * sample_rate)  # 5ms tick
        
        # FFT parameters for fine frequency resolution
        # Zero-pad to 1 second for 1 Hz resolution
        padded_length = sample_rate
        
        wwv_phases = []
        wwvh_phases = []
        wwv_complex_amps = []
        wwvh_complex_amps = []
        noise_estimates = []
        
        # Process seconds 1-58 (skip second 0 with 800ms marker, and 59 for safety margin)
        for second in range(1, 59):
            start_sample = second * samples_per_second
            end_sample = start_sample + tick_duration_samples
            
            if end_sample > len(audio_signal):
                break
            
            # Extract 5ms tick and apply Hann window
            tick_samples = audio_signal[start_sample:end_sample]
            windowed_tick = tick_samples * np.hanning(len(tick_samples))
            
            # Zero-pad to 1 second for 1 Hz FFT resolution
            padded_tick = np.pad(windowed_tick, (0, padded_length - len(windowed_tick)), mode='constant')
            
            # FFT to extract complex amplitudes
            fft_result = rfft(padded_tick)
            freqs = rfftfreq(padded_length, 1/sample_rate)
            
            # Extract complex values at WWV (1000 Hz) and WWVH (1200 Hz)
            wwv_freq_idx = np.argmin(np.abs(freqs - 1000.0))
            wwvh_freq_idx = np.argmin(np.abs(freqs - 1200.0))
            
            wwv_complex = fft_result[wwv_freq_idx]
            wwvh_complex = fft_result[wwvh_freq_idx]
            
            # Measure noise in 825-875 Hz guard band
            noise_low_idx = np.argmin(np.abs(freqs - 825.0))
            noise_high_idx = np.argmin(np.abs(freqs - 875.0))
            noise_bins = fft_result[noise_low_idx:noise_high_idx]
            
            if len(noise_bins) > 0:
                noise_power = np.mean(np.abs(noise_bins)**2)
                noise_estimates.append(noise_power)
            else:
                noise_power = 1e-12
            
            # Calculate SNR for each tone
            wwv_power = np.abs(wwv_complex)**2
            wwvh_power = np.abs(wwvh_complex)**2
            
            wwv_snr_db = 10 * np.log10(wwv_power / noise_power) if noise_power > 0 else -100
            wwvh_snr_db = 10 * np.log10(wwvh_power / noise_power) if noise_power > 0 else -100
            
            # Debug: Print first tick's SNR values to stderr
            if second == 1:
                import sys
                print(f"DEBUG {self.channel_name}: Phase SNR: WWV={wwv_snr_db:.1f}dB, WWVH={wwvh_snr_db:.1f}dB (threshold={snr_threshold_db}dB)", file=sys.stderr)
            
            # Extract phase (only if SNR is sufficient for reliable measurement)
            wwv_phase = np.angle(wwv_complex)
            wwvh_phase = np.angle(wwvh_complex)
            
            # Store results with SNR qualification
            if wwv_snr_db >= snr_threshold_db:
                wwv_phases.append((second, float(wwv_phase), float(wwv_snr_db)))
                wwv_complex_amps.append((second, complex(wwv_complex)))
            
            if wwvh_snr_db >= snr_threshold_db:
                wwvh_phases.append((second, float(wwvh_phase), float(wwvh_snr_db)))
                wwvh_complex_amps.append((second, complex(wwvh_complex)))
        
        # Calculate noise floor
        noise_floor_db = 10 * np.log10(np.mean(noise_estimates)) if noise_estimates else -100
        
        # Log at INFO level if one station has significantly fewer phases
        if len(wwv_phases) < 10 or len(wwvh_phases) < 10:
            # Get average SNR for debugging
            avg_wwv_snr = np.mean([p[2] for p in wwv_phases]) if wwv_phases else -100
            avg_wwvh_snr = np.mean([p[2] for p in wwvh_phases]) if wwvh_phases else -100
            logger.info(f"{self.channel_name}: Phase extraction: WWV={len(wwv_phases)} (avg SNR {avg_wwv_snr:.1f}dB), "
                       f"WWVH={len(wwvh_phases)} (avg SNR {avg_wwvh_snr:.1f}dB), "
                       f"threshold={snr_threshold_db} dB, noise_floor={noise_floor_db:.1f} dB")
        else:
            logger.debug(f"{self.channel_name}: Extracted {len(wwv_phases)} WWV and {len(wwvh_phases)} WWVH "
                        f"tick phases (SNR threshold={snr_threshold_db} dB, noise floor={noise_floor_db:.1f} dB)")
        
        return {
            'wwv_phases': wwv_phases,
            'wwvh_phases': wwvh_phases,
            'wwv_complex': wwv_complex_amps,
            'wwvh_complex': wwvh_complex_amps,
            'valid_tick_count': max(len(wwv_phases), len(wwvh_phases)),
            'noise_floor_db': float(noise_floor_db)
        }
    
    def estimate_doppler_shift_from_ticks(
        self,
        iq_samples: np.ndarray,
        sample_rate: int,
        snr_threshold_db: float = 0.0  # Lowered from 10 dB - per-tick SNR uses different noise ref
    ) -> Optional[Dict[str, float]]:
        """
        Estimate instantaneous Doppler shift from per-tick phase progression.
        
        Uses the adjacent pulse phase difference method:
            Δf_D,k = Δφ_k / (2π × 1s)
        
        where Δφ_k is the unwrapped phase difference between tick k and tick k-1.
        
        This provides ~57 instantaneous Doppler measurements per minute, enabling
        accurate determination of the maximum coherent integration window:
            T_max ≈ 1 / (4 × |Δf_D|)
        
        Args:
            iq_samples: Full minute of complex IQ samples
            sample_rate: Sample rate in Hz
            snr_threshold_db: Minimum SNR for reliable phase tracking
            
        Returns:
            Dictionary with:
                - wwv_doppler_hz: Mean Doppler shift for WWV (Hz)
                - wwvh_doppler_hz: Mean Doppler shift for WWVH (Hz)
                - wwv_doppler_std_hz: Doppler variability (Hz)
                - wwvh_doppler_std_hz: Doppler variability (Hz)
                - max_coherent_window_sec: Maximum window for π/4 phase error
                - doppler_quality: Confidence metric (0-1)
                - phase_variance_rad: RMS phase deviation from linear fit
                - instantaneous_doppler: List of per-second Doppler measurements
            Returns None if insufficient high-SNR ticks available
        """
        # Extract per-tick phases
        tick_data = self.extract_per_tick_phases(iq_samples, sample_rate, snr_threshold_db)
        
        wwv_phases = tick_data['wwv_phases']
        wwvh_phases = tick_data['wwvh_phases']
        
        if len(wwv_phases) < 10 and len(wwvh_phases) < 10:
            logger.debug(f"{self.channel_name}: Insufficient high-SNR ticks for Doppler estimation "
                        f"(WWV: {len(wwv_phases)}, WWVH: {len(wwvh_phases)})")
            return None
        
        def compute_doppler_from_phases(phases_list):
            """Compute Doppler shift from list of (second, phase, snr) tuples."""
            if len(phases_list) < 10:
                return None, None, None, []
            
            # Extract times and phases
            times = np.array([p[0] for p in phases_list])
            phases = np.array([p[1] for p in phases_list])
            
            # Unwrap phases to handle 2π discontinuities
            phases_unwrapped = np.unwrap(phases)
            
            # Method 1: Linear regression for mean Doppler
            # φ(t) = 2π·Δf_D·t + φ₀
            coeffs = np.polyfit(times, phases_unwrapped, deg=1)
            mean_doppler_hz = coeffs[0] / (2 * np.pi)
            
            # Method 2: Adjacent difference for instantaneous Doppler
            # Δf_D,k = (φ_k - φ_{k-1}) / (2π × Δt)
            instantaneous_doppler = []
            for i in range(1, len(phases_list)):
                dt = times[i] - times[i-1]
                if dt > 0:
                    dphi = phases_unwrapped[i] - phases_unwrapped[i-1]
                    inst_doppler = dphi / (2 * np.pi * dt)
                    instantaneous_doppler.append({
                        'second': int(times[i]),
                        'doppler_hz': float(inst_doppler),
                        'snr_db': float(phases_list[i][2])
                    })
            
            # Doppler variability (standard deviation)
            if instantaneous_doppler:
                doppler_values = [d['doppler_hz'] for d in instantaneous_doppler]
                doppler_std = np.std(doppler_values)
            else:
                doppler_std = 0.0
            
            # Phase fit quality
            fit_phases = np.polyval(coeffs, times)
            residuals = phases_unwrapped - fit_phases
            phase_variance = np.var(residuals)
            
            return mean_doppler_hz, doppler_std, phase_variance, instantaneous_doppler
        
        # Compute for WWV
        wwv_doppler, wwv_std, wwv_var, wwv_inst = compute_doppler_from_phases(wwv_phases)
        
        # Compute for WWVH
        wwvh_doppler, wwvh_std, wwvh_var, wwvh_inst = compute_doppler_from_phases(wwvh_phases)
        
        # Use whichever station has more valid measurements
        if wwv_doppler is None and wwvh_doppler is None:
            return None
        
        # Default to 0 if one station missing
        wwv_doppler = wwv_doppler or 0.0
        wwvh_doppler = wwvh_doppler or 0.0
        wwv_std = wwv_std or 0.0
        wwvh_std = wwvh_std or 0.0
        wwv_var = wwv_var or 0.0
        wwvh_var = wwvh_var or 0.0
        
        # Calculate maximum coherent integration window
        # Limit phase error to π/4 (45°) for <3 dB coherent loss
        # T_max = π/4 / (2π × |Δf_D|) = 1 / (8 × |Δf_D|)
        max_doppler = max(abs(wwv_doppler), abs(wwvh_doppler))
        if max_doppler > 0.001:  # Avoid division by zero
            max_coherent_window = 1.0 / (8.0 * max_doppler)
        else:
            max_coherent_window = 60.0  # Stable channel, no Doppler limit
        
        # Clamp to reasonable range
        max_coherent_window = min(max_coherent_window, 60.0)
        
        # Quality metric from phase fit residuals
        # Quality: 1.0 = perfect fit, 0.0 = random phase (variance = π²/3)
        phase_variance = max(wwv_var, wwvh_var)
        doppler_quality = max(0.0, min(1.0, 1.0 - (phase_variance / (np.pi**2 / 3))))
        
        logger.info(f"{self.channel_name}: Doppler estimate (per-tick): "
                   f"WWV={wwv_doppler:+.4f}±{wwv_std:.4f} Hz, "
                   f"WWVH={wwvh_doppler:+.4f}±{wwvh_std:.4f} Hz, "
                   f"T_max={max_coherent_window:.1f}s, quality={doppler_quality:.2f}")
        
        return {
            'wwv_doppler_hz': float(wwv_doppler),
            'wwvh_doppler_hz': float(wwvh_doppler),
            'wwv_doppler_std_hz': float(wwv_std),
            'wwvh_doppler_std_hz': float(wwvh_std),
            'max_coherent_window_sec': float(max_coherent_window),
            'doppler_quality': float(doppler_quality),
            'phase_variance_rad': float(np.sqrt(phase_variance)),
            'wwv_instantaneous_doppler': wwv_inst,
            'wwvh_instantaneous_doppler': wwvh_inst
        }
    
    def estimate_doppler_shift(
        self,
        tick_results: List[Dict[str, float]]
    ) -> Optional[Dict[str, float]]:
        """
        Estimate instantaneous Doppler shift from tick phase progression.
        
        Uses phase tracking of 1000 Hz (WWV) and 1200 Hz (WWVH) tones across
        consecutive ticks to measure ionospheric Doppler shift. This determines
        the maximum coherent integration window before phase rotation degrades SNR.
        
        Args:
            tick_results: List of tick window dictionaries from detect_tick_windows()
        
        Returns:
            Dictionary with:
                - wwv_doppler_hz: Doppler shift for WWV signal (Hz)
                - wwvh_doppler_hz: Doppler shift for WWVH signal (Hz)
                - max_coherent_window_sec: Maximum window for π/4 phase error
                - doppler_quality: Confidence metric (0-1, based on fit residuals)
                - phase_variance_rad: RMS phase deviation from linear fit
            Returns None if insufficient high-SNR ticks available
        """
        if not tick_results or len(tick_results) < 10:
            return None
        
        # Extract WWV phases from high-SNR ticks (need clean phase measurements)
        wwv_phases = []
        wwvh_phases = []
        times_sec = []
        
        for i, tick in enumerate(tick_results):
            # Require high SNR for reliable phase tracking (noise doesn't dominate phase)
            if tick.get('wwv_snr_db', -100) > 10.0:
                times_sec.append(tick.get('second', i))
                wwv_phases.append(tick.get('wwv_phase_rad', 0.0))
            
            if tick.get('wwvh_snr_db', -100) > 10.0:
                if len(wwvh_phases) < len(wwv_phases):  # Keep arrays aligned
                    wwvh_phases.append(tick.get('wwvh_phase_rad', 0.0))
        
        if len(wwv_phases) < 10:
            logger.debug(f"{self.channel_name}: Insufficient high-SNR ticks for Doppler estimation")
            return None
        
        # Unwrap phase to handle 2π discontinuities
        wwv_unwrapped = np.unwrap(wwv_phases)
        
        # Linear regression: φ(t) = 2π·Δf_D·t + φ₀
        # Slope gives Doppler shift in rad/s, convert to Hz
        wwv_coeffs = np.polyfit(times_sec, wwv_unwrapped, deg=1)
        wwv_doppler_hz = wwv_coeffs[0] / (2 * np.pi)
        
        # Repeat for WWVH
        wwvh_unwrapped = np.unwrap(wwvh_phases) if len(wwvh_phases) >= 10 else wwv_unwrapped
        wwvh_coeffs = np.polyfit(times_sec, wwvh_unwrapped, deg=1) if len(wwvh_phases) >= 10 else wwv_coeffs
        wwvh_doppler_hz = wwvh_coeffs[0] / (2 * np.pi)  # [0] is slope, [1] is intercept
        
        # Calculate maximum coherent integration window
        # Limit phase error to π/4 (45°) for <3 dB coherent loss
        max_doppler = max(abs(wwv_doppler_hz), abs(wwvh_doppler_hz))
        if max_doppler > 0.001:  # Avoid division by zero
            max_coherent_window = 1.0 / (8.0 * max_doppler)
        else:
            max_coherent_window = 60.0  # Stable channel, no Doppler limit
        
        # Quality metric from phase fit residuals
        wwv_fit = np.polyval(wwv_coeffs, times_sec)
        phase_residuals = wwv_unwrapped - wwv_fit
        phase_variance = np.var(phase_residuals)
        
        # Quality: 1.0 = perfect fit, 0.0 = random phase (variance = π²/3)
        doppler_quality = max(0.0, min(1.0, 1.0 - (phase_variance / (np.pi**2 / 3))))
        
        logger.info(f"{self.channel_name}: Doppler estimate: "
                   f"WWV={wwv_doppler_hz:+.3f} Hz, WWVH={wwvh_doppler_hz:+.3f} Hz, "
                   f"max_window={max_coherent_window:.1f}s, quality={doppler_quality:.2f}")
        
        return {
            'wwv_doppler_hz': float(wwv_doppler_hz),
            'wwvh_doppler_hz': float(wwvh_doppler_hz),
            'max_coherent_window_sec': float(max_coherent_window),
            'doppler_quality': float(doppler_quality),
            'phase_variance_rad': float(np.sqrt(phase_variance))
        }
    
    def bcd_correlation_discrimination(
        self,
        iq_samples: np.ndarray,
        sample_rate: int,
        minute_timestamp: float,
        frequency_mhz: Optional[float] = None,
        window_seconds: float = 10,
        step_seconds: float = 1,
        adaptive: bool = False,
        enable_single_station_detection: bool = True,
        timing_power_ratio_db: Optional[float] = None,  # WWV-WWVH power from 1000/1200 Hz (positive=WWV stronger)
        ground_truth_station: Optional[str] = None,  # From 500/600 Hz exclusive minutes ('WWV' or 'WWVH')
        wwv_tick_snr_db: Optional[float] = None,  # SNR of 1000 Hz tick
        wwvh_tick_snr_db: Optional[float] = None,  # SNR of 1200 Hz tick
        downsample_factor: int = 4  # Downsample for CPU efficiency (4x = 5kHz, safe for 100Hz BCD)
    ) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float], List[Dict[str, float]]]:
        """
        Discriminate WWV/WWVH using 100 Hz BCD cross-correlation with sliding windows
        
        Both WWV and WWVH transmit the IDENTICAL 100 Hz BCD time code simultaneously.
        By cross-correlating the received 100 Hz signal against the expected template,
        we get two peaks separated by the ionospheric differential delay (~10-20ms).
        
        COHERENCE-LIMITED WINDOWING (2025-11-26 fix):
        Default uses 10-second windows with 1-second sliding steps. This keeps T_int
        within typical HF ionospheric coherence time (Tc ~10-20s), preventing Doppler-
        induced phase rotation from destroying correlation quality.
        
        WHY 10 SECONDS:
        - Within typical HF coherence time (Tc ~10-20s for quiet ionosphere)
        - Provides √10 = 3.2x SNR improvement over 1-second (+5 dB)
        - 1-second sliding step produces ~50 windows/minute for time-series tracking
        - Captures propagation dynamics (fading, multipath variations)
        
        WHY NOT 60 SECONDS:
        - Exceeds Tc under typical conditions, causing phase rotation
        - Doppler shift of ±0.1 Hz causes 37.7 radians rotation over 60s
        - Averages over multiple fading periods, destroying amplitude information
        
        This method completely avoids the 1000/1200 Hz time marker tone separation problem!
        The 100 Hz BCD signal is the actual carrier.
        
        Args:
            iq_samples: Full minute of complex IQ samples
            sample_rate: Sample rate in Hz (typically 16000)
            minute_timestamp: UTC timestamp of minute boundary
            frequency_mhz: Operating frequency for geographic ToA prediction (optional)
            window_seconds: Integration window length (default 10s, within typical Tc)
            step_seconds: Sliding step size (default 1s for high-resolution time-series)
            adaptive: Enable adaptive window recommendations (default False, use Doppler-adaptive wrapper)
            enable_single_station_detection: Use geographic predictor for single peaks (default True)
            
        Returns:
            Tuple of (wwv_amp_mean, wwvh_amp_mean, delay_mean, quality_mean, windows_list)
            Scalar values are means across all windows; windows_list contains time-series data
            Returns (None, None, None, None, None) if correlation fails
        """
        try:
            # Step 1: Extract 100 Hz BCD tone from the combined IQ signal
            # BCD is amplitude modulation of a 100 Hz subcarrier, independent of 1000/1200 Hz ID tones
            # Both WWV and WWVH transmit the same BCD pattern on 100 Hz
            
            # Bandpass filter around 100 Hz to isolate BCD subcarrier
            nyquist = sample_rate / 2
            bcd_low_norm = 50 / nyquist   # 50-150 Hz captures 100 Hz BCD
            bcd_high_norm = 150 / nyquist
            sos_bcd = scipy_signal.butter(4, [bcd_low_norm, bcd_high_norm], 'bandpass', output='sos')
            bcd_100hz = scipy_signal.sosfilt(sos_bcd, iq_samples)
            
            # Step 2: Use the bandpass-filtered 100 Hz signal directly for correlation
            # The 100 Hz carrier IS the BCD signal - correlate directly with template
            # For complex IQ, take real part since template is real
            if np.iscomplexobj(bcd_100hz):
                bcd_signal = np.real(bcd_100hz)
            else:
                bcd_signal = bcd_100hz
            
            # Normalize signal for correlation
            bcd_signal = bcd_signal - np.mean(bcd_signal)
            
            # Step 2b: Downsample for CPU efficiency (BCD is 100 Hz, 5 kHz is 50x oversampling)
            # This reduces correlation CPU by ~75% with negligible accuracy loss
            # Timing resolution at 5 kHz = 0.2 ms, well within ionospheric uncertainty (~3-10 ms)
            effective_sample_rate = sample_rate
            if downsample_factor > 1:
                # Anti-alias filter before decimation (already bandpass filtered to 50-150 Hz)
                # scipy.signal.decimate applies proper anti-aliasing
                bcd_signal = scipy_signal.decimate(bcd_signal, downsample_factor, ftype='fir', zero_phase=True)
                effective_sample_rate = sample_rate // downsample_factor
                logger.debug(f"{self.channel_name}: BCD downsampled {sample_rate}→{effective_sample_rate} Hz ({downsample_factor}x)")
            
            # Step 3: Generate expected BCD template for this minute (full 60 seconds)
            # Template includes 100 Hz carrier modulated by BCD pattern
            # Generate at effective (downsampled) rate
            bcd_template_full = self._generate_bcd_template(minute_timestamp, effective_sample_rate, envelope_only=False)
            
            if bcd_template_full is None:
                logger.warning(f"{self.channel_name}: Failed to generate BCD template")
                return None, None, None, None, None
            
            # Step 5: Sliding window correlation to find delay AND amplitudes
            # The 100 Hz BCD signal IS the carrier - both stations transmit on 100 Hz
            # Correlation peak heights give us the individual station amplitudes
            window_samples = int(window_seconds * effective_sample_rate)
            step_samples = int(step_seconds * effective_sample_rate)
            
            # Calculate number of windows - CRITICAL: limit by BOTH signal AND template length
            # Template is exactly 60 seconds; signal may be longer
            total_samples = len(bcd_signal)
            template_samples = len(bcd_template_full)
            max_start_sample = min(total_samples, template_samples) - window_samples
            
            if max_start_sample <= 0:
                logger.warning(f"{self.channel_name}: BCD signal ({total_samples}) or template ({template_samples}) "
                              f"too short for {window_seconds}s window ({window_samples} samples)")
                return None, None, None, None, None
            
            num_windows = max_start_sample // step_samples + 1
            
            windows_data = []
            
            for i in range(num_windows):
                start_sample = int(i * step_samples)
                end_sample = int(start_sample + window_samples)
                
                # Safety check - skip if we'd exceed template bounds
                if end_sample > template_samples:
                    break
                    
                window_start_time = start_sample / effective_sample_rate  # Seconds into the minute
                
                # Extract BCD signal window and template
                signal_window = bcd_signal[start_sample:end_sample]
                template_window = bcd_template_full[start_sample:end_sample]
                
                # Cross-correlate to find two peaks (WWV and WWVH arrivals)
                correlation = scipy_signal.correlate(signal_window, template_window, mode='full', method='fft')
                correlation = np.abs(correlation)
                
                # Zero-lag is at index len(template_window) - 1
                zero_lag_idx = len(template_window) - 1
                
                # Use geographic predictor for targeted peak search if available
                # With improved timing, we know where to look for each station's peak
                if self.geo_predictor and frequency_mhz:
                    expected = self.geo_predictor.calculate_expected_delays(frequency_mhz)
                    wwv_expected_ms = expected['wwv_delay_ms']
                    wwvh_expected_ms = expected['wwvh_delay_ms']
                    
                    # Search ±15ms around each expected delay (tight window with good timing)
                    search_window_ms = 15.0
                    search_window_samples = int(search_window_ms * effective_sample_rate / 1000)
                    
                    # WWV search window
                    wwv_center_idx = zero_lag_idx + int(wwv_expected_ms * effective_sample_rate / 1000)
                    wwv_start = max(0, wwv_center_idx - search_window_samples)
                    wwv_end = min(len(correlation), wwv_center_idx + search_window_samples)
                    
                    # WWVH search window
                    wwvh_center_idx = zero_lag_idx + int(wwvh_expected_ms * effective_sample_rate / 1000)
                    wwvh_start = max(0, wwvh_center_idx - search_window_samples)
                    wwvh_end = min(len(correlation), wwvh_center_idx + search_window_samples)
                    
                    # Find best peak in each window
                    wwv_region = correlation[wwv_start:wwv_end]
                    wwvh_region = correlation[wwvh_start:wwvh_end]
                    
                    wwv_peak_local = np.argmax(wwv_region)
                    wwvh_peak_local = np.argmax(wwvh_region)
                    
                    wwv_peak_idx = wwv_start + wwv_peak_local
                    wwvh_peak_idx = wwvh_start + wwvh_peak_local
                    
                    wwv_peak_height = float(wwv_region[wwv_peak_local])
                    wwvh_peak_height = float(wwvh_region[wwvh_peak_local])
                    
                    # Noise floor for quality calculation
                    noise_floor = np.median(correlation)
                    
                    # Build peaks array in order (early, late)
                    if wwv_peak_idx < wwvh_peak_idx:
                        peaks = np.array([wwv_peak_idx, wwvh_peak_idx])
                        properties = {'peak_heights': np.array([wwv_peak_height, wwvh_peak_height])}
                    else:
                        peaks = np.array([wwvh_peak_idx, wwv_peak_idx])
                        properties = {'peak_heights': np.array([wwvh_peak_height, wwv_peak_height])}
                    
                    # Threshold check - both peaks should be above noise
                    mean_corr = np.mean(correlation)
                    std_corr = np.std(correlation)
                    threshold = mean_corr + 0.5 * std_corr
                    
                    if wwv_peak_height < threshold or wwvh_peak_height < threshold:
                        # Weak signal - fall back to single peak detection
                        if wwv_peak_height >= wwvh_peak_height and wwv_peak_height >= threshold:
                            peaks = np.array([wwv_peak_idx])
                            properties = {'peak_heights': np.array([wwv_peak_height])}
                        elif wwvh_peak_height >= threshold:
                            peaks = np.array([wwvh_peak_idx])
                            properties = {'peak_heights': np.array([wwvh_peak_height])}
                        else:
                            peaks = np.array([])
                            properties = {'peak_heights': np.array([])}
                else:
                    # Fallback: broad search ±150ms (no geographic predictor)
                    search_radius_samples = int(0.150 * effective_sample_rate)
                    search_start = max(0, zero_lag_idx - search_radius_samples)
                    search_end = min(len(correlation), zero_lag_idx + search_radius_samples)
                    
                    search_region = correlation[search_start:search_end]
                    
                    mean_corr = np.mean(search_region)
                    std_corr = np.std(search_region)
                    threshold = mean_corr + 0.5 * std_corr
                    
                    min_peak_distance = int(0.003 * effective_sample_rate)  # 3ms minimum
                    
                    peaks_local, properties = scipy_signal.find_peaks(
                        search_region,
                        height=threshold,
                        distance=min_peak_distance,
                        prominence=std_corr * 0.2
                    )
                    
                    peaks = peaks_local + search_start
                
                # Handle both dual-peak (both stations) and single-peak (one station) scenarios
                if len(peaks) >= 2:
                    # DUAL PEAK: Both WWV and WWVH detected
                    peak_heights = properties['peak_heights']
                    sorted_indices = np.argsort(peak_heights)[-2:]
                    sorted_indices = np.sort(sorted_indices)
                    
                    peak1_idx = sorted_indices[0]
                    peak2_idx = sorted_indices[1]
                    
                    # Peak times relative to zero-lag (positive = signal delayed from template)
                    peak1_time = (peaks[peak1_idx] - zero_lag_idx) / effective_sample_rate
                    peak2_time = (peaks[peak2_idx] - zero_lag_idx) / effective_sample_rate
                    
                    delay_ms = (peak2_time - peak1_time) * 1000
                    
                    # Log rejection reasons for debugging sparse detections
                    if delay_ms < 3 or delay_ms > 35:
                        logger.debug(f"{self.channel_name}: BCD window {window_start_time:.0f}s: delay {delay_ms:.1f}ms outside 3-35ms range")
                    
                    if 3 <= delay_ms <= 35:  # Relaxed from 5-30ms to 3-35ms
                        # Joint Least Squares Estimation to overcome temporal leakage
                        # At each peak, we measure: C(τ) = A_early*R(τ-τ_early) + A_late*R(τ-τ_late)
                        # This forms a 2x2 linear system we solve for A_early and A_late
                        
                        # Get correlation values at both peaks (peak1=early, peak2=late)
                        c_peak_early = float(peak_heights[peak1_idx])
                        c_peak_late = float(peak_heights[peak2_idx])
                        
                        # Peak times relative to zero-lag (for geographic classification)
                        peak_early_delay_ms = peak1_time * 1000
                        peak_late_delay_ms = peak2_time * 1000
                        
                        # Compute template autocorrelation at delay Δτ
                        delay_samples = int(delay_ms * effective_sample_rate / 1000)
                        
                        # R(0) = template autocorrelation at zero lag (template energy)
                        R_0 = float(np.sum(template_window**2))
                        
                        # R(Δτ) = template autocorrelation at the measured delay
                        # Shift template and compute overlap
                        if delay_samples < len(template_window):
                            R_delta = float(np.sum(template_window[:-delay_samples] * 
                                                  template_window[delay_samples:]))
                        else:
                            R_delta = 0.0
                        
                        # Set up the 2x2 system: [R(0) R(Δτ)] [A_early] = [C(τ_early)]
                        #                        [R(Δτ) R(0) ] [A_late ]   [C(τ_late) ]
                        # Note: R(-Δτ) = R(Δτ) due to autocorrelation symmetry
                        
                        if R_0 > 0:
                            # Solve the linear system
                            A_matrix = np.array([[R_0, R_delta],
                                               [R_delta, R_0]])
                            b_vector = np.array([c_peak_early, c_peak_late])
                            
                            try:
                                amplitudes = np.linalg.solve(A_matrix, b_vector)
                                early_amp = float(amplitudes[0])
                                late_amp = float(amplitudes[1])
                                
                                # Normalize by sqrt(template energy) for physical units
                                early_amp = early_amp / np.sqrt(R_0)
                                late_amp = late_amp / np.sqrt(R_0)
                                
                                # Amplitudes must be non-negative (use absolute value)
                                early_amp = abs(early_amp)
                                late_amp = abs(late_amp)
                            except np.linalg.LinAlgError:
                                # Matrix is singular, fall back to naive method
                                early_amp = abs(c_peak_early / np.sqrt(R_0))
                                late_amp = abs(c_peak_late / np.sqrt(R_0))
                        else:
                            early_amp = 0.0
                            late_amp = 0.0
                        
                        # GEOGRAPHIC PEAK ASSIGNMENT: Use ToA prediction to assign WWV/WWVH
                        # The geographic predictor uses receiver location to determine which
                        # station should arrive first based on propagation delay
                        if self.geo_predictor and frequency_mhz:
                            early_station, late_station = self.geo_predictor.classify_dual_peaks(
                                peak_early_delay_ms, peak_late_delay_ms,
                                early_amp, late_amp,
                                frequency_mhz
                            )
                            if early_station == 'WWV':
                                wwv_amp = early_amp
                                wwvh_amp = late_amp
                            else:
                                wwv_amp = late_amp
                                wwvh_amp = early_amp
                        else:
                            # Fallback: Assume WWV arrives first (common for US receivers)
                            # This is a heuristic that works for most continental US locations
                            early_station = 'WWV'
                            late_station = 'WWVH'
                            wwv_amp = early_amp
                            wwvh_amp = late_amp
                            logger.debug(f"{self.channel_name}: No geo predictor, assuming early=WWV")
                        
                        # Safety check for NaN/Inf values (breaks JSON)
                        if not np.isfinite(wwv_amp):
                            wwv_amp = 0.0
                        if not np.isfinite(wwvh_amp):
                            wwvh_amp = 0.0
                        
                        # Quality from correlation SNR
                        noise_floor = np.median(correlation)
                        quality = (c_peak_early + c_peak_late) / (2 * noise_floor) if noise_floor > 0 else 0.0
                        
                        if not np.isfinite(quality):
                            quality = 0.0
                        
                        # Measure delay spread (τD) from correlation peak widths (FWHM)
                        # This quantifies channel multipath time spreading
                        def measure_peak_width(correlation, peak_idx, sample_rate):
                            """Measure FWHM of correlation peak in milliseconds"""
                            peak_val = correlation[peak_idx]
                            half_max = peak_val / 2.0
                            
                            # Find left edge
                            left_idx = peak_idx
                            while left_idx > 0 and correlation[left_idx] > half_max:
                                left_idx -= 1
                            
                            # Find right edge
                            right_idx = peak_idx
                            while right_idx < len(correlation) - 1 and correlation[right_idx] > half_max:
                                right_idx += 1
                            
                            # Width in samples → milliseconds
                            width_samples = right_idx - left_idx
                            width_ms = (width_samples / sample_rate) * 1000.0
                            return width_ms
                        
                        wwv_delay_spread_ms = measure_peak_width(correlation, peaks[peak1_idx], effective_sample_rate)
                        wwvh_delay_spread_ms = measure_peak_width(correlation, peaks[peak2_idx], effective_sample_rate)
                        
                        # === DUAL-STATION TIME RECOVERY ===
                        # Both stations transmit at the same UTC second boundary.
                        # By subtracting expected propagation delay from measured ToA,
                        # we back-calculate the emission time. Both should agree.
                        time_recovery_data = {}
                        if self.geo_predictor and frequency_mhz:
                            expected = self.geo_predictor.calculate_expected_delays(frequency_mhz)
                            
                            # Determine which peak is WWV vs WWVH based on geographic assignment
                            if early_station == 'WWV':
                                wwv_toa_ms = peak_early_delay_ms
                                wwvh_toa_ms = peak_late_delay_ms
                            else:
                                wwv_toa_ms = peak_late_delay_ms
                                wwvh_toa_ms = peak_early_delay_ms
                            
                            wwv_expected_ms = expected['wwv_delay_ms']
                            wwvh_expected_ms = expected['wwvh_delay_ms']
                            
                            # Back-calculate emission time offset from minute boundary
                            # If everything is perfect, this should be ~0 for both
                            t_emission_wwv = wwv_toa_ms - wwv_expected_ms
                            t_emission_wwvh = wwvh_toa_ms - wwvh_expected_ms
                            
                            # Cross-validation: both should give the same result
                            cross_error = abs(t_emission_wwv - t_emission_wwvh)
                            
                            if cross_error < 1.0:
                                confidence = 'excellent'
                            elif cross_error < 2.0:
                                confidence = 'good'
                            elif cross_error < 5.0:
                                confidence = 'fair'
                            else:
                                confidence = 'investigate'
                            
                            time_recovery_data = {
                                'wwv_toa_ms': float(wwv_toa_ms),
                                'wwvh_toa_ms': float(wwvh_toa_ms),
                                'wwv_expected_delay_ms': float(wwv_expected_ms),
                                'wwvh_expected_delay_ms': float(wwvh_expected_ms),
                                't_emission_from_wwv_ms': float(t_emission_wwv),
                                't_emission_from_wwvh_ms': float(t_emission_wwvh),
                                'cross_validation_error_ms': float(cross_error),
                                'dual_station_confidence': confidence
                            }
                        
                        windows_data.append({
                            'window_start_sec': float(window_start_time),
                            'wwv_amplitude': wwv_amp,
                            'wwvh_amplitude': wwvh_amp,
                            'differential_delay_ms': float(delay_ms),
                            'correlation_quality': float(quality),
                            'detection_type': 'dual_peak',
                            # Channel characterization: delay spread from peak width
                            'wwv_delay_spread_ms': float(wwv_delay_spread_ms),
                            'wwvh_delay_spread_ms': float(wwvh_delay_spread_ms),
                            # Dual-station time recovery
                            **time_recovery_data
                        })
                        
                        # Update geographic predictor history if available
                        if self.geo_predictor and frequency_mhz:
                            # Convert peak times to absolute delays from correlation zero
                            peak1_delay_ms = peak1_time * 1000
                            peak2_delay_ms = peak2_time * 1000
                            self.geo_predictor.update_dual_peak_history(
                                frequency_mhz,
                                peak1_delay_ms, peak2_delay_ms,
                                wwv_amp, wwvh_amp
                            )
                
                elif len(peaks) == 1 and enable_single_station_detection:
                    # SINGLE PEAK: One station detected - use multi-evidence classification
                    peak_idx = 0
                    peak_time = (peaks[peak_idx] - zero_lag_idx) / effective_sample_rate
                    peak_delay_ms = peak_time * 1000
                    peak_height = float(properties['peak_heights'][peak_idx])
                    
                    # Normalize amplitude
                    R_0 = float(np.sum(template_window**2))
                    if R_0 > 0:
                        peak_amplitude = abs(peak_height / np.sqrt(R_0))
                    else:
                        continue
                    
                    noise_floor = np.median(correlation)
                    quality = peak_height / noise_floor if noise_floor > 0 else 0.0
                    
                    # === MULTI-EVIDENCE CLASSIFICATION ===
                    # Collect votes from multiple sources with exclusion logic
                    wwv_votes = 0.0
                    wwvh_votes = 0.0
                    exclusion_wwv = False  # If True, cannot be WWV
                    exclusion_wwvh = False  # If True, cannot be WWVH
                    evidence_sources = []
                    
                    # EVIDENCE 1: 500/600 Hz Ground Truth (DEFINITIVE - can exclude)
                    if ground_truth_station == 'WWV':
                        wwv_votes += 10.0
                        exclusion_wwvh = True
                        evidence_sources.append('gt_wwv')
                    elif ground_truth_station == 'WWVH':
                        wwvh_votes += 10.0
                        exclusion_wwv = True
                        evidence_sources.append('gt_wwvh')
                    
                    # EVIDENCE 2: Geographic ToA prediction
                    geo_station = None
                    if self.geo_predictor and frequency_mhz:
                        geo_station = self.geo_predictor.classify_single_peak(
                            peak_delay_ms, peak_amplitude, frequency_mhz, quality
                        )
                        if geo_station == 'WWV':
                            wwv_votes += 3.0
                            evidence_sources.append('geo_wwv')
                        elif geo_station == 'WWVH':
                            wwvh_votes += 3.0
                            evidence_sources.append('geo_wwvh')
                    
                    # EVIDENCE 3: Timing tone power ratio (1000/1200 Hz)
                    if timing_power_ratio_db is not None:
                        if abs(timing_power_ratio_db) > 3.0:
                            # Strong difference - high confidence
                            if timing_power_ratio_db > 0:
                                wwv_votes += 5.0
                                evidence_sources.append('pwr_wwv_strong')
                            else:
                                wwvh_votes += 5.0
                                evidence_sources.append('pwr_wwvh_strong')
                        elif abs(timing_power_ratio_db) > 1.0:
                            # Moderate difference
                            if timing_power_ratio_db > 0:
                                wwv_votes += 2.0
                                evidence_sources.append('pwr_wwv_mod')
                            else:
                                wwvh_votes += 2.0
                                evidence_sources.append('pwr_wwvh_mod')
                        else:
                            # Marginal - still counts but less
                            if timing_power_ratio_db > 0:
                                wwv_votes += 0.5
                            else:
                                wwvh_votes += 0.5
                    
                    # EVIDENCE 4: Tick SNR comparison
                    if wwv_tick_snr_db is not None and wwvh_tick_snr_db is not None:
                        snr_diff = wwv_tick_snr_db - wwvh_tick_snr_db
                        if snr_diff > 3.0:
                            wwv_votes += 2.0
                            evidence_sources.append('snr_wwv')
                        elif snr_diff < -3.0:
                            wwvh_votes += 2.0
                            evidence_sources.append('snr_wwvh')
                    
                    # === APPLY EXCLUSIONS ===
                    if exclusion_wwv:
                        wwv_votes = 0.0  # Cannot be WWV
                    if exclusion_wwvh:
                        wwvh_votes = 0.0  # Cannot be WWVH
                    
                    # === MAKE DECISION ===
                    total_votes = wwv_votes + wwvh_votes
                    
                    if total_votes > 0:
                        wwv_confidence = wwv_votes / total_votes
                        wwvh_confidence = wwvh_votes / total_votes
                        
                        if wwv_confidence > 0.6:
                            detection_type = 'single_peak_wwv_multi'
                            if ground_truth_station == 'WWV':
                                detection_type = 'single_peak_wwv_gt'
                            quality_adj = quality * min(1.0, wwv_confidence + 0.2)
                            windows_data.append({
                                'window_start_sec': float(window_start_time),
                                'wwv_amplitude': peak_amplitude,
                                'wwvh_amplitude': 0.0,
                                'differential_delay_ms': None,
                                'correlation_quality': float(quality_adj),
                                'detection_type': detection_type,
                                'peak_delay_ms': float(peak_delay_ms),
                                'evidence': evidence_sources
                            })
                        elif wwvh_confidence > 0.6:
                            detection_type = 'single_peak_wwvh_multi'
                            if ground_truth_station == 'WWVH':
                                detection_type = 'single_peak_wwvh_gt'
                            quality_adj = quality * min(1.0, wwvh_confidence + 0.2)
                            windows_data.append({
                                'window_start_sec': float(window_start_time),
                                'wwv_amplitude': 0.0,
                                'wwvh_amplitude': peak_amplitude,
                                'differential_delay_ms': None,
                                'correlation_quality': float(quality_adj),
                                'detection_type': detection_type,
                                'peak_delay_ms': float(peak_delay_ms),
                                'evidence': evidence_sources
                            })
                        else:
                            # Ambiguous - lean toward stronger evidence
                            if wwv_votes > wwvh_votes:
                                windows_data.append({
                                    'window_start_sec': float(window_start_time),
                                    'wwv_amplitude': peak_amplitude,
                                    'wwvh_amplitude': 0.0,
                                    'differential_delay_ms': None,
                                    'correlation_quality': float(quality * 0.6),
                                    'detection_type': 'single_peak_wwv_ambig',
                                    'peak_delay_ms': float(peak_delay_ms)
                                })
                            else:
                                windows_data.append({
                                    'window_start_sec': float(window_start_time),
                                    'wwv_amplitude': 0.0,
                                    'wwvh_amplitude': peak_amplitude,
                                    'differential_delay_ms': None,
                                    'correlation_quality': float(quality * 0.6),
                                    'detection_type': 'single_peak_wwvh_ambig',
                                    'peak_delay_ms': float(peak_delay_ms)
                                })
                    else:
                        # No evidence available - unclassified
                        windows_data.append({
                            'window_start_sec': float(window_start_time),
                            'wwv_amplitude': 0.0,
                            'wwvh_amplitude': 0.0,
                            'differential_delay_ms': None,
                            'correlation_quality': float(quality * 0.3),
                            'detection_type': 'single_peak_unclassified',
                            'peak_delay_ms': float(peak_delay_ms)
                        })
            
            # Step 5: Compute summary statistics from all valid windows
            if not windows_data:
                logger.info(f"{self.channel_name}: No valid BCD correlation windows detected (threshold={threshold:.1f}, mean={mean_corr:.1f}, std={std_corr:.1f})")
                return None, None, None, None, []
            
            wwv_amps = [w['wwv_amplitude'] for w in windows_data]
            wwvh_amps = [w['wwvh_amplitude'] for w in windows_data]
            delays = [w['differential_delay_ms'] for w in windows_data if w['differential_delay_ms'] is not None]
            qualities = [w['correlation_quality'] for w in windows_data]
            
            wwv_amp_mean = float(np.mean(wwv_amps))
            wwvh_amp_mean = float(np.mean(wwvh_amps))
            delay_mean = float(np.mean(delays)) if delays else None
            quality_mean = float(np.mean(qualities))
            
            # Adaptive windowing: Adjust window size based on signal conditions
            window_adjustment = None
            if adaptive:
                # Calculate amplitude ratio (dB)
                amp_ratio_db = 20 * np.log10(max(wwv_amp_mean, 1e-10) / max(wwvh_amp_mean, 1e-10))
                
                # Determine if one station is dominant or both are similar
                if abs(amp_ratio_db) > 10:
                    # One station is 10+ dB stronger (dominant or alone)
                    # → Tighten window for better temporal resolution
                    if window_seconds > 5:
                        window_adjustment = "tighten"
                        logger.info(f"{self.channel_name}: One station dominant ({amp_ratio_db:+.1f}dB) "
                                   f"- consider 5-second windows for better resolution")
                
                elif abs(amp_ratio_db) < 3:
                    # Stations within 3 dB (similar strength, hard to discriminate)
                    # → Expand window for better SNR discrimination
                    if window_seconds < 15:
                        window_adjustment = "expand"
                        logger.info(f"{self.channel_name}: Similar amplitudes ({amp_ratio_db:+.1f}dB) "
                                   f"- consider 15-second windows for better discrimination")
                
                # Check overall signal strength (quality)
                if quality_mean < 3.0 and window_seconds < 20:
                    # Weak signals (poor SNR)
                    # → Expand window regardless of amplitude ratio
                    window_adjustment = "expand_weak"
                    logger.info(f"{self.channel_name}: Weak signals (quality={quality_mean:.1f}) "
                               f"- consider 15-20 second windows for better SNR")
            
            # Format delay info (may be None if all single-peak detections)
            if delay_mean is not None and delays:
                delay_str = f"delay={delay_mean:.2f}±{np.std(delays):.2f}ms"
            else:
                delay_str = "delay=N/A (single-peak only)"
            
            logger.info(f"{self.channel_name}: BCD correlation ({len(windows_data)} windows, {window_seconds}s) - "
                       f"WWV amp={wwv_amp_mean:.4f}±{np.std(wwv_amps):.4f}, "
                       f"WWVH amp={wwvh_amp_mean:.4f}±{np.std(wwvh_amps):.4f}, "
                       f"ratio={20*np.log10(max(wwv_amp_mean,1e-10)/max(wwvh_amp_mean,1e-10)):+.1f}dB, "
                       f"{delay_str}, "
                       f"quality={quality_mean:.1f}")
            
            return wwv_amp_mean, wwvh_amp_mean, delay_mean, quality_mean, windows_data
            
        except Exception as e:
            logger.error(f"{self.channel_name}: BCD discrimination failed: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None, None, None, None, None
    
    def detect_bcd_discrimination(
        self,
        iq_samples: np.ndarray,
        sample_rate: int,
        minute_timestamp: float,
        frequency_mhz: Optional[float] = None,
        doppler_info: Optional[Dict[str, float]] = None,
        timing_power_ratio_db: Optional[float] = None,  # WWV-WWVH power for single-peak classification
        ground_truth_station: Optional[str] = None,  # From 500/600 Hz exclusive minutes
        wwv_tick_snr_db: Optional[float] = None,  # SNR of 1000 Hz tick
        wwvh_tick_snr_db: Optional[float] = None  # SNR of 1200 Hz tick
    ) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float], List[Dict[str, float]]]:
        """
        Wrapper method for BCD discrimination with adaptive window sizing.
        
        Calls bcd_correlation_discrimination with Doppler-adaptive window selection.
        Uses ionospheric Doppler shift to determine maximum coherent integration
        window, preventing phase rotation from degrading correlation quality.
        
        CRITICAL FIX (2025-11-26): Changed default from 60s non-overlapping to 10s
        sliding windows. The 60s window exceeded typical HF coherence time (Tc ~10-20s),
        causing Doppler-induced phase rotation to destroy correlation. Now defaults to
        10s windows with 1s steps, producing ~50 measurements/minute for time-series
        tracking of propagation dynamics.
        
        Args:
            iq_samples: Full minute of complex IQ samples
            sample_rate: Sample rate in Hz
            minute_timestamp: UTC timestamp of minute boundary
            frequency_mhz: Operating frequency for geographic ToA prediction
            doppler_info: Optional Doppler estimation from tick phase tracking
            
        Returns:
            Tuple of (wwv_amp_mean, wwvh_amp_mean, delay_mean, quality_mean, windows_list)
        """
        # 1. Determine Window Size (T_int)
        # Default to 10 seconds - within typical HF coherence time (Tc ~10-20s)
        # This prevents Doppler-induced phase rotation from destroying correlation
        window_seconds = 10.0  # Safe default within typical Tc
        
        if doppler_info and 'max_coherent_window_sec' in doppler_info:
            # Use Doppler-derived coherence limit, clamped to [10s, 20s]
            # Even with stable channel, >20s risks averaging over fading periods
            doppler_limit = doppler_info['max_coherent_window_sec']
            window_seconds = max(10.0, min(doppler_limit, 20.0))
            
            logger.info(f"{self.channel_name}: Doppler-limited BCD window to {window_seconds:.1f}s "
                       f"(Δf_D={doppler_info.get('wwv_doppler_hz', 0):+.3f} Hz, "
                       f"quality={doppler_info.get('doppler_quality', 0):.2f})")
        
        # 2. Determine Slide Step (T_slide)
        # Use 1-second sliding step for high-resolution time-series tracking
        # This produces ~50 windows/minute, capturing propagation dynamics
        step_seconds = 1.0
        
        logger.debug(f"{self.channel_name}: BCD correlation: T_int={window_seconds:.1f}s, T_slide={step_seconds:.1f}s")
        
        return self.bcd_correlation_discrimination(
            iq_samples=iq_samples,
            sample_rate=sample_rate,
            minute_timestamp=minute_timestamp,
            frequency_mhz=frequency_mhz,
            window_seconds=window_seconds,  # Now 10-20s (within Tc)
            step_seconds=step_seconds,      # 1s sliding for time-series
            adaptive=False,  # Doppler adaptation handles window sizing
            enable_single_station_detection=True,
            timing_power_ratio_db=timing_power_ratio_db,  # For single-peak cross-validation
            ground_truth_station=ground_truth_station,  # From 500/600 Hz exclusive minutes
            wwv_tick_snr_db=wwv_tick_snr_db,  # SNR evidence
            wwvh_tick_snr_db=wwvh_tick_snr_db
        )
    
    def _generate_bcd_template(
        self,
        minute_timestamp: float,
        sample_rate: int,
        envelope_only: bool = False
    ) -> Optional[np.ndarray]:
        """
        Generate expected 100 Hz BCD template for a given UTC minute
        
        Uses the WWVBCDEncoder to generate an accurate template based on
        Phil Karn's wwvsim.c implementation.
        
        Args:
            minute_timestamp: UTC timestamp of minute boundary
            sample_rate: Sample rate in Hz
            envelope_only: If True, return envelope without 100 Hz carrier
                          (for correlation with demodulated signals)
            
        Returns:
            60-second BCD template as numpy array, or None if generation fails
        """
        try:
            # Use the encoder instance that was created during __init__
            template = self.bcd_encoder.encode_minute(minute_timestamp, envelope_only=envelope_only)
            return template
            
        except Exception as e:
            logger.error(f"{self.channel_name}: Failed to generate BCD template: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None
    
    def bcd_correlation_with_doppler_compensation(
        self,
        iq_samples: np.ndarray,
        sample_rate: int,
        minute_timestamp: float,
        frequency_mhz: Optional[float] = None,
        window_seconds: float = 10.0,
        overlap_fraction: float = 0.5
    ) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float], List[Dict]]:
        """
        BCD correlation with Doppler de-rotation for improved coherent integration.
        
        This method addresses the phase coherence problem by:
        1. Estimating Doppler shift from per-tick phase progression
        2. De-rotating the signal to remove Doppler-induced phase drift
        3. Using overlapping windows for better time resolution
        
        The de-rotation allows longer integration windows without phase destruction,
        recovering up to 3 dB of coherent gain that would otherwise be lost.
        
        Args:
            iq_samples: Full minute of complex IQ samples
            sample_rate: Sample rate in Hz
            minute_timestamp: UTC timestamp of minute boundary
            frequency_mhz: Operating frequency for geographic prediction
            window_seconds: Integration window length (default 10s)
            overlap_fraction: Window overlap (default 0.5 = 50%)
            
        Returns:
            Tuple of (wwv_amp_mean, wwvh_amp_mean, delay_mean, quality_mean, windows_list)
        """
        try:
            # Step 1: Estimate Doppler from per-tick phase progression
            doppler_info = self.estimate_doppler_shift_from_ticks(iq_samples, sample_rate)
            
            wwv_doppler_hz = 0.0
            wwvh_doppler_hz = 0.0
            
            if doppler_info is not None:
                wwv_doppler_hz = doppler_info.get('wwv_doppler_hz', 0.0)
                wwvh_doppler_hz = doppler_info.get('wwvh_doppler_hz', 0.0)
                doppler_quality = doppler_info.get('doppler_quality', 0.0)
                
                logger.info(f"{self.channel_name}: Doppler estimate for de-rotation: "
                           f"WWV={wwv_doppler_hz:+.4f}Hz, WWVH={wwvh_doppler_hz:+.4f}Hz, "
                           f"quality={doppler_quality:.2f}")
            
            # Step 2: AM demodulation
            magnitude = np.abs(iq_samples)
            audio = magnitude - np.mean(magnitude)
            
            # Step 3: Bandpass filter around 100 Hz BCD
            nyquist = sample_rate / 2
            bcd_low = 50 / nyquist
            bcd_high = 150 / nyquist
            sos_bcd = scipy_signal.butter(4, [bcd_low, bcd_high], 'bandpass', output='sos')
            bcd_signal = scipy_signal.sosfilt(sos_bcd, audio)
            
            # Step 4: Apply Doppler de-rotation
            # Use average of WWV and WWVH Doppler (they should be similar for same ionospheric path)
            avg_doppler_hz = (wwv_doppler_hz + wwvh_doppler_hz) / 2.0
            
            if abs(avg_doppler_hz) > 0.001:
                # Create de-rotation phasor
                t = np.arange(len(bcd_signal)) / sample_rate
                derotation = np.exp(-2j * np.pi * avg_doppler_hz * t)
                
                # Convert to analytic signal for de-rotation
                from scipy.signal import hilbert
                analytic_bcd = hilbert(bcd_signal)
                
                # Apply de-rotation
                derotated = analytic_bcd * derotation
                bcd_signal_compensated = np.real(derotated)
                
                logger.debug(f"{self.channel_name}: Applied Doppler de-rotation: {avg_doppler_hz:+.4f} Hz")
            else:
                bcd_signal_compensated = bcd_signal
            
            # Step 5: Downsample for efficiency
            downsample_factor = 4
            bcd_signal_ds = scipy_signal.decimate(bcd_signal_compensated, downsample_factor, 
                                                   ftype='fir', zero_phase=True)
            effective_rate = sample_rate // downsample_factor
            
            # Step 6: Generate BCD template
            bcd_template = self._generate_bcd_template(minute_timestamp, effective_rate, envelope_only=False)
            if bcd_template is None:
                return None, None, None, None, []
            
            # Step 7: Sliding window correlation with overlap
            window_samples = int(window_seconds * effective_rate)
            step_samples = int(window_samples * (1.0 - overlap_fraction))
            
            windows_data = []
            total_samples = min(len(bcd_signal_ds), len(bcd_template))
            
            window_idx = 0
            start_sample = 0
            
            while start_sample + window_samples <= total_samples:
                end_sample = start_sample + window_samples
                window_start_sec = start_sample / effective_rate
                
                # Extract windows
                signal_window = bcd_signal_ds[start_sample:end_sample]
                template_window = bcd_template[start_sample:end_sample]
                
                # Cross-correlation
                correlation = scipy_signal.correlate(signal_window, template_window, mode='full', method='fft')
                correlation = np.abs(correlation)
                
                zero_lag_idx = len(template_window) - 1
                
                # Search for peaks in ±100ms around zero lag
                search_radius = int(0.100 * effective_rate)
                search_start = max(0, zero_lag_idx - search_radius)
                search_end = min(len(correlation), zero_lag_idx + search_radius)
                search_region = correlation[search_start:search_end]
                
                # Find peaks
                mean_corr = np.mean(search_region)
                std_corr = np.std(search_region)
                threshold = mean_corr + 0.5 * std_corr
                
                peaks, properties = scipy_signal.find_peaks(
                    search_region,
                    height=threshold,
                    distance=int(0.003 * effective_rate),
                    prominence=std_corr * 0.2
                )
                peaks = peaks + search_start
                
                if len(peaks) >= 2:
                    # Dual peak detection
                    peak_heights = properties['peak_heights']
                    sorted_idx = np.argsort(peak_heights)[-2:]
                    sorted_idx = np.sort(sorted_idx)
                    
                    peak1_idx, peak2_idx = sorted_idx[0], sorted_idx[1]
                    peak1_time = (peaks[peak1_idx] - zero_lag_idx) / effective_rate
                    peak2_time = (peaks[peak2_idx] - zero_lag_idx) / effective_rate
                    
                    delay_ms = (peak2_time - peak1_time) * 1000
                    
                    if 3 <= delay_ms <= 35:
                        # Extract amplitudes
                        R_0 = float(np.sum(template_window**2))
                        early_amp = abs(peak_heights[peak1_idx] / np.sqrt(R_0)) if R_0 > 0 else 0.0
                        late_amp = abs(peak_heights[peak2_idx] / np.sqrt(R_0)) if R_0 > 0 else 0.0
                        
                        # Assign WWV/WWVH based on geographic predictor
                        if self.geo_predictor and frequency_mhz:
                            early_delay_ms = peak1_time * 1000
                            late_delay_ms = peak2_time * 1000
                            early_station, _ = self.geo_predictor.classify_dual_peaks(
                                early_delay_ms, late_delay_ms, early_amp, late_amp, frequency_mhz
                            )
                            if early_station == 'WWV':
                                wwv_amp, wwvh_amp = early_amp, late_amp
                            else:
                                wwv_amp, wwvh_amp = late_amp, early_amp
                        else:
                            wwv_amp, wwvh_amp = early_amp, late_amp
                        
                        noise_floor = np.median(correlation)
                        quality = (peak_heights[peak1_idx] + peak_heights[peak2_idx]) / (2 * noise_floor) if noise_floor > 0 else 0.0
                        
                        windows_data.append({
                            'window_start_sec': float(window_start_sec),
                            'window_idx': window_idx,
                            'wwv_amplitude': float(wwv_amp),
                            'wwvh_amplitude': float(wwvh_amp),
                            'differential_delay_ms': float(delay_ms),
                            'correlation_quality': float(quality),
                            'detection_type': 'dual_peak_doppler_compensated',
                            'doppler_compensation_hz': float(avg_doppler_hz)
                        })
                
                elif len(peaks) == 1:
                    # Single peak
                    peak_time = (peaks[0] - zero_lag_idx) / effective_rate
                    peak_delay_ms = peak_time * 1000
                    R_0 = float(np.sum(template_window**2))
                    peak_amp = abs(properties['peak_heights'][0] / np.sqrt(R_0)) if R_0 > 0 else 0.0
                    
                    noise_floor = np.median(correlation)
                    quality = properties['peak_heights'][0] / noise_floor if noise_floor > 0 else 0.0
                    
                    # Classify single peak
                    if self.geo_predictor and frequency_mhz:
                        station = self.geo_predictor.classify_single_peak(
                            peak_delay_ms, peak_amp, frequency_mhz, quality
                        )
                    else:
                        station = 'WWV'  # Default assumption
                    
                    if station == 'WWV':
                        wwv_amp, wwvh_amp = peak_amp, 0.0
                    else:
                        wwv_amp, wwvh_amp = 0.0, peak_amp
                    
                    windows_data.append({
                        'window_start_sec': float(window_start_sec),
                        'window_idx': window_idx,
                        'wwv_amplitude': float(wwv_amp),
                        'wwvh_amplitude': float(wwvh_amp),
                        'differential_delay_ms': None,
                        'correlation_quality': float(quality),
                        'detection_type': f'single_peak_{station.lower()}_doppler_compensated',
                        'peak_delay_ms': float(peak_delay_ms),
                        'doppler_compensation_hz': float(avg_doppler_hz)
                    })
                
                start_sample += step_samples
                window_idx += 1
            
            # Compute summary statistics
            if not windows_data:
                logger.info(f"{self.channel_name}: No valid BCD windows with Doppler compensation")
                return None, None, None, None, []
            
            wwv_amps = [w['wwv_amplitude'] for w in windows_data]
            wwvh_amps = [w['wwvh_amplitude'] for w in windows_data]
            delays = [w['differential_delay_ms'] for w in windows_data if w['differential_delay_ms'] is not None]
            qualities = [w['correlation_quality'] for w in windows_data]
            
            wwv_mean = float(np.mean(wwv_amps))
            wwvh_mean = float(np.mean(wwvh_amps))
            delay_mean = float(np.mean(delays)) if delays else None
            quality_mean = float(np.mean(qualities))
            
            logger.info(f"{self.channel_name}: BCD Doppler-compensated ({len(windows_data)} windows): "
                       f"WWV={wwv_mean:.4f}, WWVH={wwvh_mean:.4f}, "
                       f"ratio={20*np.log10(max(wwv_mean,1e-10)/max(wwvh_mean,1e-10)):+.1f}dB, "
                       f"Doppler={avg_doppler_hz:+.4f}Hz")
            
            return wwv_mean, wwvh_mean, delay_mean, quality_mean, windows_data
            
        except Exception as e:
            logger.error(f"{self.channel_name}: Doppler-compensated BCD failed: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None, None, None, None, []
    
    def analyze_minute_with_440hz(
        self,
        iq_samples: np.ndarray,
        sample_rate: int,
        minute_timestamp: float,
        frequency_mhz: Optional[float] = None,
        detections: Optional[List[ToneDetectionResult]] = None
    ) -> Optional[DiscriminationResult]:
        """
        Complete discrimination analysis including all methods - FULLY INDEPENDENT
        
        This method now detects timing tones directly from IQ samples, making it
        fully reprocessable from archived data without external dependencies.
        
        Args:
            iq_samples: Full minute of IQ samples (typically 16 kHz, 60 seconds)
            sample_rate: Sample rate in Hz
            minute_timestamp: UTC timestamp of minute boundary
            frequency_mhz: Operating frequency for geographic ToA prediction (optional)
            detections: DEPRECATED - Optional external tone detections (for backward compatibility).
                       If None, will detect tones internally using detect_timing_tones().
            
        Returns:
            Enhanced DiscriminationResult with all discrimination methods
        """
        # PHASE 1: Detect timing tones (800ms WWV/WWVH tones)
        # If detections not provided, detect them from IQ samples (NEW: fully independent)
        if detections is None or len(detections) == 0:
            logger.debug(f"{self.channel_name}: No external detections provided, detecting tones from IQ data")
            wwv_power_db, wwvh_power_db, differential_delay_ms, detections = self.detect_timing_tones(
                iq_samples, sample_rate, minute_timestamp
            )
            # Create result directly from detected values
            result = self.compute_discrimination(detections, minute_timestamp)
        else:
            # Legacy path: use provided detections
            logger.debug(f"{self.channel_name}: Using {len(detections)} external tone detections")
            result = self.compute_discrimination(detections, minute_timestamp)
        
        if result is None:
            return None
        
        # CRITICAL: Override power values with accurate FFT measurement
        # Matched filter SNR measures detection confidence, not relative power.
        # For station comparison, we need actual tone power from FFT.
        fft_wwv_power_db, fft_wwvh_power_db = self.measure_tone_powers_fft(iq_samples, sample_rate)
        
        # Update result with accurate FFT-based power values
        result.wwv_power_db = fft_wwv_power_db
        result.wwvh_power_db = fft_wwvh_power_db
        result.power_ratio_db = fft_wwv_power_db - fft_wwvh_power_db
        
        # Re-determine dominant station based on accurate power ratio
        if result.wwv_detected or result.wwvh_detected:
            if abs(result.power_ratio_db) < 3.0:
                result.dominant_station = 'BALANCED'
            elif result.power_ratio_db > 0:
                result.dominant_station = 'WWV'
            else:
                result.dominant_station = 'WWVH'
        
        logger.debug(f"{self.channel_name}: FFT power - WWV={fft_wwv_power_db:.1f}dB, "
                    f"WWVH={fft_wwvh_power_db:.1f}dB, ratio={result.power_ratio_db:+.1f}dB "
                    f"-> {result.dominant_station}")
        
        # Get minute number (0-59)
        dt = datetime.utcfromtimestamp(minute_timestamp)
        minute_number = dt.minute
        
        # PHASE 2: Detect 440 Hz station ID tone (minutes 1 & 2 only)
        if minute_number == 1:
            # WWVH should have 440 Hz tone
            detected, power_db = self.detect_440hz_tone(iq_samples, sample_rate, 1)
            result.tone_440hz_wwvh_detected = detected
            result.tone_440hz_wwvh_power_db = power_db
            
            # If 440 Hz detected, increases confidence that WWVH is present
            if detected and result.confidence == 'low':
                result.confidence = 'medium'
        
        elif minute_number == 2:
            # WWV should have 440 Hz tone
            detected, power_db = self.detect_440hz_tone(iq_samples, sample_rate, 2)
            result.tone_440hz_wwv_detected = detected
            result.tone_440hz_wwv_power_db = power_db
            
            # If 440 Hz detected, increases confidence that WWV is present
            if detected and result.confidence == 'low':
                result.confidence = 'medium'
        
        # PHASE 2.5: Detect 500/600 Hz tones for ground truth (exclusive minutes only)
        # Minutes 43-51: Only WWVH broadcasts 500/600 Hz (9 minutes)
        # Minutes 16,17,19: Only WWV broadcasts 500/600 Hz (3 minutes)
        # This provides 12 ground truth minutes per hour!
        try:
            (detected, power_db, freq_hz, ground_truth_station, 
             harmonic_500_1000, harmonic_600_1200) = self.detect_500_600hz_tone(
                iq_samples, sample_rate, minute_number
            )
            result.tone_500_600_detected = detected
            result.tone_500_600_power_db = power_db
            result.tone_500_600_freq_hz = freq_hz
            result.tone_500_600_ground_truth_station = ground_truth_station
            result.harmonic_ratio_500_1000 = harmonic_500_1000
            result.harmonic_ratio_600_1200 = harmonic_600_1200
            
            # If detected in exclusive minute, this is strong ground truth
            if detected and ground_truth_station:
                # Boost confidence if tone detection agrees with power-based station
                if result.dominant_station == ground_truth_station:
                    if result.confidence != 'high':
                        result.confidence = 'high'
                        logger.info(f"{self.channel_name}: {freq_hz} Hz ground truth confirms {ground_truth_station}")
        except Exception as e:
            logger.debug(f"{self.channel_name}: 500/600 Hz detection failed: {e}")
        
        # PHASE 3: Detect 5ms tick marks with coherent integration (60-second baseline)
        # DISCRIMINATION-FIRST: Use full minute for maximum tick stacking sensitivity
        try:
            tick_windows = self.detect_tick_windows(iq_samples, sample_rate, window_seconds=60)
            result.tick_windows_10sec = tick_windows  # Field name unchanged for compatibility
            
            # Log summary with coherent integration statistics
            good_windows = [w for w in tick_windows if w['wwv_snr_db'] > 0 or w['wwvh_snr_db'] > 0]
            if good_windows:
                coherent_count = sum(1 for w in good_windows if w.get('integration_method') == 'coherent')
                avg_ratio = np.mean([w['ratio_db'] for w in good_windows])
                avg_coherence_wwv = np.mean([w.get('coherence_quality_wwv', 0) for w in good_windows])
                avg_coherence_wwvh = np.mean([w.get('coherence_quality_wwvh', 0) for w in good_windows])
                
                logger.info(f"{self.channel_name}: Tick analysis - {len(good_windows)}/{len(tick_windows)} windows valid, "
                           f"{coherent_count}/{len(good_windows)} coherent, avg ratio: {avg_ratio:+.1f}dB, "
                           f"coherence: WWV={avg_coherence_wwv:.2f} WWVH={avg_coherence_wwvh:.2f}")
        except Exception as e:
            logger.warning(f"{self.channel_name}: Tick detection failed: {e}")
            result.tick_windows_10sec = []
        
        # PHASE 3.5: Estimate Doppler shift from per-tick phase progression
        # Uses adjacent pulse phase difference method for accurate Δf_D measurement
        # This determines maximum coherent integration window for BCD analysis
        doppler_info = None
        try:
            # New method: extract per-tick phases directly from IQ samples
            # Provides ~57 instantaneous Doppler measurements per minute
            doppler_info = self.estimate_doppler_shift_from_ticks(iq_samples, sample_rate)
            
            # Store Doppler info in result for CSV logging and web UI display
            if doppler_info:
                result.doppler_wwv_hz = doppler_info.get('wwv_doppler_hz')
                result.doppler_wwvh_hz = doppler_info.get('wwvh_doppler_hz')
                result.doppler_wwv_std_hz = doppler_info.get('wwv_doppler_std_hz')
                result.doppler_wwvh_std_hz = doppler_info.get('wwvh_doppler_std_hz')
                result.doppler_max_coherent_window_sec = doppler_info.get('max_coherent_window_sec')
                result.doppler_quality = doppler_info.get('doppler_quality')
                result.doppler_phase_variance_rad = doppler_info.get('phase_variance_rad')
                # Count valid ticks from the tick data extraction
                tick_data = self.extract_per_tick_phases(iq_samples, sample_rate)
                result.doppler_valid_tick_count = tick_data.get('valid_tick_count', 0)
        except Exception as e:
            logger.debug(f"{self.channel_name}: Doppler estimation failed: {e}")
        
        # PHASE 4: BCD discrimination using 100 Hz subcarrier analysis
        # Adaptive window sizing based on Doppler limits
        # Amplitudes measured directly from 100 Hz BCD signal correlation peaks
        # Delay spread measurement quantifies channel multipath (complements Doppler spread)
        # 
        # MULTI-EVIDENCE CLASSIFICATION (2025-11-27):
        # Pass all available evidence for single-peak classification:
        # - timing_power_ratio_db: 1000/1200 Hz power difference
        # - ground_truth_station: From 500/600 Hz exclusive minutes (DEFINITIVE)
        # - wwv_tick_snr_db/wwvh_tick_snr_db: Tick SNR comparison
        
        # Extract tick SNR values for evidence
        wwv_tick_snr = None
        wwvh_tick_snr = None
        if result.tick_windows_10sec:
            # Get mean SNR across ticks
            wwv_snrs = [t.get('wwv_snr_db', -100) for t in result.tick_windows_10sec if t.get('wwv_snr_db', -100) > 0]
            wwvh_snrs = [t.get('wwvh_snr_db', -100) for t in result.tick_windows_10sec if t.get('wwvh_snr_db', -100) > 0]
            if wwv_snrs:
                wwv_tick_snr = float(np.mean(wwv_snrs))
            if wwvh_snrs:
                wwvh_tick_snr = float(np.mean(wwvh_snrs))
        
        try:
            bcd_wwv, bcd_wwvh, bcd_delay, bcd_quality, bcd_windows = self.detect_bcd_discrimination(
                iq_samples, sample_rate, minute_timestamp, frequency_mhz, doppler_info,
                timing_power_ratio_db=result.power_ratio_db,  # 1000/1200 Hz power difference
                ground_truth_station=result.tone_500_600_ground_truth_station if result.tone_500_600_detected else None,
                wwv_tick_snr_db=wwv_tick_snr,
                wwvh_tick_snr_db=wwvh_tick_snr
            )
            
            # Log 440 Hz reference measurements when available (hourly calibration anchor)
            # 440 Hz provides harmonic-free reference (WWV minute 2, WWVH minute 1)
            if minute_number == 1 and result.tone_440hz_wwvh_detected and result.tone_440hz_wwvh_power_db:
                logger.debug(f"{self.channel_name}: 440 Hz WWVH reference: {result.tone_440hz_wwvh_power_db:.1f} dB")
            elif minute_number == 2 and result.tone_440hz_wwv_detected and result.tone_440hz_wwv_power_db:
                logger.debug(f"{self.channel_name}: 440 Hz WWV reference: {result.tone_440hz_wwv_power_db:.1f} dB")
            
            result.bcd_wwv_amplitude = bcd_wwv
            result.bcd_wwvh_amplitude = bcd_wwvh
            result.bcd_differential_delay_ms = bcd_delay
            result.bcd_correlation_quality = bcd_quality
            result.bcd_windows = bcd_windows  # Time-series data (~50 windows/minute with 1s steps)
            
            # === DUAL-STATION TIME RECOVERY - Aggregate from windows ===
            # Extract time recovery data from dual-peak windows
            if bcd_windows:
                dual_peak_windows = [w for w in bcd_windows 
                                    if w.get('detection_type') == 'dual_peak' 
                                    and w.get('cross_validation_error_ms') is not None]
                
                if dual_peak_windows:
                    # Use median for robustness against outliers
                    result.wwv_toa_ms = float(np.median([w['wwv_toa_ms'] for w in dual_peak_windows]))
                    result.wwvh_toa_ms = float(np.median([w['wwvh_toa_ms'] for w in dual_peak_windows]))
                    result.wwv_expected_delay_ms = float(np.median([w['wwv_expected_delay_ms'] for w in dual_peak_windows]))
                    result.wwvh_expected_delay_ms = float(np.median([w['wwvh_expected_delay_ms'] for w in dual_peak_windows]))
                    result.t_emission_from_wwv_ms = float(np.median([w['t_emission_from_wwv_ms'] for w in dual_peak_windows]))
                    result.t_emission_from_wwvh_ms = float(np.median([w['t_emission_from_wwvh_ms'] for w in dual_peak_windows]))
                    result.cross_validation_error_ms = float(np.median([w['cross_validation_error_ms'] for w in dual_peak_windows]))
                    
                    # Classify confidence based on median cross-validation error
                    if result.cross_validation_error_ms < 1.0:
                        result.dual_station_confidence = 'excellent'
                    elif result.cross_validation_error_ms < 2.0:
                        result.dual_station_confidence = 'good'
                    elif result.cross_validation_error_ms < 5.0:
                        result.dual_station_confidence = 'fair'
                    else:
                        result.dual_station_confidence = 'investigate'
                    
                    logger.info(f"{self.channel_name}: DUAL-STATION TIME RECOVERY: "
                               f"WWV ToA={result.wwv_toa_ms:.2f}ms (exp={result.wwv_expected_delay_ms:.2f}), "
                               f"WWVH ToA={result.wwvh_toa_ms:.2f}ms (exp={result.wwvh_expected_delay_ms:.2f}), "
                               f"T_emission: WWV={result.t_emission_from_wwv_ms:.2f}ms, WWVH={result.t_emission_from_wwvh_ms:.2f}ms, "
                               f"cross-validation={result.cross_validation_error_ms:.2f}ms ({result.dual_station_confidence})")
                    
        except Exception as e:
            logger.warning(f"{self.channel_name}: BCD discrimination failed: {e}")
            result.bcd_wwv_amplitude = None
            result.bcd_wwvh_amplitude = None
            result.bcd_differential_delay_ms = None
            result.bcd_correlation_quality = None
            result.bcd_windows = None
        
        # PHASE 4.5: Test Signal Discrimination (minutes 8 and 44 only)
        # Scientific modulation test provides strongest discrimination when present
        # Minute 8 = WWV, Minute 44 = WWVH
        if minute_number in [8, 44]:
            try:
                test_detection = self.test_signal_detector.detect(
                    iq_samples, minute_number, sample_rate
                )
                
                result.test_signal_detected = test_detection.detected
                result.test_signal_station = test_detection.station
                result.test_signal_confidence = test_detection.confidence
                result.test_signal_multitone_score = test_detection.multitone_score
                result.test_signal_chirp_score = test_detection.chirp_score
                result.test_signal_noise_correlation = test_detection.noise_correlation
                result.test_signal_snr_db = test_detection.snr_db
                result.test_signal_toa_offset_ms = test_detection.toa_offset_ms
                result.test_signal_burst_toa_offset_ms = test_detection.burst_toa_offset_ms
                result.test_signal_delay_spread_ms = test_detection.delay_spread_ms
                result.test_signal_coherence_time_sec = test_detection.coherence_time_sec
                result.test_signal_frequency_selectivity_db = test_detection.frequency_selectivity_db
                result.test_signal_noise1_score = test_detection.noise1_score
                result.test_signal_noise2_score = test_detection.noise2_score
                result.test_signal_noise_coherence_diff = test_detection.noise_coherence_diff
                
                if test_detection.detected:
                    toa_str = f", ToA={test_detection.toa_offset_ms:+.2f}ms" if test_detection.toa_offset_ms is not None else ""
                    snr_str = f", SNR={test_detection.snr_db:.1f}dB" if test_detection.snr_db else ""
                    fss_str = f", FSS={test_detection.frequency_selectivity_db:.1f}dB" if test_detection.frequency_selectivity_db else ""
                    delay_str = f", delay_spread={test_detection.delay_spread_ms:.1f}ms" if test_detection.delay_spread_ms else ""
                    coh_str = f", coherence={test_detection.coherence_time_sec:.1f}s" if test_detection.coherence_time_sec else ""
                    logger.info(f"{self.channel_name}: ✨ Test signal detected! "
                               f"Station={test_detection.station} (schedule-based), "
                               f"confidence={test_detection.confidence:.3f}{snr_str}{toa_str}{fss_str}{delay_str}{coh_str}")
                    
                    # High-confidence test signal overrides other methods
                    if test_detection.confidence > 0.7:
                        result.dominant_station = test_detection.station
                        result.confidence = 'high'
                        logger.info(f"{self.channel_name}: Test signal confidence high, "
                                   f"overriding other discriminators → {test_detection.station}")
                        
            except Exception as e:
                logger.warning(f"{self.channel_name}: Test signal detection failed: {e}")
        
        # PHASE 5: Finalize discrimination with weighted voting combiner
        result = self.finalize_discrimination(
            result=result,
            minute_number=minute_number,
            bcd_wwv_amp=result.bcd_wwv_amplitude,
            bcd_wwvh_amp=result.bcd_wwvh_amplitude,
            tone_440_wwv_detected=result.tone_440hz_wwv_detected,
            tone_440_wwvh_detected=result.tone_440hz_wwvh_detected,
            tick_results=result.tick_windows_10sec
        )
        
        # PHASE 6: Inter-method cross-validation
        # Aggregate independent measurements to assess agreement and adjust confidence
        result = self._cross_validate_methods(result, minute_number)
        
        return result
    
    def _cross_validate_methods(
        self,
        result: 'DiscriminationResult',
        minute_number: int
    ) -> 'DiscriminationResult':
        """
        Cross-validate independent discrimination methods for consistency.
        
        Checks for agreement between:
        1. Power-based: FFT power ratio (1000/1200 Hz timing tones)
        2. Timing-based: BCD differential delay vs geographic prediction
        3. Per-tick voting: Majority of 59 ticks
        4. Ground truth: 440 Hz (min 1,2) or test signal (min 8,44)
        
        Agreement boosts confidence, disagreement triggers investigation flags.
        """
        agreements = []
        disagreements = []
        
        # 1. Power vs Timing cross-check
        # If WWVH is louder (power_ratio < 0) and WWV arrives first (bcd_delay > 0),
        # they AGREE: the louder station is the more distant one (WWVH from Hawaii)
        if result.power_ratio_db is not None and result.bcd_differential_delay_ms is not None:
            power_says_wwvh = result.power_ratio_db < -3
            power_says_wwv = result.power_ratio_db > 3
            
            # Positive BCD delay means WWVH peak is later than WWV peak
            wwv_arrives_first = result.bcd_differential_delay_ms > 5
            wwvh_arrives_first = result.bcd_differential_delay_ms < -5
            
            if (power_says_wwvh and wwv_arrives_first) or (power_says_wwv and wwvh_arrives_first):
                agreements.append('power_timing_agree')
                logger.debug(f"{self.channel_name}: ✓ Power + Timing agree on station identity")
            elif power_says_wwvh or power_says_wwv:
                if wwv_arrives_first or wwvh_arrives_first:
                    disagreements.append('power_timing_disagree')
                    logger.warning(f"{self.channel_name}: ✗ Power ({result.dominant_station}) vs Timing disagree")
        
        # 2. Per-tick majority voting
        if result.tick_windows_10sec:
            tw = result.tick_windows_10sec[0]
            # Use absolute power ratio if available
            tick_power_ratio = tw.get('power_ratio_db', tw.get('ratio_db', 0))
            
            if abs(tick_power_ratio) > 3:
                tick_says = 'WWV' if tick_power_ratio > 3 else 'WWVH'
                if result.dominant_station == tick_says:
                    agreements.append('tick_power_agree')
                elif result.dominant_station not in ['BALANCED', 'UNKNOWN']:
                    disagreements.append('tick_power_disagree')
                    logger.debug(f"{self.channel_name}: Tick power ({tick_says}) differs from FFT ({result.dominant_station})")
        
        # 3. Geographic delay validation
        # Check if measured BCD delay matches expected for receiver location
        if result.bcd_differential_delay_ms is not None and self.geo_predictor:
            try:
                freq_mhz = self.channel_frequency_mhz or 10.0
                pred = self.geo_predictor.calculate_expected_delays(freq_mhz)
                expected_diff = pred['wwv_delay_ms'] - pred['wwvh_delay_ms']
                measured_diff = result.bcd_differential_delay_ms
                
                # Expected diff is negative (WWV closer), measured should be positive
                # (WWV arrives first = WWVH peak is later)
                deviation = abs(abs(measured_diff) - abs(expected_diff))
                
                if deviation < 10:  # Within 10ms of expected
                    agreements.append('geographic_timing_agree')
                    logger.debug(f"{self.channel_name}: ✓ BCD delay ({measured_diff:.1f}ms) matches "
                               f"geographic prediction ({expected_diff:.1f}ms) within {deviation:.1f}ms")
                elif deviation > 20:  # More than 20ms off
                    disagreements.append('geographic_timing_unusual')
                    logger.info(f"{self.channel_name}: BCD delay ({measured_diff:.1f}ms) deviates "
                              f"{deviation:.1f}ms from expected ({expected_diff:.1f}ms) - unusual propagation?")
            except Exception as e:
                logger.debug(f"{self.channel_name}: Could not validate geographic delay: {e}")
        
        # 4. Ground truth validation (when available)
        if minute_number == 1 and result.tone_440hz_wwvh_detected:
            # Minute 1: 440 Hz = WWVH
            if result.dominant_station == 'WWVH':
                agreements.append('440hz_ground_truth_agree')
                logger.info(f"{self.channel_name}: ✓ 440 Hz ground truth confirms WWVH")
            else:
                disagreements.append('440hz_ground_truth_disagree')
                logger.warning(f"{self.channel_name}: ✗ 440 Hz says WWVH but power says {result.dominant_station}")
        
        if minute_number == 2 and result.tone_440hz_wwv_detected:
            # Minute 2: 440 Hz = WWV
            if result.dominant_station == 'WWV':
                agreements.append('440hz_ground_truth_agree')
                logger.info(f"{self.channel_name}: ✓ 440 Hz ground truth confirms WWV")
            else:
                disagreements.append('440hz_ground_truth_disagree')
                logger.warning(f"{self.channel_name}: ✗ 440 Hz says WWV but power says {result.dominant_station}")
        
        # 5. BCD correlation quality validation
        # High BCD correlation quality confirms received signal matches expected minute template
        # This validates that we're synchronized with the correct UTC minute
        if result.bcd_correlation_quality is not None:
            result.bcd_correlation_peak_quality = result.bcd_correlation_quality
            if result.bcd_correlation_quality > 5.0:  # Threshold for "good" correlation
                result.bcd_minute_validated = True
                agreements.append('bcd_minute_validated')
                logger.debug(f"{self.channel_name}: ✓ BCD correlation quality {result.bcd_correlation_quality:.1f} "
                           f"confirms minute timing")
            elif result.bcd_correlation_quality < 2.0:
                # Very low correlation suggests timing issue or no signal
                disagreements.append('bcd_minute_quality_low')
                logger.debug(f"{self.channel_name}: BCD correlation quality {result.bcd_correlation_quality:.1f} "
                           f"is low - possible timing issue")
        
        # 6. 500/600 Hz exclusive tone ground truth (12 minutes per hour!)
        if result.tone_500_600_detected and result.tone_500_600_ground_truth_station:
            gt_station = result.tone_500_600_ground_truth_station
            if result.dominant_station == gt_station:
                agreements.append('500_600hz_ground_truth_agree')
                logger.info(f"{self.channel_name}: ✓ {result.tone_500_600_freq_hz} Hz ground truth confirms {gt_station}")
            else:
                disagreements.append('500_600hz_ground_truth_disagree')
                logger.warning(f"{self.channel_name}: ✗ {result.tone_500_600_freq_hz} Hz says {gt_station} "
                             f"but power says {result.dominant_station}")
        
        # 7. Differential Doppler Shift Agreement (Phase 6A)
        # WWV (Colorado) and WWVH (Hawaii) have different propagation paths,
        # leading to different Doppler shifts. The Δf_D acts as a geographic
        # signature that can validate power-based discrimination.
        if result.doppler_wwv_hz is not None and result.doppler_wwvh_hz is not None:
            # Only consider if Doppler quality is acceptable
            doppler_quality = result.doppler_quality or 0.0
            if doppler_quality > 0.3:
                # Calculate differential Doppler magnitude
                doppler_diff_hz = abs(result.doppler_wwv_hz) - abs(result.doppler_wwvh_hz)
                power_ratio = result.power_ratio_db or 0.0
                
                # Agreement: When power strongly favors one station AND its Doppler
                # magnitude is dominant (suggesting shorter/more active path)
                # This is heuristic but leverages path independence
                if (power_ratio > 3.0 and doppler_diff_hz > 0.005) or \
                   (power_ratio < -3.0 and doppler_diff_hz < -0.005):
                    agreements.append('doppler_power_agree')
                    logger.debug(f"{self.channel_name}: ✓ Doppler Δf_D ({doppler_diff_hz:.4f} Hz) "
                               f"agrees with power ratio ({power_ratio:.1f} dB)")
                elif abs(doppler_diff_hz) > 0.02:
                    # Very high differential Doppler suggests complex, non-reciprocal channel
                    # This is informational, not necessarily a disagreement
                    logger.info(f"{self.channel_name}: High differential Doppler: {doppler_diff_hz:.4f} Hz - "
                              f"complex multipath channel")
        
        # 8. Coherence Quality Confidence Adjustment (Phase 6B)
        # Low coherence quality means unstable channel (fading, multipath) where
        # phase-derived measurements (BCD timing, Doppler) are unreliable.
        if result.tick_windows_10sec:
            # Use the full minute window (index 0) for coherence assessment
            full_min_tick_data = result.tick_windows_10sec[0]
            
            coherence_wwv = full_min_tick_data.get('coherence_quality_wwv', 1.0)
            coherence_wwvh = full_min_tick_data.get('coherence_quality_wwvh', 1.0)
            min_coherence = min(coherence_wwv, coherence_wwvh)
            
            if min_coherence < 0.3:
                # Significant phase instability - timing measurements unreliable
                # This is a strong indicator to reduce confidence
                disagreements.append('low_coherence_downgrade')
                logger.warning(f"{self.channel_name}: ⚠ Coherence very low ({min_coherence:.2f}) - "
                             f"phase-derived measurements unreliable")
            elif min_coherence > 0.85:
                # Excellent coherence confirms stable channel - all methods trustworthy
                agreements.append('high_coherence_boost')
                logger.debug(f"{self.channel_name}: ✓ High coherence ({min_coherence:.2f}) - "
                           f"stable channel, measurements reliable")
        
        # 9. Harmonic Content Cross-Validation (Phase 6C)
        # When one station broadcasts 500/600 Hz exclusively, receiver nonlinearity
        # creates harmonics at 1000/1200 Hz that add to the timing tone power:
        #   - 500 Hz × 2 = 1000 Hz (adds to WWV timing marker)
        #   - 600 Hz × 2 = 1200 Hz (adds to WWVH timing marker)
        # The harmonic ratio P_1000/P_500 or P_1200/P_600 indicates contamination level.
        if result.tone_500_600_detected and result.tone_500_600_ground_truth_station:
            gt_station = result.tone_500_600_ground_truth_station
            gt_freq = result.tone_500_600_freq_hz
            power_ratio = result.power_ratio_db or 0.0
            
            # Get harmonic ratios (already computed in detect_500_600hz_tone)
            h_500_1000 = result.harmonic_ratio_500_1000  # P_1000/P_500 in dB
            h_600_1200 = result.harmonic_ratio_600_1200  # P_1200/P_600 in dB
            
            # Analyze exclusive WWV minutes (1, 16, 17, 19)
            # WWV broadcasts 600 Hz → harmonic at 1200 Hz adds to WWVH timing power
            # If WWV is truly dominant, this harmonic is significant
            if gt_station == 'WWV' and gt_freq == 600 and h_600_1200 is not None:
                # Strong harmonic ratio (>-15 dB) confirms significant 600 Hz presence
                # This supports WWV as the received station
                if h_600_1200 > -15 and power_ratio > 0:
                    agreements.append('harmonic_signature_wwv')
                    logger.debug(f"{self.channel_name}: ✓ 600→1200 Hz harmonic ratio ({h_600_1200:.1f} dB) "
                               f"confirms WWV 600 Hz presence")
            
            # Analyze exclusive WWVH minutes (2, 43-51)
            # WWVH broadcasts 600 Hz → harmonic at 1200 Hz adds to WWVH timing power
            # If WWVH is truly dominant, this harmonic is significant
            elif gt_station == 'WWVH' and gt_freq == 600 and h_600_1200 is not None:
                if h_600_1200 > -15 and power_ratio < 0:
                    agreements.append('harmonic_signature_wwvh')
                    logger.debug(f"{self.channel_name}: ✓ 600→1200 Hz harmonic ratio ({h_600_1200:.1f} dB) "
                               f"confirms WWVH 600 Hz presence")
            
            # 500 Hz exclusive minutes: harmonic at 1000 Hz
            elif gt_freq == 500 and h_500_1000 is not None:
                if h_500_1000 > -15:
                    if gt_station == 'WWV' and power_ratio > 0:
                        agreements.append('harmonic_signature_wwv')
                        logger.debug(f"{self.channel_name}: ✓ 500→1000 Hz harmonic ratio ({h_500_1000:.1f} dB) "
                                   f"confirms WWV 500 Hz presence")
                    elif gt_station == 'WWVH' and power_ratio < 0:
                        agreements.append('harmonic_signature_wwvh')
                        logger.debug(f"{self.channel_name}: ✓ 500→1000 Hz harmonic ratio ({h_500_1000:.1f} dB) "
                                   f"confirms WWVH 500 Hz presence")
        
        # Store cross-validation results
        result.inter_method_agreements = agreements
        result.inter_method_disagreements = disagreements
        
        # Adjust confidence based on agreement
        agreement_count = len(agreements)
        disagreement_count = len(disagreements)
        
        if agreement_count >= 2 and disagreement_count == 0:
            if result.confidence != 'high':
                result.confidence = 'high'
                logger.info(f"{self.channel_name}: Confidence → HIGH ({agreement_count} methods agree)")
        elif disagreement_count >= 2:
            if result.confidence == 'high':
                result.confidence = 'medium'
                logger.warning(f"{self.channel_name}: Confidence → MEDIUM ({disagreement_count} disagreements)")
        elif disagreement_count > agreement_count:
            if result.confidence != 'low':
                result.confidence = 'low'
                logger.warning(f"{self.channel_name}: Confidence → LOW (more disagreements than agreements)")
        
        return result
    
    def get_recent_measurements(self, count: int = 10) -> List[DiscriminationResult]:
        """Get most recent discrimination measurements"""
        return self.measurements[-count:]
    
    def get_statistics(self) -> Dict:
        """Get statistics over recent measurements"""
        if not self.measurements:
            return {
                'count': 0,
                'mean_power_ratio_db': 0.0,
                'mean_differential_delay_ms': 0.0,
                'wwv_dominant_count': 0,
                'wwvh_dominant_count': 0,
                'balanced_count': 0
            }
        
        power_ratios = [m.power_ratio_db for m in self.measurements if m.power_ratio_db is not None]
        delays = [m.differential_delay_ms for m in self.measurements if m.differential_delay_ms is not None]
        
        wwv_count = sum(1 for m in self.measurements if m.dominant_station == 'WWV')
        wwvh_count = sum(1 for m in self.measurements if m.dominant_station == 'WWVH')
        balanced_count = sum(1 for m in self.measurements if m.dominant_station == 'BALANCED')
        
        return {
            'count': len(self.measurements),
            'mean_power_ratio_db': float(np.mean(power_ratios)) if power_ratios else 0.0,
            'std_power_ratio_db': float(np.std(power_ratios)) if power_ratios else 0.0,
            'mean_differential_delay_ms': float(np.mean(delays)) if delays else 0.0,
            'std_differential_delay_ms': float(np.std(delays)) if delays else 0.0,
            'wwv_dominant_count': wwv_count,
            'wwvh_dominant_count': wwvh_count,
            'balanced_count': balanced_count
        }


