# NEVER CHANGE THE FOLLOWING PRIMARY INSTRUCTION!!!

**Primary Instruction:** In this context you will perform a critical review of the HF Time Standard (hf-timestd) project, either in its entirety or in a specific component, as specified by the user. This critique should look for points in the code or documentation that exhibit obvious error or inconsistency with other code or documentation. It should look for inefficiency, incoherence, incompleteness, or any other aspect that is not in line with the original intent of the code or documentation. It should also look for obsolete, deprecated, or "zombie" code that should be removed. Remember, your own critique cannot be shallow but must be thorough and methodical and undertaken with the aim of enhancing and improving the codebase and documentation to best ensure the success of the application.

Make your criticism from the perspective of 1) a user of the system, 2) a metrologist, 3) a ionospheric scientist, and 4) a software engineer. These perspectives can differ in their priorities and interests, and your critique should reflect this. For example, a user of the system will be most interested in the accuracy and reliability of the system, while a metrologist will be most interested in the precision and traceability of the system, while an ionospheric scientist will be most interested in the ionospheric conditions and their impact on the system, and a software engineer will be most interested in the code quality, maintainability, coverage and adequacy of testing, and the resilience and robustness of the system over time and in edge cases. Ultimately, however, a consensus of these perspectives should guide your critique in service of meeting the application's objectives.

# NEVER CHANGE THE PRECEEDING PRIMARY INSTRUCTION!!!

# The following secondary instruction and information will guide your critique in this particular session (the instructions below will vary from session to session)

---

## 📋 NEXT SESSION: dTEC/dt QUALITY DEGRADATION DIAGNOSIS

**Task:** The carrier-phase dTEC/dt assessments have degraded from showing clear diurnal ionospheric variation to resembling noise. Systematically investigate the physics, methodology, and measurement layers of the dTEC pipeline to identify what changed and what is broken. The goal is to either restore the diurnally-varying dTEC/dt signal or identify the upstream data quality problem that is masking it.

**Symptom:** dTEC/dt time series on the web dashboard (`/tec/dtec` endpoint) now look like uncorrelated noise across all station-frequency paths, rather than the smooth, correlated diurnal TEC variation seen in earlier data (late February 2026). The diurnal pattern — rising TEC after sunrise, peak near local solar noon, declining after sunset — should be clearly visible on paths with decent SNR (>15 dB).

---

## System Context

- **System:** hf-timestd v6.9.1 (March 5, 2026) — multi-broadcast HF time transfer and ionospheric measurement
- **Focus Area:** dTEC/dt pipeline quality — from L1 carrier phase measurement to L3 dTEC products
- **Recent Fix (2026-03-04):** RTP timing mismatch causing ~4500s minute boundary offset. Fixed by removing the timing poll thread and seeding GPS/RTP mapping from `channel_info`. See `docs/changes/SESSION_2026_03_04_RTP_TIMING_FIX.md`.
- **Data Output Directories:**
  - Per-minute dTEC summaries: `/var/lib/timestd/phase2/science/dtec/`
  - Full ~1s resolution time series: `/var/lib/timestd/phase2/science/dtec_timeseries/`
  - Differential dTEC frequency pairs: `/var/lib/timestd/phase2/science/dtec_diff/`
  - L2 tick_phase (carrier phase per tick): `/var/lib/timestd/phase2/<CHANNEL>/tick_phase/`

---

## 1. The Three Investigation Axes

The diagnosis must proceed along three independent axes. A flaw in ANY of them will produce noise-like dTEC/dt. The agent must verify each axis independently before concluding.

### Axis 1: PHYSICS — Are the equations correct?

The carrier-phase dTEC pipeline implements:

```
φ(t) = (2πf/c) ∫ n_φ ds       # carrier phase is integral of refractive index
f_D = dφ/dt / (2π)              # Doppler from phase rate
dTEC/dt = -f_D · c · f / 40.3   # Doppler → dTEC rate (TECU/s)
TEC(t) = ∫ dTEC/dt · dt         # integrate rate → relative TEC
```

**Key files:**
- `src/hf_timestd/core/carrier_tec.py` — `CarrierTECEstimator.compute_dtec_from_phase()` (lines 104-235)
- `src/hf_timestd/core/physics_fusion_service.py` — `_process_carrier_dtec()` (lines 866-1080)

