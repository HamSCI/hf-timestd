# HF-TimeStd: Metrological Description for Time Transfer Specialists

**Prepared for:** Time metrology professionals and "time nuts"  
**Date:** January 5, 2026  
**System Version:** 4.4.0  
**Author:** Based on comprehensive codebase and documentation review

---

## Executive Summary

**hf-timestd** is an HF time transfer system that achieves **±0.5 ms (1σ) accuracy to UTC(NIST)** through multi-broadcast fusion of WWV/WWVH/CHU/BPM time signals. The system demonstrates metrological rigor through:

- **ISO GUM-compliant uncertainty budgets** with full traceability
- **GPSDO-disciplined sampling** (RTP timestamps as primary reference)
- **Physics-informed propagation modeling** (IONEX VTEC + IRI-2020)
- **Kalman-filtered fusion** with inverse variance weighting
- **Chrony SHM integration** for system clock discipline

Unlike simpler implementations, this system treats timing as a **measurement problem** with proper uncertainty quantification, systematic error correction, and validation against physical constraints.

---

## 1. The Fundamental Measurement Problem

### 1.1 What We Actually Measure

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

### 1.2 The Central Challenge

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

## 2. System Architecture: Metrological Design Principles

### 2.1 RTP Timestamp as Primary Reference (Not Wall Clock)

**Critical Design Decision:** System time is **derived from RTP timestamps**, not vice versa.

```python
# Precise time reconstruction
utc = time_snap_utc + (rtp_ts - time_snap_rtp) / sample_rate
```

**Why this matters:**

- **Sample count integrity**: Gaps are unambiguous (RTP timestamp jumps)
- **No time stretching**: Never adjust sample count to fit wall clock
- **Traceable to GPSDO**: RTP timestamps from ka9q-radio are GPSDO-disciplined
- **Reprocessable**: Raw data can be reanalyzed with improved algorithms

This follows Phil Karn's (KA9Q) timing architecture and is essential for sub-millisecond precision.

### 2.2 Three-Phase Pipeline (Separation of Concerns)

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

### 2.3 HDF5-Native Pipeline (Performance + Metadata)

All critical path data uses **HDF5 with Single Writer Multiple Reader (SWMR)**:

- **Performance**: 10x-100x faster than CSV parsing
- **Low latency**: Fusion reads data within milliseconds of write
- **Atomic updates**: No partial reads
- **Schema versioning**: All files have version metadata
- **Compression**: Reduces disk usage vs CSV

**Data Levels:**

- **L0**: Digital RF (raw IQ)
- **L1A**: Tone detections, channel observables
- **L1B**: BCD timecode
- **L2**: Timing measurements (D_clock + ISO GUM uncertainty)
- **L3**: Fused timing (Kalman filtered)

---

## 3. Measurement Methodology

### 3.1 Signal Reception (9 Channels, 17 Broadcasts)

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

### 3.2 Tone Detection (Sub-Sample Precision)

**Primary Timing Tones:**

- **WWV**: 1000 Hz (5 ms duration per second)
- **WWVH**: 1200 Hz (5 ms duration per second)
- **CHU**: 1000 Hz (300 ms duration per second)
- **BPM**: 1000 Hz (10 ms UTC, 100 ms UT1)

**Detection Method:**

- Matched filter correlation
- Sub-sample peak interpolation (parabolic fit)
- **Precision**: ±0.1 ms (tone detection uncertainty)

**Quality Metrics:**

- SNR (signal-to-noise ratio)
- Correlation peak sharpness
- BCD timecode validation
- Cross-frequency consistency

### 3.3 Station Discrimination (Shared Frequencies)

On 2.5, 5, 10, 15 MHz, **WWV and WWVH transmit simultaneously**. Misidentification causes **3-8 ms systematic error**.

**Discrimination Methods:**

1. **BCD Correlation (Primary)**
   - 100 Hz subcarrier cross-correlation
   - Distinct station peaks separated by propagation delay
   - **Confidence**: High when peaks > 15 ms apart

2. **Tone Power Ratio**
   - WWV: 1000 Hz strong, 1200 Hz weak
   - WWVH: 1200 Hz strong, 1000 Hz weak
   - **Threshold**: ±6 dB for high confidence

