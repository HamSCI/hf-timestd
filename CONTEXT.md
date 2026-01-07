# HF-TimeStd AI Agent Context

**Last Updated**: 2026-01-07 11:15 UTC  
**System Version**: 4.5.3  
**Current Focus**: Analytics/Fusion Redesign - GPSDO-Centric Architecture  
**Next Session**: Redesign analytics and fusion to anchor on GPSDO temporal stability  
**System Status**: ⚠️ Functional but architecturally flawed (calibration feedback loop)

---

## Executive Summary

The `hf-timestd` system receives WWV/WWVH/CHU/BPM time signals via HF radio and provides UTC time corrections to Chrony. The system is currently operational but has fundamental architectural problems that prevent it from achieving its design goals.

**Critical Discovery (2026-01-07):**

The current system has a **calibration/Kalman feedback loop** that prevents convergence to UTC(NIST). Auto-calibration tries to force D_clock → 0ms, but the Kalman filter has converged to the wrong state (-6ms to -18ms), creating a feedback loop. The system should leverage **GPSDO temporal stability** as the primary constraint, not dynamic calibration.

---

## Key Insight: The GPSDO is the Steel Ruler

**Fundamental Principle**: With GPSDO nanosecond-level stability, when broadcasts return after a gap, they should appear at the **same RTP timestamp offset** because the GPSDO hasn't drifted.

**What This Means**:

- **GPSDO**: Provides stable RTP timestamps (the steel ruler)
- **Broadcasts**: Provide UTC(NIST)-aligned time signals
- **Measurement**: `D_clock = T_arrival(RTP) - T_emission(UTC)` = propagation delay + any clock offset
- **Expected Result**: D_clock should naturally center around 0ms ± propagation variations

**Current Problem**: System treats each measurement as independent, ignoring GPSDO temporal stability.

---

## What We Learned (2026-01-07 Session)

### 1. Auto-Calibration is Harmful

**Original Intent**: Learn systematic detection bias (matched filter delay ~1-2ms) for "warm start" after reboots.

**What It Actually Does**:

- Formula: `offset = -mean(D_clock)` tries to force measurements to 0ms
- Creates feedback loop with Kalman filter
- Learns Kalman error state instead of detection bias
- Results in corrupted offsets (e.g., CHU_3.3: +38ms!)

**Why It's Wrong**:

- Propagation delays are **real physics** that vary minute-to-minute
- System should report these variations, not calibrate them away
- GPSDO + UTC broadcasts should naturally center around 0ms

**Observation**: Raw measurements ARE at +0.7ms (good!), but Kalman pulls to -6ms, then calibration learns this as "normal."

### 2. Hierarchical Stability Weighting Concept is Sound

**The Idea**: Weight measurements by stability hierarchy:

1. Same broadcast, consecutive minute (GPSDO temporal stability) → highest weight
2. Same station, different frequency (ionospheric variation) → medium weight
3. Different stations (path variation) → lowest weight

**Why Initial Implementation Failed**: Used absolute uncertainty values (0.1ms vs 2.0ms), creating 400:1 weight ratios that broke Kalman filter numerics.

**Correct Approach**: Use relative multipliers (0.5x-1.0x) to preserve numerical stability.

### 3. System is Fragile

**Broadcast Availability**: Fluctuates 2-5 broadcasts (should be 5-7+)

- Best: 5 broadcasts (grade B)
- Typical: 3 broadcasts (grade C)
- Current: 2 broadcasts (grade D)

**Data Gaps**: 1-hour gap (09:32-10:38 UTC) with no fusion output

**Root Causes** (to investigate):

- D_clock validation too strict? (±5ms threshold)
- Cross-station validation too strict? (0.75ms threshold)
- Analytics not detecting signals?
- Propagation conditions?

---

## Design Principles for Redesign

### 1. GPSDO-Centric Architecture

**Primary Constraint**: GPSDO temporal continuity

- Track minute-to-minute consistency
- Use previous measurement + GPSDO stability to predict next
- Flag discontinuities (>1ms residual) as anomalies

