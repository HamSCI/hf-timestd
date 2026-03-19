# HF-TimeStd: Ionospheric Physics Capabilities

**Purpose:** Document the ionospheric physics measurements and scientific capabilities of the HF Time Standard system  
**Scope:** This document covers the *science* — what measurements mean physically and how they are validated. For *design decisions* (why the system is built this way), see `docs/ARCHITECTURE.md`. For *algorithms and data formats*, see `docs/TECHNICAL_REFERENCE.md`.  
**Audience:** Scientists, researchers, and amateur radio operators interested in ionospheric studies  
**System Version:** 6.11.0 (Unified Measurement Path + Adaptive Windowing + Multipath-Aware Uncertainty)  
**Last Updated:** March 19, 2026

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

### 3.1 Ionospheric Electron Content: dTEC and TEC ✅

**Status:** Carrier-phase dTEC fully operational (v6.8); group-delay TEC available as validation product

The system produces two distinct TEC-related products with very different characteristics:

#### 3.1.1 Carrier-Phase Differential TEC (dTEC) — Primary Product

**Sensitivity:** ~6 mTECU/min (carrier-phase precision)
**Volume:** ~250,000 records/day across all station-channels
**Anchoring:** GNSS VTEC (±1 TECU) when available; group-delay TEC fallback

Carrier-phase dTEC measures the *rate of change* of TEC along each HF path by tracking the carrier phase progression across successive minutes. The integrated dTEC is inherently **relative** (unknown DC offset) until anchored by an external absolute TEC source.

**Anchor source priority (v6.8):**

| Priority | Source | Method | Accuracy | anchor_status |
|----------|--------|--------|----------|---------------|
| 1 | **Local GNSS VTEC** | ZED-F9P overhead VTEC | ±1 TECU | `ANCHORED_GNSS` |
| 2 | Group-delay TEC | HF 1/f² fit | Noise-dominated (see below) | `ANCHORED_GROUP_DELAY` |
| 3 | None | — | — | `NO_ANCHOR` |

**Implementation:**
- Carrier-phase dTEC: `src/hf_timestd/core/carrier_tec.py`
- GNSS VTEC anchor: `src/hf_timestd/core/physics_fusion_service.py`
- GNSS VTEC acquisition: `src/hf_timestd/core/gnss_tec.py`, `scripts/live_vtec.py`

**Scientific value:**
- TID (Traveling Ionospheric Disturbance) detection via cross-path correlation
- Solar flare signatures (rapid TEC enhancement)
- Diurnal TEC variation with minute-level resolution
- Storm-time ionospheric dynamics

#### 3.1.2 HF Group-Delay TEC — Validation Product

**Physics:** The ionospheric group delay follows the dispersion relation:

```
τ_iono = K × TEC / f²
where K = 40.3 m³/s² (ionospheric constant)
      τ_ms = 1.344 × TEC_TECU / f_MHz² (delay in milliseconds)
```

By fitting measured arrival times across multiple frequencies to a 1/f² model, the system estimates path-integrated TEC. However, **this product is noise-dominated** due to multipath, mode mixing, and the limited frequency spread of HF broadcasts.

**Limitation:** The group-delay TEC fit has a typical signal-to-noise ratio of ~0.13, meaning individual estimates are unreliable for absolute TEC. Its primary value is:
- **Validation** of carrier-phase dTEC trends
- **Consistency check** that multi-frequency measurements follow 1/f² physics
- **Confidence gating** — good R² boosts measurement confidence; poor R² reduces it
- **Mode change detection** — breaks in the 1/f² relationship indicate mode transitions

**Implementation:** `src/hf_timestd/core/tec_estimator.py`

#### 3.1.3 GNSS VTEC for Ionospheric Correction

When local GNSS VTEC is available, the system applies a direct correction to D_clock measurements in the metrology pathway:

```
D_clock_corrected = D_clock + Δiono
Δiono = 1.344 × (TEC_model - TEC_gnss) × n_hops × obliquity / f²
```

**TEC source hierarchy for propagation modeling:**

| Priority | Source | Accuracy | Latency |
|----------|--------|----------|---------|
| 1 | Local GNSS VTEC | ±1-2 TECU | ~1s |
| 2 | IONEX maps | ±2-5 TECU | 2 hours |
| 3 | IRI-2020 | ±5-10 TECU | Climatology |
| 4 | Parametric | ±10-20 TECU | Climatology |

