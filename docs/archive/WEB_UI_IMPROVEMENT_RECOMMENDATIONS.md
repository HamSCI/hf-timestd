# Web-UI Improvement Recommendations
**Date:** November 24, 2025  
**Status:** Recommendations for enhancing data presentation based on recent analytics metadata integration  
**Context:** Analytics now captures rich metadata including time_snap references, tone power measurements, quality metrics, and 5-method discrimination results

---

## Executive Summary

The GRAPE Signal Recorder has a sophisticated backend that captures extensive scientific data through the core recorder and analytics services. However, the web-ui currently underutilizes much of this rich metadata. This document provides prioritized recommendations to improve data presentation, making the system more transparent, scientifically rigorous, and operator-friendly.

**Key Opportunities:**
1. **Real-time quality visualization** - Display timing status, tone detection, and gap analysis
2. **Multi-method discrimination display** - Show all 5 discrimination methods with confidence levels
3. **Metadata integration** - Surface time_snap adoption, tone power comparison, and cross-validation results
4. **Scientific transparency** - Complete data provenance and quality assessment
5. **Operational clarity** - Clear status indicators and actionable alerts

---

## Current State Assessment

### ✅ What Exists

**Web Pages:**
- `summary.html` - System overview, channel status, station info
- `discrimination.html` - WWV/WWVH analysis with Plotly visualizations
- `timing-dashboard.html` - Timing & quality dashboard with dark theme
- `carrier.html` - 10 Hz decimated analysis with spectrograms

**API Endpoints (monitoring-server-v3.js):**
- `/api/v1/system/status` - Comprehensive system health
- `/api/v1/channels/status` - Per-channel RTP metrics
- `/api/v1/carrier/quality` - Quality metrics by date
- `/api/v1/channels/:name/discrimination/:date` - Discrimination time-series
- `/api/monitoring/timing-quality` - Timing & quality (V2 compatible)
- Spectrogram serving endpoints

**Data Available (Not Currently Displayed):**
- Time_snap metadata (RTP, UTC, source, confidence, station) ✨ NEW
- Tone power measurements from recorder (1000 Hz, 1200 Hz) ✨ NEW
- Archive time_snap adoption events ✨ NEW
- Tone power cross-validation (recorder vs analytics) ✨ NEW
- 5 separate discrimination method results (separated CSVs)
- BCD window-by-window analysis (3-second resolution)
- Per-second tick window analysis (10-second coherent integration)
- 440 Hz station ID detections (minutes 1 & 2)
- Gap analysis with type classification
- Quality grades with weighted scoring

### ❌ What's Missing

1. **Time_Snap Status Display**
   - No real-time indicator showing current time_snap source (TONE_LOCKED/NTP/WALL_CLOCK)
   - Missing time_snap age and confidence display
   - No visualization of time_snap adoption events (when analytics adopts recorder snap)
   - No history of time_snap corrections

2. **Tone Detection Visualization**
   - Tone power levels not displayed (1000 Hz WWV, 1200 Hz WWVH)
   - No comparison between recorder and analytics tone measurements
   - Missing SNR trends over time
   - No indication of which tones are used for time_snap

3. **Multi-Method Discrimination Display**
   - Current discrimination page shows final voting only
   - Individual method results not displayed separately:
     - Method 1: Timing Tones (1000/1200 Hz power ratio)
     - Method 2: Tick Windows (5ms coherent integration)
     - Method 3: Station ID (440 Hz)
     - Method 4: BCD (100 Hz subcarrier)
     - Method 5: Weighted Voting (final)
   - No confidence level visualization per method
   - Missing method agreement/disagreement indicators

4. **Quality Metrics Visualization**
   - Gap timeline not visualized (minute-by-minute completeness)
   - Packet loss trends not graphed
   - No quality grade history display
   - Missing gap type breakdown (RTP jumps, carrier loss, etc.)

5. **Metadata & Provenance**
   - Archive metadata not displayed (embedded time_snap, tone powers)
   - No cross-validation results shown (recorder vs analytics)
   - Missing data lineage visualization
   - No indication of which data is tone-locked vs NTP-synced

