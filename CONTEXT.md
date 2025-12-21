# HF Time Standard - Unexposed Ionospheric & Propagation Measurements

**Purpose:** Context for exploring scientifically valuable measurements that are calculated but not yet exposed in the web UI.

**Last Updated:** 2025-12-21

---

## Overview

The Phase 2 analytics pipeline computes numerous ionospheric and propagation parameters that have scientific value for studying HF propagation, ionospheric dynamics, and space weather effects. Many of these are written to CSV files but not visualized or correlated in the web UI.

---

## 1. Measured/Calculated Parameters NOT Exposed in Web UI

### 1.1 Doppler Measurements (`ChannelCharacterization`)
**Location:** `src/hf_timestd/core/phase2_temporal_engine.py` lines 291-299

| Parameter | Type | Scientific Value |
|-----------|------|------------------|
| `doppler_carrier_hz` | float | Bulk ionospheric motion, TID detection |
| `doppler_wwv_hz` | float | Per-station Doppler (path-specific) |
| `doppler_wwvh_hz` | float | Differential Doppler reveals path geometry |
| `doppler_wwv_std_hz` | float | Channel stability, scintillation indicator |
| `doppler_wwvh_std_hz` | float | Multipath/fading severity |
| `max_coherent_window_sec` | float | Coherence time for integration |
| `phase_variance_rad` | float | Phase stability metric |

**CSV:** `doppler_analysis.csv` (written but not displayed)

### 1.2 Multipath & Channel Quality
**Location:** `src/hf_timestd/core/phase2_temporal_engine.py` lines 301-304

| Parameter | Type | Scientific Value |
|-----------|------|------------------|
| `delay_spread_ms` | float | Multipath severity, mode mixing |
| `coherence_time_sec` | float | Channel stability window |
| `spreading_factor` | float | L = τ_D × f_D (Doppler-delay product) |

### 1.3 Test Signal Analysis (Minutes 8 & 44)
**Location:** `src/hf_timestd/core/phase2_temporal_engine.py` lines 325-330

| Parameter | Type | Scientific Value |
|-----------|------|------------------|
| `test_signal_fss_db` | float | Frequency Selectivity Score - D-layer absorption indicator |
| `test_signal_delay_spread_ms` | float | High-precision multipath from chirp |
| `test_signal_toa_offset_ms` | float | Sub-ms timing from wideband signal |
| `test_signal_coherence_time_sec` | float | Channel stability during test |

**CSV:** `test_signal_analysis.csv`

### 1.4 TEC Estimation (Multi-Frequency)
**Location:** `src/hf_timestd/core/tec_estimator.py`

| Parameter | Type | Scientific Value |
|-----------|------|------------------|
| `tec_tecu` | float | Total Electron Content (TECU) |
| `t_vacuum_error_ms` | float | Ionosphere-corrected timing |
| `group_delay_ms[freq]` | dict | Per-frequency ionospheric delay |
| `residuals_ms` | float | Fit quality / anomaly detection |

**Physics:** τ(f) = 40.3 × TEC / f² — enables ionospheric correction

### 1.5 Propagation Mode Analysis
**Location:** `src/hf_timestd/core/propagation_mode_solver.py`, `physics_propagation.py`

| Parameter | Type | Scientific Value |
|-----------|------|------------------|
| `propagation_mode` | str | 1F, 2F, 1E, GW identification |
| `n_hops` | int | Number of ionospheric reflections |
| `layer_height_km` | float | Effective reflection height |
| `elevation_angle_deg` | float | Launch/arrival angle |
| `residual_ms` | float | **THE SCIENCE PRODUCT** - observed minus predicted |
| `mode_candidates` | list | All viable modes with probabilities |

**Key Insight:** The residual (observed - physics_predicted) reveals ionospheric weather vs climatology.

### 1.6 Ionospheric Model State
**Location:** `src/hf_timestd/core/ionospheric_model.py`

| Parameter | Type | Scientific Value |
|-----------|------|------------------|
| `hmF2_km` | float | F2 layer peak height (IRI-2020 or measured) |
| `foF2_mhz` | float | F2 critical frequency |
| `hmE_km` | float | E layer height |
| `model_tier` | enum | IRI-2020, Parametric, or Static |
| `calibration_offset_km` | float | Learned correction to climatology |

