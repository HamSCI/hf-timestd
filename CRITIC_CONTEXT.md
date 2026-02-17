# NEVER CHANGE THE FOLLOWING PRIMARY INSTRUCTION!!!

**Primary Instruction:** In this context you will perform a critical review of the HF Time Standard (hf-timestd) project, either in its entirety or in a specific component, as specified by the user. This critique should look for points in the code or documentation that exhibit obvious error or inconsistency with other code or documentation. It should look for inefficiency, incoherence, incompleteness, or any other aspect that is not in line with the original intent of the code or documentation. It should also look for obsolete, deprecated, or "zombie" code that should be removed. Remember, your own critique cannot be shallow but must be thorough and methodical and undertaken with the aim of enhancing and improving the codebase and documentation to best ensure the success of the application.

Make your criticism from the perspective of 1) a user of the system, 2) a metrologist, 3) a ionospheric scientist, and 4) a software engineer. These perspectives can differ in their priorities and interests, and your critique should reflect this. For example, a user of the system will be most interested in the accuracy and reliability of the system, while a metrologist will be most interested in the precision and traceability of the system, while an ionospheric scientist will be most interested in the ionospheric conditions and their impact on the system, and a software engineer will be most interested in the code quality, maintainability, coverage and adequacy of testing, and the resilience and robustness of the system over time and in edge cases. Ultimately, however, a consensus of these perspectives should guide your critique in service of meeting the application's objectives.

# NEVER CHANGE THE PRECEEDING PRIMARY INSTRUCTION!!!

# The following secondary instruction and information will guide your critique in this particular session (the instructions below will vary from session to session)

---

## 📋 NEXT SESSION: GRAPE MODULE DEBUGGING

**Objective:** Restore GRAPE daily processing pipeline — last successful upload was 2026-02-13.

### Problem Statement

The GRAPE (Global Radio Archive for Propagation Experiments) module has stopped producing spectrograms and uploading data to PSWS (Propagation Studies Web Server). Last successful processing was for 2026-02-13. The pipeline should run daily at 01:00 UTC via `grape-daily.timer`.

### Background

**GRAPE Pipeline Overview:**
```
grape daily → orchestrated pipeline with validation gates:
  1. Decimate raw buffers (24 kHz → 10 Hz)
  2. Validate decimated files (gap detection, continuity)
  3. Generate spectrograms (9 channels: CHU_3330, CHU_7850, CHU_14670, SHARED_5000, SHARED_10000, SHARED_15000, WWV_20000, WWV_25000, BPM_5000)
  4. Validate spectrograms (file existence, size checks)
  5. Package for upload (tar.gz with metadata)
  6. Upload to PSWS server
```

**Key Files:**
- **Main module**: `src/hf_timestd/grape/` (daily.py, decimate.py, spectrogram.py, upload.py, validate.py)
- **CLI entry**: `src/hf_timestd/__main__.py` → `grape daily` command
- **Systemd**: `systemd/grape-daily.service` + `systemd/grape-daily.timer`
- **Documentation**: `docs/GRAPE_DAILY_PROCESSING.md`

**Data Paths:**
- Raw buffers: `/var/lib/timestd/raw/YYYY/MM/DD/`
- Decimated: `/var/lib/timestd/grape/decimated/YYYY/MM/DD/`
- Spectrograms: `/var/lib/timestd/grape/spectrograms/YYYY/MM/DD/`
- Packages: `/var/lib/timestd/grape/packages/YYYY/MM/DD/`

### Diagnostic Priorities

**Priority 1: Service Execution Status**
- Check if `grape-daily.timer` is active and triggering
- Review `journalctl -u grape-daily.service` for recent runs
- Verify timer schedule (should be 01:00 UTC daily)
- Check for service failures or timeouts

**Priority 2: Pipeline Stage Failures**
- Identify which stage is failing (decimate, spectrogram, validate, upload)
- Check for error messages in service logs
- Verify input data exists (raw buffers for recent dates)
- Check output directories for partial results

**Priority 3: File System Issues**
- Verify `/var/lib/timestd/grape/` directory structure exists
- Check disk space availability
- Verify permissions (timestd user must have write access)
- Look for orphaned lock files or incomplete writes

**Priority 4: Dependency Failures**
- Verify required Python packages (numpy, scipy, matplotlib, h5py)
- Check for missing system dependencies
- Verify network connectivity for PSWS uploads
- Check PSWS credentials/authentication

**Priority 5: Data Validation Gates**
- Review validation criteria in `validate.py`
- Check if validation is too strict (rejecting valid data)
- Look for gap detection false positives
- Verify continuity checks aren't failing on legitimate data

**Priority 6: Spectrogram Generation**
- Check matplotlib backend configuration
- Verify spectrogram parameters (FFT size, overlap, colormap)
- Look for memory issues during processing
- Check for corrupted decimated input files

