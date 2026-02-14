# NEVER CHANGE THE FOLLOWING PRIMARY INSTRUCTION!!!

**Primary Instruction:** In this context you will perform a critical review of the HF Time Standard (hf-timestd) project, either in its entirety or in a specific component, as specified by the user. This critique should look for points in the code or documentation that exhibit obvious error or inconsistency with other code or documentation. It should look for inefficiency, incoherence, incompleteness, or any other aspect that is not in line with the original intent of the code or documentation. It should also look for obsolete, deprecated, or "zombie" code that should be removed. Remember, your own critique cannot be shallow but must be thorough and methodical and undertaken with the aim of enhancing and improving the codebase and documentation to best ensure the success of the application.

Make your criticism from the perspective of 1) a user of the system, 2) a metrologist, 3) a ionospheric scientist, and 4) a software engineer. These perspectives can differ in their priorities and interests, and your critique should reflect this. For example, a user of the system will be most interested in the accuracy and reliability of the system, while a metrologist will be most interested in the precision and traceability of the system, while an ionospheric scientist will be most interested in the ionospheric conditions and their impact on the system, and a software engineer will be most interested in the code quality, maintainability, coverage and adequacy of testing, and the resilience and robustness of the system over time and in edge cases. Ultimately, however, a consensus of these perspectives should guide your critique in service of meeting the application's objectives.

# NEVER CHANGE THE PRECEEDING PRIMARY INSTRUCTION!!!

# The following secondary instruction and information will guide your critique in this particular session (the instructions below will vary from session to session)

---

## 📋 NEXT SESSION: TEC MEASUREMENT AUDIT + IMPLEMENTATION (CODE CHANGES EXPECTED)

**Objective:** Critically scrutinize the current TEC measurement pipeline for weaknesses, errors, and missed opportunities. Then implement four enhancements from the HamSCI 2026 Workshop recommendations:

1. **Bayesian TEC estimator with propagation mode priors** (Rec 1)
2. **Carrier-phase differential TEC (dTEC)** (Rec 2)
3. **Multi-layer E/F tomographic constraints** (Rec 3)
4. **VTEC map generation from 17 sTEC paths** (Rec 6)

**Context:** The system is operational and producing TEC estimates, but the current estimator is naive — unconstrained 1/f² regression that is vulnerable to mode mixing, has no carrier-phase input, treats the ionosphere as a single slab, and does not produce community data products. The HamSCI presentation (see `docs/HAMSCI_2026_WORKSHOP_ABSTRACT.md`) commits to these capabilities as next steps.

**Note on PHaRLAP (Rec 4):** PHaRLAP source is not available. Skip this recommendation. The current 1D numerical integration through Chapman profiles is adequate for shorter paths. BPM multi-hop accuracy remains a known limitation.

**Note on phase-engine (Rec 5):** Diversity reception integration is the next major enhancement after this session. Not in scope here.

---

### 🔍 PART 1: TEC PIPELINE AUDIT — Known Weaknesses to Investigate

The audit should examine the following files and issues **before** implementing changes. Read each file carefully and identify bugs, physics errors, missed opportunities, and architectural problems.

#### A. `TECEstimator` (`src/hf_timestd/core/tec_estimator.py`) — 225 lines

**Known concerns:**

1. **Mode mixing is the dominant error source.** The estimator fits `T_obs(f) = T_vacuum + K·TEC/f²` across all frequencies for a station. But if 5 MHz arrives via 1F and 2.5 MHz arrives via 2F, their geometric path lengths differ by hundreds of km. The 1/f² fit absorbs this geometric difference as fake TEC. The `ionospheric_reanalysis.py` partially addresses this by using D_clock (geometric delay already subtracted per-mode), but `physics_fusion_service.py` feeds raw ToA grouped by `(station, mode)` — **check whether mode assignment is reliable enough to prevent contamination.**

2. **Negative slope handling is wrong.** When `m < 0` (negative TEC), the code forces `m = 0` and `confidence = 0`. But negative slope can also indicate mode misidentification (2F measurement mixed with 1F). The estimator should **reject** these cases rather than silently produce `TEC = 0.0 TECU` which looks like a valid measurement.

