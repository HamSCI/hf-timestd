# NEVER CHANGE THE FOLLOWING PRIMARY INSTRUCTION!!!

**Primary Instruction:** In this context you will perform a critical review of the HF Time Standard (hf-timestd) project, either in its entirety or in a specific component, as specified by the user. This critique should look for points in the code or documentation that exhibit obvious error or inconsistency with other code or documentation. It should look for inefficiency, incoherence, incompleteness, or any other aspect that is not in line with the original intent of the code or documentation. It should also look for obsolete, deprecated, or "zombie" code that should be removed. Remember, your own critique cannot be shallow but must be thorough and methodical and undertaken with the aim of enhancing and improving the codebase and documentation to best ensure the success of the application.

Make your criticism from the perspective of 1) a user of the system, 2) a metrologist, 3) a ionospheric scientist, and 4) a software engineer. These perspectives can differ in their priorities and interests, and your critique should reflect this. For example, a user of the system will be most interested in the accuracy and reliability of the system, while a metrologist will be most interested in the precision and traceability of the system, while an ionospheric scientist will be most interested in the ionospheric conditions and their impact on the system, and a software engineer will be most interested in the code quality, maintainability, coverage and adequacy of testing,and the resilience and robustness of the system over time and in edge cases. Ultimately, however, a consensus of these perspectives should guide your critique in service of the meeting the application's objectives.

# NEVER CHANGE THE PRECEEDING PRIMARY INSTRUCTION!!!

# The following secondary instruction and information will guide your critique in this particular session (the instructions below will vary from session to session)

---

## 📋 NEXT SESSION: FUSION FAILURE + THEORETICAL vs ACHIEVED ACCURACY

**Objective:** Diagnose why fusion has stopped producing output despite L2 measurements being generated, and critically examine why achieved timing accuracy falls far short of theoretical limits.

---

## 🚨 CRITICAL ISSUE: Fusion Service Producing No Output

### Current State (2026-02-06)

| Component | Status |
|-----------|--------|
| `timestd-metrology.service` | Running, producing L2 measurements |
| `timestd-fusion.service` | Running but **no output for 10+ hours** |
| L2 measurements (24h) | WWV: 700+, CHU: 600+, etc. ✓ |
| L3 fusion output | **0 records in last 6 hours** |
| `/api/metrology/fusion/latest` | `"No recent fusion data available"` |

### Questions to Investigate

1. **Why is fusion silent?** The service is running (PID active, 305MB memory) but producing nothing.
   - Is it failing to read L2 data?
   - Is it rejecting all measurements?
   - Is there an exception being swallowed?

2. **What are the fusion rejection criteria?** Review `multi_broadcast_fusion.py`:
   - Minimum number of broadcasts required?
   - Quality thresholds?
   - Timing consistency requirements?

3. **Is there a data flow break?** Trace the path:
   ```
   L2 HDF5 files → DataProductReader → FusionEngine → L3 HDF5 + Chrony SHM
   ```

### Key Files for Fusion Investigation

| File | Purpose |
|------|---------|
| `src/hf_timestd/core/multi_broadcast_fusion.py` | Main fusion orchestration |
| `src/hf_timestd/core/broadcast_kalman_filter.py` | Per-broadcast state estimation |
| `src/hf_timestd/core/global_kalman_filter.py` | Multi-broadcast fusion |
| `src/hf_timestd/io/hdf5_reader.py` | L2 data reading |
| `src/hf_timestd/io/hdf5_writer.py` | L3 data writing |

### Diagnostic Commands

```bash
# Check fusion service logs
journalctl -u timestd-fusion -f

# Check if L2 data is readable
python3 -c "from hf_timestd.io.hdf5_reader import DataProductReader; ..."

# Check fusion HDF5 file (may be locked)
h5dump -H /var/lib/timestd/phase2/fusion/global_physics_20260206.h5
```

---

## 🎯 CRITICAL ISSUE: Theoretical vs Achieved Accuracy Gap

