# Simplified Discrimination Architecture - Physics-Based Approach

## Core Principle

**Use the simplest sufficient method at each phase. Don't guess when you can't discriminate with certainty.**

## Problem with Weighted Voting

The current weighted voting system is **legacy complexity** that:
- Tries to guess station identity from weak heuristics (signal strength, BCD correlation)
- Combines multiple uncertain signals hoping to get a better guess
- Was necessary before timing accuracy was established
- Becomes unnecessary once physics-based discrimination is available

## Physics-Based Discrimination

### Unambiguous Signals (No Discrimination Needed)

**Anchor Channels** (unique frequencies):
- CHU: 3.33, 7.85, 14.67 MHz → always CHU
- WWV: 20, 25 MHz → always WWV

**Unique Modulation**:
- 1200 Hz tone → always WWVH (only station using this frequency)
- FSK modulation → always CHU (only station using FSK)

**Effectively Unambiguous**:
- 1000 Hz tone with no 1200 Hz detected → WWV (BPM typically not receivable)

### Geographic Constraints (Timing-Based)

Once global RTP offset is established (from anchor channels):

**Deterministic arrival order**:
```
WWV (Colorado, ~1120 km):   3-10 ms
WWVH (Hawaii, ~6600 km):    20-40 ms
BPM (China, ~11500 km):     35-55 ms
```

**Non-overlapping windows** (with ±2ms timing):
```
WWV:  [1 ms,  12 ms]
WWVH: [18 ms, 42 ms]  ← 6ms gap from WWV
BPM:  [33 ms, 57 ms]  ← overlaps WWVH, but 21ms from WWV
```

## Three-Phase Discrimination Strategy

### BOOTSTRAP Phase (0-10 min)

**Objective**: Establish global RTP offset from unambiguous signals

**Method**: Only process signals that are unambiguous by frequency or modulation

```python
def identify_station_bootstrap(frequency_mhz, detections):
    """
    Identify station during bootstrap using only unambiguous signals.
    
    Returns:
        (station, confidence) or (None, 0.0) if ambiguous
    """
    # Anchor channels (unique frequencies)
    if frequency_mhz in [3.33, 7.85, 14.67]:
        return ('CHU', 1.0)
    
    if frequency_mhz in [20.0, 25.0]:
        return ('WWV', 1.0)
    
    # Shared frequencies - use modulation to discriminate
    if frequency_mhz in [2.5, 5.0, 10.0, 15.0]:
        # WWVH 1200 Hz tone is unambiguous
        if detections.has_1200hz_tone:
            return ('WWVH', 1.0)
        
        # WWV 1000 Hz tone (if no 1200 Hz)
        if detections.has_1000hz_tone and not detections.has_1200hz_tone:
            return ('WWV', 0.9)  # High confidence, but BPM possible
        
        # Ambiguous - don't guess
        return (None, 0.0)
    
    return (None, 0.0)
```

**Key insight**: If we can't identify with certainty, **skip the measurement**. Better to have fewer high-quality measurements than many uncertain guesses.

### REFINEMENT Phase (10-30 min)

**Objective**: Use timing to validate station assignments

**Method**: Check if detection timing matches expected delay for claimed station

```python
def identify_station_refinement(
    frequency_mhz,
    detections,
    measured_delay_ms,
    station_delay_models
):
    """
    Identify station using timing validation.
    
    Returns:
        (station, confidence) or (None, 0.0) if invalid
    """
    # First check if unambiguous by frequency/modulation
    station, conf = identify_station_bootstrap(frequency_mhz, detections)
    if station is not None:
        # Validate timing matches expected delay
        expected_delay = station_delay_models[station].mean_delay_ms
        window = station_delay_models[station].get_validation_window_ms()
        
        if abs(measured_delay_ms - expected_delay) < window:
            return (station, 1.0)  # Confirmed by timing
        else:
            return (None, 0.0)  # Timing mismatch - reject
    
    # Shared frequency - use timing to discriminate
    if frequency_mhz in [2.5, 5.0, 10.0, 15.0]:
        # Check which station's timing window this falls into
        for station_name in ['WWV', 'WWVH', 'BPM']:
            if station_name not in station_delay_models:
                continue
            
            model = station_delay_models[station_name]
            expected = model.mean_delay_ms
            window = model.get_validation_window_ms()
            
            if abs(measured_delay_ms - expected) < window:
                return (station_name, 0.95)  # Timing-based identification
        
        # Doesn't match any known station timing
        return (None, 0.0)
    
    return (None, 0.0)
```

**Key insight**: Timing validation is **physics-based rejection**, not heuristic voting. A signal claiming to be WWVH but arriving at WWV timing is **physically impossible**.

### MEASUREMENT Phase (30+ min)

**Objective**: Extract and measure all stations independently

**Method**: No discrimination needed - temporal separation sufficient

```python
def extract_all_stations_measurement(
    iq_samples,
    station_delay_models,
    sample_rate
):
    """
    Extract IQ samples for all stations from temporal windows.
    
    Returns:
        {station: iq_window} for each station
    """
    channels = {}
    
    for station, model in station_delay_models.items():
        # Calculate temporal window
        delay_ms = model.mean_delay_ms
        window_width_ms = 2.0  # ±1ms
        
        center_sample = int(delay_ms * sample_rate / 1000)
        half_width = int(window_width_ms * sample_rate / 1000)
        
        start = center_sample - half_width
        end = center_sample + half_width
        
        # Extract window
        if 0 <= start < len(iq_samples) and end <= len(iq_samples):
            channels[station] = iq_samples[start:end]
    
    return channels
```

