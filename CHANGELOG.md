# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
