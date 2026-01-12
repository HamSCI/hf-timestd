#!/usr/bin/env python3
"""
Physics-Based Station Identification

================================================================================
PURPOSE
================================================================================
Identify broadcast stations using the simplest sufficient method at each
operational phase. Replaces weighted voting with deterministic, physics-based
discrimination.

Core Principle: Don't guess when you can't discriminate with certainty.

================================================================================
THREE-PHASE IDENTIFICATION STRATEGY
================================================================================

BOOTSTRAP Phase:
    Method: Unambiguous signals only (frequency or modulation)
    - Anchor channels (CHU 3.33/7.85/14.67, WWV 20/25)
    - WWVH 1200 Hz tone (unique)
    - WWV 1000 Hz tone (if no 1200 Hz detected)
    - Skip ambiguous measurements
    
REFINEMENT Phase:
    Method: Timing validation against expected delays
    - Use established delay models
    - Validate detection timing matches expected arrival
    - Reject physically impossible detections
    
MEASUREMENT Phase:
    Method: Multi-channel extraction (no discrimination needed)
    - Extract IQ from temporal windows for each station
    - Measure all stations independently
    - Temporal separation sufficient

================================================================================
Author: HF Time Standard Team
Date: 2026-01-12
================================================================================
"""

import logging
import numpy as np
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from enum import Enum

from .operational_phase_manager import OperationalPhase, OperationalPhaseManager
from .wwv_constants import (
    WWV_COORDINATES,
    WWVH_COORDINATES,
    BPM_COORDINATES,
    CHU_COORDINATES
)

logger = logging.getLogger(__name__)

# Anchor frequencies (unambiguous by frequency alone)
ANCHOR_FREQUENCIES = {
    3.33: 'CHU',
    7.85: 'CHU',
    14.67: 'CHU',
    20.0: 'WWV',
    25.0: 'WWV'
}

# Shared frequencies (require discrimination)
SHARED_FREQUENCIES = [2.5, 5.0, 10.0, 15.0]


@dataclass
class StationDelayModel:
    """Propagation delay model for a station."""
    station: str
    frequency_mhz: float
    mean_delay_ms: float
    std_delay_ms: float
    n_measurements: int
    last_updated: float
    
    def get_validation_window_ms(self) -> float:
        """Get timing validation window (±ms)."""
        if self.n_measurements < 10:
            return 5.0  # Wide during bootstrap
        elif self.std_delay_ms < 1.0:
            return 2.0  # Narrow when confident
        else:
            return 3.0  # Medium otherwise
    
    def is_timing_consistent(self, measured_delay_ms: float) -> bool:
        """Check if measured delay is consistent with model."""
        window = self.get_validation_window_ms()
        return abs(measured_delay_ms - self.mean_delay_ms) < window


@dataclass
class StationIdentification:
    """Result of station identification."""
    station: Optional[str]           # Identified station (None if ambiguous)
    confidence: float                # 0.0-1.0 confidence
    method: str                      # Identification method used
    reason: str                      # Explanation of decision
    timing_validated: bool = False   # Was timing validation used?
    timing_error_ms: Optional[float] = None  # Timing error if validated


