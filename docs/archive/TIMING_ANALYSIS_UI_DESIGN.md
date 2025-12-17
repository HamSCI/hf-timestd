# Timing Analysis UI Design - Comprehensive Proposal

## Executive Summary

This document proposes enhancements to the timing-dashboard.html page to provide comprehensive visibility into:
1. **Time base establishment** status per channel
2. **Timing accuracy** and stability metrics  
3. **RTP vs time_snap drift** measurements
4. **NTP comparison** for validation
5. **Time source transitions** (tone → NTP → wall clock)
6. **Historical trending** and anomaly detection

---

## Design Philosophy

### Key Principles

1. **At-a-glance health** - Immediate understanding of timing system status
2. **Drill-down capability** - Progressive disclosure of details
3. **Temporal awareness** - Show history, trends, and transitions
4. **Comparative analysis** - Cross-channel and cross-method comparisons
5. **Actionable insights** - Clear indicators when intervention needed

### Target Users

- **Operators:** Is my timing good enough? Do I need to act?
- **Researchers:** What's the quality of my time base for analysis?
- **Developers:** Is the timing system working correctly?

---

## Proposed Page Structure

```
┌─────────────────────────────────────────────────────────────┐
│ 🎯 Primary Time Reference (Global)                          │
│  ├─ Current source: WWV 10 MHz                             │
│  ├─ Quality: TONE_LOCKED (±1ms)                            │
│  ├─ Age: 47 seconds                                         │
│  └─ Next check: 4m 13s                                      │
├─────────────────────────────────────────────────────────────┤
│ 📊 System Timing Health (5-minute window)                   │
│  ├─ Tone-locked: 6/9 channels (67%)                       │
│  ├─ RTP drift: ±0.3ms (excellent)                          │
│  ├─ NTP offset: +2.1ms                                      │
│  └─ Time source transitions: 0 (stable)                    │
├─────────────────────────────────────────────────────────────┤
│ 🕐 Time Source Timeline (24-hour view)                      │
│  [Interactive chart showing time source per channel]        │
│   ▓▓▓▓▓▓▓▓▓▓▓░░░░░▓▓▓▓▓▓▓▓▓▓                              │
│   Blue=Tone  Gray=NTP  Red=Wall Clock                       │
├─────────────────────────────────────────────────────────────┤
│ 📈 RTP Drift Analysis (Per channel, 1-hour window)          │
│  [Chart: RTP timestamp vs time_snap over time]             │
│   Shows: Drift rate, jitter, stability                      │
├─────────────────────────────────────────────────────────────┤
│ 🔄 Time Source Transitions (Recent events)                  │
│  [Timeline of tone detections, NTP fallbacks, upgrades]    │
├─────────────────────────────────────────────────────────────┤
│ 📋 Per-Channel Timing Detail                                │
│  [Table with expanded metrics per channel]                  │
│   - Current source & confidence                             │
│   - Time since last tone detection                          │
│   - Drift rate (ms/hour)                                    │
│   - Jitter (±ms)                                            │
│   - NTP comparison                                           │
│   - Health score                                             │
└─────────────────────────────────────────────────────────────┘
```

---

## Section 1: Primary Time Reference (Hero Section)

### Purpose
Show the **system-wide timing reference** - the "best" time source currently being used.

### Metrics Displayed

```
┌─────────────────────────────────────────────────┐
│ 🎯 Primary Time Reference                        │
├─────────────────────────────────────────────────┤
│  Current Source: WWV 10 MHz - 1000 Hz Tone     │
│  Quality Level:  TONE_LOCKED (±1ms precision)   │
│  SNR:           28.4 dB                          │
│  Confidence:    0.95 (Excellent)                │
│  Age:           47 seconds                       │
│  Next Check:    4m 13s                          │
│  RTP Anchor:    1,558,805,472                   │
│  UTC Anchor:    2025-11-26T03:45:00.000Z        │
└─────────────────────────────────────────────────┘
```

### Visual Indicators

**Quality Badge:**
- 🟢 **TONE_LOCKED** (green) - Locked to WWV/CHU tone (±1ms)
- 🔵 **NTP_SYNCED** (blue) - Using NTP-synchronized clock (±10ms)
- 🟡 **INTERPOLATED** (yellow) - Using aged tone reference (±50ms)
- 🔴 **WALL_CLOCK** (red) - Unsynchronized clock (±seconds)

**Age Indicator:**
- Fresh (< 1 min): Solid green
- Recent (1-5 min): Light green  
- Aging (5-60 min): Yellow
- Stale (> 60 min): Red

