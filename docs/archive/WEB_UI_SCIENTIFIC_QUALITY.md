# Web UI Information Architecture - Scientific Data Quality

**Purpose:** Define data quality reporting, provenance tracking, and scientific metadata presentation for ionospheric propagation research.

**Last Updated:** 2024-11-10  
**Status:** Specification Draft

---

## Overview

Scientific integrity requires transparent reporting of:
1. **Completeness:** What percentage of expected data was captured?
2. **Provenance:** How was timing established and maintained?
3. **Discontinuities:** Where are the gaps and what caused them?
4. **Propagation Data:** WWV/WWVH discrimination for ionospheric studies

This document defines how quality information is presented to enable:
- **Trust:** Researchers can assess data reliability
- **Reproducibility:** Complete metadata for analysis replication
- **Discovery:** Identify interesting propagation events

---

## 1. Data Completeness Reporting

### 1.1 Quantitative Completeness

**Primary Metric:**
```
Completeness % = (samples_captured / samples_expected) × 100
```

**Display Requirements:**
- **Value:** Percentage with 2-3 decimal precision
- **Sample Count:** Actual vs expected sample counts
- **Time Period:** Duration of measurement

**Example:**
```
Data Completeness: 99.973%
  Captured: 57,598,080 samples
  Expected: 57,600,000 samples
  Missing: 1,920 samples (120ms)
  Period: 60 minutes (2024-11-10 19:00-20:00 UTC)
```

### 1.2 Completeness Categories

**NO Subjective Quality Grades** (A/B/C/D/F are deprecated)

Instead, report **quantitative gap breakdown:**

```
Gap Analysis:
  Network Gaps: 120ms (packet loss, overflow)
  Source Failures: 0ms (radiod down/channel missing)
  Recorder Offline: 0ms (daemon stopped)
  
  Total Gap Duration: 120ms
  Completeness: 99.973%
```

### 1.3 Time-Series Completeness

**Hourly Summary:**
```
Hour    | Completeness | Gap Duration | Gap Count
--------|--------------|--------------|----------
19:00   | 99.98%       | 12ms         | 2
20:00   | 99.95%       | 30ms         | 5
21:00   | 100.00%      | 0ms          | 0
```

**Daily Summary:**
```
Date       | Completeness | Total Gaps | Data Volume
-----------|--------------|------------|------------
2024-11-10 | 99.92%       | 4.8s       | 25.6 GB
2024-11-09 | 99.87%       | 7.2s       | 25.5 GB
2024-11-08 | 100.00%      | 0s         | 25.6 GB
```

---

## 2. Discontinuity Tracking

### 2.1 Discontinuity Types

**Categories (from `DiscontinuityType` enum):**
- `GAP`: Packet loss (missing RTP timestamps)
- `RTP_RESET`: RTP timestamp discontinuity (radiod restart)
- `SYNC_ADJUST`: Time snap correction applied
- `SOURCE_UNAVAILABLE`: Radiod down or channel missing
- `RECORDER_OFFLINE`: Daemon stopped
- `OVERFLOW`: Receiver buffer overflow
- `UNDERFLOW`: Receiver buffer underflow

### 2.2 Discontinuity Log Display

**Table Format:**
```
Timestamp (UTC)      | Type            | Magnitude | Samples  | Explanation
---------------------|-----------------|-----------|----------|---------------------------
2024-11-10 19:15:23  | GAP             | +20ms     | 320      | Packet loss (seq 12345 missing)
2024-11-10 19:42:18  | GAP             | +60ms     | 960      | Network congestion (3 packets)
2024-11-10 20:05:00  | SYNC_ADJUST     | -15ms     | -240     | WWV time_snap correction
```

**Fields:**
- **Timestamp:** When discontinuity occurred (UTC)
- **Type:** Classification from enum
- **Magnitude:** Duration in milliseconds (+ for gap, - for overlap)
- **Samples:** Sample count (magnitude × sample_rate / 1000)
- **Explanation:** Human-readable cause

### 2.3 Discontinuity Visualization

**Timeline View (Future):**
```
19:00 |████████████████████████████████████████| 100%
19:15 |██████████▓███████████████████████████| 99.8% (1 gap)
19:30 |████████████████████████████████████████| 100%
19:45 |█████████████████▓▓████████████████████| 99.7% (2 gaps)
20:00 |████████████████████████████████████████| 100%
```

Legend:
- `█` Complete data
- `▓` Gap/discontinuity
- Hover for details

