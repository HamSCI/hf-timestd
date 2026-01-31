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
        
        # Initialize Arrival Pattern Matrix for physics-based validation
        self._init_arrival_matrix()
        
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
        
        # Use ArrivalPatternMatrix for physics-based search windows
        likely_station = self._station_from_channel_name()
        if likely_station != 'UNKNOWN':
            # Get expected delay from physics model (IRI-2020 if available)
            expected_delay_ms, dist_km, uncertainty_ms = self._predict_geometric_delay(
                likely_station, system_time
            )
            
            if expected_delay_ms > 0:
                expected_offset_ms = expected_delay_ms
                
                # Search window: 3-sigma from physics model, minimum 50ms
                # CHU needs wider window due to template offset
                if likely_station == 'CHU':
                    # CHU: correlation peak at ~250ms + propagation delay
                    adaptive_window_ms = max(100.0, uncertainty_ms * 3)
                else:
                    # WWV/WWVH: tighter window from physics model
                    adaptive_window_ms = max(50.0, uncertainty_ms * 3)
                
                logger.debug(f"{self.channel_name}: Physics-based search: "
                            f"expected={expected_offset_ms:.1f}ms, window=±{adaptive_window_ms:.0f}ms")
        
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
            logger.debug(f"{self.channel_name}: FSK decode result: detected={fsk_res.detected}, "
                        f"frames={fsk_res.frames_decoded}/9, confidence={fsk_res.decode_confidence:.2f}")
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
        # Validate each detection against the ArrivalPatternMatrix
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
                # Convert timing_error_ms to sample offset for validation
                detected_sample = int(det.timing_error_ms * self.sample_rate / 1000)
                
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
                
                # Log validation result but DON'T reject - let downstream handle it
                # The physics model may need calibration, so we flag rather than reject
                if not is_valid:
                    logger.info(f"{self.channel_name}: Physics validation WARNING: "
                               f"{det.station.value} @ {det.timing_error_ms:.1f}ms - {reason}")
                    # Reduce confidence for physics outliers but don't reject
                    physics_confidence = 0.3
                else:
                    logger.debug(f"{self.channel_name}: Detection VALIDATED: "
                                f"{det.station.value} @ {det.timing_error_ms:.1f}ms - {reason}")
            
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
            
        with self._lock:
            self.minutes_processed += 1
        
        # Store FSK data for caller to retrieve
        self._last_chu_fsk_data = chu_metrics if chu_metrics else None
            
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