**Secondary Constraint**: Cross-frequency consistency (same station)

- Different frequencies sample different ionospheric layers
- Consistency validates propagation model

**Tertiary Constraint**: Cross-station consistency

- Different paths provide redundancy
- Disagreement indicates propagation anomaly, not measurement error

### 2. No Dynamic Calibration

**Static Detection Bias**: Measure once during system characterization

- Matched filter group delay
- Tone detection threshold effects
- Store as constants, never update from data

**No Forcing to Zero**: Let measurements report actual propagation state

### 3. Simplified Fusion

**Input**: Raw D_clock measurements (RTP arrival - UTC emission)

**Process**:

1. Check temporal continuity (GPSDO ruler)
2. Weight by stability hierarchy
3. Combine with Kalman filter (tracking slow drift, not forcing to target)

**Output**: Fused D_clock that naturally centers around 0ms

### 4. Robust to Sparse Data

**Minimum Viable**: 1 broadcast (with appropriate uncertainty)

- Use last-known-good during brief gaps
- Increase uncertainty based on gap duration

**Graceful Degradation**: Don't fail completely when broadcasts drop

---

## System Architecture

### Data Flow

```
Radiod (IQ) 
  → Core Recorder (Digital RF HDF5)
  → Analytics (9 channels) 
  → Fusion 
  → Chrony SHM
```

### Key Services

1. **timestd-core-recorder**: Receives IQ from radiod, writes Digital RF
2. **timestd-analytics**: Processes IQ → timing measurements (9 channels)
3. **timestd-fusion**: Fuses measurements → Chrony SHM updates
4. **timestd-web-api**: FastAPI Dashboard & API (Port 8000)

### Critical File Locations

- **Source Code**: `/home/mjh/git/hf-timestd/src/hf_timestd/`
- **Production Code**: `/opt/hf-timestd/venv/lib/python3.11/site-packages/hf_timestd/`
- **Data Root**: `/var/lib/timestd/`
- **Logs**: `journalctl -u timestd-*`

---

## Broadcast Structure

The system monitors up to 17 time signal broadcasts:

| Station | Frequencies | Notes |
|---------|-------------|-------|
| WWV | 2.5, 5, 10, 15, 20, 25 MHz | 6 broadcasts |
| WWVH | 2.5, 5, 10, 15 MHz | 4 broadcasts (shared with WWV) |
| CHU | 3.33, 7.85, 14.67 MHz | 3 broadcasts (unique, FSK) |
| BPM | 2.5, 5, 10, 15 MHz | 4 broadcasts (shared) |

**Anchor Channels** (unambiguous station ID):

- CHU: 3.33, 7.85, 14.67 MHz (CHU-only)
- WWV: 20, 25 MHz (WWV-only)

**Shared Channels** (require discrimination):

- 2.5, 5, 10, 15 MHz (WWV + WWVH + BPM)

---

## Current System Problems

### 1. Calibration/Kalman Feedback Loop

**Symptom**: Fusion output at -1.5ms to -6ms instead of centering around 0ms

**Root Cause**:

1. Raw measurements: +0.7ms (correct!)
2. Kalman filter: Pulls to -6ms (wrong state)
3. Calibration: Learns `-mean(D_clock)` = +6ms offset
4. Next iteration: Measurements now at 0ms, Kalman pulls to new wrong state
5. Feedback loop prevents convergence

**Solution**: Remove auto-calibration, redesign Kalman to track drift not force to target

### 2. Hierarchical Weighting Numerics

**Problem**: Absolute uncertainty values (0.1ms vs 2.0ms) create 400:1 weight ratios

**Impact**: Kalman filter covariance matrix becomes unstable → infinite uncertainty

**Solution**: Use relative multipliers (0.5x-1.0x) instead of absolute values

### 3. Analytics Bloat

**Problem**: Propagation solver has accumulated complexity over time

**Impact**: Harder to maintain, debug, and understand

**Solution**: Simplify to core function: RTP timestamp → D_clock measurement

---

## Files to Focus On for Redesign

### Analytics (Phase 2)

**Core Engine**:

- `src/hf_timestd/core/phase2_temporal_engine.py` - Main processing loop
- `src/hf_timestd/core/transmission_time_solver.py` - Propagation delay calculation
- `src/hf_timestd/core/timing_calibrator.py` - Bootstrap and calibration

**Key Changes Needed**:

- Simplify propagation solver
- Remove dynamic calibration
- Implement GPSDO temporal continuity tracking

### Fusion (Phase 3)

**Core Engine**:

- `src/hf_timestd/core/multi_broadcast_fusion.py` - Fusion and Kalman filter

**Key Changes Needed**:

- Remove auto-calibration system (lines 1788-1878)
- Redesign Kalman filter to track drift, not force to target
- Implement hierarchical stability weighting with safe multipliers
- Add temporal continuity validation

---

## Specific Issues to Investigate

### Why Only 2-3 Broadcasts Available?

**Expected**: 5-7+ broadcasts  
**Actual**: 2-3 broadcasts (grade D)

**Possible Causes**:

1. D_clock validation rejecting too many? (±5ms threshold in `phase2_temporal_engine.py`)
2. Cross-station validation too strict? (0.75ms threshold in `multi_broadcast_fusion.py`)
3. Analytics not detecting signals?
4. Propagation conditions genuinely poor?

**Investigation Steps**:

1. Check analytics logs for D_clock validation failures
2. Check fusion logs for cross-station exclusions
3. Review HDF5 files for measurement availability
4. Check propagation conditions (solar activity, time of day)

### What Caused the 1-Hour Data Gap?

**Observation**: No fusion output from 09:32-10:38 UTC (66 minutes)

**Possible Causes**:

1. All broadcasts lost signal simultaneously (unlikely)
2. Analytics service stopped producing measurements
3. HDF5 file access issue
4. Service crash/restart (not evident in logs)

**Investigation Steps**:

1. Check analytics service logs for 09:30-10:40 UTC
2. Check HDF5 file timestamps
3. Check system logs for service restarts
4. Check for disk space or I/O issues

---

## Next Session Objectives

### Phase 1: Understand Current State

1. **Investigate broadcast availability**: Why only 2-3 instead of 5-7+?
2. **Investigate data gap**: What caused the 1-hour outage?
3. **Review validation thresholds**: Are they too strict?

### Phase 2: Redesign Analytics

1. **Simplify propagation solver**: Focus on core measurement
2. **Remove dynamic calibration**: Use static detection bias only
3. **Implement GPSDO temporal continuity**: Track minute-to-minute consistency
4. **Adjust validation thresholds**: Based on Phase 1 findings

### Phase 3: Redesign Fusion

1. **Remove auto-calibration system**: Delete `_update_calibration()` method
2. **Redesign Kalman filter**: Track drift, don't force to target
3. **Implement hierarchical weighting**: Use safe multipliers (0.5x-1.0x)
4. **Add temporal continuity validation**: Flag discontinuities

### Phase 4: Verify and Deploy

1. **Test with historical data**: Verify convergence to 0ms
2. **Monitor for stability**: No discontinuities, no feedback loops
3. **Deploy to production**: With rollback plan
4. **Document changes**: Update CHANGELOG and TECHNICAL_REFERENCE

---

## Important Notes for AI Agents

- **GPSDO is the steel ruler**: Temporal continuity is the primary constraint
- **No dynamic calibration**: Propagation delays are real physics, not errors to calibrate away
- **Measurements should center around 0ms**: With GPSDO + UTC broadcasts
- **Hierarchical weighting**: Use relative multipliers, not absolute uncertainties
- **Graceful degradation**: System should work with 1 broadcast, not fail completely

**Critical Files**:

- `/home/mjh/.gemini/antigravity/brain/.../walkthrough.md` - Detailed learnings from 2026-01-07 session
- `CONTEXT.md` - This file (keep updated as redesign progresses)

---

**Last Updated**: 2026-01-07 11:15 UTC  
**Prepared For**: Analytics/Fusion Redesign Session  
**Key Insight**: GPSDO temporal stability is the foundation - build everything on that
