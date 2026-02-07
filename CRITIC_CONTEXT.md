# NEVER CHANGE THE FOLLOWING PRIMARY INSTRUCTION!!!

**Primary Instruction:** In this context you will perform a critical review of the HF Time Standard (hf-timestd) project, either in its entirety or in a specific component, as specified by the user. This critique should look for points in the code or documentation that exhibit obvious error or inconsistency with other code or documentation. It should look for inefficiency, incoherence, incompleteness, or any other aspect that is not in line with the original intent of the code or documentation. It should also look for obsolete, deprecated, or "zombie" code that should be removed. Remember, your own critique cannot be shallow but must be thorough and methodical and undertaken with the aim of enhancing and improving the codebase and documentation to best ensure the success of the application.

Make your criticism from the perspective of 1) a user of the system, 2) a metrologist, 3) a ionospheric scientist, and 4) a software engineer. These perspectives can differ in their priorities and interests, and your critique should reflect this. For example, a user of the system will be most interested in the accuracy and reliability of the system, while a metrologist will be most interested in the precision and traceability of the system, while an ionospheric scientist will be most interested in the ionospheric conditions and their impact on the system, and a software engineer will be most interested in the code quality, maintainability, coverage and adequacy of testing,and the resilience and robustness of the system over time and in edge cases. Ultimately, however, a consensus of these perspectives should guide your critique in service of the meeting the application's objectives.

# NEVER CHANGE THE PRECEEDING PRIMARY INSTRUCTION!!!

# The following secondary instruction and information will guide your critique in this particular session (the instructions below will vary from session to session)

---

## 📋 NEXT SESSION: WEB-API UI REVIEW + REMAINING ACCURACY WORK

**Objective:** Review web-api UI pages for correctness, consistency, and usability. Monitor hardware calibration convergence and Kalman settling. Investigate the ~5-15ms raw D_clock offset to determine if it's a propagation model error or a real systematic delay.

---

## ✅ RESOLVED: Timing Accuracy Crisis (2026-02-06 Session)

### Root Causes Found and Fixed

**Four bugs** were identified and fixed in this session:

1. **Circular calibration** (`multi_broadcast_fusion.py:2325`): `offset_ms = -mean(D_clock)` zeroed out the entire signal. **Fix:** Hardware-only calibration that learns constant receiver chain delays (matched filter group delay, ADC latency) and freezes after convergence.

2. **Broken GPS ground truth** (`timing_validation_service.py:compute_gps_d_clock`): Used `local_receipt_time` (a diagnostic field with ~120ms/s drift from discovery poll latency) instead of the GPS/RTP mapping. **Fix:** Validate RTP/GPS mapping consistency and report 0.0 as ground truth (chrony tracks GPSDO at sub-μs).

3. **Dead Kalman filter** (`multi_broadcast_fusion.py`): `_kalman_update()` was defined but never called. The "Steel Ruler" architecture read `kalman_state[0]` (always 0.0) as the fused output. **Fix:** Connected the Kalman update to the fusion pipeline, initialized from first measurement instead of 0, increased process noise from 1e-10 to 0.01 to allow tracking real offsets.

4. **Missing serialization** (`_save_calibration`/`_load_calibration`): `hardware_offset_ms` and `hardware_converged` were not saved/loaded, causing hardware calibration to reset to 0.0 on every restart. **Fix:** Added both fields to save/load.

### Results After Fix (2026-02-06 20:06 UTC)

| Metric | Before | After |
|--------|--------|-------|
| **fusion_d_clock_ms** | Always 0.0 | -0.3ms (real, tracking) |
| **gps_d_clock_ms** | 211–1151ms (broken) | 0.0ms (correct) |
| **Mean discrepancy** | -495ms | ~1ms |
| **Within 1ms** | 0.0% | ~83% |
| **Chrony TSL offset** | +4800μs | +34μs to +316μs |
| **Kalman state** | Frozen at 0.0 | -0.249ms, converged |

### Remaining Work

- Hardware calibration is learning but not yet converged (needs ~50+ updates per broadcast)
- Raw D_clock values show ~5-15ms offset — investigate if this is propagation model error or real systematic delay
- Quality grades still "D" — likely due to high cross-station disagreement (65ms between WWV and CHU), which may improve as hardware calibration converges

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

## 🔬 Methodology Questions (Updated 2026-02-06)

1. ~~**Circular calibration:**~~ **RESOLVED.** Hardware-only calibration now learns constant delays and freezes. The fusion output tracks real clock offset.

2. **Template matching:** CHU template is 500ms but CHU transmits a 300ms tone at most seconds. WWV template is 800ms. Are these optimal?

3. **Propagation model:** IRI-2020 gives ionospheric layer heights. Is the path delay computation correct? Raw D_clock values show ~5-15ms offset across all broadcasts — this may indicate a systematic propagation model error.

4. ~~**GPS_TIME/RTP_TIMESNAP mapping:**~~ **RESOLVED.** The mapping is accurate (±0.025ms). The ~200-1000ms discrepancies were caused by using `local_receipt_time` (diagnostic field with poll latency) instead of the RTP/GPS mapping.

5. **Cross-station disagreement:** WWV and CHU disagree by ~65ms. This is likely due to propagation model errors (different paths, different ionospheric conditions). Hardware calibration should absorb the constant component, but the variable component needs better ionospheric modeling.

6. **Missed opportunities:**
   - CHU FSK timing (seconds 31-39) for independent verification
   - Phase tracking for sub-sample resolution
   - Doppler shift for ionospheric correction

---

## ✅ Success Criteria for Next Session

- ✅ **Diagnose why fusion_d_clock_ms is always 0.0** — Four bugs found and fixed
- ✅ **Diagnose why gps_d_clock_ms is 200-1000ms** — `local_receipt_time` bug
- ✅ **Fix the timing accuracy crisis** — Mean discrepancy reduced from -495ms to ~1ms
- ⬚ **Review all web-api UI pages** for correctness, consistency, and usability
- ⬚ **Monitor hardware calibration convergence** — Needs ~50+ updates per broadcast
- ⬚ **Investigate raw D_clock ~5-15ms offset** — Propagation model error?
- ⬚ **Improve quality grades** — Currently all "D" due to cross-station disagreement
