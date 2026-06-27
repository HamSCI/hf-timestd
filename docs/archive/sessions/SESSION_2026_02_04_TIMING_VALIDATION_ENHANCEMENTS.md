# Session 2026-02-04: Timing Validation Enhancements

## Overview

This session focused on ensuring accurate detection of all 17 broadcasts within propagation constraints, implementing multi-constraint timing validation, and establishing station priority policy for fusion.

## Dual-Purpose Architecture

Documented the fundamental duality of the HF Time Standard system:

1. **Timing Reconstruction (Fusion Mode):** Use the 17 broadcasts as perfect references to reconstruct UTC locally
2. **Ionospheric Measurement (RTP Mode):** Given authoritative GPS+PPS timing, measure ionospheric effects as residuals

See `/docs/design/DUAL_PURPOSE_ARCHITECTURE.md` for full documentation.

## Bug Fixes (7 Total)

### Fix 1: False Detections from Flat Correlation
**File:** `metrology_engine.py`

**Problem:** When signal is weak/noisy, `np.argmax()` on flat correlation data returns index 0, causing false detections at `arrival=0.00ms`.

**Solution:** Added edge detection rejection - if peak is at edge of search window AND correlation variation is <50%, reject as noise.

### Fix 2: Double-Subtraction of Propagation Delay
**File:** `metrology_engine.py`

**Problem:** Tick analysis timing error was computed as:
```python
timing_error_ms = tick_analysis.mean_timing_offset_ms - expected_delay_ms
```
But `mean_timing_offset_ms` is already a relative offset (should be ~0), so subtracting `expected_delay_ms` caused large negative errors (-53ms to -94ms).

**Solution:** Use `mean_timing_offset_ms` directly without subtracting expected delay.

### Fix 3: Physics Validation Now Rejects (Not Just Warns)
**File:** `metrology_engine.py`

**Problem:** Detections outside the physics-predicted arrival window were logged as warnings but still accepted, leading to false "VALIDATED" messages.

**Solution:** Changed from warning to actual rejection with `continue` to skip invalid detections.

### Fix 4: Raised Correlation SNR Threshold
**File:** `metrology_engine.py`

**Problem:** 6 dB threshold (2x ratio) allowed random noise peaks to pass as detections.

**Solution:** Raised to 12 dB (4x ratio) to reject noise while accepting weak real signals.

### Fix 5: Misleading Log Message
**File:** `metrology_engine.py`

**Problem:** "VALIDATED" was logged before physics validation occurred.

**Solution:** Changed to "DETECTED" - physics validation logs separately.

### Fix 6: Physics Validation Used Wrong Value
**File:** `metrology_engine.py`

**Problem:** Physics validation was using `det.timing_error_ms` (arrival - expected) instead of raw arrival sample for window checking.

**Solution:** Now uses `det.sample_position_original` (raw arrival sample from minute boundary).

### Fix 7: No Station Priority in Fusion
**File:** `multi_broadcast_fusion.py`

**Problem:** All stations weighted equally, allowing BPM's high-uncertainty measurements to influence timing.

**Solution:** Added station priority system (see below).

## New Feature: TimingConsistencyValidator

**File:** `src/hf_timestd/core/timing_consistency_validator.py`

A comprehensive multi-constraint timing validation module that exploits all known timing constraints:

### Intra-Minute Constraints
1. **Arrival Sequence** - Stations at different distances must arrive in order (WWV before WWVH before BPM)
2. **Cross-Station Consistency** - All stations transmit at UTC second 0, so T_emission = T_arrival - T_propagation should agree within ±5ms
3. **Cross-Frequency Ionospheric Dispersion** - Same station on multiple frequencies follows 1/f² delay law, enabling TEC estimation

### Inter-Minute Constraints
4. **Sample Interval Stability** - Exactly 1,440,000 samples between minute boundaries
5. **Arrival Time Stability** - Same broadcast arrives at consistent offset (±ionospheric variation)
6. **Differential Arrival Stability** - (T_wwv - T_wwvh) is stable, removing common-mode effects

### Integration
- Integrated into `MetrologyEngine.process_minute()`
- Validates all detections after physics validation
- Logs validation summary and stability metrics every 10 minutes
- Stores validation results for downstream use

## Station Priority Policy

**File:** `multi_broadcast_fusion.py`

Established clear hierarchy for timing anchors:

| Station | Weight | Role |
|---------|--------|------|
| CHU | 1.0 | Primary Anchor (Reference) - unique frequencies, FSK verification |
| WWV | 1.0 | Primary Anchor - closest station, well-characterized |
| WWVH | 0.9 | Primary Anchor - longer path but reliable |
| BPM | 0.3 | Secondary/Scientific - long path, high uncertainty |

### Rationale for BPM as Secondary
- Very long path (~11,000 km) with high ionospheric variability
- Multi-hop propagation introduces more uncertainty
- UT1/UTC alternation (minutes 25-29, 55-59) requires careful handling
- Maintained for scientific interest (ionospheric probing of trans-Pacific path)

## Files Modified

| File | Changes |
|------|---------|
| `src/hf_timestd/core/metrology_engine.py` | 6 bug fixes, TimingConsistencyValidator integration |
| `src/hf_timestd/core/multi_broadcast_fusion.py` | Station priority system |
| `src/hf_timestd/core/timing_consistency_validator.py` | **NEW** - Multi-constraint validation |
| `docs/design/DUAL_PURPOSE_ARCHITECTURE.md` | **NEW** - Dual-purpose architecture documentation |
| `CRITIC_CONTEXT.md` | Updated with session results and next session tasks |

## Remaining Tasks for Next Session

1. **HF-derived TEC archival** - Archive TEC estimates from TimingConsistencyValidator as L3 products
2. **Ionospheric residual products** - In RTP mode, compute and archive T_iono = T_observed - T_transmitted - T_geometric
3. **Adaptive weighting** - Weight broadcasts by current SNR and ionospheric stability
4. **Real-time TEC feedback** - Feed TEC estimates back into arrival predictions
5. **Cross-path correlation** - Detect Traveling Ionospheric Disturbances (TIDs)
6. **Broadcast coverage audit** - Verify all 17 broadcasts are being detected correctly

## Testing

- Deployed to production via `update-production.sh`
- TimingConsistencyValidator initialized on all 9 channels
- Physics validation now correctly rejecting out-of-window detections
- Station priority applied in fusion weight calculations

## Version

These changes are part of the ongoing v5.x development series.