**Implementation:** `src/hf_timestd/core/multi_broadcast_fusion.py` (GNSS VTEC correction block)

**Outputs:**
- `dtec_mean_tecu`: Anchored differential TEC (primary science product)
- `tec_tecu`: Group-delay TEC estimate (validation product)
- `anchor_status`: `ANCHORED_GNSS`, `ANCHORED_GROUP_DELAY`, or `NO_ANCHOR`
- `gnss_vtec_tecu`: Local GNSS-measured vertical TEC
- `confidence`: R² of the group-delay dispersion fit

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

### 3.4 Doppler Shift Measurement 

**Status:** Implemented (Unified in TickEdgeDetector, v6.8+)

**Physics:** Ionospheric motion causes Doppler shifts:

```
Δf_D = 2 × v_iono × f / c
```

Where v_iono is the effective ionospheric velocity along the propagation path.

**Implementation:** `src/hf_timestd/core/tick_edge_detector.py`

**TickEdgeDetector Carrier Phase Doppler (v6.8+):**

The `TickEdgeDetector` extracts Doppler from carrier phase progression across the minute:

1. At each detected tick, mix raw IQ samples at the tone frequency over the tick duration
2. Take the angle of the mean phasor → carrier phase at that tick
3. Unwrap phase across all detected ticks in the minute
4. Linear fit: slope (rad/s) / (2π) = Doppler frequency shift (Hz)

```python
# Carrier phase at each tick
iq_tick = iq_samples[tick_start:tick_end]
mixer = np.exp(-1j * 2π * tick_freq * t_tick)
carrier_phase = np.angle(np.mean(iq_tick * mixer))

# Doppler from phase slope across minute
phase_unwrapped = np.unwrap(phase_vals)
coeffs, cov = np.polyfit(phase_times, phase_unwrapped, 1, cov=True)
doppler_hz = coeffs[0] / (2π)
```

Requires ≥5 detected ticks spanning ≥5 seconds for a meaningful fit.

**Outputs (in tick_timing HDF5 product):**
- `doppler_hz`: Carrier Doppler shift from phase slope (Hz)
- `doppler_uncertainty_hz`: Uncertainty from linear fit covariance (Hz)

**Scientific Value:**
- Ionospheric velocity estimation
- TID (Traveling Ionospheric Disturbance) detection
- Channel stability assessment
- Diurnal Doppler signature tracks ionospheric layer motion

### 3.5 Multipath and Delay Spread 

**Status:** Implemented (Enhanced in v6.2, v6.11)

**Physics:** When multiple propagation modes arrive simultaneously, they create:
- Delay spread (time spreading of the signal)
- Fading (constructive/destructive interference)
- Phase distortion

**Implementation:** 
- `src/hf_timestd/core/advanced_signal_analysis.py`
- `src/hf_timestd/core/tone_detector.py` (_detect_multipath_from_correlation)
- `src/hf_timestd/core/tick_edge_detector.py` (CLEAN deconvolution)
- `src/hf_timestd/core/metrology_engine.py` (multipath-aware uncertainty widening)

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

**v6.11 Enhancement: Multipath-Aware Uncertainty Widening**

The unified measurement path (v6.11) extends multipath detection to influence the adaptive search window and Kalman filter weighting.  Two complementary multipath indicators are computed per station per minute:

1. **CLEAN deconvolution delay spread:** The `TickEdgeDetector` runs Högbom CLEAN on each per-second tick's correlation envelope to resolve multipath arrivals.  The maximum `delay_offset_ms` across all resolved secondary components gives the CLEAN delay spread.

2. **Per-second timing spread:** The `ensemble_uncertainty_ms` from the edge ensemble (MAD of per-tick timing errors) minus the noise floor (~0.5 ms at 24 kHz) captures multipath-induced timing variance.

The larger of these two indicators becomes the `multipath_spread_ms` for that station, which affects the pipeline in two ways:

