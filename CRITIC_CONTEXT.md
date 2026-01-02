# NEVER CHANGE THE FOLLOWING PRIMARY INSTRUCTION

Primary Instruction:  In this context you will perform a critical review of the HF Time Standard (hf-timestd) project, either in its entirety or in a specific component, as specified by the user.  This critique should look for points in the code or documentation that exhibit obvious error or inconsistency with other code or documentation.  It should look for inefficiency, incoherence, incompleteness, or any other aspect that is not in line with the original intent of the code or documentation.  It should also look for obsolete, deprecated, or "zombie" code that should be removed.  Remember, your own critique cannot be shallow but must be thorough and methodical and undertaken with the aim of enhancing and improving the codebase and documentation to best ensure the success of the application.

# The following secondary instruction and information will guide your critique in this particular session (the instructions below will vary from session to session)

---

## ✅ SESSION COMPLETE (2026-01-02): FUSION VULNERABILITY FIXES

**Status:** 🟢 **RESOLVED** - Critical vulnerabilities in Fusion service fixed (VTEC safety, Global Solver check, HDF5 parity, Warmup penalty removal).

**Author:** AI Agent (Antigravity)
**Date:** 2026-01-02

### Main Accomplishments

1. **VTEC Safety**: Implemented consistency checks before boosting confidence in GNSS VTEC corrections.
2. **Robustness**: Removed "God Mode" immunity for Global Solver; it is now subject to outlier rejection.
3. **HDF5 Parity**: Harmonized HDF5 reader to accept Grade D measurements, preventing data starvation during fallback.
4. **Availability**: Removed artificial 3-hour warmup penalty when calibration is loaded from disk.

---

## ✅ SESSION COMPLETE (2026-01-02 22:00 UTC): FUSION BUG FIXED, UPSTREAM ISSUE IDENTIFIED

**Status:** 🟢 **FUSION FIXED** | 🔴 **UPSTREAM RTP PACKET LOSS BLOCKING PIPELINE**

**Author:** AI Agent (Cascade)
**Date:** 2026-01-02 22:00 UTC

### Summary

**Primary Issue Resolved:** Fixed critical `UnboundLocalError` in Fusion service that was causing crashes every 60 seconds.

**Root Cause Discovered:** Analytics stopped writing HDF5 at 20:12 UTC due to **83% RTP packet loss** from upstream recorder. This is a network/hardware issue, not a code bug.

### Fixes Implemented

**1. Fusion Service Bug (CRITICAL)**
- **File:** `src/hf_timestd/core/multi_broadcast_fusion.py`
- **Bug:** `UnboundLocalError: cannot access local variable 'has_verified_global' where it is not associated with a value` at line 2107
- **Root Cause:** Variable used before definition (defined at line 2142, used at line 2107)
- **Fix:** Moved variable definition to line 2096 (before first use)
- **Impact:** Fusion service was crashing every 60 seconds, preventing any fusion calculations
- **Status:** ✅ Fixed, deployed to production, service running successfully

### Investigation Findings

**Timeline of Events:**
- **17:11 UTC:** All services started normally
- **20:12 UTC:** Core recorder restarted (per systemd)
- **20:12 UTC:** Analytics stopped writing HDF5 (last timestamp in files)
- **20:12-22:18 UTC:** Massive RTP packet loss (~83%) preventing data processing
- **21:25 UTC:** Investigation started (misdiagnosed as Analytics bug)
- **22:00 UTC:** Fusion bug fixed, upstream packet loss identified

**Current System State (22:18 UTC):**
```
Core Recorder:  ⚠️  Running, but losing 83% of RTP packets
                    Expected: 1,440,000 samples/min
                    Receiving: 240,000 samples/min (16.7%)
                    Symptoms: "Lost packet recovery" warnings (15k+ sample gaps)
                             "RTP offset drift detected" on all channels
                    
Analytics:      ⚠️  Running, but cannot process incomplete minutes
                    HDF5 files exist with data up to 20:12 UTC:
                    - SHARED_10000: 1202 records, last=2026-01-02T20:12:00Z
                    - SHARED_5000: 1206 records, last=2026-01-02T20:12:00Z
                    - CHU_3330: 1178 records, last=2026-01-02T20:12:00Z
                    Logs: "Incomplete minute: 240000/1440000 (16.7%)"
                    
Fusion:         ✅  FIXED and running successfully
                    Output: +0.453ms ± 1.108ms [69 broadcasts, grade C]
                    Using CSV fallback (HDF5 data is stale)
                    No crashes since fix deployed
```

