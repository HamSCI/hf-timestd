# Web UI Information Architecture - System Monitoring

**Purpose:** Define system-level operational metrics and health indicators for the GRAPE V2 dual-service architecture.

**Last Updated:** 2024-11-10  
**Status:** Specification Draft

---

## Overview

The V2 architecture operates two independent services that must be monitored:
- **Core Recorder:** RTP → NPZ archives (critical, minimal changes)
- **Analytics Service:** NPZ → derived products (can restart independently)

System monitoring must provide operators with clear visibility into:
1. Service health and uptime
2. Data pipeline status
3. Resource utilization
4. Error conditions requiring intervention

---

## 1. Service Health Status

### 1.1 Core Recorder Health

**Primary Indicators:**
- **Running State:** Boolean (running/stopped)
- **Staleness:** Time since last status update (alert if >30s)
- **Uptime:** Hours/minutes since service start
- **PID:** Process identifier for troubleshooting

**Data Source:** `/tmp/grape-test/status/core-recorder-status.json`

**Update Frequency:** 10 seconds (file write by core recorder)

**Display Requirements:**
```
Core Recorder: ✅ Running (2h 34m)
Status: Fresh (updated 3s ago)
PID: 1926862
```

**Alert Conditions:**
- 🔴 **Critical:** Status file >60s stale (service likely crashed)
- 🟡 **Warning:** Status file 30-60s stale (possible hang)
- 🟢 **Normal:** Status file <30s fresh

### 1.2 Analytics Service Health

**Primary Indicators:**
- **Running State:** Aggregated from per-channel status files
- **Channels Processing:** Count of active analytics workers
- **Staleness:** Newest status file age
- **Uptime:** Maximum uptime across all workers

**Data Source:** `/tmp/grape-test/analytics/{channel}/status/analytics-service-status.json` (per channel)

**Update Frequency:** 10 seconds per channel

**Display Requirements:**
```
Analytics Service: ✅ Running
Processing: 9/9 channels
Status: Fresh (updated 5s ago)
Uptime: 1h 47m
```

**Alert Conditions:**
- 🔴 **Critical:** All channels stale (entire service down)
- 🟡 **Warning:** Some channels stale (partial failure)
- 🟢 **Normal:** All channels fresh

### 1.3 Radiod Connection

**Primary Indicators:**
- **Connected:** Inferred from core recorder receiving packets
- **Status Address:** KA9Q radiod multicast address
- **Last Packet:** Time since last RTP packet received

**Data Source:** Core recorder status (packets_received incrementing)

**Display Requirements:**
```
Radiod Connection: ✅ Connected
Address: wwv-status@239.255.93.1
Last Packet: 2s ago
```

**Alert Conditions:**
- 🔴 **Critical:** No packets received in >60s (radiod down or network issue)
- 🟡 **Warning:** Packet rate below expected (15-20 packets/sec per channel)
- 🟢 **Normal:** Packets flowing normally

---

## 2. Data Pipeline Status

### 2.1 Archive Writing (Core Recorder)

**Metrics:**
- **NPZ Files Written:** Total count across all channels
- **Archive Rate:** Files/minute (should be ~1 per channel per minute)
- **Last Archive:** Most recent NPZ file timestamp
- **Archive Directory:** Path to archive storage

**Display Requirements:**
```
NPZ Archives Written: 2,856 files
Rate: 9 files/min (9 channels × 1/min)
Last Archive: 23s ago
Path: /tmp/grape-test/archives/
```

**Alert Conditions:**
- 🔴 **Critical:** No files written in >5 minutes (archive pipeline broken)
- 🟡 **Warning:** Write rate below expected (missed minutes)
- 🟢 **Normal:** Consistent 1 file/channel/minute

### 2.2 Analytics Processing

**Metrics:**
- **NPZ Files Processed:** Total count by analytics service
- **Processing Rate:** Files/minute (should match write rate with lag)
- **Pending Files:** Count of unprocessed NPZ files
- **Processing Lag:** Time difference between write and process

**Display Requirements:**
```
NPZ Processed: 1,423 files
Processing Rate: 8.7 files/min
Pending: 3 files (< 1 minute lag)
```

**Alert Conditions:**
- 🔴 **Critical:** Pending >60 files (processing falling behind)
- 🟡 **Warning:** Pending >10 files (slow processing)
- 🟢 **Normal:** Pending <5 files

### 2.3 Digital RF Output

**Metrics:**
- **Samples Written:** Total 10 Hz IQ samples written
- **Files Written:** Count of Digital RF HDF5 files
- **Last Write:** Timestamp of most recent write
- **Output Directory:** Path to Digital RF storage

**Display Requirements:**
```
Digital RF Samples: 1.2M samples (120s at 10 Hz)
Files Written: 47 files
Last Write: 18s ago
Path: /tmp/grape-test/analytics/WWV_5_MHz/digital_rf/
```

**Alert Conditions:**
- 🔴 **Critical:** No writes in >5 minutes (decimation/write broken)
- 🟡 **Warning:** Write rate inconsistent
- 🟢 **Normal:** Regular writes every ~60s

### 2.4 Upload Queue Status

**Metrics (Future):**
- **Queued Files:** Count of files pending upload
- **Active Uploads:** Currently transferring
- **Upload Rate:** MB/s or files/hour
- **Failed Uploads:** Retry queue size

**Display Requirements:**
```
Upload Queue: 12 files pending
Active: 2 uploads in progress
Rate: 1.2 MB/s
Failed: 0 (no retries needed)
```

---

## 3. Resource Utilization

### 3.1 Disk Space

**Metrics:**
- **Total Space:** Filesystem capacity
- **Used Space:** Current usage in GB
- **Free Space:** Available GB
- **Percent Used:** Usage percentage

