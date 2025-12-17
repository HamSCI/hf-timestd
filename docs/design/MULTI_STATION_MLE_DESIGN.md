# Multi-Station Maximum Likelihood Estimation (MLE) Design

**Author:** Michael James Hauan (AC0G)  
**Date:** 2025-12-17  
**Status:** Design Document

---

## Executive Summary

This document describes a fundamental shift from **voting-based discrimination** to **Maximum Likelihood Estimation (MLE)** for multi-station detection on shared HF time standard frequencies. The key insight is that with BPM (China) now in the mix, we have a **three-way deconvolution problem** that cannot be solved by simple voting.

---

## The Problem: Three-Station Superposition

On shared frequencies (2.5, 5, 10, 15 MHz), the received signal is:

```
y(t) = Σ_station Σ_path A_{s,p} · x_s(t - τ_{s,p}) + n(t)
```

Where:
- `x_s(t)` = known template for station s (WWV, WWVH, BPM)
- `τ_{s,p}` = predicted ToA for station s via path p
- `A_{s,p}` = complex amplitude (path gain and phase)
- `n(t)` = noise

### Station Characteristics

| Station | Tone (Hz) | Tick Duration | Timing Offset | Distance from EM38 |
|---------|-----------|---------------|---------------|-------------------|
| WWV     | 1000      | 5 ms          | 0 ms (UTC)    | 1,119 km          |
| WWVH    | 1200      | 5 ms          | 0 ms (UTC)    | 6,600 km          |
| BPM     | 1000      | 10 ms (UTC), 100 ms (UT1) | **-20 ms** (advance) | 11,504 km |

### The BPM Challenge

BPM is "tricky" because:
1. **Same frequency as WWV** (1000 Hz) - cannot separate by tone frequency
2. **20 ms advance** - pulses emitted 20 ms BEFORE UTC second
3. **Long propagation** - ~38-50 ms delay from China to EM38
4. **Net arrival**: -20 ms + 45 ms ≈ **+25 ms after UTC** (overlaps with WWV!)

This means BPM and WWV ticks can arrive within milliseconds of each other, making simple power ratio discrimination unreliable.

---

## Solution: MLE-Based Component Decomposition

### Core Principle

Instead of detecting features and voting, run a **bank of correlators** centered on predicted ToA windows for each station. The output is a set of "likelihood peaks" that quantify the power contribution of each station simultaneously.

### Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ CORRELATOR BANK (per minute)                                                 │
│                                                                             │
│   IQ Samples ──┬──► WWV Correlator (1000 Hz, 5ms) ──► WWV_power, WWV_ToA    │
│                │    [search: expected_delay ± 10ms]                         │
│                │                                                             │
│                ├──► WWVH Correlator (1200 Hz, 5ms) ──► WWVH_power, WWVH_ToA │
│                │    [search: expected_delay ± 10ms]                         │
│                │                                                             │
│                └──► BPM Correlator (1000 Hz, 10ms) ──► BPM_power, BPM_ToA   │
│                     [search: expected_delay - 20ms ± 10ms]                  │
│                                                                             │
│   Output: ChannelAssignment {                                               │
│       wwv_component_power_db: float,                                        │
│       wwvh_component_power_db: float,                                       │
│       bpm_component_power_db: float,                                        │
│       residual_noise_db: float,                                             │
│       cross_validation_error_ms: float                                      │
│   }                                                                         │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Key Innovation: Predicted ToA Windows

Each station's correlator is centered on its **predicted ToA**, not a generic search window:

```python
# For receiver at EM38ww (38.918°N, 92.128°W)
PREDICTED_TOA_MS = {
    'WWV':  8.0,   # 1,119 km → ~3.7ms light + ~4ms ionospheric
    'WWVH': 35.0,  # 6,600 km → ~22ms light + ~13ms ionospheric
    'BPM':  25.0,  # 11,504 km → ~38ms light + ~7ms iono - 20ms advance
}

# Search windows (narrow after calibration)
SEARCH_WINDOW_MS = {
    'WWV':  10.0,  # ±10ms around predicted
    'WWVH': 15.0,  # ±15ms (longer path, more variability)
    'BPM':  10.0,  # ±10ms around predicted
}
```

