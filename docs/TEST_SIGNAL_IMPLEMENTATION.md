# Test Signal Implementation Summary

## Overview

Completed implementation of test signal detection and visualization for WWV/WWVH scientific modulation test signals transmitted at:
- **Minute :08** - WWV (Fort Collins, CO)
- **Minute :44** - WWVH (Kauai, HI)

## Current Architecture (v5.4.0)

The test signal pipeline now uses HDF5 data products with enhanced scintillation and timing analysis:

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
| `WWVTestSignalDetector` | `core/wwv_test_signal.py` | Detection, timing, scintillation |
| `MetrologyService` | `core/metrology_service.py` | Triggers detection, writes HDF5 |
| `TestSignalService` | `web-api/services/test_signal_service.py` | API for test signal data |
| `physics.html` | `web-api/static/physics.html` | Channels tab visualization |

### HDF5 Schema

Schema: `l2_test_signal_v1.json`

Output: `/var/lib/timestd/phase2/{CHANNEL}/L2/test_signal/test_signal_YYYYMMDD.h5`

## Scintillation Analysis (v5.4.0)

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

**Key improvements (v5.4.0):**
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

## High-Precision Timing (v5.4.0)

### White Noise Template Correlation

The white noise segments (10-12s and 37-39s) are deterministic (known seed), enabling matched filter timing extraction with ~40dB processing gain.

**Implementation** (`_detect_noise_template_correlation`):
```python
# Generate template with known seed
template = self.generator.generate_white_noise(2.0, seed=42)

# High-pass filter to isolate wideband noise
b, a = signal.butter(4, Wn, 'high', fs=self.sample_rate)
template_filt = signal.filtfilt(b, a, template)
search_filt = signal.filtfilt(b, a, search_segment)

# Cross-correlation for ToA
Rxy = signal.correlate(search_filt, template_filt, mode='valid')
peak_idx = np.argmax(np.abs(Rxy))
toa_offset_ms = (actual_sample - expected_sample) / sample_rate * 1000.0
```

**Timing sources (priority order):**
1. **Burst** (single-cycle pulses): Highest time resolution
2. **Chirp** (matched filter): High BT product (~5000)
3. **Noise** (template correlation): Highest processing gain (~40dB)
4. **Multitone** (onset detection): Coarse timing

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
    toa_offset_ms: Optional[float]
    toa_source: Optional[str]  # 'burst', 'chirp', 'multitone', 'noise'
    burst_toa_offset_ms: Optional[float]
    noise_toa_offset_ms: Optional[float]  # v5.4.0
    noise_correlation_peak: Optional[float]  # v5.4.0
    
    # Channel characterization
    snr_db: Optional[float]
    effective_snr_db: Optional[float]
    delay_spread_ms: Optional[float]
    coherence_time_sec: Optional[float]
    frequency_selectivity_db: Optional[float]
    
    # Scintillation (v5.4.0 enhanced)
    scintillation_index: Optional[float]  # S4, can exceed 1.0
    s4_by_frequency: Optional[Dict[int, float]]  # {2000: 0.3, ...}
    s4_frequency_slope: Optional[float]  # D-layer vs F-layer
    fading_variance: Optional[float]
    
    # Anomaly detection
    anomaly_detected: bool
    anomaly_type: Optional[str]
    channel_quality: Optional[str]  # 'excellent', 'good', 'fair', 'poor'
```

## Web UI Display

### physics.html Channels Tab

Displays per-frequency test signal results with:

| Metric | Description | Color Coding |
|--------|-------------|--------------|
| SNR | Signal-to-noise ratio | — |
| Delay Spread | Multipath delay | — |
| Coherence | Channel coherence time | — |
| S4 Index | Scintillation index | Green <0.3, Yellow 0.3-0.6, Red >0.6 |
| ToA Offset | Timing from noise correlation | — |
| Corr Peak | Correlation coefficient | — |
| S4 Slope | Frequency dependence | Green (F-layer), Yellow (D-layer) |
| Multipath | Multipath detected | Green NO, Red YES |

## Legacy Components

### 1. CSV Writer Infrastructure (`discrimination_csv_writers.py`)

Added `TestSignalRecord` dataclass and `write_test_signal()` method:

```python
@dataclass
class TestSignalRecord:
    """Record from test signal detection (minutes 8 and 44)"""
    timestamp_utc: str
    minute_number: int
    detected: bool
    station: Optional[str]  # 'WWV' or 'WWVH' if detected
    confidence: float
    multitone_score: float
    chirp_score: float
    snr_db: Optional[float]
