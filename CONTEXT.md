# HF-TimeStd Development Context

**Last Updated**: 2025-12-27
**Current Phase**: Analytics Validation & Scientific Capabilities
**Next Session Focus**: Automate CDDIS DCB acquisition for TEC correction

---

## Recent Session Summary (2025-12-27)

### Analytics & TEC Integration - Completed (v3.2.0)

Successfully validated and enhanced the analytics pipeline to support ionospheric science.

**Achievements**:

1. **HDF5 L1A Enhancements**:
    * Modified `phase2_analytics_service.py` to populate previously empty fields in HDF5 L1A files.
    * **Now Capturing**: Doppler shift (`carrier_doppler_hz`), Doppler spread (`doppler_std_hz`), Phase variance (`phase_variance_rad`), Coherence time (`coherence_time_sec`).
    * Verified 100% field population in production.

2. **TEC Estimator Integration**:
    * Modified `multi_broadcast_fusion.py` to persist internal TEC calculations.
    * **New Data Product**: `phase2/fusion/tec_estimates.csv` (written in real-time).
    * Captures relative TEC, confidence, and vacuum timing errors.

3. **Production Release v3.2.0**:
    * Cleaned up production environment (`/opt/hf-timestd`).
    * Upgraded package from git repository.
    * Full documentation and changelog updated.

**Status**: ✅ System is live and generating enhanced scientific data products.

---

## Next Session Objective

### Automate CDDIS DCB Download for TEC Correction

**Goal**: Implement an automated workflow to download Differential Code Bias (DCB) data (and potentially IONEX maps) from NASA's CDDIS (Crustal Dynamics Data Information System) to calibrate absolute TEC estimates.

**Context**:
The current `tec_estimates.csv` provides *relative* TEC derived from multi-frequency HF measurements. To convert these to accurate *absolute* TEC values (or to validate them), we need to correct for instrumental biases (DCBs). CDDIS provides this data via GNSS products.

**Tasks**:

1. **Design Download Mechanism**:
    * Create a script (e.g., `scripts/fetch_cddis_dcb.py`) to access NASA CDDIS.
    * Handle authentication (likely Earthdata Login/.netrc).
    * Identify correct data products (IONEX files, DCB specific files).

2. **Integrate Calibration**:
    * Parse the downloaded DCB/IONEX data.
    * Apply bias corrections to the `TECResult` in `multi_broadcast_fusion.py` or a post-processing step.
    * Comparison: Validate HF-derived TEC against GNSS-derived VTEC maps from CDDIS.

3. **Automation**:
    * Schedule the download (systemd timer or cron).
    * Ensure robust error handling for network/availability issues.

**Key Documents/Files**:
* `src/hf_timestd/core/multi_broadcast_fusion.py`: Where TEC is estimated.
* `src/hf_timestd/core/tec_estimator.py`: The estimation algorithm.
* `NASA CDDIS`: <https://cddis.nasa.gov/> (External reference).

---

## Current System Status

### Production Environment

* **Location**: `/opt/hf-timestd/`
* **Version**: v3.2.0
* **Services**:
  * `timestd-core-recorder`: ✅ Active (9 channels)
  * `timestd-analytics`: ✅ Active (Writing enhanced HDF5 L1A)
  * `timestd-fusion`: ✅ Active (Writing `tec_estimates.csv`)

### Data Products

* **HDF5**: `l1_channel_observables` now contains Doppler/Phase/Coherence data.
* **CSV**: `/var/lib/timestd/phase2/fusion/tec_estimates.csv` being populated.
* **Fusion**: `fused_d_clock.csv` active.

---

## Agent Preparation Notes

**For the Next Agent**:
* **Authentication**: You will likely need to help the user set up Earthdata Login credentials if they haven't already. Check for `~/.netrc`.
* **Libraries**: You may need `cdflib` or `netCDF4` if CDDIS uses those formats, or standard Hatanaka/RINEX parsers. IONEX is text-based.
* **Focus**: The primary goal is *getting the data* to enable calibration. Don't over-engineer the physics before we have the reference data.
* **Existing Code**: Look at `tec_estimator.py` to see how TEC is currently calculated (slope of group delay vs frequency).

**Tools Available**:
* `requests` library (standard).
* `run_command` to test connectivity.
* `write_to_file` to create the downloader script.
