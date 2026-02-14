# Gap Typology for Signal Recording Pipeline

**Date:** 2025-12-01  
**Purpose:** Classify gaps by their source in the recording pipeline for accurate diagnosis

## Overview

Gaps in recorded data can originate at different stages of the pipeline. Understanding the source is essential for:
1. Accurate diagnosis of problems
2. Appropriate metadata annotation
3. Downstream data quality assessment
4. Operator action guidance

## Gap Types by Pipeline Stage

### Stage 1: RF Source Layer

#### 1.1 Radiod Down
- **Description:** radiod process not running; no RTP packets generated
- **Detection:** No packets on multicast group; radiod process check fails
- **Metadata:** `gap_source: radiod_down`
- **Duration:** Typically minutes to hours
- **User Action:** Restart radiod, check hardware

#### 1.2 Radiod Running but No RTP Stream
- **Description:** radiod alive but not producing packets for this channel/SSRC
- **Detection:** Packets present on other SSRCs but not target frequency
- **Metadata:** `gap_source: no_rtp_stream`
- **Causes:** Channel not configured, frequency outside tuned range, ADC issue
- **User Action:** Check radiod configuration, verify frequency coverage

#### 1.3 RTP Stream Present but Empty Payload
- **Description:** RTP packets arrive but contain null/zero payload
- **Detection:** Packets received but `payload_bytes == 0` or all zeros
- **Metadata:** `gap_source: empty_payload`
- **Causes:** Radiod internal error, signal chain disconnected
- **User Action:** Check radiod logs, verify antenna connection

### Stage 2: Network Layer

#### 2.1 Multicast Not Joined
- **Description:** Recorder not receiving multicast group
- **Detection:** Zero packets received; multicast membership check fails
- **Metadata:** `gap_source: multicast_not_joined`
- **Causes:** Network misconfiguration, firewall, IGMP snooping issues
- **User Action:** Check network configuration, verify multicast routing

#### 2.2 Network Packet Loss
- **Description:** RTP packets lost in transit (UDP drops)
- **Detection:** Sequence number gaps in received packets
- **Metadata:** `gap_source: network_loss`, `packets_lost: N`
- **Patterns:** Burst (congestion), isolated (transient), sustained (overload)
- **User Action:** Check network utilization, switch buffers

#### 2.3 Packets Arrived Out of Order (Resequencing Gaps)
- **Description:** Packets arrived too late to be resequenced into stream
- **Detection:** Packet sequence < expected, beyond resequencing window
- **Metadata:** `gap_source: resequence_timeout`, `late_packets: N`
- **Causes:** Network jitter, routing changes
- **User Action:** Increase resequencing buffer (trade-off: latency)

### Stage 3: Core Recorder Layer

#### 3.1 Core Recorder Not Running
- **Description:** Recording service not started; no files written
- **Detection:** No NPZ files for time period; process check fails
- **Metadata:** `gap_source: recorder_not_running`
- **Duration:** Minutes to hours (until service started)
- **User Action:** Start grape-core-recorder service

#### 3.2 Recorder Buffer Overflow
- **Description:** Incoming data rate exceeds processing capacity
- **Detection:** Buffer full warnings in logs; dropped packet counter
- **Metadata:** `gap_source: buffer_overflow`, `dropped: N`
- **Causes:** CPU overload, disk I/O bottleneck, too many channels
- **User Action:** Reduce channel count, optimize system, faster storage

#### 3.3 Disk Write Failure
- **Description:** Cannot write to storage (full, permissions, I/O error)
- **Detection:** Write errors in logs; partial/missing files
- **Metadata:** `gap_source: disk_error`
- **User Action:** Check disk space, permissions, hardware health

#### 3.4 SSRC Change Not Tracked
- **Description:** Radiod changed SSRC (e.g., retuned) and recorder lost track
- **Detection:** Sudden stop of one SSRC, start of another
- **Metadata:** `gap_source: ssrc_change`
- **User Action:** Usually automatic recovery; check if intentional retune

### Stage 4: Segmentation Layer

