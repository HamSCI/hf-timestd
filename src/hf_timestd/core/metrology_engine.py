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
        precise_lon: Optional[float] = None
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
        
        # Initialize sub-components
        self._init_components()
        
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
        
        logger.info(
            f"MetrologyEngine initialized for {channel_name} "
            f"({self.frequency_mhz} MHz)"
        )

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
                
        except ImportError as e:
            logger.error(f"Failed to initialize Metrology components: {e}")
            raise

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

    def _predict_geometric_delay(self, station: str) -> Tuple[float, float, float]:
        """
        Calculate expected light-speed travel time.
        Returns: (min_delay_ms, distance_km, uncertainty_ms)
        """
        # Use centralized station coordinates from wwv_constants (single source of truth)
        from .wwv_constants import STATION_LOCATIONS
        STATIONS = {k: {'lat': v['lat'], 'lon': v['lon']} for k, v in STATION_LOCATIONS.items()}
        
        if station not in STATIONS or self.precise_lat is None or self.precise_lon is None:
            return 0.0, 0.0, 500.0 # Blind fallback
            
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
        
        # For HF, propagation is always longer than light time (reflection).
        # Expected delay is roughly light_time + 10-20%.
        # But for search window centering, light_time is a firm lower bound.
        # Let's center the window slightly after light_time.
        # 1-hop F-layer adds ~1-2ms extra path?
        # Actually it's significant. 1500km path -> 5ms light time.
        # Skywave is hypotenuse. 
        # But this is just for search window centering. 
        # A simple model: expected = light_time + 2.0ms?
        # Let's say expected = light_time.
        # Uncertainty is large because we don't know the hop.
        
        return light_time_ms, dist_km, 20.0 # +/- 20ms uncertainty around line-of-sight is reasonable start

    def process_minute(
        self,
        iq_samples: np.ndarray,
        system_time: float,
        rtp_timestamp: int
    ) -> List[L1MetrologyMeasurement]:
        """
        Process minute: Tone Detection + Channel Char -> L1 Measurements.
        """
        minute_boundary = (int(system_time) // 60) * 60
        minute_number = int((system_time // 60) % 60)
        
        iq_samples, _ = self._validate_input(iq_samples)
        
        # === Step 1: Tone Detection ===
        # Use simpler window logic than Phase 2
        adaptive_window_ms = 500.0
        expected_offset_ms = 0.0
        
        # Simple Geometric Prior (if available) to aid detection
        # Determine likely station
        likely_station = self._station_from_channel_name()
        if likely_station != 'UNKNOWN':
            # Use geometric delay as search center
            geom_delay, dist, unc = self._predict_geometric_delay(likely_station)
            if geom_delay > 0:
                expected_offset_ms = geom_delay + 2.0 # Bias slightly for skywave
                adaptive_window_ms = 50.0 # Narrower window if we know geometry? 
                # Actually, during bootstrap, keep it wide? 
                # Let's stick to 200ms around geometric delay.
                adaptive_window_ms = 200.0
        
        # Run detection
        # Note: We replicate _step1_tone_detection logic simplified
        buffer_mid_time = system_time + len(iq_samples)/self.sample_rate/2
        
        detections = self.tone_detector.process_samples(
            timestamp=buffer_mid_time,
            samples=iq_samples,
            rtp_timestamp=rtp_timestamp,
            original_sample_rate=self.sample_rate,
            buffer_rtp_start=rtp_timestamp,
            search_window_ms=adaptive_window_ms,
            expected_offset_ms=expected_offset_ms
        )
        
        if not detections:
             logger.debug(f"{self.channel_name}: No detections for minute {minute_boundary}")
             return []
        
        # Log detected stations for multi-station debugging
        station_names = [det.station.value for det in detections]
        logger.info(f"{self.channel_name}: Detected {len(detections)} station(s): {station_names}")
             
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
        chu_metrics = {}
        if hasattr(self, 'chu_fsk_decoder'):
            fsk_res = self.chu_fsk_decoder.decode_minute(iq_samples, system_time)
            if fsk_res.detected:
                chu_metrics['fsk_valid'] = True
                chu_metrics['fsk_frames_decoded'] = fsk_res.frames_decoded
                chu_metrics['fsk_confidence'] = fsk_res.decode_confidence
                
                # Decoded time verification
                if fsk_res.decoded_day is not None:
                    chu_metrics['decoded_day'] = fsk_res.decoded_day
                    chu_metrics['decoded_hour'] = fsk_res.decoded_hour
                    chu_metrics['decoded_minute'] = fsk_res.decoded_minute
                
                # Auxiliary data from Frame B
                if fsk_res.dut1_seconds is not None:
                    chu_metrics['dut1_seconds'] = fsk_res.dut1_seconds
                if fsk_res.tai_utc is not None:
                    chu_metrics['tai_utc'] = fsk_res.tai_utc
                if fsk_res.year is not None:
                    chu_metrics['year'] = fsk_res.year
                
                # Timing precision
                if fsk_res.timing_offset_ms is not None:
                    chu_metrics['timing_offset_ms'] = fsk_res.timing_offset_ms
                
                logger.info(f"{self.channel_name}: CHU FSK decoded - "
                           f"frames={fsk_res.frames_decoded}/9, "
                           f"DUT1={fsk_res.dut1_seconds}s, TAI-UTC={fsk_res.tai_utc}s")
                 
        # === Step 3: Package into L1MetrologyMeasurement ===
        results = []
        for det in detections:
            # Map station name to Enum
            try:
                station_id_enum = StationID[det.station.value]
            except KeyError:
                station_id_enum = StationID.UNKNOWN

            # Geometric check
            geo_delay, dist_km, _ = self._predict_geometric_delay(det.station.value)
            
            # Construct L1
            meas = L1MetrologyMeasurement(
                timestamp_utc=datetime.fromtimestamp(buffer_mid_time, tz=timezone.utc).isoformat(),
                minute_boundary_utc=minute_boundary,
                rtp_timestamp=rtp_timestamp, # Base RTP
                station_id=station_id_enum,
                frequency_mhz=self.frequency_mhz,
                
                raw_toa_ms=det.timing_error_ms, # "timing_error" is effectively TOA relative to second boundary?
                # In Phase 2: "timing_error_ms = offset from expected second boundary"
                # If emission is at 0, then timing_error_ms = TOA.
                tone_detected=True,
                
                snr_db=det.snr_db,
                doppler_hz=doppler_metrics.get(f"{det.station.value.lower()}_doppler_hz"),
                
                identification_method="tone_frequency",
                identification_confidence=det.confidence,
                
                distance_km=dist_km,
                light_travel_time_ms=geo_delay,
                
                quality_flag=QualityFlag.GOOD if det.confidence > 0.5 else QualityFlag.MARGINAL
            )
            results.append(meas)
            
        with self._lock:
            self.minutes_processed += 1
            
        return results

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
