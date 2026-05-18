# DATA CONTRACT — hf-timestd

**Version:** 1.1.0
**Last Updated:** 2026-05-17
**Status:** Active — evolves with implementation
**Last refresh:** 2026-05-17 — reconciled against code. Factual drift corrected in place; clauses the code does not yet meet are listed in §5 Known Deviations. See `docs/CODE_REVIEW_2026-05-17_METROLOGY_PHYSICS.md`.

---

## 1. Goal

Ensure all data flowing through the hf-timestd pipeline is **semantically correct, schema-validated, crash-safe, and traceable** from raw IQ capture through L3 science products.

### Performance Objectives

- **Zero data loss** in Phase 1 (Core Recorder): every RTP sample is archived or accounted for as a gap
- **Schema-validated writes** for all data products: every record is validated against its JSON schema at write time. The 7 consistency rules (CR-1 through CR-7) are *defined* in `data_dictionary.json` but are not currently enforced in the write path — see §5 D1
- **Crash-safe HDF5**: SWMR model — writers keep the daily file open and flush per write; `h5clear -s` resets stale SWMR flags on the next open after an unclean shutdown
- **Latency**: Phase 2 metrology products written within 90 seconds of raw buffer availability; Phase 3 fusion within 120 seconds

### Deliverable Products

| Level | Product | Schema | HDF5 Path | Cadence |
|-------|---------|--------|-----------|---------|
| L0 | Binary IQ + JSON sidecar | N/A (binary) | `raw_buffer/{CHANNEL}/{YYYYMMDD}/` | Per minute |
| L1 | Metrology measurements | `l1_metrology_measurements_v1.json` | `phase2/{CHANNEL}/metrology/` | Per minute |
| L2 | Tick timing (D_clock, Doppler, SNR) | `l2_tick_timing_v1.json` | `phase2/{CHANNEL}/tick_timing/` | Per minute |
| L2 | Calibrated timing | `l2_timing_measurements_v1.json` | `phase2/{CHANNEL}/clock_offset/` | Per minute |
| L2 | Detection attempts | `l2_detection_attempts_v1.json` | `phase2/{CHANNEL}/detection_attempts/` | Per minute |
| L2 | Tick phase | `l2_tick_phase_v1.json` | `phase2/{CHANNEL}/tick_phase/` | ~55/min/station |
| L2 | CHU FSK decode | `l2_chu_fsk_v1.json` | `phase2/{CHANNEL}/chu_fsk/` | Per minute |
| L3 | Fusion timing | `l3_fusion_timing_v1.json` | `phase2/fusion/` | Every 8 seconds |
| L3 | TEC estimates | `l3_tec_v1.json` | `phase2/science/tec/` | Per minute |
| L3 | dTEC rate | `l3_dtec_v1.json` | `phase2/science/dtec/` | Per minute |
| L3 | dTEC time series | `l3_dtec_timeseries_v1.json` | `phase2/science/dtec_timeseries/` | ~55/min/station |
| L3 | Differential dTEC | `l3_dtec_diff_v1.json` | `phase2/science/dtec_diff/` | Per minute |

### Verification Steps

1. `python -c "from hf_timestd.schemas import check_field; print(check_field('clock_offset_ms'))"` — confirms data dictionary is loadable and field is defined
2. `scripts/verify_pipeline.sh` — confirms data freshness across all pipeline stages
3. HDF5 row counts grow monotonically per day file (no truncation on restart)
4. JSON sidecar `gap_count` and `completeness_percent` fields are populated for every raw buffer file

---

## 2. Constraints

### Data Dictionary Authority

- `src/hf_timestd/schemas/data_dictionary.json` is the **single authoritative definition** of every observable and derived quantity
- All schema field descriptions, code comments, and documentation must be consistent with the data dictionary
- Before using any field in a calculation, verify its entry in the data dictionary
- The `check_field()` API must be used to validate field semantics programmatically

### Schema Registry

- All HDF5 products must have a corresponding JSON schema in `src/hf_timestd/schemas/`
- Schema files define field names, types, units, and version metadata
- The `DataProductWriter` validates records against schemas at write time
- Schema versions must be bumped when fields are added, removed, or semantics change

### Storage Backends

An HDF5 → SQLite migration is in progress. Producers obtain a writer via `make_data_product_writer(...)` and use one API regardless of backend. Three configurations, driven by the `[storage]` config section (`write_hdf5`, `write_sqlite`):

