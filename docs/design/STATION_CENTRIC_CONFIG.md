# Station-Centric Configuration Design

**Status**: Phase 1 Implemented (2026-01-21)

## Overview

This document describes a proposed redesign of the `timestd-config.toml` schema to be
**station-centric** rather than **channel-centric**. This aligns the configuration with
the physics reality (17 broadcasts from 4 stations) rather than implementation details
(9 frequency channels).

## Implementation Status

| Component | Status | Location |
|-----------|--------|----------|
| `BroadcastRegistry` class | ✅ Done | `src/hf_timestd/models/broadcast.py` |
| Channel derivation (radiod/phase-engine) | ✅ Done | `BroadcastRegistry._derive_channels_*()` |
| Geometry computation | ✅ Done | `haversine_distance()`, `bearing()` |
| Config loader integration | ✅ Done | `create_registry_from_config()` |
| Station-centric API endpoints | ✅ Done | `web-api/routers/stations.py` |
| `[ka9q].source` config field | 🔲 Pending | Config schema extension |
| Discrimination bypass | 🔲 Pending | `tone_detector.py` |

## Current Schema (Channel-Centric)

```toml
[[recorder.channels]]
frequency_hz = 5000000
description = "SHARED_5000"  # User must know WWV+WWVH+BPM share this
```

**Problems:**
- Channels are implementation details, not physics
- User must manually track which stations share which frequencies
- No explicit broadcast registry
- Doesn't scale to phase-engine (17 dedicated channels)

## Proposed Schema (Station-Centric)

```toml
# =============================================================================
# RECEIVER STATION (Your Location)
# =============================================================================
[receiver]
callsign = "AC0G"
grid_square = "EM38ww40pk"
id = "S000171"
instrument_id = "172"
description = "beelink rx888 SAS"
latitude = 38.918461
longitude = -92.127974

# =============================================================================
# TIME SIGNAL STATIONS (The Physics Truth)
# =============================================================================
# These define the 17 broadcasts we're interested in.
# The system derives channels from these based on source mode.

[[broadcast_station]]
name = "WWV"
location = "Fort Collins, CO"
latitude = 40.67805
longitude = -105.04719
frequencies_hz = [2500000, 5000000, 10000000, 15000000, 20000000, 25000000]
tone_pattern = "1000Hz"  # For discrimination on shared channels

[[broadcast_station]]
name = "WWVH"
location = "Kekaha, HI"
latitude = 21.98830
longitude = -159.76220
frequencies_hz = [2500000, 5000000, 10000000, 15000000]
tone_pattern = "1200Hz"

[[broadcast_station]]
name = "CHU"
location = "Ottawa, ON"
latitude = 45.29525
longitude = -75.75433
frequencies_hz = [3330000, 7850000, 14670000]
tone_pattern = "BCD_FSK"

[[broadcast_station]]
name = "BPM"
location = "Pucheng, Shaanxi"
latitude = 34.94833
longitude = 109.54167
frequencies_hz = [2500000, 5000000, 10000000, 15000000]
tone_pattern = "1000Hz_BPM"  # Similar to WWV but different BCD

# =============================================================================
# DATA SOURCE (The Pipe)
# =============================================================================
[ka9q]
# Source mode determines how channels are created:
#   "radiod"       - 9 channels (unique frequencies), discrimination required
#   "phase-engine" - 17 channels (one per broadcast), discrimination bypassed
source = "radiod"
status_address = "bee1-hf-status.local"
auto_create_channels = true

# Phase-engine specific (ignored if source = "radiod")
[ka9q.phase_engine]
control_address = "phase-engine1.local"
combination_mode = "MRC"  # MRC, EGC, nulling, MVDR

# =============================================================================
# RECORDER SETTINGS
# =============================================================================
[recorder]
mode = "production"
test_data_root = "/tmp/timestd-test"
production_data_root = "/var/lib/timestd"

# Channel parameters (applied to all derived channels)
[recorder.channel_defaults]
preset = "iq"
sample_rate = 24000
agc = 0
gain = 0
encoding = "F32"

# Storage settings
compression = "zstd"
compression_level = 3
tiered_storage = true
hot_buffer_root = "/dev/shm/timestd"
ram_percent = 20

# =============================================================================
# REMAINING SECTIONS (unchanged)
# =============================================================================
[uploader]
enabled = false
# ...

[logging]
level = "INFO"

[monitoring]
enable_metrics = true

[web_ui]
port = 8000

[gnss_vtec]
enabled = true
# ...
```