### 1.7 CHU FSK Decoded Data
**Location:** `src/hf_timestd/core/phase2_temporal_engine.py` lines 332-339

| Parameter | Type | Scientific Value |
|-----------|------|------------------|
| `chu_fsk_dut1_seconds` | float | UT1-UTC correction (Earth rotation) |
| `chu_fsk_tai_utc` | int | TAI-UTC leap seconds |
| `chu_fsk_timing_offset_ms` | float | Independent timing verification |
| `chu_fsk_decode_confidence` | float | Signal quality metric |

### 1.8 Advanced Signal Analysis
**Location:** `src/hf_timestd/core/advanced_signal_analysis.py`

| Parameter | Type | Scientific Value |
|-----------|------|------------------|
| `sub_sample_offset` | float | Phase-derived sub-sample timing (~10x resolution) |
| `doppler_hz` (phase slope) | float | Instantaneous Doppler from phase |
| `multipath.is_multipath` | bool | Multipath detection flag |
| `multipath.quality_metric` | float | Measurement reliability |
| `differential_delay_ms` | float | WWV-WWVH cross-correlation |
| `coherence` | float | Both-station presence indicator |

---

## 2. Data Files Written But Not Visualized

| File | Location | Contents |
|------|----------|----------|
| `doppler_analysis.csv` | `phase2/{channel}/` | Per-minute Doppler metrics |
| `test_signal_analysis.csv` | `phase2/{channel}/` | Minutes 8/44 chirp analysis |
| `clock_offset/*.csv` | `phase2/{channel}/` | Full timing solution with all metrics |
| `discrimination/*.csv` | `phase2/{channel}/` | Station ID voting details |

---

## 3. Scientific Correlations to Explore

### 3.1 Doppler vs Timing Residual
- Correlation between `doppler_std_hz` and `uncertainty_ms`
- High Doppler variance -> unreliable timing

### 3.2 TEC vs Frequency-Dependent Delay
- Plot ToA vs 1/f^2 across 2.5, 5, 10, 15, 20, 25 MHz
- Slope reveals TEC, intercept reveals vacuum delay

### 3.3 Delay Spread vs Propagation Mode
- `delay_spread_ms` should correlate with mode mixing
- Single-mode paths have narrow spread

### 3.4 FSS (D-Layer) vs Time of Day
- Test signal FSS reveals D-layer absorption
- Should show strong diurnal pattern

### 3.5 hmF2 Residual vs Geomagnetic Activity
- Compare `calibration_offset_km` to Kp index
- Storm-time ionospheric perturbations

### 3.6 Multi-Station Differential Delay
- WWV-WWVH differential reveals path geometry differences
- BPM adds third independent path (trans-Pacific)

---

## 4. Key Source Files

| File | Purpose |
|------|---------|
| `phase2_temporal_engine.py` | Central orchestrator, defines all result dataclasses |
| `tec_estimator.py` | Multi-frequency TEC calculation |
| `ionospheric_model.py` | IRI-2020 integration, layer heights |
| `propagation_mode_solver.py` | Mode identification, delay calculation |
| `physics_propagation.py` | Physics-based delay with residual output |
| `advanced_signal_analysis.py` | Phase analysis, multipath detection |
| `phase2_analytics_service.py` | CSV writers, data persistence |

---

## 5. Web UI Current State

**Exposed:** D_clock, SNR, station detection, basic timing

**NOT Exposed:** Doppler, TEC, delay spread, FSS, propagation mode details, residuals

**API Gaps:** `monitoring-server-v3.js` parses `delay_spread_ms`, `doppler_std_hz`, `fss_db` from CSVs but leaves them empty in responses (lines 438-440).

---

## 6. Suggested Next Steps

1. **Add Doppler panel** to timing dashboard showing per-station Doppler and stability
2. **Add TEC display** when multi-frequency data available for same station
3. **Add propagation mode visualization** ("Mode Ridge" showing candidate modes)
4. **Add residual time series** - the primary scientific output
5. **Correlate with external data** - Kp index, solar flux, GOES X-ray
6. **Export science-grade CSV** with all parameters for offline analysis
