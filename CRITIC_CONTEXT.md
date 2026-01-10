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
