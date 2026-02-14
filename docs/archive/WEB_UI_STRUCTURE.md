# GRAPE Web-UI Structure

## Overview

The GRAPE web-UI consists of **5 stable core pages** with unified navigation and consistent styling. This structure prevents URL proliferation and provides clear categorical organization.

---

## 🌐 Core Pages

### 1. **Summary** (`summary.html`)
**Purpose:** Station info, daemon uptime, data collection status

**Current Features:**
- ⏱️ **Timing Status Widget** - Shows time basis (TONE_LOCKED/NTP_SYNCED/INTERPOLATED/WALL_CLOCK)
  - Primary reference channel
  - Precision estimate
  - Time snap adoption history
  - Per-channel timing breakdown

- 📊 **Tone Power Display** - Current 1000/1200 Hz tone levels
  - WWV (1000 Hz) vs WWVH (1200 Hz) power
  - Power ratio per channel
  - Visual bars with color coding

- 📡 **Station Information**
  - Call sign, grid, instrument ID
  - Location, elevation
  - Operator information

- 🖥️ **System Status**
  - Daemon processes (core-recorder, analytics, drf-writer)
  - Uptime, PID, status
  - Storage utilization

**API Endpoints Used:**
- `GET /api/v1/station/info`
- `GET /api/v1/system/status`
- `GET /api/v1/timing/status`
- `GET /api/v1/tones/current`

---

### 2. **Carrier Analysis** (`carrier.html`)
**Purpose:** 10 Hz carrier quality, spectrograms, signal characteristics

**Current Features:**
- Spectrograms for each channel
- Carrier quality metrics
- Date/channel selection

**API Endpoints Used:**
- `GET /api/v1/carrier/quality?date=YYYYMMDD`
- `GET /api/v1/carrier/available-dates`
- `GET /spectrograms/{date}/{filename}`

**Planned Additions:**
- FFT-based carrier drift visualization
- Long-term quality trends
- Cross-channel correlation

---

### 3. **Discrimination** (`discrimination.html`)
**Purpose:** WWV/WWVH station discrimination analysis

**Current Features (All 5 Methods):**

#### **Method 1: Timing Tones** (1000/1200 Hz)
- Power over time for both tones
- Differential power analysis
- Geographic/ToA correspondence

#### **Method 2: Tick Windows** (10-second coherent integration)
- WWV vs WWVH SNR over time
- Coherent vs incoherent integration
- Per-second tick analysis

#### **Method 3: Station ID** (440 Hz identification tones)
- Hourly station ID detections
- Power levels per station
- :08-:44 test signal analysis

#### **Method 4: BCD Discrimination** (100 Hz time code)
- WWV vs WWVH BCD amplitude
- Differential delay measurements
- Correlation quality

#### **Method 5: Weighted Voting** (final determination)
- Dominant station per minute
- Confidence levels
- Method agreement visualization

**API Endpoints Used:**
- `GET /api/v1/channels/:name/discrimination/:date/methods`

**Data Structure:**
All methods sourced from single CSV:
```
analytics/{channel}/discrimination/{channel}_discrimination_YYYYMMDD.csv
```

Columns include:
- `wwv_power_db`, `wwvh_power_db` (Method 1)
- `tick_windows_10sec` (Method 2, JSON)
- `tone_440hz_wwv_power_db`, `tone_440hz_wwvh_power_db` (Method 3)
- `bcd_wwv_amplitude`, `bcd_wwvh_amplitude` (Method 4)
- `dominant_station`, `confidence` (Method 5)

**Future Additions:**
- Per-hour summary statistics
- Method agreement heatmaps
- Propagation delay correlation with geography

---

### 4. **Timing Analysis** (`timing.html`)
**Purpose:** Detailed time basis analysis, precision, variance

**Status:** 🚧 To be created

**Planned Features:**
- **Time Basis Evolution**
  - TONE_LOCKED → NTP_SYNCED → INTERPOLATED transitions
  - Time snap adoption timeline
  - Precision vs confidence scatter