3. **`high_precision_mode` flag is unused.** The constructor accepts it but it has no effect on any code path. Dead parameter.

4. **R² as confidence metric is misleading.** With only 2 frequencies (the minimum), R² is always 1.0 (perfect fit to a line through 2 points). This gives false confidence. The estimator needs a minimum of 3 frequencies to have any residual degrees of freedom, or must use a different confidence metric for N=2.

5. **No propagation mode output.** The `TECResult` dataclass has no field for which propagation mode was assumed. The `physics_fusion_service.py` monkey-patches `result.propagation_mode = mode` (line 294) — this will crash if anyone accesses the attribute on a TECResult created elsewhere.

6. **Units confusion risk.** The estimator works internally in seconds (converts ms→s on line 112) but the constant `K_IONOSPHERE = 40.3 / c` has units of `s·Hz²·m²/electrons`. The TECU conversion `tec / 1e16` is correct but the intermediate `tec` variable is in `electrons/m²` — verify the full unit chain is consistent.

#### B. `PhysicsFusionService` (`src/hf_timestd/core/physics_fusion_service.py`) — 455 lines

**Known concerns:**

7. **Mode grouping may starve the estimator.** `_read_l2_slice()` groups measurements by `(station, mode)`. If mode assignment is noisy (e.g., same frequency alternates between "1F" and "UNKNOWN" minute-to-minute), each group may have only 1 frequency, failing the N≥2 requirement. **Check whether cross-mode grouping would be more robust** (using D_clock as `ionospheric_reanalysis.py` does).

