#!/usr/bin/env python3
"""
BPM Station Discriminator - UT1/UTC Handling and Signal Processing

================================================================================
PURPOSE
================================================================================
Handle the unique characteristics of BPM (China) time signal broadcasts that
differ from WWV/WWVH/CHU:

1. UT1/UTC ALTERNATION:
   - Minutes 0-24, 30-54: UTC timing (10ms ticks)
   - Minutes 25-29, 55-59: UT1 timing (100ms ticks)
   
   The UT1 minutes transmit UT1 time (Earth rotation time), NOT UTC.
   Using UT1 minutes for UTC timing would introduce DUT1 error (~0.1-0.9s).

2. TICK DURATION DIFFERENCES:
   - UTC ticks: 10ms (vs 5ms for WWV/WWVH)
   - UT1 ticks: 100ms (easily distinguishable)
   - Minute marker: 300ms

3. SHARED FREQUENCIES (require discrimination):
   - 2.5, 5, 10, 15 MHz: BPM shares with WWV + WWVH
   - Discrimination uses: geographic delay, tick duration, tone analysis

4. LONG PROPAGATION PATH:
   - From China to continental US: ~10,000-12,000 km
   - Expected delays: 35-50 ms (much longer than WWV/WWVH)
   - Multi-hop F-layer paths are common

================================================================================
BPM SIGNAL STRUCTURE
================================================================================
Reference: NTSC (National Time Service Center, China) specifications

TIMING TONES:
    Frequency: 1000 Hz (same as WWV)
    UTC tick duration: 10 ms
    UT1 tick duration: 100 ms
    Minute marker: 300 ms (first tick of each minute)

SCHEDULE:
    Seconds 0: Minute marker (300ms)
    Seconds 1-59: Regular ticks (10ms UTC or 100ms UT1)
    
    Minutes 0-24: UTC timing
    Minutes 25-29: UT1 timing (DO NOT USE FOR UTC)
    Minutes 30-54: UTC timing
    Minutes 55-59: UT1 timing (DO NOT USE FOR UTC)

BCD TIME CODE:
    100 Hz subcarrier (similar to WWV)
    Encodes: Year, Day, Hour, Minute, DUT1

================================================================================
DISCRIMINATION APPROACH
================================================================================
BPM discrimination is simpler than WWV/WWVH because:
1. BPM has unique propagation delay (much longer path from China)
2. Tick duration is different (10ms vs 5ms for WWV)
3. No voice announcements to confuse with WWV/WWVH

Primary discrimination method:
    - Geographic delay prediction (BPM is ~10,000 km from US)
    - Tick duration measurement (10ms vs 5ms)
    - Correlation with expected BPM signal template

================================================================================
Author: HF Time Standard Team
"""

import logging
import numpy as np
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Tuple, List, Dict, Set
from enum import Enum

from .wwv_constants import (
    BPM_LAT, BPM_LON,
    BPM_TICK_FREQ,
    BPM_UTC_TICK_DURATION,
    BPM_UT1_TICK_DURATION,
    BPM_MINUTE_MARKER_DURATION,
    BPM_UT1_MINUTES,
    SPEED_OF_LIGHT_KM_S,
    EARTH_RADIUS_KM,
)

logger = logging.getLogger(__name__)


class BPMTimingMode(Enum):
    """BPM timing mode for current minute"""
    UTC = "UTC"      # Minutes 0-24, 30-54: Use for timing
    UT1 = "UT1"      # Minutes 25-29, 55-59: DO NOT use for UTC timing
    UNKNOWN = "UNKNOWN"


@dataclass
class BPMDiscriminationResult:
    """Result of BPM discrimination analysis"""
    # Detection confidence
    is_bpm_detected: bool
    confidence: float  # 0.0 - 1.0
    
    # Timing mode
    timing_mode: BPMTimingMode
    is_usable_for_utc: bool  # False during UT1 minutes
    
    # Signal characteristics
    tick_duration_ms: float  # Measured tick duration
    expected_tick_duration_ms: float  # Expected based on minute
    tick_duration_match: bool  # Does measured match expected?
    
    # Propagation
    expected_delay_ms: float  # From geographic model
    measured_delay_ms: Optional[float] = None
    delay_residual_ms: Optional[float] = None  # measured - expected
    
    # SNR and quality
    snr_db: float = 0.0
    quality_grade: str = "X"  # A/B/C/D/X
    
    # Discrimination method used
    method: str = "geographic"  # geographic, tick_duration, correlation
    
    # DUT1 correction (if UT1 minute and we want to use it anyway)
    dut1_correction_ms: Optional[float] = None


