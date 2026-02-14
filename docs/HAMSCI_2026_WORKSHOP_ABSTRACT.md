# HamSCI 2026 Workshop — Presentation Abstract

**Author:** Michael James Hauan (AC0G)  
**Last Updated:** February 14, 2026  
**Status:** Accepted for presentation

---

## Project Description

**Project Title:** Multi-Static HF Time Signal Analysis for Ionospheric Sounding and TEC Estimation

### Executive Summary

This project implements a precision HF monitoring station that receives 17 distinct time signal broadcasts across nine frequencies from four major national standards stations: WWV (USA), WWVH (Hawaii), CHU (Canada), and BPM (China). A Leo Bodnar GPS-Disciplined Oscillator (GPSDO) phase-locks the receiving hardware (stability ≈ 1 × 10⁻¹²), eliminating local clock drift and ensuring that all measured timing residuals are attributable to the propagation path.

The system (`hf-timestd`, v5.4.1) is fully operational and runs 24/7 as eight independent systemd services on a single Linux host. It extracts high-precision Time of Arrival (ToA), carrier phase, and Doppler measurements from each broadcast, then compares these against predictions from a hierarchical ionospheric model stack: WAM-IPE numerical weather prediction, GIRO real-time ionosonde corrections, IRI-2020 climatology, and GPS-derived IONEX TEC maps. The residuals are inverted to solve for real-time ionospheric parameters — slant TEC along each ray path, effective reflection height, and propagation mode — effectively functioning as a passive, multi-frequency oblique ionosonde with 17 simultaneous sounding paths.

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

| Metric | Value |
|--------|-------|
| **Channels monitored** | 9 frequencies, 17 broadcasts |
| **Timing accuracy** | Fusion D_clock ≈ 1 ms mean discrepancy vs. GPS ground truth |
| **Chrony TSL offset** | 34–316 μs |
| **Bootstrap convergence** | ~2 minutes to LOCKED state |
| **Tick detection** | 50–57 ticks/min/station (CHU: 55/58, WWV: 57/57) |
| **CHU FSK decode** | 8/9 frames, confidence 1.00 |
| **Phase extraction** | Three-tier: audio, carrier, DC carrier (σ_φ improving) |
| **Carrier phase stability** | DC carrier 30% more stable than audio phase on unambiguous channels |

---

### Recommendations for Next Development Steps

The system described above is operational. The following recommendations target the transition from **timing-quality observation** to **high-precision TEC measurement**, optimizing how external models and direct measurements are combined.

#### 1. Constrained TEC Inversion with Model Priors (Highest Priority)

The current `TECEstimator` performs unconstrained 1/f² regression, which is sensitive to outliers and mode ambiguity (a 2F-hop measurement mixed with 1F-hop measurements corrupts the fit).

- **Recommendation:** Implement a **Bayesian TEC estimator** that uses the propagation model's mode predictions as informative priors. For each station, the model predicts which mode (1F, 2F, etc.) is active on each frequency. The estimator should:
    1. Assign each ToA measurement to its predicted propagation mode
    2. Subtract the mode-specific geometric path length (known from the model)
    3. Fit the residual dispersive delay (∝ 1/f²) to extract sTEC
    4. Weight by measurement SNR and model confidence
    5. Reject measurements whose residuals exceed 3σ (mode misidentification)

- **Impact:** Eliminates the dominant error source (mode mixing) and enables TEC estimation even when only 2 frequencies are available per station.

#### 2. Differential TEC from Carrier Phase (Sub-TECU Precision)

Group delay TEC (from ToA) has ~ms precision, corresponding to ~1–5 TECU uncertainty. Carrier phase is 1000× more precise but ambiguous (unknown integer cycles).

- **Recommendation:** Implement **carrier-phase differential TEC (dTEC)**:
    1. Use the existing three-tier phase extraction (already producing ~55 phase measurements/minute/station)
    2. Compute phase rate of change (already implemented as Doppler)
    3. Convert Doppler to dTEC/dt via: `dTEC/dt = -f² · Δf_D / (40.3 · f_carrier)`
    4. Integrate dTEC/dt over time, anchored to the group-delay TEC absolute value
    5. This gives **sub-TECU temporal resolution** of ionospheric dynamics while the group delay provides the absolute calibration

- **Impact:** Resolves Traveling Ionospheric Disturbances (TIDs) with 15–60 minute periods and Medium-Scale TIDs (MSTIDs) that are invisible in group-delay TEC.

#### 3. Cross-Path Tomographic Constraints

With 17 simultaneous ray paths through the ionosphere at different frequencies and geometries, the system is over-determined for a single-layer TEC model.

