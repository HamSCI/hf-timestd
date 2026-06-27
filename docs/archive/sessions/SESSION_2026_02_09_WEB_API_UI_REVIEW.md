# Session 2026-02-09: Web-API UI Review and Fixes

## Context
Pre-demo review of all 13 web-API UI pages for the HamSCI-WWV working group
presentation. Also includes test signal pipeline fixes from earlier in the session.

## Test Signal Pipeline Fixes (src/)

### wwv_test_signal.py — 3 metric calculation bugs
- **SNR estimator**: Replaced broadband power ratio (always ~0 dB because tones are
  narrowband) with spectral SNR measuring tone peak vs adjacent noise floor in FFT domain
- **Coherence time estimator**: Limited to first 5 windows (0–15 dB attenuation) where
  tone SNR is sufficient; switched to least-squares detrending instead of anchoring to
  tone_powers[0]
- **Channel quality thresholds**: Adjusted from unrealistic values (SNR > 5 dB for "fair")
  to HF-appropriate thresholds where spectral SNR is typically 5–15 dB

### chu_fsk_decoder.py — Memory leak fix
- Extract ~1.1s audio slice before `_fsk_demodulate()` instead of passing full 60s buffer
- Reduces scipy.signal.hilbert() peak allocation from ~23 MB to ~0.4 MB per call
- Prevents glibc malloc arena fragmentation that caused 1.9–2.3 GB RSS after 14+ hours

### metrology_service.py — Memory leak fix
- `np.frombuffer(data, dtype=np.complex64).copy()` prevents numpy holding reference to
  decompressed bytes
- `np.array(mm); del mm` releases memmap file descriptor promptly

## Web-API Backend Fixes

### routers/logs.py — Graceful fallback for journalctl failure
- When journalctl returns non-zero (permission denied), return `{error: "hint", logs: [],
  count: 0}` instead of HTTP 500
- Added `timestd` user to `systemd-journal` group so logs now work in production

### routers/correlations.py — Timeout wrapper
- All 5 correlation endpoints wrapped with 15s async timeout via ThreadPoolExecutor
- Returns HTTP 504 with helpful message instead of hanging indefinitely
- Root cause: `get_mode_timeline()` does full HDF5 table scans, and `propagation-kp`
  has O(n×m) nested loop over timestamps × Kp measurements

## Web-API Frontend Fixes

### test_signal.html — Frequency Analysis tab broken
- **Root cause**: `plotS4Tones(measurements)` and `plotS4Slope(measurements)` referenced
  undefined variable `measurements`; should be built from `data.by_station`
- Fixed: builds flat array from `data.by_station.WWV` + `data.by_station.WWVH`
- Multi-Frequency S4 chart now shows average per-tone S4 for selected carrier frequency
- S4 Frequency Slope chart shows slope over time for all frequencies
- Fixed corrupted emoji in `<h1>` tag

### logs.html — Error handling
- Shows backend error hint (with fix command) instead of blank/broken display when
  journalctl access is denied

### Navigation consistency
- Added 📡 Test Signal link to nav bar on all 13 pages
- Added missing 📊 24h link to solar-correlation.html nav

## Endpoint Verification (17 endpoints tested)

| Status | Count | Notes |
|--------|-------|-------|
| 200 OK | 16 | All core, propagation, physics, dashboard, validation, logs, docs, CHU FSK |
| 404 (data) | 1 | `correlations/sid-detection` — no L2 propagation data available (not a code bug) |

## Known Remaining Issues (for next session)
- Correlation sub-tabs on solar-correlation.html depend on L2 timing data accumulating
- Space weather xray/kp return empty when no recent NOAA data in window
- `get_mode_timeline()` full HDF5 table scan is fundamentally slow — needs indexed reads
- Some pages could benefit from better empty-state messaging
