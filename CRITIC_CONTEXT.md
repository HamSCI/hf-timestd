# NEVER CHANGE THE FOLLOWING PRIMARY INSTRUCTION!!!

**Primary Instruction:** In this context you will perform a critical review of the HF Time Standard (hf-timestd) project, either in its entirety or in a specific component, as specified by the user. This critique should look for points in the code or documentation that exhibit obvious error or inconsistency with other code or documentation. It should look for inefficiency, incoherence, incompleteness, or any other aspect that is not in line with the original intent of the code or documentation. It should also look for obsolete, deprecated, or "zombie" code that should be removed. Remember, your own critique cannot be shallow but must be thorough and methodical and undertaken with the aim of enhancing and improving the codebase and documentation to best ensure the success of the application.

Make your criticism from the perspective of 1) a user of the system, 2) a metrologist, 3) a ionospheric scientist, and 4) a software engineer. These perspectives can differ in their priorities and interests, and your critique should reflect this. For example, a user of the system will be most interested in the accuracy and reliability of the system, while a metrologist will be most interested in the precision and traceability of the system, while an ionospheric scientist will be most interested in the ionospheric conditions and their impact on the system, and a software engineer will be most interested in the code quality, maintainability, coverage and adequacy of testing, and the resilience and robustness of the system over time and in edge cases. Ultimately, however, a consensus of these perspectives should guide your critique in service of meeting the application's objectives.

# NEVER CHANGE THE PRECEEDING PRIMARY INSTRUCTION!!!

# The following secondary instruction and information will guide your critique in this particular session (the instructions below will vary from session to session)

---

## 📋 NEXT SESSION: TEC QUALITY + L2 SCHEMA

**Objective:** Two issues remain from the dashboard/tick_timing investigation:

### Issue 1: TEC outliers (max 3930 TECU)

The TEC pipeline produces physically unreasonable values. Today's data: 35.9% in 1–100 TECU range, high-confidence subset includes values up to 3662 TECU. CHU shows good diurnal pattern (26→93 TECU), but WWV/WWVH/BPM have frequent negative-slope rejections (mode mixing).

**Actions:**
- Add TEC validation bounds (cap at 200 TECU for mid-latitude)
- Investigate mode mixing: shared-frequency D_clock values may combine 1F and 2F arrivals
- Check if `propagation_mode` field is being used to gate TEC inputs

### Issue 2: L2 schema vs data inconsistency

The L2 schema says `clock_offset_ms = raw_arrival_time_ms - propagation_delay_ms`, but `l2_calibration_service.py:340` writes `d_clock_ms = raw_toa_ms` (which IS already D_clock from L1) to `clock_offset_ms`, and the same value to `raw_arrival_time_ms`. Both fields are identical in the HDF5 data. Either:
- Fix the L2 writer to store actual raw arrival time in `raw_arrival_time_ms` and computed D_clock in `clock_offset_ms`
- Or update the schema documentation to reflect reality

### Service Inventory

| Service | Purpose | Logs |
|---------|---------|------|
| `timestd-core-recorder` | RTP → raw buffer (authoritative timestamps) | journalctl |
| `timestd-metrology` | IQ → L1/L2 measurements + tick phase extraction | `/var/log/hf-timestd/phase2-*.log` |
| `timestd-fusion` | Multi-broadcast fusion → Chrony (WatchdogSec=120) | `/var/log/hf-timestd/fusion.log` |
| `timestd-physics` | TEC estimation, L3 physics products (+ tomography, VTEC) | `/var/log/hf-timestd/physics.log` |
| `timestd-l2-calibration` | L2 adaptive calibration | journalctl |
| `timestd-web-api` | REST API + dashboard (FastAPI, port 8000) | journalctl |
| `timestd-vtec` | GNSS VTEC estimation (optional) | journalctl |
| `grape-daily.timer` | Daily GRAPE processing trigger (01:00 UTC) | journalctl |
| `grape-daily.service` | `grape daily` — orchestrated pipeline with validation gates | journalctl |
| **radiod** | Real-time USB/FFT (CPU 8-15, uncontested L3 cache) | journalctl |

### Deployment

- **Git repo**: `/home/mjh/git/hf-timestd/`
- **Production install**: `/opt/hf-timestd/` (venv with non-editable `pip install`)
- **Deploy script**: `sudo scripts/update-production.sh [--pull]` — removes editable installs, copies package, syncs web-api/scripts/docs, updates systemd, restarts services, verifies
- **NEVER use `pip install -e`** in production — editable installs make the git repo live production code
- **Config**: `/etc/hf-timestd/timestd-config.toml`
- **Data root**: `/var/lib/timestd/`

