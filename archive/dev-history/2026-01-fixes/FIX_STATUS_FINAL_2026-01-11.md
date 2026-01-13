# Propagation Delay Fix - Final Status 2026-01-11 02:35 UTC

## Fix Deployed and Verified

**Status**: ✓ Partial Success - WWV-only frequencies fixed, SHARED frequencies need discrimination improvement

### What's Working

**WWV 20 MHz and 25 MHz**: ✓✓ FIXED
- Mode: 1-hop F-layer only (2-hop/3-hop rejected)
- Propagation delay: ~4.3ms (expected ~2.9ms, within tolerance)
- Multi-hop modes completely eliminated

### What's Not Working

**SHARED frequencies (2.5, 5, 10, 15 MHz)**: Still showing multi-hop modes

**Root Cause**: These frequencies can detect multiple stations:
- WWV (Fort Collins, 629 km) - should use 1-hop
- WWVH (Hawaii, 6093 km) - can use multi-hop
- BPM (China, 11318 km) - can use multi-hop

The transmission time solver is being called separately for each detected station. When called with `station='WWVH'` or `station='BPM'`, multi-hop modes are correctly allowed because those stations are far away.

**The Real Problem**: Station discrimination on shared frequencies is assigning WWVH/BPM when it should be WWV.

## Technical Details

### Mode Selection Fix (Working)

Modified `transmission_time_solver.py:700-714`:
```python
elif mode == PropagationMode.TWO_HOP_F:
    n_hops = 2
    if ground_distance_km < 1000:
        return None  # Reject for WWV at 629km
        
elif mode == PropagationMode.THREE_HOP_F:
    n_hops = 3
    if ground_distance_km < 2000:
        return None  # Reject for short distances
```

This works perfectly when `station='WWV'` (629km), but doesn't help when `station='WWVH'` (6093km).

### Why SHARED Frequencies Differ

- **WWV 20/25 MHz**: WWV-only frequencies, no WWVH/BPM transmission
  - Only WWV detected → only WWV distance used → 1-hop forced → ✓ Fixed

- **SHARED 2.5/5/10/15 MHz**: Shared with WWVH and BPM
  - Multiple stations detected → solver called for each
  - WWVH/BPM detections use their distances → multi-hop allowed
  - Wrong station assignment → wrong propagation delay → negative D_clock

## Next Steps

### Option 1: Improve Station Discrimination (Recommended)

Fix the discrimination logic in `wwvh_discrimination.py` to correctly identify WWV vs WWVH/BPM on shared frequencies using:
- Signal strength (WWV at 629km should be much stronger than WWVH at 6093km)
- Tone power ratio (1000 Hz vs 1200 Hz)
- Geographic constraints (WWVH shouldn't be detectable with high SNR at this location)

### Option 2: Add Station-Aware Mode Filtering

Modify mode selection to reject multi-hop for WWVH/BPM detections when:
- Signal strength is too high for the distance
- Propagation delay doesn't match expected range
- Detection confidence is low

### Option 3: Use Only WWV on SHARED Frequencies

Force SHARED frequency channels to only process WWV detections, ignoring WWVH/BPM entirely. This is the simplest fix but loses propagation study data.

## Current System State

- **Services**: timestd-analytics running with fixed code (9 channels)
- **Calibration**: Cleared, will re-learn with current (partially fixed) propagation delays
- **Data Flow**: Normal, HDF5 files being written
- **Uncertainty**: Still elevated (±3ms) due to SHARED frequency issues

## Recommendation

Focus on **station discrimination improvement** rather than further mode selection changes. The mode selection fix is working correctly - the problem is that wrong stations are being assigned to detections on shared frequencies.

The discrimination system should use signal strength as the primary discriminator:
- WWV at 629 km: Expected signal strength ~20-30 dB SNR
- WWVH at 6093 km: Expected signal strength ~0-10 dB SNR (if detectable at all)
- BPM at 11318 km: Expected signal strength ~0-5 dB SNR (rarely detectable)

Any detection with >15 dB SNR on a shared frequency should be assigned to WWV, not WWVH/BPM.