#### 4.1 Segment Boundary Padding (Cadence Fill)
- **Description:** Zeros inserted to align data to segment boundaries
- **Detection:** `gaps_filled` metadata at segment start/end
- **Metadata:** `gap_source: cadence_fill`, `fill_samples: N`
- **Normal Operation:** Expected at startup, minor at boundaries
- **Segment Sizes:**
  - GRAPE: 1-minute segments (960,000 samples @ 16 kHz)
  - WSPR: 2-minute segments
  - FT8: 15-second segments

#### 4.2 Late Start Fill
- **Description:** Recording started mid-segment; padded to boundary
- **Detection:** Gap at beginning of first file of session
- **Metadata:** `gap_source: late_start`, `fill_ms: N`
- **Normal Operation:** Expected once per recording session start

#### 4.3 Tone Lock Waiting (GRAPE-specific)
- **Description:** Waiting for WWV/CHU tone detection before recording
- **Detection:** Gap between service start and first file
- **Metadata:** `gap_source: tone_lock_wait`
- **Duration:** Typically 1-60 seconds
- **Normal Operation:** Expected; ensures time_snap accuracy

### Stage 5: Analytics Layer

#### 5.1 Analytics Service Not Running
- **Description:** 16 kHz archives exist but no 10 Hz decimated files
- **Detection:** Archives present, decimated directory empty/stale
- **Metadata:** `gap_source: analytics_not_running`
- **User Action:** Start grape-analytics service

#### 5.2 Decimation Processing Failed
- **Description:** Analytics tried to process but encountered error
- **Detection:** Error logs; partial decimated output
- **Metadata:** `gap_source: decimation_failed`
- **Causes:** Corrupt NPZ, memory error, bug
- **User Action:** Check analytics logs, reprocess manually

#### 5.3 Decimation Backlog
- **Description:** Analytics running but behind real-time
- **Detection:** Decimated files lag archives by >N minutes
- **Metadata:** `gap_source: decimation_backlog`
- **User Action:** Check CPU load; analytics may catch up

### Stage 6: Upload/Distribution Layer

#### 6.1 Upload Service Not Running
- **Description:** Files exist locally but not uploaded to server
- **Detection:** Local files present, server files missing
- **Metadata:** `gap_source: upload_not_running`

#### 6.2 Upload Failed
- **Description:** Upload attempted but failed (network, auth, server)
- **Detection:** Upload error logs; retry queue growing
- **Metadata:** `gap_source: upload_failed`

---

## Gap Detection Summary Table

| Gap Type | Detected By | Severity | Auto-Recovery |
|----------|-------------|----------|---------------|
| radiod_down | Process check | Critical | No |
| no_rtp_stream | Packet counter | Critical | No |
| empty_payload | Payload inspection | Critical | No |
| multicast_not_joined | Packet counter | Critical | No |
| network_loss | RTP sequence gaps | Warning | Yes (fill) |
| resequence_timeout | Late packet counter | Warning | Yes (fill) |
| recorder_not_running | Process check | Critical | No |
| buffer_overflow | Drop counter | Error | Partial |
| disk_error | Write errors | Critical | No |
| ssrc_change | SSRC tracking | Info | Yes |
| cadence_fill | Segment metadata | Info | N/A (normal) |
| late_start | First file metadata | Info | N/A (normal) |
| tone_lock_wait | GRAPE state | Info | N/A (normal) |
| analytics_not_running | File comparison | Warning | No |
| decimation_failed | Error logs | Error | Manual |
| decimation_backlog | Timestamp comparison | Info | Yes |

## Metadata Fields for Gap Annotation

Every NPZ file should include gap metadata:

```python
# In NPZ metadata
{
    'gaps_count': 3,           # Number of distinct gaps
    'gaps_filled': 4800,       # Total samples filled with zeros
    'gap_sources': [           # Detailed gap breakdown
        {'source': 'network_loss', 'samples': 3200, 'packets': 10},
        {'source': 'cadence_fill', 'samples': 1600, 'position': 'start'}
    ],
    'completeness_pct': 99.5,  # (expected - gaps_filled) / expected * 100
}
```

## Display Recommendations for Web UI

### Carrier Analysis Page

