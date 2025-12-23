#!/usr/bin/env python3
"""
Correlator Bank - Parallel Matched Filtering for Multi-Station Detection

================================================================================
PURPOSE
================================================================================
Implement Maximum Likelihood Estimation (MLE) based station detection by running
a bank of correlators in parallel, each centered on the predicted Time-of-Arrival
(ToA) window for a specific station.

This replaces voting-based discrimination with physics-based component decomposition:
- Each station has a known template (tone frequency, tick duration)
- Each station has a predicted ToA (based on distance + ionospheric model)
- The correlator output quantifies each station's power contribution

================================================================================
ARCHITECTURE
================================================================================

    IQ Samples ──┬──► WWV Correlator (1000 Hz, 5ms) ──► WWV_power, WWV_ToA
                 │    [search: 4ms ± 10ms]
                 │
                 ├──► WWVH Correlator (1200 Hz, 5ms) ──► WWVH_power, WWVH_ToA
                 │    [search: 25ms ± 10ms]
                 │
                 └──► BPM Correlator (1000 Hz, 10ms) ──► BPM_power, BPM_ToA
                      [search: 24ms ± 10ms]  (includes -20ms offset)

    Output: ChannelAssignment with per-station power and ToA

================================================================================
KEY FEATURES
================================================================================
1. Station-specific templates (different tone frequencies and durations)
2. Predicted ToA windows (narrow search after calibration)
3. Super-resolution ToA via parabolic interpolation
4. Cross-validation between stations
5. Residual noise estimation

================================================================================
Author: HF Time Standard Team
Date: 2025-12-17
"""

import logging
import numpy as np
from typing import Optional, Dict, List, Tuple
from dataclasses import dataclass
from scipy import signal as scipy_signal
from scipy.signal import correlate

from .station_model import (
    StationModel, StationModelFactory, ChannelAssignment, StationID
)
from .wwv_constants import (
    BPM_UT1_MINUTES,
    SAMPLE_RATE_FULL,
)

logger = logging.getLogger(__name__)


@dataclass
class CorrelatorResult:
    """Result from a single station correlator."""
    station: str
    detected: bool
    
    # Correlation metrics
    peak_correlation: float
    snr_db: float
    confidence: float
    
    # Timing
    toa_ms: float  # Time of arrival from buffer start
    toa_refined_ms: float  # Sub-sample refined ToA
    timing_error_ms: float  # ToA - expected_delay (residual)
    
    # Power
    component_power_db: float
    
    # Search window used
    search_center_ms: float
    search_width_ms: float
    
    # Template info
    tone_frequency_hz: float
    tick_duration_ms: float


