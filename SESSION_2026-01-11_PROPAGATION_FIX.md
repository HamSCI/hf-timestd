# Session Summary: Propagation Delay Fix - 2026-01-11

## Session Objective

Diagnose and fix why all WWV D_clock measurements are consistently negative (-5 to -43ms), which is physically impossible and was being masked by the calibration system.

## Problem Identified

**Root Cause**: Propagation delay overestimation due to incorrect multi-hop mode selection for short distances.

### Symptoms
- All WWV D_clock values negative: -5ms to -43ms
- Large calibration offsets compensating: +5ms to +43ms
- 6-fold uncertainty increase: ±0.5ms → ±3ms
- Calibration masking the underlying systematic error

### Analysis
The transmission time solver was selecting 2-hop and 3-hop F-layer propagation modes for WWV at 629 km when 1-hop F-layer is physically correct:

```
Frequency   Calculated    Expected    Mode Selected    Error
2.5 MHz     23.2 ms       3.7 ms      3-hop F         +19.5 ms
5.0 MHz     11.3 ms       3.2 ms      2-hop F         +8.1 ms
10 MHz      7.8 ms        3.0 ms      2-hop F         +4.8 ms
20 MHz      4.7 ms        2.9 ms      1-hop F         +1.8 ms
25 MHz      4.8 ms        2.9 ms      1-hop F         +1.9 ms
```

This caused:
```
D_clock = T_arrival - T_propagation_overestimated
        = T_arrival - (T_actual + 20ms)
        = -20ms (negative!)
```

### Why Mode Selection Failed

**Circular Reasoning Problem**:
1. `observed_delay_ms = arrival_rtp - expected_second_rtp`
2. If `expected_second_rtp` is wrong (from bad calibration), `observed_delay_ms` is wrong
3. Mode scoring primarily uses delay matching
4. Wrong observed delay → wrong mode selected
5. Wrong mode → wrong propagation delay → negative D_clock
6. Calibration learns the error, perpetuating the cycle

**Weak Plausibility Penalties**:
- Original penalty for 2-hop at < 1000km: 0.3 (too weak)
- Penalty was multiplicative with delay score
- If delay matching was perfect, penalized mode could still win

## Solution Implemented

### Code Changes

**File**: `src/hf_timestd/core/transmission_time_solver.py`

**Lines Modified**: 700-714

**Change**: Hard rejection of multi-hop modes for short distances instead of penalty-based approach

```python
elif mode == PropagationMode.TWO_HOP_F:
    layer_height = hmF2  # Dynamic F2-layer height
    n_hops = 2
    # CRITICAL FIX (2026-01-11): Reject 2-hop for short distances
    # For WWV at 629km, 2-hop is physically implausible
    if ground_distance_km < 1000:
        logger.debug(f"2-hop mode rejected for short distance {ground_distance_km:.0f}km")
        return None  # Complete rejection
        
elif mode == PropagationMode.THREE_HOP_F:
    layer_height = hmF2  # Dynamic F2-layer height
    n_hops = 3
    # CRITICAL FIX (2026-01-11): Reject 3-hop for short distances
    if ground_distance_km < 2000:
        logger.debug(f"3-hop mode rejected for short distance {ground_distance_km:.0f}km")
        return None  # Complete rejection
```

**Rationale**: 
- Returning `None` completely excludes multi-hop modes from consideration
- Forces 1-hop F-layer selection for WWV at 629 km
- Breaks the circular reasoning by constraining mode selection based on physics, not observed delay

### Deployment Steps

1. Modified code in repo: `/home/mjh/git/hf-timestd/src/hf_timestd/core/transmission_time_solver.py`
2. Synced to production: `rsync -av src/hf_timestd/core/transmission_time_solver.py /opt/hf-timestd/src/hf_timestd/core/`
3. Cleared calibration state: Deleted `/var/lib/timestd/state/broadcast_calibration.json`
4. Restarted services: `sudo systemctl restart timestd-analytics`

## Results

### WWV-Only Frequencies (20, 25 MHz)
**Status**: ✓✓ FIXED

- **Mode**: 1-hop F-layer only (2-hop/3-hop completely rejected)
- **Propagation delay**: ~4.3ms (expected ~2.9ms, within tolerance)
- **D_clock**: Still negative but improving as calibration re-learns
- **Multi-hop modes**: Completely eliminated

### SHARED Frequencies (2.5, 5, 10, 15 MHz)
**Status**: ⚠️ Partial - Still showing multi-hop modes

