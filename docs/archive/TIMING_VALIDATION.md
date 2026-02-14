# WWV/CHU Timing Validation & Discontinuity Tracking

**Design document for ground-truth timing validation using WWV/CHU time-standard signals.**

---

## Overview

The GRAPE recorder captures WWV (Fort Collins, CO) and CHU (Ottawa, Canada) time-standard broadcasts. These signals contain **timing markers** that can validate our RTP timestamp accuracy and detect discontinuities in the data stream.

### Goals

1. **Independent timing validation**: Use WWV/CHU tones as ground truth, not just RTP timestamps
2. **Complete discontinuity tracking**: Document every gap, sync adjustment, or timing jump
3. **Scientific rigor**: Provide provenance for all timing-related data quality issues
4. **Automatic correction**: Optionally re-align to WWV tone onsets for improved accuracy

---

## Background

### The Problem

**Current state:**
- We trust RTP timestamps from radiod
- We compare RTP clock to system clock (both could be wrong)
- Missed packets create gaps we track but don't validate
- No independent ground truth for timing accuracy

**Scientific issue:**
- Timing errors invalidate ionospheric analysis
- Gaps in data must be documented for scientific integrity
- Researchers need provenance: "Is this continuous data or not?"

### The Opportunity

**WWV broadcasts:**
- **1200 Hz tone** for 1 second at the start of each minute (UTC)
- Tone onset = minute boundary (modulo propagation delay)
- Independent of RTP/system clocks

**CHU broadcasts:**
- **60 Hz tone** (300 ms duration)
- Less obvious timing marker but detectable

**Benefits:**
- Ground truth validation of RTP timestamps
- Detect clock drift before it becomes significant
- Auto-correction to UTC minute boundaries
- Scientific proof of data quality

---

## Architecture

### Parallel Processing Paths

```
RTP Packet (16 kHz IQ)
    ├─→ Main Path: 16 kHz → 10 Hz (scipy decimate 1600x)
    │                          ↓
    │                    Digital RF Writer
    │
    └─→ Tone Detection Path: 16 kHz → 3 kHz (scipy decimate 2.67x)
                                        ↓
                              WWV/CHU Tone Detector
                                        ↓
                              Timing Validation & Discontinuity Tracker
```

### Components

**1. Tone Detector** (`WWVToneDetector`, `CHUToneDetector`)
- Bandpass filter around tone frequency
- Envelope detection
- Onset/offset detection
- Duration validation

**2. Timing Validator** (`TimingValidator`)
- Compare detected onset to expected minute boundary
- Track timing errors over time
- Flag large discrepancies

**3. Discontinuity Tracker** (`DiscontinuityTracker`)
- Log all gaps, sync adjustments, resets
- Embed metadata in Digital RF
- Export discontinuity log for analysis

---

## Phase 1: MVP Implementation

### Features

**Tone Detection:**
- ✅ WWV 1200 Hz tone detection
- ✅ 3 kHz decimation path (parallel to 10 Hz main path)
- ✅ Log tone detections with timing error vs RTP
- ✅ Add to monitoring stats

**Discontinuity Tracking:**
- ✅ Track missed packets (RTP sequence gaps)
- ✅ Track sync adjustments (if implemented)
- ✅ Track buffer overflows/underflows
- ✅ Log to JSON, embed in Digital RF metadata
- ✅ Display in web UI

**No auto-correction yet** - just detection and logging.

### Implementation

#### 1. WWV Tone Detector

