# Critic Context: HF-TimeStd Project State

## Current Status (2026-01-10 - Post Critical Analysis)

- **Clock Discipline**: Restored with STRICTER validation criteria. `timestd-fusion` feeds Chrony SHM only with validated, multi-station data.
- **Pipeline Health**: `verify_pipeline.sh` reports **PASS**. l1 (Tone), l2 (Measurements), and l3 (Fusion) are active.
- **Recent Fixes** (2026-01-10):
  - **GPSDO Lock Protection**: Calibration updates now skip if any measurement has unlocked GPSDO (prevents absorbing clock drift).
  - **Single-Station Safeguards**: Uncertainty inflated 5x in single-station mode, Chrony feed disabled, validation flags added to output.
  - **Stricter Chrony Feed**: Only feeds OK consistency or low-uncertainty (<0.5ms) INTER_ANOMALY, requires n_stations >= 2.
  - **Validation Flags**: Added `single_station_mode` boolean to HDF5 output for scientific data quality tracking.
- **Previous Fixes**:
  - **Single-Station Fusion**: Enabled to support 10MHz-only bootstrap (`n_stations >= 1`) - NOW WITH SAFEGUARDS.
  - **Chrony Precision**: Fixed inverted math bug that caused "false ticker" rejection.
  - **Tone Detection**: Widened search bounds to `±250ms` to catch uncalibrated clocks.

## Critical Analysis Completed (2026-01-10)

A comprehensive critical analysis was performed examining fusion, calibration, and Chrony feed for theoretical and methodological integrity. See `CRITICAL_ANALYSIS.md` for full details.

### Key Findings

1. **Theoretical Integrity of Fusion**: ✅ **SOUND**
    - Inverse variance weighting follows ISO GUM principles
    - MAD-based outlier rejection is robust (3σ threshold)
    - Kalman filter mathematically correct but requires monitoring
    - **Single-station mode identified as bootstrap hack, NOT scientifically valid**

2. **Adaptive Calibration**: ⚠️ **RISK MITIGATED**
    - Safeguards in place: cross-validation gating, rate limiting, per-broadcast separation
    - **FIXED**: Added GPSDO lock check to prevent absorbing clock drift
    - Calibration learned but not applied during normal operation (Kalman handles convergence)

3. **Chrony Feed Methodology**: ⚠️ **IMPROVED**
    - Precision mapping mathematically correct: `log2(uncertainty_ms) - 10`
    - **FIXED**: Stricter consistency criteria (only OK or low-uncertainty INTER_ANOMALY)
    - **FIXED**: Disabled feed in single-station mode (no cross-validation possible)

### Fixes Implemented

See `IMPLEMENTATION_SUMMARY.md` for detailed documentation. Summary:

1. ✅ **GPSDO Lock Protection**: Calibration skips updates if GPSDO unlocked
2. ✅ **Single-Station Safeguards**: 5x uncertainty inflation, Chrony feed disabled, validation flags
3. ✅ **Stricter Chrony Feed**: Requires n_stations >= 2, only OK or low-uncertainty INTER_ANOMALY
4. ✅ **Validation Flags**: `single_station_mode` added to all output formats (HDF5, schema, models)

## Critical Focus Areas for Next Session

With the immediate critical issues addressed, future work should focus on:

1. **Monitoring & Validation**:
    - Monitor Kalman state for divergence (>2ms threshold)
    - Track single-station mode frequency and duration
    - Verify Chrony feed decisions in logs

2. **Independent Time Reference**:
    - Add GPS receiver for ground truth validation
    - Compare HF-derived time to GPS hourly
    - Alert if disagreement >2ms

3. **Long-Term Stability**:
    - Implement calibration quality metrics (convergence rate)
    - Add automatic calibration reset on sustained cross-validation failure
    - Open-loop monitoring to detect feedback loop issues

## Known Risks / Technical Debt

- **Precision Formula**: The new `-10` constant is empirical. It implies `1ms error -> precision -20 (1µs)`. Wait, `log2(0.001) = -10`.
  - Formula used: `log2(uncertainty_sec)`.
  - Example: 1ms = 0.001s -> `log2(0.001) ≈ -10`.
  - Previous bad code: `-10 - log2(1) = -10`. `-10 - log2(0.001) = 0`. This claimed 1ms error was 1s precision (bad) and 1s error was 1ms precision (inverted).
  - New code: `log2(uncertainty_ms) - 10`.
    - 1000ms -> `log2(1000)=10`. `10-10=0`. Precision $2^0=1s$. Correct.
    - 1ms -> `log2(1)=0`. `0-10=-10`. Precision $2^{-10} \approx 1ms$. Correct.
  - **CRITICAL**: Verify Chrony's precision definition (log2 seconds?). If so, ensuring this mapping is mathematically robust is key.
