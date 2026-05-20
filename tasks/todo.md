# Next session — HDF5 → SQLite cutover

The 2026-05-17 metrology/physics review remediation arc is **complete**
on `main` (HEAD `6ab5d5b`, 2026-05-20).  All findings — including the
deferred P-H29 (TID L3 deliverable) — are resolved; all 8
PHYSICS_CONTRACT Known Deviations are closed.  Suite green: 1993
passed, 1 deselected (the standing `test_geometric_prediction`
time-of-day flake).  Full session log:
[`docs/changes/SESSION_2026-05-20_REMEDIATION_COMPLETE.md`].

The next workstream is the HDF5 → SQLite cutover.  Runbook lives in
[`docs/HDF5-TO-SQLITE-MIGRATION.md`]; ongoing-task state is in the
project memory `project_hf_timestd_sqlite_cutover`.

## Pre-flight state (carried over from 2026-05-19 on bee1)

- Phase 1 (`SqliteDataProductReader` + `make_data_product_reader` +
  `[storage] read_sqlite` knob), Phase 2 (all producers via
  `make_data_product_writer`), and Phase 3a-prep (all consumers
  backend-agnostic via `make_data_product_reader`) are all committed.
- On bee1, all 13 data products were dual-writing as of 2026-05-19.
  The first full 9-channel `parity_check_all.sh` sweep at ~11:43 UTC
  returned **70 checks → 60 OK / 10 SKIP / 0 PENDING / 0 FAIL**
  (SKIPs benign: `chu_fsk` on non-FSK channels, idle reanalysis).
  The parity window has been accumulating clean runs since.
- The "merge branch → main" step in the runbook is **already done**
  (commit `2387c3b`, 2026-05-19 evening / 2026-05-20 morning).  The
  cutover session can skip directly to the parity-check → flip steps.

## Cutover steps (in order)

1. **Parity check** — on bee1:

   ```
   sudo journalctl -u timestd-sqlite-parity --since "2026-05-19 11:30" \
       | grep -E 'Summary|FAIL|PENDING'
   ```

   Go criteria: every run since 2026-05-19 11:32 shows `fail=0 err=0
   pending=0`.  (The first run after l2-calibration's 11:32 restart
   may show `L2_timing_measurements` PENDING; runs from 12:00 UTC
   onwards must all be clean.)  A live full sweep is also fine:

   ```
   bash scripts/parity_check_all.sh
   ```

   with the canonical channel list (`CHANNELS="CHU_3330 CHU_7850
   CHU_14670 SHARED_2500 SHARED_5000 SHARED_10000 SHARED_15000
   WWV_20000 WWV_25000"`).

2. **Phase 3a — the flip.** With parity verified clean:

   - `/etc/hf-timestd/timestd-config.toml`, `[storage]`: add
     `read_sqlite = true`.
   - Repo `config/timestd-config.toml`, `[storage]`: set
     `read_sqlite = true`.
   - Restart consumers: `timestd-fusion`, `timestd-l2-calibration`,
     the physics-fusion service, web-api.
   - Watch chrony TSL2 and the next parity run.  Any FAIL → **do
     not flip**, investigate the divergence first.

3. **Phase 3b — `write_hdf5 = false`.** Once Phase 3a has been
   running clean for at least one full parity window (and ideally
   a full day), set `write_hdf5 = false` so SQLite is the sole
   writer.  Re-validate the suite and chrony TSL2.

4. **Phase 4 — remove HDF5 + h5py.** Drop the HDF5 code paths,
   remove the pinned `h5py>=3.8.0,<3.16.0` dependency, retire
   `HDF5-TO-SQLITE-MIGRATION.md`'s "during cutover" sections.
   The fusion-service h5py memory leak (documented in
   `multi_broadcast_fusion.py`'s `_malloc_trim` block) is only
   fully fixed once fusion also holds a *long-lived* SQLite
   connection (still per-cycle today); that's a Phase-4 follow-up.

## Reader semantic to remember

`SqliteDataProductReader` returns `None` for `NULL`, whereas the HDF5
reader fills with `NaN`/`0`/`""` (the `f7ec934` DUT1 bug class).
Intentional; downstream code already handles the SQLite-style
nullability where it matters.

## Workflow

```
uv run --frozen --extra dev pytest tests/
```

`--frozen` keeps `uv.lock` pinned (without it `uv run` re-resolves
`ka9q-python` and dirties the lock; `git checkout uv.lock` to drop
any drift).  Standing flake to deselect (not a regression):
`tests/test_metrology_engine.py::test_geometric_prediction` (F-layer
height time-of-day behaviour).