- **Adaptive window:** Fed to `record_detection()` in `BroadcastWindowState`, which inflates the tracked variance in quadrature: `σ_eff = √(σ_obs² + σ_mp²)`.  This prevents the window from narrowing below the multipath-induced timing ambiguity.
- **Kalman confidence:** The `physics_confidence` for multipath-affected detections is reduced by `1/(1 + spread/3)`, which reduces their weight in the Fusion Kalman filter.

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
- **Adaptive window tracking** prevents false narrowing during multipath conditions

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
- ✅ **3D TDOA Resolution:** When $\ge 3$ paths are correlated, uses a 2D Time-Delay of Arrival array solver to unambiguously resolve velocity vector and azimuth.
- ✅ Fallback TID velocity/direction estimation from 2-path geometry and lag
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

## 5. Ionospheric Reanalysis: Leveraging Physics to Interpret Noisy Data

### 5.1 The Problem: Mode Misidentification in Real-Time Processing

The real-time propagation mode solver (Section 3.2) assigns modes by matching measured arrival delays to geometrically computed candidates. This is a purely kinematic approach — it asks *"which mode geometry best explains this delay?"* without asking *"is this mode physically possible right now?"*

The consequence is predictable: at night, when the F2-layer is thin and the critical frequency drops below 5 MHz, the solver still happily labels noise-floor detections at 10 or 15 MHz as `4F2` or `3F2` modes. These are geometrically plausible (the delay matches) but physically impossible (the ionosphere cannot support F-layer propagation at those frequencies under current conditions).

This contamination propagates downstream:

1. **MUF inflation**: The Maximum Usable Frequency is estimated from the highest frequency showing F-layer propagation. Noise detections at 15 MHz labeled `3F2` inflate the MUF to ~17 MHz when the true MUF might be 8 MHz.

2. **TEC contamination**: The TEC estimator fits a 1/f² dispersion model across frequencies. Including noise-floor "measurements" that carry no ionospheric information corrupts the fit.

3. **Mode statistics**: Hourly and daily propagation statistics include phantom F-layer modes that never actually occurred.

The root cause is that the real-time solver operates on individual measurements in isolation, with no awareness of the ionospheric state. It cannot distinguish a genuine 15 MHz F-layer reflection from a noise spike that happens to fall at the right delay.

<!-- LIVE: reanalysis-summary -->

### 5.2 The Solution: Offline Physics-Based Reanalysis

The ionospheric reanalysis service addresses this by applying what we *know* about the ionosphere to constrain what we *observe*. It runs hourly as a low-priority offline job, re-examining the previous hour's L2 timing measurements through a physics-based lens.

The governing principle is straightforward: **a propagation mode can only exist if the ionosphere can support it.** The F2-layer reflects a signal only if the signal frequency is below the layer's Maximum Usable Frequency (MUF) for that geometry. The MUF depends on the critical frequency (foF2), which depends on solar illumination. All of these quantities are calculable from first principles.

**Implementation:** `src/hf_timestd/core/ionospheric_reanalysis.py`

**Execution:** Hourly via `systemd` timer at `:05` past each hour, `nice 19`, `IOSchedulingClass=idle`

### 5.3 The Physics: From Solar Zenith Angle to Mode Validity

The reanalysis applies a chain of physical reasoning, each step grounded in well-established ionospheric physics:

#### Step 1: Solar Zenith Angle at the Path Midpoint

The ionosphere is a solar-driven phenomenon. The degree of ionization at any point depends primarily on the solar zenith angle (χ) — the angle between the sun and the local vertical. At the subsolar point χ = 0° and ionization is maximum; at the terminator χ = 90° and ionization drops rapidly.

For each transmitter-receiver path, we compute the geographic midpoint (where the signal reflects off the ionosphere) and calculate the solar elevation at that point for the hour being analyzed:

```
midpoint = great_circle_midpoint(tx_lat, tx_lon, rx_lat, rx_lon)
solar_elevation = solar_position(midpoint, timestamp)
χ = 90° - solar_elevation
```

**Implementation:** `src/hf_timestd/core/solar_zenith_calculator.py`

#### Step 2: Critical Frequency Estimation via Chapman Layer Model

The F2-layer critical frequency (foF2) — the highest frequency that can be reflected at vertical incidence — follows the Chapman production function. Under solar illumination, photoionization produces free electrons; in darkness, recombination depletes them. The equilibrium electron density, and hence foF2, depends on cos(χ):

```
foF2 = foF2_noon × cos^0.4(χ)     for χ < 80° (daytime)
foF2 = foF2_night_floor             for χ > 100° (deep night)
foF2 = interpolated                  for 80° < χ < 100° (twilight)
```