- **Variance Analysis**
  - Per-channel timing variance
  - Cross-correlation with reference
  - Allan deviation plots

- **Time Snap Quality**
  - RTP timestamp accuracy
  - Source confidence distribution
  - Age histogram

- **Per-Minute Analysis**
  - Timing quality grade (A/B/C/D/F)
  - Drift rates
  - Reference switching events

**API Endpoints (to be created):**
- `GET /api/v1/timing/history?channel=X&hours=Y`
- `GET /api/v1/timing/variance?date=YYYYMMDD`
- `GET /api/v1/timing/transitions?channel=X`

---

### 5. **Gap Analysis** (`gaps.html`)
**Purpose:** Data continuity, missing records, SNR-related gaps

**Status:** 🚧 To be created

**Planned Features:**
- **Gap Timeline**
  - Minute-by-minute completeness visualization
  - 🟢 Complete | 🟡 Partial | 🔴 Absent status
  - Interactive zoom to inspect gaps

- **Gap Statistics by Frequency**
  - Histogram of gap durations
  - Per-channel gap rates
  - Frequency-dependent patterns

- **Gap Statistics by Time**
  - Diurnal patterns
  - Propagation-related outages
  - Equipment restart events

- **SNR-Correlated Gaps**
  - Low-SNR threshold analysis
  - Signal dropout events
  - Atmospheric effects

- **Quality vs Completeness**
  - Trade-off visualization
  - Acceptance threshold tuning
  - Archive quality metrics

**API Endpoints (to be created):**
- `GET /api/v1/gaps/timeline?channel=X&date=Y`
- `GET /api/v1/gaps/statistics?start=X&end=Y`
- `GET /api/v1/gaps/by-snr?channel=X`

---

## 🧭 Unified Navigation

All pages share a consistent navigation component defined in `components/navigation.js`:

```javascript
window.GRAPE_CURRENT_PAGE = 'summary'; // or 'carrier', 'discrimination', 'timing', 'gaps'
<div id="grape-navigation"></div>
<script src="/components/navigation.js"></script>
```

**Navigation Features:**
- 🍇 GRAPE branding
- 5 core page links with active indicators
- Live connection status indicator
- Responsive mobile layout
- Consistent styling across all pages

---

## 📂 File Organization

```
web-ui/
├── summary.html              # Page 1: Summary
├── carrier.html              # Page 2: Carrier Analysis
├── discrimination.html       # Page 3: Discrimination (all 5 methods)
├── timing.html               # Page 4: Timing Analysis (to be created)
├── gaps.html                 # Page 5: Gap Analysis (to be created)
├── components/
│   ├── navigation.js         # Shared navigation component
│   ├── timing-status-widget.js
│   ├── tone-power-display.js
│   └── discrimination-charts.js
└── monitoring-server-v3.js   # Express server with all API endpoints
```

---

## 🎨 Design Principles

### **1. Stable URLs**
- Each page has a **permanent, memorable URL**
- No experimental or versioned URLs (e.g., `discrimination-v2.html`)
- Add/remove graphs within existing pages

### **2. Categorical Organization**
- Each page focuses on **one major category**
- Related analyses grouped together
- Easy to find relevant information

### **3. Iterative Improvement**
- Add new graphs to existing pages as insights emerge
- Remove unhelpful visualizations
- Converge on "what works best"

### **4. New Categories**
- If a new major category emerges, **add a 6th page**
- Must justify independent page vs. adding to existing
- Update navigation component accordingly

### **5. Visual Consistency**
- Shared color scheme: 🔵 WWV (blue) | 🟠 WWVH (amber) | 🟣 BALANCED (purple)
- Unified typography and spacing
- Consistent chart styling (Plotly.js)

---

## 🔌 API Structure

All API endpoints follow RESTful conventions under `/api/v1/`:

### **Current Endpoints**

**System & Summary:**
- `GET /api/v1/station/info` - Station metadata
- `GET /api/v1/system/status` - Process status
- `GET /api/v1/system/processes` - Daemon details
- `GET /api/v1/summary` - Aggregated summary

