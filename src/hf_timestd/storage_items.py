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
}

# Entries of the data root that another client registers as its own sink.
# hf-timestd neither counts nor trims them: the storage design forbids a client
# to delete another's files, and only the owner knows what has been sent.
# upload/ holds hamsci-physics's GRAPE spool (its contract.py sink); listed
# here as `sent`, it lost packaged days to hf-timestd's trim on B4, 2026-11-22/23.
FOREIGN_ENTRIES: Dict[str, str] = {
    "upload": "hamsci-physics",
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
    """Class of one top-level entry; an unknown name counts as derived.
    "foreign" marks an entry another client owns (FOREIGN_ENTRIES)."""
    if name in FOREIGN_ENTRIES:
        return "foreign"
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


STATUS_FRESH_S = 3600.0
STATUS_FUTURE_S = 60.0


def _archived_days(now: float, status_path: Optional[Path] = None):
    """Predicate (channel, day) -> bool from the archiver's own status file,
    read once.  Never touches the archive drive: listing must answer within
    sigmond's 10 s, and an away or asleep drive stalls any stat of it.

    A day counts as archived only when the latest archiver run was fresh, ok,
    saw the drive, and left nothing behind: behind == 0 (closed chunks not yet
    copied or past the failure cap), failures == 0, invalid == 0 (chunks the
    copy refused), orphans == 0 (chunks that never got a sidecar, which
    `behind` does not count), and the channel's drive range covers the day.
    Deleting stays gated by archiver.raw_day_may_leave.
    """
    from . import archiver
    never = lambda ch, day: False  # noqa: E731
    try:
        st = json.loads(Path(status_path or archiver.STATUS_PATH).read_text())
        run = datetime.strptime(st["run_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
        if not (-STATUS_FUTURE_S <= now - run <= STATUS_FRESH_S):
            return never
        if (st.get("state") != "ok" or st.get("present") is not True
                or st.get("behind") != 0 or st.get("failures") != 0
                or st.get("invalid", 0) != 0 or st.get("orphans", 0) != 0
                or st.get("dry_run")):
            return never
        chans = st["channels"]
        if not isinstance(chans, dict):
            return never
    except (OSError, ValueError, KeyError, TypeError):
        return never

    def archived(ch: str, day: str) -> bool:
        c = chans.get(ch)
        try:
            return bool(c) and c["oldest"] <= day <= c["newest"]
        except (KeyError, TypeError):
            return False
    return archived


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


def list_items(data_root: Path, now: Optional[float] = None,
               status_path: Optional[Path] = None) -> List[Item]:
    """Every holding of the data root as a client_kit.Item.

    raw: one item per raw_buffer/<channel>/<YYYYMMDD> directory.
    pinned: one item per entry (listed so holdings count it; never deleted).
    sent, derived: one item per file.  SQLite files (live databases with
    their -wal/-shm/-journal) are never listed: trim must not take one apart.
    A raw day directory must be named YYYYMMDD.  A raw day older than
    yesterday (UTC) that the archiver's status file reports copied is class
    `sent`: it goes first.  Files under any `state` directory, and derived
    files written in the last two hours, are listed as pinned.
    """
    t = now if now is not None else time.time()
    root = Path(data_root)
    recent = {_utc_day(t), _utc_day(t - DAY)}
    archived = _archived_days(t, status_path)
    items: List[Item] = []
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return items
    for e in entries:
        cls = class_of(e.name)
        if cls == "foreign":
            continue
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
                    if d.name not in recent and archived(ch.name, d.name):
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
         "classes": ["derived", "pinned"]},
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


def _journal(line: str) -> None:
    """One line to the journal.  sigmond keeps neither stdout nor stderr of a
    trim, so a deletion logged anywhere else leaves no trace."""
    try:
        import syslog
        syslog.openlog("hf-timestd-storage", 0, syslog.LOG_DAEMON)
        syslog.syslog(syslog.LOG_NOTICE, line)
    except Exception:
        pass


def _deleted(item) -> bool:
    _journal(f"storage trim: deleted {item.path} ({item.bytes} B, {item.cls})")
    return True


def delete_item(item, now: float) -> bool:
    """Delete one item, and say so in the journal.  False means it stays:
    pinned, a recent raw day, or a raw day the archive gate holds."""
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
        return _deleted(item)
    if p.is_dir():
        return False
    try:
        if now - p.lstat().st_mtime < LIVE_WINDOW:
            return False
    except OSError:
        return False
    p.unlink()
    return _deleted(item)


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
        apply_policy=lambda policy, fit: apply_final(kit, data_root, policy, fit,
                                                     rate["floor_bytes"]),
    )
    _file_level_holdings(subparsers, kit, data_root)
    return True


def apply_final(kit, data_root: Path, policy: str, fit_bytes: int, floor_bytes: int,
                now: Optional[float] = None) -> dict:
    """Only give-up-oldest:raw takes the sent-then-raw order; any other
    give-up-oldest:<class> follows the kit's generic path."""
    if policy == RAW_POLICY:
        return final_raw(data_root, fit_bytes, now)
    t = now if now is not None else time.time()
    items = list_items(data_root, t)
    kind, _, cls = policy.partition(":")
    if kind != "give-up-oldest" or cls not in kit.CLASSES or cls == "pinned":
        return kit._answer(items, True, f"policy {policy} is not handled by this client")
    chosen = kit.choose_trim(items, fit_bytes, floor_bytes, final=True, classes=[cls])
    left, errors = kit._delete_all(chosen, lambda it: _delete_or_raise(it), items)
    return kit._answer(left, False,
                       ("could not delete " + ", ".join(errors)) if errors else "")


def final_raw(data_root: Path, fit_bytes: int, now: Optional[float] = None) -> dict:
    """give-up-oldest:raw with the spec's order: archived raw days (`sent`)
    first, then the rest, oldest first.  May go below the floor; delete_item
    still holds today, yesterday and the archive gate."""
    t = now if now is not None else time.time()
    items = list_items(data_root, t)
    remaining = sum(i.bytes for i in items)
    days = [i for i in items if i.cls in ("raw", "sent") and _is_raw_day_path(Path(i.path))]
    chosen = sorted((i for i in days if i.cls == "sent"), key=lambda i: i.mtime) + \
        sorted((i for i in days if i.cls == "raw"), key=lambda i: i.mtime)
    gone, errors = set(), []
    for it in chosen:
        if remaining <= fit_bytes:
            break
        try:
            ok = delete_item(it, t)
        except Exception as exc:  # a failed delete leaves the item held
            errors.append(f"{it.path}: {exc}")
            continue
        if ok:
            gone.add(it.path)
            remaining -= it.bytes
        else:
            errors.append(f"{it.path}: held back (recent, pinned, or not yet archived)")
    left = [i for i in items if i.path not in gone]
    by: Dict[str, int] = {}
    for i in left:
        by[i.cls] = by.get(i.cls, 0) + i.bytes
    return {"held_bytes": sum(i.bytes for i in left), "bytes_by_class": by,
            "refused": False,
            "reason": ("could not delete " + ", ".join(errors)) if errors else ""}


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
