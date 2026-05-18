# M-H21 — chrony SHM struct format vs segment size

## Finding
`chrony_shm.py`: the pack format `'@ii q i 4x q i iiii II iiiiiiii'` produces
92 bytes (confirmed via `struct.calcsize`), but the SHM segment is
`SHM_SIZE = 96` — the two were independent constants that could drift. The
header docstring described a stale 56-byte layout (and had the clock/receive
semantics backwards plus a since-removed bogus pad).

## Fix
- New module constant `SHM_STRUCT_FORMAT = '@ii q i 4x q i iiii II iiiiiiii 4x'`
  — the trailing `4x` is chrony's C struct trailing padding (time_t forces
  8-byte struct alignment). The format now totals exactly 96 bytes = chrony's
  real `struct shmTime`.
- `SHM_SIZE = struct.calcsize(SHM_STRUCT_FORMAT)` — derived, cannot drift.
- `struct.pack` in `update()` uses the constant.
- Header docstring rewritten to the true 96-byte layout (dummy[8] + pad,
  clock = reference time, receive = system time).

Deviation from the finding: it prescribed `dummy[10]`, but dummy[10] from
offset 60 is 100 bytes, not 96. chrony's struct has `int dummy[8]`; its C
sizeof is 96 (92 fields + 4 trailing pad). The fix represents that pad as
`4x` — correct, and arithmetically consistent with the 96-byte segment.

Scope: `src/hf_timestd/core/chrony_shm.py`. No behavioural change — the
packed record now fills all 96 bytes (the last 4, which chrony ignores, are
explicit zero pad rather than left untouched).

## Tasks — done
- [x] `SHM_STRUCT_FORMAT` constant; `SHM_SIZE` derived via `struct.calcsize`
- [x] `struct.pack` uses the constant; inline-comment "92"->"96"
- [x] Rewrite the header docstring (full 96-byte layout)
- [x] Tests: 2 added to `tests/unit/test_chrony_shm.py`
- [x] Full suite run

## Review
- Files: `chrony_shm.py` (+34 -19); `tests/unit/test_chrony_shm.py` (+21).
- New tests (`TestStructLayout`): `SHM_SIZE == struct.calcsize(SHM_STRUCT_FORMAT)`
  (cannot drift); both equal 96 (chrony's `struct shmTime`). Verified the old
  format calcsized to 92, the new one to 96.
- Full repo suite: 1626 passed, 9 subtests passed (1624 + 2 new). One
  pre-existing unrelated `test_l2_clickhouse_wire` failure, deselected.
