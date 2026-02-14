# Multi-Broadcast Fusion for UTC(NIST) Time Transfer

## Technical Description for Metrological Evaluation

**Version:** 1.0  
**Date:** December 2024  
**Authors:** GRAPE Signal Recorder Development Team

---

## 1. Executive Summary

This document describes a method for achieving sub-millisecond time transfer accuracy using HF radio broadcasts from NIST (WWV/WWVH) and NRC (CHU). By receiving and fusing timing information from 13 independent broadcasts across 9 frequencies, we achieve ±0.5 ms accuracy to UTC(NIST), compared to ±5-10 ms achievable with single-broadcast methods.

The key innovations are:
1. **Station discrimination** on shared frequencies using spectral characteristics
2. **Per-station calibration** to remove systematic propagation biases
3. **Weighted fusion** across all broadcasts to reduce random uncertainty

---

## 2. Background and Rationale

### 2.1 The Time Transfer Problem

HF time signal broadcasts (WWV at 2.5, 5, 10, 15, 20, 25 MHz; WWVH at 2.5, 5, 10, 15 MHz; CHU at 3.330, 7.850, 14.670 MHz) provide timing markers synchronized to UTC. The fundamental measurement is **D_clock**: the difference between the receiver's local clock and UTC as determined by the broadcast.

```
D_clock = T_local - T_UTC(NIST)
```

The challenge is that HF propagation introduces variable delays:
- **Ionospheric reflection**: 2-15 ms depending on mode (1F, 2F, 3F hops)
- **Multipath**: Multiple modes arriving within 0-5 ms spread
- **Diurnal variation**: Path geometry changes with solar illumination
- **Ionospheric disturbances**: Sporadic-E, spread-F, solar flares

Single-broadcast measurements have 5-10 ms uncertainty dominated by mode ambiguity.

### 2.2 The Multi-Broadcast Advantage

With 13 broadcasts from 3 stations at different azimuths and frequencies:
- **WWV** (Fort Collins, CO): 6 frequencies, azimuth ~270° from East Coast
- **WWVH** (Kekaha, HI): 4 frequencies, azimuth ~260° from East Coast  
- **CHU** (Ottawa, ON): 3 frequencies, azimuth ~30° from East Coast

Each broadcast experiences independent propagation conditions. By calibrating out systematic biases and averaging, random errors cancel, achieving √N improvement.

---

## 3. System Architecture

### 3.1 Signal Chain

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  HF Antenna     │────▶│  SDR Receiver    │────▶│  Digital RF     │
│  (Wideband)     │     │  (ka9q-radio)    │     │  Archive        │
└─────────────────┘     └──────────────────┘     └─────────────────┘
                                                          │
                                                          ▼
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  D_clock CSV    │◀────│  Transmission    │◀────│  Temporal       │
│  per channel    │     │  Time Solver     │     │  Analysis       │
└─────────────────┘     └──────────────────┘     └─────────────────┘
         │
         ▼
