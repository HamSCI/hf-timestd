#!/usr/bin/env python3
"""
Multi-Station Detector - Physics-Based Station Detection and Propagation Analysis

================================================================================
PURPOSE
================================================================================
Detect ALL receivable time standard stations on a frequency and extract
propagation information from each. This replaces the "voting" approach with
a physics-based approach where:

1. The GPSDO is the timing reference (not the loudest station)
2. ALL detected stations provide valid timing measurements
3. Each station's ToA reveals propagation conditions on that path
4. Fusion combines all measurements with appropriate uncertainty weighting

================================================================================
KEY INSIGHT
================================================================================
On shared frequencies (2.5, 5, 10, 15 MHz), we may receive:
- WWV (Fort Collins, CO)
- WWVH (Kekaha, HI)  
- BPM (Pucheng, China)

Each station transmits at the EXACT same UTC second boundary. The differences
in arrival time are due to:
1. Geographic distance (known precisely)
2. Ionospheric propagation path (variable, but predictable within bounds)

By detecting ALL stations and comparing measured ToA to expected ToA, we get:
- Timing from each station (for fusion)
- Propagation delay measurements (for ionospheric characterization)
- Cross-validation (stations should agree within propagation uncertainty)

================================================================================
ARCHITECTURE
================================================================================
                                                                              
    IQ Samples ──► Multi-Station Detection ──► Per-Station Results ──► Fusion
                         │                           │
                         │                           ├── WWV: ToA, SNR, delay
                         │                           ├── WWVH: ToA, SNR, delay
                         │                           ├── BPM: ToA, SNR, delay
                         │                           └── CHU: ToA, SNR, delay
                         │
                         └── Cross-Frequency Guidance (optional)
                             Use strong detection on one freq to
                             narrow search window on another

================================================================================
Author: HF Time Standard Team
"""

import logging
import numpy as np
import os
import json
import time
from pathlib import Path
from typing import Optional, Dict, List, Tuple, Any
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

logger = logging.getLogger(__name__)

# Import constants
from .wwv_constants import (
    WWV_LAT, WWV_LON,
    WWVH_LAT, WWVH_LON,
    BPM_LAT, BPM_LON,
    CHU_LAT, CHU_LON,
    SPEED_OF_LIGHT_KM_S,
    EARTH_RADIUS_KM,
    MAX_DISPERSION_MS,
    SAMPLE_RATE_FULL,
)


class DetectionQuality(Enum):
    """Quality level of a station detection"""
    EXCELLENT = "excellent"  # SNR > 20 dB, high confidence
    GOOD = "good"            # SNR 15-20 dB
    FAIR = "fair"            # SNR 10-15 dB
    MARGINAL = "marginal"    # SNR 6-10 dB
    NONE = "none"            # Not detected or below threshold


@dataclass
class StationDetection:
    """
    Detection result for a single station on a single frequency.
    
    This represents what we measured, what we expected, and the difference.
    """
    # Station identity
    station: str              # 'WWV', 'WWVH', 'BPM', 'CHU'
    frequency_mhz: float      # Center frequency
    channel: str              # Channel name (e.g., 'WWV 10 MHz')
    
    # Detection status
    detected: bool            # Was the station detected?
    quality: DetectionQuality
    snr_db: float             # Signal-to-noise ratio
    confidence: float         # Detection confidence (0-1)
    
    # Timing measurements (GPSDO-referenced)
    measured_toa_ms: float    # Measured time-of-arrival from minute boundary
    rtp_timestamp: int        # RTP timestamp of detection
    system_time: float        # System time of detection
    
    # Expected values (from physics/geography)
    expected_delay_ms: float  # Expected propagation delay
    distance_km: float        # Great circle distance to station
    
    # Propagation analysis
    delay_residual_ms: float  # measured_toa - expected_delay (ionospheric variation)
    delay_residual_valid: bool  # Is residual within plausible bounds?
    
    # For fusion
    timing_uncertainty_ms: float  # Estimated uncertainty for this measurement
    usable_for_timing: bool       # Can this be used for D_clock calculation?
    
    # Additional metadata
    detection_method: str = "tone_matched_filter"
    tick_duration_ms: Optional[float] = None  # For BPM discrimination


