# HF Time Standard - System Architecture

**Last Updated:** 2026-05-24  
**Author:** Michael James Hauan (AC0G)  
**Status:** CANONICAL - Single source of truth for system design  
**Version:** 7.0.0  (single source of truth: `pyproject.toml`)

> **Foundational principles**: read
> [ARCHITECTURE-FIRST-PRINCIPLES.md](ARCHITECTURE-FIRST-PRINCIPLES.md)
> before this doc.  It states the **substrate** (the RTP sample counter
> is the timeline; UTC labels are per-sample annotations with a T-tier
> quality grade; chrony is a downstream consumer, not the design
> center).  Everything below assumes that framing.

---

## Document Purpose

This document explains **WHY** the hf-timestd system is designed the way it is. For **WHAT** functions exist, see `docs/TECHNICAL_REFERENCE.md`. For **HOW** to deploy, see `docs/DEPLOYMENT_CORRESPONDENCE_CHECKLIST.md`. For **first principles** (substrate vs annotation, T-tier hierarchy, what role chrony plays), see [ARCHITECTURE-FIRST-PRINCIPLES.md](ARCHITECTURE-FIRST-PRINCIPLES.md).

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Dual-Purpose Architecture](#dual-purpose-architecture)
3. [Timing-Only vs Full-Science Mode](#timing-only-vs-full-science-mode)
4. [Design Philosophy](#design-philosophy)
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
- Compute carrier-phase dTEC with GNSS VTEC anchoring (primary); group-delay TEC as validation
- Detect traveling ionospheric disturbances (TIDs)
- The timing accuracy enables precision ionospheric science — propagation delays are the measurement, not the error

### Key Capabilities

1. **D_clock extraction** - System clock offset relative to UTC(NIST)
2. **WWV/WWVH discrimination** on 4 shared frequencies (2.5, 5, 10, 15 MHz)
3. **Propagation mode estimation** - Ionospheric hop identification (Physics-Informed)
4. **Multi-broadcast fusion** - ±0.5 ms accuracy via weighted combination (HDF5)
5. **Carrier-phase dTEC** - Differential TEC with GNSS VTEC anchoring (~6 mTECU/min sensitivity)
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

> **Read first**: [ARCHITECTURE-FIRST-PRINCIPLES.md](ARCHITECTURE-FIRST-PRINCIPLES.md).
> The architecture rests on the RTP sample counter as the timeline,
> with UTC labels (and per-sample tier annotations) layered on top.
> "Dual purpose" here means the *same annotated sample stream* serves
> both the science pipeline and the chrony-facing convenience layer —
> they are two consumers of one product, not two parallel systems.

The system exploits a fundamental duality: the same RTP-anchored
sample stream serves both ionospheric science and host-clock
discipline.  The RTP counter is the steel ruler; UTC annotations on
top of it (at whatever T-tier the station currently supports) are
what both consumers read.

### Why there is no circular dependency

Reading older docs you'll sometimes find a "circular dependency"
framing:
- To measure the ionosphere we need good timing
- To get good timing from fusion we need to model the ionosphere

This framing was an artifact of treating chrony as the design center.
The actual architecture is **not circular** — it is a hierarchy
(see [ARCHITECTURE-FIRST-PRINCIPLES.md §2](ARCHITECTURE-FIRST-PRINCIPLES.md)):

| Active tier         | What provides UTC                            | Ionospheric science                              |
|---------------------|----------------------------------------------|--------------------------------------------------|
| **T5/T4/T6**        | Local hardware PPS / LAN NTP                 | Measured as the residual the science wants       |
| **T3** (Fusion)     | HF-station consensus, IRI-2020 path modeling | Improved iteratively from fusion residuals       |
| **T2/T1/T0**        | Whatever the host has                        | Limited by absolute-time accuracy                |

The tier in play tells us how good our UTC is for a given sample, and
the ionospheric science consumes the same annotated stream that
chrony does (when chrony is wired in at all).  Fusion-mode stations
(T3) still produce science; they just lean more on the propagation
model for absolute-time alignment because they lack a local hardware
reference.

### Station Priority Policy (v6.5)

| Station | Role | Weight | Rationale |
|---------|------|--------|----------|
| **CHU** | Reference | 100% | Unique frequencies, FSK-verified timing |
| **WWV** | Primary | 100% | Closest station, best SNR |
| **WWVH** | Primary | 100% | Independent path, cross-validation |
| **BPM** | Science-only | 0% | EXCLUDED from fusion since 2026-02-07 (long-path systematic error too large); retained as a science observable. See `multi_broadcast_fusion.py` priority dict. |

See `docs/design/METROLOGY_PHYSICS_SPLIT.md` for the canonical
two-pipeline rationale.  (The legacy `docs/design/DUAL_PURPOSE_ARCHITECTURE.md`
was renamed/superseded by the split doc; cross-references still
landing there should be redirected.)

---

## Timing-Only vs Full-Science Mode

> **Reframing**: this section was historically titled in chrony-feed
> terms.  The substrate principle is that the system always produces an
> *annotated RTP sample stream* — the question is whether the
> physics-overlay consumers run alongside the annotation core.
> Resource-constrained stations can disable physics overlays without
> affecting annotation quality.

The pipeline has a hard boundary between the functions that **produce
per-sample T-tier annotations** (always on) and the physics overlay
that consumes those annotations to produce ionospheric science
products (optional).  Operators on resource-constrained hardware
(Raspberry Pi, low-RAM systems, metered network connections) can opt
out of the physics overlay without any effect on the annotation core
(and therefore without any effect on the chrony-feed consumer if it is
wired in).

### The boundary

**Annotation core** — must run to produce the annotated RTP stream
(which any downstream consumer, including chrony, reads):

| Component | Product | Why critical |
|-----------|---------|--------------|
| `CoreRecorderV2` | Raw IQ `.bin.zst` | Source of all downstream data |
| `MetrologyEngine` tone correlator + tick matched filter + edge detector | — | Produces raw TOA measurements |
| `MetrologyEngine` WWV/WWVH/BPM discrimination | — | Identifies which station was received |
| `MetrologyService` → `metrology_measurements` writer (L1 HDF5) | L1 timing | Input to L2CalibrationService |
| `MetrologyService` → `tick_timing` writer (L2 HDF5) | Ensemble `d_clock_ms` | Highest-precision per-minute timing |
| `MetrologyService` → `chu_fsk` writer (L2 HDF5) | DUT1, TAI-UTC, leap-second detection | Guards epoch correctness |
| `L2CalibrationService` | L2 timing with propagation correction | Required by fusion |
| `MultiBroadcastFusion` + Kalman filter | — | Produces the Chrony SHM feed |

**Physics overlay** — adds science value; the annotation core (and therefore any consumer including chrony) does not need these:

| Component | Product | Controlled by |
|-----------|---------|---------------|
| `MetrologyEngine._find_all_correlation_peaks` | Secondary propagation paths | `physics_products` |
| `tick_phase` writer | 1 Hz Doppler / scintillation phase series | `physics_products` |
| `test_signal` writer | WWV/WWVH ionospheric sounding (min 8 & 44) | `physics_products` |
| `detection_attempts` writer | Threshold-calibration diagnostics | `physics_products` |
| `all_arrivals` writer | Multi-path propagation paths | `physics_products` |
| `IonoDataService` | Real-time WAM-IPE + GIRO network data | `realtime_iono` |
| `PhysicsFusionService` | TEC, tomography, VTEC maps (L3 HDF5) | Separate systemd service |
| GRAPE pipeline | Decimated IQ / PSWS upload | Separate systemd timer |

### Configuration

Add a `[metrology]` section to `/etc/hf-timestd/timestd-config.toml`:

```toml
[metrology]
# Set false to skip ionospheric science writers and the secondary-arrival
# peak search in MetrologyEngine.  The timing pipeline is unaffected.
physics_products = true

# Set false to disable real-time WAM-IPE and GIRO ionospheric data fetching.
# The propagation model falls back to climatological IRI-2020 automatically.
# Chrony discipline continues normally; u_propagation_model_ms may increase
# by up to ~5 ms at very low mode-confidence.
realtime_iono = true
```

Both flags default to `true` — existing deployments without this section are unaffected.

### What `physics_products = false` skips

With `physics_products = false`, MetrologyService initialises only the three timing-critical writers. The following are **not created** — no HDF5 files, no disk I/O, no CPU for secondary-peak search:

- `L2/tick_phase/` — 1 Hz carrier-phase time series for Doppler/scintillation analysis
- `L2/test_signal/` — ionospheric sounding amplitude/phase at WWV/WWVH test-signal minutes
- `L2/detection_attempts/` — per-attempt records for detection-threshold calibration
- `L1/all_arrivals/` — all above-threshold correlation peaks (multi-path propagation paths)

The `MetrologyEngine` also skips `_find_all_correlation_peaks()`, saving the correlation-peak search across the full 60-second window on every detected tone.

### What `realtime_iono = false` skips

With `realtime_iono = false`, the `IonoDataService` singleton is never started in either `MetrologyService` or `L2CalibrationService`. Both services fall through to the climatological fallback chain automatically:

```
WAM-IPE (real-time)  →  GIRO ionosonde  →  IRI-2020 climatology  →  parametric  →  geometric
```

On a system with no network access to NOAA/LGDC, set `realtime_iono = false` to prevent repeated network-timeout log noise and avoid the exponential backoff overhead.

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

### 4. HDF5 SWMR Pipeline (v5.0 / v6.10)

**Decision:** Use HDF5 with Single Writer Multiple Reader (SWMR) protocol for all inter-service data exchange (Phase 2 -> Phase 3).

**Why?**

- **Performance:** Binary format is 10x-100x faster than CSV parsing
- **Concurrency safety:** SWMR allows one writer and many concurrent readers with zero lock contention. Writer keeps the daily file open (`swmr_mode=True`) and calls `flush()` after each append so readers see data immediately.
- **Crash recovery:** `h5clear -s` is called **unconditionally** on every open of an existing file (not just on error). This automatically clears stale SWMR consistency flags left by unclean shutdowns (SIGKILL, service restart) — no manual intervention required.
- **Structure:** Hierarchical data storage matches the signal complexity
- **Low Latency:** Fusion sees new data within seconds of Analytics writing it

### 5. "Steel Ruler" Metrology (v5.3)

**Philosophy:** When disciplining a system with a GPSDO (stratum-1 reference), the local clock is the most stable element in the loop.

- **Concept:** We treat the GPSDO as a "Steel Ruler" (fixed, zero drift) measuring a "Rubber Sheet" (ionosphere).
- **Implementation:**
  - **Process Noise:** Low Q (0.01) for clock drift. Increased from 1e-10 to allow the filter to track real measurements while still trusting the GPSDO hardware spec (sub-ppb).
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
│    3. WWV/WWVH Discrimination (cross-freq gate + voting)       │
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

- **SWMR concurrency:** Writer keeps file open with `swmr_mode=True`; readers use `swmr=True`. Zero contention. `h5clear -s` on every writer open handles crash recovery automatically.
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
Phase 2: Metrology Service (polls for new files)
     ↓ (HDF5 crash-safe writes)
├─→ phase2/{CHANNEL}/metrology/{date}_metrology_measurements.h5 (L1/L2)
├─→ phase2/{CHANNEL}/tick_timing/{date}_tick_timing.h5
└─→ phase2/{CHANNEL}/detection_attempts/{date}_detection_attempts.h5
     ↓ (HDF5 read)
Phase 3: Fusion Service
     ↓ (Dual Kalman Filter + Physics Model)
├─→ Chrony SHM (TSL1=L1 geometric, TSL2=L2 physics-corrected)
└─→ phase2/fusion/fusion_timing_{date}.h5 (L3)
```

### Web UI Visualization

```
Web Browser
     ↓
FastAPI Monitoring Server (Python, port 8000)
     ↓ (reads HDF5 + Status JSON)
├─→ phase2/{CHANNEL}/metrology/*.h5
├─→ phase2/{CHANNEL}/tick_timing/*.h5
├─→ phase2/fusion/fusion_timing_*.h5
└─→ phase2/{CHANNEL}/state/*.json
     ↓
JSON Response → Plotly.js plots
```

---

## Timing Architecture

### Physics-Informed Propagation (v6.7)

We don't just "guess" the path; we model it using a tiered hierarchy of physics models, now driven by real-time ionospheric data.

#### Tier 0: WAM-IPE + GIRO (Real-Time, v6.7)

- **Source:** NOAA WAM-IPE 3D grids from AWS S3 (`noaa-nws-wam-ipe-pds`) and NOMADS.
- **Correction:** GIRO ionosonde measurements for real-time hmF2/foF2 at path midpoints.
- **Process:** `IonoDataService` fetches and caches data; `HFPropagationModel` numerically integrates group delay through electron density profiles.
- **Result:** Frequency-dependent, time-varying delay predictions with multi-mode support (1F, 2F, 3F, 1E).
- **Uncertainty:** ±0.5–1.0 ms (1σ) when WAM-IPE + GIRO available.

#### Tier 1: PHaRLAP 2D Ray Tracing (Implemented)

- **Engine:** PHaRLAP 4.7.4 via pyLAP Python interface (`raytrace_engine.py`).
- **Grid:** Spatially varying IRI-2020 electron density profiles sampled along the great-circle path (auto-scaled: 1 per 500 km, min 5, max 25 samples). Profiles are linearly interpolated across range columns to form a true 2D Ne(h) grid.
- **Output:** Numerical ray fan with group path delay, mode identification (1F, 2F, 3F), and reflection geometry.
- **Cost:** ~15 ms for IRI grid construction + ray tracing computation. Used for science products and QEX figures; not in the real-time metrology loop.
- **Horizontal variation:** Significant for long paths (WWVH: ±16–38% foF2 variation across 6,600 km); modest for short paths (WWV/CHU: ±1–5%).

#### Tier 1.5: IONEX VTEC (Production)

- **Source:** IGS Global Ionosphere Maps (NASA CDDIS).
- **Process:** Calculates ionospheric pierce points along the great circle path.
- **Integration:** Interpolates VTEC from the grid (lat/lon/time).
- **Result:** Provides the most accurate group delay estimation available without full reanalysis.

#### Tier 2: IRI-2020 + Geometric

- Uses the International Reference Ionosphere (IRI-2020) model to estimate layer heights (hmF2, hmE) and monthly average parameters.
- Used as a fallback or baseline for IONEX.

#### Tier 3: Parametric/Empirical

- Diurnal/seasonal parametric model with latitude dependence.
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

```
{data_root}/
├── raw_buffer/{CHANNEL}/{YYYYMMDD}/   # Phase 1: Binary IQ + JSON
│   ├── {minute}.bin.zst
│   └── {minute}.json
├── phase2/{CHANNEL}/                   # Phase 2: Metrology HDF5
│   ├── metrology/                      # L1/L2 HDF5 (primary output)
│   ├── tick_timing/                    # Per-second tick timing HDF5
│   ├── detection_attempts/             # Detection attempts HDF5
│   └── state/                          # Service state JSON
├── phase2/fusion/                      # Phase 3: Fusion Output
│   └── fusion_timing_{date}.h5         # L3 HDF5
├── phase2/science/tec/                 # TEC estimates (HDF5)
├── products/{CHANNEL}/                 # GRAPE products
│   ├── spectrograms/
│   └── decimated/
└── state/                              # Global state files
    ├── broadcast_kalman_state.json
    └── broadcast_calibration.json
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
| `timestd-physics.service` | Phase 3: Carrier-phase dTEC, group-delay TEC validation, T_iono |
| *(IonoDataService)* | Ionospheric data ingestion (WAM-IPE, GIRO) — runs as a **background thread** within metrology, not a separate service |
| `timestd-web-api.service` | Web monitoring UI (FastAPI) |
| `timestd-radiod-monitor.service` | Hardware health monitoring |

### CPU Affinity

All timestd Python services are pinned to CPUs 0-7 (`CPUAffinity=0-7` in systemd units, `taskset 0x00ff` in metrology shell script). radiod runs on CPUs 8-15 (`ff00`). This ensures radiod has uncontested L3 cache access for real-time USB/FFT processing.

### Resilience

- **Watchdogs:** All Python services integrate `systemd-python` to send heartbeat `WATCHDOG=1` notifications. If a service hangs, systemd restarts it automatically.
- **Frequent watchdog pinging (v6.8):** The physics service calls `_pet_watchdog()` between every major processing step (TEC estimation, tomography, VTEC mapping, each HDF5 write) — 17+ times per `process_minute()` cycle. This prevents the 2-minute systemd watchdog from firing during heavy I/O.
- **HDF5 SWMR (v6.10):** All HDF5 I/O uses SWMR protocol — writer holds the file open, readers open with `swmr=True`. This eliminates the write/read lock contention that previously caused the physics service crash-loop and periodic `OSError: file already open for write` errors in the web API and fusion services. `_timed_write()` (30s timeout) is retained as a belt-and-suspenders guard.
- **Alerting:** Failures trigger email alerts via `OnFailure` handlers.

---

## Performance & Reliability

### Disk Usage

- **Raw Buffer:** ~2-3 GB/day/channel
- **HDF5:** ~50-100 MB/day (significantly larger than CSV, but much richer data)

### Failure Recovery

- **Crash Safety:** Phase 1 uses atomic writes. Phase 2/3 can restart and process backlog. HDF5 SWMR dirty flags are cleared unconditionally on every writer open, so no manual `h5clear` is ever needed after a crash.
- **Backfill:** If Analytics is down for an hour, it will process the raw buffer backlog upon restart until caught up.

---

## HamSCI GRAPE Data Product

The system produces HamSCI GRAPE-compatible data products for community science. This is an independent subsystem (`src/hf_timestd/grape/`) that consumes the same raw IQ data recorded by Phase 1.

### Pipeline

```
raw_buffer/{CHANNEL}/ (24 kHz IQ, configurable chunk duration)
    ↓
grape/decimation_pipeline.py — 10 Hz IQ decimation (all 9 channels)
  • Enumerates all 1440 expected minutes per day explicitly
  • Single StatefulDecimator per channel preserves filter state across minutes
  • CIC (R=60) → compensation FIR → final FIR (R=40)
    ↓
grape/packager.py — Digital RF (DRF) packaging (MIT Haystack format)
    ↓
grape/uploader.py — SFTP upload to HamSCI PSWS network
    ↓
grape/spectrogram.py — Daily spectrograms from decimated data
  • Edge tapering at gap boundaries (half-cosine, 5s) — no zero interpolation
  • Full-window validity masking: NFFT=512 (51.2s) windows that overlap
    any gap minute are NaN-masked, not just windows centred in a gap
```

### Raw IQ File Cadence

Raw IQ files use a configurable chunk duration (`file_duration_sec`, default **600s = 10 minutes**). This reduces filesystem overhead 10× vs the legacy 1-minute cadence and improves compression ratios. The GRAPE `RawBinaryReader` handles both legacy 1-minute files and multi-minute chunks transparently — it reads `file_duration_sec` from the JSON sidecar and extracts the requested 1-minute slice.

### Scheduling

- **`grape-daily.timer`** — Triggers daily at a configured time
- **`grape-daily.service`** — Runs `grape_daily.py` which orchestrates decimation → packaging → upload → spectrogram
- **PSWS station ID:** S000171 (sftp-only: `pswsnetwork.eng.ua.edu`)

### Design Decision: Separate from Metrology

GRAPE decimation is intentionally decoupled from the timing/metrology pipeline. It operates on the immutable Phase 1 raw buffer after-the-fact, does not interfere with real-time services, and uses Digital RF format (MIT Haystack) only for GRAPE output — the rest of the system uses binary IQ + HDF5.

---

## Related Documentation

- **`docs/TECHNICAL_REFERENCE.md`** - API and algorithm details
- **`docs/METROLOGY.md`** - Metrological description and uncertainty budgets
- **`docs/PHYSICS.md`** - Ionospheric physics capabilities
- **`docs/PHASE_ENGINE_ARCHITECTURE.md`** - Multi-antenna coherent array design, scientific benefits, raw data preservation
- **`docs/DEPLOYMENT_CORRESPONDENCE_CHECKLIST.md`** - Production deployment and verification gates
- **`INSTALLATION.md`** - Setup and service configuration

---

## Unified Measurement Path (v6.11)

The v6.11 release eliminates the RTP/Fusion fork in the per-minute detection pipeline.  Both modes now share a single detection path with mode-specific post-processing.

### The Problem

Prior to v6.11, `process_minute()` contained two parallel detection branches:
- **RTP branch:** Per-second correlator with BufferTiming, edge ensemble, robust median filter
- **Fusion branch:** `tone_detector.process_samples()` with FusionTimingState search window

This fork duplicated logic, made it difficult to keep both branches feature-equivalent, and prevented Fusion mode from benefiting from edge detection and per-second analysis.

### The Solution: Unified Per-Second Correlator

Both modes now use the same detection path:
1. When `BufferTiming` is available (RTP or Fusion with GPSDO), use the per-second correlator with adaptive windowing, edge detection, and consistency filtering.
2. Only when `BufferTiming` is missing (Fusion without GPSDO), fall back to the legacy `tone_detector.process_samples()`.

### Adaptive Search Windows (v6.11)

The physics model provides a 1σ uncertainty for each station's expected delay.  This is converted to a 3σ search window and passed to `_measure_tone_at_known_time()`.  In Fusion mode, the UTC estimate uncertainty from `FusionTimingState` is added in quadrature:

```
σ_total = √(σ_physics² + σ_utc²)
search_window = 3 × σ_total
```

Three safeguards prevent the adaptive window from becoming too narrow or stale:
- **Staleness decay:** Exponential widening after 5 minutes without a detection
- **Consecutive miss counter:** Resets window to initial width after 5 consecutive misses
- **Model floor rule:** Tracked variance can only narrow below the model floor when confidence ≥ 0.95 and ≥ 30 observations

### Physics Confidence Weighting (v6.11)

The binary physics validation gate has been replaced with continuous Gaussian confidence:

```python
physics_confidence = exp(-0.5 * (deviation_sigma)²) × snr_factor
```

This confidence weights the detection's influence on both the adaptive window tracker and the Kalman filter.  Detections at >0.1 confidence feed the Fusion timing state (previously binary pass/fail).

### Multipath-Aware Uncertainty Widening (v6.11)

When CLEAN deconvolution or per-second timing spread detects multipath, the measurement uncertainty is inflated:

- **CLEAN delay spread:** Maximum `delay_offset_ms` across resolved multipath components
- **Per-second timing spread:** `ensemble_uncertainty_ms` minus noise floor (~0.5 ms)
- **Widening:** Fed to `record_detection()` as `multipath_spread_ms`, which inflates the tracked variance in quadrature: `σ_eff = √(σ_obs² + σ_mp²)`
- **Kalman penalty:** Multipath-affected detections have `physics_confidence` reduced by `1/(1 + spread/3)`

**Implementation:** `src/hf_timestd/core/metrology_engine.py`, `src/hf_timestd/core/arrival_pattern_matrix.py`  
**Design doc:** `docs/design/UNIFIED_MEASUREMENT_PATH.md`

---

## Real-Time Ionospheric Propagation Model (v6.7)

The v6.7 release replaces the static vacuum propagation model with a real-time ionospheric data-driven system.

### New Modules

| Module | Purpose |
|--------|---------|
| `propagation_model.py` | `HFPropagationModel` — multi-mode delay prediction with numerical Ne(h) integration |
| `iono_data_service.py` | `IonoDataService` — background WAM-IPE/GIRO data fetching, caching, interpolation |

### Data Flow

```
IonoDataService (background thread, 5-min cycle)
    ├── WAM-IPE NetCDF from NOAA S3/NOMADS → /var/lib/timestd/iono_cache/
    ├── GIRO ionosonde data (DIDBase API)
    └── Climatological fallback (always available)
         ↓
HFPropagationModel.predict(station, freq, utc_time)
    ├── Evaluates 4 propagation modes (1F, 2F, 3F, 1E)
    ├── Computes frequency-dependent ionospheric group delay
    ├── Returns adaptive uncertainty based on data source quality
    └── Provides self-consistency check via multi-freq differential delay
         ↓
ArrivalPatternMatrix.compute_matrix()
    ├── Primary arrivals dict (backward-compatible)
    └── Multi-mode arrivals dict (new: all feasible modes)
         ↓
MetrologyEngine._predict_geometric_delay()
    └── Uses model predictions for physics validation
         ↓
MultiBroadcastFusion.fuse()
    ├── Mode ambiguity scoring via HFPropagationModel.predict()
    ├── GNSS VTEC correction using model TEC + n_hops
    └── Chrony SHM (TSL1/TSL2)
         ↓
BootstrapValidator._get_expected_delay()
    └── Physics-based delay prediction (replaces static bounds)
         ↓
Web API (/model/predict, /model/all-stations, /model/iono-status)
    └── Live model observability
```

### Multi-Mode Arrival Support

The `ArrivalMatrix` now supports multiple propagation modes per (station, frequency):

- `arrivals[(station, freq)]` — primary (lowest-delay feasible mode), backward-compatible
- `multi_mode_arrivals[(station, freq, mode)]` — all feasible modes with independent search windows
- `get_all_mode_arrivals(station, freq)` — returns all modes sorted by delay

This enables the system to accept multi-hop arrivals (e.g., CHU 7.85 MHz 2F at night) that were previously rejected by the fixed ±50 ms window.

### Deprecated Modules

| Module | Status | Replacement |
|--------|--------|-------------|
| `physics_propagation.py` | **Deprecated v6.7** | `propagation_model.HFPropagationModel` |
| `PhysicsPropagationModel` | **Deprecated v6.7** | `HFPropagationModel` (exported from `core/__init__.py`) |

The old `PhysicsPropagationModel` used a static TIER 1/2/3 hierarchy without real-time data. All callers (`multi_broadcast_fusion.py`, `bootstrap_validator.py`, `arrival_pattern_matrix.py`) have been migrated to `HFPropagationModel`.

---

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
| **Sample Interval** | Consistent 1,440,000 samples per minute (within each chunk file) |

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
