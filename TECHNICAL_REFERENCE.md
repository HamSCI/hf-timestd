# HF Time Standard (hf-timestd) - Technical Reference

**Quick reference for developers working on the HF Time Standard (hf-timestd) codebase.**

**Author:** Michael James Hauan (AC0G)  
**Last Updated:** December 30, 2025

---

## Current Operational Configuration

**9 channels** monitoring 9 frequencies at 20 kHz IQ (config-driven):

- **Shared frequencies (4):** 2.5, 5, 10, 15 MHz - WWV and WWVH both transmit
- **WWV-only (2):** 20, 25 MHz
- **CHU (3):** 3.33, 7.85, 14.67 MHz

**Data products generated**:

1. **20 kHz DRF archives** - Phase 1 immutable raw archive (`raw_archive/{CHANNEL}/`)
2. **Phase 2 analytics** - D_clock, discrimination, carrier analysis (`phase2/{CHANNEL}/`)
3. **10 Hz decimated data** - Phase 3 carrier time series (`products/{CHANNEL}/decimated/`)
4. **Spectrograms** - Phase 3 visualization with solar zenith (`products/{CHANNEL}/spectrograms/`)
5. **Timing metrics** - D_clock convergence, propagation mode identification

**Goal**: Archive raw 20 kHz IQ (Phase 1), perform timing analysis (Phase 2), generate derived products (Phase 3) for PSWS upload, provide WWV/WWVH discrimination on 4 shared frequencies.

---

## System Architecture

### Three-Service Design (V3.14 - Remediation & Feedback)

```
Core Recorder (core_recorder_v2.py)
├─ Uses ka9q-python RadiodStream for RTP reception
├─ Uses ka9q-python RadiodControl for channel management
├─ Anti-hijacking: only modifies channels with our multicast destination
├─ StreamRecorderV2 per channel → PipelineOrchestrator
└─ Binary archive writing (1,200,000 samples/minute @ 20 kHz)


Analytics Service (phase2_analytics_service.py) - per channel
├─ Unified PropagationEngine (Physics-based delays)
├─ 12 voting methods (BCD, tones, ticks, 440Hz, test signals, FSS, etc.)
├─ Feedback Loop: Calibrated offsets from Fusion -> Detection
├─ Doppler estimation
├─ Decimation (20 kHz → 10 Hz)
└─ Timing metrics

Fusion Service (multi_broadcast_fusion.py)
├─ Aggregates clock offsets from all channels
├─ Weighted Fusion + Kalman Filtering
├─ Feeds Chrony SHM for system clock discipline
└─ Updates calibration state (feedback loop)

DRF Batch Writer (drf_batch_writer.py)
├─ 10 Hz NPZ → Digital RF HDF5
├─ Multi-subchannel format (9 frequencies in ch0)
└─ SFTP upload to PSWS with trigger directories
```

**Why split?** Core stability vs analytics experimentation. Analytics can restart without data loss.

### ka9q-python Components Used

| Component | Purpose |
|-----------|--------|
| `RadiodStream` | RTP reception, packet resequencing, gap detection, sample decoding |
| `RadiodControl` | Channel creation, configuration, tune commands |
| `discover_channels()` | Enumerate existing channels from radiod status |
| `StreamQuality` | Completeness %, packets lost/resequenced, gap count |
| `ChannelInfo` | Channel metadata (frequency, preset, sample_rate, destination) |
| `StreamManager` | Manages channel reuse and deterministic SSRC allocation to prevent proliferation |

---

## Recent Fixes (v3.2.1 - 2025-12-30)

### Analytics Pipeline & HDF5 SWMR Integration

**IRI-2020 Array Handling:** Fixed incompatibility with updated `iri2020` package that returns `xarray.DataArray` instead of scalars. Added `_extract_scalar()` helper to normalize all IRI output types.

**Bootstrap Second Boundary:** Fixed propagation solver calculating wrong second boundary (36 seconds ahead). Now correctly rounds to nearest second using RTP timestamp modulo.

**HDF5 Schema Compliance:** Added missing `processing_version` field to L1A channel observables to satisfy schema requirements.

**HDF5 SWMR Visibility:** Fixed data visibility issue where analytics was writing successfully but fusion couldn't read. Added explicit `refresh()` calls after `flush()` to update SWMR metadata for concurrent readers.

**Pipeline Status:** Complete end-to-end operation verified - Recorder → Analytics → HDF5 (SWMR) → Fusion → Chrony SHM all working.

---

## Critical Design Principles

### 1. RTP Timestamp is Primary Reference

**Not wall clock.** System time is derived from RTP via time_snap.

```python
# Precise time reconstruction:
utc = time_snap_utc + (rtp_ts - time_snap_rtp) / sample_rate
```

**Source**: Phil Karn's ka9q-radio design (pcmrecord.c)

**Ingestion Hardening (v3.13.0):**
The `BinaryArchiveWriter` now uses a **Streaming Mean** over the first 50 data chunks to determine the initial `rtp_to_unix_offset`. This prevents transient NTP jitter at startup (±5-10ms) from locking in a permanent offset error for the duration of the file.

### 2. Sample Count Integrity

**Invariant**: 20 kHz × 60 sec = 1,200,000 samples (exactly)

- Gaps filled with zeros
- Sample count never adjusted
- Discontinuities logged for provenance

**SSRC Stability (v3.13.0):**
`ChannelRecorder` delegates channel creation to `StreamManager`. This ensures that a channel (e.g., "WWV 10 MHz") is **reused** if it already exists in `radiod`, preventing the proliferation of duplicate SSRC streams that exhaust system resources on service restarts.

### 3. Channels Share GPS Clock, Not RTP Origin

Each ka9q-radio stream has a **different RTP timestamp origin** (arbitrary starting value):

```
WWV 5 MHz:   RTP 304,122,240
WWV 10 MHz:  RTP 302,700,560  ← Different origin, but same clock rate
```

**However**, all channels are driven by **the same GPS-disciplined master clock**. This means:

- ❌ Cannot copy raw RTP timestamp values between channels
- ✅ CAN share UTC anchor time across channels (the "master RTP ruler")
- ✅ CAN use arrival time on one channel to predict arrival on another (within ionospheric dispersion)

