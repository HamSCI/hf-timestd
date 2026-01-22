"""
Unit tests for BroadcastRegistry - Station-Centric Data Model

Tests the core functionality of the broadcast registry including:
- Registry initialization and geometry computation
- Channel derivation for radiod vs phase-engine modes
- Station and broadcast lookups
- Config loading integration
"""

import pytest
import math
from hf_timestd.models.broadcast import (
    BroadcastRegistry,
    BroadcastStation,
    Broadcast,
    DerivedChannel,
    ReceiverLocation,
    SourceMode,
    TonePattern,
    haversine_distance,
    bearing,
    create_registry_from_config,
    DEFAULT_STATIONS,
)


class TestGeometryFunctions:
    """Test geometry calculation functions."""
    
    def test_haversine_distance_known_path(self):
        """Test haversine distance for known path (WWV to AC0G)."""
        # WWV: Fort Collins, CO
        wwv_lat, wwv_lon = 40.67805, -105.04719
        # AC0G: Columbia, MO
        rx_lat, rx_lon = 38.918461, -92.127974
        
        distance = haversine_distance(rx_lat, rx_lon, wwv_lat, wwv_lon)
        
        # Expected ~1120 km
        assert 1100 < distance < 1140
    
    def test_haversine_distance_zero(self):
        """Test haversine distance for same point."""
        distance = haversine_distance(40.0, -105.0, 40.0, -105.0)
        assert distance == 0.0
    
    def test_bearing_north(self):
        """Test bearing calculation - due north."""
        # Point directly north
        brg = bearing(0.0, 0.0, 10.0, 0.0)
        assert abs(brg - 0.0) < 1.0  # Should be ~0° (north)
    
    def test_bearing_east(self):
        """Test bearing calculation - due east."""
        brg = bearing(0.0, 0.0, 0.0, 10.0)
        assert abs(brg - 90.0) < 1.0  # Should be ~90° (east)
    
    def test_bearing_west(self):
        """Test bearing calculation - due west."""
        brg = bearing(0.0, 0.0, 0.0, -10.0)
        assert abs(brg - 270.0) < 1.0  # Should be ~270° (west)