**Key insight**: In MEASUREMENT phase, we're not asking "which station is this?" but rather "what is each station telling us about its propagation path?"

## Comparison: Old vs New

### Old Approach (Weighted Voting)

```python
# Accumulate votes from multiple methods
wwv_score = 0.0
wwvh_score = 0.0

# Vote 1: Carrier power (weight: 10.0)
if wwv_power > wwvh_power:
    wwv_score += 10.0
else:
    wwvh_score += 10.0

# Vote 2: BCD correlation (weight: 8.0)
if wwv_bcd_amp > wwvh_bcd_amp:
    wwv_score += 8.0
else:
    wwvh_score += 8.0

# Vote 3: Timing validation (weight: 12.0)
if wwv_timing_valid:
    wwv_score += 12.0
if wwvh_timing_valid:
    wwvh_score += 12.0

# ... 10+ more votes

# Winner takes all
station = 'WWV' if wwv_score > wwvh_score else 'WWVH'
```

**Problems**:
- Combines certain knowledge (timing) with uncertain guesses (power)
- Can override physics with signal strength
- Complex, hard to debug
- No clear confidence metric

### New Approach (Physics-Based)

```python
# Decision tree: use simplest sufficient method
if phase == BOOTSTRAP:
    # Only unambiguous signals
    if frequency in ANCHOR_FREQUENCIES:
        station = get_anchor_station(frequency)
    elif has_1200hz_tone:
        station = 'WWVH'
    elif has_1000hz_tone and not has_1200hz_tone:
        station = 'WWV'
    else:
        station = None  # Skip ambiguous measurements

elif phase == REFINEMENT:
    # Timing validation
    station = get_station_from_timing(measured_delay, delay_models)
    if station and not timing_validates(station, measured_delay):
        station = None  # Reject physically impossible

else:  # MEASUREMENT
    # Extract all stations
    stations = extract_all_stations(iq_samples, delay_models)
    # Measure each independently
```

**Benefits**:
- Simple, deterministic logic
- Physics-based (can't override with heuristics)
- Clear confidence (certain, probable, or unknown)
- Easy to debug and validate

## Implementation Plan

### 1. Create New Module: `station_identifier.py`

Replace `wwvh_discrimination.py` with simpler physics-based identification:

```python
class StationIdentifier:
    """
    Physics-based station identification.
    
    Uses simplest sufficient method:
    - BOOTSTRAP: Unambiguous signals only
    - REFINEMENT: Timing validation
    - MEASUREMENT: Multi-channel extraction
    """
    
    def __init__(self, operational_phase_manager):
        self.phase_manager = operational_phase_manager
        self.station_delay_models = {}
    
    def identify(self, frequency_mhz, detections, measured_delay_ms):
        """Identify station using phase-appropriate method."""
        phase = self.phase_manager.get_phase()
        
        if phase == OperationalPhase.BOOTSTRAP:
            return self._identify_bootstrap(frequency_mhz, detections)
        elif phase == OperationalPhase.REFINEMENT:
            return self._identify_refinement(frequency_mhz, detections, measured_delay_ms)
        else:  # MEASUREMENT
            return self._identify_measurement(frequency_mhz, measured_delay_ms)
```

### 2. Remove Weighted Voting

**Delete or deprecate**:
- `wwvh_discrimination.py` (650+ lines of voting logic)
- Vote weight calculations
- Score accumulation
- "Agreement" tracking

**Keep**:
- BCD correlation analysis (for propagation study, not discrimination)
- Test signal analysis (for channel characterization)
- Phase coherence (for quality metrics)

### 3. Simplify `timing_discrimination.py`

Current: 582 lines with phase tracking, vote weights, etc.

New: ~200 lines focused on timing validation only:
- Validate detection timing against expected delay
- Reject physically impossible detections
- Update delay models

### 4. Update `phase2_temporal_engine.py`

Replace discrimination step with identification:

```python
# Old (Step 2C)
discrimination_result = self.discriminator.discriminate(
    tone_detections, bcd_result, test_signal, ...
)

# New
station_id = self.identifier.identify(
    frequency_mhz=self.frequency_hz / 1e6,
    detections=tone_detections,
    measured_delay_ms=time_snap.timing_error_ms
)

if station_id.station is None:
    # Ambiguous - skip this measurement
    logger.debug(f"Skipping ambiguous measurement: {station_id.reason}")
    return None
```

## Benefits of Simplification

**Code reduction**:
- Remove ~800 lines of voting logic
- Simplify to ~300 lines of physics-based identification
- **Net: -500 lines of complex code**

**Methodological clarity**:
- No heuristic voting
- Physics-based decisions
- Clear confidence metrics
- Easier to validate scientifically

**Operational benefits**:
- Fewer false positives (reject ambiguous instead of guessing)
- Higher confidence in measurements
- Easier to debug (deterministic logic)
- Clearer transition to multi-channel measurement

## Migration Path

**Phase 1**: Create new `station_identifier.py` alongside existing code
**Phase 2**: Update `phase2_temporal_engine.py` to use new identifier
**Phase 3**: Deprecate `wwvh_discrimination.py`
**Phase 4**: Remove weighted voting code

---

**Date**: 2026-01-12  
**Status**: Design complete, implementation next  
**Impact**: Simplifies discrimination, removes ~500 lines of legacy code
