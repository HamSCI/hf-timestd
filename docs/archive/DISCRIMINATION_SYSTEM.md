# WWV/WWVH Station Discrimination System

## Technical Summary

**Version:** 2.0 (November 2025)  
**Authors:** GRAPE Project Team

---

## 1. Problem Statement

### 1.1 The Discrimination Challenge

WWV (Fort Collins, Colorado) and WWVH (Kauai, Hawaii) simultaneously broadcast standard time and frequency on shared frequencies (2.5, 5, 10, 15, and 20 MHz). At a receiver location, both signals may be present with overlapping propagation paths, making it difficult to determine which station is dominant or whether both are being received.

Accurate station discrimination is critical for:
- **Ionospheric research**: Path-specific propagation characteristics
- **Receiver calibration**: Station-specific timing offsets
- **Signal quality assessment**: Per-station SNR monitoring
- **Scientific data validation**: Ground truth for propagation models

### 1.2 Signal Interference Mechanisms

The discrimination problem is complicated by several interference mechanisms:

#### Harmonic Interference
The stations use different audio tone markers, but their harmonics fall on each other's frequencies:

| Fundamental | 2nd Harmonic | Interference Target |
|-------------|--------------|---------------------|
| WWV 500 Hz  | 1000 Hz      | WWV timing marker   |
| WWVH 600 Hz | 1200 Hz      | WWVH timing marker  |
| WWV 600 Hz  | 1200 Hz      | WWVH timing marker  |
| WWVH 500 Hz | 1000 Hz      | WWV timing marker   |

Receiver nonlinearity generates 2nd harmonics proportional to the fundamental power. When both stations are received, the 500 Hz and 600 Hz tones each contribute harmonic content to both the 1000 Hz and 1200 Hz timing markers.

#### Intermodulation Products (BCD × Audio Tones)
The 100 Hz BCD time code signal intermodulates with the 500/600 Hz audio tones:

```
500 Hz + 100 Hz = 600 Hz  (interferes with WWVH 600 Hz tone)
600 Hz - 100 Hz = 500 Hz  (interferes with WWV 500 Hz tone)
500 Hz - 100 Hz = 400 Hz  (near 440 Hz station ID)
600 Hz + 100 Hz = 700 Hz  (additional spurious)
```

These intermodulation products (IM3) arise from mixer nonlinearity and create false tone detections, particularly problematic when one station is significantly stronger.

#### Multipath and Differential Delay
Signals from both stations travel different ionospheric paths with:
- **Different propagation delays**: ~1-15 ms differential
- **Different Doppler shifts**: Due to ionospheric motion
- **Different fading patterns**: Independent Rayleigh fading

When both signals are present with similar power levels, the timing markers can constructively or destructively interfere, causing rapid power fluctuations.

---

## 2. Discriminating Features

We leverage multiple unique features that differ between the two stations:

### 2.1 Time-Domain Features

| Feature | WWV | WWVH | Discrimination Value |
|---------|-----|------|---------------------|
| **Timing Tone Frequency** | 1000 Hz (5ms pulse) | 1200 Hz (5ms pulse) | Primary - distinct frequencies |
| **BCD Time Code** | Arrives first (closer to most US receivers) | Arrives ~1-15ms later | ToA difference indicates path |
| **Test Signal** | 300-1500 Hz sweep (min 8) | 1200 Hz pulse (min 44) | Definitive identification |

### 2.2 Frequency-Domain Features

| Feature | WWV | WWVH | Discrimination Value |
|---------|-----|------|---------------------|
| **440 Hz Station ID** | Minute 1 | Minute 2 | Ground truth (exclusive) |
| **500 Hz Tone** | Minutes 0-7, 10-15, 18-19, 30-39 | Minutes 0, 30-39, 43-51 | Partial overlap |
| **600 Hz Tone** | Minutes 0-7, 10-15, 18-19, 30-39 | Minutes 0, 30-39, 43-51 | Partial overlap |
| **Doppler Signature** | Path-dependent | Different path | Validates separation |

