#!/usr/bin/env python3
"""
Station Model - Physics-Based Multi-Station Detection Framework

================================================================================
PURPOSE
================================================================================
Define physics-based models for each time standard station (WWV, WWVH, BPM, CHU).
Each model encapsulates:

1. Signal characteristics (tone frequency, tick duration, timing offset)
2. Confidence windows (minutes where station has unique/unambiguous features)
3. Expected propagation parameters (receiver-specific)
4. Template generation for matched filtering

This replaces the voting-based discrimination with Maximum Likelihood Estimation
(MLE) where we simultaneously estimate the power contribution of each station.

================================================================================
THE BPM CHALLENGE
================================================================================
BPM (China) complicates discrimination because:
- Same 1000 Hz tone as WWV (cannot separate by frequency)
- 20 ms advance (pulses emitted 20 ms BEFORE UTC second)
- Long propagation (~38-50 ms from China to continental US)
- Net arrival: -20ms + 45ms ≈ +25ms (overlaps with WWV's ~8ms arrival!)

Solution: Exploit BPM's unique features:
- UT1 minutes (25-29, 55-59): 100 ms pulses (10× longer than WWV)
- Pure carrier minutes (10-15, 40-45): No time code modulation
- Tick duration: 10 ms vs WWV's 5 ms

================================================================================
ARCHITECTURE
================================================================================
                                                                              
    StationModel ──► Correlator Template ──► Predicted ToA Window
         │                                         │
         │                                         ▼
         │                              ┌──────────────────────┐
         │                              │ Matched Filter Bank  │
         │                              │ (parallel per station)│
         │                              └──────────────────────┘
         │                                         │
         ▼                                         ▼
    Confidence Windows ──────────────► ChannelAssignment
    (calibration minutes)              (per-station power, ToA)

================================================================================
Author: HF Time Standard Team
Date: 2025-12-17
"""

import logging
import numpy as np
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Set, Tuple
from enum import Enum
from scipy import signal as scipy_signal

from .wwv_constants import (
    WWV_LAT, WWV_LON,
    WWVH_LAT, WWVH_LON,
    BPM_LAT, BPM_LON,
    CHU_LAT, CHU_LON,
    WWV_TICK_FREQ, WWVH_TICK_FREQ, BPM_TICK_FREQ, CHU_TICK_FREQ,
    BPM_UTC_TICK_DURATION, BPM_UT1_TICK_DURATION,
    BPM_UT1_MINUTES, BPM_PURE_CARRIER_MINUTES,
    WWV_ONLY_TONE_MINUTES, WWVH_ONLY_TONE_MINUTES,
    WWV_TEST_SIGNAL_MINUTE, WWVH_TEST_SIGNAL_MINUTE,
    SPEED_OF_LIGHT_KM_S, EARTH_RADIUS_KM,
    PROPAGATION_BOUNDS_MS,
)

logger = logging.getLogger(__name__)

# BPM timing offset: pulses emitted 20 ms BEFORE UTC second
BPM_TIMING_OFFSET_MS = -20.0


class StationID(Enum):
    """Station identifiers"""
    WWV = "WWV"
    WWVH = "WWVH"
    BPM = "BPM"
    CHU = "CHU"