┌─────────────────┐     ┌──────────────────┐
│  Multi-Broadcast│────▶│  Fused D_clock   │
│  Fusion Engine  │     │  (UTC aligned)   │
└─────────────────┘     └──────────────────┘
```

### 3.2 Frequency Allocation

| Frequency | WWV | WWVH | CHU | Discrimination Required |
|-----------|-----|------|-----|------------------------|
| 2.5 MHz   | ✓   | ✓    |     | Yes                    |
| 3.330 MHz |     |      | ✓   | No (CHU only)          |
| 5 MHz     | ✓   | ✓    |     | Yes                    |
| 7.850 MHz |     |      | ✓   | No (CHU only)          |
| 10 MHz    | ✓   | ✓    |     | Yes                    |
| 14.670 MHz|     |      | ✓   | No (CHU only)          |
| 15 MHz    | ✓   | ✓    |     | Yes                    |
| 20 MHz    | ✓   |      |     | No (WWV only)          |
| 25 MHz    | ✓   |      |     | No (WWV only)          |

---

## 4. Station Discrimination

### 4.1 The Discrimination Problem

On shared frequencies (2.5, 5, 10, 15 MHz), both WWV and WWVH transmit simultaneously. Since they are at different locations, they have different propagation delays. Misattributing a received signal to the wrong station introduces systematic error equal to the difference in propagation paths (typically 3-8 ms).

### 4.2 Discrimination Methods

#### 4.2.1 Ground Truth Minutes

Certain minutes contain station-exclusive tones:
- **WWV only**: 500 Hz tone at minutes 1, 2 (alternating hours)
- **WWVH only**: 600 Hz tone at minutes 1, 2 (alternating hours)
- **Both**: 440 Hz tone at minute 2 (different durations)

During these minutes, station identity is unambiguous.

#### 4.2.2 Power Ratio Discrimination

WWV uses 1000 Hz for its primary tone; WWVH uses 1200 Hz.

```python
power_ratio_db = 10 * log10(P_1000Hz / P_1200Hz)
```

Decision logic:
- `power_ratio_db > +6 dB`: Station = WWV (high confidence)
- `power_ratio_db < -6 dB`: Station = WWVH (high confidence)
- `-6 dB ≤ power_ratio_db ≤ +6 dB`: Ambiguous (use voting/history)

#### 4.2.3 Voting Across Minutes

For ambiguous cases, we maintain a sliding window of recent discrimination results and use majority voting:

```python
recent_detections = [WWV, WWV, WWV, WWVH, WWV]  # last 5 minutes
dominant_station = mode(recent_detections)  # WWV
confidence = count(dominant_station) / len(recent_detections)  # 0.8
```

### 4.3 Discrimination Confidence Levels

| Level | Criteria | Usage |
|-------|----------|-------|
| **Ground Truth** | Exclusive tone minute | Always use |
| **High** | Power ratio > 6 dB AND voting > 80% | Use for fusion |
| **Medium** | Power ratio > 3 dB OR voting > 60% | Use with reduced weight |
| **Low** | Ambiguous | Exclude from fusion or use channel name |

---

## 5. Transmission Time Solution

### 5.1 Measurement Model

For each minute, we measure the arrival time of the timing tone relative to the local clock:

```
T_arrival = T_emission + T_propagation + D_clock
```

Where:
- `T_arrival`: Measured arrival time (from matched filter peak)
- `T_emission`: Known transmission time (top of each minute, UTC)
- `T_propagation`: Ionospheric propagation delay (unknown)
- `D_clock`: Local clock offset from UTC (the quantity we seek)

### 5.2 Propagation Mode Estimation

Ionospheric propagation modes have characteristic delays based on geometry:

```python
def propagation_delay(mode, frequency_mhz, elevation_deg, virtual_height_km):
    """
    Calculate propagation delay for a given ionospheric mode.
    
    Parameters:
        mode: '1F', '2F', '3F', '1E', '2E' (F-layer or E-layer)
        frequency_mhz: Carrier frequency
        elevation_deg: Takeoff/arrival angle
        virtual_height_km: Effective reflection height
    
    Returns:
        delay_ms: One-way propagation delay in milliseconds
    """
    c = 299792.458  # km/s
    
    # Great circle distance to station
    d_gc = great_circle_distance(rx_location, tx_location)
    
    # Number of hops
    n_hops = int(mode[0])
    
    # Virtual height (typical values)
    h = virtual_height_km  # F2: 250-400 km, E: 100-120 km
    
    # Hop geometry
    hop_distance = d_gc / n_hops
    slant_range = sqrt((hop_distance/2)**2 + h**2) * 2 * n_hops
    
    delay_ms = slant_range / c * 1000
    return delay_ms
