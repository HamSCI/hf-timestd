# HF Time Standard - Metrology Reference

**Comprehensive guide to the metrological methodology used in hf-timestd for RTP-to-UTC calibration and time transfer.**

**Author:** Michael James Hauan (AC0G)  
**Last Updated:** January 29, 2026 (v6.4.0)

---

## Overview

The HF Time Standard system derives UTC from shortwave time signal broadcasts (WWV, WWVH, CHU, BPM) received via Software Defined Radio. The fundamental challenge is establishing a precise relationship between the **RTP timestamp domain** (sample counts from the SDR) and **UTC** (Coordinated Universal Time).

This document describes the **Timing Bootstrap** methodology introduced in v6.3.0, which provides a robust, broadcast-validated approach to RTP-to-UTC calibration.

---

## The RTP-to-UTC Calibration Problem

### Background

The ka9q-radio SDR system delivers IQ samples via RTP (Real-time Transport Protocol). Each RTP packet contains:

- **RTP Timestamp**: A 32-bit sample counter (wraps every ~49.7 hours at 24 kHz)
- **Payload**: IQ samples at the configured sample rate (24,000 Hz)

The system clock provides wall-clock time, but this is **not directly tied** to the RTP timestamp domain. To convert detected tone arrivals (measured in RTP samples) to UTC, we need to establish the **RTP-to-UTC offset**:

```
UTC = RTP_timestamp / sample_rate + offset
```

### The Challenge

Several factors complicate this calibration:

1. **RTP timestamps are arbitrary** - They start at a random value when the SDR begins streaming
2. **System clock uncertainty** - Even with NTP, system time has ±10ms uncertainty
3. **Propagation delay** - Radio signals take 4-45ms to travel from transmitter to receiver
4. **Ionospheric variability** - Propagation delay varies with solar conditions, time of day, and frequency

### Previous Approaches (Pre-v6.3)

Earlier versions attempted to derive the RTP-to-UTC offset purely from tone detection:

1. Detect a tone in the audio
2. Assume the tone was transmitted at a known UTC second boundary
3. Calculate offset from the detected RTP timestamp

**Problem**: This approach suffered from a ~340ms systematic error because:
- Buffer boundaries were not precisely aligned to minute markers
- Per-second ticks were often detected instead of minute markers
- The system clock time associated with buffers was ambiguous (start vs. end)

---

## The Timing Bootstrap Methodology (v6.3.0)

### Design Philosophy

The new methodology separates two distinct problems:

1. **Offset Establishment**: Use buffer metadata (RTP + system time) for initial calibration
2. **Offset Validation**: Use broadcast signals to validate and refine the offset

This approach leverages the fact that:
- The system clock (via NTP) is accurate to ±10ms
- Buffer metadata provides a direct RTP↔system_time correspondence
- Broadcast signals provide physical validation of the offset

### Two-Phase Bootstrap

#### Phase 1: Metadata-Based Offset Establishment

When the Core Recorder writes each minute buffer, it records:

```json
{
  "start_rtp_timestamp": 164520840,
  "start_system_time": 1769306160.0665529,
  "sample_rate": 24000
}
```

The Timing Bootstrap uses this metadata directly:

```python
offset = system_time - (rtp_timestamp / sample_rate)
# offset ≈ 1769299305.03 seconds
```

**Key insight**: The buffer's `start_system_time` is captured at the moment the first sample arrives, providing a direct correspondence between the RTP and UTC domains.

#### Phase 2: Broadcast Signal Validation

Once the initial offset is established, tone detection validates that:

1. Detected tones arrive at expected times (within propagation delay tolerance)
2. Station identities match expected characteristics (tone frequency, schedule)
3. Multi-station ordering is geographically consistent

### State Machine

The bootstrap progresses through four states:

```
ACQUIRING → CORRELATING → TRACKING → LOCKED
```

| State | Description | Criteria to Advance |
|-------|-------------|---------------------|
| **ACQUIRING** | Initial state, no offset | First tone cluster detected |
| **CORRELATING** | Validating cluster consistency | Recurring clusters at 60s intervals |
| **TRACKING** | Clusters validated, awaiting time confirmation | NTP-based time confirmation |
| **LOCKED** | Time confirmed, offset stable | Continuous validation |

### Convergence Timeline (v6.4)

| Time | State | Uncertainty |
|------|-------|-------------|
| 0 min | ACQUIRING | Unknown |
| 1 min | CORRELATING | ±30ms |
| 2 min | TRACKING → LOCKED | ±5ms (NTP-confirmed) |
| 10+ min | LOCKED (refined) | <1ms (with BCD/FSK) |

