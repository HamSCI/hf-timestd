# HamSCI 2026 Workshop — Presentation Abstract

**Author:** Michael James Hauan (AC0G)  
**Last Updated:** February 19, 2026 (revised)  
**Status:** Accepted for presentation

---

## Project Description

**Project Title:** Multi-Static HF Time Signal Analysis for Ionospheric Sounding and TEC Estimation

### Executive Summary

This project implements a precision HF monitoring station that receives 17 distinct time signal broadcasts across nine frequencies from four major national standards stations: WWV (USA), WWVH (Hawaii), CHU (Canada), and BPM (China). A Leo Bodnar GPS-Disciplined Oscillator (GPSDO) phase-locks the receiving hardware (stability ≈ 1 × 10⁻¹²), eliminating local clock drift and ensuring that all measured timing residuals are attributable to the propagation path.

The system (`hf-timestd`, v5.4.1) is fully operational and runs 24/7 as eight independent systemd services on a single Linux host. It extracts high-precision Time of Arrival (ToA), carrier phase, and Doppler measurements from each broadcast, then compares these against predictions from a hierarchical ionospheric model stack: WAM-IPE numerical weather prediction, GIRO real-time ionosonde corrections, IRI-2020 climatology, and GPS-derived IONEX TEC maps. A key result of this work is a precise characterization of the instrument's detection limits: the 24 kHz GPSDO-locked sample clock provides 41.7 µs timing resolution, but the dominant noise source is propagation model error (~6.5 ms 1σ for WWV), which sets the group-delay TEC noise floor far above the ionospheric dispersion signal. Carrier-phase dTEC bypasses this limit entirely, achieving ~0.1 mTECU/s sensitivity — sufficient to detect Traveling Ionospheric Disturbances (TIDs), solar flares, and Sporadic-E onset with large margin. The system is therefore best characterized as a high-sensitivity dTEC/dt sensor and passive oblique ionosonde with 17 simultaneous sounding paths.

### System Architecture & Methodology

- **Instrumentation:** A KA9Q-radio software-defined radio environment provides RTP-timestamped IQ streams at 20 kHz bandwidth per channel, clocked by a Leo Bodnar GPSDO. The common clock ensures coherent reception across all nine monitored channels. Raw IQ is archived as binary files with JSON metadata for reproducibility.

- **Signal Sources:**
    - **WWV (Fort Collins, CO):** 2.5, 5, 10, 15, 20, 25 MHz
    - **WWVH (Kekaha, HI):** 2.5, 5, 10, 15 MHz
    - **CHU (Ottawa, ON):** 3.330, 7.850, 14.670 MHz
    - **BPM (Pucheng, China):** 2.5, 5, 10, 15 MHz