**Priority 7: Upload Mechanism**
- Verify PSWS server endpoint is reachable
- Check authentication credentials
- Review upload protocol (HTTP POST, FTP, rsync?)
- Look for network timeouts or rate limiting

### Known Issues from Previous Sessions

**Gap Samples Unit Mismatch (Fixed 2026-02-14):**
- `gap_samples` was in audio samples but compared against 10 Hz decimated samples
- Fixed in validation logic
- Should not be recurring, but verify fix is deployed

**9/9 Channels Verified (2026-02-14):**
- All channels were processing and uploading successfully
- This confirms the pipeline was working recently
- Something changed between 2026-02-14 and now

### Investigation Strategy

1. **Check timer status**: `systemctl status grape-daily.timer` — is it running?
2. **Review recent logs**: `journalctl -u grape-daily.service --since "2026-02-14"` — what errors appear?
3. **Manual test run**: `sudo -u timestd /opt/hf-timestd/venv/bin/python -m hf_timestd grape daily --date 2026-02-14` — does it work manually?
4. **Check data availability**: `ls -lh /var/lib/timestd/raw/2026/02/15/` — are raw buffers being written?
5. **Verify decimation**: Check if decimated files exist for recent dates
6. **Test spectrogram**: Try generating one spectrogram manually to isolate the failure point
7. **Check upload logs**: Look for PSWS server errors or authentication failures

### Success Criteria

1. **Pipeline runs successfully** — `grape daily` completes without errors for recent dates
2. **Spectrograms generated** — All 9 channels produce valid PNG files
3. **Data uploaded** — PSWS server receives and acknowledges uploads
4. **Timer operational** — Daily processing resumes automatically at 01:00 UTC
5. **Validation passes** — No false positives rejecting valid data

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

### A/B Decoder Comparison System (2026-02-17)

**Root causes:**
1. **Critical indentation bug** in `tick_matched_filter.py`: Result handling was outside the for loop, causing only the last window to be processed instead of all 55+ windows per minute. This made `valid_windows` always 0 or 1.
2. **Missing `TickPLLDecoder` class**: The PLL decoder file had implementation classes but was missing the wrapper class that `metrology_engine.py` expected.
3. **Parameter signature mismatch**: `TickPLLDecoder.__init__()` needed `alpha` and `max_missed` parameters.
4. **Missing `d_clock_ms` field**: `MinutePLLAnalysis` dataclass lacked the field needed for comparison tracking.

**Fixes:**
- Fixed indentation in `tick_matched_filter.py` (lines 944-962) — moved result handling inside the for loop
- Created `TickPLLDecoder` wrapper class with proper interface matching `metrology_engine.py` expectations
- Added `d_clock_ms` field to `MinutePLLAnalysis` dataclass
- Added decoder comparison API endpoint (`/decoder-comparison/status`) and UI
- Created `DecoderConfig` singleton for shared state between API and metrology service

**Validation:** Both decoders now operational and detecting ticks:
- **Matched Filter**: 50+ windows/min, SNR 16-34 dB (WWV: 33.8dB, WWVH: 30.2dB)
- **PLL Flywheel**: Successfully locking onto WWV (1000 Hz) and WWVH (1200 Hz) signals
- A/B testing enabled, comparison metrics will populate as data accumulates

**Files modified:** `tick_matched_filter.py`, `tick_pll_decoder.py`, `metrology_engine.py`, `decoder_config.py` (new), `decoder_comparison.py` (new), `decoder-comparison.html` (new)

**Technical insight:** The indentation bug was particularly insidious because no exceptions were raised, the code appeared to run normally, and debug logs weren't visible at INFO level in production. The last window often had valid detections, so `valid_windows=1` seemed plausible.

### TEC Outliers + L2 Schema Alignment (2026-02-15)

**Root causes:**
1. **TEC mode mixing:** Shared-frequency measurements combined 1F and 2F arrivals, corrupting 1/f² fit
2. **L2 field semantics:** Both `raw_arrival_time_ms` and `clock_offset_ms` written with same value (D_clock)

**Fixes:**
- Added dominant-mode gating in fusion TEC solver (only use measurements from same propagation mode)
- Added hard TEC bounds (0 < TEC ≤ 200 TECU) in physics service
- L2 writer now reconstructs absolute `raw_arrival_time_ms = d_clock + propagation_delay`
- Dashboard switched to read `clock_offset_ms` for D_clock (not `raw_arrival_time_ms`)
- Added missing `quality_flags` to `l2_tick_phase_v1.json` schema

**Validation:** 28/28 targeted tests passing. TEC outliers now bounded. L2 schema consistent with documentation.

## ✅ RESOLVED IN PREVIOUS SESSIONS (EARLIER)

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
