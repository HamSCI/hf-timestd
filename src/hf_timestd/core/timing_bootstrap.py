"""
Timing Bootstrap: Broadcast-Driven RTP-to-UTC Calibration

This module implements a bootstrap algorithm that discovers the RTP-to-UTC
correspondence from the broadcasts themselves, without relying on system
time accuracy.

Architecture:
------------
ACQUIRING → CORRELATING → TRACKING → LOCKED
    ↑______________|___________|
         (retreat on errors)

ACQUIRING: Collect candidates and look for minute marker clusters
CORRELATING: Validate clusters using cross-station timing
TRACKING: Narrow-window detection around predicted positions
LOCKED: Offset determined with high confidence

Key Insight - Clustering:
------------------------
At each UTC minute boundary, WWV, CHU, and WWVH all transmit minute markers.
These arrive within a tight window (~50ms) with predictable ordering:
  WWV arrives first (closest to most US receivers)
  CHU arrives ~1-5ms after WWV
  WWVH arrives ~15-30ms after WWV

By finding CLUSTERS of high-quality candidates that match this pattern,
we can identify true minute markers and reject per-second ticks.

Discriminating Features:
-----------------------
1. Tone duration: WWV/WWVH 800ms, CHU 500ms minute markers
2. Tone frequency: WWV=1000Hz, WWVH=1200Hz, CHU=1000Hz
3. Clustering: True minute markers cluster across stations
4. Geographic ordering: WWV < CHU < WWVH arrival times

Geographic Priors (receiver-dependent):
--------------------------------------
The expected delay difference between stations depends on receiver location.
For a receiver in Missouri (~38.9°N, 92.1°W):
- WWV (Colorado): ~1100 km, ~4-12ms propagation
- CHU (Ottawa): ~1500 km, ~5-15ms propagation  
- WWVH (Hawaii): ~5500 km, ~18-40ms propagation
"""

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple, Set
from math import radians, sin, cos, sqrt, atan2
from statistics import median

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
    TRACKING = "tracking"        # Narrow-window tracking with known offset (provisional lock)
    LOCKED = "locked"           # Offset confirmed, high confidence (refined lock)


class LockTier(Enum):
    """Lock tier for two-tier bootstrap.
    
    Tier 0: No lock - still acquiring/correlating
    Tier 1: Provisional lock - minute boundaries established, archiving can begin
    Tier 2: Refined lock - stable offset after ionospheric averaging
    """
    NONE = 0
    PROVISIONAL = 1
    REFINED = 2


