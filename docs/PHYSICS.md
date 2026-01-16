# HF-TimeStd: Ionospheric Physics Capabilities

**Purpose:** Document the ionospheric physics measurements and scientific capabilities of the HF Time Standard system  
**Audience:** Scientists, researchers, and amateur radio operators interested in ionospheric studies  
**System Version:** 5.3.4  
**Last Updated:** January 2026

---

## 1. Executive Summary

The HF-TimeStd system, while primarily designed for time transfer, is inherently an **ionospheric measurement instrument**. Every timing measurement encodes information about the ionosphere through which the signal propagated. This document describes:

1. **Currently implemented** physics measurements and their validation status
2. **Partially implemented** capabilities requiring further development
3. **Potential future** capabilities enabled by the existing infrastructure

The system monitors **17 broadcasts** across **9 frequencies** from **4 stations** (WWV, WWVH, CHU, BPM), providing continuous ionospheric sounding of multiple paths simultaneously.

---

## 2. The Ionosphere as a Measurement Medium

### 2.1 Why HF Time Signals Reveal Ionospheric Physics

HF radio waves (2-30 MHz) interact strongly with the ionosphere:

- **Refraction**: Signals bend and reflect at ionospheric layers
- **Dispersion**: Lower frequencies are delayed more than higher frequencies (∝ 1/f²)
- **Absorption**: D-layer absorbs energy, especially at lower frequencies
- **Doppler shift**: Ionospheric motion causes frequency shifts
- **Multipath**: Multiple propagation modes arrive at different times
- **Fading**: Interference between paths causes amplitude variations

Each of these effects is measurable and encodes ionospheric state information.

### 2.2 Measurement Geometry

| Station | Location | Distance (typical) | Azimuth (from central US) |
|---------|----------|-------------------|---------------------------|
| **WWV** | Fort Collins, CO | 500-1500 km | Variable |
| **WWVH** | Kauai, HI | 4000-5000 km | West |
| **CHU** | Ottawa, Canada | 1500-2500 km | Northeast |
| **BPM** | Pucheng, China | 10000-12000 km | Northwest |

This geometry provides ionospheric sampling across:
- Multiple azimuths (continental to transoceanic)
- Multiple path lengths (single-hop to multi-hop)
- Multiple reflection points (different ionospheric regions)

---

## 3. Currently Implemented Measurements

### 3.1 Total Electron Content (TEC) ✅

**Status:** Implemented, validation in progress

**Physics:** The ionospheric group delay follows the dispersion relation:

```
τ_iono = K × TEC / f²
where K = 40.3 m³/s² (ionospheric constant)
```

**Implementation:** `src/hf_timestd/core/tec_estimator.py`

The system performs least-squares fitting across multiple frequencies to solve for TEC:

```python
# Model: T_obs(f) = T_vacuum + (40.3 × TEC) / f²
# Solve for TEC and T_vacuum simultaneously
```

**Outputs:**
- `tec_tecu`: Total Electron Content in TECU (10¹⁶ electrons/m²)
- `t_vacuum_error_ms`: Residual timing error after TEC correction
- `confidence`: R² of the dispersion fit
- `group_delay_ms`: Per-frequency ionospheric delay

**Validation:**
- Comparison with GPS-derived IONEX maps (1-2 hour latency)
- Optional local GNSS-VTEC from dual-frequency receiver (real-time)
- Expected agreement: ±2-5 TECU under quiet conditions

**Scientific Value:**
- Continuous TEC monitoring along multiple paths
- Diurnal TEC variation tracking
- Storm-time TEC enhancement detection

### 3.2 Propagation Mode Identification ✅

**Status:** Implemented

**Physics:** HF signals can propagate via multiple ionospheric modes:

| Mode | Layer | Height | Typical Delay |
|------|-------|--------|---------------|
| **1E** | E-layer | 110 km | Shortest |
| **1F** | F-layer | 250-350 km | Medium |
| **2F** | F-layer (2-hop) | 250-350 km | Longer |
| **3F** | F-layer (3-hop) | 250-350 km | Longest |

**Implementation:** `src/hf_timestd/core/propagation_mode_solver.py`

The system calculates expected delays for each mode and matches against observed arrival times:

```python
# For each candidate mode:
path_length = calculate_hop_geometry(distance, layer_height, n_hops)
delay_ms = path_length / speed_of_light + ionospheric_delay
# Match to observed delay
```