class StationIdentifier:
    """
    Physics-based station identification.
    
    Uses simplest sufficient method based on operational phase:
    - BOOTSTRAP: Unambiguous signals only
    - REFINEMENT: Timing validation
    - MEASUREMENT: Multi-channel extraction
    
    Usage:
        identifier = StationIdentifier(operational_phase_manager)
        
        result = identifier.identify(
            frequency_mhz=10.0,
            has_1000hz_tone=True,
            has_1200hz_tone=False,
            measured_delay_ms=3.2
        )
        
        if result.station:
            print(f"Station: {result.station} (confidence: {result.confidence:.2f})")
        else:
            print(f"Ambiguous: {result.reason}")
    """
    
    def __init__(self, operational_phase_manager: OperationalPhaseManager):
        """
        Initialize station identifier.
        
        Args:
            operational_phase_manager: Phase manager for phase-dependent behavior
        """
        self.phase_manager = operational_phase_manager
        
        # Station delay models (learned during bootstrap/refinement)
        self.station_delay_models: Dict[str, StationDelayModel] = {}
        
        logger.info("StationIdentifier initialized (physics-based, no voting)")
    
    def identify(
        self,
        frequency_mhz: float,
        has_1000hz_tone: bool,
        has_1200hz_tone: bool,
        measured_delay_ms: float,
        has_fsk: bool = False
    ) -> StationIdentification:
        """
        Identify station using phase-appropriate method.
        
        Args:
            frequency_mhz: Frequency in MHz
            has_1000hz_tone: 1000 Hz tone detected (WWV/BPM)
            has_1200hz_tone: 1200 Hz tone detected (WWVH)
            measured_delay_ms: Measured propagation delay
            has_fsk: FSK modulation detected (CHU)
        
        Returns:
            StationIdentification with station, confidence, and method
        """
        phase = self.phase_manager.get_phase()
        
        if phase == OperationalPhase.BOOTSTRAP:
            return self._identify_bootstrap(
                frequency_mhz, has_1000hz_tone, has_1200hz_tone, has_fsk
            )
        
        elif phase == OperationalPhase.REFINEMENT:
            return self._identify_refinement(
                frequency_mhz, has_1000hz_tone, has_1200hz_tone, 
                measured_delay_ms, has_fsk
            )
        
        else:  # MEASUREMENT
            return self._identify_measurement(
                frequency_mhz, measured_delay_ms
            )
    
    def _identify_bootstrap(
        self,
        frequency_mhz: float,
        has_1000hz_tone: bool,
        has_1200hz_tone: bool,
        has_fsk: bool
    ) -> StationIdentification:
        """
        Identify station during bootstrap using only unambiguous signals.
        
        Strategy: Only process signals that are unambiguous by frequency or
        modulation. Skip ambiguous measurements rather than guessing.
        """
        # Anchor channels (unique frequencies)
        if frequency_mhz in ANCHOR_FREQUENCIES:
            station = ANCHOR_FREQUENCIES[frequency_mhz]
            return StationIdentification(
                station=station,
                confidence=1.0,
                method='anchor_frequency',
                reason=f'{station} anchor frequency ({frequency_mhz} MHz)'
            )
        
        # FSK modulation (CHU only)
        if has_fsk:
            return StationIdentification(
                station='CHU',
                confidence=1.0,
                method='fsk_modulation',
                reason='CHU FSK modulation detected'
            )
        
        # Shared frequencies - use modulation to discriminate
        if frequency_mhz in SHARED_FREQUENCIES:
            # WWVH 1200 Hz tone is unambiguous
            if has_1200hz_tone:
                return StationIdentification(
                    station='WWVH',
                    confidence=1.0,
                    method='unique_tone',
                    reason='WWVH 1200 Hz tone (unique)'
                )
            
            # WWV 1000 Hz tone (if no 1200 Hz detected)
            # High confidence but not certain (BPM also uses 1000 Hz)
            if has_1000hz_tone and not has_1200hz_tone:
                return StationIdentification(
                    station='WWV',
                    confidence=0.9,
                    method='likely_wwv',
                    reason='1000 Hz tone, no 1200 Hz (likely WWV, BPM possible)'
                )
            
            # Ambiguous - don't guess
            return StationIdentification(
                station=None,
                confidence=0.0,
                method='bootstrap_skip',
                reason='Ambiguous signal during bootstrap - skipping'
            )
        
        # Unknown frequency
        return StationIdentification(
            station=None,
            confidence=0.0,
            method='unknown_frequency',
            reason=f'Unknown frequency {frequency_mhz} MHz'
        )
    
    def _identify_refinement(
        self,
        frequency_mhz: float,
        has_1000hz_tone: bool,
        has_1200hz_tone: bool,
        measured_delay_ms: float,
        has_fsk: bool
    ) -> StationIdentification:
        """
        Identify station using timing validation.
        
        Strategy: First check if unambiguous by frequency/modulation, then
        validate timing. For shared frequencies, use timing to discriminate.
        """
        # First check if unambiguous by frequency/modulation
        bootstrap_result = self._identify_bootstrap(
            frequency_mhz, has_1000hz_tone, has_1200hz_tone, has_fsk
        )
        
        if bootstrap_result.station and bootstrap_result.confidence == 1.0:
            # Unambiguous by frequency/modulation - validate timing
            station = bootstrap_result.station
            
            if station in self.station_delay_models:
                model = self.station_delay_models[station]
                
                if model.is_timing_consistent(measured_delay_ms):
                    timing_error = measured_delay_ms - model.mean_delay_ms
                    return StationIdentification(
                        station=station,
                        confidence=1.0,
                        method='frequency_timing_validated',
                        reason=f'{station} confirmed by timing (error: {timing_error:+.2f}ms)',
                        timing_validated=True,
                        timing_error_ms=timing_error
                    )
                else:
                    # Timing mismatch - reject
                    expected = model.mean_delay_ms
                    window = model.get_validation_window_ms()
                    return StationIdentification(
                        station=None,
                        confidence=0.0,
                        method='timing_rejection',
                        reason=f'{station} rejected: timing {measured_delay_ms:.2f}ms outside expected {expected:.2f}±{window:.2f}ms',
                        timing_validated=False,
                        timing_error_ms=measured_delay_ms - expected
                    )
            else:
                # No timing model yet - trust frequency/modulation
                return bootstrap_result
        
        # Shared frequency - use timing to discriminate
        if frequency_mhz in SHARED_FREQUENCIES:
            # Check which station's timing window this falls into
            best_match = None
            best_error = float('inf')
            
            for station_name in ['WWV', 'WWVH', 'BPM']:
                if station_name not in self.station_delay_models:
                    continue
                
                model = self.station_delay_models[station_name]
                timing_error = abs(measured_delay_ms - model.mean_delay_ms)
                window = model.get_validation_window_ms()
                
                if timing_error < window and timing_error < best_error:
                    best_match = station_name
                    best_error = timing_error
            
            if best_match:
                return StationIdentification(
                    station=best_match,
                    confidence=0.95,
                    method='timing_discrimination',
                    reason=f'{best_match} identified by timing (error: {best_error:+.2f}ms)',
                    timing_validated=True,
                    timing_error_ms=best_error
                )
            
            # Doesn't match any known station timing
            return StationIdentification(
                station=None,
                confidence=0.0,
                method='timing_no_match',
                reason=f'Timing {measured_delay_ms:.2f}ms does not match any known station'
            )
        
        # Not a shared frequency and not unambiguous
        return StationIdentification(
            station=None,
            confidence=0.0,
            method='refinement_skip',
            reason=f'Cannot identify on {frequency_mhz} MHz'
        )
    
    def _identify_measurement(
        self,
        frequency_mhz: float,
        measured_delay_ms: float
    ) -> StationIdentification:
        """
        Identify station in measurement phase.
        
        Strategy: Use timing to identify which temporal window the signal
        falls into. In this phase, we extract all stations independently,
        so this is mainly for validation.
        """
        # Anchor frequencies are still unambiguous
        if frequency_mhz in ANCHOR_FREQUENCIES:
            station = ANCHOR_FREQUENCIES[frequency_mhz]
            return StationIdentification(
                station=station,
                confidence=1.0,
                method='anchor_frequency',
                reason=f'{station} anchor frequency'
            )
        
        # Shared frequencies - identify by timing
        if frequency_mhz in SHARED_FREQUENCIES:
            # Find which station's window this falls into
            for station_name in ['WWV', 'WWVH', 'BPM']:
                if station_name not in self.station_delay_models:
                    continue
                
                model = self.station_delay_models[station_name]
                window = 1.0  # ±1ms in measurement phase
                
                if abs(measured_delay_ms - model.mean_delay_ms) < window:
                    timing_error = measured_delay_ms - model.mean_delay_ms
                    return StationIdentification(
                        station=station_name,
                        confidence=1.0,
                        method='temporal_window',
                        reason=f'{station_name} in temporal window (error: {timing_error:+.2f}ms)',
                        timing_validated=True,
                        timing_error_ms=timing_error
                    )
            
            # Outside all known windows
            return StationIdentification(
                station=None,
                confidence=0.0,
                method='outside_windows',
                reason=f'Timing {measured_delay_ms:.2f}ms outside all known windows'
            )
        
        return StationIdentification(
            station=None,
            confidence=0.0,
            method='measurement_unknown',
            reason=f'Unknown frequency {frequency_mhz} MHz'
        )
    
    def update_delay_model(
        self,
        station: str,
        frequency_mhz: float,
        delay_ms: float,
        timestamp: float
    ):
        """
        Update station delay model with new measurement.
        
        Args:
            station: Station name
            frequency_mhz: Frequency in MHz
            delay_ms: Measured propagation delay
            timestamp: Unix timestamp
        """
        key = f"{station}_{frequency_mhz}"
        
        if key not in self.station_delay_models:
            # Create new model
            self.station_delay_models[key] = StationDelayModel(
                station=station,
                frequency_mhz=frequency_mhz,
                mean_delay_ms=delay_ms,
                std_delay_ms=5.0,  # Initial uncertainty
                n_measurements=1,
                last_updated=timestamp
            )
            logger.info(f"Created delay model for {key}: {delay_ms:.2f}ms")
        else:
            # Update existing model (running mean and std)
            model = self.station_delay_models[key]
            n = model.n_measurements
            
            # Update mean
            old_mean = model.mean_delay_ms
            new_mean = (old_mean * n + delay_ms) / (n + 1)
            
            # Update std (Welford's online algorithm)
            if n > 1:
                old_std = model.std_delay_ms
                new_std = np.sqrt(
                    ((n - 1) * old_std**2 + (delay_ms - old_mean) * (delay_ms - new_mean)) / n
                )
                model.std_delay_ms = new_std
            
            model.mean_delay_ms = new_mean
            model.n_measurements = n + 1
            model.last_updated = timestamp
            
            logger.debug(
                f"Updated delay model for {key}: "
                f"{new_mean:.2f}±{model.std_delay_ms:.2f}ms (n={n+1})"
            )
    
    def get_delay_model(self, station: str, frequency_mhz: float) -> Optional[StationDelayModel]:
        """
        Get delay model for a station-frequency pair.
        
        Args:
            station: Station name
            frequency_mhz: Frequency in MHz
        
        Returns:
            StationDelayModel or None if not available
        """
        key = f"{station}_{frequency_mhz}"
        return self.station_delay_models.get(key)
    
    def get_all_delay_models(self) -> Dict[str, StationDelayModel]:
        """Get all delay models."""
        return self.station_delay_models.copy()
