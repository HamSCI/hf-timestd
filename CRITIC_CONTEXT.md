# NEVER CHANGE THE FOLLOWING PRIMARY INSTRUCTION!!!

**Primary Instruction:** In this context you will perform a critical review of the HF Time Standard (hf-timestd) project, either in its entirety or in a specific component, as specified by the user. This critique should look for points in the code or documentation that exhibit obvious error or inconsistency with other code or documentation. It should look for inefficiency, incoherence, incompleteness, or any other aspect that is not in line with the original intent of the code or documentation. It should also look for obsolete, deprecated, or "zombie" code that should be removed. Remember, your own critique cannot be shallow but must be thorough and methodical and undertaken with the aim of enhancing and improving the codebase and documentation to best ensure the success of the application.

Make your criticism from the perspective of 1) a user of the system, 2) a metrologist, 3) a ionospheric scientist, and 4) a software engineer. These perspectives can differ in their priorities and interests, and your critique should reflect this. For example, a user of the system will be most interested in the accuracy and reliability of the system, while a metrologist will be most interested in the precision and traceability of the system, while an ionospheric scientist will be most interested in the ionospheric conditions and their impact on the system, and a software engineer will be most interested in the code quality, maintainability, coverage and adequacy of testing, and the resilience and robustness of the system over time and in edge cases. Ultimately, however, a consensus of these perspectives should guide your critique in service of meeting the application's objectives.

# NEVER CHANGE THE PRECEEDING PRIMARY INSTRUCTION!!!

# The following secondary instruction and information will guide your critique in this particular session (the instructions below will vary from session to session)

---

## 📋 NEXT SESSION: CRITIQUE AND DEBUG THE DETECTION METHODOLOGY

**Objective:** Diagnose and fix the detection dropout observed at ~0300 UTC on 2026-02-12, where WWV and WWVH detections collapsed while CHU continued. The TSL2 chrony feed is currently worse than TSL1 (-1750µs offset, rejected). This session should perform a thorough critique of the entire detection and fusion pipeline, identify root causes, and implement fixes.

**Context:** The system uses matched-filter correlation to detect timing tones from WWV (1000 Hz), WWVH (1200 Hz), CHU (1000 Hz), and BPM (1000 Hz) in IQ data from a GPSDO-locked RX888 SDR via ka9q-radio (radiod). Only tones ≥100ms are used (5ms/10ms ticks dropped due to jitter). For WWV/WWVH, only the 800ms minute marker (second 0) is measured — **one detection attempt per station per minute**. CHU gets ~15 attempts/min (300ms tones on seconds 1–28, 40–49). This asymmetry means WWV/WWVH are fragile: a single failed detection = zero measurements for that minute.

---

## 🚨 INCIDENT: ~0300 UTC DETECTION DROPOUT (2026-02-12)

### Observed Symptoms

From the dashboard screenshot and chrony output:

1. **~0300 UTC**: Sharp dropout of detections on ALL WWV channels (2.5, 5, 10, 15, 20, 25 MHz) and ALL WWVH channels (2.5, 5, 10, 15 MHz)
2. **CHU 14.67 MHz**: Continues with detections through the gap — **CHU is unaffected**
3. **Recovery**: Varies by frequency. Higher frequencies (15, 20 MHz) recover first (~0600), lower frequencies (2.5, 5 MHz) recover later (~0800+). This is consistent with sunrise terminator restoring HF propagation.
4. **WWVH 15 MHz**: Shows suspiciously high SNR spikes (>50 dB) after recovery — possible false detections or interference
5. **TSL2 chrony feed**: -1750µs offset (marked `x` = rejected by chrony), while TSL1 shows +79µs. The fusion layer is producing bad timing from sparse/noisy detections.

### Chrony Status (12:49 UTC)

```
#? TSL1    0   4    42    54    +79us[  +79us] +/- 2000us
#x TSL2    0   4    10    70  -1750us[-1750us] +/-  600us
^* GPS     1   3   377     7  -1985ns[-2402ns] +/-   92us
```

- **TSL1** (L1 Kalman): +79µs, reachability 42 (intermittent), ±2000µs uncertainty — marginal but tracking
- **TSL2** (L2 Kalman): -1750µs, reachability 10 (very poor), rejected by chrony — **broken**
- **GPS**: -2ns, reachability 377 (perfect) — the reference is fine

### Key Diagnostic Questions

