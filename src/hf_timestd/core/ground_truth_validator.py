#!/usr/bin/env python3
"""
Ground Truth Validation Framework for Phase 2 Analytics

================================================================================
VULNERABILITY ADDRESSED (Issue 6.1 from PHASE2_CRITIQUE.md)
================================================================================

ORIGINAL PROBLEM:
-----------------
The Phase 2 analytics system had no mechanism to:
1. Compare D_clock estimates against GPS PPS (Pulse Per Second) ground truth
2. Log ground-truth minutes for discrimination validation
3. Track mode identification accuracy

Without ground truth validation, there was no way to:
- Verify the accuracy of timing measurements
- Calibrate systematic biases
- Detect algorithm regressions
- Build confidence in the system's performance

SOLUTION: THREE-TIER VALIDATION FRAMEWORK
=========================================

TIER 1: GPS PPS Validation
--------------------------
For receivers with GPS disciplined oscillators (GPSDO) or access to GPS PPS:
- Compare measured D_clock against GPS-derived reference
- Track systematic bias and random error statistics
- Enable automatic calibration offset computation

TIER 2: Station Discrimination Validation  
-----------------------------------------
Ground truth for which station was actually received:
- Known single-station minutes (WWV silent :29, :59; WWVH silent :00, :30)
- Test tone minutes (distinctive patterns)
- Manual annotations from listening
- Cross-validation with multiple receivers

TIER 3: Mode Identification Validation
--------------------------------------
Verify propagation mode identification:
- Compare predicted delays against measured delays
- Track mode consistency across frequencies
- Detect multipath when modes disagree

================================================================================
USAGE
================================================================================

    # Initialize validator
    validator = GroundTruthValidator(
        receiver_id="station_001",
        data_dir=Path("/data/validation")
    )
    
    # Register GPS PPS event
    validator.register_gps_pps(
        timestamp=time.time(),
        pps_offset_us=0.0,  # GPS PPS is the reference
        source="gpsd"
    )
    
    # Validate D_clock measurement
    result = validator.validate_d_clock(
        timestamp=time.time(),
        measured_d_clock_ms=15.234,
        station="WWV",
        frequency_mhz=10.0
    )
    
    # Register known station ground truth
    validator.register_station_truth(
        timestamp=time.time(),
        minute=29,  # WWV silent minute
        true_station="WWVH"  # Only WWVH transmits at :29
    )
    
    # Get validation statistics
    stats = validator.get_statistics()
    print(f"D_clock bias: {stats['d_clock_bias_ms']:.3f} ms")
    print(f"D_clock std: {stats['d_clock_std_ms']:.3f} ms")
    print(f"Discrimination accuracy: {stats['discrimination_accuracy']:.1%}")

================================================================================
REFERENCES
================================================================================
1. NIST SP 250-67: "WWV and WWVH Time and Frequency Services"
2. ITU-R TF.538-4: "Measures for Random Time/Frequency Instabilities"

================================================================================
REVISION HISTORY
================================================================================
2025-12-07: Initial implementation addressing Issue 6.1
"""

import json
import logging
import math
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from collections import deque

import numpy as np

logger = logging.getLogger(__name__)


# =============================================================================
# CONSTANTS
# =============================================================================

# WWV/WWVH silent minutes (for discrimination ground truth)
# WWV is silent at minutes 29 and 59
# WWVH is silent at minutes 00 and 30
WWV_SILENT_MINUTES = {29, 59}
WWVH_SILENT_MINUTES = {0, 30}

# Ground truth sources
class GroundTruthSource(Enum):
    """Source of ground truth information."""
    GPS_PPS = "gps_pps"           # GPS Pulse Per Second
    GPS_NMEA = "gps_nmea"         # GPS NMEA time sentences
    NTP = "ntp"                   # NTP server comparison
    MANUAL = "manual"             # Manual annotation
    SILENT_MINUTE = "silent_min"  # WWV/WWVH silent minute
    TEST_TONE = "test_tone"       # Distinctive test tones
    CROSS_RECEIVER = "cross_rx"   # Cross-validation with other receivers


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class GPSPPSEvent:
    """A GPS PPS timing event."""
    timestamp: float              # Unix timestamp of PPS edge
    pps_offset_us: float          # Offset from true second (microseconds)
    source: str                   # Source identifier (e.g., "gpsd", "pps0")
    quality: float = 1.0          # Quality weight (0-1)