---

## ✅ RESOLVED IN PREVIOUS SESSIONS

### Tick Timing Reference Frame Fix (2026-02-15)

**Root cause:** `tick_matched_filter.py` computed timing offsets relative to buffer start (sample 0), not UTC. The `_detect_minute_marker()` had no access to `buffer_timing` and assumed sample 0 = second 0. The buffer start is arbitrary, so `mean_timing_offset_ms` was a buffer-relative position (0–500ms), not D_clock.

**Fix:** Plumbed `buffer_timing` and `minute_boundary` through from `metrology_engine.py` → `tick_matched_filter.process_minute()` → `_detect_minute_marker()`. Minute marker search now uses `buffer_timing.utc_to_sample(minute_boundary)` to locate second 0, searches forward 100ms (covers all HF skywave ToF). D_clock computed via `sample_to_utc()`. Added `d_clock_ms` field to `MinuteTickAnalysis`. Updated `metrology_service.py` and `multi_broadcast_fusion.py` to use `d_clock_ms` instead of buffer-relative `mean_timing_offset_ms`.

**Key insight:** The primary timing path (ntpd-style edge detector → L1 → L2 `clock_offset/`) was correct all along. The tick_matched_filter is a secondary IQ-domain module for carrier phase/Doppler — its D_clock is supplementary. Dashboard reads from the primary path.

**Files modified:** `tick_matched_filter.py`, `metrology_engine.py`, `metrology_service.py`, `multi_broadcast_fusion.py`

### Dashboard Flat D_clock Fix (2026-02-14, late session)

**Root cause:** Three bugs in the dashboard data pipeline, NOT in the measurement pipeline:

1. **Field name mismatch** (`dashboard.py:197,442`): Read `m.get('raw_toa_ms')` but L2 HDF5 field is `raw_arrival_time_ms`. Result: `timing_error` always `None`, grid panels showed only SNR (flat on strong channels).
2. **Double subtraction** (`dashboard.py:200`): Subtracted `min_propagation_ms` from `raw_arrival_time_ms`, but that field already IS D_clock (observed − expected). The L2 calibration service writes D_clock to both `raw_arrival_time_ms` and `clock_offset_ms` (see Issue 3 in next session).
3. **Incompatible reference frame** (`dashboard.py:249`): tick_timing second pass injected `mean_timing_offset_ms` (buffer-relative, 0–500ms) into the same `timing_error_ms` array as clock_offset D_clock (±15ms). Fixed: tick_timing contributes only SNR, not timing error.
4. **Missing chart trace** (`dashboard-24h.html:renderMiniChart`): Only plotted SNR on y-axis. Added timing error as primary trace with auto-scaled y-axis, SNR demoted to faint secondary on y3.

**Verification:** 17/17 broadcasts now have timing error data. CHU shows clear diurnal pattern (26→93 TECU via TEC). Fusion healthy (grade B, ±1.3ms). IONEX files being generated (59 today).

**Files modified:** `web-api/routers/dashboard.py`, `web-api/static/dashboard-24h.html`

### TEC Pipeline Audit & Enhancement (2026-02-14, evening session)

Complete audit of 17 concerns across 5 files. Four new modules implemented and wired into `physics_fusion_service.py`:

1. **Bayesian TEC Estimator** (`tec_estimator.py` rewrite) — MAD-based 3σ outlier rejection, SNR weighting, N=2 confidence cap at 0.3, negative slope → None rejection, `propagation_mode` on TECResult, dead `high_precision_mode` removed
2. **Carrier-Phase dTEC** (`carrier_tec.py` new) — phase rate → Doppler → dTEC/dt → integrated TEC(t), anchored to group-delay absolute TEC
3. **Multi-Layer Tomography** (`iono_tomography.py` new) — E/F layer separation via constrained least squares, solar-dependent priors, condition monitoring
4. **VTEC Map Generator** (`vtec_mapper.py` new) — sTEC→vTEC mapping, IPP computation, 2D polynomial surface, IONEX output

