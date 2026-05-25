# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

HF Time Standard Analysis (`hf-timestd`) — a Python system that receives HF time standard broadcasts (WWV, WWVH, CHU, BPM) via ka9q-radio RTP streams, produces sub-millisecond UTC timing measurements for Chrony clock discipline, and generates ionospheric science products (dTEC, TEC, propagation mode identification).

**Version:** 7.0.0 (canonical: `pyproject.toml`) | **License:** MIT | **Python:** >=3.10

## Quick Reference

```bash
# Development setup (uv is the standard; see README for pip fallback)
uv sync --extra dev --extra gnss --extra iono
uv run pytest tests/

# First-run install (apt deps, user, dirs, venv)
sudo ./scripts/install.sh

# Ongoing deploy after editing source (Pattern A: editable install)
sudo ./scripts/deploy.sh           # refuses on dirty tree
sudo ./scripts/deploy.sh --pull    # git pull then deploy

# CLI surface (selection — there are ~15 subcommands; see `hf-timestd --help`)
hf-timestd version --json
hf-timestd inventory --json          # sigmond client-contract resource view
hf-timestd validate --json           # config validation
hf-timestd status                    # health check
hf-timestd quality                   # timing quality report
hf-timestd profile show|list        # operational profile (archive/rtp/fusion/full)
sudo hf-timestd profile set fusion   # switch profile (restarts services)
hf-timestd service status            # per-service config + systemd state
hf-timestd daemon                    # recorder daemon
hf-timestd data summary              # storage usage
hf-timestd data clean-{data,analytics,uploads,all}
hf-timestd grape daily               # full GRAPE pipeline (decimate → spec → package → upload)
hf-timestd grape {decimate,spectrogram,package,upload,test-upload,status}
hf-timestd calibrate                 # BPSK-PPS calibration utilities
hf-timestd discover                  # available radiod channels
hf-timestd create-channels           # provision channels in radiod
```

### Tests

```bash
uv run pytest tests/                          # full suite
uv run pytest tests/test_<area>.py -v         # one file
uv run pytest tests/test_<area>.py::TestClass::test_X  # one test
uv run pytest -k authority -v                 # by keyword
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
systemd/                 # ~25 unit files (8-service core pipeline + timers/housekeeping)
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

- **Timing-authority invariant (read this first):** RTP timestamps from radiod are the only authoritative timing substrate; the host wall clock is a *derived* product and must never be used as a source.  Whether radiod's clock has GPS+PPS authority (RTP mode) or not (Fusion mode), the chrony feed is built as `rtp_time + rtp_to_utc_offset_ns`, where the offset comes from a peer authority (T5) or a fusion-derived measurement (T3), **never** from `chronyc tracking` on the host (T4 is bootstrap-only).  Fusion runs always-on — even in RTP mode — to provide authority backup if GPS+PPS fails and to study HF-fusion quality against the higher reference.  See `docs/METROLOGY.md` §4.5–§4.6 for the full hierarchy and the reasoning.  Any change that introduces a new use of `time.time()`, `datetime.now()`, or `chronyc tracking` in the timing path violates this invariant and needs to be reviewed against the doc before merging.
- **Pipeline:** Recording (RTP -> binary IQ) -> Metrology (IQ -> HDF5 L1/L2) -> Fusion (Kalman + WLS -> Chrony SHM)
- **Two modes:** RTP (GPSDO ground truth, testing) and FUSION (GPS-denied, production) — *which authority controls the chrony feed*, not *whether fusion runs*
- **Service profiles** (archive/rtp/fusion/full) control which of the core services run
- **Logging:** every `timestd-*` unit logs to journald — no per-service log files. See `docs/DEBUGGING.md`.
- **HDF5 SWMR:** writers keep files open + flush; readers use `swmr=True`
- **Raw IQ storage:** Configurable chunk duration (`file_duration_sec`, default 600s = 10 min). Compressed `.bin.zst` + JSON sidecar per chunk. GRAPE raw reader handles both legacy 1-min and multi-minute chunks transparently.
- **GRAPE spectrogram:** Edge tapering at gap boundaries (half-cosine, 5s); full-window validity masking (NFFT=512 → ±25.6s). No zero interpolation.
- **Config:** TOML-based (`config/timestd-config.toml.template`); production at `/etc/hf-timestd/`

## Client contract: PROVIDER (not subscriber)

hf-timestd participates in the HamSCI client contract differently from
the recorders: it is the **timing-authority producer** that other
clients (psk-recorder, wspr-recorder, hfdl-recorder, mag-recorder…)
optionally subscribe to via §18.

- **§18 (timing authority, new in contract v0.7)** — hf-timestd
  publishes the authority snapshot fields (`utc_anchor_ns`, `tier`,
  `sigma_ns`, `snapshot_age_s`, plus the radiod-subscriber extras
  `rtp_anchor_sample`, `rate_samples_per_utc_sec`, `radiod_id`, and the
  non-radiod `host_monotonic_at_anchor`). The producer-side reference
  is `docs/ARCHITECTURE-FIRST-PRINCIPLES.md`; the contract document
  (`/opt/git/sigmond/sigmond/docs/CLIENT-CONTRACT.md`) names what
  subscribers may rely on without specifying the wire protocol.
- **Self-describe surfaces** — `inventory`/`validate`/`version --json`
  via `cli.py` (no separate `contract.py` module like the recorders;
  inventory is assembled inline). Reports
  `provides_timing_calibration = true` per the §3 amendment.
- Recent authority work is in the `authority_*` modules under
  `src/hf_timestd/core/` (see commit log: Phase 2A/2B `T5` substrate
  work).

## Dependencies of Note

- `h5py>=3.8.0,<3.16.0` — h5py 3.16 bundles HDF5 2.0.0 which breaks SWMR in long-running processes
- `ka9q-python>=3.3` — RTP stream interface to ka9q-radio
- `pylap` (optional) — PHaRLAP ray tracing for propagation mode identification

## Further reading

`docs/` is extensive (~50 files). The load-bearing ones:

- `docs/METROLOGY.md` — timing hierarchy §4.5–§4.6 (the canonical
  reference for the timing-authority invariant above).
- `docs/ARCHITECTURE-FIRST-PRINCIPLES.md` — producer-side reference
  for the §18 contract surface.
- `docs/ARCHITECTURE.md` — pipeline + service layering.
- `docs/DEBUGGING.md` — journald-only logging patterns.
- `docs/TIMING-PIPELINE-WIRING.md` — RTP / chrony / fusion wiring.
- `docs/PHASE_ENGINE_ARCHITECTURE.md` / `PHYSICS.md` — DSP internals.
- `docs/GRAPE_DAILY_PROCESSING.md` — daily PSWS upload pipeline.