Where:
- **foF2_noon = 9.0 MHz** (moderate solar activity, F10.7 ≈ 150)
- **foF2_night_floor = 3.0 MHz** (residual nighttime ionization)
- The 0.4 exponent comes from the Chapman layer theory for the F2-layer, where the effective recombination rate gives a weaker dependence on χ than the simple cos^0.5 of a Chapman α-layer

This is a climatological estimate — it represents typical conditions, not the exact foF2 at this moment. But it is sufficient to reject clearly impossible modes (e.g., 15 MHz F-layer propagation when foF2 ≈ 4 MHz at night).

#### Step 3: Oblique MUF from Secant Law

A signal propagating obliquely through the ionosphere can be reflected at frequencies higher than foF2, because the effective path through the layer is longer. The relationship is the **secant law**:

```
MUF_oblique = foF2 × sec(θ_i)
```

Where θ_i is the angle of incidence at the ionospheric layer. For a signal making n hops over a great-circle distance d, reflecting at height h above the Earth (radius R):

```
half_angle = d / (2 × n × R)
sin(elevation) = cos(half_angle) - R × sin(half_angle)² / (R + h)
θ_i = 90° - elevation
```

More hops means steeper incidence (smaller θ_i), which means *lower* oblique MUF. A 1-hop path at 1000 km has a much higher MUF than a 4-hop path over the same distance.

#### Step 4: Mode Validation

With the oblique MUF computed for each candidate mode geometry, the validation is a simple inequality:

```
if signal_frequency > oblique_MUF(mode):
    mode is PHYSICALLY IMPOSSIBLE → reject or reclassify
```

Combined with an SNR gate (measurements below 12 dB are likely noise), this eliminates the phantom modes that contaminate the real-time estimates.

When a mode is rejected, the service attempts reclassification — trying higher hop counts (which have steeper angles and thus higher MUF) or, for strong daytime signals above the MUF, flagging as possible sporadic-E.

<!-- LOGS: reanalysis | filter: "mode_validation" -->

### 5.4 TEC Re-Estimation from Cleaned Data

With physically impossible modes removed, the TEC estimation becomes more reliable. The reanalysis uses a refined approach:

**The D_clock Insight:** Each L2 timing measurement contains `raw_arrival_time_ms` (actually D_clock — the timing error after subtracting the propagation model delay). If the propagation model were perfect, D_clock would be identical across all frequencies. The residual frequency-dependent pattern in D_clock *is* the ionospheric dispersion signal:

```
D_clock(f) = D_clock_vacuum + K × TEC / f²
```

Where K = 1.344 ms·MHz²/TECU is the ionospheric dispersion constant.

**Median Aggregation:** Rather than using a single measurement per frequency, the reanalysis takes the *median* D_clock across all valid measurements at each frequency within the hour. This is robust to outliers from occasional mode mis-assignments that survive the physics filter.

**Frequency Deduplication:** The TEC estimator fits T_obs vs 1/f². Multiple measurements at the same frequency map to the same x-value and add noise without improving the fit. The median aggregation naturally produces one data point per distinct frequency.

**Physical Validation:** Results are checked against physical bounds (0-200 TECU) and negative slopes (which indicate mode mixing or measurement pathology) are flagged and forced to zero rather than producing nonsensical negative TEC values.

<!-- LOGS: reanalysis | filter: "tec_reanalysis" -->

### 5.5 Outputs: L3C Propagation Statistics and Reanalyzed TEC

The reanalysis produces two data products:

**L3C Propagation Statistics** (`l3c_propagation_stats_v1` schema):
- Per-station, per-frequency mode probabilities (validated against physics)
- Estimated MUF with confidence
- Mean SNR, observation count, data completeness
- Quality flag (GOOD/MARGINAL/BAD)

**Reanalyzed L3A TEC** (`l3_tec_v1` schema):
- TEC in TECU from cleaned multi-frequency D_clock fit
- Confidence (R² of the 1/f² fit)
- Number of distinct frequencies used
- Dominant propagation mode
- Quality flag

The propagation service API (`/api/propagation/conditions`) now serves three MUF values:
- `muf_realtime_mhz`: Naive estimate from real-time mode assignments
- `muf_reanalyzed_mhz`: Physics-validated estimate from reanalysis
- `muf_estimate_mhz`: Best available (prefers reanalyzed when available)

