# HF Time Standard - System Context

**Last Updated:** 2025-12-23  
**Current Version:** v3.0.1

## System Architecture

### Core Services

- **`timestd-core-recorder`**: Receives RTP streams from `radiod`, writes Digital RF archives (Phase 1)
- **`timestd-analytics`**: 9 Phase 2 processes + fusion engine for timing analysis
- **`timestd-web-ui`**: Node.js monitoring server serving real-time dashboards

### Data Flow

```
SDR → radiod → RTP/F32 → core-recorder → Hot Buffer (/dev/shm/timestd/raw_buffer)
                                               ↓ (background archiver)
                                           Cold Buffer (/var/lib/timestd/raw_buffer)
                                               ↑ (reads hot first, falls back to cold)
                                           analytics → timing CSVs → chrony SHM
                                                                   → web-ui
```

**Tiered Storage:** Core recorder writes to RAM (`/dev/shm`), background thread archives old minutes to disk. Analytics reads from hot buffer first (zero-latency), falls back to cold if needed.

---

## Recent Changes (v3.0.1 - 2025-12-23)

### Receiver Proliferation Fix ✅

**Problem:** Duplicate radiod channels created on every service restart (18+ instead of 9)  
**Root Cause:** `create_channel()` always creates new channels without checking for existing ones  
**Solution:** Discovery-before-create pattern + health monitoring

**Key Changes:**

- [`stream_recorder_v2.py`](file:///home/mjh/git/hf-timestd/src/hf_timestd/core/stream_recorder_v2.py): Discovery-before-create + health monitoring (5s check, 10s threshold)
- [`core_recorder_v2.py`](file:///home/mjh/git/hf-timestd/src/hf_timestd/core/core_recorder_v2.py): Simplified delegation
- [`tiered_storage.py`](file:///home/mjh/git/hf-timestd/src/hf_timestd/core/tiered_storage.py): Fixed dataclass parameter ordering

**Critical Principles:**

- ✅ **Always discover before creating** - Check for existing channels first
- ✅ **Match by freq/preset/rate** - Reuse existing channels when found
- ✅ **Health monitoring** - Auto-recovery from radiod restarts (10-15s)
- ❌ **Never** manually manage channels - let discovery handle it

**Test Results:**

- Clean slate: 9 channels created
- Service restart: 9 channels (reused existing)
- Radiod restart: Auto-recovery in 10-15 seconds

---

## Focus for Next Session: Timing Analysis & Chrony Integration

### Current Issue

**Problem:** Client is not feeding chrony as expected - timing data not reaching system clock discipline

### Timing Analysis Pipeline

#### Phase 2: Per-Channel Timing Analysis

Each of 9 channels runs independent timing analysis:

**Key Files:**

- [`phase2_temporal_engine.py`](file:///home/mjh/git/hf-timestd/src/hf_timestd/core/phase2_temporal_engine.py) - Main temporal analysis engine
- [`correlator_bank.py`](file:///home/mjh/git/hf-timestd/src/hf_timestd/core/correlator_bank.py) - BCD correlation and timing extraction
- [`transmission_time_solver.py`](file:///home/mjh/git/hf-timestd/src/hf_timestd/core/transmission_time_solver.py) - Solves for transmission time from BCD
- [`station_model.py`](file:///home/mjh/git/hf-timestd/src/hf_timestd/core/station_model.py) - Station-specific timing models (WWV/WWVH/CHU)
- [`propagation_engine.py`](file:///home/mjh/git/hf-timestd/src/hf_timestd/core/propagation_engine.py) - Ionospheric propagation delay estimation

**Output:** Per-channel timing CSVs at `/var/lib/timestd/phase2/{CHANNEL}/timing_analysis.csv`

#### Phase 3: Multi-Broadcast Fusion

Combines all channels into single UTC(NIST) estimate:

**Key File:**

- [`multi_broadcast_fusion.py`](file:///home/mjh/git/hf-timestd/src/hf_timestd/core/multi_broadcast_fusion.py) - Kalman filter fusion engine

**Outputs:**

- `/var/lib/timestd/phase2/fusion/fused_d_clock.csv` - Fused clock offset (D_clock)
- Chrony SHM refclock (unit 0) - System clock discipline

#### Chrony Integration

**Key File:**

- [`chrony_shm.py`](file:///home/mjh/git/hf-timestd/src/hf_timestd/core/chrony_shm.py) - Shared memory interface to chrony

**Configuration:**

- Chrony config snippet: `refclock SHM 0 refid HF poll 3 precision 1e-3`
- Update cadence: 8 seconds (matches poll 3)
- Quality filter: Only writes grades A/B/C/D to chrony

**Integration Point:**

- `multi_broadcast_fusion.py` line 1600-1625: Chrony SHM update logic
- Enabled by default: `enable_chrony=True`

### Investigation Areas

1. **Verify Chrony SHM Connection**
   - Check if `ChronySHM` connects successfully on startup
   - Verify SHM segment exists: `ipcs -m | grep 0x4e545030`
   - Check chrony config: `/etc/chrony/chrony.conf` has `refclock SHM 0`

2. **Verify Fusion Engine Output**
   - Check if fusion is producing quality grades A-D
   - Verify `fused_d_clock.csv` is being written
   - Check update frequency (should be ~8 seconds)

3. **Verify Chrony is Reading SHM**
   - Run: `chronyc sources -v` - look for "HF" reference
   - Run: `chronyc sourcestats` - check HF statistics
   - Check if HF is selected as reference: `chronyc tracking`

4. **Check Timing Analysis Quality**
   - Review per-channel timing CSVs for valid data
   - Check for gaps or errors in timing extraction
   - Verify propagation delay calculations are reasonable

### Relevant Commands

```bash
# Check chrony sources
chronyc sources -v

# Check chrony tracking (current reference)
chronyc tracking

# Check SHM segment
ipcs -m | grep 0x4e545030

# View fusion output
tail -f /var/lib/timestd/phase2/fusion/fused_d_clock.csv

# Check analytics service logs
journalctl -u timestd-analytics -f

# View per-channel timing
ls -lh /var/lib/timestd/phase2/*/timing_analysis.csv
```

---

## Key Configuration

- **Config**: `/etc/hf-timestd/timestd-config.toml`
- **Data Root**: `/var/lib/timestd/`
- **Hot Buffer**: `/dev/shm/timestd/raw_buffer/`
- **Logs**: `journalctl -u timestd-core-recorder` / `timestd-analytics` / `timestd-web-ui`

## Channel Specifications (config.toml)

9 channels: 2.5, 5, 10, 15, 20, 25 MHz (WWV/WWVH) + 3.33, 7.85, 14.67 MHz (CHU)  
All use: `preset=iq`, `sample_rate=20000`, `agc=0`, `gain=0`, `encoding=F32`

## Important Patterns

### Channel Management (v3.0.1+)

- ✅ **Always discover before creating** - Check for existing channels
- ✅ **Match by freq/preset/rate** - Reuse when possible
- ✅ **Health monitoring enabled** - Auto-recovery from radiod restarts
- ❌ **Never** manually manage channels - let discovery handle it

### Timing Analysis

- **Phase 2**: Per-channel analysis → timing CSVs
- **Phase 3**: Multi-broadcast fusion → fused D_clock + chrony SHM
- **Quality grades**: A (best) → F (worst), only A-D feed chrony
- **Update cadence**: 8 seconds (matches chrony poll interval)
