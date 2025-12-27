# HF-TimeStd: Signal Features and Scientific Capabilities

## Overview

This document provides an honest assessment of what signal features the HF-TimeStd system can measure and what scientific questions those measurements can address. We distinguish between:

1. **Validated measurements** - Features we confidently detect and measure
2. **Partially validated** - Features measured but requiring further validation
3. **Theoretical capabilities** - Features that could be measured with additional work

**Philosophy**: We provide only the data we can justify given our instrument's capabilities and limitations. Scientists can then determine if the data quality meets their research needs.

---

## Detectable Signal Features

### WWV/WWVH (NIST, Fort Collins, CO / Kekaha, HI)

#### Validated Measurements ✅

- **Test signal** (minutes 8, 44)
  - 1000 Hz continuous tone
  - SNR measurement
  - Detection confidence
  
- **BCD modulation** (100 Hz subcarrier)
  - Binary-coded decimal time code
  - Correlation-based decoding
  - UTC time extraction
  - Decoding confidence score

- **Station ID tones**
  - WWV: 500 Hz (even minutes), 600 Hz (odd minutes)
  - WWVH: 1200 Hz (even minutes), 1500 Hz (odd minutes)
  - Tone power (dB)
  - Detection timing (ms precision)
  - Mutual exclusivity validation

- **Second ticks**
  - Timing pulse detection
  - Per-tick SNR measurement
  - Timing precision: ~1 ms (limited by ionospheric variability)

- **Carrier SNR**
  - Channel-level signal strength
  - Noise floor estimation
  - Units: dB relative to noise

- **Carrier Doppler shift**
  - Frequency offset from nominal carrier
  - Range: ±10 Hz (typical ionospheric motion)
  - Precision: ~0.1 Hz

- **Voice announcements**
  - 440 Hz voice ID tone detection
  - Content: Not decoded (voice recognition not implemented)

#### Partially Validated ⚠️

- **Phase variance** (per-second ticks)
  - Measured in radians
  - Interpretation: Channel coherence quality
  - **Validation needed**: Correlation with scintillation indices

- **Doppler spread**
  - Standard deviation of Doppler over measurement window
  - Units: Hz
  - **Validation needed**: Window size, physical interpretation

### CHU (Ottawa, ON, Canada)

#### Validated Measurements ✅

- **AFSK modulation**
  - Bell 103 FSK (1070/1270 Hz)
  - Binary time code decoding
  - UTC time extraction
  - Decoding confidence

- **Second ticks**
  - Timing pulse detection
  - Per-tick SNR

- **Carrier SNR**
  - Channel-level signal strength

#### Partially Validated ⚠️

- Same phase variance and Doppler spread as WWV/WWVH

### BPM (Pucheng, China)

#### Partially Validated ⚠️

- **Second ticks** - Basic detection implemented
- **Carrier SNR** - Measured
- **Modulation format** - Needs full characterization
- **Station ID method** - Requires research/documentation

**Status**: BPM support is experimental. Full validation pending.

---

## Cross-Station and Multi-Frequency Features

### Validated Measurements ✅

**Time of Arrival (ToA)**

- Measured propagation delay from transmitter to receiver
- Precision: ~1 ms (ionospheric variability limit)
- Validation: Compared against great circle distance + ionospheric model

**Multi-frequency SNR**

- SNR measured across 2.5, 5, 10, 15, 20, 25 MHz
- Enables frequency-dependent absorption analysis
- Diurnal patterns observable

**Propagation mode classification**

- Modes: 1E (E-layer), 1F (F-layer), 2F (two-hop F), 3F, GW (ground wave)
- Method: Delay-based heuristics
- **Limitation**: Cannot always distinguish mode mixing

**Propagation delay**

- Total path delay estimation
- Includes: Free-space + ionospheric delay
- Uncertainty: ~2-5 ms depending on mode

### Partially Validated ⚠️

**Total Electron Content (TEC)**

- Method: Multi-frequency dispersion (f^-2 dependence)
- Units: TECU (10^16 electrons/m²)
- **Validation needed**: Comparison with GPS TEC maps (IONEX data)
- **Limitation**: Requires accurate ToA across multiple frequencies
- **Status**: TECEstimator class exists, needs validation

**Delay spread**

- Multipath severity indicator
- Units: milliseconds
- **Validation needed**: Measurement method verification
- **Use case**: Mode mixing detection

**Frequency selective spread (FSS)**

- Multipath in frequency domain
- Units: dB
- **Validation needed**: Confirm measurement implementation

**Coherence time**

- Maximum coherent integration window
- Units: seconds
- **Validation needed**: Calculation method verification

---

## Scientific Questions Addressable with Current Measurements

### 1. D-Layer Absorption Studies

**Measurements Used**:

- Multi-frequency SNR (2.5 - 25 MHz)
- Solar zenith angle at path midpoint
- Time of day

**Scientific Questions**:

- How does D-layer absorption vary with frequency?
- What is the diurnal pattern of absorption?
- Can we detect Sudden Ionospheric Disturbances (SIDs) from solar flares?