@dataclass
class DClockValidation:
    """Result of D_clock validation against ground truth."""
    timestamp: float
    measured_d_clock_ms: float    # Measured value
    reference_d_clock_ms: float   # Ground truth reference
    error_ms: float               # measured - reference
    station: str
    frequency_mhz: float
    source: GroundTruthSource
    quality: float = 1.0


@dataclass
class DiscriminationValidation:
    """Result of station discrimination validation."""
    timestamp: float
    minute: int
    predicted_station: str        # What the algorithm predicted
    true_station: str             # Ground truth
    is_correct: bool
    confidence: float             # Algorithm's confidence
    source: GroundTruthSource


@dataclass
class ModeValidation:
    """Result of propagation mode validation."""
    timestamp: float
    station: str
    frequency_mhz: float
    predicted_mode: str
    predicted_delay_ms: float
    measured_delay_ms: float
    delay_error_ms: float
    mode_plausible: bool          # Is predicted mode consistent with delay?


@dataclass
class ValidationStatistics:
    """Aggregated validation statistics."""
    # D_clock statistics
    d_clock_count: int = 0
    d_clock_bias_ms: float = 0.0      # Mean error (systematic)
    d_clock_std_ms: float = 0.0       # Standard deviation (random)
    d_clock_rms_ms: float = 0.0       # Root mean square error
    d_clock_max_error_ms: float = 0.0
    
    # Discrimination statistics
    discrimination_count: int = 0
    discrimination_correct: int = 0
    discrimination_accuracy: float = 0.0
    
    # Mode identification statistics
    mode_count: int = 0
    mode_plausible_count: int = 0
    mode_accuracy: float = 0.0
    mode_delay_bias_ms: float = 0.0
    mode_delay_std_ms: float = 0.0
    
    # Calibration recommendations
    recommended_calibration_offset_ms: float = 0.0
    calibration_confidence: float = 0.0


# =============================================================================
# GROUND TRUTH VALIDATOR
# =============================================================================

