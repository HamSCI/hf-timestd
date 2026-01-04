# Changelog

All notable changes to this project will be documented in this file.

## [3.10.3] - 2026-01-04

### Fixed - Comprehensive Architectural Improvements

**Priority 1: Critical Fixes**

#### Calibration Update Order Fixed
- **Issue**: Calibration being updated BEFORE cross-validation, allowing outliers to contaminate calibration state
- **Root Cause**: WWV tone misidentification was updating calibration, causing slow drift toward incorrect values
- **Impact**: Calibration slowly diverged, requiring periodic manual resets
- **Fix**: Moved calibration update AFTER cross-validation, only updating with validated measurements
- **Files**: `src/hf_timestd/core/multi_broadcast_fusion.py`

#### Cross-Station Validation Threshold Increased
- **Issue**: 0.2ms threshold too strict, causing false positives on legitimate propagation differences
- **Root Cause**: Real physics - different ionospheric paths between stations (CHU vs WWV = 2000+ km)
- **Impact**: Valid measurements flagged as suspects, reducing fusion quality
- **Fix**: Increased threshold from 0.2ms to 1.0ms to account for real propagation differences
- **Files**: `src/hf_timestd/core/multi_broadcast_fusion.py`

#### GPSDO Lock Status Check Added
- **Issue**: Fusion accepting measurements from unlocked GPSDOs
- **Root Cause**: No validation of `gpsdo_locked` flag in fusion service
- **Impact**: Unlocked GPSDO can drift by seconds, causing massive timing errors
- **Fix**: Filter out measurements where GPSDO is not locked
- **Files**: `src/hf_timestd/core/multi_broadcast_fusion.py`

**Priority 2: High Priority Fixes**

#### Calibration Persistence Across Restarts
- **Issue**: Calibration reset to zero on every service restart, requiring 10-20 minute bootstrap
- **Root Cause**: No persistence mechanism for calibration state
- **Impact**: Service restarts cause grade degradation and chrony instability
- **Fix**: Auto-save calibration every 50 updates, load on startup, skip warmup penalty
- **Behavior**: Immediate grade A performance after restart
- **Files**: `src/hf_timestd/core/multi_broadcast_fusion.py`

#### Kalman Filter State Bounds
- **Issue**: Kalman filter can diverge if fed bad data, no recovery mechanism
- **Root Cause**: No bounds checking on filter state
- **Impact**: Once diverged, takes hours to recover, causes multi-hour timing errors
- **Fix**: Reset filter if state exceeds ±10ms
- **Files**: `src/hf_timestd/core/multi_broadcast_fusion.py`

**Priority 3: Medium Priority Fixes**

#### Complete Uncertainty Budget (ISO GUM Compliant)
- **Issue**: Uncertainty budget missing RTP jitter component
- **Root Cause**: Incomplete uncertainty sources in RSS calculation
- **Impact**: Underestimated uncertainty, not fully traceable to UTC(NIST)
- **Fix**: Added RTP timestamp jitter component (0.1ms) to uncertainty budget
- **Files**: `src/hf_timestd/core/multi_broadcast_fusion.py`

#### D_clock Monotonicity Check
- **Issue**: No validation that D_clock changes are physically reasonable
- **Root Cause**: Large jumps (>5ms) not detected or logged
- **Impact**: Tone misidentification events go unnoticed
- **Fix**: Log error when D_clock jumps >5ms between cycles
- **Files**: `src/hf_timestd/core/multi_broadcast_fusion.py`

**Technical Details:**
- All fixes follow metrologist best practices and ISO GUM guidelines
- Calibration now protected from outlier contamination
- System recovers gracefully from filter divergence
- Complete uncertainty budget ensures traceability
- Immediate grade A performance after service restart

## [3.10.2] - 2026-01-04

### Fixed - Fusion Discontinuities from Tone Misidentification

#### Aggressive Outlier Rejection for Discrimination Suspects

- **Issue**: Fusion D_clock showing discontinuities (jumps of 5-10ms) despite GPSDO lock
- **Root Cause**: WWV station systematically reporting D_clock 1-4ms too negative due to tone misidentification, contaminating Kalman filter
- **Impact**: Fusion drift of 15ms over 5 minutes, quality degradation from grade A to C
- **Fix**: Modified Kalman filter to use only clean measurements when `DISCRIMINATION_SUSPECT` flag is set
- **Behavior**: Outliers properly excluded, fusion stable at grade A with <0.5ms uncertainty
- **Files**: `src/hf_timestd/core/multi_broadcast_fusion.py`

**Technical Details:**
- System correctly detected cross-station disagreement (CHU vs WWV: 1-4ms)
- Flagged measurements as `DISCRIMINATION_SUSPECT` 
- Previous code recalculated fused D_clock but still fed contaminated data to Kalman filter
- Fix ensures Kalman filter receives only validated measurements
- Result: Fusion converges smoothly to UTC without discontinuities

#### Removed Duplicate Chrony SHM Updates