## Channel Derivation Logic

### Mode: `source = "radiod"` (Current Behavior)

The system derives **9 channels** from unique frequencies across all stations:

```python
def derive_channels_radiod(stations: List[BroadcastStation]) -> List[Channel]:
    """Derive 9 frequency-based channels for radiod mode."""
    freq_to_stations = defaultdict(list)
    for station in stations:
        for freq in station.frequencies_hz:
            freq_to_stations[freq].append(station.name)
    
    channels = []
    for freq, station_names in sorted(freq_to_stations.items()):
        if len(station_names) > 1:
            name = f"SHARED_{freq // 1000}"
        else:
            name = f"{station_names[0]}_{freq // 1000}"
        channels.append(Channel(
            frequency_hz=freq,
            name=name,
            stations=station_names,
            requires_discrimination=len(station_names) > 1
        ))
    return channels  # 9 channels
```

**Result:**
| Channel | Frequency | Stations | Discrimination |
|---------|-----------|----------|----------------|
| SHARED_2500 | 2.5 MHz | WWV, WWVH, BPM | Required |
| SHARED_5000 | 5 MHz | WWV, WWVH, BPM | Required |
| SHARED_10000 | 10 MHz | WWV, WWVH, BPM | Required |
| SHARED_15000 | 15 MHz | WWV, WWVH, BPM | Required |
| WWV_20000 | 20 MHz | WWV | None |
| WWV_25000 | 25 MHz | WWV | None |
| CHU_3330 | 3.33 MHz | CHU | None |
| CHU_7850 | 7.85 MHz | CHU | None |
| CHU_14670 | 14.67 MHz | CHU | None |

### Mode: `source = "phase-engine"` (Future)

The system derives **17 channels** — one per broadcast:

```python
def derive_channels_phase_engine(stations: List[BroadcastStation]) -> List[Channel]:
    """Derive 17 broadcast-specific channels for phase-engine mode."""
    channels = []
    for station in stations:
        for freq in station.frequencies_hz:
            channels.append(Channel(
                frequency_hz=freq,
                name=f"{station.name}_{freq // 1000}",
                stations=[station.name],
                requires_discrimination=False,  # Phase-engine provides isolation
                target_station=station.name,
                beam_direction=station.azimuth_from_receiver  # Computed
            ))
    return channels  # 17 channels
```

**Result:**
| Channel | Frequency | Target Station | Beam Direction |
|---------|-----------|----------------|----------------|
| WWV_2500 | 2.5 MHz | WWV | 275° |
| WWVH_2500 | 2.5 MHz | WWVH | 252° |
| BPM_2500 | 2.5 MHz | BPM | 330° |
| WWV_5000 | 5 MHz | WWV | 275° |
| ... | ... | ... | ... |
| CHU_14670 | 14.67 MHz | CHU | 42° |

## Discrimination Bypass

When `source = "phase-engine"`, the tone detector can bypass multi-station discrimination:

```python
class MultiStationToneDetector:
    def __init__(self, channel_name: str, target_station: Optional[str] = None, ...):
        self.target_station = target_station
        
        if target_station:
            # Phase-engine mode: single station, no discrimination needed
            self.stations_to_detect = [target_station]
            self.discrimination_required = False
        else:
            # Radiod mode: detect all possible stations on this frequency
            self.stations_to_detect = self._get_stations_for_frequency(freq_mhz)
            self.discrimination_required = len(self.stations_to_detect) > 1
```

## Broadcast Registry

The system maintains an implicit broadcast registry derived from config:

