# Propagation Model Critique — Session 2026-02-12

## Overview

Critical review of v6.7 propagation model (`propagation_model.py`, `iono_data_service.py`)
and integration points (`arrival_pattern_matrix.py`, `metrology_engine.py`), plus zombie code audit.

Reviewed from four perspectives: Software Engineer, Ionospheric Scientist, Metrologist, User.

---

## BUGS — Must Fix Before Deployment

### B1. HFPropagationModel instantiated on every call in metrology_engine.py

**File:** `src/hf_timestd/core/metrology_engine.py:350-358`

Every call to `_predict_geometric_delay()` when ArrivalPatternMatrix is unavailable creates a
**new** HFPropagationModel instance:
- Re-computes all haversine distances
- Logs "HFPropagationModel initialized" (log spam)
- Creates a new empty cache (defeating the 60s cache TTL)
- Lazy-inits a new IonoDataService reference

**Fix:** Cache the model instance on `self` (e.g., `self._prop_model_fallback`).

### B2. IonoDataService singleton not safe against re-parameterization

**File:** `src/hf_timestd/core/iono_data_service.py:292-307`

First caller's parameters win. If test code calls `get_instance(enable_wamipe=False)` before
production code calls `get_instance()`, WAM-IPE is permanently disabled. The `__init__` is
also public, so non-singleton instances bypass the lock.

**Fix:** Validate that subsequent `get_instance()` calls have compatible parameters, or
warn when parameters differ from existing instance.

### B3. TEC fallback missing ×2 factor — underestimates ionospheric delay by 2×

**File:** `src/hf_timestd/core/propagation_model.py:779-785`

The numerical integration (`_integrate_group_delay`) multiplies by `2.0 * n_hops` (up + down
per hop), but the TEC fallback (`_tec_group_delay`) multiplies by only `n_hops`.

VTEC is a one-way vertical integral. Obliquity converts to one-way slant. For a single hop,
the signal traverses the ionosphere **twice** (up and down). The TEC method needs `* 2.0`.

**Fix:** Change `_tec_group_delay` to `delay_ms = delay_s * 2.0 * n_hops * 1000.0`.

### B4. Unbounded `requests` import at runtime in background thread

**File:** `src/hf_timestd/core/iono_data_service.py` (4 locations)

`requests` is imported inside `_fetch_wamipe()`, `_fetch_giro()`, etc. If not installed,
the background thread logs an error every 5 minutes forever.

**Fix:** Import at module level with graceful fallback, or check once at `start()`.

### B5. Cache directory creation can crash singleton init

**File:** `src/hf_timestd/core/iono_data_service.py:346`

If `/var/lib/timestd` has wrong permissions, `mkdir()` raises PermissionError during
`__init__`, crashing the singleton creation and propagating up to kill the caller.

**Fix:** Wrap in try/except, fall back to temp directory.

### B6. GIRO station list never refreshed

**File:** `src/hf_timestd/core/iono_data_service.py:661`

Station list fetched once. If first fetch fails (network down at startup), `_giro_stations`
stays empty forever — GIRO corrections permanently disabled.

**Fix:** Retry station list fetch periodically (e.g., every hour).

### B7. GIRO correction uses degree-distance, not km-distance

**File:** `src/hf_timestd/core/iono_data_service.py:930-933`

Euclidean distance in degrees is distorted by latitude. Acceptable at mid-latitudes but
incorrect for equatorial/polar stations.

---

## PHYSICS ERRORS

### P1. Midpoint ionosphere assumption wrong for multi-hop paths

**File:** `src/hf_timestd/core/propagation_model.py:309-311`

For 2F/3F paths, ionospheric pierce points are at 1/4, 3/4 (2F) or 1/6, 3/6, 5/6 (3F)
of the great circle, NOT at the midpoint. Using midpoint for 3F to BPM (~10,000 km) means
iono params sampled ~2,500 km from actual reflection points.

**Fix:** For multi-hop, sample at each reflection point and average.

### P2. MUF formula uses flat-Earth secant law

**File:** `src/hf_timestd/core/propagation_model.py:533-535`

`foF2 / sin(elevation)` is the flat-Earth approximation. Overestimates MUF by 10-20% at
<15° elevation. Not a bug per se but should be documented.

### P3. Chapman scale height fixed at 60 km

**File:** `src/hf_timestd/core/iono_data_service.py:1049`

F2 scale height varies 40-90 km. Fixed 60 km means Chapman profile TEC wrong by up to 50%.

**Fix:** Derive from available parameters: `H ≈ 0.22 * hmF2` (rough scaling).

### P4. E-layer NmE = 10% of NmF2 at night — should be near zero

**File:** `src/hf_timestd/core/iono_data_service.py:1058`

E-layer disappears at night. Setting NmE = 0.1 × NmF2 regardless of time adds phantom
electron density that biases numerical integration.

**Fix:** Scale NmE by solar zenith angle or time-of-day factor.

### P5. Path TEC uses linear lat/lon interpolation, not great circle

**File:** `src/hf_timestd/core/iono_data_service.py:884-885`

For long paths (BPM ~10,000 km), linear interpolation doesn't follow great circle.

### P6. Obliquity factor constant with altitude

**File:** `src/hf_timestd/core/propagation_model.py:694-697`

Standard thin-shell mapping function gives ~15% less obliquity than `1/sin(e)` at 300 km
and 10° elevation. Systematic bias in ionospheric delay estimate.

---

## CODE QUALITY

### Q1. Three independent parametric fallback models

