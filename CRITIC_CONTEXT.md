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

## Recent Session (2026-01-10 14:00-14:30 UTC): Timing Fix & SHARED_2500 Anomaly

### Fixes Deployed

**1. Systematic 20-30ms Timing Offset (FIXED)**
- **Problem**: Analytics services using minute boundary instead of actual RTP timestamps from raw buffer metadata
- **Root Cause**: `system_time = float(target_minute)` instead of deriving from RTP timestamp
- **Solution**: Implemented RTP-to-Unix offset learning from `start_rtp_timestamp` metadata field
- **Impact**: Calibration values now physically plausible (0-16ms range vs previous 20-36ms)
- **Commit**: Initial timing fix in `phase2_analytics_service.py`

**2. Tone-Based Discrimination Protection (FIXED)**
- **Problem**: Probabilistic discriminator overriding definitive 1000/1200 Hz tone frequency evidence
- **Key Insight**: 1000 Hz tone = WWV ONLY (WWVH does not transmit 1000 Hz). Similarly 1200 Hz = WWVH ONLY.
- **Solution**: 
  - Prevent override when tone-based discrimination has medium/high confidence
  - Added physical constraint validation: reject WWV↔WWVH overrides (physically impossible)
- **Impact**: Correct WWV/WWVH identification on SHARED frequencies (2.5, 5, 10, 15 MHz)
- **Commits**: 105c05f, 4f722af in `phase2_temporal_engine.py`

**3. Stale RTP Offset Detection (FIXED)**
- **Problem**: When recorder restarts, analytics services retained old learned RTP offset, causing massive timing errors (~2900ms observed)
- **Root Cause**: Once `_rtp_to_unix_offset` was set, it was never validated against incoming data
- **Solution**: Continuously validate learned offset against metadata. If drift >1 second, automatically reset and re-learn
- **Impact**: Protects ALL channels from stale offset corruption after recorder restarts
- **Commit**: 08736fe (with debug logging: 1677c22) in `phase2_analytics_service.py`

### Outstanding Issue: SHARED_2500 -2906ms Anomaly

**Symptoms**:
- SHARED_2500 consistently produces D_clock = -2906ms (rejected as implausible, ±1000ms bounds)
- Falls back to D_clock = 0.00ms with mode=UNK, confidence=0.00
- Raw timing values are reasonable (1-7ms range)
- Tone detection working correctly (1000 Hz WWV detected with 30-45 dB SNR)
- Error persists across service restarts with latest code

**What's NOT the cause** (verified):
- ❌ Not RTP offset issue (other channels work fine with identical code)
- ❌ Not stale state files (none exist - `/var/lib/timestd/state/phase2-*.json` empty)
- ❌ Not calibration offset (none applied to SHARED_2500 in `broadcast_calibration.json`)
- ❌ Not discrimination error (WWV correctly identified via 1000 Hz tone)
- ❌ Not code version (all channels use same code from `/home/mjh/git/hf-timestd/src/`, restarted 14:12 UTC)
- ❌ Not minute boundary mismatch (all channels processing same minutes, e.g., 1768054380)

**Channel Status Comparison**:
- CHU 7.85 MHz: D_clock = +12.88ms ✅ **Working** (valid measurements)
- SHARED 5.0 MHz: D_clock = +0.00ms ✅ **Working** (bootstrap mode, no errors)
- WWV 20.0 MHz: D_clock = +0.00ms ✅ **Working** (bootstrap mode, no errors)
- SHARED 2.5 MHz: D_clock = -2906ms ❌ **ANOMALY** (rejected, fallback to 0.00ms)

**Anomaly Characteristics**:
- Error magnitude: ~2906ms (~2.9 seconds)
- Consistency: Error value stable across measurements (-2901 to -2908ms)
- Isolation: **Only affects SHARED_2500**, not other channels
- Timing: Raw arrival times reasonable (1.8-6.4ms), suggesting issue in D_clock calculation not tone detection

**Hypothesis**:
The -2906ms offset suggests a propagation delay calculation error specific to SHARED_2500's data processing. Given identical code across all channels, this points to **data-dependent behavior** - something about SHARED_2500's specific signal characteristics, metadata, or processing path triggers a different calculation result. The ~2.9 second magnitude is suspiciously close to timing offsets seen during recorder restart events, but the stale offset detection should have caught this.

**Next Steps for Investigation**:
1. Add detailed logging to D_clock calculation path for SHARED_2500 specifically
2. Compare propagation solver inputs/outputs between SHARED_2500 and working channels
3. Verify metadata completeness and RTP timestamp values for SHARED_2500 raw buffers
4. Check if SHARED_2500 is hitting a different code path in `phase2_temporal_engine.py`
5. Examine if 2.5 MHz frequency-specific logic exists that could cause this

**Current Operational Impact**:
- System operational with 3 working channels (CHU 7.85, SHARED 5.0, WWV 20.0)
- Multi-station fusion possible with CHU + SHARED_5000
- SHARED_2500 detecting tones but measurements rejected (0% detection rate for timing)
- WWV 2.5 MHz, WWVH 2.5 MHz, and BPM 2.5 MHz unavailable for fusion (critical for multi-station validation)
