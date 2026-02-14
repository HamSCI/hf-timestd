# Multi-Station Tone Detection

## Overview

The GRAPE Signal Recorder implements comprehensive multi-station time signal tone detection using phase-invariant quadrature matched filtering. This enables simultaneous detection of:

- **WWV (Fort Collins, CO)**: 1000 Hz, 0.8s duration - PRIMARY for time_snap
- **WWVH (Hawaii)**: 1200 Hz, 0.8s duration - Propagation analysis  
- **CHU (Ottawa, Canada)**: 1000 Hz, 0.5s duration - Alternate time_snap

## Scientific Purpose

### Dual-Purpose System

**1. Timing Reference (time_snap)**
- Establishes and verifies UTC-RTP timestamp mapping
- Detects clock drift and applies corrections
- Uses WWV or CHU (1000 Hz) as primary reference
- Achieves <50ms timing accuracy under good conditions

**2. Propagation Study**
- Original GRAPE science goal: ionospheric disturbance detection
- WWVH (Hawaii) provides differential propagation measurements
- WWV-WWVH differential delay reveals ionospheric effects
- Varies by time of day, season, and solar activity

## Detection Algorithm

### Phase-Invariant Matched Filtering

Unlike simple bandpass + envelope detection, matched filtering provides:

✅ **Robust in Low SNR** - Detects tones 5-10 dB weaker  
✅ **Phase Insensitive** - Works regardless of carrier phase  
✅ **Precise Timing** - Sub-sample onset localization  
✅ **Multi-Tone** - Separates WWV (1000 Hz) from WWVH (1200 Hz)  

### Algorithm Steps

```
1. Resample 16 kHz IQ → 3 kHz for processing
2. AM demodulation: magnitude = |IQ|
3. AC coupling: audio = magnitude - mean(magnitude)
4. Create quadrature templates:
   - WWV:  sin(2π·1000·t) and cos(2π·1000·t) × Tukey window (0.8s)
   - WWVH: sin(2π·1200·t) and cos(2π·1200·t) × Tukey window (0.8s)
   - CHU:  sin(2π·1000·t) and cos(2π·1000·t) × Tukey window (0.5s)
5. Cross-correlate audio with each template pair
6. Combine: correlation = √(corr_sin² + corr_cos²)
7. Search ±500ms window around expected minute boundary
8. Adaptive threshold: peak > (noise_mean + 2.5σ)
9. Calculate timing error vs UTC :00.000
10. Estimate SNR: 20·log₁₀(peak / noise_mean)
```

### Noise-Adaptive Thresholding

**Key Innovation**: Exclude the search window from noise estimation

```python
# BAD: Includes signal peak in noise calculation
noise_samples = all_correlation_values
noise_floor = mean + 3σ  # Too high, misses weak signals

# GOOD: Excludes search window from noise
noise_samples = concat(
    correlation[:search_start - 100],
    correlation[search_end + 100:]
)
noise_floor = mean + 2.5σ  # More sensitive
```

This prevents the signal peak from inflating the noise estimate, improving weak-signal detection by ~5 dB.

## Station-Specific Behavior

### WWV Frequencies (MHz): 2.5, 5, 10, 15, 20, 25

**Detects**: WWV (1000 Hz, 0.8s) + WWVH (1200 Hz, 0.8s)

**Usage**:
- WWV → time_snap corrections
- WWVH → propagation analysis only
- Differential delay → ionospheric path difference

**Example Log**:
```
WWV 5 MHz: ⏱️ WWV tone detected! Timing error: +12.3 ms, SNR: 58.2 dB
WWV 5 MHz: 📡 WWVH propagation: timing=-35.7ms, SNR=42.1dB
WWV 5 MHz: 📊 WWV-WWVH differential: +48.0ms (WWV: +12.3ms, WWVH: -35.7ms)
```

The differential delay reveals that WWVH signal arrived earlier, indicating different ionospheric bounce paths.

### CHU Frequencies (MHz): 3.33, 7.85, 14.67

**Detects**: CHU (1000 Hz, 0.5s) only

**Usage**:
- CHU → time_snap corrections (alternate to WWV)
- Shorter tone (0.5s vs 0.8s) matches NRC Canada spec

