# Timing Measurement Hierarchy

## Overview

The timing system uses **three levels of measurements** with different precision and purposes:

```
Gold Standard → Operational → Fallback

1. Tone-to-Tone (±0.1 ppm)
   ↓
2. RTP vs NTP (±10ms)
   ↓
3. RTP vs Wall Clock (±seconds)
```

---

## 1. **Tone-to-Tone Measurement** 🥇 Gold Standard

### What It Measures
**A/D clock frequency stability** - Is the hardware clock running at exactly 16000 Hz?

### Method
```python
# Between consecutive tone detections:
Tone A: RTP=X1, UTC=Y1 (WWV/CHU ground truth)
Tone B: RTP=X2, UTC=Y2 (WWV/CHU ground truth)

# Expected RTP samples based on tone times:
expected_samples = (Y2 - Y1) * 16000

# Actual RTP samples:
actual_samples = X2 - X1

# A/D clock frequency ratio:
clock_ratio = actual_samples / expected_samples

# Drift in PPM:
drift_ppm = (clock_ratio - 1.0) * 1e6
```

### Example
```
Tone A: RTP=1,000,000  UTC=100.0s
Tone B: RTP=5,760,000  UTC=400.0s

Expected: (400.0 - 100.0) * 16000 = 4,800,000 samples
Actual:   5,760,000 - 1,000,000   = 4,760,000 samples

Ratio: 4,760,000 / 4,800,000 = 0.991667
Drift: (0.991667 - 1.0) * 1e6 = -8,333 ppm

→ A/D clock running 8,333 ppm (0.83%) SLOW
```

### Characteristics
- **Precision:** ±0.1 ppm (parts per million)
- **Reference:** WWV/CHU atomic clock (cesium standard)
- **Update Rate:** Every ~5 minutes (when fresh tones detected)
- **What It Shows:** Pure A/D hardware clock stability
- **Independence:** Completely independent of system clock

### Interpretation
| Drift (ppm) | Quality | Meaning |
|-------------|---------|---------|
| < ±1 ppm | Excellent | TCXO or better oscillator |
| < ±10 ppm | Good | Quality crystal oscillator |
| < ±100 ppm | Fair | Standard crystal |
| > ±100 ppm | Poor | Unstable or cheap oscillator |

---

## 2. **RTP vs NTP Measurement** ⚡ Operational

### What It Measures
**RTP clock offset from real time** - with NTP-synced system clock as reference

### Method
```python
# At any moment:
rtp_predicted_time = time_snap.calculate_sample_time(current_rtp)
actual_time = time.time()  # NTP-synced

drift_ms = (actual_time - rtp_predicted_time) * 1000
```

### Characteristics
- **Precision:** ±10ms (limited by NTP)
- **Reference:** NTP-synchronized system clock
- **Update Rate:** Every minute
- **What It Shows:** Combined (RTP + system clock) drift
- **Limitation:** Includes both RTP instability AND residual NTP error

### Interpretation
| Drift (ms) | Quality | Meaning |
|------------|---------|---------|
| < ±5 ms | Excellent | Stable RTP + good NTP |
| < ±50 ms | Good | Some drift or NTP jitter |
| < ±500 ms | Fair | Significant drift |
| > ±500 ms | Poor | Major timing problem |

### Use Cases
- Real-time monitoring
- Detecting gross RTP failures
- Operational health checks
- Minute-by-minute tracking

---

## 3. **RTP vs Wall Clock** ⚠️ Fallback Only

### What It Measures
**RTP clock offset** - but with unsynchronized system clock (poor reference)

### Method
```python
# Same as above, but time.time() is NOT NTP-synced
drift_ms = (time.time() - rtp_predicted_time) * 1000
```

### Characteristics
- **Precision:** ±seconds (wall clock jitter)
- **Reference:** Unsynchronized system clock (drifts significantly)
- **Update Rate:** Every minute
- **What It Shows:** Mostly wall clock jitter, not RTP stability
- **Limitation:** Wall clock itself may drift several seconds per day

