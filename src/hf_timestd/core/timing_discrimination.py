#!/usr/bin/env python3
"""
Timing-Based Station Discrimination

================================================================================
PURPOSE
================================================================================
Discriminate between stations on SHARED frequencies using timing validation
rather than signal strength. Leverages GPSDO-anchored timing precision to
validate station assignments against geographic propagation constraints.

This module implements the three-phase discrimination strategy:
1. Bootstrap: Use schedule-based ground truth to establish timing
2. Validation: Use timing to discriminate stations
3. Refinement: Use phase coherence to validate and improve

Key Insight: Once timing is established with ±1ms accuracy, geography becomes
the discriminator. A signal claiming to be WWVH but arriving at WWV timing is
physically impossible and can be rejected with certainty.

================================================================================
ARCHITECTURE
================================================================================

Phase 1: BOOTSTRAP (Minutes 0-10)
----------------------------------
- GPSDO provides ±10ns UTC reference via RTP timestamps
- Use schedule-based ground truth minutes:
  * Minutes 1, 2: 440 Hz tones (WWVH-only, WWV-only)
  * Minutes 8, 44: Test signals (WWV-only, WWVH-only)
  * Minutes 16, 17, 19: WWV 500 Hz only
  * Minutes 43-51: WWVH 600 Hz only
- BPM tick duration: 10ms (UTC) or 100ms (UT1) vs 5ms (WWV/WWVH)
- Build station-specific propagation delay models
- Establish D_clock baseline (±1ms)

Phase 2: TIMING VALIDATION (Minutes 10+)
-----------------------------------------
- Validate each tick arrival against expected timing:
  * Expected_ToA = second + delay_station + D_clock
  * Reject if |measured - expected| > threshold
- Phase coherence validation (5-second windows):
  * Track phase progression across windows
  * Validate against expected Doppler from geography
  * Reject signals with random/inconsistent phase
- Ground truth tone timing validation:
  * 440/500/600 Hz tones must arrive at correct time
  * Onset timing validates both station ID and delay model

Phase 3: CONTINUOUS REFINEMENT (Ongoing)
-----------------------------------------
- Better discrimination → Better timing measurements
- Better timing → Tighter discrimination windows
- Phase stability tracking → Adaptive coherent integration
- Converge to high-confidence station assignment

================================================================================
Author: HF Time Standard Team
Date: 2026-01-12
================================================================================
"""

import logging
import numpy as np
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Set
from enum import Enum
from pathlib import Path
import json

from .wwv_constants import (
    TONE_SCHEDULE_500_600,
    WWV_COORDINATES,
    WWVH_COORDINATES,
    BPM_COORDINATES,
    CHU_COORDINATES
)

logger = logging.getLogger(__name__)


class DiscriminationPhase(Enum):
    """Discrimination phase tracking."""
    BOOTSTRAP = "bootstrap"           # Learning delays from ground truth
    VALIDATING = "validating"         # Using timing to discriminate
    REFINED = "refined"               # High-confidence, sub-ms accuracy


@dataclass
class StationDelayModel:
    """Propagation delay model for a station."""
    station: str                      # WWV, WWVH, BPM, CHU
    frequency_mhz: float              # Frequency in MHz
    
    # Delay statistics
    mean_delay_ms: float              # Mean propagation delay
    std_delay_ms: float               # Standard deviation
    min_delay_ms: float               # Minimum observed
    max_delay_ms: float               # Maximum observed
    
    # Quality metrics
    n_measurements: int               # Number of measurements
    last_updated: float               # Unix timestamp
    confidence: float                 # 0-1, based on consistency
    
    # Ground truth sources
    ground_truth_minutes: Set[int] = field(default_factory=set)
    
    def get_validation_window_ms(self) -> float:
        """Get timing validation window (±ms)."""
        # Start wide during bootstrap, narrow as confidence improves
        if self.n_measurements < 10:
            return 5.0  # ±5ms during bootstrap
        elif self.confidence > 0.9:
            return 1.0  # ±1ms when highly confident
        else:
            return 2.0  # ±2ms during validation
    
    def is_timing_consistent(self, measured_delay_ms: float) -> bool:
        """Check if measured delay is consistent with model."""
        window = self.get_validation_window_ms()
        return abs(measured_delay_ms - self.mean_delay_ms) < window


