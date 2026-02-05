# HF-TimeStd: Ionospheric Physics Capabilities

**Purpose:** Document the ionospheric physics measurements and scientific capabilities of the HF Time Standard system  
**Audience:** Scientists, researchers, and amateur radio operators interested in ionospheric studies  
**System Version:** 6.5.0 (Physics-Based Validation + TEC Feedback)  
**Last Updated:** 2026-02-04

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

**Status:** Fully implemented with GNSS VTEC correction (v6.1)

**Physics:** The ionospheric group delay follows the dispersion relation:

```
τ_iono = K × TEC / f²
where K = 40.3 m³/s² (ionospheric constant)
      τ_ms = 1.344 × TEC_TECU / f_MHz² (delay in milliseconds)
```

**Implementation:**
- HF-derived TEC: `src/hf_timestd/core/tec_estimator.py`
- GNSS VTEC: `src/hf_timestd/core/gnss_tec.py`
- Ionospheric correction: `src/hf_timestd/core/multi_broadcast_fusion.py`

**TEC Source Hierarchy (v6.1):**

| Priority | Source | Method | Accuracy | Latency |
|----------|--------|--------|----------|---------|
| 1 | Local GNSS VTEC | Dual-frequency GPS (ZED-F9P) | ±1-2 TECU | ~1s |
| 2 | IONEX maps | Global GPS network (NASA CDDIS) | ±2-5 TECU | 2 hours |
| 3 | IRI-2020 | Climatological model | ±5-10 TECU | N/A |
| 4 | Parametric | Diurnal/solar model | ±10-20 TECU | N/A |

**GNSS VTEC Ionospheric Correction (NEW in v6.1):**

When local GNSS VTEC is available, the system applies a direct correction to D_clock:

```
D_clock_corrected = D_clock + Δiono
Δiono = 1.344 × (TEC_model - TEC_gnss) × n_hops × obliquity / f²
```

This correction removes the systematic bias from using modeled TEC instead of measured TEC.

**Outputs:**
- `tec_tecu`: Total Electron Content in TECU (10¹⁶ electrons/m²)
- `t_vacuum_error_ms`: Residual timing error after TEC correction
- `confidence`: R² of the dispersion fit
- `group_delay_ms`: Per-frequency ionospheric delay
- `gnss_vtec_tecu`: Local GNSS-measured vertical TEC

**Validation:**
- Cross-validation between GNSS VTEC and HF-derived TEC
- Comparison with GPS-derived IONEX maps (1-2 hour latency)
- Expected agreement: ±2-5 TECU under quiet conditions (validated 2026-01-16)

**Scientific Value:**
- Continuous TEC monitoring along multiple paths
- Diurnal TEC variation tracking
- Storm-time TEC enhancement detection
- Real-time ionospheric correction for timing

<!-- LIVE: tec-summary -->

<!-- LIVE: ionospheric-conditions -->

### 3.2 Propagation Mode Identification ✅

**Status:** Implemented (Enhanced in v6.2)

**Physics:** HF signals can propagate via multiple ionospheric modes:

| Mode | Layer | Height | Typical Delay | Timing Uncertainty |
|------|-------|--------|---------------|-------------------|
| **GW** | Ground Wave | N/A | ~3.3 ms/1000km | ±0.1 ms |
| **1E** | E-layer | 110 km | Shortest sky | ±1.0 ms |
| **1F2** | F2-layer | 250-350 km | Medium | ±0.5 ms |
| **2F2** | F2-layer (2-hop) | 250-350 km | Longer | ±1.5 ms |
| **3F2** | F2-layer (3-hop) | 250-350 km | Longest | ±2.5 ms |

**Implementation:** `src/hf_timestd/core/propagation_mode_solver.py`

#### 3.2.1 Mode Identification Algorithm

The system identifies propagation modes through a multi-step process:

**Step 1: Calculate Candidate Mode Delays**

For each candidate mode, compute the expected propagation delay:

```python
# Geometric path length calculation
for n_hops in [1, 2, 3]:
    # Virtual reflection height from IRI-2020 or parametric model
    h_reflection = get_layer_height(frequency, time, location)
    
    # Ray geometry: signal reflects off ionosphere n_hops times
    # Using spherical Earth geometry
    earth_radius = 6371 km
    path_length = calculate_hop_path(
        tx_lat, tx_lon,
        rx_lat, rx_lon,
        h_reflection,
        n_hops
    )
    
    # Total delay = geometric + ionospheric
    delay_geometric_ms = path_length / c * 1000
    delay_ionospheric_ms = K * TEC / f² * n_hops
    delay_total_ms = delay_geometric_ms + delay_ionospheric_ms
```

**Step 2: Match Observed Delay to Candidates**

Compare the measured arrival time against each candidate:

```python
# Observed delay from tone detection
observed_delay_ms = T_arrival - T_emission

# Find best-matching mode
for mode, expected_delay in candidates.items():
    residual = abs(observed_delay_ms - expected_delay)
    if residual < threshold:
        matched_modes.append((mode, residual))

# Select mode with smallest residual
best_mode = min(matched_modes, key=lambda x: x[1])
```

**Step 3: Validate with Multi-Frequency Consistency**

For stations with multiple frequencies, validate mode identification:

```python
# Same station, different frequencies should show 1/f² dispersion
# within the same mode
for freq_pair in frequency_pairs:
    delay_diff = delay[f1] - delay[f2]
    expected_diff = K * TEC * (1/f1² - 1/f2²)
    
    if abs(delay_diff - expected_diff) < tolerance:
        mode_validated = True
    else:
        # Mode mixing or misidentification
        flag_for_review()
```

#### 3.2.2 Mode Uncertainty by Type

| Mode | Physical Basis | Uncertainty Source |
|------|----------------|-------------------|
| **GW** | Direct surface wave | Path loss limits range |
| **1F2** | Single F-layer reflection | Layer height uncertainty |
| **2F2** | Double F-layer reflection | Cumulative height uncertainty |
| **1E** | E-layer reflection | E-layer variability |
| **Mixed** | Multiple simultaneous modes | Multipath interference |

#### 3.2.3 v6.2 Enhancement: Mode Tracking in Fusion

The fusion service now records propagation modes for each measurement:

```python
# In FusedResult (v6.2):
propagation_modes_used: str      # e.g., "1F2,2F2,GW"
dominant_propagation_mode: str   # Most common mode
```

This enables:
- **Mode statistics** by time of day, season, frequency
- **Uncertainty weighting** based on mode reliability
- **Science products** for propagation research

**Outputs:**
- `propagation_mode`: Identified mode (GW, 1E, 1F2, 2F2, etc.)
- `n_hops`: Number of ionospheric reflections
- `confidence`: Match quality (0-1)
- `multipath_detected`: Flag for multiple simultaneous modes
- `propagation_modes_used`: All modes in fusion window (v6.2)
- `dominant_propagation_mode`: Most common mode (v6.2)

**Scientific Value:**
- Mode statistics by time of day, season, frequency
- MUF (Maximum Usable Frequency) estimation
- Propagation prediction validation
- Ionospheric research (mode transitions indicate layer changes)

<!-- LIVE: propagation-paths -->

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

**Status:** Implemented (Enhanced in v6.2)

**Physics:** Ionospheric motion causes Doppler shifts:

```
Δf_D = 2 × v_iono × f / c
```

Where v_iono is the effective ionospheric velocity along the propagation path.

**Implementation:** 
- `src/hf_timestd/core/wwvh_discrimination.py` (estimate_doppler_shift)
- `src/hf_timestd/core/tone_detector.py` (_estimate_doppler_from_phase_slope) — **NEW in v6.2**

**v6.2 Enhancement: Phase-Based Doppler Estimation**

The tone detector now estimates Doppler directly from the complex correlation phase slope:

```python
# Extract phase around correlation peak
phase_window = corr_phase[peak_idx - N : peak_idx + N]
# Unwrap phase and fit linear slope
phase_unwrapped = np.unwrap(phase_window)
slope = np.polyfit(t, phase_unwrapped, 1)[0]  # rad/sample
doppler_hz = slope × sample_rate / (2π)
```

**Doppler Timing Correction (v6.2):**

Doppler shift causes systematic timing bias that is now corrected:

```
Δt_bias ≈ (f_doppler / f_tone) × (T_tone / 2)
```

For typical HF Doppler (±1-5 Hz) on 1000 Hz tone over 800 ms:
- Δt_bias ≈ (5 / 1000) × 0.4 = **2 ms** (worst case)

**Outputs:**
- `doppler_hz`: Measured Doppler shift (±0.01-0.1 Hz precision)
- `doppler_std_hz`: Doppler spread (variability)
- `max_coherent_window_sec`: Maximum coherent integration time
- `timing_error_ms`: Now Doppler-corrected (v6.2)

**Scientific Value:**
- Ionospheric velocity estimation
- TID (Traveling Ionospheric Disturbance) detection
- Channel stability assessment
- **Improved timing accuracy** via Doppler correction

### 3.5 Multipath and Delay Spread ✅

**Status:** Implemented (Enhanced in v6.2)

**Physics:** When multiple propagation modes arrive simultaneously, they create:
- Delay spread (time spreading of the signal)
- Fading (constructive/destructive interference)
- Phase distortion

**Implementation:** 
- `src/hf_timestd/core/advanced_signal_analysis.py`
- `src/hf_timestd/core/tone_detector.py` (_detect_multipath_from_correlation) — **NEW in v6.2**

