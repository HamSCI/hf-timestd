# NEVER CHANGE THE FOLLOWING PRIMARY INSTRUCTION!!!

**Primary Instruction:** In this context you will perform a critical review of the HF Time Standard (hf-timestd) project, either in its entirety or in a specific component, as specified by the user. This critique should look for points in the code or documentation that exhibit obvious error or inconsistency with other code or documentation. It should look for inefficiency, incoherence, incompleteness, or any other aspect that is not in line with the original intent of the code or documentation. It should also look for obsolete, deprecated, or "zombie" code that should be removed. Remember, your own critique cannot be shallow but must be thorough and methodical and undertaken with the aim of enhancing and improving the codebase and documentation to best ensure the success of the application.

Make your criticism from the perspective of 1) a user of the system, 2) a metrologist, 3) a ionospheric scientist, and 4) a software engineer. These perspectives can differ in their priorities and interests, and your critique should reflect this. For example, a user of the system will be most interested in the accuracy and reliability of the system, while a metrologist will be most interested in the precision and traceability of the system, while an ionospheric scientist will be most interested in the ionospheric conditions and their impact on the system, and a software engineer will be most interested in the code quality, maintainability, coverage and adequacy of testing, and the resilience and robustness of the system over time and in edge cases. Ultimately, however, a consensus of these perspectives should guide your critique in service of meeting the application's objectives.

# NEVER CHANGE THE PRECEEDING PRIMARY INSTRUCTION!!!

# The following secondary instruction and information will guide your critique in this particular session (the instructions below will vary from session to session)

---

## 📋 NEXT SESSION: IMPROVE PROPAGATION DELAY MODELING

**Objective:** Replace the current static propagation delay model with real-time ionospheric data for accurate HF group delay estimation. The current model uses fixed great-circle distances and a simple speed-of-light calculation with a hardcoded uncertainty window (±50ms). Real ionospheric group delay varies by 5–10% from vacuum speed-of-light and changes on timescales of minutes to hours. Accurate delay modeling is the key to tightening the physics validation window and improving timing precision.

**Context:** The measurement chain from antenna to Chrony is now working correctly (see "Resolved" section below). The system produces real metrology — CHU 3.33 MHz delivers 14–15 validated measurements per minute with timing errors of +0.3 to +20ms. The bottleneck is now the **propagation model**, which determines the expected arrival time against which measurements are validated.

---

## 🚨 THE CURRENT PROPAGATION MODEL AND ITS LIMITATIONS

### What we have now

The current model in `metrology_engine.py` method `_get_expected_delay()` computes:
```
expected_delay_ms = great_circle_distance_km / speed_of_light_km_per_ms
```
with a fixed 15ms 1-sigma uncertainty. This is a **vacuum straight-line** estimate that ignores:

1. **Ionospheric group delay** — HF signals reflect off the ionosphere at 200–400 km altitude. The actual path length is longer than the great-circle distance by 5–10%, adding 0.3–3ms depending on frequency and hop geometry.
2. **Frequency-dependent delay** — Lower frequencies penetrate less deeply into the ionosphere and experience more group delay. A 3.33 MHz signal has ~3× more excess delay than a 15 MHz signal on the same path.
3. **Diurnal variation** — The ionosphere's electron density (and thus group delay) varies smoothly over 24 hours, with sunrise/sunset transitions causing rapid changes.
4. **Multi-hop propagation** — At night on frequencies like 7.85 MHz, signals can take 2F or 3F paths with 100–300ms additional delay. The current model only predicts single-hop.
5. **Geomagnetic storms** — During disturbed conditions, delays can change by 50–100% within minutes.

### What the production data shows

From the 2026-02-12 session:

- **CHU 3.33 MHz**: timing errors +0.3 to +20ms, all within ±50ms window → validated. The positive bias suggests the real path is ~10ms longer than the vacuum model predicts.
- **CHU 7.85 MHz**: timing errors +110 to +312ms at night → rejected by ±50ms window. These are real multi-hop arrivals that the model doesn't predict.
- **Shared channels (2.5/5/15 MHz)**: WWV/WWVH 800ms minute markers not detected at 0250 UTC — nighttime propagation dead on these frequencies. The model doesn't predict band closures.

### The arrival matrix (arrival_pattern_matrix.py)

