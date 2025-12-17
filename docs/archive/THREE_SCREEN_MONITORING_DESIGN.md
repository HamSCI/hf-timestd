# GRAPE Monitoring UI - 3-Screen Design
**Design Date:** 2024-11-15  
**Philosophy:** Clean, focused, scientific - show critical metrics, hide complexity

---

## Design Goals

- **Operational Awareness** - Is the system running? What's its health?
- **Timing Quality** - Most critical metric for scientific validity
- **Signal Analysis** - 10 Hz carrier monitoring for all channels
- **Propagation Science** - WWV/WWVH discrimination for ionospheric research

---

## Screen 1: Summary Dashboard

**Purpose:** At-a-glance system health and operational status  
**Update Frequency:** 5 seconds  
**URL:** `/` or `/summary`

### Layout
```
┌─────────────────────────────────────────────────────────────┐
│  GRAPE Signal Recorder - Summary                            │
│  Station: K1ABC (FN42) | Mode: TEST                        │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│ SYSTEM STATUS                                               │
├─────────────────────────────────────────────────────────────┤
│ Core Recorder:    ● RUNNING    Uptime: 3h 24m             │
│ Analytics:        ● RUNNING    9/9 channels active         │
│ Web Monitoring:   ● RUNNING    Port 3000                   │
│                                                             │
│ NTP Sync:         ✓ SYNCED     Offset: ±2.3ms             │
│ Disk Usage:       23.4 GB      /tmp/grape-test             │
│ Last Update:      2024-11-15 18:15:42 UTC                  │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│ CHANNEL STATUS (9 channels)                                 │
├──────────────┬─────────┬──────────────┬────────┬───────────┤
│ Channel      │ Status  │ Timing       │ Tones  │ DRF Files │
├──────────────┼─────────┼──────────────┼────────┼───────────┤
│ WWV 2.5 MHz  │ ● RECV  │ GPS_LOCKED   │ 3 WWV  │ 1,234     │
│ WWV 5 MHz    │ ● RECV  │ GPS_LOCKED   │ 18 WWV │ 1,235     │
│              │         │              │ 5 WWVH │           │
│ WWV 10 MHz   │ ● RECV  │ GPS_LOCKED   │ 24 WWV │ 1,236     │
│              │         │              │ 12 WWVH│           │
│ WWV 15 MHz   │ ● RECV  │ GPS_LOCKED   │ 15 WWV │ 1,232     │
│              │         │              │ 8 WWVH │           │
│ WWV 20 MHz   │ ● RECV  │ NTP_SYNCED   │ 2 WWV  │ 1,230     │
│ WWV 25 MHz   │ ● RECV  │ NTP_SYNCED   │ 0      │ 1,228     │
│ CHU 3.33 MHz │ ● RECV  │ GPS_LOCKED   │ 12 CHU │ 1,233     │
│ CHU 7.85 MHz │ ● RECV  │ GPS_LOCKED   │ 8 CHU  │ 1,234     │
│ CHU 14.67MHz │ ● RECV  │ NTP_SYNCED   │ 1 CHU  │ 1,229     │
└──────────────┴─────────┴──────────────┴────────┴───────────┘

┌─────────────────────────────────────────────────────────────┐
│ TIME REFERENCE STATUS                                       │
├─────────────────────────────────────────────────────────────┤
│ Last WWV Detection:   WWV 10 MHz @ 18:00:00.002 UTC       │
│ Last CHU Detection:   CHU 7.85 MHz @ 17:59:00.015 UTC     │
│ Time Snap Age:        15 minutes (GPS_LOCKED quality)      │
│                                                             │
│ 24hr Detection Rate:  WWV: 67% | CHU: 45%                  │
│ Expected Next Tone:   19:00:00 UTC (in 44m 18s)           │
└─────────────────────────────────────────────────────────────┘
```

### Data Requirements

**System Status:**
- Core recorder: `GET /api/v1/core/status`
  - Running (boolean)
  - Uptime
  - Channels active count
  - Packets received total

- Analytics service: `GET /api/v1/analytics/status`
  - Running (boolean)
  - Channels processing
  - DRF write status

- NTP status: `GET /api/v1/system/ntp`
  - Synchronized (boolean)
  - Offset (ms)
  - Stratum

- Disk usage: `GET /api/v1/system/disk`
  - Used bytes
  - Available bytes
  - Data root path

