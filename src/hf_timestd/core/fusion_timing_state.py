#!/usr/bin/env python3
"""
Fusion Timing State: Unified Bootstrap/Metrology Timing Management

This module replaces the separate BootstrapService by integrating timing lock
management directly into the metrology pipeline. It handles:

1. Initial wide-window search (±200ms) when timing is unknown
2. Candidate accumulation across minutes
3. Two-tier lock transition (PROVISIONAL → REFINED)
4. Narrow-window operation (±100ms) after lock

Architecture:
------------
In FUSION mode (NTP-only, no GPS+PPS), we don't know the exact RTP-to-UTC mapping.
The FusionTimingState tracks detections and establishes timing lock:

    UNLOCKED → PROVISIONAL → REFINED
       ↑          |            |
       └──────────┴────────────┘
         (retreat on errors)

UNLOCKED: Wide search window (±200ms), accumulating candidates
PROVISIONAL: Minute boundaries established, archiving can begin (2-3 min)
REFINED: Stable offset after ionospheric averaging (10+ min)

Key Difference from Bootstrap:
-----------------------------
- Bootstrap was a separate service with rolling buffers
- FusionTimingState is embedded in MetrologyEngine, uses archive buffers
- No separate "bootstrap phase" - just wider search windows initially

Usage:
------
    # In MetrologyEngine.__init__:
    if not is_rtp_authority:
        self.fusion_state = FusionTimingState(sample_rate=self.sample_rate)
    
    # In MetrologyEngine.process_minute:
    search_window_ms = self.fusion_state.get_search_window_ms()
    detections = self.tone_detector.process_samples(..., search_window_ms=search_window_ms)
    
    for det in detections:
        self.fusion_state.add_detection(det, system_time)
    
    if self.fusion_state.check_lock_criteria():
        # Lock achieved, can narrow search window
"""

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple
from statistics import median, stdev

logger = logging.getLogger(__name__)


class LockTier(Enum):
    """Lock tier for two-tier timing establishment.
    
    Tier 0: No lock - still searching with wide window
    Tier 1: Provisional lock - minute boundaries established
    Tier 2: Refined lock - stable offset after ionospheric averaging
    """
    NONE = 0
    PROVISIONAL = 1
    REFINED = 2


@dataclass
class TimingMeasurement:
    """A single timing measurement for offset calculation."""
    timestamp: float           # Unix timestamp when measurement was taken
    timing_error_ms: float     # Measured timing error (raw_toa - expected_delay)
    station: str               # Station that provided this measurement
    frequency_mhz: float       # Broadcast frequency
    snr_db: float              # SNR of the detection
    confidence: float          # Detection confidence


