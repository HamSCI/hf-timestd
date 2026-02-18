# NEVER CHANGE THE FOLLOWING PRIMARY INSTRUCTION!!!

**Primary Instruction:** In this context you will perform a critical review of the HF Time Standard (hf-timestd) project, either in its entirety or in a specific component, as specified by the user. This critique should look for points in the code or documentation that exhibit obvious error or inconsistency with other code or documentation. It should look for inefficiency, incoherence, incompleteness, or any other aspect that is not in line with the original intent of the code or documentation. It should also look for obsolete, deprecated, or "zombie" code that should be removed. Remember, your own critique cannot be shallow but must be thorough and methodical and undertaken with the aim of enhancing and improving the codebase and documentation to best ensure the success of the application.

Make your criticism from the perspective of 1) a user of the system, 2) a metrologist, 3) a ionospheric scientist, and 4) a software engineer. These perspectives can differ in their priorities and interests, and your critique should reflect this. For example, a user of the system will be most interested in the accuracy and reliability of the system, while a metrologist will be most interested in the precision and traceability of the system, while an ionospheric scientist will be most interested in the ionospheric conditions and their impact on the system, and a software engineer will be most interested in the code quality, maintainability, coverage and adequacy of testing, and the resilience and robustness of the system over time and in edge cases. Ultimately, however, a consensus of these perspectives should guide your critique in service of meeting the application's objectives.

# NEVER CHANGE THE PRECEEDING PRIMARY INSTRUCTION!!!

# The following secondary instruction and information will guide your critique in this particular session (the instructions below will vary from session to session)

---

## 📋 NEXT SESSION: PHYSICS MODULE CRITICAL REVIEW

**Approach:** Fix backend data pipelines first, then frontend. The ionosphere.html sub-pages have display issues, but most stem from broken or empty backend data (TEC constants, scintillation nulls, stale reanalysis). Fixing the frontend without fixing the data would be cosmetic. Pick one concrete pipeline (e.g., TEC producing constant values) and fix it end-to-end before touching the frontend display.

**Objective:** Critically review the physics module of hf-timestd. The physics dashboard and API endpoints currently lack focus — they display many metrics but do not clearly answer the fundamental question: *"Does this instrument produce scientifically useful ionospheric measurements?"* The review should simplify and focus on **at most 4 high-yield demonstrations** of the instrument's utility as an ionospheric physics tool.

### The Two Concerns of hf-timestd

1. **UTC Reconstruction (Fusion mode):** Reconstruct UTC as accurately as possible when GPS+PPS is lost. This is an ongoing implementation effort — not the focus of this session.
2. **Ionospheric Physics (RTP mode):** While GPS+PPS provides sub-microsecond UTC accuracy (Lock Tiers L4–L6), use that precision to make interesting physics observations about the ionosphere. **This is the focus of this session.**

### System Context

- **Receiver:** GPSDO-locked RX888 SDR via KA9Q-radio (`radiod`), RTP-timestamped IQ at 24 kHz/channel
- **Stations:** WWV (2.5–25 MHz), WWVH (2.5–15 MHz), CHU (3.33, 7.85, 14.67 MHz), BPM (2.5–15 MHz)
- **Geometry:** 17 broadcasts × 9 frequencies × 4 stations = multi-path, multi-frequency ionospheric sounding
- **Location:** EM38 (central Missouri, ~38.9°N, ~92.1°W)
- **Timing authority:** GPS+PPS via radiod RTP chain (Lock Tier L6 — authoritative)

### What the Instrument Actually Produces (Verified 2026-02-17)

| Data Product | Status | Evidence |
|---|---|---|
| **Raw timing measurements** | ✅ 15K/day, 4 stations, 7 freqs | `/api/propagation/timeline` returns 14,953 measurements/24h |
| **SNR per broadcast** | ✅ Real, varies with conditions | Plotted on physics dashboard |
| **Propagation mode labels** | ⚠️ Noisy — phantom 4F2 at night | BPM 2.5 MHz labeled "4F2" at 07:00 UTC (nighttime) |
| **Mode change events** | ✅ Detected, logged | `/api/physics/events/recent` shows mode transitions |
| **TEC (L3 science HDF5)** | ❌ Broken — alternates between 2 constants | `tec_tecu` = {86.18, 34.51} repeating; `frequencies_mhz` = "0.00,0.00,0.00" |
| **TEC (API)** | ❌ Returns "no_data" or stale model values | `/api/tec/current` → `"status": "no_data"` |
| **Scintillation (S4, σ_φ)** | ❌ All nulls | `/api/physics/scintillation/paths` → `s4_mean: null` for all stations |
| **Physics `/latest`** | ❌ Empty | `utc_offset_ms: null`, `stations_used: ""` |
| **Reanalysis** | ❌ Stale | Last ran 2026-02-13, propagation_stats 4 days old |
| **Doppler** | ❓ Untested | HDF5 dirs exist under `doppler/` but not exposed via API |
| **D_clock (timing residuals)** | ✅ Real, sub-ms | Edge ensemble: ±3ms, PLL: ±1ms (A/B comparison verified) |
| **GRAPE spectrograms** | ✅ Operational | 9/9 channels, daily upload to PSWS |

