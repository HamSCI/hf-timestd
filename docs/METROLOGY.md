# HF-TimeStd: Metrological Description

**Prepared for:** Time metrology professionals, "time nuts", and general users  
**System Version:** 6.1.0  
**Last Updated:** January 24, 2026  
**Author:** Michael James Hauan (AC0G)

---

## 1. Executive Summary

**hf-timestd** is an HF time transfer system that achieves **±0.5 ms (1σ) accuracy to UTC(NIST)** through multi-broadcast fusion of WWV/WWVH/CHU/BPM time signals. The system demonstrates metrological rigor through:

- **ISO GUM-compliant uncertainty budgets** with full traceability
- **GPSDO-disciplined sampling** (RTP timestamps as primary reference)
- **Physics-informed propagation modeling** (IONEX VTEC + IRI-2020)
- **Kalman-filtered fusion** with inverse variance weighting
- **Chrony SHM integration** for system clock discipline

**The Core Value Proposition:** Unlike typical implementations that require expensive atomic oscillators for holdover, this system leverages the short-term frequency stability of a standard GPSDO and fuses it with multi-station HF broadcasts to synthesize a UTC-traceable time standard. In essence, it uses software complexity and ionospheric physics to upgrade a GPSDO into a precision time standard—without the cost of a Cesium beam.

This system treats timing as a **measurement problem** with proper uncertainty quantification, systematic error correction, and validation against physical constraints.

---

## 2. The Measurement Problem

### 2.1 What We Actually Measure

The system measures **D_clock**, the offset between the local system clock and UTC(NIST):

```
D_clock = T_system - T_UTC(NIST)
```

This is extracted from HF time signal broadcasts by solving the **transmission time equation**:

```
T_arrival = T_emission + τ_propagation + D_clock
```

Where:

- **T_arrival**: Measured tone arrival time (RTP timestamp from GPSDO-disciplined SDR)
- **T_emission**: Known transmission time (top of minute, UTC)
- **τ_propagation**: Ionospheric path delay (2-70 ms, station-dependent)
- **D_clock**: The unknown we solve for

### 2.2 The Central Challenge

The difficulty is that **τ_propagation is not constant**:

- Varies with ionospheric mode (1F, 2F, 3F hops)
- Changes diurnally (day/night propagation)
- Affected by space weather (geomagnetic storms, solar flares)
- Multipath creates 0-5 ms delay spread
- Different for each station/frequency combination

Single-broadcast methods achieve only **±5-10 ms** due to mode ambiguity. Our multi-broadcast fusion approach achieves **±0.5 ms** by:

1. **Calibrating** systematic propagation biases per station
2. **Fusing** 9-17 independent measurements with inverse variance weighting
3. **Validating** against physical constraints (cross-station consistency, continuity)

---

## 3. "Steel Ruler" Philosophy

### 3.1 Core Principle

The **"Steel Ruler"** philosophy asserts that a GPSDO provides a rigid "ruler" (precise tick rate/frequency), but this ruler is floating in time. The system's job is not to straighten the ruler (the GPSDO does that), but to use ionospheric physics to **pin the ruler's zero-point to UTC**.

In a GPSDO-disciplined system, the local clock is significantly more stable (sub-ppb stability) than the ionosphere (10-100 ppb equivalent jitter). Therefore, we must:

1. **Trust the local clock** (zero process noise)
2. **Attribute all residuals** to ionospheric path variation
3. **Clamp long-term drift** to 0.0, as the GPSDO prevents accumulation

**The Hardware Hierarchy:**

| Hardware | Frequency (Slope) | Time (Offset) | Cost |
|----------|-------------------|---------------|------|
| **GPSDO** | Excellent | Drifting (if undisciplined) | ~$200-500 |
| **Cesium Beam** | Excellent | Excellent | ~$30,000+ |
| **GPSDO + hf-timestd** | Excellent | ±0.5 ms | Software |

This system bridges the gap between a common GPSDO (which provides excellent *frequency*) and a laboratory Cesium Standard (which provides absolute *time*), using the ionosphere as the correction mechanism.

### 3.2 The Three-Layer Metrological Architecture

Understanding the fundamental difference between **Frequency Stability** (Slope) and **Time Accuracy** (Offset) is essential:

#### Layer 1: Single Broadcast — "The Floating Ruler"

A single broadcast (e.g., WWV 15 MHz) provides:

- **Capability**: Measures the stability of local clock's *tick rate* relative to the transmitter's *tick rate*
- **Limitation**: NOT anchored to UTC — the signal always arrives late by the propagation delay (τ)
- **Result**: If averaged for a year, you get a perfect line with the correct slope (frequency), but the line is shifted vertically by the average propagation delay (e.g., +8 ms)
- **You know**: *How fast* time is passing, but not *what time it is*

#### Layer 2: Single Station, Multiple Frequencies — "The Dispersion Anchor"

By adding multiple frequencies (e.g., WWV 5, 10, 15 MHz), you unlock the **dispersion calculation**:

- **Physics**: Lower frequencies are delayed MORE by the ionosphere than higher ones
- **Mechanism**: This difference allows calculation of **Total Electron Content (TEC)** along that specific path
- **Gain**: Once TEC is known, the ionospheric delay (τ_iono) can be calculated and subtracted
- **Result**: Characterizes the **Path Physics**, moves the "Floating Ruler" closer to the true UTC line

#### Layer 3: Multiple Stations (17 Broadcasts) — "The Geometry Lock"

By adding geography (WWV vs WWVH vs CHU vs BPM), you sound the ionosphere from different angles and reflection points:

- **Physics**: Different stations probe different ionospheric regions and paths
- **Gain**: Cancels localized anomalies ("Weather") — solar flares affect paths differently depending on sun angle
- **Result**: "Triangulates" the ionosphere globally, provides **Integrity** (validation)

### 3.3 Summary Table

| Component | Provides | Function |
|-----------|----------|----------|
| **GPSDO** | Slope (Rate) | Ensures the ruler is straight and rigid |
| **Multi-Frequency Dispersion** | Vertical Shift (Path Delay) | Calibrates the zero-point for each station |
| **Multi-Station Fusion** | Integrity (Validation) | Ensures the zero-point is consistent across the hemisphere |

**Key Insight**: The combined regression of 17 broadcasts doesn't just average noise — it **solves the geometry** of the ionosphere to find the true UTC origin point.

---

## 4. System Architecture

### 4.1 The Six Services

The system is composed of six independent systemd services:

| Service | Responsibility | Output |
|---------|---------------|--------|
| **timestd-core-recorder** | Reliable Data Capture | `/var/lib/timestd/raw_buffer/` |
| **timestd-metrology** | Signal Processing & Timing Extraction | `/var/lib/timestd/phase2/{CHANNEL}/` |
| **timestd-fusion** | Multi-Broadcast Synthesis | `/var/lib/timestd/phase2/fusion/` + Chrony SHM |
| **timestd-vtec** | Ionospheric Data Acquisition | `/var/lib/timestd/gnss_vtec.h5`, `/var/lib/timestd/ionex/` |
| **timestd-physics** | Propagation Modeling & TEC Estimation | Enriched L2 HDF5 files |
| **timestd-web-api** | User Visualization & System API | Port 8000 |

### 4.2 Three-Phase Pipeline

```
Phase 1: Core Recorder (Immutable Archive)
  ↓ Digital RF HDF5 (24 kHz IQ)
Phase 2: Analytics (Timing Extraction)
  ↓ HDF5 L2 Timing Measurements
Phase 3: Fusion (Multi-Broadcast Synthesis)
  ↓ Chrony SHM (System Clock Discipline)
```

**Metrological Advantages:**

- **Phase 1 never drops data** during Phase 2 updates
- **Reprocessability**: Improve algorithms without re-recording
- **Independent validation**: Test analytics on archived data
- **HDF5 SWMR**: Fusion reads data milliseconds after Analytics writes it

### 4.3 RTP Timestamp as Primary Reference

**Critical Design Decision:** System time is **derived from RTP timestamps**, not vice versa.

```python
utc = time_snap_utc + (rtp_ts - time_snap_rtp) / sample_rate
```

**Why this matters:**

- **Sample count integrity**: Gaps are unambiguous (RTP timestamp jumps)
- **No time stretching**: Never adjust sample count to fit wall clock
- **Traceable to GPSDO**: RTP timestamps from ka9q-radio are GPSDO-disciplined
- **Reprocessable**: Raw data can be reanalyzed with improved algorithms

### 4.4 Data Levels