### API Endpoint

```javascript
// GET /api/v1/timing/primary-reference
{
  "source_channel": "WWV 10 MHz",
  "source_type": "wwv_startup",  // or 'chu_startup', 'ntp', 'wall_clock'
  "station": "WWV",
  "quality": "TONE_LOCKED",
  "precision_ms": 1.0,
  "confidence": 0.95,
  "snr_db": 28.4,
  "tone_frequency_hz": 1000.0,
  "age_seconds": 47,
  "next_check_seconds": 253,
  "rtp_anchor": 1558805472,
  "utc_anchor": 1732610700.0,
  "utc_anchor_iso": "2025-11-26T03:45:00.000Z"
}
```

---

## Section 2: System Timing Health (Summary Cards)

### Purpose
Provide **quick health metrics** across all channels in a single glance.

### Layout (4-column grid)

```
┌──────────────────┬──────────────────┬──────────────────┬──────────────────┐
│ Tone-Locked      │ RTP Drift        │ NTP Offset       │ Transitions      │
│ Channels         │ (5-min avg)      │ (Current)        │ (Last hour)      │
├──────────────────┼──────────────────┼──────────────────┼──────────────────┤
│      6/9         │    ±0.3ms        │    +2.1ms        │       0          │
│    (67%)         │  (excellent)     │   (good)         │   (stable)       │
│                  │                  │                  │                  │
│ 🟢 WWV 5 MHz    │ Range: 0.1-0.5ms │ System clock:    │ Last: 47m ago    │
│ 🟢 WWV 10 MHz   │ Jitter: ±0.08ms  │   slightly fast  │ Type: tone→tone  │
│ 🟢 WWV 15 MHz   │ Trend: stable    │ Quality: synced  │ Channel: WWV 5   │
│ ...              │                  │                  │                  │
│ 🔵 CHU 3.33      │                  │                  │                  │
│ 🔵 CHU 7.85      │                  │                  │                  │
└──────────────────┴──────────────────┴──────────────────┴──────────────────┘
```

### Metrics Explained

#### 1. Tone-Locked Channels
- **Count/Total:** How many channels have tone-detected time_snap
- **Percentage:** Overall tone-lock ratio
- **List:** Which channels (with status color)
- **Trend:** Up/down from previous period

#### 2. RTP Drift
- **5-min average:** Mean drift across all channels
- **Range:** Min to max drift observed
- **Jitter:** Standard deviation (stability metric)
- **Trend:** Increasing/decreasing/stable
- **Quality:** Excellent (<1ms), Good (1-5ms), Fair (5-10ms), Poor (>10ms)

#### 3. NTP Offset
- **Current offset:** System clock vs NTP reference
- **Direction:** Fast/slow relative to NTP
- **Sync quality:** Synced/degraded/lost
- **Age:** Time since last NTP update

#### 4. Transitions
- **Count:** Time source changes in last hour
- **Last transition:** Time and type
- **Reason:** Why it transitioned (signal lost, better source found, etc.)
- **Stability:** Stable/unstable based on transition frequency

---

## Section 3: Time Source Timeline (24-Hour View)

### Purpose
Visualize **when and why** time sources changed over the past 24 hours.

### Chart Type: Stacked Timeline Chart

```
Channel          00:00    06:00    12:00    18:00    24:00
────────────────┼────────┼────────┼────────┼────────┼────
WWV 2.5 MHz     ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓
WWV 5 MHz       ▓▓▓▓▓▓▓▓▓░░░░░░░░▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓
WWV 10 MHz      ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓
WWV 15 MHz      ▓▓▓▓▓▓▓▓░░░░░░░░░░░░░░░░▓▓▓▓▓▓▓▓▓▓▓▓▓
WWV 20 MHz      ░░░░░░░░░░░░░░░░░░░░▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓
WWV 25 MHz      ░░░░░░░░░░░░░░░░▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░░░░░░
CHU 3.33 MHz    ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓
CHU 7.85 MHz    ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓
CHU 14.67 MHz   ▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░░░░░░░▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓

Legend:  ▓ Tone-locked (±1ms)   ░ NTP (±10ms)   ▒ Wall clock (±seconds)
         ⚡ Transition event      ⭐ High-quality detection (SNR > 30dB)
```

### Interactive Features

**Hover:**
- Show exact time, source, SNR, confidence
- Duration in current state
- Why transitioned (if applicable)