### Interpretation
Only useful for detecting **gross failures** (> 10 seconds drift).

---

## Measurement Comparison Table

| Metric | Tone-to-Tone | RTP vs NTP | RTP vs Wall Clock |
|--------|--------------|------------|-------------------|
| **Precision** | ±0.1 ppm | ±10 ms | ±seconds |
| **Update Rate** | ~5 min | 1 min | 1 min |
| **Reference** | Cesium atomic | NTP (GPS/atomic) | Quartz crystal |
| **Measures** | Pure A/D clock | RTP + NTP combined | RTP + wall clock |
| **Use Case** | Scientific | Operational | Emergency only |
| **Trust Level** | Absolute | High | Low |

---

## Quality Classification Logic

### Source Quality (Anchor)
1. **TONE_LOCKED** - Fresh tone detection (< 5 min old)
2. **INTERPOLATED** - Aging tone (5 min - 1 hour old)
3. **NTP_SYNCED** - NTP-based timing (no recent tone)
4. **WALL_CLOCK** - Unsynchronized (no tone, no NTP)

### Measurement Reliability
```python
if tone_to_tone_available:
    reliability = "DEFINITIVE"  # ±0.1 ppm
elif ntp_synced:
    reliability = "RELIABLE"     # ±10 ms
else:
    reliability = "APPROXIMATE"  # ±seconds
```

---

## Implementation Details

### CSV Output Columns
```
timestamp, channel, source_type, quality,
snr_db, confidence, age_seconds,
rtp_anchor, utc_anchor,
drift_ms,              # RTP vs NTP/wall clock (every minute)
jitter_ms,             # Variation in drift_ms
ntp_offset_ms,         # System clock vs NTP
health_score,          # 0-100 overall quality
tone_drift_ppm         # Tone-to-tone A/D clock drift (every ~5 min)
```

### Logging
```
[INFO] Baseline tone established for A/D clock measurement
[INFO] Tone-to-tone A/D clock drift: +2.34 ppm (over 300.0s, 4800000 samples)
[DEBUG] Large drift (2345.6ms) - NTP not synced, wall clock jitter included
```

---

## Best Practices

### For Scientific Analysis
✅ **Use:** Tone-to-tone measurements only
- Most accurate A/D clock characterization
- Immune to system clock issues
- Updates every ~5 minutes when tones detected

### For Operational Monitoring
✅ **Use:** RTP vs NTP measurements
- Good minute-by-minute tracking
- Catches RTP issues quickly
- Requires NTP sync for reliability

### For Debugging
⚠️ **Use:** Both measurements together
- Tone-to-tone: Hardware clock health
- RTP vs NTP: System integration health
- Large divergence indicates problem

---

## Common Scenarios

### Scenario 1: Stable System
```
Tone-to-tone: +0.5 ppm (excellent A/D clock)
RTP vs NTP:   ±8 ms   (good operational stability)
Status: 🟢 Perfect
```

### Scenario 2: A/D Clock Drift
```
Tone-to-tone: -150 ppm (A/D clock running slow!)
RTP vs NTP:   +45 ms  (accumulating offset)
Status: 🟡 Hardware issue - time_snap needs frequent updates
```

### Scenario 3: NTP Loss
```
Tone-to-tone: +1.2 ppm (A/D clock excellent)
RTP vs NTP:   ±2000 ms (wall clock jitter)
Status: 🟡 A/D is fine, but system clock lost NTP sync
```

### Scenario 4: Tone Loss
```
Tone-to-tone: N/A (no recent tones)
RTP vs NTP:   ±15 ms (interpolating from old time_snap)
Status: 🟡 Operational OK, but aging reference
```

---

## Summary

**Three-tier measurement system:**

1. **Tone-to-Tone** (PPM) - *What is the A/D hardware doing?*
2. **RTP vs NTP** (ms) - *Is the system staying synchronized?*
3. **RTP vs Wall Clock** (seconds) - *Emergency gross error detection*

Each serves a purpose. Use the right measurement for the right question!