- `write_hdf5=true, write_sqlite=false` → `DataProductWriter` (HDF5; current default)
- `write_hdf5=false, write_sqlite=true` → `SqliteDataProductWriter`
- `write_hdf5=true, write_sqlite=true` → `DualWriter` wrapping both (canary)

`DualWriter` validates a row once against the JSON schema, then dispatches the already-validated row to both backends so they never see different inputs. The HDF5 conventions and file structure below describe the HDF5 backend specifically.

### HDF5 Conventions

- **SWMR model** (v6.10+): writers create the daily file with `libver='latest'`, pre-create all datasets, set `swmr_mode=True`, and keep the handle open — flushing after each write; readers open with `swmr=True, libver='latest'`
- On every open of an existing file, `h5clear -s` is run to reset stale SWMR consistency flags left by an unclean shutdown
- Batch writes preferred over per-record writes (see tick_phase HDF5 heap corruption fix)
- Daily file naming: `{date}_{product_name}.h5`
- A legacy `locking=False` open remains in `multi_broadcast_fusion.py`; the SWMR model does not require it

### Consistency Rules (CR-1 through CR-7)

These are **defined** in `data_dictionary.json`. As of 2026-05-17 they are documented but not enforced in the `io/` write path — the writers perform JSON-schema validation only (see §5 D1):

| Rule | Formula | Severity |
|------|---------|----------|
| CR-1 | `abs(clock_offset_ms - (raw_arrival_time_ms - propagation_delay_ms)) < 0.001` | ERROR |
| CR-2 | `tec_tecu > 0` | WARNING |
| CR-3 | `0 < tec_tecu <= 200` | WARNING |
| CR-4 | `vtec_tecu <= tec_tecu or isnan(vtec_tecu)` | WARNING |
| CR-5 | `if is_anchored then anchor_tec_tecu > 0 and isfinite(anchor_tec_tecu)` | ERROR |
| CR-6 | `propagation_delay_ms > 0` | ERROR |
| CR-7 | `raw_arrival_time_ms >= light_travel_time_ms` | ERROR |

**CR-2 is intentionally WARNING, not ERROR (settled 2026-05-17).** True TEC is non-negative, but a negative `tec_tecu` *estimate* is a normal noisy realization — group-delay TEC is below the noise floor for WWV/WWVH/CHU/BPM (see the `tec_tecu` `noise_floor_analysis` in `data_dictionary.json`). Rejecting or clamping records on TEC sign censors the estimator and biases every downstream aggregate (mean TEC, climatology) high, worst when true TEC is genuinely low. Negative `tec_tecu` must be **retained as-is** and flagged MARGINAL; aggregation must use the value with its uncertainty, never by discarding. Once `tec_uncertainty_tecu` exists (Physics Contract / review P-H2), significance should be judged value-vs-uncertainty rather than by sign.

### Raw Buffer Format

- Binary IQ: `.bin.zst` (zstd-compressed complex64)
- Chunk duration: configurable `file_duration_sec` (default 600s = 10 min; legacy 60s supported)
- JSON sidecar: RTP timestamps, gap info, system time, quality metrics, `file_duration_sec`
- Per-minute sample count invariant: `24000 × 60 = 1,440,000` samples per minute within each chunk
- Per-chunk sample count: `24000 × file_duration_sec` samples per chunk file
- Gaps filled with zeros (maintains timing alignment)

### Schema Lookup (DataProductRegistry)

- `DataProductRegistry.get_schema(level, product)` → full JSON schema with field definitions
- `DataProductRegistry.get_field_type(level, product, field)` → `{'type', 'format', 'description'}`
- **Readers MUST use the registry to discover field types** — do not assume format from naming conventions
- The `_utc` suffix does NOT guarantee ISO 8601: `timestamp_utc` is a string, `minute_boundary_utc` is an integer epoch
- The canonical data dictionary (`data_dictionary.json`) has a `structural_fields` section documenting these conventions

### Field Semantics (Critical Pitfalls)

- `clock_offset_ms` is a **timing residual** (arrival − expected_propagation_delay), NOT a clock offset in the metrological sense
- `raw_arrival_time_ms` is a **model-dependent reconstruction**, not a raw observable
- `minute_boundary_utc` is an **integer Unix epoch** (seconds since 1970), NOT an ISO string
- `tof_kalman_ms` is **deprecated** (all NaN) — marked `deprecated=true` in L2 schema
- `tec_tecu` is **below noise floor** in production — use `dtec_rate_tecu_per_s` instead
- `vtec_tecu` is **all NaN in production** — it derives from group-delay TEC, which is below the noise floor (consistent with the `data_dictionary.json` entry and the Physics Contract)