3. **Station ID Tones**
   - WWV: 500 Hz (even minutes), 600 Hz (odd minutes)
   - WWVH: 600 Hz (even minutes), 1500 Hz (odd minutes)
   - **Ground truth** minutes for calibration

4. **Cross-Frequency Guidance**
   - WWVH ToA at 10 MHz → narrow search at 5 MHz
   - Exploits ionospheric coherence across frequencies
   - Reduces search window from ±500 ms to ±3-5 ms

**Validation:**

- Inter-station D_clock consistency (< 1 ms spread required)
- D_clock continuity (jumps > 5 ms flagged)
- GPSDO lock status check

### 3.4 Propagation Delay Modeling (Physics-Informed)

**Tiered Hierarchy:**

**Tier 1: IONEX VTEC (Production)**

- NASA/IGS Global Ionosphere Maps
- Calculates Ionospheric Pierce Point (IPP) at 350 km altitude
- Interpolates VTEC from grid (lat/lon/time)
- **Group delay**: τ_iono ∝ TEC/f²
- **Accuracy**: ±1-2 ms (best available without raytracing)

**Tier 2: IRI-2020 (Fallback)**

- International Reference Ionosphere model
- Estimates hmF2 (layer height) and statistical VTEC
- **Accuracy**: ±2-5 ms

**Tier 3: Geometric/Empirical (Last Resort)**

- Great circle distance + virtual height assumption
- **Accuracy**: ±5-10 ms

**Validation:**

- Propagation delay bounds per station:
  - WWV: 4-12 ms
  - WWVH: 15-30 ms
  - CHU: 6-15 ms
  - BPM: 40-70 ms
- Delays outside bounds have plausibility reduced by 70%
- Ionospheric delay must follow 1/f² relationship

### 3.5 Adaptive Search Windows (Bootstrap → Calibrated)

**Phase Progression:**

1. **Bootstrap** (±500 ms)
   - Wide search, no prior knowledge
   - Uses anchor channels (WWV 20/25 MHz, CHU)
   - Establishes preliminary D_clock

2. **Provisional** (±5-15 ms)
   - After 10+ detections, 2+ stations
   - Physics-based prediction
   - D_clock σ < 1 ms

3. **Calibrated** (±2-5 ms)
   - After 30+ detections, 60 min span
   - Learned ToA per broadcast
   - RTP variance < 50²

**Key Insight:** GPSDO is the foundation. Stations are periodic calibration checks, not the primary reference.

---

## 4. Multi-Broadcast Fusion (The Core Innovation)

### 4.0 The Three-Layer Metrological Architecture

Understanding the fundamental difference between **Frequency Stability** (Slope) and **Time Accuracy** (Offset) is essential to understanding why multi-broadcast fusion is necessary.

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

#### The "Steel Ruler" Summary

| Component | Provides | Function |
|-----------|----------|----------|
| **GPSDO** | Slope (Rate) | Ensures the ruler is straight and rigid |
| **Multi-Frequency Dispersion** | Vertical Shift (Path Delay) | Calibrates the zero-point for each station |
| **Multi-Station Fusion** | Integrity (Validation) | Ensures the zero-point is consistent across the hemisphere |

**Key Insight**: The combined regression of 17 broadcasts doesn't just average noise — it **solves the geometry** of the ionosphere to find the true UTC origin point.

### 4.1 Calibration (Systematic Error Removal)

Each station has systematic biases due to:

- Propagation path geometry
- Mode preference at receiver location
- Antenna pattern effects
- Matched filter group delay

**Calibration Model:**

```python
calibration_offset = -mean(D_clock_station)
```

**Update Method:** Exponential Moving Average (α=0.5)

```python
offset_new = α × (-mean_current) + (1-α) × offset_old
```

- Prevents outliers from contaminating calibration state.

**Persistence:** Auto-saved every 50 updates, loaded on startup. Enables immediate Grade A performance after service restart.

### 4.2 Inverse Variance Weighting (ISO GUM Compliance)

```python
w = 1 / (uncertainty_ms²)
```

**Why this matters:**

