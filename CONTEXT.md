# HF-TimeStd AI Agent Context

**Last Updated**: 2026-01-04  
**System Version**: 3.9.0  
**Focus**: Chrony Pipeline Hardening & Metrology Reliability

---

## Executive Summary

The `hf-timestd` system is a high-precision time transfer system that receives WWV/WWVH/CHU time signals via HF radio, processes them through a multi-stage pipeline, and provides UTC time corrections to the system clock via Chrony. The system is currently operational and providing time synchronization, but the next priority is to **harden the data and calculation pathway to Chrony** to ensure a resilient, robust, bullet-proof metrology service that runs reliably regardless of subsequent analytics or science products.

---

## Recent Accomplishments (2026-01-04 Session)

### TEC Fix Implementation - COMPLETED ✅

Successfully implemented and deployed the fix for Total Electron Content (TEC) calculations:

**Problem Solved:**

- TEC estimators were receiving calibrated `clock_offset_ms` values that had ionospheric delays removed
- This eliminated the frequency-dependent dispersion signal needed for TEC estimation
- Result: Near-zero or NaN TEC values

**Solution Implemented:**

- Added `raw_arrival_time_ms` field to L2 timing measurements schema (v1.0.0 → v1.1.0)
- Modified Analytics Service to calculate and write uncalibrated ToA: `raw_arrival_time_ms = effective_d_clock + propagation_delay_ms`
- Updated Science Aggregator and Fusion Service to use `raw_arrival_time_ms` for TEC calculations
- Implemented backward compatibility fallback for older data

**Deployment Status:**

- ✅ Code deployed to production venv (`/opt/hf-timestd/venv/`)
- ✅ Services restarted and operational
- ✅ Field being written to HDF5 files in `/var/lib/timestd/phase2/{CHANNEL}/clock_offset/`
- ✅ Verified 36+ measurements with `raw_arrival_time_ms` values (e.g., 22.761 ms, 111.359 ms)
- ⏳ Monitoring TEC output for non-zero values (2-50 TECU range expected)

**Key Lesson Learned:**
HDF5 files created with old schema cannot have new datasets added retroactively. Schema updates require file deletion/recreation or daily rotation to new files.

**Cleanup Needed:**

- Remove debug logging added during troubleshooting (search for "DEBUG TEC FIX" in codebase)

---

## Next Session Priority: Chrony Pipeline Hardening

### Objective

Create a **resilient, robust, bullet-proof metrology service** that provides reliable UTC time corrections to Chrony regardless of:

- Analytics failures or bugs
- Science product processing issues
- TEC calculation problems
- Ionospheric model failures
- Network or data source outages

### Core Principle

**The Chrony pipeline must be decoupled from analytics and science products.** Time synchronization is the PRIMARY mission; everything else is secondary.

### Current Chrony Data Flow

```
Phase 1 (L0): Raw IQ Samples from radiod
         ↓
Phase 2 (L2): Tone Detection → Timing Measurements
         ↓                      (clock_offset_ms, propagation_delay_ms)
Phase 3 (L3): Multi-Broadcast Fusion
         ↓                      (combines multi-station, multi-frequency)
    Fused UTC Time Estimate
         ↓
    Chrony SHM (Shared Memory)
         ↓
    System Clock Discipline
```

**Critical Path Components:**

1. **Core Recorder** (Phase 1): Receives IQ samples from radiod
2. **Analytics Service** (Phase 2): Detects tones, calculates timing
3. **Fusion Service** (Phase 3): Fuses measurements, writes to Chrony SHM
4. **Chrony SHM Writer**: Updates shared memory every 8 seconds

### Known Vulnerabilities & Hardening Opportunities

#### 1. **Analytics Service Failures**

**Current Risk:** If analytics crashes or stops detecting tones, no timing data flows to fusion.

**Hardening Ideas:**

- Implement watchdog monitoring for tone detection
- Add fallback to last-known-good calibration if no recent detections
- Create health metrics for analytics pipeline (tone detection rate, SNR, etc.)
- Separate "metrology-critical" processing from "science analytics"

#### 2. **Fusion Service Robustness**

**Current Risk:** Fusion service depends on multiple data sources (L2 measurements, GNSS VTEC, IRI-2020).

**Hardening Ideas:**

- Implement graceful degradation when GNSS VTEC unavailable
- Add timeout/fallback for IRI-2020 ionospheric model calls
- Ensure fusion continues with reduced accuracy rather than failing completely
- Monitor "reach" metric (currently showing low values - investigate why)

#### 3. **Chrony SHM Write Reliability**

**Current Risk:** SHM writes can fail silently if Chrony not running or permissions issues.

**Hardening Ideas:**

- Add verification that Chrony is consuming SHM updates
- Monitor "reach" value in Chrony sources
- Implement alerting when SHM writes fail
- Add automatic recovery from SHM permission issues

#### 4. **Data Pipeline Continuity**

**Current Risk:** HDF5 write failures, disk full, or file locking issues can disrupt pipeline.

**Hardening Ideas:**

- Implement in-memory buffering for critical timing data
- Add disk space monitoring and cleanup
- Improve HDF5 SWMR mode error handling
- Separate critical metrology data from bulk analytics data

#### 5. **Service Dependencies**

**Current Risk:** Services depend on each other in ways that can cause cascading failures.

**Hardening Ideas:**

- Document and minimize inter-service dependencies
- Implement circuit breakers for non-critical dependencies
- Add service health checks and auto-restart logic
- Create "degraded mode" operation for each service

### Recommended Investigation Areas