### 2.4 Gap Severity Classification

**Severity Levels:**
- **Minor:** <100ms (likely single packet loss)
  - Impact: Minimal, normal network variation
  - Action: None required
  
- **Moderate:** 100ms-1s (multiple packet loss)
  - Impact: Noticeable, may affect some analyses
  - Action: Monitor for patterns
  
- **Major:** 1s-10s (network/source issue)
  - Impact: Significant data loss
  - Action: Investigate network/radiod
  
- **Critical:** >10s (service interruption)
  - Impact: Data unusable for affected period
  - Action: Review system logs, check services

---

## 3. Timing Provenance

### 3.1 Time Reference Chain

**Display Timing Ancestry:**
```
Timing Provenance:
  Primary Reference: RTP Timestamps (ka9q-radio)
  Time Anchor: WWV 10 MHz @ 2024-11-10 19:00:00.000 UTC
    - Detection: 1000 Hz tone, 0.8s duration
    - Confidence: 0.95 (strong signal, 58 dB SNR)
    - Age: 95 minutes
  
  Sample Time Calculation:
    utc = time_snap_utc + (rtp_ts - time_snap_rtp) / sample_rate
    
  Accuracy: ±15ms (time_snap precision + RTP drift)
```

### 3.2 Time Snap Status

**Per-Channel Time Reference:**
```
Channel: WWV 10 MHz
Time Snap: ✅ ESTABLISHED
  Source: WWV (Fort Collins, CO)
  Station: 1000 Hz tone detection
  Established: 2024-11-10 19:00:00.000 UTC
  Method: Phase-invariant matched filtering
  Confidence: 0.95
  Age: 95 minutes
  Used for Timing: YES ✅
```

**WWVH Handling (Critical):**
```
Channel: WWV 10 MHz
WWVH Detection: 8 events (1200 Hz tone)
  Purpose: PROPAGATION STUDY ONLY
  Used for Timing: NO ❌
  Reason: 2500 miles farther than WWV
  Scientific Use: WWV-WWVH differential delay analysis
```

### 3.3 Timing Corrections Applied

**Log of Time Snap Updates:**
```
Time Snap History:
  2024-11-10 19:00:00 | INITIAL    | RTP timestamp (no verification)
  2024-11-10 19:00:00 | WWV_VERIFY | +0ms adjustment (first detection)
  2024-11-10 19:01:00 | WWV_VERIFY | -15ms adjustment (drift correction)
  2024-11-10 19:15:00 | CHU_VERIFY | +8ms adjustment (independent verify)
```

**Discontinuity Link:**
- Each time_snap correction creates a `SYNC_ADJUST` discontinuity
- Magnitude and explanation logged
- Traceability for data users

### 3.4 RTP Timestamp Drift

**Drift Monitoring:**
```
RTP Drift Analysis:
  Wall Clock vs RTP: +2.3 PPM (parts per million)
  Drift Rate: 0.138ms/minute
  Cumulative Drift (95 min): 13.1ms
  
  Status: NORMAL (< 10 PPM acceptable)
```

**Purpose:** Detect oscillator drift or timing issues

---

## 4. WWV/WWVH Propagation Analysis

### 4.1 Multi-Station Discrimination

**Purpose:** Study ionospheric propagation via station identification

**Display:**
```
WWV vs WWVH Detection Analysis:
  
  WWV (Fort Collins, CO - 1000 Hz):
    Total Detections: 42
    Detection Rate: 44.2% (42 of 95 minutes)
    Average SNR: 58.3 dB
    Used for time_snap: YES ✅
  
  WWVH (Kauai, HI - 1200 Hz):
    Total Detections: 23
    Detection Rate: 24.2% (23 of 95 minutes)
    Average SNR: 51.7 dB
    Used for time_snap: NO ❌
  
  Propagation Ratio: 64.6% WWV, 35.4% WWVH
  Interpretation: Closer station (WWV) stronger signal path
```

### 4.2 Differential Delay (Future)

**Scientific Goal:** Measure path difference via arrival time

**Display:**
```
WWV-WWVH Differential Delay:
  Frequency: 10 MHz
  Date: 2024-11-10
  
  Timestamp           | WWV Delay | WWVH Delay | Differential
  --------------------|-----------|------------|-------------
  19:00:00            | 0ms       | +85ms      | +85ms
  19:01:00            | 0ms       | +82ms      | +82ms
  19:02:00            | 0ms       | +88ms      | +88ms
  
  Average Differential: +85ms
  Std Deviation: ±2.5ms
  
  Interpretation: WWVH signal arrives 85ms later
  Distance Difference: ~25,500 km (ionospheric path)
```

