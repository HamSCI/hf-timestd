# Web UI Information Architecture - Per-Channel Metrics

**Purpose:** Define per-channel data characterization for signal quality, recording status, and analytics processing.

**Last Updated:** 2024-11-10  
**Status:** Specification Draft

---

## Overview

Each channel (WWV/CHU frequency) requires independent monitoring to assess:
1. Recording quality and completeness
2. Tone detection performance
3. Analytics processing status
4. Time reference accuracy

The dashboard displays 9 channels simultaneously:
- **WWV:** 2.5, 5, 10, 15, 20, 25 MHz
- **CHU:** 3.33, 7.85, 14.67 MHz

---

## 1. Core Recording Metrics

### 1.1 Recording Status

**Primary Indicator:**
- **Status:** `recording` | `idle` | `error` | `unknown`
- **Visual:** Status badge with color coding
  - 🔴 `recording` = Green badge with pulsing dot
  - ⏸️ `idle` = Yellow badge (no packets recently)
  - ❌ `error` = Red badge (gaps/errors detected)

**Data Source:** `core-recorder-status.json` → `channels[ssrc].status`

**Display Format:**
```
WWV 5 MHz: 🔴 RECORDING
CHU 7.85 MHz: ⏸️ IDLE (no signal)
```

### 1.2 Sample Completeness

**Definition:** Percentage of expected samples received without gaps

**Calculation:**
```python
total_samples = packets_received * 320  # 320 IQ samples per packet
gap_samples = total_gap_samples
completeness_pct = ((total_samples - gap_samples) / total_samples) * 100
```

**Display Requirements:**
- **Value:** Percentage with 2 decimal places
- **Color Coding:**
  - Green: ≥99.9% (excellent)
  - Yellow: 99.0-99.9% (good)
  - Orange: 95.0-99.0% (degraded)
  - Red: <95.0% (poor)

**Example:**
```
Completeness: 99.97% ✅
Completeness: 98.45% ⚠️
```

### 1.3 Packet Loss

**Definition:** Percentage of samples lost due to gaps

**Calculation:**
```python
packet_loss_pct = (total_gap_samples / total_samples) * 100
```

**Display Requirements:**
- **Value:** Percentage with 4 decimal places (precision matters)
- **Color Coding:**
  - Green: <0.01% (excellent)
  - Yellow: 0.01-0.1% (acceptable)
  - Red: >0.1% (investigate)

**Example:**
```
Packet Loss: 0.0032% ✅
Packet Loss: 0.1456% ⚠️
```

### 1.4 Gap Detection

**Metrics:**
- **Gaps Detected:** Total count of discontinuities
- **Total Gap Samples:** Cumulative samples lost
- **Gap Duration:** Milliseconds (samples / sample_rate * 1000)

**Display Requirements:**
```
Gaps: 3 events
Total Duration: 60ms (960 samples)
```

**Gap Severity:**
- **Minor:** <100ms (likely single packet loss)
- **Moderate:** 100ms-1s (multiple packet loss)
- **Major:** >1s (network/radiod issue)

### 1.5 RTP Packet Statistics

**Metrics:**
- **Packets Received:** Total count since service start
- **Expected Rate:** ~18.75 packets/second (16kHz / 320 samples)
- **Last Packet Time:** Timestamp of most recent packet
- **Packet Age:** Time since last packet

**Display Requirements:**
```
Packets Received: 282,974
Last Packet: 3s ago
Rate: 18.7 packets/sec ✅
```

**Alert Conditions:**
- 🔴 **Critical:** No packet in >60s (channel dead)
- 🟡 **Warning:** No packet in 10-60s (possible issue)
- 🟢 **Normal:** Packet <10s ago

### 1.6 NPZ Archive Files

**Metrics:**
- **Files Written:** Total NPZ files for this channel
- **Last File:** Path to most recent archive
- **Expected Count:** Should match uptime in minutes

**Display Requirements:**
```
NPZ Files: 95 files
Last: 20251111T022500Z_5000000_iq.npz
Expected: ~95 (uptime: 95 minutes)
```

---

## 2. Analytics Processing Metrics

### 2.1 Processing Status

**Metrics:**
- **NPZ Processed:** Count of files analyzed
- **Processing Lag:** Difference between written and processed
- **Last Processed File:** Filename
- **Last Processed Time:** Timestamp

**Display Requirements:**
```
NPZ Processed: 57 / 95 files
Lag: 38 files (~38 minutes behind)
Last: 20251111T021500Z_5000000_iq.npz
```

**Alert Conditions:**
- 🔴 **Critical:** Lag >60 files (analytics failing)
- 🟡 **Warning:** Lag >10 files (slow processing)
- 🟢 **Normal:** Lag <5 files

### 2.2 Digital RF Output

**Metrics:**
- **Samples Written:** Total 10 Hz IQ samples
- **Files Written:** Count of Digital RF HDF5 files
- **Data Volume:** GB written
- **Last Write Time:** Timestamp of most recent write

**Display Requirements:**
```
Digital RF Samples: 34.2M samples (57 minutes at 10 Hz)
Files: 57 HDF5 files
Volume: 2.1 GB
Last Write: 45s ago
```

