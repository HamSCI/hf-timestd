# HDF5 → SQLite cutover — status & flip runbook

## Code: done & committed (branch metrology-physics-review-remediation)
- `08097cc` reader foundation — `SqliteDataProductReader` + `make_data_product_reader`
- `4e9b0d0` all producers → `make_data_product_writer`
- `c13a2d8` all consumers → `make_data_product_reader`
- `5518968` parity tooling covers every product (+`--hdf5-data-dir`, exit-3 PENDING)
- repo `config/timestd-config.toml` gained a `[storage]` section (web-api)

The whole data path is backend-agnostic; behaviour is HDF5 until
`[storage]` flips.

## Live state on bee1 (via `scripts/parity_check_all.sh`)
- metrology 6 products — dual-write, parity OK
- L3 physics / tec / dtec / dtec_timeseries / dtec_diff — dual-write,
  parity OK (physics_fusion already redeployed with the new code)
- L3C propagation_stats, L3_tec REANALYZED — dual-write (reanalysis)
- **L2_timing_measurements — PENDING.** `timestd-l2-calibration` is
  still on pre-cutover code (last restart 2026-05-18 15:06); it is not
  dual-writing yet.

## The flip — blocked on one thing
Flipping `read_sqlite=true` now would point fusion at a missing
`L2_timing_measurements` table → fusion gets no L2 timing → chrony
TSL2 loses its discipline source. Do NOT flip until:

1. **Restart `timestd-l2-calibration`** — it then picks up the
   committed code (editable install) and dual-writes
   `L2_timing_measurements`.
2. **Parity window.** Run `bash scripts/parity_check_all.sh` (ideally
   `CHANNELS="CHU_3330 CHU_7850 CHU_14670 SHARED_2500 SHARED_5000
   SHARED_10000 SHARED_15000 WWV_20000 WWV_25000" bash
   scripts/parity_check_all.sh` for a full sweep). All products must
   read OK/SKIP — zero PENDING, zero FAIL — sustained over the chosen
   window (doc suggests ~3 days; at minimum several clean 6-hourly
   `timestd-sqlite-parity` runs).
3. **Flip reads:**
   - `/etc/hf-timestd/timestd-config.toml` `[storage]`: add
     `read_sqlite = true` (deployed config has write_* but not yet
     read_sqlite).
   - `config/timestd-config.toml` `[storage]`: `read_sqlite = true`
     (web-api).
   - Restart consumers: `timestd-fusion`, `timestd-l2-calibration`,
     physics-fusion, web-api.
4. Watch chrony (TSL2) + the parity timer for ~a day.
5. Phase 3b: `write_hdf5 = false`. Phase 4: delete HDF5 writer/reader
   paths + drop h5py.

## Next
Resume the metrology/physics remediation at P-H29 — the TID L3
detector, built SQLite-native via the factories.

## Review
SQLite cutover code is complete and committed; the flip is an
operational step gated on the l2-calibration restart + a parity
window (see above). No production behaviour changed by these commits.