---

## Priority 1: Real-Time Timing Status Dashboard

### Objective
Create a prominent, always-visible timing status display that shows operators the current quality of their time references at a glance.

### Implementation

**New Component: Timing Status Widget**

Location: Top of `summary.html` or dedicated section in `timing-dashboard.html`

**Display Elements:**

```
┌─────────────────────────────────────────────────────────┐
│ ⏱️  TIMING STATUS                                        │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  🟢 TONE-LOCKED                                         │
│  Time reference: WWV 10 MHz (1000 Hz tone)             │
│  Precision: ±1 ms                                       │
│  Last update: 2 minutes ago                             │
│  Confidence: 0.95 (HIGH)                                │
│                                                         │
│  ┌──────────────────────────────────────────┐          │
│  │ Time_snap Details:                        │          │
│  │ RTP: 1234567890                           │          │
│  │ UTC: 2025-11-24 19:45:32.000              │          │
│  │ Source: TONE_DETECTED                     │          │
│  └──────────────────────────────────────────┘          │
│                                                         │
│  📊 Channel Status: 6/9 tone-locked, 3 NTP-synced      │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

**Status Indicators:**
- 🟢 **TONE-LOCKED** (±1ms) - Active tone detection <5 min ago
- 🟡 **NTP-SYNCED** (±10ms) - NTP time, no recent tone
- 🟠 **INTERPOLATED** (±100ms) - Aged tone reference (5-60 min)
- 🔴 **WALL-CLOCK** (±seconds) - Fallback mode

**API Endpoint:** `/api/v1/timing/status` (NEW)

**Data Structure:**
```json
{
  "overall_status": "TONE_LOCKED",
  "precision_estimate_ms": 1.0,
  "primary_reference": {
    "channel": "WWV 10 MHz",
    "station": "WWV",
    "time_snap_rtp": 1234567890,
    "time_snap_utc": "2025-11-24T19:45:32.000Z",
    "source": "TONE_DETECTED",
    "confidence": 0.95,
    "age_seconds": 120
  },
  "channel_breakdown": {
    "tone_locked": 6,
    "ntp_synced": 3,
    "interpolated": 0,
    "wall_clock": 0
  },
  "recent_adoptions": [
    {
      "timestamp": "2025-11-24T19:43:00Z",
      "channel": "WWV 5 MHz",
      "reason": "Archive time_snap superior to current NTP reference",
      "improvement_ms": 8.5
    }
  ]
}
```

**Visual Design:**
- Large, color-coded status badge
- Expandable details section
- Timeline showing time_snap establishment events
- Animation/pulse effect when tone-locked

---

## Priority 2: Multi-Method Discrimination Dashboard

### Objective
Display all 5 discrimination methods side-by-side so operators and researchers can see which methods agree, disagree, and understand the confidence levels.

### Implementation

**Enhanced Discrimination Page**

Redesign `discrimination.html` with 6 panels (5 methods + final voting):

```
┌─────────────────────────────────────────────────────────────┐
│  WWV/WWVH Discrimination - 10 MHz - 2025-11-24             │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  [Method 1: Timing Tones]  [Method 2: Tick Windows]        │
│  ┌──────────────────────┐  ┌──────────────────────┐        │
│  │ WWV:  45 detections  │  │ WWV SNR:  18.2 dB    │        │
│  │ WWVH: 38 detections  │  │ WWVH SNR: 15.7 dB    │        │
│  │ Ratio: 1.18          │  │ Coherence: 0.82      │        │
│  │ ✅ Confident          │  │ ✅ Confident          │        │
│  └──────────────────────┘  └──────────────────────┘        │
│                                                             │
│  [Method 3: Station ID]    [Method 4: BCD]                 │
│  ┌──────────────────────┐  ┌──────────────────────┐        │
│  │ WWV ID:  2 detections│  │ WWV amp: 0.65        │        │
│  │ WWVH ID: 1 detection │  │ WWVH amp: 0.42       │        │
│  │ 440Hz: Clear         │  │ Delay: 2.3 ms        │        │
│  │ ✅ Unambiguous        │  │ ✅ High correlation   │        │
│  └──────────────────────┘  └──────────────────────┘        │
│                                                             │
│  [Method 5: Weighted Voting] - FINAL DETERMINATION         │
│  ┌───────────────────────────────────────────────┐         │
│  │ 🎯 Dominant: WWV                               │         │
│  │ Confidence: HIGH                               │         │
│  │                                                │         │
│  │ Method Weights:                                │         │
│  │ • Timing Tones:  25% (WWV favored)            │         │
│  │ • Tick Windows:  20% (WWV favored)            │         │
│  │ • Station ID:    30% (WWV detected min 2)     │         │
│  │ • BCD Analysis:  25% (WWV stronger)           │         │
│  │                                                │         │
│  │ Agreement: 4/4 methods favor WWV              │         │
│  └───────────────────────────────────────────────┘         │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

