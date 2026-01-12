# Multi-Channel Measurement Architecture

## Core Principle

**The system is not a discriminator - it's an ionospheric observatory measuring 17 independent propagation paths.**

## Architectural Evolution

### Current State (Pre-2026-01-12)
```
Signal arrives → Discriminate: "Is this WWV or WWVH?" → Measure winner
```

**Problem**: Treats SHARED frequencies as a single-station problem requiring discrimination.

### Target State (Post-Implementation)
```
Signal arrives → Extract WWV at delay_WWV → Measure WWV path
              → Extract WWVH at delay_WWVH → Measure WWVH path
              → Extract BPM at delay_BPM → Measure BPM path
```

**Solution**: Treat SHARED frequencies as multi-station measurement problem using temporal separation.

## Three-Phase System Evolution

### Phase 1: BOOTSTRAP (Minutes 0-10)
**Purpose**: Learn propagation delays for each station

**Challenge**: Don't know delays yet, signals overlap in uncertainty window

**Method**: Use discrimination to identify signals
- Ground truth minutes (14/hour) provide definitive station ID
- BPM tick duration (10/100ms vs 5ms) discriminates BPM
- Build delay models: `station_frequency → mean_delay ± std`

**Output**: 
```
WWV_10MHz: 3.2 ± 2.0 ms (n=8 measurements)
WWVH_10MHz: 24.8 ± 3.0 ms (n=6 measurements)
BPM_10MHz: 45.0 ± 5.0 ms (n=4 measurements)
```

### Phase 2: TEMPORAL SEPARATION (Minutes 10-30)
**Purpose**: Transition from discrimination to independent measurement

**Key Insight**: Once delays are known with ±2ms accuracy, temporal windows don't overlap:
```
WWV:  [1.2 ms,  5.2 ms]  ← 3.2 ± 2.0 ms
WWVH: [21.8 ms, 27.8 ms] ← 24.8 ± 3.0 ms (no overlap!)
BPM:  [40.0 ms, 50.0 ms] ← 45.0 ± 5.0 ms (no overlap!)
```

**Method**: Extract each station's signal from its temporal window
```python
# Not "which station is dominant?"
if dominant_station == 'WWV':
    measure_wwv()

# But "measure all stations independently"
wwv_iq = extract_window(iq_samples, wwv_window)
wwvh_iq = extract_window(iq_samples, wwvh_window)
bpm_iq = extract_window(iq_samples, bpm_window)

wwv_metrics = analyze(wwv_iq)    # Colorado → Missouri path
wwvh_metrics = analyze(wwvh_iq)  # Hawaii → Missouri path
bpm_metrics = analyze(bpm_iq)    # China → Missouri path
```

**Output**: Independent metrics for each broadcast
```json
{
  "WWV_10MHz": {
    "snr_db": 25.3,
    "doppler_hz": 0.12,
    "delay_spread_ms": 1.2,
    "phase_variance_rad2": 0.08,
    "detected": true
  },
  "WWVH_10MHz": {
    "snr_db": 18.7,
    "doppler_hz": -0.34,
    "delay_spread_ms": 3.5,
    "phase_variance_rad2": 0.22,
    "detected": true
  },
  "BPM_10MHz": {
    "snr_db": -5.2,
    "detected": false
  }
}
```

### Phase 3: IONOSPHERIC SCIENCE (Minutes 30+)
**Purpose**: Measure ionospheric phenomena, not timing errors

**Key Insight**: With sub-millisecond timing accuracy, variations describe physics:
- Timing jitter → TEC fluctuations
- Phase variance → Ionospheric turbulence
- Doppler shift → Layer movement
- Delay spread → Multipath structure

**Method**: Narrow windows (±1ms), high-precision measurement
```python
# Windows narrow as confidence improves
wwv_window = [2.2 ms, 4.2 ms]     # 3.2 ± 1.0 ms
wwvh_window = [23.8 ms, 25.8 ms]  # 24.8 ± 1.0 ms

# Measure with sub-ms precision
wwv_toa = measure_precise_toa(wwv_iq)  # 3.247 ms
wwvh_toa = measure_precise_toa(wwvh_iq)  # 24.831 ms

# Variations are ionospheric, not measurement errors
delta_toa = wwv_toa - expected_wwv_toa  # +0.047 ms
# → Indicates ionospheric delay increase (TEC change)
```

## The 17 Broadcasts as Ionospheric Probes

Each broadcast provides unique propagation information:

### Short Paths (1-2 hops)
- **WWV 20/25 MHz**: ~629 km, 1-hop E/F layer
- **CHU 3.33/7.85/14.67 MHz**: ~1522 km, 1-2 hop F layer