class TestBroadcastRegistry:
    """Test BroadcastRegistry class."""
    
    @pytest.fixture
    def receiver(self):
        """Create a test receiver location."""
        return ReceiverLocation(
            callsign='AC0G',
            latitude=38.918461,
            longitude=-92.127974,
            grid_square='EM38ww40pk',
        )
    
    def test_registry_initialization(self, receiver):
        """Test registry initializes with correct counts."""
        registry = BroadcastRegistry(receiver)
        
        assert registry.n_stations == 4
        assert registry.n_broadcasts == 17
        assert registry.source_mode == SourceMode.RADIOD
    
    def test_radiod_mode_9_channels(self, receiver):
        """Test radiod mode derives 9 channels."""
        registry = BroadcastRegistry(receiver, source_mode=SourceMode.RADIOD)
        
        assert registry.n_channels == 9
        
        # Check shared frequencies require discrimination
        shared_channels = [ch for ch in registry.channels if ch.requires_discrimination]
        unique_channels = [ch for ch in registry.channels if not ch.requires_discrimination]
        
        assert len(shared_channels) == 4  # 2.5, 5, 10, 15 MHz
        assert len(unique_channels) == 5  # 20, 25, 3.33, 7.85, 14.67 MHz
    
    def test_phase_engine_mode_17_channels(self, receiver):
        """Test phase-engine mode derives 17 channels."""
        registry = BroadcastRegistry(receiver, source_mode=SourceMode.PHASE_ENGINE)
        
        assert registry.n_channels == 17
        
        # All channels should have target_station set
        for ch in registry.channels:
            assert ch.target_station is not None
            assert ch.beam_azimuth_deg is not None
            assert not ch.requires_discrimination  # Phase-engine bypasses discrimination
    
    def test_geometry_computed(self, receiver):
        """Test geometry is computed for all broadcasts."""
        registry = BroadcastRegistry(receiver)
        
        for broadcast_id, broadcast in registry.broadcasts.items():
            assert broadcast.distance_km > 0
            assert 0 <= broadcast.azimuth_deg < 360
            assert broadcast.min_propagation_ms > 0
    
    def test_wwv_geometry(self, receiver):
        """Test WWV geometry is reasonable."""
        registry = BroadcastRegistry(receiver)
        
        wwv_5000 = registry.get_broadcast('WWV_5000')
        assert wwv_5000 is not None
        
        # WWV is ~1120 km from AC0G, bearing ~284°
        assert 1100 < wwv_5000.distance_km < 1140
        assert 280 < wwv_5000.azimuth_deg < 290
        
        # Min propagation time: 1120 km / 299792 km/s ≈ 3.7 ms
        assert 3.5 < wwv_5000.min_propagation_ms < 4.0
    
    def test_get_station(self, receiver):
        """Test station lookup."""
        registry = BroadcastRegistry(receiver)
        
        wwv = registry.get_station('WWV')
        assert wwv is not None
        assert wwv.name == 'WWV'
        assert len(wwv.frequencies_hz) == 6
        
        # Non-existent station
        assert registry.get_station('FAKE') is None
    
    def test_get_broadcast(self, receiver):
        """Test broadcast lookup."""
        registry = BroadcastRegistry(receiver)
        
        chu_7850 = registry.get_broadcast('CHU_7850')
        assert chu_7850 is not None
        assert chu_7850.station == 'CHU'
        assert chu_7850.frequency_hz == 7850000
        
        # Non-existent broadcast
        assert registry.get_broadcast('FAKE_1234') is None
    
    def test_get_broadcasts_for_station(self, receiver):
        """Test getting all broadcasts for a station."""
        registry = BroadcastRegistry(receiver)
        
        wwv_broadcasts = registry.get_broadcasts_for_station('WWV')
        assert len(wwv_broadcasts) == 6
        
        chu_broadcasts = registry.get_broadcasts_for_station('CHU')
        assert len(chu_broadcasts) == 3
    
    def test_get_broadcasts_for_frequency(self, receiver):
        """Test getting all broadcasts on a frequency."""
        registry = BroadcastRegistry(receiver)
        
        # Shared frequency (5 MHz)
        broadcasts_5mhz = registry.get_broadcasts_for_frequency(5000000)
        assert len(broadcasts_5mhz) == 3  # WWV, WWVH, BPM
        
        # Unique frequency (7.85 MHz)
        broadcasts_7850 = registry.get_broadcasts_for_frequency(7850000)
        assert len(broadcasts_7850) == 1  # CHU only
    
    def test_shared_frequencies(self, receiver):
        """Test identification of shared frequencies."""
        registry = BroadcastRegistry(receiver)
        
        shared = registry.get_shared_frequencies()
        assert len(shared) == 4
        assert 2500000 in shared
        assert 5000000 in shared
        assert 10000000 in shared
        assert 15000000 in shared
    
    def test_unique_frequencies(self, receiver):
        """Test identification of unique frequencies."""
        registry = BroadcastRegistry(receiver)
        
        unique = registry.get_unique_frequencies()
        assert len(unique) == 5
        assert unique[20000000] == 'WWV'
        assert unique[25000000] == 'WWV'
        assert unique[3330000] == 'CHU'
        assert unique[7850000] == 'CHU'
        assert unique[14670000] == 'CHU'
    
    def test_channel_naming_radiod(self, receiver):
        """Test channel naming in radiod mode."""
        registry = BroadcastRegistry(receiver, source_mode=SourceMode.RADIOD)
        
        channel_names = [ch.name for ch in registry.channels]
        
        # Shared frequencies should be named SHARED_*
        assert 'SHARED_5000' in channel_names
        assert 'SHARED_10000' in channel_names
        
        # Unique frequencies should be named STATION_*
        assert 'WWV_20000' in channel_names
        assert 'CHU_7850' in channel_names
    
    def test_channel_naming_phase_engine(self, receiver):
        """Test channel naming in phase-engine mode."""
        registry = BroadcastRegistry(receiver, source_mode=SourceMode.PHASE_ENGINE)
        
        channel_names = [ch.name for ch in registry.channels]
        
        # All channels should be named STATION_FREQ
        assert 'WWV_5000' in channel_names
        assert 'WWVH_5000' in channel_names
        assert 'BPM_5000' in channel_names
        assert 'CHU_7850' in channel_names


