# Session 2025-12-07: BCD Correlation and UI Fixes

## Summary
This session addressed multiple issues: BCD correlation amplitude problems, 440 Hz station ID filtering, noise floor measurement, and carrier.html UI simplification.

---

## 1. BCD Correlation Fix (Critical)

### Problem
BCD amplitudes were extremely small (0.0001-0.01) causing the web UI to show no BCD discrimination data.

### Root Cause
**Signal/Template Mismatch**: The code was:
1. Extracting the envelope from the 100 Hz BCD signal (demodulating)
2. Correlating against a template that still had the 100 Hz carrier

This is fundamentally wrong - you can't correlate a demodulated envelope against a modulated signal.

### Fix
**Direct 100 Hz correlation** - both signal and template use the 100 Hz carrier:

```python
# wwvh_discrimination.py - Line ~2016
# Step 2: Use the bandpass-filtered 100 Hz signal directly for correlation
# The 100 Hz carrier IS the BCD signal - correlate directly with template
if np.iscomplexobj(bcd_100hz):
    bcd_signal = np.real(bcd_100hz)
else:
    bcd_signal = bcd_100hz

# Normalize signal for correlation
bcd_signal = bcd_signal - np.mean(bcd_signal)

# Step 3: Generate expected BCD template for this minute (full 60 seconds)
# Template includes 100 Hz carrier modulated by BCD pattern
bcd_template_full = self._generate_bcd_template(minute_timestamp, sample_rate, envelope_only=False)
```

### Files Modified
- `src/grape_recorder/grape/wwvh_discrimination.py`
- `src/grape_recorder/grape/wwv_bcd_encoder.py` (added `envelope_only` parameter)

---

## 2. Geographic Predictor Integration for BCD

### Problem
BCD peak search was using a broad ±150ms window, making it hard to find the correct peaks.

### Fix
With improved timing from Phase 2, we know the expected delays for WWV and WWVH from the geographic predictor. Now searches in tight ±15ms windows around expected delays:

```python
if self.geo_predictor and frequency_mhz:
    expected = self.geo_predictor.calculate_expected_delays(frequency_mhz)
    wwv_expected_ms = expected['wwv_delay_ms']
    wwvh_expected_ms = expected['wwvh_delay_ms']
    
    # Search ±15ms around each expected delay (tight window with good timing)
    search_window_ms = 15.0
```

### Files Modified
- `src/grape_recorder/grape/wwvh_discrimination.py`

---

## 3. 440 Hz Station ID CSV Filtering

### Problem
The 440 Hz station ID CSV was recording all 60 minutes per hour, creating noise floor measurements that cluttered the chart with low-dB points.

### Fix
Only write records for minutes 1 (WWVH 440 Hz) and 2 (WWV 440 Hz):

```python
# phase2_analytics_service.py - Line ~519
def _write_station_id(self, minute_boundary: int, channel_char):
    minute_number = (minute_boundary // 60) % 60
    
    # Only write for 440 Hz minutes: 1 = WWVH, 2 = WWV
    if minute_number not in [1, 2]:
        return  # Skip - not a 440 Hz minute
```

### Files Modified
- `src/grape_recorder/grape/phase2_analytics_service.py`

---

## 4. Noise Floor Measurement Band Change

### Problem
SNR measurements for intermodulation were showing correlated variations between WWV and WWVH, suggesting a common noise floor issue. The noise band at 750-850 Hz was contaminated by BCD sidebands (700+100=800 Hz).

### Fix
Moved noise floor measurement to 275-325 Hz band:

```python
# audio_tone_monitor.py - Line ~154
# Calculate noise floor (275-325 Hz band - clear of tones and BCD sidebands)
# Previously 750-850 Hz but that has BCD harmonic contamination (700+100=800)
noise_mask = (freqs >= 275) & (freqs <= 325)
```

### Files Modified
- `src/grape_recorder/grape/audio_tone_monitor.py`

---

## 5. Web UI BCD Threshold Fix

### Problem
Web UI had a hardcoded threshold of 0.01 for BCD amplitudes, but actual amplitudes ranged 0.0001-0.01.

### Fix
Lowered threshold to 0.0005:

```javascript
// monitoring-server-v3.js - Line ~3990
const ampThreshold = 0.0005;  // Lowered from 0.01 - BCD amplitudes are small
```

### Files Modified
- `web-ui/monitoring-server-v3.js`

---

## 6. Carrier.html UI Simplification

### Problem
Metrics panel showed irrelevant fields for fusion-based timing:
- Time Basis (irrelevant with multi-broadcast fusion)
- Tone Age (irrelevant with fusion)
- Upload status was generic DRF, not PSWS

### Fix
Simplified to 4 relevant metrics:
- SNR (dB)
- Completeness
- Packet Loss
- PSWS Upload (placeholder for future tracking)

### Files Modified
- `web-ui/carrier.html`

---

## Files Changed Summary

| File | Changes |
|------|---------|
| `src/grape_recorder/grape/wwvh_discrimination.py` | BCD correlation fix, geo predictor integration |
| `src/grape_recorder/grape/wwv_bcd_encoder.py` | Added `envelope_only` parameter |
| `src/grape_recorder/grape/phase2_analytics_service.py` | 440 Hz minute filtering |
| `src/grape_recorder/grape/audio_tone_monitor.py` | Noise floor band 275-325 Hz |
| `web-ui/monitoring-server-v3.js` | BCD amplitude threshold |
| `web-ui/carrier.html` | Simplified metrics panel |

---

## Next Session: RTP to DRF Pipeline Investigation

The spectrograms show worrisome vertical striping/periodicity that should be investigated. Potential causes:
1. RTP packet timing irregularities
2. Buffer boundary effects in packet resequencer
3. DRF write timing issues
4. Decimation artifacts
5. FFT windowing effects in spectrogram generation

Key files to investigate:
- `src/grape_recorder/core/rtp_receiver.py`
- `src/grape_recorder/core/packet_resequencer.py`
- `src/grape_recorder/core/digital_rf_writer.py`
- `scripts/generate_spectrograms_from_10hz.py`
