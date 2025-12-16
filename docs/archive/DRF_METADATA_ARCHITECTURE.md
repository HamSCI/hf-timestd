# Digital RF Metadata Architecture
*Updated: November 16, 2025, 6:25 AM*

## Overview

The DRF Writer Service now implements proper Digital RF multi-channel architecture with parallel metadata channels following the Digital RF specification. This enables future expansion to include timing quality, data quality, and WWV-H discrimination metadata alongside the main IQ data channel.

## Digital RF Channel Structure

```
digital_rf/YYYYMMDD/STATION/RECEIVER/OBS/CHANNEL/
├── drf_properties.h5          # Main channel properties
├── rf@<timestamp>.h5           # IQ data (10 Hz complex samples)
└── metadata/
    ├── timing_quality@<timestamp>.h5         # Timing quality annotations
    ├── data_quality@<timestamp>.h5           # Completeness, gaps, packet loss
    ├── wwvh_discrimination@<timestamp>.h5    # Propagation analysis
    └── station_info@<timestamp>.h5           # Static station metadata
```

## Channel Details

### Main Data Channel (IQ Samples)
- **Sample Rate:** 10 Hz (decimated from 16 kHz)
- **Data Type:** complex64 (32-bit float I + 32-bit float Q)
- **Format:** Continuous (gaps filled with zeros)
- **Compression:** Level 9 (maximum)
- **Purpose:** Core IQ data for upload to HamSCI/GRAPE repository

### Timing Quality Metadata Channel
**File:** `timing_quality@<timestamp>.h5`  
**Sample Rate:** 10 Hz (same as main channel)

**Fields:**
```python
{
    'timing_quality': str,           # 'TONE_LOCKED', 'NTP_SYNCED', 'WALL_CLOCK'
    'time_snap_age_seconds': float,  # Age of time_snap reference
    'time_snap_source': str,         # 'WWV', 'CHU', 'WWVH'
    'time_snap_confidence': float,   # 0.0 to 1.0
    'ntp_offset_ms': float,          # NTP offset if available
    'reprocessing_recommended': bool # True if timing suspect
}
```

**Purpose:** Enables selective reprocessing and quality filtering by HamSCI scientists

### Data Quality Metadata Channel
**File:** `data_quality@<timestamp>.h5`  
**Sample Rate:** 10 Hz

**Fields:**
```python
{
    'completeness_pct': float,       # 0-100%
    'packet_loss_pct': float,        # 0-100%
    'gap_count': int,                # Number of discontinuities
    'gap_duration_ms': float,        # Total gap duration
    'samples_filled': int,           # Zero-filled samples
    'packets_received': int,
    'packets_expected': int
}
```

**Purpose:** Data provenance and quality assessment

### WWV-H Discrimination Metadata Channel
**File:** `wwvh_discrimination@<timestamp>.h5`  
**Sample Rate:** 10 Hz (one entry per minute)

**Fields:**
```python
{
    'wwv_detected': bool,            # WWV 1000 Hz tone detected
    'wwvh_detected': bool,           # WWVH 1200 Hz tone detected
    'wwv_snr_db': float,             # WWV signal strength
    'wwvh_snr_db': float,            # WWVH signal strength
    'power_ratio_db': float,         # WWV/WWVH power ratio
    'differential_delay_ms': float,  # Propagation delay difference
    'dominant_station': str,         # 'WWV', 'WWVH', 'BOTH', 'NEITHER'
    'confidence': float              # 0.0 to 1.0
}
```

**Purpose:** Ionospheric propagation analysis (path selection, fading)

### Station Info Metadata Channel
**File:** `station_info@<timestamp>.h5`  
**Cadence:** Daily (written once per day)

**Fields:**
```python
{
    'callsign': str,                 # e.g., 'AC0G'
    'grid_square': str,              # e.g., 'EM38ww'
    'receiver_name': str,            # e.g., 'GRAPE'
    'psws_station_id': str,          # HamSCI station ID
    'psws_instrument_id': str,       # HamSCI instrument ID
    'center_frequency_hz': float,    # Channel frequency
    'channel_name': str,             # e.g., 'WWV 5 MHz'
    'sample_rate_hz': int,           # 10 Hz
    'data_type': str,                # 'complex64'
    'processing_chain': str,         # Version info
    'date': str                      # ISO date
}
```

