#!/usr/bin/env python3
"""
Operational Phase Manager - Unified System-Wide Phase Control

================================================================================
PURPOSE
================================================================================
Provides a single source of truth for the system's operational phase, replacing
the fragmented phase systems in TimingCalibrator and TimingDiscriminator.

All subsystems query this manager to determine:
- Search window sizing (timing_calibrator)
- Discrimination strategy (timing_discrimination)
- Measurement mode (phase2_temporal_engine)
- Output confidence (phase2_analytics_service)

================================================================================
THREE-PHASE OPERATIONAL MODEL
================================================================================

BOOTSTRAP (0-10 minutes):
    Objective: Establish global RTP-to-UTC offset
    Search Window: ±500ms (wide, unknown propagation)
    Discrimination: Schedule-based ground truth only
    Measurement: Single dominant station per frequency
    Output: D_clock ±5ms, initial station assignments
    
    Transition Criteria:
    - Global RTP offset established (≥2 anchor stations)
    - ≥10 detections per station
    - D_clock std < 5ms over last 5 minutes

REFINEMENT (10-30 minutes):
    Objective: Refine timing accuracy to ±1ms
    Search Window: ±5ms (narrow, centered on expected)
    Discrimination: Timing validation enabled (Vote 10b weight: 8.0)
    Measurement: Single dominant station (timing-validated)
    Output: D_clock ±1ms, validated station assignments
    
    Transition Criteria:
    - D_clock std < 1ms over last 10 minutes
    - Station delay models: std < 2ms
    - ≥30 validated measurements per station

MEASUREMENT (30+ minutes):
    Objective: Measure ionospheric propagation independently
    Search Window: ±1ms (tight, sub-ms precision)
    Discrimination: High-confidence timing (Vote 10b weight: 12.0)
    Measurement: Multi-channel extraction (all 17 broadcasts)
    Output: Independent metrics for each broadcast
    
    Degradation Detection:
    - D_clock std > 2ms for 5 minutes → REFINEMENT
    - D_clock std > 5ms for 5 minutes → BOOTSTRAP

================================================================================
Author: HF Time Standard Team
Date: 2026-01-12
================================================================================
"""

import fcntl
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import numpy as np

logger = logging.getLogger(__name__)

# State file version (increment on schema changes)
OPERATIONAL_PHASE_STATE_VERSION = 1


class OperationalPhase(Enum):
    """System-wide operational phase."""
    BOOTSTRAP = "bootstrap"      # 0-10 min: Establish global RTP offset
    REFINEMENT = "refinement"    # 10-30 min: Refine timing, validate stations
    MEASUREMENT = "measurement"  # 30+ min: Operational ionospheric measurement


