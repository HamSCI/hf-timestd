# CLAUDE.md

## Project Overview

HF Time Standard Analysis (`hf-timestd`) — a Python system that receives HF time standard broadcasts (WWV, WWVH, CHU, BPM) via ka9q-radio RTP streams, produces sub-millisecond UTC timing measurements for Chrony clock discipline, and generates ionospheric science products (dTEC, TEC, propagation mode identification).

**Version:** 6.12.0 | **License:** MIT | **Python:** >=3.10

## Quick Reference

```bash
# Development setup
pip install -e ".[dev,gnss,iono]"
pytest tests/

# First-run install (apt deps, user, dirs, venv)
sudo ./scripts/install.sh

# Ongoing deploy after editing source (Pattern A: editable install)
sudo ./scripts/deploy.sh           # refuses on dirty tree
sudo ./scripts/deploy.sh --pull    # git pull then deploy

# CLI
hf-timestd version --json
hf-timestd profile show              # active profile + services
hf-timestd service status            # per-service config + systemd state
sudo hf-timestd profile set fusion   # switch operational profile
```

## Project Structure

```
src/hf_timestd/          # Main package
  core/                  # Signal processing, timing, physics (~80 modules)
  stream/                # ka9q-radio RTP stream API
  interfaces/            # Data contracts
  io/                    # HDF5/Binary archive I/O
  grape/                 # GRAPE daily processing + PSWS upload
  models/                # Pydantic data models
  cli.py                 # CLI entry point
  paths.py               # Path management (production/test)
  config_utils.py        # TOML config parsing
web-api/                 # FastAPI dashboard (port 8000)
tests/                   # Unit/integration tests
config/                  # Config templates (TOML, chrony, systemd env)
systemd/                 # 8 systemd service files
scripts/                 # Utility/deployment scripts
docs/                    # Technical docs, QEX paper draft
```

## Key Conventions

- **One class per file**, filename matches class (e.g., `tick_edge_detector.py` -> `TickEdgeDetector`)
- **Type hints** throughout; Pydantic for data models
- **Naming:** `PascalCase` classes, `snake_case` functions, `UPPER_SNAKE_CASE` constants, `_leading_underscore` private methods
- **Scientific rigor:** uncertainties tracked alongside measurements (Cramer-Rao bounds, std devs)
- **NumPy/SciPy** for DSP; `complex64` IQ data; HDF5 SWMR for inter-process I/O
- **Formatter:** black | **Linter:** flake8 | **Types:** mypy

## Architecture Notes

- **Pipeline:** Recording (RTP -> binary IQ) -> Metrology (IQ -> HDF5 L1/L2) -> Fusion (Kalman + WLS -> Chrony SHM)
- **Two modes:** RTP (GPSDO ground truth, testing) and FUSION (GPS-denied, production)
- **Service profiles** (archive/rtp/fusion/full) control which of 8+ systemd services run
- **HDF5 SWMR:** writers keep files open + flush; readers use `swmr=True`
- **Raw IQ storage:** Configurable chunk duration (`file_duration_sec`, default 600s = 10 min). Compressed `.bin.zst` + JSON sidecar per chunk. GRAPE raw reader handles both legacy 1-min and multi-minute chunks transparently.
- **GRAPE spectrogram:** Edge tapering at gap boundaries (half-cosine, 5s); full-window validity masking (NFFT=512 → ±25.6s). No zero interpolation.
- **Config:** TOML-based (`config/timestd-config.toml.template`); production at `/etc/hf-timestd/`

## Dependencies of Note

- `h5py>=3.8.0,<3.16.0` — h5py 3.16 bundles HDF5 2.0.0 which breaks SWMR in long-running processes
- `ka9q-python>=3.3` — RTP stream interface to ka9q-radio
- `pylap` (optional) — PHaRLAP ray tracing for propagation mode identification
