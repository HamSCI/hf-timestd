# Phase 4 plan — remove HDF5 code paths and `h5py` dependency

**Status as of 2026-05-20:** Phase 3a + Phase 3b live on bee1 since
~11:43 / ~13:35 UTC. SQLite is sole writer of every schema-based data
product. HDF5 reader/writer modules and `h5py` direct calls remain in
the codebase as a rollback path. This document plans their removal.

**Prerequisite — soak time.** Phase 3b has only been live for ~40 min
at the time of writing. Before starting Phase 4, give it **at least
24 h** of clean operation, watching for slow-burn issues that didn't
show up in the immediate post-flip check:

* Memory growth in `timestd-fusion` (h5py leak was the `_malloc_trim`
  workaround target; the leak should be ABSENT post-Phase-3b because
  fusion no longer touches h5py for writes — verify by tracking RSS
  over 24 h).
* SQLite database size growth rate and `PRAGMA wal_checkpoint;`
  efficiency.
* Any error in `journalctl -u timestd-*` we'd want to catch before
  removing the safety net.
* Bootstrap CORRELATING stall progress (separate task #8 — not Phase-4
  blocking but useful signal).

If any of these surfaces a Phase-3b-induced regression, FIX IT FIRST
before Phase 4.

## Removal sequence

Each numbered step is its own commit on its own branch, with the
`uv run pytest tests/` suite passing before merging. Fast-forward into
`main` after each step's verification clears.

### Step 1 — `chrony_stats` schema conversion

`src/hf_timestd/core/chrony_stats.py` writes HDF5 directly via raw
`h5py.File(...)` with no SQLite counterpart. Phase 3b gated the write
off; Phase 4 needs to bring the diagnostic back via SQLite parity.

* Create `src/hf_timestd/schemas/diag_chrony_stats_v1.json` (or
  `l3_chrony_stats_v1.json` if the `diag/` level isn't worth a new
  product_level entry). Schema fields match what's currently in the
  HDF5 `chrony_sources` group (timestamp, source name, mode, state,
  offset_us, std_dev_us, etc.).
* Refactor `ChronyStatsCollector` to use `make_data_product_writer`
  instead of `_write_hdf5`. Drop the `import h5py`.
* Remove the `self._write_hdf5_enabled` gate added in `e251111` — the
  factory now governs backend selection.
* Verify post-restart: rows accumulate in the new SQLite table at
  ~1/min.

### Step 2 — Convert raw-h5py *reader* call sites to SQLite

Five sites read HDF5 directly via `h5py.File(...)` for startup-seed
and recovery paths:

| File | Lines | Purpose |
|---|---|---|
| `l2_calibration_service.py` | 327-345 | Startup seed — last L1 row per channel |
| `physics_fusion_service.py` | 1119-1131 | Startup seed — propagation history |
| `physics_fusion_service.py` | 1525-1534 | Startup seed — L3 propagation |
| `physics_fusion_service.py` | 1561-1577 | Restart-resume — L3 dtec checkpoint |
| `timing_validation_service.py` | 330-333 | Validation read — fusion timing |

All five can be replaced with `make_data_product_reader(...).read_*()`
calls against the SQLite tables that now hold the same data
(`L1_metrology_measurements`, `L3_dtec`, `L3_fusion_timing`, etc.).

Each conversion should be a separate commit so blast radius is small
and tests are local. After all five: search for any remaining
`import h5py` outside `src/hf_timestd/io/hdf5_*` — should be zero.

### Step 3 — Simplify the factory

`src/hf_timestd/io/dual_writer.py`:

* `make_data_product_writer` currently dispatches between HDF5 / SQLite
  / both. With Phase 4 it becomes SQLite-only.
* `make_data_product_reader` (in dual_writer or elsewhere — confirm)
  similarly simplifies.

Options:
* **Keep the factory** but reduce it to a thin SQLite-only wrapper.
  Minimal disruption to call-sites.
* **Inline the factory** and replace every call with direct
  `SqliteDataProductWriter(...)` / `SqliteDataProductReader(...)`.
  Cleaner but ~20 call-sites to touch.