### The Core Problem

The physics module has many implemented *code paths* but few produce *validated, interpretable output*. The dashboard shows SNR scatter plots and empty cards. The TEC pipeline writes 93K records to HDF5 but the values are model constants, not measurements. Scintillation indices are all null. The physics `/latest` endpoint returns empty fields.

**PHYSICS.md** (1074 lines) documents 10+ capabilities with ✅ markers, but the live system contradicts many of these claims. The HamSCI abstract (`docs/HAMSCI_2026_WORKSHOP_ABSTRACT.md`) promises capabilities that need honest validation.

The goal of this review is NOT to fix everything, but to identify the **4 highest-yield demonstrations** that would prove the instrument works as a physics tool, determine what's blocking each one, and focus implementation effort there.

---

## 🎯 THE 4 HIGH-YIELD DEMONSTRATIONS

Each demonstration should be achievable with data the instrument already collects. The question is whether the processing pipeline and visualization correctly extract and present the physics.

### Demo 1: Diurnal D_clock Variation (The Raw Observable)

**What it proves:** The instrument measures real ionospheric group delay, not noise.

**The physics:** D_clock = observed_ToA − expected_geometric_delay. During daytime, ionospheric electron density increases, adding group delay (D_clock increases). At night, it decreases. This diurnal pattern is the most basic proof that the instrument sees the ionosphere.

**What should be visible:** A 24-hour time series of D_clock for a single station (e.g., WWV 10 MHz) should show:
- Smooth increase after sunrise (~12:00–14:00 UTC for Missouri)
- Peak in afternoon (~18:00–20:00 UTC)
- Decrease after sunset (~00:00–02:00 UTC)
- Amplitude: ~2–10 ms variation (depending on frequency and path)

**Current state:** D_clock is computed correctly in the primary timing path (edge detector → L1 → L2 `clock_offset/`). The A/B decoder comparison shows edge ensemble at ±3ms and PLL at ±1ms — both physically plausible. But the physics dashboard does NOT plot D_clock vs time. The "Measurement History" plot shows only SNR.

**Investigation targets:**
- `web-api/routers/physics.py` — does any endpoint serve D_clock time series?
- `web-api/routers/dashboard.py` — the 24h dashboard was fixed to show timing_error; does the physics page use the same data?
- L2 HDF5 `clock_offset/` directories — do they contain real D_clock values?
- `/api/propagation/timeline` — returns timestamps, modes, stations, frequencies, snr_db but **no timing_error or d_clock field**

**Key files:**
- `web-api/routers/physics.py` (API endpoints)
- `web-api/routers/propagation.py` (timeline endpoint)
- `web-api/static/physics.html` (dashboard)
- `src/hf_timestd/core/physics_fusion_service.py` (L3 aggregation)

### Demo 2: Multi-Frequency Dispersion → TEC

**What it proves:** The instrument can extract Total Electron Content from the 1/f² dispersion relation.

**The physics:** Ionospheric group delay follows τ = 1.344 × TEC / f². If we plot D_clock vs 1/f² for multiple frequencies from the same station at the same time, the slope gives TEC in TECU. This is the instrument's primary scientific measurement.

**What should be visible:** A scatter plot of D_clock vs 1/f² for WWV (6 frequencies: 2.5, 5, 10, 15, 20, 25 MHz) at a single time instant, showing a linear relationship. The slope = 1.344 × TEC. Daytime TEC should be 20–80 TECU; nighttime 5–20 TECU.