**Outputs:**
- `propagation_mode`: Identified mode (1E, 1F, 2F, etc.)
- `n_hops`: Number of ionospheric reflections
- `confidence`: Match quality
- `multipath_detected`: Flag for multiple simultaneous modes

**Scientific Value:**
- Mode statistics by time of day, season, frequency
- MUF (Maximum Usable Frequency) estimation
- Propagation prediction validation

### 3.3 Ionospheric Layer Heights (hmF2, hmE) ✅

**Status:** Implemented via IRI-2020 integration

**Physics:** The F2-layer peak height (hmF2) varies with:
- Time of day (rises at night as ionization decays)
- Solar activity (higher during solar maximum)
- Season (higher in summer)
- Latitude (higher at equator)
- Geomagnetic activity (disturbed during storms)

**Implementation:** `src/hf_timestd/core/ionospheric_model.py`

Three-tier hierarchy:
1. **IRI-2020**: International Reference Ionosphere (±25-30 km accuracy)
2. **Parametric model**: Diurnal/solar/latitude model (±40-60 km)
3. **Static fallback**: Fixed heights (±80 km)

Plus **calibration layer** that learns from actual propagation measurements.

**Outputs:**
- `hmF2_km`: F2-layer peak height
- `hmE_km`: E-layer height
- `hmF1_km`: F1-layer height (daytime only)
- `model_tier`: Which model provided the estimate
- `hmF2_uncertainty_km`: Estimated uncertainty

**Scientific Value:**
- Real-time hmF2 tracking
- Model validation against actual propagation
- Storm-time layer height changes

### 3.4 Doppler Shift Measurement ✅

**Status:** Implemented

**Physics:** Ionospheric motion causes Doppler shifts:

```
Δf_D = 2 × v_iono × f / c
```

Where v_iono is the effective ionospheric velocity along the propagation path.

**Implementation:** `src/hf_timestd/core/wwvh_discrimination.py` (estimate_doppler_shift)

Phase tracking of timing tones across consecutive seconds:

```python
# Track phase progression
φ_k = phase at tick k
Δφ = φ_k - φ_{k-1}  # Unwrapped phase difference
Δf_D = Δφ / (2π × 1s)  # Doppler shift in Hz
```

**Outputs:**
- `doppler_hz`: Measured Doppler shift (±0.01-0.1 Hz precision)
- `doppler_std_hz`: Doppler spread (variability)
- `max_coherent_window_sec`: Maximum coherent integration time

**Scientific Value:**
- Ionospheric velocity estimation
- TID (Traveling Ionospheric Disturbance) detection
- Channel stability assessment

### 3.5 Multipath and Delay Spread ✅

**Status:** Implemented

**Physics:** When multiple propagation modes arrive simultaneously, they create:
- Delay spread (time spreading of the signal)
- Fading (constructive/destructive interference)
- Phase distortion

**Implementation:** `src/hf_timestd/core/advanced_signal_analysis.py`

```python
# Correlation peak width analysis
FWHM = time width where peak > peak_max/2
delay_spread_ms = FWHM  # Multipath time spreading
```

**Outputs:**
- `delay_spread_ms`: Multipath delay spread
- `multipath_detected`: Boolean flag
- `fading_variance`: Amplitude variation metric
- `phase_stability`: Phase coherence metric

**Scientific Value:**
- Channel characterization
- Mode mixing detection
- Propagation quality assessment

### 3.6 D-Layer Absorption ✅

**Status:** Implemented (via SNR analysis)

**Physics:** The D-layer (60-90 km) absorbs HF energy, especially:
- At lower frequencies (absorption ∝ 1/f²)
- During daytime (D-layer only exists when sun is up)
- During solar flares (Sudden Ionospheric Disturbances)

**Implementation:** Multi-frequency SNR comparison

**Outputs:**
- `snr_db`: Per-frequency signal-to-noise ratio
- Frequency-dependent absorption pattern
- Diurnal absorption variation

**Scientific Value:**
- D-layer absorption studies
- SID (Sudden Ionospheric Disturbance) detection
- Solar flare effects on HF propagation

---

## 4. Partially Implemented Capabilities

### 4.1 Sporadic-E Detection ⚠️

**Status:** Detection possible, characterization needs work

**Physics:** Sporadic-E (Es) is thin, dense ionization at E-layer heights (~100-120 km) that can reflect frequencies normally above the E-layer MUF.

