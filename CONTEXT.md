# Project Context: HF Time Standard (hf-timestd)

## 🚀 Current Status: "Steel Ruler" Release (v5.3.7)

**Version**: v5.3.7 - 2026-01-18
**Core Philosophy**: **"Steel Ruler" Metrology**. The system treats the local GPSDO as a fixed standard (zero process noise) to measure ionospheric variance.

### 🌟 Recent Accomplishments (v5.3.7)

1. **Allan Deviation Fix**: Corrected ADEV calculation to use proper IEEE 1139-2008 second-difference formula for phase data. ADEV now correctly decreases with averaging time.
2. **Core Stability Module**: Created `src/hf_timestd/core/stability_analysis.py` with proper ADEV algorithms in the core library (not web-api).
3. **Pipeline Recovery**: Fixed recorder RTP timestamp stall and fusion logging issues.
4. **Web UI Enhancements**: Physics multi-view tabs, CHU FSK section in metrology, health page fixes.

### 🔴 Next Session: Metrology Page Enhancement

**Objective:** Enhance `metrology.html` to more explicitly display the data and calculations described in `docs/METROLOGY.md` that accomplish the stated metrological goal and provide the timing accuracy that enables ionospheric measurements.

---

## 📋 Metrology Page Enhancement Task

### The Metrological Goal (from METROLOGY.md)

The system achieves **±0.5 ms (1σ) accuracy to UTC(NIST)** through:

1. **Multi-broadcast fusion** of 9-17 independent measurements
2. **ISO GUM-compliant uncertainty budgets** with Type A (statistical) and Type B (systematic) components
3. **Inverse variance weighting** for statistically optimal combination
4. **Kalman filtering** with "Steel Ruler" parameters (zero process noise)
5. **Inter-station consistency validation** (cross-station agreement < 1 ms)

### Current metrology.html Structure

| Section | What It Shows | What's Missing |
|---------|---------------|----------------|
| **Hero Display** | D_clock, uncertainty, grade, broadcast count | No explanation of what D_clock means |
| **Station Contributions** | List of contributing stations | No per-station D_clock values or weights |
| **Expert Metrics** | ISO GUM budget, inter-station consistency | Good coverage, but could show the fusion equation |
| **CHU FSK** | DUT1, TAI-UTC, timing offset | Complete |
| **Fusion History** | D_clock time series with error bars | No propagation delay breakdown |
| **Allan Deviation** | ADEV plot with noise type | Complete |
| **Uncertainty Evolution** | Uncertainty time series | No breakdown by component |

### Key Concepts to Expose (from METROLOGY.md)

#### 1. The Transmission Time Equation
```
T_arrival = T_emission + τ_propagation + D_clock
```
Where:
- **T_arrival**: Measured tone arrival time (RTP timestamp from GPSDO-disciplined SDR)
- **T_emission**: Known transmission time (top of minute, UTC)
- **τ_propagation**: Ionospheric path delay (2-70 ms, station-dependent)
- **D_clock**: The unknown we solve for

#### 2. The Three-Layer Architecture
- **Layer 1 (Single Broadcast)**: Measures tick rate, but floating in time
- **Layer 2 (Multi-Frequency)**: Dispersion calculation → TEC → path delay correction
- **Layer 3 (Multi-Station)**: Geometry lock → integrity validation

#### 3. Inverse Variance Weighting
```
w_i = 1 / σ_i²
D_clock_fused = Σ(w_i × D_clock_i) / Σ(w_i)
```
Measurements with 0.5 ms uncertainty get 4x weight vs 1.0 ms uncertainty.

#### 4. ISO GUM Uncertainty Components
- **Type A (Statistical)**: Weighted standard error, reduces as √N
- **Type B (Systematic)**: Tone detection (±0.1 ms), propagation model (±1-2 ms), RTP jitter (±0.1 ms)
- **Combined**: u_combined = √(u_A² + u_B² + u_propagation²)

#### 5. Quality Grades
| Grade | Uncertainty | Criteria |
|-------|-------------|----------|
| **A** | ±0.5 ms | 30+ detections, 60 min span, calibrated, inter-station < 1 ms |
| **B** | ±1.0 ms | 10+ detections, provisional phase |
| **C** | ±2.0 ms | Bootstrap phase, limited validation |
| **D/F** | > 2.0 ms | Insufficient data or validation failures |

### Available API Data (from fusion_service.py)

The `/api/metrology/fusion/latest` endpoint returns:

```python
{
    'd_clock_ms': float,           # Fused clock offset
    'd_clock_raw_ms': float,       # Raw (pre-Kalman) offset
    'uncertainty_ms': float,       # Combined uncertainty
    'statistical_uncertainty_ms',  # Type A
    'systematic_uncertainty_ms',   # Type B  
    'propagation_uncertainty_ms',  # Propagation model
    'quality_grade': str,          # A/B/C/D/F
    'n_broadcasts': int,           # Number of measurements
    'n_stations': int,             # Number of stations
    'stations_used': list,         # Station names
    'inter_station_spread_ms',     # Cross-station consistency
    'consistency_flag': str,       # OK / CROSS_STATION_DISAGREE
    'kalman_state': str,           # LOCKED / CONVERGING
    'calibration_applied': bool,
    # Per-station data:
    'wwv_mean_ms', 'wwvh_mean_ms', 'chu_mean_ms', 'bpm_mean_ms',
    'wwv_count', 'wwvh_count', 'chu_count', 'bpm_count',
    'wwv_intra_std_ms', 'wwvh_intra_std_ms', 'chu_intra_std_ms',
    # Global solve:
    'global_solve_verified': bool,
    'global_solve_consistency_ms': float,
}
```