---

## Exploiting BPM's Idiosyncratic Schedule

BPM has unique features that provide **calibration windows**:

### 1. UT1 Minutes (25-29, 55-59): 100 ms Pulses

During these minutes, BPM transmits 100 ms pulses (10× longer than UTC mode). These are **unambiguous BPM signatures**:

```python
def detect_bpm_ut1_pulses(iq_samples, sample_rate, minute):
    """
    UT1 pulses are 100ms long - easily distinguishable from WWV's 5ms.
    Use these to calibrate BPM path gain and delay.
    """
    if minute not in BPM_UT1_MINUTES:
        return None
    
    # Correlate with 100ms 1000 Hz template
    template = create_template(1000, duration_sec=0.100)
    correlation = matched_filter(iq_samples, template)
    
    # BPM UT1 pulses will have 10× higher energy than WWV ticks
    # This provides unambiguous BPM power measurement
    return extract_bpm_power_and_delay(correlation)
```

### 2. Pure Carrier Minutes (10-15, 40-45)

BPM transmits **pure carrier** (no time code) during these windows:

```python
BPM_PURE_CARRIER_MINUTES = set(range(10, 16)) | set(range(40, 46))

def measure_bpm_carrier_power(iq_samples, minute):
    """
    During pure carrier minutes, BPM's contribution is just the carrier.
    Measure this to establish BPM signal strength without modulation.
    """
    if minute not in BPM_PURE_CARRIER_MINUTES:
        return None
    
    # Narrowband power measurement at carrier frequency
    # No ticks to confuse with WWV
    return measure_carrier_power(iq_samples, bandwidth_hz=10)
```

### 3. The 20 ms Advance

BPM pulses are emitted 20 ms **before** UTC. Combined with propagation delay:

```
BPM_arrival = -20ms (advance) + propagation_delay
            = -20ms + 45ms (typical for EM38)
            = +25ms after UTC second

WWV_arrival = 0ms + propagation_delay
            = 0ms + 8ms (typical for EM38)
            = +8ms after UTC second

Separation: 25ms - 8ms = 17ms
```

This 17 ms separation is **exploitable** with sub-millisecond ToA resolution.

---

## StationModel Class Design

```python
@dataclass
class StationModel:
    """
    Physics-based model for a single time standard station.
    
    Each model defines:
    1. Signal template (tone frequency, duration)
    2. Timing characteristics (UTC offset, tick pattern)
    3. Confidence windows (minutes where station is alone or unique)
    4. Expected propagation delay (from receiver location)
    """
    station: str  # 'WWV', 'WWVH', 'BPM', 'CHU'
    
    # Signal characteristics
    tone_frequency_hz: float
    tick_duration_sec: float
    timing_offset_ms: float  # BPM = -20ms, others = 0
    
    # Receiver-specific
    expected_delay_ms: float
    delay_uncertainty_ms: float
    
    # Confidence windows (minutes where this station has unique features)
    calibration_minutes: Set[int]  # Minutes for unambiguous measurement
    ground_truth_minutes: Set[int]  # Minutes where station is alone
    
    def get_search_window(self, minute: int, calibrated: bool) -> Tuple[float, float]:
        """Return (center_ms, width_ms) for correlator search."""
        center = self.expected_delay_ms + self.timing_offset_ms
        width = 10.0 if calibrated else 50.0
        return center, width
    
    def create_template(self, sample_rate: int) -> np.ndarray:
        """Create matched filter template for this station."""
        pass


# Station model instances
WWV_MODEL = StationModel(
    station='WWV',
    tone_frequency_hz=1000,
    tick_duration_sec=0.005,
    timing_offset_ms=0.0,
    expected_delay_ms=8.0,  # EM38-specific
    delay_uncertainty_ms=5.0,
    calibration_minutes={8},  # Test signal minute
    ground_truth_minutes={8, 16, 17, 19},  # WWV-only minutes
)

WWVH_MODEL = StationModel(
    station='WWVH',
    tone_frequency_hz=1200,
    tick_duration_sec=0.005,
    timing_offset_ms=0.0,
    expected_delay_ms=35.0,
    delay_uncertainty_ms=10.0,
    calibration_minutes={44},  # Test signal minute
    ground_truth_minutes={44} | set(range(43, 52)),  # WWVH-only minutes
)

BPM_MODEL = StationModel(
    station='BPM',
    tone_frequency_hz=1000,
    tick_duration_sec=0.010,  # 10ms UTC, 100ms UT1
    timing_offset_ms=-20.0,  # 20ms advance
    expected_delay_ms=45.0,
    delay_uncertainty_ms=10.0,
    calibration_minutes=set(range(25, 30)) | set(range(55, 60)),  # UT1 minutes
    ground_truth_minutes=set(),  # BPM is never alone on shared frequencies
)
```

