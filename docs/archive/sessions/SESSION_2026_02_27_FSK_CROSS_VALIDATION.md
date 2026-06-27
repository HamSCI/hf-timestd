# Session 2026-02-27: CHU FSK Cross-Validation Integration

## Summary

Integrated CHU FSK Frame B decoded data (DUT1, TAI-UTC, year) and Frame A
decoded time into the metrology pipeline as cross-validation and correction
inputs. Previously these values were decoded and written to HDF5 but not
consumed by any downstream function.

## Four Integrations

### 1. Frame A UTC Sanity Check

**File:** `metrology_engine.py` → `_cross_validate_fsk()`

CHU Frame A broadcasts the current UTC minute (day, hour, minute) in seconds
32-39. This is the **only independent UTC source** in the system — all other
timing derives from the RTP chain (GPS → radiod → RTP counter → UTC formula).

The sanity check compares the FSK-decoded minute against the RTP-derived
`minute_boundary`. If they disagree for 3+ consecutive minutes, an ERROR is
logged indicating the RTP timing chain may be broken.

This catches catastrophic failures (wrong minute, wrong hour, firmware bugs in
the RTP counter) that tick/tone timing cannot detect because those measure
*relative* arrival time, not *absolute* UTC.

### 2. TAI-UTC Leap Second Watch

**Files:** `metrology_engine.py` → `_cross_validate_fsk()`,
`multi_broadcast_fusion.py` → FSK timing integration + Kalman hold

CHU Frame B broadcasts TAI-UTC (currently 37s). When a leap second is
scheduled, CHU updates this field in advance of the event.

Two components:

- **Detection (metrology engine):** Tracks `_fsk_last_tai_utc`. When it
  changes, sets `_fsk_tai_utc_changed = True` and logs a WARNING.

- **Kalman hold (fusion):** When `_fsk_leap_second_hold` is True, the fusion
  Kalman filter skips its update and coasts on the prediction. Without this,
  the 1-second UTC jump during a leap second would produce a massive Kalman
  innovation that corrupts the state estimate.

### 3. DUT1 → Propagation Model (UT1 Recovery)

**Files:** `propagation_model.py` → `set_dut1()` + `_parametric_iono()`,
`multi_broadcast_fusion.py` → DUT1 passthrough

DUT1 = UT1 - UTC, broadcast to 0.1s precision. UT1 is Earth rotation angle.
The parametric ionospheric fallback model computes local solar time (LST) for
the diurnal ionospheric variation. Using UT1 instead of UTC gives the correct
solar geometry:

```
UT1 = UTC + DUT1
LST = UT1_hour + longitude / 15.0
```

The effect is small (±0.9s → ±0.004° solar angle) but represents a real
physical correction from a national time lab. The DUT1 value flows:

```
CHU FSK Frame B → metrology_engine._cross_validate_fsk()
                → fusion._fsk_dut1
                → physics_model.set_dut1()
                → _parametric_iono() LST calculation
```

### 4. BER-Based Confidence Weighting

**Files:** `metrology_engine.py` → `_cross_validate_fsk()`,
`multi_broadcast_fusion.py` → FSK timing integration

The FSK decode rate (frames_decoded / 9) is a direct measure of channel
quality during each minute. This is now used to scale confidence:

- **Metrology engine:** `fsk_confidence *= decode_rate`. A minute with 9/9
  frames keeps full confidence; 2/9 frames → 22% confidence.

- **Fusion:** `BroadcastMeasurement.confidence = raw_confidence × decode_rate`.
  Minutes with heavy fading (few frames decoded) get proportionally lower
  weight in the weighted mean. Quality grade degrades from 'A' to 'B' when
  decode_rate < 0.5.

## State Variables Added

### MetrologyEngine (`__init__`)
- `_fsk_last_tai_utc: Optional[int]` — Last decoded TAI-UTC
- `_fsk_last_dut1: Optional[float]` — Last decoded DUT1 (seconds)
- `_fsk_tai_utc_changed: bool` — True when leap second detected
- `_fsk_utc_mismatch_count: int` — Consecutive UTC mismatches

### MultiBroadcastFusion (`__init__`)
- `_fsk_dut1: Optional[float]` — Latest DUT1 from CHU FSK
- `_fsk_tai_utc: Optional[int]` — Latest TAI-UTC from CHU FSK
- `_fsk_leap_second_hold: bool` — True during leap second transition

### HFPropagationModel (`__init__`)
- `_dut1_seconds: float` — DUT1 correction (default 0.0)

## Files Modified

| File | Changes |
|------|---------|
| `metrology_engine.py` | Added `_cross_validate_fsk()` method, FSK state vars, wired into `process_minute()` |
| `multi_broadcast_fusion.py` | BER-weighted FSK confidence, DUT1/TAI-UTC passthrough, Kalman leap second hold |
| `propagation_model.py` | Added `set_dut1()`, DUT1 correction in parametric LST calculation |

## Also in This Session

- **L2 calibration fix** (`012b9a8`): `_channels_from_config()` read wrong
  config path. Fixed to read `[[recorder.channels]]` with legacy fallback.
