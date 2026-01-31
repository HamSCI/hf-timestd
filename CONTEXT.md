# Project Context: HF Time Standard (hf-timestd)

## 🚀 Next Session: Timing-Aware Architecture Refactoring

**Objective:** Refactor the architecture to explicitly account for timing accuracy levels. The system should gracefully support different timing sources with automatic fallback, enabling scientific measurements appropriate to the available precision.

### Core Principle: GPSDO as Foundation

All scenarios assume a **GPSDO-disciplined RX888 ADC**. The 27 MHz clock from the Leo Bodnar (or similar) GPSDO provides:
- Sub-ppb frequency stability
- GPS-traceable sample clock
- Stable phase relationship for coherent processing

The GPSDO ensures RTP timestamps have a *stable* relationship to UTC. What varies is how *accurately* we know that relationship.

---

## 📊 Timing Accuracy Hierarchy

The system should support multiple timing sources with different accuracy levels. Each level unlocks additional ionospheric phenomena that "come into view" as timing improves.

### Timing Sources (Best to Worst)

| Level | Source | Typical Accuracy | RTP-to-UTC Mapping |
|-------|--------|------------------|-------------------|
| **L5** | GPS+PPS on radiod machine | ±100 ns | Direct PPS edge timestamps |
| **L4** | GPS+PPS on LAN | ±1 μs | PPS via NTP/PTP, network jitter |
| **L3** | GPS-sync (NMEA only) | ±10 ms | GPS time-of-day, no PPS |
| **L2** | NTP-sync (stratum 1-2) | ±1-10 ms | Network time, variable latency |
| **L1** | HF bootstrap only | ±5-50 ms | BCD/FSK decoded time, ionospheric delay |

### Ionospheric Phenomena by Timing Accuracy

| Phenomenon | Required Accuracy | Timing Level | Description |
|------------|-------------------|--------------|-------------|
| **Minute identification** | ±500 ms | L1+ | Which UTC minute are we in? |
| **Propagation mode ID** | ±50 ms | L1+ | 1F vs 2F vs 3F hop discrimination |
| **TEC estimation** | ±10 ms | L2+ | Multi-frequency differential delay |
| **TID detection** | ±5 ms | L2+ | Traveling Ionospheric Disturbances |
| **Scintillation (S4)** | ±1 ms | L3+ | Amplitude fading statistics |
| **Group delay variation** | ±100 μs | L4+ | Fine ionospheric structure |
| **Phase scintillation (σ_φ)** | ±10 μs | L4+ | Phase coherence measurements |
| **Absolute ToF** | ±1 μs | L5 | True time-of-flight measurement |
| **Multipath resolution** | ±100 ns | L5 | Separate closely-spaced arrivals |

### Architecture Implications

The system should:

1. **Detect available timing sources** at startup and runtime
2. **Select the best available source** with automatic fallback
3. **Advertise current timing level** to all components
4. **Enable/disable measurements** based on achievable accuracy
5. **Log timing source transitions** for data quality assessment

### Fallback Chain

```
L5 (PPS local) → L4 (PPS LAN) → L3 (GPS) → L2 (NTP) → L1 (HF bootstrap)
```

If a higher-accuracy source fails, the system should:
- Fall back to the next available source
- Continue measurements appropriate to the new accuracy level
- Flag data with the timing source used
- Alert operators to degraded timing

### Current Implementation Gap

The current system assumes L1/L2 timing (HF bootstrap + NTP). To support L4/L5:

1. **PPS integration**: Read `/dev/pps0` for edge timestamps
2. **Chrony PPS refclock**: Configure chrony to use PPS as primary
3. **RTP timestamp calibration**: Map RTP to PPS edges during lock
4. **Timing level API**: Expose current timing accuracy to services

### Leo Bodnar GPS Setup (Target Configuration)

- **27 MHz → RX888 ADC**: GPSDO-locked sample clock
- **PPS → /dev/pps0**: Via FTDI TTL-to-USB adapter
- **NMEA → USB**: Time-of-day for chrony

This configuration enables L5 timing (±100 ns) when PPS is available, with automatic fallback to L2 (NTP) if PPS fails.

---

## 🔧 Current Status: v5.3.11

**Version**: v5.3.11 - 2026-01-30
**Core Philosophy**: **"Steel Ruler" Metrology**. The system treats the local GPSDO as a fixed standard (zero process noise) to measure ionospheric variance.

### Recent Changes (v5.3.11)

1. **Chrony Feed Gating Diagnostics**: Added logging to show why chrony writes are blocked
2. **ArrivalPatternMatrix**: Physics-based arrival predictions with dynamic window narrowing
3. **Precision Calculation Fix**: Correct max/min clamping for chrony SHM precision