### 4.3 CHU Detection

**Canadian Time Standard:**
```
CHU (Ottawa, Canada - 1000 Hz, 0.5s tone):
  Frequency: 7.85 MHz
  Total Detections: 12
  Detection Rate: 12.6%
  Used for time_snap: YES ✅
  
  Note: Shorter tone duration (0.5s vs 0.8s)
  Independent timing verification source
```

### 4.4 Propagation Quality Indicators

**Signal Strength Correlation:**
```
Detection Success vs Signal Strength:
  Strong Signals (>55 dB SNR): 98% detection rate
  Medium Signals (45-55 dB SNR): 67% detection rate
  Weak Signals (<45 dB SNR): 12% detection rate
  
  Low detection rates are NORMAL and EXPECTED
  Purpose: Study ionospheric propagation variability
```

---

## 5. Metadata for Scientific Use

### 5.1 Archive File Metadata

**NPZ File Contents:**
```
File: 20241110T190000Z_10000000_iq.npz
  
  Arrays:
    - iq_samples: complex64[960000] (60s × 16kHz)
    - rtp_timestamps: uint32[3000] (320 samples/packet)
  
  Metadata:
    - channel_name: "WWV 10 MHz"
    - frequency_hz: 10000000
    - sample_rate: 16000
    - start_time_utc: "2024-11-10T19:00:00.000Z"
    - time_snap_source: "wwv_verified"
    - time_snap_confidence: 0.95
    - completeness_pct: 99.98
    - gap_count: 1
    - gap_duration_ms: 12
    - discontinuities: [...]
```

### 5.2 Digital RF Metadata

**HDF5 Attributes:**
```
Digital RF File Metadata:
  
  RF Parameters:
    - frequency: 10000000 Hz
    - sample_rate: 10 Hz (decimated from 16 kHz)
    - sample_rate_numerator: 10
    - sample_rate_denominator: 1
  
  Timing:
    - epoch: Unix timestamp (samples/sample_rate)
    - time_snap_source: "wwv_verified"
    - time_snap_utc: 1699635600.000
    - time_snap_rtp: 1234567890
  
  Station (PSWS Format):
    - station_id: "AC0G"
    - instrument_id: "GRAPE_V2"
    - receiver: "ka9q-radio"
    - location: "DM79lv"
  
  Quality:
    - completeness_pct: 99.98
    - discontinuities: [JSON array]
```

### 5.3 Quality Summary Files

**Daily CSV Export:**
```csv
timestamp_utc,channel,completeness_pct,gap_duration_ms,wwv_detections,wwvh_detections,differential_delay_avg_ms
2024-11-10T19:00:00Z,WWV_10MHz,99.98,12,1,0,null
2024-11-10T19:01:00Z,WWV_10MHz,100.00,0,1,1,85
2024-11-10T19:02:00Z,WWV_10MHz,99.95,30,1,1,82
```

**Purpose:** Easy import to analysis tools (Python, R, MATLAB)

---

## 6. Data Provenance Chain

### 6.1 Processing Pipeline Audit

**Track Data Through Pipeline:**
```
Data Provenance Chain:
  
  1. RTP Reception:
     - Source: ka9q-radio @ 239.255.93.1
     - SSRC: 0x989680
     - Packets: 282,974 received, 0 lost
  
  2. Resequencing:
     - Buffer: 64 packets
     - Resequenced: 12 packets (out-of-order)
     - Gaps Filled: 1 gap (320 samples, zeros)
  
  3. Time Snap:
     - Initial: RTP timestamp reference
     - Verified: WWV 10 MHz @ 19:00:00.000 UTC
     - Corrections: 1 adjustment (-15ms)
  
  4. Archive:
     - Format: NPZ (compressed)
     - Files: 95 written
     - Integrity: MD5 checksums available
  
  5. Analytics:
     - Processed: 57 files
     - Tone Detection: 42 WWV, 23 WWVH
     - Quality Metrics: Generated
  
  6. Digital RF:
     - Decimation: 16 kHz → 10 Hz (scipy.signal.decimate)
     - Format: HDF5 (Digital RF v2.6)
     - Files: 57 written (PSWS compatible)
  
  7. Upload (Pending):
     - Destination: PSWS repository
     - Method: rsync/sftp
     - Status: Not yet implemented
```

