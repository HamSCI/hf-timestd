# Web-UI Modernization Progress

## ✅ Completed (Session 1)

### 1. File Cleanup
**Deleted legacy backup files:**
- `index.html.OLD`
- `monitoring.html.backup`
- `monitoring.html.OLD`
- `simple-server.js.backup-20251103-094306`
- `simple-server.js.backup-20251104-070345`

**Archived:**
- `simple-server.js` → `simple-server.js.ARCHIVED-config-ui`
  - Reason: Full configuration UI not needed (editing TOML files directly)
  - Decision: Stick with `monitoring-server.js` as the single server

###  2. API v1 Implementation
**New endpoints added to `monitoring-server.js`:**

#### `/api/v1/system/status`
Comprehensive system health check including:
- Recorder status (running, uptime, PID, mode)
- Radiod connection status
- Channel count (total, active, errors)
- Time_snap status (established, source, age, confidence)
- Overall quality grade (last 10 minutes)
- Data paths (archive, analytics, upload)
- **Disk usage** (total, used, free, percent) ✨ NEW
- **Recent errors** (last 5 from logs) ✨ NEW
- Station info

#### `/api/v1/system/health`
Simple health check for monitoring tools:
- Recorder status check
- Disk space check (warn if >90%)
- Time_snap check
- Returns: `healthy` | `degraded`

#### `/api/v1/system/errors`
Error log access with filtering:
- Query params: `limit`, `severity` (error/warning)
- Parses log file for ERROR and WARNING lines
- Returns structured error list with timestamps

### 3. Helper Functions Added
- `getDiskUsage(path)` - Uses `df` command to get disk metrics
- `getRecorderStatus()` - Checks recording-stats.json file age
- `getChannelStatus()` - Counts active channels from status file
- `getTimeSnapStatus()` - Finds time_snap from WWV/CHU channels
- `getRecentErrors(limit)` - Parses log file for errors/warnings

### 4. Backward Compatibility
- All existing `/api/monitoring/*` endpoints preserved
- No breaking changes to existing dashboards
- Ready to add 301 redirects when clients migrate to v1

---

## 📋 Next Steps (Prioritized per user request)

### Priority 1: WWV/WWVH Discrimination API ✨
**Goal:** Provide detailed WWV/WWVH/CHU detection data and differential delays

**New endpoints to create:**
1. `/api/v1/timing/wwv-detections?channel=<id>&date=<YYYYMMDD>`
   - Separate WWV, WWVH, CHU detection counts
   - Detection rates (percentage)
   - Timing errors for each station
   - SNR data
   - Differential delays (WWV-WWVH)

2. `/api/v1/timing/differential-delays?date=<YYYYMMDD>`
   - WWV-WWVH propagation differences
   - Mean, std dev, min, max
   - Timeline data (per-minute)
   - Per-frequency analysis (2.5, 5, 10, 15 MHz)

**Data source:** CSV files already contain:
- `wwv_detected`, `wwvh_detected`, `chu_detected` columns
- `differential_delay_ms` column
- `drift_ms` for timing errors

**Implementation:**
- Parse existing quality CSV files
- Aggregate WWV/WWVH/CHU data separately
- Calculate statistics
- Return JSON for visualization

### Priority 2: Spectrogram Generation (Hourly Cron) 📊
**Goal:** Generate 10 Hz carrier spectrograms from Digital RF data

**Approach:** Hourly batch generation
- Cron job runs every hour
- Processes previous hour's Digital RF data
- Generates PNG spectrograms
- Stores in `/cache/spectrograms/`

**New endpoint:**
- `/api/v1/data/spectrograms/<channel_id>?date=<YYYYMMDD>&hour=<HH>`
  - Returns PNG image (binary)
  - Cached for 1 hour
  - Shows ±5 Hz around carrier
  - Visualizes Doppler shifts