Recommend: keep the factory (it's a one-line `return
SqliteDataProductWriter(...)`) so the public API stays stable; tag the
factory `Deprecated` in docstring with a Phase-5 removal note if
desired.

### Step 4 — Delete the HDF5 library modules

After Step 2 + Step 3, nothing in `src/` should import from
`src/hf_timestd/io/hdf5_writer.py` or `hdf5_reader.py`. Verify:

```
grep -rn "from hf_timestd.io.hdf5_writer\|from hf_timestd.io.hdf5_reader\|hdf5_writer\|hdf5_reader" src/ tests/
```

If clean, delete:

* `src/hf_timestd/io/hdf5_writer.py`
* `src/hf_timestd/io/hdf5_reader.py`

(Leave `dual_writer.py` in place — see Step 3.)

### Step 5 — Drop `h5py` from `pyproject.toml`

Remove `"h5py>=3.8.0,<3.16.0",` and its trailing comment. After:

```
uv lock
uv sync --extra dev --extra gnss --extra iono
uv run pytest tests/
```

Verify the lock file no longer references `h5py`.

### Step 6 — Clean up h5py-related comments and workarounds

* `multi_broadcast_fusion.py:181-220` — the `_malloc_trim()` glibc
  helper. Originally a defensive workaround for h5py's allocation
  pattern; with h5py gone, evaluate whether it's still useful (it may
  still help against numpy temporaries, but the framing is now
  misleading). Either delete or update the comment + rename to clarify
  what it actually does post-Phase-4.
* `multi_broadcast_fusion.py:242-259` — `HDF5_AVAILABLE` flag and the
  optional `import h5py` block. Delete entirely; the conditional
  branches at lines 1432 + 1459 become unconditional (or are dead code
  to remove).
* Other dead comments about HDF5 SWMR, file locking, etc.

### Step 7 — Tests

9 test files reference HDF5 directly:

```
tests/test_l2_seed_logging.py
tests/test_physics_fusion_seed_minutes.py
tests/unit/test_dual_writer.py
tests/unit/test_hdf5_io.py
(plus 5 others — confirm via grep before starting)
```

For each:
* If it tests HDF5 file I/O specifically: **delete**.
* If it tests behaviour that's now SQLite-only: **rewrite** against
  the SQLite reader/writer fixtures.

`tests/unit/test_hdf5_io.py` is almost certainly delete-on-sight.
`tests/unit/test_dual_writer.py` becomes a SQLite-only writer test.

After this step, the suite should still pass the full 1993 (or
whatever Phase 3b leaves it at) tests with the HDF5-specific ones
either removed or replaced.

### Step 8 — Fusion memory-leak follow-up (separate, optional)

The `_malloc_trim` workaround in `multi_broadcast_fusion.py` was
introduced because h5py allocated/freed large temporaries per fusion
cycle, but glibc never returned the pages to the OS, leading to
RSS-bloat that looked like a leak. With h5py gone, the pattern likely
stops. But if Phase-4 RSS monitoring shows growth, the cause may be
that fusion still opens a fresh `SqliteDataProductReader` per cycle
instead of holding a long-lived connection.

Convert fusion to a long-lived SQLite connection (one reader per
product, opened in `__init__` and reused). Verify RSS stays flat over
24 h.

This is post-Phase-4 polish; not blocking.

## Rollback strategy

Phase 4 commits can be reverted with `git revert <sha>` and a producer
redeploy. **Important caveat:** any new SQLite-only data written after
Step 1 (e.g. chrony_stats SQLite-table content) will not have an HDF5
counterpart on rollback — the rollback restores the HDF5 path but
doesn't backfill HDF5 from SQLite. Backfill is possible but expensive.

For the cleanest rollback experience: keep the HDF5 code paths
deletion (Step 4) **last**, after Steps 1-3 have soaked for a day
each. That way the riskiest changes (data-format conversions) are in
place and proven before the safety net is removed.

## Estimated effort

* Step 1 (chrony_stats schema): 1-2 hours including schema design,
  refactor, test.
* Step 2 (raw-h5py readers): 30 min per site × 5 = 2.5 hours.
* Step 3 (factory simplification): 30 min if keep-factory; 2 hours if
  inline-everything.
* Step 4 (delete modules): 10 min.
* Step 5 (drop dep): 30 min including `uv lock`/`sync` and validation.
* Step 6 (cleanup): 30 min.
* Step 7 (tests): 1-2 hours depending on rewrite scope.
* Step 8 (fusion long-lived connection): 1-2 hours; separate session.

Total: **6-10 hours of work** across multiple sessions, with at least
a day's soak between Step 1 and Step 4.

## Pre-execution checklist

- [ ] Phase 3b live for at least 24 h with no Phase-3b-induced
      regressions
- [ ] `timestd-fusion` RSS verified stable (not growing) over 24 h
- [ ] All 14 timestd services active continuously
- [ ] Bootstrap state (`SEARCHING` / `CORRELATING` / `LOCKED`) not
      worse than at end of Phase-3b session
- [ ] User signs off on starting Phase 4

When all checked: start with Step 1.
