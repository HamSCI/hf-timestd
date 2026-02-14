# Phase 2 Analytics Clarity and Rigor Plan

**Author:** AC0G / Claude  
**Created:** 2025-12-07  
**Status:** Planning

---

## Objective

Make the Phase 2 analytics code:
1. **Clear and readable** - Any reader can understand the logic
2. **Thoroughly documented** - Theory, equations, assumptions explicit
3. **Mathematically sound** - Correct signal processing and statistics
4. **Theoretically rigorous** - Grounded in HF propagation physics
5. **Consistent** - Same patterns and conventions throughout

## Three Main Goals of Phase 2 Analytics

### Goal 1: UTC(NIST) Estimation
**Objective:** Establish the best possible estimate of UTC(NIST) from passive HF reception.

**Key Equation:**
```
D_clock = T_system - T_UTC(NIST)
        = T_arrival - T_propagation - T_emission
```

Where:
- `T_arrival` = Detected arrival time of timing tone (measured)
- `T_propagation` = Ionospheric propagation delay (calculated from mode)
- `T_emission` = Known transmission time at WWV/WWVH (0 at top of second)

**Key Modules:**
| Module | Purpose | Status |
|--------|---------|--------|
| `tone_detector.py` | Matched filter detection of 1000/1200 Hz tones | Needs theoretical documentation |
| `transmission_time_solver.py` | Propagation mode identification | Has good physics documentation |
| `clock_offset_series.py` | D_clock computation and storage | Needs cleanup |
| `clock_convergence.py` | Statistical convergence model | Well documented |
| `multi_broadcast_fusion.py` | Weighted fusion of 13 broadcasts | Needs theoretical basis |

**Theoretical References:**
- ITU-R P.533: HF propagation prediction method
- CCIR Rep. 894: Sky-wave propagation delays
- Nickisch & Franke (1990): HF propagation physics

### Goal 2: WWV/WWVH Discrimination
**Objective:** Distinguish between WWV (Fort Collins, CO) and WWVH (Kauai, HI) on shared frequencies (2.5, 5, 10, 15 MHz).

**Physical Basis:**
- Different propagation paths → different delays
- BCD time code differs in seconds 0-59 (minute encoding)
- Exclusive broadcast minutes: 500 Hz (WWV) vs 600 Hz (WWVH)
- 440 Hz station ID: minute 2 (WWV) vs minute 1 (WWVH)

**Key Modules:**
| Module | Purpose | Status |
|--------|---------|--------|
| `wwvh_discrimination.py` | Main discrimination (173 KB!) | Needs major cleanup/refactor |
| `wwv_bcd_encoder.py` | BCD template generation | Well structured |
| `global_station_voter.py` | Voting combiner | Needs documentation |
| `wwv_geographic_predictor.py` | Expected delay calculation | Good |

**Discrimination Methods (Current):**
1. Geographic prediction (expected delay difference)
2. BCD correlation (template matching)
3. Carrier power ratio (SNR comparison)
4. 500/600 Hz exclusive tones (ground truth)
5. 440 Hz station ID (ground truth)
6. Differential delay measurement

**Theoretical References:**
- NIST Special Publication 250-67: WWV/WWVH time and frequency services
- ITU-R TF.768: Standard frequencies and time signals

### Goal 3: Decimation (20 kHz → 10 Hz)
**Objective:** Produce the highest-quality decimated IQ data for upload to GRAPE/HamSCI/PSWS.

**Design Requirements:**
- Preserve ±5 Hz Doppler information (ionospheric science)
- Flat passband (< 0.1 dB ripple in 0-5 Hz)
- High stopband rejection (> 90 dB) to prevent aliasing
- Phase-linear (preserve pulse shapes)

**Current Implementation:**
```
20 kHz → CIC(R=50) → 400 Hz → Comp FIR → Final FIR(R=40) → 10 Hz
```

**Key Modules:**
| Module | Purpose | Status |
|--------|---------|--------|
| `decimation.py` | 3-stage filter design | Well documented |
| `decimated_buffer.py` | Binary storage format | Clean |
| `phase2_analytics_service.py` | Integration | Needs cleanup |

**Theoretical References:**
- Hogenauer (1981): CIC filter design
- Lyons (2010): Understanding Digital Signal Processing, Ch. 10

---

## Proposed Documentation Standard

### Module Header Template
```python
#!/usr/bin/env python3
"""
Module Name - One-Line Description

PURPOSE:
    Clear statement of what this module accomplishes.

THEORY:
    Physical/mathematical basis for the approach.
    Key equations with variable definitions.
    Assumptions and their validity ranges.

REFERENCES:
    [1] Author (Year): Title. Journal/Source.
    [2] ...

INPUTS:
    Description of expected input data.

OUTPUTS:
    Description of produced outputs.

USAGE:
    Example code showing typical usage.

REVISION HISTORY:
    2025-12-07: Initial documentation pass
"""
```

### Function Documentation Template
```python
def function_name(param1: type, param2: type) -> ReturnType:
    """
    One-line description of what this function does.
    
    THEORY:
        Mathematical basis if applicable.
        Key equation: y = f(x) where x is... and y is...
    
    Args:
        param1: Description including units if applicable
        param2: Description including valid range
    
    Returns:
        Description of return value and its meaning
    
    Raises:
        ValueError: When parameters are out of valid range
    
    Note:
        Implementation notes, edge cases, or gotchas.
    
    References:
        [1] Relevant citation if applicable
    """
```

