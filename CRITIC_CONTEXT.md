# NEVER CHANGE THE FOLLOWING PRIMARY INSTRUCTION!!!

**Primary Instruction:** In this context you will perform a critical review of the HF Time Standard (hf-timestd) project, either in its entirety or in a specific component, as specified by the user. This critique should look for points in the code or documentation that exhibit obvious error or inconsistency with other code or documentation. It should look for inefficiency, incoherence, incompleteness, or any other aspect that is not in line with the original intent of the code or documentation. It should also look for obsolete, deprecated, or "zombie" code that should be removed. Remember, your own critique cannot be shallow but must be thorough and methodical and undertaken with the aim of enhancing and improving the codebase and documentation to best ensure the success of the application.

Make your criticism from the perspective of 1) a user of the system, 2) a metrologist, 3) a ionospheric scientist, and 4) a software engineer. These perspectives can differ in their priorities and interests, and your critique should reflect this. For example, a user of the system will be most interested in the accuracy and reliability of the system, while a metrologist will be most interested in the precision and traceability of the system, while an ionospheric scientist will be most interested in the ionospheric conditions and their impact on the system, and a software engineer will be most interested in the code quality, maintainability, coverage and adequacy of testing,and the resilience and robustness of the system over time and in edge cases. Ultimately, however, a consensus of these perspectives should guide your critique in service of the meeting the application's objectives.

# NEVER CHANGE THE PRECEEDING PRIMARY INSTRUCTION!!!

# The following secondary instruction and information will guide your critique in this particular session (the instructions below will vary from session to session)

---

## 📋 NEXT SESSION: WEB-API UI REVIEW + TIMING ACCURACY CRISIS

**Objective:** Carefully review the web-api UI pages, especially the Timing (metrology.html) and Validation (timing-validation.html) pages. The validation page data shows the system is failing catastrophically at its core metrology objective of recovering UTC from HF time standard signals. Diagnose the root cause and propose fixes.

---

## 🚨 CRITICAL: Validation Page Shows Catastrophic Failure

### Live Data (2026-02-06 18:46 UTC)

The `/api/timing-validation/dashboard` endpoint returns:

| Metric | Value | Expected |
|--------|-------|----------|
| **Mean discrepancy** | **-495 ms** | < 1 ms |
| **Std deviation** | 397 ms | < 1 ms |
| **Within 1ms** | **0.0%** | > 95% |
| **Within 5ms** | **0.0%** | > 90% |
| **Within uncertainty** | **0.0%** | > 68% |
| **Grade distribution** | **100% Grade D** | Mostly A/B |
| **fusion_d_clock_ms** | **Always 0.0** | Varies |
| **gps_d_clock_ms** | 211–1151 ms | ~0 |

### The Fundamental Problem

The fusion reports `d_clock_ms = 0.0` for every minute. This is **by design** — the calibration model in `multi_broadcast_fusion.py` computes:

```
calibration_offset_station = -mean(D_clock_station)
```

This forces each station's mean D_clock to zero, which is supposed to represent UTC(NIST). But the GPS ground truth (via RTP timing snapshots from the GPSDO-locked radiod) shows the actual clock offset is **hundreds of milliseconds**, not zero.

**The calibration is circular:** It assumes the mean of HF measurements IS UTC, then reports the deviation from that mean. It never anchors to an absolute time reference. The GPS/PPS data available via `timing_snapshots` (482 per minute!) provides that anchor but is not used in the fusion.

### Root Cause Chain

1. **Metrology layer** produces `d_clock_ms` = (detected_arrival - expected_arrival) for each broadcast
2. **Expected arrival** = second boundary + propagation_delay (from IRI model)
3. **Fusion calibration** zeros out the mean of these measurements per station
4. **Result:** Fusion always reports ~0 ms offset, regardless of actual system clock error
5. **GPS validation** compares fusion (always ~0) against GPS ground truth (real offset) → massive discrepancy

