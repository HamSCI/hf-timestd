# Session 2026-05-20 — HDF5→SQLite Phase 3a flip

## Summary

Flipped `read_sqlite = true` on bee1 at ~11:43 UTC 2026-05-20. All four
consumer services (`timestd-l2-calibration`, `timestd-fusion`,
`timestd-physics`, `timestd-web-api`) restarted and now read from
SQLite. HDF5 writes remain on (dual-write); Phase 3b will turn HDF5
writes off.

Chrony post-flip: TSL3 still primary, reach=377, offset ≈ -2 µs,
system time within 1 µs of NTP — no regression from the pre-flip
baseline.

## What was done

### Config changes

- `/etc/hf-timestd/timestd-config.toml` (live) — added
  `read_sqlite = true` to `[storage]`; comment block updated to
  record Phase 3a. Backup at
  `…timestd-config.toml.bak-pre-readsqlite-20260520T113944Z`.
- `config/timestd-config.toml` (repo) — same flip; also set
  `write_sqlite = true` (was `false`) to mirror live. **Uncommitted**
  in working tree at session close; needs a small follow-up branch +
  commit.

### Service restarts (in order)

```
sudo systemctl restart timestd-l2-calibration
sudo systemctl restart timestd-fusion
sudo systemctl restart timestd-physics
sudo systemctl restart timestd-web-api
```

All four returned `is-active=active` within 3 s of restart. Journals
confirm `hf_timestd.io.sqlite_reader:Initialized ... SQLite reader`
for every channel/product. The legacy `fusion_loop_metrics` sub-phase
label `hdf5_read=…` is a cosmetic carry-over — the actual read goes
through `make_data_product_reader` and now dispatches to SQLite.

## Why the parity sweep showed 11–12 FAILs and we proceeded anyway

A 9-channel `parity_check_all.sh` sweep at 11:33 UTC showed
**70 checks → 50 OK / 9 SKIP / 0 PENDING / 11 FAIL** — a regression
from the 2026-05-19 11:43 UTC sweep (0 FAIL). The journal had rotated
overnight; the 06:00 UTC scheduled run had exited 1, cause not
recorded.

Per-failure investigation (`verify_sqlite_parity.py --verbose`)
showed all divergence is **HDF5-side row misalignment**:

- `L1_all_arrivals` (5 channels): for each `ts`, HDF5
  `processed_at` is the *next* row's timestamp; `detection_method`
  oscillates with SQLite, then flips on the next row.
- `L2_tick_phase` (4 channels): same shift; reader's own warnings
  prove it —
  `Index N out of bounds for field X (len=103699) ... n_meas=103702`
  — different columns have different lengths.
- `L3_dtec_timeseries` (`AGGREGATED`): HDF5 `epoch` is mis-joined
  with row timestamps and **jumps backwards**
  (1779276825.5 → 1779276796.5). SQLite `epoch` is perfectly
  monotonic.

Diagnosis: the **HDF5 non-atomic-append race** — exactly the bug
class the SQLite cutover exists to fix. SQLite is verified-correct;
yesterday's clean window was coincidence (drift small enough to fit
in fp tolerance). User authorised proceeding with Phase 3a despite
the FAILs on that basis.

## Phase 3b gating — important re-think needed

The runbook's "clean parity window before Phase 3b" criterion is
**unreachable** while `write_hdf5 = true`: the HDF5-race FAILs will
persist until HDF5 writes are off. A second parity sweep right after
the flip showed the same picture (12 FAILs, identical signatures —
confirming the flip introduced no SQLite-side regressions, just one
extra HDF5-race victim).

Proposed new gate for Phase 3b:

1. Phase 3a runs cleanly for a soak period (ideally a full day):
   - Chrony stays disciplined (TSL3 RMS offset stays sub-µs).
   - `timestd-fusion` continues producing graded D_clock without
     SQLite read errors in the journal.
   - `timestd-physics` continues writing dtec/tec/etc.
2. Optional: per-row sanity comparing consecutive SQLite rows for
   physical plausibility, or comparing SQLite row content against a
   producer-side checkpoint.
3. User signs off acknowledging parity will never clear until
   `write_hdf5 = false`.

## Health snapshot at session close

```
chronyc tracking:
Reference ID    : 54534C33 (TSL3)
Stratum         : 1
System time     : 0.000000608 seconds slow of NTP time
RMS offset      : 0.000000334 seconds
Skew            : 0.101 ppm

chronyc sources -v (relevant):
#* TSL3                          0   0   377     3  -2099ns[-2102ns] +/-   55us
#? TSL1                          0   4   124    48   +263us[ +267us] +/- 2000us
#? TSL2                          0   4   124    48   +365us[ +368us] +/-  600us
```

## Follow-ups

- **Commit repo config change** (Phase 3a flag):
  ```
  git checkout -b chore/sqlite-phase3a-flip
  git add config/timestd-config.toml
  git commit  # message: "config: Phase 3a flip — read_sqlite=true"
  ```
- **Reproduce / diagnose** the 06:00 UTC scheduled-parity exit-1 if it
  recurs (journal had rotated, so root cause not captured this round).
- **Phase 3b** — flip `write_hdf5 = false`, restart producers
  (`timestd-metrology@*`, the L2/L3 writers, fusion). After this the
  HDF5 race goes away.
- **Phase 4** — remove HDF5 reader/writer code, drop the pinned
  `h5py>=3.8.0,<3.16.0` dependency. The fusion h5py memory leak
  (`multi_broadcast_fusion.py:_malloc_trim`) only fully resolves
  once fusion holds a long-lived SQLite connection — Phase-4 task.