class GroundTruthValidator:
    """
    Comprehensive ground truth validation framework.
    
    Provides mechanisms to:
    1. Compare D_clock against GPS PPS or other references
    2. Validate station discrimination using silent minutes
    3. Track mode identification accuracy
    4. Compute calibration offsets from ground truth
    
    All validation data is persisted for long-term analysis.
    """
    
    def __init__(
        self,
        receiver_id: str = "default",
        data_dir: Optional[Path] = None,
        max_history: int = 10000,
        auto_persist: bool = True
    ):
        """
        Initialize the ground truth validator.
        
        Args:
            receiver_id: Unique identifier for this receiver
            data_dir: Directory for persisting validation data
            max_history: Maximum number of validation events to keep in memory
            auto_persist: Automatically save data periodically
        """
        self.receiver_id = receiver_id
        self.data_dir = data_dir
        self.max_history = max_history
        self.auto_persist = auto_persist
        
        # GPS PPS reference tracking
        self.gps_pps_events: deque = deque(maxlen=1000)
        self.last_gps_pps: Optional[GPSPPSEvent] = None
        
        # Validation history
        self.d_clock_validations: deque = deque(maxlen=max_history)
        self.discrimination_validations: deque = deque(maxlen=max_history)
        self.mode_validations: deque = deque(maxlen=max_history)
        
        # Running statistics (online algorithm)
        self._d_clock_stats = OnlineStatistics()
        self._discrimination_stats = {'correct': 0, 'total': 0}
        self._mode_stats = OnlineStatistics()
        
        # Calibration tracking
        self._calibration_history: deque = deque(maxlen=100)
        
        # Load persisted data if available
        if data_dir:
            self._load_state()
        
        logger.info(f"Ground truth validator initialized for receiver '{receiver_id}'")
    
    # =========================================================================
    # GPS PPS VALIDATION (Tier 1)
    # =========================================================================
    
    def register_gps_pps(
        self,
        timestamp: float,
        pps_offset_us: float = 0.0,
        source: str = "gps",
        quality: float = 1.0
    ) -> None:
        """
        Register a GPS PPS timing event.
        
        This provides ground truth for the exact UTC second boundary.
        The D_clock measurement can be validated against this reference.
        
        Args:
            timestamp: Unix timestamp when PPS edge occurred
            pps_offset_us: Known offset of PPS from true UTC (microseconds)
            source: Source identifier (e.g., "gpsd", "pps0", "u-blox")
            quality: Quality weight (0-1), lower if signal degraded
        """
        event = GPSPPSEvent(
            timestamp=timestamp,
            pps_offset_us=pps_offset_us,
            source=source,
            quality=quality
        )
        
        self.gps_pps_events.append(event)
        self.last_gps_pps = event
        
        logger.debug(f"GPS PPS registered: offset={pps_offset_us:.1f}μs, source={source}")
    
    def get_reference_time(self, timestamp: float) -> Tuple[Optional[float], float]:
        """
        Get the reference (ground truth) time for a given timestamp.
        
        Uses the most recent GPS PPS event to compute the true time.
        
        Args:
            timestamp: Unix timestamp to get reference for
            
        Returns:
            (reference_time, quality) or (None, 0) if no reference available
        """
        if not self.last_gps_pps:
            return None, 0.0
        
        pps = self.last_gps_pps
        
        # Check if PPS is recent enough (within 60 seconds)
        age = timestamp - pps.timestamp
        if abs(age) > 60:
            logger.debug(f"GPS PPS too old: {age:.1f}s")
            return None, 0.0
        
        # Reference time = PPS timestamp + PPS offset
        reference_time = pps.timestamp + pps.pps_offset_us / 1e6
        
        # Quality degrades with age
        quality = pps.quality * max(0, 1 - abs(age) / 60)
        
        return reference_time, quality
    
    def validate_d_clock(
        self,
        timestamp: float,
        measured_d_clock_ms: float,
        station: str,
        frequency_mhz: float,
        propagation_delay_ms: float = 0.0
    ) -> Optional[DClockValidation]:
        """
        Validate a D_clock measurement against GPS ground truth.
        
        D_clock represents the system clock offset from UTC(NIST):
            D_clock = T_system - T_UTC(NIST)
        
        With GPS PPS as ground truth (assuming GPS ≈ UTC(NIST) within ~100ns):
            D_clock_reference ≈ T_system - T_GPS_PPS
        
        Args:
            timestamp: Unix timestamp of measurement
            measured_d_clock_ms: Measured D_clock value
            station: Station identifier (WWV, WWVH, CHU)
            frequency_mhz: Frequency in MHz
            propagation_delay_ms: Expected propagation delay (already subtracted)
            
        Returns:
            DClockValidation result or None if no ground truth available
        """
        reference_time, quality = self.get_reference_time(timestamp)
        
        if reference_time is None:
            return None
        
        # Compute reference D_clock
        # The system clock reading at 'timestamp' should be 'reference_time'
        # D_clock_reference = timestamp - reference_time (in ms)
        reference_d_clock_ms = (timestamp - reference_time) * 1000
        
        # Compute error
        error_ms = measured_d_clock_ms - reference_d_clock_ms
        
        validation = DClockValidation(
            timestamp=timestamp,
            measured_d_clock_ms=measured_d_clock_ms,
            reference_d_clock_ms=reference_d_clock_ms,
            error_ms=error_ms,
            station=station,
            frequency_mhz=frequency_mhz,
            source=GroundTruthSource.GPS_PPS,
            quality=quality
        )
        
        # Update statistics
        self.d_clock_validations.append(validation)
        self._d_clock_stats.update(error_ms, weight=quality)
        
        # Track for calibration
        self._calibration_history.append(error_ms)
        
        logger.debug(f"D_clock validation: measured={measured_d_clock_ms:.3f}ms, "
                    f"reference={reference_d_clock_ms:.3f}ms, error={error_ms:.3f}ms")
        
        # Auto-persist periodically
        if self.auto_persist and len(self.d_clock_validations) % 100 == 0:
            self._save_state()
        
        return validation
    
    # =========================================================================
    # STATION DISCRIMINATION VALIDATION (Tier 2)
    # =========================================================================
    
    def register_station_truth(
        self,
        timestamp: float,
        minute: int,
        true_station: str,
        source: GroundTruthSource = GroundTruthSource.MANUAL
    ) -> None:
        """
        Register ground truth for which station was actually received.
        
        This can be used for:
        - Silent minute validation (WWV silent at :29, :59; WWVH at :00, :30)
        - Manual annotation from listening
        - Cross-validation from other receivers
        
        Args:
            timestamp: Unix timestamp
            minute: Minute within the hour (0-59)
            true_station: The actual station transmitting (WWV, WWVH, or BOTH)
            source: Source of ground truth information
        """
        # Store for later validation
        self._pending_station_truth = {
            'timestamp': timestamp,
            'minute': minute,
            'true_station': true_station,
            'source': source
        }
        
        logger.debug(f"Station truth registered: minute={minute}, station={true_station}")
    
    def get_silent_minute_truth(self, minute: int) -> Optional[str]:
        """
        Get ground truth station for a silent minute.
        
        WWV is silent at minutes 29 and 59 → only WWVH transmits
        WWVH is silent at minutes 00 and 30 → only WWV transmits
        
        Args:
            minute: Minute within the hour (0-59)
            
        Returns:
            Station name if this is a silent minute, None otherwise
        """
        if minute in WWV_SILENT_MINUTES:
            return "WWVH"  # Only WWVH transmits when WWV is silent
        elif minute in WWVH_SILENT_MINUTES:
            return "WWV"   # Only WWV transmits when WWVH is silent
        return None
    
    def validate_discrimination(
        self,
        timestamp: float,
        minute: int,
        predicted_station: str,
        confidence: float = 1.0
    ) -> DiscriminationValidation:
        """
        Validate station discrimination against ground truth.
        
        Uses silent minutes as automatic ground truth, or manual annotations
        if registered.
        
        Args:
            timestamp: Unix timestamp
            minute: Minute within the hour
            predicted_station: Algorithm's prediction (WWV or WWVH)
            confidence: Algorithm's confidence in prediction
            
        Returns:
            DiscriminationValidation result
        """
        # Check for silent minute ground truth
        true_station = self.get_silent_minute_truth(minute)
        source = GroundTruthSource.SILENT_MINUTE
        
        # Check for manual annotation
        if hasattr(self, '_pending_station_truth'):
            truth = self._pending_station_truth
            if abs(truth['timestamp'] - timestamp) < 60:  # Within 1 minute
                true_station = truth['true_station']
                source = truth['source']
        
        # If no ground truth, can't validate
        if true_station is None:
            true_station = "UNKNOWN"
            is_correct = None
        else:
            is_correct = (predicted_station == true_station)
        
        validation = DiscriminationValidation(
            timestamp=timestamp,
            minute=minute,
            predicted_station=predicted_station,
            true_station=true_station,
            is_correct=is_correct if is_correct is not None else False,
            confidence=confidence,
            source=source
        )
        
        # Update statistics (only if we have ground truth)
        if is_correct is not None:
            self.discrimination_validations.append(validation)
            self._discrimination_stats['total'] += 1
            if is_correct:
                self._discrimination_stats['correct'] += 1
            
            logger.debug(f"Discrimination validation: predicted={predicted_station}, "
                        f"true={true_station}, correct={is_correct}")
        
        return validation
    
    # =========================================================================
    # MODE IDENTIFICATION VALIDATION (Tier 3)
    # =========================================================================
    
    def validate_mode(
        self,
        timestamp: float,
        station: str,
        frequency_mhz: float,
        predicted_mode: str,
        predicted_delay_ms: float,
        measured_delay_ms: float
    ) -> ModeValidation:
        """
        Validate propagation mode identification.
        
        A mode prediction is plausible if the predicted delay matches
        the measured delay within a reasonable tolerance.
        
        Args:
            timestamp: Unix timestamp
            station: Station identifier
            frequency_mhz: Frequency in MHz
            predicted_mode: Algorithm's mode prediction (e.g., "1F", "2F")
            predicted_delay_ms: Predicted propagation delay for this mode
            measured_delay_ms: Actually measured delay
            
        Returns:
            ModeValidation result
        """
        delay_error_ms = measured_delay_ms - predicted_delay_ms
        
        # Mode is plausible if error is within 1 ms (typical multipath spread)
        mode_plausible = abs(delay_error_ms) < 1.0
        
        validation = ModeValidation(
            timestamp=timestamp,
            station=station,
            frequency_mhz=frequency_mhz,
            predicted_mode=predicted_mode,
            predicted_delay_ms=predicted_delay_ms,
            measured_delay_ms=measured_delay_ms,
            delay_error_ms=delay_error_ms,
            mode_plausible=mode_plausible
        )
        
        # Update statistics
        self.mode_validations.append(validation)
        self._mode_stats.update(delay_error_ms)
        
        logger.debug(f"Mode validation: {station}@{frequency_mhz}MHz, "
                    f"mode={predicted_mode}, error={delay_error_ms:.3f}ms, "
                    f"plausible={mode_plausible}")
        
        return validation
    
    # =========================================================================
    # STATISTICS AND CALIBRATION
    # =========================================================================
    
    def get_statistics(self) -> ValidationStatistics:
        """
        Get aggregated validation statistics.
        
        Returns comprehensive statistics for:
        - D_clock validation (bias, std, RMS)
        - Discrimination accuracy
        - Mode identification accuracy
        - Recommended calibration offset
        """
        stats = ValidationStatistics()
        
        # D_clock statistics
        if self._d_clock_stats.count > 0:
            stats.d_clock_count = self._d_clock_stats.count
            stats.d_clock_bias_ms = self._d_clock_stats.mean
            stats.d_clock_std_ms = self._d_clock_stats.std_dev
            stats.d_clock_rms_ms = math.sqrt(
                stats.d_clock_bias_ms**2 + stats.d_clock_std_ms**2
            )
            
            # Find max error from recent validations
            if self.d_clock_validations:
                recent_errors = [v.error_ms for v in list(self.d_clock_validations)[-100:]]
                stats.d_clock_max_error_ms = max(abs(e) for e in recent_errors)
        
        # Discrimination statistics
        if self._discrimination_stats['total'] > 0:
            stats.discrimination_count = self._discrimination_stats['total']
            stats.discrimination_correct = self._discrimination_stats['correct']
            stats.discrimination_accuracy = (
                stats.discrimination_correct / stats.discrimination_count
            )
        
        # Mode statistics
        if self._mode_stats.count > 0:
            stats.mode_count = self._mode_stats.count
            stats.mode_delay_bias_ms = self._mode_stats.mean
            stats.mode_delay_std_ms = self._mode_stats.std_dev
            
            # Count plausible modes
            if self.mode_validations:
                recent_modes = list(self.mode_validations)[-100:]
                stats.mode_plausible_count = sum(1 for m in recent_modes if m.mode_plausible)
                stats.mode_accuracy = stats.mode_plausible_count / len(recent_modes)
        
        # Calibration recommendation
        if len(self._calibration_history) >= 10:
            # Recommended offset is the negative of the mean bias
            # (add this offset to cancel the bias)
            stats.recommended_calibration_offset_ms = -stats.d_clock_bias_ms
            # Confidence based on number of samples and consistency
            stats.calibration_confidence = min(1.0, len(self._calibration_history) / 100)
            if stats.d_clock_std_ms > 0:
                # Lower confidence if high variance
                stats.calibration_confidence *= min(1.0, 1.0 / stats.d_clock_std_ms)
        
        return stats
    
    def get_calibration_offset(self, min_samples: int = 30) -> Optional[float]:
        """
        Get recommended calibration offset from ground truth.
        
        The calibration offset should be ADDED to measured D_clock
        to correct systematic bias.
        
        Args:
            min_samples: Minimum samples required for confidence
            
        Returns:
            Calibration offset in ms, or None if insufficient data
        """
        if len(self._calibration_history) < min_samples:
            return None
        
        # Use trimmed mean to reject outliers
        errors = list(self._calibration_history)
        errors_sorted = sorted(errors)
        n = len(errors_sorted)
        
        # Trim 10% from each end
        trim_n = max(1, n // 10)
        trimmed = errors_sorted[trim_n:-trim_n]
        
        if not trimmed:
            return None
        
        mean_error = sum(trimmed) / len(trimmed)
        return -mean_error  # Negate to get correction
    
    # =========================================================================
    # PERSISTENCE
    # =========================================================================
    
    def _get_state_file(self) -> Optional[Path]:
        """Get path to state file."""
        if not self.data_dir:
            return None
        return self.data_dir / f"validation_state_{self.receiver_id}.json"
    
    def _save_state(self) -> None:
        """Save validation state to disk."""
        state_file = self._get_state_file()
        if not state_file:
            return
        
        try:
            state_file.parent.mkdir(parents=True, exist_ok=True)
            
            # Convert deques to lists for JSON serialization
            state = {
                'receiver_id': self.receiver_id,
                'timestamp': datetime.now(timezone.utc).isoformat(),
                'd_clock_validations': [asdict(v) for v in list(self.d_clock_validations)[-1000:]],
                'discrimination_validations': [asdict(v) for v in list(self.discrimination_validations)[-1000:]],
                'mode_validations': [asdict(v) for v in list(self.mode_validations)[-1000:]],
                'discrimination_stats': self._discrimination_stats,
                'd_clock_stats': {
                    'count': self._d_clock_stats.count,
                    'mean': self._d_clock_stats.mean,
                    'variance': self._d_clock_stats.variance
                },
                'calibration_history': list(self._calibration_history)
            }
            
            with open(state_file, 'w') as f:
                json.dump(state, f, indent=2, default=str)
            
            logger.debug(f"Saved validation state to {state_file}")
            
        except Exception as e:
            logger.warning(f"Failed to save validation state: {e}")
    
    def _load_state(self) -> None:
        """Load validation state from disk."""
        state_file = self._get_state_file()
        if not state_file or not state_file.exists():
            return
        
        try:
            with open(state_file, 'r') as f:
                state = json.load(f)
            
            # Restore discrimination stats
            self._discrimination_stats = state.get('discrimination_stats', {'correct': 0, 'total': 0})
            
            # Restore D_clock stats
            d_stats = state.get('d_clock_stats', {})
            if d_stats.get('count', 0) > 0:
                self._d_clock_stats.count = d_stats['count']
                self._d_clock_stats.mean = d_stats['mean']
                self._d_clock_stats.M2 = d_stats['variance'] * d_stats['count']
            
            # Restore calibration history
            cal_hist = state.get('calibration_history', [])
            self._calibration_history = deque(cal_hist, maxlen=100)
            
            logger.info(f"Loaded validation state: {self._d_clock_stats.count} D_clock, "
                       f"{self._discrimination_stats['total']} discrimination validations")
            
        except Exception as e:
            logger.warning(f"Failed to load validation state: {e}")
    
    def export_report(self, output_path: Optional[Path] = None) -> Dict[str, Any]:
        """
        Export a comprehensive validation report.
        
        Args:
            output_path: Optional path to save JSON report
            
        Returns:
            Dictionary with complete validation report
        """
        stats = self.get_statistics()
        
        report = {
            'receiver_id': self.receiver_id,
            'generated_at': datetime.now(timezone.utc).isoformat(),
            'summary': {
                'd_clock': {
                    'sample_count': stats.d_clock_count,
                    'bias_ms': round(stats.d_clock_bias_ms, 4),
                    'std_ms': round(stats.d_clock_std_ms, 4),
                    'rms_ms': round(stats.d_clock_rms_ms, 4),
                    'max_error_ms': round(stats.d_clock_max_error_ms, 4)
                },
                'discrimination': {
                    'sample_count': stats.discrimination_count,
                    'correct_count': stats.discrimination_correct,
                    'accuracy': round(stats.discrimination_accuracy, 4)
                },
                'mode_identification': {
                    'sample_count': stats.mode_count,
                    'plausible_count': stats.mode_plausible_count,
                    'accuracy': round(stats.mode_accuracy, 4),
                    'delay_bias_ms': round(stats.mode_delay_bias_ms, 4),
                    'delay_std_ms': round(stats.mode_delay_std_ms, 4)
                },
                'calibration': {
                    'recommended_offset_ms': round(stats.recommended_calibration_offset_ms, 4),
                    'confidence': round(stats.calibration_confidence, 4)
                }
            },
            'recommendations': self._generate_recommendations(stats)
        }
        
        if output_path:
            try:
                with open(output_path, 'w') as f:
                    json.dump(report, f, indent=2)
                logger.info(f"Validation report exported to {output_path}")
            except Exception as e:
                logger.warning(f"Failed to export report: {e}")
        
        return report
    
    def _generate_recommendations(self, stats: ValidationStatistics) -> List[str]:
        """Generate recommendations based on validation results."""
        recommendations = []
        
        # D_clock recommendations
        if stats.d_clock_count < 100:
            recommendations.append("Collect more GPS PPS validation data (target: 100+ samples)")
        
        if abs(stats.d_clock_bias_ms) > 0.5:
            recommendations.append(
                f"Apply calibration offset of {stats.recommended_calibration_offset_ms:.3f} ms "
                f"to correct {stats.d_clock_bias_ms:.3f} ms systematic bias"
            )
        
        if stats.d_clock_std_ms > 1.0:
            recommendations.append(
                f"D_clock variance is high ({stats.d_clock_std_ms:.3f} ms). "
                "Consider improving propagation mode identification or SNR thresholds."
            )
        
        # Discrimination recommendations
        if stats.discrimination_accuracy < 0.95 and stats.discrimination_count > 20:
            recommendations.append(
                f"Discrimination accuracy is {stats.discrimination_accuracy:.1%}. "
                "Consider adjusting discrimination weights or thresholds."
            )
        
        # Mode recommendations
        if stats.mode_accuracy < 0.8 and stats.mode_count > 50:
            recommendations.append(
                f"Mode identification accuracy is {stats.mode_accuracy:.1%}. "
                "Consider refining ionospheric model or layer heights."
            )
        
        if not recommendations:
            recommendations.append("All validation metrics are within acceptable ranges.")
        
        return recommendations


# =============================================================================
# HELPER CLASSES
# =============================================================================

class OnlineStatistics:
    """
    Welford's online algorithm for running statistics.
    
    Note: This is appropriate here because we're computing statistics
    of ERRORS which are assumed to be stationary (unlike D_clock itself).
    """
    
    def __init__(self):
        self.count = 0
        self.mean = 0.0
        self.M2 = 0.0  # Sum of squared differences from mean
        self._weight_sum = 0.0
    
    @property
    def variance(self) -> float:
        if self.count < 2:
            return 0.0
        return self.M2 / self.count
    
    @property
    def std_dev(self) -> float:
        return math.sqrt(self.variance)
    
    def update(self, value: float, weight: float = 1.0) -> None:
        """Update statistics with new value."""
        self.count += 1
        self._weight_sum += weight
        
        delta = value - self.mean
        self.mean += (weight / self._weight_sum) * delta if self._weight_sum > 0 else delta
        delta2 = value - self.mean
        self.M2 += weight * delta * delta2


# =============================================================================
# CONVENIENCE FUNCTIONS
# =============================================================================

_default_validator: Optional[GroundTruthValidator] = None


def get_validator(receiver_id: str = "default", data_dir: Optional[Path] = None) -> GroundTruthValidator:
    """Get or create the default ground truth validator."""
    global _default_validator
    if _default_validator is None or _default_validator.receiver_id != receiver_id:
        _default_validator = GroundTruthValidator(receiver_id=receiver_id, data_dir=data_dir)
    return _default_validator
