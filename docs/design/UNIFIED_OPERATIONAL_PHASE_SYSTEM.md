# Unified Operational Phase System Design

## Problem Statement

The current architecture has two separate phase systems:
1. `TimingCalibrator.CalibrationPhase` (BOOTSTRAP → PROVISIONAL → CALIBRATED → VERIFIED)
2. `TimingDiscriminator.DiscriminationPhase` (BOOTSTRAP → VALIDATING → REFINED)

These systems are solving the same problem with different state machines, creating:
- **Coordination complexity**: Two phase transitions that must be synchronized
- **Unclear ownership**: Which system drives search windows? Discrimination strategy?
- **Architectural confusion**: Does "CALIBRATED" mean the same as "VALIDATING"?

## Solution: Single Operational Phase System

### Core Principle

**The system has ONE operational state that drives ALL subsystems:**
- Search window sizing (timing_calibrator)
- Discrimination strategy (timing_discrimination)
- Measurement mode (phase2_temporal_engine)
- Output confidence (phase2_analytics_service)

### Three-Phase Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         BOOTSTRAP PHASE (0-10 min)                      │
│                                                                         │
│  Objective: Establish global RTP-to-UTC offset                         │
│                                                                         │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │ Timing Calibrator:                                               │  │
│  │   - Search window: ±500ms (wide)                                 │  │
│  │   - Learn RTP offset from anchor stations                        │  │
│  │   - Build propagation delay models                               │  │
│  └──────────────────────────────────────────────────────────────────┘  │
│                                                                         │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │ Timing Discriminator:                                            │  │
│  │   - Use ground truth minutes (14/hour)                           │  │
│  │   - BPM tick duration (10/100ms vs 5ms)                          │  │
│  │   - Build station delay models                                   │  │
│  └──────────────────────────────────────────────────────────────────┘  │
│                                                                         │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │ Measurement Mode:                                                │  │
│  │   - Single dominant station per frequency                        │  │
│  │   - Weighted voting discrimination                               │  │
│  └──────────────────────────────────────────────────────────────────┘  │
│                                                                         │
│  Output: D_clock ±5ms, initial station assignments                     │
│                                                                         │
│  Transition Criteria:                                                  │
│    ✓ Global RTP offset established (≥2 anchor stations)               │
│    ✓ ≥10 detections per station                                        │
│    ✓ D_clock std < 5ms over last 5 minutes                             │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                       REFINEMENT PHASE (10-30 min)                      │
│                                                                         │
│  Objective: Refine timing accuracy to ±1ms                             │
│                                                                         │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │ Timing Calibrator:                                               │  │
│  │   - Search window: ±5ms (narrow)                                 │  │
│  │   - Refine RTP offset with high-SNR detections                   │  │
│  │   - Validate cross-station consistency                           │  │
│  └──────────────────────────────────────────────────────────────────┘  │
│                                                                         │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │ Timing Discriminator:                                            │  │
│  │   - Enable timing validation (Vote 10b weight: 8.0)              │  │
│  │   - Reject physically impossible detections                      │  │
│  │   - Phase coherence validation                                   │  │
│  └──────────────────────────────────────────────────────────────────┘  │
│                                                                         │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │ Measurement Mode:                                                │  │
│  │   - Still single dominant station                                │  │
│  │   - But timing-validated assignments                             │  │
│  └──────────────────────────────────────────────────────────────────┘  │
│                                                                         │
│  Output: D_clock ±1ms, validated station assignments                   │
│                                                                         │
│  Transition Criteria:                                                  │
│    ✓ D_clock std < 1ms over last 10 minutes                            │
│    ✓ Station delay models: std < 2ms                                   │
│    ✓ ≥30 validated measurements per station                            │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                      MEASUREMENT PHASE (30+ min)                        │
│                                                                         │
│  Objective: Measure ionospheric propagation independently              │
│                                                                         │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │ Timing Calibrator:                                               │  │
│  │   - Search window: ±1ms (tight)                                  │  │
│  │   - Monitor for timing anomalies                                 │  │
│  │   - Detect propagation mode changes                              │  │
│  └──────────────────────────────────────────────────────────────────┘  │
│                                                                         │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │ Timing Discriminator:                                            │  │
│  │   - Timing validation (Vote 10b weight: 12.0)                    │  │
│  │   - Sub-ms discrimination windows                                │  │
│  │   - High-confidence rejection                                    │  │
│  └──────────────────────────────────────────────────────────────────┘  │
│                                                                         │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │ Measurement Mode: MULTI-CHANNEL EXTRACTION                       │  │
│  │   - Extract ALL stations from temporal windows                   │  │
│  │   - WWV: [1.2, 5.2] ms                                           │  │
│  │   - WWVH: [21.8, 27.8] ms                                        │  │
│  │   - BPM: [40.0, 50.0] ms                                         │  │
│  │   - Measure each broadcast independently                         │  │
│  └──────────────────────────────────────────────────────────────────┘  │
│                                                                         │
│  Output: Independent metrics for all 17 broadcasts                     │
│          Variations describe ionospheric physics, not errors           │
│                                                                         │
│  Degradation Detection:                                                │
│    ⚠ If D_clock std > 2ms for 5 consecutive minutes → REFINEMENT      │
│    ⚠ If D_clock std > 5ms for 5 consecutive minutes → BOOTSTRAP       │
└─────────────────────────────────────────────────────────────────────────┘
```

## Implementation Plan

### Step 1: Create Unified Phase Manager

**New Module**: `src/hf_timestd/core/operational_phase_manager.py`

```python
class OperationalPhase(Enum):
    """System-wide operational phase."""
    BOOTSTRAP = "bootstrap"      # 0-10 min: Establish global RTP offset
    REFINEMENT = "refinement"    # 10-30 min: Refine timing, validate stations
    MEASUREMENT = "measurement"  # 30+ min: Operational ionospheric measurement