---

## ChannelAssignment Output

Replace `DiscriminationResult` with `ChannelAssignment`:

```python
@dataclass
class ChannelAssignment:
    """
    Component decomposition result for a shared channel.
    
    Instead of "dominant_station", we output power for ALL detected stations.
    This enables proper fusion weighting and cross-validation.
    """
    minute_timestamp: float
    channel: str
    frequency_mhz: float
    
    # Per-station power (dB relative to noise floor)
    wwv_component_power_db: Optional[float]
    wwvh_component_power_db: Optional[float]
    bpm_component_power_db: Optional[float]
    
    # Per-station ToA (ms from minute boundary)
    wwv_toa_ms: Optional[float]
    wwvh_toa_ms: Optional[float]
    bpm_toa_ms: Optional[float]
    
    # Per-station confidence (0-1)
    wwv_confidence: float
    wwvh_confidence: float
    bpm_confidence: float
    
    # Residual after component subtraction
    residual_noise_db: float
    
    # Cross-validation
    cross_validation_passed: bool
    cross_validation_error_ms: Optional[float]
    
    # BPM-specific
    bpm_timing_mode: str  # 'UTC' or 'UT1'
    bpm_usable_for_timing: bool
    
    def get_usable_stations(self) -> List[str]:
        """Return list of stations usable for timing."""
        usable = []
        if self.wwv_confidence > 0.3 and self.wwv_component_power_db > 6.0:
            usable.append('WWV')
        if self.wwvh_confidence > 0.3 and self.wwvh_component_power_db > 6.0:
            usable.append('WWVH')
        if self.bpm_usable_for_timing and self.bpm_confidence > 0.3:
            usable.append('BPM')
        return usable
```

---

## Improved BCD Correlation (Phase Coherence Fix)

### Problem

60-second BCD integration fails because ionospheric coherence time is typically <10 seconds. Phase rotation destroys coherent gain.

### Solution: 10-Second Windows with Doppler De-rotation

```python
def correlate_bcd_with_derotation(
    iq_samples: np.ndarray,
    sample_rate: int,
    doppler_estimate_hz: float,
    window_seconds: int = 10
) -> Dict:
    """
    BCD correlation with Doppler de-rotation.
    
    1. Estimate Doppler from per-tick phase progression
    2. De-rotate signal before integration
    3. Use 10-second windows with 50% overlap
    """
    # Step 1: Estimate Doppler slope from ticks
    doppler_hz = estimate_doppler_from_ticks(iq_samples, sample_rate)
    
    # Step 2: De-rotate the entire signal
    t = np.arange(len(iq_samples)) / sample_rate
    derotation = np.exp(-2j * np.pi * doppler_hz * t)
    iq_derotated = iq_samples * derotation
    
    # Step 3: 10-second windows with 50% overlap
    results = []
    for window_start in range(0, 55, 5):  # 0, 5, 10, ..., 50
        window_end = min(window_start + window_seconds, 60)
        window_samples = iq_derotated[window_start*sample_rate : window_end*sample_rate]
        
        # Correlate with BCD template
        wwv_corr = correlate_bcd_template(window_samples, 'WWV')
        wwvh_corr = correlate_bcd_template(window_samples, 'WWVH')
        
        results.append({
            'window_start': window_start,
            'wwv_amplitude': np.max(np.abs(wwv_corr)),
            'wwvh_amplitude': np.max(np.abs(wwvh_corr)),
            'coherence_quality': measure_coherence(window_samples)
        })
    
    return results
```