class CorrelatorBank:
    """
    Bank of parallel correlators for multi-station detection.
    
    Each correlator is tuned to a specific station's signal characteristics
    and searches within a predicted ToA window.
    """
    
    def __init__(
        self,
        receiver_lat: float,
        receiver_lon: float,
        sample_rate: int = SAMPLE_RATE_FULL,
        calibrated: bool = False
    ):
        """
        Initialize correlator bank.
        
        Args:
            receiver_lat: Receiver latitude (degrees)
            receiver_lon: Receiver longitude (degrees)
            sample_rate: Sample rate in Hz
            calibrated: Whether station delays have been calibrated
        """
        self.sample_rate = sample_rate
        self.calibrated = calibrated
        
        # Create station models
        self.factory = StationModelFactory(receiver_lat, receiver_lon)
        self.models: Dict[StationID, StationModel] = self.factory.create_all_models()
        
        # Pre-compute templates for each station
        self.templates: Dict[StationID, Dict] = {}
        for station_id, model in self.models.items():
            self.templates[station_id] = self._create_quadrature_template(model)
        
        # Calibration state (updated from UT1 detection, etc.)
        self.calibration_offsets: Dict[StationID, float] = {
            sid: 0.0 for sid in StationID
        }
        
        logger.info(f"CorrelatorBank initialized with {len(self.models)} station models")
    
    def _create_quadrature_template(
        self,
        model: StationModel,
        minute: int = 0
    ) -> Dict:
        """
        Create quadrature matched filter templates for a station.
        
        Args:
            model: Station model
            minute: Current minute (affects BPM tick duration)
            
        Returns:
            Dict with 'sin', 'cos' templates and metadata
        """
        duration_sec = model.get_tick_duration_for_minute(minute)
        n_samples = int(duration_sec * self.sample_rate)
        t = np.arange(n_samples) / self.sample_rate
        
        # Tukey window (α=0.1) for smooth edges
        window = scipy_signal.windows.tukey(n_samples, alpha=0.1)
        
        # Quadrature templates
        template_sin = np.sin(2 * np.pi * model.tone_frequency_hz * t) * window
        template_cos = np.cos(2 * np.pi * model.tone_frequency_hz * t) * window
        
        # Normalize to unit energy
        template_sin = template_sin / np.linalg.norm(template_sin)
        template_cos = template_cos / np.linalg.norm(template_cos)
        
        return {
            'sin': template_sin,
            'cos': template_cos,
            'frequency_hz': model.tone_frequency_hz,
            'duration_sec': duration_sec,
            'n_samples': n_samples
        }
    
    def _correlate_station(
        self,
        audio_signal: np.ndarray,
        model: StationModel,
        template: Dict,
        minute: int,
        second: int = 0
    ) -> CorrelatorResult:
        """
        Run correlator for a single station.
        
        Args:
            audio_signal: AM-demodulated audio signal
            model: Station model
            template: Quadrature template dict
            minute: Current minute (0-59)
            second: Second within minute to analyze (0-59)
            
        Returns:
            CorrelatorResult with detection metrics
        """
        # Get search window
        calibration_offset = self.calibration_offsets.get(model.station, 0.0)
        center_ms, width_ms = model.get_search_window(minute, self.calibrated)
        center_ms += calibration_offset
        
        # Convert to samples
        samples_per_ms = self.sample_rate / 1000.0
        
        # Search window for this second
        second_start_sample = int(second * self.sample_rate)
        search_center_sample = second_start_sample + int(center_ms * samples_per_ms)
        search_half_width_samples = int(width_ms * samples_per_ms)
        
        search_start = max(0, search_center_sample - search_half_width_samples)
        search_end = min(len(audio_signal), search_center_sample + search_half_width_samples)
        
        if search_end <= search_start + template['n_samples']:
            # Not enough samples
            return CorrelatorResult(
                station=model.station.value,
                detected=False,
                peak_correlation=0.0,
                snr_db=0.0,
                confidence=0.0,
                toa_ms=0.0,
                toa_refined_ms=0.0,
                timing_error_ms=0.0,
                component_power_db=-100.0,
                search_center_ms=center_ms,
                search_width_ms=width_ms,
                tone_frequency_hz=model.tone_frequency_hz,
                tick_duration_ms=model.get_tick_duration_for_minute(minute) * 1000
            )
        
        # Extract search region
        search_region = audio_signal[search_start:search_end]
        
        # Quadrature correlation
        corr_sin = correlate(search_region, template['sin'], mode='valid')
        corr_cos = correlate(search_region, template['cos'], mode='valid')
        
        # Phase-invariant envelope
        envelope = np.sqrt(corr_sin**2 + corr_cos**2)
        
        if len(envelope) == 0:
            return CorrelatorResult(
                station=model.station.value,
                detected=False,
                peak_correlation=0.0,
                snr_db=0.0,
                confidence=0.0,
                toa_ms=0.0,
                toa_refined_ms=0.0,
                timing_error_ms=0.0,
                component_power_db=-100.0,
                search_center_ms=center_ms,
                search_width_ms=width_ms,
                tone_frequency_hz=model.tone_frequency_hz,
                tick_duration_ms=model.get_tick_duration_for_minute(minute) * 1000
            )
        
        # Find peak
        peak_idx = np.argmax(envelope)
        peak_value = envelope[peak_idx]
        
        # Sub-sample refinement via parabolic interpolation
        refined_offset = 0.0
        if 0 < peak_idx < len(envelope) - 1:
            y_m1 = envelope[peak_idx - 1]
            y_0 = envelope[peak_idx]
            y_p1 = envelope[peak_idx + 1]
            
            denom = y_m1 - 2*y_0 + y_p1
            if abs(denom) > 1e-10:
                refined_offset = 0.5 * (y_m1 - y_p1) / denom
                refined_offset = max(-0.5, min(0.5, refined_offset))
        
        # Calculate ToA
        peak_sample = search_start + peak_idx + template['n_samples'] // 2
        refined_sample = peak_sample + refined_offset
        
        toa_ms = (peak_sample / self.sample_rate) * 1000.0 - second * 1000.0
        toa_refined_ms = (refined_sample / self.sample_rate) * 1000.0 - second * 1000.0
        
        # Timing error (residual from expected)
        expected_toa_ms = model.expected_delay_ms + model.timing_offset_ms + calibration_offset
        timing_error_ms = toa_refined_ms - expected_toa_ms
        
        # Check Exclusion Zones (prevent cross-talk)
        # timing_error_ms is the offset from expected arrival.
        # exclusion_zones are defined as (start, end) ranges of this error.
        in_exclusion_zone = False
        for zone_start, zone_end in model.exclusion_zones:
            if zone_start <= timing_error_ms <= zone_end:
                in_exclusion_zone = True
                logger.debug(f"{model.station.value} peak at {timing_error_ms:+.1f}ms masked by exclusion zone [{zone_start:+.1f}, {zone_end:+.1f}]")
                break
        
        # Noise estimation (from correlation values away from peak)
        noise_region = np.concatenate([envelope[:max(1, peak_idx-10)], 
                                       envelope[min(len(envelope)-1, peak_idx+10):]])
        if len(noise_region) > 0:
            noise_floor = np.median(noise_region)
            noise_std = np.std(noise_region)
        else:
            noise_floor = 1e-10
            noise_std = 1e-10
        
        # SNR calculation
        if noise_floor > 0:
            snr_linear = peak_value / noise_floor
            snr_db = 20 * np.log10(snr_linear) if snr_linear > 0 else -100.0
        else:
            snr_db = 0.0
        
        # Component power (dB relative to noise)
        component_power_db = snr_db
        
        # Detection threshold
        detection_threshold = noise_floor + 3 * noise_std
        detected = (peak_value > detection_threshold and 
                    snr_db > 6.0 and 
                    not in_exclusion_zone)
        
        # Confidence based on SNR and timing plausibility
        snr_confidence = min(1.0, max(0.0, (snr_db - 6.0) / 20.0))
        timing_confidence = max(0.0, 1.0 - abs(timing_error_ms) / 20.0)
        
        # Zero confidence if in exclusion zone
        if in_exclusion_zone:
            confidence = 0.0
        else:
            confidence = 0.7 * snr_confidence + 0.3 * timing_confidence
        
        return CorrelatorResult(
            station=model.station.value,
            detected=detected,
            peak_correlation=float(peak_value),
            snr_db=float(snr_db),
            confidence=float(confidence),
            toa_ms=float(toa_ms),
            toa_refined_ms=float(toa_refined_ms),
            timing_error_ms=float(timing_error_ms),
            component_power_db=float(component_power_db),
            search_center_ms=float(center_ms),
            search_width_ms=float(width_ms),
            tone_frequency_hz=model.tone_frequency_hz,
            tick_duration_ms=model.get_tick_duration_for_minute(minute) * 1000
        )
    
    def process_minute(
        self,
        iq_samples: np.ndarray,
        frequency_mhz: float,
        minute: int,
        channel: str,
        minute_timestamp: float
    ) -> ChannelAssignment:
        """
        Process a full minute of IQ samples through the correlator bank.
        
        Args:
            iq_samples: Complex IQ samples (full minute)
            frequency_mhz: Center frequency in MHz
            minute: Current minute (0-59)
            channel: Channel name
            minute_timestamp: Unix timestamp of minute boundary
            
        Returns:
            ChannelAssignment with per-station power and ToA
        """
        # AM demodulation
        magnitude = np.abs(iq_samples)
        audio_signal = magnitude - np.mean(magnitude)
        
        # Get station models for this frequency
        models = self.factory.get_models_for_frequency(frequency_mhz)
        
        # Update templates for BPM if this is a UT1 minute
        for model in models:
            if model.station == StationID.BPM:
                self.templates[StationID.BPM] = self._create_quadrature_template(model, minute)
        
        # Run correlators for each station at second 0 (minute marker)
        results: Dict[str, CorrelatorResult] = {}
        for model in models:
            template = self.templates[model.station]
            result = self._correlate_station(audio_signal, model, template, minute, second=0)
            results[model.station.value] = result
        
        # Also correlate across multiple seconds and average for better SNR
        # Use seconds 1-10 for additional measurements
        multi_second_results: Dict[str, List[CorrelatorResult]] = {
            model.station.value: [] for model in models
        }
        
        for second in range(1, min(11, len(iq_samples) // self.sample_rate)):
            for model in models:
                template = self.templates[model.station]
                result = self._correlate_station(audio_signal, model, template, minute, second)
                if result.detected:
                    multi_second_results[model.station.value].append(result)
        
        # Average multi-second results
        for station_name, second_results in multi_second_results.items():
            if len(second_results) >= 3:
                avg_snr = np.mean([r.snr_db for r in second_results])
                avg_toa = np.mean([r.toa_refined_ms for r in second_results])
                avg_confidence = np.mean([r.confidence for r in second_results])
                
                # Update main result if multi-second average is better
                if station_name in results:
                    if avg_snr > results[station_name].snr_db:
                        results[station_name] = CorrelatorResult(
                            station=station_name,
                            detected=True,
                            peak_correlation=results[station_name].peak_correlation,
                            snr_db=float(avg_snr),
                            confidence=float(avg_confidence),
                            toa_ms=float(avg_toa),
                            toa_refined_ms=float(avg_toa),
                            timing_error_ms=results[station_name].timing_error_ms,
                            component_power_db=float(avg_snr),
                            search_center_ms=results[station_name].search_center_ms,
                            search_width_ms=results[station_name].search_width_ms,
                            tone_frequency_hz=results[station_name].tone_frequency_hz,
                            tick_duration_ms=results[station_name].tick_duration_ms
                        )
        
        # Build ChannelAssignment
        assignment = ChannelAssignment(
            minute_timestamp=minute_timestamp,
            channel=channel,
            frequency_mhz=frequency_mhz
        )
        
        # Populate per-station fields
        for station_name, result in results.items():
            if station_name == 'WWV':
                assignment.wwv_component_power_db = result.component_power_db if result.detected else None
                assignment.wwv_toa_ms = result.toa_refined_ms if result.detected else None
                assignment.wwv_confidence = result.confidence
            elif station_name == 'WWVH':
                assignment.wwvh_component_power_db = result.component_power_db if result.detected else None
                assignment.wwvh_toa_ms = result.toa_refined_ms if result.detected else None
                assignment.wwvh_confidence = result.confidence
            elif station_name == 'BPM':
                assignment.bpm_component_power_db = result.component_power_db if result.detected else None
                assignment.bpm_toa_ms = result.toa_refined_ms if result.detected else None
                assignment.bpm_confidence = result.confidence
                assignment.bpm_timing_mode = 'UT1' if minute in BPM_UT1_MINUTES else 'UTC'
                assignment.bpm_usable_for_timing = minute not in BPM_UT1_MINUTES
                assignment.bpm_tick_duration_ms = result.tick_duration_ms
            elif station_name == 'CHU':
                assignment.chu_component_power_db = result.component_power_db if result.detected else None
                assignment.chu_toa_ms = result.toa_refined_ms if result.detected else None
                assignment.chu_confidence = result.confidence
        
        # Cross-validation
        assignment = self._cross_validate(assignment, results)
        
        # Check for calibration opportunity
        for model in models:
            if model.is_calibration_minute(minute):
                assignment.is_calibration_minute = True
                assignment.calibration_station = model.station.value
                break
        
        # Estimate residual noise
        detected_powers = [r.component_power_db for r in results.values() if r.detected]
        if detected_powers:
            # Residual is noise floor estimate
            assignment.residual_noise_db = float(np.min(detected_powers) - 10.0)
        
        # Log summary
        detected_stations = [r.station for r in results.values() if r.detected]
        if detected_stations:
            logger.info(f"CorrelatorBank {channel} minute {minute}: "
                       f"Detected: {', '.join(detected_stations)}")
            for r in results.values():
                if r.detected:
                    logger.debug(f"  {r.station}: SNR={r.snr_db:.1f}dB, "
                               f"ToA={r.toa_refined_ms:.2f}ms, "
                               f"error={r.timing_error_ms:+.2f}ms")
        
        return assignment
    
    def _cross_validate(
        self,
        assignment: ChannelAssignment,
        results: Dict[str, CorrelatorResult]
    ) -> ChannelAssignment:
        """
        Cross-validate timing between detected stations.
        
        All stations transmit at the same UTC instant, so after correcting
        for propagation delay, they should agree within ionospheric uncertainty.
        """
        # Collect corrected emission times
        emission_times = []
        
        for station_name, result in results.items():
            if not result.detected:
                continue
            
            model = self.models.get(StationID(station_name))
            if model is None:
                continue
            
            # T_emission = T_arrival - T_propagation - timing_offset
            expected_delay = model.expected_delay_ms + model.timing_offset_ms
            t_emission = result.toa_refined_ms - expected_delay
            emission_times.append((station_name, t_emission))
        
        if len(emission_times) >= 2:
            times = [t for _, t in emission_times]
            max_error = max(times) - min(times)
            
            # Adaptive threshold based on time of day and frequency
            # Ionospheric conditions are more variable at night and at lower frequencies
            hour = int((assignment.minute_timestamp % 86400) / 3600)
            is_nighttime = (hour >= 18 or hour < 6)
            
            # Base threshold
            base_threshold_ms = 5.0
            
            # Adjust for nighttime (more ionospheric variability)
            if is_nighttime:
                threshold_ms = base_threshold_ms * 1.5  # 7.5 ms
            else:
                threshold_ms = base_threshold_ms
            
            # Adjust for frequency (lower frequencies more variable)
            if assignment.frequency_mhz <= 5.0:
                threshold_ms *= 1.2  # 6.0 ms (day) or 9.0 ms (night)
            
            assignment.cross_validation_error_ms = float(max_error)
            assignment.cross_validation_passed = max_error < threshold_ms
            assignment.cross_validation_threshold_ms = float(threshold_ms)  # Store for logging
            
            if not assignment.cross_validation_passed:
                logger.warning(f"Cross-validation failed: max error {max_error:.2f}ms > threshold {threshold_ms:.1f}ms "
                             f"between {[s for s, _ in emission_times]} "
                             f"(nighttime={is_nighttime}, freq={assignment.frequency_mhz:.1f}MHz)")
        
        return assignment
    
    def update_calibration(
        self,
        station: StationID,
        offset_ms: float
    ):
        """
        Update calibration offset for a station.
        
        Args:
            station: Station to calibrate
            offset_ms: Offset to add to expected delay
        """
        old_offset = self.calibration_offsets.get(station, 0.0)
        self.calibration_offsets[station] = offset_ms
        logger.info(f"CorrelatorBank: {station.value} calibration updated: "
                   f"{old_offset:+.2f}ms → {offset_ms:+.2f}ms")
    
    def set_calibrated(self, calibrated: bool):
        """Set calibration state (affects search window width)."""
        self.calibrated = calibrated
        logger.info(f"CorrelatorBank: calibrated={calibrated}")


def create_correlator_bank(
    receiver_lat: float,
    receiver_lon: float,
    sample_rate: int = SAMPLE_RATE_FULL
) -> CorrelatorBank:
    """Factory function to create a correlator bank."""
    return CorrelatorBank(
        receiver_lat=receiver_lat,
        receiver_lon=receiver_lon,
        sample_rate=sample_rate
    )