**v6.2 Enhancement: Integrated Multipath Detection**

The tone detector now performs multipath detection as part of the main detection pipeline:

```python
# Three-indicator multipath detection:
# 1. Peak width analysis (broadening indicates multipath)
peak_width = correlation_fwhm_samples / sample_rate * 1000  # ms
is_broadened = peak_width > expected_width * 1.5

# 2. Secondary peak detection
secondary_peaks = find_peaks(magnitude, height=0.3 * peak_max)
has_secondary = len(secondary_peaks) > 1

# 3. Phase stability around peak
phase_std = np.std(phase[peak_idx - N : peak_idx + N])
phase_unstable = phase_std > 0.5  # radians
```

**Uncertainty Inflation (v6.2):**

When multipath is detected, the timing uncertainty is inflated:

```python
if is_multipath and delay_spread_ms > 0:
    multipath_uncertainty_ms = delay_spread_ms / 2.0
    timing_uncertainty_ms = sqrt(
        timing_uncertainty_ms² + multipath_uncertainty_ms²
    )
```

**Outputs:**
- `delay_spread_ms`: Multipath delay spread
- `multipath_detected`: Boolean flag (now in ToneDetectionResult)
- `multipath_delay_spread_ms`: Delay spread in ms (now in ToneDetectionResult)
- `multipath_quality`: 0-1 metric, higher = cleaner path (now in ToneDetectionResult)
- `fading_variance`: Amplitude variation metric
- `phase_stability`: Phase coherence metric

**Scientific Value:**
- Channel characterization
- Mode mixing detection
- Propagation quality assessment
- **Rigorous uncertainty propagation** for timing measurements

<!-- LIVE: scintillation-status -->

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

### 4.1 Sporadic-E Detection ✅

**Status:** Implemented (2026-01-16)

**Physics:** Sporadic-E (Es) is thin, dense ionization at E-layer heights (~100-120 km) that can reflect frequencies normally above the E-layer MUF.

**Implementation:** `src/hf_timestd/core/propagation_mode_solver.py` (`SporadicEDetector`)

**Capabilities:**
- ✅ Automated Es event detection algorithm
- ✅ SNR anomaly detection (sudden increases at 10/15 MHz)
- ✅ Mode change detection (F→E transitions)
- ✅ Critical frequency (foEs) estimation
- ✅ Multi-frequency confirmation
- ✅ Confidence scoring from multiple evidence sources

**Outputs:**
- `detected`: Boolean Es event flag
- `confidence`: Detection confidence (0-1)
- `estimated_foEs_mhz`: Critical frequency estimate
- `snr_increase_db`: SNR jump at detection
- `mode_changed_to_e`: True if mode switched to 1E
- `detection_method`: 'snr_anomaly', 'mode_change', or 'combined'

**Scientific Value:**
- Real-time Es event detection and cataloging
- foEs estimation for propagation prediction
- Correlation with geomagnetic activity

### 4.2 Scintillation Indices ✅

**Status:** Implemented (2026-01-16)

**Physics:** Ionospheric irregularities cause amplitude and phase scintillation:
- **S4**: Normalized amplitude variance (amplitude scintillation)
- **σ_φ**: Phase scintillation index (Doppler-detrended)

**Implementation:** `src/hf_timestd/core/advanced_signal_analysis.py` (`calculate_scintillation_indices`)

**Capabilities:**
- ✅ S4 calculation: `S4 = sqrt(var(I) / mean(I)²)`
- ✅ σ_φ calculation with Doppler trend removal
- ✅ Severity classification (weak/moderate/strong)
- ✅ Scintillation event flagging
- ✅ Confidence scoring

**Outputs:**
- `s4_index`: Amplitude scintillation index (0-1+)
- `s4_severity`: 'weak' (<0.3), 'moderate' (0.3-0.6), 'strong' (≥0.6)
- `sigma_phi_rad`: Phase scintillation in radians
- `sigma_phi_severity`: 'weak' (<0.2), 'moderate' (0.2-0.5), 'strong' (≥0.5)
- `scintillation_event`: Boolean event flag
- `doppler_removed_hz`: Doppler trend that was removed

**Scientific Value:**
- Standard ITU-R P.531 scintillation metrics
- Real-time ionospheric irregularity detection
- Channel quality assessment for timing

### 4.3 Traveling Ionospheric Disturbances (TIDs) ✅

**Status:** Implemented (2026-02-04)

**Physics:** TIDs are wave-like perturbations in the ionosphere with:
- Periods: 10 minutes to several hours
- Wavelengths: 100-1000 km
- Velocities: 50-300 m/s (medium-scale) or 300-1000 m/s (large-scale)