**Click:**
- Zoom to specific time period
- Show detailed event log
- Link to discrimination data for that period

**Pattern Recognition:**
- **Daily cycle:** HF propagation follows sunrise/sunset
- **Dropout periods:** When tones unavailable (e.g., 20 MHz at night)
- **Stability periods:** Long stretches of tone-lock indicate good propagation
- **Transition clusters:** Multiple transitions suggest unstable conditions

### Data Structure

```javascript
// GET /api/v1/timing/timeline?hours=24&channel=all
{
  "channels": {
    "WWV 10 MHz": [
      {
        "start_time": "2025-11-26T00:00:00Z",
        "end_time": "2025-11-26T08:30:00Z",
        "source": "wwv_startup",
        "quality": "TONE_LOCKED",
        "snr_avg_db": 28.4,
        "confidence_avg": 0.95,
        "drift_rate_ms_per_hour": 0.3
      },
      {
        "start_time": "2025-11-26T08:30:00Z",
        "end_time": "2025-11-26T09:15:00Z",
        "source": "ntp",
        "quality": "NTP_SYNCED",
        "transition_reason": "Tone SNR dropped below 10dB"
      },
      // ... more segments
    ]
  },
  "transitions": [
    {
      "time": "2025-11-26T08:30:15Z",
      "channel": "WWV 10 MHz",
      "from_source": "wwv_startup",
      "to_source": "ntp",
      "reason": "Tone SNR dropped below 10dB",
      "last_snr_db": 8.2
    }
  ]
}
```

---

## Section 4: RTP Drift Analysis (Multi-Channel Chart)

### Purpose
Show **how accurate** the time_snap is by comparing RTP timestamps to derived UTC times.

### Chart Type: Time Series with Confidence Bands

```
Drift (ms)
   +2 ┤                                    
   +1 ┤   ╱╲      ╱╲                      
    0 ┼──╱──╲────╱──╲──────────────────── 
   -1 ┤          ╲  ╲   ╱╲                
   -2 ┤           ╲──╲─╱──╲─────          
      └───────────────────────────────────
      00:00    06:00    12:00    18:00

Lines:
  WWV 5 MHz  (green)    - Very stable, ±0.2ms
  WWV 10 MHz (blue)     - Stable, ±0.4ms
  WWV 15 MHz (orange)   - Some drift, ±1.2ms
  
Shaded regions:
  Green band: ±1ms (excellent for tone-locked)
  Yellow band: ±10ms (acceptable for NTP)
  Red: >10ms (needs attention)
```

### Calculation Method

**RTP Drift Measurement:**

```python
# For each minute boundary:
expected_rtp = time_snap_rtp + (utc_minutes_elapsed * 60 * sample_rate)
actual_rtp = measured_rtp_timestamp_at_minute_boundary
drift_samples = actual_rtp - expected_rtp
drift_ms = (drift_samples / sample_rate) * 1000.0

# Accumulated over time to show drift rate
drift_rate_ms_per_hour = linear_regression(time, drift_ms)
```

### Key Metrics Displayed

**Per Channel:**
- **Current drift:** ±0.3ms
- **Drift rate:** +0.05ms/hour (very slow increase)
- **Peak-to-peak jitter:** ±0.15ms (stable)
- **RMS jitter:** 0.08ms
- **Quality grade:** A (excellent)

**Interpretation:**
- **Drift < 1ms:** Time_snap is accurate, tone-locked working well
- **Drift 1-10ms:** Acceptable, may be on NTP or aged tone
- **Drift > 10ms:** Problem - investigate tone detection or NTP
- **Increasing drift:** Time_snap anchor aging, needs refresh
- **Sudden jumps:** Time source transitioned (expected)

---

## Section 5: NTP Comparison

### Purpose
Validate time_snap accuracy by **comparing to independent NTP reference**.

### Layout

```
┌─────────────────────────────────────────────────────────┐
│ 🔄 NTP Comparison & Validation                          │
├─────────────────────────────────────────────────────────┤
│                                                          │
│  System NTP Status                                      │
│  ├─ Synchronized: ✅ Yes                                │
│  ├─ Stratum: 2 (secondary reference)                   │
│  ├─ NTP Server: time.nist.gov                           │
│  ├─ Offset: +2.1ms (system fast)                       │
│  ├─ Jitter: ±0.5ms                                      │
│  └─ Last Update: 43 seconds ago                        │
│                                                          │
│  Time_Snap vs NTP Comparison                            │
│  ├─ Agreement: ±2.8ms (excellent)                      │
│  ├─ Trend: Stable over last hour                       │
│  └─ Assessment: Time_snap validated by NTP             │
│                                                          │
│  [Chart: time_snap UTC - NTP UTC over time]            │
│   Shows drift of tone-based time relative to NTP       │
│                                                          │
└─────────────────────────────────────────────────────────┘
```

