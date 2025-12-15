# Digital RF Writer Modes

The DRF Writer Service supports two modes for Digital RF output.

## Mode 1: Wsprdaemon-Compatible (Default)

**Purpose:** Match wsprdaemon's Digital RF output exactly for compatibility with existing PSWS infrastructure.

**Metadata Written:**
- `callsign` - Station callsign (e.g., "AC0G")
- `grid_square` - Maidenhead grid square (e.g., "EM38ww")
- `receiver_name` - Receiver name (e.g., "GRAPE")
- `center_frequencies` - Array of frequencies in Hz (e.g., `[10000000.0]`)
- `uuid_str` - PSWS station ID for dataset identification

**What's NOT included:**
- ❌ Timing quality annotations
- ❌ Gap analysis (completeness, packet loss)
- ❌ WWV/WWVH discrimination results
- ❌ Time_snap metadata

**Usage:**
```bash
python -m signal_recorder.drf_writer_service \
  --input-dir /tmp/grape-test/analytics/WWV_10_MHz/decimated \
  --output-dir /tmp/grape-test/analytics/WWV_10_MHz \
  --channel-name "WWV 10 MHz" \
  --frequency-hz 10000000 \
  --analytics-state-file /tmp/grape-test/state/analytics-WWV_10_MHz.json \
  --callsign AC0G \
  --grid-square EM38ww \
  --receiver-name GRAPE \
  --psws-station-id AC0G \
  --psws-instrument-id 0 \
  --wsprdaemon-compatible  # This is the default
```

**Output Structure:**
```
digital_rf/
└── 20251120/
    └── AC0G_EM38ww/
        └── GRAPE@AC0G_0/
            └── OBS2025-11-20T00-00/
                └── ch0/
                    ├── 2025-11-20T00-00-00.h5
                    └── metadata/
                        └── metadata_2025-11-20T00-00-00.h5  # Static station info only
```

---

## Mode 2: Enhanced Metadata

**Purpose:** Include timing quality, gap analysis, and discrimination data for advanced scientific analysis.

**Metadata Written:**
All wsprdaemon-compatible fields PLUS:

**Timing Quality Channel (`metadata/timing_quality/`):**
- `quality` - TONE_LOCKED, NTP_SYNCED, or WALL_CLOCK
- `time_snap_age_seconds` - Age of most recent WWV/CHU tone detection
- `ntp_offset_ms` - NTP synchronization offset (if applicable)
- `reprocessing_recommended` - Boolean flag for data quality

**Data Quality Channel (`metadata/data_quality/`):**
- `completeness_pct` - Percentage of expected samples received (goal: >99%)
- `packet_loss_pct` - RTP packet loss percentage
- `gaps_count` - Number of discontinuities in the data
- `gaps_filled` - Number of gaps filled with zeros

**WWV-H Discrimination Channel (`metadata/wwvh_discrimination/`):**
- `wwv_detected` - Boolean
- `wwvh_detected` - Boolean
- `power_ratio_db` - WWV/WWVH signal strength ratio
- `differential_delay_ms` - Propagation delay difference
- `dominant_station` - WWV, WWVH, BALANCED, or UNKNOWN
- `confidence` - Discrimination confidence (0.0-1.0)

**Usage:**
```bash
python -m signal_recorder.drf_writer_service \
  --input-dir /tmp/grape-test/analytics/WWV_10_MHz/decimated \
  --output-dir /tmp/grape-test/analytics/WWV_10_MHz \
  --channel-name "WWV 10 MHz" \
  --frequency-hz 10000000 \
  --analytics-state-file /tmp/grape-test/state/analytics-WWV_10_MHz.json \
  --callsign AC0G \
  --grid-square EM38ww \
  --receiver-name GRAPE \
  --psws-station-id AC0G \
  --psws-instrument-id 0 \
  --enhanced-metadata  # Enable enhanced mode
```

**Output Structure:**
```
digital_rf/
└── 20251120/
    └── AC0G_EM38ww/
        └── GRAPE@AC0G_0/
            └── OBS2025-11-20T00-00/
                └── ch0/
                    ├── 2025-11-20T00-00-00.h5
                    └── metadata/
                        ├── station_info_2025-11-20T00-00-00.h5  # Static info
                        ├── timing_quality/
                        │   └── timing_quality_2025-11-20T00-00-00.h5  # Per-minute timing
                        ├── data_quality/
                        │   └── data_quality_2025-11-20T00-00-00.h5  # Per-minute quality
                        └── wwvh_discrimination_2025-11-20T00-00-00.h5  # Per-minute discrimination
```

---

## Comparison