Show gap summary with source breakdown:

```
Data Coverage: 87.3%
├── Missing Files: 180 minutes (recorder not running 00:00-03:00)
├── Network Loss: 2.3 minutes (scattered packet drops)  
├── Cadence Fill: 0.1 minutes (normal segment padding)
└── Analytics Backlog: 5 minutes (processing, will appear soon)
```

### Color Coding

- 🔴 **Critical** (needs action): radiod_down, recorder_not_running, disk_error
- 🟠 **Warning** (degraded): network_loss, buffer_overflow, analytics_not_running
- 🟡 **Info** (expected/normal): cadence_fill, late_start, tone_lock_wait
- 🟢 **Good**: No gaps or only minor cadence fills

## Detection Responsibility & Downstream Propagation

### Principle
Each pipeline stage is responsible for:
1. **Detecting** gaps within its scope
2. **Recording** gap metadata in its output
3. **Passing through** upstream gap metadata unchanged

### Detection Ownership by Component

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        DETECTION RESPONSIBILITY                         │
├─────────────────────┬───────────────────────┬───────────────────────────┤
│ Component           │ Detects               │ Records In                │
├─────────────────────┼───────────────────────┼───────────────────────────┤
│ radiod_health.py    │ radiod_down           │ state/radiod-status.json  │
│                     │ no_rtp_stream         │                           │
├─────────────────────┼───────────────────────┼───────────────────────────┤
│ rtp_receiver.py     │ empty_payload         │ per-packet flags          │
│                     │ multicast_not_joined  │ (passed to session)       │
├─────────────────────┼───────────────────────┼───────────────────────────┤
│ packet_resequencer  │ network_loss          │ resequencer stats         │
│                     │ resequence_timeout    │ (sequence gaps, late pkt) │
├─────────────────────┼───────────────────────┼───────────────────────────┤
│ recording_session   │ buffer_overflow       │ session metadata          │
│                     │ ssrc_change           │                           │
├─────────────────────┼───────────────────────┼───────────────────────────┤
│ segment_writer      │ cadence_fill          │ NPZ metadata:             │
│ (grape_npz_writer)  │ late_start            │   gaps_count              │
│                     │                       │   gaps_filled             │
│                     │                       │   gap_details[]           │
├─────────────────────┼───────────────────────┼───────────────────────────┤
│ grape_recorder      │ tone_lock_wait        │ NPZ metadata:             │
│                     │                       │   tone_lock_delay_ms      │
├─────────────────────┼───────────────────────┼───────────────────────────┤
│ analytics_service   │ decimation_failed     │ 10Hz NPZ metadata:        │
│                     │ decimation_backlog    │   source_gaps (passthru)  │
│                     │                       │   decimation_status       │
├─────────────────────┼───────────────────────┼───────────────────────────┤
│ upload_service      │ upload_failed         │ upload log / server meta  │
└─────────────────────┴───────────────────────┴───────────────────────────┘
```

### Core vs Application Boundary

The key architectural insight: **segmentation and format are application concerns, not core concerns**.

```
┌─────────────────────────────────────────────────────────────────────────┐
│                            CORE LAYER                                    │
│                   (signal-recorder infrastructure)                       │
│                                                                          │
│  Responsibility:                                                         │
│    - Receive RTP packets from radiod                                     │
│    - Resequence out-of-order packets                                     │
│    - Fill gaps with zeros (maintain continuous stream)                   │
│    - Track quality metadata (gaps, timing, upstream stats)               │
│    - Deliver stream + metadata to application callback                   │
│                                                                          │
│  Does NOT:                                                               │
│    - Segment into files (app decides: 1-min, 2-min, 15-sec)             │
│    - Choose output format (app decides: NPZ, WAV, DRF)                  │
│    - Add app-specific metadata (tone detection, discrimination)          │
│                                                                          │
│  Output: Continuous sample stream + StreamQuality metadata               │
└─────────────────────────────┬───────────────────────────────────────────┘
                              │
                              │  callback(samples, quality_metadata)
                              │
