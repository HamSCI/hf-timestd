"""
Timing Bootstrap: Broadcast-Driven RTP-to-UTC Calibration

This module implements a bootstrap algorithm that discovers the RTP-to-UTC
correspondence from the broadcasts themselves, without relying on system
time accuracy.

Architecture:
------------
ACQUIRING → CORRELATING → TRACKING
    ↑______________|___________|
         (retreat on errors)

ACQUIRING: Full-buffer cross-correlation to find any tones
CORRELATING: Validate candidates using relative timing and discriminating features
TRACKING: Narrow-window detection around predicted positions

Key Insight:
-----------
Time station tones are emitted exactly on UTC minute boundaries. The arrival
pattern itself IS the clock reference. We validate using:
- Relative timing: WWVH always arrives after WWV on shared frequencies
- Minute spacing: Consecutive tones are exactly 1,440,000 samples apart
- Discriminating features: 1000/1200 Hz, tone duration, voice timing, tone schedule

Discriminating Features:
-----------------------
1. Tone frequency: WWV=1000Hz, WWVH=1200Hz (minute marker)
2. Tone schedule: 500/600 Hz tones follow known per-minute pattern
3. Test signals: WWV minute 8, WWVH minute 44
4. Voice announcements: WWV male voice, WWVH female voice
5. BCD time code: WWV/WWVH encode time in 100Hz subcarrier

Geographic Priors (receiver-dependent):
--------------------------------------
The expected delay difference between WWV and WWVH depends on receiver location.
For a receiver in Missouri (~38.9°N, 92.1°W):
- WWV (Colorado): ~1100 km, ~4-12ms propagation
- WWVH (Hawaii): ~5500 km, ~18-40ms propagation
- Expected WWVH-WWV delay: ~15-30ms

Unambiguous Channels:
--------------------
- CHU (3.33, 7.85, 14.67 MHz): Only CHU transmits
- WWV 20 MHz, 25 MHz: Only WWV transmits (WWVH doesn't use these)
"""

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple, Set
from math import radians, sin, cos, sqrt, atan2

logger = logging.getLogger(__name__)

# Constants
SAMPLE_RATE = 24000
SAMPLES_PER_MINUTE = SAMPLE_RATE * 60  # 1,440,000

# Station locations (lat, lon in degrees)
STATION_LOCATIONS = {
    'CHU': (45.2975, -75.7533),    # Ottawa, Canada
    'WWV': (40.6781, -105.0469),   # Fort Collins, Colorado
    'WWVH': (21.9886, -159.7642),  # Kekaha, Hawaii
    'BPM': (34.95, 109.55),        # Pucheng, China
}

# Speed of light for propagation delay calculation
SPEED_OF_LIGHT_KM_S = 299792.458

# Ionospheric path factor (great circle distance * factor = actual path)
# Typical range: 1.0 (ground wave) to 1.5+ (high angle skywave)
IONOSPHERIC_PATH_FACTOR = 1.15  # Conservative estimate

# =============================================================================
# DISCRIMINATING FEATURES - Station Identification
# =============================================================================

# Minutes where only WWV broadcasts 500/600 Hz tones (WWVH silent or 440 Hz)
WWV_ONLY_TONE_MINUTES: Set[int] = {1, 16, 17, 19}

# Minutes where only WWVH broadcasts 500/600 Hz tones (WWV silent or 440 Hz)
WWVH_ONLY_TONE_MINUTES: Set[int] = {2, 43, 44, 45, 46, 47, 48, 49, 50, 51}

# Test signal minutes (exclusive broadcast, other station silent)
WWV_TEST_SIGNAL_MINUTE = 8
WWVH_TEST_SIGNAL_MINUTE = 44

# Voice announcement timing (seconds within minute)
# WWV: Male voice announces time at seconds 52-59
# WWVH: Female voice announces time at seconds 45-52
WWV_VOICE_START_SEC = 52
WWV_VOICE_END_SEC = 59
WWVH_VOICE_START_SEC = 45
WWVH_VOICE_END_SEC = 52

# Minute marker tone characteristics
TONE_CHARACTERISTICS = {
    'WWV': {'frequency_hz': 1000, 'duration_ms': 800},
    'WWVH': {'frequency_hz': 1200, 'duration_ms': 800},
    'CHU': {'frequency_hz': 1000, 'duration_ms': 500},  # 1000ms at top of hour
    'BPM': {'frequency_hz': 1000, 'duration_ms': 300},
}

# Shared frequencies where WWV and WWVH both transmit
SHARED_FREQUENCIES_KHZ: Set[int] = {2500, 5000, 10000, 15000}