**Check specifically:**
- Sign convention: `dtec_rate = -doppler * C_LIGHT * freq_hz / K_GROUP_DELAY` — is the negative sign correct? Increasing TEC causes phase advance (positive Doppler in our convention?).
- Units: `frequency_mhz * 1e6` gives Hz. `K_GROUP_DELAY = 40.3` (m³/s²). `C_LIGHT = 299792458.0` (m/s). Result should be el/m²/s, then `/1e16` for TECU/s.
- Phase unwrapping: `np.unwrap()` on `carrier_phase_rad`. If the input phases are noisy or sparsely sampled (~1s intervals), unwrapping can introduce large spurious jumps.
- Cycle-slip detection: threshold `|d²φ/dt²| > 5 Hz/s` — is this appropriate for HF ionospheric Doppler? Typical HF Doppler is 0.01-1 Hz; 5 Hz/s acceleration may be too loose.
- Integration: trapezoidal with 120s gap threshold. Are there edge effects?
- GNSS VTEC anchor: applied as a DC offset to integrated dTEC at mid-minute. This is methodologically sound only if the integration drift within one minute is small compared to VTEC accuracy (~1 TECU).

### Axis 2: METHODOLOGY — Is the data pipeline intact?

The dTEC pipeline has five stages. A break at any stage produces garbage:

```
Stage 1: tick_matched_filter.py → carrier_phase_rad per tick (~55/min)
Stage 2: metrology_service.py → writes carrier_phase_rad to L2/tick_phase HDF5
Stage 3: physics_fusion_service.py → reads L2/tick_phase, calls carrier_tec.py
Stage 4: carrier_tec.py → unwrap, differentiate, convert, integrate
Stage 5: physics_fusion_service.py → write L3/dtec summary + timeseries HDF5
```

**Key files at each stage:**
- **Stage 1:** `src/hf_timestd/core/tick_matched_filter.py` — `TickMatchedFilter._compute_ensemble()` produces `carrier_phase_rad` by coherently combining per-tick IQ phasors at the tone frequency. This is the raw measurement.
- **Stage 2:** `src/hf_timestd/core/metrology_service.py` — writes `carrier_phase_rad` field to tick_phase HDF5 via `write_measurements_batch()`.
- **Stage 3:** `src/hf_timestd/core/physics_fusion_service.py` — `_read_tick_phase_minute()` reads from HDF5. Check: epoch = `minute_boundary + window_center_second`. Is `minute_boundary` a Unix timestamp? Is `window_center_second` in [0, 60)?
- **Stage 4:** `src/hf_timestd/core/carrier_tec.py` — `compute_dtec_from_records()`. Check: are the epochs monotonically increasing? Are there duplicate timestamps? Are the phases in radians (not degrees)?
- **Stage 5:** `src/hf_timestd/core/physics_fusion_service.py` — `_process_carrier_dtec()`. Check: `dtec_rate_tecu_per_s` in the summary record is `np.mean(rate_arr)` — is averaging the rate over the minute meaningful, or does it cancel the signal?