@dataclass
class MinuteDetectionResult:
    """
    All station detections for a single minute on a single frequency.
    
    This is the primary output - ALL detected stations, not just the "best" one.
    """
    # Identification
    minute_boundary: int      # Unix timestamp of minute start
    frequency_mhz: float
    channel: str
    rtp_timestamp: int
    
    # All station detections (may have 0-4 entries depending on propagation)
    detections: Dict[str, StationDetection] = field(default_factory=dict)
    
    # Summary statistics
    n_stations_detected: int = 0
    stations_detected: List[str] = field(default_factory=list)
    
    # Cross-validation
    cross_validation_passed: bool = True
    cross_validation_error_ms: Optional[float] = None  # Max disagreement between stations
    
    # Best measurement for timing (but ALL are passed to fusion)
    best_timing_station: Optional[str] = None
    best_timing_uncertainty_ms: Optional[float] = None
    
    def add_detection(self, detection: StationDetection):
        """Add a station detection to this minute's results."""
        self.detections[detection.station] = detection
        if detection.detected and detection.usable_for_timing:
            self.n_stations_detected += 1
            self.stations_detected.append(detection.station)
    
    def get_all_usable_detections(self) -> List[StationDetection]:
        """Get all detections usable for timing."""
        return [d for d in self.detections.values() if d.detected and d.usable_for_timing]
    
    def cross_validate(self) -> Tuple[bool, Optional[float]]:
        """
        Cross-validate timing between detected stations.
        
        All stations transmit at the same UTC instant, so after correcting
        for propagation delay, they should agree within ionospheric uncertainty.
        """
        usable = self.get_all_usable_detections()
        if len(usable) < 2:
            return True, None  # Can't cross-validate with < 2 stations
        
        # Calculate corrected emission times
        # T_emission = T_arrival - T_propagation
        # All should be ~0 (relative to minute boundary)
        emission_times = []
        for d in usable:
            t_emission = d.measured_toa_ms - d.expected_delay_ms
            emission_times.append(t_emission)
        
        # Max disagreement
        max_error = max(emission_times) - min(emission_times)
        
        # Threshold: 5ms is reasonable for ionospheric uncertainty
        passed = max_error < 5.0
        
        self.cross_validation_passed = passed
        self.cross_validation_error_ms = max_error
        
        return passed, max_error


