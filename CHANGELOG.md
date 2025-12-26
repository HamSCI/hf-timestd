# Changelog

All notable changes to this project will be documented in this file.

## [3.1.0] - 2025-12-26

### Added

- **Fusion Service**: New `timestd-fusion.service` (Phase 3) for dedicated multi-broadcast fusion and Chrony SHM feeding.
- **Service Control Script**: New `scripts/timestd-fusion.sh` for managing the fusion service.
- **Two-Tier Calibration**: Implemented `PROVISIONAL` (10 min) and `CALIBRATED` (60 min) calibration phases.
- **Documentation**: Updated `ARCHITECTURE.md`, `TECHNICAL_REFERENCE.md`, `README.md`, and `INSTALLATION.md` to reflect the 3-phase architecture.

### Changed

- **Installation**: Updated `scripts/install.sh` to install and enable `timestd-fusion.service` automatically in production mode.
- **CLI Scripts**: Updated `scripts/timestd-all.sh` and `scripts/timestd-analytics.sh` to integrate the new fusion control script and separate concerns.
- **Calibration Logic**: Enhanced `timing_calibrator.py` to support two-tier calibration with GPSDO stability checks.

### Fixed

- **HDF5 Pipeline**: Resolved NoneType format errors and restored data flow for L1A/L1B/L2 products.
- **Orphaned Process**: Fixed `multi-broadcast-fusion` running as an unmanaged process.

## [3.0.0] - 2025-12-25

### Added

- Initial release of Phase 2 Analytics pipeline.
