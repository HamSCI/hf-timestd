# Core Recorder Time Base Establishment - Complete Flow

## Executive Summary

**YOU ARE CORRECT** - The system WAS properly designed to establish time_snap from WWV 800ms tones or CHU 500ms tones at startup. However, **a recent fix introduced a bug** that causes analytics to create an "rtp_bootstrap" time anchor that **overrides** the proper tone-detected time_snap from the core recorder.

This document explains:
1. How the core recorder CORRECTLY establishes precise timing from tones
2. How this determines 960000-sample minute file boundaries  
3. How time_snap metadata is embedded in NPZ files
4. **The bug introduced in the analytics RTP bootstrap fix**
5. How to restore the original proper behavior

---

## Part 1: Core Recorder Time Base (✅ Working Correctly)

### Startup Sequence

#### 1. **Buffering Phase** (First 120 seconds)

When a channel processor starts:

```python
# core_recorder.py:543-560
def _handle_startup_buffering(self, rtp_timestamp, samples, gap_info):
    """Buffer samples during startup to establish time_snap"""
    # Accumulate 120 seconds of IQ samples
    self.startup_buffer.append((rtp_timestamp, samples, gap_info))
    
    if elapsed_time >= self.startup_buffer_duration:  # 120 seconds
        logger.info(f"Startup buffer complete, establishing time_snap...")
        self._establish_time_snap()
        self.startup_mode = False
```

