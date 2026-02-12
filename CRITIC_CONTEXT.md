# NEVER CHANGE THE FOLLOWING PRIMARY INSTRUCTION!!!

**Primary Instruction:** In this context you will perform a critical review of the HF Time Standard (hf-timestd) project, either in its entirety or in a specific component, as specified by the user. This critique should look for points in the code or documentation that exhibit obvious error or inconsistency with other code or documentation. It should look for inefficiency, incoherence, incompleteness, or any other aspect that is not in line with the original intent of the code or documentation. It should also look for obsolete, deprecated, or "zombie" code that should be removed. Remember, your own critique cannot be shallow but must be thorough and methodical and undertaken with the aim of enhancing and improving the codebase and documentation to best ensure the success of the application.

Make your criticism from the perspective of 1) a user of the system, 2) a metrologist, 3) a ionospheric scientist, and 4) a software engineer. These perspectives can differ in their priorities and interests, and your critique should reflect this. For example, a user of the system will be most interested in the accuracy and reliability of the system, while a metrologist will be most interested in the precision and traceability of the system, while an ionospheric scientist will be most interested in the ionospheric conditions and their impact on the system, and a software engineer will be most interested in the code quality, maintainability, coverage and adequacy of testing, and the resilience and robustness of the system over time and in edge cases. Ultimately, however, a consensus of these perspectives should guide your critique in service of meeting the application's objectives.

# NEVER CHANGE THE PRECEEDING PRIMARY INSTRUCTION!!!

# The following secondary instruction and information will guide your critique in this particular session (the instructions below will vary from session to session)

---

## 📋 NEXT SESSION: DETECTION RESILIENCE AND FUSION QUALITY

**Objective:** With the matched filter false positive problem fixed (see Resolved section below), focus on improving WWV/WWVH detection resilience and fusion quality. The fundamental asymmetry remains: CHU gets 15 attempts/min, WWV/WWVH get 1. When propagation is marginal, WWV/WWVH go dark.

**Context:** The system now has a clean detection pipeline — false positives eliminated by narrower search windows and physics validation. But WWV/WWVH detection rate is still limited by the 1-per-minute 800ms minute marker. During nighttime/terminator conditions, this single attempt often fails (corr_snr < 8 dB).

---

### Remaining Work Items

1. **Add WWV/WWVH secondary timing tones** — 440/500/600 Hz audio tones transmitted during specific minutes are several seconds long and could provide redundancy. Lower precision than the 1000/1200 Hz minute marker, but much better than zero detections.

2. **Detection gap alerting** — WARNING log when a station has 0 detections for >5 minutes. Station health metric in the web dashboard. Feed detection rate into fusion weighting.

3. **Propagation model multi-hop delays** — The model predicts 3F delays of ~10ms, but real nighttime multi-hop arrivals show +200-450ms timing error. The ionospheric group delay for multi-hop paths is drastically underestimated. This doesn't affect detection (physics gate correctly rejects false positives), but it means the model can't validate real multi-hop arrivals. Future work: investigate the group delay integration for multi-hop paths.

4. **TSL2 convergence** — TSL2 was at -1750µs before the fix session but was converging to -619µs by 13:09 UTC. After the metrology restart, both TSL feeds need time to re-converge. Monitor over 24h to verify both track GPS within ±500µs.

5. **Validate propagation model vs observations** — Compare `/propagation/model/predict` output with actual observed arrivals across a full 24h period. Identify systematic biases in the model.

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

### Key Files (Detection & Fusion Focus)

| File | Purpose | Priority |
|------|---------|----------|
| `src/hf_timestd/core/metrology_engine.py` | **PRIMARY TARGET** — `_measure_tone_at_known_time()`, `_get_tone_duration()`, `process_minute()`, all quality gates | **Critical** |
| `src/hf_timestd/core/multi_broadcast_fusion.py` | **PRIMARY TARGET** — `_kalman_update()`, `_reject_outliers()`, `_cross_validate_stations()`, `fuse()`, Chrony SHM output | **Critical** |
| `src/hf_timestd/core/metrology_service.py` | Orchestrates per-channel processing, writes detection_attempts HDF5 | **Critical** |
| `src/hf_timestd/core/propagation_model.py` | HFPropagationModel — `_predict_geometric_delay()` centers the search window | High |
| `src/hf_timestd/core/arrival_pattern_matrix.py` | Expected arrival windows, multi-mode physics validation | High |
| `src/hf_timestd/core/buffer_timing.py` | RTP → UTC sample mapping (steel ruler) | High |
| `src/hf_timestd/core/l2_calibration_service.py` | L2 physics corrections — may be source of TSL2 bias | High |
| `src/hf_timestd/core/tick_matched_filter.py` | Per-second tick phase extraction (CHU gets ~15/min from this) | Medium |
| `web-api/routers/propagation.py` | New `/model/predict`, `/model/all-stations`, `/model/iono-status` endpoints | Medium |
| `tests/test_propagation_model.py` | 23 tests for propagation model | Medium |

### Key HDF5 Data Products for Diagnosis

