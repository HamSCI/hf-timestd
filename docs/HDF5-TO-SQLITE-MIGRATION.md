# HDF5 → SQLite migration — design

**Status:** ✅ COMPLETE. Implemented across Phases 1–4; Phase 3b cut bee1 to SQLite-only writes (2026-05-20) and Phase 4 deleted the HDF5 backend (`io/hdf5_writer.py` / `io/hdf5_reader.py` removed; `io/dual_writer.py` now unconditionally returns `SqliteDataProductWriter`). The HDF5↔SQLite dual-write `timestd-sqlite-parity` verification units were removed 2026-06-12 as obsolete (no HDF5 store remains to compare against). The design discussion below is retained as a historical record.
**Audience:** Rob, Michael — and future contributors arriving cold.
**Why this exists:** the 2026-05-15 fusion-leak chase concluded that
the residual ~184 MB/h RSS growth (after `_iri_cache` eviction and
jemalloc) is in **h5py per-cycle reader internal state**. The
2026-05-12 sigmond CH→SQLite migration set a clean precedent for
the upload pipeline (`sink.db` replacing ClickHouse as the local
store-and-forward buffer). This document proposes extending that
move into hf-timestd's *internal* data flow — replacing HDF5 as the
substrate for metrology → fusion measurement plumbing — and lays out
a phased plan that can be executed across several sessions without
disrupting the live deployment.

---

## 1. Why migrate at all

The proximate driver is the leak. But the case for SQLite is
broader than just one bug:

- **One storage model across sigmond.** sink.db already exists on
  every sigmond host. Today, hf-timestd is the lone holdout still
  running its own bespoke HDF5 sub-pipeline. Consolidating means one
  fewer file format, one fewer Python dependency (h5py), one fewer
  on-disk schema family, and one fewer flavor of corrupt-trailing-
  chunk handling code.
- **h5py's per-file open/close pattern is the wrong shape.** Fusion
  creates ~5–10 fresh `DataProductReader` instances per 10 s cycle.
  Each opens an HDF5 file, reads ENTIRE-day datasets into memory
  (then filters), closes. The internal state h5py keeps for type
  tables, group caches, and chunk indices is what leaks. The
  alternative — long-lived readers per channel — runs into HDF5's
  concurrent-writer / SWMR locking gotchas, which the existing
  reader already has a corrupt-trailing-chunk codepath to handle.
  SQLite handles concurrent writer + reader natively via WAL mode.
- **Random-access by time range is the dominant query shape.**
  Fusion's primary read is "give me the last N minutes of station
  X's measurements." HDF5's only support for this is "read the
  whole dataset, then filter in Python." SQLite gives us
  `WHERE timestamp_utc BETWEEN ?  AND ? ORDER BY timestamp_utc`
  served from an index, which is the right shape for the workload.
- **Schemas are already SQL-shaped.** The 25 JSON schemas in
  `src/hf_timestd/schemas/` are field-list-with-types — directly
  translatable to `CREATE TABLE`. The existing per-field
  validation in `DataProductWriter._validate_field()` already
  enforces type/range/enum constraints that map 1:1 to SQL
  CHECK constraints.

What we lose: HDF5 compression (gzip on chunks). SQLite has its own
compression options if size becomes an issue, but in practice the
hf-timestd record rate is low (1-60 rows/minute per data product),
so an uncompressed SQLite table will be smaller than the same data
in an HDF5 file with the chunk overhead anyway.

## 2. Current state, surveyed

The hf-timestd HDF5 plumbing has three pieces:

| Component | Lives at | Role |
|---|---|---|
| 25 schemas | `src/hf_timestd/schemas/*.json` | Declarative type/range/enum spec per data product |
| `DataProductRegistry` | `src/hf_timestd/data_product_registry.py` | Maps product names → schemas |
| `DataProductWriter` | `src/hf_timestd/io/hdf5_writer.py` | Append-row writer with corrupt-recovery |
| `DataProductReader` | `src/hf_timestd/io/hdf5_reader.py` | Read time range, filter, return list-of-dicts |

Producers:
- `metrology_service.py` — writes 5–8 products per channel per minute
- `l2_calibration_service.py` — reads L1, writes L2
- `multi_broadcast_fusion.py` — reads L1+L2, writes L3
- `gnss_vtec` — writes its own product

Consumers:
- `multi_broadcast_fusion.py` is the hot reader (every 8 s)
- Various analysis scripts in `scripts/` (offline)

