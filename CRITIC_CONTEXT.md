# NEVER CHANGE THE FOLLOWING PRIMARY INSTRUCTION!!!

**Primary Instruction:** In this context you will perform a critical review of the HF Time Standard (hf-timestd) project, either in its entirety or in a specific component, as specified by the user. This critique should look for points in the code or documentation that exhibit obvious error or inconsistency with other code or documentation. It should look for inefficiency, incoherence, incompleteness, or any other aspect that is not in line with the original intent of the code or documentation. It should also look for obsolete, deprecated, or "zombie" code that should be removed. Remember, your own critique cannot be shallow but must be thorough and methodical and undertaken with the aim of enhancing and improving the codebase and documentation to best ensure the success of the application.

Make your criticism from the perspective of 1) a user of the system, 2) a metrologist, 3) a ionospheric scientist, and 4) a software engineer. These perspectives can differ in their priorities and interests, and your critique should reflect this. For example, a user of the system will be most interested in the accuracy and reliability of the system, while a metrologist will be most interested in the precision and traceability of the system, while an ionospheric scientist will be most interested in the ionospheric conditions and their impact on the system, and a software engineer will be most interested in the code quality, maintainability, coverage and adequacy of testing,and the resilience and robustness of the system over time and in edge cases. Ultimately, however, a consensus of these perspectives should guide your critique in service of the meeting the application's objectives.

# NEVER CHANGE THE PRECEEDING PRIMARY INSTRUCTION!!!

# The following secondary instruction and information will guide your critique in this particular session (the instructions below will vary from session to session)

---

## 📋 NEXT SESSION: PHASE & DOPPLER METHODOLOGY REVIEW + WEB-API-UI EXPOSURE

**Objective:** Two-part session:

1. **Critically review the phase and Doppler shift extraction methodology** for weaknesses, errors, and missed opportunities. The system now extracts three distinct phase measurements per tick window from IQ samples. These must be verified for correctness, physical meaning, and consistency before being exposed to users.

2. **Expose phase and Doppler data through the web-api-ui** with visualizations that reveal ionospheric dynamics. Phase drift → Doppler shift. Phase jumps → propagation mode changes. Scintillation patterns → ionospheric irregularities. The data should correlate with mode of propagation (1-hop F, 2-hop F, sporadic E, etc.).

---

## 🎯 FOUR PERSPECTIVES ON PHASE/DOPPLER

### 1. The User Perspective
- **"What is the ionosphere doing right now?"** — Phase drift rate (Doppler) as a real-time indicator of ionospheric motion
- **"When did the propagation mode change?"** — Phase discontinuities mark mode transitions (e.g., 1F→2F, F→sporadic E)
- **"Which path is more stable?"** — Compare WWV vs WWVH phase stability on the same frequency