1. `propagation_model.py:_parametric_iono()` — no seasonal, no latitude
2. `iono_data_service.py:_climatological_fallback()` — has seasonal + equatorial anomaly
3. `arrival_pattern_matrix.py:_get_ionospheric_height_km()` — different amplitude

**Fix:** Consolidate into single canonical model in `iono_data_service.py`.

### Q2. Station coordinates hardcoded in 4 places

`propagation_model.py`, `arrival_pattern_matrix.py`, `physics_propagation.py`, `wwv_constants.py`

**Fix:** Import from `wwv_constants` everywhere.

### Q3. Haversine implemented 4 times

**Fix:** Single utility function.

### Q4. `_integrate_group_delay` uses Python loop instead of numpy

Trivially vectorizable for 10-100× speedup.

### Q5. Prediction cache misleading TTL

Cache entries never TTL-evicted; staleness controlled by key bucketing only.

---

## METROLOGICAL CONCERNS

### M1. Confidence values (0.0-0.8) are arbitrary, not calibrated

Directly control adaptive uncertainty window width → acceptance rate.

**Fix:** After deployment, compute actual RMS errors per data source.

### M2. Uncertainty sigma convention inconsistent

`ModeArrival.uncertainty_ms` is 1-sigma; `ExpectedArrival.uncertainty_3sigma_ms` is 3-sigma.
Naming is correct but error-prone.

### M3. Self-consistency check is dead code

`self_consistency_check()` exists but nothing calls it or acts on results.

**Fix:** Wire into fusion pipeline to flag/downweight inconsistent measurements.

### M4. No traceability of delay predictions in HDF5

`data_source` field exists in predictions but not written to HDF5 with measurements.

**Fix:** Add `model_data_source` and `model_confidence` to L2/timing_measurements schema.

### M5. Base uncertainty values not physically justified

WAM-IPE+GIRO base 0.5 ms seems reasonable. Parametric base 3.0 ms likely too optimistic
during disturbed conditions (hmF2 error 80+ km → >0.5 ms/hop, TEC error 20+ TECU).

---

## USER-FACING ISSUES

### U1. IonoDataService is NOT started — v6.7 real-time capability dead on arrival

`IonoDataService.get_instance().start()` is never called in `metrology_service.py`.
WAM-IPE never fetched, GIRO never applied. Model always falls to parametric/IRI fallback.

### U2. No web-api endpoint for model observability

No `/api/propagation/matrix` endpoint. Model is a black box.

### U3. CHU 7.85 MHz multi-hop acceptance untested in production

The primary motivation for v6.7 hasn't been validated against live data.

---

## ZOMBIE CODE AUDIT

### `physics_propagation.py` — SUPERSEDED, deprecate

796 lines entirely superseded by `propagation_model.py`. Both do multi-mode evaluation,
IRI integration, geometric hops, TEC delay. The new module uses spherical geometry (more
accurate) vs flat-Earth in the old one. PyLap support (never used) is the only unique feature.

**Recommendation:** Deprecate. Add PyLap to `HFPropagationModel` if needed later.

### `ionospheric_model.py` — PARTIALLY OVERLAPPING, keep for IRI/IONEX

1437 lines. Unique capabilities: IRI-2020/2016 integration, IONEX parsing, calibration layer.
Parametric fallback is duplicated.

**Recommendation:** Keep for IRI/IONEX. Remove parametric fallback (delegate to iono_data_service).
Wire IONEX into iono_data_service as additional data source.

### `bootstrap_validator.py` — STALE, uses hardcoded delay bounds

`EXPECTED_DELAYS_MS` is static, ignores frequency/time/ionosphere. `_get_expected_delay()`
returns midpoint of static bounds.

**Recommendation:** Wire `HFPropagationModel` into `_get_expected_delay()`.

---

## FIX PRIORITY

### Must Fix (before deployment)
| # | Issue | File | Impact |
|---|-------|------|--------|
| B1 | Model instantiated per-call | metrology_engine.py:354 | Performance, log spam |
| B3 | TEC fallback missing ×2 | propagation_model.py:783 | Iono delay underestimated 2× |
| U1 | IonoDataService never started | metrology_service.py | v6.7 real-time dead |
| P1 | Midpoint iono for multi-hop | propagation_model.py:310 | Wrong iono for 2F/3F |

### Should Fix (this session)
| # | Issue | File | Impact |
|---|-------|------|--------|
| B2 | Singleton re-parameterization | iono_data_service.py:292 | Silent misconfiguration |
| B4 | requests import in thread | iono_data_service.py | Repeated import failures |
| B5 | Cache dir permission crash | iono_data_service.py:346 | Init crash |
| B6 | GIRO stations never refreshed | iono_data_service.py:661 | Permanent GIRO failure |
| P3 | Fixed scale height 60 km | iono_data_service.py:1049 | TEC error up to 50% |
| P4 | E-layer at night | iono_data_service.py:1058 | Phantom electron density |
| Q1 | Three parametric fallbacks | Multiple | Inconsistent predictions |
| Q2 | Duplicated station coords | Multiple | Maintenance risk |

### Nice to Have (future)
| # | Issue | Impact |
|---|-------|--------|
| M1 | Calibrate confidence values | Better acceptance rates |
| M3 | Wire self-consistency check | Model quality monitoring |
| M4 | Traceability in HDF5 | Historical analysis |
| P5 | Great circle path sampling | Better long-path TEC |
| P6 | Altitude-dependent obliquity | More accurate delay |
| U2 | Web-api propagation endpoint | Observability |