@dataclass
class FusionTimingState:
    """
    Manages timing lock state for Fusion mode operation.
    
    Embedded in MetrologyEngine to handle timing establishment when
    GPS+PPS is not available (NTP-only operation).
    """
    sample_rate: int = 24000
    
    # Lock state
    lock_tier: LockTier = field(default=LockTier.NONE)
    provisional_lock_time: Optional[float] = None
    refined_lock_time: Optional[float] = None
    
    # Accumulated measurements
    measurements: List[TimingMeasurement] = field(default_factory=list)
    _max_measurements: int = 500  # Limit to prevent unbounded growth
    
    # Lock criteria
    min_stations_for_provisional: int = 2
    min_minutes_for_provisional: int = 2
    min_measurements_for_provisional: int = 4
    
    refined_lock_duration_sec: float = 600.0  # 10 minutes
    min_measurements_for_refined: int = 30
    max_offset_std_for_refined_ms: float = 15.0
    
    # Search window configuration
    wide_search_window_ms: float = 200.0   # Before lock
    narrow_search_window_ms: float = 100.0  # After lock
    
    # Statistics
    _stations_seen: set = field(default_factory=set)
    _minutes_with_detections: set = field(default_factory=set)
    
    def __post_init__(self):
        """Initialize mutable defaults."""
        if not hasattr(self, '_stations_seen') or self._stations_seen is None:
            self._stations_seen = set()
        if not hasattr(self, '_minutes_with_detections') or self._minutes_with_detections is None:
            self._minutes_with_detections = set()
    
    @property
    def is_locked(self) -> bool:
        """Check if any level of lock has been achieved."""
        return self.lock_tier != LockTier.NONE
    
    @property
    def is_fully_locked(self) -> bool:
        """Check if refined lock has been achieved."""
        return self.lock_tier == LockTier.REFINED
    
    def get_search_window_ms(self) -> float:
        """
        Get appropriate search window based on lock state.
        
        Returns:
            Search window half-width in milliseconds
        """
        if self.lock_tier == LockTier.NONE:
            return self.wide_search_window_ms
        else:
            return self.narrow_search_window_ms
    
    def add_detection(
        self,
        station: str,
        timing_error_ms: float,
        frequency_mhz: float,
        snr_db: float,
        confidence: float,
        system_time: float
    ) -> Optional[str]:
        """
        Add a detection and check for lock transitions.
        
        Args:
            station: Station identifier (WWV, WWVH, CHU, BPM)
            timing_error_ms: Measured timing error (raw_toa - expected_delay)
            frequency_mhz: Broadcast frequency
            snr_db: Signal-to-noise ratio
            confidence: Detection confidence (0-1)
            system_time: Unix timestamp of the measurement
            
        Returns:
            Status message if lock state changed, None otherwise
        """
        # Create measurement
        measurement = TimingMeasurement(
            timestamp=system_time,
            timing_error_ms=timing_error_ms,
            station=station,
            frequency_mhz=frequency_mhz,
            snr_db=snr_db,
            confidence=confidence
        )
        
        self.measurements.append(measurement)
        
        # Track statistics
        self._stations_seen.add(station)
        minute_boundary = (int(system_time) // 60) * 60
        self._minutes_with_detections.add(minute_boundary)
        
        # Trim old measurements to prevent memory leak
        if len(self.measurements) > self._max_measurements:
            trim_count = self._max_measurements // 5
            self.measurements = self.measurements[trim_count:]
            logger.debug(f"[FUSION_STATE] Trimmed {trim_count} old measurements")
        
        # Check for lock transitions
        return self._check_lock_transitions()
    
    def _check_lock_transitions(self) -> Optional[str]:
        """Check and perform lock state transitions."""
        
        if self.lock_tier == LockTier.NONE:
            # Check for provisional lock
            if self._check_provisional_criteria():
                self.lock_tier = LockTier.PROVISIONAL
                self.provisional_lock_time = time.time()
                
                offset_stats = self._compute_offset_stats()
                logger.info(f"[FUSION_STATE] PROVISIONAL LOCK achieved! "
                           f"stations={list(self._stations_seen)}, "
                           f"minutes={len(self._minutes_with_detections)}, "
                           f"median_error={offset_stats['median_ms']:.1f}ms")
                
                return f"PROVISIONAL_LOCK: {len(self._stations_seen)} stations"
        
        elif self.lock_tier == LockTier.PROVISIONAL:
            # Check for refined lock
            if self._check_refined_criteria():
                self.lock_tier = LockTier.REFINED
                self.refined_lock_time = time.time()
                
                offset_stats = self._compute_offset_stats()
                logger.info(f"[FUSION_STATE] REFINED LOCK achieved! "
                           f"measurements={len(self.measurements)}, "
                           f"median_error={offset_stats['median_ms']:.1f}ms, "
                           f"std={offset_stats['std_ms']:.1f}ms")
                
                return f"REFINED_LOCK: std={offset_stats['std_ms']:.1f}ms"
        
        return None
    
    def _check_provisional_criteria(self) -> bool:
        """Check if provisional lock criteria are met."""
        # Need detections from multiple stations
        if len(self._stations_seen) < self.min_stations_for_provisional:
            return False
        
        # Need detections across multiple minutes
        if len(self._minutes_with_detections) < self.min_minutes_for_provisional:
            return False
        
        # Need minimum number of measurements
        if len(self.measurements) < self.min_measurements_for_provisional:
            return False
        
        # Check that timing errors are consistent (not random noise)
        offset_stats = self._compute_offset_stats()
        if offset_stats['std_ms'] > 100.0:  # Too much variance
            return False
        
        return True
    
    def _check_refined_criteria(self) -> bool:
        """Check if refined lock criteria are met."""
        if self.provisional_lock_time is None:
            return False
        
        # Need sufficient time since provisional lock
        elapsed = time.time() - self.provisional_lock_time
        if elapsed < self.refined_lock_duration_sec:
            return False
        
        # Need sufficient measurements
        if len(self.measurements) < self.min_measurements_for_refined:
            return False
        
        # Need low standard deviation (stable offset)
        offset_stats = self._compute_offset_stats()
        if offset_stats['std_ms'] > self.max_offset_std_for_refined_ms:
            # Log progress
            if len(self.measurements) % 10 == 0:
                logger.info(f"[FUSION_STATE] Refined lock pending: "
                           f"std={offset_stats['std_ms']:.1f}ms > {self.max_offset_std_for_refined_ms}ms")
            return False
        
        return True
    
    def _compute_offset_stats(self) -> Dict[str, float]:
        """Compute statistics on timing error measurements."""
        if not self.measurements:
            return {'median_ms': 0.0, 'std_ms': 999.0, 'count': 0}
        
        # Use recent measurements (last 5 minutes)
        cutoff = time.time() - 300
        recent = [m for m in self.measurements if m.timestamp > cutoff]
        
        if len(recent) < 3:
            recent = self.measurements[-10:]  # Fallback to last 10
        
        errors = [m.timing_error_ms for m in recent]
        
        median_error = median(errors)
        std_error = stdev(errors) if len(errors) > 1 else 0.0
        
        return {
            'median_ms': median_error,
            'std_ms': std_error,
            'count': len(recent)
        }
    
    def get_timing_correction_ms(self) -> Optional[float]:
        """
        Get the timing correction to apply to NTP-derived timestamps.
        
        Returns:
            Correction in milliseconds, or None if not locked
        """
        if not self.is_locked:
            return None
        
        offset_stats = self._compute_offset_stats()
        # The median timing error IS the correction needed
        # If tones arrive 5ms late, we need to subtract 5ms from NTP time
        return -offset_stats['median_ms']
    
    def get_status(self) -> Dict:
        """Get current state for diagnostics."""
        offset_stats = self._compute_offset_stats()
        
        return {
            'lock_tier': self.lock_tier.name,
            'is_locked': self.is_locked,
            'stations_seen': list(self._stations_seen),
            'minutes_with_detections': len(self._minutes_with_detections),
            'total_measurements': len(self.measurements),
            'median_error_ms': offset_stats['median_ms'],
            'std_error_ms': offset_stats['std_ms'],
            'search_window_ms': self.get_search_window_ms(),
            'provisional_lock_time': self.provisional_lock_time,
            'refined_lock_time': self.refined_lock_time,
        }
    
    def reset(self):
        """Reset state (e.g., after timing anomaly detected)."""
        logger.warning("[FUSION_STATE] Resetting timing state")
        self.lock_tier = LockTier.NONE
        self.provisional_lock_time = None
        self.refined_lock_time = None
        self.measurements.clear()
        self._stations_seen.clear()
        self._minutes_with_detections.clear()