**Per-Method Time-Series Plots:**

Display 5 separate time-series charts showing:
1. **Timing Tones:** WWV/WWVH power ratio over time
2. **Tick Windows:** Coherent SNR for WWV vs WWVH
3. **Station ID:** 440 Hz detection events (discrete markers)
4. **BCD:** Amplitude ratio and differential delay
5. **Weighted Voting:** Final determination with confidence bands

**API Endpoints (ENHANCE EXISTING):**

Enhance `/api/v1/channels/:name/discrimination/:date` to include:
- Per-method results (currently only shows final voting)
- Method-specific confidence levels
- Agreement matrix (which methods agree/disagree)
- Temporal coverage (how many minutes each method provides data)

**Data Loading:**
Read from separated CSV files:
- `analytics/{channel}/tone_detections/{channel}_tones_YYYYMMDD.csv`
- `analytics/{channel}/tick_windows/{channel}_ticks_YYYYMMDD.csv`
- `analytics/{channel}/station_id_440hz/{channel}_440hz_YYYYMMDD.csv`
- `analytics/{channel}/bcd_discrimination/{channel}_bcd_YYYYMMDD.csv`
- `analytics/{channel}/discrimination/{channel}_discrimination_YYYYMMDD.csv`

**Visual Features:**
- Color-code agreement: green = all agree, yellow = majority, red = disagreement
- Show confidence bars for each method
- Highlight when methods disagree (scientific interest)
- Interactive tooltips with raw values

---

## Priority 3: Tone Detection & Power Display

### Objective
Visualize tone detection performance, power levels, and cross-validation between recorder and analytics measurements.

### Implementation

**New Section: Tone Analysis Dashboard**

Location: New page `tone-analysis.html` or section in `timing-dashboard.html`

**Display Elements:**