@dataclass
class StationModel:
    """
    Physics-based model for a single time standard station.
    
    Each model defines the signal characteristics and confidence windows
    that enable MLE-based component decomposition on shared frequencies.
    
    Attributes:
        station: Station identifier (WWV, WWVH, BPM, CHU)
        tone_frequency_hz: Primary timing tone frequency
        tick_duration_sec: Duration of timing tick (varies for BPM by mode)
        timing_offset_ms: Offset from UTC second (BPM = -20ms, others = 0)
        location: (latitude, longitude) of transmitter
        
        expected_delay_ms: Receiver-specific expected propagation delay
        delay_uncertainty_ms: Uncertainty in delay estimate
        
        calibration_minutes: Minutes with unambiguous station features
        ground_truth_minutes: Minutes where station broadcasts alone
        ut1_minutes: Minutes where station transmits UT1 (BPM only)
        pure_carrier_minutes: Minutes with no time code (BPM only)
    """
    station: StationID
    tone_frequency_hz: float
    tick_duration_sec: float
    timing_offset_ms: float
    location: Tuple[float, float]  # (lat, lon)
    
    # Receiver-specific (set during initialization)
    expected_delay_ms: float = 0.0
    delay_uncertainty_ms: float = 10.0
    distance_km: float = 0.0
    
    # Confidence windows
    calibration_minutes: Set[int] = field(default_factory=set)
    ground_truth_minutes: Set[int] = field(default_factory=set)
    ut1_minutes: Set[int] = field(default_factory=set)
    pure_carrier_minutes: Set[int] = field(default_factory=set)
    
    # Propagation bounds (min, max delay in ms)
    delay_bounds_ms: Tuple[float, float] = (0.0, 100.0)
    
    def get_search_window(self, minute: int, calibrated: bool = False) -> Tuple[float, float]:
        """
        Get correlator search window for this station.
        
        Args:
            minute: Current minute (0-59)
            calibrated: Whether system has calibrated this station's delay
            
        Returns:
            (center_ms, half_width_ms) for correlator search window
        """
        # Center on expected delay + timing offset
        center = self.expected_delay_ms + self.timing_offset_ms
        
        # Narrow window after calibration
        if calibrated:
            half_width = 10.0  # ±10 ms
        else:
            half_width = 50.0  # ±50 ms bootstrap
        
        return center, half_width
    
    def get_tick_duration_for_minute(self, minute: int) -> float:
        """
        Get tick duration for this station at given minute.
        
        BPM has different tick durations for UTC vs UT1 modes.
        """
        if self.station == StationID.BPM and minute in self.ut1_minutes:
            return BPM_UT1_TICK_DURATION  # 100 ms
        return self.tick_duration_sec
    
    def is_calibration_minute(self, minute: int) -> bool:
        """Check if this minute provides unambiguous calibration data."""
        return minute in self.calibration_minutes
    
    def is_ground_truth_minute(self, minute: int) -> bool:
        """Check if this station broadcasts alone during this minute."""
        return minute in self.ground_truth_minutes
    
    def is_ut1_minute(self, minute: int) -> bool:
        """Check if this minute transmits UT1 (BPM only)."""
        return minute in self.ut1_minutes
    
    def is_pure_carrier_minute(self, minute: int) -> bool:
        """Check if this minute has no time code modulation (BPM only)."""
        return minute in self.pure_carrier_minutes
    
    def is_usable_for_utc(self, minute: int) -> bool:
        """Check if this station is usable for UTC timing at this minute."""
        # BPM UT1 minutes are NOT usable for UTC (transmit UT1 time)
        if self.station == StationID.BPM and minute in self.ut1_minutes:
            return False
        return True
    
    def create_template(self, sample_rate: int, minute: int = 0) -> Dict[str, np.ndarray]:
        """
        Create matched filter template for this station.
        
        Returns quadrature (sin/cos) templates for phase-invariant detection.
        
        Args:
            sample_rate: Sample rate in Hz
            minute: Current minute (affects BPM tick duration)
            
        Returns:
            Dict with 'sin', 'cos' templates and metadata
        """
        duration_sec = self.get_tick_duration_for_minute(minute)
        n_samples = int(duration_sec * sample_rate)
        t = np.arange(n_samples) / sample_rate
        
        # Tukey window (α=0.1) for smooth edges
        window = scipy_signal.windows.tukey(n_samples, alpha=0.1)
        
        # Quadrature templates
        template_sin = np.sin(2 * np.pi * self.tone_frequency_hz * t) * window
        template_cos = np.cos(2 * np.pi * self.tone_frequency_hz * t) * window
        
        # Normalize to unit energy
        template_sin /= np.linalg.norm(template_sin)
        template_cos /= np.linalg.norm(template_cos)
        
        return {
            'sin': template_sin,
            'cos': template_cos,
            'frequency_hz': self.tone_frequency_hz,
            'duration_sec': duration_sec,
            'n_samples': n_samples
        }


