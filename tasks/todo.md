# P-C1 / P-C2 — physics-service coupling & ionogram-calibration crash

## Outcome — both already remediated
Both Critical physics findings were already fixed by commit `c9117b3`
("Tier-1 remediation"), verified by inspection + git blame:

**P-C1** — `systemd/timestd-physics.service` now has `Wants=` (not `Requires=`)
on `timestd-l2-calibration.service`, `Type=simple` (not `notify`), and the
`ExecStartPre` chown is scoped to the physics-owned `phase2/fusion` and
`phase2/science` subdirs (not the whole `phase2` tree). All three carry
explanatory comments referencing `METROLOGY_PHYSICS_SPLIT.md`. A systemd unit
file is config, not unit-testable in any meaningful way — verified by
inspection, no test.

**P-C2** — `IonosphericModel.update_calibration_from_ionogram` now calls
`get_layer_heights(timestamp=, latitude=, longitude=)` by keyword (real
signature), reads `base_heights.hmF2` (correct field), computes `loc_key`
inline, and stores into `self._calibration_data` (the real attribute). The
three nonexistent-identifier references and the swapped args are gone.

## Task — done
The P-C2 finding explicitly asked for "a unit test exercising the path" — that
was the only thing missing. Added `tests/test_ionospheric_ionogram_calibration.py`
(2 tests). No source change.

## Review
- Files: new `tests/test_ionospheric_ionogram_calibration.py` (2 tests).
- New tests: `update_calibration_from_ionogram` runs without crashing and
  stores a correct anchor (offset = measured − model-predicted hmF2, clamped
  ±150 km; `get_calibration_stats` reflects it); an extreme measured height
  clamps the offset to ±150 km. Verified live: a first call produced
  predicted hmF2 212.1 km, offset +107.9 km, one stored entry.
- Full repo suite: 1630 passed, 9 subtests passed (1628 + 2 new). One
  pre-existing unrelated `test_l2_clickhouse_wire` failure, deselected.