### Key Insights

**Agreement < 5ms:**
- ✅ Time_snap is accurate
- Tone detection working correctly
- Safe to use for scientific analysis

**Agreement 5-20ms:**
- ⚠️ Some discrepancy
- Check tone SNR and quality
- May be ionospheric delay variation

**Agreement > 20ms:**
- ❌ Problem detected
- Investigate tone detection
- Check for system clock issues
- Consider reprocessing data

### Chart Features

**Plot Lines:**
1. **Time_snap UTC** (from tone detection)
2. **NTP UTC** (system clock)  
3. **Difference** (time_snap - NTP)

**Annotations:**
- Mark tone detection events
- Mark NTP sync updates
- Highlight large deviations
- Show ionospheric delay estimates (WWV propagation ~7-25ms)

---

## Section 6: Time Source Transitions (Event Log)

### Purpose
Track **when and why** the system switched between time sources.

### Layout: Timeline with Event Cards

```
Recent Time Source Transitions (Last 24 hours)
═══════════════════════════════════════════════

  08:30:15  WWV 10 MHz: Tone → NTP
  ├─ Reason: Tone SNR dropped below 10dB (measured 8.2dB)
  ├─ Duration on tone: 8h 30m
  ├─ New precision: ±10ms (was ±1ms)
  └─ Action: Monitoring for tone recovery

  09:15:42  WWV 10 MHz: NTP → Tone
  ├─ Reason: Tone detected (SNR 24.5dB, confidence 0.95)
  ├─ Duration on NTP: 45 minutes
  ├─ Precision improved: ±10ms → ±1ms
  └─ Quality: Excellent signal restored

  12:00:03  WWV 15 MHz: Tone → Tone (upgrade)
  ├─ Reason: Better confidence (0.92 → 0.97)
  ├─ New SNR: 32.1dB
  ├─ Precision: ±1ms (maintained)
  └─ Action: Adopted better reference

═══════════════════════════════════════════════
Total transitions today: 8
Avg duration per source: 3h 12m
Stability score: 85/100 (good)
```

### Event Types

1. **Tone → NTP**
   - **Cause:** Tone lost (SNR drop, propagation fade)
   - **Impact:** Precision degraded ±1ms → ±10ms
   - **Action:** Monitor for tone recovery

2. **NTP → Tone**
   - **Cause:** Tone detected after outage
   - **Impact:** Precision improved ±10ms → ±1ms
   - **Action:** Quality upgrade, no intervention needed

3. **Tone → Tone (upgrade)**
   - **Cause:** Better tone detected (higher SNR/confidence)
   - **Impact:** Quality maintained or improved
   - **Action:** Reference upgraded for accuracy

4. **NTP → Wall Clock**
   - **Cause:** NTP sync lost
   - **Impact:** Precision degraded ±10ms → ±seconds
   - **Action:** **ALERT** - Check system NTP configuration

5. **Wall Clock → NTP**
   - **Cause:** NTP sync restored
   - **Impact:** Precision improved ±seconds → ±10ms
   - **Action:** Baseline restored, monitor for tone

### Stability Metrics

**Stability Score (0-100):**
- **90-100:** Excellent - minimal transitions, mostly tone-locked
- **70-89:** Good - some transitions, mostly stable
- **50-69:** Fair - frequent transitions, check propagation
- **0-49:** Poor - very unstable, investigate system issues

**Calculation:**
```
base_score = 100
penalty_per_transition = 2
penalty_for_wall_clock_use = 20
bonus_for_long_tone_duration = 5

score = base_score 
        - (transitions_count * penalty_per_transition)
        - (wall_clock_minutes * penalty_for_wall_clock_use / 60)
        + (tone_locked_hours * bonus_for_long_tone_duration)
```

---

## Section 7: Per-Channel Timing Detail (Expanded Table)

### Purpose
Comprehensive **per-channel metrics** for detailed analysis.

### Table Columns