### 2. The Metrologist Perspective
- **Phase measurement validity** — Is `carrier_phase_rad` actually tracking the RF carrier, or is it an artifact of the processing? What is the measurement noise floor?
- **Ambiguity** — Phase wraps at ±π. Unwrapping is needed for Doppler extraction but introduces errors at discontinuities. How robust is the unwrapping?
- **Consistency** — Do the three phase measurements (audio, carrier, DC) agree where they should? On unambiguous channels, `dc_carrier_phase_rad` and `carrier_phase_rad` should differ by a constant (the tone frequency's contribution).
- **Uncertainty** — What is the phase measurement uncertainty as a function of SNR? At 10 dB SNR, phase noise is ~18° (0.3 rad). At 20 dB, ~6° (0.1 rad).

### 3. The Ionospheric Scientist Perspective
- **Doppler from phase rate** — `f_Doppler = -(1/2π) × dφ/dt`. For 10 MHz carrier, 1 rad/s phase drift = 0.16 Hz Doppler = 4.8 m/s ionospheric motion.
- **Mode identification** — Different propagation modes have different path lengths → different absolute phases. Mode transitions cause phase jumps of known magnitudes (calculable from geometry).
- **Multipath** — When two modes are present simultaneously, the phase oscillates (beating). The beat frequency reveals the path length difference.
- **Diurnal signatures** — Sunrise/sunset cause systematic Doppler shifts as the ionosphere rises/descends. These should be visible as smooth phase ramps.
- **Scintillation** — Rapid phase fluctuations (S4 > 0.3) indicate small-scale ionospheric irregularities. Phase scintillation index (σ_φ) is a standard metric.
- **Correlation across frequencies** — Phase changes should scale with frequency (dispersive ionosphere). 15 MHz should show 1.5× the phase change of 10 MHz for the same TEC change.

### 4. The Programmer Perspective
- **Is the IQ mix-down correct?** — The `exp(-j·2π·f_tone·t)` mixer must use the correct `t` (time relative to window start, not absolute). Sign convention matters.
- **Is the DC phasor meaningful?** — `mean(IQ)` over a tick duration (~5ms at 20 kHz = 100 samples) — is this enough samples for a stable phasor estimate?
- **Window-to-window continuity** — Overlapping 5-second windows should produce smoothly varying phase. If phase jumps ~1.7 rad between windows (as currently observed), something is wrong.
- **Template artifact** — The composite template sums multiple tick positions. If the template phase reference shifts between windows, the extracted phase will jump even if the signal phase is continuous.
- **HDF5 data volume** — ~55 rows/station/minute × 3 stations × 9 channels = ~1485 rows/minute. At ~200 bytes/row, that's ~430 MB/day. Is this sustainable?

---

## 📡 PHASE EXTRACTION ARCHITECTURE (Implemented 2026-02-11)

### Three Phase Measurements

| Field | Method | Physical Meaning | Best Channel Type |
|-------|--------|-----------------|-------------------|
| `phase_rad` | `atan2(corr_sin, corr_cos)` on AM envelope | Audio modulation phase of the tone | All (but least informative) |
| `carrier_phase_rad` | `IQ × exp(-j·2π·f_tone·t)` → `angle(mean)` | RF carrier phase at tone frequency | All (primary ionospheric observable) |
| `dc_carrier_phase_rad` | `angle(mean(IQ))` over tick duration | Bare RF carrier phase (DC phasor) | **Unambiguous only** (CHU, WWV 20/25) |

### Unambiguous vs Shared Channels

| Channel | Station(s) | DC Carrier Meaningful? | Notes |
|---------|-----------|----------------------|-------|
| CHU_3330 | CHU only | **Yes** — USB preserved carrier | Strongest DC phasor |
| CHU_7850 | CHU only | **Yes** — USB preserved carrier | Strongest DC phasor |
| CHU_14670 | CHU only | **Yes** — USB preserved carrier | Strongest DC phasor |
| WWV_20000 | WWV only | **Yes** — AM carrier | Clean single-station |
| WWV_25000 | WWV only | **Yes** — AM carrier | Clean single-station |
| SHARED_2500 | WWV+WWVH+BPM | No — mixed carriers | Use `carrier_phase_rad` per station |
| SHARED_5000 | WWV+WWVH+BPM | No — mixed carriers | Use `carrier_phase_rad` per station |
| SHARED_10000 | WWV+WWVH+BPM | No — mixed carriers | Use `carrier_phase_rad` per station |
| SHARED_15000 | WWV+WWVH+BPM | No — mixed carriers | Use `carrier_phase_rad` per station |

### Modulation Types

- **CHU**: USB with preserved carrier. IQ baseband has strong DC (carrier) + 1000 Hz sideband (tone). `Re(IQ)` recovers audio. DC phasor is the carrier phase directly.
- **WWV/WWVH**: Conventional AM. IQ baseband has DC (carrier) + ±1000/1200 Hz sidebands. `|IQ| - DC` recovers envelope. On shared channels, DC is a mix of multiple AM carriers.
- **BPM**: Conventional AM at 1000 Hz, similar to WWV.

### Early Results (2026-02-11, first hour of data)

| Channel | Type | audio σ_φ | carrier σ_φ | dc σ_φ | dc stability gain |
|---------|------|-----------|-------------|--------|-------------------|
| CHU_14670 | Unambiguous | 2.01 rad | 1.79 rad | **1.55 rad** | **1.30×** |
| CHU_7850 | Unambiguous | 1.88 rad | 1.82 rad | **1.68 rad** | **1.12×** |
| WWV_25000 | Unambiguous | 1.58 rad | 1.63 rad | **1.46 rad** | **1.08×** |
| WWV_20000 | Unambiguous | 1.54 rad | 1.60 rad | 1.83 rad | 0.84× |
| SHARED_10000 | Mixed | 1.67 rad | 1.99 rad | 1.80 rad | 0.93× |
| SHARED_5000 | Mixed | 1.66 rad | 1.78 rad | 1.78 rad | 0.94× |

**Concern:** All σ_φ values are ~1.5–2.0 rad (approaching uniform random on [-π,π] which has σ=1.81 rad). This suggests the phase is not being tracked coherently across windows. Possible causes:
1. Composite template phase reference shifts between windows
2. Window overlap not accounting for phase continuity
3. The 5ms tick (100 samples at 20 kHz) provides too few cycles for stable phase
4. Bandpass filter in tick_matched_filter introduces phase shifts that vary with window position

---

## 🔍 KNOWN ISSUES TO INVESTIGATE

### 1. Phase Continuity Problem (HIGH PRIORITY)
All phase measurements show σ ≈ 1.5–2.0 rad across consecutive windows. For a stable carrier, consecutive 5-second windows should show phase changes of order `2π × f_Doppler × Δt` — typically < 0.1 rad for sub-Hz Doppler. The observed ~1.7 rad jumps are 10–100× too large.

**Hypotheses to test:**
- **Template phase reference:** The composite template in `_build_composite_template()` sums tick templates at different positions within the window. If the template's phase reference changes between windows (e.g., because different seconds are valid), the correlation phase will jump.
- **Bandpass filter phase:** `sosfiltfilt` is zero-phase, but the filter is applied per-window. Edge effects at window boundaries could introduce phase artifacts.
- **IQ mix-down time reference:** The `t_tick` array in `_correlate_window` starts at 0 for each tick. If the tick's absolute time within the minute matters for the mixer phase, this introduces a window-dependent phase offset. **This is likely the primary bug** — the mixer should use absolute time, not tick-relative time.
- **Sample count:** 5ms × 20 kHz = 100 samples. At 1000 Hz, that's 5 cycles. Phase estimation from 5 cycles has σ ≈ 1/(SNR_linear × √N) — at 10 dB SNR, σ ≈ 0.1 rad. So the noise floor is NOT the problem; the jumps are systematic.

### 2. Cross-Frequency Discrimination (RESOLVED 2026-02-11)
5ms template had 33% cross-response between 1000↔1200 Hz. Fixed with cross-frequency gate requiring 3 dB advantage. WWV/WWVH now show distinct detection rates on shared channels.

### 3. Threshold Calibration (ONGOING)
`BASE_CORR_SNR_DB = 8.0` is the dominant rejection reason. `L2/detection_attempts` HDF5 product collects all attempts (detected + rejected) with rejection reasons for offline analysis.

### 4. Memory Leak
CHU metrology services grow to 2.5GB RSS over 12+ hours. Root cause unknown.

---

## 🏗️ ARCHITECTURE REFERENCE

### Data Flow for Phase Measurements

```
ka9q-radio (radiod) → RTP multicast → timestd-core-recorder → Raw IQ Buffer
   (GPS+PPS, ~50μs)                                               ↓
                                                          timestd-metrology
                                                           ↓ (per channel)
                                                    TickMatchedFilter.process_minute()
                                                      ↓ overlapping 5-sec windows
                                                    _correlate_window()
                                                      ├─ AM envelope → quadrature corr → phase_rad
                                                      ├─ IQ × exp(-j2πft) → mean → carrier_phase_rad
                                                      └─ mean(IQ) → dc_carrier_phase_rad
                                                           ↓
                                                    TickDetectionResult (per window)
                                                           ↓
                                                    MetrologyService → L2/tick_phase (HDF5)
                                                           ↓
                                                    timestd-web-api (FastAPI)
                                                           ↓
                                                    [NEW] Phase/Doppler visualization
```

### Key Files for This Session

| File | Purpose | Priority |
|------|---------|----------|
| `src/hf_timestd/core/tick_matched_filter.py` | **Phase extraction code** — review `_correlate_window()` and `_build_composite_template()` | **Critical** |
| `src/hf_timestd/core/metrology_engine.py` | Cross-freq gate, RTP measurement loop, tick filter invocation | High |
| `src/hf_timestd/core/metrology_service.py` | tick_phase_writer, persists all 3 phase fields to HDF5 | High |
| `src/hf_timestd/schemas/l2_tick_phase_v1.json` | Schema: phase_rad, carrier_phase_rad, dc_carrier_phase_rad | Reference |
| `web-api/routers/stability.py` | Existing stability/ADEV API — model for new phase API | Reference |
| `web-api/static/metrology.html` | Existing metrology dashboard — model for phase UI | Reference |
| `web-api/static/css/styles.css` | Dark theme styles | Reference |
| `web-api/static/js/common.js` | Shared JS utilities | Reference |

### HDF5 Data Products

| Product | Path | Rate | Fields |
|---------|------|------|--------|
| `L2/tick_phase` | `phase2/{CH}/tick_phase/` | ~55 rows/station/min | phase_rad, carrier_phase_rad, dc_carrier_phase_rad, timing_offset_ms, snr_db, coherence_quality, window position |
| `L2/tick_timing` | `phase2/{CH}/tick_timing/` | ~1 row/station/min | Aggregate timing from tick filter |
| `L2/detection_attempts` | `phase2/{CH}/detection_attempts/` | ~45 rows/min | All RTP measurement attempts with rejection reasons |
| `L2/timing_measurements` | `phase2/{CH}/timing_measurements/` | ~1-15 rows/min | Accepted timing measurements |

### Service Inventory

| Service | CPUAffinity | Purpose |
|---------|-------------|--------|
| `timestd-core-recorder` | 0-7 | RTP → raw buffer (authoritative timestamps) |
| `timestd-metrology` | 0-7 (taskset) | IQ → L1/L2 measurements + tick phase extraction |
| `timestd-fusion` | 0-7 | Multi-broadcast fusion → Chrony |
| `timestd-web-api` | 0-7 | REST API + dashboard (FastAPI, port 8000) |
| **radiod** | **8-15** | **Real-time USB/FFT (uncontested L3 cache)** |

---

## 🎨 PHASE/DOPPLER VISUALIZATION GOALS

### What to Show

1. **Phase vs Time** — Per-channel, per-station phase time series (unwrapped). 24h plot reveals diurnal ionospheric cycle. Phase ramps = Doppler. Jumps = mode changes.

2. **Doppler Shift vs Time** — Derived from phase rate: `f_D = -(1/2π) dφ/dt`. Shows ionospheric vertical motion. Sunrise/sunset produce characteristic Doppler signatures.

3. **Multi-Frequency Phase Comparison** — Same station on multiple frequencies. Ionospheric phase is dispersive: Δφ ∝ TEC/f. If 10 MHz shows X rad drift, 15 MHz should show ~0.67X. Deviations indicate multipath or mode changes.

4. **Phase Scintillation Index (σ_φ)** — Standard deviation of detrended phase over sliding windows. High σ_φ = ionospheric irregularities. Compare with S4 amplitude scintillation.

5. **DC Carrier Phase (Unambiguous Channels)** — CHU and WWV 20/25 MHz: the bare carrier phase, independent of tone detection. This is the cleanest ionospheric observable. Show alongside tone-derived carrier phase for consistency check.

6. **Mode Transition Detection** — Phase jumps exceeding a threshold (e.g., > 1 rad in < 10s) flagged as mode transitions. Annotate on the phase plot.

### Visual Design
- **Station colors:** WWV=blue, WWVH=amber, CHU=green, BPM=red
- **Phase plots:** Unwrapped phase on left y-axis, Doppler on right y-axis
- **Time axis:** UTC, 24h default with zoom capability
- **Frequency panels:** Stacked vertically, shared time axis, for multi-frequency comparison
- **Dark theme:** Consistent with existing dashboard

### API Endpoints Needed

- `GET /api/phase/timeseries?channel=...&station=...&start=...&end=...` — Phase time series from L2/tick_phase
- `GET /api/phase/doppler?channel=...&station=...&start=...&end=...` — Derived Doppler (compute server-side from phase rate)
- `GET /api/phase/scintillation?channel=...&station=...&start=...&end=...` — σ_φ time series
- `GET /api/phase/summary` — Current phase/Doppler state across all channels

---

## ✅ RESOLVED IN PREVIOUS SESSIONS

### Phase Continuity Fix + Doppler UI (2026-02-11, session 2)
- **Phase continuity bug fixed**: IQ mixer used window-relative time (t=0 per tick), causing ~1.7 rad phase jumps. Fixed: buffer-relative time (sample_index/sample_rate), independent of RTP/GPS/NTP timing authority.
- **Per-tick phase extraction**: Phase now extracted from individual ticks (not whole 5-second window), phasors combined coherently. Eliminates inter-tick noise dilution.
- **Regression tests**: `test_carrier_phase_continuity` and `test_dc_carrier_phase_stability` verify σ < 0.3 rad.
- **Phase/Doppler web dashboard**: 4 API endpoints (timeseries, doppler, scintillation, summary) + visualization page with carrier phase, Doppler, σ_φ, and DC carrier phase plots.
- See: `docs/changes/SESSION_2026_02_11_PHASE_CONTINUITY_AND_DOPPLER_UI.md`

### Phase Extraction & Cross-Talk Fix (2026-02-11, session 1)
- Three-tier phase extraction implemented: audio phase, IQ carrier phase, DC carrier phasor
- Cross-frequency discrimination gate: 3 dB advantage required between 1000↔1200 Hz
- WWV/WWVH detection rates now distinct on shared channels (72% vs 55%)
- CHU modulation comments corrected: USB with preserved carrier, not DSB-SC
- Allan deviation UI: standard time range buttons
- See: `docs/changes/SESSION_2026_02_11_PHASE_EXTRACTION_AND_CROSSTALK.md`

### CHU FSK Decoder — USB Sidecar Channels (2026-02-10)
- Dedicated USB-preset sidecar channels for FSK decoding
- See: `docs/SESSION_2026-02-10_CHU_FSK_USB_SIDECAR.md`

### Timing Accuracy (2026-02-06)
- Four bugs fixed: circular calibration, broken GPS ground truth, dead Kalman filter, missing serialization
- Mean discrepancy reduced from -495ms to ~1ms

### Pipeline Offset Calibration (2026-02-09)
- Removed entirely — radiod's RTP timestamps are authoritative
- Verified: CHU_3330 error=-0.31ms, CHU_7850 error=-3.01ms at 39.5dB SNR

### HDF5 Crash Safety (2026-02-06)
- SWMR eliminated — open-write-close per measurement

---

## ✅ Success Criteria — This Session (2026-02-11, session 2)

### Part 1: Methodology Review
- ✅ **Verify IQ mix-down correctness** — Found and fixed: mixer used window-relative time. Now uses buffer-relative (sample_index/sample_rate), independent of RTP/GPS/NTP.
- ✅ **Diagnose phase continuity problem** — Root cause: t_tick started at 0 per tick + phase extracted over whole 5s window. Fixed: per-tick extraction with buffer-relative time.
- ✅ **Verify DC phasor on unambiguous channels** — Regression test confirms σ < 0.3 rad for stable carrier.
- ⬚ **Consistency check** — carrier_phase_rad vs dc_carrier_phase_rad offset verification (needs production data after restart)
- ⬚ **Quantify phase noise floor** — σ_φ vs SNR (needs production data after restart)
- ✅ **Fix any bugs found** — Buffer-relative time mixer, per-tick extraction, type hint fix, regression tests added.

### Part 2: Web-API-UI Visualization
- ✅ **Phase time series plot** — per-channel, per-station, unwrapped, with time range controls
- ✅ **Doppler shift derivation and display** — from phase rate, with configurable smoothing
- ⬚ **Multi-frequency comparison** — verify dispersive scaling (needs production data)
- ✅ **Phase scintillation index** — σ_φ time series with 0.3 rad threshold line
- ⬚ **Mode transition annotations** — phase jumps flagged on plots (deferred to next session)
- ✅ **DC carrier phase on unambiguous channels** — dedicated plot for CHU + WWV 20/25 MHz
- ⬚ **Correlation with propagation mode** — needs production data to validate

## 📋 NEXT SESSION: PRODUCTION VALIDATION + MODE DETECTION

**Objective:** After restarting metrology services with the phase continuity fix:

1. **Validate phase continuity in production** — Confirm σ_φ drops from ~1.7 rad to < 0.3 rad on real data
2. **Carrier vs DC phase consistency** — On CHU channels, verify carrier_phase_rad and dc_carrier_phase_rad differ by a predictable constant
3. **Phase noise floor** — Measure σ_φ vs SNR across channels, compare with theoretical 1/(SNR_linear × √N)
4. **Multi-frequency dispersive check** — Same station on multiple frequencies: Δφ ∝ TEC/f
5. **Mode transition detection** — Implement automatic detection of phase jumps > 1 rad in < 10s, annotate on plots
6. **Diurnal Doppler signatures** — Verify sunrise/sunset produce expected smooth Doppler ramps
7. **Threshold calibration** — Use detection_attempts data to calibrate BASE_CORR_SNR_DB and MIN_BPM_SNR_DB
