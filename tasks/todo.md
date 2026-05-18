# M-H22 — L2 startup seed swallows parse errors silently

## Finding
`L2CalibrationService._seed_last_processed` reads the newest L2 output file
per channel to resume from the last calibrated minute. A bare
`except Exception: continue` swallowed every per-file error. If a channel's
files all failed to parse, `last_ts` stayed 0, the cursor stayed at 0, and the
next cycle silently reprocessed the entire lookback window — a 24-hour
reprocessing storm with no logged cause.

## Fix
- Per-file: on a parse exception, log at WARNING (file name, channel, the
  exception) before falling back to the next-oldest file.
- Channel-level: if a channel ends up with no readable file (`last_ts <= 0`)
  *because* of parse failures, log a WARNING naming the consequence (cursor
  stays at 0 → full-window reprocess). The exception is still caught broadly
  (`# noqa: BLE001`) — any file problem is a fallback, not a crash — it is
  just no longer silent.

Scope: `src/hf_timestd/core/l2_calibration_service.py` — `_seed_last_processed`
only. +16 -1.

## Tasks — done
- [x] Log per-file parse failures at WARNING; track `parse_failures`
- [x] Log the channel-level "unseedable → reprocess storm" consequence
- [x] Tests: `tests/test_l2_seed_logging.py` (2)
- [x] Full suite run

## Review
- Files: `l2_calibration_service.py` (+16 -1); new
  `tests/test_l2_seed_logging.py` (2 tests).
- New suite 2/2: a corrupt L2 file produces both the per-file and the
  channel-level WARNING (and the cursor is visibly left at 0); a valid L2 file
  seeds the cursor with no WARNING.
- Full repo suite: 1624 passed, 9 subtests passed (1622 + 2 new). One
  pre-existing unrelated `test_l2_clickhouse_wire` failure, deselected.
