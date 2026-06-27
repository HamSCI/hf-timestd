# Session 2026-02-11: Phase Extraction, Cross-Talk Fix, Signal Audit

## Summary

Comprehensive audit of the detection pipeline's signal processing chain, fixing
WWV/WWVH cross-talk on shared frequencies, correcting CHU modulation documentation,
and implementing three-tier phase extraction from IQ samples for ionospheric analysis.

## Changes

### 1. Cross-Frequency Discrimination Gate (metrology_engine.py)

**Problem:** On shared channels (2.5, 5, 10, 15 MHz), the 5 ms matched filter
template has only 9.7 dB rejection between 1000 Hz (WWV) and 1200 Hz (WWVH) —
33% cross-response. Every strong WWV tick also triggered the WWVH template and
vice versa, producing identical SNR plots for both stations.

**Fix:** After finding a correlation peak at the claimed frequency, also correlate
at the competing frequency (`CROSS_FREQ_PAIRS = {1000: 1200, 1200: 1000}`). Reject
if the claimed frequency doesn't dominate by at least `MIN_FREQ_ADVANTAGE_DB = 3.0 dB`.
Rejection reason: `cross_freq`.

**Result:**
- Before: WWV 17.4%, WWVH 17.9% detection rate (identical — suspicious)
- After: WWV 72.0%, WWVH 54.7% (WWV stronger as expected at 1120 km vs 6600 km)
- 3–11 cross-freq rejections per minute on shared channels

**Note:** The tick_matched_filter already had ±100 Hz bandpass filtering and was
not affected by this issue.

### 2. Three-Tier Phase Extraction (tick_matched_filter.py)

**Problem:** All detection paths AM-demodulated (`|IQ|`) before correlation,
destroying the RF carrier phase. The `phase_rad` field measured audio modulation
phase, not ionospheric phase.

**Fix:** Added two new phase measurements extracted directly from IQ:

| Field | Method | What it measures |
|-------|--------|-----------------|
| `phase_rad` | `atan2(corr_sin, corr_cos)` on AM envelope | Audio modulation phase |
| `carrier_phase_rad` | `IQ × exp(-j·2π·f_tone·t)` → `angle(mean)` | RF phase at tone frequency |
| `dc_carrier_phase_rad` | `angle(mean(IQ))` over tick duration | Bare RF carrier phase (DC phasor) |

- `carrier_phase_rad`: Mixes IQ down to the tone frequency and coherently averages
  over the tick duration. The resulting phasor angle tracks the RF carrier phase at
  the tone frequency, which changes with ionospheric path length.
- `dc_carrier_phase_rad`: The DC component of the IQ baseband — the bare carrier
  phasor. On **unambiguous channels** (CHU 3.33/7.85/14.67, WWV 20/25 MHz) this is
  a clean, high-power phase reference independent of tone detection. On shared
  channels it's a mix of multiple carriers.

**Early results (phase std over 55-window minutes):**

| Channel | audio std | carrier std | dc std | audio/dc ratio |
|---------|-----------|-------------|--------|----------------|
| CHU_14670 | 2.011 | 1.789 | 1.548 | **1.30×** |
| CHU_7850 | 1.882 | 1.818 | 1.681 | **1.12×** |
| WWV_25000 | 1.581 | 1.628 | 1.458 | **1.08×** |

DC carrier phase is most stable on unambiguous channels, as expected.

### 3. CHU Modulation Comment Corrections

**Error:** Comments in metrology_engine.py, tick_matched_filter.py, and
tone_detector.py (5 locations) incorrectly stated CHU uses "DSB suppressed carrier."

**Fact:** CHU transmits USB with preserved carrier. The IQ baseband has a strong DC
carrier component plus the 1000 Hz tone as a USB sideband. The `Re(IQ)` demodulation
code was already correct; only the comments were wrong.

### 4. Allan Deviation UI (metrology.html)

Removed hardcoded "Since Stable (Jan 1 1900Z)" button. Replaced with standard data
windows: 1000s, 10000s, 100000s (default), 24h, 7d, All Data (365d).

### 5. L2/tick_phase Schema Update (l2_tick_phase_v1.json)

Added `carrier_phase_rad` and `dc_carrier_phase_rad` fields to the schema.
Updated `phase_rad` description to clarify it is audio-domain modulation phase.

### 6. L2/detection_attempts Data Product

New HDF5 product persisting every measurement attempt (detected + rejected) with
rejection reasons and metrics. Enables offline threshold calibration.

- Schema: `l2_detection_attempts_v1.json`
- Registered in `data_product_registry.py`
- Writer in `metrology_service.py`

## Files Modified

| File | Changes |
|------|---------|
| `src/hf_timestd/core/metrology_engine.py` | Cross-freq gate, search window scaling, always-return-dict refactor, CHU comment fix |
| `src/hf_timestd/core/tick_matched_filter.py` | IQ carrier phase + DC phasor extraction, CHU comment fix |
| `src/hf_timestd/core/metrology_service.py` | tick_phase_writer, attempts_writer, carrier/dc phase persistence |
| `src/hf_timestd/core/tone_detector.py` | CHU comment fixes (3 locations) |
| `src/hf_timestd/data_product_registry.py` | Register tick_phase + detection_attempts |
| `src/hf_timestd/schemas/l2_tick_phase_v1.json` | New schema with 3 phase fields |
| `src/hf_timestd/schemas/l2_detection_attempts_v1.json` | New schema for all attempts |
| `web-api/static/metrology.html` | Allan deviation time range buttons |
| `web-api/routers/dashboard.py` | Dashboard reads tick_timing for SNR data density |

## Remaining Work

- **Phase continuity**: All phase measurements show ~1.5–2.0 rad std per window.
  Need to investigate window-to-window phase tracking, possible composite template
  artifacts, and phase unwrapping.
- **Threshold calibration**: `BASE_CORR_SNR_DB = 8.0` is now the dominant rejection
  reason. `L2/detection_attempts` data enables data-driven recalibration.
- **Web-API visualization**: Expose phase and Doppler data through the dashboard.
  Phase drift → Doppler, phase jumps → mode changes, scintillation → irregularities.
  Expect correlation with propagation mode.