- **Single Point of Failure**: Currently relying heavily on 10MHz. 25MHz is dead. Need to assess 5MHz/15MHz.

## Operational Context

- **Services**: `timestd-core-recorder`, `timestd-analytics`, `timestd-fusion`, `timestd-physics` are all **active**.
- **Time Source**: `198.71.50.75` (NTP) is the primary reference. `TMGR` (SHM 0) is the fusion feed.

## Recent Session (2026-01-10 14:30-14:50 UTC): SHARED_2500 Anomaly Resolution

### Issue: SHARED_2500 -2906ms Anomaly (RESOLVED)

**Symptoms**:
- SHARED_2500 consistently produced D_clock = -2906ms to -3589ms (rejected as implausible)
- Fell back to D_clock = 0.00ms with mode=UNK, confidence=0.00
- 0% detection rate for all 2.5 MHz broadcasts (WWV, WWVH, BPM)

**Root Cause**:
Stale timing calibration file with **sample rate mismatch**:
- Calibration file: `sample_rate: 20000` Hz, `rtp_offset_samples: 586210` (from old system configuration)
- Current system: `sample_rate: 24000` Hz (all channels)
- Mismatch caused `expected_second_rtp` to be calculated 86107 samples (3587ms) too large

**Why Only SHARED_2500**:
The stale calibration at `/dev/shm/timestd/state/timing_calibration.json` only had an entry for SHARED_2500 with the incorrect sample_rate. Other channels either had no calibration entry (bootstrapping fresh) or had correct 24 kHz calibration.

**Fix Applied**:
1. Deleted stale calibration files (`/dev/shm/timestd/state/timing_calibration.json`, `/var/lib/timestd/state/timing_calibration.json`)
2. Restarted `timestd-analytics` service
3. System re-learned calibration at correct 24 kHz sample rate

**Verification**:
- Before: D_clock = -3583ms (rejected)
- After: D_clock = -22.42ms, -2.17ms (valid, within ±100ms)
- New calibration: `sample_rate: 24000`, `rtp_offset_samples: 500640` ✓

**Diagnostic Improvements**:
Added detailed D_clock calculation logging to `transmission_time_solver.py` and `phase2_temporal_engine.py` to aid future debugging.

**Documentation**: See `SHARED_2500_FIX_2026-01-10.md` for complete analysis.

## Next Session Objective (2026-01-10+): Chrony Feed Diagnosis

### Current Issue: Chrony Not Being Updated

**Observation**: Despite stricter Chrony feed criteria being implemented, Chrony SHM is not receiving updates from `timestd-fusion`.

**Investigation Required**:
1. **Verify Fusion Service Status**:
   - Check if `timestd-fusion` is running and processing measurements
   - Review fusion logs for Chrony feed decisions
   - Confirm multi-station measurements are available (n_stations >= 2)

2. **Check Chrony Feed Criteria**:
   - Verify measurements meet consistency requirements (OK or low-uncertainty INTER_ANOMALY)
   - Check if single-station mode is preventing feed
   - Review uncertainty thresholds (<0.5ms for INTER_ANOMALY)

3. **Validate SHM Communication**:
   - Confirm SHM segment 0 exists and is writable
   - Check Chrony configuration for SHM refclock
   - Verify no permission issues

4. **Review Recent Changes**:
   - Stricter Chrony feed criteria (2026-01-04) may be too restrictive
   - GPSDO lock protection may be blocking updates
   - Single-station safeguards may be triggering incorrectly

**Expected Behavior**:
- Fusion should feed Chrony SHM when:
  - n_stations >= 2 (multi-station validation)
  - Consistency = OK OR (Consistency = INTER_ANOMALY AND uncertainty < 0.5ms)
  - GPSDO locked for all contributing measurements

**Diagnostic Commands**:
```bash
# Check fusion service
systemctl status timestd-fusion
journalctl -u timestd-fusion --since "10 minutes ago" | grep -i chrony

# Check SHM status
ipcs -m | grep timestd

# Check Chrony sources
chronyc sources -v
chronyc sourcestats

# Review fusion output
tail -100 /var/lib/timestd/fusion/fusion_output.csv
```

**Success Criteria**:
- Chrony SHM receiving regular updates (every minute when conditions met)
- TMGR (SHM 0) showing as valid refclock in `chronyc sources`
- System clock disciplined by HF-derived time when multi-station validation passes
