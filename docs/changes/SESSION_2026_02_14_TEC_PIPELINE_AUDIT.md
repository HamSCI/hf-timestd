# TEC Pipeline Audit & Enhancement — 2026-02-14

## Audit Summary (17 Concerns)

### TECEstimator (tec_estimator.py) — Concerns 1-6

| # | Concern | Finding | Fix |
|---|---------|---------|-----|
| 1 | D_clock vs raw ToA input | `multi_broadcast_fusion.py` correctly prefers `raw_arrival_time_ms`; `ionospheric_reanalysis.py` uses D_clock (best approach). | Documented D_clock as preferred input in new docstring. |
| 2 | Negative slope handling | Returned `TECResult(tec_u=0.0, confidence=0.0)` — misleading. | **Fixed**: returns `None` (clean rejection). |
| 3 | Dead `high_precision_mode` | Stored but never used. | **Fixed**: removed parameter entirely. |
| 4 | R² misleading for N=2 | R²=1.0 always for 2 points (zero DOF). | **Fixed**: capped at `MAX_CONFIDENCE_N2 = 0.3`. |
| 5 | Missing `propagation_mode` field | Monkey-patched downstream. | **Fixed**: added to `TECResult` dataclass with default `'UNKNOWN'`. |
| 6 | No outlier rejection | Mode-mixed measurements corrupt the fit. | **Fixed**: iterative MAD-based 3σ outlier rejection. |

### PhysicsFusionService (physics_fusion_service.py) — Concerns 7-10

| # | Concern | Finding | Fix |
|---|---------|---------|-----|
| 7 | Mode grouping starves estimator | Groups by `(station, mode)` — can leave <2 freqs per group. | Documented; D_clock approach makes this less critical. |
| 8 | ToA source: `tof_kalman_ms` smooths dispersion | Kalman-smoothed ToA removes the 1/f² signal we need. | Documented; `raw_arrival_time_ms` preferred in fusion. |
| 9 | Inverted uncertainty weighting | Used `1/confidence` instead of actual timing uncertainty. | **Fixed**: uses `tof_uncertainty_ms` with fallback. |
| 10 | TEC validation window too narrow | 5-100 TECU rejects valid nighttime/solar-max values. | **Fixed**: widened to 1-200 TECU, confidence threshold 0.5. |

### IonosphericReanalysis (ionospheric_reanalysis.py) — Concerns 11-12

| # | Concern | Finding | Fix |
|---|---------|---------|-----|
| 11 | Best TEC but offline only | `_estimate_tec_cleaned()` uses D_clock + median filtering. | Approach adopted in new Bayesian estimator. |
| 12 | Crude foF2 estimation | Fixed Chapman cosine scaling. | Documented; IonoDataService provides better data when available. |

### HFPropagationModel (propagation_model.py) — Concerns 13-15

| # | Concern | Finding | Fix |
|---|---------|---------|-----|
| 13 | Double-counting iono delay | D_clock = observed - model. If model is perfect, no 1/f² residual. TEC estimator measures model error, not absolute TEC. | Documented; this is correct behavior. |
| 14 | 2× multiplier per hop | Correct: one hop = up + down through ionosphere. | Confirmed correct. |
| 15 | Same TEC for all hops | `_get_mode_iono_params` averages over reflection points (good). `IonosphericDelayCalculator` still uses `slant_tec * n_hops` (simplified). | Documented discrepancy. |

### Carrier Phase → TEC Pipeline — Concerns 16-17

| # | Concern | Finding | Fix |
|---|---------|---------|-----|
| 16 | Phase data not used for TEC | Doppler computed but never converted to dTEC. | **Fixed**: new `carrier_tec.py` module. |
| 17 | Phase continuity not validated in production | Fix committed 2026-02-12 but production validation pending. | Documented; requires service restart. |

---

## New Modules Implemented

### 1. Bayesian TEC Estimator (`tec_estimator.py` rewrite)
- Iterative WLS with MAD-based 3σ outlier rejection
- SNR and mode-confidence weighting
- N=2 confidence cap at 0.3
- Negative slope → clean rejection (returns None)
- `propagation_mode` field on `TECResult`
- Backward-compatible `estimate_tec()` API

### 2. Carrier-Phase Differential TEC (`carrier_tec.py`)
- Converts carrier phase rate → dTEC/dt via Doppler
- Integrates to relative TEC(t) with ~1s resolution
- Anchors to absolute TEC from group-delay estimates
- Differential dTEC between frequencies for consistency checks
- MAD-based noise floor estimation

### 3. Multi-Layer E/F Tomography (`iono_tomography.py`)
- Two-shell model (E=110km, F=300km) with thin-shell obliquity factors
- Constrained least squares with non-negativity bounds
- Solar-dependent priors: strong E-layer suppression at night
- Per-path residual diagnostics
- Condition number monitoring

### 4. VTEC Map Generator (`vtec_mapper.py`)
- Slant-to-vertical TEC conversion via thin-shell mapping
- Ionospheric pierce point computation (great-circle midpoint)
- 2D polynomial surface fit with adaptive degree
- IONEX-format output (compatible with GPS community tools)
- Regional grid evaluation

### 5. Integration (`physics_fusion_service.py`)
- All new modules wired into `process_minute()` pipeline
- Tomography runs after TEC estimation (≥2 estimates)
- VTEC map generation after tomography (≥3 IPPs)
- IONEX files written to `phase2/ionex/`

## Test Coverage

19 tests in `tests/core/test_tec_estimator_diagnostics.py`:
- 9 tests for TECEstimator (flat data, negative slope, N=2 cap, N≥3 fit, outlier rejection, propagation_mode, SNR weighting, insufficient freqs, no dead param)
- 2 tests for CarrierTEC (diurnal dTEC, anchoring)
- 3 tests for IonoTomography (nighttime E-layer, daytime E/F separation, insufficient paths)
- 5 tests for VTECMapper (zenith mapping, oblique mapping, map generation, IONEX output, insufficient IPPs)

## Files Modified
- `src/hf_timestd/core/tec_estimator.py` — rewritten
- `src/hf_timestd/core/physics_fusion_service.py` — wired new modules, fixed concerns 9-10
- `src/hf_timestd/core/ionospheric_reanalysis.py` — removed dead parameter
- `src/hf_timestd/core/multi_broadcast_fusion.py` — fixed concerns 9-10

## Files Created
- `src/hf_timestd/core/carrier_tec.py`
- `src/hf_timestd/core/iono_tomography.py`
- `src/hf_timestd/core/vtec_mapper.py`
- `docs/changes/SESSION_2026_02_14_TEC_PIPELINE_AUDIT.md`

## Deployment
```bash
sudo scripts/update-production.sh --pull
sudo systemctl restart timestd-physics-fusion.service
```
