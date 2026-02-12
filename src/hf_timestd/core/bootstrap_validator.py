"""
Bootstrap Validator for RTP-to-UTC Offset Calibration

================================================================================
PURPOSE
================================================================================
Validates and refines the RTP-to-UTC offset using multi-station correlation
and station-specific confirmation. The GPSDO provides the "steel ruler" -
once we correctly identify the offset, it remains stable.

================================================================================
BOOTSTRAP PHILOSOPHY
================================================================================
1. System clock provides INITIAL GUESS for RTP-to-UTC offset (may be wrong)
2. Tone detection finds candidate minute markers
3. Multi-station correlation VALIDATES the offset:
   - WWVH must arrive later than WWV (on shared frequencies)
   - CHU timing must be consistent with geographic prediction
   - All stations must agree within propagation bounds
4. Station-specific features CONFIRM identity:
   - WWV: 1000 Hz tone, BCD time codes, minute 8 test signal
   - WWVH: 1200 Hz tone, minute 44 test signal
   - CHU: 1000 Hz tone, FSK time codes with decoded UTC
5. Once validated with high confidence, offset is LOCKED
6. GPSDO stability maintains alignment through propagation changes

================================================================================
KEY INSIGHT
================================================================================
The ~340ms offset error we observed was because:
- System clock was used to establish RTP-to-UTC offset
- Tone detector found a signal at that offset
- But it was the WRONG signal (possibly WWVH instead of WWV, or noise)
- Without multi-station correlation, the error was locked in

The fix is to require AGREEMENT between multiple stations before locking.

================================================================================
Author: Cascade AI
Date: 2026-01-24
================================================================================
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
import numpy as np
import json

from .wwv_constants import (
    PROPAGATION_BOUNDS_MS_BOOTSTRAP,
    UNAMBIGUOUS_BOOTSTRAP_CHANNELS,
    MAX_CALIBRATION_OFFSET_MS,
)

logger = logging.getLogger(__name__)


class BootstrapPhase(Enum):
    """Bootstrap phase tracking."""
    SEARCHING = "searching"      # Looking for initial offset candidates
    CORRELATING = "correlating"  # Validating with multi-station correlation
    CONFIRMING = "confirming"    # Confirming with station-specific features
    LOCKED = "locked"            # Offset validated and locked


@dataclass
class OffsetCandidate:
    """A candidate RTP-to-UTC offset with supporting evidence."""
    offset_sec: float                    # RTP-to-UTC offset in seconds
    source_channel: str                  # Channel that proposed this offset
    source_station: str                  # Station detected
    detection_time: float                # Unix timestamp of detection
    timing_error_ms: float               # Timing error at detection
    confidence: float                    # Detection confidence
    snr_db: float                        # Signal-to-noise ratio
    
    # Validation state
    n_confirmations: int = 0             # Number of confirming detections
    confirming_stations: List[str] = field(default_factory=list)
    confirming_channels: List[str] = field(default_factory=list)
    
    # Multi-station correlation
    wwv_wwvh_delay_valid: bool = False   # WWVH > WWV delay confirmed
    chu_timing_valid: bool = False       # CHU timing consistent
    geographic_valid: bool = False       # All delays within geographic bounds
    
    # Station-specific confirmation
    bcd_confirmed: bool = False          # WWV BCD time code decoded
    fsk_confirmed: bool = False          # CHU FSK time code decoded
    test_signal_confirmed: bool = False  # Test signal (min 8/44) detected
    tone_freq_confirmed: bool = False    # Tone frequency matches station


@dataclass
class StationTiming:
    """Timing measurement for a single station."""
    station: str
    channel: str
    frequency_mhz: float
    timing_error_ms: float               # Raw timing error from detection
    propagation_delay_ms: float          # Expected propagation delay
    corrected_timing_ms: float           # timing_error - propagation_delay
    confidence: float
    snr_db: float
    tone_frequency_hz: Optional[float] = None  # Detected tone frequency


class BootstrapValidator:
    """
    Validates RTP-to-UTC offset using multi-station correlation.
    
    Usage:
        validator = BootstrapValidator(receiver_lat, receiver_lon)
        
        # Feed detections from all channels
        for detection in detections:
            validator.add_detection(detection)
        
        # Check if offset is validated
        if validator.phase == BootstrapPhase.LOCKED:
            offset_correction = validator.get_offset_correction()
            # Apply correction to RTP-to-UTC offset
    """
    
    # Validation thresholds
    MIN_CONFIRMING_STATIONS = 2          # Need at least 2 stations to agree
    MIN_CONFIRMATIONS_PER_STATION = 3    # Need 3 detections per station
    MAX_INTER_STATION_ERROR_MS = 10.0    # Max disagreement between stations
    WWVH_WWV_MIN_DELAY_DIFF_MS = 5.0     # WWVH must be at least 5ms later than WWV
    
    # Geographic bounds (from receiver in Missouri)
    EXPECTED_DELAYS_MS = {
        'WWV': (5.0, 25.0),    # Colorado: 5-25ms typical
        'WWVH': (25.0, 60.0),  # Hawaii: 25-60ms typical
        'CHU': (5.0, 30.0),    # Ottawa: 5-30ms typical
        'BPM': (40.0, 80.0),   # China: 40-80ms typical (excluded from bootstrap)
    }
    
    def __init__(
        self,
        receiver_lat: float = 38.9,
        receiver_lon: float = -92.1,
        state_file: Optional[Path] = None
    ):
        self.receiver_lat = receiver_lat
        self.receiver_lon = receiver_lon
        self.state_file = state_file
        
        # Current phase
        self.phase = BootstrapPhase.SEARCHING
        
        # Offset candidates (may have multiple during search)
        self.candidates: List[OffsetCandidate] = []
        self.best_candidate: Optional[OffsetCandidate] = None
        
        # Per-station timing history (for correlation)
        self.station_timings: Dict[str, List[StationTiming]] = {
            'WWV': [], 'WWVH': [], 'CHU': [], 'BPM': []
        }
        
        # Validated offset (once locked)
        self.validated_offset_sec: Optional[float] = None
        self.validation_time: Optional[float] = None
        
        # Statistics
        self.stats = {
            'detections_processed': 0,
            'candidates_proposed': 0,
            'candidates_rejected': 0,
            'correlations_checked': 0,
            'correlations_passed': 0,
        }
        
        logger.info(f"BootstrapValidator initialized at ({receiver_lat:.4f}, {receiver_lon:.4f})")
    
    def add_detection(
        self,
        channel: str,
        station: str,
        frequency_mhz: float,
        timing_error_ms: float,
        confidence: float,
        snr_db: float,
        rtp_timestamp: int,
        sample_rate: int,
        minute_boundary: int,
        tone_frequency_hz: Optional[float] = None,
        propagation_delay_ms: Optional[float] = None
    ) -> Optional[float]:
        """
        Add a detection and check if it validates/refines the offset.
        
        Returns:
            Offset correction in seconds if validation achieved, None otherwise
        """
        self.stats['detections_processed'] += 1
        
        # Skip BPM - too distant for bootstrap
        if station == 'BPM':
            return None
        
        # Calculate expected propagation delay if not provided
        if propagation_delay_ms is None:
            propagation_delay_ms = self._get_expected_delay(station, frequency_mhz)
        
        # Create timing record
        corrected_timing = timing_error_ms - propagation_delay_ms
        timing = StationTiming(
            station=station,
            channel=channel,
            frequency_mhz=frequency_mhz,
            timing_error_ms=timing_error_ms,
            propagation_delay_ms=propagation_delay_ms,
            corrected_timing_ms=corrected_timing,
            confidence=confidence,
            snr_db=snr_db,
            tone_frequency_hz=tone_frequency_hz
        )
        
        # Add to history
        self.station_timings[station].append(timing)
        
        # Keep only recent history (last 30 per station)
        if len(self.station_timings[station]) > 30:
            self.station_timings[station] = self.station_timings[station][-30:]
        
        # Phase-specific processing
        if self.phase == BootstrapPhase.SEARCHING:
            return self._process_searching(timing, rtp_timestamp, sample_rate, minute_boundary)
        elif self.phase == BootstrapPhase.CORRELATING:
            return self._process_correlating(timing)
        elif self.phase == BootstrapPhase.CONFIRMING:
            return self._process_confirming(timing)
        else:
            # Already locked - no correction needed
            return None
    
    def _process_searching(
        self,
        timing: StationTiming,
        rtp_timestamp: int,
        sample_rate: int,
        minute_boundary: int
    ) -> Optional[float]:
        """
        Search phase: Look for initial offset candidates from unambiguous channels.
        """
        # Prefer unambiguous channels (CHU, WWV 20/25 MHz)
        # Check both formats: WWV_20.0 (MHz) and WWV_20000 (kHz)
        channel_key_mhz = f"{timing.station}_{timing.frequency_mhz:.2f}"
        channel_key_khz = f"{timing.station}_{int(timing.frequency_mhz * 1000)}"
        is_unambiguous = (channel_key_mhz in UNAMBIGUOUS_BOOTSTRAP_CHANNELS or
                         channel_key_khz in UNAMBIGUOUS_BOOTSTRAP_CHANNELS or
                         timing.station == 'CHU')  # All CHU frequencies are unambiguous
        
        # Only high-confidence detections from unambiguous channels
        if not is_unambiguous:
            logger.debug(f"Skipping ambiguous channel {channel_key_mhz} during search phase")
            return None
        
        if timing.confidence < 0.6:
            logger.debug(f"Skipping low-confidence detection ({timing.confidence:.2f}) during search")
            return None
        
        # Calculate the offset correction implied by this detection
        # If timing_error_ms is +100ms, the tone arrived 100ms "late"
        # This means our RTP-to-UTC offset is 100ms too low
        offset_correction_sec = timing.timing_error_ms / 1000.0
        
        # Check if this is within plausible bounds
        # During initial bootstrap, allow up to 1 second offset (system clock can be off)
        # After bootstrap, tighter bounds apply
        max_offset_sec = 1.0 if self.phase == BootstrapPhase.SEARCHING else 0.5
        if abs(offset_correction_sec) > max_offset_sec:
            logger.info(f"[BOOTSTRAP] Implausible offset {offset_correction_sec*1000:.1f}ms from {channel_key_mhz} (max={max_offset_sec*1000:.0f}ms)")
            return None
        
        # Create candidate
        candidate = OffsetCandidate(
            offset_sec=offset_correction_sec,
            source_channel=timing.channel,
            source_station=timing.station,
            detection_time=time.time(),
            timing_error_ms=timing.timing_error_ms,
            confidence=timing.confidence,
            snr_db=timing.snr_db,
            n_confirmations=1,
            confirming_stations=[timing.station],
            confirming_channels=[timing.channel]
        )
        
        self.candidates.append(candidate)
        self.stats['candidates_proposed'] += 1
        
        logger.info(
            f"[BOOTSTRAP] New offset candidate from {channel_key}: "
            f"correction={offset_correction_sec*1000:+.1f}ms, "
            f"confidence={timing.confidence:.2f}, SNR={timing.snr_db:.1f}dB"
        )
        
        # Check if we have enough candidates to start correlating
        if len(self.candidates) >= 3:
            self._transition_to_correlating()
        
        return None
    
    def _process_correlating(self, timing: StationTiming) -> Optional[float]:
        """
        Correlating phase: Validate candidates with multi-station correlation.
        """
        if not self.best_candidate:
            # Find best candidate based on confirmations
            if self.candidates:
                self.best_candidate = max(self.candidates, key=lambda c: c.n_confirmations)
        
        if not self.best_candidate:
            return None
        
        # Check if this detection confirms the best candidate
        expected_timing = self.best_candidate.offset_sec * 1000  # Convert to ms
        actual_timing = timing.timing_error_ms
        error = abs(actual_timing - expected_timing)
        
        if error < self.MAX_INTER_STATION_ERROR_MS:
            # This detection confirms the candidate
            self.best_candidate.n_confirmations += 1
            if timing.station not in self.best_candidate.confirming_stations:
                self.best_candidate.confirming_stations.append(timing.station)
            if timing.channel not in self.best_candidate.confirming_channels:
                self.best_candidate.confirming_channels.append(timing.channel)
            
            logger.info(
                f"[BOOTSTRAP] Candidate confirmed by {timing.station}: "
                f"error={error:.1f}ms, confirmations={self.best_candidate.n_confirmations}, "
                f"stations={self.best_candidate.confirming_stations}"
            )
        else:
            logger.debug(
                f"[BOOTSTRAP] Detection from {timing.station} does not confirm candidate: "
                f"error={error:.1f}ms > threshold={self.MAX_INTER_STATION_ERROR_MS}ms"
            )
        
        # Check multi-station correlation
        self._check_multi_station_correlation()
        
        # Check if we can transition to confirming
        if (len(self.best_candidate.confirming_stations) >= self.MIN_CONFIRMING_STATIONS and
            self.best_candidate.n_confirmations >= self.MIN_CONFIRMATIONS_PER_STATION * 2):
            self._transition_to_confirming()
        
        return None
    
    def _process_confirming(self, timing: StationTiming) -> Optional[float]:
        """
        Confirming phase: Verify with station-specific features.
        """
        if not self.best_candidate:
            return None
        
        # Check tone frequency confirmation
        if timing.tone_frequency_hz is not None:
            if timing.station in ('WWV', 'CHU') and 990 <= timing.tone_frequency_hz <= 1010:
                self.best_candidate.tone_freq_confirmed = True
                logger.info(f"[BOOTSTRAP] Tone frequency confirmed for {timing.station}: {timing.tone_frequency_hz:.1f}Hz")
            elif timing.station == 'WWVH' and 1190 <= timing.tone_frequency_hz <= 1210:
                self.best_candidate.tone_freq_confirmed = True
                logger.info(f"[BOOTSTRAP] Tone frequency confirmed for WWVH: {timing.tone_frequency_hz:.1f}Hz")
        
        # Check if we have enough confirmation to lock
        if self._can_lock():
            return self._lock_offset()
        
        return None
    
    def _check_multi_station_correlation(self):
        """
        Check multi-station correlation for the best candidate.
        """
        if not self.best_candidate:
            return
        
        self.stats['correlations_checked'] += 1
        
        # Check WWV vs WWVH delay ordering (on shared frequencies)
        wwv_timings = [t for t in self.station_timings['WWV'] if t.confidence > 0.5]
        wwvh_timings = [t for t in self.station_timings['WWVH'] if t.confidence > 0.5]
        
        if wwv_timings and wwvh_timings:
            # Get recent timing errors
            wwv_mean = np.mean([t.timing_error_ms for t in wwv_timings[-5:]])
            wwvh_mean = np.mean([t.timing_error_ms for t in wwvh_timings[-5:]])
            
            # WWVH should arrive later than WWV (longer path)
            delay_diff = wwvh_mean - wwv_mean
            if delay_diff >= self.WWVH_WWV_MIN_DELAY_DIFF_MS:
                self.best_candidate.wwv_wwvh_delay_valid = True
                logger.info(
                    f"[BOOTSTRAP] WWV-WWVH delay ordering VALID: "
                    f"WWVH arrives {delay_diff:.1f}ms after WWV"
                )
            else:
                logger.warning(
                    f"[BOOTSTRAP] WWV-WWVH delay ordering INVALID: "
                    f"diff={delay_diff:.1f}ms (expected >= {self.WWVH_WWV_MIN_DELAY_DIFF_MS}ms)"
                )
        
        # Check CHU timing consistency
        chu_timings = [t for t in self.station_timings['CHU'] if t.confidence > 0.5]
        if chu_timings:
            chu_mean = np.mean([t.corrected_timing_ms for t in chu_timings[-5:]])
            chu_std = np.std([t.corrected_timing_ms for t in chu_timings[-5:]]) if len(chu_timings) > 1 else 0
            
            # CHU corrected timing should be near zero (after propagation correction)
            if abs(chu_mean) < 20.0 and chu_std < 10.0:
                self.best_candidate.chu_timing_valid = True
                logger.info(
                    f"[BOOTSTRAP] CHU timing VALID: "
                    f"corrected={chu_mean:.1f}ms ± {chu_std:.1f}ms"
                )
        
        # Check geographic bounds for all stations
        all_valid = True
        for station, timings in self.station_timings.items():
            if station == 'BPM' or not timings:
                continue
            
            recent = [t for t in timings[-5:] if t.confidence > 0.5]
            if not recent:
                continue
            
            mean_delay = np.mean([t.timing_error_ms for t in recent])
            bounds = self.EXPECTED_DELAYS_MS.get(station, (0, 100))
            
            if not (bounds[0] <= mean_delay <= bounds[1]):
                all_valid = False
                logger.warning(
                    f"[BOOTSTRAP] {station} delay {mean_delay:.1f}ms outside bounds {bounds}"
                )
        
        if all_valid:
            self.best_candidate.geographic_valid = True
            self.stats['correlations_passed'] += 1
    
    def _transition_to_correlating(self):
        """Transition from searching to correlating phase."""
        self.phase = BootstrapPhase.CORRELATING
        
        # Find best candidate
        if self.candidates:
            # Group candidates by similar offset (within 20ms)
            groups: Dict[int, List[OffsetCandidate]] = {}
            for c in self.candidates:
                key = int(c.offset_sec * 50)  # 20ms bins
                if key not in groups:
                    groups[key] = []
                groups[key].append(c)
            
            # Find largest group
            largest_group = max(groups.values(), key=len)
            self.best_candidate = max(largest_group, key=lambda c: c.confidence)
            
            logger.info(
                f"[BOOTSTRAP] Transitioning to CORRELATING phase. "
                f"Best candidate: offset={self.best_candidate.offset_sec*1000:+.1f}ms "
                f"from {self.best_candidate.source_channel}"
            )
    
    def _transition_to_confirming(self):
        """Transition from correlating to confirming phase."""
        self.phase = BootstrapPhase.CONFIRMING
        logger.info(
            f"[BOOTSTRAP] Transitioning to CONFIRMING phase. "
            f"Candidate has {len(self.best_candidate.confirming_stations)} confirming stations"
        )
    
    def _can_lock(self) -> bool:
        """Check if we have enough evidence to lock the offset."""
        if not self.best_candidate:
            return False
        
        c = self.best_candidate
        
        # Require at least 2 confirming stations
        if len(c.confirming_stations) < self.MIN_CONFIRMING_STATIONS:
            return False
        
        # Require geographic validation
        if not c.geographic_valid:
            return False
        
        # Require at least one of: WWV-WWVH ordering, CHU timing, or tone frequency
        confirmations = [c.wwv_wwvh_delay_valid, c.chu_timing_valid, c.tone_freq_confirmed]
        if not any(confirmations):
            return False
        
        return True
    
    def _lock_offset(self) -> float:
        """Lock the validated offset and return the correction."""
        self.phase = BootstrapPhase.LOCKED
        self.validated_offset_sec = self.best_candidate.offset_sec
        self.validation_time = time.time()
        
        logger.info("=" * 80)
        logger.info("🎯 BOOTSTRAP OFFSET LOCKED")
        logger.info("=" * 80)
        logger.info(f"Offset correction: {self.validated_offset_sec*1000:+.1f}ms")
        logger.info(f"Confirming stations: {self.best_candidate.confirming_stations}")
        logger.info(f"Confirmations: {self.best_candidate.n_confirmations}")
        logger.info(f"WWV-WWVH delay valid: {self.best_candidate.wwv_wwvh_delay_valid}")
        logger.info(f"CHU timing valid: {self.best_candidate.chu_timing_valid}")
        logger.info(f"Geographic valid: {self.best_candidate.geographic_valid}")
        logger.info(f"Tone frequency confirmed: {self.best_candidate.tone_freq_confirmed}")
        logger.info("=" * 80)
        
        return self.validated_offset_sec
    
    def _get_expected_delay(self, station: str, frequency_mhz: float) -> float:
        """Get expected propagation delay for a station.
        
        Uses HFPropagationModel when available for physics-based prediction.
        Falls back to static midpoint of geographic bounds.
        """
        if not hasattr(self, '_prop_model'):
            self._prop_model = None
            try:
                from .propagation_model import HFPropagationModel
                self._prop_model = HFPropagationModel(
                    receiver_lat=self.receiver_lat,
                    receiver_lon=self.receiver_lon,
                    enable_realtime=False  # Don't need real-time during bootstrap
                )
            except Exception:
                pass
        
        if self._prop_model is not None:
            try:
                from datetime import datetime, timezone
                prediction = self._prop_model.predict(
                    station, frequency_mhz, datetime.now(timezone.utc)
                )
                if prediction.primary_delay_ms > 0:
                    return prediction.primary_delay_ms
            except Exception:
                pass
        
        # Static fallback
        bounds = self.EXPECTED_DELAYS_MS.get(station, (10, 50))
        return (bounds[0] + bounds[1]) / 2
    
    def get_offset_correction(self) -> Optional[float]:
        """Get the validated offset correction in seconds."""
        if self.phase == BootstrapPhase.LOCKED:
            return self.validated_offset_sec
        return None
    
    def get_status(self) -> Dict[str, Any]:
        """Get current bootstrap status."""
        return {
            'phase': self.phase.value,
            'n_candidates': len(self.candidates),
            'best_candidate_offset_ms': self.best_candidate.offset_sec * 1000 if self.best_candidate else None,
            'confirming_stations': self.best_candidate.confirming_stations if self.best_candidate else [],
            'validated_offset_ms': self.validated_offset_sec * 1000 if self.validated_offset_sec else None,
            'stats': self.stats
        }