# Unambiguous channels (only one station transmits)
UNAMBIGUOUS_CHANNELS: Dict[str, str] = {
    'CHU_3330': 'CHU',
    'CHU_7850': 'CHU',
    'CHU_14670': 'CHU',
    'WWV_20000': 'WWV',
    'WWV_25000': 'WWV',
}


class BootstrapState(Enum):
    """Bootstrap state machine states."""
    ACQUIRING = "acquiring"      # Full-buffer search for any tones
    CORRELATING = "correlating"  # Validating candidates across channels
    TRACKING = "tracking"        # Narrow-window tracking with known offset
    LOCKED = "locked"           # Offset confirmed, high confidence


@dataclass
class AcquisitionCandidate:
    """A tone candidate from acquisition mode."""
    channel: str                 # e.g., "CHU_14670", "WWV_10000"
    station: str                 # Identified station (WWV, WWVH, CHU, BPM)
    frequency_khz: int           # Broadcast frequency
    tone_frequency_hz: float     # Tone frequency (1000 or 1200 Hz)
    rtp_timestamp: int           # RTP sample number at tone onset
    sample_position: int         # Position within buffer
    snr_db: float               # Signal-to-noise ratio
    confidence: float           # Detection confidence (0-1)
    buffer_rtp_start: int       # RTP at start of buffer


@dataclass
class ValidatedTone:
    """A tone that has passed validation checks."""
    candidate: AcquisitionCandidate
    minute_index: int            # Which minute (0 = first)
    validation_score: float      # How well it matches expectations
    is_unambiguous: bool        # From unambiguous channel?