- **Statistically optimal** for combining independent measurements with different uncertainties
- **ISO GUM best practice** (GUM-S1)
- Measurements with 0.5 ms uncertainty get 4x weight vs 1.0 ms uncertainty
- Confidence used as scaling factor for non-statistical quality

**Fusion Calculation:**

```python
d_clock_fused = Σ(w_i × d_clock_i) / Σ(w_i)
```

### 4.3 Kalman Filtering (Temporal Smoothing)

**State Model:**

```
x_k = x_{k-1} + w_k  (process noise)
z_k = x_k + v_k      (measurement noise)
```

**Process Noise:** Models GPSDO drift (very small, < 0.1 ms/min)  
**Measurement Noise:** From fusion uncertainty (0.5-2 ms)

**Bounds Check:** Reset filter if state exceeds ±10 ms (prevents divergence)

**Result:** Smooth convergence to UTC with reduced jitter

### 4.4 Uncertainty Budget (ISO GUM)

**Type A (Statistical):**

- Weighted standard error of fusion
- Reduces as √N with more broadcasts

**Type B (Systematic):**

- Tone detection: ±0.1 ms
- Propagation model: ±1-2 ms (IONEX), ±2-5 ms (IRI)
- RTP jitter: ±0.1 ms
- GPSDO stability: ±0.001 ms (negligible)

**Combined Uncertainty (RSS):**

```
u_combined = √(u_statistical² + u_systematic² + u_propagation²)
```

**Typical Values:**

- Single broadcast: ±3.6 ms (1σ)
- 13 broadcasts fused: ±1.0 ms (1σ)
- With calibration: **±0.5 ms (1σ)**

**Coverage Factors:**

- 68% (1σ): ±0.5 ms
- 95% (2σ): ±1.0 ms
- 99% (3σ): ±1.5 ms

---

## 5. Quality Grading and Validation

### 5.1 Quality Grades (A/B/C/D/F)

**Grade A** (±0.5 ms):

- 30+ detections, 60 min span
- RTP variance < 50²
- Calibrated phase
- Inter-station consistency < 1 ms
- D_clock continuity (no jumps > 5 ms)

**Grade B** (±1.0 ms):

- 10+ detections
- Provisional phase
- Some validation passing

**Grade C** (±2.0 ms):

- Bootstrap phase
- Limited validation

**Grade D/F** (> 2.0 ms):

- Insufficient data or validation failures

### 5.2 Validation Checks

**Inter-Station Consistency:**

- All stations must agree within 1 ms after calibration
- Flags `DISCRIMINATION_SUSPECT` if spread > 1 ms
- Prevents tone misidentification from contaminating fusion

**D_clock Continuity:**

- Tracks jumps between consecutive minutes
- Flags discontinuities > 5 ms
- Detects CHU frame slips (500 ms jumps)

**GPSDO Lock:**

- Filters out measurements when GPSDO unlocked
- Unlocked GPSDO can drift by seconds

**Propagation Physics:**

- Delay must be within station-specific bounds
- Ionospheric delay must follow 1/f² relationship
- Negative delays rejected

### 5.3 Allan Deviation Tracking

**Real-time stability monitoring** using overlapping Allan deviation:

```python
σ_y(τ) = √(1/(2(M-1)) × Σ(y_{i+1} - y_i)²)
```

**Typical Values:**

- τ=1s: ~1×10⁻⁹ (GPSDO-limited)
- τ=10s: ~5×10⁻¹⁰
- τ=100s: ~2×10⁻¹⁰
- τ=1000s: ~1×10⁻¹⁰

**Noise Identification:**

- White phase noise: σ(τ) ∝ τ⁻¹
- Flicker phase noise: σ(τ) ∝ τ⁰
- Random walk: σ(τ) ∝ τ⁺¹

---

## 6. System Clock Discipline (Chrony Integration)

### 6.1 Chrony SHM Refclock

**Configuration:**

```
refclock SHM 0 refid TMGR poll 4 precision 1e-3
```

**Update Cadence:**

- Fusion calculates D_clock every **8 seconds**
- Chrony polls every **16 seconds**
- Fresh data available for every poll

Single update path ensures consistent timing.