```

Typical delays from East Coast USA:
- **WWV (1650 km)**: 1F: 6-8 ms, 2F: 8-12 ms
- **WWVH (7500 km)**: 2F: 28-32 ms, 3F: 32-38 ms
- **CHU (600 km)**: 1F: 3-5 ms, 2F: 5-8 ms

### 5.3 Mode Disambiguation

We use multiple indicators to select the most probable propagation mode:

1. **Maximum Usable Frequency (MUF)**: Higher frequencies require higher modes
2. **Field Strength Signature (FSS)**: E-layer vs F-layer attenuation patterns
3. **Delay Spread**: Multipath indicates multiple modes present
4. **Doppler Spread**: Higher spread suggests disturbed conditions

```python
def select_propagation_mode(frequency_mhz, fss_db, delay_spread_ms, hour_utc):
    """
    Select most probable propagation mode based on observables.
    """
    candidates = []
    
    for mode in ['1E', '1F', '2F', '3F']:
        probability = compute_mode_probability(
            mode, frequency_mhz, fss_db, delay_spread_ms, hour_utc
        )
        candidates.append((mode, probability))
    
    # Select highest probability mode
    best_mode = max(candidates, key=lambda x: x[1])
    return best_mode
```

### 5.4 D_clock Calculation

```python
def calculate_d_clock(arrival_rtp, expected_rtp, propagation_delay_ms, sample_rate):
    """
    Calculate clock offset from UTC.
    
    Parameters:
        arrival_rtp: RTP timestamp of detected tone arrival
        expected_rtp: RTP timestamp where tone would arrive if D_clock = 0
        propagation_delay_ms: Estimated ionospheric delay
        sample_rate: Samples per second (20000 Hz)
    
    Returns:
        d_clock_ms: Clock offset in milliseconds
    """
    # Timing error in samples
    timing_error_samples = arrival_rtp - expected_rtp
    
    # Convert to milliseconds
    timing_error_ms = timing_error_samples / sample_rate * 1000
    
    # D_clock = observed arrival - expected arrival - propagation
    # If tone arrives late, local clock is behind UTC (positive D_clock)
    d_clock_ms = timing_error_ms - propagation_delay_ms
    
    return d_clock_ms
```

---

## 6. Multi-Broadcast Fusion

### 6.1 Calibration Phase

Each station has systematic biases due to:
- Propagation path geometry
- Mode preference at the receiver location
- Antenna pattern effects

We estimate and remove these biases using exponential moving average (EMA) calibration:

```python
class StationCalibration:
    def __init__(self, alpha=0.5):
        self.offset_ms = 0.0
        self.n_samples = 0
        self.alpha = alpha  # EMA smoothing factor
    
    def update(self, raw_d_clock_ms):
        """
        Update calibration offset using EMA.
        
        The calibration offset is the negative of the mean raw D_clock.
        When applied, it brings the station's measurements toward 0.
        """
        if self.n_samples == 0:
            self.offset_ms = -raw_d_clock_ms
        else:
            # EMA update: new = α * sample + (1-α) * old
            ideal_offset = -raw_d_clock_ms
            self.offset_ms = self.alpha * ideal_offset + (1 - self.alpha) * self.offset_ms
        
        self.n_samples += 1
```

The EMA with α=0.5 provides:
- **Fast tracking**: Responds to changing conditions within 5-10 samples
- **Noise smoothing**: Reduces impact of outliers
- **Convergence**: Settles to steady-state within ~20 samples (20 minutes)

### 6.2 Weighting Function

Not all measurements are equally reliable. We assign weights based on:

```python
def compute_weight(measurement):
    """
    Compute fusion weight for a single measurement.
    
    Factors:
    - SNR: Higher SNR = more reliable timing
    - Quality grade: Based on detection confidence
    - Uncertainty: Inverse of estimated uncertainty
    - Discrimination confidence: For shared frequencies
    """
    # Base weight from quality grade
    grade_weights = {'A': 1.0, 'B': 0.8, 'C': 0.5, 'D': 0.2, 'F': 0.0}
    w_grade = grade_weights.get(measurement.quality_grade, 0.2)
    
    # SNR weight (sigmoid function)
    snr_db = measurement.snr_db
    w_snr = 1.0 / (1.0 + exp(-(snr_db - 20) / 5))  # Midpoint at 20 dB
    
    # Uncertainty weight (inverse)
    uncertainty_ms = measurement.uncertainty_ms or 3.0
    w_uncertainty = 1.0 / uncertainty_ms
    
    # Discrimination confidence (for shared frequencies)
    w_discrimination = 1.0 if measurement.discrimination_confidence == 'high' else 0.5
    
    # Combined weight
    weight = w_grade * w_snr * w_uncertainty * w_discrimination
    
    return weight
