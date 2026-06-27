# Session 2026-02-15: Tick Timing Reference Frame Fix

## Problem

The tick_matched_filter's `_detect_minute_marker()` and `process_minute()` computed
timing offsets relative to the buffer start (sample 0), not relative to UTC.  Since
the buffer start is arbitrary (set by the recorder, not by physics), the resulting
`mean_timing_offset_ms` was a buffer-relative position (0–500ms), not a proper
D_clock measurement.  This value was written to `tick_timing/` HDF5 as `d_clock_ms`
and consumed by the fusion engine — contaminating downstream products.

### Minute marker correlation issue

The `_detect_minute_marker` function searched a wide region around sample 0 with no
knowledge of where UTC second 0 actually fell in the buffer.  The IQ-domain
correlation of long templates (500–800ms) against continuous carrier signals produced
a nearly flat envelope, causing the peak to land at the search window edge rather
than at the true marker onset.  Detected offsets jumped 50–100ms between minutes —
far exceeding any physical variation.

## Root Cause

1. `_detect_minute_marker` had no access to `buffer_timing` (the RTP-derived
   sample↔UTC mapping).  It assumed sample 0 = second 0.
2. `process_minute` computed D_clock as `timing_offset_ms` directly, which is
   buffer-relative, not UTC-relative.
3. `metrology_service.py` wrote `mean_timing_offset_ms` as `d_clock_ms`.
4. `multi_broadcast_fusion.py` consumed `mean_timing_offset_ms` for timing fusion.

## Fix

### tick_matched_filter.py
- Added `d_clock_ms` and `d_clock_uncertainty_ms` fields to `MinuteTickAnalysis`
- `process_minute()` accepts `buffer_timing` and `minute_boundary` parameters
- `_detect_minute_marker()` uses `buffer_timing.utc_to_sample(minute_boundary)` to
  locate where second 0 falls in the buffer, then searches forward (the tone arrives
  after geometric time-of-flight: 3–80ms for HF skywave)
- D_clock computed by converting detected sample positions to UTC via
  `buffer_timing.sample_to_utc()`, then subtracting the expected UTC second
- Search window: 5ms before sec0 through 100ms after (covers all realistic HF ToF)

### metrology_engine.py
- Passes `buffer_timing` and `minute_boundary` to `tick_filter.process_minute()`

### metrology_service.py
- Uses `tick_analysis.d_clock_ms` instead of `mean_timing_offset_ms`

### multi_broadcast_fusion.py
- Uses `d_clock_ms` instead of `mean_timing_offset_ms` from tick_timing data
- Skips entries where `d_clock_ms` is None (no timing authority available)

## Key Insight: Two Timing Paths

The system has two independent timing paths:

1. **Primary (ntpd-style edge detector)**: AM-envelope matched filter with
   front-edge back-calculation → L1 `raw_toa_ms` → L2 `clock_offset/` HDF5 →
   dashboard.  This produces correct timing residuals (D_clock = observed − expected
   propagation delay).  Values of ±1–10ms are physically correct residuals, not
   absolute arrival times.

2. **Secondary (IQ tick matched filter)**: Complex IQ correlation for carrier phase
   and Doppler extraction → `tick_timing/` HDF5.  Its D_clock is now architecturally
   correct (uses timing authority) but the IQ correlation has limited timing
   discrimination against continuous carriers.  Primary value is carrier phase, not
   timing precision.

The dashboard reads from path 1.  The tick_timing D_clock (path 2) feeds the fusion
engine as a supplementary input.

## Verification

- `buffer_timing.utc_to_sample()` confirmed at line 75 of `buffer_timing.py`
- Per-second tick offsets: WWVH +22ms, BPM +15ms, WWV +41ms on SHARED_10000
  (physically plausible propagation delays)
- Primary detector (L2): WWV residuals ±1–10ms, WWVH ±7–10ms — correct when
  propagation_delay_ms is added back (WWV 4ms < WWVH 24ms < BPM 39ms)
- Physics gate validates primary detector: WWV timing_err=+1.3ms (0.3σ)

## Remaining Issues

1. **TEC outliers**: Max 3930 TECU, only 35.9% in 1–100 TECU range
2. **L2 schema inconsistency**: `raw_arrival_time_ms == clock_offset_ms` in HDF5
   (schema says `clock_offset = raw_arrival - propagation_delay`)
3. **IQ minute marker correlation**: Flat envelope against continuous carriers limits
   timing discrimination.  Not a priority — carrier phase is the IQ module's purpose.

## Files Modified

- `src/hf_timestd/core/tick_matched_filter.py`
- `src/hf_timestd/core/metrology_engine.py`
- `src/hf_timestd/core/metrology_service.py`
- `src/hf_timestd/core/multi_broadcast_fusion.py`
