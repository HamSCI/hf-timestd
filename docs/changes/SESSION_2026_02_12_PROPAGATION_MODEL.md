# Session 2026-02-12: Improved Propagation Delay Modeling

## Summary

Replaced the static vacuum speed-of-light propagation model with a real-time
ionospheric data-driven model. The new system provides frequency-dependent
group delay predictions, multi-hop arrival support, and adaptive uncertainty
windows.

## Problem

The previous propagation delay model had several limitations:

1. **Static vacuum model**: `_predict_geometric_delay()` in `metrology_engine.py`
   used `light_time_ms * 1.15` — a fixed 15% overhead regardless of frequency,
   time of day, or ionospheric conditions.

2. **No frequency dependence**: Ionospheric group delay scales as 1/f², so
   5 MHz signals experience 4× more delay than 10 MHz. The old model treated
   all frequencies identically.

3. **Single-mode only**: Only predicted one arrival per (station, frequency).
   Multi-hop paths (2F, 3F) were not modeled, causing valid detections on
   long paths to be rejected.

4. **Fixed uncertainty**: ±50ms bootstrap window was too tight for multi-hop
   paths and too loose for single-hop with a good ionospheric model.

## Solution

### New Files

- **`src/hf_timestd/core/iono_data_service.py`** — Background service that
  fetches and caches real-time ionospheric data:
  - WAM-IPE 2D products (TEC, NmF2, HmF2) from NOAA's public S3 bucket
    (`s3://noaa-nws-wam-ipe-pds/`) and NOMADS
  - GIRO ionosonde measurements for real-time hmF2/foF2 corrections
  - Climatological fallback with diurnal/seasonal/latitudinal variation
  - Chapman layer electron density profile construction
  - Thread-safe singleton with background update thread

- **`src/hf_timestd/core/propagation_model.py`** — Physics-based HF group
  delay prediction engine:
  - Numerical integration of group delay through Ne(h) profile
  - TEC-based group delay fallback (40.3 × sTEC / (c × f²))
  - Multi-mode evaluation (1F, 2F, 3F, 1E) with MUF/geometry checks
  - Adaptive uncertainty estimation based on data source quality
  - Differential delay computation for TEC estimation
  - Self-consistency check (multi-frequency differential delay vs model TEC)

- **`tests/test_propagation_model.py`** — 23 tests covering:
  - Distance computation, single/multi-hop predictions
  - Frequency dependence (1/f² scaling)
  - Diurnal variation
  - Mode feasibility checks
  - TEC group delay formula verification
  - Numerical integration vs analytical TEC formula
  - ArrivalPatternMatrix backward compatibility
  - Self-consistency checks

### Modified Files

- **`src/hf_timestd/core/arrival_pattern_matrix.py`**:
  - `ExpectedArrival` dataclass: added `propagation_mode`, `geometric_delay_ms`,
    `iono_delay_ms`, `elevation_angle_deg`, `data_source`, `model_confidence`
  - `ArrivalMatrix` dataclass: added `multi_mode_arrivals` dict keyed by
    `(station, freq, mode)`, plus `get_mode_arrival()` and
    `get_all_mode_arrivals()` methods
  - `ArrivalPatternMatrix.__init__()`: initializes `HFPropagationModel`
  - `compute_matrix()`: delegates to `_compute_matrix_with_prop_model()` when
    available, falls back to `_compute_matrix_legacy()`
  - New `_compute_matrix_with_prop_model()`: evaluates all propagation modes,
    populates both primary and multi-mode arrival dicts
  - New `_add_arrival_to_matrix()`: shared helper with adaptive uncertainty
  - New `_compute_single_legacy()` and `_compute_matrix_legacy()`: extracted
    legacy path for clean fallback
  - `log_matrix_summary()`: shows mode labels, iono delay, data source

- **`src/hf_timestd/core/metrology_engine.py`**:
  - `_predict_geometric_delay()`: added `HFPropagationModel` as tier-2 fallback
    between ArrivalPatternMatrix and the simple vacuum calculation

- **`pyproject.toml`**: added `[project.optional-dependencies] iono` group
  with `netCDF4>=1.6.0` and `boto3>=1.28.0`

## Architecture

```
MetrologyEngine._predict_geometric_delay()
    ├── ArrivalPatternMatrix.get_expected_arrivals()
    │       └── HFPropagationModel.predict()
    │               ├── IonoDataService.get_iono_params()
    │               │       ├── WAM-IPE grid (primary)
    │               │       ├── GIRO corrections (supplementary)
    │               │       └── Climatological fallback
    │               ├── _evaluate_mode() × [1F, 2F, 3F, 1E]
    │               │       ├── Geometric feasibility (max hop distance)
    │               │       ├── MUF check (freq vs foF2/sec(i))
    │               │       ├── Spherical Earth path length
    │               │       └── Ionospheric group delay
    │               │               ├── Ne(h) numerical integration
    │               │               └── TEC-based fallback
    │               └── _estimate_uncertainty()
    ├── HFPropagationModel.predict() (direct, if matrix unavailable)
    └── Vacuum × 1.15 (last resort)
```

## Adaptive Uncertainty

The uncertainty window now adapts based on:

1. **Data source quality**: WAM-IPE+GIRO → ±1.5ms 3σ, IRI → ±4.5ms,
   parametric → ±9ms, no model → ±15ms
2. **Observed variance**: tracked per (station, freq) via exponential smoothing
3. **Model confidence**: blends model uncertainty with tracked variance
4. **The tighter of model and tracked** is used, floored at ±5ms (3σ)

## Backward Compatibility

- `ArrivalMatrix.arrivals` dict still keyed by `(station, freq)` — all existing
  callers work unchanged
- `get_arrival()`, `get_station_arrivals()`, `get_frequency_arrivals()` unchanged
- `validate_detection()` unchanged
- Legacy computation path preserved when `HFPropagationModel` is unavailable

## Test Results

23 new tests, all passing. 76 existing tests pass with no regressions.
3 pre-existing failures unrelated to this change.

## Next Steps

- Install `netCDF4` and `boto3` on production for WAM-IPE ingestion
- Start `IonoDataService` in the metrology service lifecycle
- Validate multi-hop predictions against observed CHU 7.85 MHz arrivals
- Expose propagation model diagnostics via web-api
- Compare model TEC with GNSS VTEC measurements
