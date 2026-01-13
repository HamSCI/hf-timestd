# Propagation Delay Fix - Status Report 2026-01-11 02:30 UTC

## Problem Identified

**Root Cause**: Propagation delay overestimation due to multi-hop mode selection for short distances (WWV at 629 km).

The transmission time solver was selecting 2-hop and 3-hop F-layer modes when 1-hop is correct, causing:
- Propagation delays of 7-27ms instead of 3ms
- D_clock values of -7 to -43ms (negative, physically impossible)
- Calibration learning large offsets to compensate (+43ms for 2.5 MHz)
- 6-fold uncertainty increase (±0.5ms → ±3ms)

## Fix Implemented

Modified `src/hf_timestd/core/transmission_time_solver.py` to **hard reject** multi-hop modes for short distances:

```python
elif mode == PropagationMode.TWO_HOP_F:
    n_hops = 2
    if ground_distance_km < 1000:  # WWV at 629km
        logger.debug(f"2-hop mode rejected for short distance {ground_distance_km:.0f}km")
        return None  # Complete rejection, not just penalty
        
elif mode == PropagationMode.THREE_HOP_F:
    n_hops = 3
    if ground_distance_km < 2000:
        logger.debug(f"3-hop mode rejected for short distance {ground_distance_km:.0f}km")
        return None
```

This ensures that for WWV at 629 km, only 1-hop F-layer mode is available.

## Code Deployed

- ✓ Fix applied to repo: `/home/mjh/git/hf-timestd/src/hf_timestd/core/transmission_time_solver.py`
- ✓ Synced to production: `/opt/hf-timestd/src/hf_timestd/core/transmission_time_solver.py`
- ✓ Services restarted with fixed code
- ✓ Calibration state cleared to force fresh bootstrap

## Current Status

**Services Running**: 9 phase2_analytics_service processes active

**Data Flow Issue**: No new HDF5 files being created because:
- No raw data files in `/dev/shm/timestd/raw_buffer/` from last 5 minutes
- Upstream recorder (radiod) may not be running or writing data
- Analytics services are waiting for data to process

**Verification Pending**: Cannot verify fix until data flows again

## Expected Results (Once Data Flows)

For all WWV frequencies (2.5, 5, 10, 15, 20, 25 MHz):
- **Mode**: 1-hop F-layer only (2-hop/3-hop rejected)
- **Propagation delay**: 2.9-3.7 ms (was 7-27 ms)
- **D_clock**: Near 0 ms (was -7 to -43 ms)
- **Calibration offsets**: Small, < 5ms (was up to 43ms)
- **Uncertainty**: ±0.5ms (was ±3ms)

## Next Steps

1. **Check if radiod is running**: `ps aux | grep radiod`
2. **Check if raw data is being written**: `ls -lh /dev/shm/timestd/raw_buffer/SHARED_2500/*.bin | tail`
3. **Once data flows**, verify fix with:
   ```bash
   python3 /home/mjh/git/hf-timestd/scripts/verify_propagation_fix.py
   ```
4. **Monitor calibration convergence** over next 30 minutes
5. **Verify uncertainty drops** back to ±0.5ms

## Technical Details

### Why Penalty-Based Approach Failed

Initial attempts used plausibility penalties:
```python
if n_hops >= 2 and ground_distance_km < 1000:
    plausibility *= 0.001  # Very strong penalty
```

This failed because the penalty is multiplicative with delay score. If observed delay perfectly matches the wrong mode (due to bad calibration), that mode can still win:
```
2-hop score = 1.0 (perfect delay match) × 0.001 (penalty) = 0.001
1-hop score = 0.1 (poor delay match) × 1.0 (no penalty) = 0.1
```

### Why Hard Rejection Works

By returning `None` for multi-hop modes at short distances, those modes are completely excluded from consideration. Only 1-hop F-layer remains as a candidate, forcing correct mode selection regardless of observed delay.

This breaks the circular reasoning:
1. Old: Wrong calibration → wrong observed delay → wrong mode selected → wrong propagation → negative D_clock
2. New: Multi-hop rejected → only 1-hop available → correct propagation → D_clock near zero → calibration converges correctly

## Files Modified

- `src/hf_timestd/core/transmission_time_solver.py` (lines 700-714)

## Documentation Created

- `/home/mjh/git/hf-timestd/PROPAGATION_DELAY_FIX_2026-01-11.md`
- `/home/mjh/git/hf-timestd/NEGATIVE_DCLOCK_DIAGNOSIS_2026-01-11.md`
- `/home/mjh/git/hf-timestd/EXPECTED_SECOND_RTP_ANALYSIS.md`
- `/home/mjh/git/hf-timestd/PROPAGATION_FIX_STATUS_2026-01-11.md` (this file)