### 2.3 Ground Truth Minutes (Exclusive Broadcasts)

Certain minutes provide **definitive ground truth** because only one station broadcasts specific tones:

#### 440 Hz Station Identification
- **Minute 1**: WWV broadcasts 440 Hz, WWVH broadcasts 600 Hz
- **Minute 2**: WWV broadcasts 440 Hz, WWVH broadcasts 600 Hz

*Note: Both broadcast 440 Hz but at different times within these minutes*

#### 500/600 Hz Exclusive Tones (14 minutes/hour)

**WWV-only minutes** (500/600 Hz): 1, 16, 17, 19 (4 minutes)
- WWVH broadcasts only 440 Hz during these minutes

**WWVH-only minutes** (500/600 Hz): 2, 43, 44, 45, 46, 47, 48, 49, 50, 51 (10 minutes)
- WWV broadcasts only 440 Hz during these minutes

### 2.4 BCD Time Code Encoding

Both stations transmit identical BCD-encoded time information, but:
- **Amplitude modulation depth** differs slightly
- **Propagation delay** creates measurable ToA difference
- **Correlation peak shape** indicates single vs. dual station reception

---

## 3. Discrimination Methods

### 3.1 Method Overview

The system employs a **multi-modal weighted voting architecture** with 8 independent voting methods:

| Vote | Method | Weight | Trigger Condition |
|------|--------|--------|-------------------|
| 0 | Test Signal Detection | 15.0 | Minutes 8, 44 |
| 1 | 440 Hz Tone Detection | 10.0 | Minutes 1, 2 |
| 2 | BCD Amplitude Ratio | 2.0-10.0 | Dual peaks detected |
| 3 | 1000/1200 Hz Power Ratio | 5.0-10.0 | Standard minutes |
| 4 | Tick SNR Comparison | 5.0 | All minutes |
| 5 | 500/600 Hz Ground Truth | 10.0 | Exclusive minutes |
| 6 | Differential Doppler | 2.0 | Quality > 0.3 |
| 7 | Test Signal ↔ BCD ToA | 3.0 | Minutes 8, 44 |
| 8 | Harmonic Power Ratio | 1.5 | Ratio diff > 3dB |

### 3.2 Primary Detection Methods

#### 3.2.1 Timing Tone Power Ratio (VOTE 3)
```
Power Ratio (dB) = P_1000Hz - P_1200Hz
```
- **WWV dominant**: Ratio > +3 dB
- **WWVH dominant**: Ratio < -3 dB
- **Balanced**: |Ratio| < 3 dB

**Processing**: 
1. AM demodulate IQ samples
2. Apply 4096-point FFT with Hann window
3. Measure power at 1000 Hz and 1200 Hz bins
4. Calculate noise floor in 825-875 Hz guard band
5. Compute SNR-weighted power ratio

#### 3.2.2 BCD Correlation Discrimination (VOTE 2)

The BCD time code provides 60 correlation opportunities per minute (one per second).

**Algorithm**:
1. Generate BCD reference waveforms for expected minute
2. Cross-correlate with AM-demodulated audio
3. Detect peaks in correlation envelope
4. Classify peaks by Time of Arrival (ToA):
   - **Early peak** → Closer station (typically WWV for US receivers)
   - **Late peak** → Farther station (typically WWVH)
5. Measure amplitude ratio between peaks

**Dual-Peak Detection**:
- Peak separation > 1ms indicates both stations present
- Differential delay computed from peak ToA difference
- Amplitude ratio indicates relative signal strength

**Single-Peak Classification**:
When only one correlation peak is detected:
1. Use geographic ToA prediction based on receiver location
2. Cross-validate with timing tone power ratio
3. Apply Doppler signature consistency check

#### 3.2.3 Test Signal Detection (VOTE 0)

**WWV Test Signal (Minute 8)**:
- 300-1500 Hz linear frequency sweep
- Detected via matched filter (chirp correlation)
- Provides absolute station identification

**WWVH Test Signal (Minute 44)**:
- 1200 Hz continuous tone (same as timing marker)
- Distinguished by duration (full minute vs. 5ms tick)