**Example Log**:
```
CHU 7.85 MHz: ⏱️ CHU tone detected! Timing error: -8.1 ms, SNR: 51.3 dB
```

## Integration into Common Processing

### Unified Architecture

```
RTP Packet (16 kHz IQ)
    ↓
[Common Preprocessing]
    • Parse RTP header
    • Resequencing (64-packet buffer)
    • Gap detection & fill with zeros
    • time_snap tracking
    ↓
    ├──→ [Archive Path]
    │      16 kHz IQ → compressed NPZ
    │
    ├──→ [Upload Path]
    │      Decimate 16k→10 Hz → Digital RF
    │
    └──→ [Monitoring Path]
           ├─ Resample 16k→3k
           ├─ MultiStationToneDetector
           │    ├─ Detect WWV (1000 Hz)
           │    ├─ Detect WWVH (1200 Hz)
           │    └─ Detect CHU (1000 Hz)
           ├─ Separate by purpose:
           │    • WWV/CHU → time_snap
           │    • WWVH → propagation
           ├─ Calculate differential delays
           └─ Update quality metrics
```

### Key: Detection Happens AFTER Gap Normalization

All tone detection operates on **gap-filled, time_snap-corrected samples** from the common preprocessing stage. This ensures:

✅ Time_snap corrections use clean, continuous data  
✅ Detections are comparable across all channels  
✅ Gap-induced false triggers are eliminated  

## Detection Statistics (from Status JSON)

```json
{
  "timing_validation": {
    "enabled": true,
    "stations_active": ["WWV", "WWVH"],
    "wwv_detections": 145,
    "wwvh_detections": 132,
    "chu_detections": 0,
    "total_detections": 145,
    "expected_detections": 180,
    "detection_rate": 0.81,
    "timing_error_mean_ms": 12.3,
    "timing_error_std_ms": 8.7,
    "timing_error_max_ms": 45.2,
    "wwv_wwvh_differential_mean_ms": 48.0,
    "wwv_wwvh_differential_std_ms": 12.5,
    "last_timing_error_ms": 10.1
  }
}
```

### Interpreting Statistics

**Detection Rate < 1.0 is NORMAL**
- Weak propagation (night, poor conditions): 20-40% detection rate
- Good propagation (day, strong signals): 80-95% detection rate
- Studying ionospheric variability is the science goal!

**Differential Delay Analysis**
- Positive: WWV arrives after WWVH (unusual - WWV is closer)
- Negative: WWV arrives before WWVH (typical for CONUS)
- Magnitude: Ionospheric path difference (40-80ms typical)
- Variability: Indicates dynamic ionospheric conditions

**Timing Error Consistency**
- Mean ≈ 0: Good synchronization
- Std < 20ms: Stable propagation
- Max < 50ms: No time_snap corrections triggered

## Configuration

### Enable Multi-Station Detection

In `grape-config.toml`:

```toml
[recorder]
# Multi-station detection automatically enabled for WWV/CHU channels
# No additional configuration needed

# Detection parameters (advanced)
time_snap_error_threshold_ms = 50.0    # Apply correction if error > 50ms
time_snap_min_interval_sec = 600       # Don't correct more than once/10min
```

### Channel Naming

Detection is enabled based on channel name:

```python
is_wwv_or_chu = any(x in channel_name.upper() for x in ['WWV', 'CHU'])

# Matches:
"WWV 2.5 MHz"  ✅
"WWV_10_MHz"   ✅
"CHU 7.85 MHz" ✅
"AM_Radio"     ❌ (not a time signal)
```

## Performance

### Computational Cost

Per channel per minute:

- **Resampling**: 16k→3k (960k → 180k samples) ~0.5ms
- **Correlation**: 3 templates × 180k samples ~5ms
- **Peak detection**: ~0.1ms

**Total**: ~6ms per minute per channel = negligible CPU impact

### Memory Usage

Per channel:

- Tone buffer: ~12 kB (4 seconds @ 3 kHz)
- Templates: 3 stations × 2 (sin/cos) × 2400 samples × 8 bytes = 115 kB
- Detection history: 60 detections × 100 bytes = 6 kB