**HDF5 Schema Verification:**
- ✅ Analytics writing correct schema (new format: individual datasets)
- ✅ Files have proper structure: `timestamp_utc`, `clock_offset_ms`, etc.
- ✅ Fusion HDF5 reader compatible with schema
- ⚠️ Data is stale (2 hours old) due to upstream packet loss

---

## ✅ SESSION COMPLETE (2026-01-02 23:30 UTC): OOM KILLER ISSUE RESOLVED

**Status:** 🟢 **RESOLVED** - Core recorder OOM issue fixed, pipeline fully operational

**Author:** AI Agent (Cascade)
**Date:** 2026-01-02 23:30 UTC

### Summary

**Root Cause:** Core recorder was being killed by the Linux OOM (Out-Of-Memory) killer every few minutes due to excessive memory consumption from Digital RF HDF5 writers running on all 9 channels simultaneously.

**Impact:** 32+ hours of data loss (Jan 1 15:05 UTC - Jan 2 23:20 UTC). No files written during this period.

### Investigation Timeline

**22:30 UTC:** Investigation started
- Initial hypothesis: RTP packet loss (from previous session notes)
- Found: No files written since Jan 1, 15:05 UTC
- Radiod confirmed healthy and transmitting RTP packets

**22:45 UTC:** Root cause identified
- Checked systemd logs: `Memory cgroup out of memory: Killed process`
- OOM killer terminated recorder 52+ times
- Process consuming ~2GB RSS, hitting 4GB cgroup limit
- Service configured with `MemoryMax=4G` in systemd unit

**23:00 UTC:** Issue diagnosed
- Production config had `save_digital_rf = true` 
- Git repo config already had `save_digital_rf = false`
- Digital RF HDF5 writers for 9 channels × 24kHz = excessive memory
- Memory grew from 200MB → 1.8GB+ within minutes

**23:20 UTC:** Fix deployed
- Synced production config from git repo
- Restarted core-recorder service
- Memory stabilized at ~1.8GB (within limits without Digital RF)
- Files immediately began writing to `/dev/shm/timestd/raw_buffer/`

### Fixes Implemented

**1. Configuration Sync**
- **File:** `/etc/hf-timestd/timestd-config.toml`
- **Change:** `save_digital_rf = true` → `save_digital_rf = false`
- **Impact:** Eliminated Digital RF HDF5 memory overhead
- **Status:** ✅ Deployed, configs now in sync (MD5 verified)

### Current System State (23:30 UTC)

```
Core Recorder:  ✅ Running stable (14min uptime)
                   Memory: 1.8GB RSS (within 4GB limit)
                   Writing: 45 files/5min to /dev/shm/timestd
                   Completeness: 100% on all 9 channels
                   
Analytics:      ✅ Processing fresh data
                   HDF5 files: 9 channels actively writing
                   CSV output: 9 files in last 10 minutes
                   
Fusion:         ✅ Running successfully
                   Output: Grade C (CSV fallback, HDF5 accumulating)
                   No crashes, stable operation
                   
Science:        ⚠️  TEC stale (3h) - expected during recovery
                   Will auto-recover as fresh data accumulates
                   Propagation stats: operational
```

### Pipeline Verification Results

- **PASS:** 31 checks
- **WARN:** 1 (Chrony TMGR reach low - minor)
- **FAIL:** 1 (TEC staleness - expected during recovery)

**All critical systems operational.** TEC staleness will resolve within 1-2 hours as fresh timing measurements accumulate.

### Lessons Learned

1. **Memory Limits:** Digital RF HDF5 format unsuitable for 9 simultaneous channels with 4GB memory limit
2. **Config Drift:** Production config diverged from git repo (Digital RF setting)
3. **Tiered Storage:** Working correctly - files in `/dev/shm/timestd/raw_buffer/` not `/var/lib/timestd/raw_buffer/`
4. **OOM Symptoms:** Silent failures - process killed before writing any data, no obvious error messages

### Recommendations

1. ✅ Keep Digital RF disabled for multi-channel deployments
2. ✅ Use binary `.bin.zst` format (efficient, reliable)
3. ✅ Maintain config sync between repo and production
4. Consider increasing `MemoryMax` if Digital RF needed in future (8GB minimum for 9 channels)