**Channel Status Table:**
- Per-channel status: `GET /api/v1/channels`
  ```json
  {
    "channels": [
      {
        "name": "WWV 10 MHz",
        "status": "receiving",
        "timing_quality": "GPS_LOCKED",
        "tone_detections_24h": {
          "wwv": 24,
          "wwvh": 12,
          "chu": 0
        },
        "drf_files_count": 1236,
        "last_update": 1700075742.5
      }
    ]
  }
  ```

**Time Reference Status:**
- Time snap info: `GET /api/v1/timing/time-snap`
  ```json
  {
    "current_time_snap": {
      "station": "WWV",
      "frequency_mhz": 10.0,
      "detection_time": "2024-11-15T18:00:00.002Z",
      "age_seconds": 900,
      "quality": "GPS_LOCKED"
    },
    "detection_stats_24h": {
      "wwv_rate": 0.67,
      "chu_rate": 0.45,
      "total_wwv": 48,
      "total_chu": 32
    },
    "next_expected": "2024-11-15T19:00:00Z"
  }
  ```

### UI Behavior

- **Auto-refresh:** Every 5 seconds
- **Status indicators:**
  - Green (●) = Active/Good
  - Yellow (◐) = Degraded/Warning
  - Red (○) = Stopped/Error
- **Time Basis:**
- TONE_LOCKED: WWV/CHU time_snap within last 5 minutes (green badge)
- NTP_SYNCED: System NTP synchronized (yellow badge)
- WALL_CLOCK: Unsynchronized fallback (red badge)
- **Click channel row:** Navigate to carrier analysis for that channel

---

## Screen 2: 10 Hz Carrier Analysis (9 Channels)

**Purpose:** Real-time carrier signal visualization and quality metrics  
**Update Frequency:** Manual refresh + auto-refresh options (10s/30s/60s)  
**URL:** `/carrier` or `/carrier?date=YYYYMMDD`

### Layout
```
┌─────────────────────────────────────────────────────────────┐
│  10 Hz Carrier Analysis                                     │
│  Date: 2024-11-15 [◄] [Today] [►]   Auto-refresh: [10s ▼] │
└─────────────────────────────────────────────────────────────┘

┌────────────────────────────────────┐ ┌───────────────────────┐
│ WWV 2.5 MHz                        │ │ WWV 5 MHz             │
│ [24-hour spectrogram image]        │ │ [24-hour spectrogram] │
│                                    │ │                       │
│ SNR: 42 dB | Gaps: 3 (0.2%)       │ │ SNR: 58 dB | Gaps: 1  │
│ Timing: GPS_LOCKED                 │ │ Timing: GPS_LOCKED    │
└────────────────────────────────────┘ └───────────────────────┘

┌────────────────────────────────────┐ ┌───────────────────────┐
│ WWV 10 MHz                         │ │ WWV 15 MHz            │
│ [24-hour spectrogram image]        │ │ [24-hour spectrogram] │
│ ...                                │ │ ...                   │
└────────────────────────────────────┘ └───────────────────────┘

┌────────────────────────────────────┐ ┌───────────────────────┐
│ WWV 20 MHz                         │ │ WWV 25 MHz            │
│ [24-hour spectrogram image]        │ │ [24-hour spectrogram] │
│ ...                                │ │ ...                   │
└────────────────────────────────────┘ └───────────────────────┘

┌────────────────────────────────────┐ ┌───────────────────────┐
│ CHU 3.33 MHz                       │ │ CHU 7.85 MHz          │
│ [24-hour spectrogram image]        │ │ [24-hour spectrogram] │
│ ...                                │ │ ...                   │
└────────────────────────────────────┘ └───────────────────────┘

┌────────────────────────────────────┐
│ CHU 14.67 MHz                      │
│ [24-hour spectrogram image]        │
│ ...                                │
└────────────────────────────────────┘
```

### Data Requirements

**Spectrogram Images:**
- Endpoint: `GET /api/v1/spectrograms/:date`
  ```json
  {
    "date": "20241115",
    "spectrograms": [
      {
        "channel": "WWV 10 MHz",
        "type": "carrier",
        "url": "/spectrograms/20241115/WWV_10_MHz_20241115_carrier_spectrogram.png",
        "generated_time": 1700075742.5,
        "hours_covered": 24
      }
    ]
  }
  ```

**Channel Metrics:**
- Endpoint: `GET /api/v1/channels/:channel/metrics?date=YYYYMMDD`
  ```json
  {
    "channel": "WWV 10 MHz",
    "date": "20241115",
    "metrics": {
      "mean_snr_db": 58.3,
      "median_snr_db": 59.1,
      "gap_count": 1,
      "gap_total_seconds": 120,
      "completeness_pct": 99.86,
      "timing_quality_distribution": {
        "GPS_LOCKED": 0.95,
        "NTP_SYNCED": 0.04,
        "INTERPOLATED": 0.01,
        "WALL_CLOCK": 0.0
      }
    }
  }
  ```

