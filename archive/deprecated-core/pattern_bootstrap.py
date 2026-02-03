"""
Pattern-Based Bootstrap for RTP-to-UTC Offset Calibration

This module implements a bootstrap algorithm that derives the RTP-to-UTC offset
purely from the physical reality of radio propagation, without relying on
system time accuracy.

Key Insight:
-----------
We KNOW that time station tones are emitted exactly on UTC minute boundaries.
We KNOW the geographic locations of transmitters and our receiver.
Therefore, we can PREDICT the relative arrival times of tones from different stations.

The arrival pattern itself IS the clock reference.

Algorithm:
---------
1. Mark RTP timestamp when first tone detected on unambiguous channel
2. Predict when next tones should arrive (RTP + 1,440,000 samples per minute)
3. Validate arrival sequence matches geographic expectations:
   - CHU, WWV 20MHz, WWV 25MHz are unambiguous (only one station)
   - On shared frequencies: WWV arrives before WWVH (from Missouri)
4. Once pattern confirmed across multiple minutes, derive offset:
   RTP_of_UTC_minute = RTP_of_tone - propagation_delay_samples

Geographic Priors (from Missouri ~38.9°N, 92.1°W):
-------------------------------------------------
- CHU (Ottawa, 45.3°N, 75.9°W): ~1500 km, ~5-15ms propagation
- WWV (Colorado, 40.7°N, 105.0°W): ~1100 km, ~4-12ms propagation
- WWVH (Hawaii, 21.99°N, 159.76°W): ~5500 km, ~18-40ms propagation

Expected arrival order on shared frequencies: WWV first, then WWVH ~15-30ms later
"""

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple
from math import radians, sin, cos, sqrt, atan2

logger = logging.getLogger(__name__)

# Sample rate
SAMPLE_RATE = 24000
SAMPLES_PER_MINUTE = SAMPLE_RATE * 60  # 1,440,000

# Station locations (lat, lon in degrees)
STATION_LOCATIONS = {
    'CHU': (45.2975, -75.7533),    # Ottawa, Canada
    'WWV': (40.6781, -105.0469),   # Fort Collins, Colorado
    'WWVH': (21.9886, -159.7642),  # Kekaha, Hawaii
    'BPM': (34.95, 109.55),        # Pucheng, China
}

# Unambiguous channels (only one station transmits)
UNAMBIGUOUS_CHANNELS = {
    'CHU_3330': 'CHU',
    'CHU_7850': 'CHU', 
    'CHU_14670': 'CHU',
    'WWV_20000': 'WWV',
    'WWV_25000': 'WWV',
}

# Shared frequencies where WWV and WWVH both transmit
SHARED_FREQUENCIES_KHZ = [2500, 5000, 10000, 15000]


class BootstrapState(Enum):
    """Bootstrap state machine."""
    LISTENING = "listening"      # Waiting for first tone
    TRACKING = "tracking"        # Tracking tone pattern
    VALIDATING = "validating"    # Validating multi-station agreement
    LOCKED = "locked"            # Offset confirmed and locked


@dataclass
class ToneArrival:
    """Record of a detected tone arrival."""
    rtp_timestamp: int           # RTP sample number at tone detection
    channel: str                 # e.g., "CHU_14670"
    station: str                 # Identified station
    frequency_khz: int           # Frequency in kHz
    confidence: float            # Detection confidence
    snr_db: float               # Signal quality
    sample_position: int         # Position within buffer
    minute_index: int = 0        # Which minute (0 = first detected)


@dataclass  
class StationExpectation:
    """Expected arrival characteristics for a station."""
    station: str
    distance_km: float
    propagation_delay_ms: float
    propagation_delay_samples: int
    
    
