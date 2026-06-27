# Session 2026-05-20 — HDF5→SQLite Phase 3b flip

## Summary

Flipped `write_hdf5 = false` on bee1 at ~13:35 UTC 2026-05-20. SQLite
is now the sole writer of all hf-timestd data products. HDF5 readers
remain in code so rollback (set `write_hdf5 = true` and restart
producers) is still possible.

Along the way, two producers were found to be bypassing the
`storage_config` dispatch — they kept writing HDF5 after the flip
until I patched them.

## What was done

### Config flips

- `/etc/hf-timestd/timestd-config.toml` (live): `write_hdf5 = false`.
  Backup at `…timestd-config.toml.bak-pre-phase3b-20260520T133505Z`.
  Comment updated to record Phase 3b.
- `config/timestd-config.toml` (repo): same flip, with updated comment
  block. Committed in `e251111`.

### Producer restarts (in order)

```
sudo systemctl restart 'timestd-metrology@*.service'   # 9 channels
sudo systemctl restart timestd-l2-calibration
sudo systemctl restart timestd-fusion
sudo systemctl restart timestd-physics
sudo systemctl restart timestd-web-api
```

All 14 services returned `is-active=active` within seconds.
Chrony briefly lost TSL3 reach during the restart sequence and
promoted the LAN `time` server to primary, then TSL3 re-acquired
within ~1 min.

### Two missed Phase-2 conversions surfaced

After the config flip, journal scrutiny showed two producers still
writing HDF5:

1. **`MultiBroadcastFusion.__init__` (fusion_timing writer):**
   `multi_broadcast_fusion.py:790` instantiated `DataProductWriter`
   directly instead of going through `make_data_product_writer`, so
   it ignored `storage_config`. Switched to the factory, threaded
   `storage_config=self._storage_config`, renamed the now-misnamed
   attributes (`hdf5_fusion_writer` → `fusion_writer`,
   `enable_hdf5_fusion_writes` → `enable_fusion_writes`), and inlined
   `_write_fused_result_hdf5` since the backend dispatch is now inside
   the factory.

2. **`ChronyStatsCollector` (chrony_stats diagnostic):**
   `chrony_stats.py` writes HDF5 directly via raw `h5py`, with no
   SQLite counterpart and no schema registry entry — it's a diagnostic
   product outside the schema system. Gated the HDF5 write on
   `storage_config['write_hdf5']` so Phase 3b stops it. Phase 4
   follow-up: convert chrony_stats to a proper schema-based product so
   SQLite parity is available before flipping the gate back on.

Both fixes committed as `e251111` and fast-forwarded into main. The
fusion service was restarted a second time (~13:44 UTC) to deploy
both. Verified live:

- `L3_fusion_timing` SQLite table created on first init; rows
  accumulating at ~7/min (one per fusion cycle).
- `phase2/fusion/chrony_stats_20260520.h5` mtime frozen post-restart
  despite the `[CHRONY]` collector continuing to log every 60 s.
- No HDF5 files modified in the 5 min before close-out.
- SQLite writes flowing in 8 tables, 4549 rows in a 2-min sample.

## Health snapshot at session close (13:46 UTC)

```
chronyc tracking:
Reference ID    : 54534C33 (TSL3)
System time     : 0.000000778 seconds slow of NTP time
RMS offset      : 0.000000232 seconds
Skew            : 0.106 ppm

chronyc sources (relevant):
#* TSL3                          0   0   377     2   -687ns[ -860ns] +/-   55us
#? TSL1                          0   4    11    27    +97us[  +99us] +/- 2000us
#? TSL2                          0   4    11    27   +174us[ +176us] +/-  600us
^+ time                          1   2   377     0   +716ns[ +518ns] +/-  122us
```

TSL1/TSL2 reach=11 because they're rebuilding the reach register
post-restart; offsets are sub-200 µs which is fine. TSL3 is the
chrony primary with sub-µs precision.

## Side issues addressed during the session

- **`/run/hf-timestd` bind-mount loss after fusion restarts.**
  `timestd-fusion` and `timestd-core-recorder` both declared
  `RuntimeDirectory=hf-timestd` with the default
  `RuntimeDirectoryPreserve=no`. Any stop on either service let
  systemd remove `/run/hf-timestd` host-side, orphaning the bind mount
  in the other service's `ProtectSystem=strict` namespace and causing
  `EROFS` on writes. Fixed pre-Phase-3b in commit `453a691`
  (`RuntimeDirectoryPreserve=yes` on both units) plus live drop-ins.
  Without this fix, Phase 3b's producer restarts would have re-broken
  core-recorder.

- **Fusion bootstrap stuck in SEARCHING.** Pre-existing typo at
  `multi_broadcast_fusion.py:2065` (`'model_confidence'` instead of
  `'confidence'`) caused bootstrap to reject every detection.
  Independent of the cutover; fixed in `b466ec6`. Bootstrap is now in
  `CORRELATING` with CHU confirming, but stuck on single-station
  confirmations (an upstream CHU-vs-WWV bias issue — separate
  follow-up).

## Phase 3b runbook deviations

Runbook said: *"Phase 3b — after Phase 3a runs clean for at least one
full parity window (ideally a full day): set `write_hdf5 = false`."*

What we did instead: flipped after ~2 h of Phase 3a soak because the
"clean parity window" gate is unreachable while `write_hdf5 = true`
(parity FAILs were entirely HDF5-side row-misalignment from the
non-atomic-append race; only fix is removing HDF5 writes). User
explicitly authorised proceeding without the long soak.

## Follow-ups

- **Phase 4** — remove HDF5 reader/writer code, drop the pinned
  `h5py>=3.8.0,<3.16.0` dependency. The fusion h5py memory leak
  (`multi_broadcast_fusion.py:_malloc_trim`) only fully resolves once
  fusion holds a long-lived SQLite connection.
- **chrony_stats schema conversion** — currently HDF5-gated-off but
  no SQLite alternative. Phase 4 work.
- **Bootstrap CORRELATING single-station stall** (task #8) — CHU
  carries a ~60 ms bias relative to WWV/WWVH that the per-broadcast
  Kalman doesn't persist across restarts. Not blocking; chrony uses
  TSL3 anyway.
