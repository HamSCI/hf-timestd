# Timing Integration Improvements

**Date**: December 5, 2025  
**Version**: Phase 2 Temporal Engine v2.1

## Overview

This document describes the comprehensive integration of all discrimination methods into the timing solution. Previously, many detection methods (test signals, BCD, 500/600 Hz tones, etc.) were only used for display purposes. Now they actively contribute to timing accuracy and uncertainty reduction.

## Architecture: Multi-Evidence Timing

The Phase 2 engine now follows a hierarchical evidence fusion approach:

```
┌─────────────────────────────────────────────────────────────────┐
│                    TIMING EVIDENCE SOURCES                       │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐          │
│  │ Step 1:      │  │ Step 2:      │  │ Step 3:      │          │
│  │ Tone Detect  │──│ Channel Char │──│ Time Solver  │──► D_clock│
│  │ (1000/1200Hz)│  │ (BCD, FSK)   │  │ (Mode Select)│          │
│  └──────────────┘  └──────────────┘  └──────────────┘          │
│         │                 │                 │                   │
│         ▼                 ▼                 ▼                   │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │              UNCERTAINTY CALCULATION                      │  │
│  │  base × confidence × channel × doppler × improvements    │  │
│  └──────────────────────────────────────────────────────────┘  │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

## Evidence Sources by Station Type

### WWV/WWVH Channels

| Method | Timing Use | Uncertainty Impact |
|--------|------------|-------------------|
| **1000/1200 Hz Tones** | Coarse anchor (±500ms → sub-ms) | Base timing |
| **BCD Correlation** | Delay spread, quality metric | ±10-30% |
| **BCD ToA** | Dual-station cross-validation | Confidence boost |
| **Doppler Tracking** | Channel stability indicator | Scales uncertainty |
| **500/600 Hz Tones** | Ground truth station ID | -10% |
| **440 Hz Station ID** | Ground truth confirmation | -10% |
| **Test Signal FSS** | D-layer mode disambiguation | Mode selection |
| **Test Signal Delay** | Multipath severity | ±20-40% |
| **Test Signal Coherence** | Channel stability | ±20-40% |
| **Harmonic Ratios** | Receiver linearity check | Quality indicator |

### CHU Channels

| Method | Timing Use | Uncertainty Impact |
|--------|------------|-------------------|
| **1000 Hz Tone** | Coarse anchor | Base timing |
| **FSK Time Code** | **Time verification** | -40% (high confidence) |
| **FSK 500ms Boundary** | Precise timing reference | Sub-ms accuracy |
| **DUT1 Decode** | UT1-UTC correction | Earth rotation |
| **TAI-UTC Decode** | Leap second count | Absolute time |

## Detailed Integration Points

### 1. BCD Delay Spread Extraction

BCD correlation now extracts multipath delay spread from correlation peak widths:

```python
# From phase2_temporal_engine.py
wwv_spread = w.get('wwv_delay_spread_ms')
wwvh_spread = w.get('wwvh_delay_spread_ms')
if wwv_spread is not None and wwvh_spread is not None:
    result.delay_spread_ms = (wwv_spread + wwvh_spread) / 2.0
```

**Impact on Uncertainty:**
- Delay spread < 1.0 ms: -20% (very clean channel)
- Delay spread < 2.0 ms: -10% (clean channel)
- Delay spread > 5.0 ms: +30% (multipath degradation)

### 2. Test Signal Integration (WWV/WWVH)

Test signals at minutes 8 (WWV) and 44 (WWVH) provide:

```python
# FSS for mode disambiguation
fss_db = channel.test_signal_fss_db  # D-layer attenuation indicator

# Passed to TransmissionTimeSolver
solver_result = self.solver.solve(
    station=station,
    frequency_mhz=self.frequency_mhz,
    arrival_rtp=arrival_rtp,
    delay_spread_ms=delay_spread_ms,  # From BCD or test signal
    doppler_std_hz=doppler_std_hz,
    fss_db=fss_db,  # From test signal
    expected_second_rtp=expected_second_rtp
)
```

**FSS Usage in Mode Selection:**
- Negative FSS → D-layer attenuation → Favor multi-hop modes
- FSS < -2.0 dB: Multi-hop bonus +10%
- FSS < -1.0 dB: Multi-hop bonus +5%

### 3. CHU FSK Decoder

New Bell 103 compatible FSK decoder for CHU channels:

```python
# From chu_fsk_decoder.py
MARK_FREQ = 2225.0  # Hz - logic 1
SPACE_FREQ = 2025.0  # Hz - logic 0
BAUD_RATE = 300  # bits per second

# Frame A (seconds 32-39): Time of day
# Format: 6d dd hh mm ss (BCD)
day_of_year, hour, minute, second = decode_frame_a(raw_bytes)