| Channel | Source | Quality | Age | Drift Rate | Jitter | NTP Δ | SNR | Confidence | Health |
|---------|--------|---------|-----|------------|--------|-------|-----|------------|--------|
| WWV 5 MHz | 🟢 Tone | A | 23s | +0.02ms/h | ±0.08ms | +2.3ms | 32.1dB | 0.97 | 98/100 |
| WWV 10 MHz | 🟢 Tone | A | 47s | +0.05ms/h | ±0.12ms | +2.1ms | 28.4dB | 0.95 | 97/100 |
| WWV 15 MHz | 🟢 Tone | B | 1m 15s | +0.15ms/h | ±0.25ms | +2.5ms | 18.7dB | 0.90 | 92/100 |
| WWV 20 MHz | 🔵 NTP | C | 3h 22m | +1.2ms/h | ±2.1ms | +1.8ms | - | 0.60 | 75/100 |
| WWV 25 MHz | 🔵 NTP | C | 5h 45m | +2.4ms/h | ±3.5ms | +0.8ms | - | 0.50 | 68/100 |
| CHU 3.33 MHz | 🟢 Tone | A | 12s | +0.01ms/h | ±0.05ms | +2.4ms | 38.2dB | 0.98 | 99/100 |
| CHU 7.85 MHz | 🟢 Tone | A | 8s | -0.02ms/h | ±0.06ms | +2.2ms | 35.1dB | 0.97 | 99/100 |
| CHU 14.67 MHz | 🟢 Tone | B | 2m 34s | +0.08ms/h | ±0.18ms | +2.6ms | 20.3dB | 0.92 | 94/100 |

### Column Definitions

**Source:**
- 🟢 Tone-locked (WWV/CHU tone detected)
- 🔵 NTP-synced (using system NTP)
- 🟡 Interpolated (aged tone reference)
- 🔴 Wall clock (unsynchronized)

**Quality Grade:**
- **A:** Excellent (±1ms, high SNR)
- **B:** Good (±1-5ms, moderate SNR)
- **C:** Fair (±5-10ms, low SNR or NTP)
- **D:** Poor (>10ms, unreliable)
- **F:** Failing (no valid reference)

**Age:**
- Time since time_snap was established or updated

**Drift Rate:**
- Rate of change in ms/hour
- Positive = time advancing faster than reference
- Negative = time lagging reference

**Jitter:**
- Peak-to-peak variation (stability)
- ±0.1ms = excellent
- ±1ms = good
- ±10ms = acceptable
- >10ms = concerning

**NTP Δ:**
- Difference between time_snap and NTP time
- Should be within expected propagation delay
- WWV Colorado to typical US station: 7-25ms

**Health Score (0-100):**
```
health = 100
health -= (age_minutes * 0.1)           # Freshness penalty
health -= (abs(drift_rate) * 10)        # Drift penalty
health -= (jitter * 5)                  # Stability penalty
health -= quality_penalty               # A=0, B=5, C=15, D=30, F=50
health += (snr_db * 0.5) if snr > 20    # Bonus for strong signal
health = max(0, min(100, health))
```

### Sort & Filter Options

**Sort by:**
- Health (default - worst first to highlight problems)
- Age (oldest first - needs attention)
- Drift rate (highest first - most concerning)
- SNR (lowest first - weak signals)

**Filter:**
- Show only problems (health < 70)
- Show only tone-locked
- Show only NTP/wall clock
- Show specific band (HF/MF)

### Row Expansion

Click row to expand with additional details:
- RTP anchor timestamp
- UTC anchor time
- Last tone detection time
- Tone detection history (last 10)
- Drift chart (last hour)
- Transition history

---

## Section 8: Alerts & Recommendations

### Purpose
Proactive **warnings and suggested actions** based on timing metrics.

### Alert Types & Triggers

#### Critical Alerts (Red)

**1. No Tone Lock for Extended Period**
```
❌ CRITICAL: No channels tone-locked for 6+ hours
   ├─ Impact: All data using NTP (±10ms precision)
   ├─ Possible causes: 
   │  • Antenna/receiver issue
   │  • Software fault in tone detector
   │  • Extreme propagation conditions (unlikely all bands)
   └─ Action: Check receiver status, restart if needed
```

**2. Excessive Drift**
```
❌ CRITICAL: WWV 10 MHz drift >50ms detected
   ├─ Current drift: +127ms
   ├─ Drift rate: +45ms/hour (accelerating)
   ├─ Possible causes:
   │  • System clock issue
   │  • RTP timestamp corruption
   │  • Time_snap anchor incorrect
   └─ Action: Restart analytics service, verify system clock
```