**Current Capability:**
- SNR sudden increases at 10-15 MHz detectable
- Mode change to 1E identifiable
- Event timing and duration measurable

**Missing:**
- Automated Es event detection algorithm
- Critical frequency (foEs) estimation
- Es layer height determination

**Implementation Path:**
1. Add Es detection heuristics to mode solver
2. Track SNR anomalies at higher frequencies
3. Correlate with propagation mode changes

### 4.2 Scintillation Indices ⚠️

**Status:** Infrastructure exists, indices not computed

**Physics:** Ionospheric irregularities cause amplitude and phase scintillation:
- **S4**: Normalized amplitude variance (amplitude scintillation)
- **σ_φ**: Phase scintillation index

**Current Capability:**
- Amplitude time series available
- Phase tracking implemented
- Fading variance computed

**Missing:**
- S4 calculation from amplitude variance
- σ_φ calculation from detrended phase
- Scintillation event flagging

**Implementation Path:**
1. Add S4 calculation: `S4 = sqrt(var(I) / mean(I)²)`
2. Add σ_φ calculation from phase time series
3. Flag scintillation events (S4 > 0.3)

### 4.3 Traveling Ionospheric Disturbances (TIDs) ⚠️

**Status:** Doppler measured, TID detection not automated

**Physics:** TIDs are wave-like perturbations in the ionosphere with:
- Periods: 10 minutes to several hours
- Wavelengths: 100-1000 km
- Velocities: 50-300 m/s

**Current Capability:**
- Doppler shift time series available
- Multi-frequency observations
- Multi-path geometry

**Missing:**
- Coherent oscillation detection across frequencies
- Period/wavelength estimation
- TID event cataloging

**Implementation Path:**
1. FFT analysis of Doppler time series
2. Cross-correlation between frequencies/paths
3. Phase velocity estimation from multi-path delays

### 4.4 CHU FSK Time Code Decoding ⚠️

**Status:** Partially implemented

**Physics:** CHU transmits FSK-encoded time information including:
- UTC time (verified, not just relative)
- DUT1 correction (UT1-UTC)
- Leap second announcements
- TAI-UTC offset

**Current Capability:**
- FSK demodulation framework exists
- Bell 103 (2025/2225 Hz) detection

**Missing:**
- Complete BCD time code extraction
- DUT1 parsing
- Leap second warning extraction

**Implementation Path:**
1. Complete FSK demodulator in `advanced_signal_analysis.py`
2. Parse 10-byte CHU time code packet
3. Extract DUT1 and leap second fields

---

## 5. Potential Future Capabilities

### 5.1 Critical Frequency Estimation (foF2)

**Concept:** The highest frequency that can be reflected by the F2-layer at vertical incidence.

**Method:**
- Track which frequencies show F-layer propagation
- Estimate MUF from oblique propagation
- Back-calculate foF2 using secant law

**Requirements:**
- Reliable mode identification
- Multi-frequency coverage up to MUF
- Comparison with ionosonde data

### 5.2 Ionospheric Tilt Detection

**Concept:** Large-scale ionospheric gradients cause systematic TEC differences between paths.

**Method:**
- Compare TEC from WWV, WWVH, CHU, BPM paths
- Calculate TEC gradient vector
- Track gradient evolution

**Requirements:**
- Validated TEC from multiple paths
- Good azimuthal coverage
- Gradient calculation algorithm

### 5.3 Space Weather Correlation

**Concept:** Correlate ionospheric measurements with space weather indices.

**Method:**
- Ingest real-time Kp, Dst, F10.7 indices
- Correlate with TEC, absorption, Doppler
- Build predictive models

**Requirements:**
- Real-time space weather data feed
- Historical correlation analysis
- Prediction algorithm development

### 5.4 Luxembourg Effect Detection

**Concept:** Cross-modulation between powerful transmitters via ionospheric non-linearity.

**Method:**
- Detect intermodulation products (e.g., 500+600 Hz)
- Measure cross-modulation depth
- Correlate with D-layer density

**Requirements:**
- High-resolution spectrum analysis
- Intermodulation product detection
- D-layer model integration

### 5.5 Geomagnetic Storm Effects

**Concept:** Track ionospheric response to geomagnetic storms.

**Measurements:**
- TEC enhancement/depletion
- Layer height changes
- Absorption increases
- Propagation blackouts

