# WEB-API CONTRACT — hf-timestd

**Version:** 1.0.0
**Last Updated:** 2026-02-23
**Status:** Active — evolves with implementation

---

## 1. Goal

Ensure the web API and dashboard provide **accurate, responsive, well-structured visualization and data access** for all pipeline products, with proper loading/error states and honest representation of data quality.

### Performance Objectives

- **API response time**: <2 seconds for all dashboard endpoints; <5 seconds for large time-range queries
- **Data freshness**: dashboard reflects measurements within 2 minutes of HDF5 write
- **Availability**: systemd watchdog heartbeat (`WATCHDOG=1`); auto-restart on hang
- **Coverage**: every HDF5 data product has at least one API endpoint and one dashboard visualization

### Deliverable Products

| Dashboard Page | URL | Primary Data Source |
|---------------|-----|-------------------|
| Station Overview | `/static/index.html` | System metadata, recent activity |
| System Health | `/static/health.html` | Process status, uptime |
| Metrology Dashboard | `/static/metrology.html` | Fusion timing, ADEV, per-channel D_clock |
| Carrier Phase / Doppler | `/static/phase.html` | tick_phase HDF5 (unwrapped phase, Doppler, σ_φ) |
| Propagation Analysis | `/static/ionosphere.html` | Mode timeline, multi-freq comparison, per-path TEC |
| dTEC Overlay | `/static/dtec.html` | Multi-station dTEC time series with SNR filtering |
| Ionogram | `/static/ionogram.html` | Griffin-style ToF vs SNR scatter with KDE contours |
| Allan Deviation | `/static/stability.html` | ADEV at standard τ values |
| Timing Validation | `/static/timing-validation.html` | GPS ground truth comparison |
| GRAPE | `/static/grape.html` | Spectrogram status, PSWS upload |
| Test Signal | `/static/test_signal.html` | WWV/WWVH :08/:44 channel sounding |
| System Logs | `/static/logs.html` | Real-time journalctl viewer |
| Living Docs | `/static/docs.html` | Documentation with live evidence |
| API Docs | `/api/docs` | Interactive Swagger/OpenAPI |

### API Endpoint Groups

| Router | Prefix | Key Endpoints |
|--------|--------|--------------|
| `dashboard.py` | `/api/dashboard/` | `/summary`, `/24h/{channel}`, `/channels` |
| `metrology.py` | `/api/metrology/` | `/measurements`, `/summary` |
| `phase.py` | `/api/phase/` | `/timeseries`, `/doppler`, `/scintillation`, `/summary` |
| `propagation.py` | `/api/propagation/` | `/conditions`, `/timeline/{station}`, `/model/predict` |
| `tec.py` | `/api/tec/` | `/current`, `/history`, `/dtec` |
| `stability.py` | `/api/stability/` | `/adev` |
| `correlations.py` | `/api/correlations/` | `/solar`, `/space-weather` |
| `health.py` | `/api/health` | System health check |
| `logs.py` | `/api/logs/` | Real-time log streaming |
| `stations.py` | `/api/stations/` | Per-station status and metrics |
| `ionogram.py` | `/api/ionogram/` | ToF clusters, KDE contours |
| `space_weather.py` | `/api/space-weather/` | GOES X-ray, Kp, proton flux |
| `docs.py` | `/api/living-docs/` | Live evidence for documentation |
| `timing_validation.py` | `/api/timing-validation/` | GPS ground truth comparison |

### Verification Steps

1. `curl http://localhost:8000/api/health | python3 -m json.tool` — returns 200 with service status
2. `curl http://localhost:8000/api/dashboard/summary | python3 -m json.tool` — returns channel data within 2 seconds
3. All 14+ static HTML pages load without JavaScript errors (check browser console)
4. Navigation links present and consistent across all HTML pages
5. Charts render with data points (not empty) when HDF5 files contain recent data
6. Loading spinners shown during data fetch; error messages shown on failure

---

## 2. Constraints

### Technology Stack

- **Framework**: FastAPI with uvicorn ASGI server
- **Port**: 8000
- **Static files**: served via `StaticFiles` mount from `static/` directory
- **Charts**: Chart.js (client-side rendering)
- **Styling**: vanilla CSS (no framework — keep consistent with existing pages)
- **Icons**: none currently — do not introduce icon libraries without discussion
- **No build step**: all static files are plain HTML/JS/CSS, no bundler or transpiler

### Deployment Architecture