class TestConfigIntegration:
    """Test config loading integration."""
    
    def test_create_registry_from_config(self):
        """Test creating registry from config dict."""
        config = {
            'station': {
                'callsign': 'TEST',
                'latitude': 40.0,
                'longitude': -100.0,
                'grid_square': 'DN00',
            }
        }
        
        registry = create_registry_from_config(config)
        
        assert registry.receiver.callsign == 'TEST'
        assert registry.receiver.latitude == 40.0
        assert registry.n_broadcasts == 17
    
    def test_create_registry_missing_station(self):
        """Test creating registry with missing station config."""
        config = {}
        
        # Should use defaults
        registry = create_registry_from_config(config)
        assert registry.receiver.callsign == 'UNKNOWN'
    
    def test_create_registry_with_source_mode(self):
        """Test creating registry with source mode from config."""
        config = {
            'station': {'callsign': 'TEST', 'latitude': 40.0, 'longitude': -100.0},
            'ka9q': {'source': 'phase-engine'}
        }
        
        registry = create_registry_from_config(config)
        assert registry.source_mode == SourceMode.PHASE_ENGINE
        assert registry.n_channels == 17
    
    def test_create_registry_invalid_source_mode(self):
        """Test creating registry with invalid source mode defaults to radiod."""
        config = {
            'station': {'callsign': 'TEST', 'latitude': 40.0, 'longitude': -100.0},
            'ka9q': {'source': 'invalid-mode'}
        }
        
        registry = create_registry_from_config(config)
        assert registry.source_mode == SourceMode.RADIOD
        assert registry.n_channels == 9


class TestDefaultStations:
    """Test default station definitions."""
    
    def test_default_stations_count(self):
        """Test we have 4 default stations."""
        assert len(DEFAULT_STATIONS) == 4
    
    def test_default_stations_names(self):
        """Test default station names."""
        names = [s.name for s in DEFAULT_STATIONS]
        assert 'WWV' in names
        assert 'WWVH' in names
        assert 'CHU' in names
        assert 'BPM' in names
    
    def test_wwv_frequencies(self):
        """Test WWV has correct frequencies."""
        wwv = next(s for s in DEFAULT_STATIONS if s.name == 'WWV')
        assert len(wwv.frequencies_hz) == 6
        assert 5000000 in wwv.frequencies_hz
        assert 10000000 in wwv.frequencies_hz
        assert 20000000 in wwv.frequencies_hz
    
    def test_chu_unique_frequencies(self):
        """Test CHU has unique frequencies."""
        chu = next(s for s in DEFAULT_STATIONS if s.name == 'CHU')
        assert len(chu.frequencies_hz) == 3
        assert 3330000 in chu.frequencies_hz
        assert 7850000 in chu.frequencies_hz
        assert 14670000 in chu.frequencies_hz
    
    def test_tone_patterns(self):
        """Test tone patterns are correctly assigned."""
        wwv = next(s for s in DEFAULT_STATIONS if s.name == 'WWV')
        wwvh = next(s for s in DEFAULT_STATIONS if s.name == 'WWVH')
        chu = next(s for s in DEFAULT_STATIONS if s.name == 'CHU')
        
        assert wwv.tone_pattern == TonePattern.WWV_1000HZ
        assert wwvh.tone_pattern == TonePattern.WWVH_1200HZ
        assert chu.tone_pattern == TonePattern.CHU_BCD_FSK


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
