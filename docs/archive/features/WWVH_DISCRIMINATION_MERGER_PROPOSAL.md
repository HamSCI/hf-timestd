# WWV/WWVH Discrimination Merger Proposal

## Executive Summary

Merge the `wwv_h-discrimination` package into `signal-recorder` to create a unified multi-station time signal analysis system with comprehensive discrimination capabilities, quality tracking, and automated upload.

## Current State Analysis

### signal-recorder (Current V2)
**Strengths:**
- ✅ 30-second buffered tone detection (working perfectly)
- ✅ Phase-invariant quadrature matched filtering
- ✅ Multi-station detection (WWV 1000Hz, WWVH 1200Hz, CHU 1000Hz)
- ✅ Minute file archive (16 kHz IQ, compressed .npz)
- ✅ RTP resequencing with gap filling
- ✅ Quality metrics tracking (packet loss, gaps, resequencing)
- ✅ time_snap corrections for RTP→UTC anchoring
- ✅ Web UI with live status

**Gaps:**
- ❌ No Digital RF upload path (lost when switching from V1 to V2)
- ❌ No WWV/WWVH discrimination ratio computation
- ❌ No 440 Hz tone analysis (minutes 1 and 2)
- ❌ No carrier strength measurements (RSSI, absolute power)
- ❌ No frequency-domain power ratio analysis
- ❌ No 24-hour propagation summaries

### wwv_h-discrimination (Separate Package)
**Strengths:**
- ✅ Dual discrimination approaches:
  1. Time-domain: 440 Hz tone gating (minute 1/2, absolute carrier strength)
  2. Frequency-domain: 1000/1200 Hz marker ratio (per-minute)
- ✅ Comprehensive DSP library (Goertzel, bandpass, AM demod)
- ✅ RSSI and SNR measurements
- ✅ CSV data logging with session management
- ✅ 24-hour summary visualization
- ✅ Propagation analysis and statistics

**Gaps:**
- ❌ No time_snap corrections (timing less accurate)
- ❌ No gap detection/filling (missing data not handled)
- ❌ No file archive (only CSV logs, no IQ storage)
- ❌ No upload capability
- ❌ Simpler RTP handling (no resequencing)
- ❌ No web UI

## Proposed Unified Architecture

### Three-Path Processing Pipeline

```
RTP Packets (16 kHz IQ)
         ↓
   Resequencing + Gap Fill
   (time_snap corrections)
         ↓
    ┌────┴────┬────────────┬──────────────┐
    ↓         ↓            ↓              ↓
  Path 1    Path 2      Path 3        Path 4
 Archive   Upload     Detection   Discrimination
    ↓         ↓            ↓              ↓
 16kHz    10Hz DRF   WWV/WWVH/CHU   WWV vs WWVH
  .npz     HDF5      Tone Onset     Ratio & RSSI
```

### Path 1: Archive (Current V2 - Keep As-Is)
- **Input**: 16 kHz IQ from resequencer
- **Output**: Compressed .npz files (1/minute)
- **Purpose**: Full-bandwidth preservation for reprocessing

### Path 2: Upload (From V1 - Add to V2)
- **Input**: 16 kHz IQ from resequencer
- **Decimation**: 16 kHz → 10 Hz (scipy.signal.decimate)
- **Output**: Digital RF HDF5 files
- **Purpose**: Upload to central repository (PSWS-compatible)

### Path 3: Tone Detection (Current V2 - Enhance)
- **Input**: 16 kHz IQ from resequencer
- **Buffer**: 30 seconds (:45 to :15)
- **Resampling**: 16 kHz → 3 kHz
- **Detector**: MultiStationToneDetector
  - WWV 1000 Hz (0.8s)
  - WWVH 1200 Hz (0.8s)
  - CHU 1000 Hz (0.5s)
- **Output**: Detection events with timing errors
- **Enhancement**: Add power measurements for discrimination

### Path 4: Discrimination (NEW - Merge from wwv_h-discrimination)
- **Input**: Same 16 kHz IQ stream
- **Two sub-paths**:

#### 4A: Frequency-Domain (Per-Minute)
- **Trigger**: Top of every minute (except 29, 59)
- **Window**: 0.8s starting at :00.0
- **Method**: Extract 1000 Hz and 1200 Hz power from Path 3 detection
- **Output**: 
  - WWV marker power (dB)
  - WWVH marker power (dB)
  - Ratio (dB): WWV - WWVH
  - Dominant station flag

#### 4B: Time-Domain (Hourly)
- **Trigger**: Minutes 1 and 2
- **Windows**:
  - Minute 1: :15-:59 (44s) - WWVH active, WWV silent
  - Minute 2: :15-:59 (44s) - WWV active, WWVH silent
- **Method**: Carrier strength measurement on full IQ
- **Output**:
  - RSSI (dBm) - absolute carrier strength
  - SNR (dB)
  - Noise floor (dB)
  - 440 Hz tone verification
  - Spectrum analysis

## Implementation Plan

### Phase 1: Add Digital RF Upload Path (Restore V1 Capability)
**Location**: `grape_channel_recorder_v2.py`

1. Add `DigitalRFWriter` initialization
2. Add decimator (16 kHz → 10 Hz) using scipy.signal.decimate
3. Fork samples to DRF writer in `_process_reseq_queue()`
4. Handle minute boundaries for file rotation
5. Update status reporting

**Components to port from V1**:
- `DailyDigitalRFBuffer` initialization
- Decimation chain (16k → 8k → 4k → 2k → 1k → 500 → 250 → 125 → 62.5 → 31.25 → 15.625 → 10 Hz)
- Or use scipy.signal.decimate(factor=1600, order=8)
- Digital RF writer with PSWS-compatible parameters

**Estimated effort**: 4-6 hours

### Phase 2: Enhance Tone Detection with Power Measurements
**Location**: `MultiStationToneDetector._correlate_with_template()`

Currently returns:
```python
{
    'station': 'WWV',
    'frequency_hz': 1000,
    'onset_sample_idx': 12345,
    'timing_error_ms': 7.5,
    'snr_db': 25.8
}
```

Add power measurements:
```python
{
    'station': 'WWV',
    'frequency_hz': 1000,
    'onset_sample_idx': 12345,
    'timing_error_ms': 7.5,
    'snr_db': 25.8,
    'marker_power_db': -35.2,      # NEW: Tone power relative to noise
    'noise_floor_db': -61.0,       # NEW: Background noise level
    'peak_correlation': 0.876      # NEW: Correlation peak value
}
```

**Method**: Use existing correlation peak and noise floor calculations

**Estimated effort**: 2-3 hours

### Phase 3: Add Frequency-Domain Discrimination
**Location**: New class `WWVHDiscriminator` in new file `wwvh_discrimination.py`

```python
class WWVHDiscriminator:
    """
    Compute WWV vs WWVH discrimination using frequency-domain marker analysis
    """
    
    def __init__(self, channel_name: str):
        self.channel_name = channel_name
        self.measurements = []  # List of per-minute measurements
        
    def compute_discrimination(self, detections: List[Dict]) -> Optional[Dict]:
        """
        Given WWV and WWVH detections from same minute, compute discrimination
        
        Args:
            detections: List of detection dicts from MultiStationToneDetector
            
        Returns:
            {
                'minute_timestamp': 1699401600,
                'wwv_detected': True,
                'wwv_power_db': -35.2,
                'wwvh_detected': True,
                'wwvh_power_db': -42.1,
                'ratio_db': 6.9,  # WWV - WWVH (positive = WWV stronger)
                'dominant_station': 'WWV',
                'confidence': 'high'  # based on SNR levels
            }
        """
        wwv_det = next((d for d in detections if d['station'] == 'WWV'), None)
        wwvh_det = next((d for d in detections if d['station'] == 'WWVH'), None)
        
        if not wwv_det or not wwvh_det:
            return None
            
        ratio_db = wwv_det['marker_power_db'] - wwvh_det['marker_power_db']
        dominant = 'WWV' if ratio_db > 0 else 'WWVH'
        
        # Confidence based on SNR
        min_snr = min(wwv_det['snr_db'], wwvh_det['snr_db'])
        if min_snr > 20:
            confidence = 'high'
        elif min_snr > 10:
            confidence = 'medium'
        else:
            confidence = 'low'
            
        return {
            'minute_timestamp': int(time.time() // 60 * 60),
            'wwv_detected': True,
            'wwv_power_db': wwv_det['marker_power_db'],
            'wwvh_detected': True,
            'wwvh_power_db': wwvh_det['marker_power_db'],
            'ratio_db': ratio_db,
            'dominant_station': dominant,
            'confidence': confidence
        }
```