1. **Why did WWV/WWVH drop at 0300 but CHU survived?** CHU 14.67 MHz is a higher frequency than most WWV channels, and CHU is closer (1522 km vs 1120 km). But WWV 15 MHz and 20 MHz also dropped — same frequency range. Is this purely propagation, or is the detection pipeline more fragile for WWV/WWVH?
2. **Why is TSL2 at -1750µs?** The L2 Kalman filter should be robust to detection gaps. Is it being corrupted by false detections during recovery? Is the Kalman divergence recovery (>20ms threshold) too loose?
3. **Are the WWVH 15 MHz high-SNR spikes (>50 dB) real?** If these are false detections, they could be corrupting the fusion.
4. **Is the 1-per-minute WWV/WWVH detection rate fundamentally too fragile?** CHU gets 15 attempts/min. WWV/WWVH get 1. Should we reconsider the 5ms tick decision?

---

## 🔍 CRITIQUE TARGETS FOR THIS SESSION

### 1. Detection Fragility — WWV/WWVH vs CHU

**The core asymmetry:** WWV/WWVH only use the 800ms minute marker (second 0). All other seconds return `duration=0.0` and are skipped. CHU uses 300ms tones on ~20 seconds per minute. This means:

- **CHU**: 15+ detection attempts per minute → robust to fading on individual seconds
- **WWV/WWVH**: 1 detection attempt per minute → single fade = total loss

**Critique targets in `metrology_engine.py`:**

- `_get_tone_duration()` (line ~414): Returns 0.0 for all WWV/WWVH seconds except 0. The 5ms ticks were dropped for good reason (±50ms jitter, cross-frequency confounding). But are there other usable tones? WWV/WWVH transmit 440/500/600 Hz audio tones during specific minutes. These are longer (several seconds) and could provide timing if the minute is known.
- `process_minute()` (line ~898): The `measurable[:15]` limit caps attempts. For WWV/WWVH this is moot (only 1 measurable second), but verify the prioritization logic.
- The 800ms template with ±500ms search window (`SEARCH_WINDOW_MS = max(50, min(500, tone_duration_sec * 625))`) — is the search window appropriate for nighttime multi-hop? Could the arrival be outside ±500ms?

**Question for the critic:** Should we add WWV/WWVH long audio tones (440/500/600 Hz, several seconds duration) as secondary timing sources? They wouldn't have the same precision as the 1000/1200 Hz minute marker, but they'd provide redundancy during fades.

### 2. Matched Filter Sensitivity — Thresholds and Gates

The detection pipeline has multiple quality gates. Each one could be rejecting valid signals during marginal propagation:

| Gate | Threshold | Location | Risk |
|------|-----------|----------|------|
| **Correlation SNR** | `MIN_CORR_SNR_DB = 8.0` | line ~708 | Too high for weak nighttime signals? |
| **Correlation flat** | `range/mean < 0.5` | line ~671 | May reject weak but real signals |
| **Cross-freq discrimination** | `MIN_FREQ_ADVANTAGE_DB = 3.0` | line ~756 | Correct, but verify edge cases |
| **Arrival tolerance** | `±500ms` | line ~835 | May be too tight for 3F/multi-hop |
| **BPM SNR** | `MIN_BPM_SNR_DB = 12.0` | line ~857 | Appropriate for BPM |
| **Edge rejection** | Peak at edge of search window | line ~667 | May reject real signals near window edge |

**Critique approach:**
- Pull `L2/detection_attempts` HDF5 data for the 0200–0600 UTC period
- Tabulate rejection reasons by station, frequency, and time
- Identify which gate is responsible for the dropout
- Check if lowering `MIN_CORR_SNR_DB` from 8.0 to 6.0 would recover valid detections without admitting false ones

### 3. Fusion Layer Robustness — TSL2 Corruption

The fusion pipeline in `multi_broadcast_fusion.py` has several potential failure modes during detection gaps:

**Kalman filter behavior during gaps:**
- `_kalman_update()` (line ~2743): The Kalman predict step runs every cycle, but the update step only runs when measurements exist. During a gap, the state coasts with process noise `q_offset = 0.01 ms²/min`. After a long gap, the covariance grows, making the filter vulnerable to the first (possibly bad) measurement.
- **Divergence recovery** (line ~2875): Triggers at `|state| > 20ms`. TSL2 at -1750µs = -1.75ms — well below the 20ms threshold. The filter won't self-correct.
- **L2 vs L1 independence**: TSL1 and TSL2 use independent Kalman states. TSL2 uses L2 calibration data which may have different biases. Why is TSL2 worse?