### Previous: Two-Tier Bootstrap (v5.3.10)

1. **Two-Tier Bootstrap**: Implemented ionospheric averaging before refined lock
   - Tier 1 (Provisional): Quick lock in 2-3 min for minute alignment
   - Tier 2 (Refined): Stable lock after 10 min with median offset, std < 15ms
2. **LockTier Enum**: Added `LockTier.NONE`, `PROVISIONAL`, `REFINED` states
3. **Offset Measurement Tracking**: Collect measurements during provisional phase

---

## ✅ Two-Tier Bootstrap Implementation Task (COMPLETE)

**Status**: Implemented in v5.3.10 - See `docs/changes/SESSION_2026_01_27_TWO_TIER_BOOTSTRAP.md`

### Problem Statement

The current bootstrap system locks too quickly, capturing ionospheric variability as systematic offset error. The ionosphere introduces path delay variations at multiple timescales:

| Timescale | Phenomenon | Typical Variation |
|-----------|------------|-------------------|
| **Seconds** | Scintillation, multipath | ±5-20 ms |
| **Minutes** | Traveling Ionospheric Disturbances (TIDs) | ±10-30 ms |
| **Hours** | Diurnal TEC variation | ±50-100 ms equivalent |

To achieve a stable RTP-to-UTC offset, we need to average over the TID timescale (~10-15 minutes). Locking in 2-3 minutes captures ionospheric variability as systematic offset error.

### Two-Tier Bootstrap Design

| Tier | Name | Purpose | Timing | Criteria |
|------|------|---------|--------|----------|
| **Tier 1** | Provisional Lock | Establish minute boundaries for archiving | 2-3 minutes | 2+ stations, 2+ frequencies, consistent clusters |
| **Tier 2** | Refined Lock | Stable RTP-to-UTC offset after ionospheric averaging | 10-15 minutes | 50+ measurements, offset std < 15ms, median-based |

### Current Implementation State

The `BootstrapConfig` dataclass in `bootstrap_service.py` already has the two-tier parameters defined:

```python
# Tier 1: Provisional lock criteria (quick, for minute alignment)
min_stations_for_provisional: int = 2
min_frequencies_for_provisional: int = 2
min_minutes_for_provisional: int = 2

# Tier 2: Refined lock criteria (stable, after ionospheric averaging)
refined_lock_duration_sec: float = 600.0  # 10 minutes for TID averaging
min_measurements_for_refined: int = 50
max_offset_std_for_refined_ms: float = 15.0

# Callbacks
on_provisional_lock: Optional[Callable[[float], None]] = None
on_full_lock: Optional[Callable[[float, float], None]] = None
```

### Implementation Tasks

1. **Track offset measurements during provisional lock**
   - After provisional lock, continue collecting tone detections
   - Store offset measurements with timestamps in a rolling window
   - Calculate running median and standard deviation

2. **Implement refined lock transition logic**
   - After `refined_lock_duration_sec` (10 min), check if criteria are met:
     - At least `min_measurements_for_refined` (50) measurements
     - Offset standard deviation < `max_offset_std_for_refined_ms` (15ms)
   - If met, transition to `LOCKED` phase with refined offset (median)
   - If not met, continue collecting until criteria are satisfied

3. **Update offset calculation**
   - Provisional lock: Use first valid cluster offset (current behavior)
   - Refined lock: Use median of all measurements during provisional phase

4. **Expose lock tier in status**
   - Add `lock_tier` field to bootstrap status (0=none, 1=provisional, 2=refined)
   - Log tier transitions with offset statistics

### Key Files

| File | Purpose |
|------|---------|
| `src/hf_timestd/core/bootstrap_service.py` | Bootstrap coordination, phase management, config |
| `src/hf_timestd/core/timing_bootstrap.py` | State machine, candidate clustering, offset calculation |
| `src/hf_timestd/core/bootstrap_rolling_buffer.py` | Circular buffer for IQ samples |
| `src/hf_timestd/core/tone_detector.py` | FFT-based tone detection |

### Testing Strategy

1. **Unit tests**: Add tests for refined lock transition logic
2. **Integration test**: Verify offset improves after 10 minutes
3. **Monitoring**: Log offset statistics during provisional phase to validate improvement

### Reference: Ionospheric Averaging Theory

The Allan deviation of ionospheric delay reaches a minimum at τ ≈ 10-20 minutes. This is the optimal averaging time to:
- Average out scintillation (seconds)
- Average out TIDs (minutes)
- Not be affected by diurnal trends (hours)