**Integration**: Call from `grape_channel_recorder_v2._detect_wwv_in_buffer()` after tone detection

**Estimated effort**: 3-4 hours

### Phase 4: Add Time-Domain Discrimination (440 Hz Analysis)
**Location**: Extend `WWVHDiscriminator` class

Add methods:
```python
def should_measure_carrier(self, current_time: datetime) -> Tuple[bool, str, int]:
    """
    Check if we should measure carrier strength (minutes 1 or 2)
    
    Returns:
        (should_measure, station, seconds_into_window)
    """
    minute = current_time.minute % 60
    second = current_time.second
    
    # Minute 1: WWVH 440 Hz tone (WWV silent)
    if minute == 1 and 15 <= second <= 59:
        return True, 'WWVH', second - 15
        
    # Minute 2: WWV 440 Hz tone (WWVH silent)
    if minute == 2 and 15 <= second <= 59:
        return True, 'WWV', second - 15
        
    return False, None, 0

def measure_carrier_strength(self, iq_samples: np.ndarray, station: str) -> Dict:
    """
    Measure absolute carrier strength during unique transmission window
    
    Args:
        iq_samples: 16 kHz IQ samples (44 seconds = 704,000 samples)
        station: 'WWV' or 'WWVH'
        
    Returns:
        {
            'station': 'WWV',
            'rssi_dbm': -68.3,
            'power_db': 45.2,
            'noise_floor_db': -75.1,
            'snr_db': 6.8,
            'tone_440hz_detected': True,
            'tone_440hz_power_db': -45.3,
            'num_samples': 704000,
            'duration_sec': 44.0
        }
    """
    # Compute RSSI
    power_linear = np.mean(np.abs(iq_samples)**2)
    power_dbm = 10 * np.log10(power_linear) + 30  # Assuming 50Ω, 0dBFS = 0dBm
    
    # Estimate noise floor (use lower percentile)
    magnitudes = np.abs(iq_samples)
    noise_floor = np.percentile(magnitudes, 10)
    noise_floor_db = 20 * np.log10(noise_floor)
    
    # Compute SNR
    snr_db = power_dbm - noise_floor_db
    
    # Verify 440 Hz tone (AM demodulate and detect)
    audio = np.abs(iq_samples)  # Simple AM demodulation
    tone_detected, tone_power_db = self._detect_440hz_tone(audio, sample_rate=16000)
    
    return {
        'station': station,
        'rssi_dbm': power_dbm,
        'power_db': 10 * np.log10(power_linear),
        'noise_floor_db': noise_floor_db,
        'snr_db': snr_db,
        'tone_440hz_detected': tone_detected,
        'tone_440hz_power_db': tone_power_db,
        'num_samples': len(iq_samples),
        'duration_sec': len(iq_samples) / 16000
    }
```

**Integration**: Add separate buffer for minutes 1 and 2 in `grape_channel_recorder_v2.py`

**Estimated effort**: 4-5 hours

### Phase 5: Data Logging and Status Reporting

**Add to V2 recorder**:
1. CSV logging for discrimination measurements
2. Update status JSON with discrimination data
3. Add discrimination metrics to web UI

**CSV Format**:
```csv
timestamp,frequency,minute,wwv_power_db,wwvh_power_db,ratio_db,dominant,confidence,wwv_rssi_dbm,wwvh_rssi_dbm
1699401600,5000000,0,-35.2,-42.1,6.9,WWV,high,,,
1699404720,5000000,1,,,,,,-68.3,
1699404780,5000000,2,,,,,,,-75.1
```