```

**Output Location:** `/tmp/grape-test/analytics/{CHANNEL}/test_signal/{CHANNEL}_test_signal_{DATE}.csv`

### 2. Path API Updates

**Python (`paths.py`):**
```python
def get_test_signal_dir(self, channel_name: str) -> Path:
    """Get test signal directory (minutes 8 and 44 scientific modulation test)."""
    return self.get_analytics_dir(channel_name) / 'test_signal'
```

**JavaScript (`grape-paths.js`):**
```javascript
getTestSignalDir(channelName) {
    return join(this.getAnalyticsDir(channelName), 'test_signal');
}
```

### 3. Analytics Service Integration (`analytics_service.py`)

Added test signal CSV writing after 440 Hz detection:

```python
# 3.5. Write test signal detection data (minutes 8 and 44)
if result.test_signal_detected or dt.minute in [8, 44]:
    test_signal_record = TestSignalRecord(
        timestamp_utc=timestamp_utc,
        minute_number=dt.minute,
        detected=result.test_signal_detected,
        station=result.test_signal_station,
        confidence=result.test_signal_confidence or 0.0,
        multitone_score=result.test_signal_multitone_score or 0.0,
        chirp_score=result.test_signal_chirp_score or 0.0,
        snr_db=result.test_signal_snr_db
    )
    self.csv_writers.write_test_signal(test_signal_record)
```

**Key Feature:** Records are written for ALL minutes :08 and :44, whether detection succeeds or fails, providing complete coverage for analysis.

### 4. Web-UI Server (`monitoring-server-v3.js`)

Added test signal data loading to the discrimination methods API:

```javascript
// 3.5. Load test signal detections (minutes 8 and 44)
const testSignalPath = join(paths.getTestSignalDir(channelName), 
                            `${fileChannelName}_test_signal_${date}.csv`);
const testSignalData = parseCSV(testSignalPath);
result.methods.test_signal = {
    status: testSignalData.status,
    records: testSignalData.records.map(r => ({
        timestamp_utc: r.timestamp_utc,
        minute_number: parseInt(r.minute_number),
        detected: r.detected === '1',
        station: r.station || null,
        confidence: parseFloat(r.confidence),
        multitone_score: parseFloat(r.multitone_score),
        chirp_score: parseFloat(r.chirp_score),
        snr_db: r.snr_db ? parseFloat(r.snr_db) : null
    })),
    count: testSignalData.count
};
```

### 5. Web-UI Visualization (`discrimination-charts.js`)

Added new chart panel and rendering function:

**Panel:**
```javascript
<!-- Method 3.5: Test Signal -->
<div class="method-card">
  <div class="method-header">
    <div class="method-title">Test Signal (Minutes :08/:44)</div>
    <div class="method-badge">Scientific Mod</div>
  </div>
  <div class="chart-container" id="chart-test-signal"></div>
  <div class="insight-grid">
    <div class="insight-card">
      <div class="insight-label">Records</div>
      <div class="insight-value">${data.methods.test_signal.count || 0}</div>
    </div>
  </div>
</div>
```

**Chart Rendering:**
- **WWV Detected** (minute :08): Blue circles
- **WWVH Detected** (minute :44): Orange squares
- **Not Detected**: Gray X marks
- **Y-axis**: Detection confidence (0-100%)
- **Hover info**: Station, confidence, SNR

## Bug Fixes Applied

### Fix 1: `noise_power_density_db` Undefined Error

**Location:** `wwvh_discrimination.py:934`

**Problem:** Variable used before calculation.

**Fix:**
```python
# Convert noise power density to dB (relative to 1.0 = 0 dB)
# This is N₀ in dBW/Hz
noise_power_density_db = 10 * np.log10(N0) if N0 > 0 else -100
```

### Fix 2: `TypeError: 'float' object cannot be interpreted as an integer`

**Location:** `wwvh_discrimination.py:1160-1171`

**Problem:** Float values used in `range()` and array indexing.

**Fix:**
```python
window_samples = int(window_seconds * sample_rate)
step_samples = int(step_seconds * sample_rate)
# ...
for i in range(num_windows):
    start_sample = int(i * step_samples)
    end_sample = int(start_sample + window_samples)