### The Promise

The theoretical timing accuracy of this system should be **sub-millisecond** based on:
- GPSDO-locked sample clock (L4/L5 accuracy, ~10ns)
- 24 kHz sample rate → 42µs sample resolution
- Matched filter detection → sub-sample interpolation possible
- Multiple independent broadcasts → √N improvement from fusion

### The Reality

Current achieved accuracy is **tens of milliseconds**, orders of magnitude worse:
- Systematic offset: +40 to +85ms (CHU), +30 to +55ms (WWV)
- Fusion D_clock: When working, shows ±10-30ms variations
- Allan deviation: ~15ms at τ=10s (should be <1ms)

### Root Causes to Investigate

1. **GPS_TIME/RTP_TIMESNAP Latency (~70ms)**
   - radiod captures this mapping, but when?
   - Is there pipeline latency between RTP packet and GPS time sample?
   - This is the dominant systematic error.

2. **Ionospheric Propagation Model**
   - Are we using the correct propagation delay model?
   - IRI-2020 gives layer heights, but are we computing path delay correctly?
   - Is the "minimum propagation delay" (great circle / c) appropriate?

3. **Detection Algorithm Bias**
   - Matched filter finds correlation peak, but is there systematic bias?
   - Template asymmetry? Edge effects? Interpolation errors?

4. **Fusion Algorithm Issues**
   - Kalman filter process noise tuning?
   - Measurement noise estimation?
   - Outlier rejection too aggressive or too lenient?

### What "Success" Looks Like

| Metric | Current | Target | Theoretical Limit |
|--------|---------|--------|-------------------|
| Systematic offset | +50ms | <1ms | ~0 (calibrated) |
| Random error (1σ) | ~15ms | <1ms | ~0.1ms (SNR-limited) |
| Fusion D_clock | ±30ms | ±1ms | ±0.1ms |
| Allan deviation (τ=60s) | ~6ms | <0.5ms | ~0.05ms |

---

## 📊 Recent Progress (2026-02-06)

### Completed This Session

1. **24-Hour Dashboard** — New visualization showing all 17 broadcasts:
   - Solar zenith at path midpoints
   - SNR time series
   - Timing error (ToA - expected)
   - API endpoints: `/api/dashboard/broadcasts/24h`, etc.

2. **Bug Fixes**:
   - Timing Stability panel in station.html (was empty)
   - Navigation links to 24h dashboard on all pages

### L2 Data Production (Working)

```
WWV_2500:  706 measurements/24h
WWV_5000:  652 measurements/24h
WWV_10000: 622 measurements/24h
CHU_7850:  600+ measurements/24h
```

---

## 🔬 Methodology Review (Carried Forward)

### Detection Pipeline

```
Raw IQ Buffer (1 minute)
    ↓
AM Demodulation (magnitude - mean)
    ↓
Matched Filter Detection (500ms CHU, 800ms WWV/WWVH)
    ↓
Physics Validation (ArrivalPatternMatrix)
    ↓
L1 Metrology Measurement
    ↓
L2 Calibrated Timing (propagation model applied)
    ↓
L3 Fusion (multi-broadcast Kalman filter) ← FAILING HERE
```

### Questions Still Open

1. **Circularity:** Does expected arrival time depend on measurements that depend on expected arrival time?

2. **Template matching:** Are templates optimal? CHU uses 500ms but transmits 300ms at most seconds.

3. **Missed opportunities:**
   - CHU FSK timing (seconds 31-39)
   - Phase tracking for sub-sample resolution
   - Doppler for ionospheric correction

---

## ✅ Success Criteria for Next Session

- ⬚ **Fusion producing output again** — Identify and fix the blockage
- ⬚ **Root cause of ~70ms systematic offset identified**
- ⬚ **Plan to achieve sub-millisecond accuracy** — Either fix source or calibrate
- ⬚ **Document findings** in `docs/changes/` or update architecture docs