**Quality Indicator:**
- Expected: 600 samples/minute (10 Hz × 60s)
- Actual vs Expected ratio shows decimation accuracy

### 2.3 Quality Metrics

**From Analytics Processing:**
- **Last Completeness:** From most recent NPZ processed
- **Avg Packet Loss:** Rolling average
- **Quality Grade:** A/B/C/D/F (if using grading system)

**Display Requirements:**
```
Quality (Last Minute):
  Completeness: 99.98%
  Packet Loss: 0.0023%
  Grade: A ✅
```

---

## 3. Tone Detection Metrics

### 3.1 Station Detection Counts

**Metrics per Station:**
- **WWV Detections:** Count of 1000 Hz, 0.8s tone detections
- **WWVH Detections:** Count of 1200 Hz, 0.8s tone detections  
- **CHU Detections:** Count of 1000 Hz, 0.5s tone detections
- **Total Detections:** Sum across all stations

**Data Source:** `analytics-service-status.json` → `tone_detections`

**Display Requirements:**
```
Tone Detections (Session):
  WWV: 12 (1000 Hz, timing ref)
  WWVH: 8 (1200 Hz, propagation)
  CHU: 0 (1000 Hz, timing ref)
  Total: 20 detections
```

**Per-Channel Expectations:**
- **WWV channels:** Detect both WWV (1000 Hz) and WWVH (1200 Hz)
- **CHU channels:** Detect only CHU (1000 Hz, 0.5s)

### 3.2 Detection Rate

**Calculation:**
```python
detection_rate = (detections / minutes_processed) * 100
```

**Display Requirements:**
```
Detection Rate: 21.1% (12 of 57 minutes)
```

**Interpretation:**
- **High rate (>50%):** Strong signals, good propagation
- **Low rate (10-30%):** Weak signals, normal ionospheric variation
- **No detections (0%):** Poor propagation OR signal processing issue

**Important:** Low detection rates are NORMAL and EXPECTED - this is studying ionospheric propagation!

### 3.3 Last Detection Time

**Metric:**
- **Timestamp:** When last tone was detected
- **Age:** Time since last detection

**Display Requirements:**
```
Last Detection: 2024-11-10 20:23:00 UTC (3 minutes ago)
Station: WWV
```

### 3.4 WWV/WWVH Discrimination (WWV Channels Only)

**Purpose:** Propagation study - different path lengths

**Metrics:**
- **WWV Count:** 1000 Hz detections (Fort Collins, CO)
- **WWVH Count:** 1200 Hz detections (Kauai, HI)
- **Ratio:** WWV / (WWV + WWVH) × 100%
- **Differential Delay:** Time difference between arrivals (future)

**Display Requirements:**
```
WWV/WWVH Discrimination:
  WWV: 12 (Fort Collins) - 60%
  WWVH: 8 (Hawaii) - 40%
  Path difference favors WWV (closer station)
```

**Scientific Value:**
- Ratio varies by time of day, frequency, propagation conditions
- Differential delay indicates ionospheric path difference
- **Critical:** WWVH is NEVER used for time_snap (only WWV/CHU)

---

## 4. Time Reference Status

### 4.1 Time Snap Established

**Per-Channel Indicator:**
- **Established:** Boolean (this channel has time_snap)
- **Source:** Which station provided anchor (WWV/CHU)
- **Confidence:** 0.0-1.0 quality metric
- **Age:** Minutes since time_snap established

**Data Source:** `analytics-service-status.json` → `time_snap`

**Display Requirements:**
```
Time Snap: ✅ ESTABLISHED
Source: WWV (verified)
Confidence: 0.95
Age: 12 minutes
```

**Status Levels:**
- 🟢 **Established:** WWV/CHU verified time anchor active
- 🟡 **Initial:** Using initial RTP timestamp (not verified)
- 🔴 **Not Established:** No time reference available

### 4.2 Global Time Snap Summary

**Aggregate Across All Channels:**
- **Channels with Time Snap:** Count
- **Best Channel:** Highest confidence
- **Newest Time Snap:** Most recently established

**Display Requirements:**
```
Global Time Reference:
  Active Channels: 6 of 9 with time_snap
  Best: WWV 10 MHz (confidence 0.97, 8 min old)
  Newest: WWV 15 MHz (established 2 min ago)
```

---

## 5. Channel Comparison Table

### 5.1 Compact Table View

**Columns (Priority Order):**
1. **Channel Name:** Frequency identification
2. **Status:** Recording state badge
3. **Completeness %:** Sample quality
4. **Loss %:** Packet loss rate
5. **Gaps:** Count of discontinuities
6. **NPZ Files:** Archives written
7. **Packets Rx:** Total packets received
8. **Last Packet:** Time since last packet
9. **NPZ Processed:** Analytics progress (optional)
10. **WWV Detections:** Tone count (optional)
11. **DRF Samples:** Digital RF written (optional)