### NTP-Based Time Confirmation (v6.4)

**Architecture Change (2026-01-29):** Bootstrap no longer requires BCD/FSK decode to reach LOCKED state.

**Previous Approach (v6.3):**
- Bootstrap waited for BCD/FSK decode to confirm UTC minute
- BCD decode fragile under HF fading (often 0/7 markers)
- Pipeline blocked indefinitely waiting for decode

**New Approach (v6.4):**
- Cluster detection finds minute markers (800ms tones at second 0)
- `wallclock_time` from GPSDO "steel ruler" identifies UTC minute directly
- Bootstrap transitions to LOCKED based on NTP confirmation (~2 minutes)
- BCD/FSK decode becomes OPTIONAL refinement for sub-second accuracy

**Implementation:**
```python
# confirm_time_from_ntp() in timing_bootstrap.py
# Uses NTP-derived wallclock from cluster detection
minute_boundary_wallclock = (best_wallclock // 60) * 60
utc_dt = datetime.utcfromtimestamp(minute_boundary_wallclock)
# Compute RTP-to-UTC offset from anchor_rtp and UTC minute
```

**Metrology Service Timing (v6.4):**
- Each raw buffer file contains `start_system_time` (NTP-derived wallclock)
- Metrology uses this directly instead of converting through bootstrap RTP reference
- Avoids SSRC mismatch issues (each channel has independent RTP epoch)

---

## Discriminating Features

To validate station identity and offset accuracy, the system uses multiple discriminating features:

### 1. Tone Frequency

Different stations use different minute marker frequencies:

| Station | Frequency | Duration |
|---------|-----------|----------|
| WWV | 1000 Hz | 800 ms |
| WWVH | 1200 Hz | 800 ms |
| CHU | 1000 Hz | 500 ms (1000 ms at top of hour) |
| BPM | 1000 Hz | 300 ms |

**Validation**: If a detection claims to be WWVH but the tone frequency is 1000 Hz, the detection is rejected.

### 2. Tone Schedule (Ground-Truth Minutes)

During certain minutes, only one station broadcasts 500/600 Hz tones:

**WWV-only minutes**: 1, 16, 17, 19  
**WWVH-only minutes**: 2, 43, 44, 45, 46, 47, 48, 49, 50, 51

**Validation**: If WWVH is detected at minute 16 with a 500/600 Hz tone, the detection is rejected (WWV-only minute).

### 3. Test Signal Minutes

| Minute | Station | Content |
|--------|---------|---------|
| 8 | WWV | Test signal (other station silent) |
| 44 | WWVH | Test signal (other station silent) |

### 4. Geographic Ordering

For receivers in continental North America:

- **WWV** (Fort Collins, Colorado) arrives first
- **WWVH** (Kauai, Hawaii) arrives 15-25ms later

**Validation**: If WWVH arrives before WWV on a shared frequency, the detection is rejected.

### 5. Unambiguous Channels

Some frequencies have only one transmitter:

| Channel | Station |
|---------|---------|
| CHU 3.33 MHz | CHU only |
| CHU 7.85 MHz | CHU only |
| CHU 14.67 MHz | CHU only |
| WWV 20 MHz | WWV only |
| WWV 25 MHz | WWV only |

Detections on these channels provide high-confidence station identification.

---

## Geographic Priors

The system computes expected propagation delays based on:

1. **Transmitter locations** (known precisely)
2. **Receiver location** (from configuration)
3. **Great circle distance**
4. **Ionospheric path factor** (1.15× for typical skywave)

### Transmitter Coordinates

| Station | Latitude | Longitude |
|---------|----------|-----------|
| WWV | 40.68°N | 105.04°W |
| WWVH | 21.99°N | 159.76°W |
| CHU | 45.30°N | 75.75°W |
| BPM | 34.95°N | 109.55°E |

### Expected Delays (Example: Columbia, MO)

| Station | Distance | Expected Delay | Range |
|---------|----------|----------------|-------|
| CHU | 1522 km | 5.8 ms | 4.7-8.8 ms |
| WWV | 1120 km | 4.3 ms | 3.4-6.4 ms |
| WWVH | 6600 km | 25.3 ms | 20.3-38.0 ms |
| BPM | 11504 km | 44.1 ms | 35.3-66.2 ms |

---

## Implementation Details

### Key Classes

#### `TimingBootstrap` (`timing_bootstrap.py`)

The main bootstrap state machine:

```python
class TimingBootstrap:
    def __init__(self, receiver_lat: float, receiver_lon: float):
        """Initialize with receiver coordinates for geographic priors."""
        
    def establish_offset_from_metadata(
        self,
        buffer_rtp_start: int,
        buffer_system_time: float,
        channel: str
    ) -> Optional[str]:
        """Establish or validate RTP-to-UTC offset from buffer metadata."""
        
    def validate_station_by_tone_frequency(
        self,
        detected_station: str,
        tone_frequency_hz: float
    ) -> Tuple[bool, float]:
        """Validate station identity by minute marker tone frequency."""
        
    def validate_station_by_schedule(
        self,
        detected_station: str,
        minute_of_hour: int,
        has_500_600_hz_tone: bool
    ) -> Tuple[bool, float]:
        """Validate station identity using the 500/600 Hz tone schedule."""
        
    def validate_wwv_wwvh_ordering(
        self,
        wwv_rtp: int,
        wwvh_rtp: int,
        frequency_khz: int
    ) -> Tuple[bool, float]:
        """Validate that WWVH arrives after WWV on shared frequencies."""
```

### Constants

```python
# Ground-truth minutes
WWV_ONLY_TONE_MINUTES = {1, 16, 17, 19}
WWVH_ONLY_TONE_MINUTES = {2, 43, 44, 45, 46, 47, 48, 49, 50, 51}

# Test signal minutes
WWV_TEST_SIGNAL_MINUTE = 8
WWVH_TEST_SIGNAL_MINUTE = 44

# Tone characteristics
TONE_CHARACTERISTICS = {
    'WWV': {'frequency_hz': 1000, 'duration_ms': 800},
    'WWVH': {'frequency_hz': 1200, 'duration_ms': 800},
    'CHU': {'frequency_hz': 1000, 'duration_ms': 500},
    'BPM': {'frequency_hz': 1000, 'duration_ms': 300},
}

# Unambiguous channels
UNAMBIGUOUS_CHANNELS = {
    'CHU_3330': 'CHU',
    'CHU_7850': 'CHU',
    'CHU_14670': 'CHU',
    'WWV_20000': 'WWV',
    'WWV_25000': 'WWV',
}
```

---

## Uncertainty Analysis

### Sources of Uncertainty

| Source | Magnitude | Notes |
|--------|-----------|-------|
| NTP synchronization | ±10 ms | System clock accuracy |
| Buffer timestamp jitter | ±1 ms | Kernel scheduling |
| Tone detection | ±0.1 ms | Cross-correlation precision |
| Propagation delay | ±5-15 ms | Ionospheric variability |

### Combined Uncertainty

After LOCKED state is achieved:

- **Offset uncertainty**: <0.1 ms (metadata consistency)
- **Absolute UTC uncertainty**: ±10 ms (limited by NTP)
- **Relative timing precision**: ±0.1 ms (tone detection)

### Improving Absolute Accuracy

To achieve better than ±10 ms absolute accuracy:

1. **GNSS disciplined clock** - Provides ±1 μs system time
2. **Multi-frequency TEC correction** - Removes ionospheric delay uncertainty
3. **Multi-station fusion** - Geometric solution for UTC origin

---

## Operational Considerations

### Startup Behavior

1. Service starts with bootstrap in ACQUIRING state
2. First buffer metadata establishes initial offset
3. Subsequent buffers validate consistency
4. After 10 minutes, system reaches LOCKED state

### Handling Discontinuities

If the RTP stream restarts (e.g., radiod restart):

1. Bootstrap detects inconsistent metadata (>100ms deviation)
2. System retreats to ACQUIRING state
3. Re-establishes offset from new metadata
4. Returns to LOCKED state within 10 minutes

### Monitoring

Check bootstrap status in logs:

```bash
grep "BOOTSTRAP" /var/log/hf-timestd/phase2-*.log
```

Expected progression:
```
[BOOTSTRAP] Offset from metadata: 1769299305.031553s
[BOOTSTRAP] Metadata offset validated → TRACKING
[BOOTSTRAP] Metadata offset LOCKED: 1769299305.031553s (uncertainty=0.0ms)
```

---

## References

### Standards

- **ITU-R TF.460-6**: Standard-frequency and time-signal emissions
- **NIST Special Publication 432**: NIST Time and Frequency Services

### Related Documentation

- `TECHNICAL_REFERENCE.md` - System architecture and algorithms
- `ARCHITECTURE.md` - Design philosophy
- `docs/METROLOGIST_DESCRIPTION.md` - Detailed metrological analysis

---

**Version**: 6.3.0  
**Last Updated**: January 25, 2026