**Status JSON additions**:
```json
{
  "discrimination": {
    "freq_domain": {
      "last_measurement": 1699401600,
      "wwv_power_db": -35.2,
      "wwvh_power_db": -42.1,
      "ratio_db": 6.9,
      "dominant_station": "WWV",
      "confidence": "high"
    },
    "time_domain": {
      "wwv_rssi_dbm": -68.3,
      "wwvh_rssi_dbm": -75.1,
      "last_wwv_measurement": 1699404780,
      "last_wwvh_measurement": 1699404720
    },
    "statistics_24h": {
      "mean_ratio_db": 5.2,
      "std_ratio_db": 2.1,
      "dominant_station": "WWV",
      "measurements_count": 720
    }
  }
}
```

**Web UI enhancements**:
- Add "Discrimination" column showing ratio (color-coded: blue=WWV, red=WWVH)
- Add "Dominant" badge (WWV/WWVH/BALANCED)
- Add 24-hour ratio plot (optional, future)

**Estimated effort**: 5-6 hours

### Phase 6: 24-Hour Summary Visualization (Optional)

**Port from wwv_h-discrimination**:
- `visualize.py` → `grape_visualization.py`
- Generate summary plots from CSV logs
- 5-row layout: RSSI, SNR, Marker Power, Ratios, Statistics

**Can be deferred** - not critical for core functionality

**Estimated effort**: 6-8 hours

## Module Organization

```
src/signal_recorder/
├── grape_rtp_recorder.py          # Main recorder manager
├── grape_channel_recorder_v2.py   # Per-channel V2 recorder (ENHANCED)
├── wwvh_discrimination.py         # NEW: WWV/WWVH discrimination
├── minute_file_writer.py          # Minute archive (current)
├── digital_rf_writer.py           # NEW: Digital RF upload
├── quality_metrics.py             # Quality tracking (current)
├── multistation_tone_detector.py  # Tone detection (ENHANCED)
└── grape_visualization.py         # NEW: 24-hour summaries (optional)
```

## Benefits of Merger

### For WWV/WWVH Discrimination
✅ Better timing accuracy (time_snap corrections)
✅ Gap detection and handling
✅ Full IQ archive for reprocessing
✅ Web UI integration
✅ Quality metrics

### For Signal Recorder
✅ Complete discrimination capability
✅ RSSI and carrier strength measurements
✅ Propagation analysis
✅ 24-hour visualization
✅ Digital RF upload restored

### For Both
✅ Single unified codebase
✅ Shared infrastructure
✅ Consistent data management
✅ Better maintainability

## Total Estimated Effort

- Phase 1 (Digital RF): 4-6 hours
- Phase 2 (Power measurements): 2-3 hours
- Phase 3 (Freq discrimination): 3-4 hours
- Phase 4 (Time discrimination): 4-5 hours
- Phase 5 (Logging/UI): 5-6 hours
- Phase 6 (Visualization): 6-8 hours (optional)

**Core functionality (Phases 1-5)**: 18-24 hours
**With visualization (Phases 1-6)**: 24-32 hours

## Recommended Approach

1. **Start with Phase 1** (Digital RF) - Restores complete 3-path architecture
2. **Then Phase 2** (Power measurements) - Minimal change, big value
3. **Then Phase 3** (Freq discrimination) - Leverages existing tone detection
4. **Defer Phases 4-6** until core paths proven - Can validate with freq-domain first

This gets you to a working 3-path system with basic discrimination in ~10 hours.

## Questions for Discussion

1. **Digital RF parameters**: Keep V1 decimation chain or use scipy.signal.decimate?
2. **CSV vs database**: Continue with CSV logs or move to SQLite/PostgreSQL?
3. **Visualization**: In-browser (D3.js/plotly) or matplotlib PNG generation?
4. **Upload schedule**: Real-time or batch (end of day)?
5. **Discrimination channels**: All 9 channels or just 2.5, 5, 10, 15 MHz?

## Success Criteria

After merger, the system should:
- ✅ Detect WWV/WWVH/CHU tones with 80%+ success rate
- ✅ Generate 10 Hz Digital RF files for upload
- ✅ Archive 16 kHz IQ minute files
- ✅ Compute WWV/WWVH discrimination ratio every minute
- ✅ Measure absolute carrier strength hourly (minutes 1 and 2)
- ✅ Display discrimination status in web UI
- ✅ Log all measurements to CSV
- ✅ Generate 24-hour summaries (optional)
