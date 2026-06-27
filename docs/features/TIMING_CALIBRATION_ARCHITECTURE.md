# GPSDO-Calibrated Timing Architecture

## Overview

This document describes the three-phase timing calibration system that leverages
GPSDO-locked RTP timestamps for high-accuracy time marker detection.

## Key Insight: RTP Timing is Deterministic

With a GPSDO-disciplined SDR (ka9q-radio), RTP timestamps are perfectly stable:

```
Minute 1765645260: Δ=1200000 samples (expected 1200000), drift=+0.000ms
Minute 1765645320: Δ=1200000 samples (expected 1200000), drift=+0.000ms
Minute 1765645380: Δ=1200000 samples (expected 1200000), drift=+0.000ms
```

**Zero drift** between minutes. This means:
1. Once we establish the RTP-to-UTC offset, we know exactly where every sample falls
2. The D_clock variance we observe is NOT timing jitter - it's detection/discrimination error
3. Search windows can be dramatically narrowed from 500ms to ~5ms

## Three-Phase Calibration

### Phase 1: BOOTSTRAP (First ~3-5 Minutes)

**Goal**: Establish RTP-to-UTC calibration from high-quality detections

**Parameters**:
- Search window: ±500ms (wide)
- SNR threshold: >15 dB
- Confidence threshold: >0.7
- Minimum detections: 5
- Minimum stations: 2

**Process**:
1. Use wide search window to find tones on any frequency
2. High-quality detections (SNR > 15dB, confidence > 0.7) contribute to calibration
3. Build per-station propagation delay estimates
4. Build per-channel RTP offset calibration
5. Exit bootstrap when sufficient data collected

### Phase 2: CALIBRATED (Normal Operation)

**Goal**: Use narrow search windows for improved sensitivity and discrimination

**Parameters**:
- Search window: ±5ms (narrow, based on propagation delay uncertainty)
- Intra-station consistency threshold: 5ms
- Inter-station spread: expected ~20ms (WWV-WWVH geographic difference)

**Process**:
1. Calculate expected tone position from RTP calibration + propagation delay
2. Search only within narrow window around expected position
3. Check intra-station consistency (same station, different frequencies)
4. Flag discrimination suspects when consistency fails
5. Continuously refine propagation delay estimates

### Phase 3: VERIFIED (Optional, Sub-ms Accuracy)

**Goal**: Use secondary signals for verification and sub-ms accuracy

**Verification Signals**:

#### WWV/WWVH BCD (100 Hz subcarrier)
- Binary-coded decimal time code
- 100 Hz AM subcarrier
- Provides minute/hour/day verification
- Alignment confirms tone detection accuracy

#### CHU FSK (300 baud)
- Frequency-shift keying time code
- Mark/space at ±85 Hz from carrier
- Provides exact 500ms boundary alignment
- FSK frame boundaries are precise time markers

#### WWV/WWVH Test Signals
- 440 Hz (WWV) and 600 Hz (WWVH) test tones
- Broadcast during specific seconds
- Can verify station discrimination

## Consistency Checks

### Intra-Station Consistency

Same-station broadcasts should agree within ~1-3ms (ionospheric variation only):

```
WWV 5 MHz:  D_clock = -5.2ms
WWV 10 MHz: D_clock = -5.4ms
WWV 15 MHz: D_clock = -5.1ms
WWV 20 MHz: D_clock = -5.3ms
WWV 25 MHz: D_clock = -5.5ms
                     --------
Intra-station σ = 0.15ms ✓ (good)
```

If intra-station σ > 5ms, suspect discrimination error:

```
"WWV" 5 MHz:  D_clock = -5.2ms  ← Actually WWV
"WWV" 10 MHz: D_clock = -25.4ms ← Actually WWVH (misclassified!)
"WWV" 15 MHz: D_clock = -5.1ms  ← Actually WWV
                       --------
Intra-station σ = 11.7ms ⚠️ DISCRIMINATION_SUSPECT
```

### Inter-Station Spread

Different stations have geographic propagation differences:

```
WWV mean:  -5.3ms (Fort Collins, CO)
WWVH mean: -25.1ms (Hawaii)
CHU mean:  -3.8ms (Ottawa, Canada)
           --------
Inter-station spread: 21.3ms (expected for this receiver location)
```

## Implementation Files

- `src/hf_timestd/core/timing_calibrator.py` - Main calibrator class
- `src/hf_timestd/core/multi_broadcast_fusion.py` - Consistency checks
- `pipeline_orchestrator.py` - (archived; no longer present in the tree)

## State Persistence

Calibration state is saved to `{data_root}/state/timing_calibration.json`:

```json
{
  "phase": "calibrated",
  "station_calibration": {
    "WWV": {
      "propagation_delay_ms": 6.42,
      "propagation_delay_std_ms": 1.23,
      "n_samples": 47,
      "frequencies_contributing": [5.0, 10.0, 15.0, 20.0, 25.0]
    },
    "WWVH": {
      "propagation_delay_ms": 25.18,
      "propagation_delay_std_ms": 2.15,
      "n_samples": 23,
      "frequencies_contributing": [2.5, 5.0, 10.0, 15.0]
    }
  },
  "rtp_calibration": {
    "WWV 10 MHz": {
      "rtp_offset_samples": 411380,
      "calibration_snr_db": 28.5,
      "n_confirmations": 156
    }
  }
}
```

## Expected Tone Position Calculation

With calibration, we can predict exactly where a tone should appear:

```python
def expected_tone_sample(second_number, propagation_delay_ms, buffer_start_rtp):
    samples_per_second = 20000
    samples_per_minute = 1200000
    
    # Tone position in minute (from minute boundary)
    tone_in_minute = (second_number * samples_per_second + 
                      int(propagation_delay_ms * 20))
    
    # Buffer start position in minute
    buffer_offset = buffer_start_rtp % samples_per_minute
    
    # Tone position relative to buffer start
    return tone_in_minute - buffer_offset
```

## Benefits

1. **Improved Sensitivity**: Narrow search window reduces false positives
2. **Better Discrimination**: Cross-channel consistency catches misclassifications
3. **Faster Convergence**: Kalman filter converges faster with less noise
4. **Self-Healing**: Consistency checks can trigger re-discrimination
5. **Verification**: Secondary signals (BCD, FSK) provide independent confirmation

## Future Enhancements

1. **Adaptive Window Sizing**: Widen window during ionospheric disturbances
2. **Cross-Channel Voting**: Use majority vote for discrimination on shared frequencies
3. **BCD Decoder**: Verify minute/hour from 100 Hz subcarrier
4. **FSK Boundary Alignment**: Use CHU FSK for sub-ms timing verification
5. **Test Signal Detection**: Use 440/600 Hz tones for station verification
