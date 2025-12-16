# GRAPE Quality-Tracked Recording Architecture

## Overview

This document describes the new architecture for GRAPE/PSWS data recording with comprehensive quality tracking and scientific provenance.

## Architecture Goals

1. **Preserve Full Bandwidth** - Store complete 8 kHz IQ data for reprocessing
2. **Quality Tracking** - Track every gap, discontinuity, and timing error
3. **Scientific Provenance** - Complete audit trail for published results
4. **Wsprdaemon Compatible** - Produce identical Digital RF output
5. **Python Native** - Maintainable, no external dependencies (except scipy)

## Data Pipeline

### Phase 1: Real-time Recording (Per-Minute)

```
RTP Multicast (239.192.152.141:5004)
↓
Parse RTP packets (8 kHz complex IQ, 80 samples/packet)
↓
Gap detection & filling (track discontinuities)
↓
Accumulate 60 seconds (480,000 samples)
↓
Write compressed minute file (.npz)
  • Format: numpy compressed (blosc)
  • Size: ~1-2 MB per minute
  • Total: ~2 GB per channel per day
↓
WWV timing analysis (parallel path)
  • Bandpass 900-1100 Hz
  • Detect 1000 Hz tone onset
  • Measure timing error vs UTC
↓
Track quality metrics
  • Completeness percentage
  • Packet loss
  • RTP jitter
  • Signal power
  • WWV timing errors
```

**Memory usage:** Only 1 minute buffered (~4 MB per channel)

### Phase 2: Quality Metrics Export (Per-Minute)

For each completed minute:
- Update `MinuteQualityMetrics`
- Track discontinuities
- Log WWV detections
- Calculate signal statistics

At end of day (or on-demand):
- Export `minute_quality.csv` (per-minute metrics)
- Export `discontinuities.csv` (every gap/reset)
- Export `daily_summary.json` (aggregate statistics)

### Phase 3: Daily Post-Processing (00:05 UTC)

```
Load 1440 minute files for the day
↓
Concatenate (fill any missing minutes with zeros)
↓
High-quality decimation (3-stage FIR)
  • Stage 1: 8000 → 800 Hz (q=10)
  • Stage 2: 800 → 80 Hz (q=10)
  • Stage 3: 80 → 10 Hz (q=8)
↓
Create Digital RF format
  • 1-hour subdirectories
  • 1-second files
  • Compression level 6
  • Wsprdaemon-compatible structure
↓
Embed quality metadata in Digital RF
↓
Queue for upload to PSWS
```

**Processing time:** ~5-10 minutes per channel (CPU-bound)

## File Structure

```
/var/lib/signal-recorder/
  data/                              # Full-bandwidth archive
    YYYYMMDD/
      CALLSIGN_GRID/
        RECEIVER_NAME/
          WWV_2.5_MHz/
            20251103T000000Z_2500000_iq.npz    # 1-minute file
            20251103T000100Z_2500000_iq.npz
            ...                                # 1440 files per day
            
  analytics/
    quality/
      YYYYMMDD/
        WWV_2.5_MHz_minute_quality_20251103.csv
        WWV_2.5_MHz_discontinuities_20251103.csv
        WWV_2.5_MHz_daily_summary_20251103.json
        
  processed/                         # Digital RF output
    YYYYMMDD/
      WWV_2.5_MHz/
        YYYY-MM-DDTHH-MM-SS/         # 1-hour subdirs
          rf@TIMESTAMP.h5            # 1-second files
        metadata/
          metadata@TIMESTAMP.h5      # With quality extensions
```

## Quality Metadata Format

### Per-Minute Metrics CSV

```csv
timestamp_utc,minute_start,samples,completeness_pct,packets_rx,packets_drop,packet_loss_pct,gaps,gap_duration_ms,rtp_jitter_ms,wwv_detected,wwv_error_ms,signal_power_db,notes
1730678400.0,2025-11-03T00:00:00Z,480000,100.0,6000,0,0.0,0,0.0,0.23,True,-2.3,-42.5,""
```

### Discontinuities CSV

```csv
timestamp_utc,sample_index,type,magnitude_samples,magnitude_ms,rtp_seq_before,rtp_seq_after,rtp_ts_before,rtp_ts_after,explanation
1730678471.234,479832,GAP,18,2.25,15234,15238,243744000,243744640,"3 packets dropped, filled with zeros"
```

