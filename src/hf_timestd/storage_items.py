"""Storage classes and the contract v0.9 storage verbs (sigmond 17.7).

hf-timestd declares what its data root holds, one class per top-level
entry, and answers `storage holdings|trim|final` for sigmond's ledger.
The classes come from the KPH1 tree of 2026-11-16.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

DAY = 86400.0
# A derived file written within this window may still be open: never a trim item.
LIVE_WINDOW = 2 * 3600.0
RAW_DIR = "raw_buffer"
RAW_POLICY = "give-up-oldest:raw"

# Where a station keeps sigmond's library; tests may point it elsewhere.
SIGMOND_LIB = "/opt/git/sigmond/sigmond/lib"

SINK_CLASSES: Dict[str, str] = {
    "state": "pinned",
    "authority_history.db": "pinned",
    "status": "pinned",
    "events-staging": "pinned",
    "raw_buffer": "raw",
    "upload": "sent",
}


_DAY_RE = re.compile(r"\d{8}")
_SQLITE_SUFFIXES = (".db", ".db-wal", ".db-shm", ".db-journal")


@dataclass(frozen=True)
class Item:
    """Same fields as sigmond's client_kit.Item; kept here so listing works
    without the kit."""
    path: str
    bytes: int
    cls: str
    mtime: float


def _is_sqlite(name: str) -> bool:
    return name.endswith(_SQLITE_SUFFIXES) or ".sqlite" in name


def class_of(name: str) -> str:
    """Class of one top-level entry; an unknown name counts as derived."""
    if name in SINK_CLASSES:
        return SINK_CLASSES[name]
    if name.startswith("authority_history.db"):
        return "pinned"
    return "derived"


def load_kit():
    """Import sigmond's client kit, or return None when it cannot load."""
    try:
        from sigmond.storage import client_kit
        return client_kit
    except ImportError:
        pass
    mod = sys.modules.get("sigmond")
    if mod is not None and getattr(mod, "__file__", None) is None:
        for k in [k for k in sys.modules if k == "sigmond" or k.startswith("sigmond.")]:
            del sys.modules[k]
    if SIGMOND_LIB not in sys.path:
        sys.path.append(SIGMOND_LIB)
    try:
        from sigmond.storage import client_kit
        return client_kit
    except ImportError:
        return None


def _tree(path: Path):
    """(bytes, newest mtime) of a file or a tree; a vanished file counts 0."""
    total, newest = 0, 0.0
    stack = [path]
    while stack:
        p = stack.pop()
        try:
            if p.is_dir() and not p.is_symlink():
                stack.extend(p.iterdir())
            else:
                st = p.lstat()
                total += st.st_size
                newest = max(newest, st.st_mtime)
        except OSError:
            continue
    return total, newest


def _archived(root: Path, day: str, ch: Path) -> bool:
    """This channel's day holds a chunk and the whole day sits on the archive
    drive."""
    from . import archiver
    try:
        if not any((ch / day).glob("*.bin.zst")):
            return False
        return bool(archiver.raw_day_archived(root, day))
    except Exception:
        return False


def raw_written_24h(data_root: Path, now: Optional[float] = None) -> int:
    """Bytes of raw_buffer files modified in the last 24 h (file mtimes, so a
    day directory that closed yesterday does not count whole)."""
    t = now if now is not None else time.time()
    total = 0
    for dirpath, _dn, files in os.walk(Path(data_root) / RAW_DIR):
        for f in files:
            try:
                st = (Path(dirpath) / f).lstat()
            except OSError:
                continue
            if t - DAY < st.st_mtime <= t + DAY:
                total += st.st_size
    return total


def written_per_day(data_root: Path, items: List[Item], now: Optional[float] = None) -> int:
    """Bytes written in the last 24 h: file-level for raw_buffer, item-level
    for everything else."""
    t = now if now is not None else time.time()
    raw_prefix = str(Path(data_root) / RAW_DIR) + os.sep
    other = sum(i.bytes for i in items
                if not i.path.startswith(raw_prefix) and t - DAY < i.mtime <= t + DAY)
    return other + raw_written_24h(data_root, t)


def list_items(data_root: Path, now: Optional[float] = None) -> List[Item]:
    """Every holding of the data root as a client_kit.Item.

    raw: one item per raw_buffer/<channel>/<YYYYMMDD> directory.
    pinned: one item per entry (listed so holdings count it; never deleted).
    sent, derived: one item per file.  SQLite files (live databases with
    their -wal/-shm/-journal) are never listed: trim must not take one apart.
    A raw day directory must be named YYYYMMDD.  A raw day older than
    yesterday (UTC) that the archive drive already holds in full is class
    `sent`: it goes first.  Files under any `state` directory, and derived
    files written in the last two hours, are listed as pinned.
    """
    t = now if now is not None else time.time()
    root = Path(data_root)
    recent = {_utc_day(t), _utc_day(t - DAY)}
    items: List[Item] = []
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return items
    for e in entries:
        cls = class_of(e.name)
        if cls == "raw":
            try:
                chans = sorted(c for c in e.iterdir() if c.is_dir())
            except OSError:
                continue
            for ch in chans:
                try:
                    days = sorted(d for d in ch.iterdir()
                                  if d.is_dir() and _DAY_RE.fullmatch(d.name))
                except OSError:
                    continue
                for d in days:
                    n, m = _tree(d)
                    kind = "raw"
                    if d.name not in recent and _archived(root, d.name, ch):
                        kind = "sent"
                    items.append(Item(str(d), n, kind, m))
        elif cls == "pinned":
            n, m = _tree(e)
            items.append(Item(str(e), n, "pinned", m))
        else:
            if e.is_dir() and not e.is_symlink():
                for dirpath, _dn, files in os.walk(e):
                    in_state = "state" in Path(dirpath).relative_to(e).parts
                    for f in files:
                        if _is_sqlite(f):
                            continue
                        p = Path(dirpath) / f
                        try:
                            st = p.lstat()
                        except OSError:
                            continue
                        kind = cls
                        if cls == "derived" and (
                                in_state or t - st.st_mtime < LIVE_WINDOW):
                            kind = "pinned"
                        items.append(Item(str(p), st.st_size, kind, st.st_mtime))
            else:
                n, m = _tree(e)
                items.append(Item(str(e), n, cls, m))
    return items