@dataclass
class BootstrapMetrics:
    """Metrics for bootstrap phase transition criteria."""
    # RTP offset establishment
    anchor_stations_detected: set = field(default_factory=set)
    global_rtp_offset_established: bool = False
    
    # Station detection counts
    station_detections: Dict[str, int] = field(default_factory=dict)
    
    # D_clock convergence (last N measurements)
    recent_d_clock_ms: List[float] = field(default_factory=list)
    recent_timestamps: List[float] = field(default_factory=list)
    window_size: int = 5  # minutes
    
    def add_measurement(self, station: str, d_clock_ms: float, timestamp: float, is_anchor: bool):
        """Add a measurement and update metrics."""
        # Track station detections
        if station not in self.station_detections:
            self.station_detections[station] = 0
        self.station_detections[station] += 1
        
        # Track anchor stations
        if is_anchor:
            self.anchor_stations_detected.add(station)
        
        # Track recent D_clock for convergence
        self.recent_d_clock_ms.append(d_clock_ms)
        self.recent_timestamps.append(timestamp)
        
        # Keep only last window_size minutes
        cutoff_time = timestamp - (self.window_size * 60)
        while self.recent_timestamps and self.recent_timestamps[0] < cutoff_time:
            self.recent_d_clock_ms.pop(0)
            self.recent_timestamps.pop(0)
    
    def check_transition_criteria(self) -> Tuple[bool, str]:
        """
        Check if bootstrap phase is complete.
        
        Returns:
            (ready_to_transition, reason)
        """
        # Criterion 1: Global RTP offset established (≥2 anchor stations)
        if len(self.anchor_stations_detected) < 2:
            return False, f"Need 2 anchor stations, have {len(self.anchor_stations_detected)}"
        
        # Criterion 2: ≥10 detections per station (at least 2 stations)
        stations_with_sufficient_data = [
            s for s, count in self.station_detections.items() if count >= 10
        ]
        if len(stations_with_sufficient_data) < 2:
            return False, f"Need 2 stations with ≥10 detections, have {len(stations_with_sufficient_data)}"
        
        # Criterion 3: D_clock std < 5ms over last 5 minutes
        if len(self.recent_d_clock_ms) < 5:
            return False, f"Need 5 minutes of data, have {len(self.recent_d_clock_ms)}"
        
        d_clock_std = np.std(self.recent_d_clock_ms)
        if d_clock_std >= 5.0:
            return False, f"D_clock std {d_clock_std:.2f}ms ≥ 5ms threshold"
        
        return True, f"Bootstrap complete: {len(self.anchor_stations_detected)} anchors, {len(stations_with_sufficient_data)} stations, D_clock std {d_clock_std:.2f}ms"


@dataclass
class RefinementMetrics:
    """Metrics for refinement phase transition criteria."""
    # D_clock convergence (last N measurements)
    recent_d_clock_ms: List[float] = field(default_factory=list)
    recent_timestamps: List[float] = field(default_factory=list)
    window_size: int = 10  # minutes
    
    # Station delay model quality
    station_delay_std: Dict[str, float] = field(default_factory=dict)
    station_measurement_counts: Dict[str, int] = field(default_factory=dict)
    
    def add_measurement(
        self,
        station: str,
        d_clock_ms: float,
        timestamp: float,
        delay_std_ms: Optional[float] = None
    ):
        """Add a measurement and update metrics."""
        # Track recent D_clock for convergence
        self.recent_d_clock_ms.append(d_clock_ms)
        self.recent_timestamps.append(timestamp)
        
        # Keep only last window_size minutes
        cutoff_time = timestamp - (self.window_size * 60)
        while self.recent_timestamps and self.recent_timestamps[0] < cutoff_time:
            self.recent_d_clock_ms.pop(0)
            self.recent_timestamps.pop(0)
        
        # Track station delay model quality
        if delay_std_ms is not None:
            self.station_delay_std[station] = delay_std_ms
        
        if station not in self.station_measurement_counts:
            self.station_measurement_counts[station] = 0
        self.station_measurement_counts[station] += 1
    
    def check_transition_criteria(self) -> Tuple[bool, str]:
        """
        Check if refinement phase is complete.
        
        Returns:
            (ready_to_transition, reason)
        """
        # Criterion 1: D_clock std < 1ms over last 10 minutes
        if len(self.recent_d_clock_ms) < 10:
            return False, f"Need 10 minutes of data, have {len(self.recent_d_clock_ms)}"
        
        d_clock_std = np.std(self.recent_d_clock_ms)
        if d_clock_std >= 1.0:
            return False, f"D_clock std {d_clock_std:.2f}ms ≥ 1ms threshold"
        
        # Criterion 2: Station delay models: std < 2ms
        stations_with_good_models = [
            s for s, std in self.station_delay_std.items() if std < 2.0
        ]
        if len(stations_with_good_models) < 2:
            return False, f"Need 2 stations with delay std < 2ms, have {len(stations_with_good_models)}"
        
        # Criterion 3: ≥30 validated measurements per station (at least 2 stations)
        stations_with_sufficient_data = [
            s for s, count in self.station_measurement_counts.items() if count >= 30
        ]
        if len(stations_with_sufficient_data) < 2:
            return False, f"Need 2 stations with ≥30 measurements, have {len(stations_with_sufficient_data)}"
        
        return True, f"Refinement complete: D_clock std {d_clock_std:.2f}ms, {len(stations_with_good_models)} stations with good models"
    
    def check_degradation(self) -> Tuple[bool, str]:
        """
        Check if timing has degraded enough to require re-bootstrap.
        
        Returns:
            (degraded, reason)
        """
        if len(self.recent_d_clock_ms) < 5:
            return False, "Insufficient data"
        
        d_clock_std = np.std(self.recent_d_clock_ms)
        if d_clock_std >= 5.0:
            return True, f"D_clock std {d_clock_std:.2f}ms ≥ 5ms degradation threshold"
        
        return False, ""