### 5.6 Why This Works: The Epistemological Argument

The reanalysis exemplifies a general principle in measurement science: **prior knowledge constrains interpretation of noisy data.**

The real-time mode solver treats each measurement as an isolated observation and asks only "what mode geometry fits this delay?" This is the maximum-likelihood approach with a uniform prior — every mode is equally likely. The result is that noise, which is uniformly distributed in delay space, gets assigned to whichever mode geometry happens to be closest.

The reanalysis introduces a *physics-informed prior*: modes that require frequencies above the oblique MUF have zero probability. This is not a statistical assumption — it is a hard physical constraint. The ionosphere *cannot* reflect a 15 MHz signal when foF2 is 4 MHz, regardless of what the delay measurement says.

The improvement is most dramatic at night, when:
- foF2 drops to 3-5 MHz (only low frequencies can use F-layer)
- SNR drops on higher frequencies (signals are absorbed or not reflected)
- Noise-floor detections become a larger fraction of measurements
- The real-time solver has the most opportunity to misclassify noise as F-layer modes

During the day, when foF2 is 8-12 MHz and most frequencies genuinely propagate via F-layer, the reanalysis largely confirms the real-time assignments. This is the expected behavior — the physics constraint is most valuable precisely when the data is most ambiguous.

<!-- LOGS: reanalysis | filter: "hourly_summary" -->

### 5.7 Validation: Evidence from This Installation

The following live data demonstrates the reanalysis in operation on this installation. The key observable is the difference between the naive real-time MUF and the physics-validated reanalyzed MUF.

**When the correction is large** (e.g., real-time says 17 MHz, reanalysis says 8 MHz), it means the real-time pipeline was counting noise-floor detections as F-layer modes. The reanalysis correctly identified these as physically impossible and excluded them.

**When the correction is small or zero**, the real-time mode assignments were already physically consistent — the ionosphere could support the observed modes. This typically occurs during daytime when foF2 is high.

<!-- LIVE: reanalysis-summary -->

<!-- LOGS: reanalysis | filter: "muf_estimate" -->

---

## 6. Potential Future Capabilities

### 6.1 PHaRLAP 2D Numerical Ray Tracing ✔️

**Status:** Implemented (2026-03-19)

**Physics:** Full 2D numerical ray tracing through the ionosphere provides the most accurate propagation delay and mode identification, accounting for refraction, reflection height, and group path length through realistic electron density profiles.

**Implementation:** `src/hf_timestd/core/raytrace_engine.py`

The ray tracing engine uses PHaRLAP 4.7.4 (via the pyLAP Python interface) with a **spatially varying IRI-2020 electron density grid**. Rather than sampling IRI at a single midpoint, the engine:

1. Computes the great-circle bearing from TX to RX
2. Samples IRI-2020 Ne(h) profiles at multiple points along the path (auto-scaled: 1 per 500 km, minimum 5, maximum 25)
3. Linearly interpolates the Ne(h) profiles across all range columns to form a true 2D electron density grid
4. Passes the grid to `pylap.raytrace_2d()` for numerical ray tracing

**Horizontal variation significance:**

| Path | Distance | foF2 Variation | hmF2 Variation |
|------|----------|---------------|----------------|
| WWV → AC0G | 1,119 km | ±1–5% | ±10 km |
| CHU → AC0G | 1,522 km | ±1–5% | ±12 km |
| WWVH → AC0G | 6,600 km | ±16–38% | ±54 km |

The WWVH path spans tropical to mid-latitude ionosphere, where the horizontal gradient is significant — especially during dawn/dusk terminator crossing.

**Outputs:**
- Ray fan plots showing all propagation modes
- Group path delay per ray
- Mode identification (1F, 2F, 3F) from closing rays
- Ionospheric grid parameters (foF2, hmF2 at midpoint)

**Computational cost:** ~2–3 ms per IRI call; 5 samples add ~15 ms. Negligible compared to ray tracing itself.

**Scientific value:**
- Validates the real-time `HFPropagationModel` delay predictions
- Provides ground truth for mode identification
- Enables QEX article figures showing ray geometry through realistic ionosphere
- Captures horizontal Ne gradients missed by single-point models

### 6.2 Critical Frequency Estimation (foF2) — Partially Implemented