1. **Chrony Reach Analysis**
   - Current status shows "TMGR reach low (3)"
   - Investigate why reach is low despite fusion service running
   - Check SHM write frequency and Chrony poll interval alignment

2. **Calibration Staleness**
   - System shows "Calibration very stale (1d 8h)"
   - Understand why no tone detections in 24+ hours
   - Determine if this is propagation-related or a system issue

3. **Metrology vs. Analytics Separation**
   - Identify which code paths are critical for Chrony updates
   - Separate "must work" metrology from "nice to have" analytics
   - Consider creating a minimal "metrology-only" mode

4. **Failure Mode Testing**
   - Test what happens when analytics service crashes
   - Test what happens when GNSS VTEC unavailable
   - Test what happens when IRI-2020 model fails
   - Verify Chrony continues receiving updates in degraded scenarios

### Success Criteria for Next Session

1. ✅ Chrony receives consistent time updates even when analytics has issues
2. ✅ System operates in "degraded but functional" mode when non-critical components fail
3. ✅ Clear separation between metrology-critical and analytics-optional code paths
4. ✅ Monitoring and alerting for Chrony pipeline health
5. ✅ Documentation of failure modes and recovery procedures

---

## System Architecture Overview

### Data Processing Levels

- **L0 (Raw)**: Digital RF IQ samples from radiod (24 kHz, 16-bit)
- **L1 (Processed)**: Tone detections, BCD decoding, signal quality metrics
- **L2 (Calibrated)**: Station-assigned timing measurements with uncertainty budgets
- **L3 (Fused)**: Multi-station, multi-frequency fusion for optimal UTC estimate
- **L3C (Science)**: TEC, propagation statistics, ionospheric products

### Key Services

1. **timestd-core-recorder**: Receives IQ from radiod, writes Digital RF
2. **timestd-analytics**: Processes IQ → timing measurements (9 channels)
3. **timestd-fusion**: Fuses measurements → Chrony SHM updates
4. **timestd-science-aggregator**: Generates science products (TEC, propagation stats)
5. **timestd-vtec**: Downloads and processes GNSS VTEC data
6. **timestd-web-ui**: Monitoring dashboard

### Critical File Locations

- **Production Code**: `/opt/hf-timestd/venv/lib/python3.11/site-packages/hf_timestd/`
- **Data Root**: `/var/lib/timestd/`
- **Logs**: `/var/log/hf-timestd/`
- **Config**: `/etc/hf-timestd/timestd-config.toml`
- **Git Repository**: `/home/mjh/git/hf-timestd/` (source code, not used by production)

### HDF5 Data Locations

- **L2 Timing**: `/var/lib/timestd/phase2/{CHANNEL}/clock_offset/{CHANNEL}_timing_measurements_YYYYMMDD.h5`
- **L3 Fusion**: `/var/lib/timestd/phase2/fusion/FUSED_timing_YYYYMMDD.h5`
- **Science Products**: `/var/lib/timestd/phase2/science/{PRODUCT}/`

---

## Important Notes for AI Agents

### Production Code Management

**CRITICAL**: Production services run from `/opt/hf-timestd/venv/`, NOT from the git repository.

To update production code:

```bash
cd /home/mjh/git/hf-timestd
sudo /opt/hf-timestd/venv/bin/pip install . --no-deps
sudo systemctl restart timestd-{service-name}
```

### HDF5 Schema Evolution

When updating HDF5 schemas:

1. Increment schema version in JSON file
2. Existing HDF5 files will NOT get new datasets automatically
3. Either delete old files or wait for daily rotation to new files
4. Test with a single channel before deploying to all channels

### Service Restart Best Practices

1. Stop service: `sudo systemctl stop timestd-{service}`
2. Clear Python cache if code changed: `sudo find /opt/hf-timestd/venv -name "*.pyc" -delete`
3. Reinstall package: `sudo /opt/hf-timestd/venv/bin/pip install /home/mjh/git/hf-timestd --no-deps`
4. Start service: `sudo systemctl start timestd-{service}`
5. Verify: `systemctl is-active timestd-{service}`

### Debugging Workflow

1. Check service status: `systemctl status timestd-{service}`
2. View recent logs: `sudo journalctl -u timestd-{service} -n 100 --no-pager`
3. Check channel-specific logs: `/var/log/hf-timestd/phase2-{channel}.log`
4. Verify HDF5 files: Check `/var/lib/timestd/phase2/{CHANNEL}/clock_offset/` for active files
5. Monitor Chrony: `chronyc sources -v` to check TMGR source

---

## TEC Calculation Details (For Reference)

**Physics**: Ionospheric delay τ(f) ∝ TEC / f²

**Model**: T_obs(f) = T_vacuum + (40.3 · TEC) / f²

**Input Required**: Raw, uncalibrated time-of-arrival (ToA) that preserves frequency-dependent dispersion

**Current Implementation**:

- `raw_arrival_time_ms` = `effective_d_clock` + `propagation_delay_ms`
- This is the total observed arrival time before calibration removes ionospheric component
- TEC estimator performs linear regression: y = T_obs, x = 1/f², slope = 40.3 · TEC

**Note**: TEC calculations are **NOT** in the critical path to Chrony. They are science products only.

---

## Questions for Next Session

1. What is causing the low Chrony reach value (3)?
2. Why is calibration stale (no tone detections for 1d 8h)?
3. Can we create a "metrology-only" mode that bypasses all analytics?
4. What are the minimum requirements for Chrony to receive valid time updates?
5. How can we monitor and alert on Chrony pipeline health?

---

## End of Context Document

This document should be updated at the end of each session to reflect current system state and priorities.