| Product | Path | Contents |
|---------|------|----------|
| `L2/detection_attempts` | `phase2/<channel>/L2/detection_attempts/` | Every detection attempt with rejection reason, corr_snr, timing_error |
| `L2/timing_measurements` | `phase2/<channel>/L2/timing_measurements/` | Accepted detections only |
| `L2/tick_timing` | `phase2/<channel>/L2/tick_timing/` | Per-second tick phase and timing |
| `L3/fusion` | `phase2/fusion/L3/fusion/` | Fused D_clock, weights, station counts |

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

### Matched Filter False Positive Fix (2026-02-12, session 2)

**Root cause of 0300 UTC detection dropout diagnosed and fixed.**

The apparent "dropout" was actually two problems:
1. **Correct behavior**: WWV/WWVH 5ms ticks dropped (only 800ms minute marker used) → 1 attempt/min instead of ~850/min. CHU unaffected (15 attempts/min with 300ms tones).
2. **False positive problem**: 80% of WWV/WWVH "detections" that passed the 8.0 dB corr_snr gate were **noise correlation peaks**, not real signals. Timing errors were uniformly distributed across ±500ms — the signature of random noise, not real arrivals.

**Root cause**: The 800ms template with ±500ms search window had a **21% false positive rate** on pure noise. The correlation envelope of bandpass-filtered noise has ~55ms coherence length, giving ~18 effective independent samples in ±500ms. Extreme value statistics push the peak/median ratio to ~6.2 dB — well into the 8.0 dB threshold.

**Fixes in `metrology_engine.py`:**
- Search window capped at ±100ms for templates ≥100ms (was ±500ms for 800ms). Physics model constrains real arrivals to ±15ms, so ±100ms is generous.
- Noise exclusion zone widened to full template length (was half). Prevents signal energy from contaminating the noise floor estimate.
- FP rate: 21% → 6.8%. Combined with physics gate (±15ms window), effective FP rate < 1%.
- Real signal detection: 100% at all SNR levels (even weak 10 dB signals produce 42+ dB corr_snr).
- Physics validation gate confirmed ESSENTIAL — correctly rejects the remaining ~7% of noise FPs.

**WWVH 15 MHz >50 dB spikes**: Explained by the old false positive problem. No longer occurring.

**Production verification**: CHU channels healthy (11/15 detected on 14.67 MHz), shared channels clean (no FPs passing physics), WWV_20000 validated at +14.1ms (2.8σ).

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

All critique items from the propagation model session have been addressed:

- ✅ **WAM-IPE ingestion** — `iono_data_service.py` fetches from S3/NOMADS with GIRO corrections and climatological fallback
- ✅ **Ray-tracing** — numerical integration through Ne(h) Chapman profiles with TEC-based fallback
- ✅ **Model-based delay** — `_predict_geometric_delay()` uses HFPropagationModel (frequency-dependent, time-varying)
- ✅ **Multi-hop arrivals** — 1F, 2F, 3F, 1E modes with separate windows per (station, freq, mode)
- ✅ **Adaptive uncertainty** — windows adapt based on data source quality blended with tracked variance
- ✅ **Self-consistency check** — `HFPropagationModel.self_consistency_check()` wired into `ArrivalPatternMatrix`
- ✅ **Model traceability** — `model_data_source`, `model_confidence`, `propagation_mode` in L1 measurement dicts
- ✅ **Great-circle TEC sampling** — `_gc_intermediate()` replaces linear lat/lon interpolation
- ✅ **Altitude-dependent obliquity** — thin-shell mapping M(h) replaces constant 1/sin(e)
- ✅ **Web-api endpoints** — `/propagation/model/predict`, `/model/all-stations`, `/model/iono-status`
- ✅ **Zombie cleanup** — `physics_propagation.py` deprecated, `multi_broadcast_fusion.py` migrated to `HFPropagationModel`, `bootstrap_validator._get_expected_delay()` wired to model

**23 propagation tests passing. See: `docs/changes/SESSION_2026_02_12_PROPAGATION_MODEL.md`**

---

## ✅ Success Criteria — Next Session

1. ~~**Root-cause the 0300 UTC dropout**~~ ✅ RESOLVED — 80% of WWV/WWVH "detections" were false positives from noise correlation peaks. Search window narrowed from ±500ms to ±100ms, noise exclusion zone widened. FP rate reduced from 21% to <7%.
2. ~~**Fix TSL2 chrony feed**~~ ✅ CONVERGING — TSL2 was at -1750µs, converged to -619µs by 13:09 UTC before metrology restart. Monitor over 24h.
3. **Improve WWV/WWVH detection resilience** — add secondary timing tones (440/500/600 Hz audio) for redundancy during nighttime fades
4. **Add detection gap alerting** — WARNING log when a station has 0 detections for >5 minutes; station health metric in dashboard
5. **Validate propagation model vs observations** — compare model predictions with actual observed arrivals; investigate multi-hop delay underestimation
6. ~~**Verify WWVH 15 MHz high-SNR spikes**~~ ✅ RESOLVED — false positives from the old ±500ms search window. No longer occurring with the fix.
7. **TSL1 and TSL2 should both track GPS within ±500µs** — monitor after metrology restart