class MultiStationDetector:
    """
    Physics-based multi-station detector.
    
    Detects ALL receivable stations and extracts propagation information.
    Does NOT vote or pick a "best" station - that's fusion's job.
    """
    
    # Station locations
    STATION_LOCATIONS = {
        'WWV': (WWV_LAT, WWV_LON),
        'WWVH': (WWVH_LAT, WWVH_LON),
        'BPM': (BPM_LAT, BPM_LON),
        'CHU': (CHU_LAT, CHU_LON),
    }
    
    # Stations present on each frequency
    FREQUENCY_STATIONS = {
        2.5: ['WWV', 'WWVH', 'BPM'],
        5.0: ['WWV', 'WWVH', 'BPM'],
        10.0: ['WWV', 'WWVH', 'BPM'],
        15.0: ['WWV', 'WWVH', 'BPM'],
        20.0: ['WWV'],  # WWV only
        25.0: ['WWV'],  # WWV only
        3.33: ['CHU'],  # CHU only
        7.85: ['CHU'],  # CHU only
        14.67: ['CHU'], # CHU only
    }
    
    def __init__(
        self,
        receiver_lat: Optional[float] = None,
        receiver_lon: Optional[float] = None,
        sample_rate: int = SAMPLE_RATE_FULL,
        ipc_dir: Path = Path('/dev/shm/timestd_detector')
    ):
        """
        Initialize multi-station detector.
        
        Args:
            receiver_lat: Receiver latitude (degrees), defaults to US center if None
            receiver_lon: Receiver longitude (degrees), defaults to US center if None
            sample_rate: Sample rate in Hz
            ipc_dir: Directory for cross-process coordination
        """
        # Default to approximate US center if coordinates not provided
        self.receiver_lat = receiver_lat if receiver_lat is not None else 39.0
        self.receiver_lon = receiver_lon if receiver_lon is not None else -98.0
        self.sample_rate = sample_rate
        self.ipc_dir = ipc_dir
        
        # Pre-calculate distances and expected delays to all stations
        self.station_distances: Dict[str, float] = {}
        self.station_expected_delays: Dict[str, float] = {}
        
        for station, (lat, lon) in self.STATION_LOCATIONS.items():
            dist = self._haversine_distance(self.receiver_lat, self.receiver_lon, lat, lon)
            self.station_distances[station] = dist
            # Base delay estimate (will be refined by frequency-dependent model)
            self.station_expected_delays[station] = self._estimate_base_delay(dist)
        
        # IPC for cross-frequency coordination
        self.ipc_dir.mkdir(parents=True, exist_ok=True)
        
        # Statistics
        self.stats = {
            'minutes_processed': 0,
            'total_detections': 0,
            'cross_validations_passed': 0,
            'cross_validations_failed': 0,
        }
        
        logger.info(f"MultiStationDetector initialized at ({self.receiver_lat:.4f}, {self.receiver_lon:.4f})")
        for station, dist in self.station_distances.items():
            logger.info(f"  {station}: {dist:.0f} km, base delay ~{self.station_expected_delays[station]:.1f} ms")
    
    @staticmethod
    def _haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Calculate great circle distance in km."""
        lat1_rad = np.radians(lat1)
        lat2_rad = np.radians(lat2)
        dlat = np.radians(lat2 - lat1)
        dlon = np.radians(lon2 - lon1)
        
        a = np.sin(dlat/2)**2 + np.cos(lat1_rad) * np.cos(lat2_rad) * np.sin(dlon/2)**2
        c = 2 * np.arctan2(np.sqrt(a), np.sqrt(1-a))
        
        return EARTH_RADIUS_KM * c
    
    def _estimate_base_delay(self, distance_km: float) -> float:
        """
        Estimate base propagation delay for a given distance.
        
        This is a rough estimate - actual delay depends on:
        - Frequency (affects ionospheric refraction)
        - Time of day (D/E/F layer heights)
        - Solar activity
        
        For HF skywave, path length is typically 1.1-1.3x ground distance.
        """
        # Assume average path length multiplier of 1.15
        path_length_km = distance_km * 1.15
        delay_ms = (path_length_km / SPEED_OF_LIGHT_KM_S) * 1000.0
        return delay_ms
    
    def get_expected_delay(
        self,
        station: str,
        frequency_mhz: float,
        time_of_day_utc: Optional[float] = None
    ) -> float:
        """
        Get expected propagation delay for a station at a given frequency.
        
        This can be refined with ionospheric models, but the base estimate
        is sufficient for detection and cross-validation.
        """
        base_delay = self.station_expected_delays.get(station, 30.0)
        
        # Frequency-dependent adjustment (higher freq = slightly shorter path)
        # This is a simplified model - real ionospheric models are more complex
        freq_factor = 1.0 - 0.01 * (frequency_mhz - 10.0)  # ±1% per MHz from 10 MHz
        freq_factor = max(0.95, min(1.05, freq_factor))
        
        return base_delay * freq_factor
    
    def get_stations_on_frequency(self, frequency_mhz: float) -> List[str]:
        """Get list of stations that broadcast on this frequency."""
        # Round to handle floating point
        freq_key = round(frequency_mhz, 2)
        return self.FREQUENCY_STATIONS.get(freq_key, [])
    
    def process_detections(
        self,
        channel: str,
        frequency_mhz: float,
        minute_boundary: int,
        rtp_timestamp: int,
        system_time: float,
        tone_detections: Dict[str, Any],  # From tone detector
        bpm_detection: Optional[Any] = None,  # From BPM discriminator
    ) -> MinuteDetectionResult:
        """
        Process all station detections for a minute and build unified result.
        
        Args:
            channel: Channel name
            frequency_mhz: Center frequency
            minute_boundary: Unix timestamp of minute start
            rtp_timestamp: RTP timestamp
            system_time: System time
            tone_detections: Dict with keys 'wwv', 'wwvh', 'chu' containing detection results
            bpm_detection: Optional BPM discrimination result
            
        Returns:
            MinuteDetectionResult with all detected stations
        """
        result = MinuteDetectionResult(
            minute_boundary=minute_boundary,
            frequency_mhz=frequency_mhz,
            channel=channel,
            rtp_timestamp=rtp_timestamp
        )
        
        possible_stations = self.get_stations_on_frequency(frequency_mhz)
        
        # Process each possible station
        for station in possible_stations:
            detection = self._create_detection(
                station=station,
                frequency_mhz=frequency_mhz,
                channel=channel,
                rtp_timestamp=rtp_timestamp,
                system_time=system_time,
                tone_detections=tone_detections,
                bpm_detection=bpm_detection
            )
            result.add_detection(detection)
        
        # Cross-validate if multiple stations detected
        result.cross_validate()
        
        # Find best timing measurement (lowest uncertainty among usable)
        usable = result.get_all_usable_detections()
        if usable:
            best = min(usable, key=lambda d: d.timing_uncertainty_ms)
            result.best_timing_station = best.station
            result.best_timing_uncertainty_ms = best.timing_uncertainty_ms
        
        # Update stats
        self.stats['minutes_processed'] += 1
        self.stats['total_detections'] += result.n_stations_detected
        if result.n_stations_detected >= 2:
            if result.cross_validation_passed:
                self.stats['cross_validations_passed'] += 1
            else:
                self.stats['cross_validations_failed'] += 1
        
        # Log summary
        if result.n_stations_detected > 0:
            stations_str = ', '.join(result.stations_detected)
            logger.info(
                f"📡 {channel}: Detected {result.n_stations_detected} station(s): {stations_str}"
            )
            if result.n_stations_detected >= 2:
                cv_status = "✓" if result.cross_validation_passed else "✗"
                logger.info(
                    f"    Cross-validation: {cv_status} (error: {result.cross_validation_error_ms:.2f} ms)"
                )
        
        return result
    
    def _create_detection(
        self,
        station: str,
        frequency_mhz: float,
        channel: str,
        rtp_timestamp: int,
        system_time: float,
        tone_detections: Dict[str, Any],
        bpm_detection: Optional[Any]
    ) -> StationDetection:
        """Create a StationDetection for a specific station."""
        
        # Get expected values
        expected_delay = self.get_expected_delay(station, frequency_mhz)
        distance = self.station_distances.get(station, 0.0)
        
        # Check if this station was detected
        detected = False
        snr_db = 0.0
        confidence = 0.0
        measured_toa_ms = 0.0
        tick_duration_ms = None
        usable = False
        
        station_key = station.lower()
        
        if station == 'BPM':
            # Check both discriminator and tone detector
            if bpm_detection is not None and bpm_detection.is_bpm_detected:
                # Use discriminator result (usually higher confidence due to tick matching)
                detected = True
                snr_db = bpm_detection.snr_db
                confidence = bpm_detection.confidence
                measured_toa_ms = bpm_detection.measured_delay_ms or expected_delay
                tick_duration_ms = bpm_detection.tick_duration_ms
                usable = bpm_detection.is_usable_for_utc
            elif 'bpm' in tone_detections and tone_detections['bpm'] is not None:
                # Fall back to matched filter tone detection (Stage 1)
                det = tone_detections['bpm']
                detected = True
                snr_db = getattr(det, 'snr_db', 0.0) or 0.0
                confidence = getattr(det, 'confidence', 0.0) or 0.0
                measured_toa_ms = getattr(det, 'timing_error_ms', 0.0) or 0.0
                usable = True  # We consider it usable for now
        elif station_key in tone_detections and tone_detections[station_key] is not None:
            det = tone_detections[station_key]
            detected = True
            snr_db = getattr(det, 'snr_db', 0.0) or 0.0
            confidence = getattr(det, 'confidence', 0.0) or 0.0
            measured_toa_ms = getattr(det, 'timing_error_ms', 0.0) or 0.0
            usable = True
        
        # Determine quality
        if not detected:
            quality = DetectionQuality.NONE
        elif snr_db >= 20:
            quality = DetectionQuality.EXCELLENT
        elif snr_db >= 15:
            quality = DetectionQuality.GOOD
        elif snr_db >= 10:
            quality = DetectionQuality.FAIR
        elif snr_db >= 6:
            quality = DetectionQuality.MARGINAL
        else:
            quality = DetectionQuality.NONE
            detected = False  # Below threshold
        
        # Calculate delay residual
        delay_residual = measured_toa_ms - expected_delay if detected else 0.0
        
        # Residual is valid if within plausible ionospheric bounds (±20 ms)
        delay_residual_valid = abs(delay_residual) < 20.0 if detected else False
        
        # Estimate timing uncertainty based on SNR
        # Higher SNR = lower uncertainty
        if snr_db >= 20:
            timing_uncertainty = 0.5  # ms
        elif snr_db >= 15:
            timing_uncertainty = 1.0
        elif snr_db >= 10:
            timing_uncertainty = 2.0
        elif snr_db >= 6:
            timing_uncertainty = 5.0
        else:
            timing_uncertainty = 10.0
        
        return StationDetection(
            station=station,
            frequency_mhz=frequency_mhz,
            channel=channel,
            detected=detected,
            quality=quality,
            snr_db=snr_db,
            confidence=confidence,
            measured_toa_ms=measured_toa_ms,
            rtp_timestamp=rtp_timestamp,
            system_time=system_time,
            expected_delay_ms=expected_delay,
            distance_km=distance,
            delay_residual_ms=delay_residual,
            delay_residual_valid=delay_residual_valid,
            timing_uncertainty_ms=timing_uncertainty,
            usable_for_timing=usable and detected and delay_residual_valid,
            tick_duration_ms=tick_duration_ms
        )
    
    def save_detection_for_cross_freq(
        self,
        detection: StationDetection,
        minute_boundary: int
    ):
        """
        Save a detection for cross-frequency coordination.
        
        Strong detections on one frequency can help narrow the search
        window on other frequencies (dispersion is typically < 3 ms).
        """
        if not detection.detected or detection.quality == DetectionQuality.NONE:
            return
        
        minute_dir = self.ipc_dir / str(minute_boundary)
        minute_dir.mkdir(parents=True, exist_ok=True)
        
        filename = f"{detection.station}_{detection.channel.replace(' ', '_')}.json"
        filepath = minute_dir / filename
        
        data = {
            'station': detection.station,
            'channel': detection.channel,
            'frequency_mhz': detection.frequency_mhz,
            'measured_toa_ms': detection.measured_toa_ms,
            'snr_db': detection.snr_db,
            'quality': detection.quality.value,
            'timestamp': time.time()
        }
        
        try:
            tmp_path = filepath.with_suffix('.tmp')
            with open(tmp_path, 'w') as f:
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
            tmp_path.rename(filepath)
        except Exception as e:
            logger.debug(f"Failed to save detection for cross-freq: {e}")
    
    def get_cross_freq_guidance(
        self,
        station: str,
        target_frequency_mhz: float,
        minute_boundary: int
    ) -> Optional[Dict[str, Any]]:
        """
        Get guidance from detections on other frequencies.
        
        If we have a strong detection of a station on one frequency,
        we can narrow the search window on other frequencies.
        """
        minute_dir = self.ipc_dir / str(minute_boundary)
        if not minute_dir.exists():
            return None
        
        best_guidance = None
        best_snr = 0.0
        
        for filepath in minute_dir.glob(f"{station}_*.json"):
            try:
                with open(filepath, 'r') as f:
                    data = json.load(f)
                
                # Skip if same frequency
                if abs(data['frequency_mhz'] - target_frequency_mhz) < 0.1:
                    continue
                
                if data['snr_db'] > best_snr:
                    best_snr = data['snr_db']
                    
                    # Calculate expected ToA adjustment for frequency difference
                    # Dispersion is typically < 3 ms between HF frequencies
                    freq_diff = abs(data['frequency_mhz'] - target_frequency_mhz)
                    dispersion_uncertainty_ms = min(3.0, 0.2 * freq_diff)
                    
                    best_guidance = {
                        'source_channel': data['channel'],
                        'source_frequency_mhz': data['frequency_mhz'],
                        'expected_toa_ms': data['measured_toa_ms'],
                        'search_window_ms': dispersion_uncertainty_ms + 2.0,  # Add margin
                        'source_snr_db': data['snr_db'],
                        'source_quality': data['quality']
                    }
            except Exception as e:
                logger.debug(f"Caught exception: {e}")
                continue
        
        return best_guidance
    
    def get_statistics(self) -> Dict[str, Any]:
        """Get detector statistics."""
        return {
            **self.stats,
            'receiver_location': (self.receiver_lat, self.receiver_lon),
            'station_distances_km': self.station_distances
        }


# Backward compatibility alias
GlobalStationVoter = MultiStationDetector


def create_detector(
    receiver_lat: float,
    receiver_lon: float,
    sample_rate: int = SAMPLE_RATE_FULL
) -> MultiStationDetector:
    """Factory function to create a multi-station detector."""
    return MultiStationDetector(
        receiver_lat=receiver_lat,
        receiver_lon=receiver_lon,
        sample_rate=sample_rate
    )