@dataclass
class TimingValidationResult:
    """Result of timing-based validation."""
    station: str
    frequency_mhz: float
    minute_number: int
    
    # Timing validation
    expected_delay_ms: float
    measured_delay_ms: float
    timing_error_ms: float
    timing_valid: bool
    
    # Phase coherence validation
    phase_coherent: bool
    phase_variance_rad2: float
    coherence_quality: float
    
    # Ground truth validation
    is_ground_truth_minute: bool
    ground_truth_station: Optional[str]
    ground_truth_match: bool
    
    # Overall confidence
    discrimination_confidence: float
    rejection_reason: Optional[str] = None


@dataclass
class GroundTruthSchedule:
    """Schedule of ground truth minutes for station discrimination."""
    
    # 440 Hz tone minutes (definitive)
    MINUTE_1_WWVH = 1   # WWVH only
    MINUTE_2_WWV = 2    # WWV only
    
    # Test signal minutes (definitive)
    MINUTE_8_WWV = 8    # WWV only
    MINUTE_44_WWVH = 44 # WWVH only
    
    # 500 Hz WWV-only minutes
    WWV_500HZ_MINUTES = {1, 16, 17, 19}
    
    # 600 Hz WWVH-only minutes
    WWVH_600HZ_MINUTES = {2, 43, 44, 45, 46, 47, 48, 49, 50, 51}
    
    @classmethod
    def get_ground_truth_station(cls, minute: int, tone_freq: Optional[int] = None) -> Optional[str]:
        """
        Get definitive station for a ground truth minute.
        
        Args:
            minute: Minute within hour (0-59)
            tone_freq: Optional tone frequency (440, 500, 600)
            
        Returns:
            Station name ('WWV', 'WWVH') or None if not ground truth
        """
        # 440 Hz tone minutes
        if minute == cls.MINUTE_1_WWVH:
            return 'WWVH'
        if minute == cls.MINUTE_2_WWV:
            return 'WWV'
        
        # Test signal minutes
        if minute == cls.MINUTE_8_WWV:
            return 'WWV'
        if minute == cls.MINUTE_44_WWVH:
            return 'WWVH'
        
        # Tone-specific ground truth
        if tone_freq == 500 and minute in cls.WWV_500HZ_MINUTES:
            return 'WWV'
        if tone_freq == 600 and minute in cls.WWVH_600HZ_MINUTES:
            return 'WWVH'
        
        return None
    
    @classmethod
    def is_ground_truth_minute(cls, minute: int) -> bool:
        """Check if minute has any ground truth signal."""
        return (minute in {cls.MINUTE_1_WWVH, cls.MINUTE_2_WWV, 
                          cls.MINUTE_8_WWV, cls.MINUTE_44_WWVH} or
                minute in cls.WWV_500HZ_MINUTES or
                minute in cls.WWVH_600HZ_MINUTES)
    
    @classmethod
    def get_all_ground_truth_minutes(cls) -> Set[int]:
        """Get all ground truth minutes (14 per hour)."""
        return ({cls.MINUTE_1_WWVH, cls.MINUTE_2_WWV, 
                cls.MINUTE_8_WWV, cls.MINUTE_44_WWVH} |
                cls.WWV_500HZ_MINUTES | cls.WWVH_600HZ_MINUTES)


