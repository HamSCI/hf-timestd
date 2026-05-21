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

## Operator restart — done 2026-05-21 14:30-14:43 UTC

* `sudo systemctl restart timestd-l2-calibration` @ 14:30:59 UTC. Seed
  log: `Startup: L2 gap ≤10 minutes — normal lookback window sufficient`.
* `sudo systemctl restart timestd-physics` @ 14:31:22 UTC. Backfilled
  L3_dtec from minute -30 forward.
* `sudo systemctl restart timestd-fusion` @ 14:32:36 UTC. Initialized
  SQLite readers for every channel; authority dropped to A1/T4 σ=2ms
  during bootstrap then recovered to A1/T6 σ=50μs by 14:37:03 UTC.
* `sudo systemctl restart timestd-web-api` — FIRST attempt at 14:33:02
  succeeded, but endpoints returned empty because the running web-api
  was using `/opt/hf-timestd/web-api/` and `/opt/hf-timestd/src/` — both
  separate stale snapshots, not the editable-install path.

### Web-api dual-snapshot finding (resolved)

`/opt/hf-timestd/web-api/` is a March-deployed copy that drifts from
the source tree.  11 files in services/ and routers/ still imported
`from hf_timestd.io.hdf5_reader import DataProductReader` — they were
already converted in source, just never deployed.  Plus
`/opt/hf-timestd/src/` was a separate April-3 snapshot of `hf_timestd`
that `sys.path.insert(...)` lines in web-api forced into the import
path, shadowing the venv's editable install at /opt/git/sigmond/.

Resolution (preserved here as the recipe for the next drift):
```
# Move the stale /opt/hf-timestd/src snapshot aside so sys.path.insert
# points at nothing and the editable install takes precedence.
TS=$(date -u +%Y%m%dT%H%M%SZ)
sudo mv /opt/hf-timestd/src /opt/hf-timestd/src.bak-pre-phase4-$TS

# Back up + sync the 11 web-api files that differ from source.  (Listed
# by `diff -rq /home/mjh/git/hf-timestd/web-api/ /opt/hf-timestd/web-api/
# | grep differ | grep -v __pycache__`.)
BAK=/opt/hf-timestd/web-api/.bak-pre-phase4-$TS
sudo mkdir -p $BAK/routers $BAK/services
for f in routers/dashboard.py routers/docs.py routers/propagation.py \
         routers/ionogram.py routers/tec.py \
         services/event_service.py services/fusion_service.py \
         services/health_service.py services/physics_service.py \
         services/propagation_service.py services/stability_service.py \
         services/test_signal_service.py services/tid_service.py \
         services/chrony_service.py services/chu_fsk_service.py \
         services/phase_service.py services/scintillation_service.py \
         services/tec_service.py config.py; do
  sudo cp -p /opt/hf-timestd/web-api/$f $BAK/$f
  sudo install -o timestd -g timestd -m 644 \
      /home/mjh/git/hf-timestd/web-api/$f /opt/hf-timestd/web-api/$f
done

sudo systemctl reset-failed timestd-web-api
sudo systemctl restart timestd-web-api
```

### Post-restart verification (snapshot 2026-05-21 ~14:43 UTC)

* `timestd-{fusion,l2-calibration,physics,web-api,vtec}` all active
* fusion RSS: 205 MB / 11m uptime ✓
* Authority: A1/T6 σ=50μs ✓
* L3 freshness:
  - L3_fusion_timing: 14:43:53 UTC (≤10s old)
  - L3_gnss_vtec: 14:43:46 UTC (≤10s old)
  - L3_dtec: 14:41:00 UTC (per-minute cadence, ~3 min old at check)
  - DIAG_chrony_stats: 14:42:54 UTC
  - L2_chu_fsk: 14:43:04 UTC
* Web-api endpoints verified live:
  - `/api/chrony/history?hours=1` → 7 sources
  - `/api/phase/summary` → 17 traces
  - `/api/phase/channels` → 9 channels
  - `/api/tec/dtec?start=-30m` → 17 series, 931 points
  - `/api/ionogram/channels` → 5 channels

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
