# Arrival Pattern Matrix Architecture

**Version:** 1.0  
**Date:** 2026-01-29  
**Status:** Implemented (v6.11) — central to the unified measurement path; classes/methods live in `core/arrival_pattern_matrix.py`

---

## Executive Summary

This document describes a fundamental architectural change: **validation against physics, not history**.

The `ArrivalPatternMatrix` pre-computes expected tone arrival times based on:
- Geography (fixed)
- Frequency (known per channel)
- UTC time (from GPSDO)
- IRI-2020 ionospheric model (real-time)

This eliminates dependence on historical measurements for operational decisions, preventing contamination from stale calibration data.

---

## The Problem

### Current Architecture Vulnerabilities

1. **Historical Contamination**: Calibration offsets persist across sessions, potentially trapping the system in incorrect state

2. **Circular Dependencies**: Bootstrap uses NTP → Chrony tracks TSL → TSL uses bootstrap offset

3. **Stale Data Propagation**: L1/L2 HDF5 files contain old measurements that fusion continues to read

4. **Pattern Validation Gap**: Bootstrap validates 60-second recurrence, but metrology/fusion don't enforce the same rigor

### Root Cause

The current architecture validates detections against **what we measured before** rather than **what physics predicts**.

---

## The Solution

### Pre-Computed Arrival Pattern Matrix

Before the radio starts, compute expected arrivals for all (station, frequency) pairs:

```
┌─────────────────────────────────────────────────────────────────────┐
│                    ARRIVAL PATTERN MATRIX                           │
│                                                                     │
│  Inputs (all deterministic):                                        │
│    • Receiver location (lat/lon)                                    │
│    • Station locations (WWV, WWVH, CHU, BPM)                        │
│    • Great circle distances                                         │
│    • IRI-2020 ionospheric model                                     │
│    • Current UTC time                                               │
│                                                                     │
│  Output per (station, frequency):                                   │
│    • expected_delay_ms: Propagation delay                           │
│    • expected_sample: Samples from minute boundary                  │
│    • search_window: ±3σ bounds for detection                        │
│                                                                     │
│  Update cadence: Every minute (tracks diurnal ionosphere)           │
└─────────────────────────────────────────────────────────────────────┘
```

### Validation Flow

```
┌─────────────────────────────────────────────────────────────────────┐
│                         DETECTION                                    │
│                                                                     │
│  For each minute:                                                   │
│    1. Get search windows from matrix                                │
│    2. Search for tones ONLY within those windows                    │
│    3. Validate: detection within ±3σ of prediction?                 │
│       • YES → Accept, compute timing error                          │
│       • NO  → Reject immediately (outlier)                          │
│    4. If nothing detected → propagation issue, not timing issue     │
│                                                                     │
│  NO historical data consulted. Each minute is fresh from physics.   │
└─────────────────────────────────────────────────────────────────────┘
```

### Time Authority Model

| Phase | Time Authority | Purpose |
|-------|---------------|---------|
| **Startup** | GPSDO | Provides stable ADC sample clock (frequency only, no absolute time) |
| **Bootstrap** | NTP from GPS server | Identifies which UTC minute we're in |
| **Locked** | HF tone arrivals | Ongoing time reference, validated against matrix |
| **Output** | TSL1/TSL2 → Chrony | Sets system clock for other programs |

**Hardware Architecture:**
```
┌─────────────────────────────────────────────────────────────────────┐
│  RX888 + GPSDO                                                      │
│  - Provides stable ADC sample clock (RTP timestamps)                │
│  - Frequency reference only — does NOT provide absolute time        │
│  - [Future option: PPS injection for absolute sample alignment]     │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  HF-TimeStd System                                                  │
│  - Bootstrap: Uses NTP (from GPS server) for initial minute ID     │
│  - Locked: HF tone arrivals become time authority                   │
│  - Outputs: TSL1/TSL2 to Chrony → sets system clock                 │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  GPS Time Server (192.168.0.202)                                    │
│  - PPS + UBX → gpsd                                                 │
│  - Provides NTP for initial bootstrap orientation                   │
│  - Provides GNSS VTEC data for TEC/ionospheric calculations         │
│  - Independent reference for validation (not same as GPSDO)         │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Client Programs                                                    │
│  - Use system clock (set by TSL1/TSL2 via Chrony)                   │
│  - Start 1-minute recordings aligned to UTC                         │
└─────────────────────────────────────────────────────────────────────┘
```

**Future: PPS Injection Option**

A future hardware modification could inject PPS into the HF stream:
- Provides absolute sample-to-UTC alignment at the ADC level
- Eliminates need for NTP-based bootstrap orientation
- Could achieve sub-millisecond minute boundary alignment
- Would be configurable based on local hardware setup

**Critical**: NTP is used ONCE for initial orientation. After lock, the system derives time from HF signals validated against physics predictions. The HF-derived time then sets the system clock for other programs.