```python
class WWVToneDetector:
    """
    Detect WWV 1200 Hz tone onset for timing validation
    
    WWV broadcasts a 1200 Hz tone for 1 second at the start of each minute (UTC).
    This provides an independent ground truth for timing validation.
    """
    
    def __init__(self, sample_rate=3000):
        """
        Initialize detector
        
        Args:
            sample_rate: Input sample rate (Hz), should be ≥ 2.4 kHz for 1200 Hz
        """
        self.sample_rate = sample_rate
        
        # Design bandpass filter for 1200 Hz ± 50 Hz
        # WWV tone is very stable, narrow filter works well
        from scipy import signal
        self.sos = signal.butter(
            N=4,
            Wn=[1150, 1250],
            btype='band',
            fs=sample_rate,
            output='sos'
        )
        
        # Detection parameters
        self.envelope_threshold = 0.3  # Relative to max envelope
        self.min_tone_duration_sec = 0.8  # WWV tone is 1 sec, require 80%
        self.max_tone_duration_sec = 1.2  # Allow some tolerance
        
        # State
        self.last_detection_time = 0
        self.detection_count = 0
        
    def detect_tone_onset(self, iq_samples, current_unix_time):
        """
        Detect 1200 Hz tone onset in IQ sample buffer
        
        Args:
            iq_samples: Complex IQ samples (numpy array)
            current_unix_time: Unix timestamp corresponding to first sample
            
        Returns:
            tuple: (detected: bool, onset_sample_idx: int or None, timing_error_ms: float or None)
        """
        # 1. Bandpass filter around 1200 Hz
        from scipy import signal
        magnitude = np.abs(iq_samples)
        filtered = signal.sosfiltfilt(self.sos, magnitude)
        
        # 2. Envelope detection
        analytic = signal.hilbert(filtered)
        envelope = np.abs(analytic)
        
        # Normalize envelope
        max_envelope = np.max(envelope)
        if max_envelope > 0:
            envelope = envelope / max_envelope
        
        # 3. Threshold detection
        above_threshold = envelope > self.envelope_threshold
        
        # Find edges (transitions)
        edges = np.diff(above_threshold.astype(int))
        rising_edges = np.where(edges == 1)[0]
        falling_edges = np.where(edges == -1)[0]
        
        if len(rising_edges) == 0 or len(falling_edges) == 0:
            return False, None, None
        
        # Take first rising edge as onset candidate
        onset_idx = rising_edges[0]
        
        # Find corresponding falling edge
        offset_candidates = falling_edges[falling_edges > onset_idx]
        if len(offset_candidates) == 0:
            # Tone continues beyond buffer
            return False, None, None
        
        offset_idx = offset_candidates[0]
        
        # 4. Validate tone duration
        tone_duration_samples = offset_idx - onset_idx
        tone_duration_sec = tone_duration_samples / self.sample_rate
        
        if tone_duration_sec < self.min_tone_duration_sec:
            return False, None, None  # Too short
        
        if tone_duration_sec > self.max_tone_duration_sec:
            return False, None, None  # Too long
        
        # 5. Calculate timing error
        # Onset should occur at a minute boundary (0 seconds past the minute)
        onset_time = current_unix_time + (onset_idx / self.sample_rate)
        
        # Get the minute boundary
        minute_boundary = int(onset_time / 60) * 60
        
        # Calculate error (how far from the minute boundary)
        timing_error_sec = onset_time - minute_boundary
        
        # Handle case where onset is near the end of previous minute
        # (could be detected in first second of new minute due to propagation delay)
        if timing_error_sec > 30:
            timing_error_sec -= 60  # It was actually late in previous minute
        
        timing_error_ms = timing_error_sec * 1000
        
        # Update state
        self.last_detection_time = onset_time
        self.detection_count += 1
        
        return True, onset_idx, timing_error_ms
```

#### 2. Discontinuity Data Structures