---

## 3. Format

### HDF5 File Structure

```
phase2/{CHANNEL}/metrology/{date}_metrology_measurements.h5
    ├── minute_boundary_utc    (int64, Unix epoch seconds)
    ├── station                (string)
    ├── frequency_hz           (float64)
    ├── raw_toa_ms             (float64)
    ├── snr_db                 (float64)
    └── ... (per schema)
```

### JSON Sidecar Structure

```json
{
    "start_rtp_timestamp": 164520840,
    "start_system_time": 1769306160.066,
    "sample_rate": 24000,
    "center_frequency_hz": 10000000,
    "gap_count": 0,
    "completeness_percent": 100.0,
    "timing_snapshots": []
}
```

### Timestamp Conventions

- All UTC timestamps in HDF5: ISO 8601 string `YYYY-MM-DDTHH:MM:SSZ` via `strftime('%Y-%m-%dT%H:%M:%SZ')`
- Never use `isoformat() + 'Z'` (causes double-timezone suffix)
- RTP timestamps are 32-bit unsigned counters; UTC derived via `gps_time_unix + (rtp_ts - rtp_timesnap) / sample_rate`

### Logging

- Schema validation failures logged at WARNING level with field name, expected vs actual value
- CR rule violations logged at ERROR level with rule ID
- HDF5 write operations logged at DEBUG level with row count and file path

---

## 4. Failure Conditions

- **Writing a record that violates any CR-ERROR rule** — record must be rejected, not silently written
- **Using a field name not defined in the data dictionary** in a new schema or calculation
- **Creating datasets after `swmr_mode=True` is set** — SWMR forbids structural changes once the flag is on; all datasets must be pre-created
- **Failing to run `h5clear -s` when opening a file left dirty by an unclean shutdown** — readers then cannot open it
- **Per-record HDF5 writes for high-frequency products** (>10 writes/min) — causes heap corruption; must use batch writes
- **Truncating or overwriting daily HDF5 files on service restart** — files must be append-only
- **Dropping raw IQ samples without logging a gap** — violates Phase 1 completeness guarantee
- **Misinterpreting `clock_offset_ms` as a UTC clock offset** — it contains propagation model error (up to 76 ms for CHU)
- **Using `tof_kalman_ms` in any calculation** — field is deprecated and all NaN
- **Failing to bump schema version when changing field semantics**

---

## 5. Known Deviations (current code vs. this contract)

Recorded 2026-05-17 from the code review (`docs/CODE_REVIEW_2026-05-17_METROLOGY_PHYSICS.md`). These are points where the **current code does not yet meet the contract above**. The contract states the intended design; this section is the honest gap list. Each item should be resolved by either fixing the code or — if the clause itself is wrong — amending the clause.

| # | Contract clause | Current code reality | Notes |
|---|-----------------|----------------------|-------|
| D1 | §1/§2 — the 7 consistency rules (CR-1…CR-7) are enforced at write time by `DataProductWriter` | The `io/` writers (`hdf5_writer.py`, `sqlite_writer.py`, `dual_writer.py`) validate records against JSON schemas only; none reference `consistency_rules`/CR. The rules exist as data in `data_dictionary.json` but are not wired into the write path | Either implement a CR checker in the writer/`DualWriter`, or amend §1/§2 to "defined, advisory" |
| D2 | §4 — no per-record HDF5 writes for high-frequency products (>10/min) | `metrology_service.py` still writes `all_arrivals` and `detection_attempts` one record at a time; only `tick_phase` uses batch writes | Code review ref M-M18 |
| D3 | §2 CR-2 / Physics Contract §2 — negative TEC handling | **Resolved 2026-05-17 (contract + code).** CR-2 downgraded ERROR→WARNING (flag, never reject); Physics Contract "force to zero" clause removed; `tec_estimator.py`, `physics_fusion_service.py`, and `ionospheric_reanalysis.py` updated to retain negative / out-of-range TEC (flagged MARGINAL, confidence 0). Tomography guards its own input at the consumption site. Tests `test_negative_slope_retained` updated | Review ref P-H5 |