**Requirements:**
- Storm event detection
- Multi-day tracking
- Comparison with magnetometer data

---

## 6. The WWV/WWVH Scientific Test Signal

### 6.1 Overview

At minutes :08 (WWV) and :44 (WWVH), a special **scientific modulation test signal** is transmitted. This 45-second signal was designed by the WWV/H Scientific Modulation Working Group specifically for ionospheric channel characterization.

**Implementation:** `src/hf_timestd/core/wwv_test_signal.py`

### 6.2 Signal Structure

| Time | Content | Scientific Purpose |
|------|---------|-------------------|
| 0-10s | Voice announcement | Synchronization |
| 10-12s | White noise #1 | Wideband coherence |
| 12-13s | Blank | - |
| 13-23s | Multi-tone (2,3,4,5 kHz) | Frequency selectivity |
| 23-24s | Blank | - |
| 24-32s | Chirp sequences | Delay spread via pulse compression |
| 32-34s | Blank | - |
| 34-36s | Single-cycle bursts | High-precision timing |
| 36-37s | Blank | - |
| 37-39s | White noise #2 | Transient detection |
| 39-42s | Blank | - |

### 6.3 Measurements from Test Signal

**Frequency Selectivity Score (FSS):**
```
FSS = 10×log10((P_2kHz + P_3kHz) / (P_4kHz + P_5kHz))
```
- Positive FSS = high-frequency attenuation (longer/more dispersive path)
- Provides path signature independent of overall signal strength

**Delay Spread:**
- Chirp pulse compression reveals multipath structure
- Higher resolution than BCD correlation method

**Scintillation:**
- Fading variance from multi-tone amplitude variations
- S4 index from 10-second tone segment

**Transient Detection:**
- Compare noise #1 (10-12s) with noise #2 (37-39s)
- Large difference indicates transient event (solar flare, etc.)

### 6.4 Current Implementation Status

| Feature | Status | Notes |
|---------|--------|-------|
| Test signal detection | ✅ Implemented | Template correlation |
| Multi-tone power | ✅ Implemented | Per-frequency power measurement |
| FSS calculation | ✅ Implemented | Frequency selectivity score |
| Delay spread | ⚠️ Partial | Basic measurement, needs refinement |
| Scintillation | ⚠️ Partial | Fading variance computed |
| Transient detection | ⚠️ Partial | Noise comparison implemented |

---

## 7. Optional GNSS-VTEC Enhancement

### 7.1 Local TEC Ground Truth

When a dual-frequency GNSS receiver (e.g., u-blox ZED-F9P) is available, the system can measure **local vertical TEC in real-time**.

**Implementation:** `timestd-vtec` service

**Advantages:**
- ~1 minute latency (vs 1-2 hours for IONEX)
- Point measurement at receiver location
- Tracks rapid TEC changes during storms

### 7.2 Integration with HF Measurements

The local GNSS-VTEC provides:

1. **Anchor point** for HF-derived TEC validation
2. **Real-time TEC** for propagation delay correction
3. **Storm detection** via rapid TEC changes

**Comparison:**

| Source | Latency | Spatial Resolution | Accuracy |
|--------|---------|-------------------|----------|
| IONEX maps | 1-2 hours | 2.5° × 5° grid | ±2-5 TECU |
| Local GNSS | ~1 minute | Point at receiver | ±1-2 TECU |
| HF dispersion | Real-time | Path-integrated | ±2-5 TECU |

### 7.3 Configuration

Enable via `timestd-config.toml`:

```toml
[gnss]
enabled = true
device = "/dev/ttyACM0"
baud_rate = 115200
```

---

## 8. Data Products for Science

### 8.1 L2 Timing Measurements

Each minute, per broadcast:
- `d_clock_ms`: Clock offset measurement
- `uncertainty_ms`: ISO GUM uncertainty
- `propagation_mode`: Identified mode
- `propagation_delay_ms`: Calculated delay
- `raw_arrival_time_ms`: Uncalibrated ToA (for TEC)

### 8.2 L3 Science Products

Aggregated across frequencies/stations:
- `tec_tecu`: Total Electron Content
- `tec_confidence`: Fit quality
- `group_delay_ms`: Per-frequency delays

### 8.3 Channel Characterization

Per broadcast:
- `doppler_hz`: Carrier Doppler shift
- `doppler_std_hz`: Doppler spread
- `delay_spread_ms`: Multipath delay spread
- `snr_db`: Signal-to-noise ratio
- `fading_variance`: Amplitude stability

