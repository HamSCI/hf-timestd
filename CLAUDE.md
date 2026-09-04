# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

HF Time Standard Analysis (`hf-timestd`) — a Python system that receives HF time standard broadcasts (WWV, WWVH, BPM) via ka9q-radio RTP streams, produces sub-millisecond UTC timing measurements for Chrony clock discipline, and generates ionospheric science products (dTEC, TEC, propagation mode identification).

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
hf-timestd calibrate                 # BPSK-PPS calibration utilities
hf-timestd discover                  # available radiod channels
hf-timestd create-channels           # provision channels in radiod
hf-timestd raytrace WWV 10.0         # PHaRLAP 2-D ray trace → propagation modes
```

## Key Conventions

- **One class per file**, filename matches class (e.g., `tick_edge_detector.py` -> `TickEdgeDetector`)
- **Type hints** throughout; Pydantic for data models
- **Naming:** `PascalCase` classes, `snake_case` functions, `UPPER_SNAKE_CASE` constants, `_leading_underscore` private methods
- **Scientific rigor:** uncertainties tracked alongside measurements (Cramer-Rao bounds, std devs)
- **NumPy/SciPy** for DSP; `complex64` IQ data; HDF5 SWMR for inter-process I/O
- **Formatter:** black | **Linter:** flake8 | **Types:** mypy

## Architecture Notes

- **Timing-authority invariant (read this first):** RTP timestamps from radiod are the only authoritative timing substrate; the host wall clock is a *derived* product and must never be used as a source.  Whether radiod's clock has GPS+PPS authority (RTP mode) or not (Fusion mode), the chrony feed is built as `rtp_time + rtp_to_utc_offset_ns`, where the offset comes from a peer authority (T5) or a fusion-derived measurement (T3), **never** from `chronyc tracking` on the host (T4 is bootstrap-only).  When the T6 anchor authority is AUTHORITATIVE (see `docs/design/T6_ANCHOR_INVERSION_DESIGN.md`), the TS-1 fine-stage edge defines the RTP→UTC anchor and the coarse cascade only names the integer second; `chain_delay` values are diagnostics, never corrections.  Fusion runs always-on — even in RTP mode — to provide authority backup if GPS+PPS fails and to study HF-fusion quality against the higher reference.  See `docs/METROLOGY.md` §4.5–§4.6 for the full hierarchy and the reasoning.  Any change that introduces a new use of `time.time()`, `datetime.now()`, or `chronyc tracking` in the timing path violates this invariant and needs to be reviewed against the doc before merging.  **One sanctioned exception:** the Offset Judge (`core/offset_judge.py`, contract: `docs/OFFSET-JUDGE-SPEC-2026-08-05.md` §2) deliberately uses chrony and the host wallclock as calibrated *measurement benches* — instruments for detecting radiod epoch error, with each bench's own σ carried honestly on every verdict.  That is a judging role, not a chrony-feed role: judge output never feeds chrony, so no circularity is introduced.  Tier adoption is cross-bench gated: a higher bench is adopted only when it agrees with the trusted lower tier within `cross_bench_k`·√(σ_c²+σ_l²) over the whole advance window AND does not materially regress precision (σ_candidate ≤ σ_incumbent·`sigma_regression_margin`; incumbent loss adopts regardless) (`docs/JUDGE-CROSS-BENCH-GATE-2026-08-05.md`) — precision claims never substitute for cross-validation, and tier rank never substitutes for demonstrated precision.  The invariant continues to protect the chrony-feed path unchanged.
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
- `iri2020` (git pin in `pyproject.toml`) — IRI-2020 via the space-physics
  package. **It compiles Fortran on first use, so it needs `gfortran`** (plus
  `build-essential`). Without a Fortran compiler the build fails and
  `IonosphericModel` silently falls back to its internal **parametric** tier
  (`tier=parametric`, not `iri`) — degraded ionosphere with no hard error.
  Resolution (2026-06-13): `gfortran` + `build-essential` are now declared apt
  prerequisites in `scripts/install.sh`, so any fresh install gets them; the
  same toolchain also covers the pyLAP build. On an existing host that predates
  this, `sudo apt install build-essential gfortran` then re-run the installer.
  Quick check: `hf-timestd data sources` (Raytrace line) / confirm IRI returns
  `tier=iri` rather than `parametric`.
- `pylap` (optional) — PHaRLAP ray tracing for propagation mode identification.
  PHaRLAP is licence-restricted (DST) and never bundled — operator-staged via
  `scripts/install-pharlap.sh`; pyLAP is built on install by
  `scripts/ensure-pylap.sh` (also needs `gfortran`). See
  `docs/EXTERNAL_PREREQUISITES.md` §3.

## Further reading

`docs/` is extensive. **Start at `docs/INDEX.md`** — the reading-order map that
groups every doc (Start here → Architecture → Metrology/Timing → Physics/Science
→ Data products → Operations) and marks the canonical (★) reference per domain.
Historical/superseded material lives under `docs/archive/`; the QEX paper draft
and HamSCI talks are under `docs/publications/`.

The load-bearing ones:

- `docs/METROLOGY.md` ★ — timing hierarchy §4.5–§4.6 (the canonical
  reference for the timing-authority invariant above).
- `docs/PHYSICS.md` ★ — canonical science capability inventory (DSP/physics).
- `docs/ARCHITECTURE-FIRST-PRINCIPLES.md` — producer-side reference
  for the §18 contract surface.
- `docs/ARCHITECTURE.md` — pipeline + service layering (SQLite backend).
- `docs/DEBUGGING.md` — journald-only logging patterns + triage recipes.
- `docs/TIMING-PIPELINE-WIRING.md` — RTP / chrony / fusion wiring.
- GRAPE/PSWS moved out: the daily pipeline, its docs and its units now
  live in **hamsci-physics** (2026-08-24 split). This repo keeps the
  timing core; hamsci-physics reads its products under the unchanged
  `/var/lib/timestd` root.
- `docs/PHARLAP_RAYTRACING.md` — PHaRLAP/pyLAP ray tracing (advisory physics
  overlay): 2-D/3-D capabilities, the `raytrace` CLI, worked
  Alaska→EM38ww examples. Engine: `core/raytrace_engine.py`.
- `docs/PHASE_ENGINE_ARCHITECTURE.md` — **planned** coherent multi-antenna
  array design (not yet implemented; reads as a roadmap, not current behavior).