File layout:
```
/var/lib/timestd/phase2/
  ├── {channel}/
  │   ├── clock_offset/{channel}_timing_measurements_YYYYMMDD.h5
  │   ├── metrology/{channel}_metrology_measurements_YYYYMMDD.h5
  │   ├── tick_timing/{channel}_tick_phase_YYYYMMDD.h5
  │   ├── chu_fsk/{channel}_chu_fsk_YYYYMMDD.h5
  │   ├── tone_observations/{channel}_tone_observations_YYYYMMDD.h5
  │   ├── all_arrivals/{channel}_all_arrivals_YYYYMMDD.h5
  │   ├── detection_attempts/{channel}_detection_attempts_YYYYMMDD.h5
  │   └── test_signal/{channel}_test_signal_YYYYMMDD.h5
  └── fusion/
      ├── global_physics_YYYYMMDD.h5
      ├── chrony_stats_YYYYMMDD.h5
      └── fusion_timing_YYYYMMDD.h5
```

Each data product lives in its own per-channel daily file. Channels
× products × dates × ~20 fields each → ~thousands of files on disk
after a year.

## 3. Target state

Single SQLite database per data root: `/var/lib/timestd/phase2/timestd.db`
(or `/var/lib/sigmond/sink.db` if we choose to share with the
upload pipeline — see §7 question 1).

Each data product is one table, keyed by `(channel, timestamp_utc)`
or `(channel, minute_boundary_utc)` depending on cadence. Schema
columns map 1:1 to JSON-schema fields. WAL mode + reasonable
PRAGMA tuning for the concurrent-writer + reader pattern.

Reader API stays the same: a class that mirrors `DataProductReader`,
called `SqliteDataProductReader` with identical method signatures.
Same goes for the writer. Producers and consumers don't need to
know which backend they're talking to — selected by config at
service startup.

## 4. Migration phases

### Phase 1 — parallel writers (1–2 sessions)

- `SqliteDataProductWriter`: mirror of `DataProductWriter` API,
  emits rows into a per-product SQLite table.
- Schema bootstrap from JSON schema files.
- Service-level config knob `[storage] write_sqlite = false`
  (default off; opt-in for early-adopter producers).
- One producer chosen as the canary (probably
  `timestd-metrology@CHU_7850` — most stable channel) writes to
  both HDF5 and SQLite in parallel.
- Verification: row counts match, field values byte-equal (allowing
  for float precision noise) between the two stores.

### Phase 2 — reader migration (1–2 sessions)

