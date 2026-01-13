# Session 2026-01-12: Timing-Based Station Discrimination Implementation

## Objective
Implement timing-based station discrimination across all 17 broadcasts, leveraging GPSDO-anchored timing precision to validate station assignments against geographic propagation constraints rather than relying on signal strength.

## Problem Statement

### Root Cause of Station Confusion
On SHARED frequencies (2.5, 5, 10, 15 MHz), the tone detector creates false positives by assigning station labels based purely on template matching (frequency + duration), without validating against physics, geography, or timing constraints.

**Example of confusion:**
```
Minute 15 on 10 MHz (all three stations broadcasting):
  
  Actual signal: WWV at 629 km (arrives at ~3 ms)
  
  Detector output:
    ✓ WWV template matches at +3.2 ms (SNR 25 dB) ← CORRECT
    ✓ WWVH template matches at +3.2 ms (SNR 18 dB) ← FALSE POSITIVE
    ✓ BPM template matches at +3.2 ms (SNR 20 dB) ← FALSE POSITIVE
```

All three templates can match the SAME signal because:
1. WWV and BPM both use 1000 Hz - templates nearly identical
2. WWVH uses 1200 Hz - but harmonics/nonlinearity create false 1200 Hz content
3. Matched filter doesn't know about geography - just looks for patterns

## Solution Architecture

### Three-Phase Discrimination Strategy

#### Phase 1: BOOTSTRAP (Minutes 0-10)
**Foundation: GPSDO provides ±10ns UTC reference via RTP timestamps**

- Use schedule-based ground truth minutes (14 per hour):
  - Minutes 1, 2: 440 Hz tones (WWVH-only, WWV-only)
  - Minutes 8, 44: Test signals (WWV-only, WWVH-only)
  - Minutes 16, 17, 19: WWV 500 Hz only
  - Minutes 43-51: WWVH 600 Hz only
- BPM tick duration: 10ms (UTC) or 100ms (UT1) vs 5ms (WWV/WWVH)
- Build station-specific propagation delay models
- Establish D_clock baseline (±1ms)

#### Phase 2: TIMING VALIDATION (Minutes 10+)
**Use established timing as a discriminator**

- Validate each tick arrival against expected timing:
  ```
  Expected_ToA = second + delay_station + D_clock
  Reject if |measured - expected| > threshold
  ```
- Phase coherence validation (5-second windows)
- Ground truth tone timing validation

#### Phase 3: CONTINUOUS REFINEMENT (Ongoing)
**Feedback loop for improved accuracy**

- Better discrimination → Better timing measurements
- Better timing → Tighter discrimination windows (±1ms → ±0.5ms)
- Phase stability tracking → Adaptive coherent integration
- Converge to high-confidence station assignment

### Key Insight: The Critical Threshold

**Below ±1 ms accuracy**: Timing errors dominate, can't discriminate stations reliably

**Above ±1 ms accuracy**: 
- Geography discriminates stations (WWV vs WWVH separated by 22 ms)
- Timing variations reveal ionospheric dynamics
- System becomes a **stable reference** observing a **variable medium**

## Implementation

### New Module: `timing_discrimination.py`

Created comprehensive timing-based discrimination module with:

1. **`TimingDiscriminator` class**:
   - Tracks discrimination phase (BOOTSTRAP → VALIDATING → REFINED)
   - Maintains station delay models with uncertainty estimates
   - Validates detections against geographic constraints
   - Persists state across restarts

2. **`StationDelayModel` dataclass**:
   - Mean/std/min/max propagation delays
   - Confidence metrics based on consistency
   - Ground truth minute tracking
   - Adaptive validation windows (±5ms → ±1ms → ±0.5ms)

3. **`GroundTruthSchedule` class**:
   - Definitive schedule of 14 ground truth minutes per hour
   - Station identification for each minute
   - Tone-specific validation (440/500/600 Hz)

4. **`TimingValidationResult` dataclass**:
   - Timing error measurements
   - Phase coherence validation
   - Ground truth matching
   - Discrimination confidence
   - Rejection reasons for diagnostics

### Integration into `wwvh_discrimination.py`

**Modified `WWVHDiscriminator.__init__`**:
- Added optional `timing_discriminator` parameter
- Logs discrimination phase on initialization

**Added Vote 10b: Timing-Based Validation**:
- Weight: 12.0 (REFINED), 8.0 (VALIDATING), 0.0 (BOOTSTRAP)
- Validates both WWV and WWVH detections against delay models
- Uses phase coherence quality for additional validation
- Rejects physically impossible detections
- Logs detailed rejection reasons

**Integration logic**:
```python
if wwv_validation.timing_valid and not wwvh_validation.timing_valid:
    # WWV timing consistent, WWVH rejected
    wwv_score += w_timing_validation
    agreements.append('TIMING_WWV')
elif wwvh_validation.timing_valid and not wwv_validation.timing_valid:
    # WWVH timing consistent, WWV rejected
    wwvh_score += w_timing_validation
    agreements.append('TIMING_WWVH')
elif both valid:
    # Weight by confidence
    wwv_score += w_timing_validation * wwv_confidence
    wwvh_score += w_timing_validation * wwvh_confidence
else:
    # Neither valid - log warning
    logger.warning("Timing validation rejected both stations")
```