Also fixed: inverted uncertainty weighting in fusion (concern #9), too-narrow TEC validation window 5-100→1-200 TECU (concern #10). 19/19 tests passing. Full details: `docs/changes/SESSION_2026_02_14_TEC_PIPELINE_AUDIT.md`.

**Validated:** Upstream measurements confirmed to contain real ionospheric variation. TEC pipeline producing diurnal patterns (CHU: 26 TECU night → 93 TECU afternoon). Remaining issue: TEC outliers up to 3930 TECU from mode mixing (see next session Issue 2).

### HamSCI 2026 Workshop Abstract (2026-02-14)

Presentation abstract written and committed: `docs/HAMSCI_2026_WORKSHOP_ABSTRACT.md`. Updated project description reflecting current system capabilities. Six forward-looking recommendations for TEC optimization. Recommendations 1, 2, 3, 6 implemented in the TEC session above.

### Documentation Conformance Audit (2026-02-14)

Broken links fixed in `docs/PHYSICS.md` and `docs/STATION_SETUP_GUIDE.md`. Audit execution status updated in `docs/DOCUMENTATION_AUDIT_2026_02_14.md`. Bulk archival of superseded docs completed.

### GRAPE Pipeline + PSWS Uploads (2026-02-14)

Full GRAPE pipeline operational. `grape daily` orchestrates: decimate → validate → spectrogram → validate → package → upload. Fixed `gap_samples` unit mismatch. Verified 9/9 channels processed and uploaded.

### Physics Memory + Operational Resilience (2026-02-14)

Physics service caches readers, bounds retry state. Memory safety limits in systemd. Logrotate `copytruncate`. Freshness monitoring. Deployment correspondence checklist.

### Signal Presence Gate Fix (2026-02-12)

`_check_signal_presence()` failed on shared channels (0.5% duty cycle). Fixed: use `edge_results` as primary signal presence indicator. tick_timing now writing 56 windows/min.

### Edge Ensemble (2026-02-12)

`TickEdgeDetector`: 50–57 ticks/min, ±2ms uncertainty. Recovery when minute marker fails.

### Phase Continuity + Doppler UI (2026-02-11)

Buffer-relative time fix for IQ mixer. Three-tier phase extraction. Cross-frequency discrimination gate. Phase/Doppler web dashboard.

### Earlier Fixes

- **CHU FSK Decoder** (2026-02-10): USB sidecar, quadrature demod, ring buffer timing
- **Pipeline Offset Calibration** (2026-02-09): Removed — radiod RTP timestamps authoritative
- **Timing Accuracy** (2026-02-06): Circular calibration, GPS ground truth, Kalman filter
- **HDF5 Crash Safety** (2026-02-06): SWMR eliminated — open-write-close
- **CHU Memory Leak** (2026-01-02): Extract 1.1s slice before demodulation
- **HDF5 File Lock Contention** (recurring): `locking=False` on all h5py.File() calls

---

## 🔑 KEY PRINCIPLES

1. **The GPSDO is a steel ruler.** Every sample has a known UTC timestamp via the RTP chain. The buffer exists only to find the tone. Once found, read the timestamp. That's the ToA.
2. **The ionosphere is the unknown.** Multi-frequency, multi-station geometry solves it — with or without GPS. GPS just removes one unknown (clock error).
3. **D_clock is the observable for TEC.** D_clock = observed_toa − predicted_geometric_delay. Any residual 1/f² pattern in D_clock across frequencies IS the ionospheric dispersion signal. Use D_clock, not raw ToA.
4. **Carrier phase gives dTEC; group delay gives absolute TEC.** Phase is 1000× more precise but ambiguous. Anchor phase-derived dTEC to group-delay absolute TEC at minute boundaries.
5. **Mode priors prevent contamination.** A 2F measurement mixed with 1F measurements corrupts the 1/f² fit. Use the propagation model's mode predictions to gate which measurements enter the estimator.
6. **17 paths = geometric diversity.** Different elevation angles and azimuths separate E-layer from F-layer contributions. Low-angle paths (BPM) are E-layer sensitive; high-angle paths (CHU) are F-layer dominated.
7. **Bandpass before correlate.** On shared channels, competing stations corrupt long-template correlations.
8. **Edge results are the signal presence indicator.** If the edge detector found ticks, signal is present.

---

## ✅ Success Criteria — Next Session

1. **TEC outliers bounded** — cap at 200 TECU for mid-latitude, investigate mode mixing
2. **L2 schema consistent** — `raw_arrival_time_ms` and `clock_offset_ms` have distinct, documented meanings
3. **No regressions** — existing tests still pass, fusion service still feeds chrony