**Purpose:** Dataset documentation and provenance

## Data Flow Architecture

```
Raw 16kHz NPZ
      ↓
Analytics Service
  ├─→ Tone detection → time_snap
  ├─→ Quality metrics
  ├─→ WWV-H discrimination
  ├─→ Decimation (16kHz → 10Hz)
  └─→ 10Hz NPZ with optional metadata
            ↓
      DRF Writer Service
        ├─→ Main channel: IQ samples
        ├─→ timing: Timing quality metadata
        ├─→ quality: Data quality metadata
        ├─→ discrimination: WWV-H analysis
        └─→ station: Static metadata
              ↓
      Digital RF (HDF5)
              ↓
    Upload to HamSCI/GRAPE
```

## Enhanced 10Hz NPZ Format

The 10Hz NPZ files now support optional metadata fields:

```python
{
    # Core fields (always present)
    'iq_decimated': complex64,
    'rtp_timestamp': int,
    'sample_rate_original': 16000,
    'sample_rate_decimated': 10,
    'decimation_factor': 1600,
    'created_timestamp': float,
    'source_file': str,
    
    # Optional metadata (future expansion)
    'timing_metadata': {
        'timing_quality': str,
        'time_snap_age_seconds': float,
        # ... (all timing fields)
    },
    'quality_metadata': {
        'completeness_pct': float,
        'packet_loss_pct': float,
        # ... (all quality fields)
    },
    'discrimination_metadata': {
        'wwv_detected': bool,
        'wwvh_detected': bool,
        # ... (all discrimination fields)
    }
}
```

## Backward Compatibility

- **Current:** DRF writer works with basic 10Hz files (just IQ data)
- **Future:** Analytics service will embed metadata in 10Hz files
- **Automatic:** DRF writer detects and writes metadata if present
- **Gradual:** Can deploy DRF writer now, add metadata later

## HamSCI Upload Benefits

### Current (IQ Only)
- 10 Hz complex IQ samples
- Minimal bandwidth (1600x less than 16kHz)

### Future (With Metadata)
1. **Selective Reprocessing:** Download only high-quality segments
2. **Propagation Studies:** WWV-H discrimination for path analysis
3. **Quality Filtering:** Scientists can filter by completeness/timing
4. **Provenance:** Complete data quality and processing history
5. **Multi-use:** Same dataset supports multiple research goals

## Implementation Status

✅ **DRF Writer Service:**
- Main data channel (10 Hz IQ)
- Four metadata channels (timing, quality, discrimination, station)
- Proper Digital RF spec compliance
- Backward compatible (works without metadata)

⏳ **Analytics Service:**
- Currently creates basic 10Hz files
- Needs update to embed metadata in NPZ
- All metadata already computed, just needs export

## Next Steps

### Phase 1: Test DRF Writer (Current)
1. Verify DRF writer works with existing 10Hz files
2. Check Digital RF structure is correct
3. Validate timestamps (year 2025, not 2081!)

### Phase 2: Add Metadata Export (Future)
1. Update analytics service to write enhanced 10Hz format
2. Test metadata channels populate correctly
3. Verify HamSCI upload includes metadata

### Phase 3: HamSCI Integration
1. Coordinate with HamSCI on metadata usage
2. Implement selective download/reprocessing tools
3. Enable WWV-H discrimination analysis

## Files Modified

- ✅ `src/signal_recorder/drf_writer_service.py`
  - Added 4 metadata channels
  - Enhanced DecimatedArchive dataclass
  - Backward-compatible metadata writing

## References

- Digital RF Specification: https://github.com/MITHaystack/digital_rf
- HamSCI PSWS: Personal Space Weather Station project
- GRAPE: Global Radio Amateur Propagation Experiment