class TimingDiscriminator:
    """
    Timing-based station discriminator for all 17 broadcasts.
    
    Uses GPSDO-anchored timing precision to validate station assignments
    against geographic propagation constraints.
    """
    
    def __init__(
        self,
        receiver_lat: float,
        receiver_lon: float,
        state_file: Optional[Path] = None
    ):
        """
        Initialize timing discriminator.
        
        Args:
            receiver_lat: Receiver latitude (degrees)
            receiver_lon: Receiver longitude (degrees)
            state_file: Optional path to state persistence file
        """
        self.receiver_lat = receiver_lat
        self.receiver_lon = receiver_lon
        self.state_file = state_file
        
        # Discrimination phase
        self.phase = DiscriminationPhase.BOOTSTRAP
        
        # Station delay models (keyed by station_frequency)
        self.delay_models: Dict[str, StationDelayModel] = {}
        
        # D_clock estimate (system time offset from UTC)
        self.d_clock_ms: Optional[float] = None
        self.d_clock_std_ms: Optional[float] = None
        
        # Bootstrap tracking
        self.bootstrap_measurements: List[Dict] = []
        self.bootstrap_start_time: Optional[float] = None
        
        # Ground truth schedule
        self.ground_truth = GroundTruthSchedule()
        
        # Load persisted state if available
        if state_file and state_file.exists():
            self._load_state()
        
        logger.info(f"TimingDiscriminator initialized at ({receiver_lat:.4f}, {receiver_lon:.4f})")
    
    def validate_detection(
        self,
        station: str,
        frequency_mhz: float,
        measured_toa_ms: float,
        minute_number: int,
        second_number: int,
        d_clock_ms: float,
        phase_variance_rad2: Optional[float] = None,
        coherence_quality: Optional[float] = None
    ) -> TimingValidationResult:
        """
        Validate a station detection using timing constraints.
        
        Args:
            station: Claimed station (WWV, WWVH, BPM, CHU)
            frequency_mhz: Frequency in MHz
            measured_toa_ms: Measured time of arrival (ms from second boundary)
            minute_number: Minute within hour (0-59)
            second_number: Second within minute (0-59)
            d_clock_ms: Current D_clock estimate
            phase_variance_rad2: Optional phase variance for coherence check
            coherence_quality: Optional coherence quality (0-1)
            
        Returns:
            TimingValidationResult with validation decision
        """
        key = f"{station}_{int(frequency_mhz * 1000)}"
        
        # Check ground truth
        is_ground_truth = self.ground_truth.is_ground_truth_minute(minute_number)
        ground_truth_station = self.ground_truth.get_ground_truth_station(minute_number)
        ground_truth_match = (ground_truth_station == station) if ground_truth_station else True
        
        # Get delay model
        if key not in self.delay_models:
            # No model yet - accept during bootstrap
            if self.phase == DiscriminationPhase.BOOTSTRAP:
                return TimingValidationResult(
                    station=station,
                    frequency_mhz=frequency_mhz,
                    minute_number=minute_number,
                    expected_delay_ms=0.0,
                    measured_delay_ms=measured_toa_ms,
                    timing_error_ms=0.0,
                    timing_valid=True,
                    phase_coherent=True,
                    phase_variance_rad2=phase_variance_rad2 or 0.0,
                    coherence_quality=coherence_quality or 0.5,
                    is_ground_truth_minute=is_ground_truth,
                    ground_truth_station=ground_truth_station,
                    ground_truth_match=ground_truth_match,
                    discrimination_confidence=0.3 if ground_truth_match else 0.1
                )
            else:
                # No model in validation phase - reject
                return TimingValidationResult(
                    station=station,
                    frequency_mhz=frequency_mhz,
                    minute_number=minute_number,
                    expected_delay_ms=0.0,
                    measured_delay_ms=measured_toa_ms,
                    timing_error_ms=999.0,
                    timing_valid=False,
                    phase_coherent=False,
                    phase_variance_rad2=phase_variance_rad2 or 999.0,
                    coherence_quality=coherence_quality or 0.0,
                    is_ground_truth_minute=is_ground_truth,
                    ground_truth_station=ground_truth_station,
                    ground_truth_match=ground_truth_match,
                    discrimination_confidence=0.0,
                    rejection_reason="No delay model available"
                )
        
        model = self.delay_models[key]
        
        # Calculate expected ToA
        # ToA = second_boundary + propagation_delay + D_clock
        expected_delay_ms = model.mean_delay_ms
        timing_error_ms = abs(measured_toa_ms - expected_delay_ms)
        
        # Timing validation
        validation_window = model.get_validation_window_ms()
        timing_valid = timing_error_ms < validation_window
        
        # Phase coherence validation
        phase_coherent = True
        if phase_variance_rad2 is not None:
            # Phase variance should be < (π/4)² for coherent signal
            phase_coherent = phase_variance_rad2 < (np.pi / 4) ** 2
        
        # Calculate discrimination confidence
        confidence = 0.0
        rejection_reason = None
        
        if not ground_truth_match:
            confidence = 0.0
            rejection_reason = f"Ground truth mismatch: expected {ground_truth_station}, got {station}"
        elif not timing_valid:
            confidence = 0.0
            rejection_reason = f"Timing error {timing_error_ms:.2f}ms exceeds window {validation_window:.2f}ms"
        elif not phase_coherent:
            confidence = 0.3
            rejection_reason = f"Phase incoherent: variance {phase_variance_rad2:.3f} rad²"
        else:
            # Valid detection - confidence based on timing precision
            timing_quality = 1.0 - min(1.0, timing_error_ms / validation_window)
            phase_quality = coherence_quality if coherence_quality is not None else 0.5
            model_quality = model.confidence
            
            confidence = 0.4 * timing_quality + 0.3 * phase_quality + 0.3 * model_quality
            
            # Boost confidence for ground truth matches
            if is_ground_truth and ground_truth_match:
                confidence = max(confidence, 0.9)
        
        return TimingValidationResult(
            station=station,
            frequency_mhz=frequency_mhz,
            minute_number=minute_number,
            expected_delay_ms=expected_delay_ms,
            measured_delay_ms=measured_toa_ms,
            timing_error_ms=timing_error_ms,
            timing_valid=timing_valid,
            phase_coherent=phase_coherent,
            phase_variance_rad2=phase_variance_rad2 or 0.0,
            coherence_quality=coherence_quality or 0.5,
            is_ground_truth_minute=is_ground_truth,
            ground_truth_station=ground_truth_station,
            ground_truth_match=ground_truth_match,
            discrimination_confidence=confidence,
            rejection_reason=rejection_reason
        )
    
    def update_delay_model(
        self,
        station: str,
        frequency_mhz: float,
        measured_delay_ms: float,
        minute_number: int,
        confidence: float = 1.0
    ):
        """
        Update delay model with new measurement.
        
        Args:
            station: Station name
            frequency_mhz: Frequency in MHz
            measured_delay_ms: Measured propagation delay
            minute_number: Minute within hour (for ground truth tracking)
            confidence: Measurement confidence (0-1)
        """
        key = f"{station}_{int(frequency_mhz * 1000)}"
        
        # Check if this is a ground truth minute
        is_ground_truth = self.ground_truth.is_ground_truth_minute(minute_number)
        
        if key not in self.delay_models:
            # Create new model
            self.delay_models[key] = StationDelayModel(
                station=station,
                frequency_mhz=frequency_mhz,
                mean_delay_ms=measured_delay_ms,
                std_delay_ms=2.0,  # Initial uncertainty
                min_delay_ms=measured_delay_ms,
                max_delay_ms=measured_delay_ms,
                n_measurements=1,
                last_updated=0.0,
                confidence=confidence,
                ground_truth_minutes={minute_number} if is_ground_truth else set()
            )
            logger.info(f"Created delay model for {key}: {measured_delay_ms:.2f}ms")
        else:
            # Update existing model (exponential moving average)
            model = self.delay_models[key]
            alpha = 0.1  # Smoothing factor
            
            model.mean_delay_ms = alpha * measured_delay_ms + (1 - alpha) * model.mean_delay_ms
            
            # Update variance estimate
            error = measured_delay_ms - model.mean_delay_ms
            model.std_delay_ms = np.sqrt(alpha * error**2 + (1 - alpha) * model.std_delay_ms**2)
            
            model.min_delay_ms = min(model.min_delay_ms, measured_delay_ms)
            model.max_delay_ms = max(model.max_delay_ms, measured_delay_ms)
            model.n_measurements += 1
            
            if is_ground_truth:
                model.ground_truth_minutes.add(minute_number)
            
            # Update confidence based on consistency
            if model.n_measurements > 10:
                # High confidence if std is low
                model.confidence = max(0.5, 1.0 - min(1.0, model.std_delay_ms / 5.0))
        
        # Check if ready to advance phase
        self._check_phase_advancement()
    
    def _check_phase_advancement(self):
        """Check if ready to advance to next discrimination phase."""
        if self.phase == DiscriminationPhase.BOOTSTRAP:
            # Need delay models for at least 2 stations with good coverage
            stations_ready = 0
            for model in self.delay_models.values():
                if (model.n_measurements >= 10 and 
                    model.confidence > 0.7 and
                    len(model.ground_truth_minutes) >= 3):
                    stations_ready += 1
            
            if stations_ready >= 2:
                self.phase = DiscriminationPhase.VALIDATING
                logger.info("Advanced to VALIDATING phase - timing-based discrimination enabled")
                self._save_state()
        
        elif self.phase == DiscriminationPhase.VALIDATING:
            # Need high-confidence models for all active stations
            all_refined = True
            for model in self.delay_models.values():
                if model.n_measurements < 30 or model.confidence < 0.9:
                    all_refined = False
                    break
            
            if all_refined and len(self.delay_models) >= 3:
                self.phase = DiscriminationPhase.REFINED
                logger.info("Advanced to REFINED phase - sub-ms discrimination active")
                self._save_state()
    
    def _save_state(self):
        """Save discriminator state to disk."""
        if not self.state_file:
            return
        
        state = {
            'version': 1,
            'phase': self.phase.value,
            'd_clock_ms': self.d_clock_ms,
            'd_clock_std_ms': self.d_clock_std_ms,
            'delay_models': {
                key: {
                    'station': model.station,
                    'frequency_mhz': model.frequency_mhz,
                    'mean_delay_ms': model.mean_delay_ms,
                    'std_delay_ms': model.std_delay_ms,
                    'min_delay_ms': model.min_delay_ms,
                    'max_delay_ms': model.max_delay_ms,
                    'n_measurements': model.n_measurements,
                    'confidence': model.confidence,
                    'ground_truth_minutes': list(model.ground_truth_minutes)
                }
                for key, model in self.delay_models.items()
            }
        }
        
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.state_file, 'w') as f:
                json.dump(state, f, indent=2)
            logger.debug(f"Saved timing discriminator state to {self.state_file}")
        except Exception as e:
            logger.warning(f"Failed to save timing discriminator state: {e}")
    
    def _load_state(self):
        """Load discriminator state from disk."""
        if not self.state_file or not self.state_file.exists():
            return
        
        try:
            with open(self.state_file, 'r') as f:
                state = json.load(f)
            
            self.phase = DiscriminationPhase(state.get('phase', 'bootstrap'))
            self.d_clock_ms = state.get('d_clock_ms')
            self.d_clock_std_ms = state.get('d_clock_std_ms')
            
            for key, model_data in state.get('delay_models', {}).items():
                self.delay_models[key] = StationDelayModel(
                    station=model_data['station'],
                    frequency_mhz=model_data['frequency_mhz'],
                    mean_delay_ms=model_data['mean_delay_ms'],
                    std_delay_ms=model_data['std_delay_ms'],
                    min_delay_ms=model_data['min_delay_ms'],
                    max_delay_ms=model_data['max_delay_ms'],
                    n_measurements=model_data['n_measurements'],
                    last_updated=0.0,
                    confidence=model_data['confidence'],
                    ground_truth_minutes=set(model_data.get('ground_truth_minutes', []))
                )
            
            logger.info(f"Loaded timing discriminator state: {self.phase.value}, "
                       f"{len(self.delay_models)} delay models")
        except Exception as e:
            logger.warning(f"Failed to load timing discriminator state: {e}")
    
    def get_status(self) -> Dict:
        """Get discriminator status summary."""
        return {
            'phase': self.phase.value,
            'd_clock_ms': self.d_clock_ms,
            'd_clock_std_ms': self.d_clock_std_ms,
            'n_delay_models': len(self.delay_models),
            'delay_models': {
                key: {
                    'station': model.station,
                    'frequency_mhz': model.frequency_mhz,
                    'mean_delay_ms': round(model.mean_delay_ms, 3),
                    'std_delay_ms': round(model.std_delay_ms, 3),
                    'n_measurements': model.n_measurements,
                    'confidence': round(model.confidence, 3),
                    'validation_window_ms': round(model.get_validation_window_ms(), 3),
                    'n_ground_truth_minutes': len(model.ground_truth_minutes)
                }
                for key, model in self.delay_models.items()
            }
        }