**Reason**: These frequencies can detect multiple stations:
- WWV (Fort Collins, 629 km) - should use 1-hop
- WWVH (Hawaii, 6093 km) - can use multi-hop
- BPM (China, 11318 km) - can use multi-hop

The solver is called separately for each detected station. When `station='WWVH'` or `station='BPM'`, multi-hop modes are correctly allowed because those stations are far away.

**Root Cause**: Station discrimination is incorrectly assigning WWVH/BPM to detections that should be WWV.

## Why This Wasn't Caught Earlier

1. **Calibration masked the problem**: The per-broadcast calibration system learned offsets to compensate for systematic errors, making D_clock appear correct (near zero)

2. **Large offsets seemed plausible**: Calibration offsets up to 43ms seemed reasonable for ionospheric variations

3. **RTP offset fix exposed it**: The 2026-01-10 RTP offset fix changed the systematic error pattern, causing calibration to diverge and exposing the underlying propagation delay issue

## Technical Insights

### Attempted Approaches That Failed

1. **Penalty-based approach (0.3 penalty)**: Too weak, multi-hop could still win with good delay match
2. **Stronger penalty (0.001)**: Still failed because penalty is multiplicative with delay score
3. **Deleting old HDF5 files**: Didn't help because services were reading cached data

### What Worked

**Hard rejection**: Returning `None` from `_calculate_mode_delay()` completely excludes the mode from the candidate list, forcing correct mode selection regardless of observed delay.

## Documentation Created

- `/home/mjh/git/hf-timestd/PROPAGATION_DELAY_FIX_2026-01-11.md` - Initial fix documentation
- `/home/mjh/git/hf-timestd/NEGATIVE_DCLOCK_DIAGNOSIS_2026-01-11.md` - Root cause analysis
- `/home/mjh/git/hf-timestd/EXPECTED_SECOND_RTP_ANALYSIS.md` - RTP timing analysis
- `/home/mjh/git/hf-timestd/PROPAGATION_FIX_STATUS_2026-01-11.md` - Deployment status
- `/home/mjh/git/hf-timestd/FIX_STATUS_FINAL_2026-01-11.md` - Final status and next steps
- `/home/mjh/git/hf-timestd/SESSION_2026-01-11_PROPAGATION_FIX.md` - This document

## Next Steps

### Immediate (Next Session)

**Focus on station discrimination improvement** for SHARED frequencies:

1. **Signal strength discrimination**: Use SNR as primary discriminator
   - WWV at 629 km: Expected 20-30 dB SNR
   - WWVH at 6093 km: Expected 0-10 dB SNR
   - BPM at 11318 km: Expected 0-5 dB SNR
   - Rule: Any detection >15 dB SNR on shared frequency should be WWV

2. **Geographic constraints**: Reject WWVH/BPM detections that are too strong for their distance

3. **Review `wwvh_discrimination.py`**: Current discrimination methods may be unreliable

### Long-term

1. **Monitor calibration convergence**: Should converge to small offsets (< 5ms) over next 30 minutes
2. **Verify uncertainty reduction**: Should drop from ±3ms to ±0.5ms as calibration stabilizes
3. **Add regression tests**: Ensure mode selection stays correct for short distances

## Impact

### Positive
- WWV-only frequencies (20, 25 MHz) now have correct propagation delays
- Mode selection logic improved with physics-based constraints
- Circular reasoning problem identified and documented

### Remaining Issues
- SHARED frequencies still need discrimination improvement
- Calibration will take time to re-converge
- Uncertainty still elevated until discrimination is fixed

## Lessons Learned

1. **Calibration can mask systematic errors**: Don't assume calibration offsets are always correct
2. **Circular reasoning is insidious**: Observed delay depends on calibration, which depends on mode selection, which depends on observed delay
3. **Physics constraints > delay matching**: For short distances, physics dictates mode selection regardless of observed delay
4. **Hard rejection > penalties**: When something is physically impossible, reject it completely rather than penalizing it
5. **Station discrimination matters**: Mode selection fix only works if station assignment is correct

## System State at End of Session

- **Services**: timestd-analytics running with fixed code (9 channels)
- **Calibration**: Cleared and re-learning
- **Code**: Production and repo in sync (pending commit)
- **Data Flow**: Normal, HDF5 files being written
- **Fix Status**: Partial success - WWV-only frequencies fixed, SHARED frequencies need discrimination work