```python
from dataclasses import dataclass, asdict
from enum import Enum
from typing import Optional

class DiscontinuityType(Enum):
    """Types of discontinuities in the data stream"""
    GAP = "gap"                    # Missed packets, samples lost
    SYNC_ADJUST = "sync_adjust"    # Time sync adjustment
    RTP_RESET = "rtp_reset"        # RTP sequence/timestamp reset
    OVERFLOW = "overflow"          # Buffer overflow, samples dropped
    UNDERFLOW = "underflow"        # Buffer underflow, samples duplicated

@dataclass
class TimingDiscontinuity:
    """
    Record of a timing discontinuity in the data stream
    
    Every gap, jump, or correction is logged for scientific provenance.
    """
    timestamp: float  # Unix time when discontinuity was detected
    sample_index: int  # Sample number in output stream where discontinuity occurs
    discontinuity_type: DiscontinuityType
    magnitude_samples: int  # Positive = gap/forward jump, negative = overlap/backward jump
    magnitude_ms: float  # Time equivalent in milliseconds
    
    # RTP packet info
    rtp_sequence_before: Optional[int]
    rtp_sequence_after: Optional[int]
    rtp_timestamp_before: Optional[int]
    rtp_timestamp_after: Optional[int]
    
    # Validation
    wwv_tone_detected: bool  # Was this related to WWV tone detection?
    explanation: str  # Human-readable description
    
    def to_dict(self):
        """Convert to dictionary for JSON serialization"""
        d = asdict(self)
        d['discontinuity_type'] = self.discontinuity_type.value
        return d
    
    def to_digital_rf_metadata(self):
        """Format for Digital RF metadata"""
        return {
            'timestamp_unix': self.timestamp,
            'type': self.discontinuity_type.value,
            'magnitude_samples': self.magnitude_samples,
            'magnitude_ms': self.magnitude_ms,
            'rtp_seq_gap': (self.rtp_sequence_after - self.rtp_sequence_before) 
                          if self.rtp_sequence_before is not None else None,
            'wwv_validated': self.wwv_tone_detected,
            'explanation': self.explanation
        }
```

#### 3. Discontinuity Tracker

```python
class DiscontinuityTracker:
    """
    Track and log all timing discontinuities in the data stream
    """
    
    def __init__(self, channel_name: str):
        self.channel_name = channel_name
        self.discontinuities = []  # List[TimingDiscontinuity]
        
    def add_discontinuity(self, disc: TimingDiscontinuity):
        """Add a discontinuity to the log"""
        self.discontinuities.append(disc)
        
        # Log based on severity
        if disc.discontinuity_type == DiscontinuityType.GAP:
            if abs(disc.magnitude_ms) > 100:
                logger.warning(f"{self.channel_name}: {disc.explanation}")
            else:
                logger.info(f"{self.channel_name}: {disc.explanation}")
        else:
            logger.info(f"{self.channel_name}: {disc.explanation}")
    
    def get_stats(self):
        """Get summary statistics"""
        if not self.discontinuities:
            return {
                'total_count': 0,
                'gaps': 0,
                'sync_adjustments': 0,
                'rtp_resets': 0,
                'total_samples_affected': 0,
                'total_gap_duration_ms': 0,
                'largest_gap_samples': 0,
                'last_discontinuity': None
            }
        
        gaps = [d for d in self.discontinuities if d.discontinuity_type == DiscontinuityType.GAP]
        sync_adjusts = [d for d in self.discontinuities if d.discontinuity_type == DiscontinuityType.SYNC_ADJUST]
        rtp_resets = [d for d in self.discontinuities if d.discontinuity_type == DiscontinuityType.RTP_RESET]
        
        total_samples = sum(abs(d.magnitude_samples) for d in self.discontinuities)
        total_gap_ms = sum(d.magnitude_ms for d in gaps if d.magnitude_samples > 0)
        largest_gap = max((abs(d.magnitude_samples) for d in gaps), default=0)
        
        return {
            'total_count': len(self.discontinuities),
            'gaps': len(gaps),
            'sync_adjustments': len(sync_adjusts),
            'rtp_resets': len(rtp_resets),
            'total_samples_affected': total_samples,
            'total_gap_duration_ms': total_gap_ms,
            'largest_gap_samples': largest_gap,
            'last_discontinuity': self.discontinuities[-1].to_dict() if self.discontinuities else None
        }
    
    def export_to_csv(self, output_path):
        """Export discontinuity log to CSV for analysis"""
        import csv
        
        with open(output_path, 'w', newline='') as f:
            if not self.discontinuities:
                return
            
            fieldnames = [
                'timestamp', 'sample_index', 'type', 
                'magnitude_samples', 'magnitude_ms',
                'rtp_seq_before', 'rtp_seq_after',
                'rtp_ts_before', 'rtp_ts_after',
                'wwv_validated', 'explanation'
            ]
            
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            
            for disc in self.discontinuities:
                row = {
                    'timestamp': disc.timestamp,
                    'sample_index': disc.sample_index,
                    'type': disc.discontinuity_type.value,
                    'magnitude_samples': disc.magnitude_samples,
                    'magnitude_ms': disc.magnitude_ms,
                    'rtp_seq_before': disc.rtp_sequence_before,
                    'rtp_seq_after': disc.rtp_sequence_after,
                    'rtp_ts_before': disc.rtp_timestamp_before,
                    'rtp_ts_after': disc.rtp_timestamp_after,
                    'wwv_validated': disc.wwv_tone_detected,
                    'explanation': disc.explanation
                }
                writer.writerow(row)
        
        logger.info(f"{self.channel_name}: Exported {len(self.discontinuities)} discontinuities to {output_path}")
```

