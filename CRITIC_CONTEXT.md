# NEVER CHANGE THE FOLLOWING PRIMARY INSTRUCTION!!!

**Primary Instruction:** In this context you will perform a critical review of the HF Time Standard (hf-timestd) project, either in its entirety or in a specific component, as specified by the user. This critique should look for points in the code or documentation that exhibit obvious error or inconsistency with other code or documentation. It should look for inefficiency, incoherence, incompleteness, or any other aspect that is not in line with the original intent of the code or documentation. It should also look for obsolete, deprecated, or "zombie" code that should be removed. Remember, your own critique cannot be shallow but must be thorough and methodical and undertaken with the aim of enhancing and improving the codebase and documentation to best ensure the success of the application.

Make your criticism from the perspective of 1) a user of the system, 2) a metrologist, 3) a ionospheric scientist, and 4) a software engineer. These perspectives can differ in their priorities and interests, and your critique should reflect this. For example, a user of the system will be most interested in the accuracy and reliability of the system, while a metrologist will be most interested in the precision and traceability of the system, while an ionospheric scientist will be most interested in the ionospheric conditions and their impact on the system, and a software engineer will be most interested in the code quality, maintainability, coverage and adequacy of testing,and the resilience and robustness of the system over time and in edge cases. Ultimately, however, a consensus of these perspectives should guide your critique in service of the meeting the application's objectives.

# NEVER CHANGE THE PRECEEDING PRIMARY INSTRUCTION!!!

# The following secondary instruction and information will guide your critique in this particular session (the instructions below will vary from session to session)

---

## 📋 NEXT SESSION: WWV/WWVH HOURLY TEST SIGNAL ANALYSIS + WEB-API-UI

**Objective:** Maximize the scientific value extracted from the WWV (minute :08) and WWVH (minute :44) hourly test signals. Ensure the test signal analysis pipeline is working correctly and that results are exposed effectively in the web-api-ui. Review all web-api UI pages for correctness and usability.

---

## 🎯 FUNDAMENTAL ARCHITECTURE UNDERSTANDING

### The Dual-Purpose System

This system operates in two complementary modes. Understanding this duality is essential:

**RTP Mode (Physics Pathway) — The Primary Value:**
- radiod provides **authoritative RTP timestamps** from GPS+PPS (~50 μs accuracy to UTC)
- `GPS_TIME` and `RTP_TIMESNAP` are both derived from `input_sample_index / decimation` — same counter space, no pipeline offset correction needed
- With timing accuracy on the order of 50 microseconds, we can do **precision ionospheric science**
- The propagation delay residuals (T_arrival - T_expected) ARE the ionospheric measurement
- This is where the test signal analysis lives — we know WHEN the signal arrived, so we can study WHAT the ionosphere did to it

**Fusion Mode (Metrology Pathway) — The Recovery Exercise:**
- Attempts to recover UTC from HF broadcasts alone (no GPS)
- Current accuracy: ±5-100 ms depending on ionospheric conditions
- This has NOT yet reached the accuracy we hoped for
- The comparison between fusion-recovered timing and RTP-authoritative timing reveals how well tone analysis can reconstruct the time authority
- This is a research pathway, not the primary operational mode

### Key Implication for This Session

Since RTP mode gives us ~50 μs timing, the test signal analysis can exploit this to:
1. **Precisely time-align** the 45-second test signal structure
2. **Measure ionospheric channel characteristics** (delay spread, coherence, scintillation)
3. **Compare propagation** between WWV (:08) and WWVH (:44) paths
4. **Detect anomalies** (solar flares, sporadic E, TIDs) with high confidence

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

---

## 🔍 TEST SIGNAL ANALYSIS — CURRENT STATE

### Existing Implementation

| Component | Location | Status |
|-----------|----------|--------|
| `WWVTestSignalDetector` | `src/hf_timestd/core/wwv_test_signal.py` | Implemented |
| `MetrologyService` integration | `src/hf_timestd/core/metrology_service.py` | Triggers at :08/:44 |
| `TestSignalService` API | `web-api/services/test_signal_service.py` | Implemented |
| `test_signal.html` | `web-api/static/test_signal.html` | Basic UI exists |
| `physics.html` Channels tab | `web-api/static/physics.html` | Shows per-freq results |
| HDF5 schema | `src/hf_timestd/schemas/l2_test_signal_v1.json` | Defined |
| HDF5 output | `phase2/{CHANNEL}/L2/test_signal/` | Writing |

### What the Detector Measures

- **S4 scintillation index** per frequency (2, 3, 4, 5 kHz)
- **S4 frequency slope** (D-layer vs F-layer discrimination)
- **Delay spread** from chirp matched filter
- **Coherence time** from multi-tone analysis
- **Channel quality** grade (excellent/good/fair/poor)
- **Anomaly detection** (sudden amplitude drops, rapid fading)
- **ToA offset** from burst/chirp/multitone/noise (priority order)
- **Field strength** and stability

### Key Questions for Review

1. **Is the detector actually running?** Check logs for test signal detections at :08 and :44
2. **Are HDF5 files being written?** Check `phase2/{CHANNEL}/L2/test_signal/`
3. **Is the web UI showing results?** Check `test_signal.html` and `physics.html`
4. **Are the detection thresholds appropriate?** Combined threshold is 0.20 — is this too low/high?
5. **Is the white noise correlation working?** The PRNG sequence differs from Python's random — this is a known limitation
6. **Are we exploiting the authoritative RTP timing?** The ~50 μs accuracy should enable sub-sample alignment of the test signal structure

---

## 🌐 WEB-API UI PAGES TO REVIEW

### Page Inventory (13 HTML pages)