@dataclass
class PatternBootstrap:
    """
    Pattern-based bootstrap for RTP-to-UTC offset calibration.
    
    Uses geographic priors and tone arrival patterns to derive the
    RTP-to-UTC offset without relying on system time accuracy.
    """
    receiver_lat: float
    receiver_lon: float
    sample_rate: int = SAMPLE_RATE
    
    # State
    state: BootstrapState = field(default=BootstrapState.LISTENING)
    
    # Reference point: RTP timestamp of first confirmed tone
    reference_rtp: Optional[int] = None
    reference_channel: Optional[str] = None
    reference_minute: int = 0  # Minute counter from first detection
    
    # Arrival history per channel
    arrivals: Dict[str, List[ToneArrival]] = field(default_factory=dict)
    
    # Derived offset (once locked)
    rtp_to_utc_offset_sec: Optional[float] = None
    offset_uncertainty_ms: float = 50.0  # Initial uncertainty
    
    # Validation counters
    minutes_tracked: int = 0
    pattern_matches: int = 0
    pattern_failures: int = 0
    
    def __post_init__(self):
        """Calculate expected propagation delays from receiver location."""
        self.expectations: Dict[str, StationExpectation] = {}
        
        for station, (lat, lon) in STATION_LOCATIONS.items():
            distance_km = self._haversine_km(
                self.receiver_lat, self.receiver_lon, lat, lon
            )
            # Speed of light: 299,792 km/s
            # But HF propagation via ionosphere is longer path
            # Use 1.1x great circle as rough estimate
            effective_distance = distance_km * 1.1
            delay_ms = effective_distance / 299.792
            delay_samples = int(delay_ms * self.sample_rate / 1000)
            
            self.expectations[station] = StationExpectation(
                station=station,
                distance_km=distance_km,
                propagation_delay_ms=delay_ms,
                propagation_delay_samples=delay_samples
            )
            
            logger.info(
                f"[BOOTSTRAP] {station}: distance={distance_km:.0f}km, "
                f"expected_delay={delay_ms:.1f}ms ({delay_samples} samples)"
            )
    
    def _haversine_km(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Calculate great circle distance in km."""
        R = 6371  # Earth radius in km
        
        lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
        dlat = lat2 - lat1
        dlon = lon2 - lon1
        
        a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
        c = 2 * atan2(sqrt(a), sqrt(1-a))
        
        return R * c
    
    def add_detection(
        self,
        channel: str,
        station: str,
        rtp_timestamp: int,
        sample_position: int,
        confidence: float,
        snr_db: float
    ) -> Optional[float]:
        """
        Add a tone detection and check if we can derive the offset.
        
        Args:
            channel: Channel name (e.g., "CHU_14670")
            station: Identified station
            rtp_timestamp: RTP timestamp at buffer start
            sample_position: Sample position of tone within buffer
            confidence: Detection confidence (0-1)
            snr_db: Signal-to-noise ratio
            
        Returns:
            RTP-to-UTC offset in seconds if locked, None otherwise
        """
        # Calculate exact RTP of tone
        tone_rtp = rtp_timestamp + sample_position
        
        # Extract frequency from channel name
        try:
            freq_khz = int(channel.split('_')[1])
        except (IndexError, ValueError):
            freq_khz = 0
        
        # Determine minute index relative to reference
        if self.reference_rtp is not None:
            samples_since_ref = tone_rtp - self.reference_rtp
            minute_index = round(samples_since_ref / SAMPLES_PER_MINUTE)
        else:
            minute_index = 0
        
        arrival = ToneArrival(
            rtp_timestamp=tone_rtp,
            channel=channel,
            station=station,
            frequency_khz=freq_khz,
            confidence=confidence,
            snr_db=snr_db,
            sample_position=sample_position,
            minute_index=minute_index
        )
        
        # Store arrival
        if channel not in self.arrivals:
            self.arrivals[channel] = []
        self.arrivals[channel].append(arrival)
        
        # State machine
        if self.state == BootstrapState.LISTENING:
            return self._handle_listening(arrival)
        elif self.state == BootstrapState.TRACKING:
            return self._handle_tracking(arrival)
        elif self.state == BootstrapState.VALIDATING:
            return self._handle_validating(arrival)
        elif self.state == BootstrapState.LOCKED:
            return self.rtp_to_utc_offset_sec
        
        return None
    
    def _handle_listening(self, arrival: ToneArrival) -> Optional[float]:
        """
        LISTENING state: Wait for first high-confidence tone on unambiguous channel.
        """
        # Only accept unambiguous channels for initial reference
        if arrival.channel not in UNAMBIGUOUS_CHANNELS:
            logger.debug(f"[BOOTSTRAP] Ignoring ambiguous channel {arrival.channel} during LISTENING")
            return None
        
        # Require high confidence
        if arrival.confidence < 0.7:
            logger.debug(f"[BOOTSTRAP] Low confidence {arrival.confidence:.2f} on {arrival.channel}")
            return None
        
        # Require decent SNR
        if arrival.snr_db < 6.0:
            logger.debug(f"[BOOTSTRAP] Low SNR {arrival.snr_db:.1f}dB on {arrival.channel}")
            return None
        
        # This is our reference point
        self.reference_rtp = arrival.rtp_timestamp
        self.reference_channel = arrival.channel
        self.reference_minute = 0
        self.state = BootstrapState.TRACKING
        
        logger.info(
            f"[BOOTSTRAP] Reference established: {arrival.channel} at RTP={arrival.rtp_timestamp}, "
            f"confidence={arrival.confidence:.2f}, SNR={arrival.snr_db:.1f}dB"
        )
        logger.info(f"[BOOTSTRAP] State: LISTENING -> TRACKING")
        
        return None
    
    def _handle_tracking(self, arrival: ToneArrival) -> Optional[float]:
        """
        TRACKING state: Collect arrivals and check pattern consistency.
        """
        # Check if this arrival matches expected timing
        expected_rtp = self.reference_rtp + (arrival.minute_index * SAMPLES_PER_MINUTE)
        
        # Add expected propagation delay for this station
        if arrival.station in self.expectations:
            expected_rtp += self.expectations[arrival.station].propagation_delay_samples
        
        # Calculate timing error
        timing_error_samples = arrival.rtp_timestamp - expected_rtp
        timing_error_ms = timing_error_samples * 1000 / self.sample_rate
        
        # Log arrival
        logger.info(
            f"[BOOTSTRAP] {arrival.channel} minute={arrival.minute_index}: "
            f"timing_error={timing_error_ms:+.1f}ms, conf={arrival.confidence:.2f}"
        )
        
        # Check if timing is consistent (within ±50ms for now)
        if abs(timing_error_ms) < 50:
            self.pattern_matches += 1
        else:
            self.pattern_failures += 1
            logger.warning(
                f"[BOOTSTRAP] Pattern mismatch: {arrival.channel} off by {timing_error_ms:.1f}ms"
            )
        
        # Track minutes
        if arrival.minute_index > self.minutes_tracked:
            self.minutes_tracked = arrival.minute_index
        
        # Check if we have enough data to validate
        # Need at least 3 minutes and 2+ stations
        stations_seen = set()
        for ch, arrs in self.arrivals.items():
            for a in arrs:
                if a.minute_index > 0:  # Exclude reference minute
                    stations_seen.add(a.station)
        
        if self.minutes_tracked >= 3 and len(stations_seen) >= 2 and self.pattern_matches >= 5:
            self.state = BootstrapState.VALIDATING
            logger.info(
                f"[BOOTSTRAP] State: TRACKING -> VALIDATING "
                f"(minutes={self.minutes_tracked}, stations={stations_seen}, matches={self.pattern_matches})"
            )
            return self._validate_and_lock()
        
        return None
    
    def _handle_validating(self, arrival: ToneArrival) -> Optional[float]:
        """
        VALIDATING state: Final validation before locking.
        """
        return self._validate_and_lock()
    
    def _validate_and_lock(self) -> Optional[float]:
        """
        Validate the pattern and compute the RTP-to-UTC offset.
        """
        # Check WWV vs WWVH ordering on shared frequencies
        wwv_wwvh_valid = self._validate_wwv_wwvh_ordering()
        
        if not wwv_wwvh_valid:
            logger.warning("[BOOTSTRAP] WWV/WWVH ordering validation failed")
            # Don't lock yet, keep collecting
            return None
        
        # Compute offset from reference
        # The reference RTP corresponds to a tone arrival
        # Tone was emitted at UTC minute boundary
        # Tone arrived after propagation delay
        
        ref_station = UNAMBIGUOUS_CHANNELS.get(self.reference_channel, 'UNKNOWN')
        if ref_station not in self.expectations:
            logger.error(f"[BOOTSTRAP] Unknown reference station: {ref_station}")
            return None
        
        prop_delay_samples = self.expectations[ref_station].propagation_delay_samples
        prop_delay_sec = prop_delay_samples / self.sample_rate
        
        # RTP of UTC minute boundary = RTP of tone - propagation delay
        utc_minute_rtp = self.reference_rtp - prop_delay_samples
        
        # The offset is: UTC_time = RTP / sample_rate + offset
        # At the minute boundary: UTC_minute = utc_minute_rtp / sample_rate + offset
        # We don't know which minute, but we know it's an integer number of minutes
        # For now, use system time to identify which minute (just for labeling)
        
        # The key insight: we now know the RTP position of a UTC minute boundary
        # This is the "anchor" for all future timing
        
        # Store the offset as seconds per RTP sample (should be ~1/24000)
        # Actually, we need: offset = UTC_time - RTP/sample_rate
        # We'll compute this when we have a system time reference
        
        self.state = BootstrapState.LOCKED
        self.offset_uncertainty_ms = 10.0  # Reduced after validation
        
        logger.info(
            f"[BOOTSTRAP] ✓ LOCKED! Reference: {self.reference_channel} "
            f"(propagation={prop_delay_sec*1000:.1f}ms)"
        )
        logger.info(
            f"[BOOTSTRAP] UTC minute boundary at RTP={utc_minute_rtp} "
            f"(tone at RTP={self.reference_rtp})"
        )
        
        # Return the propagation-corrected RTP as a marker
        # The caller can use this with system time to compute the actual offset
        return utc_minute_rtp / self.sample_rate
    
    def _validate_wwv_wwvh_ordering(self) -> bool:
        """
        Validate that WWV arrives before WWVH on shared frequencies.
        
        This is a key geographic constraint that confirms station identification.
        """
        # Find pairs of WWV/WWVH detections on same frequency, same minute
        for freq_khz in SHARED_FREQUENCIES_KHZ:
            wwv_channel = f"WWV_{freq_khz}"
            wwvh_channel = f"WWVH_{freq_khz}"
            # Also check SHARED_ channels
            shared_channel = f"SHARED_{freq_khz}"
            
            wwv_arrivals = self.arrivals.get(wwv_channel, [])
            wwvh_arrivals = self.arrivals.get(wwvh_channel, [])
            
            # Group by minute
            for wwv_arr in wwv_arrivals:
                for wwvh_arr in wwvh_arrivals:
                    if wwv_arr.minute_index == wwvh_arr.minute_index:
                        # Same minute - check ordering
                        delay_diff_samples = wwvh_arr.rtp_timestamp - wwv_arr.rtp_timestamp
                        delay_diff_ms = delay_diff_samples * 1000 / self.sample_rate
                        
                        # WWVH should arrive 10-40ms after WWV (from Missouri)
                        expected_diff = (
                            self.expectations['WWVH'].propagation_delay_ms -
                            self.expectations['WWV'].propagation_delay_ms
                        )
                        
                        logger.info(
                            f"[BOOTSTRAP] {freq_khz}kHz minute={wwv_arr.minute_index}: "
                            f"WWVH-WWV={delay_diff_ms:.1f}ms (expected ~{expected_diff:.1f}ms)"
                        )
                        
                        # Validate: WWVH should be later, within reasonable bounds
                        if delay_diff_ms < 5:  # WWVH arrived before or same as WWV
                            logger.warning(
                                f"[BOOTSTRAP] Invalid ordering: WWVH arrived {-delay_diff_ms:.1f}ms "
                                f"BEFORE WWV on {freq_khz}kHz"
                            )
                            return False
        
        return True
    
    def get_status(self) -> dict:
        """Get current bootstrap status."""
        return {
            'state': self.state.value,
            'reference_channel': self.reference_channel,
            'reference_rtp': self.reference_rtp,
            'minutes_tracked': self.minutes_tracked,
            'pattern_matches': self.pattern_matches,
            'pattern_failures': self.pattern_failures,
            'channels_seen': list(self.arrivals.keys()),
            'offset_locked': self.state == BootstrapState.LOCKED,
            'offset_sec': self.rtp_to_utc_offset_sec,
            'uncertainty_ms': self.offset_uncertainty_ms,
        }
    
    def get_utc_minute_rtp(self) -> Optional[int]:
        """
        Get the RTP timestamp corresponding to a UTC minute boundary.
        
        Returns None if not yet locked.
        """
        if self.state != BootstrapState.LOCKED:
            return None
        
        if self.reference_rtp is None or self.reference_channel is None:
            return None
        
        ref_station = UNAMBIGUOUS_CHANNELS.get(self.reference_channel, 'UNKNOWN')
        if ref_station not in self.expectations:
            return None
        
        prop_delay_samples = self.expectations[ref_station].propagation_delay_samples
        return self.reference_rtp - prop_delay_samples
