# WWV/WWVH Test Signal Analysis Guide

## Overview

The WWV/WWVH test signals provide rich ionospheric and propagation data through a 45-second scientific modulation sequence broadcast at:
- **Minute :08** - WWV only (WWVH silent)
- **Minute :44** - WWVH only (WWV silent)

This guide explains how to use the enhanced test signal analyzer for ionospheric research.

## Quick Start

### Basic Usage

```python
from hf_timestd.core.wwv_test_signal import WWVTestSignalDetector

# Initialize detector
detector = WWVTestSignalDetector(sample_rate=24000)

# Analyze test signal (minute 8 or 44)
result = detector.detect(
    iq_samples=iq_data,      # Complex IQ samples (60 seconds)
    minute_number=8,          # 8 for WWV, 44 for WWVH
    sample_rate=24000
)

# Check detection
if result.detected:
    print(f"Station: {result.station}")
    print(f"Confidence: {result.confidence:.3f}")
    print(f"Channel quality: {result.channel_quality}")
```

### Accessing Enhanced Metrics

```python
# Per-frequency field strength (2, 3, 4, 5 kHz)
if result.tone_power_timeseries:
    for freq, powers in result.tone_power_timeseries.items():
        print(f"{freq} Hz: {powers}")  # 10 values (1 per second)

# Ionospheric metrics
print(f"Scintillation index S4: {result.scintillation_index}")
print(f"Field strength: {result.field_strength_db} dB")
print(f"Delay spread: {result.delay_spread_ms} ms")
print(f"Coherence time: {result.coherence_time_sec} s")

# Anomaly detection
if result.anomaly_detected:
    print(f"⚠️ Anomaly: {result.anomaly_type}")
    print(f"   Confidence: {result.anomaly_confidence}")
```

## Test Signal Structure

### Timing (seconds into minute)

| Time | Segment | Purpose |
|------|---------|---------|
| 0-10s | Voice announcement | "What follows is a scientific modulation test..." |
| 10-12s | White noise #1 | Wideband coherence, synchronization |
| 12-13s | Blank | Transition |
| 13-23s | **Multi-tone** | **2, 3, 4, 5 kHz with -3dB steps** |
| 23-24s | Blank | Transition |
| 24-32s | Chirp sequences | Short/long up/down chirps (delay spread) |
| 32-34s | Blank | Transition |
| 34-36s | Single-cycle bursts | 2.5 kHz, 5 kHz (high-precision timing) |
| 36-37s | Blank | Transition |
| 37-39s | White noise #2 | Identical to #1 (transient detection) |
| 39-45s | Blank | End |

### Multi-Tone Segment (Most Important)

The 10-second multi-tone segment contains four phase-coherent tones:
- **2 kHz** - Most reliable through ionosphere
- **3 kHz** - Mid-range reference
- **4 kHz** - High-frequency reference
- **5 kHz** - Often attenuated (near Nyquist limit at 24 kHz sampling)

**Attenuation pattern:** -3 dB per second (factor of √2)

## Scientific Applications

### 1. Ionospheric Absorption Analysis

**Frequency-Dependent Absorption:**
```python
# Calculate absorption between 2 kHz and 5 kHz
if result.tone_power_timeseries:
    p_2k = result.tone_power_timeseries[2000][0]  # First second
    p_5k = result.tone_power_timeseries[5000][0]
    absorption_db = p_2k - p_5k
    
    # Typical values:
    # 0-3 dB: Low absorption (nighttime, high solar activity)
    # 3-6 dB: Moderate absorption (daytime)
    # >6 dB: High absorption (solar flare, D-layer enhancement)
```

**Frequency Selectivity Score (FSS):**
```python
fss = result.frequency_selectivity_db

# Interpretation:
# FSS > 5 dB: Strong high-frequency attenuation (long/dispersive path)
# FSS 0-5 dB: Moderate selectivity (typical F-layer)
# FSS < 0 dB: Unusual (possible sporadic E)
```