---

## RTP Packet Loss Investigation Plan (OBSOLETE - SEE ABOVE)

### Phase 1: Characterize the Packet Loss (10 minutes)

**Goal:** Understand the nature and extent of the packet loss.

```bash
# 1. Check recorder logs for loss patterns
tail -500 /var/log/hf-timestd/core-recorder.log | grep -E "Lost packet|RTP offset drift" | head -50

# 2. Check if loss is consistent across all channels
tail -200 /var/log/hf-timestd/core-recorder.log | grep "Lost packet" | awk '{print $NF}' | sort | uniq -c

# 3. Check recorder resource usage
ps aux | grep core_recorder
top -b -n 1 | grep core_recorder

# 4. Check system network statistics
netstat -s | grep -E "packet|error|drop"
ifconfig | grep -E "RX|TX|error|drop"
```

**Decision Points:**
- If loss is uniform across channels → Network/system issue
- If loss is channel-specific → Channel configuration issue
- If CPU/memory high → Resource exhaustion
- If network errors high → Network infrastructure issue

### Phase 2: Check RTP Stream Source (10 minutes)

**Goal:** Determine if the problem is with radiod or the network path.

```bash
# 1. Check radiod status and health
systemctl status timestd-radiod-monitor --no-pager
curl -s http://localhost:8073/health | jq .

# 2. Check if radiod is actually transmitting
tcpdump -i lo -c 100 udp port 5004 2>&1 | head -20

# 3. Check radiod logs for errors
tail -200 /var/log/radiod/radiod.log | grep -E "ERROR|WARNING|RTP"

# 4. Verify radiod process health
ps aux | grep radiod
cat /proc/$(pgrep radiod)/status | grep -E "State|VmSize|VmRSS"
```

**Decision Points:**
- If radiod unhealthy → Restart/investigate radiod
- If no RTP packets on network → radiod not transmitting
- If packets present but recorder missing them → Recorder buffer issue

### Phase 3: Diagnose Recorder Buffer/Processing (15 minutes)

**Goal:** Identify if recorder is dropping packets due to processing delays.

```bash
# 1. Check recorder configuration
cat /etc/hf-timestd/timestd-config.toml | grep -A 10 "\[recorder\]"

# 2. Check system buffer sizes
sysctl net.core.rmem_max
sysctl net.core.rmem_default
cat /proc/sys/net/core/netdev_max_backlog

# 3. Monitor recorder in real-time
sudo journalctl -u timestd-core-recorder -f &
# Let it run for 60 seconds, observe packet loss rate

# 4. Check for I/O bottlenecks
iostat -x 5 3
df -h /var/lib/timestd/raw_buffer/
```

**Possible Causes:**
- Insufficient socket buffer size
- Disk I/O bottleneck (writing raw buffers)
- CPU saturation (processing 9 channels)
- Memory pressure

### Phase 4: Check for System-Wide Issues (10 minutes)

**Goal:** Rule out system-level problems.

```bash
# 1. Check system load and resources
uptime
free -h
vmstat 5 3

# 2. Check for OOM events
dmesg | grep -i "out of memory\|oom"
journalctl --since "2 hours ago" | grep -i "oom\|killed"

# 3. Check for disk space issues
df -h
du -sh /var/lib/timestd/*

# 4. Check for network interface errors
ip -s link show
ethtool -S lo 2>/dev/null | grep -E "error|drop"
```

### Phase 5: Temporary Mitigation (5 minutes)

**Goal:** Restore service while investigating root cause.

```bash
# Option 1: Restart recorder (may clear transient issue)
sudo systemctl restart timestd-core-recorder
sleep 60
tail -50 /var/log/hf-timestd/core-recorder.log | grep "Lost packet"

# Option 2: Restart radiod (if source issue suspected)
# WARNING: This will interrupt all channels
# sudo systemctl restart radiod

# Option 3: Increase buffer sizes (if buffer overflow suspected)
# Edit /etc/sysctl.conf:
# net.core.rmem_max = 134217728
# net.core.rmem_default = 67108864
# sudo sysctl -p
```

---

## Potential Root Causes (RTP Packet Loss)

### 1. Recorder Buffer Overflow (High Probability)
**Hypothesis:** Recorder cannot process packets fast enough, causing buffer overflow.

**Evidence:**
- Uniform 83% loss across all channels
- Started suddenly at 20:12 UTC (recorder restart)
- "RTP offset drift" warnings (timing desync)

