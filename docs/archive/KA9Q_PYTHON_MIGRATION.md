# Migration Plan: Core Streaming to ka9q-python

**Date:** 2025-12-01  
**Goal:** Move core streaming infrastructure into ka9q-python, making it the definitive "go-between" layer that delivers the best possible product (data + metadata) to client applications.

## Architecture Target

```
┌─────────────────────────────────────────────────────────────┐
│                 ka9q-radio (Phil's C)                        │
│                    [UPSTREAM - FIXED]                        │
└─────────────────────────┬───────────────────────────────────┘
                          │ RTP multicast, SAP
                          ▼
┌─────────────────────────────────────────────────────────────┐
│                    ka9q-python                               │
│              [GO-BETWEEN - YOURS]                            │
│                                                              │
│  Delivers to apps:                                           │
│    - Continuous sample stream (gap-filled)                   │
│    - StreamQuality metadata (gaps, timing, RTP stats)        │
│    - Cross-platform (Linux, macOS, Windows)                  │
│                                                              │
│  API: callback(samples, quality) or async iterator           │
└─────────────────────────┬───────────────────────────────────┘
                          │
        ┌─────────────────┼─────────────────┐
        ▼                 ▼                 ▼
┌─────────────┐   ┌─────────────┐   ┌─────────────┐
│   grape-    │   │   wspr-     │   │   other     │
│   recorder  │   │   recorder  │   │   apps      │
└─────────────┘   └─────────────┘   └─────────────┘
```

---

## Phase 1: Define the Contract

### 1.1 Core Data Structures (ka9q-python)

```python
# ka9q_python/stream_quality.py

from dataclasses import dataclass, field
from typing import List, Optional
from enum import Enum

class GapSource(Enum):
    """Gap types detected by ka9q-python core"""
    NETWORK_LOSS = "network_loss"           # RTP sequence gap
    RESEQUENCE_TIMEOUT = "resequence_timeout"  # Packet too late
    EMPTY_PAYLOAD = "empty_payload"         # RTP packet with no data
    STREAM_START = "stream_start"           # Gap before first packet
    STREAM_INTERRUPTION = "stream_interruption"  # radiod stopped sending

@dataclass
class GapEvent:
    """Single gap detected in the stream"""
    source: GapSource
    position_samples: int      # Offset from stream start (cumulative)
    duration_samples: int      # Gap size in samples
    timestamp_utc: str         # ISO format
    packets_affected: int = 0  # For network_loss

@dataclass
class StreamQuality:
    """Quality metadata accompanying sample batches"""
    
    # This batch
    batch_start_sample: int          # Position in stream
    batch_samples_delivered: int     # Actual samples in this batch
    batch_gaps: List[GapEvent] = field(default_factory=list)
    
    # Cumulative (stream lifetime)
    total_samples_delivered: int = 0
    total_samples_expected: int = 0
    total_gaps_filled: int = 0
    total_gap_events: int = 0
    
    # RTP statistics
    rtp_packets_received: int = 0
    rtp_packets_expected: int = 0
    rtp_packets_lost: int = 0
    rtp_packets_late: int = 0
    rtp_packets_duplicate: int = 0
    
    # Timing
    stream_start_utc: str = ""
    last_packet_utc: str = ""
    
    @property
    def completeness_pct(self) -> float:
        if self.total_samples_expected == 0:
            return 100.0
        return (self.total_samples_delivered / self.total_samples_expected) * 100

    def to_dict(self) -> dict:
        """Serialize for any consumer"""
        return {
            'batch_start_sample': self.batch_start_sample,
            'batch_samples_delivered': self.batch_samples_delivered,
            'batch_gaps': [{'source': g.source.value, 
                           'position': g.position_samples,
                           'duration': g.duration_samples,
                           'timestamp': g.timestamp_utc} for g in self.batch_gaps],
            'total_samples_delivered': self.total_samples_delivered,
            'total_samples_expected': self.total_samples_expected,
            'total_gaps_filled': self.total_gaps_filled,
            'completeness_pct': self.completeness_pct,
            'rtp_packets_received': self.rtp_packets_received,
            'rtp_packets_lost': self.rtp_packets_lost,
            'stream_start_utc': self.stream_start_utc,
        }
```

### 1.2 Stream API (ka9q-python)