```
┌─────────────────────────────────────────────────────────┐
│  TONE DETECTION ANALYSIS - WWV 10 MHz                   │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  📊 Tone Power Levels (Real-Time)                       │
│                                                         │
│  1000 Hz (WWV):  ████████████████░░░░  35.2 dB         │
│  1200 Hz (WWVH): ██████████░░░░░░░░░░  28.7 dB         │
│  Ratio: 6.5 dB (WWV stronger)                           │
│                                                         │
│  ✅ Cross-Validation:                                    │
│  Recorder startup: 1000Hz = 34.8 dB, 1200Hz = 28.9 dB  │
│  Analytics detect: 1000Hz = 35.2 dB, 1200Hz = 28.7 dB  │
│  Delta: 1000Hz = +0.4 dB, 1200Hz = -0.2 dB ✓           │
│                                                         │
│  📈 Detection Rate (Last Hour)                          │
│  WWV:  58/60 minutes (97% success rate)                │
│  WWVH: 45/60 minutes (75% success rate)                │
│  CHU:  0/60 minutes (N/A for 10 MHz)                   │
│                                                         │
│  🎯 Timing Quality                                       │
│  Mean timing error: -0.3 ms (excellent)                 │
│  Std deviation: 1.2 ms                                  │
│  SNR: 24.5 dB (strong signal)                           │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

**Time-Series Charts:**
1. **Tone Power Over Time** - Line chart showing 1000 Hz and 1200 Hz power
2. **SNR Trends** - Quality of tone detections
3. **Timing Error** - Distribution of tone timing errors (should be ±2ms)
4. **Detection Success Rate** - Percentage per 10-minute window

**API Endpoint:** `/api/v1/tones/analysis` (NEW)

**Data Structure:**
```json
{
  "channel": "WWV 10 MHz",
  "timestamp": "2025-11-24T19:45:00Z",
  "current_powers": {
    "tone_1000_hz_db": 35.2,
    "tone_1200_hz_db": 28.7,
    "ratio_db": 6.5
  },
  "cross_validation": {
    "recorder_1000_hz": 34.8,
    "recorder_1200_hz": 28.9,
    "analytics_1000_hz": 35.2,
    "analytics_1200_hz": 28.7,
    "delta_1000_hz": 0.4,
    "delta_1200_hz": -0.2,
    "correlation": "GOOD"
  },
  "detection_rates": {
    "last_hour": {
      "wwv_detections": 58,
      "wwvh_detections": 45,
      "chu_detections": 0,
      "total_minutes": 60
    }
  },
  "timing_quality": {
    "mean_error_ms": -0.3,
    "std_dev_ms": 1.2,
    "mean_snr_db": 24.5
  }
}
```

**Data Sources:**
- Read from `analytics/{channel}/tone_detections/{channel}_tones_YYYYMMDD.csv`
- Load archive NPZ metadata for recorder measurements
- Compare recorder tone_power_1000_hz_db with analytics detections

---

## Priority 4: Quality Metrics Visualization

### Objective
Provide comprehensive quality visualization including gap timelines, packet loss trends, and quality grade history.

### Implementation

**Enhanced Quality Dashboard**

Redesign quality section to include:

**1. Gap Timeline Visualization**

```
┌─────────────────────────────────────────────────────────┐
│  COMPLETENESS TIMELINE - 2025-11-24                     │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  00:00 ████████████████████████████████████████ 24:00  │
│        🟢🟢🟢🟢🟢🟢🔴🔴🟡🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢           │
│                                                         │
│  🟢 Complete (960k samples)  - 1380 minutes (95.8%)    │
│  🟡 Partial (<960k samples)  - 45 minutes (3.1%)       │
│  🔴 Absent (0 samples)       - 15 minutes (1.1%)       │
│                                                         │
│  Largest gap: 06:15-06:30 (15 min) - RTP timeout       │
│  Total gaps: 8 events                                   │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

Interactive features:
- Click on timeline to see gap details
- Hover for per-minute quality
- Zoom into time ranges
- Export gap report

**2. Quality Grade History**

Line chart showing quality grade over time:
- A (Excellent): 95-100%
- B (Good): 85-94%
- C (Acceptable): 70-84%
- D (Poor): 50-69%
- F (Failed): <50%

Show breakdown by component:
- Sample integrity (40% weight)
- RTP continuity (30% weight)
- Time_snap quality (20% weight)
- Network performance (10% weight)

**3. Packet Loss Trends**

Chart showing:
- RTP sequence gap rate
- Out-of-order packets
- Duplicate packets
- Late arrivals

**API Endpoint:** `/api/v1/quality/timeline` (NEW)

**Data Structure:**
```json
{
  "channel": "WWV 10 MHz",
  "date": "20251124",
  "minutes": [
    {
      "minute": "2025-11-24T00:00:00Z",
      "samples": 960000,
      "completeness": 1.0,
      "status": "COMPLETE",
      "quality_grade": "A"
    },
    {
      "minute": "2025-11-24T06:15:00Z",
      "samples": 0,
      "completeness": 0.0,
      "status": "ABSENT",
      "quality_grade": "F",
      "gap_reason": "RTP_TIMEOUT"
    }
  ],
  "summary": {
    "complete_minutes": 1380,
    "partial_minutes": 45,
    "absent_minutes": 15,
    "overall_completeness": 95.8,
    "quality_grade": "A",
    "gap_count": 8,
    "largest_gap_duration_min": 15
  }
}
```