| Page | File | API Endpoints | Focus |
|------|------|---------------|-------|
| **Overview** | `index.html` | Various | Landing page |
| **Health** | `health.html` | `/api/health/*` | Service status |
| **Timing** | `metrology.html` | `/api/metrology/fusion/latest` | UTC offset display |
| **Validation** | `timing-validation.html` | `/api/timing-validation/dashboard` | GPS comparison |
| **Stability** | `stability.html` | `/api/stability/*` | Allan deviation |
| **Ionosphere** | `propagation.html` | `/api/propagation/*` | Propagation paths |
| **TEC/TID** | `physics.html` | `/api/physics/*` | Ionospheric science |
| **Solar** | `solar-correlation.html` | `/api/correlations/*` | Solar effects |
| **24h Dashboard** | `dashboard-24h.html` | `/api/dashboard/*` | All broadcasts |
| **Station** | `station.html` | `/api/stations/*` | Per-station detail |
| **Test Signal** | `test_signal.html` | Various | WWV/WWVH test signals |
| **Docs** | `docs.html` | `/api/docs/*` | Living documentation |
| **Logs** | `logs.html` | `/api/logs/*` | System logs |

### Priority Review: Test Signal Pages

**`test_signal.html`:**
- Does it show hourly detection results for all 9 channels?
- Does it display S4 scintillation, delay spread, coherence time?
- Does it show 24-hour trends (which hours have detections)?
- Can you compare WWV (:08) vs WWVH (:44) side by side?
- Does it expose the channel quality grades?

**`physics.html` Channels tab:**
- Does it show per-frequency test signal metrics?
- Is the S4 color coding working (green <0.3, yellow 0.3-0.6, red >0.6)?
- Does it show the S4 frequency slope (D-layer vs F-layer)?

### General UI Review

- Navigation consistency across all 13 pages
- Dark theme styling consistency
- Auto-refresh behavior (60s intervals)
- Error handling when APIs return no data
- Does each page correctly reflect the dual-purpose architecture?
- Are uncertainty and quality clearly communicated?

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
                                                    timestd-fusion
                                                    ↓              ↓
                                             L3 Fusion (HDF5)   Chrony SHM
                                                    ↓
                                             timestd-physics
                                                    ↓
                                             TEC/Science (HDF5)
```

### Key Files for This Session

| File | Purpose |
|------|---------|
| `src/hf_timestd/core/wwv_test_signal.py` | Test signal detector |
| `src/hf_timestd/core/metrology_service.py` | Triggers test signal detection |
| `src/hf_timestd/schemas/l2_test_signal_v1.json` | HDF5 schema |
| `web-api/services/test_signal_service.py` | Test signal API service |
| `web-api/routers/propagation.py` | Propagation/test signal API routes |
| `web-api/routers/physics.py` | Physics API routes |
| `web-api/static/test_signal.html` | Test signal UI |
| `web-api/static/physics.html` | Physics UI (Channels tab) |
| `web-api/static/metrology.html` | Timing UI |
| `web-api/static/dashboard-24h.html` | 24-hour dashboard |
| `src/hf_timestd/core/binary_archive_writer.py` | RTP timestamp handling (authoritative, no pipeline offset) |
| `src/hf_timestd/core/arrival_pattern_matrix.py` | Physics-based arrival predictions (±50ms bootstrap, ±100ms tolerance) |

### Service Inventory

| Service | CPUAffinity | Purpose |
|---------|-------------|---------|
| `timestd-core-recorder` | 0-7 | RTP → raw buffer (authoritative timestamps) |
| `timestd-metrology` | 0-7 (taskset) | IQ → L1/L2 measurements + test signal detection |
| `timestd-l2-calibration` | 0-7 | L2 calibration |
| `timestd-fusion` | 0-7 | Multi-broadcast fusion → Chrony |
| `timestd-physics` | 0-7 | TEC estimation |
| `timestd-web-api` | 0-7 | REST API + dashboard (FastAPI, port 8000) |
| `timestd-radiod-monitor` | — | Hardware health monitoring |
| `timestd-vtec` | — | GNSS VTEC (if running) |
| **radiod** | **8-15** | **Real-time USB/FFT (uncontested L3 cache)** |

---

## ✅ RESOLVED IN PREVIOUS SESSIONS

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

1. **Tone analysis accuracy gap:** Best channels show ±0.3-3ms error, but many show ±30-100ms. The ionospheric scatter dominates. Can the test signal's chirp/burst components provide better timing than the 1000/1200 Hz tones?

2. **Cross-station disagreement:** WWV and CHU can disagree by ~65ms. Is this purely ionospheric, or is there a propagation model error?

3. **Template durations:** CHU template is 100ms, WWV/WWVH template is 20ms. Are these optimal for the actual signal structure?

4. **Test signal white noise:** The PRNG sequence differs from Python's random generator. The [wwv-h-characterization-signal-ports](https://github.com/aidanmontare-edu/wwv-h-characterization-signal-ports) repo has the actual sequence — should we integrate it?

5. **Memory leak:** CHU metrology services grow to 2.5GB RSS over 12+ hours. Root cause unknown.

---

## ✅ Success Criteria for Next Session

- ⬚ **Verify test signal detection is running** on all 9 channels at :08 and :44
- ⬚ **Review test_signal.html** — ensure it shows useful hourly results
- ⬚ **Review physics.html** — ensure Channels tab shows S4, delay spread, coherence
- ⬚ **Expose 24-hour test signal trends** — which hours have detections, S4 evolution
- ⬚ **Compare WWV vs WWVH** test signal results side by side
- ⬚ **Review all 13 web-api UI pages** for correctness, consistency, and usability
- ⬚ **Investigate memory leak** in CHU metrology services (2.5GB RSS)
