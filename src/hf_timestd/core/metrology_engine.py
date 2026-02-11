#!/usr/bin/env python3
"""
Metrology Engine: Pure DSP Time-of-Arrival Measurement
======================================================
Part of the "Metrology First, Physics Second" architecture.

Responsibility:
1. "The Instrument": Measure what happened (Timestamp, Frequency, Power).
2. "The Facts": Report Raw Time of Arrival (TOA).
3. "No Interpretation": Do NOT attempt to calculate d_clock or propagation delay.
   (Except for basic speed-of-light sanity checks).

Inputs:
- Raw IQ buffer (complex64)
- System Time
- RTP Timestamp

Outputs:
- List[L1MetrologyMeasurement]
"""

import numpy as np
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Dict, List, Tuple, Any
import threading
import json
import math

# Imports
from hf_timestd.models import (
    L1MetrologyMeasurement,
    QualityFlag,
    StationID
)
from hf_timestd.core.wwvh_discrimination import WWVHDiscriminator
from hf_timestd.core.tone_detector import MultiStationToneDetector
from hf_timestd.core.arrival_pattern_matrix import ArrivalPatternMatrix
from hf_timestd.core.tick_matched_filter import TickMatchedFilter, StationType as TickStationType
from hf_timestd.core.fusion_timing_state import FusionTimingState, LockTier
from hf_timestd.core.bootstrap_state import BootstrapStateWriter
from hf_timestd.core.timing_consistency_validator import TimingConsistencyValidator
# We keep discriminators as they are signal analysis, not physics modeling.

logger = logging.getLogger(__name__)

# Constants (Same as Phase 2)
EXPECTED_DTYPE = np.complex64
SAMPLE_RATE_FULL = 24000
MAX_EXPECTED_AMPLITUDE = 1.0
AMPLITUDE_WARNING_THRESHOLD = 10.0
SPEED_OF_LIGHT_KM_MS = 299.792458