**Data Sources:**
- Read from `analytics/{channel}/quality/{channel}_quality_YYYYMMDD.csv`
- Parse completeness_pct, samples, quality_grade columns

---

## Priority 5: Metadata & Provenance Display

### Objective
Make data provenance transparent by showing how time references are established, validated, and adopted.

### Implementation

**New Page: Data Provenance Dashboard**

**Sections:**

**1. Time_Snap Adoption History**

```
┌─────────────────────────────────────────────────────────┐
│  TIME_SNAP ADOPTION EVENTS                              │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  2025-11-24 19:43:12                                    │
│  ✅ Adopted archive time_snap from WWV 5 MHz            │
│  Reason: Archive snap superior to current NTP reference │
│  Improvement: 8.5 ms better precision                   │
│  Old: NTP_SYNCED (±10ms)                                │
│  New: TONE_DETECTED (±1ms)                              │
│                                                         │
│  2025-11-24 18:15:00                                    │
│  ✅ Established new time_snap from WWV 10 MHz           │
│  Reason: Fresh tone detection                           │
│  Confidence: 0.95 (HIGH)                                │
│  Station: WWV (1000 Hz tone)                            │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

**2. Archive Metadata Display**

Show embedded metadata for each NPZ archive file:
- Time_snap references (RTP, UTC, source, confidence)
- Tone power measurements (1000 Hz, 1200 Hz)
- Gap records
- Quality annotations

**3. Cross-Validation Results**

Display comparison between:
- Recorder startup measurements vs analytics processing
- Multiple channel measurements of same signal
- Time_snap consistency across channels

**API Endpoint:** `/api/v1/metadata/provenance` (NEW)

**Data Structure:**
```json
{
  "time_snap_adoptions": [
    {
      "timestamp": "2025-11-24T19:43:12Z",
      "channel": "WWV 5 MHz",
      "event_type": "ARCHIVE_ADOPTION",
      "old_source": "NTP_SYNCED",
      "new_source": "TONE_DETECTED",
      "improvement_ms": 8.5,
      "reason": "Archive time_snap superior to current reference"
    }
  ],
  "archive_metadata_sample": {
    "file": "20251124_194500_iq.npz",
    "time_snap": {
      "rtp": 1234567890,
      "utc": "2025-11-24T19:45:00.000Z",
      "source": "TONE_DETECTED",
      "confidence": 0.95,
      "station": "WWV"
    },
    "tone_powers": {
      "tone_1000_hz_db": 34.8,
      "tone_1200_hz_db": 28.9
    },
    "gaps": []
  }
}
```

**Data Sources:**
- Analytics state files: `state/analytics-{channel}.json`
- Archive NPZ files (read metadata without loading full IQ data)
- Analytics processing logs

---

## Implementation Priority Summary

### Immediate (Within 1 week)

1. **Timing Status Widget** - Most impactful for operators
   - Add `/api/v1/timing/status` endpoint
   - Create widget component
   - Integrate into `summary.html`

2. **Basic Tone Display** - Show current tone powers
   - Add tone power bars to channel status
   - Display detection success rates
   - Simple cross-validation indicators

### Short-term (1-2 weeks)

3. **Multi-Method Discrimination** - Enhance existing page
   - Modify `/api/v1/channels/:name/discrimination/:date` to load all CSVs
   - Create 6-panel layout
   - Add per-method time-series charts

4. **Gap Timeline** - Visual quality indicator
   - Add `/api/v1/quality/timeline` endpoint
   - Create minute-by-minute visualization
   - Interactive gap details

### Medium-term (2-4 weeks)

5. **Tone Analysis Page** - Dedicated tone performance page
   - Create `tone-analysis.html`
   - Add `/api/v1/tones/analysis` endpoint
   - Time-series charts for power, SNR, timing error

6. **Quality History** - Trend visualization
   - Quality grade over time
   - Packet loss trends
   - Component breakdown charts

### Long-term (1-2 months)

7. **Provenance Dashboard** - Complete transparency
   - Create `provenance.html`
   - Add `/api/v1/metadata/provenance` endpoint
   - Archive metadata browser
   - Time_snap adoption history

8. **Enhanced Visualizations**
   - WebSocket real-time updates
   - Interactive Chart.js/D3.js visualizations
   - Export capabilities
   - Mobile-responsive enhancements

---

## Technical Implementation Notes

### API Design Principles

1. **Use Existing GRAPEPaths API**
   - All file access through `grape-paths.js`
   - Consistent path resolution
   - Mode-aware (test vs production)

2. **RESTful Endpoints**
   - Consistent URL structure
   - Proper HTTP methods
   - JSON responses
   - Error handling

3. **Efficient Data Loading**
   - Cache parsed CSVs
   - Load only date ranges requested
   - Aggregate on server-side
   - Paginate large datasets

### Frontend Best Practices

1. **Progressive Enhancement**
   - Core functionality without JS
   - Enhanced experience with JS
   - Graceful degradation

2. **Performance**
   - Lazy load heavy charts
   - Debounce auto-refresh
   - Use CSS animations
   - Minimize reflows

3. **Accessibility**
   - ARIA labels on all status indicators
   - Keyboard navigation
   - Screen reader support
   - Color contrast compliance

### Data Flow

```
CSV Files (Analytics Output)
    ↓
