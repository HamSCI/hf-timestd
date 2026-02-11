# NEVER CHANGE THE FOLLOWING PRIMARY INSTRUCTION!!!

**Primary Instruction:** In this context you will perform a critical review of the HF Time Standard (hf-timestd) project, either in its entirety or in a specific component, as specified by the user. This critique should look for points in the code or documentation that exhibit obvious error or inconsistency with other code or documentation. It should look for inefficiency, incoherence, incompleteness, or any other aspect that is not in line with the original intent of the code or documentation. It should also look for obsolete, deprecated, or "zombie" code that should be removed. Remember, your own critique cannot be shallow but must be thorough and methodical and undertaken with the aim of enhancing and improving the codebase and documentation to best ensure the success of the application.

Make your criticism from the perspective of 1) a user of the system, 2) a metrologist, 3) a ionospheric scientist, and 4) a software engineer. These perspectives can differ in their priorities and interests, and your critique should reflect this. For example, a user of the system will be most interested in the accuracy and reliability of the system, while a metrologist will be most interested in the precision and traceability of the system, while an ionospheric scientist will be most interested in the ionospheric conditions and their impact on the system, and a software engineer will be most interested in the code quality, maintainability, coverage and adequacy of testing,and the resilience and robustness of the system over time and in edge cases. Ultimately, however, a consensus of these perspectives should guide your critique in service of the meeting the application's objectives.

# NEVER CHANGE THE PRECEEDING PRIMARY INSTRUCTION!!!

# The following secondary instruction and information will guide your critique in this particular session (the instructions below will vary from session to session)

---

## 📋 NEXT SESSION: DEBUG TICK MATCHED FILTER — ±50ms TIMING SCATTER

**Objective:** The tick matched filter (`tick_matched_filter.py`) produces timing offsets with **σ = 50–77ms** across its 55 overlapping 5-second windows per minute. This scatter is visible on the 24-hour dashboard across **all 3 stations (bee1, B3-1, B4-1)** and **all channels** simultaneously. The expected σ for a working matched filter at these SNR levels (10–40 dB) is < 1ms. The ±50ms scatter is the dominant error source in the entire system and must be fixed before any downstream product (timing, phase, Doppler) can be trusted.

**This is NOT a regression from the 2026-02-11 phase continuity fix.** Log analysis confirms the same σ = 50–77ms was present on 2026-02-10 (before any changes). The phase continuity fix (buffer-relative time for IQ mixer) was correct for its purpose but does not address this separate, pre-existing problem.

---

## 🚨 THE PROBLEM IN DETAIL

### What the dashboards show (2026-02-11, all 3 machines)

The 24-hour dashboard (`dashboard-24h.html`) plots `timing_error_ms` = `raw_toa_ms - expected_propagation_delay_ms` for each broadcast. The data comes from two sources:
1. **`L2/timing_measurements`** — individual RTP-based single-tick detections (sparse dots)
2. **`L2/tick_timing`** — tick matched filter aggregate per minute (dense scatter, `mean_timing_offset_ms`)