### What the Validation Page Should Show (If Working)

If the system were correctly recovering UTC:
- `fusion_d_clock_ms` would track the actual clock offset (matching GPS)
- Discrepancy (fusion - GPS) would be small (< 1ms ideally, < 5ms acceptable)
- Quality grades would be A/B for most points

---

## 🔍 WEB-API UI PAGES TO REVIEW

### Page Inventory (13 HTML pages)

| Page | File | API Endpoints | Focus |
|------|------|---------------|-------|
| **Overview** | `index.html` | Various | Landing page |
| **Health** | `health.html` | `/api/health/*` | Service status |
| **Timing** | `metrology.html` | `/api/metrology/fusion/latest` | UTC offset display |
| **Validation** | `timing-validation.html` | `/api/timing-validation/dashboard` | GPS comparison |
| **Stability** | `stability.html` | `/api/stability/*` | Allan deviation |
| **Ionosphere** | `propagation.html` | `/api/propagation/*` | Propagation paths |
| **TEC/TID** | `physics.html` | `/api/physics/*` | Ionospheric science |
| **Solar** | `solar-correlation.html` | `/api/correlations/*` | Solar effects |
| **24h Dashboard** | `dashboard-24h.html` | `/api/dashboard/*` | All broadcasts |
| **Station** | `station.html` | `/api/stations/*` | Per-station detail |
| **Test Signal** | `test_signal.html` | Various | WWV/WWVH test signals |
| **Docs** | `docs.html` | `/api/docs/*` | Living documentation |
| **Logs** | `logs.html` | `/api/logs/*` | System logs |

### Key Review Questions for Each Page

**Timing page (metrology.html):**
- Shows "Current UTC Offset" hero metric with `d_clock_ms` — currently always 0.0
- Is this misleading? The "How It Works" section describes the methodology
- Does the page clearly communicate uncertainty and quality?

**Validation page (timing-validation.html):**
- Charts: Discrepancy over time, grade distribution (doughnut), histogram
- Table: Recent validation points with pass/fail status
- Currently shows all-red, all-fail — is this page correctly interpreting the data?
- Is the methodology note accurate about what's being compared?

**General UI concerns:**
- Navigation consistency across all 13 pages
- Dark theme styling consistency
- Auto-refresh behavior (60s intervals)
- Error handling when APIs return no data
- Mobile responsiveness

---

## 📊 Session Accomplishments (2026-02-06)

### Infrastructure Fixes (This Session)

1. **HDF5 Crash-Safe Writer** — Eliminated SWMR mode entirely:
   - **Root cause:** SWMR leaves dirty consistency flags on unclean shutdown (SIGKILL, os._exit). Any service restart could corrupt HDF5 files.
   - **Fix:** Open-write-close per measurement. No persistent file handle = no dirty flags on crash.
   - **Files:** `src/hf_timestd/io/hdf5_writer.py` (major refactor), `src/hf_timestd/io/hdf5_reader.py` (removed swmr=True)
   - **Verified:** All 9 metrology channels + fusion + TEC + VTEC passing in `verify_pipeline.sh`

2. **SWMR References Cleaned** — Removed all SWMR usage from:
   - `hdf5_writer.py` — No longer opens SWMR write mode
   - `hdf5_reader.py` — No longer opens with `swmr=True`
   - `timing_validation_service.py` — Simplified file open
   - `multi_broadcast_fusion.py` — Updated comments
   - `core/__init__.py`, `physics_service.py` — Updated docstrings

3. **Production Deployment** — All services restarted with new code, verified clean.

### Previous Session Accomplishments (Carried Forward)

- **24-Hour Dashboard** — Visualization of all 17 broadcasts
- **Editable install fix** — Production no longer symlinks to git repo
- **HDF5 recovery logic** — h5clear + corrupt file rename (now superseded by crash-safe design)
- **CPU affinity** — radiod confined to cache-sharing cores

---

