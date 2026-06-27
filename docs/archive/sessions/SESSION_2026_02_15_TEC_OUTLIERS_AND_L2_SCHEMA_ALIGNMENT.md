# Session 2026-02-15: TEC Outliers + L2 Schema Alignment

## Objective
Address the two remaining items from the dashboard/tick_timing investigation:

1. Bound physically unreasonable TEC outliers and reduce mode-mixing contamination.
2. Make L2 `raw_arrival_time_ms` and `clock_offset_ms` semantics consistent with schema/docs.

---

## Root Causes Confirmed

### 1) L2 timing field semantic mismatch
`l2_calibration_service.py` was writing L1 `raw_toa_ms` (which currently carries D_clock) into both timing concepts downstream, causing ambiguity between:
- absolute raw arrival time, and
- D_clock (`observed - expected`).

### 2) TEC outlier path in fusion
Even with prior estimator hardening, fusion still needed stricter input gating and output bounds:
- shared-frequency multi-mode observations can still mix propagation families,
- extreme TEC values can leak into L3 products without a final product-level bound.

---

## Code Changes

### A. L2 writer now stores distinct timing semantics
**File:** `src/hf_timestd/core/l2_calibration_service.py`

- Reconstruct absolute raw arrival time at write time:
  - `raw_arrival_time_ms = d_clock_ms + propagation_delay_ms`
- Continue writing D_clock to `clock_offset_ms`.

This restores the schema equation:
`clock_offset_ms = raw_arrival_time_ms - propagation_delay_ms`

### B. Dashboard reads D_clock from the proper L2 field
**File:** `web-api/routers/dashboard.py`

- Switched timing series source from `raw_arrival_time_ms` to `clock_offset_ms` in:
  - 24h broadcast endpoint
  - 24h timing-error endpoint

This prevents dashboard regressions after restoring `raw_arrival_time_ms` to absolute ToA semantics.

### C. Mode-gated TEC inputs in real-time fusion
**File:** `src/hf_timestd/core/multi_broadcast_fusion.py`

- Added dominant-mode gating before station TEC solve:
  - group by normalized base `propagation_mode`,
  - exclude invalid/derived labels (`UNKNOWN`, `FALLBACK`, `TICK`, `FSK`, etc.),
  - solve only with dominant group if it has at least 2 measurements.
- Passed `mode_confidence` through to estimator input weighting.

This reduces 1F/2F contamination on shared channels.

### D. Product-level TEC hard bound in physics service
**File:** `src/hf_timestd/core/physics_fusion_service.py`

- Added skip guard before writing TEC records:
  - reject values outside `(0, 200]` TECU,
  - emit warning and skip out-of-range result.

### E. Schema test conformance fix
**File:** `src/hf_timestd/schemas/l2_tick_phase_v1.json`

- Added missing top-level `quality_flags` to satisfy schema structure invariant tests.

---

## Validation

### Targeted suite (in venv)
Command:

```bash
venv/bin/python -m pytest -q tests/test_physics_fusion.py tests/unit/test_schemas.py tests/unit/test_hdf5_io.py
```

Result:
- **28 passed**
- **0 failed**

### Additional note
- Existing Pydantic v2 deprecation warnings are unchanged and non-blocking for this session.

---

## Files Modified

- `src/hf_timestd/core/l2_calibration_service.py`
- `src/hf_timestd/core/multi_broadcast_fusion.py`
- `src/hf_timestd/core/physics_fusion_service.py`
- `src/hf_timestd/schemas/l2_tick_phase_v1.json`
- `web-api/routers/dashboard.py`
- `docs/changes/SESSION_2026_02_15_TEC_OUTLIERS_AND_L2_SCHEMA_ALIGNMENT.md`