@dataclass
class ChannelAssignment:
    """
    Component decomposition result for a shared channel.
    
    Instead of "dominant_station", we output power for ALL detected stations.
    This enables proper fusion weighting and cross-validation.
    """
    minute_timestamp: float
    channel: str
    frequency_mhz: float
    
    # Per-station component power (dB relative to noise floor)
    wwv_component_power_db: Optional[float] = None
    wwvh_component_power_db: Optional[float] = None
    bpm_component_power_db: Optional[float] = None
    chu_component_power_db: Optional[float] = None
    
    # Per-station ToA (ms from minute boundary)
    wwv_toa_ms: Optional[float] = None
    wwvh_toa_ms: Optional[float] = None
    bpm_toa_ms: Optional[float] = None
    chu_toa_ms: Optional[float] = None
    
    # Per-station confidence (0-1)
    wwv_confidence: float = 0.0
    wwvh_confidence: float = 0.0
    bpm_confidence: float = 0.0
    chu_confidence: float = 0.0
    
    # Residual after component subtraction
    residual_noise_db: float = 0.0
    
    # Cross-validation
    cross_validation_passed: bool = True
    cross_validation_error_ms: Optional[float] = None
    
    # BPM-specific
    bpm_timing_mode: str = "UTC"  # 'UTC' or 'UT1'
    bpm_usable_for_timing: bool = True
    bpm_tick_duration_ms: Optional[float] = None
    
    # Calibration flags
    is_calibration_minute: bool = False
    calibration_station: Optional[str] = None
    
    def get_usable_stations(self, min_confidence: float = 0.3, min_power_db: float = 6.0) -> List[str]:
        """
        Return list of stations usable for timing.
        
        Args:
            min_confidence: Minimum confidence threshold
            min_power_db: Minimum power threshold (dB above noise)
        """
        usable = []
        
        if (self.wwv_confidence >= min_confidence and 
            self.wwv_component_power_db is not None and 
            self.wwv_component_power_db >= min_power_db):
            usable.append('WWV')
            
        if (self.wwvh_confidence >= min_confidence and 
            self.wwvh_component_power_db is not None and 
            self.wwvh_component_power_db >= min_power_db):
            usable.append('WWVH')
            
        if (self.bpm_usable_for_timing and 
            self.bpm_confidence >= min_confidence and
            self.bpm_component_power_db is not None and
            self.bpm_component_power_db >= min_power_db):
            usable.append('BPM')
            
        if (self.chu_confidence >= min_confidence and
            self.chu_component_power_db is not None and
            self.chu_component_power_db >= min_power_db):
            usable.append('CHU')
            
        return usable
    
    def get_best_station(self) -> Optional[str]:
        """Return station with highest confidence among usable stations."""
        usable = self.get_usable_stations()
        if not usable:
            return None
        
        confidences = {
            'WWV': self.wwv_confidence,
            'WWVH': self.wwvh_confidence,
            'BPM': self.bpm_confidence if self.bpm_usable_for_timing else 0.0,
            'CHU': self.chu_confidence,
        }
        
        return max(usable, key=lambda s: confidences.get(s, 0.0))
    
    def to_dict(self) -> Dict:
        """Convert to dictionary for serialization."""
        return {
            'minute_timestamp': self.minute_timestamp,
            'channel': self.channel,
            'frequency_mhz': self.frequency_mhz,
            'wwv_component_power_db': self.wwv_component_power_db,
            'wwvh_component_power_db': self.wwvh_component_power_db,
            'bpm_component_power_db': self.bpm_component_power_db,
            'chu_component_power_db': self.chu_component_power_db,
            'wwv_toa_ms': self.wwv_toa_ms,
            'wwvh_toa_ms': self.wwvh_toa_ms,
            'bpm_toa_ms': self.bpm_toa_ms,
            'chu_toa_ms': self.chu_toa_ms,
            'wwv_confidence': self.wwv_confidence,
            'wwvh_confidence': self.wwvh_confidence,
            'bpm_confidence': self.bpm_confidence,
            'chu_confidence': self.chu_confidence,
            'residual_noise_db': self.residual_noise_db,
            'cross_validation_passed': self.cross_validation_passed,
            'cross_validation_error_ms': self.cross_validation_error_ms,
            'bpm_timing_mode': self.bpm_timing_mode,
            'bpm_usable_for_timing': self.bpm_usable_for_timing,
            'bpm_tick_duration_ms': self.bpm_tick_duration_ms,
            'is_calibration_minute': self.is_calibration_minute,
            'calibration_station': self.calibration_station,
        }


