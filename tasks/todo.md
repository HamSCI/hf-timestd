# Phase 4 — gated on `live_vtec.py` redeploy

Steps 0, 1, 2a, 2b, 2c, 3 and partial-7 shipped to `main` 2026-05-21.
Schema `l3_gnss_vtec_v1.json` bumped 1.1.0 → 1.2.0 to declare 7
GNSS-diagnostic fields the producer writes.

| Step | Status | Commit |
|---|---|---|
| 0 — pipeline-watchdog SQLite freshness | ✅ | `fd48016` |
| 1 — chrony_stats SQLite | ✅ | `9eaaa65` |
| 2a — l2_calibration startup seed | ✅ | `134ad53` |
| 2b — physics_fusion L3_dtec seed + lookback | ✅ | `6c92ae3` |
| 2c — timing_validation load_fusion_result | ✅ | `3a018a9` |
| Schema 1.1.0→1.2.0 — l3_gnss_vtec diagnostic fields | ✅ | `2a537ca` |
| 3 — factory simplify + 7-partial test cleanup | ✅ | `7179cdb` |
| **2d — `_read_gnss_vtec` × 2** | ⏳ deploy-gated |
| **4 — delete hdf5_writer/hdf5_reader** | ⏳ deploy-gated |
| **5 — drop `h5py`** | ⏳ deploy-gated |
| 6 — _malloc_trim / HDF5_AVAILABLE cleanup | ⏳ pending |

Full pytest at HEAD: **1951 passed, 1 skipped, 1 deselected**.

## Pre-flight (snapshot 2026-05-21 ~13:00 UTC)

* timestd-fusion: 238 MB RSS, ~15h30m uptime since Phase 3b. ✅
* Authority A1/T6, σ=50μs, no disagreement_flags. ✅
* timestd-sqlite-parity.timer + service: inactive ✅ (already disabled per
  earlier handoff; do NOT re-enable — parity has no HDF5 side post-3b).
* pipeline-watchdog timers: active with Step-0 SQLite freshness deployed. ✅

## The blocker — `/opt/hf-timestd/scripts/live_vtec.py`

The deployed script is a March 16 snapshot:
```
< from hf_timestd.io import DataProductWriter
> from hf_timestd.io import make_data_product_writer
```
It bypasses the factory and writes HDF5 directly (the inode at
`/opt/hf-timestd/scripts/live_vtec.py` is a separate copy from the
in-tree source).  So:

1. `L3_gnss_vtec` table is empty — Step 2d (two `_read_gnss_vtec`
   sites in src/) can't read SQLite that nobody writes.
2. Deleting `hdf5_writer.py` / `hdf5_reader.py` (Step 4) breaks the
   deployed script's `from hf_timestd.io import DataProductWriter`
   on next restart.

## Resume sequence

1. **Redeploy** (operator with sudo):
   ```
   sudo cp -p /opt/hf-timestd/scripts/live_vtec.py \
     /opt/hf-timestd/scripts/live_vtec.py.bak-pre-phase4-$(date -u +%Y%m%dT%H%M%SZ)
   sudo install -o timestd -g timestd -m 755 \
     /home/mjh/git/hf-timestd/scripts/live_vtec.py \
     /opt/hf-timestd/scripts/live_vtec.py
   sudo systemctl restart timestd-vtec
   ```
2. **Verify** L3_gnss_vtec is populating:
   ```
   sleep 90 && sqlite3 -readonly /var/lib/timestd/phase2/timestd.db \
     "SELECT count(*), min(timestamp_utc), max(timestamp_utc) FROM L3_gnss_vtec;"
   ```
   Expect rows from now ± 60s (live_vtec batches HDF5/SQLite writes every
   60s) growing at ~1 Hz.

3. **Step 2d** — convert the two `_read_gnss_vtec` readers:
   * `src/hf_timestd/core/physics_fusion_service.py:1119` —
     `_read_gnss_vtec` per-cycle hot path. Replace raw h5py.File scan
     with `make_data_product_reader('L3','gnss_vtec','GNSS').read_time_range`
     over a ±120 s window around the target epoch. Honour the existing
     quality_flag GOOD/MARGINAL gate.
   * `src/hf_timestd/core/multi_broadcast_fusion.py:4602` — same
     pattern.
   * One commit each.  Verify `grep -rn 'import h5py\|hdf5_writer\|
     hdf5_reader' src/` returns only `io/hdf5_*.py`, `io/__init__.py`,
     `dual_writer.py`, `sqlite_reader.py:from .hdf5_reader`, and
     `metrology_service.py:47` (the dead import) before Step 4.

4. **Step 4** — delete + prune:
   * `git rm src/hf_timestd/io/hdf5_writer.py src/hf_timestd/io/hdf5_reader.py`
   * Edit `src/hf_timestd/io/__init__.py` to drop the
     `DataProductWriter` + `DataProductReader` re-exports.
   * Edit `src/hf_timestd/io/dual_writer.py` to drop
     `from .hdf5_writer import DataProductWriter` + the `DualWriter`
     class.
   * Edit `src/hf_timestd/io/sqlite_reader.py` to drop
     `from hf_timestd.io.hdf5_reader import DataProductReader`.
   * Edit `src/hf_timestd/core/metrology_service.py:47` — drop
     the unused direct `DataProductWriter` import.
   * Run `uv run --frozen --extra dev pytest tests/ --deselect
     tests/test_metrology_engine.py::test_geometric_prediction` — must be
     green.

5. **Step 5** — drop `h5py`:
   * Edit `pyproject.toml`: remove `"h5py>=3.8.0,<3.16.0",`.
   * `uv lock --upgrade-package h5py` (or just `uv lock`) — verify the
     resolved lock no longer mentions `h5py`.
   * `uv sync --extra dev` and re-run pytest.

6. **Step 6** — cleanup mechanical:
   * `multi_broadcast_fusion.py:202-220` — `_malloc_trim` is now a
     pure glibc-arena workaround unrelated to h5py.  Either keep with
     updated comment or delete if RSS proves stable.
   * `multi_broadcast_fusion.py:242-260` — delete `HDF5_AVAILABLE`
     branch (the reader factory cannot fail with `ImportError` now;
     SQLite is bundled in stdlib).  Drop the `if not HDF5_AVAILABLE`
     guards at lines 1432, 1459, 1585, 1664, 1872, 1936.
   * `live_vtec.py` — rename `hdf5_writer` → `vtec_writer`,
     `hdf5_write_buffer` → `vtec_write_buffer`, etc.  Update log
     strings.  This is the only producer where the naming is now
     misleading; everything else already routed through neutrally-named
     factory calls.

## Pre-existing gap surfaced this session

`SqliteDataProductWriter` rejects `float('nan')` on a `required + allow_nan`
float field because Python's `sqlite3` coerces NaN→NULL at the binding
layer, hitting the column's NOT NULL constraint.  `test_writer_accepts_
nan_velocity_direction` is skipped with a long-form reason citing this.
Not a Phase-4 regression — masked previously by the HDF5 fallback writer.
Fix later: either drop NOT NULL when `allow_nan: true`, or document that
producers must emit `None` (not NaN) and update affected schemas to
`required: false`.

## Workflow

```
uv run --frozen --extra dev pytest tests/ \
    --deselect tests/test_metrology_engine.py::test_geometric_prediction
```
