# Digital RF Upload with Timing Quality Annotations

## Summary

GRAPE implements **continuous Digital RF upload with quality annotations** rather than binary "upload or skip" decisions. This ensures complete scientific records while enabling selective reprocessing based on timing confidence.

## Key Principle: Always Upload + Annotate Quality

```
Every NPZ archive → Always uploaded to Digital RF
                   → Annotated with timing quality
                   → Enables selective reprocessing
```

## Timing Quality Hierarchy

### 1. GPS_LOCKED (±1ms) - Best

**Source:** WWV/CHU tone detection establishes time_snap  
**When:** time_snap age < 5 minutes  
**Accuracy:** Millisecond precision via RTP→UTC mapping

```python
time_snap: RTP 1470021438 = 2025-11-13 15:41:00.000 UTC
Sample timestamp: utc = time_snap_utc + (rtp - time_snap_rtp) / sample_rate
Quality: GPS_LOCKED ✅
```

### 2. NTP_SYNCED (±10ms) - Good

**Source:** System clock synchronized via NTP  
**When:** No recent time_snap, but NTP offset < 100ms  
**Accuracy:** 10 millisecond typical (validated via ntpq/chronyc)

```bash
$ chronyc tracking
System time: 0.012 seconds slow of NTP time
Quality: NTP_SYNCED ✅
```

### 3. INTERPOLATED (degrades) - Acceptable

**Source:** Aged time_snap (5 minutes to 1 hour old)  
**When:** Propagation fade, no recent tone detection  
**Accuracy:** ~1ms/hour drift, still uses RTP→UTC

```python
Last time_snap: 45 minutes ago
Quality: INTERPOLATED ⚠️
Reprocessing recommended: true
```

### 4. WALL_CLOCK (±seconds) - Fallback

**Source:** Unsynchronized system clock  
**When:** Cold start, no NTP, no time_snap  
**Accuracy:** Seconds-level uncertainty

```python
Quality: WALL_CLOCK ⚠️
Reprocessing recommended: true
```

## Why Each Channel Has Independent RTP Clock

**Critical Discovery:** Each ka9q-radio channel has its **own independent RTP timestamp counter**.

```
Channel       RTP Timestamp (same wall-clock time)
WWV 5 MHz     304,122,240
WWV 10 MHz    302,700,560  ← Different by 1.4M samples
CHU 7.85 MHz  304,122,240
```

**Implication:** time_snap from one channel **CANNOT** be used for another channel because they have different RTP clock origins.

**Solution:** Each channel establishes its own time_snap or uses NTP/wall-clock fallback.

## Sample Count Validation

### The 960,000 Sample Invariant

Every minute at 16 kHz = 960,000 samples (exactly)

```python
if actual_samples != 960000:
    error = actual_samples - 960000
    
    if error % 320 == 0:  # Packet-sized (320 samples/packet)
        cause = "packet_loss" if error < 0 else "duplicate"
    else:
        cause = "timing_drift" or "clock_error"
    
    # Log, annotate, continue upload
```

Discrepancies are logged and annotated in metadata.

## Digital RF Metadata (Per Segment)

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

## Operational Flow

### Cold Start (0-5 minutes)

```
00:00:00 - System starts, no time_snap yet
         - NTP check: offset=15ms, stratum=2
         - Upload with NTP_SYNCED quality ✅

00:02:15 - WWV 5 MHz tone detected
         - time_snap established
         - Quality upgrades to GPS_LOCKED ✅
         - Earlier data can be reprocessed if needed
```

### Normal Operation (Daytime)

```
Good propagation:
├─ WWV tones every minute
├─ time_snap continuously updated
└─ 95%+ data with GPS_LOCKED quality ✅
```

### Propagation Fade (Nighttime)

```
Hour 1: WWV 5 MHz fades
      ├─ Use NTP_SYNCED (if available) ✅
      └─ Or INTERPOLATED (aged time_snap) ⚠️

Hour 2: WWV 10 MHz still good
      └─ 10 MHz maintains GPS_LOCKED ✅
      
Hour 3: All channels fade (rare)
      └─ Use NTP_SYNCED or INTERPOLATED ⚠️
```

**Result:** Continuous upload, no data gaps, quality annotated.

## Reprocessing Strategy

### Identify Low-Quality Segments

```python
# Query Digital RF metadata
for segment in drf_metadata:
    if segment['reprocessing_recommended']:
        print(f"{segment['timestamp']}: {segment['timing_quality']}")
```

### Reprocess with Better time_snap

```bash
# Once propagation returns and time_snap established:
python3 scripts/regenerate_drf_from_npz.py \
    --date 20251113 \
    --time-range 02:00-04:00 \
    --data-root /mnt/grape-data
```

Only segments marked `reprocessing_recommended=true` need reprocessing.

## NTP Configuration (Recommended)

For GPS-disciplined NTP:

```bash
# Install chrony
sudo apt install chrony

# Configure GPS source (/etc/chrony/chrony.conf)
refclock SHM 0 refid GPS precision 1e-1
refclock SHM 1 refid PPS precision 1e-7
local stratum 1

# Restart and verify
sudo systemctl restart chrony
chronyc tracking
```

With GPS-NTP:
- Cold start uploads: NTP_SYNCED (±10ms) instead of WALL_CLOCK (±seconds)
- Fallback during propagation fades: NTP_SYNCED instead of INTERPOLATED
- Reduces reprocessing needs

## Benefits

| Aspect | Old Approach | New Approach |
|--------|-------------|--------------|
| **Data Gaps** | Yes (during fades) | No (continuous) |
| **Timing** | Binary (good/skip) | Graduated (4 levels) |
| **Reprocessing** | Guess what's missing | Metadata identifies segments |
| **Operations** | Complex gap management | Simple continuous upload |
| **Science** | Incomplete record | Complete with provenance |

## Monitoring

Expected quality distribution (24 hours):

```
GPS_LOCKED:    95%  (good propagation, frequent tones)
NTP_SYNCED:     4%  (cold start, brief fades)
INTERPOLATED:   1%  (extended fades)
WALL_CLOCK:    <1%  (rare, only if no NTP)
```

Check with:
```bash
python3 scripts/timing_quality_report.py --date 20251113
```

## Related Documents

- `docs/TIMING_QUALITY_FRAMEWORK.md` - Detailed implementation guide
- `docs/TIMING_ARCHITECTURE_V2.md` - RTP timing design
- `docs/WWV_DETECTION.md` - Tone detection for time_snap
- `src/signal_recorder/analytics_service.py` - Implementation

## Implementation Status

✅ Implemented November 2025  
✅ Timing quality enum with 4 levels  
✅ NTP validation (ntpq/chronyc)  
✅ Quality metadata in Digital RF  
✅ Continuous upload (no gaps)  
✅ Sample count validation  
✅ Reprocessing identification