## Operational Flow

### System Startup (Bootstrap Phase)

```
Minute 0: GPSDO provides RTP timestamps (±10ns UTC)
  → Wide search windows (±50ms)
  → Detect first strong signal
  → Establish global RTP offset

Minutes 1-10: Ground truth learning
  → Minute 1: Detect WWVH 440 Hz tone
  → Minute 2: Detect WWV 440 Hz tone
  → Minute 8: Detect WWV test signal
  → Build delay models:
      WWV_10MHz: 3.2 ± 2.0 ms (n=3)
      WWVH_10MHz: 24.8 ± 3.0 ms (n=2)
  
Minute 10: Advance to VALIDATING phase
  → Narrow search windows (±5ms)
  → Enable timing validation vote (weight 8.0)
```

### Steady State (Validating/Refined Phase)

```
Each minute:
  1. Tone detector finds signals at 1000 Hz and 1200 Hz
  2. Timing discriminator validates:
     - WWV at 3.2ms: ✓ Consistent (expected 3.2±2ms)
     - WWVH at 3.2ms: ✗ Rejected (expected 25±2ms)
  3. Weighted voting:
     - Timing validation: +8.0 for WWV
     - Other methods: various votes
  4. Final decision: WWV (high confidence)
  5. Update delay model: WWV_10MHz: 3.15 ± 1.5 ms (n=45)
```

### Signal Fading and Recovery

```
Night (strong signals):
  → Measure WWV at 10 MHz: ToA = 3.2 ms
  → Learn: WWV_delay_10MHz = 3.2 ms
  → Store in delay model

Day (signals fade):
  → WWV disappears for hours
  → GPSDO maintains UTC reference
  → Delay model preserved in state file

Next night (signals return):
  → System knows: "WWV at 10 MHz should arrive at 3.2 ms"
  → Narrow search window: 3.2 ± 2 ms
  → Immediate reacquisition when signal returns
  → No re-learning required
```

**Key principle**: The instrument doesn't drift, only the signal does. This is a **stable observatory** observing a **variable medium**.

## Discrimination Priority Hierarchy

1. **Schedule-based ground truth** (Definitive, 14 min/hour) - Weight: 15.0
2. **Timing validation** (Geographic constraints) - Weight: 8.0-12.0
3. **Test signal detection** (Minutes 8/44) - Weight: 15.0
4. **440 Hz tone** (Minutes 1/2) - Weight: 10.0
5. **BCD correlation** (Differential delay) - Weight: 8.0-10.0
6. **Tick duration** (BPM: 10/100ms vs WWV/WWVH: 5ms) - Weight: varies
7. **Phase coherence** (5-second window stability) - Weight: 3.0-5.0
8. **Frequency Selectivity Score** (FSS from test signals) - Weight: 5.0
9. **Carrier power ratio** (Signal strength, lowest priority) - Weight: 1.0-10.0

## Files Modified

1. **`src/hf_timestd/core/timing_discrimination.py`** (NEW)
   - 650 lines
   - Complete timing-based discrimination system
   - State persistence
   - Ground truth schedule management

2. **`src/hf_timestd/core/wwvh_discrimination.py`** (MODIFIED)
   - Added import for `TimingDiscriminator`
   - Modified `__init__` to accept timing discriminator
   - Added Vote 10b with timing validation logic
   - ~80 lines added

## Next Steps

### Immediate (This Session)
1. ✅ Create timing discrimination module
2. ✅ Integrate into WWVHDiscriminator
3. ⏳ Wire up in phase2_analytics_service
4. ⏳ Create session documentation
5. ⏳ Test implementation

### Short-term (Next Session)
1. Add timing discriminator initialization in phase2_analytics_service
2. Connect to timing_calibrator for D_clock updates
3. Test on live data
4. Monitor discrimination confidence progression

### Medium-term
1. Extend to BPM discriminator (tick duration + timing)
2. Add adaptive integration window based on measured T_max
3. Implement interference characterization for "ghost" signals
4. Create discrimination diagnostics dashboard

## Expected Outcomes

- **Eliminate false positives**: Physically impossible detections rejected
- **Sub-millisecond timing**: Geography-validated station assignments
- **Interference characterization**: "Ghost" signals identified and studied
- **Robust discrimination**: Works even with weak signals once timing established
- **Persistent calibration**: Delay models survive signal fading

## Testing Strategy

1. **Bootstrap verification**: Monitor delay model learning in first 10 minutes
2. **Validation accuracy**: Compare timing-based votes with ground truth minutes
3. **Rejection analysis**: Review logs for rejected detections and reasons
4. **Phase advancement**: Verify BOOTSTRAP → VALIDATING → REFINED progression
5. **State persistence**: Restart service and verify delay models reload correctly

## References

- `SESSION_2026-01-11_PROPAGATION_FIX.md`: Previous session on propagation delay fixes
- `src/hf_timestd/core/timing_calibrator.py`: GPSDO-based timing calibration
- `src/hf_timestd/core/wwv_constants.py`: Station coordinates and schedules
- `src/hf_timestd/core/tick_matched_filter.py`: 5-second window implementation

---

**Session Date**: 2026-01-12  
**Implementation Status**: Core modules complete, integration in progress  
**Next Action**: Wire up timing discriminator in phase2_analytics_service