# Frame B (second 31): Auxiliary data
# Format: xz yy yy tt aa
dut1_tenths, year, tai_utc, dst_pattern = decode_frame_b(raw_bytes)
```

**Timing Reference:**
- Last stop bit ends at EXACTLY 500ms past the second
- Provides sub-ms timing verification

**Uncertainty Reduction:**
- FSK detected: -20%
- Decode confidence > 50%: -30%
- Decode confidence > 80%: -40%
- Time verified: Additional -10%

### 4. Harmonic Ratio Analysis

Computes for ALL minutes (not just exclusive 500/600 Hz minutes):

```python
# P_1000/P_500 ratio in dB
harmonic_ratio_500_1000 = 10 * np.log10((power_1000 + 1e-12) / power_500)

# P_1200/P_600 ratio in dB
harmonic_ratio_600_1200 = 10 * np.log10((power_1200 + 1e-12) / power_600)
```

**Use Cases:**
- Detect receiver nonlinearity/overload
- Quality indicator for channel conditions

## Quality Grade Determination

Quality grades now consider all evidence sources:

| Grade | Criteria |
|-------|----------|
| **A** | Ground truth verified, CHU FSK verified, or multi-method agreement |
| **B** | High confidence with no disagreements, or test signal with moderate confidence |
| **C** | Moderate confidence with some disagreements |
| **D** | Low confidence with no verification |

**CHU FSK Special Case:**
CHU FSK time verification provides very strong evidence - it can boost even low-confidence solutions to Grade B because we're comparing decoded UTC time against expected time.

## CSV Data Formats

### Test Signal CSV (Updated)

```csv
timestamp_utc,minute_boundary,minute_number,detected,station,
confidence,multitone_score,chirp_score,snr_db,
fss_db,delay_spread_ms,toa_offset_ms,coherence_time_sec
```

New columns:
- `fss_db`: Frequency Selectivity Score (D-layer indicator)
- `delay_spread_ms`: Multipath from chirp analysis
- `toa_offset_ms`: High-precision ToA offset
- `coherence_time_sec`: Channel stability measure

### Station ID CSV (Updated)

```csv
timestamp_utc,minute_boundary,minute_number,
ground_truth_station,ground_truth_source,ground_truth_power_db,
station_confidence,dominant_station,
harmonic_ratio_500_1000,harmonic_ratio_600_1200
```

New columns:
- `ground_truth_power_db`: Actual measured power of detected tone
- `harmonic_ratio_500_1000`: P_1000/P_500 in dB
- `harmonic_ratio_600_1200`: P_1200/P_600 in dB

## Implementation Files

### New Files

- `src/grape_recorder/grape/chu_fsk_decoder.py` - CHU Bell 103 FSK decoder

### Modified Files

- `src/grape_recorder/grape/phase2_temporal_engine.py`
  - Added CHU FSK fields to ChannelCharacterization
  - Integrated CHU FSK detection in Step 2C
  - BCD delay spread extraction
  - Test signal FSS passthrough to solver
  - Enhanced uncertainty calculation
  - Enhanced quality grade determination

- `src/grape_recorder/grape/phase2_analytics_service.py`
  - Updated test signal CSV with new columns
  - Updated station_id CSV with power and harmonic ratios

- `src/grape_recorder/grape/wwvh_discrimination.py`
  - Fixed 500/600 Hz detection to compute harmonics for all minutes
  - Added `is_ground_truth_minute` flag

- `web-ui/monitoring-server-v3.js`
  - Updated test signal parsing for new CSV columns
  - Updated station_id parsing for power and harmonic ratios
  - Harmonic ratio extraction for API

## Testing

To verify the integration:

```bash
# Check harmonic ratios in station_id CSV
cat /tmp/grape-test/phase2/WWV_10_MHz/station_id_440hz/*.csv

# Check test signal data (after minute 8 or 44)
cat /tmp/grape-test/phase2/WWV_10_MHz/test_signal/*.csv

# Check API response
curl -s "http://localhost:3000/api/v1/channels/WWV%2010%20MHz/discrimination/$(date -u +%Y%m%d)/methods" | python3 -c "
import json,sys
d=json.load(sys.stdin)
m=d.get('methods',{})
print('Harmonic Ratio:', m.get('harmonic_ratio',{}).get('status'))
print('Test Signal:', m.get('test_signal',{}).get('status'))
"
```

## Future Enhancements

1. **CHU FSK CSV Output**: Add CSV storage for CHU FSK decode results
2. **Cross-Station Validation**: Use CHU FSK time to validate WWV/WWVH timing
3. **DUT1 Integration**: Apply decoded DUT1 for UT1 time recovery
4. **TAI-UTC Tracking**: Monitor leap second changes from CHU broadcast
