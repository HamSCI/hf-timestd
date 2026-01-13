# Session 2026-01-12: Unified Operational Phase Architecture

## Objective

Align program architecture to support the three-phase operational model:
1. **BOOTSTRAP** (0-10 min): Establish global RTP-to-UTC offset
2. **REFINEMENT** (10-30 min): Refine timing accuracy to ±1ms
3. **MEASUREMENT** (30+ min): Operational ionospheric measurement

## Problem Identified

### Architectural Fragmentation

The system had **two separate phase systems** operating independently:

**1. `TimingCalibrator.CalibrationPhase`** (`timing_calibrator.py:105-110`):
- States: `BOOTSTRAP` → `PROVISIONAL` → `CALIBRATED` → `VERIFIED`
- Focus: RTP offset establishment, propagation delay learning
- Drives: Search window sizing, RTP prediction

**2. `TimingDiscriminator.DiscriminationPhase`** (`timing_discrimination.py:82-86`):
- States: `BOOTSTRAP` → `VALIDATING` → `REFINED`
- Focus: Station discrimination, delay model validation
- Drives: Discrimination vote weights, timing validation

### The Core Issue

These systems were solving the **same problem** (system convergence) with **different state machines**, creating:
- **Coordination complexity**: Two phase transitions that must be synchronized
- **Unclear ownership**: Which system drives search windows? Discrimination strategy?
- **Architectural confusion**: Does "CALIBRATED" mean the same as "VALIDATING"?
- **No measurement mode**: Neither system addressed the transition to multi-channel extraction

## Solution: Unified Operational Phase System

### Single Source of Truth

Created `OperationalPhaseManager` as the **single source of truth** for system-wide operational phase.

All subsystems query this manager to determine:
- Search window sizing (timing_calibrator)
- Discrimination strategy (timing_discrimination)
- Measurement mode (phase2_temporal_engine)
- Output confidence (phase2_analytics_service)

### Three-Phase Architecture

```
BOOTSTRAP (0-10 min)
├─ Objective: Establish global RTP-to-UTC offset
├─ Search Window: ±500ms (wide, unknown propagation)
├─ Discrimination: Schedule-based ground truth only
├─ Measurement: Single dominant station per frequency
└─ Output: D_clock ±5ms, initial station assignments

REFINEMENT (10-30 min)
├─ Objective: Refine timing accuracy to ±1ms
├─ Search Window: ±5ms (narrow, centered on expected)
├─ Discrimination: Timing validation enabled (weight: 8.0)
├─ Measurement: Single dominant station (timing-validated)
└─ Output: D_clock ±1ms, validated station assignments

MEASUREMENT (30+ min)
├─ Objective: Measure ionospheric propagation independently
├─ Search Window: ±1ms (tight, sub-ms precision)
├─ Discrimination: High-confidence timing (weight: 12.0)
├─ Measurement: Multi-channel extraction (all 17 broadcasts)
└─ Output: Independent metrics for each broadcast
```

## Implementation

### Files Created

**1. Design Document** (`docs/design/UNIFIED_OPERATIONAL_PHASE_SYSTEM.md`):
- Complete architectural specification
- Phase transition criteria
- Migration strategy
- Testing approach

**2. Core Module** (`src/hf_timestd/core/operational_phase_manager.py`):
- 750 lines
- `OperationalPhase` enum (BOOTSTRAP, REFINEMENT, MEASUREMENT)
- `OperationalPhaseManager` class
- Phase-specific metrics classes:
  - `BootstrapMetrics`: Tracks RTP offset establishment, station detections, D_clock convergence
  - `RefinementMetrics`: Tracks D_clock convergence, station delay model quality
  - `MeasurementMetrics`: Tracks stability, degradation detection
- State persistence with file locking
- Automatic phase transitions based on criteria

### Key Features

**Phase-Dependent Parameters**:
```python
# Search windows
manager.get_search_window_ms('WWV')
# → BOOTSTRAP: 500ms, REFINEMENT: 5ms, MEASUREMENT: 1ms

# Discrimination weights
manager.get_discrimination_weight('timing_validation')
# → BOOTSTRAP: 0.0, REFINEMENT: 8.0, MEASUREMENT: 12.0

# Measurement mode
manager.should_use_multi_channel_extraction()
# → BOOTSTRAP: False, REFINEMENT: False, MEASUREMENT: True
```

