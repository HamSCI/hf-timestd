# Test Signal Implementation Summary

## Overview

Completed implementation of test signal detection and visualization for WWV/WWVH scientific modulation test signals transmitted at:

- **Minute :08** - WWV (Fort Collins, CO)
- **Minute :44** - WWVH (Kauai, HI)

## Current Architecture (v5.4.0)

The test signal pipeline uses HDF5 data products with enhanced scintillation and timing analysis:

### Data Flow

```
IQ Samples → MetrologyService → WWVTestSignalDetector
                                        ↓
                              TestSignalDetection dataclass
                                        ↓
                              DataProductWriter (HDF5 L2)
                                        ↓
              /var/lib/timestd/phase2/{CHANNEL}/L2/test_signal/
                                        ↓
                              TestSignalService API
                                        ↓
                              physics.html Channels tab
```

### Key Components

| Component | Location | Responsibility |
|-----------|----------|----------------|
| `WWVTestSignalDetector` | `src/hf_timestd/core/wwv_test_signal.py` | Detection, timing, scintillation |
| `MetrologyService` | `src/hf_timestd/core/metrology_service.py` | Triggers detection, writes HDF5 |
| `TestSignalService` | `web-api/services/test_signal_service.py` | API for test signal data |
| `physics.html` | `web-api/static/physics.html` | Channels tab visualization |

### HDF5 Schema

Schema: `src/hf_timestd/schemas/l2_test_signal_v1.json`

Output: `/var/lib/timestd/phase2/{CHANNEL}/L2/test_signal/test_signal_YYYYMMDD.h5`

---

## Signal Structure

