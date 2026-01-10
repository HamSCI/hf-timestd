# Critic Context: HF-TimeStd Project State

## Current Status (2026-01-10)

- **Clock Discipline**: Restored. `timestd-fusion` is successfully feeding Chrony SHM (Reach > 0).
- **Pipeline Health**: `verify_pipeline.sh` reports **PASS**. l1 (Tone), l2 (Measurements), and l3 (Fusion) are active.
- **Recent Fixes**:
  - **Single-Station Fusion**: Enabled to support 10MHz-only bootstrap (`n_stations >= 1`).
  - **Chrony Precision**: Fixed inverted math bug that caused "false ticker" rejection.
  - **Tone Detection**: Widened search bounds to `±250ms` to catch uncalibrated clocks.

## Critical Focus Areas for Next Session

The user intends to "examine with critical detail the fusion, adaptive calibration, and chrony feed to ensure theoretical and methodological integrity." The next agent should focus on:

1. **Theoretical Integrity of Fusion**:
    - Scrutinize the `MultiBroadcastFusion` logic, particularly the `_reject_outliers` and Kalman Filter tuning.
    - Verify if the "Science-First" architecture (separation of Physics/Fusion) is strictly adhered to, or if leakage exists.
    - **Key Question**: Is the single-station relaxation mathematically sound for long-term stability, or just a bootstrap hack?

2. **Adaptive Calibration (The "Steel Ruler")**:
    - Review `BroadcastCalibration` logic. Is the system learning valid ionospheric offsets, or absorbing clock errors into calibration?
    - **Risk**: "God Mode" calibration absorbing real clock drift, hiding it from Chrony.

3. **Chrony Feed Methodology**:
    - Validate the precision mapping (`log2(uncertainty) - 10`). Is this optimal for Chrony?
    - Investigate the `consistency_flag` logic. Should we be feeding "CROSS_STATION_DISAGREE" data even with low precision?

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