**Status:** The ionospheric reanalysis service (Section 5) now estimates foF2 from solar zenith angle using the Chapman layer model. This provides a climatological estimate suitable for mode validation.

**Remaining work:**
- Calibrate foF2_noon against ionosonde data (currently fixed at 9.0 MHz)
- Incorporate solar activity index (F10.7) for solar cycle variation
- Compare with IRI-2020 foF2 predictions
- Back-calculate foF2 from observed MUF using secant law (inverse problem)

### 6.3 Ionospheric Tilt Detection

**Concept:** Large-scale ionospheric gradients cause systematic TEC differences between paths.

**Method:**
- Compare TEC from WWV, WWVH, CHU, BPM paths
- Calculate TEC gradient vector
- Track gradient evolution

**Requirements:**
- Validated TEC from multiple paths
- Good azimuthal coverage
- Gradient calculation algorithm

### 6.4 Space Weather Correlation

**Concept:** Correlate ionospheric measurements with space weather indices.

**Method:**
- Ingest real-time Kp, Dst, F10.7 indices
- Correlate with TEC, absorption, Doppler
- Build predictive models

**Requirements:**
- Real-time space weather data feed
- Historical correlation analysis
- Prediction algorithm development

### 6.5 Luxembourg Effect Detection

**Concept:** Cross-modulation between powerful transmitters via ionospheric non-linearity.

**Method:**
- Detect intermodulation products (e.g., 500+600 Hz)
- Measure cross-modulation depth
- Correlate with D-layer density

**Requirements:**
- High-resolution spectrum analysis
- Intermodulation product detection
- D-layer model integration

### 6.6 Geomagnetic Storm Effects

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

## 7. The WWV/WWVH Scientific Test Signal

### 7.1 Overview

At minutes :08 (WWV) and :44 (WWVH), a special **scientific modulation test signal** is transmitted. This 45-second signal was designed by the WWV/H Scientific Modulation Working Group specifically for ionospheric channel characterization.

**Implementation:** `src/hf_timestd/core/wwv_test_signal.py`

### 7.2 Signal Structure

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

### 7.3 Measurements from Test Signal

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

### 7.4 Current Implementation Status

| Feature | Status | Notes |
|---------|--------|-------|
| Test signal detection | ✅ Implemented | Template correlation |
| Multi-tone power | ✅ Implemented | Per-frequency power measurement |
| FSS calculation | ✅ Implemented | Frequency selectivity score |
| Delay spread | ✅ Implemented | Chirp matched filter with -3dB width |
| Scintillation (S4) | ✅ Implemented | From multi-tone amplitude variance |
| Transient detection | ✅ Implemented | Noise #1 vs #2 comparison |

---

## 8. GNSS-VTEC Integration (Implemented 2026-02-24)

### 8.1 Local TEC Ground Truth

A dual-frequency GNSS receiver (u-blox ZED-F9P) measures **local vertical TEC in real-time** at ~1 Hz cadence.

**Implementation:** `timestd-vtec` service (`scripts/live_vtec.py`)

**Data flow:**
```
ZED-F9P (TCP) → UBX Parser → GNSSTECAnalyzer → HDF5 (86K records/day)
                                                   ↓
                              PhysicsFusionService._read_gnss_vtec()
                                                   ↓
                              carrier-phase dTEC anchoring (±120s match)
```

**Advantages:**
- ~1 second latency (vs 1-2 hours for IONEX)
- Point measurement at receiver location
- ~1 TECU accuracy (DCB-corrected, ≥6 satellites, 20° elevation mask)
- Tracks rapid TEC changes during storms

### 8.2 Carrier-Phase dTEC Anchoring

The primary use of GNSS VTEC is anchoring the carrier-phase dTEC product. Before anchoring, integrated dTEC was a **relative** product (dTEC/dt rate valid, but absolute level unknown). Now:

**Anchor source priority cascade:**

| Priority | Source | Method | Accuracy | anchor_status |
|----------|--------|--------|----------|---------------|
| 1 | **Local GNSS VTEC** | ZED-F9P overhead VTEC | ±1 TECU | `ANCHORED_GNSS` |
| 2 | Group-delay TEC | HF 1/f² fit | SNR 0.13 (noise) | `ANCHORED_GROUP_DELAY` |
| 3 | None | — | — | `NO_ANCHOR` |

