# NEVER CHANGE THE FOLLOWING PRIMARY INSTRUCTION!!!

**Primary Instruction:** In this context you will perform a critical review of the HF Time Standard (hf-timestd) project, either in its entirety or in a specific component, as specified by the user. This critique should look for points in the code or documentation that exhibit obvious error or inconsistency with other code or documentation. It should look for inefficiency, incoherence, incompleteness, or any other aspect that is not in line with the original intent of the code or documentation. It should also look for obsolete, deprecated, or "zombie" code that should be removed. Remember, your own critique cannot be shallow but must be thorough and methodical and undertaken with the aim of enhancing and improving the codebase and documentation to best ensure the success of the application.

Make your criticism from the perspective of 1) a user of the system, 2) a metrologist, 3) a ionospheric scientist, and 4) a software engineer. These perspectives can differ in their priorities and interests, and your critique should reflect this. For example, a user of the system will be most interested in the accuracy and reliability of the system, while a metrologist will be most interested in the precision and traceability of the system, while an ionospheric scientist will be most interested in the ionospheric conditions and their impact on the system, and a software engineer will be most interested in the code quality, maintainability, coverage and adequacy of testing,and the resilience and robustness of the system over time and in edge cases. Ultimately, however, a consensus of these perspectives should guide your critique in service of the meeting the application's objectives.

# NEVER CHANGE THE PRECEEDING PRIMARY INSTRUCTION!!!

# The following secondary instruction and information will guide your critique in this particular session (the instructions below will vary from session to session)

---

## 📋 NEXT SESSION: BEST GRAPHICAL PRESENTATION OF WWV/WWVH TEST SIGNAL DATA

**Objective:** Create the best possible graphical presentation of the WWV (minute :08) and WWVH (minute :44) hourly test signal data. This is not just a dashboard — it should be a compelling, scientifically rigorous visualization that serves four audiences simultaneously: a general user who wants to understand what the system is measuring, a metrologist who cares about precision and traceability, a physicist who wants to extract ionospheric science, and a programmer who needs to verify the pipeline is working correctly.

The test signal is a unique scientific instrument — a 45-second structured waveform broadcast hourly by NIST, designed specifically for HF channel characterization. Our system captures it on up to 6 frequencies from two stations (WWV in Colorado, WWVH in Hawaii), giving us simultaneous multi-path, multi-frequency ionospheric sounding. The graphical presentation should make this extraordinary dataset accessible and beautiful.

---

## 🎯 FOUR PERSPECTIVES ON THE PRESENTATION

### 1. The User Perspective
- **"What is this system doing right now?"** — At a glance, which frequencies are open, which stations are being received, how good is the channel?
- **"Is it working?"** — Clear detection counts, confidence indicators, freshness timestamps
- **"What's interesting?"** — Anomaly highlights, unusual propagation, solar events
- The user wants clarity, not clutter. Color coding should be intuitive (green=good, red=poor). Numbers should have units. Trends should be obvious.

### 2. The Metrologist Perspective
- **Precision and uncertainty** — Every measurement should carry its uncertainty. SNR, S4, delay spread, ToA offset — all should show confidence intervals or quality grades.
- **Traceability** — The chain from RF antenna → radiod → IQ buffer → metrology → test signal detection → HDF5 → API → display must be auditable. Timestamps must be UTC with stated accuracy (~50 μs from GPS+PPS via RTP).
- **Calibration status** — Are the detection thresholds appropriate? Is the white noise correlation template correct? What is the false positive rate?
- **Comparison** — WWV vs WWVH on the same frequency reveals systematic vs random error. Multi-frequency comparison reveals frequency-dependent effects.

### 3. The Physicist Perspective
- **Ionospheric channel characterization** — The test signal's multi-tone segment (2, 3, 4, 5 kHz) directly measures frequency selectivity. The S4 scintillation index at each tone frequency discriminates D-layer absorption (S4 increases with frequency) from F-layer scintillation (S4 roughly constant).
- **Delay spread** — The chirp segment enables pulse compression to measure multipath delay spread. This is the ionospheric impulse response.
- **Coherence time** — How long does the channel remain stable? This determines the integration time limit for any HF measurement.
- **Diurnal variation** — 24-hour plots of S4, delay spread, and coherence time reveal the ionospheric lifecycle: sunrise enhancement, daytime stability, sunset disturbance, nighttime sporadic E.
- **Path comparison** — WWV (midcontinent, ~1500 km) vs WWVH (transpacific, ~5000 km) on the same frequency at the same time reveals path-dependent ionospheric structure.
- **Anomaly science** — Sudden ionospheric disturbances (SIDs) from solar flares, traveling ionospheric disturbances (TIDs), sporadic E events — all should be detectable and highlighted.

