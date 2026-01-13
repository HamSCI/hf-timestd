# Propagation Delay Overestimation Fix - 2026-01-11

## Root Cause Identified

The 6-fold uncertainty increase and negative D_clock values were caused by **propagation delay overestimation** in the mode selection algorithm.

### The Problem

For WWV at 629 km, the system was calculating:
```
2.5 MHz: 23.2 ms propagation delay (should be ~3.7 ms) → 3-hop mode selected
5.0 MHz: 11.3 ms propagation delay (should be ~3.2 ms) → 2-hop mode selected
10 MHz:   7.8 ms propagation delay (should be ~3.0 ms) → 2-hop mode selected
20 MHz:   4.7 ms propagation delay (should be ~2.9 ms) → 1-hop mode (correct)
25 MHz:   4.8 ms propagation delay (should be ~2.9 ms) → 1-hop mode (correct)
```

### Why This Caused Negative D_clock

```
D_clock = T_arrival - T_propagation

If T_propagation is overestimated by 20ms:
  D_clock = T_arrival - (T_actual + 20ms)
  D_clock = -20ms (negative!)
```

The calibration system was masking this by learning large offsets:
```
WWV 2.5 MHz: D_clock = -43ms → calibration learns +43ms offset
WWV 5.0 MHz: D_clock = -7.5ms → calibration learns +7.5ms offset
```

### The Circular Reasoning Problem

1. `observed_delay_ms = arrival_rtp - expected_second_rtp`
2. Mode selection picks mode that matches observed delay
3. If expected_second_rtp is wrong (from bad calibration), observed delay is wrong
4. Wrong observed delay → wrong mode selected
5. Wrong mode → wrong propagation delay
6. Wrong propagation delay → negative D_clock
7. Calibration learns the error, perpetuating the cycle

## The Fix

Modified `transmission_time_solver.py:733-744` to **strongly penalize multi-hop modes at short distances**:

```python
# CRITICAL FIX (2026-01-11): Multi-hop modes at short distances are implausible
# Previous penalty (0.3) was too weak, allowing wrong mode selection
# For distances < 1500km, 1-hop F-layer is almost always correct
if n_hops >= 2 and ground_distance_km < 1500:
    # Very strong penalty - essentially reject multi-hop for short distances
    penalty = 0.01 if ground_distance_km < 1000 else 0.05
    logger.debug(f"{n_hops}-hop mode strongly penalized for short distance {ground_distance_km:.0f}km (penalty={penalty})")
    plausibility *= penalty

# 3+ hops are rare and only for very long distances
if n_hops >= 3 and ground_distance_km < 5000:
    plausibility *= 0.1  # Stronger penalty (was 0.5)
```

### Changes:
- **2-hop at < 1000km**: penalty 0.3 → 0.01 (100x stronger)
- **2-hop at 1000-1500km**: penalty 0.3 → 0.05 (6x stronger)
- **3-hop at < 5000km**: penalty 0.5 → 0.1 (5x stronger)

For WWV at 629 km, this essentially forces 1-hop F-layer selection.

## Expected Results

After services restart with fixed code:

```
WWV 2.5 MHz: 3.7 ms propagation (was 23.2 ms) → D_clock near 0 (was -43ms)
WWV 5.0 MHz: 3.2 ms propagation (was 11.3 ms) → D_clock near 0 (was -7.5ms)
WWV 10 MHz:  3.0 ms propagation (was 7.8 ms)  → D_clock near 0 (was -23ms)
```

Calibration offsets should converge to near-zero, and uncertainty should drop from ±3ms back to ±0.5ms.

## Why This Wasn't Caught Earlier

The calibration system was working as designed - it learned offsets to compensate for systematic errors. The problem was hidden because:

1. Calibration made D_clock appear correct (near zero)
2. Large calibration offsets (up to 43ms) seemed plausible for ionospheric variations
3. The RTP offset fix exposed the issue by changing the systematic error pattern

The real issue was that the propagation model was fundamentally wrong, selecting multi-hop modes for short distances due to weak plausibility penalties.

## Next Steps

1. Restart analytics services to load fixed code
2. Clear calibration to start fresh
3. Monitor first measurements to verify:
   - Propagation delays are ~3ms for WWV
   - D_clock values are near zero
   - Calibration offsets stay small
4. Verify uncertainty drops back to ±0.5ms

## Technical Details

### Why Mode Selection Was Wrong

The mode scoring algorithm (line 946-957) primarily uses delay matching:
```python
delay_error_ms = abs(candidate.total_delay_ms - observed_delay_ms)
if delay_error_ms < 0.5:
    delay_score = 1.0  # Excellent match
```

If observed_delay_ms is 23ms (wrong due to bad expected_second_rtp), and:
- 1-hop gives 3ms delay → error = 20ms → score = 0.1
- 3-hop gives 23ms delay → error = 0ms → score = 1.0

The 3-hop mode wins despite being physically implausible for 629km.

The fix adds strong distance-based constraints that override delay matching for short distances.
