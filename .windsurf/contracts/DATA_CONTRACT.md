# DATA CONTRACT — hf-timestd

**Version:** 1.0.0
**Last Updated:** 2026-02-23
**Status:** Active — evolves with implementation

---

## 1. Goal

Ensure all data flowing through the hf-timestd pipeline is **semantically correct, schema-validated, crash-safe, and traceable** from raw IQ capture through L3 science products.

### Performance Objectives

- **Zero data loss** in Phase 1 (Core Recorder): every RTP sample is archived or accounted for as a gap
- **Schema-validated writes** for all HDF5 products: every record passes its JSON schema and the 7 consistency rules (CR-1 through CR-7) defined in `data_dictionary.json`
- **Crash-safe HDF5**: open-write-close per measurement cycle; no dirty flags on unclean shutdown
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

### HDF5 Conventions

- **All** `h5py.File()` calls must use `locking=False`
- `os.environ["HDF5_USE_FILE_LOCKING"] = "FALSE"` must be set **before** `import h5py` in any module
- No SWMR mode — crash safety via open-write-close pattern
- Batch writes preferred over per-record writes (see tick_phase HDF5 heap corruption fix)
- Daily file naming: `{date}_{product_name}.h5`

### Consistency Rules (CR-1 through CR-7)

These are enforced at write time by `DataProductWriter`:

| Rule | Formula | Severity |
|------|---------|----------|
| CR-1 | `abs(clock_offset_ms - (raw_arrival_time_ms - propagation_delay_ms)) < 0.001` | ERROR |
| CR-2 | `tec_tecu > 0` | ERROR |
| CR-3 | `0 < tec_tecu <= 200` | WARNING |
| CR-4 | `vtec_tecu <= tec_tecu or isnan(vtec_tecu)` | WARNING |
| CR-5 | `if is_anchored then anchor_tec_tecu > 0 and isfinite(anchor_tec_tecu)` | ERROR |
| CR-6 | `propagation_delay_ms > 0` | ERROR |
| CR-7 | `raw_arrival_time_ms >= light_travel_time_ms` | ERROR |

### Raw Buffer Format

- Binary IQ: `.bin.zst` (zstd-compressed complex64, 1,440,000 samples/minute at 24 kHz)
- JSON sidecar: RTP timestamps, gap info, system time, quality metrics
- Sample count invariant: `24000 × 60 = 1,440,000` samples exactly per minute
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
- `vtec_tecu` is **55% valid but noise-dominated** — geometrically correct but sTEC unreliable (2026-02-24 audit)

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
- **Opening HDF5 without `locking=False`** — causes errno=11 file lock contention across services
- **Setting `HDF5_USE_FILE_LOCKING` after `import h5py`** — env var has no effect after library init
- **Per-record HDF5 writes for high-frequency products** (>10 writes/min) — causes heap corruption; must use batch writes
- **Truncating or overwriting daily HDF5 files on service restart** — files must be append-only
- **Dropping raw IQ samples without logging a gap** — violates Phase 1 completeness guarantee
- **Misinterpreting `clock_offset_ms` as a UTC clock offset** — it contains propagation model error (up to 76 ms for CHU)
- **Using `tof_kalman_ms` in any calculation** — field is deprecated and all NaN
- **Failing to bump schema version when changing field semantics**