class MetrologyEngine:
    """
    Metrology Engine: Pure DSP processing for Time-of-Arrival.
    Orchestrates Tone Detection and Channel Characterization.
    
    Two operating modes:
    - RTP Mode: Timing is authoritative (GPSDO + GPS+PPS). We KNOW when second 0 is.
                No searching needed - directly measure signals at known times.
    - Fusion Mode: Timing from NTP (uncertain). Bootstrap to find UTC offset first,
                   then operate like RTP mode.
    """
    
    def __init__(
        self,
        raw_buffer_dir: Path,
        output_dir: Path,
        channel_name: str,
        frequency_hz: float,
        receiver_grid: str,
        sample_rate: int = SAMPLE_RATE_FULL,
        precise_lat: Optional[float] = None,
        precise_lon: Optional[float] = None,
        is_rtp_authority: bool = True  # Default to RTP mode
    ):
        self.raw_buffer_dir = Path(raw_buffer_dir)
        self.output_dir = Path(output_dir)
        self.channel_name = channel_name
        self.frequency_hz = frequency_hz
        self.frequency_mhz = frequency_hz / 1e6
        self.receiver_grid = receiver_grid
        self.sample_rate = sample_rate
        self.precise_lat = precise_lat
        self.precise_lon = precise_lon
        self.is_rtp_authority = is_rtp_authority
        self.is_chu_channel = 'CHU' in channel_name.upper()
        
        # Initialize sub-components
        self._init_components()
        
        # Initialize Arrival Pattern Matrix for physics-based validation
        self._init_arrival_matrix()
        
        # Initialize Timing Consistency Validator for multi-constraint validation
        self._init_timing_validator()
        
        # State
        self._lock = threading.Lock()
        self.minutes_processed = 0
        
        # Calibration state (Learned RTP offsets, etc.)
        self.bpm_calibration = {
            'calibrated': False,
            'last_calibration_minute': None,
            'path_gain_db': None,
            'delay_offset_ms': None
        }
        self._load_calibration()
        
        # Fusion mode timing state (only used when is_rtp_authority=False)
        # This replaces the separate BootstrapService
        self.fusion_state: Optional[FusionTimingState] = None
        self._bootstrap_state_writer: Optional[BootstrapStateWriter] = None
        if not self.is_rtp_authority:
            self.fusion_state = FusionTimingState(sample_rate=self.sample_rate)
            self._bootstrap_state_writer = BootstrapStateWriter()
            logger.info(f"{channel_name}: Fusion mode - timing lock required before narrow search")
        
        logger.info(
            f"MetrologyEngine initialized for {channel_name} "
            f"({self.frequency_mhz} MHz), mode={'RTP' if is_rtp_authority else 'FUSION'}")

    def _init_components(self):
        """Initialize discriminators and detectors."""
        try:
            # 1. Tone Detector
            self.tone_detector = MultiStationToneDetector(
                channel_name=self.channel_name,
                sample_rate=self.sample_rate
            )
            
            # 2. WWV/WWVH Discriminator (includes BCD and Doppler)
            self.discriminator = WWVHDiscriminator(
                channel_name=self.channel_name,
                receiver_grid=self.receiver_grid,
                sample_rate=self.sample_rate
            )
            self.discriminator.frequency_mhz = self.frequency_mhz
            
            # 3. BPM Discriminator
            bpm_active_hours = set(range(24))
            if abs(self.frequency_mhz - 2.5) < 0.1:
                bpm_active_hours = {0} | set(range(8, 24))
            elif abs(self.frequency_mhz - 15.0) < 0.1:
                bpm_active_hours = set(range(1, 9))

            from hf_timestd.core.bpm_discriminator import BPMDiscriminator
            self.bpm_discriminator = BPMDiscriminator(
                receiver_lat=self.precise_lat,
                receiver_lon=self.precise_lon,
                channel_name=self.channel_name,
                active_hours=bpm_active_hours
            )

            # 4. Multi-Station Detector (Used for cross-freq guidance logic)
            # Note: We are using it for DSP purposes (signal presence), not physics solving.
            from hf_timestd.core.multi_station_detector import MultiStationDetector
            self.multi_station_detector = MultiStationDetector(
                receiver_lat=self.precise_lat,
                receiver_lon=self.precise_lon,
                sample_rate=self.sample_rate
            )
            
            # 5. Correlator Bank (Optional, if coords available)
            if self.precise_lat is not None and self.precise_lon is not None:
                from hf_timestd.core.correlator_bank import CorrelatorBank
                self.correlator_bank = CorrelatorBank(
                    receiver_lat=self.precise_lat,
                    receiver_lon=self.precise_lon,
                    sample_rate=self.sample_rate,
                    calibrated=False
                )
            else:
                self.correlator_bank = None
                
            # 6. CHU FSK Decoder
            if 'CHU' in self.channel_name.upper():
                from hf_timestd.core.chu_fsk_decoder import CHUFSKDecoder
                self.chu_fsk_decoder = CHUFSKDecoder(
                    sample_rate=self.sample_rate,
                    channel_name=self.channel_name
                )
            
            # 7. Tick Matched Filters for per-second timing (55+ estimates/minute)
            self.tick_filters: Dict[TickStationType, TickMatchedFilter] = {}
            self._init_tick_filters()
                
        except ImportError as e:
            logger.error(f"Failed to initialize Metrology components: {e}")
            raise

    def _init_arrival_matrix(self):
        """
        Initialize the Arrival Pattern Matrix for physics-based validation.
        
        The matrix provides expected arrival times based on:
        - Geography (receiver and station locations)
        - Frequency (affects ionospheric reflection height)
        - UTC time (affects ionospheric conditions via IRI-2020)
        
        This replaces historical calibration with physics-based predictions.
        """
        self.arrival_matrix = None
        
        if self.precise_lat is not None and self.precise_lon is not None:
            try:
                self.arrival_matrix = ArrivalPatternMatrix(
                    receiver_lat=self.precise_lat,
                    receiver_lon=self.precise_lon,
                    sample_rate=self.sample_rate,
                    enable_iri=True  # Use IRI-2020 if available
                )
                logger.info(f"ArrivalPatternMatrix initialized for {self.channel_name}")
            except Exception as e:
                logger.warning(f"Could not initialize ArrivalPatternMatrix: {e}")
                self.arrival_matrix = None
        else:
            logger.info(f"ArrivalPatternMatrix not initialized (no precise coordinates)")

    def _init_timing_validator(self):
        """
        Initialize the Timing Consistency Validator for multi-constraint validation.
        
        The validator exploits multiple timing constraints:
        - Intra-minute: arrival sequence, cross-station consistency, cross-frequency TEC
        - Inter-minute: sample interval stability, arrival time stability
        
        This provides additional validation beyond the physics-based arrival matrix.
        """
        self.timing_validator = None
        
        if self.precise_lat is not None and self.precise_lon is not None:
            try:
                self.timing_validator = TimingConsistencyValidator(
                    receiver_lat=self.precise_lat,
                    receiver_lon=self.precise_lon,
                    sample_rate=self.sample_rate,
                    history_minutes=60  # Track 1 hour of history
                )
                
                # Wire up TEC feedback: validator -> arrival matrix
                # When validator computes TEC, it feeds back to refine arrival predictions
                if self.arrival_matrix is not None:
                    self.timing_validator.set_tec_callback(self.arrival_matrix.update_measured_tec)
                    logger.info(f"TEC feedback enabled: validator -> arrival matrix")
                
                logger.info(f"TimingConsistencyValidator initialized for {self.channel_name}")
            except Exception as e:
                logger.warning(f"Could not initialize TimingConsistencyValidator: {e}")
                self.timing_validator = None
        else:
            logger.debug(f"TimingConsistencyValidator not initialized (no precise coordinates)")

    def _init_tick_filters(self):
        """
        Initialize per-second tick matched filters based on channel type.
        
        Creates filters for stations that can be received on this channel:
        - SHARED channels: WWV, WWVH, BPM
        - WWV-only channels (20, 25 MHz): WWV only
        - CHU channels: CHU only
        """
        channel_upper = self.channel_name.upper()
        
        if 'CHU' in channel_upper:
            # CHU-only channels (3.33, 7.85, 14.67 MHz)
            self.tick_filters[TickStationType.CHU] = TickMatchedFilter(
                station=TickStationType.CHU,
                sample_rate=self.sample_rate
            )
            logger.info(f"{self.channel_name}: CHU tick filter initialized (58 ticks/min)")
            
        elif 'WWV_20' in channel_upper or 'WWV_25' in channel_upper:
            # WWV-only channels (20, 25 MHz)
            self.tick_filters[TickStationType.WWV] = TickMatchedFilter(
                station=TickStationType.WWV,
                sample_rate=self.sample_rate
            )
            logger.info(f"{self.channel_name}: WWV tick filter initialized (57 ticks/min)")
            
        elif 'SHARED' in channel_upper:
            # Shared channels (2.5, 5, 10, 15 MHz) - WWV, WWVH, BPM all possible
            self.tick_filters[TickStationType.WWV] = TickMatchedFilter(
                station=TickStationType.WWV,
                sample_rate=self.sample_rate
            )
            self.tick_filters[TickStationType.WWVH] = TickMatchedFilter(
                station=TickStationType.WWVH,
                sample_rate=self.sample_rate
            )
            self.tick_filters[TickStationType.BPM] = TickMatchedFilter(
                station=TickStationType.BPM,
                sample_rate=self.sample_rate
            )
            logger.info(f"{self.channel_name}: WWV/WWVH/BPM tick filters initialized (57+57+59 ticks/min)")

    def _validate_input(self, iq_samples: np.ndarray) -> Tuple[np.ndarray, Dict[str, Any]]:
        """Validate and normalize input samples."""
        # Same logic as Phase2TemporalEngine
        metrics = {'amplitude_warning': False}
        if iq_samples.dtype != EXPECTED_DTYPE:
            iq_samples = iq_samples.astype(EXPECTED_DTYPE)
        
        max_amp = float(np.max(np.abs(iq_samples)))
        if max_amp > AMPLITUDE_WARNING_THRESHOLD:
            logger.warning(f"High amplitude: {max_amp}")
            metrics['amplitude_warning'] = True
            
        if max_amp > MAX_EXPECTED_AMPLITUDE:
            iq_samples = iq_samples / max_amp
            
        return iq_samples, metrics

    def _predict_geometric_delay(self, station: str, utc_time: Optional[float] = None) -> Tuple[float, float, float]:
        """
        Calculate expected propagation delay using ArrivalPatternMatrix.
        
        If ArrivalPatternMatrix is available, uses IRI-2020 ionospheric model.
        Otherwise falls back to simple light-speed calculation.
        
        Returns: (expected_delay_ms, distance_km, uncertainty_ms)
        """
        # Try ArrivalPatternMatrix first (physics-based with IRI-2020)
        if self.arrival_matrix is not None:
            try:
                from datetime import datetime, timezone
                if utc_time is not None:
                    dt = datetime.fromtimestamp(utc_time, tz=timezone.utc)
                else:
                    dt = datetime.now(timezone.utc)
                
                arrival = self.arrival_matrix.get_expected_arrivals(dt).get_arrival(
                    station, self.frequency_mhz
                )
                if arrival is not None:
                    return (
                        arrival.expected_delay_ms,
                        arrival.great_circle_km,
                        arrival.uncertainty_3sigma_ms / 3.0  # Return 1-sigma
                    )
            except Exception as e:
                logger.debug(f"ArrivalPatternMatrix lookup failed: {e}")
        
        # Fallback to simple light-speed calculation
        from .wwv_constants import STATION_LOCATIONS
        STATIONS = {k: {'lat': v['lat'], 'lon': v['lon']} for k, v in STATION_LOCATIONS.items()}
        
        if station not in STATIONS or self.precise_lat is None or self.precise_lon is None:
            return 0.0, 0.0, 500.0  # Blind fallback
            
        st = STATIONS[station]
        
        # Haversine
        R = 6371.0
        dlat = math.radians(st['lat'] - self.precise_lat)
        dlon = math.radians(st['lon'] - self.precise_lon)
        a = math.sin(dlat/2)**2 + math.cos(math.radians(self.precise_lat)) * \
            math.cos(math.radians(st['lat'])) * math.sin(dlon/2)**2
        c = 2 * math.asin(math.sqrt(a))
        dist_km = R * c
        
        light_time_ms = dist_km / SPEED_OF_LIGHT_KM_MS
        
        # Simple ionospheric overhead estimate (~10-20% longer than light time)
        expected_delay_ms = light_time_ms * 1.15
        
        return expected_delay_ms, dist_km, 15.0  # 15ms 1-sigma uncertainty

    @staticmethod
    def _get_tone_duration(station_name: str, sec_in_minute: int, minute_in_hour: int = 0) -> float:
        """
        Return the correct tone duration (seconds) for a given station and second.
        
        This ensures the matched filter template matches the actual signal duration,
        maximizing processing gain.  Key durations:
        
        WWV/WWVH:
            Second 0:  0.800s  (minute marker — 160× more energy than a tick)
            Others:    0.005s  (5ms per-second tick)
            
        CHU:
            Second 0:  0.500s  (minute marker, 1.0s at top of hour)
            Seconds 1-28, 30, 40-49: 0.300s  (regular 300ms tones)
            Seconds 31-39: 0.010s  (FSK seconds — short ticks only)
            Seconds 50-59: 0.010s  (voice seconds — short ticks only)
            
        BPM:
            Second 0:  0.300s  (minute marker)
            UTC minutes: 0.010s  (10ms ticks)
            UT1 minutes (25-29, 55-59): 0.100s  (100ms ticks)
        """
        if station_name in ('WWV', 'WWVH'):
            if sec_in_minute == 0:
                return 0.800  # Minute marker
            elif sec_in_minute in (29, 59):
                return 0.0    # Silent — no tick on seconds 29 and 59
            else:
                return 0.005  # 5ms per-second tick
        
        elif station_name == 'CHU':
            if sec_in_minute == 0:
                return 0.500  # Minute marker (1.0s at top of hour, but 0.5 is safe)
            elif sec_in_minute in range(31, 40):
                return 0.010  # FSK seconds
            elif sec_in_minute in range(50, 60):
                return 0.010  # Voice seconds
            else:
                return 0.300  # Regular 300ms tones
        
        elif station_name == 'BPM':
            if sec_in_minute == 0:
                return 0.300  # Minute marker
            # UT1 minutes: 25-29, 55-59
            elif minute_in_hour in (25, 26, 27, 28, 29, 55, 56, 57, 58, 59):
                return 0.100  # 100ms UT1 ticks
            else:
                return 0.010  # 10ms UTC ticks
        
        else:
            return 0.020  # Conservative fallback

    def _measure_tone_at_known_time(
        self,
        audio_signal: np.ndarray,
        expected_delay_ms: float,
        tone_freq_hz: float,
        tone_duration_sec: float,
        station_name: str
    ) -> Optional[Dict[str, Any]]:
        """
        Measure a tone at a KNOWN position in the buffer.
        
        expected_delay_ms is the expected arrival time in milliseconds from
        buffer sample 0. This can be anywhere in the buffer — the caller
        (process_minute) uses BufferTiming to compute the correct position.
        
        Returns arrival_ms relative to buffer sample 0 (not minute boundary).
        The caller converts to minute-boundary-relative using BufferTiming.
        
        ALWAYS returns a measurement dict (never None) so that rejected
        attempts are recorded for threshold calibration.  The 'detected'
        flag indicates whether the measurement passed all quality gates.
        'rejection_reason' explains why it was rejected (None if accepted).
        
        Args:
            audio_signal: AM-demodulated audio (magnitude - mean)
            expected_delay_ms: Expected arrival time from buffer start (ms)
            tone_freq_hz: Tone frequency (1000 or 1200 Hz)
            tone_duration_sec: Expected tone duration (0.8s WWV, 0.5s CHU)
            station_name: Station identifier for logging
            
        Returns:
            Dict with measurement results.  'detected' is True only if all
            quality gates passed.  Always contains at least station, frequency,
            expected_delay_ms, and whatever metrics could be computed.
        """
        # Base result returned on early exits when no correlation is possible
        base_result = {
            'station': station_name,
            'frequency_hz': tone_freq_hz,
            'expected_delay_ms': expected_delay_ms,
            'arrival_ms': expected_delay_ms,
            'timing_error_ms': 0.0,
            'snr_db': -99.0,
            'corr_snr_db': -99.0,
            'tone_power': 0.0,
            'peak_correlation': 0.0,
            'detected': False,
            'rejection_reason': None,
        }
        from scipy import signal as scipy_signal
        from scipy.fft import rfft, rfftfreq
        
        expected_sample = int(expected_delay_ms * self.sample_rate / 1000)
        
        # Measurement window must be large enough for the template + search margin.
        # mode='valid' correlation requires len(signal) > len(template), and we need
        # room on both sides for the search window.
        # For 5ms ticks: ±0.4s is plenty.
        # For 800ms minute markers: need at least 0.8s template + 0.5s search = ±0.9s.
        search_margin_sec = 0.5  # ±500ms for ionospheric variation
        window_sec = max(0.4, tone_duration_sec + search_margin_sec)
        window_samples = int(window_sec * self.sample_rate)
        start_sample = max(0, expected_sample - window_samples)
        end_sample = min(len(audio_signal), expected_sample + window_samples)
        
        if end_sample <= start_sample:
            base_result['rejection_reason'] = 'window_invalid'
            return base_result
            
        measurement_region = audio_signal[start_sample:end_sample]
        
        # Step 1: Full-duration matched filter for DETECTION
        # Do correlation FIRST, then measure tone SNR at the peak location
        # Use full tone duration (800ms for WWV/WWVH, 500ms for CHU, 100ms for BPM)
        # This provides excellent discrimination:
        # - 800ms template has ~160x more energy than 5ms tick → much stronger correlation
        # - Noise spikes have low correlation with long sinusoidal template
        # - Per-second ticks (5ms) produce weak correlation peaks
        
        n_template = int(tone_duration_sec * self.sample_rate)
        t = np.arange(n_template) / self.sample_rate
        
        # Quadrature templates for phase-invariant detection
        window = scipy_signal.windows.tukey(n_template, alpha=0.1)
        template_sin = np.sin(2 * np.pi * tone_freq_hz * t) * window
        template_cos = np.cos(2 * np.pi * tone_freq_hz * t) * window
        
        # Normalize to unit energy
        template_sin /= np.linalg.norm(template_sin)
        template_cos /= np.linalg.norm(template_cos)
        
        # Correlate with full-duration template
        corr_sin = scipy_signal.correlate(measurement_region, template_sin, mode='valid')
        corr_cos = scipy_signal.correlate(measurement_region, template_cos, mode='valid')
        correlation = np.sqrt(corr_sin**2 + corr_cos**2)
        
        if len(correlation) == 0:
            base_result['rejection_reason'] = 'correlation_empty'
            return base_result
        
        # Search window scaled by template duration.
        # Evidence from 20K+ measurements (2026-02-11) showed that a fixed ±500ms
        # window with 5ms tick templates causes the matched filter to lock onto
        # adjacent-second ticks or sidelobes at -185ms, rejecting 89% of attempts.
        # Short templates have poor temporal discrimination — constrain the search.
        # Long templates (800ms minute marker) need wider windows for ionospheric
        # variation and buffer alignment uncertainty.
        #   5ms tick:   ±50ms  (ionospheric variation ~30ms, no adjacent-tick ambiguity)
        #   100ms tone: ±150ms
        #   800ms marker: ±500ms
        SEARCH_WINDOW_MS = max(50.0, min(500.0, tone_duration_sec * 625))
        
        # expected_corr_idx is relative to measurement_region (which starts at start_sample)
        expected_corr_idx = expected_sample - start_sample
        window_samples = int(SEARCH_WINDOW_MS * self.sample_rate / 1000)
        
        search_start = max(0, expected_corr_idx - window_samples)
        search_end = min(len(correlation), expected_corr_idx + window_samples)
        
        if search_end <= search_start:
            logger.debug(f"{station_name}: Search window invalid - search_start={search_start}, search_end={search_end}, corr_len={len(correlation)}")
            base_result['rejection_reason'] = 'search_window_invalid'
            return base_result
        
        # Find peak within constrained window
        search_region = correlation[search_start:search_end]
        local_peak_idx = np.argmax(search_region)
        peak_idx = search_start + local_peak_idx
        peak_val = correlation[peak_idx]
        
        # VALIDATION: Reject if peak is at edge of search window (likely noise/flat correlation)
        # A real tone should produce a clear peak away from the edges
        edge_margin = min(50, len(search_region) // 10)  # At least 50 samples or 10% from edge
        if local_peak_idx < edge_margin or local_peak_idx > len(search_region) - edge_margin:
            # Check if correlation is essentially flat (noise)
            corr_range = np.max(search_region) - np.min(search_region)
            corr_mean = np.mean(search_region)
            if corr_mean > 0 and corr_range / corr_mean < 0.5:  # Less than 50% variation = flat
                logger.debug(f"{station_name}: Correlation flat/noisy - peak at edge "
                            f"(local_peak={local_peak_idx}, range/mean={corr_range/corr_mean:.2f})")
                base_result['rejection_reason'] = 'correlation_flat'
                base_result['peak_correlation'] = float(peak_val)
                base_result['corr_snr_db'] = 0.0
                return base_result
        
        # Step 3: VALIDATE that this is a full-duration tone, not a tick or noise
        # The full-duration matched filter should produce a strong peak for the minute marker
        # but a weak peak for 5ms ticks (which have ~1/160th the energy for 800ms template)
        #
        # Estimate noise floor from correlation values away from the peak
        noise_region = np.concatenate([
            correlation[:max(0, peak_idx - 100)],
            correlation[min(len(correlation), peak_idx + 100):]
        ])
        
        if len(noise_region) > 10:
            noise_median = np.median(noise_region)
            noise_mad = np.median(np.abs(noise_region - noise_median))
            noise_threshold = noise_median + 5 * 1.4826 * noise_mad  # 5-sigma threshold
        else:
            # Fallback when not enough noise samples
            noise_median = np.mean(correlation) if len(correlation) > 0 else 1.0
            noise_threshold = peak_val * 0.5
        
        # Calculate correlation SNR
        if noise_median > 0:
            corr_snr_db = 20 * np.log10(peak_val / noise_median)
        else:
            corr_snr_db = 0.0
        
        # Reject if correlation SNR is too low.
        # Scale threshold by template duration so that all stations have equivalent
        # sensitivity at the physical signal level.  A longer template provides more
        # processing gain (≈10·log10(duration/ref) dB), so a shorter template produces
        # lower correlation SNR for the same signal and needs a lower threshold.
        # Reference: 100 ms template → 8 dB threshold.
        REFERENCE_DURATION_SEC = 0.1
        BASE_CORR_SNR_DB = 8.0
        duration_gain_db = 10 * np.log10(tone_duration_sec / REFERENCE_DURATION_SEC) if tone_duration_sec > 0 else 0.0
        MIN_CORR_SNR_DB = max(1.0, BASE_CORR_SNR_DB + duration_gain_db)
        if corr_snr_db < MIN_CORR_SNR_DB:
            logger.info(f"{station_name}: Correlation too weak "
                        f"(corr_SNR={corr_snr_db:.1f}dB < {MIN_CORR_SNR_DB:.1f}dB, expected={expected_delay_ms:.1f}ms, "
                        f"peak_idx={peak_idx}, peak={peak_val:.4f}, noise={noise_median:.4f})")
            # Still compute arrival so the rejection is a complete record
            arrival_sample_rej = start_sample + peak_idx
            raw_arrival_ms_rej = arrival_sample_rej * 1000 / self.sample_rate
            base_result['rejection_reason'] = 'corr_snr_low'
            base_result['corr_snr_db'] = float(corr_snr_db)
            base_result['snr_db'] = float(corr_snr_db)
            base_result['peak_correlation'] = float(peak_val)
            base_result['arrival_ms'] = float(raw_arrival_ms_rej)
            base_result['timing_error_ms'] = float(raw_arrival_ms_rej - expected_delay_ms)
            base_result['corr_snr_threshold_db'] = float(MIN_CORR_SNR_DB)
            return base_result
        
        # Cross-frequency discrimination gate (WWV 1000Hz vs WWVH 1200Hz).
        # A 5ms template has 33% cross-response between 1000↔1200 Hz, so a strong
        # WWV tick produces a correlation peak on the WWVH template (and vice versa).
        # Fix: correlate the same region at the competing frequency and reject if
        # the claimed frequency doesn't dominate.
        CROSS_FREQ_PAIRS = {1000: 1200, 1200: 1000}  # WWV↔WWVH
        cross_freq = CROSS_FREQ_PAIRS.get(int(tone_freq_hz))
        if cross_freq is not None:
            # Build cross-frequency template (same duration, different freq)
            cross_sin = np.sin(2 * np.pi * cross_freq * t) * window
            cross_cos = np.cos(2 * np.pi * cross_freq * t) * window
            cross_sin /= np.linalg.norm(cross_sin)
            cross_cos /= np.linalg.norm(cross_cos)
            
            # Correlate at the same peak location
            cross_corr_sin = scipy_signal.correlate(measurement_region, cross_sin, mode='valid')
            cross_corr_cos = scipy_signal.correlate(measurement_region, cross_cos, mode='valid')
            cross_env = np.sqrt(cross_corr_sin**2 + cross_corr_cos**2)
            
            # Compare at the same peak index
            if peak_idx < len(cross_env):
                cross_peak = cross_env[peak_idx]
                if cross_peak > 0:
                    freq_advantage_db = 20 * np.log10(peak_val / cross_peak)
                else:
                    freq_advantage_db = 40.0
                
                # Require claimed frequency to be at least 3 dB stronger than cross-freq.
                # Clean single-frequency signal: ~10 dB advantage.
                # Cross-talk from other station: ~0 dB or negative.
                MIN_FREQ_ADVANTAGE_DB = 3.0
                if freq_advantage_db < MIN_FREQ_ADVANTAGE_DB:
                    arrival_sample_rej = start_sample + peak_idx
                    raw_arrival_ms_rej = arrival_sample_rej * 1000 / self.sample_rate
                    logger.debug(f"{station_name} @ {tone_freq_hz}Hz: REJECTED cross-talk "
                                f"(advantage={freq_advantage_db:+.1f}dB < {MIN_FREQ_ADVANTAGE_DB}dB, "
                                f"peak={peak_val:.4f}, cross={cross_peak:.4f})")
                    return {
                        'station': station_name,
                        'frequency_hz': tone_freq_hz,
                        'arrival_ms': float(raw_arrival_ms_rej),
                        'expected_delay_ms': expected_delay_ms,
                        'timing_error_ms': float(raw_arrival_ms_rej - expected_delay_ms),
                        'snr_db': float(corr_snr_db),
                        'corr_snr_db': float(corr_snr_db),
                        'tone_power': 0.0,
                        'peak_correlation': float(peak_val),
                        'detected': False,
                        'rejection_reason': 'cross_freq',
                    }
        
        # Step 2: Measure tone SNR at the DETECTED peak location (not expected location)
        # This handles buffer alignment issues where tone arrives later than expected
        tone_start = max(0, peak_idx)
        tone_end = min(len(measurement_region), tone_start + int(tone_duration_sec * self.sample_rate))
        
        if tone_end - tone_start >= int(0.1 * self.sample_rate):
            tone_segment = measurement_region[tone_start:tone_end]
            windowed = tone_segment * scipy_signal.windows.hann(len(tone_segment))
            fft_result = rfft(windowed)
            freqs = rfftfreq(len(windowed), 1/self.sample_rate)
            
            freq_idx = np.argmin(np.abs(freqs - tone_freq_hz))
            tone_power = np.abs(fft_result[freq_idx])**2
            
            noise_bins = np.concatenate([
                np.arange(max(0, freq_idx - 50), max(0, freq_idx - 10)),
                np.arange(min(len(fft_result), freq_idx + 10), min(len(fft_result), freq_idx + 50))
            ])
            if len(noise_bins) > 5:
                noise_power = np.mean(np.abs(fft_result[noise_bins.astype(int)])**2)
            else:
                noise_power = np.mean(np.abs(fft_result)**2)
            
            tone_snr_db = 10 * np.log10(tone_power / noise_power) if noise_power > 0 else 0.0
        else:
            tone_snr_db = corr_snr_db  # Fallback to correlation SNR
            tone_power = peak_val
        
        # Sub-sample interpolation
        sub_sample_offset = 0.0
        if 0 < peak_idx < len(correlation) - 1:
            y_m1 = correlation[peak_idx - 1]
            y_0 = correlation[peak_idx]
            y_p1 = correlation[peak_idx + 1]
            denom = y_m1 - 2*y_0 + y_p1
            if abs(denom) > 1e-10:
                sub_sample_offset = 0.5 * (y_m1 - y_p1) / denom
                sub_sample_offset = max(-0.5, min(0.5, sub_sample_offset))
        
        precise_peak_idx = peak_idx + sub_sample_offset
        
        # Convert to arrival time (ms from minute boundary)
        # For mode='valid', peak_idx=0 means template starts at sample 0 of measurement_region
        # The tone ONSET is at the start of the template alignment
        arrival_sample = start_sample + precise_peak_idx
        raw_arrival_ms = arrival_sample * 1000 / self.sample_rate
        
        # Timing is measured from RTP timestamp (sample 0 = minute boundary)
        # Timing error = measured_arrival - expected_propagation_delay
        timing_error_ms = raw_arrival_ms - expected_delay_ms
        
        # PROPAGATION BOUNDS VALIDATION (2026-02-05, updated 2026-02-09)
        # Validate that the measured arrival time is within tolerance of expected.
        # expected_delay_ms already includes tx_offset (e.g., 1000ms for CHU second 1).
        # RTP timestamps are authoritative (no wall-clock calibration bias).
        # Allow ±100ms for ionospheric variation (~30ms typical) plus margin.
        ARRIVAL_TOLERANCE_MS = 100.0
        
        if abs(timing_error_ms) > ARRIVAL_TOLERANCE_MS:
            logger.info(f"{station_name} @ {tone_freq_hz}Hz: REJECTED - arrival={raw_arrival_ms:.2f}ms "
                       f"error={timing_error_ms:+.1f}ms exceeds ±{ARRIVAL_TOLERANCE_MS:.0f}ms "
                       f"(expected={expected_delay_ms:.1f}ms, corr_SNR={corr_snr_db:.1f}dB)")
            return {
                'station': station_name,
                'frequency_hz': tone_freq_hz,
                'arrival_ms': raw_arrival_ms,
                'expected_delay_ms': expected_delay_ms,
                'timing_error_ms': timing_error_ms,
                'snr_db': tone_snr_db,
                'corr_snr_db': float(corr_snr_db),
                'tone_power': tone_power,
                'peak_correlation': float(peak_val),
                'detected': False,
                'rejection_reason': 'arrival_tolerance',
            }
        
        # BPM-specific: Require higher SNR due to shorter template (more false positives)
        if station_name == 'BPM':
            MIN_BPM_SNR_DB = 12.0
            if tone_snr_db < MIN_BPM_SNR_DB:
                logger.info(f"{station_name} @ {tone_freq_hz}Hz: REJECTED - SNR={tone_snr_db:.1f}dB "
                           f"< {MIN_BPM_SNR_DB}dB minimum for BPM")
                return {
                    'station': station_name,
                    'frequency_hz': tone_freq_hz,
                    'arrival_ms': raw_arrival_ms,
                    'expected_delay_ms': expected_delay_ms,
                    'timing_error_ms': timing_error_ms,
                    'snr_db': tone_snr_db,
                    'corr_snr_db': float(corr_snr_db),
                    'tone_power': tone_power,
                    'peak_correlation': float(peak_val),
                    'detected': False,
                    'rejection_reason': 'bpm_snr_low',
                }
        
        logger.info(f"{station_name} @ {tone_freq_hz}Hz: DETECTED arrival={raw_arrival_ms:.2f}ms "
                   f"(expected={expected_delay_ms:.1f}ms), error={timing_error_ms:+.2f}ms, "
                   f"corr_SNR={corr_snr_db:.1f}dB")
        
        return {
            'station': station_name,
            'frequency_hz': tone_freq_hz,
            'arrival_ms': raw_arrival_ms,  # Arrival relative to minute boundary
            'expected_delay_ms': expected_delay_ms,
            'timing_error_ms': timing_error_ms,
            'snr_db': tone_snr_db,
            'corr_snr_db': float(corr_snr_db),
            'tone_power': tone_power,
            'peak_correlation': float(peak_val),
            'detected': True,
            'rejection_reason': None,
        }

    def process_minute(
        self,
        iq_samples: np.ndarray,
        system_time: float,
        rtp_timestamp: int,
        buffer_timing=None
    ) -> List[L1MetrologyMeasurement]:
        """
        Process minute: Tone Detection + Channel Char -> L1 Measurements.
        
        Two modes of operation:
        - RTP Mode: Timing is authoritative. Measure signals at KNOWN times.
        - Fusion Mode: Use tone detector to search for signals (bootstrap or post-lock).
        
        Args:
            iq_samples: Raw IQ buffer (complex64)
            system_time: UTC timestamp (from metadata, may be inaccurate)
            rtp_timestamp: RTP counter at buffer start
            buffer_timing: BufferTiming object mapping samples to UTC.
                          If provided, overrides system_time for all timing.
        """
        minute_boundary = (int(system_time) // 60) * 60
        minute_number = int((system_time // 60) % 60)
        
        iq_samples, _ = self._validate_input(iq_samples)
        
        # Buffer mid-time for timestamp calculations
        if buffer_timing is not None:
            buffer_mid_time = buffer_timing.sample_to_utc(len(iq_samples) / 2)
        else:
            buffer_mid_time = system_time + len(iq_samples) / self.sample_rate / 2
        
        # === Step 0: Carrier SNR Check ===
        # Don't attempt detection if carrier is too weak.
        envelope = np.abs(iq_samples)
        carrier_amplitude = np.mean(envelope)
        mad = np.median(np.abs(envelope - np.median(envelope)))
        noise_std = 1.4826 * mad
        
        if noise_std > 0 and carrier_amplitude > 0:
            carrier_snr_db = 20 * np.log10(carrier_amplitude / noise_std)
        else:
            carrier_snr_db = -100.0
        
        # Log carrier SNR but don't gate on it — the matched filter can detect
        # signals well below the carrier noise floor.  That's its whole purpose.
        if carrier_snr_db < 2.0:
            logger.info(f"{self.channel_name}: Carrier SNR very low "
                       f"({carrier_snr_db:.1f}dB) — matched filter may still detect")
        
        # Demodulation:
        # CHU transmits USB with preserved carrier. The IQ baseband has a
        # strong DC carrier component plus the 1000 Hz tone as a sideband.
        # On CHU-only channels the carrier is unambiguous and high-power,
        # available for direct phase/Doppler analysis (see tick_matched_filter).
        # Re(IQ) recovers the audio (carrier + sidebands projected to real axis).
        # WWV/WWVH/BPM use conventional AM: |IQ| - DC recovers the envelope.
        if self.is_chu_channel:
            audio_signal = np.real(iq_samples).copy()
            audio_signal -= np.mean(audio_signal)
        else:
            audio_signal = envelope - np.mean(envelope)
        
        # Compute expected delays for all stations using physics model
        expected_delays_by_station = {}
        for station in ['WWV', 'WWVH', 'CHU', 'BPM']:
            expected_delay_ms, dist_km, uncertainty_ms = self._predict_geometric_delay(
                station, system_time
            )
            if expected_delay_ms > 0:
                expected_delays_by_station[station] = expected_delay_ms
        
        # === RTP MODE: Direct Measurement at Known Times ===
        # In RTP mode, timing is authoritative (GPSDO + GPS+PPS).
        # BufferTiming tells us the exact UTC time of every sample.
        # We find which seconds are in the buffer and measure tones there.
        if self.is_rtp_authority:
            logger.debug(f"{self.channel_name}: RTP mode - measuring at known times")
            
            # Define station templates based on channel type.
            # Tone frequency is per-station; duration is per-second (set in loop).
            channel_upper = self.channel_name.upper()
            if 'CHU' in channel_upper:
                station_tone_freqs = [('CHU', 1000)]
            elif 'WWV_20' in channel_upper or 'WWV_25' in channel_upper:
                station_tone_freqs = [('WWV', 1000)]
            else:
                # SHARED channels: try all stations
                station_tone_freqs = [
                    ('WWV', 1000),
                    ('WWVH', 1200),
                    ('BPM', 1000),
                ]
            
            rtp_measurements = []
            rtp_all_attempts = []
            
            if buffer_timing is not None and buffer_timing.source != 'metadata_fallback':
                # We know the UTC time of every sample.  Find which UTC
                # seconds fall within this buffer and measure tones there.
                n_samples = len(audio_signal)
                buf_start_utc = buffer_timing.sample0_utc
                buf_end_utc = buffer_timing.sample_to_utc(n_samples)
                
                for station_name, tone_freq in station_tone_freqs:
                    prop_delay_ms = expected_delays_by_station.get(station_name, 20.0)
                    prop_delay_sec = prop_delay_ms / 1000.0
                    
                    # Use the longest possible tone (minute marker) for margin calc
                    max_tone_duration = 1.0  # 1s covers all minute markers
                    margin_sec = max_tone_duration + 0.5
                    
                    # Find UTC seconds whose tone arrival falls in the buffer.
                    # A tick transmitted at UTC second T arrives at T + prop_delay.
                    # We need samples from T + prop_delay through T + prop_delay + margin.
                    first_utc_sec = int(buf_start_utc) - 1
                    last_utc_sec = int(buf_end_utc) + 1
                    
                    measurable = []
                    for utc_sec in range(first_utc_sec, last_utc_sec + 1):
                        sec_in_minute = utc_sec % 60
                        # Skip silent seconds
                        if station_name == 'CHU' and sec_in_minute == 29:
                            continue
                        if station_name in ('WWV', 'WWVH') and sec_in_minute in (29, 59):
                            continue
                        
                        tone_arrival_utc = utc_sec + prop_delay_sec
                        tone_end_utc = tone_arrival_utc + margin_sec
                        
                        onset_sample = buffer_timing.utc_to_sample(tone_arrival_utc)
                        end_sample = buffer_timing.utc_to_sample(tone_end_utc)
                        
                        if onset_sample >= 0 and end_sample < n_samples:
                            measurable.append((utc_sec, onset_sample))
                    
                    if not measurable:
                        logger.debug(f"{self.channel_name}: No {station_name} tones in buffer "
                                    f"(buf UTC {buf_start_utc:.1f}–{buf_end_utc:.1f})")
                        continue
                    
                    # Prioritize: minute marker (sec 0) first, then other seconds.
                    # Sort so second 0 comes first for maximum detection probability.
                    measurable.sort(key=lambda x: (x[0] % 60 != 0, x[0]))
                    
                    # Try up to 15 seconds per station (was 5)
                    for utc_sec, onset_sample in measurable[:15]:
                        sec_in_minute = utc_sec % 60
                        tone_duration = self._get_tone_duration(
                            station_name, sec_in_minute, minute_number
                        )
                        if tone_duration <= 0:
                            continue  # Silent second — no tone to detect
                        
                        expected_ms_from_buf_start = onset_sample * 1000 / self.sample_rate
                        
                        result = self._measure_tone_at_known_time(
                            audio_signal=audio_signal,
                            expected_delay_ms=expected_ms_from_buf_start,
                            tone_freq_hz=tone_freq,
                            tone_duration_sec=tone_duration,
                            station_name=station_name
                        )
                        
                        # Record every attempt for diagnostic summary
                        result['utc_second'] = utc_sec
                        result['tone_duration_sec'] = tone_duration
                        rtp_all_attempts.append(result)
                        
                        if result.get('detected'):
                            # arrival_ms is from buffer start.  Convert to UTC.
                            arrival_utc = buffer_timing.sample_to_utc(
                                result['arrival_ms'] * self.sample_rate / 1000
                            )
                            # Expected arrival UTC = utc_sec + prop_delay
                            expected_utc = utc_sec + prop_delay_sec
                            result['timing_error_ms'] = (arrival_utc - expected_utc) * 1000
                            result['arrival_utc'] = arrival_utc
                            rtp_measurements.append(result)
                
                # Per-minute diagnostic: what did we attempt, what passed, what failed and why?
                if rtp_all_attempts:
                    n_detected = sum(1 for a in rtp_all_attempts if a.get('detected'))
                    n_rejected = len(rtp_all_attempts) - n_detected
                    # Count rejection reasons
                    reasons = {}
                    rejected_snrs = []
                    for a in rtp_all_attempts:
                        reason = a.get('rejection_reason')
                        if reason:
                            reasons[reason] = reasons.get(reason, 0) + 1
                            if a.get('corr_snr_db', -99) > -99:
                                rejected_snrs.append(a['corr_snr_db'])
                    
                    reason_str = ', '.join(f"{r}={c}" for r, c in sorted(reasons.items()))
                    snr_str = ''
                    if rejected_snrs:
                        snr_str = f", rejected SNRs: {min(rejected_snrs):.1f}–{max(rejected_snrs):.1f}dB"
                    
                    logger.info(f"{self.channel_name}: RTP attempts={len(rtp_all_attempts)} "
                               f"detected={n_detected} rejected={n_rejected} "
                               f"[{reason_str}]{snr_str}")
                    
                    if rtp_measurements:
                        secs = [m['utc_second'] % 60 for m in rtp_measurements]
                        logger.info(f"{self.channel_name}: RTP detected at seconds {secs}")
            else:
                # No BufferTiming — fall back to legacy method.
                # Without BufferTiming we don't know which second we're at,
                # so use a conservative 20ms template as before.
                for station_name, tone_freq in station_tone_freqs:
                    prop_delay = expected_delays_by_station.get(station_name, 20.0)
                    result = self._measure_tone_at_known_time(
                        audio_signal=audio_signal,
                        expected_delay_ms=prop_delay,
                        tone_freq_hz=tone_freq,
                        tone_duration_sec=0.02,
                        station_name=station_name
                    )
                    if result and result.get('detected'):
                        rtp_measurements.append(result)
            
            if not rtp_measurements:
                logger.debug(f"{self.channel_name}: No signals detected at expected times")
                return []
            
            # Select best measurement per station (highest SNR) for timing use.
            # All measurements become detections for SNR reporting, but only the
            # best per station gets use_for_time_snap=True to avoid confusing
            # downstream fusion with redundant timing from the same station.
            best_per_station = {}
            for m in rtp_measurements:
                stn = m['station']
                if stn not in best_per_station or m['snr_db'] > best_per_station[stn]['snr_db']:
                    best_per_station[stn] = m
            
            best_keys = set()
            for m in best_per_station.values():
                best_keys.add((m['station'], m.get('utc_second', 0)))
            
            # Convert RTP measurements to ToneDetectionResult format for downstream
            from ..interfaces.data_models import ToneDetectionResult, StationType
            detections = []
            for m in rtp_measurements:
                station_type = StationType[m['station']] if m['station'] in StationType.__members__ else StationType.UNKNOWN
                is_best = (m['station'], m.get('utc_second', 0)) in best_keys
                
                if buffer_timing is not None and 'arrival_utc' in m:
                    arrival_utc = m['arrival_utc']
                    # sample_position_original for physics validator:
                    # the fractional-second part of the arrival, in samples.
                    # This represents the propagation delay from the UTC second.
                    frac_sec = arrival_utc - int(arrival_utc)
                    sample_pos = int(frac_sec * self.sample_rate)
                    timestamp_utc_val = arrival_utc
                else:
                    sample_pos = int(m['arrival_ms'] * self.sample_rate / 1000)
                    timestamp_utc_val = system_time + m['arrival_ms'] / 1000.0
                
                det = ToneDetectionResult(
                    station=station_type,
                    frequency_hz=m['frequency_hz'],
                    duration_sec=m.get('tone_duration_sec', 0.02),
                    timestamp_utc=timestamp_utc_val,
                    timing_error_ms=m['timing_error_ms'],
                    snr_db=m['snr_db'],
                    confidence=min(1.0, m['snr_db'] / 20.0),
                    use_for_time_snap=is_best,
                    correlation_peak=m.get('correlation_peak', 0.0),
                    noise_floor=0.0,
                    tone_power_db=m['snr_db'],
                    sample_position_original=sample_pos,
                    original_sample_rate=self.sample_rate
                )
                detections.append(det)
            
            n_best = len(best_per_station)
            station_names = [m['station'] for m in rtp_measurements]
            logger.info(f"{self.channel_name}: RTP mode measured {len(detections)} signal(s) "
                       f"({n_best} best for timing): {station_names}")
        
        else:
            # === FUSION MODE: Search for Signals ===
            # Timing is uncertain (NTP-based). Need to search for tones.
            logger.debug(f"{self.channel_name}: Fusion mode - searching for signals")
            
            # Use adaptive search window based on physics model
            max_uncertainty_ms = 15.0
            for station, delay in expected_delays_by_station.items():
                _, _, unc = self._predict_geometric_delay(station, system_time)
                max_uncertainty_ms = max(max_uncertainty_ms, unc)
            
            adaptive_window_ms = min(200.0, max(50.0, max_uncertainty_ms * 3))
            
            # Use FusionTimingState to determine search window
            if self.fusion_state is not None:
                adaptive_window_ms = self.fusion_state.get_search_window_ms()
            
            logger.info(f"{self.channel_name}: Fusion search: "
                       f"expected_delays={expected_delays_by_station}, window=±{adaptive_window_ms:.0f}ms, "
                       f"lock_tier={self.fusion_state.lock_tier.name if self.fusion_state else 'N/A'}")
            
            buffer_mid_time = system_time + len(iq_samples)/self.sample_rate/2
            
            detections = self.tone_detector.process_samples(
                timestamp=buffer_mid_time,
                samples=iq_samples,
                rtp_timestamp=rtp_timestamp,
                original_sample_rate=self.sample_rate,
                buffer_rtp_start=rtp_timestamp,
                search_window_ms=adaptive_window_ms,
                expected_delays_by_station=expected_delays_by_station
            )
            
            if not detections:
                logger.debug(f"{self.channel_name}: No detections for minute {minute_boundary}")
                return []
            
            station_names = [det.station.value for det in detections]
            logger.info(f"{self.channel_name}: Fusion detected {len(detections)} station(s): {station_names}")
             
        # === Step 2: Channel Characterization ===
        # We need this for Station ID and Metrics
        # Re-use Phase 2 logic style but inline or simplified?
        # Actually Phase 2 logic handles BCD, Doppler, etc.
        # We can instantiate a 'TimeSnapResult' dummy if we want to reuse existing methods,
        # or just call discriminators directly.
        # Calling discriminator methods directly is cleaner.
        
        # 2A. BCD (if applicable)
        bcd_metrics = {}
        if self.frequency_mhz in (2.5, 5.0, 10.0, 15.0):
             bcd_res = self.discriminator.detect_bcd_discrimination(
                 iq_samples, self.sample_rate, system_time, self.frequency_mhz
             )
             if bcd_res and bcd_res[0]:
                 bcd_metrics['wwv_amp'] = bcd_res[0]
                 bcd_metrics['wwvh_amp'] = bcd_res[1]
                 
        # 2B. Doppler
        doppler_metrics = {}
        doppler_info = self.discriminator.estimate_doppler_shift_from_ticks(
            iq_samples, self.sample_rate
        )
        if doppler_info:
            doppler_metrics = doppler_info
            
        # 2C. CHU FSK Time Code Decoding
        # FSK tones (2025/2225 Hz) are attenuated by the IQ decimation filter,
        # so decoding is done by separate USB sidecar channels (CHUFSKListener).
        # Results are read from shared JSON in /dev/shm/timestd/fsk_results/.
        chu_metrics = {}
        if self.is_chu_channel:
            chu_metrics = self._read_fsk_result(minute_boundary)
        
        # === Step 2D: Per-Second Tick Detection (55+ estimates/minute) ===
        tick_results = {}
        logger.info(f"{self.channel_name}: Running tick analysis for {len(self.tick_filters)} stations")
        for station_type, tick_filter in self.tick_filters.items():
            try:
                tick_analysis = tick_filter.process_minute(iq_samples, minute_number)
                logger.info(f"{self.channel_name}: {station_type.value} tick_analysis: "
                           f"valid_windows={tick_analysis.valid_windows if tick_analysis else 0}")
                if tick_analysis and tick_analysis.valid_windows > 0:
                    tick_results[station_type.value] = tick_analysis
                    
                    # Get expected propagation delay for this station
                    station_name = station_type.value
                    expected_delay_ms = expected_delays_by_station.get(station_name, 0.0)
                    
                    # NOTE: tick_analysis.mean_timing_offset_ms is a RELATIVE offset from
                    # the expected tick positions within each window, NOT an absolute ToA.
                    # It should be near zero if ticks are arriving at expected times.
                    # We do NOT subtract expected_delay_ms - that would double-count propagation.
                    timing_error_ms = tick_analysis.mean_timing_offset_ms
                    
                    # Validate: reject if timing offset is too large
                    # Large offset suggests wrong station or severe multipath
                    # Note: ~70ms systematic offset exists due to GPS_TIME/RTP_TIMESNAP latency
                    max_timing_error_ms = 100.0  # Allow up to 100ms timing offset
                    is_valid = abs(timing_error_ms) < max_timing_error_ms
                    
                    if is_valid:
                        logger.info(f"{self.channel_name}: {station_name} tick analysis - "
                                   f"{tick_analysis.valid_windows}/{tick_analysis.total_windows} windows, "
                                   f"raw_toa={tick_analysis.mean_timing_offset_ms:+.1f}ms, "
                                   f"expected={expected_delay_ms:.1f}ms, "
                                   f"timing_error={timing_error_ms:+.1f}ms, "
                                   f"std={tick_analysis.std_timing_offset_ms:.1f}ms, "
                                   f"drift={tick_analysis.drift_rate_ms_per_sec or 0:.3f}ms/s")
                    else:
                        # Remove invalid result (likely wrong station due to same frequency)
                        del tick_results[station_type.value]
                        logger.info(f"{self.channel_name}: {station_name} tick REJECTED - "
                                    f"timing_error={timing_error_ms:+.1f}ms exceeds ±{max_timing_error_ms}ms "
                                    f"(raw_toa={tick_analysis.mean_timing_offset_ms:+.1f}ms, "
                                    f"expected={expected_delay_ms:.1f}ms)")
            except Exception as e:
                logger.debug(f"{self.channel_name}: {station_type.value} tick detection failed: {e}")
                 
        # === Step 3: Package into L1MetrologyMeasurement ===
        # Validate each detection against the ArrivalPatternMatrix.
        # Only the best detection per station (use_for_time_snap=True) creates
        # an L1 timing measurement and feeds the fusion state.  All detections
        # contribute SNR data points to the HDF5 for dashboard plotting.
        results = []
        for det in detections:
            # Map station name to Enum
            try:
                station_id_enum = StationID[det.station.value]
            except KeyError:
                station_id_enum = StationID.UNKNOWN

            # Physics-based validation using ArrivalPatternMatrix
            geo_delay, dist_km, uncertainty_ms = self._predict_geometric_delay(
                det.station.value, system_time
            )
            
            # Validate detection against physics model
            physics_valid = True
            physics_confidence = 1.0
            validation_reason = "no_matrix"
            
            if self.arrival_matrix is not None:
                # Use the raw sample position for validation (not timing_error which is arrival - expected)
                # sample_position_original is the raw arrival sample from minute boundary
                detected_sample = det.sample_position_original
                
                is_valid, confidence, reason = self.arrival_matrix.validate_detection(
                    station=det.station.value,
                    frequency_mhz=self.frequency_mhz,
                    detected_sample=detected_sample,
                    snr_db=det.snr_db,
                    utc_time=datetime.fromtimestamp(system_time, tz=timezone.utc)
                )
                
                physics_valid = is_valid
                physics_confidence = confidence
                validation_reason = reason
                
                # REJECT detections that fail physics validation
                # A detection outside the physics window is likely a per-second tick,
                # not the minute marker. The arrival matrix provides the ground truth.
                if not is_valid:
                    detected_ms = detected_sample * 1000 / self.sample_rate
                    logger.info(f"{self.channel_name}: Physics REJECTED: "
                               f"{det.station.value} arrival={detected_ms:.1f}ms - {reason}")
                    continue  # Skip this detection entirely
                else:
                    detected_ms = detected_sample * 1000 / self.sample_rate
                    logger.debug(f"{self.channel_name}: Physics VALIDATED: "
                                f"{det.station.value} arrival={detected_ms:.1f}ms - {reason}")
            
            # Construct L1 measurement (only for validated detections)
            meas = L1MetrologyMeasurement(
                timestamp_utc=datetime.fromtimestamp(buffer_mid_time, tz=timezone.utc).isoformat(),
                minute_boundary_utc=minute_boundary,
                rtp_timestamp=rtp_timestamp,
                station_id=station_id_enum,
                frequency_mhz=self.frequency_mhz,
                
                raw_toa_ms=det.timing_error_ms,
                tone_detected=True,
                
                snr_db=det.snr_db,
                doppler_hz=doppler_metrics.get(f"{det.station.value.lower()}_doppler_hz"),
                
                identification_method="tone_frequency",
                identification_confidence=det.confidence * physics_confidence,
                
                distance_km=dist_km,
                light_travel_time_ms=geo_delay,
                
                quality_flag=QualityFlag.GOOD if (det.confidence > 0.5 and physics_valid) else QualityFlag.MARGINAL
            )
            results.append(meas)
            
            # Feed ONLY the best detection per station to FusionTimingState.
            # Multiple timing measurements from the same station would confuse
            # the Kalman filter with correlated noise.
            if self.fusion_state is not None and physics_valid and det.use_for_time_snap:
                lock_status = self.fusion_state.add_detection(
                    station=det.station.value,
                    timing_error_ms=det.timing_error_ms,
                    frequency_mhz=self.frequency_mhz,
                    snr_db=det.snr_db,
                    confidence=det.confidence * physics_confidence,
                    system_time=system_time
                )
                if lock_status:
                    logger.info(f"{self.channel_name}: {lock_status}")
                    self._write_bootstrap_state_on_lock()
            
        with self._lock:
            self.minutes_processed += 1
        
        # Store FSK data for caller to retrieve
        self._last_chu_fsk_data = chu_metrics if chu_metrics else None
        
        # Store tick analysis results for caller to retrieve
        self._last_tick_results = tick_results if tick_results else None
        
        # Store ALL measurement attempts (detected + rejected) for threshold calibration.
        # This is the evidence that keeps us honest: by recording what we reject and why,
        # we can later ask whether our thresholds are correctly calibrated.
        self._last_rtp_attempts = rtp_all_attempts if self.is_rtp_authority else None
        
        # === Step 4: Multi-Constraint Timing Validation ===
        # Validate detections using all known timing constraints:
        # - Arrival sequence (stations at different distances)
        # - Cross-station consistency (all transmit at UTC second 0)
        # - Sample interval stability (1,440,000 samples between minutes)
        # - Arrival time stability (consistent offsets across minutes)
        if self.timing_validator is not None and results:
            validation_detections = [
                {
                    'station': meas.station_id.value if hasattr(meas.station_id, 'value') else str(meas.station_id),
                    'frequency_mhz': meas.frequency_mhz,
                    'arrival_ms': meas.raw_toa_ms,
                    'snr_db': meas.snr_db
                }
                for meas in results
            ]
            
            validation_result = self.timing_validator.validate_minute(
                minute_boundary=minute_boundary,
                detections=validation_detections,
                rtp_timestamp=rtp_timestamp
            )
            
            # Log validation summary
            self.timing_validator.log_validation_summary(validation_result)
            
            # Update history for inter-minute tracking
            self.timing_validator.update_history(minute_boundary, validation_detections)
            
            # Store validation result for caller to retrieve
            self._last_validation_result = validation_result
            
            # Log stability metrics periodically (every 10 minutes)
            if self.minutes_processed % 10 == 0:
                stability = self.timing_validator.get_stability_metrics()
                if stability.n_minutes >= 5:
                    logger.info(f"{self.channel_name}: Stability metrics (n={stability.n_minutes}):")
                    for station, std in stability.arrival_std_ms.items():
                        mean = stability.arrival_mean_ms.get(station, 0)
                        logger.info(f"  {station}: arrival={mean:.1f}±{std:.1f}ms")
                    if stability.sample_interval_std > 0:
                        logger.info(f"  Sample interval: {stability.sample_interval_mean:.0f}±{stability.sample_interval_std:.1f}")
            
        return results

    def _write_bootstrap_state_on_lock(self):
        """Write bootstrap state file when FusionTimingState achieves lock.
        
        This unblocks the fusion service which waits for bootstrap_state.json
        before starting its main loop. Called on PROVISIONAL and REFINED lock
        transitions.
        """
        if self._bootstrap_state_writer is None or self.fusion_state is None:
            return
        
        if not self.fusion_state.is_locked:
            return
        
        try:
            offset_stats = self.fusion_state._compute_offset_stats()
            lock_tier = self.fusion_state.lock_tier.name
            d_clock_ms = offset_stats.get('median_ms', 0.0)
            uncertainty_ms = offset_stats.get('std_ms', 50.0)
            
            self._bootstrap_state_writer.write_locked(
                lock_tier=lock_tier,
                d_clock_ms=d_clock_ms,
                uncertainty_ms=uncertainty_ms,
                sample_rate=self.sample_rate
            )
            logger.info(
                f"{self.channel_name}: Bootstrap state written: {lock_tier}, "
                f"D_clock={d_clock_ms:+.1f}ms ± {uncertainty_ms:.1f}ms"
            )
        except Exception as e:
            logger.error(f"{self.channel_name}: Failed to write bootstrap state: {e}")

    def _read_fsk_result(self, minute_boundary: int) -> dict:
        """Read CHU FSK result from shared JSON (written by CHUFSKListener)."""
        from pathlib import Path
        fsk_path = Path('/dev/shm/timestd/fsk_results') / f'{self.channel_name}.json'
        try:
            if not fsk_path.exists():
                return {}
            with open(fsk_path, 'r') as f:
                data = json.load(f)
            # Only use if reasonably fresh (within 2 minutes)
            age = time.time() - data.get('written_at', 0)
            if age > 120:
                logger.debug(f"{self.channel_name}: FSK result stale ({age:.0f}s old)")
                return {}
            if not data.get('detected'):
                logger.debug(f"{self.channel_name}: FSK result: not detected")
                return {}
            chu_metrics = {
                'fsk_valid': True,
                'fsk_frames_decoded': data.get('frames_decoded', 0),
                'fsk_confidence': data.get('decode_confidence', 0.0),
            }
            if data.get('decoded_day') is not None:
                chu_metrics['decoded_day'] = data['decoded_day']
                chu_metrics['decoded_hour'] = data.get('decoded_hour')
                chu_metrics['decoded_minute'] = data.get('decoded_minute')
            if data.get('dut1_seconds') is not None:
                chu_metrics['dut1_seconds'] = data['dut1_seconds']
            if data.get('tai_utc') is not None:
                chu_metrics['tai_utc'] = data['tai_utc']
            if data.get('year') is not None:
                chu_metrics['year'] = data['year']
            if data.get('timing_offset_ms') is not None:
                chu_metrics['timing_offset_ms'] = data['timing_offset_ms']
            logger.info(f"{self.channel_name}: CHU FSK from USB sidecar - "
                       f"frames={data.get('frames_decoded', 0)}/9, "
                       f"DUT1={data.get('dut1_seconds')}s, "
                       f"TAI-UTC={data.get('tai_utc')}s")
            return chu_metrics
        except Exception as e:
            logger.debug(f"{self.channel_name}: Failed to read FSK result: {e}")
            return {}

    def _station_from_channel_name(self) -> str:
        """Helper to guess station from name."""
        if 'CHU' in self.channel_name.upper(): return 'CHU'
        if 'WWVH' in self.channel_name.upper(): return 'WWVH'
        if 'WWV' in self.channel_name.upper(): return 'WWV'
        return 'UNKNOWN'

    def _load_calibration(self):
        """Simple calibration loader for BPM."""
        try:
            cal_file = self.output_dir / "timing_calibration.json"
            if cal_file.exists():
                with open(cal_file, 'r') as f:
                    data = json.load(f)
                    if 'bpm' in data:
                        self.bpm_calibration.update(data['bpm'])
        except (OSError, IOError, json.JSONDecodeError) as e:
            logger.debug(f"Could not load calibration file: {e}")
            
    def _save_calibration(self):
        """Simple saver."""
        try:
            cal_file = self.output_dir / "timing_calibration.json"
            data = {'bpm': self.bpm_calibration}
            with open(cal_file, 'w') as f:
                json.dump(data, f)
        except (OSError, IOError) as e:
            logger.debug(f"Could not save calibration file: {e}")
