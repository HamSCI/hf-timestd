# Session 2026-02-11: Phase Continuity Fix + Doppler Visualization

## Part 1: Phase Extraction Methodology Review

### Root Cause: Phase Continuity Bug

All three phase measurements (audio, carrier, DC) showed σ_φ ≈ 1.5–2.0 rad between
consecutive overlapping windows — near uniform random on [-π,π]. The system was
extracting phase but it was meaningless noise.

**Two bugs found in `_correlate_window()` in `tick_matched_filter.py`:**

1. **Window-relative mixer time**: The IQ mixer `exp(-j·2π·f·t)` used `t_tick`
   starting at 0 for each tick extraction. Since the same IQ sample appeared in
   different windows at different `t` values, the mixer phase depended on which
   window contained the sample. A 1-sample shift at 1000 Hz / 20 kHz gives
   0.314 rad of spurious phase jump.

2. **Whole-window phase extraction**: `tick_samples = len(template_sin)` used the
   composite template length (5 seconds = 100,000 samples), not individual tick
   durations. Phase was averaged over the entire 5-second window including
   inter-tick noise, massively diluting the estimate.

### Fix Applied

- **Buffer-relative time**: The mixer now uses `t_abs = start_second + adjusted_start/sample_rate + arange(n)/sample_rate`, which maps each IQ sample to a unique, consistent time value regardless of which overlapping window contains it. This is purely sample-index-based — independent of RTP, GPS, NTP, or any external timing authority. Works identically in both RTP and FUSION modes.

- **Per-tick extraction**: Phase is now extracted from each individual tick separately (at the tick's actual duration), then per-tick phasors are combined coherently via complex summation. Between ticks the signal is noise — excluding it dramatically improves the phase estimate.

- **Type hint fix**: Return type annotation corrected from 5-tuple to 6-tuple.

- **`process_window()` updated**: Now passes `start_second` and `valid_seconds` through to `_correlate_window()`.

### Regression Tests

Two new tests in `tests/test_tick_matched_filter.py`:

- `test_carrier_phase_continuity`: Generates synthetic IQ with known constant carrier phase, verifies σ of phase differences between consecutive windows < 0.3 rad (was ~1.7 rad before fix).
- `test_dc_carrier_phase_stability`: Same for DC carrier phase on CHU-like unambiguous channels.

Both pass. All 22 existing tests continue to pass (1 pre-existing flaky offset test at borderline tolerance).

## Part 2: Phase/Doppler Web-API-UI

### New API Endpoints

| Endpoint | Purpose |
|----------|---------|
| `GET /api/phase/timeseries` | Phase time series (unwrapped), per-channel/station |
| `GET /api/phase/doppler` | Doppler shift from phase rate, with smoothing |
| `GET /api/phase/scintillation` | Phase scintillation index (σ_φ) time series |
| `GET /api/phase/summary` | Current phase/Doppler state across all channels |

All endpoints support `start`, `end`, `channel`, `station` query parameters.
Time ranges accept relative formats (`-1h`, `-24h`) or ISO8601.

### New Files

| File | Purpose |
|------|---------|
| `web-api/services/phase_service.py` | Phase/Doppler analysis service — reads L2/tick_phase HDF5, computes unwrapped phase, Doppler, scintillation |
| `web-api/routers/phase.py` | FastAPI router for phase endpoints |
| `web-api/static/phase.html` | Phase/Doppler visualization dashboard |

### Dashboard Features

- **Carrier Phase vs Time**: Unwrapped phase traces per channel/station, reveals Doppler drift and mode changes
- **Doppler Shift vs Time**: f_D = -(1/2π) dφ/dt, with configurable smoothing
- **Phase Scintillation Index**: σ_φ over 60s sliding windows, with 0.3 rad irregularity threshold line
- **DC Carrier Phase**: Unambiguous channels only (CHU, WWV 20/25 MHz) — cleanest ionospheric observable
- **Summary Cards**: Per-channel Doppler, σ_φ, SNR at a glance
- **Controls**: Time range buttons (15m/1h/6h/24h), phase type selector, channel/station filters
- **Auto-refresh**: 60-second polling
- **Dark theme**: Consistent with existing dashboard

### Navigation

Phase link added to all 14 HTML pages in the nav bar, positioned between Ionosphere and TEC/TID.

### Integration

- Router registered in `web-api/routers/__init__.py` and `web-api/main.py`
- Page route `/phase` added to `main.py`
- Service reads HDF5 directly with `locking=False` (consistent with existing HDF5 lock fix)

## Files Modified

- `src/hf_timestd/core/tick_matched_filter.py` — Phase continuity fix (buffer-relative time, per-tick extraction)
- `tests/test_tick_matched_filter.py` — Phase continuity regression tests
- `web-api/services/phase_service.py` — **NEW** Phase/Doppler analysis service
- `web-api/routers/phase.py` — **NEW** Phase API router
- `web-api/static/phase.html` — **NEW** Phase/Doppler dashboard
- `web-api/routers/__init__.py` — Register phase_router
- `web-api/main.py` — Register phase_router, add /phase page route
- `web-api/static/*.html` (14 files) — Add Phase nav link