---

## Super-Resolution ToA Estimation

With GPSDO-locked sampling, we can achieve **sub-sample timing precision**:

```python
def super_resolution_toa(
    correlation: np.ndarray,
    sample_rate: int
) -> float:
    """
    Parabolic interpolation for sub-sample ToA.
    
    Standard resolution: 1/fs = 50 μs at 20 kHz
    With interpolation: ~5 μs (10× improvement)
    """
    peak_idx = np.argmax(np.abs(correlation))
    
    if peak_idx == 0 or peak_idx == len(correlation) - 1:
        return peak_idx / sample_rate
    
    # Parabolic fit to peak and neighbors
    y_m1 = np.abs(correlation[peak_idx - 1])
    y_0 = np.abs(correlation[peak_idx])
    y_p1 = np.abs(correlation[peak_idx + 1])
    
    # Fractional sample offset
    delta = 0.5 * (y_m1 - y_p1) / (y_m1 - 2*y_0 + y_p1)
    
    refined_idx = peak_idx + delta
    return refined_idx / sample_rate
```

---

## Implementation Phases

### Phase 1: BPM UT1 Pulse Detection (Immediate)

Focus on minutes :25-:29 where BPM's 100 ms pulses are unambiguous:

1. Add `detect_bpm_ut1_pulses()` to `bpm_discriminator.py`
2. Log BPM power and ToA during UT1 minutes
3. Use this to calibrate BPM path characteristics

### Phase 2: Correlator Bank Architecture

1. Create `StationModel` class with per-station templates
2. Implement parallel correlator bank in `multi_station_detector.py`
3. Output `ChannelAssignment` instead of `DiscriminationResult`

### Phase 3: Doppler-Compensated BCD

1. Add `correlate_bcd_with_derotation()` to `wwvh_discrimination.py`
2. Use 10-second windows instead of 60-second
3. Apply Doppler estimate from tick phase tracking

### Phase 4: Super-Resolution ToA

1. Add parabolic interpolation to `tone_detector.py`
2. Achieve ~5 μs timing precision
3. Enable single-hop vs multi-hop discrimination

---

## Verification Strategy

### BPM Detection Verification

```bash
# Check BPM UT1 pulse detection at minute :25
curl -s http://localhost:3000/api/v1/phase2/multi-station/SHARED%2010%20MHz | \
  jq '.bpm_detection | select(.timing_mode == "UT1")'

# Compare BPM power during UT1 vs UTC minutes
grep "BPM" /tmp/timestd-test/phase2/SHARED_10_MHz/discrimination/*.csv | \
  awk -F, '{print $2, $NF}'  # minute, bpm_power
```

### Cross-Validation

```bash
# Check that all detected stations agree on UTC
curl -s http://localhost:3000/api/v1/phase2/multi-station/SHARED%2010%20MHz | \
  jq '.cross_validation_error_ms'
# Should be < 5 ms if all stations are correctly identified
```

---

## References

1. Turin, G.L. (1960). "An introduction to matched filters." IRE Trans. Info. Theory.
2. NIST SP 250-67 (2009). "NIST Time and Frequency Radio Stations."
3. NTSC BPM Technical Specifications (Chinese National Time Service Center)
4. ITU-R P.531-14. "Ionospheric propagation data and prediction methods."