| Feature | Wsprdaemon Mode | Enhanced Mode |
|---------|----------------|---------------|
| **IQ Data** | ✅ 10 Hz complex | ✅ 10 Hz complex |
| **Station Info** | ✅ Basic | ✅ Extended |
| **Timing Quality** | ❌ No | ✅ Per-minute |
| **Gap Analysis** | ❌ No | ✅ Per-minute |
| **WWV-H Discrimination** | ❌ No | ✅ Per-minute |
| **Upload Compatible** | ✅ Yes | ⚠️  Verify with PSWS |
| **File Size** | Smaller | Larger (metadata overhead) |
| **Processing** | Faster | Slightly slower |

---

## Migration Path

### Phase 1: Start with Wsprdaemon Mode (RECOMMENDED)
1. Use `--wsprdaemon-compatible` (default)
2. Verify uploads to PSWS work correctly
3. Confirm data compatibility with existing tools
4. Run for at least 1 week to establish baseline

### Phase 2: Test Enhanced Mode Locally
1. Create parallel test instance with `--enhanced-metadata`
2. Compare DRF output structure
3. Verify metadata is being written correctly
4. Check file sizes and processing overhead

### Phase 3: Coordinate with PSWS Team
1. Share sample enhanced metadata files
2. Confirm metadata format is acceptable
3. Update PSWS ingestion pipeline if needed
4. Plan migration timeline

### Phase 4: Switch to Enhanced Mode (Optional)
1. Only if PSWS confirms compatibility
2. Update configuration to use `--enhanced-metadata`
3. Monitor upload success rates
4. Be prepared to roll back if issues arise

---

## Implementation Details

### NPZ File Format (Input to DRF Writer)

Both modes read the same 10 Hz NPZ format from analytics service:

```python
{
    'iq': complex64 array,              # Decimated IQ samples
    'rtp_timestamp': int,                # RTP timestamp from source
    'sample_rate_original': int,         # 16000 Hz
    'sample_rate_decimated': int,        # 10 Hz
    'decimation_factor': int,            # 1600
    'created_timestamp': float,          # Unix timestamp
    'source_file': str,                  # Original 16k NPZ filename
    
    # Optional metadata (embedded by analytics service)
    'timing_metadata': {                 # Only used in enhanced mode
        'quality': 'TONE_LOCKED',
        'time_snap_age_seconds': 30.5,
        'ntp_offset_ms': 5.2,
        'reprocessing_recommended': False
    },
    'quality_metadata': {                # Only used in enhanced mode
        'completeness_pct': 99.8,
        'packet_loss_pct': 0.2,
        'gaps_count': 1,
        'gaps_filled': 320
    },
    'tone_metadata': {                   # Only used in enhanced mode (if available)
        'detections': [...]
    }
}
```

### Digital RF Parameters

Both modes use identical DRF writer parameters for IQ data:

```python
drf.DigitalRFWriter(
    dtype=np.complex64,              # Complex IQ samples
    subdir_cadence_secs=86400,       # Daily subdirectories
    file_cadence_millisecs=3600000,  # Hourly files
    sample_rate_numerator=10,        # 10 Hz sample rate
    sample_rate_denominator=1,
    uuid_str=psws_station_id,        # Dataset UUID
    compression_level=9,             # Maximum compression
    checksum=False,
    is_complex=True,
    num_subchannels=1,
    is_continuous=True,
    marching_periods=False
)
```

---

## Troubleshooting

### "NPZ file missing both 'iq' and 'iq_decimated' fields"

**Cause:** Old NPZ format or corrupted file  
**Solution:** Check analytics service version, regenerate 10Hz NPZ files

### "Metadata writer failed: Invalid sample index"

**Cause:** Out-of-order NPZ files breaking monotonic index requirement  
**Solution:** Delete DRF output and `drf_writer_state.json`, restart service

### Enhanced metadata not appearing in DRF output

**Cause:** Analytics service not embedding metadata in 10Hz NPZ files yet  
**Solution:** Update analytics service to write `timing_metadata`, `quality_metadata` fields

### PSWS upload rejects enhanced metadata

**Cause:** PSWS ingestion pipeline doesn't recognize new metadata channels  
**Solution:** Switch back to `--wsprdaemon-compatible` mode, coordinate with PSWS team

---

## References

- **Wsprdaemon reference:** `/wsprdaemon/wav2grape.py` (lines 125-191, 178-191)
- **DRF Writer implementation:** `/src/signal_recorder/drf_writer_service.py`
- **NPZ format:** `/src/signal_recorder/analytics_service.py` (lines 948-1036)
- **Digital RF spec:** https://github.com/MITHaystack/digital_rf