class OperationalPhaseManager:
    """
    Centralized manager for system-wide operational phase.
    
    All subsystems query this manager to determine:
    - Search window sizing
    - Discrimination strategy
    - Measurement mode
    - Output confidence
    """
    
    def __init__(self, state_file: Path):
        self.phase = OperationalPhase.BOOTSTRAP
        self.state_file = state_file
        
        # Phase transition tracking
        self.phase_start_time = time.time()
        self.measurements_in_phase = 0
        
        # Bootstrap metrics
        self.bootstrap_metrics = BootstrapMetrics()
        
        # Refinement metrics
        self.refinement_metrics = RefinementMetrics()
        
        # Measurement metrics
        self.measurement_metrics = MeasurementMetrics()
    
    def get_search_window_ms(self, station: str) -> float:
        """Get search window based on current phase."""
        if self.phase == OperationalPhase.BOOTSTRAP:
            return 500.0
        elif self.phase == OperationalPhase.REFINEMENT:
            return 5.0
        else:  # MEASUREMENT
            return 1.0
    
    def get_discrimination_weight(self, method: str) -> float:
        """Get discrimination vote weight based on phase."""
        if method == 'timing_validation':
            if self.phase == OperationalPhase.BOOTSTRAP:
                return 0.0  # Don't use timing during bootstrap
            elif self.phase == OperationalPhase.REFINEMENT:
                return 8.0
            else:  # MEASUREMENT
                return 12.0
        # ... other methods
    
    def should_use_multi_channel_extraction(self) -> bool:
        """Should we extract all stations independently?"""
        return self.phase == OperationalPhase.MEASUREMENT
    
    def update_metrics(self, measurement: Dict):
        """Update phase metrics and check transition criteria."""
        if self.phase == OperationalPhase.BOOTSTRAP:
            self.bootstrap_metrics.add_measurement(measurement)
            if self._check_bootstrap_complete():
                self._transition_to_refinement()
        
        elif self.phase == OperationalPhase.REFINEMENT:
            self.refinement_metrics.add_measurement(measurement)
            if self._check_refinement_complete():
                self._transition_to_measurement()
            elif self._check_degradation():
                self._transition_to_bootstrap()
        
        else:  # MEASUREMENT
            self.measurement_metrics.add_measurement(measurement)
            if self._check_degradation():
                self._transition_to_refinement()