**3. Wall Clock Fallback**
```
❌ CRITICAL: Using wall clock timing (±seconds precision)
   ├─ Channels affected: 3/9
   ├─ Duration: 2h 15m
   ├─ Possible causes:
   │  • NTP sync lost
   │  • No tone signals available
   └─ Action: Check NTP configuration, verify internet connectivity
```

#### Warning Alerts (Yellow)

**1. Degraded Tone Quality**
```
⚠️ WARNING: Tone SNR below 15dB on multiple channels
   ├─ Affected: WWV 15, 20, 25 MHz
   ├─ Current SNR: 8.2, 10.5, 12.1 dB
   ├─ Likely cause: Poor HF propagation conditions
   └─ Impact: May lose tone lock, fall back to NTP
```

**2. Frequent Transitions**
```
⚠️ WARNING: 15 time source transitions in last hour
   ├─ Channels: WWV 15 MHz, WWV 20 MHz
   ├─ Pattern: Tone ↔ NTP cycling
   ├─ Likely cause: Marginal propagation (SNR ~10-12dB)
   └─ Impact: Reduced timing stability, consider using lower bands
```

**3. NTP Offset Increase**
```
⚠️ WARNING: NTP offset increasing (now +18ms)
   ├─ Previous: +2ms (6 hours ago)
   ├─ Trend: Steady increase
   ├─ Possible cause: System clock drift, NTP server issues
   └─ Action: Monitor, may need to restart NTP daemon
```

#### Info Alerts (Blue)

**1. Propagation Change Detected**
```
ℹ️ INFO: Higher bands now usable (WWV 20, 25 MHz)
   ├─ Tone SNR improved: 5dB → 22dB
   ├─ Time: 09:45 UTC (sunrise propagation)
   └─ Benefit: More frequency diversity, better redundancy
```

**2. Tone Quality Excellent**
```
ℹ️ INFO: All channels tone-locked with excellent quality
   ├─ Average SNR: 32.1dB
   ├─ Average confidence: 0.96
   ├─ Drift: <0.5ms on all channels
   └─ Status: Optimal conditions for precise timing
```

---

## Implementation Plan

### Phase 1: Data Collection Enhancement (Backend)

#### 1.1 Timing Metrics CSV Writer

Create new CSV output: `timing_metrics_YYYYMMDD.csv`

**Fields:**
```csv
timestamp_utc,channel,source_type,quality,snr_db,confidence,age_seconds,
rtp_anchor,utc_anchor,drift_ms,jitter_ms,ntp_offset_ms,health_score
```

**Location:** `/tmp/grape-test/analytics/{CHANNEL}/timing/`

**Update frequency:** Every minute (at each time_snap check)

#### 1.2 Analytics Service Modifications

```python
# In analytics_service.py

class TimingMetricsWriter:
    """Write timing metrics for web-UI visualization"""
    
    def write_timing_snapshot(self, channel: str, time_snap: TimeSnapReference,
                              drift_ms: float, jitter_ms: float, 
                              ntp_offset_ms: float):
        """Write current timing metrics"""
        health = self._calculate_health_score(time_snap, drift_ms, jitter_ms)
        
        csv_record = {
            'timestamp_utc': datetime.now(timezone.utc).isoformat(),
            'channel': channel,
            'source_type': time_snap.source,
            'quality': self._classify_quality(time_snap, drift_ms),
            'snr_db': getattr(time_snap, 'detection_snr_db', None),
            'confidence': time_snap.confidence,
            'age_seconds': time.time() - time_snap.established_at,
            'rtp_anchor': time_snap.rtp_timestamp,
            'utc_anchor': time_snap.utc_timestamp,
            'drift_ms': drift_ms,
            'jitter_ms': jitter_ms,
            'ntp_offset_ms': ntp_offset_ms,
            'health_score': health
        }
        self._write_csv(csv_record)
    
    def _calculate_drift(self, time_snap: TimeSnapReference, 
                        current_rtp: int, current_utc: float) -> float:
        """Calculate drift from time_snap anchor"""
        expected_rtp = time_snap.rtp_timestamp + \
                      (current_utc - time_snap.utc_timestamp) * time_snap.sample_rate
        drift_samples = current_rtp - expected_rtp
        drift_ms = (drift_samples / time_snap.sample_rate) * 1000.0
        return drift_ms
```

#### 1.3 Transition Event Logging

Create new file: `timing_transitions_YYYYMMDD.json`