---

## Cleanup Priority Order

### Phase 1: Core Theory Documentation (High Priority)
1. [x] `tone_detector.py` - Matched filter theory ✅ 2025-12-07
2. [x] `transmission_time_solver.py` - Mode disambiguation ✅ 2025-12-07
3. [x] `decimation.py` - Filter design rationale ✅ 2025-12-07
4. [x] `wwvh_discrimination.py` - Multi-method weighted voting ✅ 2025-12-07

### Phase 2: Integration Documentation (Medium Priority)
1. [x] `phase2_temporal_engine.py` - Pipeline overview ✅ 2025-12-07
2. [x] `phase2_analytics_service.py` - Service integration ✅ 2025-12-07
3. [x] `clock_offset_series.py` - D_clock data structures ✅ 2025-12-07
4. [x] `multi_broadcast_fusion.py` - Fusion theory ✅ 2025-12-07

### Phase 3: Supporting Modules (Lower Priority)
1. [x] `wwv_bcd_encoder.py` - BCD format ✅ 2025-12-07
2. [x] `wwv_constants.py` - Reference values ✅ 2025-12-07
3. [x] `wwv_geographic_predictor.py` - Path geometry ✅ 2025-12-07
4. [x] `clock_convergence.py` - Statistics ✅ 2025-12-07

---

## Known Issues to Address

### 1. `wwvh_discrimination.py` (173 KB)
**Problem:** Too large, hard to navigate, mixed concerns.

**Proposed Refactor:**
```
wwvh_discrimination.py (main coordinator, ~20 KB)
├── bcd_correlator.py (BCD correlation logic)
├── tone_discriminators.py (500/600/440 Hz detection)
├── carrier_power_analyzer.py (SNR-based discrimination)
└── voting_combiner.py (weighted voting)
```

### 2. Inconsistent Signal Normalization
**Problem:** Some modules normalize, some don't. AGC is off but amplitude varies.

**Solution:** Define clear normalization policy in one place, reference it everywhere.

### 3. Missing Unit Tests for Signal Processing
**Problem:** Complex DSP code without verification.

**Solution:** Add tests with known-answer test vectors.

---

## Session Workflow

Each session should:
1. Pick one module from priority list
2. Read and understand current implementation
3. Document theory and equations
4. Verify mathematical correctness
5. Add references where applicable
6. Test with synthetic data if possible
7. Update this plan with progress

---

## Progress Tracking

| Date | Module | Action | Notes |
|------|--------|--------|-------|
| 2025-12-07 | All | Initial survey | Created this plan |
| 2025-12-07 | `tone_detector.py` | Complete documentation | Added: module header with theory/equations/references, `_create_template()` theory, `_correlate_with_template()` algorithm docs, sub-sample interpolation explanation, noise estimation explanation |
| 2025-12-07 | `transmission_time_solver.py` | Complete documentation | Added: HF propagation theory with diagrams, ionospheric delay physics, mode disambiguation methodology, `_calculate_hop_path()` geometry, `_evaluate_mode_fit()` scoring algorithm, constants documentation with references |
| 2025-12-07 | `decimation.py` | Complete documentation | Added: multi-stage decimation theory, CIC filter theory with transfer function, compensation filter design, Kaiser window anti-aliasing filter, signal flow diagram, computational efficiency analysis |
| 2025-12-07 | `wwvh_discrimination.py` | Complete documentation | Added: discrimination challenge table, 8-method overview with weights, weighted voting algorithm, minute-specific weights, inter-method cross-validation, `compute_discrimination()` docs |
| 2025-12-07 | `phase2_temporal_engine.py` | Complete documentation | Added: architectural overview with ASCII flow diagram, D_clock equation derivation, 3-step process details, input/output specifications, quality grades |
| 2025-12-07 | `phase2_analytics_service.py` | Complete documentation | Added: service vs engine architecture diagram, data flow with output directory structure, CSV file table, clock convergence model explanation |
| 2025-12-07 | `clock_offset_series.py` | Complete documentation | Added: D_clock equation derivation, data structure descriptions, quality grade table, file format specifications, module relationship diagram |
| 2025-12-07 | `multi_broadcast_fusion.py` | Complete documentation | Added: broadcast structure table (13 broadcasts), weighted fusion equations, auto-calibration model with EMA, MAD outlier rejection, quality grading criteria |
| 2025-12-07 | `wwv_bcd_encoder.py` | Complete documentation | Added: IRIG-H format spec, pulse width encoding table, time code field layout (60 seconds), little-endian BCD explanation, 100 Hz modulation |
| 2025-12-07 | `wwv_constants.py` | Complete documentation | Added: station specs (WWV/WWVH/CHU), shared vs unique frequencies, ground truth minutes summary, propagation physics constants |
| 2025-12-07 | `wwv_geographic_predictor.py` | Complete documentation | Added: Haversine formula, ionospheric path geometry diagram, frequency-dependent layer heights, single/dual peak classification, Maidenhead conversion |
| 2025-12-07 | `clock_convergence.py` | Complete documentation | Added: "Set, Monitor, Intervention" philosophy, state machine diagram, Welford's algorithm, lock criterion, scientific output (residuals as propagation data) |
| | | | |