@dataclass
class TimingBootstrap:
    """
    Bootstrap state machine for discovering RTP-to-UTC correspondence.
    
    Usage:
        bootstrap = TimingBootstrap(receiver_lat=38.9, receiver_lon=-92.1)
        
        # During acquisition phase:
        candidates = tone_detector.acquire_tones(samples, buffer_rtp_start)
        for c in candidates:
            bootstrap.add_candidate(channel, c.station, ...)
        
        # Check state
        if bootstrap.state == BootstrapState.LOCKED:
            offset = bootstrap.get_rtp_to_utc_offset()
    """
    receiver_lat: float
    receiver_lon: float
    sample_rate: int = SAMPLE_RATE
    
    # State
    state: BootstrapState = field(default=BootstrapState.ACQUIRING)
    
    # Reference point (first validated tone on unambiguous channel)
    reference_rtp: Optional[int] = None
    reference_channel: Optional[str] = None
    reference_station: Optional[str] = None
    
    # Collected candidates per minute
    candidates_by_minute: Dict[int, List[AcquisitionCandidate]] = field(default_factory=dict)
    validated_tones: List[ValidatedTone] = field(default_factory=list)
    
    # Derived offset
    rtp_to_utc_offset_samples: Optional[int] = None  # RTP sample at UTC minute 0
    offset_uncertainty_samples: int = 0
    
    # Tracking
    minutes_observed: int = 0
    consecutive_validations: int = 0
    consecutive_failures: int = 0
    
    # Geographic expectations (computed in __post_init__)
    station_expectations: Dict[str, dict] = field(default_factory=dict)
    
    def __post_init__(self):
        """Compute geographic expectations for each station."""
        for station, (lat, lon) in STATION_LOCATIONS.items():
            distance_km = self._haversine_km(
                self.receiver_lat, self.receiver_lon, lat, lon
            )
            # Propagation delay estimate (with ionospheric factor)
            path_km = distance_km * IONOSPHERIC_PATH_FACTOR
            delay_ms = (path_km / SPEED_OF_LIGHT_KM_S) * 1000
            delay_samples = int(delay_ms * self.sample_rate / 1000)
            
            self.station_expectations[station] = {
                'distance_km': distance_km,
                'path_km': path_km,
                'delay_ms': delay_ms,
                'delay_samples': delay_samples,
                # Uncertainty bounds (±50% for ionospheric variability)
                'delay_min_ms': delay_ms * 0.8,
                'delay_max_ms': delay_ms * 1.5,
            }
            
        logger.info(f"[BOOTSTRAP] Geographic expectations computed for receiver at "
                   f"({self.receiver_lat:.2f}, {self.receiver_lon:.2f})")
        for station, exp in self.station_expectations.items():
            logger.info(f"  {station}: {exp['distance_km']:.0f}km, "
                       f"delay={exp['delay_ms']:.1f}ms [{exp['delay_min_ms']:.1f}-{exp['delay_max_ms']:.1f}]")
    
    def _haversine_km(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Calculate great-circle distance between two points."""
        R = 6371.0  # Earth radius in km
        
        lat1_r, lon1_r = radians(lat1), radians(lon1)
        lat2_r, lon2_r = radians(lat2), radians(lon2)
        
        dlat = lat2_r - lat1_r
        dlon = lon2_r - lon1_r
        
        a = sin(dlat/2)**2 + cos(lat1_r) * cos(lat2_r) * sin(dlon/2)**2
        c = 2 * atan2(sqrt(a), sqrt(1-a))
        
        return R * c
    
    def add_candidate(
        self,
        channel: str,
        station: str,
        frequency_khz: int,
        tone_frequency_hz: float,
        rtp_timestamp: int,
        sample_position: int,
        snr_db: float,
        confidence: float,
        buffer_rtp_start: int,
        system_time_hint: Optional[float] = None
    ) -> Optional[str]:
        """
        Add an acquisition candidate and process through state machine.
        
        Args:
            system_time_hint: Optional system time of buffer start. Used to identify
                             which candidate is likely the minute marker (closest to
                             a minute boundary). Not required but speeds up bootstrap.
        
        Returns:
            Status message or None
        """
        candidate = AcquisitionCandidate(
            channel=channel,
            station=station,
            frequency_khz=frequency_khz,
            tone_frequency_hz=tone_frequency_hz,
            rtp_timestamp=rtp_timestamp,
            sample_position=sample_position,
            snr_db=snr_db,
            confidence=confidence,
            buffer_rtp_start=buffer_rtp_start
        )
        
        # If system time hint provided, check if this candidate is near a minute boundary
        # This helps filter out per-second ticks and focus on minute markers
        if system_time_hint is not None:
            # Calculate time of this tone
            tone_time = system_time_hint + (sample_position / self.sample_rate)
            # Distance to nearest minute boundary
            seconds_in_minute = tone_time % 60
            dist_to_minute = min(seconds_in_minute, 60 - seconds_in_minute)
            
            # If more than 1 second from minute boundary, likely a per-second tick
            # Skip unless we have no reference yet and this is high quality
            if dist_to_minute > 1.0:
                if self.reference_rtp is not None or snr_db < 15:
                    logger.debug(f"[BOOTSTRAP] Skipping candidate {dist_to_minute:.1f}s from minute boundary")
                    return None
        
        # Determine minute index based on reference
        if self.reference_rtp is None:
            minute_index = 0
        else:
            samples_from_ref = rtp_timestamp - self.reference_rtp
            # Round to nearest minute, but be tolerant of propagation delay variance
            minute_index = round(samples_from_ref / SAMPLES_PER_MINUTE)
            
            # If this candidate is BEFORE the reference (negative minute index),
            # it might be a better reference. In CORRELATING state, we should
            # consider re-establishing reference if this is earlier and high quality.
            if minute_index < 0 and self.state == BootstrapState.CORRELATING:
                if snr_db > 15 and confidence > 0.7:
                    # This is an earlier, high-quality tone - could be better reference
                    # But only if it's within the same buffer (same minute)
                    if abs(samples_from_ref) < SAMPLES_PER_MINUTE:
                        logger.debug(f"[BOOTSTRAP] Ignoring earlier tone in same minute "
                                    f"(offset {samples_from_ref} samples)")
                        return None
        
        # Store candidate
        if minute_index not in self.candidates_by_minute:
            self.candidates_by_minute[minute_index] = []
        self.candidates_by_minute[minute_index].append(candidate)
        
        # Process based on state
        if self.state == BootstrapState.ACQUIRING:
            return self._handle_acquiring(candidate, minute_index)
        elif self.state == BootstrapState.CORRELATING:
            return self._handle_correlating(candidate, minute_index)
        elif self.state == BootstrapState.TRACKING:
            return self._handle_tracking(candidate, minute_index)
        else:
            return None  # LOCKED - no action needed
    
    def _is_unambiguous_channel(self, channel: str, station: str) -> bool:
        """Check if this is an unambiguous channel (only one station transmits)."""
        # CHU frequencies
        if 'CHU' in channel.upper():
            return True
        # WWV 20 and 25 MHz (WWVH doesn't transmit here)
        if station == 'WWV':
            freq_khz = self._extract_frequency_khz(channel)
            if freq_khz in [20000, 25000]:
                return True
        return False
    
    def _extract_frequency_khz(self, channel: str) -> int:
        """Extract frequency in kHz from channel name."""
        import re
        match = re.search(r'(\d+)$', channel)
        if match:
            return int(match.group(1))
        return 0
    
    def _handle_acquiring(
        self, 
        candidate: AcquisitionCandidate, 
        minute_index: int
    ) -> Optional[str]:
        """
        ACQUIRING state: Looking for first high-confidence tone on unambiguous channel.
        """
        # Only accept high-confidence detections
        if candidate.confidence < 0.7 or candidate.snr_db < 12:
            return None
        
        # Prefer unambiguous channels for initial reference
        is_unambiguous = self._is_unambiguous_channel(
            candidate.channel, candidate.station
        )
        
        if is_unambiguous:
            # Establish reference
            self.reference_rtp = candidate.rtp_timestamp
            self.reference_channel = candidate.channel
            self.reference_station = candidate.station
            
            # Record as validated
            self.validated_tones.append(ValidatedTone(
                candidate=candidate,
                minute_index=0,
                validation_score=1.0,
                is_unambiguous=True
            ))
            
            # Transition to CORRELATING
            self.state = BootstrapState.CORRELATING
            self.minutes_observed = 1
            
            logger.info(f"[BOOTSTRAP] Reference established: {candidate.station} on "
                       f"{candidate.channel}, RTP={candidate.rtp_timestamp}, "
                       f"SNR={candidate.snr_db:.1f}dB → CORRELATING")
            
            return f"Reference: {candidate.station}@{candidate.channel}"
        
        # On shared frequency, need to validate WWV vs WWVH ordering
        # Store candidate but don't establish reference yet
        logger.debug(f"[BOOTSTRAP] Candidate on shared channel {candidate.channel}, "
                    f"waiting for unambiguous reference")
        return None
    
    def _handle_correlating(
        self,
        candidate: AcquisitionCandidate,
        minute_index: int
    ) -> Optional[str]:
        """
        CORRELATING state: Validate candidates match expected pattern.
        
        Checks:
        1. Minute spacing: ~1,440,000 samples from reference
        2. Geographic ordering: WWVH after WWV on shared frequencies
        3. Propagation delay within expected bounds
        
        Note: Multiple tones may be detected in the same buffer (per-second ticks).
        We only validate the MINUTE marker tone, not every tick.
        """
        if self.reference_rtp is None:
            return None
        
        # For minute index 0, we may have multiple candidates from the same buffer
        # These are per-second ticks, not minute markers. Only validate if this
        # candidate is close to the reference (same minute marker).
        if minute_index == 0:
            samples_from_ref = abs(candidate.rtp_timestamp - self.reference_rtp)
            # If within 1 second of reference, it's likely the same minute marker
            # or a per-second tick - skip validation
            if samples_from_ref < self.sample_rate:
                # Same tone or very close - already validated
                return None
            # If more than 1 second but less than 59 seconds, it's a per-second tick
            # These are not minute markers - skip
            if samples_from_ref < 59 * self.sample_rate:
                logger.debug(f"[BOOTSTRAP] Skipping per-second tick at offset "
                            f"{samples_from_ref/self.sample_rate:.1f}s from reference")
                return None
        
        # Check minute spacing for actual minute markers (minute_index > 0)
        expected_rtp = self.reference_rtp + (minute_index * SAMPLES_PER_MINUTE)
        actual_rtp = candidate.rtp_timestamp
        spacing_error_samples = abs(actual_rtp - expected_rtp)
        spacing_error_ms = spacing_error_samples * 1000 / self.sample_rate
        
        # Allow up to 100ms spacing error (propagation variability)
        if spacing_error_ms > 100:
            logger.warning(f"[BOOTSTRAP] Minute spacing error {spacing_error_ms:.1f}ms "
                          f"for {candidate.station} minute {minute_index}")
            self.consecutive_failures += 1
            if self.consecutive_failures >= 3:
                self._retreat_to_acquiring("Too many spacing errors")
            return None
        
        # Validate geographic ordering on shared frequencies
        freq_khz = candidate.frequency_khz
        if freq_khz in [2500, 5000, 10000, 15000]:
            # Check if we have both WWV and WWVH for this minute
            minute_candidates = self.candidates_by_minute.get(minute_index, [])
            wwv_candidates = [c for c in minute_candidates 
                            if c.station == 'WWV' and c.frequency_khz == freq_khz]
            wwvh_candidates = [c for c in minute_candidates 
                             if c.station == 'WWVH' and c.frequency_khz == freq_khz]
            
            if wwv_candidates and wwvh_candidates:
                wwv_rtp = min(c.rtp_timestamp for c in wwv_candidates)
                wwvh_rtp = min(c.rtp_timestamp for c in wwvh_candidates)
                
                # WWVH must arrive AFTER WWV
                if wwvh_rtp <= wwv_rtp:
                    logger.warning(f"[BOOTSTRAP] Geographic violation: WWVH arrived "
                                  f"before/with WWV on {freq_khz}kHz")
                    self.consecutive_failures += 1
                    if self.consecutive_failures >= 3:
                        self._retreat_to_acquiring("Geographic ordering violated")
                    return None
                
                # Check delay difference is reasonable
                delay_diff_samples = wwvh_rtp - wwv_rtp
                delay_diff_ms = delay_diff_samples * 1000 / self.sample_rate
                
                # Expected delay difference from geographic priors
                wwv_delay = self.station_expectations['WWV']['delay_ms']
                wwvh_delay = self.station_expectations['WWVH']['delay_ms']
                expected_diff = wwvh_delay - wwv_delay
                
                # Allow 2x tolerance for ionospheric variability
                if delay_diff_ms < expected_diff * 0.3 or delay_diff_ms > expected_diff * 3.0:
                    logger.warning(f"[BOOTSTRAP] Delay difference {delay_diff_ms:.1f}ms "
                                  f"outside expected range (~{expected_diff:.1f}ms)")
                    # Don't fail hard on this, just note it
        
        # Validation passed
        is_unambiguous = self._is_unambiguous_channel(candidate.channel, candidate.station)
        
        self.validated_tones.append(ValidatedTone(
            candidate=candidate,
            minute_index=minute_index,
            validation_score=candidate.confidence,
            is_unambiguous=is_unambiguous
        ))
        
        self.consecutive_failures = 0
        self.consecutive_validations += 1
        
        # Check if ready to transition to TRACKING
        if minute_index > self.minutes_observed:
            self.minutes_observed = minute_index + 1
        
        # Need at least 3 minutes and 5 validated tones to lock
        unambiguous_count = sum(1 for t in self.validated_tones if t.is_unambiguous)
        
        if self.minutes_observed >= 3 and unambiguous_count >= 3:
            self._compute_offset()
            self.state = BootstrapState.TRACKING
            logger.info(f"[BOOTSTRAP] Validated {len(self.validated_tones)} tones over "
                       f"{self.minutes_observed} minutes → TRACKING")
            return "TRACKING"
        
        return f"Validated: {candidate.station} minute {minute_index}"
    
    def _handle_tracking(
        self,
        candidate: AcquisitionCandidate,
        minute_index: int
    ) -> Optional[str]:
        """
        TRACKING state: Continue validation, can transition to LOCKED or retreat.
        """
        # Similar validation as CORRELATING but stricter
        if self.rtp_to_utc_offset_samples is None:
            return None
        
        # Predict expected RTP for this minute
        expected_rtp = self.rtp_to_utc_offset_samples + (minute_index * SAMPLES_PER_MINUTE)
        
        # Add expected propagation delay for this station
        station_delay_samples = self.station_expectations.get(
            candidate.station, {}
        ).get('delay_samples', 0)
        expected_rtp += station_delay_samples
        
        actual_rtp = candidate.rtp_timestamp
        error_samples = actual_rtp - expected_rtp
        error_ms = error_samples * 1000 / self.sample_rate
        
        # Tighter tolerance in tracking mode
        if abs(error_ms) > 50:
            logger.warning(f"[BOOTSTRAP] Tracking error {error_ms:.1f}ms for "
                          f"{candidate.station}, expected RTP={expected_rtp}")
            self.consecutive_failures += 1
            if self.consecutive_failures >= 5:
                self._retreat_to_acquiring("Tracking errors exceeded threshold")
            return None
        
        # Good tracking
        self.consecutive_failures = 0
        self.consecutive_validations += 1
        
        is_unambiguous = self._is_unambiguous_channel(candidate.channel, candidate.station)
        self.validated_tones.append(ValidatedTone(
            candidate=candidate,
            minute_index=minute_index,
            validation_score=candidate.confidence,
            is_unambiguous=is_unambiguous
        ))
        
        # Transition to LOCKED after sustained tracking
        if self.consecutive_validations >= 10 and self.minutes_observed >= 5:
            self.state = BootstrapState.LOCKED
            logger.info(f"[BOOTSTRAP] Offset LOCKED after {self.minutes_observed} minutes, "
                       f"{len(self.validated_tones)} validated tones")
            return "LOCKED"
        
        if minute_index > self.minutes_observed:
            self.minutes_observed = minute_index + 1
        
        return f"Tracking: {candidate.station} error={error_ms:+.1f}ms"
    
    def _compute_offset(self):
        """
        Compute RTP-to-UTC offset from validated tones.
        
        The offset is the RTP sample number that corresponds to UTC minute 0.
        For each validated tone:
            RTP_of_UTC_minute = tone_rtp - propagation_delay_samples
        
        We average across all validated tones, weighted by confidence.
        """
        if not self.validated_tones:
            return
        
        offsets = []
        weights = []
        
        for vt in self.validated_tones:
            c = vt.candidate
            
            # Get propagation delay for this station
            delay_samples = self.station_expectations.get(
                c.station, {}
            ).get('delay_samples', 0)
            
            # RTP of UTC minute boundary = tone RTP - propagation delay
            # Then normalize to minute 0
            minute_rtp = c.rtp_timestamp - delay_samples
            minute_0_rtp = minute_rtp - (vt.minute_index * SAMPLES_PER_MINUTE)
            
            offsets.append(minute_0_rtp)
            weights.append(c.confidence * (1.5 if vt.is_unambiguous else 1.0))
        
        # Weighted average
        total_weight = sum(weights)
        if total_weight > 0:
            weighted_offset = sum(o * w for o, w in zip(offsets, weights)) / total_weight
            self.rtp_to_utc_offset_samples = int(round(weighted_offset))
            
            # Compute uncertainty (weighted std)
            variance = sum(w * (o - weighted_offset)**2 for o, w in zip(offsets, weights)) / total_weight
            self.offset_uncertainty_samples = int(sqrt(variance)) if variance > 0 else 0
            
            offset_ms = self.rtp_to_utc_offset_samples * 1000 / self.sample_rate
            uncertainty_ms = self.offset_uncertainty_samples * 1000 / self.sample_rate
            
            logger.info(f"[BOOTSTRAP] Computed offset: {self.rtp_to_utc_offset_samples} samples "
                       f"({offset_ms:.1f}ms), uncertainty={uncertainty_ms:.1f}ms")
    
    def _retreat_to_acquiring(self, reason: str):
        """Retreat to ACQUIRING state due to validation failures."""
        logger.warning(f"[BOOTSTRAP] Retreating to ACQUIRING: {reason}")
        
        self.state = BootstrapState.ACQUIRING
        self.reference_rtp = None
        self.reference_channel = None
        self.reference_station = None
        self.candidates_by_minute.clear()
        self.validated_tones.clear()
        self.rtp_to_utc_offset_samples = None
        self.consecutive_validations = 0
        self.consecutive_failures = 0
        self.minutes_observed = 0
    
    def get_rtp_to_utc_offset(self) -> Optional[Tuple[int, int]]:
        """
        Get the RTP-to-UTC offset if available.
        
        Returns:
            Tuple of (offset_samples, uncertainty_samples) or None if not locked
        """
        if self.rtp_to_utc_offset_samples is not None:
            return (self.rtp_to_utc_offset_samples, self.offset_uncertainty_samples)
        return None
    
    def get_predicted_tone_rtp(self, minute_index: int, station: str) -> Optional[int]:
        """
        Predict the RTP timestamp for a tone from a specific station and minute.
        
        Args:
            minute_index: Which minute (0 = reference minute)
            station: Station name (WWV, WWVH, CHU, BPM)
            
        Returns:
            Predicted RTP timestamp or None if offset not established
        """
        if self.rtp_to_utc_offset_samples is None:
            return None
        
        # Base RTP for this minute
        minute_rtp = self.rtp_to_utc_offset_samples + (minute_index * SAMPLES_PER_MINUTE)
        
        # Add propagation delay
        delay_samples = self.station_expectations.get(station, {}).get('delay_samples', 0)
        
        return minute_rtp + delay_samples
    
    def get_status(self) -> dict:
        """Get current bootstrap status."""
        return {
            'state': self.state.value,
            'minutes_observed': self.minutes_observed,
            'validated_tones': len(self.validated_tones),
            'consecutive_validations': self.consecutive_validations,
            'consecutive_failures': self.consecutive_failures,
            'reference_channel': self.reference_channel,
            'reference_station': self.reference_station,
            'offset_samples': self.rtp_to_utc_offset_samples,
            'offset_uncertainty_samples': self.offset_uncertainty_samples,
        }
    
    def establish_offset_from_metadata(
        self,
        buffer_rtp_start: int,
        buffer_system_time: float,
        channel: str
    ) -> Optional[str]:
        """
        Establish RTP-to-UTC offset directly from buffer metadata.
        
        This is the simplest bootstrap method: the buffer metadata contains
        both the RTP timestamp and system time of the buffer start. If the
        system clock is NTP-synced (which is a prerequisite), this gives us
        the RTP-to-UTC correspondence directly.
        
        The tone detection then VALIDATES this offset rather than discovering it.
        
        Args:
            buffer_rtp_start: RTP timestamp at buffer start
            buffer_system_time: System time (Unix timestamp) at buffer start
            channel: Channel name for logging
            
        Returns:
            Status message or None
        """
        # Calculate offset: UTC = RTP / sample_rate + offset
        # So: offset = UTC - RTP / sample_rate
        offset_sec = buffer_system_time - (buffer_rtp_start / self.sample_rate)
        
        if self.rtp_to_utc_offset_samples is None:
            # First observation - establish reference
            # Convert offset to samples for consistency with tone-based approach
            # offset_samples = RTP at UTC=0
            # UTC = RTP / sample_rate + offset_sec
            # At UTC=0: 0 = RTP / sample_rate + offset_sec
            # RTP = -offset_sec * sample_rate
            self.rtp_to_utc_offset_samples = int(-offset_sec * self.sample_rate)
            
            self._metadata_offsets = [offset_sec]
            self.reference_channel = channel
            self.state = BootstrapState.CORRELATING
            
            logger.info(f"[BOOTSTRAP] Offset from metadata: {offset_sec:.6f}s "
                       f"(RTP={buffer_rtp_start} at UTC={buffer_system_time:.3f})")
            
            return f"Metadata offset: {offset_sec:.3f}s"
        else:
            # Validate against existing offset
            if not hasattr(self, '_metadata_offsets'):
                self._metadata_offsets = []
            
            self._metadata_offsets.append(offset_sec)
            
            # Keep last 10
            if len(self._metadata_offsets) > 10:
                self._metadata_offsets = self._metadata_offsets[-10:]
            
            # Check consistency
            mean_offset = sum(self._metadata_offsets) / len(self._metadata_offsets)
            max_deviation = max(abs(o - mean_offset) for o in self._metadata_offsets)
            
            if max_deviation < 0.1:  # Within 100ms
                self.minutes_observed = len(self._metadata_offsets)
                self.consecutive_validations += 1
                
                # Update offset with refined estimate
                self.rtp_to_utc_offset_samples = int(-mean_offset * self.sample_rate)
                self.offset_uncertainty_samples = int(max_deviation * self.sample_rate)
                
                # Transition to TRACKING after 3 consistent observations
                if self.consecutive_validations >= 3 and self.state == BootstrapState.CORRELATING:
                    self.state = BootstrapState.TRACKING
                    logger.info(f"[BOOTSTRAP] Metadata offset validated → TRACKING")
                    return "TRACKING"
                
                # Transition to LOCKED after 10 consistent observations
                if self.consecutive_validations >= 10:
                    self.state = BootstrapState.LOCKED
                    logger.info(f"[BOOTSTRAP] Metadata offset LOCKED: {mean_offset:.6f}s "
                               f"(uncertainty={max_deviation*1000:.1f}ms)")
                    return "LOCKED"
                
                return f"Validated: deviation={max_deviation*1000:.1f}ms"
            else:
                # Inconsistent - possible clock jump or RTP discontinuity
                self.consecutive_failures += 1
                logger.warning(f"[BOOTSTRAP] Metadata offset inconsistent: "
                              f"deviation={max_deviation*1000:.1f}ms")
                
                if self.consecutive_failures >= 3:
                    self._retreat_to_acquiring("Metadata offset inconsistent")
                
                return None
        
        return None
    
    # =========================================================================
    # DISCRIMINATING FEATURES VALIDATION
    # =========================================================================
    
    def validate_station_by_tone_frequency(
        self,
        detected_station: str,
        tone_frequency_hz: float
    ) -> Tuple[bool, float]:
        """
        Validate station identity by minute marker tone frequency.
        
        WWV uses 1000 Hz, WWVH uses 1200 Hz for minute markers.
        This is a strong discriminator on shared frequencies.
        
        Args:
            detected_station: Station claimed by detection
            tone_frequency_hz: Detected tone frequency
            
        Returns:
            Tuple of (is_valid, confidence)
        """
        expected = TONE_CHARACTERISTICS.get(detected_station, {}).get('frequency_hz')
        
        if expected is None:
            return True, 0.5  # Unknown station, can't validate
        
        # Allow ±50 Hz tolerance for Doppler shift
        tolerance = 50.0
        error = abs(tone_frequency_hz - expected)
        
        if error <= tolerance:
            confidence = 1.0 - (error / tolerance) * 0.3
            return True, confidence
        else:
            logger.warning(f"[BOOTSTRAP] Tone frequency mismatch: {detected_station} "
                          f"expected {expected}Hz, got {tone_frequency_hz:.0f}Hz")
            return False, 0.0
    
    def validate_station_by_schedule(
        self,
        detected_station: str,
        minute_of_hour: int,
        has_500_600_hz_tone: bool
    ) -> Tuple[bool, float]:
        """
        Validate station identity using the 500/600 Hz tone schedule.
        
        During certain minutes, only one station broadcasts these tones,
        providing unambiguous identification.
        
        Args:
            detected_station: Station claimed by detection
            minute_of_hour: Minute within the hour (0-59)
            has_500_600_hz_tone: Whether 500/600 Hz tone was detected
            
        Returns:
            Tuple of (is_valid, confidence)
        """
        # Check WWV-only minutes
        if minute_of_hour in WWV_ONLY_TONE_MINUTES:
            if has_500_600_hz_tone:
                if detected_station == 'WWV':
                    return True, 0.95  # High confidence - schedule match
                elif detected_station == 'WWVH':
                    logger.warning(f"[BOOTSTRAP] Schedule violation: WWVH detected at "
                                  f"minute {minute_of_hour} (WWV-only)")
                    return False, 0.0
        
        # Check WWVH-only minutes
        if minute_of_hour in WWVH_ONLY_TONE_MINUTES:
            if has_500_600_hz_tone:
                if detected_station == 'WWVH':
                    return True, 0.95
                elif detected_station == 'WWV':
                    logger.warning(f"[BOOTSTRAP] Schedule violation: WWV detected at "
                                  f"minute {minute_of_hour} (WWVH-only)")
                    return False, 0.0
        
        # Check test signal minutes
        if minute_of_hour == WWV_TEST_SIGNAL_MINUTE:
            if detected_station == 'WWV':
                return True, 0.9  # WWV test signal minute
            elif detected_station == 'WWVH':
                # WWVH should be silent during WWV test signal
                logger.info(f"[BOOTSTRAP] WWVH detected during WWV test signal minute")
                return False, 0.3
        
        if minute_of_hour == WWVH_TEST_SIGNAL_MINUTE:
            if detected_station == 'WWVH':
                return True, 0.9
            elif detected_station == 'WWV':
                logger.info(f"[BOOTSTRAP] WWV detected during WWVH test signal minute")
                return False, 0.3
        
        # No schedule-based validation possible for this minute
        return True, 0.5
    
    def validate_wwv_wwvh_ordering(
        self,
        wwv_rtp: int,
        wwvh_rtp: int,
        frequency_khz: int
    ) -> Tuple[bool, float]:
        """
        Validate that WWVH arrives after WWV on shared frequencies.
        
        Due to geographic positions (WWV in Colorado, WWVH in Hawaii),
        WWVH should always arrive later than WWV for receivers in
        continental US.
        
        Args:
            wwv_rtp: RTP timestamp of WWV detection
            wwvh_rtp: RTP timestamp of WWVH detection
            frequency_khz: Broadcast frequency
            
        Returns:
            Tuple of (is_valid, confidence)
        """
        if frequency_khz not in SHARED_FREQUENCIES_KHZ:
            return True, 0.5  # Not a shared frequency
        
        delay_samples = wwvh_rtp - wwv_rtp
        delay_ms = delay_samples * 1000 / self.sample_rate
        
        # WWVH must arrive after WWV
        if delay_ms < 0:
            logger.warning(f"[BOOTSTRAP] Geographic violation: WWVH arrived "
                          f"{-delay_ms:.1f}ms BEFORE WWV on {frequency_khz}kHz")
            return False, 0.0
        
        # Check if delay is within expected range
        wwv_delay = self.station_expectations.get('WWV', {}).get('delay_ms', 5)
        wwvh_delay = self.station_expectations.get('WWVH', {}).get('delay_ms', 25)
        expected_diff = wwvh_delay - wwv_delay
        
        # Allow 3x tolerance for ionospheric variability
        if delay_ms < expected_diff * 0.2:
            logger.info(f"[BOOTSTRAP] WWVH-WWV delay {delay_ms:.1f}ms smaller than "
                       f"expected ({expected_diff:.1f}ms)")
            return True, 0.6
        elif delay_ms > expected_diff * 4.0:
            logger.info(f"[BOOTSTRAP] WWVH-WWV delay {delay_ms:.1f}ms larger than "
                       f"expected ({expected_diff:.1f}ms)")
            return True, 0.6
        else:
            confidence = 0.9
            return True, confidence
    
    def get_minute_of_hour(self, utc_timestamp: float) -> int:
        """Get minute within the hour (0-59) from UTC timestamp."""
        import time
        return int(utc_timestamp // 60) % 60
    
    def is_unambiguous_channel(self, channel: str) -> bool:
        """Check if channel is unambiguous (only one station transmits)."""
        return channel in UNAMBIGUOUS_CHANNELS
    
    def get_expected_station(self, channel: str) -> Optional[str]:
        """Get expected station for an unambiguous channel."""
        return UNAMBIGUOUS_CHANNELS.get(channel)

