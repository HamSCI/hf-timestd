# HDF5 → SQLite migration — Phase 2 reader foundation

## Context
Phase 1 (parallel writers) is done and live on bee1: `[storage]
write_sqlite = true` since 2026-05-15, all 9 channels dual-writing,
`timestd-sqlite-parity` timer green. Phase 2 (reader migration) had
not been started — no SQLite reader existed.

This session: build the read-side foundation only (scope chosen by
the user). No consumer is rewired and no production behaviour changes
— the reader factory defaults to HDF5.

## Tasks
- [x] `src/hf_timestd/io/sqlite_reader.py` — `SqliteDataProductReader`,
      a drop-in for `DataProductReader.read_time_range()`, reading the
      SQLite tables `SqliteDataProductWriter` produces. Long-lived
      read-only WAL connection.
- [x] `make_data_product_reader()` factory — backend selected by
      `[storage] read_sqlite`; mirror of `make_data_product_writer`.
- [x] `[storage] read_sqlite = false` knob in
      `config/timestd-config.toml.template`.
- [x] Export the new symbols from `src/hf_timestd/io/__init__.py`.
- [x] `tests/unit/test_sqlite_reader.py`.
- [x] Verify: new + existing io tests green; spot-check the reader
      against the live `timestd.db`.

## Out of scope (follow-on sessions)
- Extending dual-write to l2_calibration / physics_fusion / vtec so
  SQLite holds the full product set.
- Wiring `multi_broadcast_fusion` (and web-api) to the factory +
  divergence logging, then flipping `read_sqlite` after a parity
  window.
- Resume the metrology/physics remediation at P-H29.

## Review
**Done — Phase 2 reader foundation.** No production behaviour change:
the factory defaults to HDF5 and no consumer is rewired yet.

- New `io/sqlite_reader.py`: `SqliteDataProductReader` (read-only WAL
  connection, `read_time_range` only — the sole method any consumer
  calls) + `make_data_product_reader` factory. Two intentional
  semantic differences from the HDF5 reader, documented in the module
  docstring: SQLite NULL is preserved as `None` (vs HDF5's NaN/0/""
  fill — the f7ec934 DUT1 bug class); rows returned chronologically.
- Missing DB file / missing table tolerated → reads return `[]`
  (mirrors HDF5 skipping absent files), so the reader is safe to point
  at products not dual-written yet.
- `[storage] read_sqlite` knob added to the config template; kept
  independent of `write_sqlite` so writes are verified before reads
  trust them.
- Tests: `tests/unit/test_sqlite_reader.py` — 21 tests incl. a
  cross-backend parity test (DualWriter → HDF5 reader vs SQLite reader,
  required-field values equal). io suite 50/50 green; full repo
  collects 1729 tests, no import errors.
- Live check on bee1 `timestd.db` (last 2 h, CHU_7850): HDF5 vs SQLite
  readers — identical row counts, 0 missing either side, 0 value diffs
  across L1_metrology / L2_tick_phase / L2_chu_fsk / L2_detection_attempts.

**Not committed** — left for the user to review/commit.

## Next (follow-on sessions)
1. Extend dual-write to l2_calibration / physics_fusion / live_vtec so
   SQLite holds the products fusion + web-api read (esp.
   `L2_timing_measurements`), then a fresh parity window.
2. Wire `multi_broadcast_fusion` (and web-api services) through
   `make_data_product_reader`; run divergence logging; flip
   `read_sqlite` per the doc's verification cadence.
3. Phase 3/4: defaults flip, HDF5 path removal.
4. Resume the metrology/physics remediation at P-H29.
