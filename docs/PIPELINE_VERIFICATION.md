# Pipeline Verification - Living Documentation

This document explains the **why** and **how** of pipeline verification, shows expected operation through logging examples, and provides data that confirms correct operation (or reveals issues to fix).

**Last Updated:** 2026-02-01  
**Script:** `scripts/verify_pipeline.sh`

---

## Purpose

The verification script provides a comprehensive health check of the entire HF-TimeStd pipeline, from raw IQ capture through to Chrony integration. It answers the question: **"Is the system working correctly right now?"**

## Pipeline Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  Phase 0: Services          │  Phase 0.5: Hardware                          │
│  timestd-core-recorder      │  radiod (SDR software)                        │
│  timestd-metrology          │  GPSDO (timing reference)                     │
│  timestd-fusion             │                                               │
│  timestd-physics            │                                               │
├─────────────────────────────┼───────────────────────────────────────────────┤
│  Phase 1: L0 Raw IQ         │  Binary archives with JSON metadata           │
│  /dev/shm/timestd/raw_buffer│  start_system_time MUST equal minute_boundary │
├─────────────────────────────┼───────────────────────────────────────────────┤
│  Phase 2: L1 Metrology      │  Tone detections, timing measurements         │
│  /var/lib/timestd/phase2/   │  HDF5 files per channel                       │
├─────────────────────────────┼───────────────────────────────────────────────┤
│  Phase 3: L3 Fusion         │  Fused D_clock estimates                      │
│  /var/lib/timestd/phase2/   │  Multi-station weighted average               │
│  fusion/                    │                                               │
├─────────────────────────────┼───────────────────────────────────────────────┤
│  Phase 4: Science           │  TEC estimates, ionospheric products          │
│  /var/lib/timestd/phase2/   │                                               │
│  science/                   │                                               │
├─────────────────────────────┼───────────────────────────────────────────────┤
│  Phase 5: Calibration       │  Broadcast calibration state                  │
│  /var/lib/timestd/state/    │  Bootstrap timing reference                   │
├─────────────────────────────┼───────────────────────────────────────────────┤
│  Chrony Integration         │  TSL1/TSL2 SHM sources                        │
│  /etc/chrony/conf.d/        │  Sub-millisecond offsets to system clock      │
└─────────────────────────────┴───────────────────────────────────────────────┘
```

---

## Verification Checks

### Phase 0: Service Status

**What we check:**
- Core services are running with reasonable uptime (>5 minutes)
- Optional services are noted but don't cause failures

**Why it matters:**
- Recent restarts may indicate crashes or configuration issues
- Services must be running for data to flow

**Expected output:**
```
✅ PASS timestd-core-recorder.service is running (uptime: 36m)
✅ PASS timestd-metrology.service is running (uptime: 36m)
✅ PASS timestd-fusion.service is running (uptime: 36m)
```

**Diagnostic commands:**
```bash
systemctl status timestd-core-recorder
journalctl -u timestd-fusion -n 100
```

---

### Phase 0.5: Radio Hardware (Radiod)

**What we check:**
- Radiod status file exists and shows healthy state
- SDR hardware is receiving data

**Why it matters:**
- No radiod = no IQ data = entire pipeline stalls

**Expected output:**
```
✅ PASS Radiod is HEALTHY (pid 0, uptime nulls)
```

**Diagnostic commands:**
```bash
cat /var/lib/timestd/state/radiod-status.json | jq '.'
```

---

### Phase 1: Binary Archive (L0 Raw IQ)

**What we check:**
- Archive directories exist (cold storage + hot buffer)
- Recent `.bin.zst` files (last 5 minutes)
- Matching `.json` metadata sidecars

**Why it matters:**
- Raw IQ is the foundation of all timing measurements
- JSON metadata contains critical `start_system_time` for timing alignment

**Critical verification - Buffer alignment (v5.3.12 fix):**
```bash
# start_system_time MUST equal minute_boundary exactly
cat /dev/shm/timestd/raw_buffer/CHU_3330/$(date -u +%Y%m%d)/*.json | \
  jq -s '.[-1] | {minute_boundary, start_system_time}'
```

**Expected (correct):**
```json
{
  "minute_boundary": 1769902200,
  "start_system_time": 1769902200
}
```

**Broken (pre-v5.3.12):**
```json
{
  "minute_boundary": 1769902200,
  "start_system_time": 1769902200.014  ← 14ms offset causes timing failures!
}
```

---

### Phase 2: Metrology (L1 Measurements)

**What we check:**
- Channel directories exist
- HDF5 metrology files are fresh (<30 minutes)
- Tone detections are at expected positions

**Why it matters:**
- Metrology produces the raw timing measurements
- Stale files indicate processing failures

**Critical verification - Tone position:**
```bash
grep "expected_marker_at_sample" /var/log/hf-timestd/phase2-*.log.1 | tail -3
```

**Expected (correct):**
```
[TIMING_DIAG] CHU: expected_marker_at_sample=-0, timing_error=+105.3ms
```
- `expected_marker_at_sample` should be 0 or small positive (not negative!)
- `timing_error` is the propagation delay (tens to hundreds of ms is normal)

**Broken:**
```
[TIMING_DIAG] CHU: expected_marker_at_sample=-336, timing_error=+3756ms
```
- Negative sample position = tone expected before buffer starts = impossible to detect

---

### Phase 3: Fusion (L3 Fused Timing)

**What we check:**
- Fusion HDF5 is being actively written (<2 minutes)
- D_clock values are reasonable (tens of ms, not hundreds)
- Chrony SHM writes have sub-millisecond offsets

**Why it matters:**
- Fusion combines multi-station measurements into a single timing estimate
- This feeds Chrony for system clock discipline

**Critical verification - D_clock stability:**
```bash
grep "Fused D_clock" /var/log/hf-timestd/fusion.log.1 | tail -5
```

**Expected (stable):**
```
Fused D_clock: -1.772 ms (raw: +22.848 ms) ± 8.307 ms [1 broadcasts, grade D]
```
- D_clock in single-digit milliseconds
- Uncertainty (±) decreasing over time

**Broken (unstable):**
```
Fused D_clock: -3756.123 ms (raw: +91.885 ms) ± 500.0 ms [4 broadcasts, grade D]
```
- D_clock in hundreds/thousands of ms indicates buffer alignment issue

**Chrony SHM verification:**
```bash
grep "ChronySHM write" /var/log/hf-timestd/fusion.log.1 | tail -5
```

**Expected:**
```
ChronySHM write: offset=-0.376ms
ChronySHM write: offset=+2.257ms
```
- Sub-millisecond offsets indicate correct timing

---

### Phase 4: Science Products

**What we check:**
- TEC HDF5 files are fresh (<15 minutes)
- GNSS VTEC files exist (optional)

**Why it matters:**
- Science products validate the physics model
- TEC estimates require multi-frequency detections

---

### Phase 5: Calibration State

**What we check:**
- Bootstrap timing reference exists and is locked
- Calibration state file exists

**Critical verification - Bootstrap state:**
```bash
cat /var/lib/timestd/state/bootstrap_timing_reference.json | jq '.'
```

**Expected:**
```json
{
  "locked": true,
  "lock_tier": "PROVISIONAL",
  "time_confirmed": true,
  "reference_utc": 1769988540,
  "uncertainty_ms": 5
}
```

- `locked: true` = bootstrap has established timing
- `time_confirmed: true` = BCD/FSK decode verified the time
- `uncertainty_ms` < 10 = good precision

---

### Chrony Integration

**What we check:**
- TSL1/TSL2 sources configured
- Sources are reachable (reach > 0)
- Chrony is using or combining HF-timestd sources

**Why it matters:**
- This is the ultimate output: disciplining the system clock

**Verification:**
```bash
chronyc sources | grep TSL
```

**Expected:**
```
#? TSL1    0   4    10    59    +15ms[  +15ms] +/- 2000us
#? TSL2    0   4    10    59    +12ms[  +12ms] +/- 1445us
```

- `reach` column (5th) should be non-zero (octal, 377 = all 8 polls successful)
- Offset should be in milliseconds range (HF propagation delay)
- `#*` prefix = selected as primary source
- `#+` prefix = combined with other sources

---

## Current Gaps in verify_pipeline.sh

The following checks are **missing** from the current script and should be added:

### 1. Bootstrap Timing Reference Check
**Gap:** Script doesn't verify `bootstrap_timing_reference.json` state
**Impact:** Won't detect if bootstrap is stuck or has stale reference
**Fix:** Add check for locked state and reference freshness

### 2. Buffer Alignment Verification
**Gap:** Script doesn't verify `start_system_time == minute_boundary`
**Impact:** Won't detect the critical timing alignment issue fixed in v5.3.12
**Fix:** Add JSON metadata check for exact boundary alignment

### 3. D_clock Sanity Check
**Gap:** Script doesn't parse fusion log for D_clock magnitude
**Impact:** Won't detect timing instability (D_clock in hundreds of ms)
**Fix:** Add check that D_clock is within reasonable bounds (e.g., ±100ms)

### 4. Chrony SHM Offset Check
**Gap:** Script doesn't verify SHM write offsets are sub-millisecond
**Impact:** Won't detect if fusion is writing bad timing to Chrony
**Fix:** Add check for recent SHM writes with reasonable offsets

### 5. IRI-2020 Health Check
**Gap:** Script doesn't detect IRI-2020 CMake failures
**Impact:** Physics model degraded without ionospheric corrections
**Fix:** Add check for IRI-2020 errors in logs

### 6. Log Rotation Awareness
**Gap:** Script checks `.log` but logs rotate at midnight to `.log.1`
**Impact:** Checks may fail immediately after midnight
**Fix:** Check both `.log` and `.log.1` for recent entries

---

## Quick Health Check Commands

```bash
# Full verification
scripts/verify_pipeline.sh

# Quick status
scripts/verify_pipeline.sh --quick

# Buffer alignment (critical)
cat /dev/shm/timestd/raw_buffer/CHU_3330/$(date -u +%Y%m%d)/*.json | \
  jq -s '.[-1] | {minute_boundary, start_system_time}'

# Tone detection positions
grep "expected_marker_at_sample" /var/log/hf-timestd/phase2-*.log* | tail -5

# D_clock stability
grep "Fused D_clock" /var/log/hf-timestd/fusion.log* | tail -10

# Chrony integration
chronyc sources | grep TSL

# Bootstrap state
cat /var/lib/timestd/state/bootstrap_timing_reference.json | jq '.'
```

---

## Troubleshooting Decision Tree

```
Pipeline not working?
│
├─► Services not running?
│   └─► sudo scripts/start-services.sh
│
├─► No raw IQ files?
│   └─► Check radiod: cat /var/lib/timestd/state/radiod-status.json
│
├─► start_system_time != minute_boundary?
│   └─► Update to v5.3.12+, restart core-recorder
│
├─► expected_marker_at_sample negative?
│   └─► Buffer alignment issue, see above
│
├─► D_clock in hundreds of ms?
│   └─► Buffer alignment issue, see above
│
├─► Chrony TSL reach = 0?
│   └─► Check fusion service, SHM permissions
│
└─► IRI-2020 CMake errors?
    └─► sudo chown -R timestd:timestd /opt/hf-timestd/venv/lib/python3.11/site-packages/iri2020/build
```
