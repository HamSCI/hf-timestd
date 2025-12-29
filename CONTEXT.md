# HF-TimeStd Development Context

## Current Session Summary (2025-12-29)

### Major Accomplishment: Enhanced Timing Performance Metrics 📊

We have implemented comprehensive timing performance enhancements to provide accurate, transparent presentation of `hf-timestd` capabilities for the "Time Nut" community.

#### 1. Enhanced Uncertainty Budget (Root Sum of Squares)

**Implementation**: `src/hf_timestd/core/multi_broadcast_fusion.py`

Three-component uncertainty model:

- **Statistical Uncertainty**: Measurement scatter (weighted std deviation)
- **Systematic Uncertainty**: Calibration convergence error (decreases over time)
- **Propagation Uncertainty**: Mode-dependent ionospheric variability (GW: 0.1ms, 1F: 0.5ms, 2F: 2.0ms, TEC-solved: 0.2ms)

Combined via RSS: `σ_total = sqrt(σ_stat² + σ_sys² + σ_prop²)`

#### 2. Real-Time Performance Metrics

**Implementation**: `web-ui/monitoring_server.py` API endpoint

Calculated from last hour of fusion data:

- **RMS Accuracy**: `sqrt(mean(d_clock²))` vs UTC(NIST)
- **Peak-to-Peak**: Excursion range
- **Mean Offset**: Average clock offset
- **Standard Deviation**: Short-term stability

#### 3. Live Allan Deviation Tracking

**Implementation**: `AllanDeviationTracker` class in fusion service

- Overlapping ADEV calculation with 24h rolling window (86400 samples)
- Standard tau values: 10s, 100s, 1000s, 10000s
- Real-time fractional frequency stability monitoring
- Typical performance: σ_y(τ=1000s) ≈ 10⁻⁶ to 10⁻⁷

#### 4. Web UI Metrology Dashboard

**Implementation**: `web-ui/metrology.html`

Enhanced display sections:

- Uncertainty budget breakdown with clear component labels
- Real-time performance metrics (last hour)
- Allan deviation with scientific notation formatting
- Static metrology plots (ADEV, residuals, heatmap, VTEC correlation)

### Data Pipeline Status

- **L0 (Raw)**: Digital RF HDF5 ✅
- **L1A (Observables)**: Channel observables HDF5 ✅
- **L1B (Timecode)**: BCD timecode HDF5 ✅
- **L2 (Timing)**: Timing measurements HDF5 ✅
- **L3 (Fusion)**: **CSV only** ⚠️ (migration target for next session)
- **L3 (Ionosphere)**: GNSS VTEC HDF5 ✅

## Next Session Objective: L3 Fusion HDF5 Migration

### Background

The fusion service currently writes L3 results (fused D_clock, uncertainty budget, ADEV) to CSV (`fused_d_clock.csv`). This is the last remaining CSV-only data product in the pipeline.

### Goals for Next Session

1. **Create L3 Fusion HDF5 Schema** (`schemas/l3_fusion_results_v1.json`)
   - Include all FusedResult fields (d_clock, uncertainty components, ADEV, quality metrics)
   - Metrological provenance metadata
   - Quality flags and consistency indicators

2. **Implement HDF5 Writer** in `MultiBroadcastFusion`
   - Parallel CSV+HDF5 writes (CSV as fallback)
   - Schema validation
   - SWMR mode for live reading

3. **Update API Endpoint** (`monitoring_server.py`)
   - Read from HDF5 with CSV fallback
   - Maintain backward compatibility

4. **Benefits of Migration**:
   - Consistent data format across all pipeline levels
   - Better metadata and provenance tracking
   - Efficient time-series queries
   - SWMR for concurrent read/write

### Key Implementation Notes

**Current CSV Structure**:

```
timestamp, d_clock_fused_ms, uncertainty_ms, n_broadcasts, n_stations,
statistical_uncertainty_ms, systematic_uncertainty_ms, propagation_uncertainty_ms,
quality_grade, wwv_mean_ms, chu_mean_ms, ...
```

**Fusion Service Location**: `src/hf_timestd/core/multi_broadcast_fusion.py`

- `_init_fusion_csv()`: Initialize output file
- `_write_fused_result()`: Write fusion results
- `FusedResult` dataclass: Complete result structure

**API Endpoint**: `web-ui/monitoring_server.py`

- `/api/v2/system/health-summary`: Reads fusion CSV for metrics
- ADEV calculation: Currently done in API from CSV data

---

**Last Updated**: 2025-12-29  
**Next Session Focus**: L3 Fusion HDF5 Migration
