# HF Time Standard Analysis - System Architecture

**Last Updated:** December 16, 2025  
**Author:** Michael James Hauan (AC0G)  
**Status:** CANONICAL - Single source of truth for system design  
**Version:** V4.0 (Two-Phase Pipeline)

---

## Document Purpose

This document explains **WHY** the hf-timestd system is designed the way it is. For **WHERE** data goes, see `DIRECTORY_STRUCTURE.md`. For **WHAT** functions exist, see `docs/API_REFERENCE.md`.

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Design Philosophy](#design-philosophy)
3. [Two-Phase Architecture](#two-phase-architecture)
4. [ka9q-python Integration](#ka9q-python-integration)
5. [Key Design Decisions](#key-design-decisions)
6. [Data Flow](#data-flow)
7. [Timing Architecture](#timing-architecture)
8. [WWV/WWVH Discrimination](#wwvwwvh-discrimination)
9. [Directory Structure](#directory-structure)
10. [Service Management](#service-management)
11. [Performance & Reliability](#performance--reliability)
12. [Failure Recovery](#failure-recovery)

---

## Executive Summary

**hf-timestd** is a precision HF monitoring system for receiving and analyzing time standard broadcasts from WWV, WWVH, CHU, and BPM. Using a GPSDO-disciplined SDR receiver, the system extracts **D_clock = T_system - T_UTC** measurements with sub-millisecond accuracy for ionospheric propagation studies and time transfer.

### Core Mission

Extract precise timing measurements from HF time standard broadcasts:

1. **D_clock extraction** - System clock offset relative to UTC(NIST)
2. **WWV/WWVH discrimination** on 4 shared frequencies (2.5, 5, 10, 15 MHz)
3. **Propagation mode estimation** - Ionospheric hop identification
4. **Multi-broadcast fusion** - ±0.5 ms accuracy via weighted combination

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

## Design Philosophy

### 1. Separation of Concerns

```
Phase 1 (Stable)     →     Phase 2 (Evolving)
  Raw Recording              Timing Analysis
  Changes <5/year            Can restart freely
  Immutable archive          Derived products
```

**Why?**

- **Scientific Integrity:** Phase 1 never drops data during Phase 2 updates
- **Reprocessability:** Improve algorithms without re-recording
- **Independent Testing:** Test analytics on archived data

### 2. RTP Timestamp as Primary Reference

**Decision:** Wall clock time is **DERIVED** from RTP timestamps, not vice versa.

**Why?**

- **Sample Count Integrity:** Gaps are unambiguous (RTP timestamp jumps)
- **Precise Reconstruction:** `utc = time_snap_utc + (rtp_ts - time_snap_rtp) / sample_rate`
- **No Time Stretching:** Never adjust sample count to fit wall clock
- **KA9Q Compatibility:** Follows Phil Karn's timing architecture

### 3. Binary Archive for Raw Data

**Decision:** Archive raw 20 kHz IQ in binary format with JSON sidecars.

**Why?**

- **Simplicity:** No external library dependencies
- **Efficiency:** Direct memory-mapped access possible
- **Compression:** Optional zstd/lz4 compression (2-3x reduction)
- **Metadata:** JSON sidecars preserve RTP timestamps and quality metrics

### 4. Independent Discrimination Methods

**Decision:** Eight voting methods + cross-validation for WWV/WWVH discrimination.

**Why?**

- **Robustness:** If one method fails, others still work
- **Confidence:** Multiple confirmations increase reliability
- **Provenance:** Clear data lineage for each result
- **Scientific Rigor:** Document how conclusions reached

---

## Two-Phase Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    PHASE 1: CORE RECORDER                       │
│                   (Immutable Raw Buffer)                        │
│                                                                 │
│  Input:  ka9q-radio RTP multicast (20 kHz IQ)                  │
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
│    5. Multi-Broadcast Fusion (Kalman filter convergence)       │
│                                                                 │
│  Outputs:                                                       │
│  • clock_offset/         D_clock time series (PRIMARY OUTPUT)  │
│  • discrimination/       Station identification results        │
│  • tone_detections/      1000/1200 Hz detection results        │
│  • bcd_correlation/      100 Hz subcarrier analysis            │
│  • carrier_analysis/     Amplitude, phase, Doppler             │
│  • status/               Service state for web UI              │
│                                                                 │
│  Responsibilities:                                              │
│  ✅ All derived timing products                                 │
│  ✅ Can restart/update independently                            │
│  ✅ Processes backlog automatically                             │
│  ✅ Chrony SHM integration for system clock discipline          │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│                    PHASE 3: FUSION SERVICE                      │
│                (Multi-Broadcast UTC Alignment)                  │
│                                                                 │
│  Input:  CSV files from Phase 2 (clock offsets)                 │
│  Process:                                                       │
│    1. Weighted Fusion (SNR, Quality, Mode)                      │
│    2. Kalman Filtering (Convergence to UTC)                     │
│    3. Global Differential Solve (Cross-frequency physics)       │
│                                                                 │
│  Outputs:                                                       │
│  • Chrony SHM (System Clock Discipline)                         │
│  • phase2/fusion/fused_d_clock.csv                              │
│  • state/broadcast_calibration.json (Feedback to Phase 2)       │
│                                                                 │
│  Responsibilities:                                              │
│  ✅ Single source of truth for system clock                      │
│  ✅ Cross-channel consistency enforcement                       │
│  ✅ Optional calibration feedback to analytics                  │
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

### Key Files

| File | Purpose |
|------|---------|
| `core_recorder.py` | Top-level orchestration using RadiodControl |
| `stream_recorder_v2.py` | Per-channel recording using RadiodStream |
| `pipeline_orchestrator.py` | Phase 1/2 coordination |
| `binary_archive_writer.py` | Raw binary + JSON metadata output |
| `phase2_analytics_service.py` | Continuous Phase 2 processing daemon |
| `phase2_temporal_engine.py` | Core timing analysis algorithms |

---

## Key Design Decisions

### Decision 1: Why Binary Archives (not NPZ)?

**Binary + JSON Advantages:**

- **No Dependencies:** Pure Python, no numpy required for reading
- **Memory Mapping:** Direct mmap access for large files
- **Streaming:** Can write continuously without buffering
- **Compression:** Optional zstd/lz4 at file level
- **Metadata Separation:** JSON sidecars are human-readable

### Decision 2: Why Two Phases (not Three)?

**Phase 1 + Phase 2 is sufficient for hf-timestd:**

- Phase 1: Immutable scientific record (raw IQ)
- Phase 2: All timing analysis and derived products

**What's NOT in hf-timestd:**

- Decimation to 10 Hz (not needed for timing)
- Digital RF format conversion
- PSWS/HamSCI uploads
- Spectrogram generation

These belong in separate projects if needed.

### Decision 3: Why Separate Services?

**Phase 1 Isolation:**

- ✅ **Stability:** Minimal code changes (rock-solid)
- ✅ **No Data Loss:** Phase 2 can crash, Phase 1 keeps recording
- ✅ **Simple:** ~300 lines, minimal dependencies

**Phase 2 Independence:**

- ✅ **Evolution:** Update algorithms without downtime
- ✅ **Testing:** Replay archived data for validation
- ✅ **Reprocessing:** Improve historical analysis
- ✅ **Restart Safe:** Processes backlog automatically

### Decision 4: Why 8 Discrimination Methods?

**Problem:** Single method can fail due to propagation conditions.

**Solution:** Multiple independent analyses with weighted voting.

**Voting Methods:**

1. Test Signal Detection (minutes :08, :44) - weight=15
2. 440 Hz Station ID (minutes 1, 2) - weight=10
3. BCD Amplitude Ratio (100 Hz subcarrier) - weight=2-10
4. 1000/1200 Hz Timing Tone Power Ratio - weight=1-10
5. Tick SNR Average (59-tick coherent integration) - weight=5
6. 500/600 Hz Ground Truth (12 exclusive min/hour) - weight=10-15
7. Doppler Stability (std ratio, independent of power) - weight=2
8. Timing Coherence (Test + BCD ToA agreement) - weight=3

---

## Data Flow

### Real-Time Recording

```
ka9q-radio RTP
     ↓
Phase 1: Core Recorder
     ↓ (binary IQ + JSON)
raw_buffer/{CHANNEL}/{YYYYMMDD}/{minute}.bin
raw_buffer/{CHANNEL}/{YYYYMMDD}/{minute}.json
     ↓
Phase 2: Analytics Service (polls for new files)
     ↓ (processes)
├─→ phase2/{CHANNEL}/clock_offset/clock_offset_series.csv
├─→ phase2/{CHANNEL}/discrimination/{date}.csv
├─→ phase2/{CHANNEL}/tone_detections/{date}.csv
├─→ phase2/{CHANNEL}/bcd_correlation/{date}.csv
└─→ phase2/{CHANNEL}/status/analytics-status.json
     ↓
├─→ phase2/{CHANNEL}/status/analytics-status.json
           ↓
Fusion Service (polls for new CSVs)
           ↓
├─→ Chrony SHM (system clock discipline)
└─→ phase2/fusion/fused_d_clock.csv
└─→ Web UI (monitoring dashboard)
```

### Web UI Visualization

```
Web Browser
     ↓
Node.js Monitoring Server
     ↓ (reads CSVs + status)
├─→ phase2/{CHANNEL}/clock_offset/*.csv
├─→ phase2/{CHANNEL}/discrimination/*.csv
└─→ phase2/{CHANNEL}/status/*.json
     ↓
JSON Response → Chart.js plots
```

---

## Timing Architecture

### GPSDO-First Calibration Philosophy

**Core Principle:** The GPSDO-disciplined RTP timestamps provide a stable, high-precision timing foundation.

```
┌─────────────────────────────────────────────────────────────────┐
│                    GPSDO-FIRST TIMING CALIBRATION               │
│                                                                 │
│  LAYER 1: GPSDO Foundation (±0.1 PPM, ~100 ns/sec drift)       │
│  ├─ RTP timestamps from GPS-disciplined ka9q-radio             │
│  ├─ All 9 channels share the same master clock                 │
│  └─ Sample count integrity: 1,200,000 samples = 60 seconds     │
│                              ↓                                  │
│  LAYER 2: Tone Detection (±1 ms initial, ±50 µs refined)       │
│  ├─ WWV/WWVH 1000/1200 Hz tones at second 0                    │
│  ├─ CHU 1000 Hz tone at second 0 (500ms duration)              │
│  ├─ Per-second tick confirmations (59 per minute)              │
│  └─ CHU FSK timing (seconds 31-39) for verification            │
│                              ↓                                  │
│  LAYER 3: Station-Level Calibration                            │
│  ├─ Each station (WWV, WWVH, CHU) has ONE atomic clock         │
│  ├─ Station mean is ground truth; frequency variance = prop.   │
│  └─ Calibration offset brings station mean to UTC(NIST) = 0    │
│                              ↓                                  │
│  LAYER 4: Multi-Broadcast Fusion (±0.5 ms)                     │
│  ├─ Weighted average across broadcasts                         │
│  ├─ Kalman filter for convergence and anomaly detection        │
│  └─ Output: Fused D_clock → Chrony SHM → System clock          │
└─────────────────────────────────────────────────────────────────┘
```

### Cross-Frequency Global Differential Fusion (Physics-Verified Constraint)

**Decision:** A cross-frequency physics solve is performed inside `core/multi_broadcast_fusion.py` using `GlobalDifferentialSolver`.

**Why?** Phase 2 is inherently per-channel/per-frequency. The fusion layer is the first place where measurements from *all* channels coexist, so it is the natural point to run a global differential solve that can combine observations from:

- Same frequency / different station (WWV vs WWVH)
- Different frequency / same station (dispersion constraints)
- Different frequency / different station (global consistency)

**Input source:** The global solve is built from per-channel `tone_detections/*_tones_YYYYMMDD.csv` files. For each observation we reconstruct a minute-relative arrival sample count:

```
arrival_rtp := timing_ms * sample_rate
minute_boundary_rtp := 0
```

This is sufficient for differential solving because the absolute anchor cancels in pairwise differences, assuming all channels reference the same system clock minute boundary.

**Latest common minute selection:** To avoid mixing observations across changing ionosphere, the global solver uses the most recent `minute_boundary` that exists across all channels with tone data in the lookback window (intersection). If no intersection exists, it logs and falls back to the latest available minute.

**CHU inclusion:** `phase2_analytics_service.py` writes CHU fields (`chu_detected`, `chu_snr_db`, `chu_timing_ms`) into new tone-detections CSVs. When present, CHU enables cross-agency triangulation (NIST + NRC) constraints.

**Fusion integration (trusted synthetic measurement):** When the global solve returns `verified=True`, fusion injects a synthetic measurement:

- `station = GLOBAL_DIFF`
- Large forced weight during fusion (dominates the weighted mean)
- Kalman measurement uncertainty floor reduced (acts like a trusted constraint)

This provides a physics-verified override path without rewriting Phase 2 per-channel outputs.

**Observability:** The fusion layer logs global solve decision context:

- Selected target minute, station/frequency mix, and any channels missing data at the target minute
- Explicit log when NIST+NRC triangulation is active
- Global solve result summary (offset/confidence/consistency)
- Synthetic injection parameters

**Fused output columns:** `phase2/fusion/fused_d_clock.csv` includes:

- `global_solve_verified`
- `global_solve_consistency_ms`
- `global_solve_n_obs`

### The D_clock Equation

```
T_arrival = T_emission + T_propagation + D_clock

Where:
  T_arrival     = Observed tone arrival time (from matched filter)
  T_emission    = 0 (tones transmitted at exact second boundary)
  T_propagation = HF signal propagation delay (ionospheric path)
  D_clock       = System clock offset (THE OUTPUT WE WANT)

Rearranging:
  D_clock = T_arrival - T_propagation
```

### Primary Time Standard (HF Time Transfer)

By back-calculating emission time from GPS-locked arrival time and identified propagation mode, we transform from a **passive listener** into a **primary time standard** that verifies UTC(NIST).

**The Equation:**

```
T_emit = T_arrival - (τ_geo + τ_iono + τ_mode)
```

| Component | Description |
|-----------|-------------|
| T_arrival | GPS-disciplined RTP timestamp |
| τ_geo | Great-circle speed-of-light delay |
| τ_iono | Ionospheric group delay (frequency-dependent) |
| τ_mode | Extra path from N ionospheric hops |

**Mode Identification:**

| Mode | Typical Delay | Uncertainty |
|------|---------------|-------------|
| 1-hop E | 3.82 ms | ±0.20 ms |
| 1-hop F2 | 4.26 ms | ±0.17 ms |
| 2-hop F2 | 5.51 ms | ±0.33 ms |
| 3-hop F2 | ~7.0 ms | ±0.50 ms |

---

## WWV/WWVH Discrimination

### The Challenge

On 4 shared frequencies (2.5, 5, 10, 15 MHz), WWV and WWVH transmit simultaneously. Their signals mix in the ionosphere, arriving at different times and strengths. Separating these signals is essential for accurate timing.

### Discrimination Methods

#### Method 1: BCD Correlation (PRIMARY)

Cross-correlate 100 Hz BCD time code to find two peaks representing the two stations.

**Outputs:**

- WWV/WWVH amplitudes from dual-peak detection
- Differential delay (ms) - propagation path difference
- Geographic peak assignment using receiver location

#### Method 2: Timing Tones (1000/1200 Hz)

Power ratio of WWV's 1000 Hz vs WWVH's 1200 Hz marker tones.

#### Method 3: 440/500/600 Hz Ground Truth

Detect station-identifying tones from the broadcast schedule:

- Minute 1: WWVH broadcasts 440 Hz
- Minute 2: WWV broadcasts 440 Hz
- 500/600 Hz exclusive minutes provide 100% certain identification

#### Method 4: Test Signal Detection

Detect WWV/WWVH test signals at minutes :08 and :44.

#### Method 5: Weighted Voting

Combine all methods with minute-specific weighting for final determination.

---

## Directory Structure

See `DIRECTORY_STRUCTURE.md` for complete specification.

**Key Principles:**

- ✅ Use `TimeStdPaths` API for all path operations
- ✅ Consistent naming: `{CHANNEL}_{METHOD}_YYYYMMDD.csv`
- ✅ Mode-aware (test vs production)

**Summary:**

```
{data_root}/
├── raw_buffer/{CHANNEL}/{YYYYMMDD}/   # Phase 1: Binary IQ + JSON
│   ├── {minute}.bin[.zst|.lz4]
│   └── {minute}.json
├── phase2/{CHANNEL}/                   # Phase 2: Analytics outputs
│   ├── clock_offset/                   # D_clock time series
│   ├── discrimination/                 # Station identification
│   ├── tone_detections/                # 1000/1200 Hz results
│   ├── bcd_correlation/                # BCD analysis
│   ├── carrier_analysis/               # Amplitude/phase/Doppler
│   └── status/                         # Service state
└── state/                              # Global state files
```

---

## Service Management

### Systemd Services

| Service | Purpose |
|---------|---------|
| `timestd-core-recorder.service` | Phase 1: RTP → raw_buffer |
| `timestd-analytics.service` | Phase 2: Timing analysis (all channels) |
| `timestd-fusion.service` | Phase 3: Multi-broadcast fusion & Chrony feed |
| `timestd-web-ui.service` | Web monitoring UI |

### Start Order

1. **Core Recorder** - Archive data immediately
2. **Analytics Service** - Process archives with polling
3. **Web UI** - Dashboard access (optional)

All services are independent. Analytics processes backlog if started late.

---

## Performance & Reliability

### Disk Usage

**Per Channel (24 hours):**

- raw_buffer (20 kHz): ~2-3 GB/day (with compression)
- Phase 2 CSVs: ~5 MB/day (all outputs combined)

**Total (9 channels):** ~20-30 GB/day

### Reliability Design

**Phase 1 (Core Recorder):**

- ✅ Minimal dependencies
- ✅ Conservative error handling
- ✅ Systemd restart on failure

**Phase 2 (Analytics):**

- ✅ Aggressive retry logic
- ✅ Processes backlog on restart
- ✅ Can reprocess historical data
- ✅ Independent per channel

---

## Failure Recovery

### Phase 1 Crash

**Impact:** Missing minutes in raw_buffer.

**Recovery:**

1. Systemd restarts service automatically
2. Gap minutes lost (can't recreate RTP stream)
3. Analytics continues with available data

### Phase 2 Crash

**Impact:** Backlog of unprocessed files.

**Recovery:**

1. Systemd restarts service
2. Service detects backlog automatically
3. Processes all unprocessed files
4. Catches up to real-time

---

## Related Documentation

- **`CONTEXT.md`** - Project context and quick reference
- **`CANONICAL_CONTRACTS.md`** - Overview of project standards
- **`DIRECTORY_STRUCTURE.md`** - Complete path specifications
- **`TECHNICAL_REFERENCE.md`** - API and algorithm details

---

## Design Principles Summary

1. **Separation of Concerns:** Phase 1 stable, Phase 2 evolving
2. **RTP Primary:** Wall clock derived, never stretched
3. **Binary Archives:** Enable reprocessability, no dependencies
4. **Independent Methods:** Robust discrimination via weighted voting
5. **Scientific Integrity:** Complete data capture, clear provenance
6. **Reliability:** Independent services, automatic recovery

---

**For detailed implementation, see:**

- Path management: `src/hf_timestd/paths.py`
- Discrimination: `src/hf_timestd/core/wwvh_discrimination.py`
- Tone detection: `src/hf_timestd/core/tone_detector.py`
- Analytics service: `src/hf_timestd/core/phase2_analytics_service.py`
- Temporal engine: `src/hf_timestd/core/phase2_temporal_engine.py`
