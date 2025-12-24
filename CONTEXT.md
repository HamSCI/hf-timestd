# HF Time Standard - System Context

**Last Updated:** 2025-12-24  
**Current Version:** v3.1.0

## System Architecture

### Core Services

- **`timestd-core-recorder`**: Receives RTP streams from `radiod`, writes Digital RF archives (Phase 1)
- **`timestd-analytics`**: 9 Phase 2 processes + fusion engine for timing analysis
- **`timestd-science-aggregator`**: Multi-frequency TEC calculation (NEW in v3.1.0)
- **`timestd-web-ui`**: Node.js monitoring server serving real-time dashboards

### Data Flow

```
SDR → radiod → RTP/F32 → core-recorder → Hot Buffer (/dev/shm/timestd/raw_buffer)
                                               ↓ (background archiver)
                                           Cold Buffer (/var/lib/timestd/raw_buffer)
                                               ↑ (reads hot first, falls back to cold)
                                           analytics → timing CSVs → chrony SHM
                                                    ↓
                                           clock_offset CSVs → science-aggregator → TEC CSVs
                                                                                   → web-ui
```

**Tiered Storage:** Core recorder writes to RAM (`/dev/shm`), background thread archives old minutes to disk. Analytics reads from hot buffer first (zero-latency), falls back to cold if needed.

---

## Recent Changes (v3.1.0 - 2025-12-24)

### TEC Implementation ✅

**Achievement:** Multi-frequency ionospheric TEC measurement from HF timing data

**Key Changes:**

- **`science_aggregator.py`**: Refactored to use `TimeStdPaths` API (proper architecture)
  - Uses `discover_phase2_channels()` for automatic channel discovery
  - Uses `get_clock_offset_dir(channel_name)` for consistent path resolution
  - Fixed field name bug: `'minute_boundary'` → `'minute_boundary_utc'`
  - Generates TEC every 5 minutes from multi-frequency clock offset data

- **`tec_geometry.py`**: Obliquity factor and geometric corrections for slant-to-vertical TEC

- **GPS Integration** (Optional, not required):
  - ZED-F9P configured on port 2001 (33 satellites, UBX-NAV-SAT streaming)
  - IONEX validation tools ready (NASA OAuth pending)
  - See `docs/GPS_TEC_OPTIONAL.md` for details

**Production Status:**

- ✅ Service running: `timestd-science-aggregator`
- ✅ Data generating: `/var/lib/timestd/phase2/science/tec/tec_YYYYMMDD.csv`
- ✅ 121+ measurements per day (CHU, WWV, WWVH)
- ✅ 3-6 frequencies per station, 1-minute cadence

**Documentation:**

- `docs/GPS_TEC_OPTIONAL.md` - Comprehensive guide (HF TEC works standalone, GPS optional)
- `docs/TEC_VALIDATION_METHODOLOGY.md` - Scientific methodology
- `docs/ZED_F9P_TEC_CONFIGURATION.md` - GPS receiver setup

---

## Focus for Next Session: Web UI Integration with TEC Data

### Current Issue

**Problem:** Web UI needs integration with new TEC data for ionospheric visualization

### TEC Data Available

**Location:** `/var/lib/timestd/phase2/science/tec/tec_YYYYMMDD.csv`

**Format:**

```csv
timestamp_utc,minute_boundary,station,tec_tecu,t_vacuum_error_ms,confidence,residuals_ms,n_frequencies,frequencies_mhz,group_delay_2_5_mhz,...
2025-12-24T16:30:00+00:00,1766593800,WWV,-0.0,-4.472,0.0999,0.116,6,2.50;5.00;10.00;15.00;20.00;25.00,-12.518,-3.13,-0.782,...
```

**Fields:**

- `timestamp_utc`: ISO 8601 timestamp
- `station`: WWV, WWVH, CHU, BPM
- `tec_tecu`: Total Electron Content (TECU units, 10^16 electrons/m²)
- `confidence`: 0.0-1.0 (quality metric)
- `n_frequencies`: Number of frequencies used (3-6)
- `frequencies_mhz`: Semicolon-separated list
- `group_delay_*_mhz`: Per-frequency group delays (ms)

### Web UI Integration Tasks

1. **Add TEC API Endpoint**
   - File: `web-ui/monitoring-server-v3.js`
   - Endpoint: `/api/v1/science/tec`
   - Read TEC CSV, return JSON for date range
   - Filter by station, time range