```python
@dataclass
class Broadcast:
    station: str
    frequency_hz: int
    broadcast_id: str  # e.g., "WWV_5000"
    
    # Computed from receiver + station locations
    distance_km: float
    azimuth_deg: float
    min_propagation_ms: float  # distance / c

def build_broadcast_registry(config: Config) -> Dict[str, Broadcast]:
    """Build broadcast registry from station definitions."""
    registry = {}
    for station in config.broadcast_stations:
        for freq in station.frequencies_hz:
            broadcast_id = f"{station.name}_{freq // 1000}"
            registry[broadcast_id] = Broadcast(
                station=station.name,
                frequency_hz=freq,
                broadcast_id=broadcast_id,
                distance_km=haversine(config.receiver, station),
                azimuth_deg=bearing(config.receiver, station),
                min_propagation_ms=haversine(config.receiver, station) / C_LIGHT * 1000
            )
    return registry  # 17 broadcasts
```

## Bootstrap Problem: Phase-Engine Mode

### The Problem

Even with phase-engine beamforming toward a specific station, bootstrap is challenging:

1. **Initial RTP timing uncertainty**: ±50ms before Kalman convergence
2. **Beamforming attenuates but doesn't eliminate**: Interferers at -15 to -20 dB
3. **Strong interferer can still be detected**: If WWVH has better propagation than WWV

### Solution: Hybrid Bootstrap with Dynamic Discrimination

```
Phase-Engine Bootstrap Sequence:
┌─────────────────────────────────────────────────────────────────┐
│ 1. START: discrimination_active = True (same as radiod)        │
│    - Detect all tones in search window                         │
│    - Classify by frequency (1000 Hz vs 1200 Hz)                │
│    - Use beamforming as confidence boost, not sole classifier  │
├─────────────────────────────────────────────────────────────────┤
│ 2. ACQUIRING: Kalman filter accumulating measurements          │
│    - n_updates < 5 OR uncertainty > 2.0 ms                     │
│    - Continue full discrimination                              │
├─────────────────────────────────────────────────────────────────┤
│ 3. LOCKED: Kalman filter converged                             │
│    - n_updates >= 5 AND uncertainty < 2.0 ms                   │
│    - Verify: detected station matches target_station           │
│    - IF match: discrimination_active = False (bypass)          │
│    - IF mismatch: Flag anomaly, keep discrimination active     │
├─────────────────────────────────────────────────────────────────┤
│ 4. REACQUIRING: Lock lost (gap, mode change, etc.)             │
│    - Re-enable discrimination_active = True                    │
│    - Return to step 2                                          │
└─────────────────────────────────────────────────────────────────┘
```

### Data Model

```python
@dataclass
class DerivedChannel:
    # ... existing fields ...
    
    # Static capability (set by source mode)
    can_bypass_discrimination: bool  # True for phase-engine channels
    
    # Dynamic state (changes during operation)
    # NOT stored in registry - managed by Kalman filter state
```

The `requires_discrimination` field in `DerivedChannel` represents the **static capability**:
- `radiod` mode: Always `True` for shared frequencies
- `phase-engine` mode: Always `False` (CAN bypass when locked)

The **actual discrimination behavior** is determined at runtime by Kalman state:
- `ACQUIRING` → discrimination active (regardless of mode)
- `LOCKED` → discrimination bypassed (if phase-engine mode)

### Implementation Notes

The tone detector should accept a `kalman_locked: bool` parameter:

```python
class MultiStationToneDetector:
    def detect(self, samples, kalman_locked: bool = False):
        if self.target_station and kalman_locked:
            # Phase-engine mode, locked: only detect target station
            return self._detect_single_station(samples, self.target_station)
        else:
            # Radiod mode OR acquiring: full discrimination
            return self._detect_all_stations(samples)
```

## Migration Path

1. **Phase 1**: Add `[[broadcast_station]]` sections to config (alongside existing `[[recorder.channels]]`)
2. **Phase 2**: Add `[ka9q].source` field, default to `"radiod"`
3. **Phase 3**: Implement channel derivation logic, deprecate `[[recorder.channels]]`
4. **Phase 4**: Implement phase-engine integration when hardware ready

## Benefits

1. **Physics-First**: Config reflects the 17 broadcasts, not 9 channels
2. **Self-Documenting**: Station locations, frequencies, and tone patterns in one place
3. **Dual-Mode Ready**: Same config works for radiod or phase-engine
4. **Broadcast Registry**: Implicit registry enables station-centric APIs
5. **Computed Geometry**: Distance, azimuth, min propagation time derived automatically