**Automatic Transitions**:
- Bootstrap → Refinement: When RTP offset established, ≥10 detections/station, D_clock std < 5ms
- Refinement → Measurement: When D_clock std < 1ms, station delay std < 2ms, ≥30 measurements/station
- Measurement → Refinement: When D_clock std > 2ms for 5 consecutive minutes
- Any Phase → Bootstrap: When D_clock std > 5ms (severe degradation)

**State Persistence**:
- JSON state file with file locking
- Version validation
- Transition history tracking
- Survives service restarts

## Phase Transition Criteria

### BOOTSTRAP → REFINEMENT

✓ **Global RTP offset established** (≥2 anchor stations)
- Anchor stations: CHU (3.33/7.85/14.67 MHz), WWV (20/25 MHz)
- Unambiguous stations provide reference RTP offset

✓ **≥10 detections per station** (at least 2 stations)
- Sufficient data to establish delay models
- Cross-station validation possible

✓ **D_clock std < 5ms** over last 5 minutes
- Timing converging to usable accuracy
- Ready for narrow search windows

### REFINEMENT → MEASUREMENT

✓ **D_clock std < 1ms** over last 10 minutes
- Sub-millisecond timing accuracy achieved
- Temporal windows can separate stations

✓ **Station delay models: std < 2ms** (at least 2 stations)
- Propagation delays well-characterized
- Prediction windows can be narrow

✓ **≥30 validated measurements per station** (at least 2 stations)
- Sufficient data for statistical confidence
- Delay models stable and reliable

### Degradation Detection

**MEASUREMENT → REFINEMENT**:
- D_clock std > 2ms for 5 consecutive minutes
- Moderate degradation, needs refinement

**Any Phase → BOOTSTRAP**:
- D_clock std > 5ms
- Severe degradation, re-bootstrap required

## Integration Plan

### Phase 1: Non-Breaking Integration (Observe Only)

**1. Initialize in Phase2AnalyticsService**:
```python
# In __init__
self.operational_phase_manager = OperationalPhaseManager(
    state_file=self.archive_dir.parent.parent / 'state' / 'operational_phase.json'
)

# Wire up callbacks
self.engine.operational_phase_manager = self.operational_phase_manager
self.timing_calibrator.operational_phase_manager = self.operational_phase_manager
```

**2. Update metrics after each measurement**:
```python
# In _write_clock_offset or process loop
self.operational_phase_manager.update_metrics(
    station=station,
    d_clock_ms=result.d_clock_ms,
    timestamp=minute_boundary,
    delay_std_ms=station_delay_std,
    is_anchor=(channel_name in ANCHOR_CHANNELS)
)
```