---

## Phase 2: Enhanced Detection (Future)

### Features

- CHU 60 Hz tone detection
- Adaptive thresholding based on SNR
- Multi-tone validation (cross-check different channels)
- Propagation delay calibration per station
- Automatic sync correction (optional)

### Implementation Notes

**CHU 60 Hz Detector:**
```python
class CHUToneDetector:
    """Detect CHU 60 Hz tone (300 ms duration)"""
    
    def __init__(self, sample_rate=1000):
        # Very narrow bandpass for 60 Hz ± 2 Hz
        self.sos = signal.butter(N=6, Wn=[58, 62], btype='band', 
                                fs=sample_rate, output='sos')
        self.min_tone_duration_sec = 0.25  # CHU tone is 300 ms
```

**Adaptive Thresholding:**
- Estimate noise floor from non-tone periods
- Set threshold = noise_floor + 6 dB
- Adjust based on recent detection success rate

**Propagation Delay Calibration:**
- Learn expected offset per station (e.g., Fort Collins → EM38ww ≈ 8 ms)
- Store in config, apply to timing error calculation
- Track variance (ionospheric effects)

---

## Phase 3: Scientific Analysis (Future)

### Features

- Track ionospheric propagation variations
- Correlate timing changes with space weather (Kp index, solar flux)
- Export timing analysis dataset separate from raw IQ
- Multi-station correlation (when multiple GRAPE stations online)

### Example Dataset

```csv
timestamp,channel,tone_detected,timing_error_ms,snr_db,kp_index
2025-10-28T10:00:00Z,WWV-10.0,true,-2.3,25.4,3
2025-10-28T10:01:00Z,WWV-10.0,true,-2.1,25.8,3
2025-10-28T10:02:00Z,WWV-10.0,false,N/A,12.1,3  # Fading
2025-10-28T10:03:00Z,WWV-10.0,true,-8.7,22.3,4  # Disturbance
```

---

## Configuration

### TOML Config

```toml
[[recorder.channels]]
ssrc = 10000000
frequency_hz = 10000000
description = "WWV 10 MHz"
enabled = true

# NEW: Timing validation settings
[recorder.channels.timing_validation]
enabled = true
tone_type = "wwv_1200hz"  # or "chu_60hz"
expected_propagation_delay_ms = 8.0  # Station-specific
enable_auto_correction = false  # Phase 1: just log, don't correct
correction_threshold_ms = 50.0

[recorder.discontinuity_tracking]
enabled = true
export_csv = true
csv_export_interval = 3600  # Export every hour
export_directory = "/var/lib/signal-recorder/discontinuities"
```

---

## Monitoring Integration

### Stats JSON Extension