### Daily Summary JSON

```json
{
  "date_utc": "2025-11-03",
  "channel_name": "WWV 2.5 MHz",
  "frequency_hz": 2500000.0,
  "minutes_recorded": 1438,
  "data_completeness_percent": 99.86,
  "total_gaps": 12,
  "total_gap_duration_sec": 8.4,
  "wwv_detection_rate_percent": 98.5,
  "wwv_timing_error_mean_ms": -1.8,
  "wwv_timing_error_std_ms": 2.1,
  "packet_loss_percent": 0.08,
  "quality_metadata": {
    "version": "1.0"
  }
}
```

## Usage

### 1. Real-time Recording

```bash
# Start daemon with new recorder
signal-recorder daemon --config /etc/signal-recorder/config.toml
```

This will:
- Write 1-minute .npz files to `/var/lib/signal-recorder/data/`
- Track quality metrics in real-time
- Perform WWV timing analysis

### 2. Daily Post-Processing

```bash
# Process yesterday's data
python -m signal_recorder.process_daily_grape \
  --date 20251103 \
  --archive-dir /var/lib/signal-recorder/data \
  --output-dir /var/lib/signal-recorder/processed \
  --analytics-dir /var/lib/signal-recorder/analytics \
  --config /etc/signal-recorder/config.toml
```

This will:
- Load all 1440 minute files
- Decimate to 10 Hz
- Create Digital RF format
- Embed quality metadata

### 3. Generate Quality Report

```bash
# Generate PDF report
python scripts/generate_quality_report.py \
  --analytics-dir /var/lib/signal-recorder/analytics \
  --date 20251103 \
  --channel "WWV 2.5 MHz" \
  --output quality_report_20251103_WWV2.5.pdf
```

## Quality Metrics Explained

### Data Completeness
- **100%** = All 1440 minutes present with all samples
- **99-100%** = Excellent (minor packet loss)
- **95-99%** = Good (some gaps)
- **<95%** = Poor (significant data loss)

### Packet Loss
- **0-0.1%** = Excellent
- **0.1-1%** = Acceptable
- **>1%** = Poor (network issues)

### WWV Timing Error
- **±2 ms** = Excellent
- **±5 ms** = Good
- **±10 ms** = Acceptable
- **>±10 ms** = Poor (clock issues)

### WWV Detection Rate
- **>95%** = Excellent
- **90-95%** = Good
- **80-90%** = Fair (SNR issues?)
- **<80%** = Poor (check receiver)

## Benefits Over Previous Architecture

| Feature | Old (DailyBuffer) | New (MinuteFiles) |
|---------|-------------------|-------------------|
| Memory usage | 62 MB (9 channels) | 4 MB (buffered) |
| Data loss risk | Up to 24 hours | <1 minute |
| Reprocessing | Not possible | Full bandwidth preserved |
| Quality tracking | Basic | Comprehensive |
| Timing analysis | Limited | Full WWV tracking |
| Disk I/O | Bursty (hourly) | Continuous (per-minute) |
| Storage | ~7 MB/day | ~2 GB/day (compressed) |

## Compatibility

### Wsprdaemon Compatibility
- ✅ Digital RF format identical
- ✅ Metadata structure compatible
- ✅ File organization matches
- ✅ Sample rate (10 Hz) matches
- ✅ Additional quality metadata backward-compatible

### PSWS Upload
- ✅ Can upload Digital RF directly
- ✅ Quality metadata embedded (optional)
- ✅ No changes to upload protocol needed

## Future Enhancements

1. **Real-time Quality Dashboard** - Web UI showing live metrics
2. **Automated Quality Alerts** - Email/SMS on poor quality
3. **Cross-Station Correlation** - Compare timing between stations
4. **Machine Learning** - Predict and prevent quality issues
5. **Adaptive Filtering** - Optimize decimation per channel conditions

## References

- Digital RF: https://github.com/MITHaystack/digital_rf
- Wsprdaemon: https://github.com/rrobinett/wsprdaemon
- PSWS Network: http://pswsnetwork.eng.ua.edu/
- HamSCI: https://hamsci.org/