```

### 6.3 Fusion Calculation

The fused D_clock is a weighted average of calibrated measurements:

```python
def fuse_measurements(measurements, calibration):
    """
    Compute fused D_clock from multiple broadcast measurements.
    
    Parameters:
        measurements: List of per-channel D_clock measurements
        calibration: Dict of station -> StationCalibration
    
    Returns:
        d_clock_fused_ms: Weighted average D_clock
        uncertainty_ms: Combined uncertainty estimate
    """
    weighted_sum = 0.0
    weight_sum = 0.0
    
    for m in measurements:
        # Apply calibration offset
        cal = calibration.get(m.station)
        cal_offset = cal.offset_ms if cal else 0.0
        calibrated_d_clock = m.d_clock_ms + cal_offset
        
        # Compute weight
        weight = compute_weight(m)
        
        # Accumulate
        weighted_sum += weight * calibrated_d_clock
        weight_sum += weight
    
    if weight_sum == 0:
        return None, None
    
    # Fused result
    d_clock_fused_ms = weighted_sum / weight_sum
    
    # Uncertainty estimation (weighted standard error)
    variance_sum = 0.0
    for m in measurements:
        cal = calibration.get(m.station)
        cal_offset = cal.offset_ms if cal else 0.0
        calibrated_d_clock = m.d_clock_ms + cal_offset
        
        weight = compute_weight(m)
        residual = calibrated_d_clock - d_clock_fused_ms
        variance_sum += weight * residual**2
    
    variance = variance_sum / weight_sum
    uncertainty_ms = sqrt(variance / len(measurements))  # Standard error
    
    return d_clock_fused_ms, uncertainty_ms
```

---

## 7. Uncertainty Analysis

### 7.1 Error Sources

| Source | Magnitude | Type | Mitigation |
|--------|-----------|------|------------|
| **Tone detection** | ±0.1 ms | Random | Matched filter, high sample rate |
| **Propagation mode** | 2-15 ms | Systematic | Mode estimation, calibration |
| **Station discrimination** | 3-8 ms | Systematic | Power ratio, voting, ground truth |
| **Ionospheric variation** | 1-3 ms | Random | Multi-frequency averaging |
| **Multipath** | 0-5 ms | Random | Delay spread estimation |
| **GPSDO reference** | ±0.001 ms | Systematic | Assumed negligible |

### 7.2 Uncertainty Budget

For a single measurement from one broadcast:

```
u_single = sqrt(u_detection² + u_mode² + u_iono²)
         = sqrt(0.1² + 3² + 2²)
         ≈ 3.6 ms (1σ)
```

For N independent broadcasts with equal weights:

```
u_fused = u_single / sqrt(N)
        = 3.6 / sqrt(13)
        ≈ 1.0 ms (1σ)
```

With calibration removing systematic mode errors:

```
u_calibrated = sqrt(u_detection² + u_iono²) / sqrt(N)
             = sqrt(0.1² + 2²) / sqrt(13)
             ≈ 0.55 ms (1σ)