Using the median instead of mean provides robustness against outliers from multipath or interference.

---

## 📋 Propagation & Physics Page Review Task

### Page Overview

| Page | Purpose | Key Features |
|------|---------|--------------|
| **propagation.html** | Ionospheric conditions and propagation modes | MUF estimate, per-broadcast mode analysis, TEC by path, mode timeline |
| **physics.html** | Multi-path ionospheric analysis | 3 tabs: Paths (TEC/scintillation), Channels (test signals), Events (ionospheric events) |

### propagation.html Structure (664 lines)

| Section | What It Shows | API Endpoint |
|---------|---------------|--------------|
| **Current Conditions** | MUF estimate, active broadcasts, measurement count | `/api/propagation/conditions` |
| **Per-Broadcast Cards** | Station, frequency, dominant mode, SNR, mode distribution | `/api/propagation/conditions` |
| **Multi-Frequency Comparison** | Per-station table comparing modes across frequencies | `/api/propagation/conditions` |
| **Frequency-Mode Bar Chart** | Observations by frequency with dominant mode | `/api/propagation/conditions` |
| **TEC by Path** | Time series with error bars, quality summary, per-station details | `/api/propagation/tec` |
| **Mode Timeline** | Scatter plot of modes over time, colored by mode type | `/api/propagation/timeline` |

### physics.html Structure (761 lines)

| Tab | Section | What It Shows | API Endpoint |
|-----|---------|---------------|--------------|
| **Paths** | Hero: UTC Consistency | Physics model validity, stations used, residuals | `/api/physics/latest` |
| **Paths** | Path Cards | Per-station: measurements, frequencies, S4 index, scintillation severity | `/api/physics/scintillation/paths` |
| **Paths** | Measurement History | S4 scintillation index over time with threshold lines | `/api/physics/scintillation/history` |
| **Channels** | Channel Characterization | WWV/WWVH test signal analysis (minutes 8 & 44) | `/api/physics/test-signals/latest` |
| **Channels** | Test Signal Results | Multi-tone, chirp, burst analysis | `/api/physics/test-signals/latest` |
| **Channels** | Channel Quality History | Quality metrics over time | `/api/physics/test-signals/history` |
| **Events** | Ionospheric Events | Sporadic-E, TIDs, solar flares, day/night transitions | `/api/physics/events` |
| **Events** | Scintillation Monitor | S4 (amplitude) and σ_φ (phase) indices | `/api/physics/scintillation/paths` |

### Key API Endpoints

```bash
# Propagation endpoints
curl -s http://localhost:8000/api/propagation/conditions | jq
curl -s http://localhost:8000/api/propagation/timeline?start=-6h | jq
curl -s http://localhost:8000/api/propagation/tec?start=-7d | jq

# Physics endpoints
curl -s http://localhost:8000/api/physics/latest | jq
curl -s http://localhost:8000/api/physics/scintillation/paths | jq
curl -s http://localhost:8000/api/physics/scintillation/history?start=-6h | jq
curl -s http://localhost:8000/api/physics/test-signals/latest | jq
curl -s http://localhost:8000/api/physics/events | jq
```

### Key Files

| File | Purpose |
|------|---------|
| `web-api/static/propagation.html` | Propagation analysis UI (664 lines) |
| `web-api/static/physics.html` | Physics dashboard UI (761 lines) |
| `web-api/routers/propagation.py` | Propagation API endpoints |
| `web-api/routers/physics.py` | Physics API endpoints |
| `web-api/services/propagation_service.py` | Propagation data service |
| `web-api/services/physics_service.py` | Physics data service |
| `web-api/services/scintillation_service.py` | S4/σ_φ calculations |
| `web-api/services/test_signal_service.py` | WWV/WWVH test signal analysis |
| `web-api/services/event_service.py` | Ionospheric event detection |

### Ionospheric Physics Concepts

#### Propagation Modes
| Mode | Description | Typical Delay |
|------|-------------|---------------|
| **1E** | Single E-layer hop (~100 km) | 2-10 ms |
| **1F** | Single F-layer hop (~300 km) | 5-20 ms |
| **2F** | Double F-layer hop | 10-40 ms |
| **3F** | Triple F-layer hop | 15-60 ms |
| **GW** | Ground wave (< 200 km) | < 1 ms |

#### TEC (Total Electron Content)
- **Units**: TECU (10¹⁶ electrons/m²)
- **Calculation**: From multi-frequency differential delay (K × TEC / f²)
- **Typical values**: 5-50 TECU (day), 1-10 TECU (night)

