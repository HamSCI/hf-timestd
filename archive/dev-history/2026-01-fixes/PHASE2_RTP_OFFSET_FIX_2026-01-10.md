# Phase 2 RTP Offset Fix - 2026-01-10

## Problem Summary

Phase 2 D_clock measurements showed a persistent 20-25ms cross-station disagreement between CHU and WWV, preventing calibration convergence and causing Grade D quality with high uncertainty.

**Root Cause**: CHU and WWV channels were using **different RTP timestamp offsets** to calculate `expected_second_rtp`, creating an artificial 7.5ms systematic offset in D_clock calculations.

## Investigation

### Symptom
```
CHU 7.8 MHz: expected_second_rtp=550580820, D_clock=-28.17ms
WWV 25 MHz:  expected_second_rtp=550580640, D_clock=-26.83ms
Difference: 180 samples = 7.5ms @ 24kHz
```

### Analysis
All channels share the same GPSDO-disciplined RTP stream, so they **must** use the same RTP-to-UTC mapping. The 180-sample difference was an artifact of per-channel RTP calibration, not real propagation delay.

## Bugs Fixed

### 1. Anchor Channel Naming Mismatch
**File**: `src/hf_timestd/core/timing_calibrator.py`

**Problem**: Anchor channels were defined as `'CHU 7.85 MHz'` but actual channel names are `'CHU_7850'`. This prevented the timing calibrator from recognizing anchor channels, so `global_rtp_offset` was never established.

**Fix**: Updated `ANCHOR_CHANNELS` to use actual naming convention:
```python
ANCHOR_CHANNELS = {
    'CHU_3330',    # CHU-only frequency (3.33 MHz)
    'CHU_7850',    # CHU-only frequency (7.85 MHz)
    'CHU_14670',   # CHU-only frequency (14.67 MHz)
    'WWV_20000',   # WWV-only frequency (20 MHz)
    'WWV_25000',   # WWV-only frequency (25 MHz)
}
```

### 2. Race Condition in Global Offset Persistence
**File**: `src/hf_timestd/core/timing_calibrator.py`

**Problem**: Multiple Phase 2 processes share `timing_calibration.json`. When Process A sets `global_rtp_offset` and Process B calls `_load_state()`, it overwrites the in-memory value with stale disk state (`None`).

**Fix**: Preserve in-memory `global_rtp_offset` across `_load_state()` if already set:
```python
# Save current global offset before reload
saved_global_offset = self.global_rtp_offset
saved_global_source = self.global_rtp_offset_source
saved_global_confidence = self.global_rtp_offset_confidence

self._load_state()

# Restore if it was set in memory but not yet on disk
if saved_global_offset is not None and self.global_rtp_offset is None:
    self.global_rtp_offset = saved_global_offset
    self.global_rtp_offset_source = saved_global_source
    self.global_rtp_offset_confidence = saved_global_confidence
```

### 3. Global Offset Not Saved to Disk
**File**: `src/hf_timestd/core/timing_calibrator.py`

**Problem**: The save condition `if self.phase == CalibrationPhase.BOOTSTRAP or total_detections % 5 == 0` failed because `_load_state()` loaded `phase=calibrated` from disk, changing the in-memory phase and preventing saves.

**Fix**: Explicitly save state immediately after establishing global offset:
```python
if self.global_rtp_offset is None:
    self.global_rtp_offset = rtp_offset
    self.global_rtp_offset_source = channel_name
    self.global_rtp_offset_confidence = min(1.0, snr_db / 30.0)
    logger.info(f"🎯 Global RTP offset established from anchor {channel_name}: {rtp_offset} samples")
    
    # CRITICAL: Save state immediately
    self._save_state()
    logger.info(f"Saved global RTP offset to state file")
```

### 4. Per-Channel Offset Priority
**File**: `src/hf_timestd/core/phase2_analytics_service.py`

**Problem**: The RTP offset callback prioritized per-channel offsets over global offset, so even when global was set, channels used their own offsets.

**Fix**: Always use `global_rtp_offset` if available:
```python
def get_rtp_offset(channel_name: str) -> Optional[int]:
    # CRITICAL: Always use global RTP offset for all channels
    if self.timing_calibrator.global_rtp_offset is not None:
        return self.timing_calibrator.global_rtp_offset
    
    # Fallback during bootstrap
    if channel_name in self.timing_calibrator.rtp_calibration:
        return self.timing_calibrator.rtp_calibration[channel_name].rtp_offset_samples
    
    return None
```

## Results

### Before Fix
```
CHU 7.8: expected_second_rtp=724820820, D_clock=-45.67ms
WWV 25:  expected_second_rtp=724820640, D_clock=-26.46ms
Difference: 180 samples = 7.5ms
Cross-station disagreement: 20-25ms
```

### After Fix
```
CHU 7.8: expected_second_rtp=733460340, D_clock=+0.21ms
WWV 25:  expected_second_rtp=734900340, D_clock=-0.50ms
Difference: 0 samples (both use global offset 500340)
Remaining difference: ~0.7ms (real propagation delay difference)
```

## Impact

1. **Eliminated 7.5ms systematic offset** between CHU and WWV
2. **D_clock values now near zero** (+0.21ms, -0.50ms) as expected
3. **Cross-station disagreement reduced** from 20-25ms to <1ms (real propagation difference)
4. **All channels now share same RTP-to-UTC mapping** via `global_rtp_offset`
5. **Fusion quality expected to improve** to Grade A/B as new measurements arrive

## Commits

1. `025dabf` - Fix Phase 2 cross-station D_clock offset by using global RTP offset
2. `39236ed` - Fix anchor channel naming to match actual channel names
3. `343b9bf` - Add debug logging for RTP calibration anchor channel recognition
4. `3fa32e9` - Fix race condition in global RTP offset persistence
5. `4af979c` - Force immediate save when global RTP offset is established

## Monitoring

The fusion service will take 10-15 minutes to converge as old measurements age out and new corrected measurements arrive. Expected final state:
- Cross-station disagreement: <5ms (real propagation + ionospheric variation)
- D_clock uncertainty: <2ms
- Quality grade: A or B
- Chrony feed: Active and disciplining system clock