**Format:**
```json
{
  "transitions": [
    {
      "timestamp": "2025-11-26T08:30:15Z",
      "channel": "WWV 10 MHz",
      "from_source": "wwv_startup",
      "to_source": "ntp",
      "from_quality": "TONE_LOCKED",
      "to_quality": "NTP_SYNCED",
      "reason": "tone_snr_low",
      "last_snr_db": 8.2,
      "last_confidence": 0.78,
      "duration_on_previous_source_minutes": 510
    }
  ]
}
```

### Phase 2: API Endpoints (Web Server)

#### 2.1 Primary Reference

```javascript
// GET /api/v1/timing/primary-reference
app.get('/api/v1/timing/primary-reference', async (req, res) => {
  // Find best time_snap across all channels
  // Return detailed metrics
});
```

#### 2.2 System Health Summary

```javascript
// GET /api/v1/timing/health-summary
app.get('/api/v1/timing/health-summary', async (req, res) => {
  // Aggregate metrics across channels
  // Calculate tone-lock percentage, drift stats, etc.
});
```

#### 2.3 Timeline Data

```javascript
// GET /api/v1/timing/timeline?hours=24&channel=all
app.get('/api/v1/timing/timeline', async (req, res) => {
  // Load timing_metrics CSV for requested period
  // Group by time_snap source
  // Return timeline segments
});
```

#### 2.4 Drift Analysis

```javascript
// GET /api/v1/timing/drift?channel=WWV%2010%20MHz&hours=1
app.get('/api/v1/timing/drift', async (req, res) => {
  // Load timing metrics for channel
  // Calculate drift over time
  // Return time series data
});
```

#### 2.5 Transitions

```javascript
// GET /api/v1/timing/transitions?hours=24
app.get('/api/v1/timing/transitions', async (req, res) => {
  // Load transitions log
  // Filter by time period
  // Return event list
});
```

### Phase 3: UI Components (Frontend)

#### 3.1 Update timing-dashboard.html

**Add new sections:**
1. Primary reference hero section
2. Health summary cards
3. Timeline chart (using Plotly.js)
4. Drift analysis chart
5. Transitions event log
6. Enhanced channel table

#### 3.2 Chart Libraries

Use **Plotly.js** (already included) for:
- Timeline chart (Gantt-style)
- Drift time series
- Multi-channel comparison
- Confidence bands

#### 3.3 Auto-refresh

**Update intervals:**
- Primary reference: 10 seconds
- Health summary: 30 seconds
- Charts: 60 seconds
- Transitions: 60 seconds

### Phase 4: Alert System

#### 4.1 Alert Detection Logic

```javascript
// In monitoring-server-v3.js
function analyzeTimingHealth(timingData) {
  const alerts = [];
  
  // Check for no tone lock
  const toneLocked = Object.values(timingData.channels)
    .filter(ch => ch.quality === 'TONE_LOCKED').length;
  if (toneLocked === 0 && /* > 6 hours */) {
    alerts.push({
      severity: 'critical',
      type: 'no_tone_lock',
      message: 'No channels tone-locked for extended period'
    });
  }
  
  // Check for excessive drift
  for (const [channel, data] of Object.entries(timingData.channels)) {
    if (Math.abs(data.drift_ms) > 50) {
      alerts.push({
        severity: 'critical',
        type: 'excessive_drift',
        channel: channel,
        drift_ms: data.drift_ms
      });
    }
  }
  
  // ... more checks
  
  return alerts;
}
```

#### 4.2 Alert Display

**Banner at top of page:**
```html
<div class="alert-banner critical" v-if="criticalAlerts.length > 0">
  ❌ {{ criticalAlerts.length }} Critical Timing Issues Detected
  <button @click="showAlertDetails">View Details</button>
</div>
```

---

## Visual Design Mockup

### Color Scheme

**Quality Levels:**
- 🟢 **TONE_LOCKED:** `#10b981` (green)
- 🔵 **NTP_SYNCED:** `#3b82f6` (blue)
- 🟡 **INTERPOLATED:** `#f59e0b` (amber)
- 🔴 **WALL_CLOCK:** `#ef4444` (red)

**Metrics:**
- **Excellent:** `#10b981` (green)
- **Good:** `#3b82f6` (blue)
- **Fair:** `#f59e0b` (amber)
- **Poor:** `#ef4444` (red)
- **Critical:** `#dc2626` (deep red)

**Background:**
- Primary: `#0a0e27` (dark blue-gray)
- Cards: `#1e293b` (slate)
- Borders: `#334155` (light slate)

---

## Example: Full Page HTML Snippet

