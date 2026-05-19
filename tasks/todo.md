# HDF5 → SQLite migration — cutover in progress

## Context
Phase 1 (parallel writers) is live on bee1: `[storage] write_sqlite =
true` since 2026-05-15, `timestd-sqlite-parity` timer green. The
cutover is being driven to completion so the new TID L3 detector
(remediation P-H29) can be built SQLite-native rather than against
HDF5.

## Done
### Phase 2 reader foundation — committed 08097cc
- `io/sqlite_reader.py`: `SqliteDataProductReader` + `make_data_product_reader`.
- `[storage] read_sqlite` knob; `tests/unit/test_sqlite_reader.py` (21 tests).
- Live-verified vs HDF5 on bee1 `timestd.db`.

### Extend dual-write to all producers — this session
Converted every remaining `DataProductWriter` producer to
`make_data_product_writer` with `[storage]` config plumbed in
(`storage_config=None` default → HDF5-only, behaviour-preserving):
- [x] `l2_calibration_service.py` — `L2_timing_measurements`
- [x] `physics_fusion_service.py` — `L3_physics/tec/dtec/dtec_timeseries/dtec_diff`
- [x] `ionospheric_reanalysis.py` — `L3C_propagation_stats`, `L3_tec` (REANALYZED)
- [x] `scripts/live_vtec.py` — `L3_gnss_vtec`
With the deployed bee1 config (`write_sqlite=true`) these dual-write
once the services are redeployed + restarted.

## Remaining
1. Wire consumers (`multi_broadcast_fusion.py`, web-api services,
   `l2_calibration`/`physics_fusion`/`reanalysis` readers) through
   `make_data_product_reader`. Pure plumbing — factory defaults to
   HDF5, no behaviour change.
2. Deploy; let dual-write run; verify parity for the newly-covered
   products (extend `parity_check_all.sh` beyond the metrology set).
3. Flip `[storage] read_sqlite=true` after a clean parity window;
   then `write_hdf5=false` (Phase 3).
4. Phase 4: remove HDF5 writer/reader code paths + h5py.
5. Resume the metrology/physics remediation at P-H29 (TID detector),
   built SQLite-native via the factories.

## Review
(updated per commit)