class StationModelFactory:
    """
    Factory for creating receiver-specific station models.
    
    Computes expected propagation delays based on receiver location.
    """
    
    def __init__(self, receiver_lat: float, receiver_lon: float):
        """
        Initialize factory with receiver location.
        
        Args:
            receiver_lat: Receiver latitude (degrees)
            receiver_lon: Receiver longitude (degrees)
        """
        self.receiver_lat = receiver_lat
        self.receiver_lon = receiver_lon
        
        # Pre-compute distances to all stations
        self.distances = {
            StationID.WWV: self._haversine(receiver_lat, receiver_lon, WWV_LAT, WWV_LON),
            StationID.WWVH: self._haversine(receiver_lat, receiver_lon, WWVH_LAT, WWVH_LON),
            StationID.BPM: self._haversine(receiver_lat, receiver_lon, BPM_LAT, BPM_LON),
            StationID.CHU: self._haversine(receiver_lat, receiver_lon, CHU_LAT, CHU_LON),
        }
        
        logger.info(f"StationModelFactory initialized at ({receiver_lat:.4f}, {receiver_lon:.4f})")
        for station, dist in self.distances.items():
            logger.info(f"  {station.value}: {dist:.0f} km")
    
    @staticmethod
    def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Calculate great circle distance in km."""
        lat1_rad = np.radians(lat1)
        lat2_rad = np.radians(lat2)
        dlat = np.radians(lat2 - lat1)
        dlon = np.radians(lon2 - lon1)
        
        a = np.sin(dlat/2)**2 + np.cos(lat1_rad) * np.cos(lat2_rad) * np.sin(dlon/2)**2
        c = 2 * np.arctan2(np.sqrt(a), np.sqrt(1-a))
        
        return EARTH_RADIUS_KM * c
    
    def _estimate_propagation_delay(self, station: StationID) -> float:
        """
        Estimate propagation delay for a station.
        
        Uses simplified model: path_length ≈ 1.15 × ground_distance
        (accounts for ionospheric reflection adding ~15% to path)
        """
        ground_km = self.distances[station]
        path_length_km = ground_km * 1.15  # Ionospheric path multiplier
        delay_ms = (path_length_km / SPEED_OF_LIGHT_KM_S) * 1000.0
        return delay_ms
    
    def create_wwv_model(self) -> StationModel:
        """Create WWV station model."""
        delay = self._estimate_propagation_delay(StationID.WWV)
        bounds = PROPAGATION_BOUNDS_MS.get('WWV', (2.0, 35.0))
        
        return StationModel(
            station=StationID.WWV,
            tone_frequency_hz=WWV_TICK_FREQ,
            tick_duration_sec=0.005,  # 5 ms
            timing_offset_ms=0.0,
            location=(WWV_LAT, WWV_LON),
            expected_delay_ms=delay,
            delay_uncertainty_ms=5.0,
            distance_km=self.distances[StationID.WWV],
            calibration_minutes={WWV_TEST_SIGNAL_MINUTE},  # Minute 8
            ground_truth_minutes=WWV_ONLY_TONE_MINUTES | {WWV_TEST_SIGNAL_MINUTE},
            ut1_minutes=set(),
            pure_carrier_minutes=set(),
            delay_bounds_ms=bounds,
        )
    
    def create_wwvh_model(self) -> StationModel:
        """Create WWVH station model."""
        delay = self._estimate_propagation_delay(StationID.WWVH)
        bounds = PROPAGATION_BOUNDS_MS.get('WWVH', (12.0, 60.0))
        
        return StationModel(
            station=StationID.WWVH,
            tone_frequency_hz=WWVH_TICK_FREQ,
            tick_duration_sec=0.005,  # 5 ms
            timing_offset_ms=0.0,
            location=(WWVH_LAT, WWVH_LON),
            expected_delay_ms=delay,
            delay_uncertainty_ms=10.0,
            distance_km=self.distances[StationID.WWVH],
            calibration_minutes={WWVH_TEST_SIGNAL_MINUTE},  # Minute 44
            ground_truth_minutes=WWVH_ONLY_TONE_MINUTES,
            ut1_minutes=set(),
            pure_carrier_minutes=set(),
            delay_bounds_ms=bounds,
        )
    
    def create_bpm_model(self) -> StationModel:
        """Create BPM station model."""
        delay = self._estimate_propagation_delay(StationID.BPM)
        bounds = PROPAGATION_BOUNDS_MS.get('BPM', (25.0, 80.0))
        
        return StationModel(
            station=StationID.BPM,
            tone_frequency_hz=BPM_TICK_FREQ,
            tick_duration_sec=BPM_UTC_TICK_DURATION,  # 10 ms (UTC mode)
            timing_offset_ms=BPM_TIMING_OFFSET_MS,  # -20 ms advance
            location=(BPM_LAT, BPM_LON),
            expected_delay_ms=delay,
            delay_uncertainty_ms=10.0,
            distance_km=self.distances[StationID.BPM],
            # UT1 minutes provide unambiguous 100ms pulses for calibration
            calibration_minutes=BPM_UT1_MINUTES,
            ground_truth_minutes=set(),  # BPM never alone on shared frequencies
            ut1_minutes=BPM_UT1_MINUTES,
            pure_carrier_minutes=BPM_PURE_CARRIER_MINUTES,
            delay_bounds_ms=bounds,
        )
    
    def create_chu_model(self) -> StationModel:
        """Create CHU station model."""
        delay = self._estimate_propagation_delay(StationID.CHU)
        bounds = PROPAGATION_BOUNDS_MS.get('CHU', (3.0, 40.0))
        
        return StationModel(
            station=StationID.CHU,
            tone_frequency_hz=CHU_TICK_FREQ,
            tick_duration_sec=0.5,  # 500 ms (300 ms for regular ticks)
            timing_offset_ms=0.0,
            location=(CHU_LAT, CHU_LON),
            expected_delay_ms=delay,
            delay_uncertainty_ms=5.0,
            distance_km=self.distances[StationID.CHU],
            calibration_minutes=set(),  # CHU has unique frequencies
            ground_truth_minutes=set(range(60)),  # CHU always alone on its frequencies
            ut1_minutes=set(),
            pure_carrier_minutes=set(),
            delay_bounds_ms=bounds,
        )
    
    def create_all_models(self) -> Dict[StationID, StationModel]:
        """Create models for all stations."""
        return {
            StationID.WWV: self.create_wwv_model(),
            StationID.WWVH: self.create_wwvh_model(),
            StationID.BPM: self.create_bpm_model(),
            StationID.CHU: self.create_chu_model(),
        }
    
    def get_models_for_frequency(self, frequency_mhz: float) -> List[StationModel]:
        """
        Get station models that broadcast on a given frequency.
        
        Args:
            frequency_mhz: Center frequency in MHz
            
        Returns:
            List of StationModel objects for stations on this frequency
        """
        # Frequency to station mapping
        freq_stations = {
            2.5: [StationID.WWV, StationID.WWVH, StationID.BPM],
            5.0: [StationID.WWV, StationID.WWVH, StationID.BPM],
            10.0: [StationID.WWV, StationID.WWVH, StationID.BPM],
            15.0: [StationID.WWV, StationID.WWVH, StationID.BPM],
            20.0: [StationID.WWV],
            25.0: [StationID.WWV],
            3.33: [StationID.CHU],
            7.85: [StationID.CHU],
            14.67: [StationID.CHU],
        }
        
        # Round frequency for lookup
        freq_key = round(frequency_mhz, 2)
        station_ids = freq_stations.get(freq_key, [])
        
        all_models = self.create_all_models()
        return [all_models[sid] for sid in station_ids]


def get_calibration_status(minute: int, models: List[StationModel]) -> Dict[str, bool]:
    """
    Check which stations have calibration opportunities at this minute.
    
    Args:
        minute: Current minute (0-59)
        models: List of station models to check
        
    Returns:
        Dict mapping station name to calibration availability
    """
    return {
        model.station.value: model.is_calibration_minute(minute)
        for model in models
    }


def get_minute_characteristics(minute: int, models: List[StationModel]) -> Dict:
    """
    Get characteristics of all stations at a given minute.
    
    Useful for understanding what signals to expect and how to process them.
    
    Args:
        minute: Current minute (0-59)
        models: List of station models
        
    Returns:
        Dict with per-station characteristics for this minute
    """
    result = {
        'minute': minute,
        'stations': {}
    }
    
    for model in models:
        station_info = {
            'tick_duration_sec': model.get_tick_duration_for_minute(minute),
            'is_calibration': model.is_calibration_minute(minute),
            'is_ground_truth': model.is_ground_truth_minute(minute),
            'is_ut1': model.is_ut1_minute(minute),
            'is_pure_carrier': model.is_pure_carrier_minute(minute),
            'usable_for_utc': model.is_usable_for_utc(minute),
            'search_window': model.get_search_window(minute, calibrated=True),
        }
        result['stations'][model.station.value] = station_info
    
    return result