**3. Log phase transitions** (observe, don't act yet):
```python
current_phase = self.operational_phase_manager.get_phase()
logger.info(f"Current operational phase: {current_phase.value}")
```

### Phase 2: Subsystem Integration (Breaking Changes)

**1. TimingCalibrator**:
- Remove `CalibrationPhase` enum
- Query `operational_phase_manager.get_search_window_ms()` instead of internal logic
- Simplify to focus on RTP offset and propagation delays

**2. TimingDiscriminator**:
- Remove `DiscriminationPhase` enum
- Query `operational_phase_manager.get_discrimination_weight()` for vote weights
- Simplify to focus on validation logic

**3. WWVHDiscriminator**:
- Update Vote 10b weight from hardcoded to phase-dependent:
```python
w_timing_validation = self.operational_phase_manager.get_discrimination_weight('timing_validation')
```

### Phase 3: Multi-Channel Extraction (New Feature)

**1. Create MultiChannelExtractor** (`multi_channel_extractor.py`):
- Extract IQ samples for all stations from temporal windows
- Only active when `operational_phase_manager.should_use_multi_channel_extraction()`

**2. Update Phase2TemporalEngine**:
- Check phase before processing
- If MEASUREMENT phase: extract all channels, measure independently
- If BOOTSTRAP/REFINEMENT: use existing single-station logic

**3. Update data products**:
- New schema: `MultiChannelMeasurement` (all broadcasts)
- Replace: `DiscriminationResult` (single station)

## Benefits

1. **Single Source of Truth**: One phase system, not two
2. **Clear Ownership**: Phase manager owns all phase-dependent behavior
3. **Predictable Transitions**: Well-defined criteria, logged transitions
4. **Testable**: Can mock phase manager to test each mode
5. **Observable**: Phase visible in logs, status files, web UI
6. **Extensible**: Easy to add new phase-dependent behaviors
7. **Supports Multi-Channel**: Enables transition to measuring all 17 broadcasts independently

## Architectural Alignment

This implementation directly supports the objectives from `ARCHITECTURE_MULTI_CHANNEL_MEASUREMENT.md`:

**Bootstrap Phase** (Minutes 0-10):
- ✅ Learn propagation delays for each station
- ✅ Use discrimination to identify signals
- ✅ Build delay models with uncertainty

**Temporal Separation Phase** (Minutes 10-30):
- ✅ Transition from discrimination to independent measurement
- ✅ Extract each station's signal from temporal window
- ✅ Output independent metrics for each broadcast

**Ionospheric Science Phase** (Minutes 30+):
- ✅ Measure ionospheric phenomena, not timing errors
- ✅ Narrow windows (±1ms), high-precision measurement
- ✅ Variations describe physics, not measurement errors

## Testing Strategy

### Unit Tests

```python
def test_bootstrap_phase():
    """Test bootstrap phase behavior."""
    manager = OperationalPhaseManager()
    assert manager.get_phase() == OperationalPhase.BOOTSTRAP
    assert manager.get_search_window_ms('WWV') == 500.0
    assert manager.get_discrimination_weight('timing_validation') == 0.0
    assert not manager.should_use_multi_channel_extraction()

def test_phase_transition_bootstrap_to_refinement():
    """Test automatic transition from bootstrap to refinement."""
    manager = OperationalPhaseManager()
    
    # Simulate bootstrap measurements
    for i in range(15):
        manager.update_metrics(
            station='WWV' if i % 2 == 0 else 'CHU',
            d_clock_ms=3.0 + np.random.normal(0, 0.5),
            timestamp=time.time() + i * 60,
            is_anchor=True
        )
    
    # Should transition to refinement
    assert manager.get_phase() == OperationalPhase.REFINEMENT
```

### Integration Tests

1. **Bootstrap convergence**: Monitor first 10 minutes, verify transition
2. **Refinement stability**: Monitor 10-30 minutes, verify transition
3. **Degradation recovery**: Inject bad measurements, verify fallback
4. **State persistence**: Restart service, verify phase preserved

## Next Steps

### Immediate (This Session)
- ✅ Create design document
- ✅ Implement `OperationalPhaseManager`
- ✅ Document integration plan
- ⏳ Create session summary

### Short-term (Next Session)
1. Initialize `OperationalPhaseManager` in `Phase2AnalyticsService`
2. Wire up metric updates
3. Log phase transitions (observe mode)
4. Monitor behavior on live data

### Medium-term
1. Refactor `TimingCalibrator` to query phase manager
2. Refactor `TimingDiscriminator` to query phase manager
3. Update `WWVHDiscriminator` vote weights
4. Add unit tests

### Long-term
1. Implement `MultiChannelExtractor`
2. Enable multi-channel measurement in MEASUREMENT phase
3. Update data products schema
4. Create phase status dashboard for web UI

## References

- `ARCHITECTURE_MULTI_CHANNEL_MEASUREMENT.md`: Multi-channel measurement architecture
- `SESSION_2026-01-12_TIMING_DISCRIMINATION.md`: Timing-based discrimination implementation
- `docs/design/UNIFIED_OPERATIONAL_PHASE_SYSTEM.md`: Complete design specification
- `src/hf_timestd/core/operational_phase_manager.py`: Implementation

---

**Session Date**: 2026-01-12  
**Implementation Status**: Core module complete, integration pending  
**Next Action**: Initialize phase manager in Phase2AnalyticsService and wire up metric updates
