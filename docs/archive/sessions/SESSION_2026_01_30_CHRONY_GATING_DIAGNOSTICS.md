# Session 2026-01-30: Chrony Feed Gating Diagnostics

## Problem

Chrony feeds (TSL1/TSL2) repeatedly becoming unreachable with large offsets (40-60ms). The feeds would show `Reach 0` and `LastRx` values of many hours, indicating no updates were being written to the SHM segments.

## Root Cause

The chrony feed gating logic in `multi_broadcast_fusion.py` was silently rejecting updates without logging why. When updates were rejected, there was no visibility into which gate condition failed:
- `quality_ok` - grade A/B/C required (or D with uncertainty <50ms during bootstrap)
- `multi_station` - at least 1 station required
- `consistent` - consistency flag must be OK or acceptable
- `discontinuity_ok` - no large jumps (>10ms converged, >100ms during bootstrap)

The underlying issue was typically a stale bootstrap timing reference that caused large D_clock offsets, which then failed the quality gate.

## Fix

Added diagnostic logging to show exactly why chrony writes are being gated:

```python
# DIAGNOSTIC: Log gating status for debugging chrony feed issues
if not (quality_ok and multi_station and consistent and discontinuity_ok):
    logger.info(
        f"Chrony feed GATED: quality_ok={quality_ok}, multi_station={multi_station}, "
        f"consistent={consistent}, discontinuity_ok={discontinuity_ok} "
        f"[grade={result.quality_grade}, n_sta={result.n_stations}, "
        f"flag={result.consistency_flag}, unc={result.uncertainty_ms:.1f}ms]"
    )
```

This produces log entries like:
```
Chrony feed GATED: quality_ok=False, multi_station=True, consistent=True, discontinuity_ok=True [grade=D, n_sta=3, flag=CROSS_STATION_DISAGREE, unc=50.2ms]
```

## Files Changed

- `src/hf_timestd/core/multi_broadcast_fusion.py` - Added diagnostic logging for chrony gating

## New Files

- `src/hf_timestd/core/arrival_pattern_matrix.py` - Physics-based arrival prediction matrix
- `docs/design/ARRIVAL_PATTERN_MATRIX_ARCHITECTURE.md` - Architecture documentation

## Recovery Procedure

When chrony feeds become unreachable:

1. Check fusion logs for `GATED` messages to identify which condition is failing
2. If `quality_ok=False` with large uncertainty, check bootstrap state:
   ```bash
   cat /var/lib/timestd/state/bootstrap_timing_reference.json | python3 -m json.tool
   ```
3. Compare `decoded_hour:decoded_minute` with current UTC time
4. If bootstrap time is wrong, reset and restart:
   ```bash
   sudo rm -f /var/lib/timestd/state/bootstrap_timing_reference.json
   sudo systemctl restart timestd-core-recorder timestd-metrology timestd-fusion
   ```
5. Wait for bootstrap to re-lock (typically 60-90 seconds)
6. Restart chrony to pick up new SHM data:
   ```bash
   sudo systemctl restart chrony
   ```

## Observations

- Bootstrap can lock with completely wrong times (e.g., 13 hours off)
- The BCD/FSK decoder may confirm stale time references
- Once bootstrap is wrong, all D_clock values are offset by the error
- The quality gate correctly rejects these bad values, but without logging it was invisible