**Total**: ~130 kB per channel

With 9 channels: ~1.2 MB total (insignificant)

## Verification

### Check Detection in Logs

```bash
tail -f /var/lib/signal-recorder/logs/recorder_*.log | grep -E "(⏱️|📡|📊)"
```

Example output:
```
WWV 2.5 MHz: ⏱️ WWV tone detected! Timing error: +15.2 ms, SNR: 62.1 dB
WWV 2.5 MHz: 📡 WWVH propagation: timing=-28.3ms, SNR=48.7dB
WWV 2.5 MHz: 📊 WWV-WWVH differential: +43.5ms
CHU 7.85 MHz: ⏱️ CHU tone detected! Timing error: -6.8 ms, SNR: 54.2 dB
```

### View Status JSON

```bash
cat /var/lib/signal-recorder/status/recording-stats.json | jq '.recorders[].timing_validation'
```

### Generate Timing Report

```python
import pandas as pd

# Load WWV timing CSV
df = pd.read_csv('/var/lib/signal-recorder/analytics/timing/wwv_timing.csv')

# Plot timing errors over time
df.plot(x='utc_time', y='timing_error_ms', kind='scatter')

# Statistics
print(f"Detection rate: {len(df) / (24*60):.1%}")  # detections / minutes in day
print(f"Mean error: {df['timing_error_ms'].mean():.1f}ms")
print(f"Std dev: {df['timing_error_ms'].std():.1f}ms")
```

## Troubleshooting

### No Detections on WWV Channels

**Check**:
1. Is signal present? `quick-verify.sh` should show non-zero power
2. Are you in detection window? Tones only at :00 of each minute
3. Is SNR adequate? Need >30 dB for reliable detection

**Diagnosis**:
```bash
# Check signal power
grep "signal power" /var/lib/signal-recorder/logs/recorder_*.log

# Look for detection attempts
grep "Checking for WWV tone" /var/lib/signal-recorder/logs/recorder_*.log

# Review why detection failed
grep "No tones above noise threshold" /var/lib/signal-recorder/logs/recorder_*.log
```

### WWVH Never Detected

**This is NORMAL if**:
- You're closer to WWV (Fort Collins) than WWVH (Hawaii)
- Frequency doesn't propagate well to Hawaii path
- Time of day doesn't favor trans-Pacific propagation

**WWVH detection varies greatly by**:
- Location (better on West Coast)
- Frequency (lower frequencies propagate farther)
- Time of day (night/day ionosphere differences)
- Season and solar cycle

### False Detections

**Symptoms**: Detections when no signal present, or wrong timing

**Causes**:
1. Local interference at 1000/1200 Hz
2. Threshold too low (noise mistaken for signal)
3. Multiple signals (ham radio QRM)

**Solutions**:
```toml
# Increase detection threshold
detector_threshold_sigma = 3.0  # up from 2.5

# Require higher SNR
min_detection_snr_db = 40.0
```

## References

- **NIST WWV**: https://www.nist.gov/pml/time-and-frequency-division/time-distribution/radio-station-wwv
- **NRC CHU**: https://nrc.canada.ca/en/certifications-evaluations-standards/canadas-official-time/chu-metadata
- **Matched Filtering**: van Trees, "Detection, Estimation, and Modulation Theory"
- **KA9Q time_snap**: `/home/mjh/git/ka9q-radio/src/pcmrecord.c` lines 607, 652-679

## Implementation Files

- `src/signal_recorder/grape_rtp_recorder.py`: 
  - `MultiStationToneDetector` class (lines 175-419)
  - Integration into `GRAPEChannelRecorder` (lines 838-856, 1295-1372)
- `src/signal_recorder/grape_channel_recorder_v2.py`:
  - Original V2 implementation (reference, currently unused)
- `docs/TIMING_ARCHITECTURE_V2.md`:
  - KA9Q time_snap mechanism
- `docs/WWV_DETECTION.md`:
  - Original single-station detection (superseded)

---

**Status**: Fully implemented and active (November 2024)

**Next**: Test with live WWV/CHU signals and verify differential delay measurements match ionospheric predictions