### 2. Solar Flare Detection

**Sudden Ionospheric Disturbance (SID):**
```python
if result.anomaly_detected and result.anomaly_type == "sudden_amplitude_drop":
    print("⚠️ SOLAR FLARE DETECTED")
    print(f"   Confidence: {result.anomaly_confidence}")
    
    # Characteristics:
    # - >10 dB drop in <3 seconds
    # - Affects all frequencies (broadband)
    # - D-layer absorption increase
```

**Time-Series Analysis:**
```python
# Look for sudden drops in field strength
if result.tone_power_timeseries:
    powers = result.tone_power_timeseries[2000]
    for i in range(len(powers) - 2):
        drop = powers[i] - powers[i+2]
        if drop > 10:
            print(f"Sudden drop at t={i}s: {drop:.1f} dB")
```

### 3. Scintillation Monitoring

**S4 Scintillation Index:**
```python
s4 = result.scintillation_index

# Thresholds:
if s4 < 0.2:
    condition = "Quiet"
elif s4 < 0.4:
    condition = "Moderate scintillation"
else:
    condition = "Strong scintillation (communication impacts)"

print(f"Ionospheric conditions: {condition} (S4={s4:.3f})")
```

**Fading Variance:**
```python
# Normalized variance (detrended from expected -3dB/sec)
variance = result.fading_variance

# High variance indicates:
# - Ionospheric turbulence
# - Multipath interference
# - Unstable propagation
```

### 4. Propagation Mode Identification

**Channel Quality Assessment:**
```python
quality = result.channel_quality

# Quality grades:
# "excellent": E-layer, stable, low multipath
# "good": F-layer, moderate conditions
# "fair": Multi-hop or disturbed conditions
# "poor": Severe multipath or fading
```

**Delay Spread Analysis:**
```python
delay_spread = result.delay_spread_ms

# Interpretation:
if delay_spread < 0.5:
    mode = "Single-hop E-layer"
elif delay_spread < 2.0:
    mode = "Single-hop F-layer"
else:
    mode = "Multi-hop or severe multipath"
```

### 5. Communication Link Planning

**Frequency Selection:**
```python
# Use per-frequency field strength to select best frequency
if result.tone_power_timeseries:
    best_freq = max(
        result.tone_power_timeseries.keys(),
        key=lambda f: result.tone_power_timeseries[f][0]
    )
    print(f"Best audio frequency: {best_freq} Hz")
    
    # Extrapolate to HF carrier frequencies:
    # Higher audio freq = higher HF carrier can propagate
```

**Link Budget:**
```python
field_strength = result.field_strength_db
stability = result.field_strength_stability

# Stable, strong signal = reliable link
if field_strength > -40 and stability > 3.0:
    print("✅ Excellent link conditions")
elif field_strength > -50 and stability > 2.0:
    print("✓ Good link conditions")
else:
    print("⚠️ Marginal link conditions")
```

## Anomaly Types

### 1. Sudden Amplitude Drop
**Cause:** Solar flare (sudden ionospheric disturbance)  
**Signature:** >10 dB drop in <3 seconds  
**Impact:** Broadband HF absorption, communication blackout

### 2. Sudden Amplitude Increase
**Cause:** Sporadic E layer formation  
**Signature:** >8 dB increase in <3 seconds  
**Impact:** Enhanced propagation, possible multi-hop

### 3. Rapid Fading
**Cause:** Severe ionospheric scintillation  
**Signature:** >5 dB RMS fluctuation  
**Impact:** Signal instability, communication degradation

### 4. Frequency-Selective Fade
**Cause:** Ionospheric structure changes  
**Signature:** High variance in frequency-dependent behavior  
**Impact:** Frequency-dependent propagation

### 5. Transient Interference
**Cause:** Equipment or local interference  
**Signature:** Noise segment mismatch (|N1-N2| > 0.3)  
**Impact:** Data quality degradation

