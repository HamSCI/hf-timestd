# Session 2026-02-14 (late): Dashboard Flat D_clock Fix

## Problem

The 24-hour broadcast dashboard showed flat/constant patterns on most channels instead of the expected diurnal ionospheric variation. This appeared to indicate a measurement pipeline failure, but investigation revealed the measurements were correct — only the dashboard display was broken.

## Root Cause

Four bugs in the dashboard data pipeline (`web-api/routers/dashboard.py` and `web-api/static/dashboard-24h.html`):

### Bug 1: Field name mismatch (dashboard.py:197, 442)

The dashboard read `m.get('raw_toa_ms')` but the L2 HDF5 schema uses `raw_arrival_time_ms`. Since the field didn't exist, `timing_error` was always `None`, and the grid panels showed only SNR data — which appears flat on strong, stable channels.

### Bug 2: Double subtraction of expected delay (dashboard.py:200)

The code computed `timing_error = raw_toa - min_propagation_ms`, but `raw_arrival_time_ms` in the L2 HDF5 is already D_clock (observed − expected). The L2 calibration service (`l2_calibration_service.py:340`) writes `d_clock_ms = raw_toa_ms` to both `raw_arrival_time_ms` and `clock_offset_ms`. Subtracting `min_propagation_ms` again would double-subtract the baseline.

### Bug 3: Incompatible reference frame from tick_timing (dashboard.py:249)

The second pass injected `mean_timing_offset_ms` from tick_timing HDF5 into the same `timing_error_ms` array. But tick_timing's `mean_timing_offset_ms` is buffer-relative (0–500ms range) while clock_offset's `raw_arrival_time_ms` is D_clock (±15ms). This contaminated the timing error data with 500ms outliers.

### Bug 4: Missing timing error chart trace (dashboard-24h.html:renderMiniChart)

The `renderMiniChart()` function only plotted SNR on the primary y-axis. Even if timing error data were correct, it would never be displayed.

## Fix

1. Changed `m.get('raw_toa_ms')` → `m.get('raw_arrival_time_ms')` in both endpoints
2. Used `raw_arrival_time_ms` directly as timing error (no subtraction needed)
3. tick_timing second pass now contributes only SNR, not timing error (`None` for timing_error_ms)
4. Added timing error as primary chart trace with auto-scaled y-axis; SNR demoted to faint secondary on y3; solar elevation on y2

## Verification

- 17/17 broadcasts now have valid timing error data
- Timing error ranges physically reasonable (e.g., WWV_20000: [-14.4, +14.7] ms)
- Diurnal variation visible: WWVH_2500 Δ=5.59ms, CHU_3330 Δ=1.90ms, WWV_15000 Δ=2.77ms
- TEC pipeline confirmed producing diurnal patterns (CHU: 26 TECU night → 93 TECU afternoon)
- Fusion service healthy (grade B, ±1.3ms)
- IONEX files being generated (59 today)

## Files Modified

- `web-api/routers/dashboard.py` — field name fix, double-subtraction fix, tick_timing reference frame fix
- `web-api/static/dashboard-24h.html` — timing error chart trace added to renderMiniChart()
- `CRITIC_CONTEXT.md` — session resolution + next session objectives

## Issues Discovered (Deferred to Next Session)

1. **tick_timing reference frame bug**: `TickMatchedFilter._detect_minute_marker()` returns buffer-relative offset (0–500ms), not D_clock. `metrology_service.py:537` uses this as `d_clock_ms`.
2. **TEC outliers**: Max 3930 TECU in today's data. Only 35.9% in physically reasonable 1–100 TECU range.
3. **L2 schema inconsistency**: `raw_arrival_time_ms` and `clock_offset_ms` are identical in HDF5 data, contradicting schema documentation.