Per [Zenodo 5602094](https://zenodo.org/records/5182323), the 45-second test signal contains:

| Time (sec) | Content | Scientific Purpose |
|------------|---------|-------------------|
| 0-10 | Voice announcement | Synchronization |
| 10-12 | White noise #1 | Wideband coherence, timing |
| 12-13 | Blank | — |
| 13-23 | Multi-tone (2,3,4,5 kHz) | Frequency selectivity, scintillation |
| 23-24 | Blank | — |
| 24-32 | Chirp sequences | Delay spread via pulse compression |
| 32-34 | Blank | — |
| 34-36 | Single-cycle bursts | High-precision timing |
| 36-37 | Blank | — |
| 37-39 | White noise #2 | Transient detection |
| 39-42 | Blank | — |

---

## Detection Algorithm

The detector uses multi-feature analysis with the following weighted confidence:

```python
confidence = 0.5 * multitone_score + 0.3 * noise_score + 0.2 * chirp_score
detected = confidence >= 0.20  # Combined threshold
```

### Detection Thresholds

| Feature | Threshold | Method |
|---------|-----------|--------|
| Multi-tone | 0.15 | Template correlation + simple FFT presence |
| Chirp | 0.15 | Matched filter detection |
| Noise | 0.30 | Energy detection (template correlation limited by PRNG sequence) |
| Burst | 0.30 | Single-cycle pulse detection |
| **Combined** | **0.20** | Weighted sum of above |

---

## Scintillation Analysis

### S4 Scintillation Index

The S4 index measures amplitude scintillation: `S4 = σ(I) / μ(I)`

**Implementation** (`_extract_per_frequency_timeseries`):

```python
# Detrend: remove expected -3dB/sec attenuation
expected_atten_db = np.array([-3.0 * i for i in range(len(powers_arr))])
detrended_db = powers_arr - (powers_arr[0] + expected_atten_db)

# Convert to linear intensity for S4
intensity = 10**(detrended_db / 10)
s4 = float(np.std(intensity) / np.mean(intensity))
```

**Key features:**

- **No artificial clipping**: S4 > 1.0 is valid for saturated scintillation
- **Detrending**: Removes designed signal attenuation to isolate ionospheric fading
- **Multi-frequency**: S4 computed at 2, 3, 4, 5 kHz separately

### S4 Frequency Slope

Discriminates D-layer vs F-layer propagation:

- **Positive slope**: D-layer absorption (frequency-dependent)
- **Near-zero slope**: F-layer (frequency-independent)

```python
# Linear regression: S4 = slope * freq + intercept
slope, _ = np.polyfit(freqs_khz, s4_values, 1)
s4_frequency_slope = float(slope)
```

---

## High-Precision Timing

### Timing Sources (Priority Order)

1. **Burst** (single-cycle pulses): Highest time resolution (τ ≈ 1/f)
2. **Chirp** (matched filter): High BT product (~5000), sub-ms precision
3. **Multitone** (onset detection): Coarse timing
4. **Noise** (energy detection): Fallback timing

> **Note:** White noise template correlation is limited because the actual WWV broadcast uses a LabVIEW PRNG sequence that differs from Python's random generator. Cross-correlation requires bit-identical sequences. See: [wwv-h-characterization-signal-ports](https://github.com/aidanmontare-edu/wwv-h-characterization-signal-ports)

---

## Data Fields

### TestSignalDetection Dataclass

```python
@dataclass
class TestSignalDetection:
    detected: bool
    confidence: float  # 0.0 to 1.0
    station: Optional[str]  # 'WWV' or 'WWVH'
    minute_number: int
    
    # Detection scores
    multitone_score: float
    chirp_score: float
    noise_correlation: float
    
    # Timing
    signal_start_time: Optional[float]  # Seconds into minute
    toa_offset_ms: Optional[float]
    toa_source: Optional[str]  # 'burst', 'chirp', 'multitone', 'noise'
    burst_toa_offset_ms: Optional[float]
    noise_toa_offset_ms: Optional[float]
    noise_correlation_peak: Optional[float]
    
    # Channel characterization
    snr_db: Optional[float]
    effective_snr_db: Optional[float]
    delay_spread_ms: Optional[float]
    coherence_time_sec: Optional[float]
    frequency_selectivity_db: Optional[float]
    tone_powers_db: Optional[Dict[int, float]]
    tone_power_timeseries: Optional[Dict[int, List[float]]]
    
    # Field strength (new in v5.4.0)
    field_strength_db: Optional[float]
    field_strength_stability: Optional[float]
    
    # Scintillation
    scintillation_index: Optional[float]  # S4, can exceed 1.0
    s4_by_frequency: Optional[Dict[int, float]]  # {2000: 0.3, ...}
    s4_frequency_slope: Optional[float]  # D-layer vs F-layer
    fading_variance: Optional[float]
    
    # Noise segment analysis
    noise1_score: float  # 10-12s segment
    noise2_score: float  # 37-39s segment
    noise_coherence_diff: Optional[float]  # |noise1 - noise2|
    transient_detected: bool
    
    # Anomaly detection
    anomaly_detected: bool
    anomaly_type: Optional[str]  # 'sudden_amplitude_drop', etc.
    anomaly_confidence: Optional[float]
    
    # Channel quality
    multipath_detected: bool
    channel_quality: Optional[str]  # 'excellent', 'good', 'fair', 'poor'
```

---

## Web UI Display

### physics.html Channels Tab

Displays per-frequency test signal results with:

| Metric | Description | Color Coding |
|--------|-------------|--------------|
| SNR | Signal-to-noise ratio | — |
| Delay Spread | Multipath delay | — |
| Coherence | Channel coherence time | — |
| S4 Index | Scintillation index | Green <0.3, Yellow 0.3-0.6, Red >0.6 |
| ToA Offset | Timing from best available source | — |
| Corr Peak | Correlation coefficient | — |
| S4 Slope | Frequency dependence | Green (F-layer), Yellow (D-layer) |
| Multipath | Multipath detected | Green NO, Red YES |

---

## Channel Quality Criteria

| Grade | SNR | Delay Spread | Coherence Time |
|-------|-----|--------------|----------------|
| **Excellent** | > 20 dB | < 0.5 ms | > 5 s |
| **Good** | > 10 dB | < 2 ms | > 2 s |
| **Fair** | > 5 dB | < 5 ms | > 1 s |
| **Poor** | < 5 dB | > 5 ms | < 1 s |

---

## Anomaly Detection

The detector identifies ionospheric anomalies:

| Type | Signature | Cause |
|------|-----------|-------|
| `sudden_amplitude_drop` | >10 dB drop in <3 seconds | Solar flare (SID) |
| `sudden_amplitude_increase` | Unexpected signal enhancement | Sporadic E layer |
| `rapid_fading` | High fading variance | Severe scintillation |
| `frequency_selective_fade` | Large FSS deviation | Frequency-dependent absorption |

Transient detection uses the difference between noise segments: `|noise1_score - noise2_score| > 0.2`

---

## Testing & Verification

### View Detection Logs

```bash
journalctl -u timestd-metrology -n 50 | grep -i "test signal"

# Example successful detection:
# INFO: WWV 10 MHz: ✨ Test signal detected! Station=WWV, confidence=0.876, SNR=23.4dB
```

### Verify HDF5 Output

```bash
ls -la /var/lib/timestd/phase2/WWV_10_MHz/L2/test_signal/
h5dump -H /var/lib/timestd/phase2/WWV_10_MHz/L2/test_signal/test_signal_$(date +%Y%m%d).h5
```

### Web API Access

```bash
curl http://localhost:8000/api/test-signal/latest | jq
```

---

## Expected Behavior

### At Minute :08 (WWV Test Signal)

- Detection runs automatically
- HDF5 record written (detected=true if signal found)
- If detected: `station='WWV'`, confidence ≥ 0.20

### At Minute :44 (WWVH Test Signal)

- Detection runs automatically
- HDF5 record written (detected=true if signal found)
- If detected: `station='WWVH'`, confidence ≥ 0.20

### Other Minutes

- No detection attempted
- Not recorded in test_signal HDF5 files

---

## References

- [WWV/H Scientific Modulation Working Group](https://www.hamsci.org/wwv)
- [Signal Specification (Zenodo 5182323)](https://zenodo.org/records/5182323)
- [PRNG Implementation Ports](https://github.com/aidanmontare-edu/wwv-h-characterization-signal-ports)

---

**Initial Implementation:** 2024-11-26  
**v5.4.0 Enhancement:** 2026-01-22  
**Documentation Updated:** 2026-01-23  
**Status:** Complete and operational
