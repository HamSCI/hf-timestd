# Project Context: HF Time Standard (hf-timestd)

## ✅ Completed: Bootstrap Migration to Metrology (2026-02-03)

The bootstrap functionality has been migrated from the separate `BootstrapService` into the `MetrologyEngine`. This simplifies the architecture by eliminating the bootstrap phase as a distinct operational mode.

### What Changed

**Before (v5.3.x):**
- Bootstrap and Metrology were separate services
- Bootstrap ran during startup, then handed off to Metrology
- Two code paths, state handoff, buffer management duplication
- Recorder gated archiving until bootstrap locked

**After (v5.4.0):**
- Single unified `MetrologyService` handles both initial lock and ongoing measurement
- No separate "bootstrap phase"—recorder archives immediately
- In RTP mode: Trust GPS_TIME/RTP_TIMESNAP, measure tones at known times
- In Fusion mode: `FusionTimingState` manages timing lock internally

### New Architecture

```
Recorder → Always archives immediately (no bootstrap gating)
              ↓
       MetrologyService
              ↓
       MetrologyEngine
              ↓
    ┌─────────┴─────────┐
    │                   │
RTP Mode           Fusion Mode
(GPS+PPS)          (NTP only)
    │                   │
Measure at         FusionTimingState
known times        manages lock
    │                   │
    └─────────┬─────────┘
              ↓
       L1 Measurements
```

### Key Files

| File | Purpose |
|------|---------|
| `src/hf_timestd/core/fusion_timing_state.py` | **NEW** - Timing lock state for Fusion mode |
| `src/hf_timestd/core/metrology_engine.py` | DSP engine with integrated Fusion support |
| `src/hf_timestd/core/metrology_service.py` | Per-channel service, minute processing |

### Deprecated Files (moved to archive/)

| File | Status |
|------|--------|
| `bootstrap_service.py` | Deprecated - functionality in MetrologyEngine |
| `timing_bootstrap.py` | Deprecated - replaced by FusionTimingState |
| `bootstrap_rolling_buffer.py` | Deprecated - no longer needed |
| `bootstrap_timing_reference.py` | Deprecated - no longer needed |

### FusionTimingState API

```python
from hf_timestd.core.fusion_timing_state import FusionTimingState, LockTier

class LockTier(Enum):
    NONE = 0        # Wide search (±200ms)
    PROVISIONAL = 1  # Minute boundaries established (2-3 min)
    REFINED = 2      # Stable offset after averaging (10+ min)

# In MetrologyEngine (Fusion mode only):
self.fusion_state = FusionTimingState(sample_rate=24000)

# Get search window based on lock state
window_ms = self.fusion_state.get_search_window_ms()  # 200 or 100

# Feed detections
self.fusion_state.add_detection(station, timing_error_ms, ...)

# Check status
status = self.fusion_state.get_status()
# {'lock_tier': 'PROVISIONAL', 'is_locked': True, ...}
```

---

## 📊 Timing Architecture

### RTP Mode (Current Default)

```
radiod (GPS+PPS) → GPS_TIME/RTP_TIMESNAP → BinaryArchiveWriter
                                              ↓
                                        MetrologyService
                                              ↓
                                    Measure at known times
```

**Key Insight**: In RTP mode, we KNOW when second 0 is. No searching needed.

### Fusion Mode (Fallback)

```
radiod (NTP only) → RTP timestamps (stable but offset unknown)
                              ↓
                      MetrologyService
                              ↓
                    Search for tones (wide window)
                              ↓
                    Establish timing lock
                              ↓
                    Measure at known times
```

**Key Insight**: Fusion mode is just RTP mode with an initial search phase.

---

## 🎯 Timing Accuracy Hierarchy

| Level | Source | Accuracy | Current Support |
|-------|--------|----------|-----------------|
| **L5** | GPS+PPS local | ±100 ns | ✅ RTP mode |
| **L4** | GPS+PPS LAN | ±1 μs | ✅ RTP mode |
| **L3** | HF fusion | ±0.5 ms | ⚠️ Needs migration |
| **L2** | NTP | ±1-10 ms | ⚠️ Needs migration |
| **L1** | HF bootstrap | ±5-50 ms | ⚠️ Needs migration |

---

## ⚠️ Known Issues

### ~60ms Systematic Offset

GPS_TIME/RTP_TIMESNAP mapping has ~60ms offset (radiod pipeline latency).
- When buffer timing is correct: WWV error = -4ms (excellent)
- When offset present: WWV error = +60ms

**Potential Fix**: Calibrate using known tone arrivals.

### BPM False Positives

BPM (China) uses same 1000 Hz tone as WWV. Discrimination relies on:
- Expected arrival time (BPM ~40ms vs WWV ~4ms from Missouri)
- Schedule (BPM has different active hours)

---

## 📚 Reference Documentation

| Document | Purpose |
|----------|---------|
| `docs/design/TIMING_AUTHORITY_ARCHITECTURE.md` | RTP vs Fusion modes |
| `docs/design/ARRIVAL_PATTERN_MATRIX_ARCHITECTURE.md` | Physics-based validation |
| `CHANGELOG.md` | Recent changes (v5.3.13) |
| `TECHNICAL_REFERENCE.md` | System architecture |

---

## 🔍 Quick Commands

```bash
# Check metrology status
sudo systemctl status timestd-metrology

# View metrology logs (10 MHz channel)
tail -f /var/log/hf-timestd/phase2-shared10.log

# Check timing detections
grep "VALIDATED" /var/log/hf-timestd/phase2-shared10.log | tail -20

# Check CPU usage
top -bn1 | head -12

# Reinstall after code changes
sudo /opt/hf-timestd/venv/bin/pip install -e /home/mjh/git/hf-timestd
sudo systemctl restart timestd-metrology
```

---

## 📋 Config Reference

### `/etc/hf-timestd/timestd-config.toml`

```toml
[timing]
authority = "rtp"  # "rtp" or "fusion"
rtp_expected_accuracy_ms = 0.001
validation_threshold_ms = 5.0
always_run_fusion = true

[recorder]
bootstrap_enabled = false  # Disabled for RTP mode
mode = "production"
```

---

## ✅ Recent Session Summary (v5.3.13)

**Fixes Applied:**
1. CPU overload: Changed polling from 0.5s to once per minute
2. False detections: Constrained search to ±100ms window
3. Code errors: ToneDetectionResult fields, buffer_mid_time

**Results:**
- CPU: Overloaded → 73% idle
- False detections: 84% → 0%
- WWV timing (when correct): -4.0ms error
