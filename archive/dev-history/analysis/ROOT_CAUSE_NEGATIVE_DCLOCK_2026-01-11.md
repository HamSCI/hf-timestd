# Root Cause Analysis: Negative D_clock Values - 2026-01-11

## The Real Problem

All WWV frequencies show **negative D_clock values**:

```
WWV Frequencies:
   2.50 MHz: D_clock = -43.146 ms
   5.00 MHz: D_clock =  -7.532 ms
  10.00 MHz: D_clock = -22.964 ms
  15.00 MHz: D_clock =  -5.855 ms
  20.00 MHz: D_clock = -17.565 ms
  25.00 MHz: D_clock =  -6.399 ms
```

## What This Means

The fundamental equation is:

```
D_clock = T_arrival - T_propagation
```

If D_clock is negative, then:

```
T_arrival < T_propagation
```

**This is physically impossible.**

The tone arrives at some time T_arrival. The propagation delay T_propagation is the time it took to get there (typically 2-40ms). The arrival time must be LATER than the emission time by the propagation delay.

## Possible Causes

### 1. RTP Offset Fix Introduced Systematic Error

From `PHASE2_RTP_OFFSET_FIX_2026-01-10.md`, the RTP offset fix applied a **global offset of ~500340 samples**.

If this offset was applied in the wrong direction:
```
T_arrival_corrected = T_arrival_raw - RTP_offset
```

Should be:
```
T_arrival_corrected = T_arrival_raw + RTP_offset
```

**Sign error in RTP offset application would cause all D_clock values to be systematically wrong.**

### 2. T_arrival Reference is Wrong

The tone detector measures arrival time relative to the minute boundary. If the minute boundary reference is wrong by ~20-40ms, all measurements would be systematically offset.

### 3. T_propagation is Being Added Instead of Subtracted

If there's a sign error in the D_clock calculation:

```python
# WRONG:
d_clock = t_arrival + t_propagation  # Should be minus!

# CORRECT:
d_clock = t_arrival - t_propagation
```

### 4. Propagation Delay is Calculated Relative to Wrong Reference

The propagation delay should be:
```
T_propagation = time_for_signal_to_travel_from_transmitter_to_receiver
```

If it's being calculated as:
```
T_propagation = expected_arrival_time - actual_arrival_time
```

Then the sign would be inverted.

## Why Calibration "Works"

The calibration system learns:
```
calibration_offset = -mean(D_clock)
```

For WWV 2.5 MHz:
```
calibration_offset = -(-43.146) = +43.146 ms
```

Then applies:
```
D_clock_calibrated = D_clock_raw + calibration_offset
D_clock_calibrated = -43.146 + 43.146 = 0 ms
```

**The calibration is masking the underlying systematic error!**

This is why:
1. Calibration offsets are so large (up to 43ms)
2. Different frequencies have different offsets (because the underlying error varies with frequency)
3. The system "works" after calibration converges

But the root cause is still there - the raw D_clock values are wrong.

## The 6-Fold Uncertainty Increase

Before the RTP offset fix:
- Raw D_clock values were wrong
- But they were consistently wrong
- Calibration learned the systematic error
- Uncertainty was ±0.5ms

After the RTP offset fix:
- The RTP offset correction changed the systematic error
- Calibration is re-learning the new error
- During convergence, scatter is high
- Different frequencies converge at different rates
- Uncertainty increased to ±3ms

## What to Check

1. **RTP offset application direction** in `phase2_analytics_service.py`
2. **D_clock calculation** in `transmission_time_solver.py`
3. **T_arrival calculation** in tone detector
4. **T_propagation calculation** in propagation model

## Expected Values

For WWV at 629 km:
```
T_propagation ≈ 2-3 ms (1-hop F-layer)
T_arrival ≈ 2-3 ms after minute boundary
D_clock = T_arrival - T_propagation ≈ 0 ms (if GPSDO is accurate)
```

If D_clock is -43ms, something is very wrong with either T_arrival or T_propagation.

## Next Steps

1. Read the RTP offset application code
2. Verify the sign of the offset correction
3. Check if there's a sign error in D_clock calculation
4. Trace through one example measurement to see where the error occurs