### 3.3 Ground Truth Methods

#### 3.3.1 440 Hz Tone Detection (VOTE 1)
Minutes 1 and 2 provide partial ground truth via 440 Hz tone presence/absence.

#### 3.3.2 500/600 Hz Exclusive Tone Detection (VOTE 5)
During exclusive broadcast minutes, detection of 500 or 600 Hz tone provides **definitive** station identification:

```python
WWV_ONLY_MINUTES = {1, 16, 17, 19}
WWVH_ONLY_MINUTES = {2, 43, 44, 45, 46, 47, 48, 49, 50, 51}
```

**Weight**: 10.0 (highest tier - ground truth)

### 3.4 Cross-Validation Methods

#### 3.4.1 Differential Doppler Voting (VOTE 6)
Different ionospheric paths produce different Doppler shifts:

```
ΔfD = fD_WWV - fD_WWVH
```

**Validation Logic**:
- Significant ΔfD (> 0.05 Hz) indicates distinct paths
- Lower Doppler standard deviation indicates stronger/cleaner signal
- Cross-validate with power ratio for consistency

#### 3.4.2 Test Signal ToA vs BCD ToA Coherence (VOTE 7)
In minutes 8 and 44, both the test signal and BCD are modulated simultaneously:

- Test signal matched filter provides superior timing resolution
- BCD correlation provides dual-peak detection
- If ToA offset < 5ms, boost confidence in station identification

#### 3.4.3 Harmonic Power Ratio (VOTE 8)
Exploits the harmonic relationship between 500/600 Hz tones and 1000/1200 Hz markers:

```
Harmonic Ratio 500→1000 = P_1000Hz / P_500Hz (dB)
Harmonic Ratio 600→1200 = P_1200Hz / P_600Hz (dB)
```

**Logic**:
- Higher 500→1000 ratio when WWV 500 Hz is strong
- Higher 600→1200 ratio when WWVH 600 Hz is strong
- Difference > 3 dB triggers confirmatory vote

---

## 4. Voting Architecture

### 4.1 Weight Assignment

Weights are dynamically adjusted based on minute-specific reliability:

| Minute Type | Primary Method | Reduced Methods |
|-------------|----------------|-----------------|
| Test Signal (8, 44) | Test Signal (15.0) | Carrier (2.0) |
| 440 Hz (1, 2) | 440 Hz (10.0), 500/600 GT (10.0) | Carrier (1.0) |
| BCD (0, 8-10, 29-30) | BCD (10.0) | Tick (5.0), Carrier (2.0) |
| 500/600 GT minutes | 500/600 Hz (10.0) | Carrier (5.0) |
| Standard | Carrier (10.0) | Tick (5.0), BCD (2.0) |

### 4.2 Decision Algorithm

```python
# Normalize scores
wwv_norm = wwv_score / total_weight
wwvh_norm = wwvh_score / total_weight

# Determine dominant station
if abs(wwv_norm - wwvh_norm) < 0.15:
    dominant_station = 'BALANCED'
    confidence = 'medium'
elif wwv_norm > wwvh_norm:
    dominant_station = 'WWV'
    margin = wwv_norm - wwvh_norm
    confidence = 'high' if margin > 0.7 else 'medium' if margin > 0.4 else 'low'
else:
    dominant_station = 'WWVH'
    margin = wwvh_norm - wwv_norm
    confidence = 'high' if margin > 0.7 else 'medium' if margin > 0.4 else 'low'
```

### 4.3 Confidence Levels

| Confidence | Score Margin | Interpretation |
|------------|--------------|----------------|
| **high** | > 0.7 | Strong agreement across methods |
| **medium** | 0.4 - 0.7 | Moderate agreement, some ambiguity |
| **low** | < 0.4 | Weak discrimination, possible dual reception |

---

## 5. Output Data Format

### 5.1 CSV Columns (31 fields)