**Critical methodological question:** The `carrier_phase_rad` field in `tick_matched_filter.py` is computed by mixing the IQ signal at the tone frequency and taking `np.angle(np.mean(mixed))`. This gives the **phase of the carrier at the tone frequency relative to the local oscillator**. For dTEC, we need the **ionospheric component** of phase change. If the dominant contribution to `dφ/dt` is NOT ionospheric (e.g., it's dominated by transmitter instability, receiver LO drift, or geometric Doppler from Earth rotation), then dTEC/dt will be noise.

**Important:** The carrier phase measurement includes ALL sources of phase change: ionospheric, geometric (Earth rotation / satellite motion — N/A for fixed ground stations, but relevant for the propagation path geometry change with ionospheric layer height), transmitter phase stability (NIST/NRC rubidium standards are ~10⁻¹² — negligible), and receiver LO stability (GPSDO at ~10⁻¹² — negligible). The dominant non-ionospheric term for a fixed ground-to-ground HF link is **multipath fading**: when two propagation modes (e.g., 1F2 and 2F2) interfere, the composite phase jumps rapidly. This would produce exactly the noise-like signature observed.

### Axis 3: MEASUREMENTS — Is the upstream L2 data healthy?

Even if the physics and methodology are correct, garbage in → garbage out. The investigation must verify:

1. **Are tick_phase HDF5 files being written?** Check `/var/lib/timestd/phase2/<CHANNEL>/tick_phase/` for recent files with non-zero size.
2. **Do they contain `carrier_phase_rad` values that vary smoothly within a minute?** Read a sample file and plot phase vs. tick position. If the phases are random, the measurement is broken.
3. **Is the phase measurement consistent across minutes?** Read consecutive minutes and check for large inter-minute phase jumps. If every minute resets to a random phase, the integration in `carrier_tec.py` produces noise.
4. **Has the RTP timing fix (2026-03-04) affected phase extraction?** The `carrier_phase_rad` is computed from IQ samples at the detected tick position. If the minute boundary shifted by ~4500s and was then corrected, the tick positions within the buffer may have been wrong during the corrupted period. **Check data from before the timing corruption (Feb 25-27) vs. after the fix (Mar 5).**
5. **SNR trends:** If channel SNR has dropped (propagation conditions, antenna issue, interference), the phase measurements become noisier. Check `mean_snr_db` in the dTEC records — is it above 15 dB?
6. **Is the GNSS VTEC anchor working?** Check `anchor_status` in dTEC records. If anchoring fails (e.g., `live_vtec.py` stopped writing), the integrated dTEC drifts freely and the `dtec_mean_tecu` field is meaningless (only `dtec_rate_tecu_per_s` is valid).

---

## 2. Recent Changes That Could Have Caused Degradation

Review these commits and their effects on the dTEC pipeline:

| Date | Commit | Change | Potential Impact |
|------|--------|--------|-----------------|
| 2026-03-03 | `c3fd733` | Power of 10 rules: pre-allocated buffers in `metrology_engine.py`, `tick_matched_filter.py` | Buffer reuse could corrupt carrier phase if not reset properly between windows |
| 2026-03-02 | `5bab3b0` | Phase-engine native support, FSK decoupled listener removal | Changed stream_recorder tap architecture; could affect IQ sample delivery to tick_matched_filter |
| 2026-02-27 | `df1458f` | Phase 3 physics critique implementations (P1-B, P3-A, P3-B, P4-B, P4-C) | Multiple changes to carrier_tec.py and physics_fusion_service.py; added cycle-slip detection, phase unwrap quality gating |
| 2026-02-24 | `aa9c31a` | GNSS VTEC anchoring | Changed anchor source; if VTEC data stops, dTEC becomes unanchored |
| 2026-03-04 | (runtime) | RTP timing mismatch: minute boundaries were ~4500s off for several days | All L2 data written during the corrupted period has wrong minute boundaries; tick_phase epochs may be wrong |

---

## 3. Diagnostic Procedure

Execute these steps in order. Each step either clears or implicates one layer.

### Step 1: Verify L2 tick_phase data exists and is recent
```bash
ls -lt /var/lib/timestd/phase2/CHU_7850/tick_phase/ | head -5
ls -lt /var/lib/timestd/phase2/SHARED_5000/tick_phase/ | head -5
```

### Step 2: Sample carrier_phase_rad from a single minute
```python
import h5py, numpy as np
f = h5py.File('/var/lib/timestd/phase2/CHU_7850/tick_phase/CHU_7850_tick_phase_YYYYMMDD.h5', 'r')
# Find the latest minute, extract carrier_phase_rad vs window_center_second
# Plot: do the phases progress smoothly or jump randomly?
```

### Step 3: Check inter-minute phase continuity
Read `carrier_phase_rad` at tick 55 of minute N and tick 0 of minute N+1. If there's a large (>π/4) jump every minute boundary, the phase is not continuous across minutes and `np.unwrap()` in `carrier_tec.py` will inject spurious dTEC.

### Step 4: Compare known-good vs. current dTEC data
```python
# Compare Feb 25 (known good) vs Mar 5 (possibly degraded)
# Look at dtec_rate_tecu_per_s distribution, variance, correlation between paths
```

### Step 5: Check GNSS VTEC availability
```bash
ls -lt /var/lib/timestd/data/gnss_vtec/ | head -5
# Is live_vtec.py running? Is the ZED-F9P connected?
```

### Step 6: Check physics service logs for anomalies
```bash
grep -E "dTEC|anchor|unwrap|cycle.slip|BAD|noise" /var/log/hf-timestd/physics-fusion.log | tail -50
```

---

## 4. Key Files to Analyze

| File | Role | What to Check |
|------|------|--------------|
| `src/hf_timestd/core/tick_matched_filter.py` | Produces `carrier_phase_rad` from IQ | Phase measurement methodology; buffer reuse after Power-of-10 changes |
| `src/hf_timestd/core/metrology_service.py` | Writes tick_phase to HDF5 | Is `carrier_phase_rad` actually being written? Field name correct? |
| `src/hf_timestd/core/carrier_tec.py` | dTEC computation | Phase unwrapping, Doppler conversion, integration, noise floor estimation |
| `src/hf_timestd/core/physics_fusion_service.py` | Orchestrator | `_read_tick_phase_minute()` epoch construction, `_process_carrier_dtec()` anchor logic |
| `src/hf_timestd/core/tick_edge_detector.py` | Also produces `carrier_phase_rad` | Separate code path for tick-edge carrier phase; used for Doppler estimation |
| `web-api/routers/tec.py` | Dashboard data source | `/tec/dtec` reads from L3 dTEC HDF5; verify it's reading the right fields |
| `src/hf_timestd/core/binary_archive_writer.py` | Raw buffer timing | If minute boundaries are wrong, tick positions within the buffer are wrong |
| `src/hf_timestd/core/buffer_timing.py` | RTP→UTC conversion | Uses `gps_time_ns` / `rtp_timesnap` for timing; recently changed |

---

## 5. What "Good" dTEC/dt Looks Like

For reference, healthy dTEC/dt data should exhibit:

1. **Diurnal variation:** TEC rises from ~5 TECU at night to 20-60 TECU during the day (at solar maximum). The rate dTEC/dt peaks at ~0.01 TECU/s near sunrise/sunset.
2. **Correlated across paths:** All paths through similar ionospheric regions should show correlated dTEC variations. If WWV 5 MHz and WWV 10 MHz show uncorrelated dTEC, something is wrong in the measurement layer (the ionosphere is the same for both).
3. **Frequency-dependent magnitude:** Higher frequencies are less affected by the ionosphere. dTEC/dt magnitude should scale roughly as 1/f for paths through the same ionosphere.
4. **Smooth within a minute:** The ~55 carrier-phase samples per minute should show a smooth phase progression (Doppler is quasi-constant over 60 seconds for ionospheric changes). Scatter > 0.1 rad within a minute suggests measurement noise, not ionospheric signal.

---

## 6. Hypotheses to Test (Ranked by Likelihood)

1. **Multipath fading dominates carrier phase:** If propagation conditions have shifted (e.g., increased multimode overlap at current solar cycle phase), the measured carrier phase may be dominated by interference fading rather than ionospheric refraction. This is physics, not a bug — but the pipeline should detect and flag it (e.g., via phase scintillation index σ_φ or S4).

2. **Power-of-10 buffer reuse corrupted phase extraction:** The `c3fd733` commit introduced pre-allocated `_envelope_buffer` arrays. If the carrier phase extraction path shares or aliases these buffers, phases could be contaminated.

3. **Inter-minute phase discontinuity:** `carrier_phase_rad` is measured relative to a local reference within each tick window. If there is no inter-minute phase coherence, the integration in `carrier_tec.py` starts fresh each minute and the "integrated dTEC" is just noise around zero.

4. **Timing corruption period data poisoning:** L2 tick_phase data written during the ~4500s timing offset period (before Mar 4 fix) has wrong `minute_boundary` values. The physics service may be reading this corrupted data for recent dTEC plots.

5. **GNSS VTEC starvation:** If `live_vtec.py` is not running or the ZED-F9P lost lock, all dTEC records are unanchored and `dtec_mean_tecu` drifts randomly. Only `dtec_rate_tecu_per_s` remains valid.

6. **Upstream SNR collapse:** Propagation or hardware issue causing low SNR across all channels, making carrier phase measurements unreliable.