def sinks(data_root: Path, rate_bytes_per_day: int) -> List[dict]:
    root = Path(data_root)
    return [
        {"kind": "file", "target": str(root / "state"), "schema_ref": None,
         "retention_days": 0, "mb_per_day": 0, "classes": ["pinned"]},
        {"kind": "file", "target": str(root / RAW_DIR), "schema_ref": None,
         "retention_days": 0, "mb_per_day": rate_bytes_per_day / 1e6,
         "floor_bytes": 2 * int(rate_bytes_per_day),
         "final_policy": RAW_POLICY, "classes": ["raw", "sent"]},
        {"kind": "file", "target": str(root), "schema_ref": None,
         "retention_days": 0, "mb_per_day": 0,
         "classes": ["derived", "sent", "pinned"]},
    ]


def sinks_for(data_root: Path, now: Optional[float] = None) -> List[dict]:
    """sinks() with the raw rate measured from the tree."""
    t = now if now is not None else time.time()
    return sinks(data_root, raw_written_24h(data_root, t))


def _utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y%m%d")


def _is_raw_day_path(p: Path) -> bool:
    """raw_buffer/<channel>/<day>: the shape of a raw item, whatever its class."""
    return p.parent.parent.name == RAW_DIR


def delete_item(item, now: float) -> bool:
    """Delete one item.  False means it stays: pinned, a recent raw day, or
    a raw day the archive gate holds."""
    if item.cls == "pinned":
        return False
    p = Path(item.path)
    if _is_sqlite(p.name) and not _is_raw_day_path(p):
        return False
    if _is_raw_day_path(p):
        day = p.name
        if not _DAY_RE.fullmatch(day):
            return False
        recent = {_utc_day(now), _utc_day(now - DAY)}
        if day in recent:
            return False
        data_root = p.parent.parent.parent
        try:
            du = shutil.disk_usage(data_root)
            pct = 100.0 * du.used / du.total if du.total else 0.0
        except OSError:
            pct = 0.0
        from . import archiver
        if not archiver.raw_day_may_leave(data_root, day, pct):
            return False
        shutil.rmtree(p)
        return True
    if p.is_dir():
        return False
    try:
        if now - p.lstat().st_mtime < LIVE_WINDOW:
            return False
    except OSError:
        return False
    p.unlink()
    return True


def resolve_data_root(config: Optional[str] = None) -> Path:
    import toml
    path = Path(config or os.environ.get("TIMESTD_CONFIG")
                or "/etc/hf-timestd/timestd-config.toml")
    root = "/var/lib/timestd"
    try:
        rec = (toml.load(path).get("recorder") or {})
        root = rec.get("production_data_root", root)
        if rec.get("mode", "production") != "production":
            root = rec.get("test_data_root", root)
    except Exception:
        pass
    return Path(root)


def add_verbs(subparsers, data_root: Path) -> bool:
    """Wire `storage` onto the CLI; False when sigmond's kit cannot load."""
    kit = load_kit()
    if kit is None:
        return False
    rate = sinks_for(data_root)[1]
    kit.add_storage_verbs(
        subparsers,
        list_items=lambda: list_items(data_root),
        delete=lambda it: _delete_or_raise(it),
        floor_bytes=rate["floor_bytes"],
        final_policy=RAW_POLICY,
    )
    _file_level_holdings(subparsers, kit, data_root)
    return True


def _file_level_holdings(subparsers, kit, data_root: Path) -> None:
    """Replace the kit's `storage holdings` so bytes_written_per_day counts raw
    files by their own mtimes (the kit counts a whole day directory)."""
    def do_holdings(args) -> int:
        items = list_items(data_root)
        print(json.dumps({"bytes_by_class": kit.holdings(items), "estimated": False,
                          "bytes_written_per_day": written_per_day(data_root, items)}))
        return 0
    try:
        storage = subparsers.choices["storage"]
        for act in storage._actions:
            ch = getattr(act, "choices", None)
            if ch and "holdings" in ch:
                ch["holdings"].set_defaults(func=do_holdings)
    except Exception:
        pass


def _delete_or_raise(item):
    if not delete_item(item, time.time()):
        raise RuntimeError("held back (recent, pinned, or not yet archived)")