---

## What Historical Data Is Actually Needed?

| Data Type | Current Use | Proposed Use |
|-----------|-------------|--------------|
| **Bootstrap state** | Persist lock across restarts | **Eliminate** — recompute from GPSDO + NTP + matrix |
| **Calibration offsets** | Per-broadcast bias correction | **Eliminate** — IRI-2020 replaces learned offsets |
| **L1/L2 HDF5** | Fusion reads for D_clock | **Archive only** — not for operational decisions |
| **Gap metadata** | Track data quality | **Keep** — for post-hoc analysis |
| **RTP timestamps** | Primary time basis | **Keep** — this IS the data |

---

## Implementation

### New Component

`src/hf_timestd/core/arrival_pattern_matrix.py`

```python
class ArrivalPatternMatrix:
    """Physics-based expected arrival predictions."""
    
    def __init__(self, receiver_lat, receiver_lon, sample_rate=24000):
        # Pre-compute great circle distances (fixed)
        # Initialize IRI-2020 model
    
    def compute_matrix(self, utc_time) -> ArrivalMatrix:
        # For each (station, frequency):
        #   1. Get ionospheric height from IRI-2020
        #   2. Compute propagation delay
        #   3. Convert to expected sample offset
        #   4. Define ±3σ search window
    
    def validate_detection(self, station, freq, sample, snr) -> (bool, confidence, reason):
        # Check if detection falls within expected window
        # Return validity, confidence score, explanation
    
    def get_search_windows(self, frequency_mhz) -> Dict[station, (min, max)]:
        # Return search windows for tone detection
```

### Integration Points

1. **Bootstrap Service** (`bootstrap_service.py`)
   - Use `matrix.get_search_windows()` instead of historical offsets
   - Validate candidates with `matrix.validate_detection()`

2. **Tone Detector** (`tone_detector.py`)
   - Pass search windows from matrix to `_detect_tones_internal()`
   - Reject detections outside matrix bounds

3. **Metrology Service** (`metrology_service.py`)
   - Validate each detection against matrix before writing to HDF5
   - No calibration offset persistence

4. **Fusion Service** (`multi_broadcast_fusion.py`)
   - Remove `_load_calibration()` / `_save_calibration()`
   - Validate L1/L2 data against matrix before fusion
   - Reject measurements outside expected windows

---

## Expected Arrivals (Example: Columbia, MO)

From `ArrivalPatternMatrix` with IRI-2020:

| Station | Distance | 10 MHz Delay | Search Window |
|---------|----------|--------------|---------------|
| WWV | 1,200 km | 4.0 ms | 0-19 ms |
| WWVH | 6,200 km | 22.5 ms | 7.5-37.5 ms |
| CHU | 1,600 km | 5.3 ms | 0-20 ms |
| BPM | 11,700 km | 39.0 ms | 24-54 ms |

These windows are computed fresh each minute from physics, not from historical measurements.

---

## Benefits

1. **No Historical Contamination**: Each minute starts fresh from physics
2. **Immediate Outlier Rejection**: Detections outside ±3σ are rejected without consulting history
3. **Simplified Bootstrap**: Just find detections matching matrix, lock immediately
4. **Eliminated Circular Dependencies**: No calibration persistence to corrupt
5. **Transparent Validation**: Every detection has a physics-based reason for acceptance/rejection

---

## Migration Path

### Phase 1: Add Matrix (Current)
- ✅ Create `ArrivalPatternMatrix` class
- ✅ Integrate IRI-2020 ionospheric model
- ✅ Test matrix predictions

### Phase 2: Integrate with Detection
- [ ] Modify `tone_detector.py` to use matrix search windows
- [ ] Modify `bootstrap_service.py` to validate against matrix
- [ ] Add matrix validation to `metrology_service.py`

### Phase 3: Remove Historical Dependencies
- [ ] Remove calibration persistence from fusion
- [ ] Remove bootstrap state persistence
- [ ] Add "valid_from" epoch to reject pre-restart data

### Phase 4: Documentation
- [ ] Update `BOOTSTRAP_METHODOLOGY.md` with matrix architecture
- [ ] Update `ARCHITECTURE.md` with physics-based validation
- [ ] Add Living Documentation evidence for matrix predictions

---

## Verification

The matrix can be verified by comparing predictions to actual detections:

```bash
# Log matrix predictions vs actual detections
grep "matrix.validate_detection" /var/log/hf-timestd/*.log

# Expected: Most detections within 1-2σ of predictions
# Outliers (>3σ) should be rare and indicate propagation anomalies
```

---

## References

1. IRI-2020: Bilitza et al. (2022), "International Reference Ionosphere 2020"
2. ITU-R P.1239-3: "ITU-R Reference Ionospheric Characteristics"
3. Davies, K. (1990), "Ionospheric Radio", Chapter 4