## 🏗️ Architecture Reference

### Data Flow

```
ka9q-radio (radiod) → RTP multicast → timestd-core-recorder → Raw IQ Buffer
                                                                    ↓
                                                          timestd-metrology
                                                           ↓ (per channel)
                                                    AM Demod → Matched Filter
                                                           ↓
                                                    L1 Metrology (HDF5)
                                                           ↓
                                                    L2 Calibrated (HDF5)
                                                           ↓
                                                    timestd-fusion
                                                    ↓              ↓
                                             L3 Fusion (HDF5)   Chrony SHM
                                                    ↓
                                             timestd-physics
                                                    ↓
                                             TEC/Science (HDF5)
```

### Key Files

| File | Purpose |
|------|---------|
| `src/hf_timestd/core/multi_broadcast_fusion.py` | Fusion engine (~4900 lines) |
| `src/hf_timestd/core/metrology_service.py` | Per-channel metrology |
| `src/hf_timestd/core/timing_validation_service.py` | GPS comparison logic |
| `src/hf_timestd/io/hdf5_writer.py` | Crash-safe HDF5 writer |
| `src/hf_timestd/io/hdf5_reader.py` | HDF5 reader |
| `web-api/routers/timing_validation.py` | Validation API |
| `web-api/static/timing-validation.html` | Validation UI |
| `web-api/static/metrology.html` | Timing UI |
| `scripts/verify_pipeline.sh` | Pipeline health check |
| `scripts/update-production.sh` | Deployment (restarts 6 of 8 services) |
| `scripts/start-services.sh` | Full start (all 8 + timers) |
| `scripts/stop-services.sh` | Full stop (all 8 + timers) |

### Service Inventory

| Service | Restarts on Update | Purpose |
|---------|-------------------|---------|
| `timestd-core-recorder` | ❌ (data gap risk) | RTP → raw buffer |
| `timestd-metrology` | ✅ | IQ → L1/L2 measurements |
| `timestd-l2-calibration` | ✅ | L2 calibration |
| `timestd-fusion` | ✅ | Multi-broadcast fusion → Chrony |
| `timestd-physics` | ✅ | TEC estimation |
| `timestd-web-api` | ✅ | REST API + dashboard |
| `timestd-radiod-monitor` | ✅ | Hardware health |
| `timestd-vtec` | ✅ (if running) | GNSS VTEC |

---

## 🔬 Methodology Questions (Carried Forward)

1. **Circular calibration:** The fusion zeros out mean D_clock per station. This makes it impossible to measure absolute clock offset. The GPS timing snapshots provide an absolute reference — should calibration anchor to GPS instead of self-referencing?

2. **Template matching:** CHU template is 500ms but CHU transmits a 300ms tone at most seconds. WWV template is 800ms. Are these optimal?

3. **Propagation model:** IRI-2020 gives ionospheric layer heights. Is the path delay computation correct? Is "minimum propagation delay" (great circle / c) the right baseline?

4. **GPS_TIME/RTP_TIMESNAP mapping:** radiod provides GPS↔RTP timestamp pairs. Is there pipeline latency in this mapping? The ~200-1000ms discrepancies suggest a fundamental timing reference error, not just propagation model inaccuracy.

5. **Missed opportunities:**
   - CHU FSK timing (seconds 31-39) for independent verification
   - Phase tracking for sub-sample resolution
   - Doppler shift for ionospheric correction

---

## ✅ Success Criteria for Next Session

- ⬚ **Review all web-api UI pages** for correctness, consistency, and usability
- ⬚ **Diagnose why fusion_d_clock_ms is always 0.0** — Is this the calibration design or a bug?
- ⬚ **Diagnose why gps_d_clock_ms is 200-1000ms** — Is this a real offset or a timing reference error?
- ⬚ **Propose a path to sub-millisecond accuracy** — What needs to change in the methodology?
- ⬚ **Fix any UI bugs** found during the review