**Implementation:** `physics_fusion_service.py:_read_gnss_vtec()` reads the nearest VTEC measurement within ±120 seconds from the HDF5 files written by `live_vtec.py`. The VTEC is applied as the DC offset for all station-channels. Per-day arrays are cached in memory with automatic eviction.

**Schema:** `l3_dtec_v1.json` and `l3_dtec_timeseries_v1.json` updated to v1.1.0 with expanded `anchor_status` enum.

**Effect on metrology:**
- `is_anchored` flips True for all station-channels when GNSS VTEC is available
- `quality_flag` can reach `GOOD` (was capped at `MARGINAL` when unanchored)
- `dtec_mean_tecu` becomes physically meaningful (absolute overhead TEC ± integration drift)
- Minute-to-minute continuity established (common GNSS reference eliminates inter-minute DC jumps)

**Known limitation:** GNSS VTEC is overhead (zenith). The true slant TEC for each HF path would be `sTEC = VTEC × M(elevation)` where M is the thin-shell mapping factor. This introduces a ~10–30% systematic bias that is constant and removable in post-processing. Future work: apply per-path slant correction using the elevation geometry from `_build_ipp_measurements()`.

**Validated (2026-02-24):** 17 station-channel records written with `ANCHORED_GNSS`, `anchor_tec=41.7 TECU` from Feb 20 data.

### 8.3 VTEC Map Enhancement (Future)

GNSS VTEC gives vertical TEC overhead at one point. For VTEC maps you need spatially distributed measurements. Options:
1. Use GNSS VTEC as a Bayesian prior in the 1/f² fit → better per-path sTEC
2. Use GNSS VTEC as map background, overlay HF-derived dTEC perturbations

### 8.4 Comparison with Other TEC Sources

| Source | Latency | Spatial Resolution | Accuracy |
|--------|---------|-------------------|----------|
| IONEX maps | 1-2 hours | 2.5° × 5° grid | ±2-5 TECU |
| **Local GNSS** | **~1 second** | **Point at receiver** | **±1 TECU** |
| HF group-delay | Real-time | Path-integrated | SNR 0.13 (noise) |
| HF carrier-phase dTEC | Real-time | Path-integrated | ~6 mTECU/min (rate) |

### 8.5 Configuration

Enable via `timestd-config.toml`:

```toml
[gnss_vtec]
enabled = true
host = "192.168.0.203"
port = 9000
save_hdf5 = true
hdf5_path = "data/gnss_vtec"
```

The `physics_fusion_service` reads `[gnss_vtec].hdf5_path` from the config and resolves relative paths against `data_root`.

---

## 9. Data Products for Science

### 9.1 L2 Timing Measurements

Each minute, per broadcast:
- `d_clock_ms`: Clock offset measurement
- `uncertainty_ms`: ISO GUM uncertainty
- `propagation_mode`: Identified mode
- `propagation_delay_ms`: Calculated delay
- `raw_arrival_time_ms`: Uncalibrated ToA (for TEC)

### 9.2 L3 Science Products

Aggregated across frequencies/stations:
- `dtec_mean_tecu`: Carrier-phase dTEC (primary product, GNSS-anchored when available)
- `tec_tecu`: Group-delay TEC estimate (validation product, noise-dominated)
- `anchor_status`: TEC anchoring source (`ANCHORED_GNSS`, `ANCHORED_GROUP_DELAY`, `NO_ANCHOR`)
- `tec_confidence`: Group-delay fit quality (R²)
- `group_delay_ms`: Per-frequency delays

### 9.3 Channel Characterization

Per broadcast:
- `doppler_hz`: Carrier Doppler shift
- `doppler_std_hz`: Doppler spread
- `delay_spread_ms`: Multipath delay spread
- `snr_db`: Signal-to-noise ratio
- `fading_variance`: Amplitude stability

### 9.4 Test Signal Products

At minutes :08/:44:
- `frequency_selectivity_db`: FSS score
- `tone_powers_db`: Per-tone power
- `scintillation_index`: S4 estimate
- `transient_detected`: Anomaly flag

---

## 10. Validation and Limitations

### 10.1 Validated Measurements