| Level | Description | Content |
|-------|-------------|---------|
| **L0** | Digital RF | Raw IQ samples |
| **L1A** | Tone Detections | Channel observables, SNR, BCD |
| **L1B** | BCD Timecode | Decoded time information |
| **L2** | Timing Measurements | D_clock + ISO GUM uncertainty |
| **L3** | Fused Timing | Kalman filtered, multi-broadcast |

---

## 5. Physics Models

### 5.1 Signal Reception (9 Channels, 17 Broadcasts)

**Configuration:**

- **WWV** (Fort Collins, CO): 2.5, 5, 10, 15, 20, 25 MHz (6 frequencies)
- **WWVH** (Kauai, HI): 2.5, 5, 10, 15 MHz (4 frequencies, shared with WWV)
- **CHU** (Ottawa, Canada): 3.33, 7.85, 14.67 MHz (3 frequencies)
- **BPM** (Pucheng, China): 2.5, 5, 10, 15 MHz (4 frequencies, experimental)

**Receiver:**

- **SDR**: ka9q-radio (Phil Karn's software-defined radio)
- **Reference**: GPSDO-disciplined sampling clock
- **Sample Rate**: 24 kHz IQ per channel
- **Format**: Digital RF (HDF5, MIT Haystack standard)

### 5.2 Tone Detection (Sub-Sample Precision)

**Primary Timing Tones:**

- **WWV**: 1000 Hz (5 ms duration per second)
- **WWVH**: 1200 Hz (5 ms duration per second)
- **CHU**: 1000 Hz (300 ms duration per second)
- **BPM**: 1000 Hz (10 ms UTC, 100 ms UT1)

**Detection Method:**

- Matched filter correlation
- Sub-sample peak interpolation (parabolic fit)
- **Precision**: ±0.1 ms (tone detection uncertainty)

### 5.3 Station Discrimination (Shared Frequencies)

On 2.5, 5, 10, 15 MHz, **WWV and WWVH transmit simultaneously**. Misidentification causes **3-8 ms systematic error**.

**Discrimination Methods:**

1. **BCD Correlation (Primary)** — 100 Hz subcarrier cross-correlation
2. **Tone Power Ratio** — WWV: 1000 Hz strong; WWVH: 1200 Hz strong
3. **Station ID Tones** — WWV: 500/600 Hz; WWVH: 600/1500 Hz
4. **Cross-Frequency Guidance** — Exploits ionospheric coherence

**Validation:**

- Inter-station D_clock consistency (< 1 ms spread required)
- D_clock continuity (jumps > 5 ms flagged)
- GPSDO lock status check

### 5.4 Propagation Delay Modeling

**Tiered Hierarchy:**

| Tier | Source | Accuracy | Usage |
|------|--------|----------|-------|
| **0** | Local GNSS-VTEC | ±0.5-1 ms | Optional (dual-frequency GNSS receiver) |
| **1** | IONEX VTEC | ±1-2 ms | Production (NASA/IGS Global Ionosphere Maps) |
| **2** | IRI-2020 | ±2-5 ms | Fallback (International Reference Ionosphere) |
| **3** | Geometric/Empirical | ±5-10 ms | Last resort |

#### Local GNSS-VTEC (Optional Enhancement)

When a dual-frequency GNSS receiver (e.g., u-blox ZED-F9P) is available, the system can measure **local vertical TEC in real-time**:

- **Mechanism**: The receiver measures L1/L2 (or L1/L5) pseudorange and carrier phase differences, which directly encode the ionospheric delay along each satellite path
- **Advantage**: Provides ground-truth TEC at the receiver location with ~1-minute latency, compared to 1-2 hour latency for IONEX maps
- **Integration**: The `timestd-vtec` service polls the GNSS receiver and writes measurements to `/var/lib/timestd/gnss_vtec.h5`

**Metrological Impact:**

| Aspect | Without Local GNSS | With Local GNSS |
|--------|-------------------|-----------------|
| **TEC Latency** | 1-2 hours (IONEX) | ~1 minute |
| **Spatial Resolution** | 2.5° × 5° grid | Point measurement at receiver |
| **Ionospheric Storms** | May miss rapid changes | Tracks real-time variations |
| **Propagation Model Accuracy** | ±1-2 ms | ±0.5-1 ms |

**How It Improves Timing:**

1. **Path Midpoint Correction**: The HF signal reflects at the ionospheric layer ~350 km altitude, typically 500-1500 km from the receiver. Local GNSS-VTEC provides the "anchor" TEC value at the receiver, which is interpolated with IONEX data at the reflection point.

2. **Diurnal Gradient Tracking**: During sunrise/sunset, TEC changes rapidly. Local GNSS captures these gradients in real-time, improving propagation delay estimates during transition periods.

3. **Storm Detection**: Geomagnetic storms can cause TEC to vary by 50-200% within minutes. Local GNSS provides early warning and real-time tracking that IONEX maps cannot match.

**Configuration**: Enable via `timestd-config.toml`:

```toml
[gnss]
enabled = true
device = "/dev/ttyACM0"
baud_rate = 115200
```

**Note**: Local GNSS-VTEC is optional. The system achieves ±0.5 ms accuracy with IONEX alone under quiet conditions. Local GNSS provides marginal improvement during quiet conditions but significant improvement during disturbed conditions.

**Ionospheric Delay Equation:**

```
τ_iono = K × TEC / f²
where K = 40.3 m³/s²
```

**Propagation Delay Bounds:**

- WWV: 4-12 ms
- WWVH: 15-30 ms
- CHU: 6-15 ms
- BPM: 40-70 ms

Delays outside bounds have plausibility reduced by 70%.

### 5.5 Adaptive Search Windows

**Phase Progression:**

| Phase | Window | Criteria |
|-------|--------|----------|
| **Bootstrap** | ±500 ms | Initial, no prior knowledge |
| **Provisional** | ±5-15 ms | 10+ detections, 2+ stations, D_clock σ < 1 ms |
| **Calibrated** | ±2-5 ms | 30+ detections, 60 min span, RTP variance < 50² |

**Key Insight:** GPSDO is the foundation. Stations are periodic calibration checks, not the primary reference.

---

## 6. Uncertainty Budget (ISO GUM)

### 6.1 Multi-Broadcast Fusion

**Calibration Model:**

```python
calibration_offset = -mean(D_clock_station)
```

**Update Method:** Exponential Moving Average (α=0.5)

**Inverse Variance Weighting:**

```python
w = 1 / (uncertainty_ms²)
d_clock_fused = Σ(w_i × d_clock_i) / Σ(w_i)
```

**Why this matters:**

- **Statistically optimal** for combining independent measurements
- **ISO GUM best practice** (GUM-S1)
- Measurements with 0.5 ms uncertainty get 4x weight vs 1.0 ms uncertainty

### 6.2 Kalman Filtering

**Steel Ruler Parameters:**

| Parameter | Value | Description |
|-----------|-------|-------------|
| **Initial P (Offset)** | 5.0 ms | Moderate initial trust |
| **Initial P (Drift)** | 1e-7 ms/min | High trust in factory calibration |
| **Q (Offset)** | 1e-10 ms | Effectively zero process noise |
| **Q (Drift)** | 1e-12 ms/min | The clock does not wander |
| **R (Measurement)** | 30.0 ms | High measurement noise (ionospheric) |

**Drift Clamping:** `drift_ms_per_min` is forced to `0.0` after convergence.

### 6.3 Error Sources

**Type A (Statistical):**

- Weighted standard error of fusion
- Reduces as √N with more broadcasts

**Type B (Systematic):**

| Source | Uncertainty |
|--------|-------------|
| Tone detection | ±0.1 ms |
| Propagation model (IONEX) | ±1-2 ms |
| Propagation model (IRI) | ±2-5 ms |
| RTP jitter | ±0.1 ms |
| GPSDO stability | ±0.001 ms (negligible) |

**Combined Uncertainty (RSS):**

```
u_combined = √(u_statistical² + u_systematic² + u_propagation²)
```

### 6.4 Typical Values

| Condition | Uncertainty (1σ) |
|-----------|------------------|
| Single broadcast | ±3.6 ms |
| 13 broadcasts fused | ±1.0 ms |
| With calibration | **±0.5 ms** |

**Coverage Factors:**

- 68% (1σ): ±0.5 ms
- 95% (2σ): ±1.0 ms
- 99% (3σ): ±1.5 ms

---

## 7. Verification Procedures

### 7.1 Verify Baseline Stability

The most critical check is ensuring the `D_clock` baseline is **horizontal**, not "walking".

1. **Open Web UI:** Go to Metrology Dashboard
2. **Check Slope:** The fused `D_clock` line should be flat over 24 hours
3. **Verify Fusion Log:**

   ```bash
   journalctl -u timestd-fusion -n 50 | grep "Steel Ruler"
   # Expected Output:
   # INFO:__main__:Steel Ruler: Baseline is STABLE (drift = 0.0 ms/min)
   ```

### 7.2 Verify Latency

Ensure latency is low to allow real-time Chrony discipline.

1. **Run Verification:** `scripts/verify_pipeline.sh`
2. **Check Phase 2 Latency:** Should be < 90 seconds for active channels
3. **Check Phase 3 Latency:** Should be < 120 seconds

### 7.3 Chrony Discipline

Verify the system is actively steering the kernel clock.

```bash
chronyc tracking
# Look for:
# Ref ID        : 544D4752 (TMGR)
# Stratum       : 1 (if treating HF as primary) or >1
# Last offset   : +0.000xxxx seconds (sub-millisecond)
# RMS offset    : 0.000xxxx seconds
# Frequency     : x.xxx ppm (should be stable)
```

**Chrony SHM Configuration:**

```
refclock SHM 0 refid TMGR poll 4 precision 1e-3
```

- Fusion calculates D_clock every **8 seconds**
- Chrony polls every **16 seconds**
- Expected reach: 87.5% (7 out of 8 polls successful)

### 7.4 Ionospheric "Weather"

If `D_clock` values are jumping significantly (> 2-3 ms), this is likely ionospheric weather (storms, TIDs), not clock failure.

- **Check Propagation Analysis:** Look for similar jumps across *multiple frequencies*
- **Global Differential:** If 10 MHz and 15 MHz both jump +2ms, it's a layer height change

### 7.5 Quality Grades

| Grade | Uncertainty | Criteria |
|-------|-------------|----------|
| **A** | ±0.5 ms | 30+ detections, 60 min span, RTP variance < 50², calibrated, inter-station < 1 ms |
| **B** | ±1.0 ms | 10+ detections, provisional phase |
| **C** | ±2.0 ms | Bootstrap phase, limited validation |
| **D/F** | > 2.0 ms | Insufficient data or validation failures |

---

## 8. Troubleshooting

### 8.1 "Walking" Baseline (Non-Zero Slope)

If the baseline starts tilting:

1. **Check GPSDO Lock:** Ensure the physical clock is actually locked
2. **Force Reset:**

   ```bash
   sudo systemctl stop timestd-fusion
   # Edit /var/lib/timestd/state/broadcast_calibration.json
   # Set "drift_ms_per_min": 0.0 for all stations.
   sudo systemctl start timestd-fusion
   ```

### 8.2 "Stale TEC" Warning

If `verify_pipeline.sh` reports stale TEC:

1. **Check Night/Day:** TEC requires multi-frequency data. At night, MUF drops, and we often lose higher bands (15/20/25 MHz), making TEC calculation impossible.
2. **Action:** Wait for sunrise or check `timestd-physics` logs.

### 8.3 WWV 20/25 MHz STALE Measurements

Not a software bug—HF propagation on higher bands is poor during night/early morning. These are single-broadcast anchor channels (no WWVH overlap), critical for bootstrap. Measurements resume when propagation improves.

---

## 9. Data Products

### 9.1 Raw Archive: Digital RF (HDF5)

- **Format:** HDF5 with `drf_properties` attribute (MIT Haystack standard)
- **Structure:**
  - `/rf_data`: Dataset containing complex64 IQ samples
  - `/rf_data_index`: Index mapping sample ranges to timestamps
- **Metadata:** Global start time, sample rate (24 kHz), center frequency
- **Size:** ~2-3 GB/day/channel

### 9.2 Analytics Output: HDF5 L1/L2

- **L1A (Tone Detections):** `feature_extraction` group with raw SNR, tone power, BCD correlation
- **L2 (Timing Measurements):** `timing_solution` group with `d_clock`, `uncertainty`, `propagation_model`
- **Size:** ~50-100 MB/day

### 9.3 Fusion Output: HDF5 L3

- **Structure:**
  - `/fused_solution`: Time series of weighted mean offset
  - `/residuals`: Per-station residuals from the mean
  - `/calibration`: Current calibration state for each station
- **Chrony SHM updates** for clock discipline
- **Allan deviation tracking**

### 9.4 Science Products

- TEC estimates
- Propagation mode statistics
- Sporadic-E events
- Space weather correlations

---

## 10. Limitations and Caveats

### 10.1 Fundamental Limits

**Ionospheric Variability:**

- Propagation delay varies by ±1-3 ms over minutes
- Cannot be eliminated, only averaged
- Dominates uncertainty budget

**Mode Ambiguity:**

- Cannot always distinguish 1F vs 2F propagation
- Multipath creates 0-5 ms delay spread
- Mitigated by calibration, not eliminated

**Single Receiver:**

- No spatial resolution
- Cannot determine propagation direction
- Not suitable for ionospheric tomography

### 10.2 Operating Conditions

| Condition | Performance |
|-----------|-------------|
| Quiet to moderate (Kp < 5) | Best |
| Ionospheric storms (Kp > 5) | Degraded |
| Solar flares (X-ray absorption) | Degraded |
| Propagation blackouts | Failure (holdover mode) |

### 10.3 What This System Does NOT Do

- **Not a frequency standard:** Disciplines system clock, not a standalone oscillator
- **Not better than GPS:** GPS achieves ±10 ns; this achieves ±0.5 ms (demonstrates the capability to derive precision timing from frequency-standard hardware)
- **Not ionospheric tomography:** Single receiver, limited spatial resolution

---

## 11. Conclusion

### 11.1 What We Claim

- **±0.5 ms (1σ) to UTC(NIST)** with proper uncertainty
- **ISO GUM-compliant** methodology
- **Physics-validated** measurements
- **Production-grade** reliability

### 11.2 What We Don't Claim

- Better than GPS (we're 50,000x worse)
- Ionospheric tomography (single receiver)
- Absolute timing better than ±0.5 ms

### 11.3 The Bottom Line

This is not an amateur "time sync" project. This is an **instrument of synthetic metrology** for HF time transfer with:

- Proper uncertainty quantification
- Physics-based validation
- Systematic error correction
- Multi-broadcast redundancy
- ISO GUM compliance

**For a time nut:** This extracts maximum utility from your existing GPSDO. It transforms a device that only disciplines *frequency* into a system that disciplines *time*, providing a second, physics-based opinion on UTC that validates your GPS solution.

**For a metrologist:** This is traceable to UTC(NIST), has a complete uncertainty budget, and follows ISO GUM best practices.

**For a skeptic:** The code is open source, the physics is documented, and the limitations are honestly stated. Verify it yourself.

---

## 12. Hierarchical Estimation Architecture (v6.0)

### 12.1 Motivation: Why Hierarchical?

The original architecture (v5.x) used a **single Kalman filter at the fusion layer**. This caused:

1. **Restart variance**: The fused D_clock settled to different values on each service restart
2. **False smoothing**: Ionospheric variations were smoothed away, hiding science
3. **Single point of failure**: All state concentrated in one filter

The revised architecture (v6.0) distributes filtering to where it is **physically justified**:

| Layer | Method | Physical Justification |
|-------|--------|------------------------|
| Per-Broadcast | Kalman filter | Ionosphere has temporal continuity |
| Per-Station | TEC 1/f² fit | Multi-frequency dispersion is physics |
| Multi-Station | Weighted Least Squares | Optimal linear combination, no temporal smoothing |

### 12.2 The Estimation Problem

**Quantity of interest:** `offset_to_UTC = T_local - T_UTC(NIST)`

**Observation model:** For each broadcast i:
```
D_clock_i = T_arrival_i - ToF_i - T_transmit_i
          = offset_to_UTC + ε_i
```

Where:
- `T_arrival_i` — measured precisely by GPSDO (known)
- `ToF_i` — ionospheric path delay (nuisance parameter, varies)
- `T_transmit_i` — scheduled transmission time (known by definition)
- `ε_i` — residual error (ionospheric + noise)

**Key insight:** All 17 broadcasts share the **same unknown** (offset_to_UTC). The ionospheric paths are **nuisance parameters** we must estimate to extract the quantity of interest.

### 12.3 Layer 1: Per-Broadcast Kalman Filter

**Purpose:** Track ionospheric path dynamics for each of the 17 broadcasts.

**State vector:**
```
x = [ToF_ms, dToF_dt]
```

**Physical model:**
- The ionosphere is a continuous medium — it cannot teleport
- ToF changes are bounded by ionospheric dynamics (~1-5 ms/minute max)
- A Kalman filter here models **real physics**

**Process noise (tuned per frequency):**
- Low frequencies (2.5-5 MHz): Higher Q (E-layer volatility)
- High frequencies (15-25 MHz): Lower Q (F-layer stability)

**Measurement noise:** SNR-dependent (high SNR → trust measurement)

**Implementation:** `src/hf_timestd/core/broadcast_kalman_filter.py`

**What this achieves:**
- Rejects detection glitches (false peaks, multipath)
- Preserves real ionospheric dynamics (science signal)
- Provides smoothed ToF with uncertainty for downstream processing

### 12.4 Layer 2: Per-Station TEC Estimation

**Purpose:** Validate multi-frequency consistency and estimate ionospheric TEC.

**Physics:** The ionospheric group delay follows:
```
τ_iono(f) = K × TEC / f²
where K = 40.3 m³/s²
```

For a given path, **TEC is the same** for all frequencies. Only the delay differs by 1/f².

**Algorithm:** Weighted Least Squares regression on ToF vs 1/f²:
```
ToF(f) = ToF_geometric + k/f²

Solve for:
  - ToF_geometric (ionosphere-free delay)
  - k (proportional to TEC)
```

**Implementation:** `src/hf_timestd/core/tec_estimator.py`

**Metrological Constraints:**

The HF-derived TEC estimator provides validation and science products, but **does not directly modify D_clock values** because mode mixing can corrupt the 1/f² fit.

**What HF TEC achieves:**
- **Validates** that multi-frequency measurements follow 1/f² physics
- **Boosts confidence** for measurements with good TEC fit (R² > 0.9)
- **Reduces confidence** for measurements with poor TEC fit
- **Produces TEC science products** for ionospheric research
- **Detects mode changes** when 1/f² relationship breaks

### 12.4.1 GNSS VTEC Ionospheric Correction (v6.1)

**NEW in v6.1:** When local GNSS VTEC is available, the fusion layer applies a **direct ionospheric correction** to D_clock measurements.

**Physics Basis:**

The propagation model computes D_clock using a **modeled** TEC value (from IRI-2020, IONEX, or parametric fallback). GNSS VTEC provides a **direct measurement** of the actual ionospheric electron content. The correction is:

```
D_clock_corrected = D_clock + Δiono
where Δiono = K × (TEC_model - TEC_gnss) × n_hops × obliquity / f²
      K = 1.344 ms·MHz²/TECU (ionospheric delay constant)
```

**Why this is metrologically justified:**

1. **GNSS VTEC is a direct measurement** — dual-frequency GPS receivers measure TEC to ±1-2 TECU accuracy
2. **The 1/f² physics is well-established** — ionospheric group delay follows this dispersion relation exactly
3. **We're correcting model error** — not adding new uncertainty, but removing systematic bias

**TEC Source Hierarchy:**

| Priority | Source | Latency | Accuracy | Usage |
|----------|--------|---------|----------|-------|
| 1 | Local GNSS VTEC | ~1s | ±1-2 TECU | Direct D_clock correction |
| 2 | IONEX maps | 2 hours | ±2-5 TECU | Propagation model (Tier 1.5) |
| 3 | IRI-2020 | Climatology | ±5-10 TECU | Propagation model (Tier 1) |
| 4 | Parametric | Climatology | ±10-20 TECU | Propagation model (Tier 2) |

**Implementation:** `src/hf_timestd/core/multi_broadcast_fusion.py` (GNSS VTEC correction block)

**Typical Correction Magnitude:**

For ΔTEC = 10 TECU, f = 10 MHz, 1 hop, obliquity = 1.5:
```
Δiono = 1.344 × 10 × 1 × 1.5 / 100 = 0.20 ms
```

For ΔTEC = 30 TECU, f = 5 MHz, 1 hop, obliquity = 1.5:
```
Δiono = 1.344 × 30 × 1 × 1.5 / 25 = 2.42 ms
```

**Uncertainty Floor with GNSS TEC:**

With proper GNSS VTEC correction, the theoretical uncertainty floor is:
- **Per-station:** ~0.1-0.2 ms (geometric path becomes calibratable constant)
- **Multi-station WLS:** ~0.05-0.1 ms

**Example:**
```
WWV 5 MHz:  ToF = 35.2 ms
WWV 10 MHz: ToF = 33.8 ms  
WWV 15 MHz: ToF = 33.4 ms

Fit: ToF = 33.1 ms + 530/(f_MHz)²
R² = 0.98 (good fit)

Result: Measurements validated, confidence boosted 15%
TEC estimate = 25 TECU (science product)
```

### 12.5 Layer 3: Multi-Station Weighted Least Squares

**Purpose:** Combine per-station D_clock estimates into a single offset_to_UTC.

**Method:** Best Linear Unbiased Estimator (BLUE):
```
offset_to_UTC = Σ(w_i × D_clock_i) / Σ(w_i)
where w_i = 1/σ_i²
```

**Why NOT a Kalman filter here?**

A Kalman filter at L3 would model `offset_to_UTC` as having **process noise** — implying the offset drifts randomly. But:

1. The GPSDO doesn't drift randomly — it has deterministic (tiny) drift
2. Any apparent "drift" in the fused offset is actually **ionospheric bias** leaking through
3. A Kalman would **mask** this bias instead of **estimating** it

**Cross-station validation:**
- Compute per-station residuals from the weighted mean
- If a station systematically deviates → flag for calibration review
- Detect ionospheric gradients (science signal, not error)

**Implementation:** `src/hf_timestd/core/multi_broadcast_fusion.py` (method: `_weighted_least_squares_fusion`)

### 12.6 State Persistence

**Per-broadcast state:** Each of the 17 Kalman filters persists its state:
```json
{
  "WWV_5000": {"tof_ms": 33.8, "dtof_dt": 0.001, "P": [[0.1, 0], [0, 0.001]], "n_updates": 1234},
  "WWV_10000": {"tof_ms": 33.4, "dtof_dt": 0.002, "P": [[0.08, 0], [0, 0.001]], "n_updates": 1189},
  ...
}
```

**File:** `/var/lib/timestd/state/broadcast_kalman_state.json`

**Restart behavior:**
- Each broadcast resumes from its last known ToF
- No single point of state that can drift
- Fusion is instantaneous (WLS), not path-dependent

### 12.7 Comparison: Old vs New Architecture

| Aspect | v5.x (Single L3 Kalman) | v6.0 (Hierarchical) |
|--------|-------------------------|---------------------|
| **Filtering location** | Fusion layer only | Per-broadcast |
| **State persistence** | Single Kalman state | 17 independent states |
| **Restart behavior** | Variance depends on trust decay | Deterministic from per-broadcast state |
| **Ionospheric bias** | Smoothed away | Removed by TEC fit |
| **Science preservation** | Variations hidden | Variations preserved |
| **Fusion method** | Kalman (temporal) | WLS (instantaneous) |

### 12.8 Code References

| Component | File | Key Method |
|-----------|------|------------|
| Per-Broadcast Kalman | `core/broadcast_kalman_filter.py` | `BroadcastKalmanFilter.update()` |
| TEC Estimation | `core/tec_estimator.py` | `TECEstimator.estimate_tec()` |
| WLS Fusion | `core/multi_broadcast_fusion.py` | `_weighted_least_squares_fusion()` |
| State Persistence | `core/multi_broadcast_fusion.py` | `_save_broadcast_kalman_state()` |

---

## Appendix A: Key Equations

**D_clock Calculation:**

```
D_clock = (T_arrival - T_expected) - τ_propagation
```

**Propagation Delay:**

```
τ_propagation = τ_geometric + τ_ionospheric + τ_mode
```

**Ionospheric Delay:**

```
τ_iono = K × TEC / f²
where K = 40.3 m³/s²
```

**Fusion:**

```
D_clock_fused = Σ(w_i × D_clock_i) / Σ(w_i)
where w_i = 1 / σ_i²
```

**Uncertainty:**

```
u_combined = √(u_statistical² + u_systematic² + u_propagation²)
```

**Allan Deviation:**

```
σ_y(τ) = √(1/(2(M-1)) × Σ(y_{i+1} - y_i)²)
```

---

## Appendix B: References

1. **NIST SP 432**: NIST Time and Frequency Services
2. **ITU-R TF.768**: Standard Frequencies and Time Signals
3. **ISO GUM**: Guide to the Expression of Uncertainty in Measurement
4. **IRI-2020**: International Reference Ionosphere
5. **Digital RF**: MIT Haystack Observatory HDF5 format
6. **ka9q-radio**: Phil Karn's software-defined radio

---

## Appendix C: Related Documentation

- **TECHNICAL_REFERENCE.md** — System architecture, service descriptions, configuration
- **INSTALLATION.md** — Setup and deployment guide
- **README.md** — Project overview

---

**Source Code:** <https://github.com/mijahauan/hf-timestd>  
**License:** MIT  
**Author:** Michael James Hauan (AC0G)
