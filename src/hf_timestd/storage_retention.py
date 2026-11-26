"""Row retention for hf-timestd's SQLite tables (contract 17.7 `storage expire`).

sigmond's hourly pass calls `hf-timestd storage expire --target T --days N`
once per target, each call in its own process.  A target names one table of
one database.  This module owns the table list, the days each table keeps, and
the delete.  sigmond never deletes a row itself.

Every mapped time column holds ISO-8601 text ("2026-11-26T04:00:00.123456Z"),
because the schemas in hamsci_dsp declare timestamp_utc and utc_published as
strings.  Such text sorts lexically against the kit's cut, which uses the 'T'
separator.  The kit refuses a column that holds anything but text.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

SCIENCE_DAYS = 30    # Michael, decision 3
TIMING_DAYS = 90     # Open question 1
AUTHORITY_DAYS = 30  # Open question 2
RETIRED_DAYS = 30    # Open question 8

TIMESTD_DB = "phase2/timestd.db"
AUTHORITY_DB = "authority_history.db"

# L3_gnss_vtec and the L3b_* tables belong to hamsci-physics: not listed here.
TABLES: Dict[str, int] = {
    "L2_tick_phase": SCIENCE_DAYS,
    "L1_all_arrivals": SCIENCE_DAYS,
    "L2_test_signal": SCIENCE_DAYS,
    "L1_metrology_measurements": TIMING_DAYS,
    "L2_timing_measurements": TIMING_DAYS,
    "L2_tick_timing": TIMING_DAYS,
    "L2_detection_attempts": TIMING_DAYS,
    "L3_fusion_timing": TIMING_DAYS,
    "DIAG_chrony_stats": TIMING_DAYS,
    "L2_decoder_comparison": RETIRED_DAYS,
    "L1_bcd_timecode": RETIRED_DAYS,
    "L1_tone_detections": RETIRED_DAYS,
    "L1_broadcast_measurements": RETIRED_DAYS,
    "L1_channel_observables": RETIRED_DAYS,
    "L2_physics": RETIRED_DAYS,
}

# target -> (database relative to the data root, table, time column)
TARGETS: Dict[str, Tuple[str, str, str]] = {
    **{f"timestd.{t}": (TIMESTD_DB, t, "timestamp_utc") for t in TABLES},
    "authority_history.authority_snapshot":
        (AUTHORITY_DB, "authority_snapshot", "utc_published"),
    # The whole file, for `storage compact`.  No table: expire refuses it.
    "timestd": (TIMESTD_DB, "", ""),
}


def days_for(target: str) -> int:
    if target.startswith("authority_history."):
        return AUTHORITY_DAYS
    return TABLES[target.split(".", 1)[1]]


def retention_sinks() -> List[dict]:
    return [{"kind": "service", "target": tgt, "schema_ref": "timestd:1",
             "retention_days": days_for(tgt), "mb_per_day": 0, "expires": True}
            for tgt, (_db, table, _col) in TARGETS.items() if table]


def _answer(deleted: int = 0, complete: bool = False, refused: bool = False,
            reason: str = "", freed_pages: int = 0) -> dict:
    return {"deleted": deleted, "complete": complete, "refused": refused,
            "reason": reason, "freed_pages": freed_pages, "unit": "rows"}


def expire_target(data_root: Path, target: str, days: int, max_seconds: int,
                  now: Optional[float] = None) -> dict:
    """Delete rows of one target older than `days`; vacuum with the time left."""
    from . import storage_items
    spec = TARGETS.get(target)
    if spec is None or not spec[1]:
        return _answer(refused=True, reason=f"unknown expire target {target!r}")
    kit = storage_items.load_kit()
    if kit is None:
        return _answer(refused=True, reason="sigmond client kit not available")
    rel, table, col = spec
    db = Path(data_root) / rel
    if not db.is_file():
        return _answer(refused=True, reason=f"database {db} not found")
    t = now if now is not None else time.time()
    deadline = time.monotonic() + max(0, int(max_seconds))
    # Become the file's owner before any connection: a root-owned -wal/-shm
    # would lock the writers out.
    try:
        kit.drop_to_owner(str(db))
        res = kit.prune_rows(str(db), table, col, kit.cutoff_iso(t, days),
                             deadline=deadline)
    except Exception as exc:
        return _answer(refused=True, reason=str(exc))
    freed = 0
    try:
        freed = kit.incremental_vacuum(str(db), deadline=deadline)
    except Exception as exc:
        return _answer(res.deleted, res.complete, reason=f"vacuum failed: {exc}")
    if res.deleted:
        storage_items._journal(
            f"storage expire: {target}: deleted {res.deleted} rows older than "
            f"{days} days, {freed} pages freed"
            + ("" if res.complete else f" ({res.reason or 'more remains'})"))
    return _answer(res.deleted, res.complete, reason=res.reason, freed_pages=freed)