**Current state:** BROKEN. The TEC HDF5 (`AGGREGATED_tec_20260217.h5`) has 93K records but:
- `frequencies_mhz` field contains `"0.00,0.00,0.00"` for all records — metadata is not being written
- `tec_tecu` alternates between exactly 2 values (86.18 and 34.51 TECU) — these are model constants, not fitted values
- `/api/tec/current` returns `"status": "no_data"`
- The physics dashboard has no TEC plot

**Investigation targets:**
- `src/hf_timestd/core/tec_estimator.py` — is the 1/f² fit actually running on real D_clock data?
- `src/hf_timestd/core/physics_fusion_service.py` — how does it aggregate per-channel D_clock into multi-frequency TEC?
- Why is `frequencies_mhz` always "0.00,0.00,0.00"? This suggests the frequency metadata is not being passed to the TEC writer.
- Why do TEC values alternate between exactly 2 constants? This suggests the estimator is returning model fallback values, not fitted values.
- `web-api/routers/propagation.py` (`/api/propagation/tec`) — returns data but all timestamps are identical (`2026-02-16T13:28:00`) — stale

**Key files:**
- `src/hf_timestd/core/tec_estimator.py` (1/f² fit)
- `src/hf_timestd/core/physics_fusion_service.py` (orchestration)
- `src/hf_timestd/io/hdf5_writer.py` (TEC HDF5 writer)
- `web-api/routers/propagation.py` (TEC API)

### Demo 3: Frequency-Dependent SNR → D-Layer Absorption

**What it proves:** The instrument observes real ionospheric absorption, not just propagation.

**The physics:** The D-layer (60–90 km) absorbs HF energy proportional to 1/f². Lower frequencies are absorbed more. During daytime, the D-layer is ionized by solar UV; at night it disappears. So:
- **Daytime:** 2.5 MHz SNR drops sharply, 15 MHz SNR stays high → steep frequency slope
- **Nighttime:** All frequencies have similar SNR (no D-layer) → flat frequency slope
- **Sunrise/sunset:** Rapid SNR transitions mark the solar terminator crossing the path midpoint

**What should be visible:** A multi-frequency SNR time series for one station (e.g., WWV) over 24 hours, with clear frequency-dependent diurnal variation. The 2.5 MHz trace should show the deepest daytime absorption dip.

**Current state:** PARTIALLY WORKING. The physics dashboard plots SNR vs time with station-colored markers, but:
- All frequencies from the same station use the same color — you can't see frequency-dependent absorption
- No sunrise/sunset markers (the code exists but `conditions.sun_times` may not be populated)
- The plot is labeled "Measurement History" with subtitle "Signal-to-noise ratio by station over time" — generic, not focused on the physics story

**Investigation targets:**
- `web-api/static/physics.html` — the SNR plot groups by station, not by frequency. Recolor by frequency to show absorption.
- `/api/physics/events/conditions` — does it return `sun_times`? The dashboard code checks for it.
- Can we compute a "D-layer absorption index" = SNR(15 MHz) − SNR(2.5 MHz) and plot it vs time? This would be a single clean curve showing D-layer ionization.

**Key files:**
- `web-api/static/physics.html` (SNR plot, lines 485–581)
- `web-api/routers/physics.py` (events/conditions endpoint)

### Demo 4: Day/Night MUF Boundary in Mode Assignments

**What it proves:** The instrument tracks the Maximum Usable Frequency and its diurnal variation.

**The physics:** The MUF is the highest frequency that can propagate via F-layer reflection. During daytime, foF2 ≈ 8–12 MHz, so MUF (oblique) can reach 20–30 MHz. At night, foF2 drops to 3–5 MHz, and higher frequencies lose F-layer propagation. This should be visible as:
- **Daytime:** 15, 20, 25 MHz show F-layer modes (1F2, 2F2)
- **Nighttime:** Only 2.5, 5 MHz show F-layer modes; higher frequencies show E-layer or no detection
- **Transition:** Mode assignments change at sunrise/sunset

**Current state:** PARTIALLY WORKING but NOISY. The mode identification assigns modes, but:
- Phantom modes at night: BPM 2.5 MHz labeled "4F2" at 07:00 UTC (nighttime in Missouri). 4-hop F2 at 2.5 MHz is geometrically possible but physically implausible when foF2 < 5 MHz.
- The reanalysis service (which applies physics-based MUF constraints) last ran 2026-02-13 — 4 days stale.
- `/api/propagation/conditions` returns mode distributions but no MUF estimate or time series.
- The physics dashboard has no MUF plot.

