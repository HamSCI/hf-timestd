# HDF5 → SQLite migration — cutover in progress

## Context
Driving the HDF5→SQLite cutover to completion so the new TID L3
detector (remediation P-H29) can be built SQLite-native. Phase 1
(dual-write canary) has been live on bee1 since 2026-05-15.

## Done & committed (2026-05-19)
- `08097cc` — Phase 2 reader foundation: `io/sqlite_reader.py`
  (`SqliteDataProductReader` + `make_data_product_reader`),
  `[storage] read_sqlite` knob, `tests/unit/test_sqlite_reader.py`.
- `4e9b0d0` — every producer routed through `make_data_product_writer`
  (`l2_calibration`, `physics_fusion`, `ionospheric_reanalysis`,
  `live_vtec`); `storage_config` plumbed from `[storage]`.

## Done this session — consumer wiring
Every `DataProductReader` consumer routed through
`make_data_product_reader` with `storage_config` from `[storage]`
(default → HDF5, behaviour-preserving):
- [x] `multi_broadcast_fusion.py` — 5 reader sites (the h5py-leak hot path)
- [x] `physics_fusion_service.py` — 2 sites
- [x] `ionospheric_reanalysis.py` — 1 site
- [x] `l2_calibration_service.py` — 1 site (l1 readers)
- [x] web-api — `config.py` exposes `config.storage`; 10 files /
      20 sites converted (dashboard router + 9 services)

## Remaining
1. Deploy; let dual-write run; verify parity for the newly-covered
   products (extend `parity_check_all.sh` past the metrology set).
2. Flip `[storage] read_sqlite=true` after a clean parity window,
   then `write_hdf5=false` (Phase 3).
   - NOTE: web-api loads `config/timestd-config.toml` (the repo file,
     NOT `/etc/hf-timestd/...`), which currently has no `[storage]`
     section → `config.storage == {}`. Add `[storage]` there for the
     flip to reach web-api.
   - The fusion h5py leak is only actually fixed once fusion reads
     SQLite via a long-lived connection — currently it still builds a
     reader per cycle. Address in the flip/perf step.
3. Phase 4: remove HDF5 writer/reader paths + h5py.
4. Resume remediation at P-H29 (TID detector), built SQLite-native.

## Review
(updated per commit)
