# Session Summary: 2026-02-05 Timing Fix

## Overview

This session fixed the ~70ms systematic timing offset in radiod and verified correct timing across all 9 hf-timestd channels.

---

## 1. Radiod GPS_TIME/RTP_TIMESNAP Timing Fix

### Problem
GPS_TIME and RTP_TIMESNAP were captured asynchronously in radiod status packets:
- GPS_TIME: Captured at status packet generation time ("now")
- RTP_TIMESNAP: From samples that arrived earlier (pipeline delay ~70ms)

This caused:
1. Systematic offset equal to FFT pipeline latency
2. Per-channel RTP offsets making cross-channel timing inconsistent
3. WWV appearing to arrive AFTER WWVH (physically impossible)

### Solution
Implemented in `ka9q-radio` branch `fix-gps-rtp-timing-alignment`:

1. **radio.h**: Added `gps_time_snapshot` and `samples_at_snapshot` fields to `struct frontend`
2. **All frontend drivers**: Capture atomic (GPS_TIME, samples) snapshot every second in sample callback
3. **radio_status.c**: Report GPS_TIME and RTP_TIMESNAP from snapshot - identical for all channels with same sample rate
4. **linear.c**: Initialize RTP timestamp from `filter.out.sample_index / decimation` (accounts for pipeline delay)

### Key Insight
The `filter.out.sample_index` tracks the input sample index of samples being OUTPUT, not the current input sample count. This accounts for pipeline buffering automatically.

### Files Modified
- `src/radio.h` - Added snapshot fields
- `src/radio_status.c` - Report uniform (GPS_TIME, RTP_TIMESNAP) pairs
- `src/linear.c` - Initialize RTP from filter's sample_index
- `src/rx888.c`, `src/airspy.c`, `src/airspyhf.c`, `src/bladerf.c`, `src/fobos.c`, `src/funcube.c`, `src/hackrf.c`, `src/hydrasdr.c`, `src/rtlsdr.c`, `src/sdrplay.c`, `src/sig_gen.c` - Capture GPS time snapshot

### Documentation
- `GPS_TIME_TIMING_FIX.md` - Detailed explanation for upstream submission

### Verification Results
- **GPS_TIME spread across channels**: 0.0ms (all identical)
- **RTP_TIMESNAP spread**: 0 samples (all identical for same sample rate)
- **WWV/WWVH order**: Consistently correct (WWV arrives before WWVH)
- **Between-channel consistency (WWV 2.5-15 MHz)**: Mean spread 0.9ms, max 1.8ms
- **Between-channel consistency (CHU 3.33-14.67 MHz)**: Mean spread 1.7ms

### Status
- ✅ Patch applied and verified
- ✅ Committed to local branch
- ⏳ Awaiting Phil Karn's approval for upstream PR

---

## 2. Metrology Service Synchronization Fix

### Problem
The metrology service was looking for files before they were written:
- Used a 2-minute delay, but files are written at END of each minute
- Race condition between file writing and file reading
- inotify doesn't work reliably on tmpfs (RAM disk)

### Solution
Implemented hybrid approach in `metrology_service.py`:

1. **inotify-based file watching** (attempted first via watchdog)
2. **5-second polling fallback** (since inotify unreliable on tmpfs)
3. **Backlog processing** at startup to catch missed files

### Key Changes
```python
# New imports
import threading
import queue
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# MinuteFileHandler class for inotify events
class MinuteFileHandler(FileSystemEventHandler):
    def on_created(self, event):
        # Detect .json files (written after .bin.zst)
        if path.suffix == '.json' and path.stem.isdigit():
            self.file_queue.put((minute_boundary, path.parent))

# Hybrid run mode
def _run_inotify_mode(self):
    self._process_backlog()
    while self.running:
        try:
            minute_boundary, _ = self._file_queue.get(timeout=2.0)
            # Process file
        except queue.Empty:
            # Fallback: poll every 5 seconds
            self._poll_for_new_files()
```

### Status
- ✅ Files now being detected and processed
- ✅ Polling fallback working (inotify events not triggering on tmpfs)

---

## 3. Detection Methodology Review

### Summary
Created comprehensive review document: `docs/DETECTION_METHODOLOGY_REVIEW.md`

### Key Findings

| Area | Assessment |
|------|------------|
| **Redundancy (Path A vs B)** | NOT redundant - complementary purposes |
| **Circularity** | NO circularity - physics-first design |
| **Templates** | Correctly configured for each station |
| **SNR Thresholds** | Need empirical validation |
| **Missed Opportunities** | CHU FSK timing, phase tracking |
| **Edge Cases** | Leap seconds not handled |

### Recommendations
1. **High Priority:** Empirically validate SNR thresholds, add leap second handling
2. **Medium Priority:** Integrate CHU FSK timing, implement phase-based refinement
3. **Low Priority:** Feed Doppler into arrival matrix

---

## 4. Resolved Issues

### 4.1 Timing Snapshots - RESOLVED
- Root cause: `binary_archive_writer.py` locked timing at startup, never updated
- Fix: Continuously update `_gps_time_unix` and `_rtp_timesnap` on each status packet
- Result: Timing now tracks drift correctly

### 4.2 Large Timing Errors - RESOLVED
- Root cause: radiod's GPS_TIME/RTP_TIMESNAP mismatch (pipeline delay not accounted for)
- Fix: Use `filter.out.sample_index` for RTP initialization, report uniform snapshot
- Result: Timing errors now <50ms (ionospheric variation), WWV/WWVH order correct

---

## 5. Files Changed This Session

### ka9q-radio
- `src/radio.h` - Added `gps_time_snapshot`, `samples_at_snapshot` fields
- `src/radio_status.c` - Report uniform (GPS_TIME, RTP_TIMESNAP) from snapshot
- `src/linear.c` - Initialize RTP from `filter.out.sample_index / decimation`
- `src/rx888.c`, `src/airspy.c`, `src/airspyhf.c`, `src/bladerf.c`, `src/fobos.c`, `src/funcube.c`, `src/hackrf.c`, `src/hydrasdr.c`, `src/rtlsdr.c`, `src/sdrplay.c`, `src/sig_gen.c` - Capture GPS time snapshot every second
- `GPS_TIME_TIMING_FIX.md` (new) - Documentation for upstream submission

### hf-timestd
- `src/hf_timestd/core/binary_archive_writer.py` - Continuously update timing (not lock at startup)
- `src/hf_timestd/core/metrology_service.py` - inotify + polling hybrid for file detection
- `src/hf_timestd/core/stream_recorder_v2.py` - Re-discover channels for fresh timing snapshots
- `docs/DETECTION_METHODOLOGY_REVIEW.md` (new)
- `docs/changes/SESSION_2026_02_05_TIMING_FIX.md` (new)

---

## 6. Next Steps

1. **Submit radiod patch upstream** - Awaiting Phil Karn's approval
2. **Monitor long-term stability** - Verify timing remains consistent over days
3. **Implement remaining recommendations** - CHU FSK timing, leap second handling

---

*Session conducted 2026-02-05, 10:00 UTC - 2026-02-06, 01:30 UTC*
