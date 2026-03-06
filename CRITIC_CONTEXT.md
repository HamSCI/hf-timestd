# NEVER CHANGE THE FOLLOWING PRIMARY INSTRUCTION!!!

**Primary Instruction:** In this context you will perform a critical review of the HF Time Standard (hf-timestd) project, either in its entirety or in a specific component, as specified by the user. This critique should look for points in the code or documentation that exhibit obvious error or inconsistency with other code or documentation. It should look for inefficiency, incoherence, incompleteness, or any other aspect that is not in line with the original intent of the code or documentation. It should also look for obsolete, deprecated, or "zombie" code that should be removed. Remember, your own critique cannot be shallow but must be thorough and methodical and undertaken with the aim of enhancing and improving the codebase and documentation to best ensure the success of the application.

Make your criticism from the perspective of 1) a user of the system, 2) a metrologist, 3) a ionospheric scientist, and 4) a software engineer. These perspectives can differ in their priorities and interests, and your critique should reflect this. For example, a user of the system will be most interested in the accuracy and reliability of the system, while a metrologist will be most interested in the precision and traceability of the system, while an ionospheric scientist will be most interested in the ionospheric conditions and their impact on the system, and a software engineer will be most interested in the code quality, maintainability, coverage and adequacy of testing, and the resilience and robustness of the system over time and in edge cases. Ultimately, however, a consensus of these perspectives should guide your critique in service of meeting the application's objectives.

# NEVER CHANGE THE PRECEEDING PRIMARY INSTRUCTION!!!

# The following secondary instruction and information will guide your critique in this particular session (the instructions below will vary from session to session)

---

## NEXT SESSION: WEB-API DASHBOARD AUDIT — DEMO-READY BY HAMSCI WORKSHOP

**Goal:** Systematically review every page of the web-api dashboard, fix broken pages, standardize time/date selection controls, improve plot readability, ensure data actually flows to every page, and update the living documentation. The system must be demo-ready for a live presentation at the HamSCI 2026 workshop (next weekend, around March 15, 2026).

**Approach:** Work through pages one at a time. For each page: verify the backend router returns data, verify the frontend renders it, fix any issues, then move on. Apply the consistency standards documented below.

---

## System Context

- **System:** hf-timestd v6.9.2 (March 6, 2026)
- **Focus Area:** `web-api/` — FastAPI backend (`web-api/routers/*.py`) + static HTML/JS frontend (`web-api/static/*.html`)
- **Deadline:** Demo-ready by approximately March 14, 2026
- **Recent change (2026-03-06):** Archived dead code from `wwvh_discrimination.py` (3918 to 1237 lines). No web-api impact.
- **Recent change (2026-03-04):** RTP timing mismatch fix. May have affected data continuity for some pages.

---

## 1. Page Inventory and Known Issues

There are **14 dashboard pages** served from `web-api/static/`. The table below summarizes the current state of each, based on a code audit performed on 2026-03-06.

| # | Page | File | Router | Chart Lib | common.js | Time Selection | Known Issues |
|---|------|------|--------|-----------|-----------|----------------|--------------|
| 1 | Overview | `index.html` | `dashboard.py` | none | YES | None (live only) | OK |
| 2 | Health | `health.html` | `health.py` | none | YES | None (live only) | OK |
| 3 | Timing | `metrology.html` | `metrology.py` | Plotly | YES | Preset buttons + datetime-local custom | Best UI — use as reference |
| 4 | Validation | `timing-validation.html` | `timing_validation.py` | **Chart.js** | **NO** | hours param (points selector) | **LIKELY BROKEN** — depends on singleton service; uses Chart.js not Plotly |
| 5 | Chrony | `chrony.html` | `chrony.py` | none | YES | time-btn (1h/6h/24h) | No custom date picker |
| 6 | Phase | `phase.html` | `phase.py` | Plotly | YES | time-btn (15m/1h/6h/24h) relative only | **NO DATE PICKER** — cannot view historical data |
| 7 | Observatory | `ionosphere.html` | `physics.py` | Plotly | **NO** | Custom date-picker | Own date picker implementation; no common.js |
| 8 | Ionogram | `ionogram.html` | `ionogram.py` | Plotly | **NO** | datetime-local start/end | No common.js |
| 9 | dTEC | `dtec.html` | `tec.py` | Plotly | **NO** | datetime-local start/end | No common.js; dTEC data may be degraded |
| 10 | Test Signal | `test_signal.html` | (inline) | Plotly | YES | date-nav + date picker + time-range bar | Unique elaborate UI; plot readability unclear |
| 11 | GRAPE | `grape.html` | `grape.py` | none | YES | Date dropdown from API | Depends on GRAPE data availability |
| 12 | Docs | `docs.html` | `docs.py` | Plotly | **NO** | None (document viewer) | **LIVING DOCS NEED UPDATING** |
| 13 | Logs | `logs.html` | `logs.py` | none | YES | datetime-local start/end | Functional as-is |
| 14 | Station | `station.html` | `stations.py` | **Chart.js+Plotly** | **NO** | Fixed hours=24 | Uses BOTH Chart.js and Plotly; hardcoded 24h |