### 8.4 Test Signal Products

At minutes :08/:44:
- `frequency_selectivity_db`: FSS score
- `tone_powers_db`: Per-tone power
- `scintillation_index`: S4 estimate
- `transient_detected`: Anomaly flag

---

## 9. Validation and Limitations

### 9.1 Validated Measurements

| Measurement | Validation Method | Status |
|-------------|------------------|--------|
| Carrier SNR | Compare with radiod | ✅ Validated |
| Doppler shift | Physical range check | ✅ Validated |
| Propagation delay | Great circle + model | ✅ Validated |
| Mode identification | Delay-based heuristics | ⚠️ ~80-90% accuracy |
| TEC | GPS IONEX comparison | ⚠️ In progress |

### 9.2 Fundamental Limitations

**Single Receiver:**
- No spatial resolution (cannot determine propagation direction)
- Cannot separate ionospheric layers
- Not suitable for tomography

**Model Dependence:**
- Propagation delay requires ionospheric model
- Mode identification uses heuristics
- TEC assumes single-layer ionosphere

**Ionospheric Variability:**
- ±1-3 ms timing uncertainty from ionosphere
- Mode mixing confuses identification
- Storm-time behavior unpredictable

### 9.3 Recommended Use Cases

✅ **Well-suited for:**
- D-layer absorption studies
- Propagation mode statistics
- Long-term TEC monitoring
- Sporadic-E event detection
- Diurnal ionospheric patterns

⚠️ **Use with caution:**
- Absolute TEC values (needs validation)
- TID detection (needs implementation)
- Real-time propagation prediction

❌ **Not suitable for:**
- Ionospheric tomography
- Layer height determination (no ionosonde)
- Sub-millisecond timing (ionospheric limit)

---

## 10. References

1. **Davies, K. (1990)**. "Ionospheric Radio." Peter Peregrinus Ltd.
2. **Bilitza, D. et al. (2022)**. "International Reference Ionosphere 2020." Earth, Planets and Space.
3. **ITU-R P.1239-3**. "ITU-R Reference Ionospheric Characteristics"
4. **NIST SP 432**. "NIST Time and Frequency Services"
5. **HamSCI WWV/H Scientific Modulation Working Group**. hamsci.org/wwv

---

## Appendix A: Key Equations

**Ionospheric Group Delay:**
```
τ_iono = 40.3 × TEC / f²  [seconds, TECU, Hz]
```

**Doppler Shift:**
```
Δf = 2 × v × f / c  [Hz, m/s, Hz, m/s]
```

**S4 Scintillation Index:**
```
S4 = sqrt(var(I) / mean(I)²)
```

**Frequency Selectivity Score:**
```
FSS = 10×log10((P_2kHz + P_3kHz) / (P_4kHz + P_5kHz))  [dB]
```

**Coherence Time:**
```
τ_c ≈ 1 / B_D  [seconds, Hz]
```

**Coherence Bandwidth:**
```
B_c ≈ 1 / τ_D  [Hz, seconds]
```

---

## Appendix B: Implementation Files

| Capability | Primary File |
|------------|--------------|
| TEC Estimation | `src/hf_timestd/core/tec_estimator.py` |
| Ionospheric Model | `src/hf_timestd/core/ionospheric_model.py` |
| Propagation Modes | `src/hf_timestd/core/propagation_mode_solver.py` |
| Physics Propagation | `src/hf_timestd/core/physics_propagation.py` |
| Doppler/Multipath | `src/hf_timestd/core/advanced_signal_analysis.py` |
| Test Signal | `src/hf_timestd/core/wwv_test_signal.py` |
| Discrimination | `src/hf_timestd/core/wwvh_discrimination.py` |
| Science Aggregator | `src/hf_timestd/core/science_aggregator.py` |

---

## Appendix C: Related Documentation

- **METROLOGY.md** — Time transfer methodology and uncertainty budgets
- **TECHNICAL_REFERENCE.md** — System architecture and configuration
- **SCIENTIFIC_CAPABILITIES.md** — Detailed feature validation status
- **CHANNEL_CHARACTERIZATION.md** — HF channel measurement details

---

**Source Code:** <https://github.com/mijahauan/hf-timestd>  
**License:** MIT  
**Author:** Michael James Hauan (AC0G)