**Timing & Analytics:**
- `GET /api/v1/timing/status` - Current timing quality
- `GET /api/v1/tones/current` - Latest tone powers

**Carrier Analysis:**
- `GET /api/v1/carrier/quality?date=YYYYMMDD` - Quality metrics
- `GET /api/v1/carrier/available-dates` - Date list

**Discrimination:**
- `GET /api/v1/channels/:name/discrimination/:date/methods` - All 5 methods
- `GET /api/v1/channels/:name/discrimination/:date/dashboard` - Summary stats
- `GET /api/v1/channels/:name/discrimination/:date/metrics` - Detailed metrics

**Health:**
- `GET /health` - Server health check

### **Planned Endpoints**

**Timing Analysis:**
- `GET /api/v1/timing/history`
- `GET /api/v1/timing/variance`
- `GET /api/v1/timing/transitions`

**Gap Analysis:**
- `GET /api/v1/gaps/timeline`
- `GET /api/v1/gaps/statistics`
- `GET /api/v1/gaps/by-snr`

---

## 📊 Data Sources

All web-UI data comes from the analytics pipeline output:

**State Files** (`/state/`)
- `analytics-{channel_key}.json` - Real-time timing status, time_snap history

**Analytics CSVs** (`/analytics/{channel}/`)
- `discrimination/{channel}_discrimination_YYYYMMDD.csv` - All 5 discrimination methods
- `quality/{channel}_quality_YYYYMMDD.csv` - Signal quality metrics

**Archives** (`/archives/{channel}/`)
- `{timestamp}_{freq}_iq.npz` - Raw 16 kHz IQ data

**Digital RF** (`/analytics/{channel}/digital_rf/`)
- HDF5 files for external compatibility

---

## 🚀 Next Steps

### **Immediate (In Progress)**
- ✅ Unified navigation across all pages
- ✅ Consolidate discrimination.html with all 5 methods
- ✅ Add timing/tone widgets to summary.html

### **Short Term**
- 🚧 Create timing.html for detailed time analysis
- 🚧 Create gaps.html for continuity analysis
- 🚧 Add more visualizations to discrimination.html as needed

### **Long Term**
- Implement WebSocket for real-time updates
- Add user preferences (theme, refresh rates)
- Export data functionality
- Mobile-optimized layouts

---

## 📝 Adding New Visualizations

### **To Add a Graph to an Existing Page:**

1. **Add HTML container** to the appropriate page
   ```html
   <div class="chart-container" id="new-chart"></div>
   ```

2. **Create chart function** in corresponding JS file
   ```javascript
   function renderNewChart(data) {
     Plotly.newPlot('new-chart', traces, layout);
   }
   ```

3. **Fetch data** from existing or new API endpoint
   ```javascript
   const response = await fetch('/api/v1/...');
   const data = await response.json();
   renderNewChart(data);
   ```

4. **Test and iterate** - keep if useful, remove if not

### **To Create a New API Endpoint:**

1. **Add helper function** in `monitoring-server-v3.js`
   ```javascript
   async function getNewData(paths, params) {
     // Read files, process data
     return result;
   }
   ```

2. **Add route** under appropriate category
   ```javascript
   app.get('/api/v1/category/endpoint', async (req, res) => {
     try {
       const data = await getNewData(paths, req.query);
       res.json(data);
     } catch (err) {
       res.status(500).json({ error: err.message });
     }
   });
   ```

3. **Document** in server startup logs and this file

---

## 🎯 Success Metrics

**Good Web-UI Design:**
- ✅ Any user can find relevant data in <3 clicks
- ✅ Page purpose obvious from title and first view
- ✅ All related analyses in one place
- ✅ No broken links or experimental URLs
- ✅ Consistent navigation and styling

**Effective Visualizations:**
- ✅ Answer specific scientific questions
- ✅ Reveal patterns not obvious in raw data
- ✅ Interactive exploration (zoom, hover, select)
- ✅ Clear legends, axes, units
- ✅ Appropriate chart type for data

---

**Last Updated:** November 25, 2025
**Status:** Core structure implemented, timing and gaps pages pending