**Key Points:**
- **120-second buffer** ensures we capture at least TWO minute marks
- Stores **RTP timestamps** for precise timing
- Records **wall clock start time** for fallback
- **No files written** during this phase (can't determine minute boundaries yet)

#### 2. **Tone Detection** (Primary Time Reference)

```python
# core_recorder.py:591-608
def _establish_time_snap(self):
    """Establish time_snap from buffered samples"""
    all_samples = np.concatenate([samples for _, samples, _ in self.startup_buffer])
    
    # Run tone detection on 120 seconds of data
    self.time_snap = self.tone_detector.detect_time_snap(
        iq_samples=all_samples,
        first_rtp_timestamp=self.startup_buffer_first_rtp,
        wall_clock_start=self.startup_buffer_start_time
    )
```

##### Tone Detection Algorithm (`startup_tone_detector.py`)

**Pattern Matching Approach:**
- Searches for **WWV 800ms tone** at 1000 Hz (second :00 of each minute)
- Searches for **WWVH 800ms tone** at 1200 Hz (second :00 of each minute)
- Searches for **CHU 500ms tone** at 1000 Hz (second :00 of each minute)

**Method:**
1. **AM Demodulation:** Extract audio envelope from IQ samples
2. **Pattern Template:** Create expected tone pattern (800ms or 500ms on, then off)
3. **Cross-Correlation:** Find pattern in 120-second buffer
4. **Rising Edge Detection:** Locate precise tone start (±1ms precision)
5. **Minute Rounding:** Round UTC time to nearest minute boundary

**Result:**
```python
StartupTimeSnap(
    rtp_timestamp=12345678,        # RTP at tone rising edge (minute :00)
    utc_timestamp=1732610400.0,    # UTC minute boundary (e.g., 10:00:00)
    sample_rate=16000,
    source='wwv_startup',          # or 'chu_startup', 'wwvh_startup'
    confidence=0.95,               # 0.0-1.0 based on SNR
    station='WWV',                 # or 'CHU', 'WWVH'
    tone_frequency=1000.0,         # Hz
    detection_snr_db=23.4,         # Signal-to-noise ratio
    
    # Pre-calculated for analytics (avoids re-detection)
    tone_power_1000_hz_db=23.4,   # WWV/CHU tone power
    tone_power_1200_hz_db=-999.0, # WWVH tone power (not detected)
    wwvh_differential_delay_ms=0.0 # Propagation delay (if both detected)
)
```

**Precision:** ±1ms based on:
- Rising edge detection using Hilbert transform
- Sub-sample interpolation
- 16 kHz sample rate (62.5 µs per sample)
- Pattern matching over 800ms/500ms duration

#### 3. **Fallback Hierarchy** (If No Tone)

```python
# core_recorder.py:609-625
if not self.time_snap:
    # FALLBACK 1: NTP-synchronized system clock
    ntp_synced, ntp_offset_ms = self._check_ntp_sync()
    if ntp_synced:
        logger.info(f"Using NTP sync (offset={ntp_offset_ms:.1f}ms)")
        self.time_snap = self.tone_detector.create_ntp_time_snap(...)
        # Precision: ±10ms
    else:
        # FALLBACK 2: Unsynchronized wall clock
        logger.warning(f"No NTP sync, using wall clock (low accuracy)")
        self.time_snap = self.tone_detector.create_wall_clock_time_snap(...)
        # Precision: ±seconds (poor)
```

**Hierarchy Summary:**
| Method | Precision | Source | Confidence |
|--------|-----------|--------|------------|
| **WWV/CHU Tone** | ±1 ms | Pattern matching tone rising edge | 0.90-0.99 |
| **NTP Sync** | ±10 ms | System clock with NTP offset | 0.50-0.70 |
| **Wall Clock** | ±seconds | System time (unsynchronized) | 0.10-0.30 |

#### 4. **Periodic Tone Checks** (Every 5 Minutes)

```python
# core_recorder.py:704-750
def _periodic_tone_check(self, current_rtp_timestamp):
    """Periodically run tone detection to update time_snap"""
    if time_since_last_check >= 300:  # 5 minutes
        # Run tone detection on 60-second buffer
        new_time_snap = self.tone_detector.detect_time_snap(...)
        
        if new_time_snap:
            # Upgrade if better source or confidence
            if self.time_snap.source in ['ntp', 'wall_clock']:
                # Always upgrade to tone-based
                self.time_snap = new_time_snap
                logger.info(f"✅ Upgraded to {new_time_snap.source}")
```

**Benefits:**
- **Improves accuracy** over time as propagation conditions change
- **Upgrades** from NTP/wall clock to tone-locked when signals return
- **Maintains precision** even if initial startup had poor propagation

---

## Part 2: Minute File Boundary Determination (✅ Working Correctly)

### NPZ Writer Initialization

After `time_snap` is established:

```python
# core_recorder.py:627-636
self.npz_writer = CoreNPZWriter(
    output_dir=self.output_dir,
    channel_name=self.description,
    frequency_hz=self.frequency_hz,
    sample_rate=self.sample_rate,
    ssrc=self.ssrc,
    time_snap=self.time_snap,  # ← Established time reference
    station_config=self.station_config
)
```

### Sample Accumulation and Minute Boundary Detection

```python
# core_npz_writer.py:88-136
def add_samples(self, rtp_timestamp, samples, gap_record=None):
    """Add samples and write minute files when complete"""
    
    # 1. Determine current minute boundary using time_snap
    if self.current_minute_timestamp is None:
        utc_timestamp = self._calculate_utc_from_rtp(rtp_timestamp)
        minute_boundary = datetime.fromtimestamp(utc_timestamp, tz=timezone.utc)
                                  .replace(second=0, microsecond=0)
        self._reset_minute_buffer(minute_boundary, rtp_timestamp)
    
    # 2. Accumulate samples
    self.current_minute_samples.extend(samples)
    
    # 3. Check if minute is complete (960,000 samples @ 16 kHz)
    if len(self.current_minute_samples) >= self.samples_per_minute:
        self.current_minute_samples = 
            self.current_minute_samples[:self.samples_per_minute]
        
        # 4. Write file
        file_path = self._write_minute_file()
        
        # 5. Start next minute
        next_utc = self._calculate_utc_from_rtp(rtp_timestamp)
        next_minute = datetime.fromtimestamp(next_utc, tz=timezone.utc)
                              .replace(second=0, microsecond=0)
        self._reset_minute_buffer(next_minute, rtp_timestamp)
```

### UTC Calculation from RTP (The Critical Function)

```python
# core_npz_writer.py:167-185
def _calculate_utc_from_rtp(self, rtp_timestamp):
    """
    Calculate UTC timestamp from RTP using fixed time_snap anchor.
    
    This is THE function that determines minute boundaries.
    """
    # Calculate elapsed RTP samples since anchor point
    rtp_elapsed = (rtp_timestamp - self.time_snap.rtp_timestamp) & 0xFFFFFFFF
    
    # Handle 32-bit RTP wrap-around
    if rtp_elapsed > 0x80000000:
        rtp_elapsed -= 0x100000000
    
    # Convert RTP samples to elapsed time
    elapsed_seconds = rtp_elapsed / self.time_snap.sample_rate
    
    # Add to anchor UTC time
    utc = self.time_snap.utc_timestamp + elapsed_seconds
    
    return utc
```

**Example:**
```
time_snap established:
  RTP: 123,450,000
  UTC: 2025-11-26 10:00:00.000 (minute boundary, tone-locked)

Current packet:
  RTP: 124,410,000
  
Calculation:
  RTP_elapsed = 124,410,000 - 123,450,000 = 960,000 samples
  elapsed_seconds = 960,000 / 16,000 = 60.0 seconds
  UTC = 10:00:00 + 60s = 10:01:00.000 ← Next minute boundary!
```

**Result:** Minute files start **exactly** at UTC minute boundaries derived from tone-locked RTP timestamps.

---

## Part 3: Metadata Embedding in NPZ Files (✅ Working Correctly)

### File Structure

Each NPZ file (`wwv10_20251126_100000.npz`) contains:

```python
# core_npz_writer.py:187-237
np.savez_compressed(
    file_path,
    
    # === IQ DATA (960,000 samples) ===
    iq=iq_array,  # Complex64 array
    
    # === TIME_SNAP (CRITICAL FOR ANALYTICS) ===
    time_snap_rtp=self.time_snap.rtp_timestamp,    # RTP at anchor
    time_snap_utc=self.time_snap.utc_timestamp,    # UTC at anchor
    time_snap_source=self.time_snap.source,        # 'wwv_startup', 'ntp', etc.
    time_snap_confidence=self.time_snap.confidence,  # 0.0-1.0
    time_snap_station=self.time_snap.station,      # 'WWV', 'CHU', 'WWVH'
    
    # === PRE-CALCULATED TONE POWERS (Avoids Re-Detection) ===
    tone_power_1000_hz_db=self.time_snap.tone_power_1000_hz_db,
    tone_power_1200_hz_db=self.time_snap.tone_power_1200_hz_db,
    wwvh_differential_delay_ms=self.time_snap.wwvh_differential_delay_ms,
    
    # === RTP METADATA ===
    rtp_timestamp=self.current_minute_rtp_start,  # RTP at file start
    rtp_ssrc=self.ssrc,
    sample_rate=self.sample_rate,
    
    # === TIMING METADATA ===
    frequency_hz=self.frequency_hz,
    channel_name=self.channel_name,
    created_timestamp=time.time(),
    
    # === GAP INFORMATION ===
    gaps_count=gaps_count,
    gaps_filled=gaps_filled_samples,
    gap_rtp_timestamps=gap_rtp_array,
    gap_sample_indices=gap_sample_indices,
    gap_samples_filled=gap_samples_filled,
    gap_packets_lost=gap_packets_lost,
    
    # === STATION CONFIG ===
    station_call=self.station_config.get('call'),
    station_grid=self.station_config.get('grid'),
    
    # === VERSION ===
    recorder_version='2.0'
)
```

**Key Point:** The `time_snap_*` fields contain **ALL timing information** needed by analytics:
- **RTP anchor point** (precise sample count)
- **UTC anchor point** (absolute time)
- **Source and confidence** (how timing was established)
- **Pre-calculated tone powers** (avoids re-detection overhead)

### Analytics Service Reads Metadata

```python
# analytics_service.py:199-221
def embedded_time_snap(self) -> Optional[TimeSnapReference]:
    """Build TimeSnapReference from embedded NPZ metadata"""
    if self.time_snap_rtp is None or self.time_snap_utc is None:
        return None
    
    return TimeSnapReference(
        rtp_timestamp=int(self.time_snap_rtp),
        utc_timestamp=float(self.time_snap_utc),
        sample_rate=self.sample_rate,
        source=str(self.time_snap_source or 'archive_time_snap'),
        confidence=float(self.time_snap_confidence or 0.0),
        station=station,
        established_at=float(self.created_timestamp)
    )
```

**This is where analytics SHOULD get its timing reference** - from the tone-detected `time_snap` embedded by the core recorder!

---

## Part 4: THE BUG - Analytics RTP Bootstrap Override

### What I Changed (INCORRECT)

In my recent RTP timestamp fix (`analytics_service.py:629-632`), I added:

```python
# Step 1.5: Bootstrap initial time anchor if none exists
if self.state.time_snap is None and self.state.files_processed == 0:
    self._create_initial_time_anchor(archive)  # ← WRONG!
```

This creates an `rtp_bootstrap` time anchor using **wall clock time**, which:
1. **Overrides** the tone-detected `time_snap` from the NPZ file
2. **Lower precision** (wall clock ±seconds vs tone ±1ms)
3. **Defeats the entire purpose** of the 120-second startup buffering

### What SHOULD Happen

```python
# Step 1.5: Adopt embedded time_snap from archive (if better than current)
time_snap = self._maybe_adopt_archive_time_snap(archive)

# The archive contains tone-detected time_snap from core recorder
# This should be adopted FIRST, before any bootstrap logic
```

### The Adoption Logic (ALREADY CORRECT)

```python
# analytics_service.py:433-456
def _maybe_adopt_archive_time_snap(self, archive):
    """Adopt time_snap embedded in NPZ if it improves current state"""
    archive_time_snap = archive.embedded_time_snap()
    
    if current is None:
        should_adopt = True  # ← Always adopt if we have none
    elif current.source == 'rtp_bootstrap' and archive_time_snap.source != 'rtp_bootstrap':
        should_adopt = True  # ← Upgrade from bootstrap
    elif current.source in ('ntp', 'wall_clock'):
        should_adopt = True  # ← Upgrade from NTP/wall clock
    ...
```

**This was already working!** The bootstrap logic I added is **unnecessary** and **harmful**.

---

## Part 5: The Fix - Remove Bootstrap Override

### Problem Code (TO BE REMOVED)

```python
# analytics_service.py:629-632 - DELETE THIS
if self.state.time_snap is None and self.state.files_processed == 0:
    self._create_initial_time_anchor(archive)
```

### Correct Behavior

The analytics should:
1. **Read embedded `time_snap`** from NPZ file (from core recorder tone detection)
2. **Adopt it immediately** if no current `time_snap` exists
3. **Use RTP-to-UTC conversion** based on the tone-detected anchor
4. **Never create bootstrap** - the core recorder already did the hard work!

### Why Bootstrap Was Added (Misunderstanding)

I added bootstrap to solve "bouncing" timestamps, but the **real problem** was:
- Analytics fell back to `unix_timestamp` when `time_snap` was None
- But `time_snap` should **never be None** if NPZ files contain embedded `time_snap`
- The adoption logic (`_maybe_adopt_archive_time_snap`) was **already correct**
- The bootstrap just introduced a **competing, inferior time source**

### Correct Solution

The analytics should **trust the core recorder's time_snap** embedded in NPZ files:

```python
# analytics_service.py:629-642 (CORRECTED)
# Step 1.5: Adopt embedded time_snap from archive FIRST
# This ensures we use tone-detected timing from core recorder
timing = self._get_timing_annotation(archive)  # Calls _maybe_adopt_archive_time_snap
```

**No bootstrap needed!** The `_get_timing_annotation` method already:
1. Calls `_maybe_adopt_archive_time_snap(archive)` 
2. Extracts embedded `time_snap` from NPZ
3. Adopts it if better than current
4. Uses it for all timestamp calculations

---

## Part 6: Timing Quality Summary

### From Core Recorder (NPZ Metadata)

| Source | When | Precision | Confidence | Notes |
|--------|------|-----------|------------|-------|
| **wwv_startup** | Tone detected at startup | ±1 ms | 0.90-0.99 | Pattern matching, 800ms WWV tone |
| **chu_startup** | Tone detected at startup | ±1 ms | 0.90-0.99 | Pattern matching, 500ms CHU tone |
| **wwvh_startup** | Tone detected at startup | ±1 ms | 0.90-0.99 | Pattern matching, 1200 Hz WWVH |
| **ntp** | No tone, NTP available | ±10 ms | 0.50-0.70 | System clock + NTP offset |
| **wall_clock** | No tone, no NTP | ±seconds | 0.10-0.30 | Unsynchronized system time |

### Analytics Should Use (Current Behavior)

| Source | Precision | When Used |
|--------|-----------|-----------|
| **Embedded tone time_snap** | ±1 ms | Always (first priority) |
| **Previous tone time_snap** | ±1 ms | If embedded unavailable |
| **Archive time_snap** | ±1-10 ms | From state persistence |

### Analytics SHOULD NOT Use (Bootstrap Bug)

| Source | Precision | Problem |
|--------|-----------|---------|
| **rtp_bootstrap** | ±seconds | Uses wall clock, defeats tone detection |

---

## Part 7: Verification

### Check Core Recorder Time Establishment

```bash
# Watch core recorder logs during startup
tail -f /tmp/grape-test/logs/core-recorder.log | grep -E "time_snap|Startup buffer|tone"

# Expected output:
# INFO: WWV 10 MHz: Startup buffer complete (120.0s), establishing time_snap...
# WARNING: 🔍 DETECT_TIME_SNAP CALLED: 1920000 samples (120.0s)
# WARNING:    Trying 1000 Hz...
# WARNING:    ✅ Detection at 1000 Hz! SNR=23.4dB
# INFO: ✅ time_snap detected: WWV tone at 2025-11-26T10:00:00+00:00, SNR=23.4dB
# INFO: WWV 10 MHz: ✅ time_snap established
# INFO:   Source: wwv_startup
# INFO:   Station: WWV
# INFO:   Confidence: 0.95
# INFO:   SNR: 23.4 dB
```

### Check NPZ File Metadata

```python
import numpy as np

# Load NPZ file
data = np.load('wwv10_20251126_100000.npz')

# Check embedded time_snap
print(f"Source: {data['time_snap_source']}")           # Should be 'wwv_startup' or 'chu_startup'
print(f"Confidence: {data['time_snap_confidence']}")   # Should be 0.90-0.99
print(f"Station: {data['time_snap_station']}")         # Should be 'WWV' or 'CHU'
print(f"RTP anchor: {data['time_snap_rtp']}")          # RTP at minute :00
print(f"UTC anchor: {data['time_snap_utc']}")          # UTC minute boundary
```

### Check Analytics Adoption

```bash
# Watch analytics logs
tail -f /tmp/grape-test/logs/analytics-wwv10.log | grep -E "time_snap|Adopted|bootstrap"

# CORRECT behavior:
# INFO: Adopted archive time_snap (wwv_startup) from wwv10_20251126_100000.npz

# WRONG behavior (bootstrap bug):
# INFO: Created initial RTP-based time anchor from wwv10_20251126_100000.npz
```

---

## Conclusion

### System Design (✅ CORRECT)

1. **Core recorder:**
   - ✅ Buffers 120 seconds at startup
   - ✅ Detects WWV/CHU tones with ±1ms precision
   - ✅ Falls back to NTP (±10ms) or wall clock (±seconds)
   - ✅ Establishes `time_snap` anchor from tone rising edge
   - ✅ Uses RTP-to-UTC conversion for minute boundaries
   - ✅ Writes 960,000-sample files at exact minute marks
   - ✅ Embeds `time_snap` metadata in NPZ files
   - ✅ Periodically re-checks tones to improve accuracy

2. **Analytics service:**
   - ✅ Reads embedded `time_snap` from NPZ files
   - ✅ Adopts tone-detected timing from core recorder
   - ✅ Uses RTP-based timestamp calculations
   - ❌ **BUG:** Creates unnecessary `rtp_bootstrap` that overrides tone timing

### The Fix Required

**REMOVE** these lines from `analytics_service.py:629-632`:

```python
# DELETE THIS BLOCK
if self.state.time_snap is None and self.state.files_processed == 0:
    self._create_initial_time_anchor(archive)
```

**REMOVE** the `_create_initial_time_anchor()` method (lines 458-484).

The existing `_maybe_adopt_archive_time_snap()` logic is **already correct** and will:
- Extract tone-detected `time_snap` from NPZ metadata
- Adopt it immediately (confidence 0.90-0.99)
- Use it for all discrimination timestamps
- Maintain ±1ms precision from the core recorder

### Summary

**You were right!** The system WAS properly designed to time_snap to WWV/CHU tones. The bootstrap fix I added yesterday was **unnecessary and harmful** - it overrides the precise tone-detected timing with imprecise wall clock timing. The core recorder does all the hard work of tone detection and embeds perfect timing metadata in the NPZ files. Analytics just needs to **trust and use it**.