- **Recommendation:** Implement a **multi-layer ionospheric tomography** approach:
    1. Divide the ionosphere into E-layer (90–150 km) and F-layer (150–500 km) shells
    2. Each ray path's sTEC is the sum of contributions from shells it traverses
    3. The 17 paths at different elevation angles provide geometric diversity to separate E and F contributions
    4. Constrain with the WAM-IPE/IRI Ne(h) profile shape (Chapman layer) but allow the peak height and density to float
    5. Solve via constrained least squares or Kalman filter

- **Impact:** Separates E-layer and F-layer TEC contributions, which is critical because E-layer TEC (daytime only, ~5–10% of total) has different dynamics than F-layer TEC. Also enables detection of sporadic-E events.

#### 4. PHaRLAP 3D Ray Tracing Integration

The current `HFPropagationModel` uses numerical integration through 1D Chapman profiles, which assumes horizontal homogeneity. For long paths (BPM: ~10,000 km), the ionosphere varies significantly along the path.

- **Recommendation:** Integrate the **PHaRLAP** (Provision of High-frequency Raytracing Laboratory for Propagation studies) ray tracing engine:
    1. PHaRLAP accepts 3D ionospheric grids (from WAM-IPE) and computes exact ray paths
    2. Use it for the BPM paths (multi-hop, trans-Pacific) where 1D assumptions break down
    3. Keep the current numerical integration for shorter paths (WWV, CHU) where it's adequate
    4. PHaRLAP provides group delay, phase path, elevation angle, and bearing — all directly comparable to measurements

- **Impact:** Resolves the known issue where the propagation model underestimates multi-hop delays (predicts 10 ms, observed 200–450 ms for some BPM paths). Enables proper 2F/3F mode identification.

#### 5. Diversity Reception via Phase-Engine Integration

HF signals suffer from polarization fading (Faraday rotation) where the signal rotates as it passes through the ionosphere, causing deep nulls in carrier phase tracking.

- **Recommendation:** Integrate the existing **phase-engine** project (4× GPSDO-locked RX888 SDRs) with hf-timestd:
    1. Phase-engine provides coherent combination modes: MRC (Maximum Ratio Combining), adaptive nulling, and MVDR beamforming
    2. MRC fills deep fading nulls, enabling continuous carrier phase tracking through ionospheric scintillation events
    3. With 4 antennas: estimate angle-of-arrival (azimuth + elevation) for each signal, directly measuring the ionospheric reflection geometry
    4. Angle-of-arrival combined with ToA provides a direct geometric constraint on reflection height, independent of any ionospheric model

- **Impact:** Transforms the system from a single-antenna passive receiver into a **direction-finding oblique ionosonde**. Angle-of-arrival measurements provide the geometric constraint needed to separate propagation mode ambiguity without relying on model predictions.

#### 6. Real-Time VTEC Map Generation

The system currently consumes external VTEC maps (IONEX) but does not produce its own.

- **Recommendation:** Generate **station-local VTEC maps** from the 17 slant TEC measurements:
    1. Convert each sTEC to vTEC using the mapping function: `vTEC = sTEC × cos(χ)` where χ is the zenith angle at the ionospheric pierce point (IPP)
    2. The 17 IPPs span a geographic area from the receiver to each transmitter's midpoint
    3. Fit a 2D polynomial or spherical harmonic surface to the vTEC values at the IPPs
    4. Publish as IONEX-format files for community use

- **Impact:** Contributes original ionospheric data to the HamSCI community. The multi-frequency, multi-azimuth geometry provides spatial resolution that single-frequency GPS receivers cannot achieve.

---

### Summary: Development Roadmap

| Priority | Task | Prerequisite | Expected Impact |
|----------|------|-------------|-----------------|
| **1** | Bayesian TEC with mode priors | Current propagation model | Eliminate mode-mixing errors |
| **2** | Carrier-phase dTEC | Current phase extraction | Sub-TECU temporal resolution |
| **3** | Multi-layer tomography | Tasks 1 + 2 | Separate E/F-layer contributions |
| **4** | PHaRLAP ray tracing | WAM-IPE 3D grids | Fix BPM multi-hop predictions |
| **5** | Phase-engine integration | phase-engine hardware | Angle-of-arrival, fading immunity |
| **6** | VTEC map generation | Task 1 | Community data product |

---

### References

- **HamSCI Ionospheric Layer Height Estimation:** [YouTube](https://www.youtube.com/watch?v=F3a37yq_y2Q) — WWV Time of Flight and Doppler shift methodology
- **IRI-2020:** Bilitza et al., Advances in Space Research, 2022
- **WAM-IPE:** Fuller-Rowell et al., Space Weather, 2023
- **PHaRLAP:** Cervera & Harris, Radio Science, 2014
- **NIST SP 432:** NIST Time and Frequency Dissemination Services

---

**Source Code:** <https://github.com/mijahauan/hf-timestd>  
**License:** MIT  
**Author:** Michael James Hauan (AC0G)
