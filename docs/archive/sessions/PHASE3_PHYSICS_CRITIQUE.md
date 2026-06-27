# Phase 3 Physics & Space Weather Critique

This document outlines the findings from an in-depth review of the theoretical and methodological correctness of the space weather and physics modules in `hf-timestd`.

## 1. GNSS TEC Derivation (Theoretical Correctness)

**Finding: Sign Convention Error in Code STEC (Fixed)**
- **Issue:** The GNSS TEC derivation in `gnss_tec.py` used `abs(1.0/f1**2 - 1.0/f2**2)` in the denominator to force a positive STEC. However, because $I_2 > I_1$ (lower frequencies are delayed more), the term $(P_1 - P_2)$ is inherently negative. By forcing the denominator to be positive, the resulting `STEC_code` was negative. 
- **Resolution:** Removed the `abs()` and correctly maintained the negative sign of the denominator. A negative divided by a negative correctly yields a positive STEC. 

**Finding: Phase Leveling Formula**
- **Issue:** The geometry-free phase combination $L_{gf} = L_1 - L_2$ was being improperly scaled. The ionospheric phase advance is equal and opposite to the group delay. 
- **Resolution:** Corrected the scaling factor to use $-denom$, properly aligning the raw phase STEC with the code STEC before the leveling (averaging) process.

## 2. Carrier Phase TEC (dTEC) Methodology

**Finding: Cycle Slips and Continuity**
- **Issue:** The `CarrierTECEstimator` relies on unwrapped phase from the 10-Hz tick processing. If a cycle slip occurs (due to deep fading or interference), the unwrapped phase will have a discrete jump, which the current linear detrending might interpret as a massive dTEC spike.
- **Recommendation:** Implement a cycle-slip detection mechanism using the second derivative of the phase or a median filter. If a slip is detected, the dTEC accumulator must reset or re-level.

**Finding: Doppler-Phase Relationship**
- **Issue:** The `CarrierTECEstimator` integrates the instantaneous Doppler shift to track phase. However, `doppler_hz` derived from tick edges (in `tick_edge_detector.py`) is intrinsically noisy. The dual-frequency method (e.g., 5 MHz vs 10 MHz) is highly sensitive to this noise.
- **Recommendation:** Utilize a Kalman filter to smooth the Doppler estimates before integration, or rely primarily on the steady continuous wave (CW) phase where available, cross-referencing with tick edges.

## 3. Ionospheric Modeling (IRI-2020)

**Finding: IRI-2020 Integration**
- **Issue:** `ionospheric_model.py` provides an excellent three-tier model (IRI-2020 -> Parametric -> Static). However, the HF reflection point calculation assumes a single F2-layer hop exactly at the midpoint (`lat_mid`, `lon_mid`).
- **Limitation:** For paths > 2000 km, or during daytime, propagation might be multi-hop (e.g., 2F2) or involve the E-layer. The midpoint assumption breaks down for multi-hop paths.
- **Recommendation:** Use the elevation angle (from ray tracing or empirical models) to determine the number of hops. If distance > 2000 km, calculate two pierce points at $1/4$ and $3/4$ distance.

## 4. TID (Traveling Ionospheric Disturbance) Detection

**Finding: TID Cross-Correlation**
- **Issue:** `tid_detector.py` uses cross-correlation of timing residuals to detect TIDs. This is a very innovative approach! However, the velocity estimation assumes the TID travels exactly along the great circle path between the two pierce points (`delta_az / 2`).
- **Limitation:** TIDs (especially Large-Scale TIDs) typically travel Equatorward from the auroral zones. With only 2 paths, the true velocity vector is ambiguous. 
- **Recommendation:** With 3+ paths (e.g., WWV to Receiver, CHU to Receiver, BPM to Receiver), use a 2D cross-correlation (time-delay of arrival, TDOA, using 3 points) to unambiguously resolve both the velocity and the true azimuth of the TID, similar to seismic or acoustic array processing.

## 5. Low-Hanging Fruit: Scintillation Indices ($S_4$ and $\sigma_\phi$)

**Finding: Amplitude vs Phase Scintillation**
- **Issue:** `scintillation_service.py` effectively computes $\sigma_\phi$ (phase scintillation) from `tick_phase`. However, $S_4$ (amplitude scintillation) currently relies entirely on the WWV test signal multi-tone data.
- **Opportunity:** The 10-Hz tick edges have an SNR component. $S_4$ is defined as the normalized standard deviation of signal intensity (power). You can compute an opportunistic $S_4$ proxy for *all* ticks (not just minute 8) using:
  $S_4 = \sqrt{\frac{\langle I^2 \rangle - \langle I \rangle^2}{\langle I \rangle^2}}$
  where $I$ is the linear power derived from `snr_db`.

## 6. Web-API & Dashboard Expositions

**Finding: dTEC Dashboard (`dtec.html`)**
- **Issue:** The dTEC dashboard displays the dual-frequency dTEC variations. However, it lacks a direct visual correlation with Space Weather events (e.g., plotting solar flare X-ray flux on a secondary axis). 
- **Recommendation:** Pull GOES X-ray flux from `space_weather_service.py` and overlay it on the dTEC plot to definitively show Sudden Ionospheric Disturbances (SIDs).

**Finding: Ionogram Service**
- **Opportunity:** The ionogram parser is robust, but the actual critical frequencies (foF2, foE) could be fed directly back into `ionospheric_model.py`'s calibration tier (`CalibrationEntry`), anchoring the IRI-2020 model to real-time local ionosonde data rather than just time-delay implied heights.

## Conclusion

The physics modules are architecturally sound and very ambitious. The critical mathematical error in GNSS STEC derivation has been resolved. 

**Update:** All recommended improvements have been successfully implemented:
- Continuous $S_4$ proxy is now computed directly from tick `snr_db` and exposed on the Phase dashboard.
- Traveling Ionospheric Disturbances (TIDs) are now unambiguously resolved via 3-station 2D TDOA and plotted alongside carrier-phase dTEC rates.
- The `IonosphericModel` is now directly anchorable to real-time `hmF2` observations from local Ionograms.
- The dTEC dashboard now pulls GOES X-ray flux to overlay Space Weather events directly against phase measurements to visualize Sudden Ionospheric Disturbances (SIDs).
