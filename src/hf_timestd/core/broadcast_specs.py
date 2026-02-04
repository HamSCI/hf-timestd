#!/usr/bin/env python3
"""
Broadcast Specifications - Complete Signal Definitions for All 17 Broadcasts

================================================================================
PURPOSE
================================================================================
This module defines the complete signal specifications for each of the 17 HF time
standard broadcasts. It is the single source of truth for:

- Tone frequencies and durations (per-second schedules)
- Skip patterns and special seconds
- Station-specific features (FSK, BCD, test signals, DUT1 encoding)
- Geographic coordinates
- Propagation bounds

The broadcast-centric architecture treats each (station, frequency) pair as a
unique scientific entity with its own ionospheric path, rather than grouping
by receiver channel.

================================================================================
THE 17 BROADCASTS
================================================================================
WWV (Fort Collins, CO):     2500, 5000, 10000, 15000, 20000, 25000 kHz (6)
WWVH (Kauai, HI):           2500, 5000, 10000, 15000 kHz (4)
CHU (Ottawa, Canada):       3330, 7850, 14670 kHz (3)
BPM (Pucheng, China):       2500, 5000, 10000, 15000 kHz (4)

Total: 17 unique broadcasts

================================================================================
FREQUENCY CONVENTION
================================================================================
All frequencies are in kHz (integers) to avoid floating-point comparison issues
and to match the existing directory naming convention (e.g., CHU_14670).

Conversion: MHz = kHz / 1000 (e.g., 14670 kHz = 14.67 MHz)

================================================================================
REFERENCES
================================================================================
- NIST Special Publication 432, "NIST Time and Frequency Services"
- NRC CHU Technical Specifications (https://nrc.canada.ca/en/chu-broadcast-codes)
- ITU-R TF.460-6, "Standard-frequency and time-signal emissions"
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, FrozenSet, List, Optional, Set, Tuple
import logging

logger = logging.getLogger(__name__)


# =============================================================================
# ENUMERATIONS
# =============================================================================

class Station(str, Enum):
    """Time standard broadcast stations."""
    WWV = "WWV"      # NIST Fort Collins, Colorado
    WWVH = "WWVH"    # NIST Kekaha, Kauai, Hawaii
    CHU = "CHU"      # NRC Ottawa, Ontario, Canada
    BPM = "BPM"      # NTSC Pucheng, Shaanxi, China


class FeatureType(str, Enum):
    """Station-specific signal features."""
    BCD_TIMECODE = "bcd"           # WWV/WWVH BCD time code
    FSK_TIMECODE = "fsk"           # CHU Bell 103 FSK
    TEST_SIGNAL = "test_signal"    # WWV/WWVH test signal (minutes 8/44)
    DUT1_SPLITS = "dut1_splits"    # CHU split tones for DUT1
    VOICE_ID = "voice_id"          # CHU voice announcement
    UT1_TICKS = "ut1_ticks"        # BPM 100ms UT1 ticks
    TONE_500_600 = "tone_500_600"  # WWV/WWVH 500/600 Hz tones


# =============================================================================
# STATION COORDINATES (Authoritative)
# =============================================================================

STATION_COORDINATES: Dict[Station, Tuple[float, float]] = {
    # WWV - Fort Collins, Colorado, USA
    # NIST official: 40° 40' 50.5" N, 105° 02' 26.6" W
    Station.WWV: (40.6807, -105.0407),
    
    # WWVH - Kekaha, Kauai, Hawaii, USA
    # NIST official: 21° 59' 14" N, 159° 45' 49" W
    Station.WWVH: (21.9872, -159.7636),
    
    # CHU - Ottawa, Ontario, Canada
    # NRC official: 45° 17' 47" N, 75° 45' 22" W
    Station.CHU: (45.2953, -75.7544),
    
    # BPM - Pucheng County, Shaanxi, China
    # 34° 56' 55.96" N, 109° 32' 34.93" E
    Station.BPM: (34.9489, 109.5430),
}


# =============================================================================
# FREQUENCY DEFINITIONS (kHz)
# =============================================================================

WWV_FREQUENCIES_KHZ: List[int] = [2500, 5000, 10000, 15000, 20000, 25000]
WWVH_FREQUENCIES_KHZ: List[int] = [2500, 5000, 10000, 15000]
CHU_FREQUENCIES_KHZ: List[int] = [3330, 7850, 14670]
BPM_FREQUENCIES_KHZ: List[int] = [2500, 5000, 10000, 15000]

# Shared frequencies (multiple stations, require discrimination)
SHARED_FREQUENCIES_KHZ: List[int] = [2500, 5000, 10000, 15000]

# Unique frequencies (single station, no discrimination needed)
UNIQUE_FREQUENCIES_KHZ: List[int] = [20000, 25000, 3330, 7850, 14670]

# All broadcast frequencies
ALL_FREQUENCIES_KHZ: List[int] = sorted(set(
    WWV_FREQUENCIES_KHZ + WWVH_FREQUENCIES_KHZ + 
    CHU_FREQUENCIES_KHZ + BPM_FREQUENCIES_KHZ
))


# =============================================================================
# TONE SCHEDULE DATA CLASS
# =============================================================================

@dataclass(frozen=True)
class ToneSchedule:
    """
    Complete per-second tone schedule for a broadcast.
    
    Defines what tone (if any) is expected at each second of the minute,
    including duration and any special characteristics.
    
    Attributes:
        tone_freq_hz: Audio frequency of timing tones (1000 or 1200 Hz)
        minute_marker_duration_ms: Duration of second-0 tone (ms)
        default_tick_duration_ms: Default duration for regular ticks (ms)
        skip_seconds: Seconds with no tone (silent)
        duration_overrides: {second: duration_ms} for non-default durations
        fsk_seconds: Seconds with FSK data (CHU only)
        voice_seconds: Seconds with voice announcement (CHU only)
        dut1_positive_seconds: Seconds for positive DUT1 encoding (CHU only)
        dut1_negative_seconds: Seconds for negative DUT1 encoding (CHU only)
        ut1_seconds: Seconds with 100ms UT1 ticks (BPM only)
    """
    tone_freq_hz: int
    minute_marker_duration_ms: float
    default_tick_duration_ms: float
    skip_seconds: FrozenSet[int]
    duration_overrides: Dict[int, float] = field(default_factory=dict)
    fsk_seconds: FrozenSet[int] = field(default_factory=frozenset)
    voice_seconds: FrozenSet[int] = field(default_factory=frozenset)
    dut1_positive_seconds: FrozenSet[int] = field(default_factory=frozenset)
    dut1_negative_seconds: FrozenSet[int] = field(default_factory=frozenset)
    ut1_seconds: FrozenSet[int] = field(default_factory=frozenset)
    
    def get_expected_duration_ms(
        self, 
        second: int, 
        minute: int = 0, 
        hour: int = 0
    ) -> Optional[float]:
        """
        Get expected tone duration for a specific second.
        
        Args:
            second: Second within minute (0-59)
            minute: Minute within hour (0-59), for hour-marker detection
            hour: Hour (0-23), currently unused
            
        Returns:
            Expected duration in ms, or None if second is silent
        """
        if second in self.skip_seconds:
            return None
        
        if second == 0:
            # CHU: 1000ms at top of hour, 500ms otherwise
            if minute == 0 and self.minute_marker_duration_ms == 500.0:
                return 1000.0  # CHU hour marker
            return self.minute_marker_duration_ms
        
        if second in self.duration_overrides:
            return self.duration_overrides[second]
        
        return self.default_tick_duration_ms
    
    def get_ticks_per_minute(self) -> int:
        """Return number of expected ticks per minute."""
        return 60 - len(self.skip_seconds)
    
    def is_special_second(self, second: int) -> bool:
        """Check if second has special features (FSK, voice, etc.)."""
        return (
            second in self.fsk_seconds or
            second in self.voice_seconds or
            second in self.ut1_seconds
        )


# =============================================================================
# STATION TONE SCHEDULES
# =============================================================================

# WWV: 1000 Hz, 800ms minute marker, 5ms ticks, skip 29 and 59
WWV_TONE_SCHEDULE = ToneSchedule(
    tone_freq_hz=1000,
    minute_marker_duration_ms=800.0,
    default_tick_duration_ms=5.0,
    skip_seconds=frozenset({29, 59}),
)

# WWVH: 1200 Hz, 800ms minute marker, 5ms ticks, skip 29 and 59
WWVH_TONE_SCHEDULE = ToneSchedule(
    tone_freq_hz=1200,
    minute_marker_duration_ms=800.0,
    default_tick_duration_ms=5.0,
    skip_seconds=frozenset({29, 59}),
)

# CHU: 1000 Hz, variable durations
# - Second 0: 500ms (1000ms at top of hour) - but second 0 is SILENT, marker at :59.5
# - Seconds 1-30 (except 29): 300ms regular tones
# - Seconds 31-39: 10ms ticks + FSK data
# - Seconds 40-49: 300ms regular tones  
# - Seconds 50-59: 10ms ticks + voice announcement
# - Second 29: ALWAYS SILENT
CHU_TONE_SCHEDULE = ToneSchedule(
    tone_freq_hz=1000,
    minute_marker_duration_ms=500.0,  # 1000ms at hour
    default_tick_duration_ms=300.0,
    skip_seconds=frozenset({0, 29}),  # Second 0 silent, marker at previous :59.5
    duration_overrides={
        **{s: 10.0 for s in range(31, 40)},  # FSK seconds: 10ms tick
        **{s: 10.0 for s in range(50, 60)},  # Voice seconds: 10ms tick
    },
    fsk_seconds=frozenset(range(31, 40)),
    voice_seconds=frozenset(range(50, 60)),
    dut1_positive_seconds=frozenset(range(1, 9)),
    dut1_negative_seconds=frozenset(range(9, 17)),
)

# BPM: 1000 Hz, 300ms minute marker, 10ms UTC ticks, 100ms UT1 ticks
BPM_TONE_SCHEDULE = ToneSchedule(
    tone_freq_hz=1000,
    minute_marker_duration_ms=300.0,
    default_tick_duration_ms=10.0,
    skip_seconds=frozenset(),  # BPM has no skipped seconds
    duration_overrides={
        **{s: 100.0 for s in range(25, 30)},   # UT1 ticks: 100ms
        **{s: 100.0 for s in range(55, 60)},   # UT1 ticks: 100ms
    },
    ut1_seconds=frozenset(range(25, 30)) | frozenset(range(55, 60)),
)

STATION_TONE_SCHEDULES: Dict[Station, ToneSchedule] = {
    Station.WWV: WWV_TONE_SCHEDULE,
    Station.WWVH: WWVH_TONE_SCHEDULE,
    Station.CHU: CHU_TONE_SCHEDULE,
    Station.BPM: BPM_TONE_SCHEDULE,
}


# =============================================================================
# BROADCAST SPECIFICATION
# =============================================================================

@dataclass(frozen=True)
class BroadcastSpec:
    """
    Complete specification for a single broadcast (station + frequency).
    
    This is the authoritative definition of what signals to expect from
    a specific broadcast and how to analyze them.
    
    Attributes:
        station: Station identifier (WWV, WWVH, CHU, BPM)
        frequency_khz: Carrier frequency in kHz (integer)
        tone_schedule: Per-second tone timing specification
        features: Set of station-specific features to analyze
        lat: Station latitude (degrees)
        lon: Station longitude (degrees)
        propagation_bounds_ms: (min, max) plausible propagation delay
        test_signal_minute: Minute with test signal (WWV=8, WWVH=44, None otherwise)
    """
    station: Station
    frequency_khz: int
    tone_schedule: ToneSchedule
    features: FrozenSet[FeatureType]
    lat: float
    lon: float
    propagation_bounds_ms: Tuple[float, float]
    test_signal_minute: Optional[int] = None
    
    @property
    def broadcast_id(self) -> str:
        """Unique broadcast identifier: 'WWV_10000' or 'CHU_7850'."""
        return f"{self.station.value}_{self.frequency_khz}"
    
    @property
    def frequency_mhz(self) -> float:
        """Frequency in MHz (for legacy compatibility)."""
        return self.frequency_khz / 1000.0
    
    @property
    def is_unique_frequency(self) -> bool:
        """True if this is the only station on this frequency."""
        return self.frequency_khz in UNIQUE_FREQUENCIES_KHZ
    
    @property
    def is_shared_frequency(self) -> bool:
        """True if multiple stations share this frequency."""
        return self.frequency_khz in SHARED_FREQUENCIES_KHZ
    
    @property
    def tone_freq_hz(self) -> int:
        """Audio frequency of timing tones."""
        return self.tone_schedule.tone_freq_hz
    
    @property
    def minute_marker_duration_ms(self) -> float:
        """Duration of minute marker tone."""
        return self.tone_schedule.minute_marker_duration_ms
    
    @property
    def ticks_per_minute(self) -> int:
        """Number of per-second ticks expected."""
        return self.tone_schedule.get_ticks_per_minute()
    
    def has_feature(self, feature: FeatureType) -> bool:
        """Check if this broadcast has a specific feature."""
        return feature in self.features
    
    def get_expected_duration_ms(self, second: int, minute: int = 0) -> Optional[float]:
        """Get expected tone duration for a specific second."""
        return self.tone_schedule.get_expected_duration_ms(second, minute)
    
    def to_dict(self) -> Dict:
        """Convert to dictionary for serialization."""
        return {
            'broadcast_id': self.broadcast_id,
            'station': self.station.value,
            'frequency_khz': self.frequency_khz,
            'frequency_mhz': self.frequency_mhz,
            'tone_freq_hz': self.tone_freq_hz,
            'minute_marker_duration_ms': self.minute_marker_duration_ms,
            'ticks_per_minute': self.ticks_per_minute,
            'features': [f.value for f in self.features],
            'lat': self.lat,
            'lon': self.lon,
            'propagation_bounds_ms': self.propagation_bounds_ms,
            'is_unique_frequency': self.is_unique_frequency,
            'test_signal_minute': self.test_signal_minute,
        }


# =============================================================================
# PROPAGATION BOUNDS (Bootstrap Mode)
# =============================================================================
# Conservative bounds for continental US receivers during bootstrap.
# These are widened to allow initial lock; calibrated bounds are tighter.

PROPAGATION_BOUNDS_MS: Dict[Station, Tuple[float, float]] = {
    Station.WWV: (-10.0, 80.0),    # Fort Collins: typically 5-25ms
    Station.WWVH: (0.0, 100.0),    # Hawaii: typically 15-50ms
    Station.CHU: (-10.0, 80.0),    # Ottawa: typically 5-30ms
    Station.BPM: (10.0, 150.0),    # China: typically 40-100ms (multi-hop)
}


# =============================================================================
# FEATURE SETS BY STATION
# =============================================================================

WWV_FEATURES: FrozenSet[FeatureType] = frozenset({
    FeatureType.BCD_TIMECODE,
    FeatureType.TEST_SIGNAL,
    FeatureType.TONE_500_600,
})

WWVH_FEATURES: FrozenSet[FeatureType] = frozenset({
    FeatureType.BCD_TIMECODE,
    FeatureType.TEST_SIGNAL,
    FeatureType.TONE_500_600,
})

CHU_FEATURES: FrozenSet[FeatureType] = frozenset({
    FeatureType.FSK_TIMECODE,
    FeatureType.DUT1_SPLITS,
    FeatureType.VOICE_ID,
})

BPM_FEATURES: FrozenSet[FeatureType] = frozenset({
    FeatureType.UT1_TICKS,
})


# =============================================================================
# BUILD ALL 17 BROADCAST SPECIFICATIONS
# =============================================================================

def _build_broadcast_specs() -> Dict[str, BroadcastSpec]:
    """Build specifications for all 17 broadcasts."""
    specs = {}
    
    # WWV broadcasts (6)
    for freq_khz in WWV_FREQUENCIES_KHZ:
        lat, lon = STATION_COORDINATES[Station.WWV]
        spec = BroadcastSpec(
            station=Station.WWV,
            frequency_khz=freq_khz,
            tone_schedule=WWV_TONE_SCHEDULE,
            features=WWV_FEATURES,
            lat=lat,
            lon=lon,
            propagation_bounds_ms=PROPAGATION_BOUNDS_MS[Station.WWV],
            test_signal_minute=8,
        )
        specs[spec.broadcast_id] = spec
    
    # WWVH broadcasts (4)
    for freq_khz in WWVH_FREQUENCIES_KHZ:
        lat, lon = STATION_COORDINATES[Station.WWVH]
        spec = BroadcastSpec(
            station=Station.WWVH,
            frequency_khz=freq_khz,
            tone_schedule=WWVH_TONE_SCHEDULE,
            features=WWVH_FEATURES,
            lat=lat,
            lon=lon,
            propagation_bounds_ms=PROPAGATION_BOUNDS_MS[Station.WWVH],
            test_signal_minute=44,
        )
        specs[spec.broadcast_id] = spec
    
    # CHU broadcasts (3)
    for freq_khz in CHU_FREQUENCIES_KHZ:
        lat, lon = STATION_COORDINATES[Station.CHU]
        spec = BroadcastSpec(
            station=Station.CHU,
            frequency_khz=freq_khz,
            tone_schedule=CHU_TONE_SCHEDULE,
            features=CHU_FEATURES,
            lat=lat,
            lon=lon,
            propagation_bounds_ms=PROPAGATION_BOUNDS_MS[Station.CHU],
            test_signal_minute=None,
        )
        specs[spec.broadcast_id] = spec
    
    # BPM broadcasts (4)
    for freq_khz in BPM_FREQUENCIES_KHZ:
        lat, lon = STATION_COORDINATES[Station.BPM]
        spec = BroadcastSpec(
            station=Station.BPM,
            frequency_khz=freq_khz,
            tone_schedule=BPM_TONE_SCHEDULE,
            features=BPM_FEATURES,
            lat=lat,
            lon=lon,
            propagation_bounds_ms=PROPAGATION_BOUNDS_MS[Station.BPM],
            test_signal_minute=None,
        )
        specs[spec.broadcast_id] = spec
    
    return specs


# The authoritative registry of all 17 broadcasts
BROADCAST_SPECS: Dict[str, BroadcastSpec] = _build_broadcast_specs()


# =============================================================================
# LOOKUP FUNCTIONS
# =============================================================================

def get_broadcast_spec(station: str, frequency_khz: int) -> Optional[BroadcastSpec]:
    """
    Get broadcast specification by station and frequency.
    
    Args:
        station: Station name ('WWV', 'WWVH', 'CHU', 'BPM')
        frequency_khz: Frequency in kHz
        
    Returns:
        BroadcastSpec or None if not found
    """
    broadcast_id = f"{station}_{frequency_khz}"
    return BROADCAST_SPECS.get(broadcast_id)


def get_broadcast_spec_by_id(broadcast_id: str) -> Optional[BroadcastSpec]:
    """
    Get broadcast specification by broadcast ID.
    
    Args:
        broadcast_id: Broadcast ID (e.g., 'WWV_10000', 'CHU_7850')
        
    Returns:
        BroadcastSpec or None if not found
    """
    return BROADCAST_SPECS.get(broadcast_id)


def get_broadcasts_for_frequency(frequency_khz: int) -> List[BroadcastSpec]:
    """
    Get all broadcasts on a given frequency.
    
    Args:
        frequency_khz: Frequency in kHz
        
    Returns:
        List of BroadcastSpec for all stations broadcasting on this frequency
    """
    return [
        spec for spec in BROADCAST_SPECS.values()
        if spec.frequency_khz == frequency_khz
    ]


def get_broadcasts_for_station(station: str) -> List[BroadcastSpec]:
    """
    Get all broadcasts for a given station.
    
    Args:
        station: Station name ('WWV', 'WWVH', 'CHU', 'BPM')
        
    Returns:
        List of BroadcastSpec for all frequencies this station broadcasts on
    """
    return [
        spec for spec in BROADCAST_SPECS.values()
        if spec.station.value == station
    ]


def get_channel_broadcasts(channel_name: str) -> List[BroadcastSpec]:
    """
    Get broadcasts receivable on a channel.
    
    Args:
        channel_name: Channel name (e.g., 'SHARED_10000', 'CHU_7850', 'WWV_20000')
        
    Returns:
        List of BroadcastSpec for broadcasts on this channel
    """
    # Extract frequency from channel name
    parts = channel_name.upper().replace(' ', '_').split('_')
    if len(parts) < 2:
        return []
    
    try:
        freq_khz = int(parts[-1])
    except ValueError:
        return []
    
    # Get all broadcasts on this frequency
    broadcasts = get_broadcasts_for_frequency(freq_khz)
    
    # If channel specifies a station (not SHARED), filter to that station
    station_prefix = parts[0]
    if station_prefix != 'SHARED':
        broadcasts = [b for b in broadcasts if b.station.value == station_prefix]
    
    return broadcasts


def list_all_broadcast_ids() -> List[str]:
    """Return list of all 17 broadcast IDs."""
    return sorted(BROADCAST_SPECS.keys())


def list_broadcasts_by_station() -> Dict[str, List[str]]:
    """Return broadcasts organized by station."""
    result = {}
    for station in Station:
        result[station.value] = [
            spec.broadcast_id for spec in get_broadcasts_for_station(station.value)
        ]
    return result


# =============================================================================
# FREQUENCY CONVERSION UTILITIES
# =============================================================================

def khz_to_mhz(khz: int) -> float:
    """Convert kHz to MHz."""
    return khz / 1000.0


def mhz_to_khz(mhz: float) -> int:
    """Convert MHz to kHz (rounded to nearest integer)."""
    return int(round(mhz * 1000))


def normalize_frequency_khz(freq: float) -> int:
    """
    Normalize a frequency to kHz integer.
    
    Handles both MHz (< 100) and kHz (>= 100) inputs.
    
    Args:
        freq: Frequency in MHz or kHz
        
    Returns:
        Frequency in kHz (integer)
    """
    if freq < 100:
        # Assume MHz
        return mhz_to_khz(freq)
    else:
        # Assume already kHz
        return int(round(freq))


# =============================================================================
# MODULE INITIALIZATION
# =============================================================================

logger.info(f"Loaded {len(BROADCAST_SPECS)} broadcast specifications")