### Suggested Enhancements

#### 1. Add "How It Works" Explainer Section
A collapsible section explaining the transmission time equation and what D_clock represents. Target audience: time nuts who want to understand the methodology.

#### 2. Enhance Station Contributions Display
Show per-station:
- D_clock value (with calibration offset applied)
- Weight in fusion (1/σ²)
- Number of broadcasts
- Intra-station standard deviation

#### 3. Add Fusion Equation Visualization
Show the actual weighted mean calculation:
```
D_clock_fused = (w_WWV × D_WWV + w_WWVH × D_WWVH + w_CHU × D_CHU) / (w_WWV + w_WWVH + w_CHU)
```
With actual values filled in.

#### 4. Add Propagation Delay Breakdown
For each station, show:
- Geometric delay (great circle distance / c)
- Ionospheric delay (K × TEC / f²)
- Total propagation delay
- Propagation model source (IONEX / IRI / empirical)

#### 5. Add Quality Grade Explanation
Show what criteria were met/not met for the current grade.

#### 6. Add "Steel Ruler" Status Indicator
Show that the Kalman filter is in "Steel Ruler" mode:
- Drift clamped to 0.0 ms/min
- Process noise effectively zero
- Baseline is STABLE

### Key Files to Modify

| File | Purpose |
|------|---------|
| `web-api/static/metrology.html` | Main UI - add new sections, enhance existing |
| `web-api/services/fusion_service.py` | May need to expose additional fields |
| `web-api/routers/metrology.py` | May need new endpoints |
| `src/hf_timestd/core/multi_broadcast_fusion.py` | Source of fusion calculations |

### Data Not Currently Exposed (May Need New Endpoints)

1. **Per-broadcast raw D_clock values** (before fusion)
2. **Per-broadcast propagation delays** (τ_propagation breakdown)
3. **Calibration offsets per station** (from broadcast_calibration.json)
4. **Kalman filter state** (P matrix, innovation)
5. **Historical quality grade transitions**

### Reference Documentation

- **`docs/METROLOGY.md`** — Complete metrological description (662 lines)
- **`docs/PHYSICS.md`** — Ionospheric physics capabilities
- **`TECHNICAL_REFERENCE.md`** — System architecture

### Testing the Page

```bash
# Verify API is returning data
curl -s http://localhost:8000/api/metrology/fusion/latest | python3 -m json.tool

# Check fusion service logs
sudo journalctl -u timestd-fusion -n 20 --no-pager

# Verify pipeline health
scripts/verify_pipeline.sh
```

---

## ⚠️ Active Issues / Watchlist

- **TEC Staleness at Night**: The `timestd-physics` service correctly reports stale TEC data during nighttime when only single frequencies are visible. This is a scientific limitation, not a software failure.
- **WWV 20/25 MHz Propagation**: These bands show STALE measurements during poor propagation conditions (nighttime/early morning). Expected behavior—signals resume when propagation improves.

---

## ✅ Session Complete: Web UI & ADEV Fix (v5.3.7)

**Date**: 2026-01-18  
**Status**: **COMPLETE** - Allan deviation fixed, web UI enhanced, pipeline recovered

### Accomplishments

1. **Allan Deviation Fix** — `stability_analysis.py` (core) + `stability_service.py` (web-api)
   - Fixed ADEV calculation to use IEEE 1139-2008 second-difference formula
   - ADEV now correctly decreases with averaging time
   - Moved core algorithms to `src/hf_timestd/core/stability_analysis.py`
   - Web-api service is now a thin wrapper

2. **Pipeline Recovery**
   - Fixed recorder RTP timestamp stall (restarted to re-sync)
   - Fixed fusion log rotation issue (service restart)
   - Pipeline now at 32 PASS, 0 FAIL

3. **Web UI Enhancements**
   - Physics page: Multi-view tabs (Paths, Channels, Events)
   - Metrology page: CHU FSK decoded data section
   - Health page: Renamed Analytics→Metrology, stale channels show last known data

### Files Created

- `src/hf_timestd/core/stability_analysis.py` — Core ADEV algorithms
- `web-api/services/stability_core.py` — Web-api fallback
- `web-api/services/chu_fsk_service.py` — CHU FSK data
- `web-api/services/event_service.py` — Ionospheric events
- `web-api/services/scintillation_service.py` — S4/σ_φ data
- `web-api/services/test_signal_service.py` — Channel characterization

### Commit

```
d22dfb8 - Web UI enhancements and ADEV fix
```

---

## ✅ Session Complete: Physics Capabilities Implementation (v5.3.6)

**Date**: 2026-01-16  
**Status**: **PHYSICS COMPLETE** - Scintillation indices and Sporadic-E detection implemented

### Accomplishments

1. **Scintillation Indices (S4, σ_φ)** — `advanced_signal_analysis.py`
2. **Sporadic-E Detection** — `propagation_mode_solver.py`
3. **CHU FSK Integration Enhanced** — `metrology_engine.py`
4. **New Test Suites** — `test_scintillation_indices.py`, `test_sporadic_e_detection.py`

---

## 📚 Archive Structure

```
archive/
├── debug-tools/          # Debug scripts and tools
├── dev-history/          # Historical development documents
│   ├── 2026-01-fixes/    # Recent fix and session documents
│   └── analysis/         # Analysis and critique documents
└── planning/             # Planning and design documents
```