### Medium Paths (2-3 hops)
- **WWV 2.5/5/10/15 MHz**: ~629 km, 2-3 hop F layer
- **WWVH 2.5/5/10/15 MHz**: ~6093 km, 2-3 hop F layer

### Long Paths (3-4 hops)
- **BPM 2.5/5/10/15 MHz**: ~11318 km, 3-4 hop trans-Pacific

### Scientific Questions Each Path Answers

**WWV (Colorado)**:
- Short continental path
- Diurnal ionospheric variations
- E/F layer transitions
- Solar flare effects

**WWVH (Hawaii)**:
- Trans-oceanic path
- Different ionospheric conditions (tropical)
- Comparison with WWV (same frequency, different path)
- Geomagnetic storm effects

**BPM (China)**:
- Trans-Pacific propagation
- Multiple ionospheric regions
- Long-path multipath
- Auroral zone effects (high-latitude path)

**CHU (Canada)**:
- Mid-latitude path
- Auroral zone proximity
- FSK modulation (different physics)
- Comparison with WWV (similar distance, different direction)

## Implementation Strategy

### Current Implementation (2026-01-12)
✅ `timing_discrimination.py`: Bootstrap discrimination with delay learning
✅ `wwvh_discrimination.py`: Timing validation vote integrated
✅ Ground truth schedule management
✅ State persistence

### Next Implementation Phase
1. **Multi-channel extraction** (new module: `multi_channel_extractor.py`)
   - Extract IQ samples from non-overlapping temporal windows
   - One window per station-frequency pair
   - Return dict of station → IQ samples

2. **Independent measurement** (modify existing analyzers)
   - Remove "dominant station" concept
   - Measure ALL stations on SHARED frequencies
   - Output metrics for each detected broadcast

3. **Data model changes**
   - Replace `DiscriminationResult` (single station) 
   - With `MultiChannelMeasurement` (all stations)
   - Schema: `{station_frequency: metrics}` not `{dominant_station: metrics}`

4. **Archive format**
   - Store all 17 broadcasts independently
   - Each with its own time series
   - Enable multi-path ionospheric analysis

## Discrimination vs Measurement

### When Discrimination is Needed
- **Bootstrap phase only** (first 10 minutes)
- **Ground truth learning** (identify signals to build delay models)
- **Fallback mode** (if timing degrades below ±5ms)

### When Discrimination is NOT Needed
- **After bootstrap** (delays known with ±2ms accuracy)
- **Normal operation** (temporal separation sufficient)
- **Science mode** (measuring all paths independently)

## Key Architectural Principle

> **Discrimination is a bootstrap tool, not a measurement strategy.**
> 
> Once timing is established, the system measures all 17 broadcasts as independent ionospheric probes. The question is not "which station is this?" but "what is each station telling us about its propagation path?"

## Astronomical Analogy

The system's evolution parallels astrophotography:

**Telescope Setup (Bootstrap)**:
1. **Orient**: Point telescope at approximate target location
2. **Focus**: Adjust focus until stars are sharp points
3. **Calibrate**: Align sidereal drive with Earth's rotation

**Long Exposure (Measurement)**:
1. **Track**: Sidereal drive keeps telescope aligned
2. **Stack**: Multiple exposures accumulated
3. **Measure**: Extract data from each star independently

### Radio Observatory Parallel

**Bootstrap Phase** (Orient & Focus):
- Use ground truth signals to "find" each station
- Adjust timing until delays are sharp (±1ms)
- Calibrate GPSDO-to-UTC alignment

**Measurement Phase** (Track & Stack):
- Timing system "tracks" each station at its known delay
- Accumulate measurements over time
- Extract independent metrics from each broadcast

**Key Insight**: Once aligned, the telescope doesn't need to "discriminate" between stars - it knows where each one is and measures them all simultaneously. Similarly, once timing is calibrated, the system doesn't discriminate between stations - it knows when each arrives and measures them all independently.

The GPSDO is the "sidereal drive" - maintaining stable timing so the system stays aligned with the broadcasts even as ionospheric conditions change (like Earth's rotation changing star positions).

## Benefits of Multi-Channel Measurement

1. **No information loss**: Measure all stations, not just "winner"
2. **Path comparison**: WWV vs WWVH on same frequency reveals ionospheric differences
3. **Interference study**: "Ghost" signals become measurable phenomena
4. **Robust to fading**: One station fades, others still measured
5. **Scientific value**: 17 independent propagation paths = rich dataset

## Analogy: Radio Telescope Array

Traditional approach: "Which antenna has the strongest signal?"
Multi-channel approach: "What is each antenna seeing?"

The system is not choosing between stations - it's measuring a multi-path ionospheric channel with 17 independent probes.

---

**Date**: 2026-01-12  
**Status**: Architectural principle established, implementation pending  
**Next Session**: Implement multi-channel extraction and independent measurement