---

## 2. Consistency Standards to Apply

### 2.1 Time Selection Widget

**Reference implementation:** `metrology.html` (Fusion History section, lines 229-248)

Every page displaying time-series data should have:
1. **Preset buttons:** 1h, 6h, 24h, 7d (at minimum)
2. **Custom date range:** Two `datetime-local` inputs with a Go button
3. **Active button highlighting:** Blue (#3b82f6) background for selected preset
4. **CSS class:** `.time-range-btn` (already defined in metrology.html)

Pages that do NOT need time selection: `index.html`, `health.html`, `docs.html`.

### 2.2 Charting Library

**Standard:** Plotly.js 2.27.0

All charting should use Plotly with this dark theme layout:
```javascript
const darkLayout = {
    paper_bgcolor: '#1e293b',
    plot_bgcolor: '#0f172a',
    font: { color: '#e0e0e0', size: 12 },
    xaxis: { gridcolor: '#334155', linecolor: '#475569', tickformat: '%H:%M' },
    yaxis: { gridcolor: '#334155', linecolor: '#475569' },
    margin: { l: 60, r: 30, t: 10, b: 40 },
    legend: { bgcolor: 'rgba(30,41,59,0.8)', font: { color: '#e0e0e0' } },
    hovermode: 'x unified',
};
```

**Action required:** Migrate `timing-validation.html` and `station.html` from Chart.js to Plotly.

### 2.3 common.js Adoption

**Standard:** Every page should include `<script src="/static/js/common.js"></script>` and use the `api` global for API calls.

Currently **missing from 6 pages:** timing-validation, ionosphere, ionogram, dtec, docs, station.

Benefits: consistent error handling, `timeAgo()`, `AutoRefresh` class, `showError()`/`clearError()`.

### 2.4 Navigation Bar

The nav bar is consistent across pages (good). One minor issue:
- Both Phase and Test Signal use the same emoji icon — differentiate them.

### 2.5 Plot Readability (Demo Projection)

- **Font size:** Minimum 12px for axis labels, 14px for titles
- **Line width:** Minimum 1.5px for data traces
- **Legend:** Always visible (not hidden behind hover)
- **Axis labels:** Always present with units
- **Zero lines:** Show for Doppler, dTEC/dt, and discrepancy plots

---

## 3. Page-by-Page Audit Procedure

For each page, execute these steps:

### Step A: Backend Verification
```bash
curl -s http://localhost:8000/api/<endpoint> | python3 -m json.tool | head -30
```
Verify: HTTP 200, contains data, fields match what frontend expects.

### Step B: Frontend Verification
1. Does data appear? (Not "Loading..." forever, not "No data")
2. Do all plots render?
3. Do time selection controls work?
4. Does auto-refresh work?
5. Is the nav bar correct with current page highlighted?

### Step C: Apply Consistency Standards
1. Add common.js if missing
2. Replace Chart.js with Plotly if applicable
3. Add standard time selection widget if missing
4. Apply dark theme layout to all Plotly charts
5. Ensure axis labels and units are present

### Step D: Fix Any Data Issues
If a page shows no data, trace the pipeline: router endpoint -> service layer -> HDF5 data files.

---

## 4. Prioritized Work Order

### Priority 1 — Core Demo Pages (must work perfectly)
1. **metrology.html** — Flagship page. Verify D_clock, fusion history, Allan deviation. Already best UI.
2. **phase.html** — Add date picker for historical data. Currently relative-only.
3. **dtec.html** — Verify dTEC data flows post-RTP-fix. Add common.js.
4. **ionogram.html** — Verify arrival patterns. Add common.js. Standardize time controls.

### Priority 2 — Supporting Demo Pages (should work)
5. **timing-validation.html** — Likely broken. Migrate Chart.js to Plotly. Add common.js. Verify service running.
6. **ionosphere.html** — Verify observatory data. Add common.js.
7. **chrony.html** — Add date picker alongside existing time buttons.
8. **test_signal.html** — Review plot readability. Standardize time controls.

### Priority 3 — Nice-to-Have Pages
9. **station.html** — Migrate Chart.js to Plotly. Add common.js.
10. **grape.html** — Verify GRAPE data. Low priority unless demo planned.
11. **health.html** — Verify health checks. No time selection needed.
12. **index.html** — Verify station cards. No time selection needed.

### Priority 4 — Documentation
13. **docs.html** — Update living documentation content. Add common.js.
14. **logs.html** — Functional as-is. Low priority.

---

## 5. Key Files

### Frontend
| File | Role |
|------|------|
| `web-api/static/js/common.js` (214 lines) | Shared API client, formatters, AutoRefresh — adopt everywhere |
| `web-api/static/css/styles.css` | Shared stylesheet — already used by all pages |
| `web-api/static/*.html` (14 files) | Individual page implementations |

### Backend Routers
| File | Prefix | Serves |
|------|--------|--------|
| `web-api/routers/dashboard.py` | `/api/dashboard` | index.html |
| `web-api/routers/health.py` | `/api/health` | health.html |
| `web-api/routers/metrology.py` | `/api/metrology` | metrology.html |
| `web-api/routers/timing_validation.py` | `/api/timing-validation` | timing-validation.html |
| `web-api/routers/chrony.py` | `/api/chrony` | chrony.html |
| `web-api/routers/phase.py` | `/api/phase` | phase.html |
| `web-api/routers/physics.py` | `/api/physics` | ionosphere.html |
| `web-api/routers/ionogram.py` | `/api/ionogram` | ionogram.html |
| `web-api/routers/tec.py` | `/api/tec` | dtec.html |
| `web-api/routers/grape.py` | `/api/grape` | grape.html |
| `web-api/routers/docs.py` | `/api/living-docs` | docs.html |
| `web-api/routers/logs.py` | `/api/logs` | logs.html |
| `web-api/routers/stations.py` | `/api/stations` | station.html |
| `web-api/routers/station.py` | `/api/station` | station.html (alt?) |

**Note:** Two station routers exist (`station.py` and `stations.py`). Investigate whether both are needed.

### Routers Without Dedicated Pages
| File | Prefix | Used By |
|------|--------|---------|
| `web-api/routers/correlations.py` | `/api/correlations` | No dedicated page |
| `web-api/routers/propagation.py` | `/api/propagation` | docs.html live widgets |
| `web-api/routers/space_weather.py` | `/api/space-weather` | dtec.html overlays |
| `web-api/routers/stability.py` | `/api/stability` | metrology.html (Allan deviation) |
| `web-api/routers/tid.py` | `/api/tid` | dtec.html |

---

## 6. Living Documentation Update Checklist

The `docs.html` page renders markdown from `/api/living-docs/`. Review:

1. **System architecture** — Verify it reflects current codebase (TickEdgeDetector is now primary, not TickMatchedFilter)
2. **Measurement methodology** — Verify L1/L2/L3 layer descriptions are current
3. **Live widgets** — Verify inline data widgets are wired to working endpoints
4. **Dead code references** — Remove references to archived voting pipeline (moved to `core/legacy/`)

---

## 7. What "Demo-Ready" Looks Like

A successful live demo should be able to:

1. Show the **overview page** with all 4 stations reporting active channels
2. Show the **Timing page** with live D_clock, fusion history (24h), and Allan deviation
3. Navigate to **Phase** and show carrier phase and Doppler for a selected channel with date navigation
4. Navigate to **dTEC** and show ionospheric TEC variation
5. Navigate to **Ionogram** and show arrival patterns with mode identification
6. Navigate to **Validation** and show timing accuracy against GPS ground truth
7. **All pages** load without broken links, missing data, or stuck loading spinners
8. **All plots** readable on a projected screen (font sizes, contrast, labels)

---

## 8. Quick Diagnostic Commands

```bash
# Check if web-api is running
curl -s http://localhost:8000/api/health | python3 -m json.tool

# Check specific endpoints that may be broken
curl -s http://localhost:8000/api/timing-validation/dashboard?hours=1 | python3 -m json.tool | head -20
curl -s http://localhost:8000/api/phase/summary | python3 -m json.tool | head -20
curl -s http://localhost:8000/api/tec/dtec | python3 -m json.tool | head -20

# Check data directories have recent files
ls -lt /var/lib/timestd/phase2/science/dtec/ | head -3
ls -lt /var/lib/timestd/phase2/CHU_7850/tick_phase/ | head -3
ls -lt /var/lib/timestd/data/fusion/ | head -3
```
