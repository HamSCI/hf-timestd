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
        channel_name: str = "BPM"
    ):
        """
        Initialize BPM discriminator.
        
        Args:
            receiver_lat: Receiver latitude (degrees), defaults to US center if None
            receiver_lon: Receiver longitude (degrees), defaults to US center if None
            dut1_ms: Current DUT1 value (UT1-UTC) in milliseconds
            enable_ut1_correction: If True, allow using UT1 minutes with DUT1 correction
            channel_name: Channel identifier for logging
        """
        # Default to approximate US center if coordinates not provided
        # This gives reasonable BPM distance estimates for continental US
        self.receiver_lat = receiver_lat if receiver_lat is not None else 39.0
        self.receiver_lon = receiver_lon if receiver_lon is not None else -98.0
        self.dut1_ms = dut1_ms
        self.enable_ut1_correction = enable_ut1_correction
        self.channel_name = channel_name
        
        # Pre-calculate great circle distance to BPM
        self.distance_to_bpm_km = self._haversine_distance(
            self.receiver_lat, self.receiver_lon, BPM_LAT, BPM_LON
        )
        
        # Expected propagation delay (rough estimate for discrimination)
        # BPM to US is typically 35-50 ms via multi-hop F-layer
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
        
        # For long paths like BPM->US, assume 2-3 hop F-layer
        # Each hop adds ~300km of height (up and down)
        # Typical: 2.5 hops for ~10,000 km path
        n_hops = max(2, ground_km / 4000)  # ~4000 km per hop
        
        # Approximate path length including ionospheric reflection
        f_layer_height_km = 300.0
        hop_overhead_km = 2 * f_layer_height_km * n_hops  # Up and down for each hop
        total_path_km = np.sqrt(ground_km**2 + hop_overhead_km**2)
        
        # Convert to delay
        delay_ms = (total_path_km / SPEED_OF_LIGHT_KM_S) * 1000.0
        
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
        snr_db: float = 0.0
    ) -> BPMDiscriminationResult:
        """
        Analyze IQ samples for BPM signal characteristics.
        
        Args:
            iq_samples: Complex IQ samples (1 minute of data)
            sample_rate: Sample rate in Hz
            minute: Current minute (0-59)
            measured_delay_ms: Measured propagation delay (if available)
            snr_db: Signal-to-noise ratio in dB
            
        Returns:
            BPMDiscriminationResult with detection and timing info
        """
        timing_mode = self.get_timing_mode(minute)
        expected_tick_ms = self.get_expected_tick_duration_ms(minute)
        
        # Measure tick duration from signal
        measured_tick_ms = self._measure_tick_duration(iq_samples, sample_rate)
        
        # Measure SNR if not provided
        if snr_db <= 0.0:
            snr_db = self._measure_snr(iq_samples, sample_rate)
        
        # Check if tick duration matches expected
        tick_tolerance_ms = 5.0  # Allow 5ms tolerance
        tick_match = abs(measured_tick_ms - expected_tick_ms) < tick_tolerance_ms
        
        # Determine if usable for UTC timing
        is_usable = timing_mode == BPMTimingMode.UTC
        
        # If UT1 minute but correction enabled, can still use with DUT1 correction
        dut1_correction = None
        if timing_mode == BPMTimingMode.UT1 and self.enable_ut1_correction:
            dut1_correction = self.dut1_ms
            is_usable = True  # Usable with correction
        
        # Calculate delay residual if measured delay available
        delay_residual = None
        if measured_delay_ms is not None:
            delay_residual = measured_delay_ms - self.expected_delay_ms
        
        # Confidence based on:
        # - Tick duration match
        # - SNR
        # - Delay plausibility
        confidence = self._calculate_confidence(
            tick_match, snr_db, measured_delay_ms
        )
        
        # Quality grade
        quality_grade = self._calculate_quality_grade(confidence, snr_db, timing_mode)
        
        # Detection threshold
        is_detected = confidence > 0.3 and snr_db > 6.0
        
        return BPMDiscriminationResult(
            is_bpm_detected=is_detected,
            confidence=confidence,
            timing_mode=timing_mode,
            is_usable_for_utc=is_usable,
            tick_duration_ms=measured_tick_ms,
            expected_tick_duration_ms=expected_tick_ms,
            tick_duration_match=tick_match,
            expected_delay_ms=self.expected_delay_ms,
            measured_delay_ms=measured_delay_ms,
            delay_residual_ms=delay_residual,
            snr_db=snr_db,
            quality_grade=quality_grade,
            method="tick_duration" if tick_match else "geographic",
            dut1_correction_ms=dut1_correction
        )
    
    def _measure_tick_duration(
        self,
        iq_samples: np.ndarray,
        sample_rate: int
    ) -> float:
        """
        Measure tick duration from IQ samples.
        
        Uses envelope detection and threshold crossing to measure
        the duration of 1000 Hz ticks.
        
        Args:
            iq_samples: Complex IQ samples
            sample_rate: Sample rate in Hz
            
        Returns:
            Measured tick duration in milliseconds
        """
        # Bandpass filter around 1000 Hz
        from scipy.signal import butter, filtfilt
        
        # Design bandpass filter: 900-1100 Hz
        nyquist = sample_rate / 2
        low = 900 / nyquist
        high = 1100 / nyquist
        
        # Ensure filter frequencies are valid
        if high >= 1.0:
            high = 0.99
        if low <= 0:
            low = 0.01
            
        try:
            b, a = butter(4, [low, high], btype='band')
            filtered = filtfilt(b, a, np.abs(iq_samples))
        except Exception as e:
            logger.warning(f"BPM tick filter failed: {e}")
            return BPM_UTC_TICK_DURATION * 1000.0  # Default to 10ms
        
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
            # Default to UTC tick duration
            return BPM_UTC_TICK_DURATION * 1000.0
    
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
        """Calculate overall detection confidence."""
        confidence = 0.0
        
        # Tick duration match: +0.4
        if tick_match:
            confidence += 0.4
        
        # SNR contribution: 0-0.3
        if snr_db > 20:
            confidence += 0.3
        elif snr_db > 15:
            confidence += 0.25
        elif snr_db > 10:
            confidence += 0.2
        elif snr_db > 6:
            confidence += 0.1
        
        # Delay plausibility: 0-0.3
        if measured_delay_ms is not None:
            delay_error = abs(measured_delay_ms - self.expected_delay_ms)
            if delay_error < 5.0:
                confidence += 0.3
            elif delay_error < 10.0:
                confidence += 0.2
            elif delay_error < 20.0:
                confidence += 0.1
        
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