class BPMDiscriminator:
    """
    Discriminator for BPM (China) time signal broadcasts.
    
    Handles:
    1. UT1/UTC minute identification and filtering
    2. Tick duration measurement and validation
    3. Geographic delay prediction for discrimination
    4. DUT1 correction when using UT1 minutes (optional)
    
    Usage:
        discriminator = BPMDiscriminator(receiver_lat=38.0, receiver_lon=-90.0)
        
        # Check if minute is usable for UTC timing
        if discriminator.is_utc_minute(minute=30):
            result = discriminator.analyze(iq_samples, sample_rate, minute)
            if result.is_bpm_detected and result.is_usable_for_utc:
                # Use for timing
                pass
    """
    
    # UT1 minutes: 25-29 and 55-59
    UT1_MINUTES: Set[int] = BPM_UT1_MINUTES
    
    def __init__(
        self,
        receiver_lat: Optional[float] = None,
        receiver_lon: Optional[float] = None,
        dut1_ms: float = 0.0,  # Current DUT1 value (UT1-UTC) in ms
        enable_ut1_correction: bool = False,  # Allow using UT1 minutes with correction
        channel_name: str = "BPM",
        expected_delay_ms: Optional[float] = None,  # Injected delay from StationModel
        active_hours: Optional[Set[int]] = None     # Set of active UTC hours
    ):
        """
        Initialize BPM discriminator.
        
        Args:
            receiver_lat: Receiver latitude (degrees), defaults to US center if None
            receiver_lon: Receiver longitude (degrees), defaults to US center if None
            dut1_ms: Current DUT1 value (UT1-UTC) in milliseconds
            enable_ut1_correction: If True, allow using UT1 minutes with DUT1 correction
            channel_name: Channel identifier for logging
            expected_delay_ms: Optional pre-calculated delay
            active_hours: Set of UTC hours (0-23) when station is transmitting
        """
        # Default to approximate US center if coordinates not provided
        # This gives reasonable BPM distance estimates for continental US
        self.receiver_lat = receiver_lat if receiver_lat is not None else 39.0
        self.receiver_lon = receiver_lon if receiver_lon is not None else -98.0
        self.dut1_ms = dut1_ms
        self.enable_ut1_correction = enable_ut1_correction
        self.channel_name = channel_name
        
        # Default to always active if not specified
        self.active_hours = active_hours if active_hours is not None else set(range(24))
        
        # Pre-calculate great circle distance to BPM
        self.distance_to_bpm_km = self._haversine_distance(
            self.receiver_lat, self.receiver_lon, BPM_LAT, BPM_LON
        )
        
        # Expected propagation delay
        if expected_delay_ms is not None:
             self.expected_delay_ms = expected_delay_ms
        else:
             # Fallback to internal estimate if not provided
             self.expected_delay_ms = self._estimate_propagation_delay()
        
        logger.info(
            f"BPMDiscriminator initialized: distance={self.distance_to_bpm_km:.0f}km, "
            f"expected_delay={self.expected_delay_ms:.1f}ms"
        )
    
    @staticmethod
    def _haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Calculate great circle distance in km using Haversine formula."""
        lat1_rad = np.radians(lat1)
        lat2_rad = np.radians(lat2)
        dlat = np.radians(lat2 - lat1)
        dlon = np.radians(lon2 - lon1)
        
        a = np.sin(dlat/2)**2 + np.cos(lat1_rad) * np.cos(lat2_rad) * np.sin(dlon/2)**2
        c = 2 * np.arctan2(np.sqrt(a), np.sqrt(1-a))
        
        return EARTH_RADIUS_KM * c
    
    def _estimate_propagation_delay(self) -> float:
        """
        Estimate propagation delay from BPM to receiver.
        
        BPM paths to continental US are typically:
        - 2-3 hop F-layer paths
        - Total path length ~12,000-15,000 km
        - Delay: 40-50 ms
        
        This is a rough estimate for discrimination purposes.
        The actual delay is computed by the ionospheric model.
        """
        # Ground distance
        ground_km = self.distance_to_bpm_km
        
        # Using StationModelFactory logic for consistency:
        # Distance-dependent path factor heuristic
        if ground_km < 3000.0:
            factor = 1.15
        elif ground_km > 10000.0:
            factor = 1.05
        else:
            slope = -0.1 / 7000.0
            factor = 1.15 + slope * (ground_km - 3000.0)
            
        path_length_km = ground_km * factor
        delay_ms = (path_length_km / SPEED_OF_LIGHT_KM_S) * 1000.0
        
        return delay_ms
    
    def is_utc_minute(self, minute: int) -> bool:
        """
        Check if the given minute transmits UTC (vs UT1).
        
        Args:
            minute: Minute of the hour (0-59)
            
        Returns:
            True if this minute transmits UTC timing
        """
        return minute not in self.UT1_MINUTES
    
    def is_ut1_minute(self, minute: int) -> bool:
        """
        Check if the given minute transmits UT1.
        
        Args:
            minute: Minute of the hour (0-59)
            
        Returns:
            True if this minute transmits UT1 timing (25-29, 55-59)
        """
        return minute in self.UT1_MINUTES
    
    def get_timing_mode(self, minute: int) -> BPMTimingMode:
        """Get the timing mode for a given minute."""
        if minute in self.UT1_MINUTES:
            return BPMTimingMode.UT1
        else:
            return BPMTimingMode.UTC
    
    def get_expected_tick_duration_ms(self, minute: int) -> float:
        """
        Get expected tick duration for the given minute.
        
        Args:
            minute: Minute of the hour (0-59)
            
        Returns:
            Expected tick duration in milliseconds
        """
        if self.is_ut1_minute(minute):
            return BPM_UT1_TICK_DURATION * 1000.0  # 100 ms
        else:
            return BPM_UTC_TICK_DURATION * 1000.0  # 10 ms
    
    def analyze(
        self,
        iq_samples: np.ndarray,
        sample_rate: int,
        minute: int,
        measured_delay_ms: Optional[float] = None,
        snr_db: float = 0.0,
        hour: Optional[int] = None
    ) -> BPMDiscriminationResult:
        """
        Analyze IQ samples for BPM signal characteristics.
        """
        # 1. Schedule Check (Specificity Layer)
        # If the station is scheduled to be OFF, probability is zero.
        if not self._is_transmitting_schedule(minute, hour):
             return BPMDiscriminationResult(
                is_bpm_detected=False,
                confidence=0.0,
                timing_mode=BPMTimingMode.UNKNOWN,
                is_usable_for_utc=False,
                tick_duration_ms=0.0,
                expected_tick_duration_ms=0.0,
                tick_duration_match=False,
                expected_delay_ms=self.expected_delay_ms,
                measured_delay_ms=measured_delay_ms,
                snr_db=snr_db,
                quality_grade="X",
                method="schedule_filter"
             )

        timing_mode = self.get_timing_mode(minute)
        expected_tick_ms = self.get_expected_tick_duration_ms(minute)
        
        # Measure tick duration from signal
        measured_tick_ms = self._measure_tick_duration(iq_samples, sample_rate)
        
        # Measure minute marker arrival (primary timing reference)
        marker_detection = self._detect_minute_marker(iq_samples, sample_rate)
        marker_snr_db = None
        measured_delay = measured_delay_ms
        if marker_detection:
            measured_delay = marker_detection.get('toa_ms')
            marker_snr_db = marker_detection.get('snr_db')
        
        # Measure SNR if not provided (or enhance with marker SNR)
        if snr_db <= 0.0:
            snr_db = self._measure_snr(iq_samples, sample_rate)
        if marker_snr_db is not None:
            snr_db = max(snr_db, marker_snr_db)
        
        # Check if tick duration matches expected
        tick_tolerance_ms = 5.0  # Allow 5ms tolerance
        # CRITICAL FIX: Only match if measurement was valid (>0)
        tick_match = (measured_tick_ms > 0) and (abs(measured_tick_ms - expected_tick_ms) < tick_tolerance_ms)
        
        # Determine if usable for UTC timing
        is_usable = timing_mode == BPMTimingMode.UTC
        
        # If UT1 minute but correction enabled, can still use with DUT1 correction
        dut1_correction = None
        if timing_mode == BPMTimingMode.UT1 and self.enable_ut1_correction:
            dut1_correction = self.dut1_ms
            is_usable = True  # Usable with correction
        
        # Calculate delay residual
        delay_residual = None
        if measured_delay is not None:
            delay_residual = measured_delay - self.expected_delay_ms
        
        # Confidence Calculation (ROC-inspired)
        confidence = self._calculate_confidence(
            tick_match, snr_db, measured_delay
        )
        
        # Quality grade
        quality_grade = self._calculate_quality_grade(confidence, snr_db, timing_mode)
        
        # Detection Threshold (Dynamic)
        # Higher threshold required if SNR is low or delay is ambiguous
        is_detected = confidence > 0.4 and snr_db > 6.0
        
        return BPMDiscriminationResult(
            is_bpm_detected=is_detected,
            confidence=confidence,
            timing_mode=timing_mode,
            is_usable_for_utc=is_usable,
            tick_duration_ms=measured_tick_ms,
            expected_tick_duration_ms=expected_tick_ms,
            tick_duration_match=tick_match,
            expected_delay_ms=self.expected_delay_ms,
            measured_delay_ms=measured_delay,
            delay_residual_ms=delay_residual,
            snr_db=snr_db,
            quality_grade=quality_grade,
            method="tick_duration" if tick_match else "geographic",
            dut1_correction_ms=dut1_correction
        )
    
    def _is_transmitting_schedule(self, minute: int, hour: Optional[int] = None) -> bool:
        """Check if BPM is broadcasting at the current UTC time."""
        # Specificity Layer: If we know the hour and it's not in active_hours, reject.
        if hour is not None:
            if hour not in self.active_hours:
                # logger.debug(f"BPM schedule rejection: Hour {hour} is not in active schedule")
                return False
        
        # If hour is unknown, we assume it fits (conservative to avoid missing valid signals)
        # unless relying on other checks.
        
        return True 

    def _measure_tick_duration(
        self,
        iq_samples: np.ndarray,
        sample_rate: int
    ) -> float:
        """
        Measure tick duration from IQ samples.
        """
        # ... (implementation same as before until return) ...
        # Bandpass filter around 1000 Hz
        from scipy.signal import butter, filtfilt
        
        # Design bandpass filter: 900-1100 Hz
        nyquist = sample_rate / 2
        low = 900 / nyquist
        high = 1100 / nyquist
        
        # Ensure filter frequencies are valid
        if high >= 1.0: high = 0.99
        if low <= 0: low = 0.01
            
        try:
            b, a = butter(4, [low, high], btype='band')
            filtered = filtfilt(b, a, np.abs(iq_samples))
        except Exception as e:
            logger.warning(f"BPM tick filter failed: {e}")
            return 0.0  # CRITICAL FIX: Return 0 on failure, not 10ms match!
        
        # Envelope detection
        envelope = np.abs(filtered)
        
        # Threshold at 50% of max
        threshold = np.max(envelope) * 0.5
        above_threshold = envelope > threshold
        
        # Find tick durations by measuring contiguous above-threshold regions
        tick_durations = []
        in_tick = False
        tick_start = 0
        
        for i, above in enumerate(above_threshold):
            if above and not in_tick:
                in_tick = True
                tick_start = i
            elif not above and in_tick:
                in_tick = False
                duration_samples = i - tick_start
                duration_ms = (duration_samples / sample_rate) * 1000.0
                # Only count reasonable tick durations (5-150 ms)
                if 5.0 < duration_ms < 150.0:
                    tick_durations.append(duration_ms)
        
        if tick_durations:
            # Return median tick duration
            return float(np.median(tick_durations))
        else:
            # CRITICAL FIX: Return 0.0 if no ticks found (noise/off-air)
            return 0.0 

    def _detect_minute_marker(
        self,
        iq_samples: np.ndarray,
        sample_rate: int
    ) -> Optional[Dict[str, float]]:
        """
        Detect the BPM minute marker (≈300 ms tick at second 0) and measure its arrival time.
        
        Returns:
            Dict with 'toa_ms', 'duration_ms', 'snr_db' if marker detected, else None.
        """
        if len(iq_samples) == 0:
            return None
        
        # Only analyze first 0.6 seconds to focus on the minute marker
        analysis_samples = min(len(iq_samples), int(sample_rate * 0.5))
        if analysis_samples <= 0:
            return None
        
        segment = iq_samples[:analysis_samples]
        magnitude = np.abs(segment)
        audio = magnitude - np.mean(magnitude)
        
        from scipy.signal import butter, filtfilt
        
        nyquist = sample_rate / 2
        low = 900 / nyquist
        high = 1100 / nyquist
        if high >= 1.0:
            high = 0.99
        if low <= 0.0:
            low = 0.01
        
        try:
            b, a = butter(4, [low, high], btype='band')
            filtered = filtfilt(b, a, audio)
        except Exception:
            return None
        
        envelope = np.abs(filtered)
        smooth_len = max(3, int(0.003 * sample_rate))
        if smooth_len > 1:
            kernel = np.ones(smooth_len) / smooth_len
            envelope = np.convolve(envelope, kernel, mode='same')
        
        # Correlate against ~200 ms window to emphasize the minute marker
        marker_len_samples = max(int(0.18 * sample_rate), 1)
        marker_window = np.ones(marker_len_samples)
        energy = envelope ** 2
        energy_avg = np.convolve(energy, marker_window, mode='same')
        
        # Use the lower quartile as noise floor estimate (more robust than median
        # when the marker occupies a significant fraction of the window)
        noise_floor = float(np.percentile(energy_avg, 25))
        max_energy = float(np.max(energy_avg)) if len(energy_avg) else 0.0
        peak_idx = int(np.argmax(energy_avg))
        peak_value = energy_avg[peak_idx]
        
        # Require peak to be at least 2x the noise floor
        if noise_floor <= 0:
            noise_floor = 1e-12
        if peak_value < noise_floor * 2.0:
            return None
        
        min_len = int(0.15 * sample_rate)
        max_len = int(0.45 * sample_rate)
        
        # Find marker onset by searching from the beginning of the envelope
        # Use a threshold based on peak value to find where the marker starts
        onset_threshold = 0.3 * np.max(envelope)
        above_onset = envelope > onset_threshold
        
        if not np.any(above_onset):
            return None
        
        # Find first and last samples above threshold
        marker_start = int(np.argmax(above_onset))
        marker_end = len(envelope) - int(np.argmax(above_onset[::-1]))
        
        duration_samples = marker_end - marker_start
        if not (min_len <= duration_samples <= max_len):
            return None
        
        duration_ms = duration_samples / sample_rate * 1000.0
        toa_ms = marker_start / sample_rate * 1000.0
        
        signal_power = np.max(energy_avg[marker_start:marker_end]) if marker_end > marker_start else 0.0
        noise_region = energy_avg[:max(marker_start, 1)]
        noise_floor = np.median(noise_region) if len(noise_region) > 0 else 1e-12
        if noise_floor <= 0:
            noise_floor = 1e-12
        snr_db = 10 * np.log10(max(signal_power, 1e-12) / noise_floor)
        
        return {
            'toa_ms': float(toa_ms),
            'duration_ms': float(duration_ms),
            'snr_db': float(snr_db)
        }
    
    def _measure_snr(
        self,
        iq_samples: np.ndarray,
        sample_rate: int
    ) -> float:
        """
        Measure SNR of 1000 Hz tone (BPM tick frequency).
        
        Args:
            iq_samples: Complex IQ samples
            sample_rate: Sample rate in Hz
            
        Returns:
            Estimated SNR in dB
        """
        try:
            from scipy.signal import welch
            
            # Compute power spectral density
            freqs, psd = welch(np.abs(iq_samples), fs=sample_rate, nperseg=min(4096, len(iq_samples)//4))
            
            # Find 1000 Hz bin
            tone_idx = np.argmin(np.abs(freqs - 1000))
            
            # Signal power: peak around 1000 Hz (±50 Hz)
            tone_mask = (freqs > 950) & (freqs < 1050)
            signal_power = np.max(psd[tone_mask]) if np.any(tone_mask) else 0
            
            # Noise power: median of spectrum excluding tone region
            noise_mask = ~tone_mask
            noise_power = np.median(psd[noise_mask]) if np.any(noise_mask) else 1e-10
            
            # SNR in dB
            if noise_power > 0 and signal_power > noise_power:
                snr_db = 10 * np.log10(signal_power / noise_power)
                return float(min(40.0, max(0.0, snr_db)))  # Clamp to reasonable range
            else:
                return 0.0
        except Exception as e:
            logger.debug(f"BPM SNR measurement failed: {e}")
            return 0.0 
    
    def _calculate_confidence(
        self,
        tick_match: bool,
        snr_db: float,
        measured_delay_ms: Optional[float]
    ) -> float:
        """Calculate overall detection confidence using Gaussian weighting."""
        confidence = 0.0
        
        # 1. Tick Duration Match (Primary Feature)
        if tick_match:
            confidence += 0.4
        
        # 2. SNR Contribution (Sigmoid-like or steps)
        # Using a smoother mapping: 0 at 6dB, max 0.3 at 20dB
        if snr_db > 6.0:
            snr_score = min(0.3, (snr_db - 6.0) / 46.6) # Linear approx: 14/46 ->~0.3
            # Or simpler:
            snr_score = min(0.3, (snr_db - 6.0) * 0.02) # +0.02 per dB above 6
            confidence += snr_score
        
        # 3. Delay Plausibility (Gaussian Window Penalty)
        # Uses Gaussian-weighted window centered on Expected ToA
        if measured_delay_ms is not None:
            delay_error = abs(measured_delay_ms - self.expected_delay_ms)
            
            # Sigma = 5.0 ms (Standard deviation of window)
            # At error=0, score=0.3
            # At error=5, score=0.3 * e^-0.5 = 0.18
            # At error=10, score=0.3 * e^-2 = 0.04
            sigma = 5.0
            gaussian_score = 0.3 * np.exp(-0.5 * (delay_error / sigma)**2)
            confidence += gaussian_score
        
        return min(1.0, confidence)
    
    def _calculate_quality_grade(
        self,
        confidence: float,
        snr_db: float,
        timing_mode: BPMTimingMode
    ) -> str:
        """Calculate quality grade for BPM measurement."""
        # UT1 minutes get downgraded
        if timing_mode == BPMTimingMode.UT1:
            if not self.enable_ut1_correction:
                return "X"  # Unusable without correction
            # With correction, downgrade by one grade
            grade_penalty = 1
        else:
            grade_penalty = 0
        
        # Base grade from confidence and SNR
        if confidence > 0.8 and snr_db > 15:
            base_grade = 0  # A
        elif confidence > 0.6 and snr_db > 10:
            base_grade = 1  # B
        elif confidence > 0.4 and snr_db > 6:
            base_grade = 2  # C
        else:
            base_grade = 3  # D
        
        # Apply penalty
        final_grade = min(3, base_grade + grade_penalty)
        
        return ["A", "B", "C", "D"][final_grade]
    
    def update_dut1(self, dut1_ms: float) -> None:
        """
        Update the DUT1 value (UT1-UTC).
        
        DUT1 is broadcast by WWV/WWVH and can be used to correct
        BPM UT1 minutes if enable_ut1_correction is True.
        
        Args:
            dut1_ms: New DUT1 value in milliseconds
        """
        self.dut1_ms = dut1_ms
        logger.debug(f"BPM DUT1 updated: {dut1_ms:.1f} ms")
    
    def detect_ut1_pulses(
        self,
        iq_samples: np.ndarray,
        sample_rate: int,
        minute: int,
        timing_offset_ms: float = -20.0
    ) -> Optional[Dict]:
        """
        Detect BPM UT1 pulses (100ms duration) for path calibration.
        
        During UT1 minutes (25-29, 55-59), BPM transmits 100ms pulses instead
        of 10ms UTC ticks. These are 10× longer than WWV's 5ms ticks, making
        them UNAMBIGUOUS markers for BPM detection and path calibration.
        
        This method provides:
        1. Definitive BPM detection (100ms pulse is unique)
        2. High-precision ToA measurement (longer pulse = better SNR)
        3. Path gain calibration (measure BPM signal strength)
        
        Args:
            iq_samples: Complex IQ samples (full minute)
            sample_rate: Sample rate in Hz
            minute: Current minute (0-59)
            timing_offset_ms: BPM timing offset (-20ms advance)
            
        Returns:
            Dict with calibration data, or None if not a UT1 minute or no detection:
            {
                'detected': bool,
                'pulse_count': int,
                'mean_duration_ms': float,
                'duration_std_ms': float,
                'mean_power_db': float,
                'mean_toa_ms': float,  # Mean ToA from minute boundary
                'toa_std_ms': float,
                'snr_db': float,
                'confidence': float,
                'calibration_quality': str  # 'excellent', 'good', 'fair', 'poor'
            }
        """
        if minute not in self.UT1_MINUTES:
            return None
        
        from scipy.signal import butter, sosfiltfilt, hilbert
        from scipy.fft import rfft, rfftfreq
        
        # AM demodulation
        magnitude = np.abs(iq_samples)
        audio = magnitude - np.mean(magnitude)
        
        # Bandpass filter around 1000 Hz (BPM tick frequency)
        # Use SOS filter for numerical stability
        nyquist = sample_rate / 2
        low_hz = 950
        high_hz = 1050
        
        try:
            sos = butter(4, [low_hz / nyquist, high_hz / nyquist], btype='band', output='sos')
            filtered = sosfiltfilt(sos, audio)
        except Exception as e:
            logger.warning(f"{self.channel_name}: UT1 pulse filter failed: {e}")
            return None
        
        # Compute envelope using Hilbert transform (more accurate than abs)
        analytic = hilbert(filtered)
        envelope = np.abs(analytic)
        
        # Smooth envelope with moving average (~5ms window)
        window_samples = max(3, int(0.005 * sample_rate))
        kernel = np.ones(window_samples) / window_samples
        envelope_smooth = np.convolve(envelope, kernel, mode='same')
        
        # Adaptive threshold: median + 3*MAD (robust to outliers)
        median_env = np.median(envelope_smooth)
        mad = np.median(np.abs(envelope_smooth - median_env))
        threshold = median_env + 3 * mad * 1.4826  # 1.4826 scales MAD to std
        
        # Find pulses by threshold crossing
        above_threshold = envelope_smooth > threshold
        
        # Detect pulse boundaries
        pulses = []
        in_pulse = False
        pulse_start = 0
        
        for i in range(len(above_threshold)):
            if above_threshold[i] and not in_pulse:
                in_pulse = True
                pulse_start = i
            elif not above_threshold[i] and in_pulse:
                in_pulse = False
                pulse_end = i
                duration_samples = pulse_end - pulse_start
                duration_ms = (duration_samples / sample_rate) * 1000.0
                
                # UT1 pulses are ~100ms; filter for 70-150ms range
                if 70.0 <= duration_ms <= 150.0:
                    # Calculate pulse properties
                    pulse_samples = envelope_smooth[pulse_start:pulse_end]
                    pulse_power = np.mean(pulse_samples**2)
                    pulse_peak_idx = pulse_start + np.argmax(pulse_samples)
                    
                    # ToA is at pulse onset (pulse_start)
                    toa_samples = pulse_start
                    toa_ms = (toa_samples / sample_rate) * 1000.0
                    
                    # Determine which second this pulse belongs to
                    second = int(toa_ms / 1000.0)
                    
                    pulses.append({
                        'second': second,
                        'start_sample': pulse_start,
                        'end_sample': pulse_end,
                        'duration_ms': duration_ms,
                        'power': pulse_power,
                        'toa_ms': toa_ms,
                        'peak_idx': pulse_peak_idx
                    })
        
        if len(pulses) < 5:
            logger.debug(f"{self.channel_name}: UT1 minute {minute}: Only {len(pulses)} pulses detected (need ≥5)")
            return None
        
        # Calculate statistics
        durations = np.array([p['duration_ms'] for p in pulses])
        powers = np.array([p['power'] for p in pulses])
        toas = np.array([p['toa_ms'] for p in pulses])
        
        mean_duration = float(np.mean(durations))
        std_duration = float(np.std(durations))
        
        # Power in dB (relative to noise floor)
        noise_power = median_env**2
        mean_power_linear = np.mean(powers)
        mean_power_db = 10 * np.log10(mean_power_linear / noise_power) if noise_power > 0 else 0.0
        
        # ToA statistics (relative to expected second boundaries)
        # Each pulse should arrive at second + expected_delay + timing_offset
        expected_arrival_offset_ms = self.expected_delay_ms + timing_offset_ms
        toa_residuals = []
        for p in pulses:
            expected_toa = p['second'] * 1000.0 + expected_arrival_offset_ms
            residual = p['toa_ms'] - expected_toa
            toa_residuals.append(residual)
        
        toa_residuals = np.array(toa_residuals)
        mean_toa_residual = float(np.mean(toa_residuals))
        std_toa_residual = float(np.std(toa_residuals))
        
        # SNR estimate
        snr_db = float(mean_power_db)
        
        # Confidence based on:
        # - Number of pulses detected (expect ~59 for full minute)
        # - Duration consistency (std should be small)
        # - ToA consistency (std should be small)
        pulse_count_score = min(1.0, len(pulses) / 50.0)
        duration_score = max(0.0, 1.0 - std_duration / 20.0)
        toa_score = max(0.0, 1.0 - std_toa_residual / 10.0)
        snr_score = min(1.0, max(0.0, (snr_db - 6.0) / 20.0))
        
        confidence = (pulse_count_score * 0.3 + duration_score * 0.2 + 
                     toa_score * 0.3 + snr_score * 0.2)
        
        # Quality grade
        if confidence > 0.8 and snr_db > 15:
            quality = 'excellent'
        elif confidence > 0.6 and snr_db > 10:
            quality = 'good'
        elif confidence > 0.4 and snr_db > 6:
            quality = 'fair'
        else:
            quality = 'poor'
        
        result = {
            'detected': True,
            'minute': minute,
            'pulse_count': len(pulses),
            'mean_duration_ms': mean_duration,
            'duration_std_ms': std_duration,
            'mean_power_db': float(mean_power_db),
            'mean_toa_residual_ms': mean_toa_residual,
            'toa_std_ms': std_toa_residual,
            'expected_delay_ms': self.expected_delay_ms,
            'timing_offset_ms': timing_offset_ms,
            'snr_db': snr_db,
            'confidence': float(confidence),
            'calibration_quality': quality,
            'pulses': pulses  # Full pulse data for detailed analysis
        }
        
        logger.info(f"{self.channel_name}: BPM UT1 calibration (minute {minute}): "
                   f"{len(pulses)} pulses, duration={mean_duration:.1f}±{std_duration:.1f}ms, "
                   f"ToA_residual={mean_toa_residual:+.2f}±{std_toa_residual:.2f}ms, "
                   f"SNR={snr_db:.1f}dB, quality={quality}")
        
        return result
    
    def calibrate_from_ut1(
        self,
        ut1_result: Dict
    ) -> Optional[Dict]:
        """
        Update calibration parameters from UT1 pulse detection.
        
        Uses the UT1 pulse detection results to refine:
        1. Expected propagation delay
        2. Path gain estimate
        3. Timing offset verification
        
        Args:
            ut1_result: Result from detect_ut1_pulses()
            
        Returns:
            Dict with calibration updates, or None if quality too low
        """
        if ut1_result is None or not ut1_result.get('detected', False):
            return None
        
        if ut1_result['calibration_quality'] in ['poor']:
            logger.debug(f"{self.channel_name}: UT1 calibration quality too low: {ut1_result['calibration_quality']}")
            return None
        
        # Update expected delay based on measured ToA residual
        # New delay = old delay + mean residual
        old_delay = self.expected_delay_ms
        toa_residual = ut1_result['mean_toa_residual_ms']
        new_delay = old_delay + toa_residual
        
        # Only update if residual is significant but not implausible
        if abs(toa_residual) > 0.5 and abs(toa_residual) < 20.0:
            self.expected_delay_ms = new_delay
            logger.info(f"{self.channel_name}: BPM delay calibrated: {old_delay:.2f}ms → {new_delay:.2f}ms "
                       f"(residual: {toa_residual:+.2f}ms)")
        
        return {
            'old_delay_ms': old_delay,
            'new_delay_ms': new_delay,
            'adjustment_ms': toa_residual,
            'path_gain_db': ut1_result['mean_power_db'],
            'confidence': ut1_result['confidence'],
            'quality': ut1_result['calibration_quality']
        }


def create_bpm_discriminator(
    receiver_lat: float,
    receiver_lon: float,
    channel_name: str = "BPM"
) -> BPMDiscriminator:
    """
    Factory function to create a BPM discriminator.
    
    Args:
        receiver_lat: Receiver latitude
        receiver_lon: Receiver longitude
        channel_name: Channel identifier
        
    Returns:
        Configured BPMDiscriminator instance
    """
    return BPMDiscriminator(
        receiver_lat=receiver_lat,
        receiver_lon=receiver_lon,
        channel_name=channel_name
    )
