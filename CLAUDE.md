# CLAUDE.md

## Project Overview

HF Time Standard Analysis (`hf-timestd`) — a Python system that receives HF time standard broadcasts (WWV, WWVH, CHU, BPM) via ka9q-radio RTP streams, produces sub-millisecond UTC timing measurements for Chrony clock discipline, and generates ionospheric science products (dTEC, TEC, propagation mode identification).

**Version:** 6.12.0 | **License:** MIT | **Python:** >=3.10

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

- **Pipeline:** Recording (RTP -> binary IQ) -> Metrology (IQ -> HDF5 L1/L2) -> Fusion (Kalman + WLS -> Chrony SHM)
- **Two modes:** RTP (GPSDO ground truth, testing) and FUSION (GPS-denied, production)
- **Service profiles** (archive/rtp/fusion/full) control which of the core services run
- **Logging:** every `timestd-*` unit logs to journald — no per-service log files. See `docs/DEBUGGING.md`.
- **HDF5 SWMR:** writers keep files open + flush; readers use `swmr=True`
- **Raw IQ storage:** Configurable chunk duration (`file_duration_sec`, default 600s = 10 min). Compressed `.bin.zst` + JSON sidecar per chunk. GRAPE raw reader handles both legacy 1-min and multi-minute chunks transparently.
- **GRAPE spectrogram:** Edge tapering at gap boundaries (half-cosine, 5s); full-window validity masking (NFFT=512 → ±25.6s). No zero interpolation.
- **Config:** TOML-based (`config/timestd-config.toml.template`); production at `/etc/hf-timestd/`

## Dependencies of Note

- `h5py>=3.8.0,<3.16.0` — h5py 3.16 bundles HDF5 2.0.0 which breaks SWMR in long-running processes
- `ka9q-python>=3.3` — RTP stream interface to ka9q-radio
- `pylap` (optional) — PHaRLAP ray tracing for propagation mode identification

## General Workflow Orchestration in Any Project

### 1. Plan Mode Default
- Enter plan mode for ANY non-trivial task (3+ steps or architectural decisions)
- If something goes sideways, STOP and re-plan immediately — don't keep pushing
- Use plan mode for verification steps, not just building
- Write detailed specs upfront to reduce ambiguity

### 2. Subagent Strategy
- Use subagents liberally to keep main context window clean
- Offload research, exploration, and parallel analysis to subagents
- For complex problems, throw more compute at it via subagents
- One task per subagent for focused execution

### 3. Self-Improvement Loop
- After ANY correction from the user: update `tasks/lessons.md` with the pattern
- Write rules for yourself that prevent the same mistake
- Ruthlessly iterate on these lessons until mistake rate drops
- Review lessons at session start for relevant project

### 4. Verification Before Done
- Never mark a task complete without proving it works
- Diff behavior between main and your changes when relevant
- Ask yourself: "Would a staff engineer approve this?"
- Run tests, check logs, demonstrate correctness

### 5. Demand Elegance (Balanced)
- For non-trivial changes: pause and ask "is there a more elegant way?"
- If a fix feels hacky: "Knowing everything I know now, implement the elegant solution"
- Skip this for simple, obvious fixes — don't over-engineer
- Challenge your own work before presenting it

### 6. Autonomous Bug Fixing
- When given a bug report: just fix it. Don't ask for hand-holding
- Point at logs, errors, failing tests — then resolve them
- Zero context switching required from the user
- Go fix failing CI tests without being told how

## Task Management

1. **Plan First**: Write plan to `tasks/todo.md` with checkable items
2. **Verify Plan**: Check in before starting implementation
3. **Track Progress**: Mark items complete as you go
4. **Explain Changes**: High-level summary at each step
5. **Document Results**: Add review section to `tasks/todo.md`
6. **Capture Lessons**: Update `tasks/lessons.md` after corrections

## Core Principles

- **Simplicity First**: Make every change as simple as possible. Impact minimal code.
- **No Laziness**: Find root causes. No temporary fixes. Senior developer standards.
- **Minimal Impact**: Changes should only touch what's necessary. Avoid introducing bugs.

1. Don’t assume. Don’t hide confusion. Surface tradeoffs.

2. Minimum code that solves the problem. Nothing speculative.

3. Touch only what you must. Clean up only your own mess.

4. Define success criteria. Loop until verified.