#### Scintillation Indices
| Index | Meaning | Thresholds |
|-------|---------|------------|
| **S4** | Amplitude scintillation (normalized std dev) | < 0.2 weak, 0.2-0.4 moderate, > 0.4 strong |
| **σ_φ** | Phase scintillation (radians) | < 0.1 weak, 0.1-0.3 moderate, > 0.3 strong |

#### MUF (Maximum Usable Frequency)
- Highest frequency that will reflect from ionosphere
- Estimated from highest frequency with successful propagation
- Varies with solar activity, time of day, season

### Potential Review Areas

#### propagation.html
1. **MUF Estimate**: Is the calculation reasonable? What happens with insufficient data?
2. **Mode Distribution**: Are percentages calculated correctly?
3. **TEC Quality**: Are error bars and quality flags properly displayed?
4. **Multi-Frequency Comparison**: Does it correctly identify multi-hop vs single-hop?
5. **Time Range Buttons**: Do all ranges work correctly?

#### physics.html
1. **UTC Consistency**: What does "CONSISTENT" vs "CHECKING" mean?
2. **Path Cards**: Are measurements correctly attributed to stations?
3. **S4 Thresholds**: Are the horizontal threshold lines at correct values?
4. **Test Signal Analysis**: Is minutes 8/44 data being captured?
5. **Events Tab**: Is event detection working? (Currently shows placeholder)
6. **Scintillation History**: Is the data being plotted correctly?

### Testing Commands

```bash
# Verify pipeline is healthy
scripts/verify_pipeline.sh

# Check propagation service logs
sudo journalctl -u timestd-physics -n 50 --no-pager

# Test API endpoints directly
curl -s http://localhost:8000/api/propagation/conditions | python3 -m json.tool
curl -s http://localhost:8000/api/physics/scintillation/paths | python3 -m json.tool

# Check for JavaScript errors in browser console
# Open http://localhost:8000/static/propagation.html
# Open http://localhost:8000/static/physics.html
```

### Reference Documentation

- **`docs/PHYSICS.md`** — Ionospheric physics capabilities
- **`docs/METROLOGY.md`** — Metrological description
- **`TECHNICAL_REFERENCE.md`** — System architecture

---

## ⚠️ Active Issues / Watchlist

- **TEC Staleness at Night**: The `timestd-physics` service correctly reports stale TEC data during nighttime when only single frequencies are visible. This is a scientific limitation, not a software failure.
- **WWV 20/25 MHz Propagation**: These bands show STALE measurements during poor propagation conditions (nighttime/early morning). Expected behavior—signals resume when propagation improves.
- **CHU Channels Stale**: CHU frequencies (3.33, 7.85, 14.67 MHz) may show stale during poor propagation to Ottawa.

---

## ✅ Session Complete: Memory & Bootstrap Fixes (v5.3.9)

**Date**: 2026-01-27  
**Status**: **COMPLETE** - Memory leaks fixed, bootstrap buffer cleanup working

### Accomplishments

1. **Memory Leak Fixes**
   - Capped `TimingBootstrap.all_candidates` at 500 entries
   - Free bootstrap buffers on provisional lock (~250MB reclaimed)
   - Fixed `_update_phase_from_bootstrap()` to always check state

2. **Calibration Convergence Fixes**
   - Relaxed sanity check to 3× limit (240ms) during initial convergence
   - Relaxed discontinuity threshold to 100ms during convergence

3. **Timer & Monitoring Fixes**
   - Changed chrony monitor timer from `OnUnitActiveSec` to `OnCalendar`
   - Added SHM, calibration freshness, single-station mode checks

4. **ka9q-python 3.4.1**
   - Upgraded to fix RTP stream deduplication

### Files Modified

- `systemd/timestd-chrony-monitor.timer`
- `scripts/check-chrony-reach.sh`
- `src/hf_timestd/core/multi_broadcast_fusion.py`
- `src/hf_timestd/core/bootstrap_service.py`
- `src/hf_timestd/core/timing_bootstrap.py`
- `docs/changes/SESSION_2026_01_27_TSL_UNREACHABLE_DIAGNOSIS.md`

---

## ✅ Session Complete: Test Suite & Phase-Engine Prep (v5.3.8)

**Date**: 2026-01-22  
**Status**: **COMPLETE** - Test suite restored, phase-engine architecture ready

---

## 📚 Archive Structure

```
archive/
├── debug-tools/          # Debug scripts and tools
├── deprecated-tests/     # Tests for deprecated/refactored code
├── dev-history/          # Historical development documents
│   ├── 2026-01-fixes/    # Recent fix and session documents
│   └── analysis/         # Analysis and critique documents
└── planning/             # Planning and design documents
```
