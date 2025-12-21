# Analytics Pipeline Issues - Investigation Notes

**Date**: 2025-12-21
**Status**: Requires investigation and fixes

## Summary

The Phase 2 analytics pipeline has numerical stability issues causing invalid Doppler, SNR, and propagation mode values. The web UI correctly displays what the analytics produces, but the analytics itself is outputting zeros/NaN values.

## Symptoms Observed

1. **Doppler values are zero** for all frequencies/stations
2. **SNR values missing** (showing as `—` or 0)
3. **Identical propagation mode (1E)** across all frequencies (physically impossible)
4. **Empty Doppler CSV columns** in recent data

## Root Cause Analysis

### Log Evidence

From `/var/lib/timestd/logs/phase2-shared10.log`:

```
RuntimeWarning: overflow encountered in square
  /opt/hf-timestd/src/hf_timestd/core/phase2_analytics_service.py:1429

RuntimeWarning: Use of fft convolution on input with NAN or inf results in NAN or inf output.
  /opt/hf-timestd/venv/lib/python3.11/site-packages/scipy/signal/_signaltools.py:283

RuntimeWarning: invalid value encountered in multiply
  /opt/hf-timestd/venv/lib/python3.11/site-packages/scipy/signal/_short_time_fft.py:1245

DEBUG SHARED_10000: Phase SNR: Carrier=-100.0dB, WWV=-100.0dB, WWVH=-100.0dB (threshold=0.0dB)

INFO - SHARED_10000: Phase extraction: WWV=0 (avg SNR -100.0dB), WWVH=0 (avg SNR -100.0dB), 
       threshold=0.0 dB, noise_floor=nan dB

WARNING - Step 2B Doppler estimation failed: unsupported format string passed to NoneType.__format__
```

### Key Issues

1. **Line 1429 in `phase2_analytics_service.py`**: Overflow in square operation
2. **NaN propagation**: Once NaN enters the signal chain, it corrupts all downstream calculations
3. **noise_floor=nan**: Noise floor calculation returning NaN, causing SNR threshold to fail
4. **Phase extraction returning 0 phases**: No valid phase data extracted due to upstream NaN

### Data Flow

```
IQ Samples → FFT Convolution (NaN warning) → Phase Extraction (0 phases, -100dB SNR)
                                                      ↓
                                           Doppler Estimation (fails)
                                                      ↓
                                           CSV Output (zeros/empty)
```

## Files to Investigate

### Primary

1. **`/home/mjh/git/hf-timestd/src/hf_timestd/core/phase2_analytics_service.py`**
   - Line 1429: `overflow encountered in square`
   - Phase extraction logic
   - Noise floor calculation

2. **`/home/mjh/git/hf-timestd/src/hf_timestd/core/wwvh_discrimination.py`**
   - `extract_per_tick_phases()` method
   - `estimate_doppler_per_tick()` method (lines ~1980-2120)
   - Phase unwrapping and Doppler calculation

3. **`/home/mjh/git/hf-timestd/src/hf_timestd/core/phase2_temporal_engine.py`**
   - Step 2B Doppler estimation (lines ~1080-1115)
   - Fixed minor NoneType format bug, but root cause is upstream

### Secondary

4. **Systemd service files** - Need to remove "GRAPE" references:
   - `/etc/systemd/system/timestd-analytics.service` - Description says "GRAPE Analytics Service"
   - Should be renamed to "HF TimeStd Analytics Service" or similar

## Doppler Scaling Note

The Doppler values in the CSV are measured at the **tone frequency (~1000 Hz)**, not the carrier frequency (~10 MHz). To get actual ionospheric Doppler at carrier frequency:

```
carrier_doppler_hz = tone_doppler_hz × (carrier_freq / tone_freq)
carrier_doppler_hz = tone_doppler_hz × 10000  (approximately)
```

This scaling is now implemented in the web UI (`ionosphere.html`), but the analytics pipeline should ideally output both values.

## Expected Doppler Values

- **Typical HF ionospheric Doppler**: ±1-5 Hz at carrier frequency
- **During rapid layer changes** (sunrise/sunset, TIDs, sporadic E): Up to ±10 Hz
- **Current output**: 0.0 Hz (invalid)

## Quick Verification Commands

```bash
# Check recent Doppler CSV
tail -10 /var/lib/timestd/phase2/CHU_3330/doppler/CHU_3330_doppler_$(date +%Y%m%d).csv

# Check analytics logs for errors
tail -100 /var/lib/timestd/logs/phase2-shared10.log | grep -iE "error|warn|nan|overflow"

# Check clock_offset CSV (source for web UI)
tail -5 /var/lib/timestd/phase2/CHU_3330/clock_offset/CHU_3330_clock_offset_$(date +%Y%m%d).csv
```

## Suggested Fix Approach

1. **Add NaN guards** at the input stage of phase extraction
2. **Clamp values** before square operations to prevent overflow
3. **Validate IQ samples** before processing (check for inf/nan)
4. **Add fallback values** when calculations fail instead of propagating NaN
5. **Consider using `np.nan_to_num()`** at critical points

## GRAPE Reference Cleanup

The project was renamed from GRAPE to HF TimeStd. References to remove:

```bash
# Find GRAPE references
grep -ri "grape" /etc/systemd/system/timestd*.service
grep -ri "grape" /home/mjh/git/hf-timestd/src/
```

Update systemd service descriptions and any remaining code references.

## Web UI Status

The web UI reorganization is **complete and deployed**:

- 4-page structure: Summary, Timing, Ionosphere, Logs
- Ionosphere page has path selector, per-frequency table, solar zenith overlay
- Doppler scaling (×10000) implemented in display
- Deployed to `/opt/hf-timestd/web-ui/`

The UI correctly displays what the analytics produces - once the analytics pipeline is fixed, the UI will show valid data.