2. **Create TEC Visualization Page**
   - File: `web-ui/public/ionosphere.html` (or new page)
   - Time series plot: TEC vs time for each station
   - Multi-frequency display: Show contributing frequencies
   - Confidence indicators: Color-code by quality
   - Station comparison: Overlay WWV/WWVH/CHU

3. **Add Real-time TEC Widget**
   - File: `web-ui/public/summary.html`
   - Current TEC value per station
   - Trend indicator (increasing/decreasing)
   - Link to full ionosphere page

4. **Handle Missing Data Gracefully**
   - TEC data only available if Science Aggregator running
   - Show "TEC data unavailable" if service stopped
   - Degrade gracefully if CSV missing or empty

### Web UI Current State

**Known Issues:**

- Audio playback problems (WebSocket streaming)
- Need to verify which pages are working
- May need to update API endpoints

**Files to Review:**

- `web-ui/monitoring-server-v3.js` - Main server, API endpoints
- `web-ui/public/summary.html` - Main dashboard
- `web-ui/public/ionosphere.html` - Ionospheric data page (if exists)
- `web-ui/science-api-endpoints.js` - Science data APIs (created but may need integration)

**Service Status:**

```bash
sudo systemctl status timestd-web-ui
journalctl -u timestd-web-ui -n 50
```

### Investigation Steps for Next Session

1. **Verify Web UI Service**
   - Check if `timestd-web-ui` is running
   - Review logs for errors
   - Test basic page access

2. **Review Existing API Structure**
   - Check `monitoring-server-v3.js` for existing patterns
   - Identify where to add TEC endpoint
   - Review how other science data is served

3. **Test TEC Data Access**
   - Verify CSV files exist and are readable
   - Test parsing TEC CSV in Node.js
   - Ensure TimeStdPaths pattern is followed

4. **Create TEC Visualization**
   - Use existing chart libraries (likely Plotly or Chart.js)
   - Follow existing page patterns
   - Add to navigation menu

### Relevant Commands

```bash
# Check web UI service
sudo systemctl status timestd-web-ui
journalctl -u timestd-web-ui -f

# Check TEC data
ls -lh /var/lib/timestd/phase2/science/tec/
tail /var/lib/timestd/phase2/science/tec/tec_$(date +%Y%m%d).csv

# Check Science Aggregator
sudo systemctl status timestd-science-aggregator
journalctl -u timestd-science-aggregator -n 20

# Test web UI access
curl http://localhost:3000/
curl http://localhost:3000/api/v1/channels
```

---

## Key Configuration

- **Config**: `/etc/hf-timestd/timestd-config.toml`
- **Data Root**: `/var/lib/timestd/`
- **Hot Buffer**: `/dev/shm/timestd/raw_buffer/`
- **TEC Data**: `/var/lib/timestd/phase2/science/tec/`
- **Logs**: `journalctl -u timestd-core-recorder` / `timestd-analytics` / `timestd-science-aggregator` / `timestd-web-ui`

## Channel Specifications (config.toml)

9 channels: 2.5, 5, 10, 15, 20, 25 MHz (WWV/WWVH) + 3.33, 7.85, 14.67 MHz (CHU)  
All use: `preset=iq`, `sample_rate=20000`, `agc=0`, `gain=0`, `encoding=F32`

## Important Patterns

### Path Management (v3.1.0+)

- ✅ **Always use TimeStdPaths** - Import from `hf_timestd.paths`
- ✅ **Use discovery methods** - `discover_phase2_channels()`, `get_clock_offset_dir()`
- ✅ **Never hard-code paths** - Let TimeStdPaths handle all path construction
- ❌ **Don't manually construct paths** - Leads to field name mismatches

### Channel Management (v3.0.1+)

- ✅ **Always discover before creating** - Check for existing channels
- ✅ **Match by freq/preset/rate** - Reuse when possible
- ✅ **Health monitoring enabled** - Auto-recovery from radiod restarts
- ❌ **Never** manually manage channels - let discovery handle it

### TEC Data (v3.1.0+)

- **Generation**: Science Aggregator runs every 5 minutes
- **Storage**: `/var/lib/timestd/phase2/science/tec/tec_YYYYMMDD.csv`
- **Stations**: WWV, WWVH, CHU (BPM when available)
- **Cadence**: 1-minute measurements, 3-6 frequencies per station
- **Optional**: GPS validation (not required for operation)