**Result:**

- Chrony reach: 87.5% (7 out of 8 polls successful)
- Chrony consistently selects TMGR as active source (#*)
- System clock disciplined to UTC(NIST) within ±0.5 ms

### 6.2 Service Resilience

**Systemd Watchdog:**

- 120-second timeout (allows HDF5 SWMR reads)
- Automatic restart if service hangs
- Heartbeat sent in main loop

**Crash Recovery:**

- Calibration persisted to disk
- Immediate Grade A performance after restart
- No warmup penalty

**Monitoring:**

- `check-chrony-reach.sh`: Monitor Chrony source health
- `timestd-chrony-monitor.timer`: Runs every 5 minutes
- Alerts if reach < 25%

---

## 7. Metrological Rigor: What Sets This Apart

### 7.1 Proper Uncertainty Quantification

**Simpler implementations:**

- Report "accuracy" without uncertainty
- No traceability to UTC
- Ignore systematic errors

**This system:**

- **ISO GUM-compliant** uncertainty budgets
- **Type A + Type B** error sources quantified
- **Coverage factors** specified (1σ, 2σ, 3σ)
- **Traceable to UTC(NIST)** via WWV/WWVH broadcasts

### 7.2 Physics-Based Validation

**Simpler implementations:**

- Accept any tone detection
- No propagation modeling
- No cross-validation

**This system:**

- **Propagation delay bounds** per station
- **Ionospheric physics** (1/f² relationship)
- **Inter-station consistency** checks
- **D_clock continuity** validation
- **GPSDO lock** verification

### 7.3 Systematic Error Correction

**Simpler implementations:**

- Raw measurements only
- No calibration
- Systematic biases uncorrected

**This system:**

- **Per-broadcast calibration** (station + frequency)
- **Exponential moving average** for tracking
- **Outlier rejection** before calibration update
- **Kalman filtering** for temporal smoothing

### 7.4 Multi-Broadcast Fusion

**Simpler implementations:**

- Single station/frequency
- ±5-10 ms accuracy
- No redundancy

**This system:**

- **9-17 independent broadcasts**
- **Inverse variance weighting** (ISO GUM)
- **√N uncertainty reduction**
- **±0.5 ms accuracy** (10x improvement)

---

## 8. Limitations and Caveats (Honest Assessment)

### 8.1 Fundamental Limits

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

### 8.2 Operating Conditions

**Best Performance:**

- Quiet to moderate geomagnetic conditions (Kp < 5)
- Stable ionosphere (no SIDs)
- Multiple frequencies propagating

**Degraded Performance:**

- Ionospheric storms (Kp > 5)
- Solar flares (X-ray absorption)
- Propagation blackouts

**Failure Modes:**

- Total blackout: Hold last value, flag HOLDOVER
- Single station outage: Continue with remaining
- Discrimination failure: Reduce weight or exclude

### 8.3 What This System Does NOT Do

**Not a frequency standard:**

- Disciplines system clock, not a standalone oscillator
- Requires GPSDO for sampling reference

**Not better than GPS:**

- GPS achieves ±10 ns, this achieves ±0.5 ms
- Useful for GPS-denied scenarios or independent validation

**Not ionospheric tomography:**

- Single receiver, limited spatial resolution
- Can measure TEC, but not layer structure

---

## 9. Validation Results

### 9.1 Internal Consistency

**Cross-Station Agreement:**

- After calibration, all stations agree within 1 ms
- Spread typically 0.3-0.8 ms
- Flags violations for investigation

**Temporal Stability:**

- D_clock drift < 0.1 ms/hour (GPSDO-limited)
- Allan deviation at τ=1000s: ~1×10⁻¹⁰
- No unexplained jumps > 5 ms

**Diurnal Pattern:**

- Follows expected ionospheric variation
- Day/night transitions visible
- Consistent with solar zenith angle

### 9.2 External Validation (Ongoing)

**GPSDO Comparison:**

- System clock disciplined by fusion
- Chrony reach: 87.5%
- Offset from GPS: < 1 ms (typical)

**IONEX TEC Validation:**

- TEC estimates from multi-frequency dispersion
- Comparison with NASA IONEX maps
- Agreement within ±2 TECU (ongoing)

---

## 10. Technical Implementation Details

### 10.1 Software Stack

**Language:** Python 3.11+  
**Key Dependencies:**

- `ka9q-python`: RTP reception and channel management
- `digital_rf`: HDF5 storage (MIT Haystack standard)
- `h5py`: HDF5 SWMR support
- `scipy`: Signal processing, Kalman filtering
- `iri2020`: Ionospheric modeling
- `systemd-python`: Watchdog integration

**Architecture:**

- 6 independent systemd services
- HDF5 SWMR for low-latency data exchange
- FastAPI web UI for monitoring
- Chrony SHM for clock discipline

### 10.2 Data Products

**Raw Data:**

- Digital RF HDF5 (24 kHz IQ)
- ~2-3 GB/day/channel
- Immutable archive for reprocessing

**Timing Measurements:**

- L2 HDF5 (D_clock + uncertainty)
- ~50-100 MB/day
- Schema-versioned, SWMR-enabled

**Fusion Results:**

- L3 HDF5 (fused D_clock)
- Chrony SHM updates
- Allan deviation tracking

**Science Products:**

- TEC estimates
- Propagation mode statistics
- Sporadic-E events
- Space weather correlations

### 10.3 Performance

**CPU Usage:**

- Core Recorder: < 5% per channel
- Analytics: Variable (batch processing)
- Fusion: < 2% (8-second cadence)

**Latency:**

- RTP → Disk: < 100 ms
- Analytics → Fusion: < 1 second (HDF5 SWMR)
- Fusion → Chrony: < 1 second

**Reliability:**

- Uptime: > 99% (systemd watchdog)
- Data completeness: > 99%
- Packet loss: < 1%

---

## 11. Conclusion: Why This Satisfies a Metrologist

### 11.1 Traceable Uncertainty Budget

Every measurement includes:

- **Type A uncertainty** (statistical)
- **Type B uncertainty** (systematic)
- **Combined uncertainty** (RSS)
- **Coverage factor** (1σ, 2σ, 3σ)

**Traceable to UTC(NIST)** via WWV/WWVH broadcasts.

### 11.2 Physics-Based Validation

Not just "it works":

- **Propagation delays** validated against physics
- **Inter-station consistency** enforced
- **D_clock continuity** checked
- **GPSDO lock** verified

### 11.3 Systematic Error Correction

Not raw measurements:

- **Calibration** removes station biases
- **Outlier rejection** prevents contamination
- **Kalman filtering** smooths temporal noise
- **Inverse variance weighting** optimizes fusion

### 11.4 Honest Limitations

We don't claim:

- Better than GPS (we're 50,000x worse)
- Ionospheric tomography (single receiver)
- Absolute timing better than ±0.5 ms

We do claim:

- **±0.5 ms (1σ) to UTC(NIST)** with proper uncertainty
- **ISO GUM-compliant** methodology
- **Physics-validated** measurements
- **Production-grade** reliability

### 11.5 The Bottom Line

This is not an amateur "time sync" project. This is a **metrological instrument** for HF time transfer with:

- Proper uncertainty quantification
- Physics-based validation
- Systematic error correction
- Multi-broadcast redundancy
- ISO GUM compliance

**For a time nut:** This achieves what GPS does, but 50,000x worse. However, it does so with proper metrology, honest uncertainty, and no reliance on GNSS.

**For a metrologist:** This is traceable to UTC(NIST), has a complete uncertainty budget, and follows ISO GUM best practices.

**For a skeptic:** The code is open source, the physics is documented, and the limitations are honestly stated. Verify it yourself.

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

## Appendix C: Contact and Source Code

**Source Code:** <https://github.com/mijahauan/hf-timestd>  
**License:** MIT  
**Author:** Michael James Hauan (AC0G)  
**Version:** 4.4.0 (January 2026)

**For questions about:**

- Measurement methodology: See `docs/TIMING_METROLOGY.md`
- Uncertainty budgets: See `src/hf_timestd/core/multi_broadcast_fusion.py`
- Validation: See `docs/SCIENTIFIC_CAPABILITIES.md`