```

### Step 2: Refactor Existing Systems

**2.1 TimingCalibrator**
- Remove `CalibrationPhase` enum
- Query `OperationalPhaseManager` for search windows
- Simplify to focus on RTP offset and propagation delays

**2.2 TimingDiscriminator**
- Remove `DiscriminationPhase` enum
- Query `OperationalPhaseManager` for vote weights
- Simplify to focus on validation logic

**2.3 Phase2AnalyticsService**
- Initialize `OperationalPhaseManager` (singleton)
- Pass to all subsystems
- Use phase to determine measurement mode

### Step 3: Multi-Channel Extraction (MEASUREMENT Phase)

**New Module**: `src/hf_timestd/core/multi_channel_extractor.py`

```python
class MultiChannelExtractor:
    """
    Extract IQ samples for all stations from temporal windows.
    
    Only active in MEASUREMENT phase when timing is accurate enough
    that temporal windows don't overlap.
    """
    
    def extract_channels(
        self,
        iq_samples: np.ndarray,
        station_delays: Dict[str, float],
        sample_rate: int
    ) -> Dict[str, np.ndarray]:
        """
        Extract IQ samples for each station.
        
        Args:
            iq_samples: Full minute of IQ data
            station_delays: {station: delay_ms}
            sample_rate: Sample rate in Hz
        
        Returns:
            {station: iq_window} for each detected station
        """
        channels = {}
        
        for station, delay_ms in station_delays.items():
            # Calculate temporal window
            window_center_samples = int(delay_ms * sample_rate / 1000)
            window_width_samples = int(2.0 * sample_rate / 1000)  # ±1ms
            
            start = window_center_samples - window_width_samples // 2
            end = window_center_samples + window_width_samples // 2
            
            # Extract window
            if 0 <= start < len(iq_samples) and end <= len(iq_samples):
                channels[station] = iq_samples[start:end]
        
        return channels
```

### Step 4: Data Model Changes

**Replace**: `DiscriminationResult` (single station)
**With**: `MultiChannelMeasurement` (all stations)

```python
@dataclass
class BroadcastMeasurement:
    """Measurement for a single broadcast (station + frequency)."""
    station: str
    frequency_mhz: float
    
    # Detection
    detected: bool
    snr_db: float
    
    # Timing
    toa_ms: float
    toa_uncertainty_ms: float
    
    # Propagation
    delay_ms: float
    delay_spread_ms: float
    
    # Ionospheric
    doppler_hz: float
    phase_variance_rad2: float
    tec_tecu: Optional[float]

@dataclass
class MultiChannelMeasurement:
    """Measurement for all broadcasts on a frequency."""
    frequency_mhz: float
    timestamp: float
    
    # Per-broadcast measurements
    broadcasts: Dict[str, BroadcastMeasurement]
    
    # Operational context
    operational_phase: OperationalPhase
    measurement_mode: str  # 'single_station' or 'multi_channel'
```

## Migration Strategy

### Phase 1: Add Unified Manager (Non-Breaking)
1. Create `OperationalPhaseManager`
2. Initialize in `Phase2AnalyticsService`
3. Log phase transitions (observe only)

### Phase 2: Refactor Subsystems (Breaking)
1. Remove duplicate phase enums
2. Query manager for parameters
3. Update tests

### Phase 3: Multi-Channel Extraction (New Feature)
1. Create `MultiChannelExtractor`
2. Enable in MEASUREMENT phase
3. Update data products

## Benefits

1. **Single Source of Truth**: One phase system, not two
2. **Clear Ownership**: Phase manager owns all phase-dependent behavior
3. **Predictable Transitions**: Well-defined criteria, logged transitions
4. **Testable**: Can mock phase manager to test each mode
5. **Observable**: Phase visible in logs, status files, web UI
6. **Extensible**: Easy to add new phase-dependent behaviors

## Testing Strategy

```python
def test_bootstrap_phase():
    """Test bootstrap phase behavior."""
    manager = OperationalPhaseManager()
    assert manager.phase == OperationalPhase.BOOTSTRAP
    assert manager.get_search_window_ms('WWV') == 500.0
    assert manager.get_discrimination_weight('timing_validation') == 0.0
    assert not manager.should_use_multi_channel_extraction()

def test_refinement_phase():
    """Test refinement phase behavior."""
    manager = OperationalPhaseManager()
    manager._transition_to_refinement()
    assert manager.phase == OperationalPhase.REFINEMENT
    assert manager.get_search_window_ms('WWV') == 5.0
    assert manager.get_discrimination_weight('timing_validation') == 8.0
    assert not manager.should_use_multi_channel_extraction()

def test_measurement_phase():
    """Test measurement phase behavior."""
    manager = OperationalPhaseManager()
    manager._transition_to_measurement()
    assert manager.phase == OperationalPhase.MEASUREMENT
    assert manager.get_search_window_ms('WWV') == 1.0
    assert manager.get_discrimination_weight('timing_validation') == 12.0
    assert manager.should_use_multi_channel_extraction()
```

---

**Date**: 2026-01-12  
**Status**: Design complete, implementation pending  
**Next Action**: Implement `OperationalPhaseManager` and integrate into existing systems
