"""
Broadcast Registry - Station-Centric Data Model

This module provides the core data structures for the station-centric architecture:
- BroadcastStation: A time signal transmitter (WWV, WWVH, CHU, BPM)
- Broadcast: A single station+frequency combination (17 total)
- BroadcastRegistry: Registry of all broadcasts with computed geometry

The registry is the foundation for:
- Channel derivation (radiod: 9 channels, phase-engine: 17 channels)
- Station-centric API endpoints
- Per-station D_clock aggregation
- TEC computation grouping
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple
from enum import Enum
import math
import logging

from hamsci_dsp.geometry import great_circle_km

logger = logging.getLogger(__name__)

# Physical constants
C_LIGHT_KM_S = 299792.458  # Speed of light in km/s


class SourceMode(str, Enum):
    """Data source mode determining channel derivation."""
    RADIOD = "radiod"           # 9 channels (unique frequencies), discrimination required
    PHASE_ENGINE = "phase-engine"  # 17 channels (one per broadcast), discrimination bypassed


class TonePattern(str, Enum):
    """Tone pattern for station discrimination."""
    WWV_1000HZ = "1000Hz"       # WWV: 1000 Hz tone
    WWVH_1200HZ = "1200Hz"      # WWVH: 1200 Hz tone
    CHU_BCD_FSK = "BCD_FSK"     # CHU: Bell 103 AFSK BCD
    BPM_1000HZ = "1000Hz_BPM"   # BPM: 1000 Hz (similar to WWV, different BCD)


@dataclass
class BroadcastStation:
    """
    A time signal transmitter station.
    
    This represents the physical station (WWV, WWVH, CHU, BPM) with its
    location and broadcast frequencies.
    """
    name: str
    latitude: float
    longitude: float
    frequencies_hz: List[int]
    tone_pattern: TonePattern
    location: str = ""
    
    @property
    def frequencies_mhz(self) -> List[float]:
        """Frequencies in MHz."""
        return [f / 1e6 for f in self.frequencies_hz]
    
    def broadcasts(self) -> List[Tuple[str, int]]:
        """Return list of (station_name, frequency_hz) tuples."""
        return [(self.name, f) for f in self.frequencies_hz]


@dataclass
class ReceiverLocation:
    """Receiver station location."""
    callsign: str
    latitude: float
    longitude: float
    grid_square: str = ""
    station_id: str = ""
    description: str = ""


@dataclass
class Broadcast:
    """
    A single broadcast: station + frequency combination.
    
    There are 17 broadcasts total:
    - WWV: 6 frequencies (2.5, 5, 10, 15, 20, 25 MHz)
    - WWVH: 4 frequencies (2.5, 5, 10, 15 MHz)
    - CHU: 3 frequencies (3.33, 7.85, 14.67 MHz)
    - BPM: 4 frequencies (2.5, 5, 10, 15 MHz)
    """
    station: str
    frequency_hz: int
    broadcast_id: str  # e.g., "WWV_5000"
    
    # Station properties (copied for convenience)
    station_lat: float
    station_lon: float
    tone_pattern: TonePattern
    
    # Computed geometry (from receiver location)
    distance_km: float = 0.0
    azimuth_deg: float = 0.0
    min_propagation_ms: float = 0.0  # distance / c (vacuum, no ionosphere)
    
    # Channel assignment
    channel_name: str = ""  # Assigned channel name
    requires_discrimination: bool = False  # True if shared frequency
    
    @property
    def frequency_mhz(self) -> float:
        """Frequency in MHz."""
        return self.frequency_hz / 1e6
    
    @property
    def frequency_khz(self) -> int:
        """Frequency in kHz (for channel naming)."""
        return self.frequency_hz // 1000


@dataclass
class DerivedChannel:
    """
    A derived channel for recording.
    
    In radiod mode: 9 channels (unique frequencies)
    In phase-engine mode: 17 channels (one per broadcast)
    """
    name: str
    frequency_hz: int
    stations: List[str]  # Stations that broadcast on this frequency
    requires_discrimination: bool
    
    # Phase-engine specific
    target_station: Optional[str] = None  # For phase-engine: which station to beam toward
    beam_azimuth_deg: Optional[float] = None


# =============================================================================
# DEFAULT STATION DEFINITIONS
# =============================================================================
# These are the canonical time signal stations. In the future, these could
# be loaded from config, but for now they're hardcoded as they rarely change.

DEFAULT_STATIONS = [
    BroadcastStation(
        name="WWV",
        location="Fort Collins, CO",
        latitude=40.67805,
        longitude=-105.04719,
        frequencies_hz=[2500000, 5000000, 10000000, 15000000, 20000000, 25000000],
        tone_pattern=TonePattern.WWV_1000HZ,
    ),
    BroadcastStation(
        name="WWVH",
        location="Kekaha, HI",
        latitude=21.98830,
        longitude=-159.76220,
        frequencies_hz=[2500000, 5000000, 10000000, 15000000],
        tone_pattern=TonePattern.WWVH_1200HZ,
    ),
    BroadcastStation(
        name="CHU",
        location="Ottawa, ON",
        latitude=45.29525,
        longitude=-75.75433,
        frequencies_hz=[3330000, 7850000, 14670000],
        tone_pattern=TonePattern.CHU_BCD_FSK,
    ),
    BroadcastStation(
        name="BPM",
        location="Pucheng, Shaanxi",
        latitude=34.94833,
        longitude=109.54167,
        frequencies_hz=[2500000, 5000000, 10000000, 15000000],
        tone_pattern=TonePattern.BPM_1000HZ,
    ),
]


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Delegates to hamsci_dsp.geometry.great_circle_km (geodesic WGS-84).

    Args:
        lat1, lon1: First point (degrees)
        lat2, lon2: Second point (degrees)

    Returns:
        Distance in kilometers
    """
    return great_circle_km(lat1, lon1, lat2, lon2)


def bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculate initial bearing from point 1 to point 2.
    
    Args:
        lat1, lon1: Starting point (degrees)
        lat2, lon2: Ending point (degrees)
        
    Returns:
        Bearing in degrees (0-360, clockwise from north)
    """
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    
    x = math.sin(dlon) * math.cos(lat2_rad)
    y = math.cos(lat1_rad) * math.sin(lat2_rad) - math.sin(lat1_rad) * math.cos(lat2_rad) * math.cos(dlon)
    
    bearing_rad = math.atan2(x, y)
    bearing_deg = math.degrees(bearing_rad)
    
    return (bearing_deg + 360) % 360


class BroadcastRegistry:
    """
    Registry of all broadcasts with computed geometry.
    
    This is the central data structure for the station-centric architecture.
    It provides:
    - All 17 broadcasts with computed distance/azimuth from receiver
    - Channel derivation for radiod (9) or phase-engine (17) modes
    - Station lookup by name
    - Broadcast lookup by ID
    """
    
    def __init__(
        self,
        receiver: ReceiverLocation,
        stations: Optional[List[BroadcastStation]] = None,
        source_mode: SourceMode = SourceMode.RADIOD
    ):
        """
        Initialize broadcast registry.
        
        Args:
            receiver: Receiver location for geometry computation
            stations: List of broadcast stations (defaults to canonical stations)
            source_mode: Data source mode (radiod or phase-engine)
        """
        self.receiver = receiver
        self.stations = stations or DEFAULT_STATIONS
        self.source_mode = source_mode
        
        # Build registries
        self._stations_by_name: Dict[str, BroadcastStation] = {}
        self._broadcasts_by_id: Dict[str, Broadcast] = {}
        self._broadcasts_by_station: Dict[str, List[Broadcast]] = {}
        self._broadcasts_by_frequency: Dict[int, List[Broadcast]] = {}
        self._channels: List[DerivedChannel] = []
        
        self._build_registry()
        self._derive_channels()
        
        logger.info(
            f"BroadcastRegistry initialized: {len(self._broadcasts_by_id)} broadcasts, "
            f"{len(self._channels)} channels ({self.source_mode.value} mode)"
        )
    
    def _build_registry(self):
        """Build the broadcast registry with computed geometry."""
        for station in self.stations:
            self._stations_by_name[station.name] = station
            self._broadcasts_by_station[station.name] = []
            
            for freq_hz in station.frequencies_hz:
                broadcast_id = f"{station.name}_{freq_hz // 1000}"
                
                # Compute geometry
                distance = haversine_distance(
                    self.receiver.latitude, self.receiver.longitude,
                    station.latitude, station.longitude
                )
                azimuth = bearing(
                    self.receiver.latitude, self.receiver.longitude,
                    station.latitude, station.longitude
                )
                min_prop_ms = distance / C_LIGHT_KM_S * 1000  # Convert to ms
                
                broadcast = Broadcast(
                    station=station.name,
                    frequency_hz=freq_hz,
                    broadcast_id=broadcast_id,
                    station_lat=station.latitude,
                    station_lon=station.longitude,
                    tone_pattern=station.tone_pattern,
                    distance_km=distance,
                    azimuth_deg=azimuth,
                    min_propagation_ms=min_prop_ms,
                )
                
                self._broadcasts_by_id[broadcast_id] = broadcast
                self._broadcasts_by_station[station.name].append(broadcast)
                
                if freq_hz not in self._broadcasts_by_frequency:
                    self._broadcasts_by_frequency[freq_hz] = []
                self._broadcasts_by_frequency[freq_hz].append(broadcast)
    
    def _derive_channels(self):
        """Derive channels based on source mode."""
        if self.source_mode == SourceMode.RADIOD:
            self._derive_channels_radiod()
        else:
            self._derive_channels_phase_engine()
    
    def _derive_channels_radiod(self):
        """
        Derive 9 frequency-based channels for radiod mode.
        
        Shared frequencies (2.5, 5, 10, 15 MHz) require discrimination.
        Unique frequencies (20, 25, 3.33, 7.85, 14.67 MHz) do not.
        """
        for freq_hz, broadcasts in sorted(self._broadcasts_by_frequency.items()):
            station_names = [b.station for b in broadcasts]
            requires_disc = len(station_names) > 1
            
            if requires_disc:
                name = f"SHARED_{freq_hz // 1000}"
            else:
                name = f"{station_names[0]}_{freq_hz // 1000}"
            
            channel = DerivedChannel(
                name=name,
                frequency_hz=freq_hz,
                stations=station_names,
                requires_discrimination=requires_disc,
            )
            self._channels.append(channel)
            
            # Update broadcasts with channel assignment
            for broadcast in broadcasts:
                broadcast.channel_name = name
                broadcast.requires_discrimination = requires_disc
    
    def _derive_channels_phase_engine(self):
        """
        Derive 17 broadcast-specific channels for phase-engine mode.
        
        Each broadcast gets its own channel with beam direction.
        """
        for broadcast_id, broadcast in sorted(self._broadcasts_by_id.items()):
            channel = DerivedChannel(
                name=broadcast_id,  # e.g., "WWV_5000"
                frequency_hz=broadcast.frequency_hz,
                stations=[broadcast.station],
                requires_discrimination=False,  # Phase-engine provides isolation
                target_station=broadcast.station,
                beam_azimuth_deg=broadcast.azimuth_deg,
            )
            self._channels.append(channel)
            
            # Update broadcast with channel assignment
            broadcast.channel_name = broadcast_id
            broadcast.requires_discrimination = False
    
    # =========================================================================
    # PUBLIC API
    # =========================================================================
    
    @property
    def broadcasts(self) -> Dict[str, Broadcast]:
        """All broadcasts by ID."""
        return self._broadcasts_by_id
    
    @property
    def channels(self) -> List[DerivedChannel]:
        """Derived channels for recording."""
        return self._channels
    
    @property
    def n_broadcasts(self) -> int:
        """Total number of broadcasts (17)."""
        return len(self._broadcasts_by_id)
    
    @property
    def n_channels(self) -> int:
        """Number of derived channels (9 for radiod, 17 for phase-engine)."""
        return len(self._channels)
    
    @property
    def n_stations(self) -> int:
        """Number of stations (4)."""
        return len(self._stations_by_name)
    
    def get_station(self, name: str) -> Optional[BroadcastStation]:
        """Get station by name."""
        return self._stations_by_name.get(name)
    
    def get_broadcast(self, broadcast_id: str) -> Optional[Broadcast]:
        """Get broadcast by ID (e.g., 'WWV_5000')."""
        return self._broadcasts_by_id.get(broadcast_id)
    
    def get_broadcasts_for_station(self, station: str) -> List[Broadcast]:
        """Get all broadcasts for a station."""
        return self._broadcasts_by_station.get(station, [])
    
    def get_broadcasts_for_frequency(self, frequency_hz: int) -> List[Broadcast]:
        """Get all broadcasts on a frequency."""
        return self._broadcasts_by_frequency.get(frequency_hz, [])
    
    def get_channel_for_broadcast(self, broadcast_id: str) -> Optional[DerivedChannel]:
        """Get the channel that carries a broadcast."""
        broadcast = self.get_broadcast(broadcast_id)
        if not broadcast:
            return None
        for channel in self._channels:
            if channel.name == broadcast.channel_name:
                return channel
        return None
    
    def get_shared_frequencies(self) -> List[int]:
        """Get frequencies shared by multiple stations."""
        return [
            freq for freq, broadcasts in self._broadcasts_by_frequency.items()
            if len(broadcasts) > 1
        ]
    
    def get_unique_frequencies(self) -> Dict[int, str]:
        """Get frequencies unique to a single station."""
        return {
            freq: broadcasts[0].station
            for freq, broadcasts in self._broadcasts_by_frequency.items()
            if len(broadcasts) == 1
        }
    
    def summary(self) -> str:
        """Return a summary string."""
        lines = [
            f"BroadcastRegistry Summary",
            f"  Receiver: {self.receiver.callsign} ({self.receiver.latitude:.4f}, {self.receiver.longitude:.4f})",
            f"  Source Mode: {self.source_mode.value}",
            f"  Stations: {self.n_stations}",
            f"  Broadcasts: {self.n_broadcasts}",
            f"  Channels: {self.n_channels}",
            "",
            "Broadcasts by Station:",
        ]
        
        for station_name, broadcasts in sorted(self._broadcasts_by_station.items()):
            station = self._stations_by_name[station_name]
            lines.append(f"  {station_name} ({station.location}):")
            for b in broadcasts:
                lines.append(
                    f"    {b.broadcast_id}: {b.frequency_mhz:.2f} MHz, "
                    f"{b.distance_km:.0f} km @ {b.azimuth_deg:.0f}°, "
                    f"min {b.min_propagation_ms:.2f} ms"
                )
        
        lines.append("")
        lines.append("Derived Channels:")
        for ch in self._channels:
            disc = " [DISC]" if ch.requires_discrimination else ""
            beam = f" → {ch.beam_azimuth_deg:.0f}°" if ch.beam_azimuth_deg else ""
            lines.append(f"  {ch.name}: {ch.frequency_hz/1e6:.2f} MHz, {ch.stations}{disc}{beam}")
        
        return "\n".join(lines)


def create_registry_from_config(config: dict) -> BroadcastRegistry:
    """
    Create a BroadcastRegistry from a parsed TOML config.
    
    Supports both old-style config (with [station] section) and new-style
    config (with [receiver] and [[broadcast_station]] sections).
    
    Args:
        config: Parsed TOML configuration dictionary
        
    Returns:
        BroadcastRegistry instance
    """
    # Extract receiver location
    # Support both [station] (old) and [receiver] (new) section names
    receiver_config = config.get('receiver', config.get('station', {}))
    
    receiver = ReceiverLocation(
        callsign=receiver_config.get('callsign', 'UNKNOWN'),
        latitude=receiver_config.get('latitude', 0.0),
        longitude=receiver_config.get('longitude', 0.0),
        grid_square=receiver_config.get('grid_square', ''),
        station_id=receiver_config.get('id', ''),
        description=receiver_config.get('description', ''),
    )
    
    # Extract source mode
    ka9q_config = config.get('ka9q', {})
    source_str = ka9q_config.get('source', 'radiod')
    try:
        source_mode = SourceMode(source_str)
    except ValueError:
        logger.warning(f"Unknown source mode '{source_str}', defaulting to radiod")
        source_mode = SourceMode.RADIOD
    
    # Extract broadcast stations (if provided in config)
    # For now, use defaults - future: parse [[broadcast_station]] sections
    stations = None
    if 'broadcast_station' in config:
        stations = []
        for station_config in config['broadcast_station']:
            try:
                station = BroadcastStation(
                    name=station_config['name'],
                    latitude=station_config['latitude'],
                    longitude=station_config['longitude'],
                    frequencies_hz=station_config['frequencies_hz'],
                    tone_pattern=TonePattern(station_config.get('tone_pattern', 'unknown')),
                    location=station_config.get('location', ''),
                )
                stations.append(station)
            except (KeyError, ValueError) as e:
                logger.warning(f"Invalid broadcast_station config: {e}")
    
    return BroadcastRegistry(
        receiver=receiver,
        stations=stations,
        source_mode=source_mode,
    )