**Outlier rejection during sparse data:**
- `_reject_outliers()` (line ~2194): Requires `len(measurements) >= 4` to activate. During recovery with only 1-2 measurements, outliers pass through unfiltered.
- Pre-fusion MAD rejection (line ~3182): Also requires `len(measurements) > 2`. Single bad measurements during recovery go straight to the Kalman filter.

**Hardware calibration drift:**
- `_update_calibration()` (line ~2346): During the detection gap, no calibration updates occur. When detections resume, the first measurements may have different propagation characteristics (sunrise terminator). The calibration EMA could chase these transients.

**Critique approach:**
- Check fusion logs for the 0300–0600 period: how many measurements per cycle? Which stations?
- Verify that the Kalman filter handles measurement gaps gracefully (no NaN propagation, no covariance explosion)
- Check if the L2 Kalman was corrupted by a single bad measurement during recovery

### 4. Propagation Model Impact on Detection

The new `HFPropagationModel` (deployed this session) affects detection through `_predict_geometric_delay()`:

- If the model predicts the wrong delay, the search window is centered in the wrong place
- During the sunrise terminator transition, ionospheric parameters change rapidly — the model may lag
- The `expected_delay_ms` feeds into `onset_sample` calculation — a bad prediction shifts the entire measurement window

**Critique approach:**
- Query `/propagation/model/all-stations` at 0300 UTC and 0600 UTC to see what the model predicted
- Compare model predictions with actual observed arrival times from the detection_attempts data
- Check if the model's uncertainty windows were wide enough to capture the actual arrivals

### 5. Chrony Feed Quality — Why TSL2 is Worse

The system feeds two independent Chrony SHM segments:
- **TSL1**: L1 Kalman (direct metrology measurements)
- **TSL2**: L2 Kalman (calibrated measurements with physics corrections)

TSL2 should be *better* than TSL1 (more corrections applied). The fact that it's *worse* (-1750µs vs +79µs) suggests:
- L2 calibration is introducing systematic error
- The L2 Kalman has different convergence behavior
- L2 physics corrections (propagation mode, GNSS VTEC) are wrong during the terminator transition

**Critique approach:**
- Compare L1 and L2 measurement values for the same detections
- Check if L2 calibration offsets (`hardware_offset_ms`) are reasonable
- Verify that the L2 Kalman filter was properly initialized and hasn't diverged

---

## 🎯 WHAT NEEDS TO HAPPEN THIS SESSION

### 1. Diagnose the 0300 UTC dropout

Pull detection_attempts data and identify exactly which quality gate rejected WWV/WWVH detections. Was it correlation SNR? Propagation bounds? Cross-frequency discrimination? Or was the signal genuinely absent (no propagation)?

### 2. Fix TSL2 chrony feed

Identify why TSL2 is at -1750µs and fix it. This likely requires either:
- Resetting the L2 Kalman state
- Fixing a systematic bias in L2 calibration
- Adding better outlier protection during sparse-data recovery

### 3. Improve WWV/WWVH detection resilience

Consider:
- Adding WWV/WWVH audio tones (440/500/600 Hz) as secondary timing sources
- Reducing `MIN_CORR_SNR_DB` with compensating quality gates
- Implementing Kalman coasting during detection gaps (predict from last good state)

### 4. Add detection gap monitoring

The system should detect and alert when a station goes dark:
- Log a WARNING when a station has 0 detections for >5 minutes
- Track detection rate per station in the web dashboard
- Consider a "station health" metric that feeds into fusion weighting

### 5. Validate propagation model predictions against observations

Use the new `/propagation/model/predict` endpoint to compare model predictions with actual observed arrivals across the 24h period. Identify systematic biases.

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

1. **Root-cause the 0300 UTC dropout** — pull `L2/detection_attempts` data, tabulate rejection reasons by station/freq/time, identify which quality gate killed WWV/WWVH detections
2. **Fix TSL2 chrony feed** — identify and correct the -1750µs bias in the L2 Kalman filter
3. **Improve WWV/WWVH detection resilience** — either add secondary timing tones (440/500/600 Hz audio), lower thresholds with compensating gates, or implement Kalman coasting
4. **Add detection gap alerting** — WARNING log when a station has 0 detections for >5 minutes; station health metric in dashboard
5. **Validate propagation model vs observations** — compare `/propagation/model/predict` output with actual observed arrivals across the 24h period
6. **Verify WWVH 15 MHz high-SNR spikes** — determine if the >50 dB detections after recovery are real or false positives
7. **TSL1 and TSL2 should both track GPS within ±500µs** — the ultimate success metric