**Example Layout:**
```
Channel        | Status      | Complete% | Loss%    | Gaps | NPZ | Packets  | Last    | Processed | WWV | DRF
---------------|-------------|-----------|----------|------|-----|----------|---------|-----------|-----|-----
WWV 2.5 MHz    | 🔴 REC      | 100.00%   | 0.0000%  | 0    | 95  | 282,974  | 3s ago  | 57        | 12  | 34M
WWV 5 MHz      | 🔴 REC      | 99.97%    | 0.0032%  | 3    | 95  | 282,970  | 3s ago  | 57        | 15  | 34M
CHU 3.33 MHz   | 🔴 REC      | 100.00%   | 0.0000%  | 0    | 95  | 282,980  | 3s ago  | 57        | 0   | 34M
```

### 5.2 Sorting Options

**User-Selectable Sort:**
- By Channel Name (default)
- By Completeness (worst first)
- By Packet Loss (highest first)
- By Last Packet (oldest first)
- By Detection Count (most active first)

### 5.3 Filtering Options

**Show/Hide:**
- All channels (default)
- WWV channels only
- CHU channels only
- Channels with errors only
- Channels with active time_snap only

---

## 6. Channel Detail View (Future)

### 6.1 Expanded Metrics

When user clicks a channel, show:
- **Minute-by-minute history:** Last 60 minutes
- **Gap timeline:** Visualize discontinuities
- **Detection timeline:** When tones were detected
- **Quality trends:** Completeness over time

### 6.2 RTP Packet Details

- **SSRC:** RTP stream identifier (hex)
- **Sequence Numbers:** Current sequence tracking
- **Timestamp Drift:** RTP vs wall clock drift (PPM)
- **Resequenced Packets:** Out-of-order count

### 6.3 File Listings

- **Recent NPZ Files:** Last 10 archives
- **Recent Digital RF Files:** Last 10 outputs
- **File Sizes:** Disk usage per channel

---

## 7. Time Windows

### 7.1 Real-Time (Current)

**Data:** Live status from JSON files  
**Refresh:** Every 10-60 seconds  
**Purpose:** Operational monitoring

### 7.2 Hourly Summary (Future)

**Data:** Last 60 minutes aggregated  
**Refresh:** Every minute  
**Metrics:**
- Average completeness
- Total detections
- Gap frequency
- Quality grade distribution

### 7.3 Daily Summary (Future)

**Data:** Last 24 hours aggregated  
**Refresh:** Every hour  
**Metrics:**
- Daily completeness percentage
- Total data volume (GB)
- Detection rate by hour
- Best/worst hours

---

## 8. Alert Thresholds

### 8.1 Per-Channel Alerts

**Completeness:**
- 🔴 <95%: CRITICAL - Significant data loss
- 🟡 95-99%: WARNING - Degraded quality
- 🟢 ≥99%: NORMAL

**Packet Loss:**
- 🔴 >1%: CRITICAL - Major network issue
- 🟡 0.1-1%: WARNING - Investigate
- 🟢 <0.1%: NORMAL

**Last Packet Age:**
- 🔴 >60s: CRITICAL - Channel dead
- 🟡 10-60s: WARNING - Possible stall
- 🟢 <10s: NORMAL

**Processing Lag:**
- 🔴 >60 files: CRITICAL - Analytics failing
- 🟡 >10 files: WARNING - Slow processing
- 🟢 <10 files: NORMAL

---

## 9. Visual Design

### 9.1 Color Scheme

**Status Colors:**
- Green `#10b981`: Healthy, recording, normal
- Yellow `#f59e0b`: Warning, degraded, investigate
- Red `#ef4444`: Critical, error, action needed
- Blue `#3b82f6`: Info, neutral, reference
- Purple `#8b5cf6`: Special (CHU detections)

### 9.2 Number Formatting

**Completeness:** `99.97%` (2 decimals)  
**Packet Loss:** `0.0032%` (4 decimals for precision)  
**Large Numbers:** `282,974` (thousands separator)  
**Data Volume:** `34.2M` or `2.1 GB` (human-readable)  
**Time Ago:** `3s ago`, `12m ago`, `2h ago`

### 9.3 Compact vs Detailed

**Compact Mode (Default):**
- Table with essential metrics
- Color-coded status badges
- Sortable columns

**Detailed Mode (Click to expand):**
- Full channel information
- Minute-by-minute history
- Detection timeline
- Quality trends

---

## API Endpoints Required

### `/api/v1/channels`
**Returns:** All channel metrics
```json
{
  "channels": {
    "WWV 5 MHz": {
      "recording": { /* core recorder metrics */ },
      "analytics": { /* analytics service metrics */ },
      "tone_detection": { /* detection counts */ }
    }
  }
}
```

### `/api/v1/channels/{channel_name}`
**Returns:** Detailed single channel
```json
{
  "channel_name": "WWV 5 MHz",
  "recording": { /* detailed metrics */ },
  "analytics": { /* processing status */ },
  "history": { /* last 60 minutes */ }
}
```

---

**Related Documents:**
- `WEB_UI_SYSTEM_MONITORING.md` - System-level health metrics
- `WEB_UI_SCIENTIFIC_QUALITY.md` - Data quality & provenance
- `WEB_UI_NAVIGATION_UX.md` - User experience & layout