**Possible Causes:**
- Socket receive buffer too small
- Processing thread blocked/slow
- Disk I/O bottleneck writing raw buffers
- CPU saturation

### 2. Radiod Transmission Issue (Medium Probability)
**Hypothesis:** Radiod is not transmitting complete RTP streams.

**Possible Causes:**
- SDR hardware malfunction
- USB bandwidth saturation
- Radiod internal buffer overflow
- Configuration change at 20:12 UTC

### 3. Network Path Issue (Low Probability)
**Hypothesis:** Localhost network stack dropping packets.

**Possible Causes:**
- Kernel network buffer exhaustion
- Firewall/iptables rules
- Network namespace issues
- System resource pressure

### 4. Recorder Code Bug (Low Probability)
**Hypothesis:** Recent code change introduced packet handling bug.

**Evidence:**
- Timing coincides with recorder restart
- May have picked up new code version

**Investigation:**
- Check git log for recent recorder changes
- Compare current version with last known good

---

## Context: Recent System Changes

### ✅ Completed This Session (2026-01-02)

**Science Aggregator Improvements (Phase 1 & 2):**
1. Fixed HDF5 timing measurements directory path bug
2. Implemented propagation statistics (hourly aggregation)
3. Implemented TEC validation against GPS IONEX data
4. All changes deployed to production

**Files Modified:**
- `src/hf_timestd/core/science_aggregator.py` (bug fixes + new features)
- `src/hf_timestd/core/propagation_stats.py` (new module)
- `src/hf_timestd/core/tec_validator.py` (new module)
- `src/hf_timestd/schemas/l3_tec_v1.json` (validation fields)

**Science Aggregator Status:**
- ✅ Service running and operational
- ✅ Propagation statistics working (12 records/hour)
- ✅ TEC validation ready (awaiting IONEX data)
- ⚠️ No TEC output (waiting for analytics input)

### Previous Session (Earlier 2026-01-02)

**Fusion Service Fixes:**
- VTEC safety checks
- Global Solver outlier rejection
- HDF5 parity improvements
- Warmup penalty removal

---

## Current System State

**Services:** All Running (No Crashes)
```
✅ timestd-core-recorder.service (uptime: 1h 9m)
✅ timestd-analytics.service (uptime: 4h 10m) ⚠️ NOT WRITING HDF5
✅ timestd-fusion.service (uptime: 3h 40m) ⚠️ STALE (no input)
✅ timestd-science-aggregator.service (uptime: 6m) ⚠️ STALE (no input)
✅ timestd-vtec.service (uptime: 22h 28m)
✅ timestd-radiod-monitor.service (uptime: 6h 21m)
✅ timestd-web-ui.service (uptime: 1d 5h)
```

**Data Pipeline Status:**
- L0 (Raw IQ): ✅ Active (45 files/5min)
- L2 (Timing): ❌ No HDF5 output
- L3 (Fusion): ❌ Stale (82 minutes)
- L3A (TEC): ❌ Stale (1 hour)
- L3C (Propagation Stats): ✅ Working (when data available)

**Hardware:**
- Radiod: ✅ HEALTHY (pid 1, uptime 3.2 days)
- GPSDO: ✅ Locked
- System: ✅ Calibrated

---

## Success Criteria for Next Session

1. **Root Cause Identified:** Determine why RTP packet loss is occurring (83% loss)
2. **Fix Implemented:** Restore full RTP packet reception (1.44M samples/min)
3. **Analytics Verified:** Confirm Analytics resumes writing HDF5 with complete minutes
4. **Pipeline Restored:** Verify full pipeline operation (Analytics → Fusion → Science)

**Critical Metrics:**
- RTP packet reception: 1,440,000 samples/minute (currently 240,000)
- Packet loss warnings: 0 (currently continuous)
- Analytics HDF5 files: Updated with timestamps > 20:12 UTC
- Fusion HDF5: Updated every ~60 seconds with fresh data
- TEC HDF5: Updated every ~5 minutes
- Pipeline verification: All checks passing

**Auto-Recovery Expected:**
Once RTP packet loss is resolved, the pipeline should self-recover:
- Analytics will resume processing complete minutes
- Fusion will automatically switch from CSV fallback to HDF5
- Science Aggregator will resume TEC estimation
- No code changes or service restarts required (Fusion bug already fixed)