**Data Quality**: ✅ High confidence

- SNR measurements validated
- Frequency dependence well-established physics
- Solar zenith angle calculable from geometry

**Limitations**:

- Cannot separate D-layer from E-layer absorption
- Requires clear day/night comparison

### 2. Propagation Mode Statistics

**Measurements Used**:

- Propagation mode classification (1E, 1F, 2F, etc.)
- Time of day
- Frequency
- Propagation delay

**Scientific Questions**:

- What is the probability of E-layer vs F-layer propagation by time/frequency?
- How often does multi-hop propagation occur?
- Can we estimate Maximum Usable Frequency (MUF)?

**Data Quality**: ⚠️ Medium confidence

- Mode classification based on delay heuristics
- Cannot always detect mode mixing
- Thresholds may need tuning

**Limitations**:

- Simplified ray-tracing model
- No direct ionospheric sounding

### 3. TEC Monitoring

**Measurements Used**:

- Multi-frequency ToA (2.5 - 25 MHz)
- Dispersion analysis (f^-2 fit)

**Scientific Questions**:

- What is the local TEC over the receiver?
- How does TEC vary diurnally?
- Can we validate GPS TEC maps?

**Data Quality**: ⚠️ Requires validation

- TEC estimation implemented but not validated
- Needs comparison with GPS TEC (IONEX)
- Accuracy depends on ToA precision

**Limitations**:

- Single line-of-sight (not tomographic)
- Assumes single-layer ionosphere
- Mode mixing affects accuracy

### 4. Sporadic-E Detection

**Measurements Used**:

- SNR sudden increases at 10-15 MHz
- Mode change to 1E
- Event timing and duration

**Scientific Questions**:

- When do sporadic-E events occur?
- What is the seasonal/diurnal pattern?
- What is the critical frequency (foEs)?

**Data Quality**: ⚠️ Detection possible, characterization needs work

- SNR increases detectable
- Mode classification may miss Es
- Critical frequency estimation not implemented

**Limitations**:

- No direct ionogram
- Cannot measure Es layer height
- Weak Es may be missed

### 5. Ionospheric Dynamics (TIDs)

**Measurements Used**:

- Doppler shift time series
- Coherent oscillations across frequencies
- Phase velocity estimation

**Scientific Questions**:

- Can we detect Traveling Ionospheric Disturbances?
- What are the TID periods and wavelengths?
- Do TIDs correlate with geomagnetic activity?

**Data Quality**: ⚠️ Theoretical capability, needs implementation

- Doppler measured but TID detection not automated
- Requires coherent analysis across frequencies
- Period/wavelength extraction not implemented

**Limitations**:

- Single receiver (cannot determine propagation direction)
- Requires stable reference (GPSDO provides this)

### 6. Ionospheric Tilt

**Measurements Used**:

- TEC from multiple transmitter paths (WWV, WWVH, CHU, BPM)
- Different azimuths from receiver
- TEC gradient calculation

**Scientific Questions**:

- What is the large-scale ionospheric structure?
- Can we detect ionospheric tilts?
- How does TEC vary with azimuth?

**Data Quality**: ⚠️ Theoretical capability

- Requires validated TEC from multiple paths
- Gradient calculation not implemented
- Needs at least 3 paths with good geometry

**Limitations**:

- Limited azimuthal coverage (4 transmitters)
- Assumes linear gradient
- Path midpoints may be too close

---

## Advanced Features (Not Yet Implemented)

### Could Be Added with Current Hardware

**Amplitude Scintillation Index (S4)**

- Normalized variance of carrier amplitude
- Requires: Continuous amplitude tracking
- Scientific value: Ionospheric irregularity strength

**Phase Scintillation Index (σ_φ)**

- Standard deviation of detrended carrier phase
- Requires: High-rate phase tracking
- Scientific value: TEC fluctuation severity

**Fading Rate**

- Zero-crossing rate of SNR time series
- Requires: SNR time series analysis
- Scientific value: Channel dynamics quantification

**Critical Frequency (foF2) Estimation**

- Highest frequency with F-layer propagation
- Requires: Multi-frequency observations + analysis
- Scientific value: Ionospheric peak density proxy

**Intermodulation Products**

- Detection of non-linear mixing (e.g., 500+600 Hz)
- Requires: Spectrum analysis at sum/difference frequencies
- Scientific value: Ionospheric non-linearity (Luxembourg effect)

### Requires External Data

**Geomagnetic Correlation**

- Correlate SNR/TEC with Kp index
- Requires: Real-time Kp data feed
- Scientific value: Space weather impacts

**Solar Activity Correlation**

- Correlate with solar flux (F10.7), sunspot number
- Requires: Real-time solar data feed
- Scientific value: Solar cycle effects

**IRI-2020 Model Validation**

- Compare measured ToA with IRI-2020 predictions
- Requires: IRI-2020 integration
- Scientific value: Ionospheric model validation

---

## Measurement Uncertainties and Limitations

### Timing Precision

- **System clock**: ±0.13 ms (Grade A, GPSDO-disciplined)
- **Ionospheric variability**: ±1-3 ms (dominant error source)
- **Propagation delay**: ±2-5 ms (model uncertainty)