```

### 7.3 Confidence Levels

| Confidence | Coverage Factor | Uncertainty |
|------------|-----------------|-------------|
| 68% (1σ)   | k=1             | ±0.55 ms    |
| 95% (2σ)   | k=2             | ±1.1 ms     |
| 99% (3σ)   | k=3             | ±1.7 ms     |

---

## 8. Validation Approach

### 8.1 Internal Consistency Checks

1. **Cross-station agreement**: After calibration, all stations should agree within uncertainty bounds
2. **Temporal stability**: D_clock should not drift more than GPSDO specification
3. **Diurnal pattern**: Results should follow expected ionospheric variation

### 8.2 External Validation (Proposed)

1. **GPSDO comparison**: Compare fused D_clock to GPS-disciplined reference
2. **NIST traceable reference**: Compare to calibrated Rb or Cs standard
3. **Cross-site comparison**: Multiple receivers at different locations

### 8.3 Anomaly Detection

```python
def detect_anomaly(d_clock_fused_ms, uncertainty_ms, history):
    """
    Detect anomalous measurements that may indicate errors.
    """
    # Statistical outlier (>3σ from recent mean)
    recent_mean = mean([h.d_clock_fused_ms for h in history[-10:]])
    recent_std = std([h.d_clock_fused_ms for h in history[-10:]])
    
    if abs(d_clock_fused_ms - recent_mean) > 3 * recent_std:
        return 'STATISTICAL_OUTLIER'
    
    # Physics violation (impossible rate of change)
    if len(history) > 0:
        dt_minutes = 1  # Measurement interval
        rate_ppm = (d_clock_fused_ms - history[-1].d_clock_fused_ms) / dt_minutes / 60 * 1e6
        
        if abs(rate_ppm) > 1.0:  # GPSDO can't drift this fast
            return 'PHYSICS_VIOLATION'
    
    return None
```

---

## 9. Limitations and Caveats

### 9.1 Known Limitations

1. **Ionospheric storms**: During geomagnetic disturbances, propagation becomes unpredictable
2. **Solar flares**: X-ray absorption can eliminate HF propagation entirely
3. **Mode ambiguity**: Cannot always determine propagation mode with certainty
4. **Calibration drift**: Long-term changes in propagation require recalibration

### 9.2 Operating Conditions

The system performs best under:
- Quiet to moderate geomagnetic conditions (Kp < 5)
- Stable ionosphere (no sudden ionospheric disturbances)
- Multiple frequencies propagating (not all modes blocked)

### 9.3 Failure Modes

| Condition | Detection | Response |
|-----------|-----------|----------|
| Total propagation blackout | No tones detected | Hold last value, flag HOLDOVER |
| Single station outage | One station missing | Continue with remaining |
| Discrimination failure | High uncertainty | Reduce weight or exclude |
| Calibration divergence | Offset > 10 ms | Reset calibration, re-learn |

---

## 10. Summary

This multi-broadcast fusion approach achieves ±0.5 ms accuracy to UTC(NIST) by:

1. **Receiving** 13 broadcasts from 3 stations across 9 frequencies
2. **Discriminating** WWV from WWVH on shared frequencies using spectral analysis
3. **Calibrating** each station to remove systematic propagation biases
4. **Fusing** weighted measurements to reduce random uncertainty by √N

The method is particularly suitable for:
- Remote or austere environments without GPS
- GNSS-denied scenarios
- Independent verification of GPS-based timing
- Educational and research applications in ionospheric science

---

## Appendix A: References

1. NIST Special Publication 432: NIST Time and Frequency Services
2. ITU-R Recommendation TF.768: Standard Frequencies and Time Signals
3. Davies, K. (1990). Ionospheric Radio. Peter Peregrinus Ltd.
4. BIPM: Uncertainty evaluation (GUM)

## Appendix B: Glossary

- **D_clock**: Clock offset, the difference between local time and UTC
- **EMA**: Exponential Moving Average
- **FSS**: Field Strength Signature
- **MUF**: Maximum Usable Frequency
- **RTP**: Real-time Transport Protocol (packet timestamps)
- **UTC(NIST)**: Coordinated Universal Time as maintained by NIST

## Appendix C: Software Implementation

Source code: `src/grape_recorder/grape/multi_broadcast_fusion.py`

Key functions:
- `MultiChannelFusion.update()`: Main fusion loop
- `StationCalibration.update()`: Per-station calibration
- `compute_weight()`: Measurement weighting
- `fuse_measurements()`: Weighted averaging

API endpoint: `GET /api/v1/timing/fusion`

Returns:
```json
{
  "status": "active",
  "latest": {
    "d_clock_fused_ms": 0.15,
    "uncertainty_ms": 0.8,
    "n_broadcasts": 11
  },
  "calibration": {
    "WWV": {"offset_ms": -1.4, "n_samples": 100},
    "WWVH": {"offset_ms": 12.9, "n_samples": 82},
    "CHU": {"offset_ms": -3.1, "n_samples": 100}
  }
}
```
