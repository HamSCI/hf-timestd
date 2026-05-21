# Phase 4 complete — operational notes

All Phase-4 steps from `docs/PHASE_4_PLAN.md` have shipped to `main`
2026-05-21.  hf-timestd is HDF5-free in tree: no `hdf5_writer.py`,
no `hdf5_reader.py`, no `import h5py` anywhere in `src/`, `tests/`,
`web-api/`, or the surviving `scripts/`.  `h5py` is removed from
`pyproject.toml` as a direct dep (it remains as a transitive dep via
`digital_rf` for GRAPE/Haystack export — out of Phase 4 scope).

| Step | Status | Commits |
|---|---|---|
| 0 — pipeline-watchdog SQLite freshness | ✅ | `fd48016` |
| 1 — chrony_stats SQLite | ✅ | `9eaaa65` |
| 2a — l2_calibration startup seed | ✅ | `134ad53` |
| 2b — physics_fusion L3_dtec seed + lookback | ✅ | `6c92ae3` |
| 2c — timing_validation load_fusion_result | ✅ | `3a018a9` |
| Schema 1.1.0→1.2.0 — l3_gnss_vtec | ✅ | `2a537ca` |
| 3 + 7-partial — factory + obsolete tests | ✅ | `7179cdb` |
| 2d — _read_gnss_vtec × 2 | ✅ | `ae5db86` |
| live_vtec.py redeploy + restart | ✅ | (operator deploy, 13:15:39 UTC) |
| 4 + 5 + 6 — purge + drop h5py + cleanup | ✅ | `0276a0d` |
| docs/handoff refresh | ✅ | (this commit) |

Full pytest: **1951 passed, 1 skipped, 1 deselected, 12 warnings.**

## Live operator action still required

The running timestd-fusion / l2-calibration / physics processes
imported the old `hf_timestd.io.hdf5_writer` / `hdf5_reader` modules
at startup.  Those modules are now deleted in the source tree.  The
in-memory cache keeps the processes running, but **a restart picks
up code that no longer has the HDF5 modules**.  Plan:

```
# Confirm services healthy before restart
systemctl is-active timestd-fusion timestd-l2-calibration timestd-physics
sqlite3 -readonly /var/lib/timestd/phase2/timestd.db \
  "SELECT max(timestamp_utc) FROM L1_metrology_measurements;"

# Restart, then watch the journal for clean startup
sudo systemctl restart timestd-fusion timestd-l2-calibration timestd-physics
sleep 10
systemctl is-active timestd-fusion timestd-l2-calibration timestd-physics

# Re-check L3 freshness after a couple cycles
sleep 90 && sqlite3 -readonly /var/lib/timestd/phase2/timestd.db \
  "SELECT max(minute_boundary), datetime('now','-2 minutes') FROM L3_fusion_timing;"
```

Web-api can be restarted alongside or separately:
```
sudo systemctl restart timestd-web-api
```

## Pre-existing SQLite-writer gap (deferred bug)

`SqliteDataProductWriter` rejects `float('nan')` on a `required +
allow_nan` float field because Python's `sqlite3` coerces NaN→NULL
at the binding layer, hitting the column's NOT NULL constraint.
Currently parked behind a `@unittest.skip` in `tests/unit/
test_tid_l3_writer.py::test_writer_accepts_nan_velocity_direction`.
Fix later: either drop NOT NULL when `allow_nan: true`, or change
producers to emit None (not NaN) and update affected schemas to
`required: false`.

Affected schemas to audit: `l3_tid` (velocity_m_s, direction_deg);
grep schemas for `"allow_nan": true` + `"required": true`.

## Polish that wasn't included in `0276a0d`

* `scripts/live_vtec.py` still uses `hdf5_writer` / `save_hdf5` /
  `hdf5_write_buffer` variable names + log messages even though the
  factory returns SqliteDataProductWriter now.  Cosmetic; rename in
  a follow-up pass.
* `_malloc_trim()` in `multi_broadcast_fusion.py:202-211` was
  originally a defensive workaround for the h5py per-cycle leak.
  With h5py-side reads gone (Phase 2d) the leak driver is gone too;
  fusion RSS has been flat at 222 MB since Phase 3b.  Evaluate
  whether `_malloc_trim` is still useful after a few days of
  post-Phase-4 RSS data; if not, delete.
* `l3_gnss_vtec_v1.json` v1.2.0 added the 7 GNSS-diagnostic fields
  that live_vtec.py was already writing.  Worth a quick audit of
  the other "stuff producers write that schemas don't declare"
  pattern across the data products.

## Workflow

```
uv run --frozen --extra dev pytest tests/ \
    --deselect tests/test_metrology_engine.py::test_geometric_prediction
```