**Python script needed:**
- `scripts/generate_spectrogram.py`
- Reads Digital RF using `DigitalRFReader` interface
- Uses scipy.signal.spectrogram
- 1-minute FFT windows
- Saves to PNG

**Crontab entry:**
```bash
0 * * * * /path/to/python scripts/generate_spectrogram.py --hour-ago 1
```

### Priority 3: Detection Timeline 📅
**Goal:** Minute-by-minute signal present/absent visualization

**New endpoint:**
- `/api/v1/data/detection-timeline?date=<YYYYMMDD>`
  - Returns 1440 minutes for all 9 channels
  - Status: present (960k samples) | partial (<960k) | absent (0)
  - Quality grade per minute
  - Compact JSON format

**Data source:** Quality CSV files
- `samples` column (should be 960000 for complete minute)
- `quality_grade` column
- `completeness_pct` column

**Frontend visualization:**
- 9 horizontal tracks (one per channel)
- Color-coded: green (present), yellow (partial), red (absent)
- Interactive hover for details
- Scrollable timeline

---

## 🔧 Technical Decisions Made

1. **Single server:** `monitoring-server.js` (not `simple-server.js`)
2. **API versioning:** `/api/v1/` prefix
3. **Desktop-only UI:** No mobile-specific optimization needed
4. **Spectrogram generation:** Hourly cron (not real-time)
5. **Analytics priority:** WWV/WWVH → Spectrograms → Detection timeline

---

## 📊 API Structure (Current + Planned)

```
/api/v1/
├── system/                 ✅ DONE
│   ├── status             ✅ Comprehensive health check
│   ├── health             ✅ Simple health check
│   └── errors             ✅ Recent errors with filtering
│
├── timing/                 🔨 NEXT (Priority 1)
│   ├── wwv-detections     🔨 WWV/WWVH/CHU data per channel
│   ├── differential-delays 🔨 WWV-WWVH propagation
│   └── time-snap          📝 Planned
│
├── data/                   📝 Priority 2 & 3
│   ├── spectrograms/<id>  📝 Spectrogram images
│   ├── detection-timeline 📝 Minute-by-minute status
│   └── signal-power/<id>  📝 Future
│
└── quality/                📝 Future
    ├── summary            📝 Aggregate quality metrics
    └── timeline           📝 Quality over time

/api/monitoring/            ✅ LEGACY (kept for compatibility)
├── station-info           ✅ Backward compatible
├── timing-quality         ✅ Backward compatible
└── live-quality           ✅ Backward compatible
```

---

## 🎯 Current Status

**What works:**
- ✅ Server starts successfully
- ✅ Legacy endpoints functional
- ✅ New API v1 endpoints defined
- ✅ Disk usage monitoring
- ✅ Error log parsing
- ✅ System health checks

**To be tested:**
- API v1 endpoints (need running recorder for full test)
- Disk usage on actual data partition
- Error parsing from real logs
- Integration with existing timing-dashboard.html

**Ready for:**
1. Commit current progress
2. Implement WWV/WWVH discrimination API (Priority 1)
3. Set up spectrogram generation (Priority 2)
4. Build detection timeline (Priority 3)

---

## 📝 Files Modified

1. `monitoring-server.js` - Enhanced with API v1
2. `test-api.sh` - Created for testing (needs working env)
3. **DELETED:** 5 backup files
4. **ARCHIVED:** `simple-server.js.ARCHIVED-config-ui`

---

## 🚀 How to Test (Manual)

```bash
cd web-ui
node monitoring-server.js

# In another terminal:
curl http://localhost:3000/api/v1/system/health | python3 -m json.tool
curl http://localhost:3000/api/v1/system/status | python3 -m json.tool
curl http://localhost:3000/api/v1/system/errors?limit=5 | python3 -m json.tool
```

Expected output:
- Health check should show recorder status, disk usage
- Status should show comprehensive system info
- Errors should show recent ERROR/WARNING from logs
