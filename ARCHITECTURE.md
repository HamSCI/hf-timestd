# HF Time Standard - System Architecture

**Last Updated:** February 9, 2026  
**Author:** Michael James Hauan (AC0G)  
**Status:** CANONICAL - Single source of truth for system design  
**Version:** V6.6 (Authoritative RTP Timestamps + Dual-Purpose Architecture)

---

## Document Purpose

This document explains **WHY** the hf-timestd system is designed the way it is. For **WHERE** data goes, see `DIRECTORY_STRUCTURE.md`. For **WHAT** functions exist, see `docs/API_REFERENCE.md`.

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Dual-Purpose Architecture](#dual-purpose-architecture)
3. [Design Philosophy](#design-philosophy)
4. [Three-Phase Architecture](#three-phase-architecture)
5. [ka9q-python Integration](#ka9q-python-integration)
6. [Key Design Decisions](#key-design-decisions)
7. [Data Flow (HDF5-Native)](#data-flow-hdf5-native)
8. [Timing Architecture](#timing-architecture)
9. [WWV/WWVH Discrimination](#wwvwwvh-discrimination)
10. [Directory Structure](#directory-structure)
11. [Service Management](#service-management)
12. [Performance & Reliability](#performance--reliability)
13. [Failure Recovery](#failure-recovery)

---

## Executive Summary

**hf-timestd** is a dual-purpose HF monitoring system for receiving and analyzing time standard broadcasts from WWV, WWVH, CHU, and BPM. Using a GPSDO-disciplined SDR receiver with GPS+PPS authoritative timing (~50 μs via radiod RTP timestamps), the system operates in two modes: **RTP Mode** uses the known-accurate timestamps to study ionospheric propagation, while **Fusion Mode** attempts to recover UTC from the broadcasts alone. The system extracts **D_clock = T_system - T_UTC** measurements for both ionospheric science and time transfer research.

### Core Mission

The system serves a **dual purpose**:

**Purpose 1: Timing Reconstruction (Fusion Mode)**
- Reconstruct local UTC precision from multiple broadcast time-of-arrival measurements
- Correct for propagation delays (geometry + ionosphere)
- Fuse multiple independent measurements for sub-millisecond accuracy

**Purpose 2: Ionospheric Characterization (RTP Mode)**
- Measure ionospheric effects as residuals using authoritative RTP timestamps (GPS+PPS, ~50 μs)
- Compute TEC from multi-frequency observations
- Detect traveling ionospheric disturbances (TIDs)
- The timing accuracy enables precision ionospheric science — propagation delays are the measurement, not the error

### Key Capabilities

1. **D_clock extraction** - System clock offset relative to UTC(NIST)
2. **WWV/WWVH discrimination** on 4 shared frequencies (2.5, 5, 10, 15 MHz)
3. **Propagation mode estimation** - Ionospheric hop identification (Physics-Informed)
4. **Multi-broadcast fusion** - ±0.5 ms accuracy via weighted combination (HDF5)
5. **TEC estimation** - Total Electron Content from multi-frequency dispersion
6. **TID detection** - Cross-path correlation for traveling ionospheric disturbances

### Channel Configuration (17 broadcasts)

| Station | Location | Frequencies | Notes |
|---------|----------|-------------|-------|
| **WWV** | Ft. Collins, CO | 2.5, 5, 10, 15, 20, 25 MHz | 1000 Hz tone, BCD subcarrier |
| **WWVH** | Kauai, HI | 2.5, 5, 10, 15 MHz | 1200 Hz tone, shares 4 frequencies |
| **CHU** | Ottawa, Canada | 3.33, 7.85, 14.67 MHz | FSK time code (seconds 31-39) |
| **BPM** | Pucheng, China | 2.5, 5, 10, 15 MHz | 1000 Hz tone, UT1/UTC alternating |

**BPM Special Handling:**

- Minutes 0-24, 30-54: UTC timing (usable)
- Minutes 25-29, 55-59: UT1 timing (filtered out automatically)
- Tick duration: 10ms (UTC) vs 100ms (UT1)

---

## Dual-Purpose Architecture

The system exploits a fundamental duality: the same observations serve both timing and ionospheric science.

### The Circular Dependency (And Its Resolution)

There's an apparent circularity:
- To measure the ionosphere, we need good timing
- To get good timing from fusion, we need to model the ionosphere

**Resolution:** The system operates in two modes:

| Mode | Timing Source | Ionosphere Treatment | Primary Output |
|------|---------------|---------------------|----------------|
| **RTP Mode** | GPS+PPS (external) | Measured as residual | TEC, TID events |
| **Fusion Mode** | Broadcast fusion | Modeled (IRI-2020) | D_clock, UTC |

### Station Priority Policy (v6.5)

| Station | Role | Weight | Rationale |
|---------|------|--------|----------|
| **CHU** | Reference | 100% | Unique frequencies, FSK-verified timing |
| **WWV** | Primary | 100% | Closest station, best SNR |
| **WWVH** | Primary | 100% | Independent path, cross-validation |
| **BPM** | Scientific | 30% | Long path, high uncertainty, scientific interest |

See `docs/design/DUAL_PURPOSE_ARCHITECTURE.md` for detailed rationale.

---

## Design Philosophy

### 1. Separation of Concerns

```
Phase 1 (Stable)     →     Phase 2 (Evolving)     →     Phase 3 (Fusion)
  Raw Recording              Timing Analysis              Global Synthesis
  Immutable archive          Derived products             System discipline
  Code changes <5/yr         Can restart freely           HDF5 consumer
```

**Why?**

- **Scientific Integrity:** Phase 1 never drops data during Phase 2 updates
- **Reprocessability:** Improve algorithms without re-recording
- **Independent Testing:** Test analytics on archived data

### 2. RTP Timestamp as Authoritative Reference

**Decision:** Wall clock time is **DERIVED** from RTP timestamps, not vice versa. RTP timestamps are **authoritative** — radiod's `GPS_TIME` and `RTP_TIMESNAP` are both derived from `input_sample_index / decimation` (same counter space). No pipeline offset correction is needed.

**Why?**

- **Sample Count Integrity:** Gaps are unambiguous (RTP timestamp jumps)
- **Authoritative Timing:** `utc = gps_time_unix + (rtp_ts - rtp_timesnap) / sample_rate` (~50 μs accuracy)
- **No Time Stretching:** Never adjust sample count to fit wall clock
- **No Calibration Needed:** GPS+PPS time follows samples through the decimation pipeline
- **KA9Q Compatibility:** Follows Phil Karn's timing architecture

### 3. Binary Archive for Raw Data

**Decision:** Archive raw 24 kHz IQ in binary format with JSON sidecars.

**Why?**

- **Simplicity:** No external library dependencies
- **Efficiency:** Direct memory-mapped access possible
- **Compression:** Optional zstd/lz4 compression (2-3x reduction)
- **Metadata:** JSON sidecars preserve RTP timestamps and quality metrics

### 4. HDF5-Native Pipeline (v5.0)

**Decision:** Use HDF5 with crash-safe open-write-close pattern for all inter-service data exchange (Phase 2 -> Phase 3).

**Why?**

- **Performance:** Binary format is 10x-100x faster than CSV parsing
- **Crash safety:** Open-write-close per measurement means no dirty HDF5 flags on unclean shutdown
- **Structure:** Hierarchical data storage matches the signal complexity
- **Low Latency:** Fusion polls for new data within seconds of Analytics writing it

### 5. "Steel Ruler" Metrology (v5.3)

**Philosophy:** When disciplining a system with a GPSDO (stratum-1 reference), the local clock is the most stable element in the loop.

- **Concept:** We treat the GPSDO as a "Steel Ruler" (fixed, zero drift) measuring a "Rubber Sheet" (ionosphere).
- **Implementation:**
  - **Process Noise:** Extremely low Q (1e-10) for clock drift. We trust the GPSDO hardware spec (sub-ppb).
  - **Drift Clamping:** `drift_ms_per_min` is hard-clamped to 0.0 after convergence.
  - **Jitter Rejection:** High measurement noise covariance (R=30ms) forces the Kalman filter to reject ionospheric turbulence rather than chasing it.

### 6. NTP-Based Time Confirmation (v6.4)

**Architecture Change (2026-01-29):** Bootstrap no longer requires BCD/FSK decode to reach LOCKED state.

- **Problem:** BCD/FSK decoding is fragile under HF fading conditions, blocking the pipeline.
- **Solution:** Use NTP-derived wallclock (from GPS time server) to identify UTC minute directly.
- **Implementation:**
  - Cluster detection finds minute markers (800ms tones at second 0)
  - `wallclock_time` from NTP tells us WHICH minute this is in UTC
  - Bootstrap transitions to LOCKED based on NTP confirmation
  - BCD/FSK decode becomes OPTIONAL refinement for sub-second accuracy
- **Benefit:** Pipeline proceeds to metrology within ~2 minutes instead of waiting indefinitely for decode.

**Hardware Distinction (Important):**
- **GPSDO** — Disciplines the RX888 ADC clock. Provides stable sample timing (RTP timestamps are frequency-locked). Does NOT provide absolute time.
- **GPS Time Server** — Separate instrument on LAN (e.g., 192.168.0.202). Provides NTP for initial bootstrap orientation, PPS+UBX for gpsd, and GNSS VTEC data for ionospheric calculations.
- **Future Option:** PPS injection into HF stream could provide absolute sample-to-UTC alignment at the ADC level.

---

## Three-Phase Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    PHASE 1: CORE RECORDER                       │
│                   (Immutable Raw Buffer)                        │
│                                                                 │
│  Input:  ka9q-radio RTP multicast (24 kHz IQ)                  │
│  Process: Resequencing + Gap Detection + Gap Fill              │
│  Output:  {minute}.bin + {minute}.json (raw_buffer)            │
│  Location: raw_buffer/{CHANNEL}/{YYYYMMDD}/                    │
│                                                                 │
│  Responsibilities:                                              │
│  ✅ Complete data capture (no analytics)                        │
│  ✅ Sample count integrity                                      │
│  ✅ RTP timestamp preservation                                  │
│  ✅ Gap filling with zeros (maintains timing)                   │
│                                                                 │
│  Changes: <5 times per year                                    │
│  Dependencies: Minimal (numpy only)                             │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│                    PHASE 2: ANALYTICS SERVICE                   │
│              (Timing Analysis + D_clock Extraction)             │
│                                                                 │
│  Input:  Binary IQ files from Phase 1 raw_buffer               │
│  Process:                                                       │
│    1. Tone Detection (WWV/WWVH/CHU @ 1000/1200 Hz)             │
│    2. Time_snap Management (GPS-quality timestamp anchors)     │
│    3. WWV/WWVH Discrimination (8 voting methods)               │
│    4. D_clock Computation (propagation mode estimation)        │
│                                                                 │
│  Outputs (HDF5):                                                 │
│  • L1A: Tone Detections (feature extraction)                   │
│  • L2:  Timing Measurements (fully solved D_clock)             │
│  • Metadata: HDF5 attributes (processing version, etc.)        │
│                                                                 │
│  Responsibilities:                                              │
│  ✅ All derived timing products                                 │
│  ✅ Can restart/update independently                            │
│  ✅ Processes backlog automatically                             │
│  ✅ Crash-safe HDF5 writer for Fusion consumption               │
└─────────────────────────────────────────────────────────────────┘
                              ↓ (HDF5)
┌─────────────────────────────────────────────────────────────────┐
│                    PHASE 3: FUSION SERVICE (v6.1)               │
│           (Hierarchical Estimation with GNSS TEC Correction)    │
│                                                                 │
│  Input:  L2 HDF5 Measurements from Phase 2 (all channels)       │
│          GNSS VTEC from timestd-vtec service (real-time)        │
│                                                                 │
│  Process (Hierarchical Architecture):                           │
│    1. Per-Broadcast Kalman Filtering (17 independent filters)   │
│    2. GNSS VTEC Ionospheric Correction (when available)         │
│    3. Per-Station TEC Validation (1/f² physics check)           │
│    4. Weighted Least Squares Fusion (BLUE estimator)            │
│                                                                 │
│  Outputs:                                                       │
│  • Chrony SHM (System Clock Discipline)                         │
│  • L3: Fused Timing HDF5 (phase2/fusion/)                       │
│  • broadcast_kalman_state.json (17 per-broadcast states)        │
│  • broadcast_calibration.json (calibration + trust)             │
│                                                                 │
│  Responsibilities:                                              │
│  ✅ Single source of truth for system clock                      │
│  ✅ Deterministic restart (per-broadcast state persistence)     │
│  ✅ Real-time ionospheric correction via GNSS VTEC              │
│  ✅ Cross-channel consistency enforcement                       │
│  ✅ Real-time Allan Deviation tracking                          │
│  ✅ Feeds Dashboard via FastAPI                                 │
└─────────────────────────────────────────────────────────────────┘
```

---

## ka9q-python Integration

The recording layer uses **ka9q-python** directly for all RTP reception and channel management.

### Architecture Stack

```
┌─────────────────────────────────────────────────────────────────┐
│                    HF-TIMESTD APPLICATION LAYER                 │
│  CoreRecorder - Top-level orchestration                        │
│  StreamRecorderV2 - Per-channel recording via RadiodStream     │
│  PipelineOrchestrator - Phase 1/2 coordination                 │
├─────────────────────────────────────────────────────────────────┤
│                    ka9q-python (RTP + Channel Management)       │
│  RadiodStream - RTP reception, resequencing, gap detection     │
│  RadiodControl - Channel creation, configuration, tune         │
│  discover_channels() - Enumerate existing channels             │
│  StreamQuality - Completeness, packets lost, gap metrics       │
├─────────────────────────────────────────────────────────────────┤
│                    ka9q-radio (radiod)                          │
│  RTP multicast transport, GPS-disciplined timestamps           │
└─────────────────────────────────────────────────────────────────┘
```

---

## Key Design Decisions

### Decision 1: HDF5 vs CSV (v5.0 Update)

**Decision:** Migrate entire Phase 2 → Phase 3 pipeline to HDF5.

**Advantages:**

- **Crash-safe writes:** Open-write-close per measurement eliminates dirty HDF5 flags on crash.
- **Precision:** Binary float64 storage eliminates ASCII truncation errors.
- **Metadata:** Attributes store schema versions, processing flags, and processing time inside the file.
- **Compression:** HDF5 internal compression reduces disk usage vs CSV.

### Decision 2: Why Two Service Phases?

**Phase 1 isolation ensures raw data safety.**

- Phase 1 (Recording) is simple, stable, and almost never changes.
- Phase 2 (Analytics) contains complex signal processing that evolves frequently.
- By separating them, we can crash/restart/update Analytics without ever losing a raw RF sample.

### Decision 3: Global Differential Fusion

**Problem:** Individual ionospheric hops are noisy (±1ms).
**Solution:** A "Global Solve" fusion engine that sees all 9 channels simultaneously.

- It can recognize that if *all* 10 MHz signals shifted +2ms, it's likely a global ionospheric event, not a clock error.
- It uses physics-based constraints (dispersion relation) to verify channel consistency.

---

## Data Flow (HDF5-Native)

### Real-Time Pipeline

```
ka9q-radio RTP
     ↓
Phase 1: Core Recorder
     ↓ (binary IQ + JSON)
raw_buffer/{CHANNEL}/{YYYYMMDD}/{minute}.bin
raw_buffer/{CHANNEL}/{YYYYMMDD}/{minute}.json
     ↓
Phase 2: Analytics Service (polls for new files)
     ↓ (HDF5 crash-safe writes)
├─→ phase2/{CHANNEL}/tone_detections/{date}.h5 (L1A)
└─→ phase2/{CHANNEL}/timing_measurements/{date}.h5 (L2)
     ↓ (HDF5 read)
Phase 3: Fusion Service
     ↓ (Kalman Filter + Physics Model)
├─→ Chrony SHM (system clock discipline)
├─→ phase2/fusion/fusion_timing_{date}.h5 (L3)
└─→ phase2/fusion/fused_d_clock.csv (Legacy UI Support)
```

### Web UI Visualization

```
Web Browser
     ↓
FastAPI Monitoring Server (Python)
     ↓ (reads HDF5 + Status JSON)
├─→ phase2/{CHANNEL}/timing_measurements/*.h5
├─→ phase2/fusion/fusion_timing_*.h5
└─→ phase2/{CHANNEL}/status/*.json
     ↓
JSON Response → Chart.js plots
```

---

## Timing Architecture

### Physics-Informed Propagation

We don't just "guess" the path; we model it using a tiered hierarchy of physics models.

#### Tier 1: PyLap Raytracing (Experimental)

- Full 3D raytracing using the PHaRLAP engine.
- Most accurate, most computationally expensive.

#### Tier 1.5: IONEX VTEC (Production)

- **Source:** IGS Global Ionosphere Maps (NASA CDDIS).
- **Process:** Calculates ionospheric pierce points along the great circle path.
- **Integration:** Interpolates VTEC map to determine precise Total Electron Content.
- **Result:** Provides the most accurate group delay estimation available without full reanalysis.

#### Tier 2: IRI-2020 + Geometric

- Uses the International Reference Ionosphere (IRI-2020) model to estimate layer heights (hmF2, hmE) and monthly average parameters.
- Used as a fallback or baseline for IONEX.

#### Tier 3: Empirical/Geometric

- Simple geometric calculation based on virtual height assumptions.
- Fallback of last resort.

### The D_clock Equation

```
T_emit = T_arrival - (τ_geo + τ_iono + τ_mode)
```

| Component | Description |
|-----------|-------------|
| T_arrival | GPS-disciplined RTP timestamp |
| τ_geo | Great-circle speed-of-light delay |
| τ_iono | Ionospheric group delay (derived from IONEX/IRI) |
| τ_mode | Extra path from N ionospheric hops |

---

## WWV/WWVH Discrimination

### The Challenge

On shared frequencies (2.5, 5, 10, 15 MHz), WWV and WWVH transmit simultaneously. Separation is critical for timing.

### Multi-Method Discriminator

We use a **Weighted Voting** system combining:

1. **BCD Correlation (Primary):** Cross-correlate 100 Hz subcarrier to find distinct station peaks.
2. **Timing Tones:** Power ratio of 1000 Hz (WWV) vs 1200 Hz (WWVH).
3. **Station Identifiers:** Detection of 440 Hz tones (min 1/2) or 500/600 Hz tones (min 29/30/45).
4. **Test Signals:** Detection of scheduled test tones (min 08/44).

---

## Directory Structure

**Key Changes in V5.0:** Usage of `phase2` HDF5 subdirectories.

```
{data_root}/
├── raw_buffer/{CHANNEL}/{YYYYMMDD}/   # Phase 1: Binary IQ + JSON
│   ├── {minute}.bin[.zst|.lz4]
│   └── {minute}.json
├── phase2/{CHANNEL}/                   # Phase 2: Analytics HDF5
│   ├── timing_measurements/            # L2 HDF5 files (Primary Output)
│   ├── tone_detections/                # L1A HDF5 files
│   ├── clock_offset/                   # Legacy CSV (Deprecated)
│   ├── discrimination/                 # Legacy CSV (Deprecated)
│   └── status/                         # Service state JSON
├── phase2/fusion/                      # Phase 3: Fusion Output
│   ├── fusion_timing_{date}.h5         # L3 HDF5
│   ├── fused_d_clock.csv               # Quick-look CSV
│   └── tec_estimates.csv               # Science product
└── state/                              # Global state files
```

---

## Service Management

### Systemd Services

| Service | Purpose |
|---------|---------|
| `timestd-core-recorder.service` | Phase 1: RTP → raw_buffer |
| `timestd-metrology.service` | Phase 2: L1 timing analysis |
| `timestd-l2-calibration.service` | Phase 2: L2 calibrated timing |
| `timestd-fusion.service` | Phase 3: Multi-broadcast fusion & Chrony feed |
| `timestd-physics.service` | Phase 3: TEC estimation |
| `timestd-web-api.service` | Web monitoring UI (FastAPI) |
| `timestd-radiod-monitor.service` | Hardware health monitoring |

### CPU Affinity

All timestd Python services are pinned to CPUs 0-7 (`CPUAffinity=0-7` in systemd units, `taskset 0x00ff` in metrology shell script). radiod runs on CPUs 8-15 (`ff00`). This ensures radiod has uncontested L3 cache access for real-time USB/FFT processing.

### Resilience

- **Watchdogs:** All Python services integrate `systemd-python` to send heartbeat `WATCHDOG=1` notifications. If a service hangs, systemd restarts it automatically.
- **Alerting:** Failures trigger email alerts via `OnFailure` handlers.

---

## Performance & Reliability

### Disk Usage

- **Raw Buffer:** ~2-3 GB/day/channel
- **HDF5:** ~50-100 MB/day (significantly larger than CSV, but much richer data)

### Failure Recovery

- **Crash Safety:** Phase 1 uses atomic writes. Phase 2/3 can restart and process backlog.
- **Backfill:** If Analytics is down for an hour, it will process the raw buffer backlog upon restart until caught up.

---

## Related Documentation

- **`CONTEXT.md`** - Project context and quick reference
- **`CANONICAL_CONTRACTS.md`** - Overview of project standards
- **`DIRECTORY_STRUCTURE.md`** - Complete path specifications
- **`TECHNICAL_REFERENCE.md`** - API and algorithm details

---

**Last Updated:** February 9, 2026

## Physics-Based Validation (v6.5)

The v6.5 release introduces physics-based validation that replaces historical calibration:

### ArrivalPatternMatrix

Pre-computes expected arrival times for all 17 broadcasts based on:
- **Geography:** Receiver and station locations (fixed)
- **Frequency:** Affects ionospheric reflection height  
- **UTC time:** Affects ionospheric conditions via IRI-2020 model

**Key Principle:** Validate against PHYSICS, not HISTORY.

### Multi-Constraint Validation

The `TimingConsistencyValidator` exploits multiple timing constraints:

| Constraint Type | Description |
|-----------------|-------------|
| **Arrival Sequence** | Stations at different distances must arrive in order |
| **Cross-Station** | All stations transmit at UTC second 0 |
| **Cross-Frequency** | Ionospheric delay follows 1/f² law |
| **Sample Interval** | Consistent 1,440,000 samples per minute |

### Real-Time TEC Feedback

Measured TEC feeds back to refine arrival predictions:

```
τ_correction = K × TEC_measured / f²
```

This creates a virtuous cycle: better timing → better TEC → better model → better timing.

### TID Detection

Cross-path correlation detects traveling ionospheric disturbances:
- Rolling buffers of timing residuals per path
- Cross-correlation reveals TID signatures
- Estimates velocity, direction, and period

See `src/hf_timestd/core/tid_detector.py`.

---

## Metrological Enhancements (v6.2)

### Tone Detection Pipeline

The v6.2 release introduces rigorous metrological improvements to the tone detection pipeline:

| Enhancement | Description | Impact |
|-------------|-------------|--------|
| **Cramér-Rao Uncertainty** | ToA uncertainty from `σ = 1/(2π√(2×SNR×B×T))` | Rigorous per-measurement uncertainty |
| **Complex Correlation** | Phase-preserving FFT correlation | Sub-sample refinement, Doppler estimation |
| **Multipath Detection** | Peak width, secondary peaks, phase stability | Uncertainty inflation when detected |
| **Doppler Correction** | `Δt = (f_D/f_tone) × (T/2)` | Removes 0.1-2 ms systematic bias |
| **Adaptive Threshold** | CFAR-like detection rate adaptation | 10-20% sensitivity improvement |

### New Data Model Fields

`ToneDetectionResult` now includes:

```python
timing_uncertainty_ms: Optional[float]      # Cramér-Rao bound (inflated if multipath)
multipath_detected: Optional[bool]          # True if multipath indicators present
multipath_delay_spread_ms: Optional[float]  # Delay spread in ms
multipath_quality: Optional[float]          # 0-1, higher = cleaner path
doppler_hz: Optional[float]                 # Estimated Doppler shift
phase_at_peak_rad: Optional[float]          # Phase at correlation peak
```

### CHU Tick Timing (v6.2)

CHU FSK decoder now provides dual timing references:

| Reference | Precision | Method |
|-----------|-----------|--------|
| FSK Boundary | ~1-2 ms | Mark-to-silence at 500ms |
| **1000 Hz Tick** | ~0.05 ms | Edge detection (NEW) |

The tick timing is returned in `CHUFSKResult.tick_timing_offset_ms`.

---

## Adaptive Search Window System (v3.9.0)

### Design Philosophy

The system implements intelligent Bootstrap → Orient → Focus progression, leveraging GPSDO stability as a "steel ruler" for rapid convergence while handling multi-station shared frequencies.

**Key Principles:**

1. **GPSDO is the Foundation** - Provides stable time reference; stations are periodic calibration checks
2. **Per-Broadcast Tracking** - Each station+frequency has independent state (WWV@10MHz ≠ WWV@5MHz)
3. **Graceful Degradation** - Automatic back-off when detections fail, re-convergence when signals return
4. **Opportunistic Multi-Station** - Use whatever stations are available at any given time

**Phase Progression:**

- **Bootstrap** (±500ms): Wide search, no prior knowledge
- **Provisional** (±5-15ms): Medium window after 10+ detections
- **Calibrated** (±2-5ms): Narrow window after 30+ detections, 60min span

**Theoretical Foundation:** Kalman filtering applied to multi-target tracking, where each propagation path is an independent observable with its own convergence state.
