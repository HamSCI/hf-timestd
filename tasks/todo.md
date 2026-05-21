# Next session — finish HDF5 → SQLite Phase 4

Phase 3a + Phase 3b + Phase 4 Step 0 + Phase 4 Step 1 all shipped on
2026-05-20.  SQLite is the sole writer on `main`; HDF5 reader/writer
modules and the `h5py` dependency remain in the tree as a rollback
path that Phase 4 Steps 2–7 will retire.

Full plan: [`docs/PHASE_4_PLAN.md`].
Migration design seed: [`docs/HDF5-TO-SQLITE-MIGRATION.md`].
Last session log: [`docs/changes/SESSION_2026-05-20_PHASE_3B_FLIP.md`]
and [`docs/changes/SESSION_2026-05-20_REMEDIATION_COMPLETE.md`].

## Pre-flight state on bee1 (snapshot 2026-05-21 ~12:00 UTC)

| Check | Status |
|---|---|
| 24 `timestd-*` units in `active` state | ✅ |
| timestd-fusion uptime since Phase 3b | 15h+ (continuous since 2026-05-20 20:40:35 UTC) |
| timestd-fusion RSS | 222 MB — flat, the h5py leak is gone now that fusion no longer touches HDF5 |
| Authority | A1/T6 active, σ_ns = 50 000, no disagreement_flags |
| `timestd-sqlite-parity.service` | ❌ failing — **expected**, see operational debt below |
| 24 h soak target | not strictly met (need ~5 h more after this snapshot) but the leak driver is gone — user previously authorised proceeding before the strict 24 h on Phase 3b for the same reason |

## Operational debt to clear before / during Phase 4

* **`timestd-sqlite-parity.timer` should be disabled.** It runs
  `scripts/parity_check_all.sh` which compares HDF5 vs SQLite.  With
  Phase 3b live there's no HDF5 side, so every run exits non-zero.
  The service has been failing since 2026-05-21 06:00:29 UTC.  Stop
  the timer + service:
  ```
  sudo systemctl disable --now timestd-sqlite-parity.timer
  sudo systemctl reset-failed timestd-sqlite-parity.service
  ```
  Phase 4 Step 7 retires `parity_check_all.sh` itself.

* **The two pipeline watchdogs must stay stopped until Phase 4 Step
  0's SQLite freshness check ships to bee1.**  Step 0 has shipped to
  `main` (commit `fd48016`) but the deployed
  `/opt/hf-timestd/scripts/pipeline-watchdog.sh` on bee1 still uses
  the `*.h5` mtime check, so re-enabling the timer would mass-restart
  every metrology@* + fusion every ~5 min until the deploy lands.
  Either deploy first (recommended) then `sudo systemctl enable --now
  timestd-pipeline-watchdog.timer timestd-tsl3-watchdog.timer`, or
  defer until after Step 2.

## Phase 4 sub-step status

| Step | Status | Commit / Notes |
|---|---|---|
| 0 — pipeline-watchdog SQLite freshness check | ✅ shipped | `fd48016` (needs bee1 deploy before re-enabling watchdog timers) |
| 1 — `chrony_stats` schema → SQLite via `make_data_product_writer` | ✅ shipped | `9eaaa65` |
| **2 — Convert 5 raw-h5py *reader* sites** | ⏳ **start here** | l2_calibration_service.py:327-345, physics_fusion_service.py:1119-1131, 1525-1534, 1561-1577, timing_validation_service.py:330-333 |
| 3 — Simplify `make_data_product_writer`/`_reader` factory | ⏳ | keep factory shape, body becomes one-line SQLite return |
| 4 — Delete `src/hf_timestd/io/hdf5_writer.py` + `hdf5_reader.py` | ⏳ | requires Step 2 + 3 first (no `from hf_timestd.io.hdf5_*` imports left) |
| 5 — Drop `h5py>=3.8.0,<3.16.0` from `pyproject.toml` + `uv lock` | ⏳ | last "no-going-back" step; do after Step 4 has soaked a few hours |
| 6 — Comment cleanup (`_malloc_trim`, `HDF5_AVAILABLE`, dead SWMR/file-locking notes) | ⏳ | mechanical |
| 7 — Test rewrites / deletions (≥9 HDF5-specific test files) | ⏳ | `test_l2_seed_logging.py`, `test_physics_fusion_seed_minutes.py`, `test_dual_writer.py`, `test_hdf5_io.py` + 5 others — grep before starting |
| 8 — Fusion long-lived SQLite connection (post-Phase-4 polish) | ⏳ optional | RSS already stable at 222 MB; this is belt-and-braces |

## Step-2 starting point (concrete)

For each reader site, replace the raw `h5py.File(...)` block with
`make_data_product_reader(...).read_*()` against the same product
that's now writing to SQLite.  One commit per site, each verified
with `uv run --frozen --extra dev pytest tests/` before the next.

| File | Line | Purpose | SQLite product to read |
|---|---|---|---|
| `src/hf_timestd/core/l2_calibration_service.py` | 327–345 | startup seed — last L1 row per channel | `L1_metrology_measurements` |
| `src/hf_timestd/core/physics_fusion_service.py` | 1119–1131 | startup seed — propagation history | `L2_propagation_history` |
| `src/hf_timestd/core/physics_fusion_service.py` | 1525–1534 | startup seed — L3 propagation | `L3_propagation` |
| `src/hf_timestd/core/physics_fusion_service.py` | 1561–1577 | restart-resume — L3 dtec checkpoint | `L3_dtec` |
| `src/hf_timestd/core/timing_validation_service.py` | 330–333 | validation read — fusion timing | `L3_fusion_timing` |

After all five: `grep -rn 'from hf_timestd.io.hdf5_writer\|from
hf_timestd.io.hdf5_reader\|hdf5_writer\|hdf5_reader' src/ tests/`
should be empty (cue for Step 3 / Step 4).

## Reader semantic to remember

`SqliteDataProductReader` returns `None` for `NULL`, whereas the HDF5
reader filled with `NaN`/`0`/`""` (the `f7ec934` DUT1 bug class).
Intentional; downstream code already handles the SQLite-style
nullability where it matters.  Worth a re-check at each Step 2 site,
since startup-seed code is exactly where the HDF5-side coercion may
have been masking a `None`-blind consumer.

## Workflow

```
uv run --frozen --extra dev pytest tests/ \
    --deselect tests/test_metrology_engine.py::test_geometric_prediction
```

`--frozen` keeps `uv.lock` pinned (without it `uv run` re-resolves
`ka9q-python` and dirties the lock; `git checkout uv.lock` to drop
any drift).  The deselected test is the standing time-of-day F-layer
flake (`project_hf_timestd_flaky_geometric_prediction`), not a
regression.

## Out-of-scope items mentioned in the previous handoff — defer

These appeared in `project_hf_timestd_authority_work.md` /
`project_hf_timestd_fusion_audit.md` as parallel work, but are not
Phase-4 blockers:

* sigmond `smd install lan-fusion-client` + `smd lan-fusion-watch`
  (consumer side of the LAN Fusion service).
* `hf-timestd` `multi-instance` branch (8d7f5af, Rob's 2026-04-02
  work) — 264 commits stale, needs a rebase-or-drop decision.
* Bootstrap CORRELATING single-station stall (CHU vs WWV ~60 ms bias)
  — separate task #8 per Phase 4 plan, "not Phase-4 blocking but
  useful signal."
* sigmond untracked `docs/SCINTILLATION-MONITORING.md` — design draft
  from 2026-05-17, not gated on cutover.
