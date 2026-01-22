# Project Context: HF Time Standard (hf-timestd)

## 🚀 Current Status: "Steel Ruler" Release (v5.3.8)

**Version**: v5.3.8 - 2026-01-22
**Core Philosophy**: **"Steel Ruler" Metrology**. The system treats the local GPSDO as a fixed standard (zero process noise) to measure ionospheric variance.

### 🌟 Recent Accomplishments (v5.3.8)

1. **Station-Centric Architecture**: Added `BroadcastRegistry` class for phase-engine integration (17 broadcasts, 9/17 channels).
2. **Test Suite Restoration**: Fixed 79 tests, added 27 new BroadcastRegistry tests, archived 3 deprecated tests.
3. **Verification Script Fixes**: Fixed chrony detection regex, removed obsolete checks.
4. **Config Schema**: Added `[ka9q].source` field for radiod/phase-engine mode selection.

### 🔴 Next Session: Propagation & Physics Page Review

**Objective:** Review `propagation.html` and `physics.html` for errors, omissions, or reasonable improvements.

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

## ✅ Session Complete: Test Suite & Phase-Engine Prep (v5.3.8)

**Date**: 2026-01-22  
**Status**: **COMPLETE** - Test suite restored, phase-engine architecture ready

### Accomplishments

1. **BroadcastRegistry** — Station-centric data model for phase-engine integration
   - 17 broadcasts, geometry computation, channel derivation
   - radiod mode: 9 channels, phase-engine mode: 17 channels
   - 27 comprehensive tests

2. **Test Suite Restoration**
   - Fixed schema tests (quality_flags, version checks)
   - Fixed HDF5 IO tests (hardcoded versions, missing fields)
   - Archived 3 deprecated tests (stale imports)
   - 79 tests now passing

3. **Verification Script Fixes**
   - Fixed chrony source detection (# prefix for refclocks, not ^)
   - Removed obsolete BCD/tone detection checks

4. **Config Schema**
   - Added `[ka9q].source` field for radiod/phase-engine mode

### Commits

```
28fb134 - Fix test suite and add BroadcastRegistry tests
1a3ba13 - Add source field to config schema
ec63631 - Fix chrony source detection in verify_pipeline.sh
f14583e - Remove obsolete BCD/tone detection checks
```

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