### SNR Measurement

- **Precision**: ±0.5 dB (FFT-based estimation)
- **Calibration**: Relative to noise floor (not absolute dBm)
- **Validation**: Should match radiod's reported SNR

### Doppler Measurement

- **Precision**: ~0.1 Hz (phase tracking)
- **Range**: ±10 Hz (ionospheric motion)
- **Systematic offset**: GPSDO frequency error (< 0.01 Hz)

### TEC Estimation

- **Precision**: ±1-2 TECU (multi-frequency dispersion)
- **Accuracy**: Requires validation against GPS TEC
- **Limitation**: Assumes single-layer ionosphere

### Propagation Mode Classification

- **Accuracy**: ~80-90% (delay-based heuristics)
- **Limitation**: Cannot always detect mode mixing
- **Validation**: Needs comparison with ray-tracing

---

## Data Product Levels

### L1A: Channel Observables

Raw signal features directly from IQ samples:

- Carrier power, SNR, Doppler
- Tone detections and timing
- Phase variance, coherence time
- **Cadence**: 1 minute
- **Format**: HDF5 (schema-validated)

### L1B: BCD Timecode

Decoded time information:

- BCD correlation results
- UTC time extraction
- Decoding confidence
- **Cadence**: 1 minute
- **Format**: HDF5

### L2: Timing Measurements

Station-assigned timing with uncertainty budget:

- D_clock (system clock offset)
- Propagation delay and mode
- ISO GUM uncertainty budget
- Quality grade (A/B/C/D)
- **Cadence**: 1 minute
- **Format**: HDF5

### L3: Fused Timing

Multi-station, multi-frequency consensus:

- Fused D_clock (Kalman filtered)
- Uncertainty reduction
- Per-station contributions
- **Cadence**: 1 minute
- **Format**: HDF5 (planned), CSV (current)

### Science Products

Derived ionospheric parameters:

- TEC estimates (when validated)
- Sporadic-E events
- TID detections
- **Cadence**: Variable
- **Format**: HDF5

---

## Validation Requirements

### Before Scientific Use

**Tier 1: Basic Validation** (Required for all features)

1. Compare carrier SNR with radiod's reported SNR
2. Verify Doppler is within ±5 Hz range
3. Check tone detection mutual exclusivity (WWV vs WWVH)
4. Validate ToA is physically reasonable (< 100 ms)

**Tier 2: Cross-Validation** (Required for ionospheric features)

1. Compare TEC with GPS TEC maps (IONEX data)
2. Validate propagation modes against ray-tracing
3. Correlate D-layer absorption with solar zenith angle
4. Understand physical meaning of phase variance

**Tier 3: Scientific Validation** (Required for publication)

1. Compare sporadic-E detections with ionosonde data
2. Validate TID period/wavelength estimates
3. Compare MUF estimates with VOACAP predictions
4. Document all systematic errors and biases

---

## Recommendations for Scientists

### Data Quality Assessment

1. **Always check timing metrology grade** (A/B/C/D)
   - Grade C or D: Timing unreliable, propagation science questionable
   - Grade A/B: Timing validated, suitable for science

2. **Verify data completeness**
   - Check for gaps in time series
   - Minimum 80% completeness recommended

3. **Understand limitations**
   - Single receiver (no spatial resolution)
   - Model-dependent propagation delay
   - Cannot separate ionospheric layers

### Recommended Use Cases

✅ **Well-suited for**:

- D-layer absorption studies (diurnal patterns)
- Propagation mode statistics
- Sporadic-E event detection
- Long-term TEC monitoring (after validation)

⚠️ **Use with caution**:

- Absolute TEC values (needs GPS validation)
- Propagation mode classification (mode mixing)
- TID detection (needs implementation)

❌ **Not suitable for**:

- Ionospheric tomography (single receiver)
- Layer height determination (no ionosonde)
- Absolute timing better than ±1 ms (ionospheric limit)

---

## Future Enhancements

### High Priority (Improves existing features)

1. Validate TEC against GPS TEC maps
2. Implement automated sporadic-E detection
3. Refine propagation mode classification
4. Add scintillation indices (S4, σ_φ)

### Medium Priority (New capabilities)

1. TID detection and characterization
2. Critical frequency (foF2) estimation
3. Fading rate analysis
4. Intermodulation product detection

### Low Priority (Requires external data)

1. Geomagnetic correlation (Kp index)
2. Solar activity correlation (F10.7)
3. IRI-2020 model validation

---

## Summary

HF-TimeStd provides validated measurements of:

- ✅ Carrier SNR, Doppler, timing ticks
- ✅ Station ID tones and BCD time code
- ✅ Multi-frequency propagation characteristics
- ⚠️ TEC (needs validation)
- ⚠️ Propagation modes (needs refinement)

**Our commitment**: Provide only data we can justify. Scientists determine if quality meets their needs.

**Validation status**: ~40% fully validated, ~30% partially validated, ~30% theoretical/future.

For questions about measurement methods or data quality, contact the development team.