- `SqliteDataProductReader`: mirror of `DataProductReader` API
  with identical method signatures (so callers don't change).
- `multi_broadcast_fusion.py` gets a config knob to choose backend
  per channel/product. Initially: read from HDF5 (unchanged),
  validate against SQLite (where available), log divergence.
- After 1–2 days of clean divergence logs, flip the default to
  SQLite-read for the canary channel.
- Expand to all channels.

### Phase 3 — HDF5 deprecation (1 session)

- Default `write_sqlite=true`, `write_hdf5=false` in shipped config.
- Existing HDF5 files preserved (they're historical archives).
- One-shot migration tool to backfill SQLite from existing HDF5
  history (optional — depends on whether we need the historical
  data online vs just-on-disk).

### Phase 4 — cleanup (1 session)

- Remove `DataProductWriter` HDF5 code paths.
- Remove `DataProductReader` HDF5 code paths.
- Remove h5py from `pyproject.toml` requires.
- The fusion leak as we currently see it disappears.

Total estimate: 4–6 sessions of focused work, ~4–6 weeks calendar
including the verification windows.

## 5. Schema mapping

For each JSON field type:

| JSON type | SQLite column type | Validation |
|---|---|---|
| `string` (no format) | `TEXT` | length cap if `max_length`; CHECK if `enum` |
| `string` (`format: iso8601`) | `TEXT` (ISO-8601 with `Z` suffix) | sortable lex order = time order |
| `integer` | `INTEGER` | CHECK range if `valid_range` |
| `float` | `REAL` | CHECK range if `valid_range`; NULL ↔ NaN |
| `boolean` | `INTEGER` (0/1) | |

Primary key: composite `(channel, timestamp_utc)` for most products;
some have `(channel, minute_boundary_utc)` as the natural key.
Index on `(channel, timestamp_utc)` for the time-range query.

NaN/None handling: SQLite stores actual `NULL`. The existing HDF5
writer at `hdf5_writer.py:597` converts None → `np.nan` for float
fields — that's the conversion that bit us in the CHU FSK / DUT1
investigation. SQLite NULL preserves the distinction, so downstream
code that uses `if dut1 is not None` works correctly without
needing the NaN guard we added in `f7ec934`.

## 6. Connection / concurrency model

Writers (metrology services × N + l2_calibration + fusion + vtec):
- Each opens its own connection (`sqlite3.connect(path, timeout=5)`).
- PRAGMA journal_mode=WAL on first connect.
- PRAGMA synchronous=NORMAL (acceptable for our durability model —
  metrology can re-derive on restart).
- `BEGIN IMMEDIATE` for batched writes; commit every N rows or T s
  (mirror existing `DataProductWriter` flush policy).

Readers (fusion, analysis scripts):
- Open in read-only mode with `?mode=ro` URI.
- WAL allows concurrent readers while a writer holds the write lock.
- No truncation/corrupt-trailing-chunk recovery needed (SQLite has
  its own corruption guarantees).

Long-lived connections (fix the leak):
- Fusion holds one read connection for the lifetime of the
  process. No per-cycle open/close.
- This is the architectural difference from h5py and is the actual
  fix for the leak.

## 7. Open questions

1. **Shared vs separate `*.db`?** Is `timestd.db` separate from
   `sink.db`, or do we extend `sink.db` with the metrology tables?
   - Argument for separate: keeps hot per-cycle write/read traffic
     off the upload pipeline's database; different durability
     models.
   - Argument for shared: one fewer file to manage; simpler backup;
     fits the "one storage model" goal.
   - Lean: separate `timestd.db`. The per-host upload buffer
     (`sink.db`) and the timestd internal data flow have different
     lifecycle expectations.
2. **Daily file rotation vs single-file?** HDF5 rotates daily.
   SQLite doesn't need to — a single multi-gigabyte file is fine.
   But operationally, daily files make trim/archive easier (just
   delete yesterday's file). Likely keep daily rotation.
3. **Existing HDF5 history.** Do we need it online? If yes, a
   one-shot migration tool is needed in phase 3. If "archive only,"
   we can leave the HDF5 files in place and only new data goes to
   SQLite.
4. **Schema version on existing JSON schemas.** Do we leave them as
   the source of truth and generate both backends from them, or do
   we copy them into a SQL-DDL-friendly form? Lean: keep as the
   source of truth, generate CREATE TABLE at runtime.
5. **Performance.** Will SQLite handle ~10 channels × 5 products
   × 1 row/min ≈ 50 writes/min comfortably? Yes — SQLite WAL
   does ~30k inserts/sec on a modest SSD. Our load is 4 orders of
   magnitude below that. The fusion read pattern (select last 30
   min by index) is also fast for our table sizes.
6. **Backwards compatibility for offline analysis scripts.** Some
   scripts in `scripts/` read HDF5 directly. They need to either be
   updated or pointed at a SQL→DataFrame helper. Inventory + plan
   needed during Phase 2.

## 8. Phase 1 plan (next session)

Concrete deliverables for the next coding session:

1. **`SqliteDataProductWriter` class** in `src/hf_timestd/io/sqlite_writer.py`:
   - Same constructor signature as `DataProductWriter`
   - `write_measurement(dict)` → upsert into appropriate table
   - `write_measurements_batch(list[dict])` → batched insert
   - Schema bootstrap (CREATE TABLE IF NOT EXISTS) from JSON schema
   - Same validation as `DataProductWriter._validate_field`
2. **Config plumbing** — add `[storage] write_sqlite = false`,
   `[storage] sqlite_path = "/var/lib/timestd/phase2/timestd.db"`
   to `timestd-config.toml.template`
3. **One producer dual-writes** — `metrology_service.py` writes to
   both backends when configured
4. **Test suite** — `tests/test_sqlite_writer.py` mirroring the
   existing `tests/test_hdf5_writer.py` structure
5. **Verification scripts** — small CLI helper that reads N minutes
   from both backends, compares row counts and field values,
   prints divergence summary

After Phase 1 ships: leave dual-write enabled on bee1 for ~3 days.
Verify the divergence summary returns "no divergence" every day.
Then Phase 2 begins.

---

## Notes for the reader

This is meta-architecture work that will affect every part of
hf-timestd. The transition is not urgent (jemalloc + restart cycle
buys us the operational headroom we need), so the right pace is
"correct over fast." Each phase should ship green CI + at least 3
days of clean dual-running before the next phase begins.

The schemas are the contract. Both backends generate from them. As
long as the schemas don't change, producer and consumer code can
trust that the data shape is stable across the migration.
