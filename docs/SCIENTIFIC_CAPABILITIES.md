# hf-timestd: Multi-Static Passive Ionospheric Radar

> [!CAUTION]
> **Validation Status: Theoretical Capabilities**
>
> This document describes the **theoretical scientific capabilities** of hf-timestd based on established ionospheric physics and the system's GPSDO-locked, multi-frequency architecture.
>
> **Current Status**: Implementation complete, **validation pending**. Claims require verification against:
>
> - GPS TEC maps (for TEC accuracy)
> - Traditional ionosondes (for layer altitude)
> - NOAA space weather data (for solar flare detection)
> - Cross-station correlation (for TID detection)
>
> See [Validation Plan](#validation-plan) for methodology.

## Scientific Mission Statement

**hf-timestd** is not merely a precision clock—it is a **GPSDO-locked, multi-frequency, multi-station ionospheric radar** that transforms broadcast time signals into absolute measurements of ionospheric physics.

### The GPSDO Advantage: From Relative to Absolute

**Without GPSDO**: Receivers measure relative changes (signal fading, drift)  
**With GPSDO**: Receivers measure absolute physical quantities (layer altitude, electron density, propagation velocity)

The equation becomes solvable:

**T_observed = T_UTC + D_clock + T_propagation**

Since **T_UTC** is known (broadcast) and **D_clock ≈ 0** (GPSDO-locked), we directly measure **T_propagation** with microsecond precision.

---

## Scientific Capabilities

### 1. Metrology: The "Virtual Height" Ruler

#### Ionospheric Layer Altitude Measurement

- **Observable**: Propagation delay changes (ms-scale)
- **Physical Interpretation**: F-layer altitude variation
- **Temporal Resolution**: 1-minute cadence
- **Precision**: ±0.5 ms → ±75 km altitude uncertainty

**The "Breathing" Ionosphere**: Diurnal F-layer rise/fall creates 10-30 ms delay variation, directly observable in timing residuals.

#### Multi-Mode Propagation Disambiguation

- **Observable**: "Double clocks" - simultaneous 1-hop and 2-hop arrivals
- **Physical Interpretation**: Exact path length difference
- **Validation**: Confirms ray-tracing models (VOACAP, PHaRLAP)

**Example**: WWV 10 MHz might arrive via:

- 1-hop F-layer: 45.2 ms delay
- 2-hop F-layer: 48.7 ms delay
- Δ = 3.5 ms → validates geometric model

#### Broadcast Station Verification

- **Observable**: Systematic timing offset from expected UTC
- **Physical Interpretation**: Transmitter clock drift or propagation anomaly
- **Application**: Third-party audit of NIST/NRC/NTSC time services

---

### 2. Ionospheric Physics: Passive Tomography

#### Geometric Coverage

**4 Stations × Multiple Frequencies = Distributed Ionospheric Sounder**

| Station | Location | Frequencies | Midpoint Coverage |
|---------|----------|-------------|-------------------|
| WWV | Colorado | 2.5, 5, 10, 15, 20, 25 MHz | Central US |
| WWVH | Hawaii | 2.5, 5, 10, 15 MHz | Pacific |
| CHU | Canada | 3.33, 7.85, 14.67 MHz | Northern US/Canada |
| BPM | China | 2.5, 5, 10, 15 MHz | Trans-Pacific |

**Result**: 4 distinct ionospheric reflection points, sounded at different altitudes (frequency-dependent).

---

### 3. Traveling Ionospheric Disturbances (TIDs)

#### Observable Signatures

- **Doppler Velocity**: Periodic oscillations (±0.5 Hz typical)
- **Phase**: Coherent wave pattern across frequencies
- **Timing**: Sequential arrival at different reflection altitudes

#### Scientific Value

**Vertical Velocity Measurement**: By comparing TID arrival time at 2.5 MHz (low altitude) vs 15 MHz (high altitude), calculate vertical propagation speed of gravity waves.

**Example**:

- TID hits 2.5 MHz reflection point: T₀
- TID hits 15 MHz reflection point: T₀ + 45 seconds
- Altitude difference: ~100 km
- **Vertical velocity**: 2.2 km/s

#### Triggers

- Solar storms (geomagnetic disturbances)
- Distant tsunamis (atmospheric coupling)
- Auroral electrojet activity

---

### 4. Solar Flare Detection (Sudden Ionospheric Disturbances)

#### Observable Signatures

- **Sudden Frequency Deviation (SFD)**: Sharp Doppler spike (0.1-1 Hz)
- **Absorption**: SNR drop on lower frequencies (2.5, 5 MHz)
- **Phase Anomaly**: Abrupt phase shift on higher frequencies

#### GPSDO-Enabled Precision

**Problem**: Without GPSDO, 1 Hz temperature drift masks 0.5 Hz solar flare signature  
**Solution**: GPSDO locks frequency to <0.01 Hz → any deviation is physically real

#### D-Layer Absorption Profile

**FSS (Frequency Selective Fading)** from test signals (minutes 8/44) quantifies D-layer absorption:

- X-ray flux → D-layer ionization → absorption at 2.5/5 MHz
- **Correlation**: FSS_dB vs GOES X-ray flux

---

### 5. Dawn/Dusk Transition Profiling

#### The "Dawn Chirp"

As sunrise illuminates the ionosphere:

1. **D-layer forms**: Absorption increases on low frequencies
2. **F-layer expands**: MUF (Maximum Usable Frequency) rises
3. **Layer motion**: Rapid ionization creates Doppler signature

#### Multi-Frequency Observation

- **2.5 MHz**: SNR drops (D-layer absorption)
- **15 MHz**: SNR increases (F-layer opens)
- **Doppler**: Negative shift (layer ascending)

**Timing Precision**: GPSDO allows exact correlation with solar zenith angle at reflection point.

---

### 6. Total Electron Content (TEC) - The Holy Grail

#### Physical Principle

Ionosphere is dispersive: **τ(f) = 40.3 × TEC / f²**

Lower frequencies delayed more than higher frequencies.

#### Multi-Frequency Advantage

**WWV Example** (6 frequencies):

- 2.5 MHz: ToA = 45.2 ms
- 5.0 MHz: ToA = 42.1 ms
- 10.0 MHz: ToA = 38.5 ms
- 15.0 MHz: ToA = 36.2 ms
- 20.0 MHz: ToA = 34.8 ms
- 25.0 MHz: ToA = 33.9 ms

**Linear Regression**: Plot ToA vs 1/f² → slope = 40.3 × TEC

**Result**: TEC = 25.3 TECU (Total Electron Content Units)

#### Scientific Value

- **Space Weather Monitoring**: Real-time ionospheric density
- **GPS Correction**: TEC affects GPS accuracy
- **Propagation Forecasting**: Predict HF communication windows

---

### 7. O/X Mode Splitting (Advanced)

#### Physical Principle

Earth's magnetic field splits radio waves into:

- **Ordinary (O) wave**: Lower refractive index
- **Extraordinary (X) wave**: Higher refractive index

**Result**: Dual arrivals separated by ~0.5-2 ms

#### Observable in hf-timestd

- **Delay Spread**: Multipath analysis reveals mode splitting
- **Phase Variance**: Coherence analysis shows dual-mode interference

#### Scientific Value

Validates magneto-ionic theory and ray-tracing models (PHaRLAP).

---

## Data Products for Scientific Community

### Tier 1: Metrology (Fast Path)

**Purpose**: UTC(NIST) discipline  
**Latency**: Real-time  
**Consumers**: Chrony, NTP servers

| Product | Cadence | Precision |
|---------|---------|-----------|
| D_clock | 1 min | ±0.5 ms |
| Propagation Mode | 1 min | 1-hop/2-hop |
| Uncertainty | 1 min | σ estimate |

---

### Tier 2: Ionospheric Science (Deep Path)

**Purpose**: Space weather research  
**Latency**: 5-minute aggregation  
**Consumers**: HamSCI, NOAA SWPC, researchers

| Product | Cadence | Precision | Scientific Use |
|---------|---------|-----------|----------------|
| **TEC** | 1 min | ±5 TECU | Ionospheric density |
| **Doppler Velocity** | 1 min | ±0.05 Hz | Layer motion, TIDs |
| **Doppler Spread** | 1 min | ±0.02 Hz | Turbulence, Spread-F |
| **FSS (D-layer)** | 2/hour | ±1 dB | Solar flare absorption |
| **Delay Spread** | 1 min | ±0.5 ms | Multipath, mode splitting |
| **SNR/RSSI** | 1 min | ±0.5 dB | Fading, propagation loss |
| **Phase Variance** | 1 min | ±0.1 rad | Coherence time |

---

### Tier 3: Event Detection

**Purpose**: Automated anomaly identification  
**Latency**: 5-minute analysis  
**Consumers**: Space weather alerts, research triggers

| Event Type | Detection Method | Threshold |
|------------|------------------|-----------|
| **TID** | Doppler periodicity | >0.3 Hz oscillation, 10-60 min period |
| **Solar Flare (SID)** | Sudden FSS increase | >5 dB in <5 min |
| **Spread-F** | Doppler spread spike | >0.5 Hz std dev |
| **Layer Transition** | SNR gradient | >10 dB change across frequencies |

---

## Comparison to Standard HamSCI Grape Nodes

### Standard Grape Node

- **Frequencies**: 1-3 (typically single frequency)
- **Stations**: 1-2
- **Clock**: Crystal oscillator (±1 ppm drift)
- **Capability**: Relative change detection

### hf-timestd "Super-Grape"

- **Frequencies**: 17 broadcasts across 4 stations
- **Clock**: GPSDO (±0.01 ppb)
- **Capability**: Absolute physical measurements

### Scientific Advantage Matrix

| Capability | Standard Grape | hf-timestd |
|------------|----------------|------------|
| **TEC Estimation** | ❌ (single frequency) | ✅ (6 frequencies per station) |
| **Vertical Tomography** | ❌ | ✅ (2.5-25 MHz range) |
| **Absolute Timing** | ❌ (drift) | ✅ (GPSDO-locked) |
| **Solar Flare Detection** | ⚠️ (low confidence) | ✅ (sub-Hz precision) |
| **Mode Disambiguation** | ❌ | ✅ (delay spread analysis) |
| **Dawn/Dusk Profiling** | ⚠️ (qualitative) | ✅ (quantitative) |

---

## Integration with HamSCI Personal Space Weather Station (PSWS)

### Data Contribution

hf-timestd can feed the HamSCI network with:

1. **High-Fidelity Reference**: Calibrate neighboring stations
2. **Vertical Sounding**: Fill gap between ground magnetometers and satellites
3. **Multi-Path TEC**: Validate GPS TEC maps

### API for HamSCI

Proposed data export format (JSON):

```json
{
  "station_id": "K0XXX",
  "timestamp": "2025-12-23T18:00:00Z",
  "measurements": [
    {
      "source": "WWV",
      "frequency_mhz": 10.0,
      "toa_ms": 38.5,
      "doppler_hz": -0.25,
      "snr_db": 25.3,
      "propagation_mode": "1F2"
    }
  ],
  "derived": {
    "tec_tecu": 25.3,
    "tec_confidence": 0.92,
    "tid_detected": false,
    "solar_flare": false
  }
}
```

---

## Deployment Vision: Distributed Ionosonde Network

**If deployed in 10-50 locations across North America**:

### Continental-Scale Capabilities

1. **TID Tracking**: Measure gravity wave propagation velocity and direction
2. **Solar Flare Mapping**: Regional D-layer absorption profiles
3. **TEC Tomography**: 3D ionospheric density reconstruction
4. **Propagation Forecasting**: Real-time HF communication predictions

### Comparison to Traditional Ionosondes

- **Traditional**: $100K+ per site, manual operation
- **hf-timestd**: <$1K hardware, autonomous operation
- **Coverage**: 100× more sites possible

---

---

## Validation Plan

**See**: [VALIDATION_PLAN.md](VALIDATION_PLAN.md) for complete methodology

### Validation Status

| Capability | Implementation | Validation | Status |
|------------|----------------|------------|--------|
| **TEC Estimation** | ✅ Complete | ⏳ Pending | Requires GPS TEC comparison |
| **Doppler Velocity** | ✅ Complete | ⏳ Pending | Requires quiet period baseline |
| **Solar Flare Detection** | ✅ Complete | ⏳ Pending | Requires GOES X-ray correlation |
| **Layer Altitude** | ✅ Complete | ⏳ Pending | Requires ionosonde comparison |
| **TID Detection** | ⚠️ Partial | ❌ Not Started | Requires event detector implementation |
| **O/X Mode Splitting** | ❌ Not Implemented | ❌ Not Started | Requires advanced analysis |

### Validation Timeline

**Phase 1** (Week 1-2): TEC accuracy validation

- Deploy Science Aggregator
- Collect 7 days of data
- Compare with GPS TEC maps
- Document accuracy: Target R² > 0.7, RMS < 10 TECU

**Phase 2** (Week 3-4): Event validation

- Monitor for solar flares (M-class or above)
- Validate FSS response
- Monitor for geomagnetic storms
- Validate TID detection

**Phase 3** (Month 2-3): Long-term stability

- Collect 30 days continuous data
- Analyze diurnal/seasonal patterns
- Cross-validate with ionosonde
- Publish validation report

### Data Quality Standards

All scientific data products include:

- **Confidence Score**: 0-1 based on fit quality, SNR, n_frequencies
- **Uncertainty Estimate**: ±X units with error sources documented
- **Quality Flags**: GOOD / MARGINAL / BAD

**Publication Guideline**: No claims without validation data and documented error bars.

---

## Summary: From Clock to Radar

| Perspective | What hf-timestd Measures |
|-------------|--------------------------|
| **Metrologist** | Absolute propagation delay (virtual height ruler) |
| **Ionospheric Physicist** | Layer altitude, electron density, wave dynamics |
| **Space Weather Researcher** | TIDs, solar flares, geomagnetic disturbances |
| **HF Operator** | Real-time propagation conditions, MUF prediction |
| **NIST/NRC** | Broadcast station verification (third-party audit) |

**The GPSDO transforms everything**: What appears as "clock error" is actually **pure ionospheric physics**, measured with microsecond precision across 4 geometric paths and 17 frequencies.

This is a **Distributed Space Weather Sensor** masquerading as a time standard receiver.