## Data Quality

### Quality Flags

```python
# Overall quality assessment
quality_flag = "GOOD"  # or "MARGINAL", "BAD", "MISSING"

# Criteria:
# GOOD: Detected with high confidence, all metrics valid
# MARGINAL: Detected but some metrics missing or low confidence
# BAD: Not detected or very poor quality
# MISSING: Not a test signal minute
```

### Confidence Scores

```python
confidence = result.confidence  # 0.0 to 1.0

# Based on:
# - Multi-tone detection score (50% weight)
# - Noise correlation (30% weight)
# - Chirp detection (20% weight)

# Thresholds:
# >0.7: High confidence
# 0.4-0.7: Medium confidence
# <0.4: Low confidence
```

## Integration Examples

### Real-Time Monitoring

```python
import time
from datetime import datetime

def monitor_test_signals():
    """Monitor for test signal minutes and analyze"""
    detector = WWVTestSignalDetector(sample_rate=24000)
    
    while True:
        now = datetime.utcnow()
        minute = now.minute
        
        # Check if test signal minute
        if minute in [8, 44]:
            print(f"Test signal minute: {minute}")
            
            # Get IQ samples (implementation specific)
            iq_samples = get_iq_samples()
            
            # Analyze
            result = detector.detect(iq_samples, minute, 24000)
            
            # Log results
            if result.detected:
                log_test_signal_data(result)
                
                # Check for anomalies
                if result.anomaly_detected:
                    send_alert(result)
        
        # Sleep until next minute
        time.sleep(60)
```

### Historical Analysis

```python
from pathlib import Path
from hf_timestd.io.hdf5_reader import DataProductReader

def analyze_historical_test_signals(start_date, end_date):
    """Analyze historical test signal data"""
    
    reader = DataProductReader(
        data_dir=Path('/var/lib/timestd/phase2/WWV_20000'),
        product_level='L2',
        product_name='test_signal',
        channel='WWV_20000'
    )
    
    measurements = reader.read_time_range(
        start_date.isoformat() + 'Z',
        end_date.isoformat() + 'Z'
    )
    
    # Analyze trends
    scintillation_values = [m['scintillation_index'] for m in measurements]
    anomaly_count = sum(1 for m in measurements if m['anomaly_detected'])
    
    print(f"Total measurements: {len(measurements)}")
    print(f"Anomalies detected: {anomaly_count}")
    print(f"Average S4: {np.mean(scintillation_values):.3f}")
```

## Troubleshooting

### Low Detection Confidence

**Possible causes:**
1. Weak signal (low SNR)
2. Heavy ionospheric absorption
3. Interference or noise
4. Sample rate mismatch

**Solutions:**
- Check carrier SNR in L1 data
- Verify correct minute number (8 or 44)
- Check for local interference
- Ensure 24 kHz sample rate

### Missing Time-Series Data

**Possible causes:**
1. Test signal not detected
2. Multi-tone segment corrupted
3. Insufficient signal duration

**Solutions:**
- Check `result.detected` flag
- Verify full 60-second IQ buffer
- Check `result.multitone_score`

### Anomaly False Positives

**Possible causes:**
1. Local interference
2. Receiver AGC transients
3. Data gaps or dropouts

**Solutions:**
- Check `transient_detected` flag
- Verify noise segment coherence
- Cross-validate with other channels

## References

- **HamSCI WWV/H Working Group:** https://www.hamsci.org/wwv
- **Test Signal Specification:** https://zenodo.org/records/5182323
- **NIST WWV/WWVH Documentation:** NIST Special Publication 250-67

## Support

For questions or issues:
- Check logs: `sudo journalctl -u timestd-analytics -f | grep "test signal"`
- Review session documentation: `docs/changes/SESSION_2026_01_03_TEST_SIGNAL_ENHANCEMENTS.md`
- Validate with known events: Use historical solar flare data for testing