```

## Detection Algorithm

The test signal detector (`wwv_test_signal.py`) uses multi-feature correlation:

1. **Multi-tone Detection (70% weight)**
   - Cross-correlates with phase-coherent 2/3/4/5 kHz tones
   - 10-second sequence with 3 dB attenuation steps
   - Threshold: 0.15 correlation coefficient

2. **Chirp Detection (30% weight)**
   - Spectrogram analysis for 0-5 kHz chirp sequences
   - Short and long up/down chirps
   - Threshold: 0.2 detection score

3. **Combined Confidence**
   - Weighted sum of detection scores
   - Overall threshold: 0.20
   - SNR estimation from detected signal

## Data Flow

```
NPZ File → Analytics Service → WWVHDiscriminator
                                      ↓
                          WWVTestSignalDetector (minutes 8, 44)
                                      ↓
                          DiscriminationResult.test_signal_*
                                      ↓
                          TestSignalRecord → CSV Writer
                                      ↓
            /tmp/grape-test/analytics/{CHANNEL}/test_signal/
                                      ↓
                          Web-UI API → Plotly Chart
```

## Testing

### Verify CSV Output
```bash
# Check for test signal directory
ls -la /tmp/grape-test/analytics/WWV_10_MHz/test_signal/

# View today's test signal records
cat /tmp/grape-test/analytics/WWV_10_MHz/test_signal/WWV_10_MHz_test_signal_$(date +%Y%m%d).csv
```

### View Detection Logs
```bash
# Watch for test signal detections
tail -f /tmp/grape-test/logs/analytics-wwv10.log | grep -i "test signal"

# Example successful detection:
# INFO: WWV 10 MHz: ✨ Test signal detected! Station=WWV, confidence=0.876, SNR=23.4dB
```

### Web-UI Access
1. Navigate to: http://localhost:3000/discrimination.html
2. Select today's date and channel (e.g., "WWV 10 MHz")
3. Click "Load Discrimination Data"
4. View "Test Signal (Minutes :08/:44)" panel

## Expected Behavior

### At Minute :08 (WWV Test Signal)
- Detection runs automatically
- CSV record written (detected=1 if signal found)
- If detected: `station='WWV'`, confidence ≥ 0.20
- Chart shows blue circle at :08 timestamp

### At Minute :44 (WWVH Test Signal)
- Detection runs automatically
- CSV record written (detected=1 if signal found)
- If detected: `station='WWVH'`, confidence ≥ 0.20
- Chart shows orange square at :44 timestamp

### Other Minutes (Not Test Signal)
- No detection attempted
- No CSV record written
- Chart empty for these timestamps

## Files Modified

1. `src/signal_recorder/discrimination_csv_writers.py` - Added TestSignalRecord and write method
2. `src/signal_recorder/paths.py` - Added get_test_signal_dir()
3. `src/signal_recorder/analytics_service.py` - Added test signal CSV writing
4. `src/signal_recorder/wwvh_discrimination.py` - Fixed runtime bugs
5. `web-ui/grape-paths.js` - Added getTestSignalDir()
6. `web-ui/monitoring-server-v3.js` - Added test signal data loading
7. `web-ui/components/discrimination-charts.js` - Added test signal chart

## Next Steps

1. **Monitor at :08/:44** - Wait for next test signal minute to verify detection
2. **Check CSV Output** - Verify test signal records are being written
3. **View Web-UI** - Confirm chart displays test signal detections
4. **Analyze Performance** - Review detection confidence and SNR values

## Deployment Status

✅ **Code Deployed** - All changes committed (v5.4.0)  
✅ **Services Restarted** - MetrologyService and Web-API running  
✅ **HDF5 Output** - `/var/lib/timestd/phase2/{CHANNEL}/L2/test_signal/`  
✅ **Web UI** - physics.html Channels tab displays test signal data  

---

**Initial Implementation:** 2024-11-26  
**v5.4.0 Enhancement:** 2026-01-22  
**Status:** Complete and operational  
**Test Signal Schedule:** Minute :08 (WWV), Minute :44 (WWVH)