**Investigation targets:**
- `src/hf_timestd/core/ionospheric_reanalysis.py` — why hasn't it run since Feb 13? Check systemd timer.
- `src/hf_timestd/core/propagation_mode_solver.py` — does it apply any physics constraints in real-time, or is it purely geometric?
- Can we derive an empirical MUF from the mode assignments? MUF ≈ highest frequency where F-layer mode is detected with SNR > threshold.
- The reanalysis was designed specifically to fix the phantom-mode problem. If it's not running, the mode assignments are unreliable.

**Key files:**
- `src/hf_timestd/core/ionospheric_reanalysis.py` (offline physics validation)
- `src/hf_timestd/core/propagation_mode_solver.py` (real-time mode ID)
- `systemd/timestd-reanalysis.timer` (if it exists)
- `web-api/routers/propagation.py` (conditions endpoint)

---

## 🔍 CROSS-CUTTING CONCERNS

These issues affect multiple demonstrations and should be examined during the review:

### Concern A: physics_fusion_service.py — Is It Actually Running?

The physics service (`timestd-physics`) is supposed to aggregate per-channel L2 measurements into L3 science products (TEC, scintillation, events). But `/api/physics/latest` returns `utc_offset_ms: null` and `stations_used: ""`. Either the service is crashing, starved of input data, or writing empty results.

- Check: `systemctl status timestd-physics`
- Check: `journalctl -u timestd-physics --since "1 hour ago"`
- Check: `/var/log/hf-timestd/physics.log`

### Concern B: L2 → L3 Data Flow

The L2 HDF5 files (per-channel `clock_offset/`, `doppler/`, `tick_phase/`) are the input to L3 science products. If the L3 service can't read them (HDF5 locking, schema mismatch, empty files), all downstream products fail silently.

- Check: Do `clock_offset/` HDF5 files contain recent D_clock values?
- Check: Does the physics service log how many L2 records it reads per cycle?

### Concern C: PHYSICS.md Accuracy

`docs/PHYSICS.md` (1074 lines) claims ✅ status for many capabilities. The review should verify each claim against the live system and downgrade to ⚠️ or ❌ where appropriate. Specific concerns:
- Section 3.1 (TEC): Claims "Fully implemented" but API returns no data
- Section 4.2 (Scintillation): Claims "Implemented" but all indices are null
- Section 4.3 (TIDs): Claims "Implemented" but no TID events detected
- Section 7 (Test Signal): Claims all features implemented — verify

### Concern D: Dashboard Focus

The physics dashboard (`physics.html`) has 3 tabs (Paths, Channels, Events) with many cards and metrics, but none clearly answer the 4 demonstration questions above. The review should recommend simplification: fewer metrics, clearer plots, focused on the physics story.

---

## 📂 KEY FILES FOR REVIEW

| File | Purpose | Priority |
|------|---------|----------|
| `src/hf_timestd/core/physics_fusion_service.py` | L3 science product orchestration | **HIGH** — is it running? |
| `src/hf_timestd/core/tec_estimator.py` | 1/f² TEC fit | **HIGH** — why broken? |
| `src/hf_timestd/core/ionospheric_reanalysis.py` | Offline mode validation | **HIGH** — why stale? |
| `src/hf_timestd/core/propagation_mode_solver.py` | Real-time mode ID | **MEDIUM** — phantom modes |
| `web-api/static/physics.html` | Physics dashboard | **MEDIUM** — needs focus |
| `web-api/routers/physics.py` | Physics API endpoints | **MEDIUM** — missing D_clock |
| `web-api/routers/propagation.py` | Propagation/TEC API | **MEDIUM** — stale TEC |
| `src/hf_timestd/core/advanced_signal_analysis.py` | Scintillation, multipath | **LOW** — all nulls |
| `docs/PHYSICS.md` | Physics documentation | **LOW** — accuracy audit |
| `docs/HAMSCI_2026_WORKSHOP_ABSTRACT.md` | Public claims | **LOW** — honest check |

---

## 🏗️ SERVICE INVENTORY