```python
# ka9q_python/stream.py

from typing import Callable, Optional, AsyncIterator, Tuple
import numpy as np

# Callback signature
StreamCallback = Callable[[np.ndarray, StreamQuality], None]

class RadiodStream:
    """
    High-level interface to a radiod IQ stream.
    
    Handles:
      - Multicast subscription
      - RTP packet reception
      - Resequencing and gap filling
      - Quality tracking
    
    Delivers:
      - Continuous sample stream (complex64)
      - StreamQuality metadata with every batch
    """
    
    def __init__(
        self,
        ssrc: int,                      # Stream identifier
        sample_rate: int = 16000,
        multicast_group: Optional[str] = None,  # Auto-discover if None
        resequence_window_ms: int = 100,
        callback: Optional[StreamCallback] = None,
    ):
        ...
    
    def start(self) -> None:
        """Begin receiving and delivering samples."""
        ...
    
    def stop(self) -> None:
        """Stop receiving, flush final quality stats."""
        ...
    
    @property
    def quality(self) -> StreamQuality:
        """Current cumulative quality statistics."""
        ...
    
    # Alternative: async iterator interface
    async def samples(self) -> AsyncIterator[Tuple[np.ndarray, StreamQuality]]:
        """Async generator yielding (samples, quality) tuples."""
        ...


# Convenience for discovery
def discover_streams(timeout_sec: float = 5.0) -> List[dict]:
    """
    Listen for SAP announcements, return available streams.
    
    Returns list of:
      {'ssrc': int, 'frequency': float, 'sample_rate': int, 
       'description': str, 'multicast_group': str}
    """
    ...
```

---

## Phase 2: Identify Code to Migrate

### From signal-recorder → ka9q-python

| File | Destination | Notes |
|------|-------------|-------|
| `core/rtp_receiver.py` | `ka9q_python/rtp.py` | Multicast + RTP parsing |
| `core/packet_resequencer.py` | `ka9q_python/resequencer.py` | Gap detection + filling |
| `core/recording_session.py` | `ka9q_python/stream.py` | Becomes RadiodStream |
| Gap typology enums | `ka9q_python/stream_quality.py` | GapSource, GapEvent |

### Stays in grape-recorder (or signal-recorder renamed)

| File | Notes |
|------|-------|
| `grape/grape_recorder.py` | App-level: tone detection, segmentation |
| `grape/grape_npz_writer.py` | App-level: NPZ format output |
| `grape/analytics_service.py` | App-level: decimation, discrimination |
| `grape/solar_zenith_calculator.py` | App-level: science features |
| `grape/wwvh_discrimination.py` | App-level: WWV vs WWVH |
| All `web-ui/` | App-level: GRAPE monitoring UI |

### Stays in wspr-recorder (new or existing)

| File | Notes |
|------|-------|
| `wspr/wspr_recorder.py` | App-level: 2-min segments |
| `wspr/wspr_wav_writer.py` | App-level: WAV output |

---

## Phase 3: Migration Steps

### Step 3.1: Create ka9q-python stream module (Week 1)

1. **Define data structures** in `ka9q_python/stream_quality.py`
   - `GapSource` enum
   - `GapEvent` dataclass  
   - `StreamQuality` dataclass

2. **Port resequencer** to `ka9q_python/resequencer.py`
   - Adapt from `signal_recorder/core/packet_resequencer.py`
   - Return `List[GapEvent]` instead of just counts
   - Add `GapSource` classification

3. **Port RTP receiver** to `ka9q_python/rtp.py`
   - Adapt from `signal_recorder/core/rtp_receiver.py`
   - Add empty payload detection
   - Cross-platform socket handling (important for macOS/Windows)

4. **Create RadiodStream** in `ka9q_python/stream.py`
   - Combines RTP + resequencer
   - Delivers `callback(samples, quality)`
   - Tracks cumulative `StreamQuality`

5. **Test independently**
   - Simple script that connects and prints quality stats
   - Verify gap detection matches current behavior

### Step 3.2: Create grape-recorder package (Week 2)

1. **Extract grape code** from signal-recorder
   - `grape/` directory becomes `grape_recorder/`
   - Update imports: `from ka9q_python import RadiodStream, StreamQuality`

2. **Adapt GrapeRecorder** to use new API
   ```python
   from ka9q_python import RadiodStream, StreamQuality
   
   class GrapeRecorder:
       def __init__(self, config: GrapeConfig):
           self.stream = RadiodStream(
               ssrc=config.ssrc,
               sample_rate=16000,
               callback=self._on_samples,
           )
       
       def _on_samples(self, samples: np.ndarray, quality: StreamQuality):
           # Buffer to 1-minute segments
           # Add cadence_fill gaps (app-level)
           # Write NPZ with combined quality
           ...
   ```

3. **Add app-level gap tracking**
   - `cadence_fill` at segment boundaries
   - `late_start` if recording started mid-minute
   - `tone_lock_wait` for WWV tone detection

4. **Preserve analytics, web-ui, etc.**
   - These stay with grape-recorder
   - No changes needed (they consume NPZ files)

### Step 3.3: Update wspr-recorder (Week 2-3)

1. **Adapt to new ka9q-python API**
   ```python
   from ka9q_python import RadiodStream, StreamQuality
   
   class WsprRecorder:
       def __init__(self, config: WsprConfig):
           self.stream = RadiodStream(
               ssrc=config.ssrc,
               sample_rate=12000,  # WSPR uses 12 kHz
               callback=self._on_samples,
           )
       
       def _on_samples(self, samples: np.ndarray, quality: StreamQuality):
           # Buffer to 2-minute segments
           # Write WAV + sidecar quality JSON
           ...
   ```