The dense scatter shows D_err bouncing ±50ms around zero for ALL stations on ALL channels. The scatter does NOT correlate with SNR (it's just as bad at 40 dB as at 10 dB). It does NOT follow the smooth diurnal ionospheric curve expected. It appears on all 3 independent receivers simultaneously, ruling out local hardware issues.

### Production log evidence (bee1, SHARED_10000, 2026-02-11 18:30 UTC)

```
WWV tick analysis  - 48/55 windows, raw_toa=-0.6ms,  std=76.7ms, drift=-0.594ms/s
WWVH tick analysis - 55/55 windows, raw_toa=+8.2ms,  std=56.6ms, drift=0.818ms/s
BPM tick analysis  - 47/55 windows, raw_toa=-23.3ms, std=53.7ms, drift=0.663ms/s
```

- **`std=76.7ms`** — the correlation peak position varies by ±77ms across 55 overlapping windows that share 4/5 of their data
- **`drift=-0.594ms/s`** — apparent drift of 0.6ms/s is physically impossible (would require km/s ionospheric motion)
- **Physics REJECTED**: arrivals at 955–999ms (near end of second, not beginning)
- **Cross-station timing INVALID**: WWV arriving after WWVH (impossible given distances)
- **Stability metrics over 60 minutes**: WWV arrival=25.1±17.2ms, WWVH=17.0±23.7ms, BPM=-10.1±30.1ms

### Same problem on 2026-02-10 (before any code changes)

```
WWV tick analysis  - 45/55 windows, raw_toa=-32.4ms, std=59.0ms, drift=0.474ms/s
WWVH tick analysis - 54/55 windows, raw_toa=-8.4ms,  std=64.8ms, drift=-0.370ms/s
BPM tick analysis  - 45/55 windows, raw_toa=-19.2ms, std=65.7ms, drift=-0.733ms/s
```

This confirms the problem is pre-existing, not a regression.

---

## 🔍 ROOT CAUSE HYPOTHESES (ranked by likelihood)

### Hypothesis 1: Correlation envelope has multiple peaks of similar height (MOST LIKELY)

The composite template (`_build_composite_template()`) places 5 tick templates at 1-second intervals within a 5-second window. The correlation of this composite against the audio signal produces an envelope with peaks at each tick position. If the ticks have similar amplitude, the envelope has **5 peaks of similar height** separated by 1 second (20,000 samples). The `search_range_ms=100` parameter limits the search to ±100ms around center — but "center" is `len(envelope) // 2`, which is the middle of the 5-second window. This means:

- The search region is ±100ms around the center of the correlation output
- But the correlation peak for a composite template should be at the point where ALL ticks align simultaneously
- If the composite correlation has sidelobes within ±100ms (from partial tick alignment), noise selects different sidelobes in different windows

**Key question:** Is `correlate(audio, template, mode='same')` the right approach for a composite template? The composite template is 5 seconds long (100,000 samples). The correlation output is also 100,000 samples. The peak should be near the center (sample 50,000) when ticks are at expected positions. But the ±100ms search (±2,000 samples) around center may contain multiple sidelobes from the composite structure.

**Diagnostic:** Plot the full correlation envelope for one window. Count peaks within ±100ms of center. Measure their relative heights.

### Hypothesis 2: Template-signal mismatch due to bandpass filter

`process_window()` applies a 4th-order Butterworth bandpass (±100 Hz around tick frequency) via `sosfiltfilt`. The template is generated without this filter. If the filter alters the tick waveform shape (e.g., ringing, envelope distortion), the correlation peak broadens and the peak position becomes noise-sensitive.

**Diagnostic:** Compare correlation SNR with and without the bandpass filter. Check if the filter's impulse response duration (~10ms for 100 Hz bandwidth) is comparable to the tick duration (5ms for WWV).

### Hypothesis 3: AM demodulation artifacts on shared channels

For WWV/WWVH/BPM (AM stations), audio is extracted as `|IQ| - mean(|IQ|)`. On shared channels, multiple AM carriers are present. The envelope detector produces intermodulation products (beat frequencies between carriers). These beats can create spurious correlation peaks.

**Diagnostic:** Compare tick filter std on unambiguous channels (CHU_3330, WWV_20000) vs shared channels (SHARED_10000). If unambiguous channels have lower std, intermodulation is a factor.

### Hypothesis 4: The composite template approach is fundamentally flawed

Correlating a 5-second composite template against a 5-second audio window using `scipy.signal.correlate(mode='same')` produces a 5-second output. The peak position represents the **average** timing offset across all ticks in the window. But if individual ticks have different offsets (e.g., due to multipath, interference, or the tick at second 29 being absent), the composite peak position is pulled by the dominant tick, not the true timing.

**Alternative approach:** Correlate each tick individually (single-tick template against 1-second audio slice), then combine the offsets. This would give per-tick timing and reveal which ticks are reliable.

### Hypothesis 5: The `offset_samples = peak_idx_refined - center` calculation is wrong

The correlation output center (`len(envelope) // 2`) assumes the template is centered in the window. But `_build_composite_template()` places ticks at positions `(sec - start_second) * sample_rate` within the window — the first tick is at position 0, not at the center. This means the correlation peak is NOT at the center when ticks are at expected positions. The offset calculation would be systematically biased.

**Diagnostic:** For a window starting at second 10, the first tick is at sample 0, last at sample 80,000. The template "center of mass" is at sample 40,000. The correlation center is at sample 50,000. This 10,000-sample (500ms) discrepancy could explain the large offsets.

---

## 🏗️ ARCHITECTURE REFERENCE

### Data Flow

```
ka9q-radio (radiod) → RTP multicast → timestd-core-recorder → Raw IQ Buffer (60s, /dev/shm)
   (GPS+PPS, ~50μs)                                               ↓
                                                          timestd-metrology (9 channels)
                                                           ↓ (per channel, per station)
                                                    TickMatchedFilter.process_minute()
                                                      ↓ 55 overlapping 5-sec windows (1s step)
                                                    process_window() → _correlate_window()
                                                      ├─ AM demod → bandpass → composite template correlation
                                                      ├─ Peak finding → timing_offset_ms  ← THE PROBLEM
                                                      ├─ IQ × exp(-j2πft) → carrier_phase_rad
                                                      └─ mean(IQ) → dc_carrier_phase_rad
                                                           ↓
                                                    TickDetectionResult (per window)
                                                           ↓
                                                    MinuteTickAnalysis (aggregate: mean, std, drift)
                                                           ↓
                                                    MetrologyEngine._process_minute_rtp()
                                                      ├─ tick_analysis.mean_timing_offset_ms → timing_error_ms
                                                      ├─ ArrivalPatternMatrix validation
                                                      └─ L1MetrologyMeasurement → fusion
                                                           ↓
                                                    HDF5: L2/tick_timing, L2/tick_phase, L2/timing_measurements
                                                           ↓
                                                    web-api dashboard-24h.html (plots D_err scatter)
```

### Key Files

| File | Purpose | Priority |
|------|---------|----------|
| `src/hf_timestd/core/tick_matched_filter.py` | **THE BUG IS HERE** — `_correlate_window()`, `_build_composite_template()`, `process_window()`, `process_minute()` | **Critical** |
| `src/hf_timestd/core/metrology_engine.py` | Calls tick filter, validates results, feeds fusion. Lines 1150-1182: tick analysis → timing_error_ms | High |
| `src/hf_timestd/core/metrology_service.py` | Orchestrates per-channel processing, writes HDF5 | Medium |
| `web-api/routers/dashboard.py` | Lines 213-258: reads `L2/tick_timing` and plots `mean_timing_offset_ms` as D_err | Reference |
| `tests/test_tick_matched_filter.py` | Existing tests — extend with timing accuracy tests | High |

### Key Parameters in tick_matched_filter.py

| Parameter | Value | Concern |
|-----------|-------|---------|
| `window_seconds` | 5 | 5-second windows with 5 ticks each |
| `overlap_seconds` | 1 | 1-second step → 55 windows per minute |
| `search_range_ms` | 100.0 | ±100ms search around center — may contain sidelobes |
| `sample_rate` | 20,000 | 20 kHz IQ sample rate |
| `bandpass` | ±100 Hz around tick freq | 4th-order Butterworth, sosfiltfilt |
| `min_snr_db` | 3.0 | Very low threshold — may admit noise peaks |

### HDF5 Data Products

| Product | Path | Rate | Key Fields |
|---------|------|------|------------|
| `L2/tick_timing` | `phase2/{CH}/tick_timing/` | ~1 row/station/min | `mean_timing_offset_ms`, `std_timing_offset_ms`, `valid_windows`, `mean_snr_db` |
| `L2/tick_phase` | `phase2/{CH}/tick_phase/` | ~55 rows/station/min | `timing_offset_ms`, `carrier_phase_rad`, `dc_carrier_phase_rad`, `snr_db` |
| `L2/timing_measurements` | `phase2/{CH}/timing_measurements/` | ~1-15 rows/min | `raw_toa_ms`, `snr_db`, `station`, `frequency_mhz` |
| `L2/detection_attempts` | `phase2/{CH}/detection_attempts/` | ~45 rows/min | All attempts with rejection reasons |

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
- **Production install**: `/opt/hf-timestd/` (copied, not symlinked)
- **Update**: `sudo scripts/update-production.sh --pull` (git pull → pip install → rsync web-api → restart services)
- **3 machines**: bee1 (primary), B3-1, B4-1 — all show same problem

---

## 🎯 FOUR PERSPECTIVES ON THE TICK FILTER PROBLEM

### 1. The User Perspective
- **"The dashboard shows garbage timing data"** — D_err scatter of ±50ms makes the 24h dashboard useless for seeing ionospheric delay variations (which are typically 1-5ms diurnal)
- **"Fusion quality is degraded"** — The fusion Kalman filter receives timing_error_ms with std=50ms, so it either rejects most measurements or produces poor estimates
- **"All 3 stations show the same problem"** — This is not a local issue; it's a systematic algorithm failure

### 2. The Metrologist Perspective
- **Measurement uncertainty is 50-100× worse than achievable** — At 20 dB SNR with 5 ticks per window, timing precision should be ~0.1ms, not 50ms
- **The mean is also wrong** — `raw_toa` values of -46ms to +31ms are far from the expected ~4ms (WWV) or ~22ms (WWVH) propagation delays
- **Drift rate is unphysical** — 0.5-3 ms/s drift implies ionospheric velocity of km/s, which is impossible

### 3. The Ionospheric Scientist Perspective
- **The diurnal curve is buried in noise** — The smooth ionospheric delay variation (±2ms over 24h) is invisible under ±50ms scatter
- **Mode transitions are undetectable** — A 1F→2F mode change adds ~2ms delay; this is lost in the noise
- **Phase/Doppler products are downstream of this** — If timing is wrong by 50ms, the carrier phase extraction (which uses `offset_samples` to locate ticks in IQ) is also wrong

### 4. The Programmer Perspective
- **The composite correlation approach needs rethinking** — `scipy.signal.correlate(mode='same')` with a 100,000-sample template against 100,000-sample audio is computationally expensive and may not be the right tool
- **Per-tick correlation would be simpler and more robust** — Correlate a single-tick template against each 1-second slice, get per-tick offsets, then combine
- **The search_range_ms=100 may be too narrow or too wide** — Need to understand the correlation envelope structure
- **The bandpass filter may be hurting more than helping** — Its impulse response (~10ms) is comparable to the tick duration (5ms), potentially distorting the correlation peak

---

## ✅ RESOLVED IN PREVIOUS SESSIONS

### Phase Continuity Fix + Doppler UI (2026-02-11, session 2)
- **Phase continuity bug fixed**: IQ mixer used window-relative time (t=0 per tick), causing ~1.7 rad phase jumps. Fixed: buffer-relative time (sample_index/sample_rate), independent of RTP/GPS/NTP timing authority.
- **Per-tick phase extraction**: Phase now extracted from individual ticks (not whole 5-second window), phasors combined coherently. Eliminates inter-tick noise dilution.
- **Regression tests**: `test_carrier_phase_continuity` and `test_dc_carrier_phase_stability` verify σ < 0.3 rad.
- **Phase/Doppler web dashboard**: 4 API endpoints + visualization page. Default: single trace visible, click legend to add more.
- **HDF5 corrupt chunk recovery**: `hdf5_reader.py` binary-searches for last good row when gzip chunk is truncated.
- See: `docs/changes/SESSION_2026_02_11_PHASE_CONTINUITY_AND_DOPPLER_UI.md`

### Phase Extraction & Cross-Talk Fix (2026-02-11, session 1)
- Three-tier phase extraction implemented: audio phase, IQ carrier phase, DC carrier phasor
- Cross-frequency discrimination gate: 3 dB advantage required between 1000↔1200 Hz
- WWV/WWVH detection rates now distinct on shared channels (72% vs 55%)
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

## ✅ Success Criteria — Next Session

1. **Diagnose the correlation envelope** — Plot the full envelope for one window, identify why the peak position varies by ±50ms between overlapping windows
2. **Fix the tick matched filter** — Achieve timing std < 2ms across windows (100× improvement over current 50-77ms)
3. **Verify on production data** — All 3 machines should show tight D_err scatter on the 24h dashboard
4. **Per-tick timing** — If the composite approach is abandoned, implement per-tick correlation and verify per-tick offsets are consistent
5. **Regression tests** — Add tests that verify timing precision, not just phase continuity
6. **Dashboard should show clean diurnal curves** — D_err should follow the smooth ionospheric delay pattern, not scatter randomly