**Implementation:** `src/hf_timestd/core/tid_detector.py`

**Detection Principle:**
1. Each HF path (receiver → station) samples the ionosphere at different points
2. A TID passing through creates timing perturbations that:
   - Appear at different times on different paths (phase delay)
   - Have similar amplitude and period on all paths
   - Show consistent propagation direction
3. Cross-correlation of timing residuals reveals TID signatures

**Capabilities:**
- ✅ Rolling buffers of timing residuals per path
- ✅ Cross-correlation between path pairs
- ✅ TID velocity estimation from path geometry and lag
- ✅ TID direction estimation from leading/lagging paths
- ✅ Period estimation from autocorrelation
- ✅ Confidence scoring

**Outputs:**
- `period_minutes`: Dominant oscillation period
- `amplitude_ms`: Timing variation amplitude
- `velocity_m_s`: Estimated TID velocity
- `direction_deg`: Propagation azimuth
- `correlation_coefficient`: Detection confidence
- `leading_path`: Path that sees TID first
- `lag_minutes`: Time delay between paths

**Scientific Value:**
- Real-time TID detection and cataloging
- TID velocity and direction estimation
- Correlation with geomagnetic activity
- Medium-scale vs large-scale TID classification

### 4.4 CHU FSK Time Code Decoding ✅

**Status:** Implemented and integrated (Enhanced in v6.2)

**Physics:** CHU transmits FSK-encoded time information including:
- UTC time (verified, not just relative)
- DUT1 correction (UT1-UTC)
- Leap second announcements
- TAI-UTC offset

**Implementation:** `src/hf_timestd/core/chu_fsk_decoder.py`

**Capabilities:**
- ✅ FSK demodulation (Hilbert transform method)
- ✅ BCD time code extraction (Frame A: time of day)
- ✅ DUT1, year, TAI-UTC parsing (Frame B: auxiliary data)
- ✅ Parity checking and error detection
- ✅ Multi-second consensus validation
- ✅ Integration with analytics pipeline
- ✅ **High-precision tick timing** (v6.2) — NEW

**v6.2 Enhancement: Dual Timing References**

CHU provides two timing references with different precision:

| Reference | Method | Precision | Implementation |
|-----------|--------|-----------|----------------|
| **FSK Boundary** | Mark-to-silence transition at 500ms | ~1-2 ms | `decode_second()` |
| **1000 Hz Tick** | Edge detection on 10-cycle tick | ~0.05 ms | `detect_tick_onset()` — **NEW** |

The tick at the start of each second provides much higher timing precision than the FSK data boundary:

```python
def detect_tick_onset(audio, expected_sample, search_window_ms=20.0):
    # Bandpass filter around 1000 Hz
    # Compute energy envelope
    # Find rising edge with sub-sample interpolation
    return tick_onset_sample, timing_offset_ms, confidence
```

**Outputs:**
- `decoded_day`, `decoded_hour`, `decoded_minute`: Verified UTC time
- `dut1_seconds`: UT1-UTC correction
- `tai_utc`: TAI-UTC offset (leap second count)
- `year`: Gregorian year
- `timing_offset_ms`: FSK boundary timing (secondary, ~1-2 ms)
- `tick_timing_offset_ms`: 1000 Hz tick timing (primary, ~0.05 ms) — **NEW in v6.2**
- `tick_timing_count`: Number of valid tick measurements — **NEW in v6.2**
- `decode_confidence`: Frame decode quality

**Scientific Value:**
- Verified UTC time (not just relative timing)
- DUT1 correction for UT1-UTC
- Leap second announcements
- TAI-UTC offset tracking
- **High-precision timing** from 1000 Hz tick (v6.2)

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
| Delay spread | ✅ Implemented | Chirp matched filter with -3dB width |
| Scintillation (S4) | ✅ Implemented | From multi-tone amplitude variance |
| Transient detection | ✅ Implemented | Noise #1 vs #2 comparison |

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
| Scintillation (S4, σ_φ) | `src/hf_timestd/core/advanced_signal_analysis.py` |
| Sporadic-E Detection | `src/hf_timestd/core/propagation_mode_solver.py` |
| CHU FSK Decoding | `src/hf_timestd/core/chu_fsk_decoder.py` |
| Test Signal | `src/hf_timestd/core/wwv_test_signal.py` |
| Discrimination | `src/hf_timestd/core/wwvh_discrimination.py` |
| Science Aggregator | `src/hf_timestd/core/science_aggregator.py` |
| **TID Detection** | `src/hf_timestd/core/tid_detector.py` |
| **Physics Validation** | `src/hf_timestd/core/arrival_pattern_matrix.py` |
| **Multi-Constraint Validation** | `src/hf_timestd/core/timing_consistency_validator.py` |

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