### Step 3.4: Deprecation period (Week 3-4)

1. **signal-recorder shim**
   - Keep `signal_recorder.core` as thin wrapper around ka9q-python
   - Emit deprecation warnings
   - Document migration path for any external users

2. **Update documentation**
   - ka9q-python README: new stream API
   - grape-recorder README: installation + usage
   - Migration guide for existing deployments

---

## Phase 4: Package Structure

### ka9q-python (expanded)

```
ka9q-python/
├── ka9q_python/
│   ├── __init__.py          # Exports: RadiodStream, StreamQuality, discover_streams
│   ├── sap.py               # SAP discovery (existing)
│   ├── rtp.py               # RTP packet handling (migrated + enhanced)
│   ├── resequencer.py       # Gap detection + filling (migrated)
│   ├── stream.py            # RadiodStream class (new)
│   ├── stream_quality.py    # GapSource, GapEvent, StreamQuality (new)
│   └── status.py            # radiod status queries (existing)
├── tests/
├── examples/
│   ├── simple_stream.py     # Basic callback example
│   ├── quality_monitor.py   # Print gap stats
│   └── async_stream.py      # Async iterator example
├── setup.py
└── README.md
```

### grape-recorder (new package)

```
grape-recorder/
├── grape_recorder/
│   ├── __init__.py
│   ├── recorder.py          # GrapeRecorder (from signal_recorder.grape)
│   ├── npz_writer.py        # NPZ output
│   ├── tone_detector.py     # WWV/CHU tone detection
│   ├── analytics/
│   │   ├── service.py       # Decimation, discrimination
│   │   ├── discrimination.py
│   │   └── solar_zenith.py
│   └── web_ui/              # Monitoring dashboard
├── config/
│   └── grape-config.toml.template
├── systemd/
├── tests/
├── setup.py
└── README.md
```

### wspr-recorder (new or expanded)

```
wspr-recorder/
├── wspr_recorder/
│   ├── __init__.py
│   ├── recorder.py          # WsprRecorder
│   ├── wav_writer.py        # WAV output + sidecar JSON
│   └── decode_hook.py       # Optional: trigger wsprd
├── setup.py
└── README.md
```

---

## Phase 5: Testing & Validation

### Unit Tests (ka9q-python)

1. **Resequencer tests**
   - In-order packets → no gaps
   - Out-of-order packets → resequenced
   - Missing packets → GapEvent with NETWORK_LOSS
   - Late packets → GapEvent with RESEQUENCE_TIMEOUT

2. **StreamQuality tests**
   - Cumulative stats accuracy
   - completeness_pct calculation
   - to_dict() serialization

### Integration Tests

1. **ka9q-python + mock radiod**
   - Simulate packet loss, verify gap detection
   - Simulate out-of-order, verify resequencing

2. **grape-recorder + ka9q-python**
   - Record 5 minutes, verify NPZ quality metadata
   - Inject gaps, verify they appear in NPZ

3. **Live test**
   - Run grape-recorder against real radiod
   - Compare output to current signal-recorder
   - Verify identical NPZ files (content + metadata)

---

## Timeline Summary

| Week | Milestone | Status |
|------|-----------|--------|
| 1 | ka9q-python stream module complete, tested standalone | ✅ DONE |
| 2 | grape-recorder extracted, using new API, basic test pass | |
| 2-3 | wspr-recorder updated, example apps working | |
| 3-4 | Deprecation shim in place, documentation updated | |
| 4+ | signal-recorder becomes grape-recorder (rename or archive) | |

## Completed Work (2025-12-01)

### Phase 1: ka9q-python Stream Module ✅

New files created in `/home/wsprdaemon/ka9q-python/ka9q/`:

1. **`stream_quality.py`** - Quality metadata structures
   - `GapSource` enum: NETWORK_LOSS, RESEQUENCE_TIMEOUT, EMPTY_PAYLOAD, etc.
   - `GapEvent` dataclass: position, duration, timestamp, packets_affected
   - `StreamQuality` dataclass: batch + cumulative stats, RTP metrics

2. **`resequencer.py`** - Packet resequencing (ported from signal-recorder)
   - `PacketResequencer` class: circular buffer, gap detection, zero-fill
   - `RTPPacket` dataclass: sequence, timestamp, ssrc, samples
   - KA9Q signed 32-bit timestamp arithmetic for wrap handling

3. **`stream.py`** - High-level stream API
   - `RadiodStream` class: combines multicast + resequencer + quality
   - Callback-based sample delivery: `on_samples(samples, quality)`
   - Cross-platform socket handling

4. **`examples/stream_example.py`** - Usage demonstration

Version bumped to **3.2.0**.

---

## Open Questions

1. **Package naming**: `grape-recorder` or keep `signal-recorder` for GRAPE?
2. **Versioning**: ka9q-python version bump (major version for API change)?
3. **Windows/macOS testing**: Who can test cross-platform before release?
4. **Backward compatibility**: How long to maintain signal_recorder.core shim?