### Spectrogram Display

**Image Specifications:**
- Resolution: 1440 x 200 pixels (one pixel per minute, 24 hours)
- Y-axis: Frequency (carrier ±50 Hz)
- X-axis: Time (00:00 - 23:59 UTC)
- Color: Power spectral density (dBm)
- Annotations:
  - Gap markers (red vertical lines)
  - Timing quality bands (colored top border)

**Overlay Information:**
- Channel name
- Date
- SNR statistics
- Gap count/percentage
- Dominant timing quality
- Click: Open detailed view

### UI Behavior

- **Date navigation:** Previous/Next day buttons, date picker
- **Today button:** Jump to current UTC day
- **Auto-refresh:** Dropdown (Off, 10s, 30s, 60s) - regenerates spectrograms
- **Grid layout:** 3x3 responsive grid (2x5 on mobile)
- **Click spectrogram:** Open full-resolution in modal + detailed metrics

---

## Screen 3: WWV/WWVH Discrimination (4 Channels)

**Purpose:** Ionospheric propagation analysis via differential timing  
**Update Frequency:** Manual refresh (daily analysis)  
**URL:** `/discrimination` or `/discrimination?date=YYYYMMDD`

### Layout
```
┌─────────────────────────────────────────────────────────────┐
│  WWV/WWVH Discrimination Analysis                           │
│  Date: 2024-11-15 [◄] [Today] [►]                          │
│                                                             │
│  Channels: WWV 5, 10, 15, 20 MHz (only these detect WWVH) │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│ WWV 5 MHz - Dual Station Detection                         │
├─────────────────────────────────────────────────────────────┤
│ [Time series plot]                                          │
│ X-axis: Time (UTC), Y-axis: Detection confidence           │
│ • Blue markers: WWV detections                              │
│ • Red markers: WWVH detections                              │
│ • Gap regions: Gray shading                                 │
│                                                             │
│ Statistics (24h):                                           │
│   WWV detections:  18 (75% of minutes)                      │
│   WWVH detections: 5 (21% of minutes)                       │
│   Dual detections: 2 (8% - both in same minute)            │
│   Mean differential delay: +23.4 ms (WWVH later)           │
│   Std dev: ±5.2 ms                                          │
│                                                             │
│ Interpretation:                                             │
│   WWVH path ~2500 mi longer → ionospheric delay visible    │
│   Delay variation indicates propagation conditions          │
│                                                             │
│ [Download CSV] [View Raw Data]                             │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│ WWV 10 MHz - Dual Station Detection                        │
│ [Similar layout]                                            │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│ WWV 15 MHz - Dual Station Detection                        │
│ [Similar layout]                                            │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│ WWV 20 MHz - Dual Station Detection                        │
│ [Similar layout]                                            │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│ FREQUENCY COMPARISON                                        │
├─────────────────────────────────────────────────────────────┤
│ [Multi-line plot]                                           │
│ X-axis: Time (UTC)                                          │
│ Y-axis: Differential delay (ms)                             │
│ Lines: 5 MHz (blue), 10 MHz (green), 15 MHz (orange),      │
│        20 MHz (red)                                         │
│                                                             │
│ Shows frequency-dependent propagation effects               │
└─────────────────────────────────────────────────────────────┘
```

### Data Requirements

**Discrimination Data:**
- Endpoint: `GET /api/v1/discrimination/:channel?date=YYYYMMDD`
  ```json
  {
    "channel": "WWV 10 MHz",
    "date": "20241115",
    "data": [
      {
        "minute": "2024-11-15T00:00:00Z",
        "wwv_detected": true,
        "wwvh_detected": false,
        "wwv_confidence": 0.92,
        "wwvh_confidence": null,
        "differential_delay_ms": null,
        "timing_quality": "GPS_LOCKED"
      },
      {
        "minute": "2024-11-15T01:00:00Z",
        "wwv_detected": true,
        "wwvh_detected": true,
        "wwv_confidence": 0.89,
        "wwvh_confidence": 0.67,
        "differential_delay_ms": 23.4,
        "timing_quality": "GPS_LOCKED"
      }
    ],
    "statistics": {
      "wwv_detections": 18,
      "wwvh_detections": 5,
      "dual_detections": 2,
      "wwv_detection_rate": 0.75,
      "wwvh_detection_rate": 0.21,
      "mean_differential_delay_ms": 23.4,
      "stddev_differential_delay_ms": 5.2
    }
  }
  ```