@dataclass
class MeasurementMetrics:
    """Metrics for measurement phase degradation detection."""
    # D_clock stability monitoring (last N measurements)
    recent_d_clock_ms: List[float] = field(default_factory=list)
    recent_timestamps: List[float] = field(default_factory=list)
    window_size: int = 5  # minutes
    
    # Consecutive degradation counter
    consecutive_degraded_minutes: int = 0
    degradation_threshold_minutes: int = 5
    
    def add_measurement(self, d_clock_ms: float, timestamp: float):
        """Add a measurement and update metrics."""
        # Track recent D_clock for stability
        self.recent_d_clock_ms.append(d_clock_ms)
        self.recent_timestamps.append(timestamp)
        
        # Keep only last window_size minutes
        cutoff_time = timestamp - (self.window_size * 60)
        while self.recent_timestamps and self.recent_timestamps[0] < cutoff_time:
            self.recent_d_clock_ms.pop(0)
            self.recent_timestamps.pop(0)
        
        # Check for degradation
        if len(self.recent_d_clock_ms) >= 5:
            d_clock_std = np.std(self.recent_d_clock_ms)
            if d_clock_std >= 2.0:
                self.consecutive_degraded_minutes += 1
            else:
                self.consecutive_degraded_minutes = 0
    
    def check_degradation_to_refinement(self) -> Tuple[bool, str]:
        """
        Check if timing has degraded to require refinement.
        
        Returns:
            (degraded, reason)
        """
        if len(self.recent_d_clock_ms) < 5:
            return False, "Insufficient data"
        
        d_clock_std = np.std(self.recent_d_clock_ms)
        
        if self.consecutive_degraded_minutes >= self.degradation_threshold_minutes:
            return True, f"D_clock std {d_clock_std:.2f}ms ≥ 2ms for {self.consecutive_degraded_minutes} consecutive minutes"
        
        return False, ""
    
    def check_degradation_to_bootstrap(self) -> Tuple[bool, str]:
        """
        Check if timing has severely degraded to require re-bootstrap.
        
        Returns:
            (degraded, reason)
        """
        if len(self.recent_d_clock_ms) < 5:
            return False, "Insufficient data"
        
        d_clock_std = np.std(self.recent_d_clock_ms)
        
        if d_clock_std >= 5.0:
            return True, f"D_clock std {d_clock_std:.2f}ms ≥ 5ms severe degradation threshold"
        
        return False, ""