**Data Source:** `df -k` command on data root directory

**Display Requirements:**
```
Disk Space: /tmp/grape-test
Total: 250 GB
Used: 42.3 GB (16.9%)
Free: 207.7 GB
```

**Alert Conditions:**
- 🔴 **Critical:** >90% used (imminent failure)
- 🟡 **Warning:** >80% used (plan cleanup)
- 🟢 **Normal:** <80% used

**Recommendations:**
- Archive cleanup: Rotate old NPZ files after successful upload
- Digital RF retention: Keep 7 days locally, rest on PSWS

### 3.2 Memory Usage (Future)

**Metrics:**
- **Core Recorder RSS:** Resident memory
- **Analytics Service RSS:** Aggregate across workers
- **System Available:** Free memory

**Collection Method:** Parse `/proc/{pid}/status` or use `ps`

**Alert Conditions:**
- 🔴 **Critical:** OOM condition imminent
- 🟡 **Warning:** Memory usage growth trend
- 🟢 **Normal:** Stable memory footprint

### 3.3 CPU Usage (Future)

**Metrics:**
- **Core Recorder CPU:** Percentage (should be low, <10%)
- **Analytics Service CPU:** Percentage (higher during decimation)
- **System Load:** Overall load average

**Alert Conditions:**
- 🟡 **Warning:** Core recorder >20% CPU (investigate inefficiency)
- 🟢 **Normal:** Analytics service CPU varies with workload

---

## 4. Error Monitoring

### 4.1 Recent Errors Display

**Data Source:** Log file tail + grep for ERROR/WARNING

**Display Requirements:**
- **Last 10-20 errors/warnings**
- **Timestamp:** When error occurred
- **Severity:** ERROR vs WARNING
- **Message:** Full error text
- **Filter:** By severity level

**Example:**
```
Recent Errors (5):
2024-11-10 20:15:23 | ERROR | Channel WWV 5 MHz: Gap detected (320 samples, 20ms)
2024-11-10 20:12:45 | WARNING | Analytics service slow (pending queue: 15 files)
2024-11-10 20:08:12 | ERROR | RTP sequence jump detected (expected 12345, got 12350)
```

### 4.2 Error Summary

**Metrics:**
- **Total Errors (24h):** Count by severity
- **Error Rate:** Errors per hour
- **Most Common:** Top 3 error types

**Alert Conditions:**
- 🔴 **Critical:** >10 errors/hour (persistent problem)
- 🟡 **Warning:** >3 errors/hour (investigate)
- 🟢 **Normal:** <1 error/hour

### 4.3 System Health Score

**Composite Indicator:**
```
Overall System Health: 🟢 HEALTHY

Checks:
✅ Core Recorder: Running
✅ Analytics Service: Running  
✅ Radiod Connection: Active
✅ Disk Space: 16.9% (healthy)
✅ Error Rate: 0.2/hour (normal)
```

**Status Levels:**
- 🟢 **HEALTHY:** All checks passing
- 🟡 **DEGRADED:** 1-2 warnings
- 🔴 **CRITICAL:** Any critical alert active

---

## 5. Real-Time Updates

### 5.1 Refresh Strategy

**Current Implementation:**
- **Full Refresh:** Every 60 seconds
- **Method:** Poll all JSON status files + aggregate

**Future Enhancement:**
- **WebSocket:** Push updates on state change
- **Incremental:** Only changed data
- **Configurable:** User-selectable refresh rate (10s/30s/60s)

### 5.2 Countdown Timer

**Display:**
```
Next update in: 47s
```

**Purpose:** User awareness of data freshness

### 5.3 Data Freshness Indicator

**Live Indicator:**
- Pulsing green dot when data <10s old
- Yellow dot when data 10-30s old
- Red dot when data >30s old (stale)

---

## 6. Implementation Priorities

### Phase 1 (Current - Complete)
- ✅ Core recorder health
- ✅ Analytics service health
- ✅ Basic channel counts
- ✅ Recent errors display

### Phase 2 (Next)
- ⏳ Pipeline status metrics (archive/process rates)
- ⏳ Disk space monitoring
- ⏳ Enhanced error summary

### Phase 3 (Future)
- ⏸️ Upload queue status
- ⏸️ Memory/CPU monitoring
- ⏸️ WebSocket real-time updates
- ⏸️ Historical trends (24h graphs)

---

## API Endpoints Required

### `/api/v1/system/status`
**Returns:** Comprehensive system status
```json
{
  "timestamp": "2024-11-10T20:30:00Z",
  "services": {
    "core_recorder": { "running": true, "uptime_seconds": 9360 },
    "analytics_service": { "running": true, "channels_processing": 9 }
  },
  "radiod": { "connected": true, "last_packet": "2s ago" },
  "disk": { "total_gb": 250, "used_gb": 42.3, "percent_used": 16.9 }
}
```

### `/api/v1/system/health`
**Returns:** Simple health check
```json
{
  "status": "healthy",
  "checks": {
    "recorder": "ok",
    "disk_space": "ok",
    "radiod": "ok"
  }
}
```

### `/api/v1/system/errors`
**Returns:** Recent errors with filtering
```json
{
  "errors": [
    { "timestamp": "2024-11-10T20:15:23Z", "level": "error", "message": "..." }
  ],
  "total_count": 5
}
```

---

**Next Documents:**
- `WEB_UI_CHANNEL_METRICS.md` - Per-channel data characterization
- `WEB_UI_SCIENTIFIC_QUALITY.md` - Data quality & provenance reporting
- `WEB_UI_NAVIGATION_UX.md` - User experience & information hierarchy
