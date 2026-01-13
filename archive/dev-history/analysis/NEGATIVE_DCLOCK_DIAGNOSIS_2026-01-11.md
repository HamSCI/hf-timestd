# Negative D_clock Root Cause Analysis - 2026-01-11

## Problem Summary

All WWV D_clock measurements are consistently negative (-5 to -43ms), which is physically impossible since it implies `T_arrival < T_propagation`. The calibration system masks this by learning large positive offsets.

## Root Cause Identified

**Propagation delay overestimation due to incorrect mode selection.**

The transmission time solver was selecting multi-hop propagation modes (2-hop, 3-hop) for short distances (WWV at 629 km) where 1-hop F-layer is correct:

```
Frequency   Calculated    Expected    Mode Selected    Error
2.5 MHz     23.2 ms       3.7 ms      3-hop F         +19.5 ms
5.0 MHz     11.3 ms       3.2 ms      2-hop F         +8.1 ms
10 MHz      7.8 ms        3.0 ms      2-hop F         +4.8 ms
20 MHz      4.7 ms        2.9 ms      1-hop F         +1.8 ms
25 MHz      4.8 ms        2.9 ms      1-hop F         +1.9 ms
```

This causes:
```
D_clock = T_arrival - T_propagation_overestimated
        = T_arrival - (T_actual + 20ms)
        = -20ms (negative!)
```

## Why Mode Selection Failed

### The Circular Reasoning Problem

1. `observed_delay_ms = arrival_rtp - expected_second_rtp`
2. If `expected_second_rtp` is wrong (from bad calibration), `observed_delay_ms` is wrong
3. Mode scoring primarily uses delay matching: `delay_error = |candidate_delay - observed_delay|`
4. If observed delay is 23ms (wrong), and:
   - 1-hop gives 3ms → error = 20ms → low score
   - 3-hop gives 23ms → error = 0ms → high score
5. Wrong mode selected → wrong propagation delay → negative D_clock
6. Calibration learns the error, perpetuating the cycle

### Why Plausibility Penalties Failed

The code had distance-based plausibility penalties:
```python
if n_hops >= 2 and ground_distance_km < 1000:
    plausibility *= 0.3  # Too weak!
```

For WWV at 629 km, 2-hop mode gets 0.3 penalty, but if delay matching gives it a high score, it still wins.

## Attempted Fix #1: Stronger Penalties

Modified penalties to:
```python
if n_hops >= 2 and ground_distance_km < 1000:
    plausibility *= 0.001  # 1000x stronger
```

**Result**: Still not working. The 2.5 MHz channel continues to select 2-hop mode.

## Why the Fix Isn't Working

The issue is that **plausibility is multiplicative with the delay score**:

```python
score = delay_score × plausibility × other_factors
```

If delay_score is very high (1.0 for perfect match), even with plausibility = 0.001:
```
2-hop score = 1.0 × 0.001 = 0.001
1-hop score = 0.1 × 1.0 = 0.1  (delay error gives low score)
```

The 1-hop mode still wins in this case, but if the observed delay is very wrong, the 2-hop can still win.

## The Real Problem: Bootstrap Dependency

During bootstrap (no calibration), the code at `phase2_temporal_engine.py:2167-2178` calculates:

```python
estimated_arrival_time = system_time + (arrival_rtp - rtp_timestamp) / sample_rate
nearest_minute = round(estimated_arrival_time / 60) * 60
time_diff = nearest_minute - system_time
expected_second_rtp = rtp_timestamp + int(time_diff * sample_rate)
```

This uses **system time** to establish the minute boundary. If system time is wrong by even a few ms, `expected_second_rtp` is wrong, making `observed_delay_ms` wrong, causing incorrect mode selection.

## Alternative Approach Needed

Instead of relying on observed delay matching during bootstrap, we should:

1. **Use station-specific expected delays** as primary constraint
2. **Force 1-hop mode for short distances** regardless of observed delay
3. **Only use delay matching as a tie-breaker** when multiple plausible modes exist

### Proposed Fix

Modify mode selection to:
1. Filter modes by distance constraints FIRST (reject multi-hop for < 1500km)
2. Then score remaining modes by delay match
3. Or: Use minimum expected delay during bootstrap instead of observed delay

## Current Status

- Services running with 0.001 penalty
- 2.5 MHz still selecting 2-hop mode
- Need to implement stronger distance-based rejection or change scoring approach

## Next Steps

1. Check if mode candidates are even being generated for multi-hop
2. Review scoring formula to understand why penalty isn't effective
3. Consider rejecting multi-hop modes entirely (return None) instead of just penalizing
4. Or: Change bootstrap to use expected delays instead of observed delays
