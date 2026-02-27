# HF-TimeStd: Metrological Description

**Prepared for:** Time metrology professionals, "time nuts", and general users  
**System Version:** 6.8.0 (TickEdgeDetector Unified Pipeline + Real-Time Ionospheric Model + GNSS VTEC Anchoring)  
**Last Updated:** February 27, 2026  
**Author:** Michael James Hauan (AC0G)

---

## 1. Executive Summary

**hf-timestd** is a dual-purpose HF time transfer and ionospheric measurement system. It receives WWV/WWVH/CHU/BPM time signal broadcasts via a GPSDO-disciplined SDR and operates in two complementary modes:

**RTP Mode (Physics Pathway):** With GPS+PPS providing authoritative timing (~50 μs accuracy via radiod's RTP timestamps), the system uses the known transmission times and measured arrival times to **study the ionosphere**. The propagation delay residuals reveal carrier-phase differential TEC (dTEC, the primary ionospheric product, anchored by GNSS VTEC), traveling ionospheric disturbances (TIDs), and space weather effects.

**Fusion Mode (Metrology Pathway):** The system attempts to **recover UTC from the HF broadcasts alone**, using multi-broadcast fusion to solve for the local clock offset. This pathway demonstrates how closely tone analysis can reconstruct the timing authority that RTP mode provides directly. Current accuracy: ±5-100 ms depending on ionospheric conditions (vs ±0.05 ms from RTP).

The system demonstrates metrological rigor through:

- **Authoritative RTP timestamps** from GPS+PPS-disciplined radiod (no pipeline offset correction needed)
- **TickEdgeDetector unified pipeline** — single source for D_clock (AM-domain front-edge ensemble), Doppler (carrier phase slope), and SNR (per-tick matched filter)
- **Real-time ionospheric propagation model** (WAM-IPE + GIRO + IRI-2020 fallback)
- **ISO GUM-compliant uncertainty budgets** with full traceability
- **Kalman-filtered fusion** with inverse variance weighting
- **Chrony SHM integration** for system clock discipline

**The Core Value Proposition:** The system serves dual purposes. In RTP mode, the ~50 μs timing accuracy from GPS+PPS enables precision ionospheric science — the HF propagation delays become the measurement, not the error. In fusion mode, the system demonstrates HF time transfer capability, using software complexity and ionospheric physics to recover UTC from broadcast signals alone.

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

### 4.1 The Eight Services

The system is composed of eight independent systemd services:

| Service | Responsibility | Output |
|---------|---------------|--------|
| **timestd-core-recorder** | Reliable Data Capture | `/var/lib/timestd/raw_buffer/` |
| **timestd-metrology** | Signal Processing & Timing Extraction | `/var/lib/timestd/phase2/{CHANNEL}/` |
| **timestd-l2-calibration** | Geometric + Ionospheric Corrections | L2 calibrated timing (HDF5) |
| **timestd-fusion** | Multi-Broadcast Synthesis | `/var/lib/timestd/phase2/fusion/` + Chrony SHM |
| **timestd-vtec** | Ionospheric Data Acquisition | `/var/lib/timestd/gnss_vtec.h5`, `/var/lib/timestd/ionex/` |
| **timestd-physics** | Carrier-phase dTEC, group-delay TEC validation, T_iono | `/var/lib/timestd/phase2/science/tec/` |
| **timestd-web-api** | User Visualization & System API | Port 8000 |
| **timestd-radiod-monitor** | Hardware Health Monitoring | Alerts on failure |

### 4.2 Three-Phase Pipeline

```
Phase 1: Core Recorder (Immutable Archive)
  ↓ Binary IQ (.bin.zst) + JSON sidecars (24 kHz IQ)
Phase 2: Analytics (Timing Extraction)
  ↓ HDF5 L2 Timing Measurements
Phase 3: Fusion (Multi-Broadcast Synthesis)
  ↓ Chrony SHM (System Clock Discipline)
```

**Metrological Advantages:**

- **Phase 1 never drops data** during Phase 2 updates
- **Reprocessability**: Improve algorithms without re-recording
- **Independent validation**: Test analytics on archived data
- **HDF5 crash-safe writes**: Open-write-close per measurement (no dirty flags on crash)

### 4.3 RTP Timestamp as Authoritative Reference

**Critical Design Decision:** System time is **derived from RTP timestamps**, not vice versa.

```python
utc = gps_time_unix + (rtp_ts - rtp_timesnap) / sample_rate
```

radiod's `GPS_TIME` and `RTP_TIMESNAP` are both derived from `input_sample_index / decimation` — they are in the same counter space. No pipeline offset correction is needed. The timestamps are authoritative, providing ~50 μs accuracy to UTC via GPS+PPS.

**Why this matters:**

- **Sample count integrity**: Gaps are unambiguous (RTP timestamp jumps)
- **No time stretching**: Never adjust sample count to fit wall clock
- **Authoritative timing**: RTP timestamps from ka9q-radio carry GPS+PPS time through the decimation pipeline
- **No calibration needed**: GPS_TIME/RTP_TIMESNAP mapping is direct — no wall-clock calibration bias
- **Reprocessable**: Raw data can be reanalyzed with improved algorithms

### 4.4 Data Levels

| Level | Description | Content |
|-------|-------------|---------|
| **L0** | Binary IQ Archive | Raw IQ samples (.bin.zst + JSON sidecars) |
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
- **Format**: Binary IQ archive (.bin.zst + JSON metadata sidecars)

### 5.2 Tick Edge Detection (TickEdgeDetector — v6.8+)

**Primary Timing Source:**

The `TickEdgeDetector` is the single source for all three `tick_timing` observables:

- **D_clock** (ms): AM-domain front-edge ensemble timing, UTC-referenced via `buffer_timing`
- **Doppler** (Hz): Carrier phase slope across the minute from IQ-domain extraction
- **SNR** (dB): Per-tick matched filter signal-to-noise ratio

**Tick Templates (per station):**

| Station | Tone Freq | Tick Duration | Template Samples (24 kHz) |
|---------|-----------|---------------|--------------------------|
| **WWV** | 1000 Hz | 5.0 ms | 120 |
| **WWVH** | 1200 Hz | 5.0 ms | 120 |
| **CHU** | 1000 Hz | 300 ms | 7200 |
| **BPM** | 1000 Hz | 10 ms | 240 |

**Detection Method (inspired by ntpd refclock_wwv.c Type 36 driver):**

1. **Quadrature Matched Filter:**
   - Generates I/Q template for the exact tick shape (e.g., 5 cycles of 1000 Hz for WWV)
   - Phase-invariant detection via envelope of complex correlation
   - 800–1400 Hz bandpass rejects 100 Hz BCD, 440/500/600 Hz audio tones
   - Processing gain: ~21 dB per tick (120 samples at 24 kHz)

2. **Front-Edge Back-Calculation:**
   - Correlation peak corresponds to the CENTER of the tick pulse
   - The on-time marker is the LEADING EDGE (NIST SP 432)
   - Subtract half the tick duration from peak position to recover front edge
   - Sub-sample parabolic interpolation (~5 μs precision)

3. **Carrier Phase Extraction (IQ Domain):**
   - At each detected tick, mix raw IQ samples at the tone frequency over the tick duration
   - Take the angle of the mean phasor → carrier phase at that tick
   - Phase progression across the minute encodes Doppler shift

4. **Ensemble Combination (57 ticks/minute):**
   - SNR-weighted robust median of all detected per-second ticks
   - Outlier rejection via MAD (Median Absolute Deviation)
   - Typical: 50–57 valid ticks per minute per station
   - Effective processing gain: 21 + 10×log10(57) ≈ 38.6 dB

5. **Doppler from Phase Slope:**
   - Unwrap carrier phase across detected ticks
   - Linear fit: slope (rad/s) / (2π) = Doppler frequency shift (Hz)
   - Requires ≥5 detected ticks spanning ≥5 seconds for meaningful fit
   - Uncertainty from linear fit covariance

6. **Intermodulation Awareness:**
   - Audio tone schedule determines "clean" vs "contaminated" minutes
   - WWV silent minutes: {29, 43–51, 59}; WWVH silent: {0, 8–10, 14–19, 30}
   - When one station's audio tone is silent, the other's ticks are intermod-free
   - Clean minutes yield higher detection confidence

**Implementation:** `src/hf_timestd/core/tick_edge_detector.py`

**Precision**: ±0.008 ms ensemble uncertainty (CHU at 20+ dB SNR), ±0.5–2 ms typical (WWV/WWVH)

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

### 5.4 Real-Time Ionospheric Propagation Model (v6.7)

The `HFPropagationModel` computes frequency-dependent ionospheric delay using a three-tier data hierarchy:

```
HFPropagationModel.predict(station, frequency, utc_time)
    ├── IonoDataService.get_iono_params()
    │       ├── WAM-IPE grid (NOAA S3/NOMADS)     ← Tier 1: Real-time 3D model
    │       ├── GIRO ionosonde corrections          ← Tier 1.5: Ground-truth hmF2/foF2
    │       ├── IRI-2020 climatology                ← Tier 2: Monthly median model
    │       └── Parametric fallback                 ← Tier 3: Diurnal/seasonal formula
    ├── _evaluate_mode() × [1F, 2F, 3F, 1E]
    │       ├── Geometric feasibility check
    │       ├── MUF check (freq vs foF2/sec(i))
    │       ├── Spherical Earth path length
    │       └── Ionospheric group delay
    │               ├── Ne(h) numerical integration  ← When profile available
    │               └── TEC-based: 40.3·sTEC/(c·f²)  ← Fallback
    └── _estimate_uncertainty()
```

**Ionospheric Group Delay Physics:**

```
Δτ = (40.3 / c) × ∫ Ne(s) ds / f²  =  40.3 × sTEC / (c × f²)
```

For a vertical TEC of 20 TECU at 10 MHz, the excess delay is ~0.27 ms. At 5 MHz, it's ~1.07 ms (4× larger).

**Multi-Mode Predictions:**

For each (station, frequency) pair, the model evaluates four propagation modes:

| Mode | Description | Typical Distance |
|------|-------------|-----------------|
| **1F** | Single F-layer hop | < 3000 km |
| **2F** | Two F-layer hops | 3000–6000 km |
| **3F** | Three F-layer hops | > 6000 km |
| **1E** | Single E-layer hop (daytime) | < 2000 km |

Each mode is checked for geometric feasibility, MUF constraint, and minimum elevation (>3°).

**Adaptive Uncertainty:**

| Data Source | 3σ Uncertainty | Confidence |
|-------------|---------------|------------|
| WAM-IPE + GIRO | ±1.5 ms | 0.8 |
| WAM-IPE alone | ±3.0 ms | 0.6 |
| IRI-2020 | ±4.5 ms | 0.5 |
| Parametric fallback | ±9.0 ms | 0.2 |
| No model | ±15.0 ms | 0.0 |

The final window blends model uncertainty with tracked observational variance, floored at ±5 ms (3σ).

**Self-Consistency Check:**

Multi-frequency differential delay validates model TEC predictions:

```
Δτ(f1,f2) = τ(f1) - τ(f2) = 40.3 × sTEC × (1/f1² - 1/f2²) / c
```

If observed differential delay disagrees with predicted by >1 ms RMS, the model flags an inconsistency.

**Great-Circle Path TEC Sampling (v6.7.1):** TEC is sampled along the great-circle path using spherical trigonometry, ensuring accurate TEC integration for long paths (e.g., BPM at 11,504 km).

**Altitude-Dependent Obliquity Mapping (v6.7.1):** `M(h) = 1 / sqrt(1 - (R·cos(e) / (R + h))²)` replaces the simpler `1/sin(e)` approximation.

**Propagation Delay Bounds:**

- WWV: 4–12 ms
- WWVH: 15–30 ms
- CHU: 6–15 ms
- BPM: 40–70 ms

Delays outside bounds have plausibility reduced by 70%.

#### Optional: Local GNSS-VTEC Enhancement

When a dual-frequency GNSS receiver (e.g., u-blox ZED-F9P) is available, the system can measure **local vertical TEC in real-time** (~1 minute latency vs 1–2 hours for IONEX maps). The `timestd-vtec` service polls the receiver and writes to `/var/lib/timestd/gnss_vtec.h5`.

**Key Files:**

| File | Purpose |
|------|---------|
| `src/hf_timestd/core/propagation_model.py` | `HFPropagationModel` — delay prediction, multi-mode, self-consistency |
| `src/hf_timestd/core/iono_data_service.py` | `IonoDataService` — WAM-IPE/GIRO fetch, cache, great-circle TEC sampling |
| `src/hf_timestd/core/arrival_pattern_matrix.py` | `ArrivalPatternMatrix` — integrates model into arrival predictions |

### 5.5 Adaptive Search Windows

**Phase Progression:**

| Phase | Window | Criteria |
|-------|--------|----------|
| **Bootstrap** | ±50 ms | Initial (RTP timestamps are authoritative, no wall-clock bias) |
| **Provisional** | ±5-15 ms | 10+ detections, 2+ stations, D_clock σ < 1 ms |
| **Calibrated** | ±2-5 ms | 30+ detections, 60 min span, RTP variance < 50² |

**Arrival Tolerance:** ±100 ms (validates detected tone arrivals against expected propagation delay)

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
| **Q (Offset)** | 0.01 ms | Allows tracking real offsets (updated 2026-02-06) |
| **Q (Drift)** | 1e-12 ms/min | The clock does not wander |
| **R (Measurement)** | 30.0 ms | High measurement noise (ionospheric) |

**Drift Clamping:** `drift_ms_per_min` is forced to `0.0` after convergence.

### 6.3 Error Sources

**Type A (Statistical):**

- Weighted standard error of fusion
- Reduces as √N with more broadcasts

**Type B (Systematic):**

| Source | Uncertainty | Notes |
|--------|-------------|-------|
| Tone detection (Cramér-Rao) | ±0.036-0.9 ms | SNR-dependent (v6.2) |
| Multipath delay spread | ±0.5-2.5 ms | Inflates uncertainty when detected |
| Doppler bias (uncorrected) | ±0.1-2 ms | Now corrected in v6.2 |
| Propagation model (IONEX) | ±1-2 ms | |
| Propagation model (IRI) | ±2-5 ms | |
| RTP jitter | ±0.1 ms | |
| GPSDO stability | ±0.001 ms | Negligible |

**Combined Uncertainty (RSS):**

```
u_combined = √(u_cramer_rao² + u_multipath² + u_propagation² + u_systematic²)
```

**v6.2 Enhancement:** The `ToneDetectionResult` now includes `timing_uncertainty_ms` computed from the Cramér-Rao bound, which is inflated when multipath is detected. This provides rigorous per-measurement uncertainty for downstream fusion.

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

### 9.1 Raw Archive: Binary IQ + JSON Sidecars

- **Format:** `.bin.zst` (zstd-compressed binary) + `.json` metadata sidecar per minute
- **Structure:**
  - Binary file: 1,440,000 complex64 IQ samples per minute (24 kHz × 60s)
  - JSON sidecar: RTP timestamps, gap info, system time, quality metrics
- **Metadata:** Start RTP timestamp, start system time, sample rate (24 kHz), center frequency, gap count
- **Size:** ~2-3 GB/day/channel

> **Note:** Digital RF (MIT Haystack) is used only for GRAPE DRF packaging/upload, not for raw recording.

### 9.2 Tick Timing: L2 HDF5 (Primary Timing Product)

**Schema:** `l2_tick_timing_v1.json` (schema version 2.0.0, processing version 5.0.0)

All fields sourced from `TickEdgeDetector`:

| Field | Type | Description |
|-------|------|-------------|
| `d_clock_ms` | float | Ensemble timing residual from expected arrival (ms) |
| `d_clock_uncertainty_ms` | float | MAD of per-tick timing errors (ms) |
| `d_clock_source` | string | Always `edge_ensemble` |
| `doppler_hz` | float | Carrier phase slope across the minute (Hz) |
| `doppler_uncertainty_hz` | float | Doppler uncertainty from linear fit (Hz) |
| `mean_snr_db` | float | Mean per-tick matched filter SNR (dB) |
| `valid_windows` | int | Number of ticks with valid detections |
| `total_windows` | int | Total seconds attempted |
| `ensemble_n_edges` | int | Number of tick edges used in ensemble |
| `n_clean` | int | Ticks from intermod-free minutes |

### 9.3 Fusion Output: HDF5 L3

- **Structure:**
  - `/fused_solution`: Time series of weighted mean offset
  - `/residuals`: Per-station residuals from the mean
  - `/calibration`: Current calibration state for each station
- **Chrony SHM updates** for clock discipline
- **Allan deviation tracking**

### 9.4 Science Products

- TEC estimates (multi-frequency 1/f² fit)
- Propagation mode statistics
- Sporadic-E events
- Space weather correlations
- Doppler time series (ionospheric motion)

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

## 13. FUSION Mode Accuracy Analysis

### 13.1 Motivating Rationale

The system serves a dual purpose. In **RTP mode** (with GPSDO), the authoritative timing comes from GPS+PPS via radiod, and the metrology pipeline functions as a testbed for refining detection algorithms, calibration models, and ionospheric corrections against a known-good reference. This refinement directly serves the second purpose: **FUSION mode**, where GPS, GPSDO, or even network access may be unavailable, and the system must derive UTC solely from HF time standard receptions.

FUSION mode addresses real operational scenarios:
- **Remote/off-grid installations** without GPS coverage
- **Disaster/emergency situations** where GPS and network infrastructure are disrupted
- **Intentional GPS denial** (jamming, spoofing) in contested environments
- **Backup timing** when primary GNSS disciplining fails

### 13.2 Error Budget

In FUSION mode, the timing chain is:

```
UTC(NIST/NRC) → HF transmitter → Ionosphere → Receiver → ADC → Detection → D_clock
```

| Source | Magnitude | Notes |
|--------|-----------|-------|
| **Transmitter timing** | < 1 µs | WWV/WWVH/CHU traceable to UTC(NIST)/UTC(NRC) |
| **Ionospheric propagation** | 3–15 ms variation | Dominant error. Diurnal, seasonal, solar cycle |
| **Multipath/mode structure** | 1–5 ms | Multiple ionospheric modes arrive at different times |
| **ADC clock accuracy** | 0.1–10 ppm | TCXO: 1–2 ppm. Cheap crystal: 10–50 ppm |
| **TickEdgeDetector** | 0.008–2 ms | Ensemble of 50–57 ticks/minute, sub-sample interpolation |
| **NTP initial sync** | 1–50 ms | Depends on network path |

### 13.3 Expected FUSION Mode Accuracy

| Configuration | Expected Accuracy | Time to Lock |
|--------------|-------------------|--------------|
| Multi-station + TCXO + NTP | **±2–5 ms** steady-state | 2–3 min |
| Multi-station + TCXO, no network | **±2–5 ms** steady-state | 5–10 min |
| Single station + TCXO | **±5–15 ms** | 2–3 min |
| Multi-station + cheap crystal | **±2–5 ms** (if freq lock works) | 5–10 min |

The ionosphere is the dominant error in all cases. Oscillator quality affects **time to lock** and **holdover during outages**, but not steady-state accuracy once locked.

### 13.4 Dual Chrony Feed Architecture (v6.5.1)

| Feed | SHM Unit | Source | Purpose |
|------|----------|--------|---------|
| **TSL1** | 0 | L1 Kalman (geometric fallback) | Raw metrology fusion — no ionospheric model |
| **TSL2** | 1 | L2 Kalman (physics model) | Full ionospheric correction via propagation model |

Each feed has its own independent Kalman filter state. TSL2 should show lower jitter and better accuracy as the ionospheric correction model removes systematic propagation biases.

---

## 14. Timing Authority Levels: Achievable Uncertainty Analysis

The system's achievable timing uncertainty depends critically on the hardware configuration and timing reference chain. Six levels (L1–L6) represent progressively better timing infrastructure.

### 14.1 Error Source Taxonomy

| # | Error Source | Symbol | Description |
|---|-------------|--------|-------------|
| 1 | **Transmitter timing** | σ_tx | UTC(NIST/NRC) to RF emission |
| 2 | **Ionospheric propagation** | σ_iono | Path delay variation (dominant for HF) |
| 3 | **Multipath/mode structure** | σ_mode | Multiple ionospheric modes |
| 4 | **Detection algorithm** | σ_det | TickEdgeDetector ensemble + sub-sample interpolation |
| 5 | **ADC sample clock** | σ_adc | Frequency accuracy and stability |
| 6 | **RTP-to-UTC mapping** | σ_rtp | Mapping RTP timestamps to wall-clock UTC |
| 7 | **Timing authority** | σ_auth | How well the system knows "what time is it now" |

Sources 1–4 are **irreducible** (physics/algorithm). Sources 5–7 are **configuration-dependent**.

### 14.2 Irreducible Error Sources

- **σ_tx < 0.001 ms**: WWV/WWVH traceable to UTC(NIST) with < 1 µs. Negligible.
- **σ_iono = 3–15 ms**: Dominant error. Diurnal, seasonal, solar cycle, geomagnetic.
- **σ_mode = 1–5 ms**: Multiple propagation modes (1F2, 2F2, 1E) arrive at different times.
- **σ_det ≈ 0.05 ms**: TickEdgeDetector ensemble of 50–57 ticks achieves ~38.6 dB processing gain. Negligible compared to σ_iono.

### 14.3 Level Summary

| Level | Sample Clock | Timing Authority | Single Meas. | Fused (10 min) | Best Grade | Primary Limiter |
|-------|-------------|-----------------|-------------|----------------|------------|-----------------|
| **L6** | GPSDO (< 1 ppb) | PPS in stream | 3–15 ms | 0.3–1.0 ms | **A** | Ionospheric scatter |
| **L5** | GPSDO (< 1 ppb) | GPS+PPS local | 3–15 ms | 0.5–1.7 ms | **A–B** | RTP mapping + iono |
| **L4** | GPSDO (< 1 ppb) | PTP/NTP via LAN | 3–15 ms | 0.5–2.0 ms | **B** | LAN timing jitter |
| **L3** | GPSDO (< 1 ppb) | NTP via WAN | 3–15 ms | 1–3 ms | **B–C** | NTP wander |
| **L2** | TCXO (1–2 ppm) | NTP via LAN | 3–15 ms | 2–5 ms | **C** | Oscillator drift + NTP |
| **L1** | TCXO (1–2 ppm) | HF self-derived | 3–15 ms* | 2–5 ms | **C** | Oscillator drift + bootstrap |

*L1 single measurement: 200+ ms during bootstrap, 3–15 ms after lock.

### 14.4 Key Insights

1. **The ionosphere is always the dominant single-measurement error** (3–15 ms). No hardware improvement changes this.
2. **Grade A (< 0.5 ms) requires L5 or L6** — sub-µs timing authority AND long averaging.
3. **The GPSDO matters for the ruler, not the zero-point.** It ensures measurements within a fusion window are coherent.
4. **NTP is the ceiling for L2–L4.** NTP jitter sets a floor that multi-station fusion cannot average below.
5. **L1 and L2 converge to the same steady-state** after bootstrap.
6. **The current system operates at L5** and achieves 2–5 ms fused uncertainty.

### 14.5 Station Priority Policy (v6.5.0)

| Station | Role | Rationale |
|---------|------|----------|
| **CHU** | Reference | Unique frequencies, FSK-verified timing |
| **WWV** | Primary | Closest station, best SNR |
| **WWVH** | Primary | Independent path, cross-validation |
| **BPM** | Scientific | Very long path (~11,000 km), weight reduced to 30% |

---

## 15. Metrological Validation (v6.2)

This section describes procedures for validating hf-timestd performance against external references and theoretical predictions.

### 15.1 TSL1 vs TSL2 Comparison

The dual Chrony feed architecture (TSL1 and TSL2) provides built-in validation of propagation corrections.

<!-- LIVE: l1-l2-comparison -->

**What TSL1 and TSL2 Represent:**

| Feed | SHM | Data Source | Processing | Typical Uncertainty |
|------|-----|-------------|------------|---------------------|
| **TSL1** | 0 | L1 metrology (raw ToA) | Multi-broadcast fusion only | ±0.85 ms |
| **TSL2** | 1 | L2 calibrated (corrected D_clock) | + Geometric delay, TEC, system cal, Kalman | ±0.3-1.0 ms |

**The L1-L2 Difference:**

```
L1 - L2 = geometric_delay + ionospheric_TEC + system_calibration
```

This difference reveals the **quality of propagation corrections**:
- **Stable difference (~0.5-1 ms)**: Propagation model is working correctly
- **Diurnal variation**: Ionospheric effects are being captured
- **Large divergence (>5 ms)**: Calibration problem or model failure

<!-- LOGS: L1-L2 | filter: "L1-L2 difference" -->

#### Understanding the Feedback Loop

**Why chrony shows `+0ns` for both TSL1 and TSL2:**

When you run `chronyc sources`, you may see both HF feeds showing `+0ns`:

```
#? TSL1    0   4   204    54     +0ns[   +0ns] +/- 2000us
#* TSL2    0   4   204    54     +0ns[   +0ns] +/- 600us
```

This is **correct behavior**, not an error. Here's why:

1. **The HF time standard measures:** `D_clock = system_time - UTC_from_HF`
2. **Chrony uses D_clock** to discipline the system clock
3. **After convergence:** The system clock matches the HF estimate, so D_clock → 0

This is a **feedback control loop working correctly**. The `+0ns` means the system clock now tracks the HF estimate of UTC — it does NOT mean the HF estimate is perfectly accurate.

**The real accuracy information is in:**
- **`+/- 600us`**: The stated uncertainty (±0.6ms for TSL2)
- **External references**: Compare against GPS or NTP pools to see absolute accuracy

**Example interpretation:**

```
#* TSL2    0   4   204    54     +0ns[   +0ns] +/- 600us   ← HF feed (converged)
^x 192.168.0.202   1   6   377    40  +1168us[+1168us] +/- 345us   ← GPS reference
```

The GPS source shows `+1168us`, meaning the system clock (disciplined by TSL2) is **~1.2ms ahead of GPS time**. This is the **actual systematic offset** of the HF time standard relative to GPS/UTC — well within expected accuracy for HF propagation-based timing.

**Key insight:** Once the feedback loop converges, external references (GPS, NTP pools) become the validation mechanism for absolute accuracy.

**Data Recording (v6.2):**

The fusion service now records L1/L2 comparison in every HDF5 output:
- `d_clock_l1_ms`: L1-only fusion result
- `d_clock_l2_ms`: L2 fusion result  
- `l1_l2_difference_ms`: L1 - L2 (propagation correction quality metric)

**Validation Procedure:**

```bash
# Check current chrony sources
chronyc sources -v | grep -E "TSL|192.168"

# Expected output after convergence:
# #* TSL2    0   4   377    15     +0ns[   +0ns] +/- 600us   ← HF (converged)
# #- TSL1    0   4   377    15     +0ns[   +0ns] +/- 900us   ← HF (converged)
# ^x GPS     1   6   377    40  +1200us[+1200us] +/- 300us   ← External reference
#
# The GPS offset (+1200us) reveals the HF systematic error (~1.2ms)
```

### 15.2 Comparison with External Time Sources

#### 15.2.1 hf-timestd vs GPS Time Server

If you have a local GPS-based time server (e.g., at 192.168.0.202), you can compare hf-timestd against it:

**Expected Performance:**

| Source | Stratum | Typical Offset | Uncertainty | Traceability |
|--------|---------|----------------|-------------|--------------|
| **Local GPS (PPS)** | 1 | <1 μs | ~10-100 ns | UTC(USNO) via GPS |
| **hf-timestd TSL2** | 1 | ±0.3-1 ms | ±0.5 ms | UTC(NIST) via WWV/CHU |
| **hf-timestd TSL1** | 1 | ±0.8-1.5 ms | ±0.85 ms | UTC(NIST) via WWV/CHU |
| **Public NTP (pool)** | 2-3 | ±1-50 ms | ±5-20 ms | Varies |

**Key Insight:** hf-timestd is **not a replacement for GPS** for sub-millisecond timing. Its value is:
1. **Independent traceability** to UTC(NIST) — different from GPS's UTC(USNO)
2. **Resilience** — works when GPS is jammed/spoofed
3. **Ionospheric science** — the "error" is the measurement

**Validation Procedure:**

```bash
# Configure Chrony to use both GPS and hf-timestd
# In /etc/chrony/chrony.conf:
server 192.168.0.202 iburst prefer  # GPS time server
refclock SHM 0 refid TSL1 poll 4 precision 1e-3
refclock SHM 1 refid TSL2 poll 4 precision 1e-4

# Compare sources
chronyc sources -v
chronyc sourcestats

# Track offset between TSL2 and GPS over time
# The difference should be stable within ±1 ms
```

**Interpreting Results:**

- **TSL2 offset from GPS < 1 ms**: System is working correctly
- **TSL2 offset from GPS 1-3 ms**: Normal ionospheric variation
- **TSL2 offset from GPS > 5 ms**: Investigate calibration or propagation model
- **Consistent drift**: Possible GPSDO issue (see Section 15.3)

#### 15.2.2 GPS PPS Exposure

If your GPS receiver outputs PPS (Pulse Per Second), it provides the highest-precision timing reference available. The PPS signal marks the exact second boundary with ~10-100 ns accuracy.

**Note:** Most GPS time servers expose PPS internally for NTP discipline but may not expose it as a separate output. Check your receiver's documentation for:
- **Hardware PPS output** (BNC or SMA connector)
- **Software PPS** (via gpsd or similar)

**Using PPS for Validation:**

If PPS is available, you can compare the GPSDO's 1PPS output against the GPS receiver's PPS to detect GPSDO drift directly. This is the most rigorous validation method.

### 15.3 GPSDO Drift Detection

The "Steel Ruler" philosophy assumes the GPSDO provides a stable frequency reference. However, GPSDOs can drift if:
- GPS lock is lost for extended periods
- The internal oscillator ages
- Temperature variations affect the oscillator

**Current Capability:**

The system assumes GPSDO is the "steel ruler" (Q ≈ 0 in Kalman). It **cannot directly detect** GPSDO drift because all timing is relative to GPSDO.

**Indirect Detection Methods:**

1. **Long-term D_clock trend**: If D_clock shows consistent drift (e.g., +0.1 ms/day), that's GPSDO drift
2. **Compare TSL2 to GPS NTP**: Long-term trend in `chronyc sources` offset
3. **Allan deviation at long tau**: Increasing ADEV at τ > 10000s indicates drift

**Data Recording (v6.2):**

The fusion service now records Allan deviation in every HDF5 output:
- `adev_60s`: ADEV at τ=60s (short-term stability)
- `adev_1000s`: ADEV at τ=1000s (medium-term stability)

**Validation Procedure:**

```bash
# Check Allan deviation via web UI (metrology.html)
# Or query the API:
curl http://localhost:8000/api/stability/adev

# Expected ADEV values for a healthy system:
# τ=60s:   ~1e-9 to 1e-8 (dominated by ionosphere)
# τ=1000s: ~1e-10 to 1e-9 (should decrease with averaging)
# τ=10000s: ~1e-10 (should plateau, not increase)
#
# If ADEV increases at long tau, suspect GPSDO drift
```

### 15.4 Theoretical Predictions vs Measured Performance

#### 15.4.1 Cramér-Rao Bound

The theoretical minimum timing uncertainty is given by the Cramér-Rao bound:

```
σ_ToA = 1 / (2π × √(2 × SNR × B × T))
```

Where:
- SNR = Signal-to-noise ratio (linear)
- B = Effective bandwidth (Hz)
- T = Tone duration (seconds)

**Theoretical vs Measured:**

| Condition | Cramér-Rao Bound | Measured (v6.2) | Notes |
|-----------|------------------|-----------------|-------|
| 20 dB SNR, 800ms tone, 50 Hz BW | 0.036 ms | 0.1-0.5 ms | Multipath, Doppler limit |
| 10 dB SNR, 800ms tone, 50 Hz BW | 0.11 ms | 0.5-1.0 ms | Noise-limited |
| 6 dB SNR, 800ms tone, 50 Hz BW | 0.9 ms | 1-2 ms | Near detection threshold |

**Data Recording (v6.2):**

The fusion service records Cramér-Rao uncertainty:
- `cramer_rao_mean_ms`: Mean Cramér-Rao bound across measurements

#### 15.4.2 Multipath Impact

Multipath propagation causes delay spread that inflates timing uncertainty:

```
u_multipath = delay_spread / 2
```

**Data Recording (v6.2):**

- `multipath_detected_count`: Number of measurements with multipath
- `multipath_mean_delay_spread_ms`: Mean delay spread

#### 15.4.3 Doppler Correction

Doppler shift from ionospheric motion causes systematic timing bias:

```
Δt_bias ≈ (f_doppler / f_tone) × (T_tone / 2)
```

For typical HF Doppler (±1-5 Hz) on 1000 Hz tone over 800 ms:
- Δt_bias ≈ (5 / 1000) × 0.4 = **2 ms** (worst case)

**Data Recording (v6.2):**

- `doppler_mean_hz`: Mean Doppler shift
- `doppler_correction_applied_ms`: Total correction applied

### 15.5 Propagation Mode Identification

The system identifies propagation modes (1F2, 2F2, GW, etc.) based on:
1. **Geometric delay** from transmitter-receiver distance
2. **Ionospheric layer height** from IRI-2020 or IONEX
3. **Frequency-dependent behavior** (higher frequencies → higher layers)

<!-- LIVE: propagation-modes -->

**Data Recording (v6.2):**

- `propagation_modes_used`: Comma-separated list of modes identified
- `dominant_propagation_mode`: Most common mode in fusion window

**Mode Uncertainty:**

| Mode | Typical Uncertainty | Physical Basis |
|------|---------------------|----------------|
| GW (Ground Wave) | ±0.1 ms | Direct path, no ionosphere |
| 1F2 (Single F-layer hop) | ±0.5 ms | Well-characterized path |
| 2F2 (Double F-layer hop) | ±1.5 ms | Longer path, more variability |
| 1E (E-layer) | ±1.0 ms | Lower, more variable layer |
| Mixed/Unknown | ±2.5 ms | Mode ambiguity |

See `docs/PHYSICS.md` for detailed explanation of propagation mode identification physics.

### 15.6 Calibration Convergence

The system learns per-broadcast calibration offsets over time. Convergence is tracked via:

<!-- LIVE: calibration-status -->

**Data Recording (v6.2):**

- `calibration_age_hours`: Age of calibration data
- `calibration_n_samples`: Total samples used in learning
- `calibration_converged`: True if converged (>80% validation success rate)

**Convergence Criteria:**

| Metric | Threshold | Meaning |
|--------|-----------|---------|
| `calibration_n_samples` | > 100 | Sufficient data for learning |
| `calibration_age_hours` | < 24 | Calibration is fresh |
| `calibration_converged` | True | Validation success rate > 80% |

### 15.7 Uncertainty Budget Summary

The complete uncertainty budget for a fused D_clock measurement:

<!-- LIVE: uncertainty-budget -->

| Component | Source | Typical Value | Data Field |
|-----------|--------|---------------|------------|
| **Cramér-Rao** | Tone detection SNR | 0.036-0.9 ms | `cramer_rao_mean_ms` |
| **Multipath** | Delay spread | 0.5-2.5 ms | `multipath_mean_delay_spread_ms` |
| **Doppler** | Ionospheric motion | 0.1-2 ms (corrected) | `doppler_correction_applied_ms` |
| **Propagation model** | Mode uncertainty | 0.5-2.5 ms | `propagation_uncertainty_ms` |
| **Calibration** | Learning convergence | 0.1-1 ms | `systematic_uncertainty_ms` |
| **Statistical** | Measurement scatter | 0.1-0.5 ms | `statistical_uncertainty_ms` |
| **Combined (RSS)** | All sources | **0.3-1.0 ms** | `uncertainty_ms` |

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

- **docs/TECHNICAL_REFERENCE.md** — System architecture, service descriptions, configuration
- **docs/PHYSICS.md** — Ionospheric physics capabilities and measurements
- **docs/ARCHITECTURE.md** — Design philosophy and system architecture
- **INSTALLATION.md** — Setup and deployment guide
- **README.md** — Project overview

---

**Source Code:** <https://github.com/mijahauan/hf-timestd>  
**License:** MIT  
**Author:** Michael James Hauan (AC0G)