@dataclass
class OffsetMeasurement:
    """A single offset measurement during provisional lock phase."""
    timestamp: float           # Unix timestamp when measurement was taken
    offset_samples: int        # RTP-to-UTC offset in samples
    station: str               # Station that provided this measurement
    snr_db: float              # SNR of the detection
    frequency_khz: int         # Broadcast frequency


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
        from .timing_bootstrap import TimingBootstrap, BootstrapState, LockTier
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
    
    # Two-tier bootstrap (2026-01-27)
    # Tier 1: Provisional lock for minute alignment (2-3 min)
    # Tier 2: Refined lock after ionospheric averaging (10-15 min)
    lock_tier: LockTier = field(default=LockTier.NONE)
    provisional_lock_time: Optional[float] = None  # Unix time when provisional lock achieved
    
    # Tier 2 configuration
    refined_lock_duration_sec: float = 600.0  # 10 minutes for TID averaging
    min_measurements_for_refined: int = 50  # Require sufficient measurements for robust median
    max_offset_std_for_refined_ms: float = 15.0  # Stability criterion
    max_refined_lock_wait_sec: float = 1800.0  # 30 min timeout - accept best offset if criteria not met
    
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
        
        # All candidates collected (for clustering)
        # CRITICAL FIX (2026-01-27): Limit size to prevent memory leak
        # With 9 channels adding candidates every few seconds, this list can grow
        # to thousands of entries. Keep only the most recent candidates needed
        # for clustering (last ~5 minutes worth = ~500 candidates max).
        self.all_candidates: List[AcquisitionCandidate] = []
        self._max_candidates = 500  # Limit to prevent unbounded growth
        
        # Validated minute marker clusters
        self.validated_clusters: List[dict] = []
        
        # Time confirmation (Phase 2: decode actual UTC from broadcasts)
        self._time_confirmer = None
        self._time_confirmed = False
        self._confirmed_minute: Optional[int] = None
        self._confirmed_hour: Optional[int] = None
        
        # Two-tier bootstrap: offset measurements during provisional lock
        # These are collected after provisional lock to compute refined offset
        self._offset_measurements: List[OffsetMeasurement] = []
        self._refined_offset_samples: Optional[int] = None
        self._refined_offset_std_ms: Optional[float] = None
    
    def find_minute_clusters(self, min_snr_db: float = 15.0) -> List[dict]:
        """
        Find clusters of candidates that represent true minute markers.
        
        A valid cluster has candidates from multiple stations arriving within
        expected delay windows based on geographic priors. The arrival ORDER
        depends on receiver location (e.g., CHU first near Ottawa, WWV first
        in central US).
        
        Returns list of clusters, each with:
        - 'anchor_rtp': RTP timestamp of earliest-arriving station
        - 'anchor_station': Which station arrived first
        - 'members': Dict of station -> candidate list
        - 'confidence': Overall cluster confidence
        """
        # Filter high-quality candidates
        good_candidates = [c for c in self.all_candidates if c.snr_db >= min_snr_db]
        
        if not good_candidates:
            return []
        
        # Separate by station
        by_station = {}
        for c in good_candidates:
            if c.station not in by_station:
                by_station[c.station] = []
            by_station[c.station].append(c)
        
        stations_found = list(by_station.keys())
        logger.info(f"[BOOTSTRAP] Clustering: " + 
                   ", ".join(f"{len(by_station[s])} {s}" for s in stations_found) +
                   f" (SNR >= {min_snr_db}dB)")
        
        if not stations_found:
            return []
        
        # Get expected delays for all stations
        delays = {s: self.station_expectations.get(s, {}).get('delay_ms', 0) 
                  for s in stations_found}
        
        # Find the earliest-arriving station (smallest delay)
        anchor_station = min(delays.keys(), key=lambda s: delays[s])
        anchor_delay = delays[anchor_station]
        
        clusters = []
        window_ms = 100  # Allow 100ms tolerance for ionospheric variability
        
        logger.debug(f"[BOOTSTRAP] Anchor station: {anchor_station} (delay={anchor_delay:.1f}ms), "
                    f"window={window_ms}ms")
        
        # Use candidates from earliest-arriving station as anchors
        for anchor_cand in by_station.get(anchor_station, []):
            cluster = {
                'anchor_rtp': anchor_cand.rtp_timestamp,
                'anchor_station': anchor_station,
                'members': {anchor_station: [anchor_cand]},
                'stations': {anchor_station},
            }
            
            # Look for candidates from other stations within expected delay windows
            for other_station, other_cands in by_station.items():
                if other_station == anchor_station:
                    continue
                
                # Expected offset: other station's delay minus anchor's delay
                expected_offset_ms = delays[other_station] - anchor_delay
                
                # Find closest candidate from this station
                best_match = None
                best_error = float('inf')
                
                for cand in other_cands:
                    offset_samples = cand.rtp_timestamp - anchor_cand.rtp_timestamp
                    offset_ms = offset_samples * 1000 / self.sample_rate
                    
                    # Allow matching across minute boundaries:
                    # The offset should be expected_offset + N*60000ms for some integer N
                    # This handles cases where different channels have different buffer ranges
                    raw_error = offset_ms - expected_offset_ms
                    minutes_diff = round(raw_error / 60000)
                    error = abs(raw_error - minutes_diff * 60000)
                    
                    if error < best_error:
                        best_error = error
                        best_match = (cand, offset_ms, minutes_diff)
                    
                    if error < window_ms:
                        if other_station not in cluster['members']:
                            cluster['members'][other_station] = []
                        cluster['members'][other_station].append(cand)
                        cluster['stations'].add(other_station)
                        # Log successful cross-minute clustering
                        if minutes_diff != 0 and other_station in ('CHU', 'WWVH'):
                            logger.info(f"[BOOTSTRAP] Cross-minute match: {other_station} at "
                                       f"minute_diff={minutes_diff}, error={error:.1f}ms")
            
            # Calculate cluster confidence
            num_stations = len(cluster['stations'])
            snr_values = [anchor_cand.snr_db]
            for station, cands in cluster['members'].items():
                if station != anchor_station and cands:
                    snr_values.append(max(c.snr_db for c in cands))
            avg_snr = sum(snr_values) / len(snr_values)
            
            cluster['confidence'] = min(1.0, (num_stations / 3) * (avg_snr / 30))
            cluster['num_stations'] = num_stations
            
            # Only keep clusters with at least 2 stations or very high SNR
            if num_stations >= 2 or anchor_cand.snr_db >= 25:
                clusters.append(cluster)
                other_stations = [s for s in cluster['stations'] if s != anchor_station]
                # Log with more detail for debugging multi-station clustering
                if num_stations >= 2 and ('CHU' in cluster['stations'] or 'WWVH' in cluster['stations']):
                    logger.info(f"[BOOTSTRAP] MULTI-STATION cluster: {anchor_station}@{anchor_cand.rtp_timestamp} "
                               f"({anchor_cand.snr_db:.1f}dB) + {other_stations}, "
                               f"conf={cluster['confidence']:.2f}")
                else:
                    logger.info(f"[BOOTSTRAP] Found cluster: {anchor_station}@{anchor_cand.rtp_timestamp} "
                               f"({anchor_cand.snr_db:.1f}dB) + {other_stations}, "
                               f"conf={cluster['confidence']:.2f}")
        
        # Log summary for CHU/WWVH clustering issues (only if no multi-station clusters found)
        chu_wwvh_clusters = [c for c in clusters if 'CHU' in c['stations'] or 'WWVH' in c['stations']]
        if not chu_wwvh_clusters and ('CHU' in stations_found or 'WWVH' in stations_found):
            # Show why CHU/WWVH aren't clustering with WWV (sample one anchor)
            if by_station.get(anchor_station) and len(clusters) % 50 == 0:  # Reduce log spam
                sample_anchor = by_station[anchor_station][0]
                for other_station in ['CHU', 'WWVH']:
                    if other_station not in stations_found:
                        continue
                    expected_offset = delays.get(other_station, 0) - anchor_delay
                    if by_station.get(other_station):
                        # Find closest match accounting for minute boundaries
                        best_errors = []
                        for c in by_station[other_station][:5]:
                            offset_ms = (c.rtp_timestamp - sample_anchor.rtp_timestamp) * 1000 / self.sample_rate
                            raw_error = offset_ms - expected_offset
                            minutes_diff = round(raw_error / 60000)
                            error = abs(raw_error - minutes_diff * 60000)
                            best_errors.append((error, minutes_diff, offset_ms))
                        min_error = min(e[0] for e in best_errors) if best_errors else float('inf')
                        logger.info(f"[BOOTSTRAP] {other_station} clustering check: "
                                   f"expected offset={expected_offset:.1f}ms, "
                                   f"min_error={min_error:.0f}ms (need <{window_ms}ms)")
        
        # Sort by confidence
        clusters.sort(key=lambda c: c['confidence'], reverse=True)
        
        return clusters
    
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
        
        # Always collect candidates for clustering
        self.all_candidates.append(candidate)
        
        # CRITICAL FIX (2026-01-27): Trim list to prevent memory leak
        # Keep only the most recent candidates needed for clustering
        if len(self.all_candidates) > self._max_candidates:
            # Remove oldest 20% to avoid frequent trimming
            trim_count = self._max_candidates // 5
            self.all_candidates = self.all_candidates[trim_count:]
            logger.debug(f"[BOOTSTRAP] Trimmed {trim_count} old candidates, keeping {len(self.all_candidates)}")
        
        # Log state on first few candidates
        if len(self.all_candidates) <= 3:
            logger.info(f"[BOOTSTRAP] add_candidate #{len(self.all_candidates)}: state={self.state.value}, "
                       f"reference_rtp={self.reference_rtp}")
        
        # In ACQUIRING state, try to find clusters that RECUR at 60-second intervals
        if self.state == BootstrapState.ACQUIRING:
            # Only check for clusters periodically (every 50 candidates) to reduce log spam
            if len(self.all_candidates) % 50 == 0:
                logger.info(f"[BOOTSTRAP] ACQUIRING: {len(self.all_candidates)} candidates collected")
            
            clusters = self.find_minute_clusters(min_snr_db=20.0)  # High threshold - true minute markers are 25-45dB
            # Filter to multi-station clusters
            multi_station = [c for c in clusters if c['num_stations'] >= 2]
            
            if not multi_station:
                return None
            
            # KEY: Find clusters that recur at 60-second (1,440,000 sample) intervals
            # This distinguishes true minute markers from per-second ticks
            for cluster in multi_station:
                anchor_rtp = cluster['anchor_rtp']
                
                # Look for another cluster at anchor_rtp ± N*SAMPLES_PER_MINUTE
                for other in multi_station:
                    if other is cluster:
                        continue
                    
                    other_rtp = other['anchor_rtp']
                    diff = abs(other_rtp - anchor_rtp)
                    
                    # Check if difference is close to N minutes (within 500ms tolerance)
                    minutes_apart = round(diff / SAMPLES_PER_MINUTE)
                    if minutes_apart == 0:
                        continue  # Same minute boundary
                    
                    expected_diff = minutes_apart * SAMPLES_PER_MINUTE
                    error_samples = abs(diff - expected_diff)
                    error_ms = error_samples * 1000 / self.sample_rate
                    
                    if error_ms < 100:  # Within 100ms of expected minute spacing (tight tolerance for true minute markers)
                        # Found recurring clusters! Use earlier one as reference
                        if anchor_rtp < other_rtp:
                            ref_cluster = cluster
                        else:
                            ref_cluster = other
                        
                        anchor_station = ref_cluster['anchor_station']
                        anchor_cands = ref_cluster['members'][anchor_station]
                        anchor_cand = anchor_cands[0]
                        
                        self.reference_rtp = ref_cluster['anchor_rtp']
                        self.reference_channel = anchor_cand.channel
                        self.reference_station = anchor_station
                        self.validated_clusters.append(ref_cluster)
                        self.validated_clusters.append(other if ref_cluster is cluster else cluster)
                        self.state = BootstrapState.CORRELATING
                        self.minutes_observed = minutes_apart + 1
                        
                        stations_list = list(ref_cluster['stations'])
                        logger.info(f"[BOOTSTRAP] RECURRING CLUSTERS FOUND: "
                                   f"{minutes_apart} minutes apart, error={error_ms:.1f}ms")
                        logger.info(f"[BOOTSTRAP] CLUSTER LOCK: {anchor_station}@{ref_cluster['anchor_rtp']} "
                                   f"with stations {stations_list} → CORRELATING")
                        
                        return f"CLUSTER: {ref_cluster['num_stations']} stations, {minutes_apart}min recurrence"
            
            # No recurring clusters found yet - keep collecting
            if multi_station and len(self.all_candidates) % 100 == 0:
                logger.info(f"[BOOTSTRAP] Found {len(multi_station)} multi-station clusters, "
                           f"waiting for 60-second recurrence validation")
            return None
        
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
            
            # Account for propagation delay difference between stations
            if station != self.reference_station:
                ref_delay_ms = self.station_expectations.get(
                    self.reference_station, {}
                ).get('delay_ms', 0)
                cand_delay_ms = self.station_expectations.get(
                    station, {}
                ).get('delay_ms', 0)
                delay_diff_samples = int((cand_delay_ms - ref_delay_ms) * self.sample_rate / 1000)
                samples_from_ref -= delay_diff_samples
            
            # Round to nearest minute
            minute_index = round(samples_from_ref / SAMPLES_PER_MINUTE)
            
            # CRITICAL: Check if this candidate is close to an expected minute boundary
            # If not, it's likely not a minute marker - reject early
            expected_samples = minute_index * SAMPLES_PER_MINUTE
            offset_from_expected = abs(samples_from_ref - expected_samples)
            offset_ms = offset_from_expected * 1000 / self.sample_rate
            
            if offset_ms > 500:  # More than 500ms from expected minute boundary
                logger.debug(f"[BOOTSTRAP] Rejecting {station} candidate: {offset_ms:.0f}ms "
                            f"from expected minute {minute_index}")
                return None
            
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
        # Note: ACQUIRING state is handled by clustering logic above (line 406)
        if self.state == BootstrapState.CORRELATING:
            return self._handle_correlating(candidate, minute_index)
        elif self.state == BootstrapState.TRACKING:
            return self._handle_tracking(candidate, minute_index)
        elif self.state == BootstrapState.LOCKED:
            # CRITICAL FIX (2026-01-27): LOCKED is a confidence threshold, not a terminal state
            # Continue refining the offset as we gather more observations.
            # The offset refinement in _handle_tracking drives raw D_clock toward zero.
            return self._handle_locked(candidate, minute_index)
        else:
            return None
    
    def _is_unambiguous_channel(self, channel: str, station: str) -> bool:
        """Check if this is an unambiguous channel (only one station transmits)."""
        # WWV 20 and 25 MHz are PREFERRED for initial reference
        # They have distinctive 800ms minute markers and no competing station
        if station == 'WWV':
            freq_khz = self._extract_frequency_khz(channel)
            if freq_khz in [20000, 25000]:
                return True
        # CHU frequencies are unambiguous but have multiple 500ms tones per minute
        # Only use as reference if we haven't found WWV yet
        if 'CHU' in channel.upper():
            # Only accept CHU as reference if SNR is very high (>25dB)
            # This helps ensure we're getting the minute marker, not another tone
            return True
        return False
    
    def _is_preferred_reference(self, channel: str, station: str, snr_db: float) -> bool:
        """Check if this is a preferred channel for initial reference."""
        # WWV 20/25 MHz with 800ms tones are the best reference
        if station == 'WWV':
            freq_khz = self._extract_frequency_khz(channel)
            if freq_khz in [20000, 25000] and snr_db > 12:
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
        
        Prefers WWV 20/25 MHz as initial reference since they have distinctive 800ms
        minute markers. CHU is accepted but requires higher SNR since it has multiple
        500ms tones per minute.
        """
        # Only accept high-confidence detections
        if candidate.confidence < 0.7 or candidate.snr_db < 12:
            logger.debug(f"[BOOTSTRAP] Rejecting {candidate.station} on {candidate.channel}: "
                        f"confidence={candidate.confidence:.2f}, SNR={candidate.snr_db:.1f}dB")
            return None
        
        # Check if this is a preferred reference (WWV 20/25 MHz)
        is_preferred = self._is_preferred_reference(
            candidate.channel, candidate.station, candidate.snr_db
        )
        
        # Check if unambiguous (includes CHU)
        is_unambiguous = self._is_unambiguous_channel(
            candidate.channel, candidate.station
        )
        
        # For CHU, require higher SNR since it has multiple 500ms tones
        if 'CHU' in candidate.channel.upper() and not is_preferred:
            if candidate.snr_db < 25:
                logger.debug(f"[BOOTSTRAP] CHU candidate SNR {candidate.snr_db:.1f}dB < 25dB, "
                            f"waiting for WWV or higher SNR CHU")
                return None
        
        if is_preferred or is_unambiguous:
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
        CORRELATING state: Look for additional clusters at subsequent minute boundaries.
        
        Uses clustering to find minute markers - looks for WWV/CHU/WWVH candidates
        that cluster within ~50ms of expected minute boundaries.
        """
        if self.reference_rtp is None:
            return None
        
        # Look for clusters that recur at minute boundaries (N × 1,440,000 samples from reference)
        # 
        # The clustering finds groups of candidates within ~100ms of each other.
        # We then check if any cluster's anchor is at an expected minute boundary:
        #   expected_rtp = reference_rtp + N * SAMPLES_PER_MINUTE (where N = 1, 2, 3, ...)
        
        clusters = self.find_minute_clusters(min_snr_db=20.0)  # High threshold - true minute markers are 25-45dB
        
        # Log periodically
        if len(self.all_candidates) % 50 == 0:
            logger.info(f"[BOOTSTRAP] CORRELATING: {len(self.all_candidates)} total candidates, "
                       f"{len(clusters)} clusters found")
        
        for cluster in clusters:
            anchor = cluster['anchor_rtp']
            
            # Check if this cluster is at a new minute boundary
            samples_from_ref = anchor - self.reference_rtp
            minute_offset = round(samples_from_ref / SAMPLES_PER_MINUTE)
            
            # Skip minute 0 (already validated)
            if minute_offset == 0:
                continue
            
            # Check spacing error
            expected_rtp = self.reference_rtp + (minute_offset * SAMPLES_PER_MINUTE)
            spacing_error_samples = abs(anchor - expected_rtp)
            spacing_error_ms = spacing_error_samples * 1000 / self.sample_rate
            
            # Log cluster evaluation
            if cluster['num_stations'] >= 2:
                logger.info(f"[BOOTSTRAP] Evaluating cluster: minute_offset={minute_offset}, "
                           f"spacing_error={spacing_error_ms:.1f}ms, stations={cluster['num_stations']}")
            
            if spacing_error_ms < 200:  # Within 200ms of expected
                # Valid cluster at minute N!
                self.validated_clusters.append(cluster)
                self.minutes_observed = max(self.minutes_observed, abs(minute_offset) + 1)
                
                logger.info(f"[BOOTSTRAP] Validated cluster at minute {minute_offset}: "
                           f"{cluster['num_stations']} stations, error={spacing_error_ms:.1f}ms")
                
                # Check if ready for time confirmation or tracking
                if len(self.validated_clusters) >= 2 and self.minutes_observed >= 2:
                    self._compute_offset()
                    
                    # Note: Time confirmation (BCD/FSK decode) happens externally
                    # via attempt_time_confirmation() when buffer data is available.
                    # For now, transition to TRACKING and await confirmation.
                    self.state = BootstrapState.TRACKING
                    logger.info(f"[BOOTSTRAP] {len(self.validated_clusters)} clusters over "
                               f"{self.minutes_observed} minutes → TRACKING "
                               f"(awaiting time confirmation from BCD/FSK decode)")
                    return "TRACKING"
        
        return None
    
    def attempt_time_confirmation(
        self,
        ntp_time: float,
        chu_samples: Optional['np.ndarray'] = None,
        wwv_samples: Optional['np.ndarray'] = None,
        wwvh_samples: Optional['np.ndarray'] = None,
    ) -> Optional[str]:
        """
        Attempt to confirm time by decoding BCD/FSK from station broadcasts.
        
        This is Phase 2 of bootstrap: after clustering finds minute boundaries,
        we decode the actual UTC time from the broadcasts to confirm.
        
        Args:
            ntp_time: Unix timestamp from NTP (hypothesis)
            chu_samples: 60 seconds of CHU IQ data
            wwv_samples: 60 seconds of WWV IQ data
            wwvh_samples: 60 seconds of WWVH IQ data
            
        Returns:
            Status string if confirmation succeeded, None otherwise
        """
        if self.state not in (BootstrapState.TRACKING, BootstrapState.CORRELATING):
            return None
        
        # Lazy-load time confirmer
        if self._time_confirmer is None:
            try:
                from .bootstrap_time_confirmation import BootstrapTimeConfirmer
                self._time_confirmer = BootstrapTimeConfirmer(sample_rate=self.sample_rate)
            except ImportError as e:
                logger.warning(f"[BOOTSTRAP] Time confirmation not available: {e}")
                return None
        
        result = self._time_confirmer.confirm_time(
            ntp_time=ntp_time,
            chu_samples=chu_samples,
            wwv_samples=wwv_samples,
            wwvh_samples=wwvh_samples,
        )
        
        if result.confirmed:
            self._time_confirmed = True
            self._confirmed_minute = result.minute
            self._confirmed_hour = result.hour
            
            if result.matches_ntp():
                # Decoded time matches NTP hypothesis - high confidence lock!
                logger.info(f"[BOOTSTRAP] TIME CONFIRMED: {result.hour:02d}:{result.minute:02d} "
                           f"(source={result.source.value}, matches NTP)")
                
                if self.state == BootstrapState.TRACKING:
                    # Can now transition to LOCKED with high confidence
                    self.state = BootstrapState.LOCKED
                    logger.info(f"[BOOTSTRAP] Offset LOCKED with decoded time confirmation")
                    return "LOCKED (time confirmed)"
            else:
                # Decoded time differs from NTP - adjust offset!
                # The decoded time is ground truth; NTP was just a hypothesis
                logger.warning(f"[BOOTSTRAP] Decoded time {result.hour:02d}:{result.minute:02d} "
                              f"differs from NTP {result.ntp_hour:02d}:{result.ntp_minute:02d}")
                
                # Calculate minute difference (decoded - NTP)
                # Positive = decoded time is ahead of NTP hypothesis
                decoded_total_min = result.hour * 60 + result.minute
                ntp_total_min = result.ntp_hour * 60 + result.ntp_minute
                minute_diff = decoded_total_min - ntp_total_min
                
                # Handle day boundary wraparound
                if minute_diff > 720:  # More than 12 hours ahead
                    minute_diff -= 1440  # Subtract a day
                elif minute_diff < -720:  # More than 12 hours behind
                    minute_diff += 1440  # Add a day
                
                if self.rtp_to_utc_offset_samples is not None:
                    # Adjust offset: if decoded is N minutes ahead of NTP,
                    # the true UTC minute 0 is N minutes earlier in RTP terms
                    offset_adjustment = minute_diff * SAMPLES_PER_MINUTE
                    old_offset = self.rtp_to_utc_offset_samples
                    self.rtp_to_utc_offset_samples -= offset_adjustment
                    
                    logger.info(f"[BOOTSTRAP] Offset adjusted by {minute_diff} minutes "
                               f"({offset_adjustment} samples): {old_offset} → {self.rtp_to_utc_offset_samples}")
                    
                    # Now we have decoded time confirmation - can lock with high confidence
                    self._time_confirmed = True
                    if self.state == BootstrapState.TRACKING:
                        self.state = BootstrapState.LOCKED
                        logger.info(f"[BOOTSTRAP] Offset LOCKED after time correction")
                        return "LOCKED (time corrected)"
                
                return f"TIME_CORRECTED: {minute_diff:+d} minutes"
        
        return None
    
    def _handle_tracking(
        self,
        candidate: AcquisitionCandidate,
        minute_index: int
    ) -> Optional[str]:
        """
        TRACKING state: Two-tier bootstrap with provisional and refined lock.
        
        Tier 1 (Provisional): Quick lock for minute alignment (2-3 min)
        - Allows archiving to begin
        - Uses initial offset estimate
        - CONTINUOUSLY REFINES offset using timing errors (metrological fix 2026-01-27)
        
        Tier 2 (Refined): Stable lock after ionospheric averaging (10-15 min)
        - Collects offset measurements during provisional phase
        - Uses median for robustness against outliers
        - Requires std < 15ms and 50+ measurements
        
        METROLOGICAL FIX (2026-01-27):
        The raw D_clock should converge to near zero once bootstrap locks.
        Previously, the metadata-derived offset was used without refinement,
        causing raw D_clock to be ~100-300ms off. Now we continuously apply
        timing error corrections to drive raw D_clock toward zero.
        """
        import time
        
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
        
        # ================================================================
        # ADAPTIVE TOLERANCE AND REFINEMENT (2026-01-27)
        # ================================================================
        # During initial convergence, allow larger errors and apply aggressive
        # corrections. After convergence, tighten tolerance and use gentler alpha.
        #
        # This solves the chicken-and-egg problem: we need to accept large errors
        # to refine them, but we also need to reject true outliers.
        
        # Determine tolerance based on convergence state
        # The initial metadata-derived offset can be off by 100-500ms, so we need
        # very permissive tolerance during initial convergence to allow refinement.
        if self.lock_tier == LockTier.NONE:
            # Before provisional lock: very permissive (initial convergence)
            # Allow up to 500ms error to handle metadata offset errors
            ERROR_TOLERANCE_MS = 500.0
            OFFSET_CORRECTION_ALPHA = 0.4  # Very aggressive: 40% of error
        elif self.lock_tier == LockTier.PROVISIONAL:
            # After provisional lock: moderate tolerance
            # Still allow large errors as we continue converging
            ERROR_TOLERANCE_MS = 200.0
            OFFSET_CORRECTION_ALPHA = 0.3  # Aggressive: 30% of error
        else:
            # After refined lock: tighter tolerance
            ERROR_TOLERANCE_MS = 100.0
            OFFSET_CORRECTION_ALPHA = 0.2  # Moderate: 20% of error
        
        if abs(error_ms) > ERROR_TOLERANCE_MS:
            logger.warning(f"[BOOTSTRAP] Tracking error {error_ms:.1f}ms for "
                          f"{candidate.station} (tolerance={ERROR_TOLERANCE_MS}ms)")
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
        
        if minute_index > self.minutes_observed:
            self.minutes_observed = minute_index + 1
        
        # ================================================================
        # CONTINUOUS OFFSET REFINEMENT
        # ================================================================
        # Apply corrections to drive raw D_clock toward zero.
        # The alpha value is set above based on convergence state.
        MIN_CORRECTION_MS = 0.1  # Apply even small corrections
        
        if abs(error_ms) > MIN_CORRECTION_MS:
            correction_samples = int(error_samples * OFFSET_CORRECTION_ALPHA)
            if correction_samples != 0:
                self.rtp_to_utc_offset_samples += correction_samples
                correction_ms = correction_samples * 1000 / self.sample_rate
                
                # Log corrections > 0.5ms
                if abs(correction_ms) > 0.5:
                    logger.info(f"[BOOTSTRAP] Offset refined: {correction_ms:+.2f}ms "
                               f"(error={error_ms:+.1f}ms, α={OFFSET_CORRECTION_ALPHA}, "
                               f"tier={self.lock_tier.name})")
        
        # === TWO-TIER BOOTSTRAP LOGIC ===
        
        # Check for Tier 1 (Provisional Lock)
        if self.lock_tier == LockTier.NONE:
            if self.consecutive_validations >= 10 and self.minutes_observed >= 2:
                self.lock_tier = LockTier.PROVISIONAL
                self.provisional_lock_time = time.time()
                logger.info(f"[BOOTSTRAP] TIER 1 PROVISIONAL LOCK after {self.minutes_observed} minutes, "
                           f"{len(self.validated_tones)} validated tones")
                return "PROVISIONAL_LOCK"
        
        # During provisional lock, collect offset measurements for refined lock
        if self.lock_tier == LockTier.PROVISIONAL:
            self._record_offset_measurement(candidate, minute_index)
            
            # Check if ready for Tier 2 (Refined Lock)
            refined_result = self._check_refined_lock_criteria()
            if refined_result:
                return refined_result
        
        return f"Tracking: {candidate.station} error={error_ms:+.1f}ms"
    
    def _record_offset_measurement(
        self,
        candidate: AcquisitionCandidate,
        minute_index: int
    ) -> None:
        """
        Record an offset measurement during provisional lock phase.
        
        Each valid tone detection provides an independent measurement of the
        RTP-to-UTC offset. We collect these to compute a refined median offset.
        """
        import time
        
        # Get propagation delay for this station
        delay_samples = self.station_expectations.get(
            candidate.station, {}
        ).get('delay_samples', 0)
        
        # Compute offset: RTP at UTC minute 0
        minute_rtp = candidate.rtp_timestamp - delay_samples
        minute_0_rtp = minute_rtp - (minute_index * SAMPLES_PER_MINUTE)
        
        measurement = OffsetMeasurement(
            timestamp=time.time(),
            offset_samples=minute_0_rtp,
            station=candidate.station,
            snr_db=candidate.snr_db,
            frequency_khz=candidate.frequency_khz
        )
        
        self._offset_measurements.append(measurement)
        
        # Log periodically
        if len(self._offset_measurements) % 10 == 0:
            logger.info(f"[BOOTSTRAP] Collected {len(self._offset_measurements)} offset measurements "
                       f"for refined lock")
    
    def _check_refined_lock_criteria(self) -> Optional[str]:
        """
        Check if criteria for refined (Tier 2) lock are met.
        
        Criteria:
        1. At least refined_lock_duration_sec (10 min) since provisional lock
        2. At least min_measurements_for_refined (50) measurements
        3. Offset standard deviation < max_offset_std_for_refined_ms (15ms)
        
        Returns:
            Status string if refined lock achieved, None otherwise
        """
        import time
        
        if self.provisional_lock_time is None:
            return None
        
        elapsed = time.time() - self.provisional_lock_time
        n_measurements = len(self._offset_measurements)
        
        # Check minimum duration
        if elapsed < self.refined_lock_duration_sec:
            return None
        
        # Check minimum measurements
        if n_measurements < self.min_measurements_for_refined:
            logger.info(f"[BOOTSTRAP] Refined lock: {n_measurements}/{self.min_measurements_for_refined} "
                       f"measurements after {elapsed:.0f}s")
            return None
        
        # Compute median and standard deviation
        offsets = [m.offset_samples for m in self._offset_measurements]
        median_offset = int(median(offsets))
        
        # Standard deviation in ms
        mean_offset = sum(offsets) / len(offsets)
        variance = sum((o - mean_offset) ** 2 for o in offsets) / len(offsets)
        std_samples = sqrt(variance)
        std_ms = std_samples * 1000 / self.sample_rate
        
        # Check stability criterion
        timeout_exceeded = elapsed >= self.max_refined_lock_wait_sec
        if std_ms > self.max_offset_std_for_refined_ms:
            if not timeout_exceeded:
                logger.info(f"[BOOTSTRAP] Refined lock: std={std_ms:.1f}ms > {self.max_offset_std_for_refined_ms}ms, "
                           f"continuing to collect measurements ({elapsed:.0f}s/{self.max_refined_lock_wait_sec:.0f}s)")
                return None
            else:
                # Timeout exceeded - accept best available offset with warning
                logger.warning(f"[BOOTSTRAP] TIER 2 TIMEOUT after {elapsed:.0f}s: "
                              f"std={std_ms:.1f}ms > {self.max_offset_std_for_refined_ms}ms threshold, "
                              f"accepting best available offset")
        
        # Criteria met OR timeout exceeded - transition to refined lock!
        self._refined_offset_samples = median_offset
        self._refined_offset_std_ms = std_ms
        
        # Update the main offset with refined value
        old_offset = self.rtp_to_utc_offset_samples
        self.rtp_to_utc_offset_samples = median_offset
        self.offset_uncertainty_samples = int(std_samples)
        
        # Transition to LOCKED state with Tier 2
        self.lock_tier = LockTier.REFINED
        self.state = BootstrapState.LOCKED
        
        offset_change_ms = (median_offset - old_offset) * 1000 / self.sample_rate if old_offset else 0
        
        logger.info(f"[BOOTSTRAP] TIER 2 REFINED LOCK achieved!")
        logger.info(f"  Duration: {elapsed:.0f}s, Measurements: {n_measurements}")
        logger.info(f"  Offset: {median_offset} samples (median), std={std_ms:.1f}ms")
        logger.info(f"  Offset change from provisional: {offset_change_ms:+.1f}ms")
        
        # Log station distribution
        station_counts = {}
        for m in self._offset_measurements:
            station_counts[m.station] = station_counts.get(m.station, 0) + 1
        logger.info(f"  Station distribution: {station_counts}")
        
        return "REFINED_LOCK"
    
    def _handle_locked(
        self,
        candidate: AcquisitionCandidate,
        minute_index: int
    ) -> Optional[str]:
        """
        LOCKED state: Continue refining offset as we gather more observations.
        
        CRITICAL INSIGHT (2026-01-27):
        LOCKED is a confidence threshold, not a terminal state. We've reached
        sufficient confidence to:
        - Start archiving data (PROVISIONAL)
        - Feed Chrony with timing (REFINED)
        
        But we should NEVER stop learning. Each tone arrival provides information
        about the true RTP-to-UTC offset. Continue applying corrections to drive
        raw D_clock toward zero (within propagation delay uncertainty ~5-10ms).
        
        This is metrologically correct: the offset estimate improves with more
        observations, and ionospheric conditions change over time.
        """
        import time
        
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
        
        # In LOCKED state, we're more confident - reject large outliers
        if abs(error_ms) > 100:
            logger.debug(f"[BOOTSTRAP] LOCKED: Ignoring outlier {candidate.station} "
                        f"error={error_ms:.1f}ms (>100ms threshold)")
            return None
        
        # ================================================================
        # CONTINUOUS OFFSET REFINEMENT
        # ================================================================
        # Apply corrections to drive raw D_clock toward zero.
        # Use more aggressive alpha in LOCKED state (higher confidence).
        # 
        # After REFINED lock, use even more aggressive correction since
        # we have high confidence in the offset estimate.
        if self.lock_tier == LockTier.REFINED:
            OFFSET_CORRECTION_ALPHA = 0.3  # 30% of error per update
        else:
            OFFSET_CORRECTION_ALPHA = 0.2  # 20% of error per update
        
        MIN_CORRECTION_MS = 0.1  # Apply even small corrections
        
        if abs(error_ms) > MIN_CORRECTION_MS:
            correction_samples = int(error_samples * OFFSET_CORRECTION_ALPHA)
            if correction_samples != 0:
                self.rtp_to_utc_offset_samples += correction_samples
                correction_ms = correction_samples * 1000 / self.sample_rate
                
                # Log significant corrections (>0.5ms) or periodically
                if abs(correction_ms) > 0.5:
                    logger.info(f"[BOOTSTRAP] LOCKED refinement: {correction_ms:+.2f}ms "
                               f"(error was {error_ms:+.1f}ms from {candidate.station})")
        
        return f"LOCKED: {candidate.station} error={error_ms:+.1f}ms"
    
    def _compute_offset(self):
        """
        Compute RTP-to-UTC offset from validated clusters.
        
        The offset is the RTP sample number that corresponds to UTC minute 0.
        For each validated cluster:
            RTP_of_UTC_minute = anchor_rtp - propagation_delay_samples
        
        We average across all validated clusters, weighted by confidence.
        """
        if not self.validated_clusters:
            logger.warning(f"[BOOTSTRAP] _compute_offset called with no validated_clusters")
            return
        
        offsets = []
        weights = []
        
        for cluster in self.validated_clusters:
            anchor_rtp = cluster['anchor_rtp']
            anchor_station = cluster['anchor_station']
            
            # Compute minute index from reference
            if self.reference_rtp is None:
                continue
            samples_from_ref = anchor_rtp - self.reference_rtp
            minute_index = round(samples_from_ref / SAMPLES_PER_MINUTE)
            
            # Get propagation delay for this station
            delay_samples = self.station_expectations.get(
                anchor_station, {}
            ).get('delay_samples', 0)
            
            # RTP of UTC minute boundary = anchor RTP - propagation delay
            # Then normalize to minute 0
            minute_rtp = anchor_rtp - delay_samples
            minute_0_rtp = minute_rtp - (minute_index * SAMPLES_PER_MINUTE)
            
            offsets.append(minute_0_rtp)
            # Weight by number of stations in cluster and SNR
            weight = cluster.get('num_stations', 1) * cluster.get('anchor_snr', 20.0) / 20.0
            weights.append(weight)
        
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
        
        # Reset two-tier bootstrap state
        self.lock_tier = LockTier.NONE
        self.provisional_lock_time = None
        self._offset_measurements.clear()
        self._refined_offset_samples = None
        self._refined_offset_std_ms = None
    
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
        """Get current bootstrap status including two-tier lock information."""
        import time
        
        status = {
            'state': self.state.value,
            'lock_tier': self.lock_tier.value,  # 0=none, 1=provisional, 2=refined
            'minutes_observed': self.minutes_observed,
            'validated_tones': len(self.validated_tones),
            'consecutive_validations': self.consecutive_validations,
            'consecutive_failures': self.consecutive_failures,
            'reference_channel': self.reference_channel,
            'reference_station': self.reference_station,
            'offset_samples': self.rtp_to_utc_offset_samples,
            'offset_uncertainty_samples': self.offset_uncertainty_samples,
        }
        
        # Add two-tier bootstrap details
        if self.lock_tier == LockTier.PROVISIONAL:
            elapsed = time.time() - self.provisional_lock_time if self.provisional_lock_time else 0
            status['provisional_lock_elapsed_sec'] = elapsed
            status['offset_measurements_count'] = len(self._offset_measurements)
            status['time_to_refined_sec'] = max(0, self.refined_lock_duration_sec - elapsed)
            
            # Compute current offset statistics if we have measurements
            if self._offset_measurements:
                offsets = [m.offset_samples for m in self._offset_measurements]
                mean_offset = sum(offsets) / len(offsets)
                variance = sum((o - mean_offset) ** 2 for o in offsets) / len(offsets)
                std_ms = sqrt(variance) * 1000 / self.sample_rate
                status['current_offset_std_ms'] = std_ms
        
        elif self.lock_tier == LockTier.REFINED:
            status['refined_offset_samples'] = self._refined_offset_samples
            status['refined_offset_std_ms'] = self._refined_offset_std_ms
            status['offset_measurements_count'] = len(self._offset_measurements)
        
        return status
    
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