This is the foundation of **cross-channel coherent processing** - see [Timing Architecture](#timing-architecture).

### 4. Timing Quality > Rejection

**Always upload, annotate quality.** No binary accept/reject.

- TONE_LOCKED (±1ms): time_snap from WWV/CHU with PPM correction
- NTP_SYNCED (±10ms): NTP fallback
- INTERPOLATED: Aged time_snap with drift compensation
- WALL_CLOCK (±sec): Unsynchronized

### 5. PPM-Corrected Timing

**ADC clock drift compensation** for sub-sample precision:

```python
# Measure actual vs nominal sample rate
ppm = ((rtp_elapsed / utc_elapsed) / nominal_rate - 1) * 1e6
clock_ratio = 1 + ppm / 1e6

# Apply correction
elapsed_seconds = (rtp_ts - time_snap_rtp) / sample_rate * clock_ratio
utc = time_snap_utc + elapsed_seconds
```

**Precision**: ±10-25 μs at 20 kHz with parabolic peak interpolation

---

## NPZ Archive Format

**20 kHz Archive Fields** (self-contained scientific record):

```python
{
    # PRIMARY DATA
    "iq": complex64[1200000],             # Gap-filled IQ samples (60 sec @ 20 kHz)
    
    # TIMING REFERENCE
    "rtp_timestamp": uint32,              # RTP timestamp of iq[0]
    "rtp_ssrc": uint32,                   # RTP stream identifier
    "sample_rate": int,                   # 20000 Hz (config-driven)
    
    # TIME_SNAP ANCHOR (embedded for self-contained files)
    "time_snap_rtp": uint32,              # RTP at timing anchor
    "time_snap_utc": float,               # UTC at timing anchor
    "time_snap_source": str,              # "wwv_startup", "ntp", etc.
    "time_snap_confidence": float,        # Confidence 0-1
    "time_snap_station": str,             # "WWV", "CHU", "NTP"
    
    # TONE POWERS (for discrimination - avoids re-detection)
    "tone_power_1000_hz_db": float,       # WWV/CHU marker tone
    "tone_power_1200_hz_db": float,       # WWVH marker tone
    "wwvh_differential_delay_ms": float,  # WWVH-WWV propagation delay
    
    # METADATA
    "frequency_hz": float,                # Center frequency
    "channel_name": str,                  # "WWV 10 MHz"
    "unix_timestamp": float,              # RTP-derived file timestamp
    "ntp_wall_clock_time": float,         # Wall clock at minute boundary
    "ntp_offset_ms": float,               # NTP offset from centralized cache
    
    # QUALITY INDICATORS
    "gaps_filled": int,                   # Total zero-filled samples
    "gaps_count": int,                    # Number of discontinuities
    "packets_received": int,              # Actual packets
    "packets_expected": int,              # Expected packets
    
    # GAP DETAILS (scientific provenance)
    "gap_rtp_timestamps": uint32[],       # RTP where each gap started
    "gap_sample_indices": uint32[],       # Sample index of each gap
    "gap_samples_filled": uint32[],       # Samples filled per gap
    "gap_packets_lost": uint32[]          # Packets lost per gap
}
```

**Why embedded time_snap?** Each file is self-contained - can reconstruct UTC without external state.

---

## RTP Packet Parsing (CRITICAL)

### Bug History (Oct 30, 2025)

Three sequential bugs corrupted all data before Oct 30 20:46 UTC:

#### Bug #1: Byte Order

```python
# WRONG:
samples = np.frombuffer(payload, dtype=np.int16)  # Little-endian

# CORRECT:
samples = np.frombuffer(payload, dtype='>i2')     # Big-endian (network order)
```

#### Bug #2: I/Q Phase

```python
# WRONG: I + jQ (carrier offset -500 Hz)
iq = samples[:, 0] + 1j * samples[:, 1]

# CORRECT: Q + jI (carrier centered at 0 Hz)
iq = samples[:, 1] + 1j * samples[:, 0]
```

#### Bug #3: Payload Offset

```python
# WRONG: Hardcoded
payload = data[12:]

# CORRECT: Calculate from header
payload_offset = 12 + (header.csrc_count * 4)
if header.extension:
    ext_length_words = struct.unpack('>HH', data[payload_offset:payload_offset+4])[1]
    payload_offset += 4 + (ext_length_words * 4)
payload = data[payload_offset:]
```

**Lesson**: Always parse RTP headers fully. Never hardcode offsets.

---

## Timing Architecture

### Time Reference Hierarchy

```
┌──────────────────────────────────────────────────────────────┐
│ 1. RTP TIMESTAMP (Primary Reference)                        │
│    • GPS-disciplined via radiod                            │
│    • 20 kHz sample rate (config-driven)                     │
│    • Common reference across ALL channels                   │
└──────────────────────────────────────────────────────────────┘
                         ↓
┌──────────────────────────────────────────────────────────────┐
│ 2. TIME_SNAP (GPS-Quality Anchor)                           │
│    • WWV/CHU 1000 Hz tone at :00.000                       │
│    • Sub-sample peak detection via parabolic interpolation │
│    • PPM correction for ADC clock drift                    │
│    • Precision: ±10-25 μs at 20 kHz                         │
└──────────────────────────────────────────────────────────────┘
                         ↓
┌──────────────────────────────────────────────────────────────┐
│ 3. CROSS-CHANNEL COHERENT PROCESSING                        │
│    • Global Station Lock across 9-12 frequencies            │
│    • Ensemble anchor selection (best SNR wins)              │
│    • Guided search: ±500 ms → ±3 ms (99.4% noise rejection)  │
└──────────────────────────────────────────────────────────────┘
                         ↓
┌──────────────────────────────────────────────────────────────┐
│ 4. PRIMARY TIME STANDARD (HF Time Transfer)                 │
│    • Back-calculate UTC(NIST) emission time                │
│    • T_emit = T_arrival - (τ_geo + τ_iono + τ_mode)         │
│    • Mode identification via quantized layer heights        │
│    • Accuracy: ±10 ms → ±0.5 ms with full processing         │
└──────────────────────────────────────────────────────────────┘
```

### time_snap Mechanism

**Purpose**: Anchor RTP to UTC via WWV/CHU tone detection with PPM correction.

```python
# Basic time reconstruction
utc = time_snap_utc + (rtp_ts - time_snap_rtp) / sample_rate

# With PPM correction for ADC clock drift
clock_ratio = 1 + ppm / 1e6
utc = time_snap_utc + (rtp_ts - time_snap_rtp) / sample_rate * clock_ratio
```

**Accuracy Progression**:

| Stage | Accuracy |
|-------|----------|
| Raw arrival time | ±10 ms |
| + Tone detection | ±1 ms |
| + PPM correction | ±25 μs |
| + Mode identification | ±2 ms (emission) |
| + Cross-channel consensus | ±0.5 ms (emission) |

### Global Station Lock

Because radiod's RTP timestamps are GPS-disciplined, all channels share a common "ruler". This enables treating 9-12 receivers as a **single coherent sensor array**.

**The Physics**:

```
Frequency dispersion:     < 2-3 ms   (group delay between HF bands)
Station separation:       15-20 ms  (WWV Colorado vs WWVH Hawaii)
Discrimination margin:    ~5×       (dispersion << separation)
```

**Three-Phase Detection**:

1. **Anchor Discovery** - Find high-confidence locks (SNR > 15 dB) across all channels
2. **Guided Search** - Narrow search window from ±500 ms to ±3 ms using anchor (99.4% noise rejection)
3. **Coherent Stacking** - Virtual channel with SNR improvement of 10·log₁₀(N) dB

### Unified Propagation Engine (v3.13.0)

**Purpose**: A single "source of truth" implementation for all physics-based delay calculations, shared by `StationModel` (Phase 2 discrimination) and `TransmissionTimeSolver` (Phase 3 timing).

**Hierarchy**:

1. **Geometric**: Great-circle speed-of-light delay (baseline).
2. **Heuristic**: Empirical delays based on station distance (if IRI unavailable).
3. **IRI-2020**: Full ionospheric ray-tracing (highest precision).

```python
# Unifies delay calculation across the pipeline
engine = PropagationEngine(enable_iri=True)
delay = engine.estimate_delay(
    tx_lat, tx_lon, rx_lat, rx_lon, frequency_hz, method='GEOMETRIC'
)
```

### Primary Time Standard (HF Time Transfer)

Back-calculate emission time from GPS-locked arrival time:

```
T_emit = T_arrival - (τ_geo + τ_iono + τ_mode)
```

| Component | Description |
|-----------|-------------|
| T_arrival | GPS-disciplined RTP timestamp |
| τ_geo | Great-circle speed-of-light delay |
| τ_iono | Ionospheric group delay (frequency-dependent) |
| τ_mode | Extra path from N ionospheric hops |

**Propagation Mode Identification** (quantized by layer heights):

| Mode | Typical Delay | Uncertainty |
|------|---------------|-------------|
| 1-hop E | 3.82 ms | ±0.20 ms |
| 1-hop F2 | 4.26 ms | ±0.17 ms |
| 2-hop F2 | 5.51 ms | ±0.33 ms |
| 3-hop F2 | ~7.0 ms | ±0.50 ms |

### PPM Correction Implementation

```python
class TimeSnapReference:
    """Immutable timing anchor with PPM correction."""
    rtp_timestamp: int       # RTP at anchor point
    utc_timestamp: float     # UTC at anchor point  
    sample_rate: int         # Nominal sample rate
    ppm: float               # ADC clock drift in parts per million
    ppm_confidence: float    # 0-1 confidence in PPM estimate
    
    @property
    def clock_ratio(self) -> float:
        return 1.0 + self.ppm / 1e6
    
    def calculate_sample_time(self, sample_rtp: int) -> float:
        elapsed_samples = sample_rtp - self.rtp_timestamp
        elapsed_seconds = elapsed_samples / self.sample_rate * self.clock_ratio
        return self.utc_timestamp + elapsed_seconds
    
    def with_updated_ppm(self, new_ppm: float, confidence: float) -> 'TimeSnapReference':
        # Exponential smoothing for stability
        blended_ppm = self.ppm * (1 - confidence) + new_ppm * confidence
        return TimeSnapReference(..., ppm=blended_ppm, ...)
```

**Tone-to-Tone PPM Measurement**:

```python
# Measure actual ADC clock vs nominal
ppm = ((rtp_elapsed / utc_elapsed) / nominal_rate - 1) * 1e6
# Typical values: ±50-200 ppm for consumer SDRs
```

### Clock Convergence Model (v3.8.0)

**Philosophy: "Set, Monitor, Intervention"**

With a GPSDO-disciplined receiver, the local clock is a secondary standard. Instead of constantly recalculating D_clock, we converge to a locked estimate and then monitor for anomalies.

```
State Machine:
ACQUIRING (N<10) → CONVERGING (building stats) → LOCKED (monitoring)
                                                       ↓
                                              5 anomalies → REACQUIRE
```

**Implementation** (`src/grape_recorder/grape/clock_convergence.py`):

```python
class ClockConvergenceModel:
    """Per-station convergence tracking with anomaly detection."""
    
    # Lock criteria
    lock_uncertainty_ms = 1.0    # uncertainty < 1ms required
    min_samples_for_lock = 30    # need 30 minutes of data
    anomaly_sigma = 3.0          # 3σ for anomaly detection
    
    # Welford's online algorithm for running statistics
    def update_accumulator(self, station_key, d_clock_ms):
        acc = self.accumulators[station_key]
        acc.count += 1
        delta = d_clock_ms - acc.mean
        acc.mean += delta / acc.count
        delta2 = d_clock_ms - acc.mean
        acc.M2 += delta * delta2
        
    @property
    def uncertainty_ms(self) -> float:
        """σ/√N - shrinks with each measurement."""
        if self.count < 2:
            return float('inf')
        variance = self.M2 / (self.count - 1)
        return math.sqrt(variance / self.count)
```

**Convergence Timeline**:

| Time | State | Uncertainty | Quality Grade |
|------|-------|-------------|---------------|
| 0-10 min | ACQUIRING | ∞ | D |
| 10-30 min | CONVERGING | ~10 ms | C |
| **30+ min** | **LOCKED** | **< 1 ms** | **A/B** |

**Key Insight**: Once locked, residuals = real ionospheric propagation effects!

```python
residual_ms = raw_measurement - converged_d_clock
# |residual| > 3σ → anomaly → propagation event detected
```

### Propagation Mode Probability (v3.8.0)

Mode probabilities use Gaussian likelihood based on converged uncertainty:

```python
# P(mode|measured) ∝ exp(-0.5 × ((measured - expected) / σ)²)
sigma = sqrt(uncertainty² + mode_spread²)
z_score = (measured_delay - expected_delay) / sigma
likelihood = exp(-0.5 * z_score²)
```

| Uncertainty | Discrimination Quality |
|-------------|----------------------|
| > 30 ms | Flat (no information) |
| 10-30 ms | Weak peaks |
| 3-10 ms | Moderate |
| **< 3 ms** | **Sharp peaks** ✓ |

### The Core Metric: `d_clock`

**Definition:** `d_clock` is the difference between the **Local System Clock** (GPS-disciplined) and the **True UTC(NIST)** time at the point of emission, after correcting for all known propagation delays.

$$ d\_{clock} = T_{system} - T_{UTC(NIST)} $$

In a perfectly synchronized system with a perfect propagation model, `d_clock` would be exactly **0.0 ms**. In practice, it acts as the "residual" that captures:

1. **Clock Error**: Small deviations in the local oscillator (typically < 0.001 ms for GPSDO).
2. **Propagation Anomalies**: Unmodeled changes in the ionosphere path length.

#### How It Is Calculated

We effectively measure the "Total Time of Flight" and subtract the "Expected Physics":

$$ d\_{clock} = T_{arrival} - T_{emission} - ( \tau_{geo} + \tau_{iono} + \tau_{mode} ) $$

- **$T_{arrival}$:** Measured using `rtp_timestamp` (GPS-quality precision).
- **$T_{emission}$:** Known schedule (e.g., top of minute).
- **$\tau_{geo}$:** Speed-of-light delay over the Great Circle path.
- **$\tau_{iono}$:** Expected group delay through the ionosphere.
- **$\tau_{mode}$:** Extra path length from multi-hop reflections (e.g., 2-hop F-layer).

#### interpreting `d_clock` Variations

Once the system is **LOCKED**, `d_clock` becomes a powerful scientific sensor. Since the GPS clock is stable, any variation in `d_clock` represents a change in the **Ionosphere**:

- **Stable ~0.0 ms:** Propagation matches the physics model (Quiet Ionosphere).
- **Positive (+) Deviation:** The signal arrived **later** than expected.
  - **Cause:** Higher electron density (signal slows down), Higher reflection layer (longer path), or Storm conditions.
- **Negative (-) Deviation:** The signal arrived **earlier** than expected.
  - **Cause:** Lower reflection layer (e.g., E-layer sporadic event, shorter path).

#### Contribution to UTC(NIST) Limit

To estimate true **UTC(NIST)** from the received signal, we invert the equation:

$$ T_{UTC(NIST)} = T_{system} - d\_{clock} $$

This calculated time is used to timestamp scientific data products, ensuring they are aligned with the international standard regardless of propagation conditions.

### Timing Calibrator (v3.13.0)

The `TimingCalibrator` manages the progression from initial bootstrap to verified timing:

```
┌─────────────────────────────────────────────────────────────────────────┐
│                    TIMING CALIBRATOR STATE MACHINE                       │
│                                                                          │
│  BOOTSTRAP                    CALIBRATED                   VERIFIED      │
│  ├─ Wide search (±500ms)      ├─ Narrow search (±50ms)     ├─ Locked    │
│  ├─ Learning RTP offsets      ├─ Using RTP prediction      ├─ Monitoring│
│  ├─ Collecting detections     ├─ Refining calibration      ├─ Anomaly   │
│  └─ Exit: 5+ from 2+ stations └─ Exit: Kalman converged    │   detection│
└─────────────────────────────────────────────────────────────────────────┘
```

**Key Features:**

1. **RTP Calibration**: Stores `rtp_offset = rtp_timestamp % 1,200,000` for each channel
2. **Station Prediction**: Uses RTP offset to predict which station should be detected
3. **Multi-Process Coordination**: Reloads state before update, saves after each detection

**State File**: `state/timing_calibration.json`

```json
{
  "phase": "bootstrap",
  "station_calibration": {
    "WWV": {"propagation_delay_ms": 6.5, "n_samples": 50},
    "CHU": {"propagation_delay_ms": 4.0, "n_samples": 30}
  },
  "rtp_calibration": {
    "WWV 10 MHz": {
      "rtp_offset_samples": 254382,
      "detected_station": "WWV",
      "n_confirmations": 10
    }
  },
  "stats": {
    "bootstrap_detections": 50,
    "discrimination_corrections": 3
  }
}
```

**RTP-Based Station Prediction:**

```python
def predict_station(self, channel_name, rtp_timestamp, detected_station, confidence):
    """Use RTP calibration history to predict expected station."""
    rtp_cal = self.rtp_calibration.get(channel_name)
    if not rtp_cal:
        return (detected_station, 0.0)
    
    current_offset = rtp_timestamp % self.samples_per_minute
    expected_offset = rtp_cal.rtp_offset_samples
    offset_diff_ms = abs(current_offset - expected_offset) / self.sample_rate * 1000
    
    if offset_diff_ms < 5.0:
        # Strong match - predict same station as calibration
        predicted_station = rtp_cal.detected_station
        if detected_station != predicted_station and confidence != 'high':
            # Override low-confidence detection with RTP prediction
            return (predicted_station, 0.9)
    
    return (detected_station, 0.0)
```

### Multi-Broadcast Fusion (v3.14+)

Combines all available broadcasts (WWV/WWVH/CHU/BPM) to converge on UTC(NIST) alignment.

**Implementation:** `src/hf_timestd/core/multi_broadcast_fusion.py`

#### Per-broadcast calibration

Calibration is maintained per **broadcast** (station + frequency) to account for frequency-dependent systematic offsets.

```python
# Broadcast key used by fusion
broadcast_key = f"{station}_{frequency_mhz:.2f}"

# Calibrated D_clock (if calibration exists)
calibrated = d_clock_ms + calibration[broadcast_key].offset_ms
```

#### Cross-frequency global differential fusion (physics-verified constraint)

Fusion also performs a *cross-frequency* global physics solve using `GlobalDifferentialSolver`:

- **Input:** per-channel `tone_detections/*_tones_YYYYMMDD.csv`
- **Minute selection:** uses the **latest common minute** across channels with tone data in the lookback window (intersection). If no intersection exists, falls back to the latest available minute and logs the fallback.
- **Timing representation:** the solver only needs minute-relative timing, so fusion reconstructs:

```
arrival_rtp := timing_ms * sample_rate
minute_boundary_rtp := 0
```

When the global solve returns `verified=True`, fusion injects a trusted synthetic measurement:

- `station = GLOBAL_DIFF`
- Strong forced weighting (dominates the weighted mean)
- Not subject to outlier rejection
- Excluded from calibration updates and TEC estimation
- Kalman measurement uncertainty floor is reduced (acts like a hard constraint)

#### Observability (required for trusted-source behavior)

Fusion logs the global solve decision context and results:

- `Global solve context: target_minute=... obs=... mix=[...] dropped_channels=[...]`
- `Global solve: cross-agency triangulation active (NIST+NRC) ...` (when both WWV/WWVH and CHU are present)
- `Global solve result: target_minute=... offset_ms=... verified=... conf=... consistency_ms=...`
- `Injecting GLOBAL_DIFF: ... force_weight=... kalman_floor_ms=...`

#### Output columns

`phase2/fusion/fused_d_clock.csv` includes:

- `global_solve_verified`
- `global_solve_consistency_ms`
- `global_solve_n_obs`

**Weighting Factors**:

- SNR (higher = more reliable)
- Quality grade (A=1.0, B=0.8, C=0.5, D=0.2)
- Propagation mode (1-hop > 2-hop > 3-hop)

**Convergence Indicators** (displayed per-station):

| Progress | Status | Meaning |
|----------|--------|---------|
| ≥95% | ✓ Locked | Calibration stable |
| 50-95% | Converging | Learning in progress |
| <50% | Learning | Initial phase |
| 0% | No signal | Station not received |

**Accuracy Achieved**:

| Configuration | Accuracy |
|--------------|----------|
| Single broadcast, uncalibrated | ±5-10 ms |
| Single broadcast, calibrated | ±1-2 ms |
| **Multi-broadcast fusion** | **±0.5 ms** |

**API Endpoint**: `/api/v1/timing/fusion`

```json
{
  "status": "active",
  "latest": {
    "d_clock_fused_ms": -0.0017,
    "d_clock_raw_ms": -3.78,
    "n_broadcasts": 52,
    "quality_grade": "B"
  },
  "calibration": {
    "WWV": { "offset_ms": 3.53, "n_samples": 100 },
    "WWVH": { "offset_ms": 13.74, "n_samples": 42 },
    "CHU": { "offset_ms": 5.06, "n_samples": 84 }
  }
}
```

---

--------------------------------------------------------------------------------

## Multi-Station Discrimination (WWV/WWVH/BPM)

### The Shared Frequencies

On **2.5, 5, 10, and 15 MHz**, three major stations transmit simultaneously:

1. **WWV** (Fort Collins, CO)
2. **WWVH** (Kauai, HI)
3. **BPM** (Xi'an, China)

Separating these signals is critical for accurate timing. The system uses a **Probabilistic Discriminator** backed by a specialized **BPM Discriminator** to decompose the received signal.

### Probabilistic Discrimination (Active Mode)

The legacy "Voting Method" has been replaced by a **Logistic Regression Model** that calculates the probability of each station being dominant:

$$ P(WWV|x) = \sigma(w \cdot x + b) $$

Where $x$ is a vector of extracted signal features. This approach handles correlated features (like power ratio and BCD ratio) correctly, unlike simple voting which over-counts them.

#### Information Flow

```
Raw IQ Samples
   │
   ├─► Tone Detector ───────► SNR, Timing, Tick Durations
   ├─► WWVH Discriminator ──► Power Ratios, BCD Correlation, Doppler
   └─► BPM Discriminator ───► BPM-specific Ticks (10ms/100ms)
            │
            ▼
   Feature Vector (x)
   [PowerRatio, BCDRatio, DopplerDiff, 440Hz, GroundTruth...]
            │
            ▼
   Probabilistic Model (Logistic Regression)
            │
            ▼
   P(WWV), P(WWVH), P(BPM)
```

### Feature Vectors

The model uses the following normalized features to make its decision:

1. **Power Ratio ($P_{WWV} - P_{WWVH}$):** The difference in signal strength at 1000 Hz and 1200 Hz.
2. **Audio BCD Correlation:** Does the received BCD waveform match the propagation delay for WWV or WWVH?
3. **Doppler Stability:** WWV (continental) usually has different Doppler characteristics than WWVH (trans-oceanic).
4. **440 Hz Presence:** Strong indicator during Minutes 1 (WWVH) and 2 (WWV).
5. **Ground Truth Schedule:** Explicit knowledge of silent minutes (e.g., WWV silent min 43-51) forces the probability to 0 or 1.

### Dealing with BPM

BPM is handled as a third party in the discrimination logic:

1. **Tick Duration Filter:** BPM uses 10ms ticks (UTC) or 100ms ticks (UT1). WWV/WWVH use 5ms ticks. The `BPMDiscriminator` isolates BPM energy based on this pulse width.
2. **Schedule Awareness:** The system knows BPM's active hours (e.g., 2.5 MHz 07:30-01:00 UTC) and suppresses false positives outside these times.
3. **UT1 Exclusion:** During BPM's UT1 minutes (25-29, 55-59), BPM is excluded from the UTC fusion pool to prevent timing contamination.

### "Detect Both" Capability

The system acknowledges that often **both** stations are present.
- **Dominant Station:** The station with $P > 0.8$.
- **Balanced:** If $0.2 < P(WWV) < 0.8$, the system flags `BALANCED` or `UNCERTAIN`, indicating useful energy from both sources. This triggers **Component Decomposition** to measure and log both signals separately.

### Scientific Rationale: Why Identify "Dominance"?

One might ask: *If we measure both stations, why force a choice of "Dominant Station"?*

**1. Operational Necessity (The Timing Prerequisite)**
We calculate the system time offset (`d_clock`) by subtracting the propagation delay.
- **"Bootstrap" (Finding Time):** When the system starts, it has no idea what time it is (within ±500ms). Dominance is **critical** here. If we mistake WWVH for WWV, we introduce a massive **16ms error**, preventing the lock.
- **"Steady State" (Locked):** Once detailed time is known (`d_clock` ≈ 0), the system uses *station-specific templates* to hunt for signals exactly where they should be (e.g., WWV at +4ms, WWVH at +20ms). However, Dominance remains the **Primary Switch** to decide which signal drives the high-precision `d_clock` output to the NTP server. We can't discipline the clock to two different times simultaneously.
- **Path Difference:** The path from Hawaii (WWVH) is ~15-20ms longer than from Colorado (WWV).

**2. Scientific Value of the Ratio (Emergent Physics)**
While "Dominance" is an operational switch, the continuous **Power Ratio** ($P_{WWV} / P_{WWVH}$) reveals atmospheric dynamics that single-station observation misses:

- **The Terminator "Crossover":** The moment when dominance flips (0 dB ratio) precisely marks the passage of the **Day/Night Terminator** between the two paths. The steepness of this transition measures ionization rates during sunrise/sunset.
- **Destructive Interference Zones:** When the ratio is near 0 dB ("Balanced"), carrier waves often destructively interfere. Identifying this state explains why decoding might fail despite high signal strength.
- **Antenna Characterization:** Persistent bias in the ratio (e.g., never hearing WWVH) calibrates the receiving station's westward nulls.

---

```
WWV (1000 Hz)  → time_snap (timing reference)
CHU (1000 Hz)  → time_snap (timing reference)
WWVH (1200 Hz) → Propagation study (science data)
```

**Differential delay** = WWVH - WWV arrival time difference (ionospheric path)

---

## International Stations: BPM (China)

BPM (National Time Service Center, Xi'an) uses a system distinct from WWV/WWVH/CHU. It actively switches between **UTC** and **UT1** standards within the same hour, and its signals are emitted **20 milliseconds in advance** of UTC.

### Signal Characteristics

- **Tone Frequency:** 1000 Hz (same as WWV/CHU)
- **Modulation:** AM
- **Timing Advance:** -20 ms (Signal emitted at $T_{UTC} - 20ms$)
  - This advance partially compensates for propagation delay to users in China.
  - For US receivers, this results in a net arrival time that often overlaps with WWV (~8ms delay) or WWVH (~15ms delay).

### Marker Formats & Schedule

BPM alternates between two formats based on the minute of the hour:

| Minutes | Content | Tick Duration | Standard |
|---------|---------|---------------|----------|
| **00-10** | UTC Time | **10 ms** | UTC |
| **10-15** | Carrier Only | *None* | - |
| **15-25** | UTC Time | **10 ms** | UTC |
| **25-29** | **UT1 Time** | **100 ms** | **UT1** |
| **29-30** | Station ID | *None* | - |
| **30-40** | UTC Time | **10 ms** | UTC |
| **40-45** | Carrier Only | *None* | - |
| **45-55** | UTC Time | **10 ms** | UTC |
| **55-59** | **UT1 Time** | **100 ms** | **UT1** |
| **59-60** | Station ID | *None* | - |

> [!NOTE]
> **Detection Impact**:
>
> - **UT1 Minutes (25-29, 55-59):** The 100ms ticks are unique and easy to distinguish from WWV's 5ms ticks. However, they encode UT1, which drifts relative to UTC.
> - **"Flam" Effect:** During UT1 minutes, you may hear a double-tick (click-beep) because the BPM UT1 tick (100ms) drifts against the WWV UTC tick (5ms).

> - **UTC Minutes:** Use 10ms ticks (vs WWV 5ms). This duration difference helps discrimination.

### Search Window Exclusion Zones (v3.14.0)

To prevent the **BPM Discriminator** from incorrectly locking onto a strong **WWV** signal (aliasing), we employ **Exclusion Zones**.

- **Problem**: WWV (Colorado) often arrives ~25ms after the BPM window center (relative time). If BPM is weak, the detector might "find" WWV and claim it is BPM.
- **Solution**: Since we know `time_snap` is accurate (±1ms), we calculate exactly where WWV should appear in the BPM search window and **mask it out**.
- **Implementation**: `StationModel` defines `exclusion_zones` (e.g., `[(22.5, 27.5)]` ms). Any correlation peak within this zone is assigned 0.0 confidence.

---

--------------------------------------------------------------------------------

## International Stations: CHU (Canada)

The CHU digital time code is transmitted from **second 31 to second 39** of every minute. It allows systems to automatically decode the precise time, separate from the primary 1000Hz ticks.

### Transmission Format

The digital code uses the **Bell 103 Audio Frequency-Shift Keying (AFSK)** standard.

- **Data Rate:** 300 baud
- **Modulation:** Frequency-Shift Keying (FSK)
- **Frequencies:**
  - **Mark (bit 1):** 2225 Hz
  - **Space (bit 0):** 2025 Hz
- **Serial Coding:** 8N2 (1 Start, 8 Data, 2 Stop)

During seconds 31-39, the standard pulses are reduced to very short ticks to accommodate the data. The entire 110-bit packet (10 \text{ chars} \times 11 \text{ bits/char}) takes about **0.367 seconds** and ends precisely at **0.500 seconds** past the second marker.

### Time Code Content (Formats)

The 10-byte packet alternates between **Format A** and **Format B**. Each format repeats its data for redundancy (except for the ancillary data distinction).

#### 1. Format A (Seconds 32-39)

Sent 8 times per minute. Contains **UTC Time and Day of Year**. Bytes 1-5 are repeated in Bytes 6-10.

| Field | Content |
|-------|---------|
| **6** | Framing constant (6) |
| **ddd** | Day of Year (001-366) |
| **hh** | UTC Hour |
| **mm** | UTC Minute |
| **ss** | UTC Second |

#### 2. Format B (Second 31 Only)

Sent once per minute. Contains **DUT1, Year, and TAI offset**. Bytes 6-10 are the bitwise inversion (one's complement) of Bytes 1-5.

| Field | Content |
|-------|---------|
| **x** | Leap Second Warning & DUT1 Sign |
| **z** | DUT1 (tenths of a second) |
| **yyyy** | Year (e.g., 2025) |
| **tt** | TAI-UTC Difference (currently 37s) |
| **aa** | Canadian Daylight Time Indicator |

### Time Standards & Corrections

CHU broadcasts key corrections needed for scientific timing:

#### TAI-UTC (Leap Second Offset)

* **TAI (Atomic):** Continuous atomic time scale (never skips).
- **UTC (Civil):** Adjusted with **Leap Seconds** to track Earth's rotation.
- **Difference:** TAI is currently **37 seconds ahead** of UTC.
- **Usage:** Format B allows automatic conversion from UTC to linear TAI.

#### DUT1 (Rotation Correction)

* **UT1 (Astronomical):** True solar time based on Earth's varying rotation.
- **DUT1:** The fine difference $DUT1 = UT1 - UTC$.
- **Values:** Broadcast in **0.1s increments**. International standards keep $|UTC - UT1| < 0.9s$.

---

## File Paths: Python/JavaScript Sync

**Problem**: Dual-language system needs identical paths.

**Solution**: Centralized APIs

**Python** (`src/grape_recorder/paths.py`):

```python
class GRAPEPaths:
    def get_quality_csv_path(self, channel):
        return self.analytics_dir / channel / "quality" / f"{channel}_quality.csv"
```

**JavaScript** (`web-ui/grape-paths.js`):

```javascript
class GRAPEPaths {
    getQualityCSVPath(channel) {
        return path.join(this.analyticsDir, channel, 'quality', `${channel}_quality.csv`);
    }
}
```

**Validation**: `./scripts/validate-paths-sync.sh`

---

## Configuration

### Environment-Based Configuration

**Environment File** (single source of truth for paths):

| Mode | Environment File | Data Root |
|------|-----------------|-----------|
| Test | `config/environment` | `/tmp/timestd-test/` |
| Production | `/etc/hf-timestd/environment` | `/var/lib/timestd/` |

```bash
# Production environment file
GRAPE_MODE=production
GRAPE_DATA_ROOT=/var/lib/timestd
GRAPE_LOG_DIR=/var/log/grape-recorder
GRAPE_CONFIG=/etc/hf-timestd/timestd-config.toml
GRAPE_VENV=/opt/grape-recorder/venv
```

### Config File

**File**: `config/timestd-config.toml` (or `/etc/hf-timestd/timestd-config.toml` in production)

```toml
[station]
callsign = "AC0G"
grid_square = "EM38ww"

[ka9q]
status_address = "myhost-hf-status.local"  # mDNS name from radiod config

[recorder]
mode = "test"                              # "test" or "production"
test_data_root = "/tmp/timestd-test"
production_data_root = "/var/lib/timestd"
sample_rate = 20000                        # Config-driven (default 20 kHz)

[[recorder.channels]]
ssrc = 10000000
frequency_hz = 10000000
preset = "iq"
description = "WWV 10 MHz"
enabled = true
processor = "grape"
```

---

## Installation & Startup

### Using install.sh (Recommended)

```bash
# Test mode (development)
./scripts/install.sh --mode test
./scripts/timestd-all.sh -start

# Production mode (24/7 operation)
sudo ./scripts/install.sh --mode production --user $USER
sudo systemctl start grape-recorder timestd-analytics grape-webui
sudo systemctl enable grape-recorder timestd-analytics grape-webui
```

### Manual Startup (Development)

```bash
cd ~/grape-recorder
source venv/bin/activate
python -m grape_recorder.grape.core_recorder --config config/timestd-config.toml
```

### Production (systemd)

```bash
# Service control
sudo systemctl start|stop|status grape-recorder
sudo systemctl start|stop|status timestd-analytics
sudo systemctl start|stop|status grape-webui

# View logs
journalctl -u grape-recorder -f
journalctl -u timestd-analytics -f

# Enable daily uploads
sudo systemctl enable --now grape-upload.timer
```

### Directory Structure

| Mode | Data | Logs | Config |
|------|------|------|--------|
| Test | `/tmp/timestd-test/` | `/tmp/timestd-test/logs/` | `config/` |
| Production | `/var/lib/timestd/` | `/var/log/grape-recorder/` | `/etc/hf-timestd/` |

---

## Data Flow (Three-Phase Architecture)

```
ka9q-radio (radiod)
    ↓ RTP multicast (mDNS discovery via ka9q-python)
PHASE 1: Core Recorder (core_recorder.py)
    ↓ 20 kHz DRF archive
    ↓ {data_root}/raw_archive/{channel}/
    ↓ {data_root}/raw_buffer/{channel}/ (binary minute buffers)
PHASE 2: Analytics Service (per channel)
    ├→ D_clock: phase2/{channel}/clock_offset/
    ├→ Discrimination: phase2/{channel}/discrimination/
    ├→ BCD correlation: phase2/{channel}/bcd_correlation/
    ├→ Carrier analysis: phase2/{channel}/carrier_analysis/
    └→ State: phase2/{channel}/state/
PHASE 3: Derived Products
    ├→ Decimated 10 Hz: products/{channel}/decimated/
    ├→ Spectrograms: products/{channel}/spectrograms/
    └→ SFTP upload to PSWS
```

---

## Key Modules

### Core Infrastructure (`src/grape_recorder/core/`)

- `recording_session.py` - Generic RTP→segments session manager
- `rtp_receiver.py` - Multi-SSRC RTP demultiplexer
- `packet_resequencer.py` - RTP packet ordering & gap detection

### Stream API (`src/grape_recorder/stream/`)

- `stream_api.py` - `subscribe_stream()` and convenience functions
- `stream_manager.py` - SSRC allocation, lifecycle, stream sharing
- `stream_spec.py` - Content-based stream identity
- `stream_handle.py` - Opaque handle returned to applications

### GRAPE Application (`src/grape_recorder/grape/`)

**Core Recording:**

- `grape_recorder.py` - Two-phase recorder (startup → recording)
- `grape_npz_writer.py` - SegmentWriter for NPZ output
- `core_recorder.py` - Top-level GRAPE orchestration
- `analytics_service.py` - NPZ watcher, 12-method processor

**Timing (Advanced):**

- `time_snap_reference.py` - Immutable timing anchor with PPM correction
- `ppm_estimator.py` - ADC clock drift measurement, exponential smoothing
- `tone_detector.py` - 1000/1200 Hz timing tones with sub-sample peak detection
- `startup_tone_detector.py` - Initial time_snap establishment
- `global_station_voter.py` - Cross-channel anchor tracking
- `station_lock_coordinator.py` - Three-phase coherent detection
- `propagation_mode_solver.py` - N-hop geometry, mode identification
- `primary_time_standard.py` - UTC(NIST) back-calculation

**Discrimination:**

- `wwvh_discrimination.py` - 12 voting methods, cross-validation
- `discrimination_csv_writers.py` - Per-method CSV output
- `bcd_discriminator.py` - 100 Hz time code dual-peak detection
- `tick_analyzer.py` - 5ms tick coherent/incoherent analysis
- `test_signal_analyzer.py` - Minutes :08/:44 channel sounding

**Processing:**

- `decimation.py` - 20 kHz → 10 Hz (multi-stage CIC+FIR)
- `doppler_estimator.py` - Per-tick frequency shift measurement

### WSPR Application (`src/grape_recorder/wspr/`)

- `wspr_recorder.py` - Simple recorder for WSPR
- `wspr_wav_writer.py` - SegmentWriter for 16-bit WAV output

### DRF & Upload

- `drf_batch_writer.py` - 10 Hz NPZ → Digital RF HDF5
- Wsprdaemon-compatible multi-subchannel format

### Infrastructure

- `paths.py` - Centralized path management (GRAPEPaths API)
- `channel_manager.py` - Channel configuration

### Web UI (`web-ui/`)

- `monitoring-server-v3.js` - Express API server
- `grape-paths.js` - JavaScript path management (synced with Python)

---

## Dependencies

**Python 3.10+** (installed via `install.sh` or `pip install -e .`):

- `ka9q-python` - Interface to ka9q-radio (from github.com/mijahauan/ka9q-python)
- `numpy>=1.24.0` - Array operations
- `scipy>=1.10.0` - Signal processing, decimation
- `digital_rf>=2.6.0` - Digital RF HDF5 format
- `zeroconf` - mDNS discovery for radiod
- `toml` - Configuration parsing
- `soundfile` - Audio file I/O (compatibility)

**Node.js 18+** (for web-ui):

- `express` - API server
- `ws` - WebSocket support
- See `web-ui/package.json` for full list

**System**:

- `avahi-utils` - mDNS resolution
- `libhdf5-dev` - Required for digital_rf

**Installation** (automated):

```bash
./scripts/install.sh --mode test      # Development
sudo ./scripts/install.sh --mode production --user $USER  # Production
```

---

## Testing

### Verify Installation

```bash
source venv/bin/activate  # or /opt/grape-recorder/venv/bin/activate
python3 -c "import digital_rf; print('Digital RF OK')"
python3 -c "from ka9q import discover_channels; print('ka9q-python OK')"
python3 -c "from grape_recorder.grape.time_snap_reference import TimeSnapReference; print('TimeSnapReference OK')"
```

### Test Recorder

```bash
./scripts/timestd-all.sh -start
# Should see: channel connections, NPZ file writes
```

### Verify Output Files

```bash
ls /tmp/timestd-test/archives/WWV_10_MHz/*.npz
# Should show timestamped NPZ files
```

### Verify Timing

```bash
python3 -c "
import numpy as np
from pathlib import Path
f = sorted(Path('/tmp/timestd-test/archives/WWV_10_MHz/').glob('*.npz'))[-1]
d = np.load(f, allow_pickle=True)
print(f'Time_snap source: {d[\"time_snap_source\"]}')
print(f'PPM: {d.get(\"ppm\", \"N/A\")}')
print(f'Clock ratio: {d.get(\"clock_ratio\", \"N/A\")}')
"
```

---

## Debugging

### Check NPZ Contents

```bash
python3 -c "
import numpy as np
from pathlib import Path
f = sorted(Path('/tmp/timestd-test/archives/WWV_10_MHz/').glob('*.npz'))[-1]
d = np.load(f, allow_pickle=True)
print(f'File: {f.name}')
print(f'Samples: {len(d[\"iq\"])}')
print(f'Gaps: {d[\"gaps_count\"]}')
print(f'Completeness: {100*(1 - d[\"gaps_filled\"]/len(d[\"iq\"])):.1f}%')
print(f'Time_snap source: {d[\"time_snap_source\"]}')
print(f'1000 Hz power: {d[\"tone_power_1000_hz_db\"]:.1f} dB')
"
```

### Check Web UI API

```bash
curl http://localhost:3000/api/v1/summary | jq
```

---

## Common Issues

### Issue: Cannot connect to radiod

**Symptom**: "Failed to discover channels" error

**Causes**:

1. radiod not running
2. mDNS name not resolving
3. Multicast network issue

**Fix**:

```bash
# Check radiod is running
sudo systemctl status radiod@rx888

# Test mDNS resolution
avahi-resolve -n myhost-hf-status.local

# Test ka9q-python discovery
python3 -c "from ka9q import discover_channels; print(discover_channels('myhost-hf-status.local'))"
```

### Issue: High packet loss

**Symptom**: Completeness < 95%, many gaps

**Causes**:

1. Network congestion
2. CPU overload
3. radiod issues

**Fix**: Check network buffers, reduce channel count if needed:

```bash
sudo sysctl -w net.core.rmem_max=26214400
```

### Issue: Timing quality degraded

**Symptom**: time_snap_source shows "ntp" instead of "wwv_startup"

**Causes**:

1. Poor propagation (no WWV/CHU signal)
2. Startup tone detection failed

**Fix**: Normal during poor propagation. System falls back to NTP timing (±10ms vs ±1ms).

---

## Performance Targets

### Core Recorder

- CPU: <5% per channel
- Memory: ~100 MB total
- Disk write: ~2 MB/min per channel (compressed NPZ)
- Latency: <100 ms (RTP → disk)

### Analytics

- CPU: Variable (batch processing)
- Processing: Can lag behind real-time
- 6 discrimination methods per minute per channel

---

## Quality Metrics

### Timing Quality Levels

| Level | Accuracy | Source | Description |
|-------|----------|--------|-------------|
| **TONE_LOCKED** | ±25 μs | WWV/CHU tone + PPM | Sub-sample peak detection with ADC drift correction |
| **TONE_LOCKED** | ±1 ms | WWV/CHU tone | Standard tone detection without PPM |
| **NTP_SYNCED** | ±10 ms | System NTP | NTP fallback when no tone detected |
| **INTERPOLATED** | ±1 ms/hr | Aged time_snap | Drifts ~1 ms/hour without refresh |
| **WALL_CLOCK** | ±seconds | System clock | Unsynchronized, mark for reprocessing |

### Cross-Channel Timing Quality

| Metric | Target | Description |
|--------|--------|-------------|
| **Station Lock** | >90% channels | High-confidence tone detection across array |
| **Anchor Consensus** | <1 ms spread | All channels agree on station arrival time |
| **PPM Consistency** | <10 ppm | ADC drift should be stable across session |

### Data Completeness

- **Target:** >99% samples received
- **Gaps:** Zero-filled, logged in NPZ metadata
- **Packet loss:** <1% healthy
- **Completeness colors:** 🟢 ≥99% | 🟡 95-99% | 🔴 <95%

---

## References

### Key Documents

- `ARCHITECTURE.md` - System design decisions
- `DIRECTORY_STRUCTURE.md` - Path conventions
- `CANONICAL_CONTRACTS.md` - API standards
- `INSTALLATION.md` - Setup guide
- `docs/PRODUCTION.md` - Production deployment with systemd

### External

- ka9q-radio: <https://github.com/ka9q/ka9q-radio>
- ka9q-python: <https://github.com/mijahauan/ka9q-python>
- Digital RF: MIT Haystack Observatory

---

**Version**: 2.2.0  
**Last Updated**: December 2, 2025  
**Purpose**: Technical reference for GRAPE Signal Recorder developers

**v2.2.0 Release (Dec 2, 2025):**

- **Unified Install Script** - `install.sh` for test/production modes
- **FHS-Compliant Paths** - `/var/lib/timestd/`, `/var/log/grape-recorder/`
- **systemd Services** - Production-ready 24/7 operation
- **Cross-Channel Coherent Timing** - Global Station Lock, ensemble anchor selection
- **Primary Time Standard** - UTC(NIST) back-calculation from arrival time
- **PPM Correction** - ADC clock drift compensation (±25 μs precision)
- **Documentation Overhaul** - All root-level docs updated for consistency

**v2.1.0 (Dec 1, 2025):**

- **Package Restructure** - `core/`, `stream/`, `grape/`, `wspr/` packages
- **Stream API** - SSRC-free `subscribe_stream()` interface
- **ka9q-python 3.1.0** - Compatible SSRC allocation algorithm
- **Sample Rate** - 20 kHz (was 16 kHz)

**v2.0.0 (Nov 30, 2025):**

- **Generic Recording Infrastructure** - Protocol-based design for multi-app support
- **GRAPE Refactor** - `GrapeRecorder`, `GrapeNPZWriter`
- **12 Voting Methods** - FSS, noise coherence, spreading factor added
- **Test Signal Channel Sounding** - Full exploitation of :08/:44 minutes

**Previous (Nov 28-29, 2025):**

- 12 cross-validation checks
- 500/600 Hz weight boosted to 15 for exclusive minutes
- Doppler vote changed to std ratio
- Coherence quality check, harmonic signature validation