- **Processing Pipeline (Three-Phase Architecture):**

    - **Phase 1 — Core Recorder:** Immutable binary IQ archive with RTP timestamps. Each channel is recorded independently with GPS-derived timing snapshots for RTP-to-UTC calibration.

    - **Phase 2 — Metrology & Analytics:**
        - **Timing Bootstrap:** State machine (ACQUIRING → CORRELATING → TRACKING → LOCKED) achieves UTC lock within ~2 minutes using NTP-confirmed minute markers, without requiring BCD/FSK decode.
        - **Station Discrimination:** Co-channel signals (e.g., WWV vs. WWVH vs. BPM on 10 MHz) are separated using IQ-domain matched filtering with station-specific templates (1000 Hz vs. 1200 Hz), cross-frequency gating (3 dB minimum advantage), geographic incidence angles from the Arrival Pattern Matrix, and station-specific modulation characteristics.
        - **Tick Matched Filter:** Quadrature matched filtering extracts per-second timing and three-tier carrier phase (audio-domain, RF carrier at tone frequency, DC carrier phasor) from up to 57 ticks per minute per station, with sub-sample parabolic interpolation and SNR-weighted robust median ensemble.
        - **Doppler Estimation:** Per-tick carrier phase progression yields ~57 instantaneous Doppler measurements per minute, enabling real-time determination of ionospheric layer velocity and maximum coherent integration window.
        - **BPM Handling:** Station-specific templates account for 10 ms UTC ticks, 100 ms UT1 ticks (minutes 25–29, 55–59), and the 300 ms minute marker. DUT1 corrections are applied to prevent misinterpretation of UT1 offsets as propagation delays.
        - **CHU FSK Decode:** USB sidecar channels demodulate CHU's FSK time code (quadrature demodulation at 2125 Hz) for independent timing validation, achieving 8/9 frame decode at confidence 1.00.

    - **Phase 3 — Fusion & TEC Estimation:**
        - **Multi-Broadcast Fusion:** Dual Kalman filter architecture (L1: raw timing, L2: ionospherically corrected) combines measurements from all detected stations into a single "Steel Ruler" UTC estimate, fed to Chrony as a TSL1/TSL2 dual reference.
        - **TEC Estimation:** Multi-frequency least-squares regression on the 1/f² dispersion relation (τ = 40.3 · sTEC / f²) solves simultaneously for vacuum transit time and slant TEC from N ≥ 2 frequency measurements per station.
        - **Ionospheric Model Stack:** Predictions are generated from a four-tier hierarchy:
            1. **WAM-IPE** (NOAA numerical weather prediction, 5-minute cadence, S3 public bucket)
            2. **GIRO** (real-time ionosonde corrections, blended with WAM-IPE)
            3. **IRI-2020** (climatological model, Fortran via Python bindings)
            4. **Parametric fallback** (diurnal + seasonal + solar activity + latitude terms)
        - **Propagation Model:** `HFPropagationModel` performs numerical group delay integration through Chapman-layer electron density profiles, evaluating multi-mode paths (1F, 2F, 3F, 1E) with MUF checks, adaptive uncertainty, and self-consistency validation.

    - **Space Weather Correlation:**
        - Automated ingestion of GOES X-ray flux, planetary Kp index, and proton flux from NOAA SWPC.
        - Correlation analysis: SNR vs. solar zenith angle, Sudden Ionospheric Disturbance (SID) detection via X-ray/SNR-drop correlation, TEC vs. F10.7 solar flux, propagation mode vs. Kp index.

    - **Web API & Visualization:**
        - FastAPI-based REST API with 15+ endpoint groups: metrology, phase/Doppler, propagation, TEC, space weather, correlations, Allan deviation, station health.
        - Real-time dashboards for timing quality, carrier phase time series, Doppler shifts, scintillation indices, and space weather overlay.

---

### Current Results (February 2026)

#### Operational Metrics

| Metric | Value |
|--------|-------|
| **Channels monitored** | 9 frequencies, 17 broadcasts |
| **L1 timing measurements** | ~15,000/day |
| **Timing accuracy (D_clock)** | Fusion D_clock ≈ 1 ms mean discrepancy vs. GPS ground truth |
| **Chrony TSL offset** | 34–316 μs |
| **Bootstrap convergence** | ~2 minutes to LOCKED state |
| **Tick detection** | 50–57 ticks/min/station (CHU: 55/58, WWV: 57/57) |
| **CHU FSK decode** | 8/9 frames, confidence 1.00 |
| **Phase extraction** | Three-tier: audio, carrier, DC carrier |
| **Carrier phase stability** | DC carrier 30% more stable than audio phase on unambiguous channels |
| **Carrier-phase dTEC records** | ~250,000/day across all stations and frequencies |
| **GRAPE spectrograms** | 9/9 channels uploading to PSWS network |

#### Detection Limit Analysis

A rigorous noise floor characterization establishes what the instrument can and cannot detect:

| Measurement | Noise Floor (1σ) | Signal at 40 TECU | SNR | Verdict |
|---|---|---|---|---|
| Group-delay TEC (WWV, 2.5–25 MHz) | 6.5 ms (model error) | 0.85 ms | 0.13 | Below noise floor |
| Group-delay TEC (CHU, 3.33–14.67 MHz) | ~8 ms (model error, systematic resolved) | 0.46 ms | 0.06 | Below noise floor |
| Group-delay TEC (WWVH/BPM, 2.5–15 MHz) | ~5 ms (model error) | ~0.7 ms | ~0.14 | Below noise floor |
| **Carrier-phase dTEC (1-min integration)** | **~6 mTECU** | **TID: 100–2000 mTECU** | **17–330×** | **Well above noise floor** |

The dominant noise source for group-delay TEC is propagation model error (minute-to-minute ionospheric variability not captured by the model), not instrument noise. The 24 kHz sample clock contributes only 41.7 µs timing noise — negligible compared to the model floor. Carrier-phase dTEC bypasses this entirely: phase noise of ~1 mrad/tick at 20 dB SNR yields dTEC/dt sensitivity of ~0.1 mTECU/s, and 55 ticks/min reduces this to ~6 mTECU integrated over one minute.

#### Ionospheric Products (Current Status)

