# Expected Second RTP Analysis - Root Cause of Negative D_clock

## The Calculation

From `phase2_temporal_engine.py:2151-2163`:

```python
calibrated_offset = self.rtp_calibration_callback(self.channel_name)  # e.g., 500340
current_offset = rtp_timestamp % samples_per_minute  # e.g., 123456
offset_diff = calibrated_offset - current_offset     # e.g., 500340 - 123456 = 376884
expected_second_rtp = rtp_timestamp + offset_diff    # e.g., rtp_timestamp + 376884
```

## What This Means

`calibrated_offset` is the RTP offset (modulo 1 minute) that corresponds to a minute boundary.

Example:
- If minute boundaries occur when `rtp_timestamp % 1200000 == 500340`
- Then `calibrated_offset = 500340`

For any given `rtp_timestamp`:
- `current_offset = rtp_timestamp % 1200000`
- `offset_diff = 500340 - current_offset`
- `expected_second_rtp = rtp_timestamp + offset_diff`

This gives the RTP timestamp of the **next** minute boundary.

## Example Calculation

```
samples_per_minute = 1,200,000
calibrated_offset = 500340  (minute boundaries at ...500340, ...1700340, ...2900340, etc.)

At some time:
  rtp_timestamp = 1234567890
  current_offset = 1234567890 % 1200000 = 367890
  offset_diff = 500340 - 367890 = 132450
  expected_second_rtp = 1234567890 + 132450 = 1235000340
```

This is correct - it gives the RTP timestamp of the next minute boundary.

## The D_clock Calculation

From `transmission_time_solver.py:1144-1155`:

```python
emission_rtp = arrival_rtp - propagation_samples
emission_offset_samples = emission_rtp - expected_second_rtp
emission_offset_ms = (emission_offset_samples / sample_rate) * 1000
```

This calculates:
```
D_clock = (arrival_rtp - propagation_samples - expected_second_rtp) / sample_rate * 1000
```

## Why D_clock is Negative

If `arrival_rtp` is the tone arrival time and `expected_second_rtp` is the minute boundary:

```
arrival_rtp ≈ expected_second_rtp + propagation_delay_samples
```

So:
```
emission_rtp = arrival_rtp - propagation_samples
             ≈ (expected_second_rtp + propagation_samples) - propagation_samples
             ≈ expected_second_rtp
```

Therefore:
```
D_clock = (emission_rtp - expected_second_rtp) / sample_rate * 1000
        ≈ 0 ms
```

**This should give D_clock ≈ 0 ms if the system clock is accurate!**

## But We're Seeing Negative Values

If D_clock is consistently negative (e.g., -20ms), then:

```
emission_rtp < expected_second_rtp
arrival_rtp - propagation_samples < expected_second_rtp
arrival_rtp < expected_second_rtp + propagation_samples
```

This means the tone is arriving **before** the expected minute boundary (accounting for propagation).

## Possible Causes

### 1. Propagation Delay Overestimated

If the propagation model calculates 20ms but actual is 2ms:
```
emission_rtp = arrival_rtp - 400 samples  (20ms at 20kHz)
But should be: arrival_rtp - 40 samples   (2ms at 20kHz)
Result: emission_rtp is 360 samples too low
D_clock = -18ms (negative!)
```

**This is the most likely cause.**

### 2. Expected Second RTP is Wrong

If `calibrated_offset` is wrong, `expected_second_rtp` will be systematically offset.

But this should have been learned from actual detections, so it should be correct.

### 3. Arrival RTP is Wrong

If the tone detector is measuring arrival time incorrectly, but this uses matched filter group delay which should be consistent.

## Hypothesis: Propagation Delay Overestimation

The propagation delay model may be systematically overestimating delays, especially for:

1. **Low frequencies (2.5 MHz)**: Ionospheric delay is frequency-dependent (1/f²)
2. **Multi-hop paths**: May be assigning 2-hop when actually 1-hop
3. **Ionospheric model**: TEC values may be too high

Let's check the actual propagation delays being calculated vs expected.

## Expected Propagation Delays

For WWV at 629 km from receiver:

```
Frequency    Mode      Geometric    Iono Delay    Total
2.5 MHz      1-hop F   2.9 ms       0.8 ms        3.7 ms
5.0 MHz      1-hop F   2.9 ms       0.3 ms        3.2 ms
10.0 MHz     1-hop F   2.9 ms       0.1 ms        3.0 ms
15.0 MHz     1-hop F   2.9 ms       0.05 ms       2.95 ms
20.0 MHz     1-hop F   2.9 ms       0.03 ms       2.93 ms
25.0 MHz     1-hop F   2.9 ms       0.02 ms       2.92 ms
```

If the model is calculating 20-40ms instead of 3ms, that would explain the -20 to -40ms D_clock values!

## Next Steps

1. Check actual propagation_delay_ms values in recent measurements
2. Compare to expected values above
3. If overestimated, check:
   - Hop count assignment (1-hop vs 2-hop)
   - Layer height (F2 height too high?)
   - TEC values (ionospheric delay too high?)
   - Mode selection logic
