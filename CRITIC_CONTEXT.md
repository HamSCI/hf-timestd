# NEVER CHANGE THE FOLLOWING PRIMARY INSTRUCTION!!!

**Primary Instruction:** In this context you will perform a critical review of the HF Time Standard (hf-timestd) project, either in its entirety or in a specific component, as specified by the user. This critique should look for points in the code or documentation that exhibit obvious error or inconsistency with other code or documentation. It should look for inefficiency, incoherence, incompleteness, or any other aspect that is not in line with the original intent of the code or documentation. It should also look for obsolete, deprecated, or "zombie" code that should be removed. Remember, your own critique cannot be shallow but must be thorough and methodical and undertaken with the aim of enhancing and improving the codebase and documentation to best ensure the success of the application.

Make your criticism from the perspective of 1) a user of the system, 2) a metrologist, 3) a ionospheric scientist, and 4) a software engineer. These perspectives can differ in their priorities and interests, and your critique should reflect this. For example, a user of the system will be most interested in the accuracy and reliability of the system, while a metrologist will be most interested in the precision and traceability of the system, while an ionospheric scientist will be most interested in the ionospheric conditions and their impact on the system, and a software engineer will be most interested in the code quality, maintainability, coverage and adequacy of testing, and the resilience and robustness of the system over time and in edge cases. Ultimately, however, a consensus of these perspectives should guide your critique in service of meeting the application's objectives.

# NEVER CHANGE THE PRECEEDING PRIMARY INSTRUCTION!!!

# The following secondary instruction and information will guide your critique in this particular session (the instructions below will vary from session to session)

---

## 📋 NEXT SESSION: DEPLOY, VALIDATE, AND CRITIQUE THE NEW PROPAGATION MODEL

**Objective:** Deploy the v6.7 real-time ionospheric propagation model to production, validate it against live data, and perform a thorough critique looking for bugs, missed opportunities, and architectural weaknesses.

**Context:** The propagation model has been implemented and tested offline (23 tests passing). It replaces the static vacuum × 1.15 delay model with frequency-dependent, time-varying group delay predictions using real-time WAM-IPE and GIRO ionospheric data. Multi-hop arrivals (1F, 2F, 3F, 1E) are now predicted with adaptive uncertainty windows. The model needs production deployment and validation against live HF observations.

---

## 🚨 CRITIQUE TARGETS FOR THIS SESSION

### 1. Deployment Integration — Wire IonoDataService into Production

The `IonoDataService` singleton exists but is NOT yet started in the metrology service lifecycle. Critique:

- **`metrology_service.py`**: Does NOT call `IonoDataService.get_instance().start()`. The background fetch thread never runs in production.
- **Cache directory**: `/var/lib/timestd/iono_cache/` may not exist. No systemd `ExecStartPre` creates it.
- **Optional dependencies**: `netCDF4` and `boto3` are in `[iono]` extras but not in the base install. If missing, does the import fail gracefully?
- **Network access**: WAM-IPE fetch requires outbound HTTPS to AWS S3 and NOMADS. Production machines may have restricted egress. Does the service degrade gracefully?

### 2. Code Quality — New Modules Need Scrutiny

Review `propagation_model.py` and `iono_data_service.py` for:

- **Thread safety**: `IonoDataService` uses a background thread with `threading.Lock`. Are all shared state accesses properly guarded?
- **Error handling**: What happens when WAM-IPE fetch fails? GIRO returns malformed data? Network timeout?
- **Resource leaks**: Does the background thread stop cleanly on service shutdown? Is there a `stop()` method?
- **Memory**: WAM-IPE grids can be large. Is the cache bounded? Old files cleaned up?
- **Singleton pattern**: `IonoDataService.get_instance()` — is it truly safe across multiple threads/processes?

### 3. Physics Correctness — Verify the Ionospheric Model

An ionospheric scientist should verify:

- **Chapman profile**: Is the scale height (H) calculation correct? Does it produce realistic Ne(h) profiles?
- **Group delay integration**: Is the numerical integration through Ne(h) correctly computing excess group delay? The formula `Δτ = 40.3 × sTEC / (c × f²)` should agree with the numerical integration to within ~5%.
- **MUF calculation**: Is `foF2 × sec(i)` the correct MUF formula for oblique incidence? (It's the secant law — correct for flat Earth, approximate for spherical.)
- **Slant factor**: How is vertical TEC converted to slant TEC? The mapping function matters at low elevation angles.
- **Climatological fallback**: Are the diurnal/seasonal/latitudinal parametric formulas for hmF2 and foF2 reasonable? Compare against IRI-2020 for a few test cases.

### 4. Metrological Concerns

A metrologist should verify:

- **Uncertainty propagation**: The adaptive uncertainty blends model confidence with tracked variance. Is this statistically rigorous? Are the confidence values (0.0–0.8) calibrated or arbitrary?
- **Bias vs. variance**: The model predicts a delay and an uncertainty. But is the delay itself biased? The climatological fallback may have systematic errors that the uncertainty doesn't capture.
- **Traceability**: Can the delay prediction be traced back to its data source? The `data_source` field exists but is it logged/archived with each measurement?
- **Self-consistency check**: The `self_consistency_check()` method compares differential delay vs model TEC. What happens when it fails? Is the failure logged? Does it trigger any corrective action?

### 5. Missed Opportunities

Look for things that SHOULD have been done but weren't:

- **IRTAM integration**: GIRO provides IRTAM coefficients that correct IRI-2020 in real-time. The current implementation fetches raw ionosonde data but doesn't use IRTAM deviation maps.
- **Doppler feedback**: The system measures Doppler shift on carrier signals. Doppler is the time derivative of group delay. This could predict delay changes before they happen.
- **IONEX integration**: The existing `ionospheric_model.py` already has IONEX/IRI support. Is the new `iono_data_service.py` duplicating functionality? Should they be merged?
- **Observation feedback loop**: The model predicts delays, but observed delays don't feed back to correct the model in real-time. A Kalman filter on the model parameters could close this loop.
- **Web API exposure**: No `/api/propagation/matrix` endpoint exists yet. The model's predictions are invisible to the user.
- **HFPropagationModel instantiation in metrology_engine.py**: `_predict_geometric_delay()` creates a NEW `HFPropagationModel` on every call when `ArrivalPatternMatrix` is unavailable. This is wasteful — the model should be cached.

---

## 🎯 WHAT NEEDS TO HAPPEN THIS SESSION

### 1. Wire IonoDataService into metrology_service.py startup

Add `IonoDataService.get_instance().start()` to the metrology service initialization. Ensure the background thread starts, fetches WAM-IPE data, and the `HFPropagationModel` receives real-time ionospheric parameters.

### 2. Validate CHU 7.85 MHz multi-hop acceptance

With the new model deployed, nighttime 2F/3F arrivals on CHU 7.85 MHz (timing errors +110 to +312 ms) should now be **accepted** by the multi-mode arrival windows instead of rejected by the old ±50 ms window.

### 3. Add `/api/propagation/matrix` web-api endpoint

Expose the current arrival predictions, modes, uncertainties, and data source for each station/frequency via the web API so the model's behavior is observable.

### 4. Compare model TEC with GNSS VTEC

Cross-check the propagation model's TEC predictions against the existing GNSS VTEC measurements in `L2/gnss_vtec` HDF5 to validate the ionospheric model.

### 5. Tune adaptive uncertainty

Observe production behavior and adjust the uncertainty blending (model vs tracked variance) to optimize the acceptance rate without admitting false detections.

---

## 🛰️ EXTERNAL DATA SOURCES (IMPLEMENTED IN v6.7)

The following data sources are now integrated in `iono_data_service.py`:

| Source | Status | Access | Cadence |
|--------|--------|--------|---------|
| **WAM-IPE** | ✅ Implemented | AWS S3 `noaa-nws-wam-ipe-pds` + NOMADS | Hourly |
| **GIRO DIDBase** | ✅ Implemented | `lgdc.uml.edu` REST API | 15 min |
| **Climatological fallback** | ✅ Implemented | Built-in parametric model | Always available |
| **IRTAM** | ❌ Not yet | GAMBIT database | 15 min |
| **IONEX** | ⚠️ Separate module | `ionospheric_model.py` (not integrated with new service) | 1-2 hr |
| **Madrigal GNSS TEC** | ❌ Not yet | MIT Haystack API | 5-15 min |

**Critique opportunity:** The existing `ionospheric_model.py` already has IONEX and IRI-2020 support. The new `iono_data_service.py` has its own climatological fallback. These should potentially be unified to avoid divergent ionospheric models in the same codebase.

### Zombie Code Check

The following modules may have overlapping or obsolete functionality now that v6.7 is in place:

| File | Concern |
|------|---------|
| `src/hf_timestd/core/ionospheric_model.py` | Has IRI-2020 + IONEX + parametric fallback. Overlaps with `iono_data_service.py` climatological fallback. Merge or deprecate? |
| `src/hf_timestd/core/physics_propagation.py` | Has `PhysicsPropagationModel` with TIER 1/2/3 hierarchy. Overlaps with `HFPropagationModel`. Which is authoritative? |
| `src/hf_timestd/core/bootstrap_validator.py` | Has `_get_expected_delay()` returning static values from `EXPECTED_DELAYS_MS` dict. Should this use `HFPropagationModel`? |

---

## 🏗️ ARCHITECTURE REFERENCE

### Data Flow (v6.7 — with Propagation Model)

```
ka9q-radio (radiod) → RTP multicast → timestd-core-recorder → Raw IQ Buffer (60s)
   (GPS+PPS, ~50μs)                                               ↓
                                                          timestd-metrology (9 channels)
                                                           ↓ (per channel, per station)
                                                    MetrologyEngine.process_minute()
                                                      ├─ AM demod → bandpass → matched filter correlation
                                                      ├─ Per-tone detection (minute markers + long tones only)
                                                      ├─ BufferTiming: sample → UTC via RTP chain
                                                      ├─ ArrivalPatternMatrix physics validation
                                                      │   └─ HFPropagationModel.predict()
                                                      │       └─ IonoDataService (WAM-IPE/GIRO/fallback)
                                                      ├─ Multi-mode arrival windows (1F, 2F, 3F, 1E)
                                                      ├─ timing_error_ms = arrival_utc - expected_utc
                                                      └─ L1MetrologyMeasurement → fusion
                                                           ↓
                                                    HDF5: L2/timing_measurements, L2/detection_attempts
                                                           ↓
                                                    MultiBroadcastFusion → Chrony SHM
                                                           ↓
                                                    web-api dashboard-24h.html
```

### Tone Hierarchy (2026-02-12 — 5ms/10ms ticks DROPPED)

| Station | Tone | Duration | Role | Status |
|---------|------|----------|------|--------|
| **CHU** | Minute marker (sec 0) | 500ms | PRIMARY timing anchor | ✅ Detected, 10–47 dB SNR |
| **CHU** | Per-second tones (sec 1–28, 40–49) | 300ms | Excellent timing source | ✅ 14–15/min validated, +0.3 to +20ms |
| **WWV/WWVH** | Minute marker (sec 0) | 800ms | PRIMARY timing anchor | ⚠️ Needs daytime verification |
| **BPM** | Minute marker (sec 0) | 300ms | Timing anchor | ⚠️ Marginal SNR on shared channels |
| **BPM** | UT1 ticks (sec 25–29, 55–59) | 100ms | Usable | ⚠️ Only during UT1 minutes |
| ~~WWV/WWVH~~ | ~~Per-second ticks~~ | ~~5ms~~ | ~~DROPPED~~ | ❌ ±50ms jitter, 2nd harmonic confounding |
| ~~BPM~~ | ~~UTC ticks~~ | ~~10ms~~ | ~~DROPPED~~ | ❌ Same jitter problem |

**Why 5ms/10ms ticks were dropped:**
- ±50ms timing jitter — essentially uniform random within the search window
- On shared channels, 2nd harmonics of 500 Hz or 600 Hz tones broadcast by WWV or WWVH appear at 1000 or 1200 Hz, confounding the tick correlator
- The 300ms+ tones provide 10–100× better timing precision

### Key Files

| File | Purpose | Priority |
|------|---------|----------|
| `src/hf_timestd/core/propagation_model.py` | **NEW v6.7** — HFPropagationModel, multi-mode delay prediction | **Critical** |
| `src/hf_timestd/core/iono_data_service.py` | **NEW v6.7** — WAM-IPE/GIRO data fetch, cache, interpolation | **Critical** |
| `src/hf_timestd/core/metrology_engine.py` | Tone detection, matched filtering, physics validation | **Critical** |
| `src/hf_timestd/core/arrival_pattern_matrix.py` | Expected arrival windows, multi-mode physics validation | **Critical** |
| `src/hf_timestd/core/metrology_service.py` | Orchestrates per-channel processing — **wire IonoDataService here** | **Critical** |
| `src/hf_timestd/core/buffer_timing.py` | RTP → UTC sample mapping (steel ruler) | High |
| `src/hf_timestd/core/multi_broadcast_fusion.py` | Multi-station/frequency fusion → Chrony | High |
| `src/hf_timestd/core/ionospheric_model.py` | **REVIEW** — overlaps with iono_data_service.py? | Medium |
| `src/hf_timestd/core/physics_propagation.py` | **REVIEW** — overlaps with propagation_model.py? | Medium |
| `src/hf_timestd/core/tick_matched_filter.py` | Carrier phase/Doppler extraction | Medium |
| `tests/test_propagation_model.py` | 23 tests for new propagation model | High |

### Service Inventory

| Service | Purpose | Logs |
|---------|---------|------|
| `timestd-core-recorder` | RTP → raw buffer (authoritative timestamps) | journalctl |
| `timestd-metrology` | IQ → L1/L2 measurements + tick phase extraction | `/var/log/hf-timestd/phase2-*.log` |
| `timestd-fusion` | Multi-broadcast fusion → Chrony | journalctl |
| `timestd-web-api` | REST API + dashboard (FastAPI, port 8000) | journalctl |
| **radiod** | Real-time USB/FFT (CPU 8-15, uncontested L3 cache) | journalctl |

### Deployment

- **Git repo**: `/home/mjh/git/hf-timestd/`
- **Production install**: `sudo /opt/hf-timestd/venv/bin/pip install -e /home/mjh/git/hf-timestd`
- **Restart**: `sudo systemctl restart timestd-metrology.service`
- **3 machines**: bee1 (primary), B3-1, B4-1

---

## ✅ RESOLVED IN PREVIOUS SESSIONS

### Metrology Methodology Audit (2026-02-12)

Major overhaul of the measurement chain. All changes in `src/hf_timestd/core/metrology_engine.py` unless noted.

**Timing chain fixes:**
- `buffer_timing.py`: RTP timestamps are sole timing authority. Most recent GPS snapshot only.
- `binary_archive_writer.py`: Detects RTP counter space changes on radiod restart, flushes stale buffers.
- `metrology_service.py`: Uses `resolve_buffer_timing()` to derive `system_time` from RTP chain instead of trusting raw `start_system_time`.
- Minute boundary rounding: `round()` instead of `int()//60*60` — fixes 60s offset when RTP-derived time is microseconds before boundary.

**Physics validation simplified:**
- Removed broken `sample_position_original` computation (buffer-relative math that wrapped at second boundaries).
- Physics validation now uses `timing_error_ms = (arrival_utc - expected_utc) * 1000` directly. `arrival_utc` IS the Time of Arrival — no offsets, no buffer-relative math.

**Tone filtering — dropped short ticks:**
- WWV/WWVH 5ms ticks: DROPPED — ±50ms jitter, confounded by 2nd harmonics of 500/600 Hz tones on shared channels.
- BPM 10ms UTC ticks: DROPPED — same jitter problem.
- Only tones ≥100ms are used for timing: CHU 500ms/300ms, WWV/WWVH 800ms, BPM 300ms/100ms.

**Matched filter improvements:**
- Bandpass filter (±50 Hz) applied to measurement region before correlation — isolates tone frequency from competing stations on shared channels.
- Wider measurement region for long templates — ensures enough noise-free correlation output for clean SNR estimation.
- Fixed correlation SNR threshold at 8.0 dB for all tone durations — removed broken duration-scaled threshold that required 17 dB for 800ms marker.
- `ARRIVAL_TOLERANCE_MS` widened from 100ms to 500ms to accommodate multi-hop ionospheric paths.

**Production results (0250 UTC):**
- CHU 3.33 MHz: 14–15 measurements/min, timing errors +0.3 to +19.9ms, all physics-validated. **Real metrology.**
- CHU 7.85 MHz: 500ms minute marker detected at 15–26 dB corr_SNR. Single-hop arrivals validated, multi-hop correctly rejected.
- Shared channels (2.5/5/15 MHz): WWV/WWVH 800ms markers not detected — nighttime propagation. Needs daytime verification.

### Phase Continuity Fix + Doppler UI (2026-02-11, session 2)
- Phase continuity bug fixed: IQ mixer used window-relative time, causing ~1.7 rad phase jumps. Fixed: buffer-relative time.
- Per-tick phase extraction: phasors combined coherently.
- Regression tests: `test_carrier_phase_continuity` and `test_dc_carrier_phase_stability` verify σ < 0.3 rad.
- Phase/Doppler web dashboard: 4 API endpoints + visualization page.
- HDF5 corrupt chunk recovery: `hdf5_reader.py` binary-searches for last good row.
- See: `docs/changes/SESSION_2026_02_11_PHASE_CONTINUITY_AND_DOPPLER_UI.md`

### Phase Extraction & Cross-Talk Fix (2026-02-11, session 1)
- Three-tier phase extraction: audio phase, IQ carrier phase, DC carrier phasor
- Cross-frequency discrimination gate: 3 dB advantage required between 1000↔1200 Hz
- CHU modulation comments corrected: USB with preserved carrier, not DSB-SC

### CHU FSK Decoder — USB Sidecar Channels (2026-02-10)
- Dedicated USB-preset sidecar channels for FSK decoding

### Timing Accuracy (2026-02-06)
- Four bugs fixed: circular calibration, broken GPS ground truth, dead Kalman filter, missing serialization
- Mean discrepancy reduced from -495ms to ~1ms

### Pipeline Offset Calibration (2026-02-09)
- Removed entirely — radiod's RTP timestamps are authoritative
- Verified: CHU_3330 error=-0.31ms, CHU_7850 error=-3.01ms at 39.5dB SNR

### HDF5 Crash Safety (2026-02-06)
- SWMR eliminated — open-write-close per measurement

### CHU Memory Leak (2026-01-02)
- CHU FSK decoder passed full 60s buffer through hilbert()/filtfilt() for each of 9 FSK seconds
- Fixed: extract ~1.1s audio slice before demodulation

### HDF5 File Lock Contention (recurring)
- Multiple services accessing same HDF5 files caused errno=11 stalls
- Fixed: `locking=False` on all h5py.File() calls, env var before import

---

## 🔑 KEY PRINCIPLES (Established 2026-02-12)

1. **The GPSDO is a steel ruler.** Every sample has a known UTC timestamp via the RTP chain. The buffer exists only to find the tone. Once found, read the timestamp. That's the ToA.
2. **The ionosphere is the unknown.** Multi-frequency, multi-station geometry solves it — with or without GPS. GPS just removes one unknown (clock error).
3. **Longer tones = better timing.** Precision scales as √(duration). 800ms marker >> 300ms CHU >> 100ms BPM. Short ticks (5ms/10ms) are noise.
4. **The physics doesn't change — only the ruler does.** Without GPS, the system can bootstrap from tone inter-relationships alone. The observables (ToA, differential delay, Doppler) are the same.
5. **Bandpass before correlate.** On shared channels, competing stations corrupt long-template correlations. A narrow bandpass (±50 Hz) isolates the target tone.

---

## ✅ RESOLVED: Propagation Delay Modeling (2026-02-12)

All 7 success criteria from the previous session have been met:

1. ✅ **WAM-IPE ingestion** — `iono_data_service.py` fetches 2D products (TEC, NmF2, HmF2) from `s3://noaa-nws-wam-ipe-pds/` and NOMADS, with GIRO corrections and climatological fallback
2. ✅ **Ray-tracing** — `propagation_model.py` numerically integrates group delay through Ne(h) Chapman profiles, with TEC-based fallback
3. ✅ **Model-based delay** — `_predict_geometric_delay()` now uses HFPropagationModel (frequency-dependent, time-varying) instead of vacuum × 1.15
4. ✅ **Multi-hop arrivals** — `ArrivalMatrix.multi_mode_arrivals` dict supports 1F, 2F, 3F, 1E modes with separate windows per (station, freq, mode)
5. ⏳ **CHU 7.85 MHz validation** — model infrastructure is in place; needs production deployment to verify
6. ✅ **Adaptive uncertainty** — windows adapt based on data source quality (WAM-IPE ±1.5ms → parametric ±9ms) blended with tracked variance
7. ✅ **Self-consistency check** — `HFPropagationModel.self_consistency_check()` compares multi-freq differential delay vs model TEC

**23 new tests, all passing. 76 existing tests pass, 0 regressions.**

See: `docs/changes/SESSION_2026_02_12_PROPAGATION_MODEL.md`

---

## ✅ Success Criteria — Next Session

1. **Deploy to production** — install `netCDF4` and `boto3` (`pip install hf-timestd[iono]`), restart metrology services, verify IonoDataService starts and fetches WAM-IPE data
2. **Start IonoDataService in metrology lifecycle** — wire `IonoDataService.get_instance().start()` into `metrology_service.py` startup so real-time data flows to the propagation model
3. **Validate CHU 7.85 MHz multi-hop** — with the new model deployed, verify that nighttime 2F/3F arrivals on CHU 7.85 MHz are accepted (not rejected by the ±50ms window)
4. **Compare model TEC with GNSS VTEC** — cross-check the propagation model's TEC predictions against the existing GNSS VTEC measurements in L2/gnss_vtec HDF5
5. **Expose propagation diagnostics via web-api** — add `/api/propagation/matrix` endpoint showing current arrival predictions, modes, uncertainties, and data source for each station/frequency
6. **Daytime verification** — confirm WWV/WWVH 800ms minute markers are detected and validated during daytime propagation on shared channels (2.5/5/10/15 MHz)
7. **Tune adaptive uncertainty** — observe production behavior and adjust the uncertainty blending (model vs tracked variance) to optimize the acceptance rate without admitting false detections