class OperationalPhaseManager:
    """
    Centralized manager for system-wide operational phase.
    
    All subsystems query this manager to determine:
    - Search window sizing
    - Discrimination strategy
    - Measurement mode
    - Output confidence
    
    Usage:
        manager = OperationalPhaseManager(state_file=Path('/var/lib/timestd/state/operational_phase.json'))
        
        # Query phase-dependent parameters
        search_window = manager.get_search_window_ms('WWV')
        timing_weight = manager.get_discrimination_weight('timing_validation')
        use_multi_channel = manager.should_use_multi_channel_extraction()
        
        # Update metrics and check transitions
        manager.update_metrics(
            station='WWV',
            d_clock_ms=3.2,
            timestamp=time.time(),
            delay_std_ms=1.5,
            is_anchor=False
        )
    """
    
    def __init__(self, state_file: Optional[Path] = None):
        """
        Initialize operational phase manager.
        
        Args:
            state_file: Path to state persistence file
        """
        self.state_file = state_file
        if self.state_file:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
        
        # Current phase
        self.phase = OperationalPhase.BOOTSTRAP
        
        # Phase transition tracking
        self.phase_start_time = time.time()
        self.measurements_in_phase = 0
        self.last_transition_time = time.time()
        self.transition_history: List[Dict] = []
        
        # Phase-specific metrics
        self.bootstrap_metrics = BootstrapMetrics()
        self.refinement_metrics = RefinementMetrics()
        self.measurement_metrics = MeasurementMetrics()
        
        # Load existing state if available
        self._load_state()
        
        logger.info(f"OperationalPhaseManager initialized in {self.phase.value} phase")
    
    def get_phase(self) -> OperationalPhase:
        """Get current operational phase."""
        return self.phase
    
    def get_search_window_ms(self, station: str) -> float:
        """
        Get search window based on current phase.
        
        Args:
            station: Station name (for future per-station tuning)
        
        Returns:
            Search window in milliseconds (±ms)
        """
        if self.phase == OperationalPhase.BOOTSTRAP:
            return 500.0
        elif self.phase == OperationalPhase.REFINEMENT:
            return 5.0
        else:  # MEASUREMENT
            return 1.0
    
    def get_discrimination_weight(self, method: str) -> float:
        """
        Get discrimination vote weight based on phase.
        
        Args:
            method: Discrimination method name
        
        Returns:
            Vote weight for this method in current phase
        """
        if method == 'timing_validation':
            if self.phase == OperationalPhase.BOOTSTRAP:
                return 0.0  # Don't use timing during bootstrap
            elif self.phase == OperationalPhase.REFINEMENT:
                return 8.0
            else:  # MEASUREMENT
                return 12.0
        
        elif method == 'ground_truth':
            # Ground truth always has high weight
            return 15.0
        
        elif method == 'test_signal':
            # Test signal always has high weight
            return 15.0
        
        elif method == 'bcd_correlation':
            if self.phase == OperationalPhase.BOOTSTRAP:
                return 10.0
            elif self.phase == OperationalPhase.REFINEMENT:
                return 8.0
            else:  # MEASUREMENT
                return 5.0  # Less important when timing is precise
        
        elif method == 'carrier_power':
            if self.phase == OperationalPhase.BOOTSTRAP:
                return 10.0  # Important during bootstrap
            elif self.phase == OperationalPhase.REFINEMENT:
                return 5.0
            else:  # MEASUREMENT
                return 1.0  # Least important when timing is precise
        
        else:
            # Default weight for unknown methods
            return 5.0
    
    def should_use_multi_channel_extraction(self) -> bool:
        """
        Should we extract all stations independently?
        
        Returns:
            True if in MEASUREMENT phase, False otherwise
        """
        return self.phase == OperationalPhase.MEASUREMENT
    
    def get_output_confidence_grade(self) -> str:
        """
        Get output confidence grade based on phase.
        
        Returns:
            'bootstrap', 'operational', or 'science'
        """
        if self.phase == OperationalPhase.BOOTSTRAP:
            return 'bootstrap'
        elif self.phase == OperationalPhase.REFINEMENT:
            return 'operational'
        else:  # MEASUREMENT
            return 'science'
    
    def update_metrics(
        self,
        station: str,
        d_clock_ms: float,
        timestamp: float,
        delay_std_ms: Optional[float] = None,
        is_anchor: bool = False
    ):
        """
        Update phase metrics and check transition criteria.
        
        Args:
            station: Station name
            d_clock_ms: D_clock measurement
            timestamp: Unix timestamp
            delay_std_ms: Station delay model std dev (if available)
            is_anchor: Is this an anchor station?
        """
        self.measurements_in_phase += 1
        
        if self.phase == OperationalPhase.BOOTSTRAP:
            self.bootstrap_metrics.add_measurement(station, d_clock_ms, timestamp, is_anchor)
            ready, reason = self.bootstrap_metrics.check_transition_criteria()
            if ready:
                self._transition_to_refinement(reason)
        
        elif self.phase == OperationalPhase.REFINEMENT:
            self.refinement_metrics.add_measurement(station, d_clock_ms, timestamp, delay_std_ms)
            
            # Check for advancement to MEASUREMENT
            ready, reason = self.refinement_metrics.check_transition_criteria()
            if ready:
                self._transition_to_measurement(reason)
            
            # Check for degradation to BOOTSTRAP
            degraded, reason = self.refinement_metrics.check_degradation()
            if degraded:
                self._transition_to_bootstrap(reason)
        
        else:  # MEASUREMENT
            self.measurement_metrics.add_measurement(d_clock_ms, timestamp)
            
            # Check for severe degradation to BOOTSTRAP
            degraded, reason = self.measurement_metrics.check_degradation_to_bootstrap()
            if degraded:
                self._transition_to_bootstrap(reason)
            
            # Check for moderate degradation to REFINEMENT
            degraded, reason = self.measurement_metrics.check_degradation_to_refinement()
            if degraded:
                self._transition_to_refinement(reason)
    
    def set_global_rtp_offset_established(self, established: bool):
        """
        Notify manager that global RTP offset has been established.
        
        Args:
            established: True if global RTP offset is established
        """
        self.bootstrap_metrics.global_rtp_offset_established = established
    
    def _transition_to_bootstrap(self, reason: str):
        """Transition to BOOTSTRAP phase."""
        old_phase = self.phase
        self.phase = OperationalPhase.BOOTSTRAP
        self._record_transition(old_phase, self.phase, reason)
        
        # Reset metrics
        self.bootstrap_metrics = BootstrapMetrics()
        
        logger.warning(f"⚠️  PHASE TRANSITION: {old_phase.value} → BOOTSTRAP: {reason}")
    
    def _transition_to_refinement(self, reason: str):
        """Transition to REFINEMENT phase."""
        old_phase = self.phase
        self.phase = OperationalPhase.REFINEMENT
        self._record_transition(old_phase, self.phase, reason)
        
        # Reset refinement metrics
        self.refinement_metrics = RefinementMetrics()
        
        logger.info(f"✅ PHASE TRANSITION: {old_phase.value} → REFINEMENT: {reason}")
    
    def _transition_to_measurement(self, reason: str):
        """Transition to MEASUREMENT phase."""
        old_phase = self.phase
        self.phase = OperationalPhase.MEASUREMENT
        self._record_transition(old_phase, self.phase, reason)
        
        # Reset measurement metrics
        self.measurement_metrics = MeasurementMetrics()
        
        logger.info(f"🎯 PHASE TRANSITION: {old_phase.value} → MEASUREMENT: {reason}")
    
    def _record_transition(self, old_phase: OperationalPhase, new_phase: OperationalPhase, reason: str):
        """Record phase transition in history."""
        transition = {
            'timestamp': time.time(),
            'from_phase': old_phase.value,
            'to_phase': new_phase.value,
            'reason': reason,
            'measurements_in_phase': self.measurements_in_phase
        }
        self.transition_history.append(transition)
        
        # Keep only last 100 transitions
        if len(self.transition_history) > 100:
            self.transition_history = self.transition_history[-100:]
        
        # Reset phase tracking
        self.phase_start_time = time.time()
        self.measurements_in_phase = 0
        self.last_transition_time = time.time()
        
        # Save state
        self._save_state()
    
    def _load_state(self):
        """Load operational phase state from disk."""
        if not self.state_file or not self.state_file.exists():
            return
        
        try:
            with open(self.state_file) as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                try:
                    state = json.load(f)
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            
            # Version validation
            file_version = state.get('version', 0)
            if file_version < OPERATIONAL_PHASE_STATE_VERSION:
                logger.warning(
                    f"Operational phase state file version {file_version} < current "
                    f"{OPERATIONAL_PHASE_STATE_VERSION}, discarding stale state"
                )
                return
            
            # Restore phase
            phase_str = state.get('phase', 'bootstrap')
            self.phase = OperationalPhase(phase_str)
            
            # Restore tracking
            self.phase_start_time = state.get('phase_start_time', time.time())
            self.measurements_in_phase = state.get('measurements_in_phase', 0)
            self.last_transition_time = state.get('last_transition_time', time.time())
            self.transition_history = state.get('transition_history', [])
            
            logger.info(
                f"Loaded operational phase state: phase={self.phase.value}, "
                f"measurements={self.measurements_in_phase}"
            )
        
        except Exception as e:
            logger.warning(f"Failed to load operational phase state: {e}")
    
    def _save_state(self):
        """Save operational phase state to disk."""
        if not self.state_file:
            return
        
        lock_file = self.state_file.with_suffix('.lock')
        
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            
            with open(lock_file, 'w') as lf:
                fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
                try:
                    state = {
                        'version': OPERATIONAL_PHASE_STATE_VERSION,
                        'phase': self.phase.value,
                        'phase_start_time': self.phase_start_time,
                        'measurements_in_phase': self.measurements_in_phase,
                        'last_transition_time': self.last_transition_time,
                        'transition_history': self.transition_history,
                        'saved_at': datetime.now(timezone.utc).isoformat()
                    }
                    
                    # Atomic write
                    temp_file = self.state_file.with_suffix('.tmp')
                    with open(temp_file, 'w') as f:
                        json.dump(state, f, indent=2)
                        f.flush()
                        os.fsync(f.fileno())
                    temp_file.replace(self.state_file)
                finally:
                    fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
        
        except Exception as e:
            logger.error(f"Failed to save operational phase state: {e}", exc_info=True)
    
    def get_status(self) -> Dict:
        """
        Get current status for monitoring/web UI.
        
        Returns:
            Status dictionary with phase info and metrics
        """
        time_in_phase = time.time() - self.phase_start_time
        
        status = {
            'phase': self.phase.value,
            'phase_start_time': self.phase_start_time,
            'time_in_phase_seconds': time_in_phase,
            'measurements_in_phase': self.measurements_in_phase,
            'last_transition_time': self.last_transition_time,
            'search_window_ms': self.get_search_window_ms('WWV'),
            'timing_validation_weight': self.get_discrimination_weight('timing_validation'),
            'multi_channel_extraction': self.should_use_multi_channel_extraction(),
            'output_confidence_grade': self.get_output_confidence_grade()
        }
        
        # Add phase-specific metrics
        if self.phase == OperationalPhase.BOOTSTRAP:
            status['bootstrap'] = {
                'anchor_stations': list(self.bootstrap_metrics.anchor_stations_detected),
                'station_detections': dict(self.bootstrap_metrics.station_detections),
                'recent_d_clock_std_ms': np.std(self.bootstrap_metrics.recent_d_clock_ms) if self.bootstrap_metrics.recent_d_clock_ms else None
            }
        
        elif self.phase == OperationalPhase.REFINEMENT:
            status['refinement'] = {
                'recent_d_clock_std_ms': np.std(self.refinement_metrics.recent_d_clock_ms) if self.refinement_metrics.recent_d_clock_ms else None,
                'station_delay_std': dict(self.refinement_metrics.station_delay_std),
                'station_measurements': dict(self.refinement_metrics.station_measurement_counts)
            }
        
        else:  # MEASUREMENT
            status['measurement'] = {
                'recent_d_clock_std_ms': np.std(self.measurement_metrics.recent_d_clock_ms) if self.measurement_metrics.recent_d_clock_ms else None,
                'consecutive_degraded_minutes': self.measurement_metrics.consecutive_degraded_minutes
            }
        
        return status
