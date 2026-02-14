# Timing Quality Framework for Digital RF Upload

## Overview

The GRAPE recorder implements a **continuous upload strategy** with **quality annotations** rather than binary "upload or skip" decisions. This ensures complete scientific records while enabling selective reprocessing based on timing confidence.

## Core Philosophy

**Always Upload + Annotate Quality** 

```
┌─────────────────┐
│  NPZ Archive    │
│  (16 kHz IQ)    │
└────────┬────────┘
         │
         ▼
┌─────────────────────────────┐
│  Timing Quality Assessment  │
│  ├─ GPS-locked (time_snap)  │
│  ├─ NTP-synced (system clock)│
│  ├─ Interpolated (aged snap) │
│  └─ Wall clock (unsync'd)    │
└────────┬────────────────────┘
         │
         ▼
┌─────────────────────────────┐
│  Digital RF Upload          │
│  WITH quality metadata      │
│  - timing_quality           │
│  - time_snap_age_seconds    │
│  - ntp_offset_ms            │
│  - reprocessing_recommended │
└─────────────────────────────┘
```

### Why This Approach?

1. **No data gaps** - Propagation fades don't stop uploads
2. **Traceable quality** - Scientists know confidence for each segment
3. **Selective reprocessing** - Only reprocess low-quality segments
4. **Continuous operation** - System never stops
5. **Scientific integrity** - Full provenance and quality metrics

## Timing Quality Levels

### GPS_LOCKED (±1ms)

**Source:** WWV/CHU tone detection  
**Requirements:**
- time_snap from WWV/CHU tone within last 5 minutes
- RTP timestamp → UTC mapping with millisecond precision

**Example:**
```
2025-11-13 15:41:23 - WWV 5 MHz tone detected
time_snap established: RTP 1470021438 = 2025-11-13 15:41:00.000 UTC
Timing quality: GPS_LOCKED (age: 23 seconds)
```

**When to expect:**
- Normal operation with good propagation
- At least one WWV/CHU frequency receiving well
- Typical: 95%+ of data during daytime

### NTP_SYNCED (±10ms)

**Source:** System clock with NTP synchronization  
**Requirements:**
- NTP offset < 100ms
- NTP stratum ≤ 4 (reasonable time source)
- Validated via `ntpq` or `chronyc`

**Example:**
```
2025-11-13 15:45:00 - No recent time_snap
NTP check: offset=12.3ms, stratum=2
Timing quality: NTP_SYNCED
```

**When to expect:**
- Cold start (first 1-5 minutes before WWV/CHU detection)
- Extended propagation fade across all frequencies (rare)
- System with GPS-disciplined NTP server

### INTERPOLATED (degrades with age)

**Source:** Aged time_snap (5 minutes to 1 hour old)  
**Requirements:**
- time_snap exists but > 5 minutes old
- Still uses RTP→UTC mapping
- Accuracy degrades slowly (~1ms/hour drift typical)

**Example:**
```
2025-11-13 16:30:00 - Last time_snap at 15:45:00 (45 minutes ago)
Timing quality: INTERPOLATED (age: 2700 seconds)
Reprocessing recommended: true
```

**When to expect:**
- Multi-hour propagation fade on monitored channel
- Still acceptable for 10 Hz sampling (100ms per sample)
- Marked for reprocessing when better time_snap available

### WALL_CLOCK (±seconds)

**Source:** Unsynchronized system clock  
**Requirements:**
- No time_snap available
- NTP sync check failed or not configured

**Example:**
```
2025-11-13 00:01:00 - Cold start, no time_snap yet
NTP check: ntpq not found
Timing quality: WALL_CLOCK
Reprocessing recommended: true
```

**When to expect:**
- System without NTP configured
- Network isolated environment
- Very rare in production (<1% of data)

## Implementation Details

### Timing Assessment Algorithm

```python
def _get_timing_annotation(archive):
    """
    Determine best available timing source with quality level
    """
    current_time = time.time()
    
    # 1. Check for recent time_snap (GPS-locked)
    if state.time_snap:
        age = current_time - state.time_snap.utc_timestamp
        
        if age < 300:  # < 5 minutes
            return TimingAnnotation(
                quality=TimingQuality.GPS_LOCKED,
                utc_timestamp=calculate_from_time_snap(),
                time_snap_age_seconds=age
            )
        elif age < 3600:  # < 1 hour
            return TimingAnnotation(
                quality=TimingQuality.INTERPOLATED,
                utc_timestamp=calculate_from_time_snap(),
                time_snap_age_seconds=age,
                reprocessing_recommended=True
            )
    
    # 2. Check NTP synchronization
    ntp_synced, offset_ms = validate_ntp_sync()
    if ntp_synced:
        return TimingAnnotation(
            quality=TimingQuality.NTP_SYNCED,
            utc_timestamp=archive.unix_timestamp,
            ntp_offset_ms=offset_ms
        )
    
    # 3. Fallback to wall clock
    return TimingAnnotation(
        quality=TimingQuality.WALL_CLOCK,
        utc_timestamp=archive.unix_timestamp,
        reprocessing_recommended=True
    )
```

### NTP Validation

Checks both `ntpd` and `chrony`:

```python
def _validate_ntp_sync():
    """
    Check NTP sync via ntpq or chronyc
    
    Criteria:
    - Offset < 100ms
    - Stratum ≤ 4
    
    Returns: (is_synced, offset_ms)
    """
    # Try ntpq
    result = subprocess.run(['ntpq', '-c', 'rv'], ...)
    # Parse: offset=XX.XXX ms, stratum=N
    
    # Try chronyc if ntpq fails
    result = subprocess.run(['chronyc', 'tracking'], ...)
    # Parse: System time : X.XXXXXX seconds slow
```

### Digital RF Metadata

Each data segment includes timing metadata:

```json
{
  "completeness_pct": 99.8,
  "gap_count": 3,
  "packet_loss_pct": 0.2,
  
  "timing_quality": "gps_locked",
  "time_snap_age_seconds": 45.2,
  "ntp_offset_ms": null,
  "sample_count_error": 0,
  "reprocessing_recommended": false,
  "timing_notes": "WWV/CHU time_snap from WWV"
}
```

## Sample Count Validation

### The 960,000 Sample Invariant

At 16 kHz sampling, **every minute must contain exactly 960,000 samples**:

```
16,000 samples/second × 60 seconds = 960,000 samples/minute
```

Any deviation indicates:
- **Negative error** (fewer samples): Packet loss, network gaps
- **Positive error** (more samples): Duplicate packets, clock error

### Validation at Boundaries

```python
def validate_minute_boundary(expected, actual):
    error = actual - expected  # 960000 expected
    
    if error != 0:
        if error % 320 == 0:
            # Packet-sized error (320 samples/packet)
            cause = "packet_loss" if error < 0 else "duplicate_packets"
        else:
            cause = "timing_drift" or "clock_error"
        
        log_sample_count_error(error, cause)
    
    # Always continue - error is logged and annotated
```

This validation is built into the quality metrics already tracked in `QualityInfo`.

## Reprocessing Strategy

### Identifying Segments for Reprocessing

Query Digital RF metadata to find segments with `reprocessing_recommended=true`:

```python
# Find all WALL_CLOCK or old INTERPOLATED segments
segments_to_reprocess = []
for metadata in drf_metadata_reader:
    if metadata['reprocessing_recommended']:
        if metadata['timing_quality'] in ['wall_clock', 'interpolated']:
            segments_to_reprocess.append({
                'timestamp': metadata['timestamp'],
                'quality': metadata['timing_quality'],
                'reason': metadata['timing_notes']
            })
```

### Reprocessing Workflow

```bash
# 1. List segments needing reprocessing
python3 scripts/find_reprocessing_candidates.py --data-root /mnt/grape-data

# 2. Reprocess specific date range with established time_snap
python3 scripts/regenerate_drf_from_npz.py \
    --date 20251113 \
    --time-range 00:00-00:05 \
    --data-root /mnt/grape-data

# 3. Verify improved quality
python3 scripts/verify_drf_quality.py --date 20251113
```

## Production Configuration

### NTP Setup (Recommended)

For GPS-disciplined NTP server:

```bash
# Install chrony
sudo apt install chrony

# Configure GPS source
sudo nano /etc/chrony/chrony.conf
```

Add:
```
# GPS receiver via GPSD
refclock SHM 0 refid GPS precision 1e-1 offset 0.0 delay 0.2
refclock SHM 1 refid PPS precision 1e-7

# Allow local stratum 1
local stratum 1
```

Restart:
```bash
sudo systemctl restart chrony
chronyc tracking  # Verify GPS lock
```

### Monitoring

Check timing quality distribution:

```bash
# Summary of timing quality over last 24 hours
python3 scripts/timing_quality_report.py --date 20251113

# Expected output:
# GPS_LOCKED:    95.2% (68,544 minutes)
# NTP_SYNCED:     4.5% (3,240 minutes) 
# INTERPOLATED:   0.2% (144 minutes)
# WALL_CLOCK:     0.1% (72 minutes)
```

## Benefits Summary

| Aspect | Old Approach (Skip Upload) | New Approach (Quality Annotation) |
|--------|---------------------------|-----------------------------------|
| **Data Gaps** | Yes (during propagation fades) | No (continuous upload) |
| **Timing Accuracy** | High (when uploading) | Graduated (annotated) |
| **Reprocessing** | Manual detection of gaps | Automatic identification |
| **Scientific Value** | Incomplete record | Complete with provenance |
| **Operations** | Complex (gap management) | Simple (always upload) |
| **Monitoring** | Difficult (what's missing?) | Easy (quality dashboard) |

## Related Documents

- `DIGITAL_RF_UPLOAD_TIMING.md` - Historical context and evolution
- `docs/TIMING_ARCHITECTURE_V2.md` - Overall RTP timing design
- `docs/WWV_DETECTION.md` - Tone detection for time_snap
- `src/signal_recorder/analytics_service.py` - Implementation

## Future Enhancements

1. **Automatic quality-based reprocessing**: Daemon that monitors for time_snap establishment and reprocesses recent WALL_CLOCK segments
2. **Real-time quality dashboard**: Web UI showing current timing quality and historical distribution
3. **Sample count trending**: Track long-term clock drift and packet loss patterns
4. **Cross-validation**: Compare time_snaps from multiple stations for consistency checking