```html
<!-- Primary Reference Hero -->
<div class="primary-reference-card">
  <div class="card-header">
    <h2>🎯 Primary Time Reference</h2>
    <div class="quality-badge tone-locked">TONE_LOCKED</div>
  </div>
  
  <div class="metrics-grid-3col">
    <div class="metric">
      <div class="metric-label">Source</div>
      <div class="metric-value">WWV 10 MHz - 1000 Hz Tone</div>
      <div class="metric-sub">Station: WWV (Fort Collins, CO)</div>
    </div>
    
    <div class="metric">
      <div class="metric-label">Precision</div>
      <div class="metric-value">±1 ms</div>
      <div class="metric-sub">SNR: 28.4 dB | Conf: 0.95</div>
    </div>
    
    <div class="metric">
      <div class="metric-label">Age</div>
      <div class="metric-value">47 seconds</div>
      <div class="metric-sub">Next check: 4m 13s</div>
    </div>
  </div>
  
  <div class="time-details">
    <div class="time-detail-item">
      <span class="label">RTP Anchor:</span>
      <span class="value monospace">1,558,805,472</span>
    </div>
    <div class="time-detail-item">
      <span class="label">UTC Anchor:</span>
      <span class="value monospace">2025-11-26T03:45:00.000Z</span>
    </div>
  </div>
</div>

<!-- System Health Cards -->
<div class="health-cards-grid">
  <div class="health-card">
    <div class="health-card-title">Tone-Locked Channels</div>
    <div class="health-card-value">6/9</div>
    <div class="health-card-percent">67%</div>
    <div class="health-card-list">
      <div class="channel-item tone-locked">🟢 WWV 5 MHz</div>
      <div class="channel-item tone-locked">🟢 WWV 10 MHz</div>
      <div class="channel-item tone-locked">🟢 WWV 15 MHz</div>
      <div class="channel-item ntp-synced">🔵 WWV 20 MHz</div>
      <div class="channel-item ntp-synced">🔵 WWV 25 MHz</div>
    </div>
  </div>
  
  <div class="health-card">
    <div class="health-card-title">RTP Drift (5-min avg)</div>
    <div class="health-card-value">±0.3 ms</div>
    <div class="health-card-quality excellent">Excellent</div>
    <div class="health-card-details">
      <div>Range: 0.1 - 0.5 ms</div>
      <div>Jitter: ±0.08 ms</div>
      <div>Trend: Stable ➡️</div>
    </div>
  </div>
  
  <!-- More cards... -->
</div>

<!-- Timeline Chart -->
<div class="chart-container">
  <h3>🕐 Time Source Timeline (24 Hours)</h3>
  <div id="timeline-chart"></div>
</div>

<!-- Drift Analysis Chart -->
<div class="chart-container">
  <h3>📈 RTP Drift Analysis</h3>
  <div id="drift-chart"></div>
</div>
```

---

## Recommendations Summary

### For Non-Expert Users

**Display:**
1. **Traffic light system** - Green/yellow/red for overall health
2. **Simple status:** "Timing: Excellent" vs "Timing: Needs Attention"
3. **One-line summary:** "6/9 channels tone-locked, ±0.3ms drift"

### For Expert Users

**Display:**
1. **Full metrics table** with all numeric values
2. **Drill-down charts** for detailed analysis
3. **Raw data export** for offline analysis
4. **Correlation tools** with propagation indices

### Progressive Disclosure

**Level 1 (At-a-glance):**
- Overall health score
- Color-coded status
- Critical alerts only

**Level 2 (Summary):**
- Per-channel status
- Key metrics (drift, jitter, transitions)
- Warning alerts

**Level 3 (Detailed):**
- Full metrics table
- Historical charts
- Event logs
- Recommendations

---

## Success Metrics

### System is Working Well When:

1. **Tone-lock >80%** of channels most of the time
2. **RTP drift <1ms** on tone-locked channels
3. **Transitions <5/hour** per channel (stable)
4. **NTP agreement ±5ms** (validates tone accuracy)
5. **No critical alerts** for extended periods

### User Can Answer:

- "Is my timing good enough for my analysis?"
- "Why did timing quality drop at 14:00?"
- "Which channel has the most stable timing?"
- "When should I consider reprocessing data?"
- "Is my NTP configuration correct?"

---

This comprehensive timing analysis UI would provide full visibility into the sophisticated timing system you've built, making it easy to monitor, diagnose, and validate timing quality for scientific analysis.

Would you like me to start implementing any of these sections?