┌─────────────────────────────▼───────────────────────────────────────────┐
│                         APPLICATION LAYER                                │
│                    (GRAPE, WSPR, SuperDARN, etc.)                       │
│                                                                          │
│  Responsibility:                                                         │
│    - Receive stream + quality from core                                  │
│    - Buffer to segment boundaries (app-specific cadence)                 │
│    - Add segment boundary padding (cadence_fill is app-level gap)       │
│    - Add app-specific processing (tone detection, etc.)                  │
│    - Choose output format and write files                                │
│    - Serialize quality metadata into chosen format                       │
│                                                                          │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐    │
│  │   GRAPE     │  │    WSPR     │  │  SuperDARN  │  │    CODAR    │    │
│  │  1-min NPZ  │  │  2-min WAV  │  │   DRF/HDF5  │  │  custom fmt │    │
│  └─────────────┘  └─────────────┘  └─────────────┘  └─────────────┘    │
└─────────────────────────────────────────────────────────────────────────┘
```

### Stream Quality Metadata (Core → Application)

The core delivers quality metadata alongside the sample stream:

```python
@dataclass
class GapEvent:
    """Single gap detected by core layer"""
    source: str              # 'network_loss', 'resequence_timeout', 'empty_payload'
    position_samples: int    # Offset in stream (cumulative sample count)
    duration_samples: int    # Gap length in samples
    timestamp_utc: str       # When gap occurred
    
@dataclass 
class StreamQuality:
    """Quality metadata delivered with each callback batch"""
    
    # For this callback batch
    batch_gaps: List[GapEvent]       # Gaps in this batch
    batch_samples_expected: int
    batch_samples_received: int
    
    # Cumulative for stream lifetime
    total_gaps_count: int
    total_gaps_filled_samples: int
    stream_start_time: str
    
    # Upstream stats (for diagnosis)
    rtp_packets_expected: int
    rtp_packets_received: int
    rtp_packets_late: int            # Arrived after resequence window
    rtp_packets_duplicate: int
```

### Application Adds Its Own Gap Types

The application layer adds segment-level gap information:

```python
@dataclass
class SegmentQuality:
    """Application-level quality for a written segment"""
    
    # Inherited from core (passthrough)
    stream_quality: StreamQuality
    
    # Added by application during segmentation
    cadence_fill_start: int          # Samples padded at segment start
    cadence_fill_end: int            # Samples padded at segment end
    late_start_delay_ms: float       # If segment started mid-boundary
    
    # App-specific (GRAPE example)
    tone_lock_wait_ms: float         # Time waiting for tone detection
    tone_detection_success: bool