```json
{
  "channel_name": "WWV-10.0",
  "samples_received": 57600000,
  "samples_written": 57602340,
  
  "timing_validation": {
    "enabled": true,
    "tone_type": "wwv_1200hz",
    "tone_detections_total": 42,
    "tone_detections_expected": 60,
    "detection_rate": 0.70,
    "timing_error_mean_ms": -2.1,
    "timing_error_std_ms": 5.3,
    "timing_error_max_ms": 12.7,
    "last_detection_time": "2025-10-28T10:59:00Z",
    "last_timing_error_ms": -2.3
  },
  
  "discontinuities": {
    "total_count": 3,
    "gaps": 2,
    "sync_adjustments": 0,
    "rtp_resets": 0,
    "total_samples_affected": 2340,
    "total_gap_duration_ms": 146.25,
    "largest_gap_samples": 1600,
    "last_discontinuity": {
      "timestamp": "2025-10-28T11:23:45Z",
      "type": "gap",
      "magnitude_samples": 1600,
      "magnitude_ms": 100.0,
      "explanation": "Missed 1 packet, 1600 samples lost",
      "wwv_validated": false
    }
  }
}
```

### Web UI Display

```html
<!-- Timing Validation Section -->
<div class="timing-validation" v-if="rec.timing_validation && rec.timing_validation.enabled">
  <h4>⏱️ WWV Timing Validation</h4>
  
  <div class="metric-row">
    <div class="metric">
      <span class="label">Tone Detection Rate</span>
      <span class="value ${rec.timing_validation.detection_rate >= 0.8 ? 'ok' : 'warning'}">
        ${(rec.timing_validation.detection_rate * 100).toFixed(1)}%
      </span>
    </div>
    
    <div class="metric">
      <span class="label">Timing Error (mean)</span>
      <span class="value ${Math.abs(rec.timing_validation.timing_error_mean_ms) < 10 ? 'ok' : 'warning'}">
        ${rec.timing_validation.timing_error_mean_ms.toFixed(1)} ms
      </span>
    </div>
    
    <div class="metric">
      <span class="label">Timing Jitter (σ)</span>
      <span class="value">
        ${rec.timing_validation.timing_error_std_ms.toFixed(1)} ms
      </span>
    </div>
  </div>
  
  <div class="metric-detail">
    Last detected: ${rec.timing_validation.last_detection_time || 'N/A'}
  </div>
</div>

<!-- Discontinuity Section -->
<div class="discontinuities" v-if="rec.discontinuities && rec.discontinuities.total_count > 0">
  <h4>⚠️ Data Discontinuities</h4>
  
  <div class="alert ${rec.discontinuities.total_count > 5 ? 'error' : 'warning'}">
    ${rec.discontinuities.total_count} discontinuit${rec.discontinuities.total_count === 1 ? 'y' : 'ies'} detected
  </div>
  
  <div class="metric-row">
    <div class="metric">
      <span class="label">Gaps</span>
      <span class="value">${rec.discontinuities.gaps}</span>
    </div>
    
    <div class="metric">
      <span class="label">Largest Gap</span>
      <span class="value">${rec.discontinuities.largest_gap_samples} samples 
        (${(rec.discontinuities.largest_gap_samples / rec.sample_rate * 1000).toFixed(1)} ms)</span>
    </div>
    
    <div class="metric">
      <span class="label">Total Lost Time</span>
      <span class="value">${rec.discontinuities.total_gap_duration_ms.toFixed(1)} ms</span>
    </div>
  </div>
  
  ${rec.discontinuities.last_discontinuity ? `
    <div class="metric-detail">
      <strong>Last event:</strong> ${rec.discontinuities.last_discontinuity.explanation}
    </div>
  ` : ''}
</div>
```

---

## Testing Strategy

### Unit Tests