```
timestamp_utc, minute_timestamp, minute_number,
wwv_detected, wwvh_detected, wwv_power_db, wwvh_power_db,
power_ratio_db, differential_delay_ms,
tone_440hz_wwv_detected, tone_440hz_wwv_power_db,
tone_440hz_wwvh_detected, tone_440hz_wwvh_power_db,
dominant_station, confidence, tick_windows_10sec,
bcd_wwv_amplitude, bcd_wwvh_amplitude, bcd_differential_delay_ms,
bcd_correlation_quality, bcd_windows,
tone_500_600_detected, tone_500_600_power_db, tone_500_600_freq_hz,
tone_500_600_ground_truth_station,
harmonic_ratio_500_1000, harmonic_ratio_600_1200,
bcd_minute_validated, bcd_correlation_peak_quality,
inter_method_agreements, inter_method_disagreements
```

### 5.2 Key Metrics

| Metric | Description | Units |
|--------|-------------|-------|
| `power_ratio_db` | P_1000Hz - P_1200Hz | dB |
| `differential_delay_ms` | ToA_WWVH - ToA_WWV | ms |
| `bcd_correlation_quality` | Peak sharpness metric | 0-1 |
| `harmonic_ratio_*` | 2nd harmonic / fundamental | dB |

---

## 6. Statistical Framework and Limitations

### 6.1 Detection Probability Framework

Each discrimination method operates as a binary classifier with associated **Probability of Detection (Pd)** and **Probability of False Alarm (Pfa)**. The performance is SNR-dependent:

| Method | Effective Bandwidth | Min SNR for Pd>0.9 | Notes |
|--------|--------------------|--------------------|-------|
| Timing Tone Power | ~50 Hz (matched filter) | >6 dB | Selective fading affects 800ms window |
| BCD Correlation | ~10 Hz per tone | >10 dB | Requires multi-second integration |
| 440 Hz Detection | ~20 Hz | >8 dB | Ground truth minute only |
| Test Signal (sweep) | ~1200 Hz | >3 dB | Chirp correlation provides processing gain |
| Harmonic Ratio | ~50 Hz | >12 dB | Requires clean fundamental detection |

**Confidence scores (0-1) approximate detection probability**, but formal ROC characterization remains future work.

### 6.2 Propagation Exceptions

The geographic predictor assumes **single-hop F-layer dominance**, which fails in several scenarios:

#### Skip Zone / MUF Limitations
At 15 MHz, WWVH (~3800 km from typical US receiver) may be **stronger than WWV (~1500 km)** when:
- MUF < 15 MHz puts WWV in skip zone
- WWV arrives via weak two-hop sidescatter (~3000+ km path)
- WWVH has single-hop path below MUF

#### Multi-hop and Sidescatter
The system detects propagation modes (1F, 2F, 3F) but **sidescatter paths are not modeled**. When detected ToA differs significantly from predicted, a "propagation event" is logged.

#### Frequency-Dependent Assumptions
- **2.5 MHz**: Nighttime only; often has single-station reception
- **20/25 MHz**: Daytime only; high MUF required for both stations
- **5/10/15 MHz**: Most likely to have dual-station interference

### 6.3 Harmonic Signature Limitations

The harmonic signature method relies on **transmitter PA nonlinearity**, not receiver nonlinearity. After ionospheric propagation, harmonic patterns may be distorted by:
- Frequency-selective fading
- Mode interference
- Doppler spreading

This method has low weight (1.5 votes) for good reason.

---

## 7. Future Enhancements

### 6.1 Planned Improvements
- **Adaptive thresholds**: SNR-dependent detection thresholds
- **Machine learning classifier**: Train on ground truth minutes
- **Ionospheric model integration**: Expected ToA from ray tracing

### 6.2 Research Opportunities
- **Dual-station simultaneous reception**: Characterize interference patterns
- **Doppler spread analysis**: Ionospheric turbulence metrics
- **Cross-frequency correlation**: Multi-band discrimination consistency

---

## 7. References

1. NIST Special Publication 432: NIST Time and Frequency Services
2. ITU-R TF.768: Standard Frequencies and Time Signals
3. GRAPE Project Technical Documentation

---

*Document generated: November 2025*
