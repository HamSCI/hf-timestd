# Time Reference Architecture - RTP to UTC(NIST) Alignment

## Overview

The GRAPE recorder maintains two separate but related timing functions:

1. **Sample Segmentation**: Dividing continuous IQ stream into minute-aligned files
2. **UTC(NIST) Alignment**: Knowing what UTC time each sample represents

These are handled by different components with different accuracy requirements.

## Timing Layers

### Layer 1: RTP Timestamps (Primary)

- **Source**: GPSDO-disciplined oscillator in SDR (RX888)
- **Stability**: Excellent (<0.1 PPM drift)
- **Properties**: 
  - Counter increments at sample_rate (20,000 Hz)
  - Wraps at 32-bit (every ~60 hours)
  - No absolute time reference - just a stable counter

### Layer 2: System Time (Bootstrap Only)

- **Source**: NTP/chrony disciplined to UTC
- **Accuracy**: ~10-50ms to UTC
- **Problem**: Has jitter from NTP adjustments (±16ms typical)
- **Use**: ONLY for initial RTP-to-UTC mapping at startup

### Layer 3: time_snap (Authoritative UTC Reference)

- **Source**: HF timing tones (WWV/WWVH/CHU)
- **Method**: Correlate detected tone arrival with expected UTC time
- **Accuracy**: 
  - Initial: ~1ms (first detection)
  - Converged: ~0.1ms (after Kalman convergence)
- **Updates**: Every minute when tone is detected
- **Provides**: Authoritative mapping of RTP timestamp ↔ UTC(NIST)

## Architecture

### Phase 1: Raw Archive (Sample Segmentation)

The binary_archive_writer segments data into minute files using:

```python
# One-time bootstrap at startup
rtp_to_unix_offset = system_time - (rtp_timestamp / sample_rate)

# All subsequent minute boundaries derived from RTP
minute_unix = rtp_timestamp / sample_rate + rtp_to_unix_offset
minute_boundary = (int(minute_unix) // 60) * 60
```

**Key point**: The offset is fixed at startup. This provides:
- Stable, jitter-free segmentation (no NTP jitter affects file boundaries)
- Consistent RTP timestamp continuity across files
- May be off by ~50ms from true UTC (acceptable for segmentation)

### Phase 2: Analytics (UTC Alignment)

The analytics service uses time_snap for accurate UTC alignment:

```python
# time_snap provides the refined mapping
time_snap = {
    'rtp_timestamp': 1234567890,      # RTP ts when tone detected
    'utc_time': 1765280400.0,         # UTC time of tone (known)
    'offset_ms': 23.456,              # Current best estimate of system offset
    'uncertainty_ms': 0.15,           # Kalman uncertainty
    'source': 'WWV 10 MHz'
}

# Convert any RTP timestamp to UTC(NIST)
utc_nist = (rtp_timestamp - time_snap.rtp_timestamp) / sample_rate + time_snap.utc_time
```

## time_snap Evolution

### Startup Sequence

1. **T=0 (startup)**: No time_snap available
   - Use system_time for file segmentation
   - Mark data as "unanchored" in metadata

2. **T=1-5 minutes**: First tone detection
   - Establish initial time_snap from tone arrival
   - Uncertainty ~1ms (single detection)
   - Update metadata with anchor

3. **T=5-60 minutes**: Kalman convergence
   - Each tone detection refines the estimate
   - Uncertainty narrows (the Kalman funnel)
   - PPM drift correction if GPSDO unlocked

4. **T>60 minutes**: Steady state
   - Uncertainty ~0.1ms or better
   - time_snap updates for drift monitoring
   - D_clock represents UTC(NIST) - system_clock offset

### Handling Transitions

The binary_archive_writer does NOT need to change its RTP-to-Unix offset when
time_snap improves. The segmentation offset can remain fixed because:

1. Files are identified by minute_boundary (Unix timestamp in filename)
2. The RTP timestamps are stored in metadata
3. Phase 2 uses time_snap (not file segmentation offset) for UTC alignment

The key insight: **Segmentation accuracy and UTC alignment accuracy are independent.**

## Data Flow

```
RTP Packets (GPSDO-disciplined)
        │
        ▼
┌───────────────────────────────────────────────────┐
│ Phase 1: Binary Archive Writer                     │
│                                                    │
│ • Bootstrap offset from system_time (once)         │
│ • Derive minute boundaries from RTP               │
│ • Store RTP timestamps in metadata                │
│ • Does NOT need time_snap                         │
└───────────────────────────────────────────────────┘
        │
        ▼
┌───────────────────────────────────────────────────┐
│ Phase 2: Analytics Service                         │
│                                                    │
│ • Detect timing tones                             │
│ • Update time_snap via Kalman filter              │
│ • Use time_snap for UTC(NIST) alignment           │
│ • Calculate D_clock = UTC(NIST) - system_clock    │
└───────────────────────────────────────────────────┘
        │
        ▼
┌───────────────────────────────────────────────────┐
│ Products: Spectrograms, PSWS Upload                │
│                                                    │
│ • Use time_snap-aligned timestamps                │
│ • Include uncertainty in metadata                 │
└───────────────────────────────────────────────────┘
```

## Metadata Requirements

Each minute file should include:

```json
{
    "minute_boundary": 1765280400,
    "start_rtp_timestamp": 1234567890,
    "samples_written": 1200000,
    
    "timing_reference": {
        "bootstrap_offset": 1764012345.678,
        "time_snap_available": true,
        "time_snap_rtp": 1234500000,
        "time_snap_utc": 1765280350.0,
        "time_snap_uncertainty_ms": 0.15,
        "time_snap_source": "WWV 10 MHz"
    }
}
```

This allows reprocessing with improved time_snap if needed.

## Summary

| Component | Time Reference | Purpose |
|-----------|---------------|---------|
| File segmentation | Bootstrap offset (fixed) | Stable minute boundaries |
| UTC alignment | time_snap (evolving) | Accurate UTC(NIST) mapping |
| D_clock | time_snap | System offset from UTC(NIST) |

The separation ensures:
- **No jitter** in file boundaries (uses stable GPSDO-derived timing)
- **Improving accuracy** as time_snap converges
- **Reprocessability** - can re-analyze with better time_snap later