The `ArrivalPatternMatrix` defines expected arrival windows per station/second:
- `BOOTSTRAP_INITIAL_UNCERTAINTY_MS = 50.0` — the ±50ms 3-sigma window
- `expected_delay_ms` comes from `_get_expected_delay()` (vacuum model)
- Detections outside this window are rejected as "physics invalid"

**The ±50ms window is simultaneously too tight and too loose:**
- Too tight for multi-hop paths (100–300ms excess delay at night)
- Too loose for single-hop paths where the real uncertainty is ±5ms with a good ionospheric model

---

## 🎯 WHAT NEEDS TO CHANGE

### 1. Replace static delay with real-time ionospheric model

The `_get_expected_delay()` method should return a **frequency-dependent, time-varying** delay estimate based on current ionospheric conditions, not a fixed vacuum calculation.

### 2. Predict multi-hop arrivals

The arrival matrix should predict **multiple arrival modes** (1F, 2F, 3F) with different expected delays, especially at night on lower frequencies. Each mode has its own expected delay and uncertainty.

### 3. Adaptive uncertainty windows

Instead of a fixed ±50ms, the uncertainty window should be:
- Narrow (±5ms) when the ionospheric model is well-constrained (daytime, near ionosonde)
- Wide (±200ms) when the model is uncertain (nighttime, disturbed conditions, no nearby data)

### 4. Use multi-frequency observations as constraints

The system monitors the same station on multiple frequencies simultaneously. The **differential delay** between frequencies is a direct observable of the ionospheric TEC along the path. This should feed back into the propagation model.

---

## 🛰️ EXTERNAL DATA SOURCES FOR REAL-TIME PROPAGATION MODELING

### Real-Time Assimilative Models (Corrected IRI)

These take the standard IRI-2020 climatological background and warp it with live measurements:

- **IRTAM (IRI-based Real-Time Assimilative Model)**
  - Run by **GIRO (Global Ionospheric Radio Observatory)**
  - Ingests real-time data from ~60 Digisondes worldwide every 15 minutes
  - Generates deviation maps that correct IRI-2020 climatology
  - Updates the **vertical structure** (hmF2, B0, B1 parameters) — critical for calculating exact reflection height and group delay
  - Access: GAMBIT database or real-time IRTAM coefficients via [GIRO GAMBIT Explorer](http://giro.uml.edu/gambit/)

- **GAIM (Global Assimilation of Ionospheric Measurements)**
  - Physics-based model assimilating GPS/GNSS TEC and ionosonde data
  - Handles plasma redistribution along magnetic field lines
  - Provides more realistic 3D electron density grid than simple TEC mapping
  - GAIM-GM (Gauss-Markov) variant sometimes available for research

### Physics-Based 3D Models (For Ray-Tracing)

For time-of-flight calculation, a **3D voxel grid** of electron density enables numerical ray-tracing (NRT):

- **NOAA WAM-IPE (Whole Atmosphere Model - Ionosphere Plasmasphere Electrodynamics)**
  - NOAA's operational space weather model
  - Couples lower atmosphere (weather) with ionosphere/plasmasphere
  - Outputs full **3D grids** of neutral density, electron density, and ion drifts
  - Enables ray-tracing through actual modeled gradients rather than assuming Chapman profile shape
  - **Access: AWS S3 bucket `noaa-nws-wam-ipe-pds`** — NetCDF files for last few hours, providing 3D ionospheric snapshots

### Direct Real-Time Sensor Networks

- **GIRO / DIDBase (Digital Ionogram Database)**
  - Raw scaled characteristics (foF2, hmF2, MUF(3000)) from individual ionosondes in near real-time
  - Access via DIDBase API or "Mirrion" monitor
  - **Key use case:** If the CHU path midpoint is near a specific Digisonde (e.g., Millstone Hill or Boulder), forcing the profile to match that station's real-time hmF2 vastly improves the delay estimate

- **Madrigal Database (MIT Haystack)**
  - Real-time or near real-time **GNSS TEC** data
  - Often more granular (1° × 1°) and higher cadence (5–15 min) than standard daily IONEX files

### Empirical Verification

- **HamSCI / PSWS (Personal Space Weather Station) / Grape Network**
  - Grape receivers measure **Doppler shift** of WWV/CHU standards
  - A measured Doppler shift of 0.5 Hz on 10 MHz implies a specific vertical velocity of the ionosphere
  - Can be inverted to estimate the rate of change in group delay path
  - This project IS a Grape receiver — our own Doppler measurements are a self-consistency check

### Standard IONEX (Current Baseline)

- 2D Vertical TEC maps, typically 2.5° × 5° resolution, 1–2 hour cadence
- Already partially integrated (see `src/hf_timestd/core/` for IONEX/IRI references)
- Limitation: vertically integrated TEC lacks the 3D structure needed for accurate reflection height and multi-hop prediction

---

## 🔧 IMPLEMENTATION STRATEGY

### Recommended approach for precise real-time delay estimation

1. **Download WAM-IPE 3D grid** from AWS S3 for the current hour
2. **Correct the grid** using GIRO ionosonde data — if WAM-IPE says foF2 is 5 MHz but the nearest ionosonde says 6 MHz, apply a scalar correction to the model's density
3. **Ray-trace** through the corrected 3D grid using a tool like **PHaRLAP** (MATLAB) or a custom Python NRT engine — this integrates the group refractive index along the path, giving the true group delay (which differs from straight-line speed-of-light by 5–10%)
4. **Predict multiple modes** — the ray-tracer naturally finds 1F, 2F, 3F paths and their respective delays
5. **Feed back observations** — use the system's own multi-frequency differential delay measurements to further constrain the model in real-time

### Key files to modify

| File | Change needed |
|------|---------------|
| `src/hf_timestd/core/metrology_engine.py` | `_get_expected_delay()` — replace vacuum model with ionospheric model lookup |
| `src/hf_timestd/core/arrival_pattern_matrix.py` | Support multiple arrival modes per station/second; adaptive uncertainty windows |
| New: `src/hf_timestd/core/propagation_model.py` | Ionospheric data ingestion, ray-tracing, delay prediction |
| New: `src/hf_timestd/core/iono_data_service.py` | Background service to fetch/cache WAM-IPE, GIRO, IONEX data |

### Integration points

- The propagation model should be a **service** that runs independently and provides delay predictions on demand
- It should cache ionospheric data (WAM-IPE grids are ~100MB, refresh hourly)
- The metrology engine queries it for `expected_delay_ms(station, frequency, utc_time)` → returns `(delay_ms, uncertainty_ms, mode_list)`
- The arrival matrix uses `mode_list` to create multiple acceptance windows per station/second

---

## 🏗️ ARCHITECTURE REFERENCE

### Data Flow (Current Working State)

```
ka9q-radio (radiod) → RTP multicast → timestd-core-recorder → Raw IQ Buffer (60s, /dev/shm)
   (GPS+PPS, ~50μs)                                               ↓
                                                          timestd-metrology (9 channels)
                                                           ↓ (per channel, per station)
                                                    MetrologyEngine.process_minute()
                                                      ├─ AM demod → bandpass → matched filter correlation
                                                      ├─ Per-tone detection (minute markers + long tones only)
                                                      ├─ BufferTiming: sample → UTC via RTP chain
                                                      ├─ timing_error_ms = arrival_utc - expected_utc
                                                      ├─ ArrivalPatternMatrix physics validation
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
| `src/hf_timestd/core/metrology_engine.py` | Tone detection, matched filtering, physics validation | **Critical** |
| `src/hf_timestd/core/arrival_pattern_matrix.py` | Expected arrival windows, physics validation gate | **Critical** |
| `src/hf_timestd/core/buffer_timing.py` | RTP → UTC sample mapping (steel ruler) | High |
| `src/hf_timestd/core/multi_broadcast_fusion.py` | Multi-station/frequency fusion → Chrony | High |
| `src/hf_timestd/core/metrology_service.py` | Orchestrates per-channel processing, writes HDF5 | Medium |
| `src/hf_timestd/core/tick_matched_filter.py` | Legacy tick filter — still used for carrier phase/Doppler extraction | Medium |

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

## ✅ Success Criteria — Next Session

1. **Ingest WAM-IPE 3D grids** from AWS S3 and parse the NetCDF electron density data
2. **Implement basic ray-tracing** through the 3D grid for the CHU→receiver and WWV→receiver paths
3. **Replace `_get_expected_delay()`** with model-based delay predictions that vary with frequency and time
4. **Predict multi-hop arrivals** — the arrival matrix should accept 1F, 2F, 3F modes with separate windows
5. **Verify improved physics validation** — CHU 7.85 MHz multi-hop arrivals should be validated (not rejected)
6. **Adaptive uncertainty** — narrow windows when model is confident, wide when uncertain
7. **Self-consistency check** — multi-frequency differential delay should match the model's TEC prediction