- **Issue**: Chrony switching away from TMGR source, causing 20ms+ discontinuities when switching to network NTP
- **Root Cause**: Duplicate SHM updates (main loop + threaded updater) causing chrony to perceive high jitter (4.3ms std dev)
- **Impact**: Chrony sourcestats showed TMGR as unreliable, switched to network NTP server
- **Fix**: Removed threaded SHM updater, now only updating SHM directly in main fusion loop
- **Behavior**: Chrony now consistently selects TMGR as active source (#*), reach stable
- **Files**: `src/hf_timestd/core/multi_broadcast_fusion.py`

**Technical Details:**
- Previous implementation had both direct SHM write in main loop AND threaded updater
- Timing inconsistencies between the two updates appeared as jitter to chrony
- Removed `ChronySHMUpdater` thread completely
- Single update path ensures consistent timing
- Chrony now trusts TMGR source and stays locked

## [3.10.1] - 2026-01-04

### Fixed - Critical Service Stability Issues

#### Fusion Interval Optimized for Chrony Reach

- **Issue**: Chrony reach cycling 21→42→104→210 (25% success rate) instead of reaching 377 (100%)
- **Root Cause**: Fusion running every 60s while chrony polls every 8s, causing stale data rejection
- **Impact**: Suboptimal time synchronization, chrony not fully utilizing fusion data
- **Fix**: Reduced fusion interval from 60s to 8s, increased chrony poll from 3 to 4 (8s to 16s)
- **Behavior**: Fresh fusion data available for every chrony poll, reach stable at 87.5%
- **Files**: `systemd/timestd-fusion.service`, `src/hf_timestd/core/multi_broadcast_fusion.py`, `/etc/chrony/chrony.conf`

**Technical Details:**
- Fusion now calculates new D_clock every 8 seconds (was 60s)
- Chrony polls every 16 seconds (was 8s), giving fusion 2 cycles to complete
- Direct SHM write in main fusion loop ensures synchronization
- Reach register shows 87.5% success rate (7 out of 8 polls)
- Improved time discipline with 8x more frequent fusion updates
- Chrony consistently selects TMGR as active source (#*)

#### Systemd Watchdog Timeout Increased

- **Issue**: Fusion service crashed continuously with SIGABRT every 30 seconds
- **Root Cause**: 30-second watchdog timeout too aggressive for HDF5 read operations
- **Impact**: 16+ consecutive crashes from 02:23 UTC onwards, complete chrony feed failure
- **Fix**: Increased watchdog timeout from 30s to 120s
- **Behavior**: Service can now complete first fusion cycle without being killed
- **Files**: `systemd/timestd-fusion.service`

**Technical Details:**
- First `fuse()` call legitimately takes >30s to read 10 minutes of HDF5 data from 9 channels
- SWMR mode reads require metadata refresh and can experience lock contention
- Watchdog ping occurs inside main loop, after fusion calculation completes
- 120s timeout provides adequate margin for worst-case HDF5 read performance

#### HDF5 SWMR Mode Schema Evolution Protection

- **Issue**: Schema changes that add new fields caused service crashes and data degradation
- **Root Cause**: HDF5 files in SWMR mode cannot have new datasets added after initialization
- **Impact**: 2026-01-04 00:00-00:45 UTC degradation when `raw_arrival_time_ms` field was added
- **Fix**: Added SWMR mode check before attempting to create new datasets
- **Behavior**: New fields are skipped with warning until next file rotation (graceful degradation)
- **Files**: `src/hf_timestd/io/hdf5_writer.py`
- **Documentation**: `DEGRADATION_ROOT_CAUSE_2026-01-04.md`

**Technical Details:**
- HDF5 writer now checks `hdf5_file.swmr_mode` before creating datasets
- Schema version mismatch logged as warning instead of causing crash
- Missing fields are skipped until daily file rotation creates new file with correct schema
- Prevents cascading failures in analytics → fusion → chrony pipeline

**Deployment Note:**
- Future schema changes must be deployed after midnight UTC to align with file rotation
- Or force file rotation before deployment
- Or wait for natural daily rotation at 00:00 UTC

## [3.10.0] - 2026-01-04

### Added - Service Stability and Monitoring

#### Systemd Watchdog Integration

- **Feature**: Enabled systemd watchdog for fusion service with 30-second timeout
- **Configuration**: Changed fusion service type from `simple` to `notify`
- **Implementation**: Service already sends `WATCHDOG=1` notifications in main loop (line 2746)
- **Impact**: Automatic detection and restart of hung fusion service
- **Files**: `systemd/timestd-fusion.service`

#### Chrony Reach Monitoring

- **Script**: `scripts/check-chrony-reach.sh` - Monitor Chrony TMGR source reach value
- **Features**:
  - Configurable threshold (default: 64 decimal = 25% success rate)
  - Optional alert command execution
  - Exit codes for integration with monitoring systems
  - Octal to decimal conversion with success percentage
- **Usage**: Can be run manually or via systemd timer
- **Files**: `scripts/check-chrony-reach.sh`

#### Periodic Monitoring Timer

- **Service**: `timestd-chrony-monitor.service` - Oneshot service to check Chrony reach
- **Timer**: `timestd-chrony-monitor.timer` - Runs every 5 minutes
- **Configuration**: Persistent across reboots, starts 2 minutes after boot
- **Logging**: Output to systemd journal with `timestd-chrony-monitor` identifier
- **Files**: `systemd/timestd-chrony-monitor.service`, `systemd/timestd-chrony-monitor.timer`

#### Deployment Automation

- **Script**: `scripts/deploy-service-improvements.sh` - One-command deployment
- **Features**:
  - Installs monitoring script to `/opt/hf-timestd/scripts/`
  - Updates fusion service configuration
  - Enables and starts monitoring timer
  - Verifies deployment
  - Interactive fusion service restart
- **Files**: `scripts/deploy-service-improvements.sh`

### Fixed - Chrony Pipeline Resilience

#### Root Cause Investigation

- **Issue**: Chrony TMGR reach = 0, indicating no time updates for 76 minutes
- **Root Cause**: `timestd-fusion` service was stopped (inactive)
- **Timeline**:
  - 00:20 UTC: Service entered crash-loop (5 consecutive failures, exit code 1)
  - 00:21 UTC: Systemd gave up after 5 restart attempts
  - 00:45 UTC: Service manually stopped
  - 00:45-02:02 UTC: Service remained inactive (77 minutes)
  - 02:02 UTC: Service restarted during investigation
  - 02:03 UTC: Chrony reach recovered (0 → 4 → 210 → continuing)

#### System Architecture Validation

- **Confirmed**: VTEC is properly optional with graceful fallback (IRI-2020 → empirical)
- **Confirmed**: HDF5 is the primary data format (CSV is legacy)
- **Confirmed**: Core Recorder writes to `.bin.zst` compressed binary (not Digital RF)
- **Confirmed**: Critical path is well-defined: Recorder → Analytics → Fusion → Chrony SHM
- **Confirmed**: Systemd watchdog already implemented in fusion service code

### Documentation

#### Analysis Documents

- **Critical Path Analysis**: Comprehensive analysis of metrology-critical vs. science-optional components
- **Chrony Reach Investigation**: Root cause analysis and resolution timeline
- **Session Summary**: Overview of investigation, findings, and recommendations
- **Walkthrough**: Detailed deployment instructions and monitoring commands

### Deployment

**Installation**:

```bash
cd /home/mjh/git/hf-timestd
sudo ./scripts/deploy-service-improvements.sh
```

**Verification**:

```bash
# Check fusion service status
systemctl status timestd-fusion

# Monitor Chrony reach (should increase toward 377)
watch -n 10 'chronyc sources -v | grep TMGR'

# View monitoring timer status
systemctl status timestd-chrony-monitor.timer

# View monitoring logs
journalctl -u timestd-chrony-monitor -n 20
```

### Technical Details

**Chrony Reach Values**:

- 377 (octal) = 11111111 (binary) = 8/8 successful polls (optimal)
- 210 (octal) = 10001000 (binary) = 5/8 successful polls (acceptable)
- 0 (octal) = 00000000 (binary) = 0/8 successful polls (critical)

**Service Stability Improvements**:

- Watchdog timeout: 30 seconds
- Monitoring interval: 5 minutes
- Alert threshold: 64 decimal (25% success rate)
- Automatic restart on watchdog timeout

### Known Issues

- Fusion service crash-loop at 00:20 UTC (5 failures) - cause unknown, requires investigation
- Service exited immediately with status=1 but no Python errors logged
- Monitoring will help detect future occurrences

### Next Steps

1. Monitor fusion service for 24 hours to ensure stability
2. Investigate crash-loop logs from 00:20 UTC to identify root cause
3. Add email alerting to monitoring timer
4. Consider implementing Chrony reach alerting webhook

## [Unreleased] - 2025-12-31

### Added

- **Ionosphere Science Dashboard**: New `ionosphere-science.html` page for visualizing advanced propagation metrics.
- **Science API Endpoints**:
  - `/api/v2/ionosphere/wwv-wwvh-discrimination`: Station dominance visualization.
  - `/api/v2/ionosphere/propagation-residuals`: Measured delay vs IRI-2020 prediction.
  - `/api/v2/ionosphere/inferred-heights`: Layer height estimation.
- **HDF5 Reader Utilities**: Enhanced `web-ui/utils/hdf5_reader.py` with SWMR race condition protection and L1B/L1A support.

## [3.9.0] - 2026-01-02

### Added - Adaptive Search Window System

- **Intelligent Window Narrowing**: Wired `TimingCalibrator` into tone detection for Bootstrap → Orient → Focus progression
  - Bootstrap phase (±500ms): Wide search, no prior knowledge
  - Provisional phase (±5-15ms): Medium window after 10+ detections
  - Calibrated phase (±2-5ms): Narrow window after 30+ detections, 60min span
  - Per-broadcast independent tracking (WWV@10MHz ≠ WWV@5MHz)
- **Graceful Back-Off**: Automatic window widening when detections fail
  - Detects lost lock after 5+ consecutive failures
  - Widens search window to re-acquire signal
  - Re-converges when signal returns
- **Expected ToA Prediction**: Uses learned arrival times for narrow search
  - `get_expected_toa()` method returns mean ToA per station+frequency
  - Enables sub-2ms search windows after convergence
  - Leverages GPSDO "steel ruler" for rapid convergence (10-30 minutes)
- **Detection/Failure Tracking**: Records success/failure for adaptive behavior
  - `record_detection()` resets failure counter on successful tone detection
  - `record_failure()` increments counter when no tones found
  - `should_back_off()` triggers window widening after threshold exceeded

## [3.8.2] - 2026-01-02

### Added - Self-Healing Calibration Recovery

- **Calibration Sanity Checks**: Added validation to prevent loading corrupted or stale calibration files:
  - Rejects calibrations with offsets exceeding ±100ms (prevents "calibration trap")
  - Rejects calibrations older than 7 days (prevents stale ionospheric assumptions)
  - Falls back to bootstrap mode on validation failure
- **Relaxed Continuity Checks**: Temporarily increased `D_clock` jump threshold from 2ms to 2000ms to allow system to "snap" back to UTC after a large calibration reset or service failure.
- **Improved Physical Constraints**: Relaxed `D_clock` absolute bounds from ±50ms to ±500ms during calibrated phase to prevent safety checks from blocking corrective data.
- **Diagnostic Logging**: Enhanced Fusion measurement ingestion logging to track cross-station agreement during recovery.

## [3.8.1] - 2026-01-02

### Fixed - Critical Calibration Semantic Bug

#### Fusion Feedback Loop Logic

- **Issue**: Analytics service was misinterpreting Fusion calibration offsets (which are *corrections* to apply) as *expected arrival times* for search window centering.
- **Impact**: caused Analytics to search for tones at the wrong temporal location (e.g. -offset instead of +offset), leading to missed detections or false positives.
- **Fix**: Removed Fusion feedback from the search window logic. The system now uses purely Physics-based priors (IRI-2020) for search window centering, which correctly predicts arrival times. Calibration offsets are applied only *after* detection to correct the measurement.
- **Result**: Valid physics-based search windows (±15ms) are now active, replacing the erroneous offsets.

## [3.9.0] - 2026-01-02

### Added - Adaptive Search Window System

- **Intelligent Window Narrowing**: Wired `TimingCalibrator` into tone detection
  - Bootstrap (±500ms) → Orient (±15ms) → Focus (±2ms) progression
  - Per-broadcast independent tracking (WWV@10MHz ≠ WWV@5MHz)
  - GPSDO stability monitoring
- **Graceful Back-Off**: Automatic window widening when detections fail
  - Detects lost lock (5+ consecutive failures)
  - Widens search window to re-acquire signal
  - Re-converges when signal returns
- **Expected ToA Prediction**: Uses learned arrival times for narrow search
  - Tracks mean ToA per station+frequency
  - Provides sub-2ms search windows after convergence
  - Enables high-sensitivity ionospheric measurements

## [3.8.0] - 2026-01-02

### Fixed - Critical Fusion "Critic" Fixes

#### Data Integrity & "God Mode" Bypass

- **VTEC Safety**: Added strict consistency checks before applying (and boosting confidence of) GNSS VTEC corrections. Corrections are now rejected if they do not improve agreement with the consensus median.
- **Global Solver Immunity**: Removed "God Mode" immunity for `GLOBAL_DIFF` measurements. The global solution is now subject to the same robust outlier rejection logic as physical measurements.

#### Data Consistency & Availability

- **HDF5 Utility Parity**: Harmonized HDF5 filter logic to accept Grade D measurements (`min_quality_grade='D'`), matching the utility of the CSV reader. This prevents data starvation when HDF5 is active.
- **Warmup Penalty**: Removed the artificial 3-hour uncertainty penalty on restart if valid calibration data is loaded from disk.

## [3.7.1] - 2026-01-01

### Added - Health Checks & Pipeline Verification

- **Health Checks**: Implemented `health-check-science.sh` and `health-check-vtec.sh` to monitor service output freshness.
- **Pipeline Verification**: Extended `verify_pipeline.sh` to include Phase 4 (Science Products) and GNSS VTEC checks.
- **Service Integration**: Added `ExecStartPost` health checks to systemd service definitions.

### Fixed

- **Installation Scripts**: Added missing `timestd-science-aggregator.service` to `install.sh` and `uninstall.sh`.
- **Service Recovery**: Restored and verified operation of `timestd-science-aggregator`.
- **Uninstall Safety**: Added explicit warnings and data preservation options to `uninstall.sh`.

## [3.7.0] - 2025-12-31

### Added - Ionosphere Science Dashboard & Data Robustness

#### Ionosphere Science Dashboard

- **New Frontend**: `ionosphere-science.html` providing advanced visualization of propagation metrics.
- **Features**:
  - **WWV vs WWVH Discrimination**: Visualizes station dominance on shared frequencies.
  - **Propagation Residuals**: Interactive plot of measured timing offsets vs IRI-2020 predictions.
  - **Inferred Layer Heights**: Physics-based proxy estimation of F2 virtual heights from timing residuals.
  - **Dynamic Frequency Selection**: Intelligent filtering of valid frequencies based on station selection (including correct CHU frequencies).

#### Data Robustness

- **HDF5 Reader Safety**: Implemented critical fixes in `utils/hdf5_reader.py` to handle SWMR race conditions and prevent `IndexError` crashes when optional datasets (SNR, Doppler) are missing or shorter than the main timeline.
- **CSV Fallback**: Implemented robust fallback mechanism in `monitoring_server.py` to read legacy CSV files for discrimination data when HDF5 files are delayed or missing.
- **Backend Stability**: Fixed `timezone` import errors preventing server startup.

### Known Issues

- **CHU 300 Baud Frame Slip**: Observed ~33ms timing jumps in CHU data, corresponding accurately to one 300-baud character duration, indicating a decoder synchronization issue.

## [3.3.0] - 2025-12-31

### Added - Phase 4: Tone Detection Selectivity & Sensitivity Improvements

#### Robust Noise Floor Estimation

- **Feature**: Implemented MAD-based (Median Absolute Deviation) noise floor estimation
- **Method**: `MultiStationToneDetector._estimate_robust_noise_floor()` in `tone_detector.py`
- **Improvement**: Uses samples OUTSIDE search region to avoid interference contamination
- **Statistics**: MAD is more robust to outliers than standard deviation (factor 1.4826 conversion)
- **Expected Impact**: 5-10% improvement in weak signal detection
- **Files**: `src/hf_timestd/core/tone_detector.py` (+75 lines)

#### Adaptive Search Windows

- **Feature**: Dynamic search window sizing based on SNR and convergence state
- **Method**: `MultiStationToneDetector._calculate_adaptive_search_window()` in `tone_detector.py`
- **Strategy**:
  - ACQUIRING: ±500ms (wide search, no prior knowledge)
  - LOCKED + High SNR (>20dB): ±5ms (100x narrower)
  - LOCKED + Good SNR (>15dB): ±15ms (33x narrower)
  - LOCKED + Medium SNR (>10dB): ±50ms (10x narrower)
- **Expected Impact**: 10-20% reduction in false positives, faster convergence
- **Files**: `src/hf_timestd/core/tone_detector.py` (+72 lines)

#### Ionospheric Propagation Prediction

- **Feature**: IRI-2020 model integration for search window centering
- **Method**: `Phase2TemporalEngine._predict_propagation_delay()` in `phase2_temporal_engine.py`
- **Physics**: Predicts F2 layer height (hmF2) and calculates 1-hop propagation delay
- **Geometry**: `path_length = 2 × sqrt(hmF2² + (distance/2)²)`
- **Stations**: WWV (~1500km), WWVH (~6000km), CHU (~1200km)
- **Expected Impact**: Search window centering within ±10ms, 15-25% reduction in false positives
- **Files**: `src/hf_timestd/core/phase2_temporal_engine.py` (+105 lines)

### Added - Testing Infrastructure

- **Unit Tests**: Comprehensive test suite in `tests/test_tone_detector_improvements.py`
  - TestRobustNoiseFloor: MAD calculation, outlier robustness, fallback behavior
  - TestAdaptiveSearchWindow: All SNR/state combinations, boundary conditions
  - TestIonosphericPrediction: All stations, day/night variation, uncertainty propagation
  - TestIntegration: Method presence verification

### Changed

- **Noise Floor Calculation**: Updated `_correlate_with_template()` to use robust MAD-based method
- **Detection Pipeline**: Enhanced with three-stage improvement (prediction → adaptive window → robust threshold)

### Technical Details

**Code Statistics**:

- Files Modified: 2
- Lines Added: +258 (production code)
- Methods Added: 3
- Backward Compatibility: ✅ All changes additive

**Combined Effect**:

- Initial acquisition: ±500ms window, standard noise floor
- After lock with high SNR: ±5ms window centered at predicted delay, robust noise floor
- **Total improvement**: Up to 100x reduction in search space with better sensitivity

### References

- Rousseeuw, P.J. & Croux, C. (1993). "Alternatives to the Median Absolute Deviation." JASA.
- Kay, S.M. (1998). "Fundamentals of Statistical Signal Processing: Detection Theory."
- Davies, K. (1990). "Ionospheric Radio." Chapter 6: HF Propagation Prediction.
- Bilitza, D. et al. (2017). "International Reference Ionosphere 2016." Space Weather.

### Deployment

**Installation** (requires virtual environment):

```bash
cd /home/mjh/git/hf-timestd
python3 -m venv venv
source venv/bin/activate
pip install -e .
```

**Testing**:

```bash
pytest tests/test_tone_detector_improvements.py -v
```

**Production Deployment**:

```bash
sudo systemctl restart timestd-analytics
sudo journalctl -u timestd-analytics -f | grep -E "Robust noise floor|Adaptive window|Ionospheric prediction"
```

### Next Steps

- [ ] Process 24 hours of historical data to measure improvements
- [ ] Wire up convergence state from `clock_convergence.py` to detector
- [ ] Monitor production for detection rate, false positive rate, timing accuracy
- [ ] Validate expected improvements (≥20% FP reduction, ≥2ms timing improvement)

## [3.2.1] - 2025-12-30

### Fixed - Analytics Pipeline & HDF5 SWMR Integration

#### IRI-2020 Array Handling Incompatibility

- **Problem:** `iri2020` package updated return types from scalars to `xarray.DataArray`/NumPy arrays, causing `ValueError: only 0-dimensional arrays can be converted to Python scalars`
- **Impact:** IRI-2020 calculations failed, forcing fallback to geometric models with absurd D_clock values (-36 seconds)
- **Fix:** Added `_extract_scalar()` helper in `ionospheric_model.py` to normalize all IRI output types to floats
- **Files:** `src/hf_timestd/core/ionospheric_model.py`

#### Bootstrap Second Boundary Calculation Error

- **Problem:** Propagation solver calculated `expected_second_rtp` pointing to next minute boundary instead of current second
- **Impact:** D_clock errors of -36 seconds (pointing 36 seconds ahead)
- **Fix:** Modified bootstrap logic to round to nearest second boundary using RTP timestamp modulo
- **Files:** `src/hf_timestd/core/phase2_temporal_engine.py`

#### Missing HDF5 L1A Schema Field

- **Problem:** L1A channel observables missing required `processing_version` field
- **Impact:** HDF5 writes failing with schema validation error
- **Fix:** Added `'processing_version': '3.2.0'` to L1A measurement dictionary
- **Files:** `src/hf_timestd/core/phase2_analytics_service.py`

#### HDF5 SWMR Visibility Issue

- **Problem:** Analytics writing to HDF5 successfully but data not visible to SWMR readers
- **Impact:** Fusion reading 0 measurements despite analytics producing valid data
- **Fix:** Added explicit `refresh()` calls after `flush()` to update SWMR metadata for readers
- **Files:** `src/hf_timestd/io/hdf5_writer.py`

### Verified

- ✅ Analytics producing valid D_clock: -2ms to +45ms range
- ✅ IRI-2020 calculations working without fallback
- ✅ HDF5 L1A and L2 writes working with SWMR visibility
- ✅ Fusion reading 28 L2 measurements from HDF5
- ✅ Chrony SHM updating every 8 seconds
- ✅ Complete data pipeline operational end-to-end

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [3.6.2] - 2025-12-30

### Added - L0 Digital RF Storage

#### Digital RF HDF5 Archive

- **Feature**: Implemented L0 raw IQ data archival in Digital RF HDF5 format
- **Storage**: Data written to `{data_root}/drf/{channel}/` alongside existing hot buffer
- **Format**: Standardized Digital RF HDF5 with GZIP compression (level 1)
- **Compatibility**: HamSCI PSWS-compatible format for data sharing and long-term archival
- **Configuration**: Controlled by `save_digital_rf` toggle in `timestd-config.toml` (default: true)
- **Storage Impact**: ~142 GB/day for 9 channels, managed by existing QuotaManager (priority 4)

#### Architecture

- **Hot Buffer** (`/dev/shm/timestd/raw_buffer/`): RAM-based, 16-minute retention for real-time analytics
- **Digital RF** (`/var/lib/timestd/drf/`): Disk-based HDF5 for long-term archival and reprocessing
- **Legacy Removed**: Deprecated binary cold storage (`/var/lib/timestd/raw_buffer/`) removed, saving 31 GB

### Fixed

#### WWVHDiscriminator Import Error

- **Issue**: `NameError: name 'WWVHDiscriminator' is not defined` in `phase2_temporal_engine.py`
- **Fix**: Added missing `from .wwvh_discrimination import WWVHDiscriminator` import
- **Impact**: Service startup failure resolved

#### Digital RF Timestamp Alignment

- **Issue**: Timestamp errors ("Trying to write at sample X, but next available sample is Y") caused by system time jitter
- **Root Cause**: Using `system_time * sample_rate` for sample indexing introduced 7ms gaps from NTP corrections and scheduling delays
- **Fix**: Implemented continuous sample indexing using RTP timestamp as initial anchor, then tracking `last_index + 1` for monotonic writes
- **Result**: Zero timestamp errors, perfect alignment with GPSDO-disciplined RTP "ruler"
- **Technical**: RTP provides GPSDO-disciplined starting point, continuous indexing ensures Digital RF library requirements are met

## [3.6.1] - 2025-12-30

### Fixed - Fusion Service Stabilization & Chrony Integration

#### HDF5 Concurrency (SWMR) Fix

- **Issue**: Fusion service crashed with `OSError: Unable to synchronously open file` due to HDF5 file locking conflicts between the writer (Analytics service) and reader (Fusion service).
- **Fix**: Disabled HDF5 file locking in `multi_broadcast_fusion.py` (`HDF5_USE_FILE_LOCKING=FALSE`). This allows the reader to safely access files in SWMR mode concurrently.
- **Result**: Fusion service now robustly reads L2 measurements while Analytics service writes new data.

#### Chrony SHM Feed Repairs

- **Protocol Repair**: Fixed `nsamples` field in SHM struct (was 0, now 1). Chrony rejects updates with `nsamples=0`.
- **Mode Change**: Switched to SHM Mode 0 (no count locking) for simpler, more robust integration.
- **Timestamp Logic**: Verified and corrected timestamp packing convention (reference time vs system time).
- **Diagnostics**: Added detailed "breadcrumb" logging to trace fusion loop execution and SHM write attempts.

### Added - Carrier-Aided Timing

#### True RF Carrier Phase Tracking

- **Feature**: Implemented "Carrier-Aided Timing" for Safe Bands (WWV 20/25 MHz, CHU).
- **Measurement**: System now extracts `carrier_phase` from raw IQ samples instead of the AM envelope on exclusive frequencies.
- **Precision Improvement**: `carrier_doppler_hz` now tracks RF cycles (~10-30m wavelength) providing ~100x higher precision than audio Doppler (~300km wavelength).
- **Safety**: Feature is automatically restricted to non-shared bands to avoid carrier beat interference from multi-station overlaps (2.5, 5, 10, 15 MHz continue to use Audio Doppler).

### Added

- **Diagnostic Scripts**:
  - `scripts/verify_chrony_shm.py`: Tool to inspect SHM segment contents, validate fields, and monitor updates in real-time.

## [3.6.0] - 2025-12-29

### Added - L3 Fusion HDF5 Storage

#### Data Pipeline Migration Complete

- **L3 Fusion HDF5 Schema** (`l3_fusion_timing_v1.json`): Enhanced from 9 to 35 fields
  - Uncertainty budget components: `statistical_uncertainty_ms`, `systematic_uncertainty_ms`, `propagation_uncertainty_ms`
  - Per-station breakdowns: mean D_clock, counts, and intra-station std devs for WWV, WWVH, CHU, BPM
  - Consistency metrics: `inter_station_spread_ms`, `consistency_flag` (OK, INTRA_ANOMALY, INTER_ANOMALY, DISCRIMINATION_SUSPECT)
  - Global solve verification: `global_solve_verified`, `global_solve_consistency_ms`, `global_solve_n_obs`
  - Calibration metadata: `calibration_applied`, `reference_station`, `outliers_rejected`
  - Quality metadata: `quality_grade` (A/B/C/D), enhanced `quality_flag`

- **HDF5 Writer Implementation** (`multi_broadcast_fusion.py`):
  - Parallel CSV+HDF5 writes with schema validation
  - SWMR mode for concurrent read access
  - Graceful fallback to CSV-only if HDF5 unavailable
  - Error handling with non-fatal logging

#### Production Deployment

- **HDF5 Files Created**: `/var/lib/timestd/phase2/fusion/fusion_fusion_timing_YYYYMMDD.h5`
- **Service Integration**: Fusion service successfully writing to HDF5
- **Backward Compatibility**: CSV writes continue unchanged

### Changed

- **Fusion Service**: Added `DataProductWriter` initialization and `_write_fused_result_hdf5()` method
- **CSV Writer**: Updated to call HDF5 writer in parallel

### Data Pipeline Status

All data products now use HDF5:

- ✅ L0 (Raw): Digital RF HDF5
- ✅ L1A (Observables): Channel observables HDF5
- ✅ L1B (Timecode): BCD timecode HDF5
- ✅ L2 (Timing): Timing measurements HDF5
- ✅ **L3 (Fusion): Fusion results HDF5** ← NEW
- ✅ L3 (Ionosphere): GNSS VTEC HDF5

**Migration Complete**: All data products in the hf-timestd pipeline now use HDF5 storage with schema validation and metrological provenance.

## [3.5.0] - 2025-12-29

### Added - Enhanced Timing Performance Metrics

#### Uncertainty Budget (Root Sum of Squares)

- **Three-Component Uncertainty Model**: Proper uncertainty budgeting with statistical, systematic, and propagation components
  - Statistical: Measurement scatter from weighted standard deviation
  - Systematic: Calibration convergence error (decreases over time)
  - Propagation: Mode-dependent ionospheric variability (GW: 0.1ms, 1F: 0.5ms, 2F: 2.0ms, TEC-solved: 0.2ms)
- **RSS Combination**: `σ_total = sqrt(σ_stat² + σ_sys² + σ_prop²)`
- **FusedResult Enhancement**: Added `statistical_uncertainty_ms`, `systematic_uncertainty_ms`, `propagation_uncertainty_ms` fields
- **CSV Output**: Updated fusion CSV to include uncertainty budget components

#### Real-Time Performance Metrics

- **API Enhancement**: `/api/v2/system/health-summary` now includes performance metrics
  - RMS Accuracy: `sqrt(mean(d_clock²))` vs UTC(NIST)
  - Peak-to-Peak: Excursion range over last hour
  - Mean Offset: Average clock offset
  - Standard Deviation: Short-term stability
- **Web UI Display**: Metrology dashboard shows real-time performance indicators

#### Live Allan Deviation Tracking

- **AllanDeviationTracker Class**: Efficient overlapping ADEV calculator with 24h rolling window
  - Maintains 86400 samples (1 per minute for 24 hours)
  - Overlapping calculation for better statistics
  - Standard tau values: 10s, 100s, 1000s, 10000s
- **Fusion Integration**: ADEV tracking added to fusion service
  - Measurements tracked after each fusion cycle
  - `get_current_adev()` method for API exposure
- **API Exposure**: ADEV values included in health summary response
- **Web UI Display**: Scientific notation formatting (e.g., 1.2×10⁻⁶)

#### Metrology Dashboard Enhancements

- **Uncertainty Budget Section**: Visual breakdown of uncertainty components
- **Performance Metrics Section**: Last hour RMS and peak-peak display
- **Allan Deviation Section**: Live ADEV at 4 tau values with labels
- **Scientific Notation**: Proper formatting for small values

### Changed

- **Fusion CSV Format**: Added 3 new columns for uncertainty budget components
- **API Response**: Enhanced `/api/v2/system/health-summary` with performance and ADEV data
- **Metrology Dashboard**: Expanded hero status with 4 new sections

## [3.4.0] - 2025-12-29

### Added - L0 Digital RF Storage

#### Phase 3: Raw IQ Archival

- **Digital RF Writer** (`src/hf_timestd/io/digital_rf_writer.py`): New class for writing continuous complex IQ data in the Digital RF HDF5 format.
- **Pipeline Integration**: `PipelineOrchestrator` now supports parallel L0 recording alongside Phase 1 binary archives.
- **Configuration**: Added `save_digital_rf` toggle in `timestd-config.toml` (default: true).
- **Storage Management**: L0 data is stored in `{data_root}/drf/` and managed by the existing `QuotaManager` (priority 4, cleaned up first).
- **Compression**: GZIP compression enabled for storage efficiency (~1.5GB/day/channel).

## [3.3.0] - 2025-12-29

### Added - HDF5 Migration Complete

#### Phase 1: Full HDF5 Coverage

- **GNSS VTEC HDF5 Schema** (`l3_gnss_vtec_v1.json`): New schema for real-time GNSS VTEC data with quality flags (GOOD/MARGINAL/BAD)
- **VTEC HDF5 Writes**: `live_vtec.py` now writes parallel CSV+HDF5 with schema validation
- **VTEC HDF5 Reads**: Fusion service reads VTEC from HDF5 with CSV fallback
- **Science Aggregator HDF5**: Reads L2 timing from HDF5, writes TEC to HDF5 with CSV fallbacks
- **Test Suite**: Automated equivalence tests for VTEC and L2 data validation

#### Data Quality & Validation

- Quality flag automatic determination for VTEC (based on satellite count)
- Quality flag automatic determination for TEC (based on confidence and residuals)
- Schema validation for all data products
- Metrological provenance metadata (ISO GUM compliant)

#### Phase 2: Data Equivalence Validation

- VTEC equivalence: 98.7% match rate, 0.013 TECU mean difference
- L2 timing: Record counts match within 1%
- 24-hour production monitoring: Zero HDF5 errors
- Performance validated: HDF5 meets operational requirements

### Changed

#### VTEC Service (`scripts/live_vtec.py`)

- Added `DataProductWriter` for HDF5 output
- Parallel CSV+HDF5 writes with configurable paths
- Enhanced error logging for HDF5 operations
- Proper resource cleanup (`hdf5_writer.close()`)

#### Fusion Service (`src/hf_timestd/core/multi_broadcast_fusion.py`)

- Updated `_read_gnss_vtec()` to HDF5-first with CSV fallback
- Enhanced logging to show data source (HDF5 vs CSV)
- Time-range queries for last 5 minutes of VTEC data
- Quality filtering (accept GOOD and MARGINAL)

#### Science Aggregator (`src/hf_timestd/core/science_aggregator.py`)

- Updated `_read_clock_offset_csv()` to read from HDF5 with CSV fallback
- Updated `_write_tec_results()` to write HDF5 with CSV fallback
- Automatic quality flag determination for TEC estimates
- Enhanced error handling and logging

#### Configuration (`config/timestd-config.toml`)

- Added `save_hdf5 = true` to `[gnss_vtec]` section
- Added `hdf5_path = "data/gnss_vtec"` for VTEC HDF5 output

#### Schema Registry (`src/hf_timestd/schemas/registry.json`)

- Added `L3A_gnss_vtec` entry for GNSS VTEC data product
- Marked `gnss_vtec.csv` as replaced by HDF5

### Fixed

- VTEC data now has proper schema validation (prevents bad data)
- TEC estimates now have quality metadata
- Concurrent access to HDF5 files (SWMR mode enabled)

### Validated

- ✅ 126 HDF5 files in production (L1A, L1B, L2 data products)
- ✅ VTEC: 3,931 measurements validated, 98.7% match rate
- ✅ L2 timing: 783 measurements per channel (SHARED_10000)
- ✅ Zero HDF5 errors in 24-hour production monitoring
- ✅ Timing accuracy maintained (Grade A, ±0.2-0.3ms)

### Migration Status

- **Phase 1 (HDF5 Coverage)**: ✅ Complete
- **Phase 2 (Data Equivalence)**: ✅ Complete - Production Ready
- **Phase 3 (Remove CSV Fallbacks)**: ⏳ Pending (monitoring period)
- **Phase 4 (Cleanup)**: ⏳ Pending

### Technical Details

#### HDF5 Data Products

- L1A: Channel observables (carrier power, SNR, Doppler, tones)
- L1A: Tone detections (station ID timing)
- L1B: BCD timecode (discrimination results)
- L2: Timing measurements (clock offset with uncertainty)
- L3A: GNSS VTEC (ionospheric corrections)
- L3A: TEC estimates (multi-frequency propagation)

#### Data Quality Metrics

- VTEC mean difference: 0.013 TECU (excellent)
- VTEC std deviation: 0.110 TECU
- L2 record completeness: >99%
- No systematic bias detected
- No data corruption detected

### Deployment Notes

**Production Deployment**:

1. Install package: `sudo /opt/hf-timestd/venv/bin/pip install -e .`
2. Copy schemas: `sudo cp src/hf_timestd/schemas/*.json /opt/hf-timestd/src/hf_timestd/schemas/`
3. Copy scripts: `sudo cp scripts/live_vtec.py /opt/hf-timestd/scripts/`
4. Update config: Add HDF5 settings to `/etc/hf-timestd/timestd-config.toml`
5. Restart services: `sudo systemctl restart timestd-vtec timestd-fusion`

**Monitoring**:

- Check HDF5 files: `find /var/lib/timestd -name "*.h5" -mtime -1`
- Check for errors: `sudo journalctl -u timestd-vtec --since "1 hour ago" | grep -i error`
- Verify data: Run `tests/test_vtec_equivalence.py`

### Breaking Changes

None - CSV fallbacks maintained for backward compatibility

### Deprecation Notice

CSV-only data access will be deprecated in version 4.0.0 after Phase 3 completion.

---

## [3.2.0] - Previous Release

(Previous changelog entries...)