| Measurement | Validation Method | Status |
|-------------|------------------|--------|
| Carrier SNR | Compare with radiod | ✅ Validated |
| Doppler shift | Physical range check | ✅ Validated |
| Propagation delay | Great circle + model | ✅ Validated |
| Mode identification | Delay-based heuristics | ⚠️ ~80-90% accuracy |
| Carrier-phase dTEC | GNSS VTEC cross-check | ✅ Validated (v6.8) |
| Group-delay TEC | GPS IONEX comparison | ⚠️ Noise-dominated (SNR ~0.13) |

### 10.2 Fundamental Limitations

**Single Receiver:**
- No spatial resolution (cannot determine propagation direction)
- Cannot separate ionospheric layers
- Not suitable for tomography

**Model Dependence:**
- Propagation delay requires ionospheric model
- Mode identification uses heuristics
- Group-delay TEC assumes single-layer ionosphere; carrier-phase dTEC avoids this assumption

**Ionospheric Variability:**
- ±1-3 ms timing uncertainty from ionosphere
- Mode mixing confuses identification
- Storm-time behavior unpredictable

### 10.3 Recommended Use Cases

✅ **Well-suited for:**
- D-layer absorption studies
- Propagation mode statistics
- Long-term dTEC monitoring (carrier-phase)
- Sporadic-E event detection
- Diurnal ionospheric patterns

⚠️ **Use with caution:**
- Absolute TEC values (group-delay TEC is noise-dominated; use GNSS-anchored dTEC instead)
- TID detection (implemented but requires further validation)
- Real-time propagation prediction

❌ **Not suitable for:**
- Ionospheric tomography
- Layer height determination (no ionosonde)
- Sub-millisecond timing (ionospheric limit)

---

## 11. References

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
| **Tick Timing (D_clock, Doppler, SNR)** | `src/hf_timestd/core/tick_edge_detector.py` |
| **Propagation Model** | `src/hf_timestd/core/propagation_model.py` |
| **PHaRLAP Ray Tracing** | `src/hf_timestd/core/raytrace_engine.py` |
| **Ionospheric Data** | `src/hf_timestd/core/iono_data_service.py` |
| TEC Estimation | `src/hf_timestd/core/tec_estimator.py` |
| Carrier-Phase dTEC + Anchoring | `src/hf_timestd/core/carrier_tec.py` |
| GNSS VTEC (ZED-F9P) | `src/hf_timestd/core/gnss_tec.py` |
| GNSS VTEC Live Service | `scripts/live_vtec.py` |
| Physics Fusion (dTEC anchor) | `src/hf_timestd/core/physics_fusion_service.py` |
| Ionospheric Model | `src/hf_timestd/core/ionospheric_model.py` |
| Propagation Modes | `src/hf_timestd/core/propagation_mode_solver.py` |
| Doppler/Multipath | `src/hf_timestd/core/advanced_signal_analysis.py` |
| Scintillation (S4, σ_φ) | `src/hf_timestd/core/advanced_signal_analysis.py` |
| Sporadic-E Detection | `src/hf_timestd/core/propagation_mode_solver.py` |
| CHU FSK Decoding | `src/hf_timestd/core/chu_fsk_decoder.py` |
| Test Signal | `src/hf_timestd/core/wwv_test_signal.py` |
| WWV/WWVH Discrimination (legacy) | `src/hf_timestd/core/wwvh_discrimination.py` |
| WWV/WWVH Discrimination (primary) | `src/hf_timestd/core/probabilistic_discriminator.py` |
| TID Detection | `src/hf_timestd/core/tid_detector.py` |
| Physics Validation | `src/hf_timestd/core/arrival_pattern_matrix.py` |
| Multi-Constraint Validation | `src/hf_timestd/core/timing_consistency_validator.py` |
| Ionospheric Reanalysis | `src/hf_timestd/core/ionospheric_reanalysis.py` |

---

## Appendix C: Related Documentation

- **docs/METROLOGY.md** — Time transfer methodology and uncertainty budgets
- **docs/TECHNICAL_REFERENCE.md** — System architecture and configuration
- **docs/ARCHITECTURE.md** — Design philosophy and system architecture
- **docs/CARRIER_DOPPLER_INTERPRETATION.md** — HF channel and Doppler measurement details

---

**Source Code:** <https://github.com/mijahauan/hf-timestd>  
**License:** MIT  
**Author:** Michael James Hauan (AC0G)