```

### Why This Separation Matters

| Concern | Core Layer | Application Layer |
|---------|------------|-------------------|
| **Gap Detection** | network_loss, empty_payload, resequence_timeout | cadence_fill, late_start, tone_lock_wait |
| **When Detected** | During stream processing | During segmentation/file writing |
| **Segmentation** | ❌ No | ✅ App decides cadence |
| **Format** | ❌ No | ✅ App decides NPZ/WAV/DRF |
| **Timing Source** | RTP timestamps, GPS | App may add tone-derived time_snap |

### Real-Time vs Scientific Split

This separation also clarifies the real-time vs scientific consumer split:

**Real-time consumers** (live audio, spectrogram):
- Connect to core stream directly
- Receive samples + quality callback
- Handle gaps inline (play silence, show dark band)
- No segmentation, no files

**Scientific consumers** (GRAPE, WSPR):
- Application layer wraps core stream
- Buffers to segment boundaries
- Writes files with full quality metadata
- Preserves gap details for downstream analysis

### Consumer Requirements by Application Type

#### Scientific Applications (NEED full gap metadata)

| Application | Gap Info Needed | Why |
|-------------|-----------------|-----|
| **GRAPE/HamSCI** | All gap sources, positions, durations | Data quality flags for analysis; exclude gapped segments from Doppler calculations |
| **WSPR** | Segment completeness, boundary fills | Incomplete 2-min segments may produce decode errors |
| **SuperDARN** | Network loss patterns, timestamps | Correlate gaps with ionospheric events |
| **CODAR** | Completeness percentage | Ocean current calculations need continuous data |

**Metadata contract for scientific apps:**
```python
# Required fields in NPZ/output files
{
    'completeness_pct': 99.7,          # Overall data present
    'gaps_count': 3,                    # Number of distinct gaps
    'gaps_filled_samples': 4800,        # Total samples zero-filled
    'gap_details': [                    # Per-gap breakdown
        {
            'source': 'network_loss',   # Gap type from typology
            'position_samples': 12000,  # Where in segment
            'duration_samples': 3200,   # How long
            'timestamp_utc': '...',     # When it occurred
        },
        {
            'source': 'cadence_fill',
            'position_samples': 0,      # At segment start
            'duration_samples': 1600,
            'timestamp_utc': '...',
        }
    ],
    'upstream_quality': {               # From earlier pipeline stages
        'rtp_packets_expected': 3000,
        'rtp_packets_received': 2987,
        'resequence_late': 5,
    }
}
```

#### Real-Time Applications (MAY NOT NEED gap metadata)

| Application | Gap Handling | Notes |
|-------------|--------------|-------|
| **Live Audio** | Just play silence | Listener won't analyze gaps |
| **Live Spectrogram** | Show as dark band | Visual indication sufficient |
| **Streaming** | Pass through zeros | Consumer handles presentation |

**For real-time, gaps are handled inline:**
- Zero-fill the buffer
- Optionally log for debugging
- No persistent metadata required

### Implementation Checklist

#### Current State (as of 2025-12-01)

**Core Layer:**
- [x] `packet_resequencer.py` - Tracks sequence gaps, fills with zeros
- [x] `recording_session.py` - Delivers samples via callback
- [ ] `GapEvent` dataclass - Not yet defined
- [ ] `StreamQuality` dataclass - Not yet defined
- [ ] Quality metadata in callback - Not yet passed to application
- [ ] `rtp_receiver.py` - Does not flag empty payloads

**Application Layer (GRAPE):**
- [x] `grape_npz_writer.py` - Records basic `gaps_count`, `gaps_filled`
- [ ] `SegmentQuality` dataclass - Not yet defined
- [ ] Receives `StreamQuality` from core - Not implemented
- [ ] Adds `cadence_fill` detection - Not implemented
- [ ] Analytics passthrough - Does not copy source gap metadata

#### Recommended Implementation Order

**Phase 1: Core Layer - Stream Quality**
1. Define `GapEvent` dataclass in `core/`
2. Define `StreamQuality` dataclass in `core/`
3. Update `packet_resequencer` to produce `List[GapEvent]`
4. Update `recording_session` to build `StreamQuality`
5. Pass `StreamQuality` in sample callback to application

**Phase 2: Application Layer - Segment Quality**
1. Define `SegmentQuality` in each app (or shared interface)
2. GRAPE: Update `grape_recorder` to receive `StreamQuality`
3. GRAPE: Track `cadence_fill` during segmentation
4. GRAPE: Update `grape_npz_writer` to serialize full quality

**Phase 3: Gap Source Classification**
1. Core: Add `empty_payload` detection in RTP receiver
2. Core: Add `resequence_timeout` vs `network_loss` distinction
3. App: Add `late_start` detection at startup
4. App: Add `tone_lock_wait` (GRAPE-specific)

**Phase 4: Downstream Propagation**
1. Analytics reads `SegmentQuality` from source NPZ
2. Analytics passes through to decimated 10Hz files
3. Upload preserves all quality metadata

**Phase 5: Visibility**
1. Web UI displays gap breakdown by source
2. API exposes gap details per file/channel/date

## Future Enhancements

1. **Automatic Root Cause Detection**: Correlate gaps across channels to distinguish system-wide vs channel-specific issues

2. **Gap Prediction**: Alert when conditions suggest imminent gaps (disk filling, CPU high, network errors increasing)

3. **Gap Attribution in Spectrogram**: Overlay gap source annotations on spectrogram display

4. **Scientific Data Quality Flags**: Propagate gap metadata to downstream analysis tools (HamSCI, etc.)

5. **Gap Correlation Service**: Cross-reference gaps with external events (solar flares, geomagnetic storms) for scientific context