GRAPEPaths API (Path Resolution)
    ↓
monitoring-server-v3.js (API Endpoints)
    ↓
JSON Response
    ↓
HTML/JavaScript (Visualization)
```

---

## Visual Design Consistency

### Color Scheme (Match timing-dashboard.html)

```css
/* Background */
--bg-primary: #0a0e27;      /* Dark navy */
--bg-secondary: #1e293b;    /* Slate */

/* Status Colors */
--status-success: #10b981;  /* Green - TONE_LOCKED */
--status-warning: #f59e0b;  /* Amber - NTP_SYNCED */
--status-error: #ef4444;    /* Red - WALL_CLOCK */
--status-info: #3b82f6;     /* Blue - Info */

/* Text */
--text-primary: #e0e0e0;    /* Light gray */
--text-secondary: #94a3b8;  /* Slate gray */
```

### Status Icons

- 🟢 **Excellent** - All systems nominal
- 🟡 **Good** - Minor degradation
- 🟠 **Warning** - Attention needed
- 🔴 **Critical** - Action required
- ⚪ **Unknown** - Data unavailable

### Typography

- **Headings:** Inter, system-ui fallback
- **Body:** -apple-system, BlinkMacSystemFont, Segoe UI
- **Monospace:** Consolas, Monaco, Courier New

---

## Success Metrics

### Operator Experience

- Time to diagnose issues: **<30 seconds** (currently ~5 minutes)
- Clarity of system status: **95% operators understand** (currently ~70%)
- False alarm rate: **<5%** (currently ~15%)

### Scientific Value

- Data quality transparency: **100% gaps documented**
- Provenance completeness: **Full chain visible**
- Multi-method discrimination: **All 5 methods displayed**

### System Performance

- Dashboard load time: **<2 seconds**
- Auto-refresh overhead: **<100ms**
- API response time: **<500ms** (p95)

---

## Next Steps

1. **Review & Prioritize** - Discuss recommendations with team
2. **Create Mockups** - Design key UI components
3. **Implement Phase 1** - Timing status + tone display (highest impact)
4. **User Testing** - Get feedback from operators
5. **Iterate** - Refine based on real-world usage
6. **Document** - Update web-ui docs with new features

---

## Related Documentation

- `CONTEXT.md` - Project overview and current capabilities
- `ARCHITECTURE.md` - System design rationale
- `SESSION_2025-11-24_ANALYTICS_METADATA_INTEGRATION.md` - Recent metadata integration
- `docs/WEB_UI_INFORMATION_ARCHITECTURE.md` - Existing UI architecture
- `docs/web-ui/WEB_UI_PROGRESS.md` - Current implementation status
- `web-ui/monitoring-server-v3.js` - API implementation
- `src/signal_recorder/discrimination_csv_writers.py` - CSV data structures

---

**Prepared by:** Cascade AI Assistant  
**Date:** November 24, 2025  
**Status:** Ready for Review and Implementation