- **Production path**: `/opt/hf-timestd/web-api/` (WorkingDirectory in systemd unit)
- **Git repo path**: `/home/mjh/git/hf-timestd/web-api/`
- **Sync**: `scripts/update-production.sh` copies repo → production
- **Static file changes**: file copy only (no restart needed for StaticFiles)
- **Python changes** (routers, services, main.py): require service restart
- **Core library changes** (`src/hf_timestd/`): require `pip install -e` + service restart

### Data Access Patterns

- All data read from HDF5 files in `/var/lib/timestd/phase2/`
- **All `h5py.File()` calls must use `locking=False`**
- Services layer (`web-api/services/`) encapsulates HDF5 reads — routers must not read HDF5 directly
- Numpy types (int64, float64) must be cast to Python native types before JSON serialization — use `_safe_float()` / `_safe_int()` helpers
- Large HDF5 reads: prefer tail reads (last N rows) over full `read_time_range()` for real-time dashboards
- ISO timestamps: use `strftime('%Y-%m-%dT%H:%M:%SZ')`, never `isoformat() + 'Z'`

### Navigation Consistency

- Every static HTML page must include the same navigation bar with links to all other pages
- When adding a new page: add nav link to **all existing pages** (currently 14+)
- When removing a page: remove nav link from **all existing pages**

### API Response Format

```json
{
    "status": "ok" | "error" | "no_data",
    "data": { ... },
    "timestamp": "2026-02-23T22:00:00Z",
    "error": "description if status=error"
}
```

- All endpoints return JSON
- HTTP 200 for success (even if no data — use `status: "no_data"`)
- HTTP 500 for unhandled errors
- HTTP 404 for unknown routes

### Living Documentation

- Markdown docs contain `<!-- LOGS: source | filter: "pattern" -->` directives
- Backend searches log files, falls back to journalctl
- Evidence is fetched from the **local installation**, not hardcoded
- Sources: bootstrap, fusion, physics, TEC, L1-L2, metrology

---

## 3. Format

### Dashboard Chart Standards

- **Time axis**: UTC, formatted as `HH:MM` for intraday, `MM-DD` for multi-day
- **D_clock**: milliseconds, y-axis centered on 0 with ±range auto-scaled
- **SNR**: dB, y-axis 0–50 dB typical range
- **Doppler**: Hz, y-axis auto-scaled
- **Phase**: radians (unwrapped for time series, wrapped for scatter)
- **dTEC**: TECU/s or mTECU, clearly labeled
- **Color coding**: per-station (not per-frequency) unless frequency is the variable of interest
- **Error bars / uncertainty**: show when available (shaded region or whiskers)

### Status Indicators

| Condition | Display |
|-----------|---------|
| Data fresh (<5 min) | Green indicator |
| Data stale (5–30 min) | Yellow indicator |
| Data missing (>30 min) | Red indicator |
| Service running | Green badge |
| Service stopped | Red badge |

### Quality Honesty

- Products that are ❌ (below noise floor, all NaN) must be clearly labeled in the UI
- Do not display `tec_tecu` as if it is a reliable measurement — show with caveat or hide
- `dtec_mean_tecu` must be labeled "relative" or "unanchored" when `is_anchored=False`
- Detection limit analysis should be accessible from relevant dashboard pages

### Logging

- API request errors logged at WARNING with endpoint path and error message
- HDF5 read failures logged at WARNING (not DEBUG — prevents silent starvation)
- Slow queries (>5 seconds) logged at WARNING with timing

---

## 4. Failure Conditions

- **Reading HDF5 directly in router code** — must go through services layer
- **Returning numpy types in JSON responses** — causes serialization errors; must cast with `_safe_float()` / `_safe_int()`
- **Missing loading states** — every chart must show a spinner or "Loading..." during data fetch
- **Missing error states** — every chart must show an error message if the API call fails
- **Inconsistent navigation** — adding a page without updating nav links on all other pages
- **Stale data without indication** — dashboard must show freshness indicators
- **Overstating data quality** — displaying noise-dominated products (group-delay TEC, VTEC) without caveats
- **Using `isoformat() + 'Z'`** for timestamps — causes double-timezone suffix; use `strftime`
- **Full HDF5 table scans in real-time endpoints** — causes timeouts; use tail reads
- **Editing files in git repo without syncing to `/opt/hf-timestd/`** — changes won't appear in production
- **Python changes without service restart** — only static file changes are hot-reloaded
- **Breaking the systemd watchdog** — `WATCHDOG=1` heartbeat must continue; long-blocking operations must not starve the main loop
- **Introducing new JS/CSS frameworks** without discussion — maintain consistency with existing vanilla approach
- **Empty charts with no explanation** — if no data exists for a time range, show "No data available for this period"