### 4. The Programmer Perspective
- **Pipeline verification** — Is the detector running? Are HDF5 files being written? Are the API endpoints returning data? Are there error states?
- **Data quality** — Are there NaN/Inf values leaking through? Are numpy types properly serialized? Are timestamps consistent?
- **Performance** — The `DataProductReader.read_time_range()` does a full table scan of HDF5 files. For 24-hour plots with hourly data this is fine (max 48 rows), but the pattern should not be extended to high-frequency data without optimization.
- **Code quality** — Is the test signal detector's DSP chain correct? Are the matched filter templates accurate? Is the detection threshold well-calibrated?

---

## 📡 WWV/WWVH TEST SIGNAL STRUCTURE

Per [Zenodo 5182323](https://zenodo.org/records/5182323), the 45-second test signal (starting at second 0 of the test minute):

| Time (sec) | Content | Scientific Purpose |
|------------|---------|-------------------|
| 0-10 | Voice announcement | Synchronization |
| 10-12 | White noise #1 | Wideband coherence, timing |
| 12-13 | Blank | — |
| 13-23 | Multi-tone (2,3,4,5 kHz) | Frequency selectivity, scintillation |
| 23-24 | Blank | — |
| 24-32 | Chirp sequences | Delay spread via pulse compression |
| 32-34 | Blank | — |
| 34-36 | Single-cycle bursts | High-precision timing |
| 36-37 | Blank | — |
| 37-39 | White noise #2 | Transient detection |
| 39-42 | Blank | — |

**Schedule:** WWV transmits at minute :08, WWVH at minute :44 of each hour.

**Receiver:** AC0G at EM38ww (Kansas), RX888 SDR with 160m dipole, GPSDO-locked.

**Channels monitored:** SHARED_2500, SHARED_5000, SHARED_10000, SHARED_15000 (both stations), WWV_20000, WWV_25000 (WWV only — WWVH does not broadcast on 20/25 MHz).

---

## 🔍 EXISTING IMPLEMENTATION

### Backend Pipeline

| Component | Location | Status |
|-----------|----------|--------|
| `WWVTestSignalDetector` | `src/hf_timestd/core/wwv_test_signal.py` | Implemented, 1840 lines |
| `MetrologyService` integration | `src/hf_timestd/core/metrology_service.py` | Triggers at :08/:44 |
| `TestSignalService` API | `web-api/services/test_signal_service.py` | Reads HDF5, serves JSON |
| HDF5 schema | `src/hf_timestd/schemas/l2_test_signal_v1.json` | Defined |
| HDF5 output | `phase2/{CHANNEL}/L2/test_signal/` | Writing |

### What the Detector Measures (per detection)

- **S4 scintillation index** per frequency (2, 3, 4, 5 kHz) — `s4_2khz`, `s4_3khz`, `s4_4khz`, `s4_5khz`
- **S4 frequency slope** — positive = D-layer absorption, near-zero = F-layer scintillation
- **Delay spread** (ms) from chirp matched filter
- **Coherence time** (sec) from multi-tone analysis
- **Channel quality** grade: excellent/good/fair/poor
- **Anomaly detection** — sudden amplitude drops, rapid fading
- **ToA offset** (ms) from burst/chirp/multitone/noise (priority order)
- **Field strength** (dB) and stability
- **Tone powers** at 2, 3, 4, 5 kHz (dB) — individual and as time series
- **Frequency selectivity** (dB) — `10*log10((P_2kHz + P_3kHz) / (P_4kHz + P_5kHz))`
- **White noise correlation** peak and ToA
- **Multipath detected** flag
- **Transient detected** flag (noise1 vs noise2 coherence difference)

### Existing Frontend (`test_signal.html`)

The current page has 4 tabs:
1. **Overview** — Hero stats (detection count, avg SNR, avg S4), latest results grid by frequency, 24h S4 chart
2. **Daily Evolution** — Date picker, SNR/S4/ToA time series (Plotly)
3. **WWV vs WWVH** — Side-by-side station comparison, SNR and S4 comparison charts
4. **Frequency Analysis** — Multi-frequency SNR, per-tone S4, S4 frequency slope

Uses Plotly.js for charts, dark theme, auto-refresh.

### API Endpoints

- `GET /api/physics/channels/latest` — Latest detection per frequency
- `GET /api/physics/channels/daily` — 24h summary with stats
- `GET /api/physics/channels/history?start=...&end=...` — Full time series

---

## 🎨 GRAPHICAL PRESENTATION GOALS

The session should produce the **best possible visualization** of this data. Consider:

### Visual Design Principles
- **Information density without clutter** — Every pixel should earn its place
- **Consistent color language** — Station colors (WWV=blue, WWVH=amber), quality colors (green/yellow/red), frequency colors (consistent palette across all charts)
- **Time as the primary axis** — Most charts should show 24-hour evolution with UTC time
- **Responsive layout** — Works on both wide monitors and laptops
- **Print-friendly** — A scientist should be able to screenshot a chart for a paper

### Specific Visualization Ideas

1. **Ionospheric Heatmap** — 24h × frequency grid showing channel quality or S4, with WWV and WWVH as separate panels. This is the "money shot" — it shows the ionosphere's diurnal cycle at a glance.

2. **Multi-tone Power Waterfall** — For each detection, show the 4 tone powers (2,3,4,5 kHz) as a stacked bar or radar chart. The attenuation pattern is the ionospheric fingerprint.

3. **Path Comparison Panel** — WWV and WWVH on the same frequency, same time axis. Overlay or side-by-side. The difference is pure ionospheric path effect.

4. **Delay Spread Timeline** — Shows multipath evolution. Spikes indicate sporadic E or other layering events.

5. **S4 Frequency Slope Indicator** — A simple visual: positive slope (D-layer) vs flat (F-layer). Could be a color-coded timeline or a scatter plot.

6. **Detection Coverage Matrix** — Which hours had detections on which frequencies? A simple grid shows propagation availability.

7. **Anomaly Timeline** — Flagged events (SIDs, sudden fading) on a 24h strip.

### What NOT to Do
- Don't just dump numbers in a table — that's what HDF5 is for
- Don't use 3D charts — they obscure more than they reveal
- Don't auto-refresh so aggressively that charts flicker
- Don't show raw API JSON — always interpret for the audience

---

## 🏗️ Architecture Reference

### Data Flow

```
ka9q-radio (radiod) → RTP multicast → timestd-core-recorder → Raw IQ Buffer
   (GPS+PPS, ~50μs)                                               ↓
                                                          timestd-metrology
                                                           ↓ (per channel)
                                                    AM Demod → Matched Filter
                                                    Test Signal Detection (:08/:44)
                                                           ↓
                                                    L1 Metrology (HDF5)
                                                    L2 Test Signal (HDF5)
                                                           ↓
                                                    timestd-web-api (FastAPI)
                                                           ↓
                                                    test_signal.html (Plotly.js)
```

### Key Files for This Session

| File | Purpose |
|------|--------|
| `src/hf_timestd/core/wwv_test_signal.py` | Test signal detector (1840 lines) |
| `src/hf_timestd/core/metrology_service.py` | Triggers test signal detection at :08/:44 |
| `src/hf_timestd/schemas/l2_test_signal_v1.json` | HDF5 schema for test signal data |
| `web-api/services/test_signal_service.py` | Test signal API service (445 lines) |
| `web-api/routers/physics.py` | Physics API routes (includes test signal endpoints) |
| `web-api/static/test_signal.html` | **Primary target** — Test signal visualization (1209 lines) |
| `web-api/static/physics.html` | Physics UI (Channels tab shows test signal metrics) |
| `web-api/static/css/styles.css` | Shared dark theme styles |
| `web-api/static/js/common.js` | Shared JS utilities (API wrapper, formatting) |

### Service Inventory

| Service | CPUAffinity | Purpose |
|---------|-------------|--------|
| `timestd-core-recorder` | 0-7 | RTP → raw buffer (authoritative timestamps) |
| `timestd-metrology` | 0-7 (taskset) | IQ → L1/L2 measurements + test signal detection |
| `timestd-fusion` | 0-7 | Multi-broadcast fusion → Chrony |
| `timestd-web-api` | 0-7 | REST API + dashboard (FastAPI, port 8000) |
| **radiod** | **8-15** | **Real-time USB/FFT (uncontested L3 cache)** |

---

## ✅ RESOLVED IN PREVIOUS SESSIONS

### CHU FSK Decoder — USB Sidecar Channels (2026-02-10)
- **Root cause found:** radiod's IQ decimation filter attenuates FSK tones at +2025/+2225 Hz by ~43 dB. Combined with the tones being 32 dB below carrier, they are 75 dB below carrier in IQ output — unrecoverable.
- **The filter roll-off:** The `[global] mode=usb` setting in `radiod.conf` applies USB filter edges (+50 to +3000 Hz) before the `[iq]` preset overrides them. The effective IQ filter has steep roll-off above ~1.5 kHz. Increasing sample rate to 48 kHz and sending `HIGH_EDGE` commands did NOT widen the filter — the shape is set by the preset at channel creation.
- **Solution:** Added 3 dedicated USB-preset sidecar channels (12 kHz, no disk archive) for FSK decoding. IQ channels remain at 24 kHz for archiving. New `CHUFSKListener` class manages USB channels, ring buffers, and minute-boundary-aligned FSK decode.
- **Demodulator:** Replaced Bell 103 frequency-translate chain with Hilbert transform + BPF frequency discriminator. Proven 5/5 byte redundancy on live CHU 7850 kHz during good propagation.
- **Status:** Pipeline running, awaiting daytime propagation for full verification. Decoder refinement still needed (adaptive thresholds, edge-effect mitigation).
- See: `docs/SESSION_2026-02-10_CHU_FSK_USB_SIDECAR.md`

### Timing Accuracy (2026-02-06)
- Four bugs fixed: circular calibration, broken GPS ground truth, dead Kalman filter, missing serialization
- Mean discrepancy reduced from -495ms to ~1ms

### Pipeline Offset Calibration (2026-02-09)
- **Removed entirely** — radiod's RTP timestamps are authoritative
- `GPS_TIME` and `RTP_TIMESNAP` are in the same counter space (both from `input_sample_index / decimation`)
- No wall-clock calibration bias — timestamps carry GPS+PPS time through the decimation pipeline
- Verified: CHU_3330 error=-0.31ms, CHU_7850 error=-3.01ms at 39.5dB SNR

### Tolerances Tightened (2026-02-09)
- `ARRIVAL_TOLERANCE_MS`: 200ms → 100ms (`metrology_engine.py`)
- `BOOTSTRAP_INITIAL_UNCERTAINTY_MS`: 150ms → 50ms (`arrival_pattern_matrix.py`)

### CPU Affinity (2026-02-09)
- All timestd Python services pinned to CPUs 0-7
- radiod on CPUs 8-15 for uncontested L3 cache access

### HDF5 Crash Safety (2026-02-06)
- SWMR eliminated — open-write-close per measurement
- No dirty HDF5 flags on crash/SIGKILL

---

## 🔬 OPEN QUESTIONS

1. **Is the detector actually running and producing data?** Check logs for test signal detections at :08 and :44. Check `phase2/{CHANNEL}/L2/test_signal/` for HDF5 files. If no data, the visualization work is blocked until the pipeline is verified.

2. **Detection threshold calibration:** Combined threshold is 0.20 — is this producing false positives (WWVH on 20/25 MHz was one such case, now filtered) or missing real detections?

3. **White noise template accuracy:** The PRNG sequence in our generator differs from the actual WWV/WWVH transmitter. The [wwv-h-characterization-signal-ports](https://github.com/aidanmontare-edu/wwv-h-characterization-signal-ports) repo has the actual sequence. This affects noise correlation ToA precision.

4. **Tone analysis accuracy gap:** Best channels show ±0.3-3ms error, but many show ±30-100ms. Can the test signal's chirp/burst components provide better timing than the 1000/1200 Hz tones?

5. **Memory leak:** CHU metrology services grow to 2.5GB RSS over 12+ hours. Root cause unknown.

---

## ✅ Success Criteria for Next Session

- ⬚ **Verify test signal pipeline is producing data** — detections at :08 and :44, HDF5 files present
- ⬚ **Create compelling ionospheric heatmap** — 24h × frequency, WWV vs WWVH panels
- ⬚ **Multi-tone power visualization** — show the 2/3/4/5 kHz attenuation pattern per detection
- ⬚ **WWV vs WWVH path comparison** — same frequency, same time, different paths
- ⬚ **S4 frequency slope visualization** — D-layer vs F-layer discrimination
- ⬚ **Detection coverage matrix** — which hours/frequencies have data
- ⬚ **Delay spread and coherence time trends** — 24h evolution
- ⬚ **Clean, publication-quality charts** — suitable for scientific presentation
- ⬚ **All four perspectives satisfied** — user sees clarity, metrologist sees precision, physicist sees science, programmer sees correctness