| Product | Status | Notes |
|---|---|---|
| L2 clock_offset_ms (D_clock) | ✅ Operational | CHU: +4 ms mean (7.85 MHz), +13 ms (14.67 MHz); WWV: 0 ms |
| SNR per broadcast | ✅ Operational | Frequency- and time-varying; D-layer absorption visible |
| Carrier-phase dTEC | ✅ Operational | 250K records/day; primary ionospheric product |
| IONEX VTEC maps | ✅ Written per minute | Based on group-delay TEC; below noise floor pending model improvement |
| All-arrivals (multipath) | ✅ Operational | CHU 7.85 MHz: 374 rows/min, 258 secondary arrivals |
| Group-delay TEC | ⚠️ Below noise floor | Estimator runs; 71% of records confidence < 0.5; model-limited |
| CHU timing systematic | ✅ **Resolved** | 74 ms H3E transmitter sideband filter group delay; corrected in pipeline |
| dTEC multi-station overlay | ✅ Operational | New web visualization: multi-station dTEC time series with SNR filtering |
| Ionogram / ToF cluster | ✅ Operational | New web visualization: Griffin-style ToF vs SNR scatter with KDE contours |

---

### Current Development Status and Future Work

The following items represent the current development state and planned improvements, ordered by scientific impact.

#### Implemented and Operational

- **Carrier-phase dTEC** (250K records/day): Phase rate-of-change converted to dTEC/dt via `dTEC/dt = −f_D · c · f / 40.3`, integrated per minute, anchored to group-delay TEC when confidence ≥ 0.5. This is the primary ionospheric product.
- **Mode-constrained TEC inversion**: Multi-frequency WLS regression on 1/f² dispersion with 3σ outlier rejection and mode-confidence weighting. Currently model-limited (propagation error >> dispersion signal).
- **VTEC map generation**: Per-minute IONEX output from slant-to-vertical TEC mapping with 2D polynomial surface fit across ionospheric pierce points.
- **Propagation model stack**: Four-tier hierarchy (WAM-IPE → GIRO → IRI-2020 → parametric) with numerical group delay integration through Chapman-layer profiles.

#### Near-Term (3 weeks, pre-HamSCI)

- **CHU systematic offset — RESOLVED**: The −74 ms frequency-independent offset on all CHU channels was traced to the H3E (USB + full carrier) transmitter's analog sideband filter group delay. Both the 1000 Hz timing pips and the 2225 Hz FSK mark tone appear ~74 ms late through the identical receiver pipeline; WWV (solid-state digital synthesis) shows 0 ms offset. The 74 ms correction is now applied in the metrology engine and edge detector, with the FSK stop-bit (+6 ms) confirming the corrected CHU clock offset. CHU is now a valid third independent path.
- **dTEC visualization — COMPLETE**: Multi-station dTEC time series overlay with per-station color coding, SNR filtering, downsampling, and 10 MHz reference comparison. Available at `/static/dtec.html`.
- **Ionogram visualization — COMPLETE**: Griffin-style dual-panel ToF time series and ToF vs SNR cluster scatter with KDE density contours. Available at `/static/ionogram.html`.
- **SNR-based D-layer product**: Frequency-stratified SNR time series as a proxy for D-layer absorption, correlated with solar zenith angle and X-ray flux.

#### Future Work

#### 1. Cross-Path Tomographic Constraints

With 17 simultaneous ray paths through the ionosphere at different frequencies and geometries, the system is over-determined for a single-layer TEC model.

- **Recommendation:** Implement a **multi-layer ionospheric tomography** approach:
    1. Divide the ionosphere into E-layer (90–150 km) and F-layer (150–500 km) shells
    2. Each ray path's sTEC is the sum of contributions from shells it traverses
    3. The 17 paths at different elevation angles provide geometric diversity to separate E and F contributions
    4. Constrain with the WAM-IPE/IRI Ne(h) profile shape (Chapman layer) but allow the peak height and density to float
    5. Solve via constrained least squares or Kalman filter

- **Impact:** Separates E-layer and F-layer TEC contributions, which is critical because E-layer TEC (daytime only, ~5–10% of total) has different dynamics than F-layer TEC. Also enables detection of sporadic-E events.

#### 2. PHaRLAP 3D Ray Tracing Integration

The current `HFPropagationModel` uses numerical integration through 1D Chapman profiles, which assumes horizontal homogeneity. For long paths (BPM: ~10,000 km), the ionosphere varies significantly along the path.