### 6.2 Configuration Snapshot

**Embed Configuration:**
```
Recording Configuration:
  Mode: TEST (/tmp/grape-test)
  
  KA9Q Radio:
    - Multicast: 239.255.93.1
    - Status Port: 5006
  
  Channels: 9 configured
    - WWV: 2.5, 5, 10, 15, 20, 25 MHz
    - CHU: 3.33, 7.85, 14.67 MHz
  
  Processing:
    - Sample Rate: 16 kHz IQ
    - Archive Format: NPZ compressed
    - Digital RF Rate: 10 Hz IQ
    - Tone Detection: Enabled (all channels)
```

### 6.3 Version Information

**Software Versions:**
```
GRAPE Signal Recorder v2.0
  - Core Recorder: v2.0 (stable)
  - Analytics Service: v1.0 (active development)
  - Digital RF Library: v2.6.8
  - Python: 3.11.2
  - NumPy: 1.24.3
  - SciPy: 1.10.1
```

---

## 7. Scientific Data Access

### 7.1 Data Discovery

**Search Interface (Future):**
```
Find Data:
  Date Range: 2024-11-01 to 2024-11-10
  Channels: [WWV 10 MHz, WWV 15 MHz]
  Min Completeness: 99%
  Min WWV Detections: 30/hour
  
  Results: 127 hours matching criteria
  Download: NPZ archives, Digital RF, Quality CSV
```

### 7.2 Data Download Links

**Per-Minute Archives:**
```
Download Archive Files:
  2024-11-10T19:00 - WWV 10 MHz
    - NPZ: 20241110T190000Z_10000000_iq.npz (12.3 MB)
    - Digital RF: rf@1699635600.000.h5 (856 KB)
    - Quality: quality_summary.json
```

### 7.3 Bulk Export

**Generate Research Dataset:**
```
Export Dataset:
  Period: 2024-11-01 to 2024-11-10 (10 days)
  Channels: All WWV frequencies
  Format: Digital RF + Quality CSV
  
  Total Size: 125 GB
  Export Status: Preparing (5 of 1440 files)
```

---

## 8. Alert Conditions for Scientific Quality

### 8.1 Quality Alerts

**Data Quality Warnings:**
- 🔴 **Critical:** Completeness <95% (data may be unusable)
- 🟡 **Warning:** Completeness <99% (reduced quality)
- ℹ️ **Info:** Gap >1s (notable discontinuity)

### 8.2 Timing Alerts

**Time Reference Warnings:**
- 🔴 **Critical:** No time_snap established after 10 minutes
- 🟡 **Warning:** Time_snap age >60 minutes (seek new verification)
- ℹ️ **Info:** Time_snap correction applied (magnitude >10ms)

### 8.3 Propagation Alerts

**Scientific Interest:**
- ℹ️ **Interesting:** WWV/WWVH both detected same minute (compare paths)
- ℹ️ **Interesting:** Detection rate >80% (strong propagation day)
- ℹ️ **Interesting:** Detection rate <10% (poor propagation, D-layer absorption?)

---

## 9. API Endpoints Required

### `/api/v1/quality/summary`
**Returns:** Aggregate quality metrics
```json
{
  "period": "2024-11-10T19:00:00Z to 2024-11-10T20:00:00Z",
  "overall_completeness": 99.92,
  "gap_breakdown": {
    "network_gaps_ms": 48,
    "source_failures_ms": 0,
    "recorder_offline_ms": 0
  }
}
```

### `/api/v1/quality/discontinuities`
**Returns:** Detailed discontinuity log
```json
{
  "discontinuities": [
    {
      "timestamp": "2024-11-10T19:15:23Z",
      "type": "GAP",
      "magnitude_ms": 20,
      "samples": 320,
      "explanation": "Packet loss (RTP seq 12345 missing)"
    }
  ]
}
```

### `/api/v1/quality/propagation`
**Returns:** WWV/WWVH discrimination analysis
```json
{
  "wwv_detections": 42,
  "wwvh_detections": 23,
  "ratio_wwv_pct": 64.6,
  "differential_delay_avg_ms": 85
}
```

---

**Related Documents:**
- `WEB_UI_SYSTEM_MONITORING.md` - System health and operational metrics
- `WEB_UI_CHANNEL_METRICS.md` - Per-channel data characterization
- `WEB_UI_NAVIGATION_UX.md` - User experience and information hierarchy