**CSV Export:**
- Endpoint: `GET /api/v1/discrimination/:channel/export?date=YYYYMMDD`
- Returns: CSV file with all discrimination data

### Visualization Details

**Per-Channel Plot:**
- Scatter plot with time on X-axis
- WWV markers (blue circles)
- WWVH markers (red triangles)
- Gap regions (gray shading)
- Hover: Show exact detection time, confidence, delay

**Frequency Comparison Plot:**
- Line chart, 4 lines (one per frequency)
- Shows differential delay over time
- Reveals frequency-dependent propagation
- Useful for ionospheric research

### UI Behavior

- **Date navigation:** Previous/Next day
- **CSV download:** Export discrimination data for scientific analysis
- **Interactive plots:** Hover for details, zoom time ranges
- **Responsive:** Stack vertically on mobile

---

## Navigation & Layout

### Top Navigation Bar
```
┌─────────────────────────────────────────────────────────────┐
│ GRAPE Monitor   [Summary] [Carrier] [Discrimination]       │
│                                           Station: K1ABC    │
└─────────────────────────────────────────────────────────────┘
```

### Responsive Design
- Desktop: Full layout as shown
- Tablet: 2-column grid for carrier analysis
- Mobile: Single column, stacked vertically

---

## API Endpoints Summary

### System & Status
- `GET /api/v1/core/status` - Core recorder status
- `GET /api/v1/analytics/status` - Analytics service status
- `GET /api/v1/system/ntp` - NTP synchronization status
- `GET /api/v1/system/disk` - Disk usage statistics

### Channels
- `GET /api/v1/channels` - All channel statuses
- `GET /api/v1/channels/:channel/metrics?date=YYYYMMDD` - Channel metrics

### Timing
- `GET /api/v1/timing/time-snap` - Current time snap status
- `GET /api/v1/timing/detections?date=YYYYMMDD` - Detection history

### Spectrograms
- `GET /api/v1/spectrograms/:date` - Available spectrograms for date
- `GET /spectrograms/:date/:filename` - Spectrogram image file

### Discrimination
- `GET /api/v1/discrimination/:channel?date=YYYYMMDD` - Discrimination data
- `GET /api/v1/discrimination/:channel/export?date=YYYYMMDD` - CSV export

---

## Implementation Priority

### Phase 1: Summary Screen (MVP)
- System status display
- Channel status table
- Time reference status
- **Goal:** Operational awareness

### Phase 2: Carrier Analysis
- Spectrogram display (existing images)
- Date navigation
- Basic metrics overlay
- **Goal:** Signal quality monitoring

### Phase 3: Discrimination
- Discrimination data display
- Interactive plots
- CSV export
- **Goal:** Scientific analysis

---

## Design Decisions & Rationale

**Why 3 screens?**
- Focus: Each screen has ONE clear purpose
- Simplicity: No feature bloat, no confusing navigation
- Scientific: Directly supports operational + research goals

**Why these specific metrics?**
- Timing quality: CRITICAL for data validity
- SNR: Indicates propagation conditions
- Gaps: Identifies data quality issues
- Discrimination: Core science objective (ionospheric study)

**What's intentionally excluded?**
- Complex configuration (use TOML files directly)
- Authentication (monitoring only, no control)
- Historical deep-dives (use analysis scripts for research)
- Real-time streaming (too resource-intensive)

**Mobile considerations?**
- Responsive layout (works on phone/tablet)
- Summary screen most useful for mobile monitoring
- Carrier/discrimination better on desktop

---

## Next Steps

1. Review and approve this design
2. Identify any missing metrics or features
3. Create API endpoint specifications
4. Design frontend component structure
5. Implement monitoring-server.js API layer (with paths API)
6. Build frontend (vanilla JS, no framework bloat)
7. Test with real data

---

## Questions for Discussion

1. **Summary screen:**
   - Are the 24-hour detection rates useful? Or prefer last-hour?
   - Should we show packet loss statistics?

2. **Carrier analysis:**
   - Grid layout preference (3x3, 2x5, other)?
   - Auto-refresh: Should it regenerate spectrograms or just fetch existing?
   - Click behavior: Modal detail view or dedicated page?

3. **Discrimination:**
   - Plot library preference (Chart.js, D3.js, Plotly)?
   - Should we show all 4 channels on one page or tabbed?
   - CSV format preferences for export?

4. **General:**
   - Color scheme (dark mode, light mode, user selectable)?
   - Update frequencies appropriate?
   - Any additional metrics needed?