| Service | Purpose | Logs |
|---------|---------|------|
| `timestd-core-recorder` | RTP → raw buffer (authoritative timestamps) | journalctl |
| `timestd-metrology` | IQ → L1/L2 measurements + tick phase extraction | `/var/log/hf-timestd/phase2-*.log` |
| `timestd-fusion` | Multi-broadcast fusion → Chrony (WatchdogSec=120) | `/var/log/hf-timestd/fusion.log` |
| `timestd-physics` | TEC estimation, L3 physics products | `/var/log/hf-timestd/physics.log` |
| `timestd-l2-calibration` | L2 adaptive calibration | journalctl |
| `timestd-web-api` | REST API + dashboard (FastAPI, port 8000) | journalctl |
| `timestd-vtec` | GNSS VTEC estimation (optional) | journalctl |
| **radiod** | Real-time USB/FFT (CPU 8-15, uncontested L3 cache) | journalctl |

### Deployment

- **Git repo**: `/home/mjh/git/hf-timestd/`
- **Production install**: `/opt/hf-timestd/` (venv with non-editable `pip install`)
- **Deploy script**: `sudo scripts/update-production.sh [--pull]`
- **Config**: `/etc/hf-timestd/timestd-config.toml`
- **Data root**: `/var/lib/timestd/`

---

## 🔑 KEY PRINCIPLES

1. **The GPSDO is a steel ruler.** Every sample has a known UTC timestamp via the RTP chain. The buffer exists only to find the tone. Once found, read the timestamp. That's the ToA.
2. **The ionosphere is the unknown.** Multi-frequency, multi-station geometry solves it — with or without GPS. GPS just removes one unknown (clock error).
3. **D_clock is the observable for TEC.** D_clock = observed_toa − predicted_geometric_delay. Any residual 1/f² pattern in D_clock across frequencies IS the ionospheric dispersion signal.
4. **Carrier phase gives dTEC; group delay gives absolute TEC.** Phase is 1000× more precise but ambiguous. Anchor phase-derived dTEC to group-delay absolute TEC at minute boundaries.
5. **Mode priors prevent contamination.** A 2F measurement mixed with 1F measurements corrupts the 1/f² fit. Use the propagation model's mode predictions to gate which measurements enter the estimator.
6. **17 paths = geometric diversity.** Different elevation angles and azimuths separate E-layer from F-layer contributions.
7. **Edge results are the signal presence indicator.** If the edge detector found ticks, signal is present.

---

## ✅ Success Criteria — This Session

1. **Demo 1 works:** D_clock time series visible on dashboard, showing diurnal variation
2. **Demo 2 diagnosed:** Root cause of broken TEC identified; fix implemented or path to fix documented
3. **Demo 3 works:** SNR plot recolored by frequency, showing D-layer absorption pattern
4. **Demo 4 diagnosed:** Reanalysis service running, phantom modes reduced
5. **PHYSICS.md honest:** Status markers reflect actual system state, not aspirational state
6. **Dashboard focused:** Physics page simplified to clearly present the 4 demonstrations

---

## ✅ RESOLVED IN PREVIOUS SESSIONS (Reference Only)

### Kalman LOCKED Status Fix (2026-02-18)
`_write_fused_result_hdf5()` overrode `result.kalman_state` with stale logic requiring `uncertainty < 1.0ms` for LOCKED. Actual uncertainty ~1.3ms, so dashboard always showed ACQUIRING. Fixed to use `result.kalman_state` directly (set from `self.kalman_converged`). Also relaxed WLS convergence threshold from 3→2 stations (normal operating condition). Deployed, verified end-to-end (HDF5 → API → dashboard), committed (e3b1d9e).

### A/B Decoder Comparison (2026-02-17)
Edge ensemble (57 ticks/min, ±3ms) vs PLL carrier phase (±1ms). MF baseline fixed to use edge ensemble instead of broken TickMatchedFilter.d_clock_ms. Deployed, verified, committed (d016ddc).

### TEC Outliers + L2 Schema (2026-02-15)
Mode-gated TEC, hard bounds 0–200 TECU, L2 field semantics fixed.

### Tick Timing Reference Frame (2026-02-15)
Plumbed buffer_timing through to tick_matched_filter. Primary timing path was correct all along.

### Dashboard D_clock Fix (2026-02-14)
Field name mismatch, double subtraction, incompatible reference frames — all fixed in dashboard.py.

### TEC Pipeline Audit (2026-02-14)
Bayesian TEC estimator, carrier-phase dTEC, multi-layer tomography, VTEC mapper — all implemented. 19/19 tests passing.

### Earlier: Edge Ensemble, CHU FSK, HDF5 Locking, Signal Presence Gate, Phase Continuity
See git log for details.