```python
def test_wwv_tone_detection():
    """Test WWV 1200 Hz tone detector with synthetic signal"""
    # Generate synthetic WWV tone
    sample_rate = 1000
    duration = 2.0  # 2 seconds
    t = np.arange(0, duration, 1/sample_rate)
    
    # Carrier + 1200 Hz modulation
    carrier_freq = 10e6
    tone_freq = 1200
    
    # Create tone burst (1 second at t=0.5 to 1.5)
    tone = np.zeros_like(t)
    tone_start_idx = int(0.5 * sample_rate)
    tone_end_idx = int(1.5 * sample_rate)
    tone[tone_start_idx:tone_end_idx] = np.cos(2 * np.pi * tone_freq * t[tone_start_idx:tone_end_idx])
    
    # Create IQ samples (tone is in magnitude)
    iq_samples = (tone + 1j * tone * 0.1) * 0.5  # Slight phase imbalance
    
    # Test detector
    detector = WWVToneDetector(sample_rate)
    detected, onset_idx, timing_error = detector.detect_tone_onset(
        iq_samples, 
        current_unix_time=0.0  # Tone starts at t=0.5
    )
    
    assert detected == True
    assert abs(onset_idx - tone_start_idx) < 5  # Within 5 samples
    assert abs(timing_error - 500) < 10  # 500 ms from minute boundary

def test_discontinuity_detection():
    """Test gap detection from RTP sequence jump"""
    tracker = DiscontinuityTracker("TEST")
    
    # Simulate gap
    disc = TimingDiscontinuity(
        timestamp=time.time(),
        sample_index=1000,
        discontinuity_type=DiscontinuityType.GAP,
        magnitude_samples=1600,
        magnitude_ms=100.0,
        rtp_sequence_before=100,
        rtp_sequence_after=102,
        rtp_timestamp_before=16000,
        rtp_timestamp_after=19200,
        wwv_tone_detected=False,
        explanation="Missed 1 packet"
    )
    
    tracker.add_discontinuity(disc)
    
    stats = tracker.get_stats()
    assert stats['total_count'] == 1
    assert stats['gaps'] == 1
    assert stats['largest_gap_samples'] == 1600
```

### Integration Test

```bash
# Record WWV for 5 minutes
signal-recorder daemon --config config/grape-test.toml &
sleep 300
pkill -INT signal-recorder

# Check for tone detections
python3 -c "
import json
with open('/tmp/signal-recorder-stats.json') as f:
    stats = json.load(f)
    
for ssrc, rec in stats['recorders'].items():
    if 'WWV' in rec['channel_name']:
        tv = rec.get('timing_validation', {})
        print(f'{rec[\"channel_name\"]}:')
        print(f'  Tone detections: {tv.get(\"tone_detections_total\", 0)}')
        print(f'  Detection rate: {tv.get(\"detection_rate\", 0)*100:.1f}%')
        print(f'  Timing error: {tv.get(\"timing_error_mean_ms\", \"N/A\")} ms')
"
```

---

## Success Metrics

**Phase 1 (MVP):**
- ✅ 70%+ WWV tone detection rate (in good conditions)
- ✅ All gaps logged with sample-level precision
- ✅ Timing error < 50 ms (mean) when tones detected
- ✅ Web UI shows tone detection status

**Phase 2 (Enhanced):**
- CHU detection working
- 90%+ detection rate (adaptive thresholds)
- Timing error < 10 ms (mean)
- Auto-correction functional (opt-in)

**Phase 3 (Scientific):**
- Ionospheric timing variation dataset published
- Multi-station correlation demonstrated
- GRAPE scientific paper references this capability

---

## References

- **WWV/WWVH Technical Info**: https://www.nist.gov/pml/time-and-frequency-division/time-distribution/radio-station-wwv
- **CHU Technical Info**: https://nrc.canada.ca/en/certifications-evaluations-standards/canadas-official-time/chu-technical-information
- **Digital RF Format**: http://digitalrf.readthedocs.io/
- **scipy.signal Documentation**: https://docs.scipy.org/doc/scipy/reference/signal.html

---

*Last Updated: 2025-10-28*
*Implementation: Phase 1 in progress*
