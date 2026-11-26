"""Compact timestd.db into an incremental-vacuum copy (contract 17.7 `storage compact`).

sigmond orchestrates; this module does the database work, in four verbs:

  --estimate  read-only: what a compaction needs and whether `prepare` would refuse
  --prepare   writers keep running: VACUUM INTO `<db>.compact`, note each table's
              max(rowid) in `<db>.compact.json`
  --holders   which pids (and systemd units) hold the database open
  --swap      every holder stopped: catch up the rows written since prepare,
              check the copy, and move it over the live file

Every verb that opens the database as root first drops to the file's owner, so
no root-owned -wal or -shm file appears and locks the writers out.  `holders`
opens no database; it must stay root to read other users' /proc fds, so it does
not drop.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import stat
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import storage_retention

MARGIN_BYTES = 2 << 30
FACTOR = 1.2

_UNIT = re.compile(r"([^/]+\.service)(?=/|$)")


def _copy(db: Path) -> Path:
    return Path(f"{db}.compact")


def _sidecar(db: Path) -> Path:
    return Path(f"{db}.compact.json")


def _kit():
    from . import storage_items
    kit = storage_items.load_kit()
    if kit is None:
        raise RuntimeError("sigmond client kit not available")
    return kit


def _unlink(path) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


# ---- holders --------------------------------------------------------------

def _unit_of(proc_root: Path, pid: str) -> Optional[str]:
    try:
        text = (proc_root / pid / "cgroup").read_text()
    except OSError:
        return None
    units = _UNIT.findall(text)
    return units[-1] if units else None


def holders(db: Path, proc_root: Path = Path("/proc")) -> List[Tuple[int, Optional[str]]]:
    """Every pid with an fd on db, db-wal or db-shm, with its .service unit."""
    db = Path(db)
    names = {str(p) for p in (db, Path(f"{db}-wal"), Path(f"{db}-shm"))}
    try:
        names |= {str(Path(n).resolve()) for n in list(names)}
    except OSError:
        pass
    found: List[Tuple[int, Optional[str]]] = []
    for ent in sorted(os.listdir(proc_root)):
        if not ent.isdigit() or int(ent) == os.getpid():
            continue
        fd_dir = proc_root / ent / "fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue  # the process left, or is not ours to read
        for fd in fds:
            try:
                target = os.readlink(fd_dir / fd)
            except OSError:
                continue
            if target in names:
                found.append((int(ent), _unit_of(proc_root, ent)))
                break
    return found


# ---- estimate / prepare ---------------------------------------------------

def _stats(db: Path) -> Tuple[int, int, int]:
    """(live_bytes, file_bytes, page_size) without creating any file.

    With no -wal beside it the file is read as immutable (no writer is open, so
    the header is current and SQLite creates no -shm).  With a -wal, a writer
    holds it; open read-only and let SQLite share the writer's -shm.
    """
    uri = f"file:{db}?mode=ro" if Path(f"{db}-wal").exists() else f"file:{db}?immutable=1"
    con = sqlite3.connect(uri, uri=True)
    try:
        pages = con.execute("PRAGMA page_count").fetchone()[0]
        size = con.execute("PRAGMA page_size").fetchone()[0]
        free = con.execute("PRAGMA freelist_count").fetchone()[0]
    finally:
        con.close()
    return (pages - free) * size, pages * size, size


def _need(live: int, factor: float, margin: int) -> int:
    return int(live * factor + margin)


def _check_room(live: int, free: int, factor: float, margin: int) -> Tuple[bool, str]:
    need = _need(live, factor, margin)
    if free >= need:
        return False, ""
    return True, (f"free space {free} bytes is below the {need} bytes a compaction "
                  f"needs (live data {live} x {factor} + {margin})")


def estimate(db: Path, *, margin_bytes: int = MARGIN_BYTES, factor: float = FACTOR) -> dict:
    """Read-only: the numbers `prepare` would judge, without writing a file."""
    db = Path(db)
    kit = _kit()
    kit.drop_to_owner(str(db))
    live, _file, _ps = _stats(db)
    free = _free_bytes(db.parent)
    refuse, reason = _check_room(live, free, factor, margin_bytes)
    return {"live_bytes": live, "expected_bytes": _need(live, factor, margin_bytes),
            "free_bytes": free, "would_refuse": refuse, "reason": reason,
            "refused": False}


def _answer_prepare(**kw) -> dict:
    base = {"live_bytes": 0, "file_bytes": 0, "compact_bytes": 0, "free_bytes": 0,
            "seconds": 0.0, "refused": False, "reason": ""}
    base.update(kw)
    return base


def _tables(con: sqlite3.Connection, schema: str = "main") -> List[str]:
    return [r[0] for r in con.execute(
        f"SELECT name FROM {schema}.sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def prepare(db: Path, *, margin_bytes: int = MARGIN_BYTES, factor: float = FACTOR,
            now: Optional[float] = None) -> dict:
    db = Path(db)
    t0 = time.monotonic()
    try:
        kit = _kit()
        kit.drop_to_owner(str(db))
        live, file_bytes, _ps = _stats(db)
        free = _free_bytes(db.parent)
    except Exception as exc:
        return _answer_prepare(refused=True, reason=str(exc))
    refuse, reason = _check_room(live, free, factor, margin_bytes)
    if refuse:
        return _answer_prepare(live_bytes=live, file_bytes=file_bytes,
                               free_bytes=free, refused=True, reason=reason)
    copy = _copy(db)
    for stale in (copy, Path(f"{copy}-wal"), Path(f"{copy}-shm"),
                  Path(f"{copy}-journal"), _sidecar(db)):
        _unlink(stale)
    try:
        con = sqlite3.connect(str(db), timeout=30)
        try:
            con.execute("PRAGMA auto_vacuum=2")
            con.execute(f"VACUUM INTO '{str(copy).replace(chr(39), chr(39) * 2)}'")
        finally:
            con.close()
        cc = sqlite3.connect(str(copy))
        try:
            marks: Dict[str, Optional[int]] = {}
            for t in _tables(cc):
                try:
                    marks[t] = cc.execute(
                        f"SELECT max(rowid) FROM {_quote(t)}").fetchone()[0] or 0
                except sqlite3.OperationalError:
                    raise RuntimeError(f"table {t} has no rowid; cannot catch it up")
        finally:
            cc.close()
        t = now if now is not None else time.time()
        _sidecar(db).write_text(json.dumps({
            "prepared_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)),
            "prepared_epoch": t, "max_rowid": marks}))
        compact_bytes = copy.stat().st_size
    except Exception as exc:
        for p in (copy, _sidecar(db)):
            _unlink(p)
        return _answer_prepare(live_bytes=live, file_bytes=file_bytes, free_bytes=free,
                               seconds=round(time.monotonic() - t0, 3),
                               refused=True, reason=f"compact copy failed: {exc}")
    return _answer_prepare(live_bytes=live, file_bytes=file_bytes,
                           compact_bytes=compact_bytes, free_bytes=free,
                           seconds=round(time.monotonic() - t0, 3))


# ---- swap -----------------------------------------------------------------

def _answer_swap(**kw) -> dict:
    base = {"before_bytes": 0, "after_bytes": 0, "rows_caught_up": {},
            "seconds": 0.0, "refused": False, "reason": ""}
    base.update(kw)
    return base


def _columns(con: sqlite3.Connection, schema: str, table: str) -> Tuple[List[str], Optional[str]]:
    """(column names, the INTEGER PRIMARY KEY alias of rowid or None)."""
    rows = con.execute(f"PRAGMA {schema}.table_info({_quote(table)})").fetchall()
    cols = [r[1] for r in rows]
    pks = [r for r in rows if r[5]]
    alias = pks[0][1] if len(pks) == 1 and (pks[0][2] or "").upper() == "INTEGER" else None
    return cols, alias


def _catch_up(con: sqlite3.Connection, marks: Dict[str, int]) -> Dict[str, int]:
    caught: Dict[str, int] = {}
    have = set(_tables(con, "main"))
    for t in _tables(con, "live"):
        if t not in have:
            sql = con.execute("SELECT sql FROM live.sqlite_master WHERE type='table' "
                              "AND name=?", (t,)).fetchone()[0]
            con.execute(sql)
            for (isql,) in con.execute("SELECT sql FROM live.sqlite_master WHERE "
                                       "type='index' AND tbl_name=? AND sql IS NOT NULL",
                                       (t,)).fetchall():
                con.execute(isql)
            floor = -1
        else:
            floor = marks.get(t, -1)
        live_cols, _ = _columns(con, "live", t)
        main_cols, alias = _columns(con, "main", t)
        common = [c for c in main_cols if c in live_cols and c != alias]
        names = ", ".join(_quote(c) for c in common)
        cur = con.execute(
            f"INSERT INTO main.{_quote(t)} (rowid, {names}) "
            f"SELECT rowid, {names} FROM live.{_quote(t)} WHERE rowid > ?", (floor,))
        caught[t] = cur.rowcount
    return caught


def swap(db: Path, *, max_age_s: int = 86400, proc_root: Path = Path("/proc"),
         now: Optional[float] = None) -> dict:
    db = Path(db)
    t0 = time.monotonic()
    copy, side = _copy(db), _sidecar(db)

    def refuse(reason: str, **kw) -> dict:
        return _answer_swap(refused=True, reason=reason,
                            seconds=round(time.monotonic() - t0, 3), **kw)

    held = holders(db, proc_root)
    if held:
        return refuse("database still held open by " + ", ".join(
            f"{pid} ({unit or 'no unit'})" for pid, unit in held))
    if not copy.is_file() or not side.is_file():
        return refuse("no compact copy and sidecar: run prepare first")
    t = now if now is not None else time.time()
    try:
        meta = json.loads(side.read_text())
        marks = dict(meta["max_rowid"])
    except (OSError, ValueError, KeyError, TypeError):
        return refuse("compact sidecar unreadable: run prepare again")
    age = t - min(copy.stat().st_mtime, side.stat().st_mtime)
    if age > max_age_s:
        return refuse(f"compact copy is {int(age)} s old; limit {max_age_s} s: run prepare again")
    try:
        kit = _kit()
        kit.drop_to_owner(str(db))
        old = db.stat()
        con = sqlite3.connect(str(copy), timeout=30)
        try:
            con.execute("ATTACH DATABASE ? AS live", (str(db),))
            was = con.execute("PRAGMA live.journal_mode").fetchall()[0][0]
            con.execute("BEGIN IMMEDIATE")
            caught = _catch_up(con, marks)
            con.execute("COMMIT")
            con.execute("DETACH DATABASE live")
            check = con.execute("PRAGMA quick_check").fetchall()[0][0]
            if check != "ok":
                return refuse(f"quick_check of the copy failed: {check}")
            if str(was).lower() == "wal":
                con.execute("PRAGMA journal_mode=WAL").fetchall()  # the header keeps it
        finally:
            con.close()  # the last close checkpoints and removes the copy's -wal
        for p in (Path(f"{copy}-wal"), Path(f"{copy}-shm"), Path(f"{copy}-journal")):
            _unlink(p)
        lv = sqlite3.connect(str(db), timeout=30)
        try:
            lv.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        finally:
            lv.close()
        _unlink(f"{db}-wal")
        _unlink(f"{db}-shm")
        os.replace(copy, db)
        os.chmod(db, stat.S_IMODE(old.st_mode))
        st = db.stat()
        if (st.st_uid, st.st_gid) != (old.st_uid, old.st_gid):
            os.chown(db, old.st_uid, old.st_gid)
        _unlink(side)
    except Exception as exc:
        return refuse(f"swap failed: {exc}")
    return _answer_swap(before_bytes=old.st_size, after_bytes=db.stat().st_size,
                        rows_caught_up=caught, seconds=round(time.monotonic() - t0, 3))


# ---- the verb -------------------------------------------------------------

def run_verb(data_root: Path, action: str, target: str) -> dict:
    rel = storage_retention.COMPACT_TARGETS.get(target)
    if rel is None:
        return {"refused": True, "reason": f"unknown compact target {target!r}"}
    db = Path(data_root) / rel
    if not db.is_file():
        return {"refused": True, "reason": f"database {db} not found"}
    try:
        if action == "holders":
            return {"refused": False, "reason": "",
                    "holders": [{"pid": p, "unit": u} for p, u in holders(db)]}
        if action == "estimate":
            return estimate(db)
        if action == "prepare":
            return prepare(db)
        if action == "swap":
            return swap(db)
    except Exception as exc:
        return {"refused": True, "reason": str(exc)}
    return {"refused": True, "reason": f"unknown action {action!r}"}