8. **ToA source inconsistency.** `_read_l2_slice()` prefers `tof_kalman_ms` over `raw_arrival_time_ms`. But the Kalman filter may have already partially removed ionospheric dispersion (if it's tracking a smoothed state). This would reduce the 1/f² signal that the TEC estimator needs. **Verify what the Kalman filter is actually tracking** — if it's tracking D_clock (timing error), it should NOT be used for TEC estimation.

9. **Uncertainty weighting is inverted.** Line 3475 in `multi_broadcast_fusion.py`: `'uncertainty_ms': 1.0 / max(0.001, m.confidence)`. This maps confidence 1.0 → uncertainty 1.0 ms, confidence 0.5 → uncertainty 2.0 ms. But confidence is R² or a quality metric, not a timing precision estimate. The actual timing uncertainty should come from the measurement's `tof_uncertainty_ms` or the edge ensemble's reported uncertainty.

10. **TEC validation window is too narrow.** `multi_broadcast_fusion.py` line 3499 requires `5.0 <= tec_u <= 100.0` for TEC_VALIDATED status. Nighttime TEC can be 2–5 TECU. Solar maximum daytime TEC can exceed 100 TECU. This gate rejects valid measurements.

#### C. `IonosphericReanalysis` (`src/hf_timestd/core/ionospheric_reanalysis.py`) — 846 lines

**Known concerns:**

11. **Best TEC implementation but runs offline.** `_estimate_tec_cleaned()` correctly uses D_clock (geometric delay already subtracted), groups by frequency, takes median per frequency (robust to outliers), and uses IQR-based uncertainty. **This approach should be the primary real-time estimator**, not the naive ToA-based one in `physics_fusion_service.py`.

12. **foF2 estimation is crude.** Uses a single `FOF2_NOON_MHZ = 9.0` constant with Chapman cosine scaling. The system already has WAM-IPE/GIRO/IRI providing foF2 — the reanalysis should use those instead of its own parametric model.

#### D. `HFPropagationModel` (`src/hf_timestd/core/propagation_model.py`) — 1086 lines

**Known concerns:**

13. **Ionospheric delay is double-counted in TEC estimation.** The propagation model computes `iono_delay_ms` and adds it to `geometric_delay_ms` to get total predicted delay. When the metrology engine computes `D_clock = observed_toa - predicted_total_delay`, the ionospheric component is already subtracted. So D_clock should be ionosphere-free. But the TEC estimator then fits D_clock for 1/f² dispersion — **if the model's ionospheric correction were perfect, there would be zero 1/f² residual.** The TEC estimator is actually measuring the **error** in the model's ionospheric correction, not the absolute TEC. This is fine for differential analysis but the absolute TEC values may be meaningless.

14. **`_tec_group_delay()` applies 2× multiplier per hop.** Line 805: `delay_ms = delay_s * 2.0 * n_hops * 1000.0`. The 2× is for "up and down through ionosphere per hop." But the slant TEC already accounts for the oblique path through the ionosphere. The 2× factor assumes the signal passes through the full ionosphere twice per hop (up-leg and down-leg), which is correct for a single-layer model but **over-estimates for a Chapman profile** where most electrons are near the peak. Verify consistency with `_integrate_group_delay()` which also applies `2.0 * n_hops`.

15. **Same TEC assumed for all hops.** Line 1322 in `ionospheric_model.py`: `total_slant_tec = slant_tec * n_hops`. For BPM (3-hop, ~10,000 km), each hop traverses a different part of the ionosphere with different TEC. The model should sample TEC at each reflection point.

#### E. Carrier Phase → TEC Pipeline (currently disconnected)

16. **Phase data exists but is not used for TEC.** `TickMatchedFilter` produces ~55 carrier phase measurements per minute per station (L2/tick_phase HDF5). The phase service (`web-api/services/phase_service.py`) computes Doppler from phase rate of change. But **nobody converts Doppler to dTEC/dt.** This is the single highest-impact missed opportunity — carrier phase gives 1000× better temporal resolution than group delay.

17. **Phase continuity was fixed (2026-02-12) but not validated in production.** The buffer-relative time fix should produce continuous phase across minutes. Verify by checking phase dashboard for σ_φ < 0.3 rad on unambiguous channels (CHU 14.67, WWV 20/25 MHz).

---

### 🛠️ PART 2: IMPLEMENTATION PLAN

After the audit, implement these four enhancements in priority order. Each builds on the previous.

#### Implementation 1: Bayesian TEC Estimator with Mode Priors

**File:** `src/hf_timestd/core/tec_estimator.py` (extend or replace `TECEstimator`)

**Design:**
- Accept propagation mode predictions from `HFPropagationModel` as priors
- For each measurement, subtract the mode-specific geometric path length (already done if using D_clock)
- Fit residual dispersive delay (∝ 1/f²) to extract sTEC
- Weight by measurement SNR AND model confidence for the assigned mode
- Reject measurements whose residuals exceed 3σ (mode misidentification)
- Require N ≥ 3 frequencies for confidence > 0.5 (N=2 capped at 0.3)
- Add `propagation_mode` field to `TECResult` dataclass properly
- Remove dead `high_precision_mode` parameter

**Key insight:** Use D_clock (as `ionospheric_reanalysis.py` already does) instead of raw ToA. D_clock has geometric delay removed per-mode, so the 1/f² residual IS the ionospheric dispersion signal. This eliminates mode-mixing contamination.

**Tests:** `tests/core/test_tec_estimator_diagnostics.py` — extend with mode-mixing rejection tests, N=2 confidence cap, negative slope rejection.

#### Implementation 2: Carrier-Phase Differential TEC (dTEC)

**Files:**
- `src/hf_timestd/core/carrier_tec.py` (new module)
- `src/hf_timestd/core/physics_fusion_service.py` (wire in)

**Design:**
- Read L2/tick_phase HDF5 (carrier_phase_rad time series, ~55 points/min/station)
- Compute phase rate of change (Δφ/Δt) — already done as Doppler in phase_service.py
- Convert Doppler to dTEC/dt: `dTEC/dt = -f² · Δf_D / (40.3 · f_carrier)` where `Δf_D = Doppler_Hz`
- Integrate dTEC/dt over time to get relative TEC(t)
- Anchor to absolute TEC from group-delay estimator (Implementation 1) at each minute boundary
- Output: sub-TECU temporal resolution TEC time series

**Key physics:** Carrier phase measures the **phase path** (integral of refractive index), while group delay measures the **group path** (integral of group refractive index). For a dispersive medium: `phase_delay = -group_delay` (opposite sign). So increasing TEC causes increasing group delay but decreasing phase delay. The Doppler shift from changing TEC is: `f_D = -(f/c) · d(phase_path)/dt = (40.3/c) · (dTEC/dt) / f`.

**Validation:** On unambiguous channels (CHU 14.67 MHz, WWV 20/25 MHz), DC carrier phase should show smooth diurnal TEC variation. Compare dTEC from carrier phase with dTEC from consecutive group-delay estimates.

#### Implementation 3: Multi-Layer E/F Tomographic Constraints

**Files:**
- `src/hf_timestd/core/iono_tomography.py` (new module)
- `src/hf_timestd/core/physics_fusion_service.py` (wire in)

**Design:**
- Divide ionosphere into 2 shells: E-layer (90–150 km) and F-layer (150–500 km)
- Each of the 17 ray paths has a known geometry (elevation angle, azimuth) from `HFPropagationModel`
- Each path's sTEC = E_contribution + F_contribution, weighted by path length through each shell
- The 17 paths at different elevation angles provide geometric diversity
- Constrain with WAM-IPE/IRI Ne(h) profile shape (Chapman layer) but allow peak height and density to float
- Solve via constrained least squares (scipy.optimize.minimize with bounds)
- Output: E-layer TEC, F-layer TEC, effective hmF2, per-path residuals

**Key insight:** Low-elevation paths (BPM: ~5–10°) traverse more E-layer relative to F-layer than high-elevation paths (CHU: ~30–40°). This geometric diversity separates the two contributions.

**Validation:** E-layer TEC should vanish at night (no solar ionization). F-layer TEC should show smooth diurnal variation. Sporadic-E events should appear as sudden E-layer TEC enhancements.

#### Implementation 4: VTEC Map Generation

**Files:**
- `src/hf_timestd/core/vtec_mapper.py` (new module)
- `src/hf_timestd/core/physics_fusion_service.py` (wire in)

**Design:**
- Convert each sTEC to vTEC: `vTEC = sTEC × cos(χ)` where χ is zenith angle at the ionospheric pierce point (IPP)
- Compute IPP for each path at the assumed shell height (from Implementation 3)
- The 17 IPPs span from receiver location toward each transmitter
- Fit a 2D polynomial surface (or thin-plate spline) to vTEC at the IPPs
- Output as IONEX-format file (standard GPS TEC map format, 2-hour cadence)
- Also write to L3 HDF5 for web-api consumption

**Validation:** Compare generated vTEC map with downloaded IONEX (GPS-derived) maps. Correlation should be > 0.7 for daytime, lower at night (fewer paths active).

---

### 🏗️ ARCHITECTURE REFERENCE

#### TEC Data Flow (Current — to be improved this session)

```
MetrologyEngine.process_minute()
  ├─ TickMatchedFilter → L2/tick_phase (carrier phase, ~55/min/station)
  ├─ TickEdgeDetector → L2/tick_timing (timing, ~55/min/station)
  └─ L1MetrologyMeasurement → L2/timing_measurements (ToA, D_clock, mode)
       ↓
  PhysicsFusionService.process_minute()  [timestd-physics, 60s poll]
    ├─ _read_l2_slice() → group by (station, mode)
    ├─ TECEstimator.estimate_tec() → naive 1/f² regression on raw ToA
    └─ _write_tec_records() → L3/tec HDF5
       ↓
  MultiBroadcastFusion._compute_tec()  [timestd-fusion, 8s cycle]
    ├─ TECEstimator.estimate_tec() → same naive regression
    ├─ TEC_VALIDATED if R²>0.9 and 5≤TEC≤100 TECU
    └─ Confidence boost for validated measurements
       ↓
  IonosphericReanalysis (hourly offline)
    ├─ _estimate_tec_cleaned() → D_clock-based, median per freq, IQR weights
    └─ L3C/propagation_stats + L3A/tec (reanalyzed)
```

**Target data flow after this session:**

```
MetrologyEngine.process_minute()
  ├─ TickMatchedFilter → L2/tick_phase (carrier phase)
  ├─ TickEdgeDetector → L2/tick_timing
  └─ L1MetrologyMeasurement → L2/timing_measurements (D_clock, mode)
       ↓
  PhysicsFusionService.process_minute()  [improved]
    ├─ BayesianTECEstimator (D_clock input, mode priors, N≥3 confidence)
    ├─ CarrierPhaseTEC (dTEC/dt from phase rate, anchored to group-delay TEC)
    ├─ IonoTomography (E/F layer separation from 17-path geometry)
    ├─ VTECMapper (IPP → 2D surface → IONEX output)
    └─ L3/tec, L3/dtec, L3/tomography, L3/vtec_map HDF5 + IONEX
```

#### Key Files (This Session Focus)

| File | Purpose | Priority |
|------|---------|----------|
| `src/hf_timestd/core/tec_estimator.py` | Current TEC estimator — audit + rewrite | **Critical** |
| `src/hf_timestd/core/physics_fusion_service.py` | Physics service — audit data flow + wire new estimators | **Critical** |
| `src/hf_timestd/core/multi_broadcast_fusion.py` | Fusion TEC — audit lines 3448–3561 | **Critical** |
| `src/hf_timestd/core/ionospheric_reanalysis.py` | Best current TEC approach — promote to real-time | **Critical** |
| `src/hf_timestd/core/propagation_model.py` | Iono delay computation — audit double-counting | High |
| `src/hf_timestd/core/ionospheric_model.py` | IonosphericDelayCalculator — audit 2× factor | High |
| `src/hf_timestd/core/iono_data_service.py` | WAM-IPE/GIRO data — verify production status | High |
| `src/hf_timestd/core/tick_matched_filter.py` | Carrier phase extraction — verify continuity | High |
| `web-api/services/phase_service.py` | Phase/Doppler dashboard — extend for dTEC | Medium |
| `web-api/services/propagation_service.py` | Propagation web API — extend for tomography | Medium |
| `tests/core/test_tec_estimator_diagnostics.py` | TEC tests — extend significantly | High |
| `tests/test_propagation_model.py` | Propagation tests — verify iono delay | High |

#### Key Files (Reference — do not modify unless audit reveals bugs)

| File | Purpose |
|------|---------|
| `src/hf_timestd/core/metrology_engine.py` | `process_minute()`, edge ensemble, physics validation |
| `src/hf_timestd/core/tick_edge_detector.py` | `TickEdgeDetector` — per-second onset edge detection |
| `src/hf_timestd/core/broadcast_specs.py` | 17 broadcast definitions, tone schedules |
| `src/hf_timestd/core/wwvh_discrimination.py` | Doppler estimation from per-tick phases |
| `src/hf_timestd/models/broadcast.py` | Station locations, frequencies |

### Service Inventory

| Service | Purpose | Logs |
|---------|---------|------|
| `timestd-core-recorder` | RTP → raw buffer (authoritative timestamps) | journalctl |
| `timestd-metrology` | IQ → L1/L2 measurements + tick phase extraction | `/var/log/hf-timestd/phase2-*.log` |
| `timestd-fusion` | Multi-broadcast fusion → Chrony (WatchdogSec=120) | `/var/log/hf-timestd/fusion.log` |
| `timestd-physics` | TEC estimation, L3 physics products | `/var/log/hf-timestd/physics.log` |
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

### HamSCI 2026 Workshop Abstract (2026-02-14)

Presentation abstract written and committed: `docs/HAMSCI_2026_WORKSHOP_ABSTRACT.md`. Updated project description reflecting current system capabilities. Six forward-looking recommendations for TEC optimization. Recommendations 1, 2, 3, 6 are the implementation targets for the next session.

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

## ✅ Success Criteria — This Session

1. **TEC pipeline audit complete** — all 17 concerns above investigated, findings documented.
2. **Bayesian TEC estimator implemented** — D_clock input, mode priors, N≥3 confidence requirement, proper rejection of mode-mixed measurements.
3. **Carrier-phase dTEC implemented** — reads L2/tick_phase, computes dTEC/dt from Doppler, integrates, anchors to group-delay TEC.
4. **Multi-layer tomography implemented** — E/F separation from 17-path geometry, constrained by Ne(h) profile shape.
5. **VTEC map generation implemented** — IPP computation, 2D surface fit, IONEX output format.
6. **Tests extended** — mode-mixing rejection, N=2 confidence cap, dTEC consistency, E-layer nighttime null, VTEC vs IONEX comparison.
7. **Production deployment** — `update-production.sh`, verify physics service produces improved TEC estimates.