- **Recommendation:** Integrate the **PHaRLAP** (Provision of High-frequency Raytracing Laboratory for Propagation studies) ray tracing engine:
    1. PHaRLAP accepts 3D ionospheric grids (from WAM-IPE) and computes exact ray paths
    2. Use it for the BPM paths (multi-hop, trans-Pacific) where 1D assumptions break down
    3. Keep the current numerical integration for shorter paths (WWV, CHU) where it's adequate
    4. PHaRLAP provides group delay, phase path, elevation angle, and bearing — all directly comparable to measurements

- **Impact:** Resolves the known issue where the propagation model underestimates multi-hop delays (predicts 10 ms, observed 200–450 ms for some BPM paths). Enables proper 2F/3F mode identification.

#### 3. Diversity Reception via Phase-Engine Integration

HF signals suffer from polarization fading (Faraday rotation) where the signal rotates as it passes through the ionosphere, causing deep nulls in carrier phase tracking.

- **Recommendation:** Integrate the existing **phase-engine** project (4× GPSDO-locked RX888 SDRs) with hf-timestd:
    1. Phase-engine provides coherent combination modes: MRC (Maximum Ratio Combining), adaptive nulling, and MVDR beamforming
    2. MRC fills deep fading nulls, enabling continuous carrier phase tracking through ionospheric scintillation events
    3. With 4 antennas: estimate angle-of-arrival (azimuth + elevation) for each signal, directly measuring the ionospheric reflection geometry
    4. Angle-of-arrival combined with ToA provides a direct geometric constraint on reflection height, independent of any ionospheric model

- **Impact:** Transforms the system from a single-antenna passive receiver into a **direction-finding oblique ionosonde**. Angle-of-arrival measurements provide the geometric constraint needed to separate propagation mode ambiguity without relying on model predictions.

#### 4. Improved VTEC Map Accuracy

The system produces per-minute IONEX VTEC maps from slant TEC measurements at 17 ionospheric pierce points (IPPs) spanning azimuths from Hawaii (SW) to China (W) to Canada (NE). Current accuracy is limited by the group-delay TEC noise floor.

- **Path forward:** Once propagation model error is reduced below ~0.5 ms (via Bayesian mode-constrained inversion or GPS-IONEX assimilation), the 17-IPP geometry provides genuine spatial resolution that single-frequency GPS receivers cannot achieve.

- **Impact:** Contributes original ionospheric data to the HamSCI community with multi-frequency, multi-azimuth geometric diversity.

---

### Summary: Development Roadmap

| Priority | Task | Status | Expected Impact |
|----------|------|--------|----------------|
| ✅ | Carrier-phase dTEC | **Operational** | 250K records/day, ~6 mTECU/min sensitivity |
| ✅ | Mode-constrained TEC inversion | **Operational** | Model-limited; noise floor characterised |
| ✅ | VTEC map generation | **Operational** | Per-minute IONEX; accuracy limited by TEC noise floor |
| ✅ | CHU systematic offset resolution | **Resolved** | 74 ms H3E sideband filter delay corrected; CHU restored as 3rd path |
| ✅ | dTEC multi-station visualization | **Operational** | Live at `/static/dtec.html`; multi-station overlay with SNR filtering |
| ✅ | Ionogram / ToF cluster visualization | **Operational** | Live at `/static/ionogram.html`; Griffin-style dual panel |
| **3** | Bayesian TEC with GPS-IONEX priors | Medium-term | Reduce model floor from 6.5 ms to ~0.5 ms |
| **4** | Multi-layer tomography | Medium-term | Separate E/F-layer contributions |
| **5** | PHaRLAP ray tracing | Long-term | Fix BPM multi-hop predictions |
| **6** | Phase-engine integration | Long-term | Angle-of-arrival, fading immunity |

---

### References

- **HamSCI Ionospheric Layer Height Estimation:** [YouTube](https://www.youtube.com/watch?v=F3a37yq_y2Q) — WWV Time of Flight and Doppler shift methodology
- **IRI-2020:** Bilitza et al., Advances in Space Research, 2022
- **WAM-IPE:** Fuller-Rowell et al., Space Weather, 2023
- **PHaRLAP:** Cervera & Harris, Radio Science, 2014
- **NIST SP 432:** NIST Time and Frequency Dissemination Services

---

**Source Code:** <https://github.com/HamSCI/hf-timestd>  
**License:** MIT  
**Author:** Michael James Hauan (AC0G)
