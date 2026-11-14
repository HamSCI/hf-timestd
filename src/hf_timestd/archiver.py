"""The archive-drive archiver: copy closed raw IQ to the drive sigmond attached.

Design: sigmond docs/superpowers/specs/2026-11-12-archive-drive-design.md,
part 2.  sigmond owns the drive (`smd archive add` writes
/etc/sigmond/archive.toml and mounts it); this module owns the data.  It copies
every closed ``raw_buffer/<CH>/<YYYYMMDD>/<start>.bin.zst`` with its sidecar to
``<mount>/timestd/<station>/raw_buffer/...``, converting complex64 to F16 at
the codec's fixed scale, verifies every file before it counts, and keeps the
drive below its high-water mark by deleting the oldest whole day.

Recording never depends on it: it reads the SSD buffer, never writes there,
and a drive that errors ends the run.
"""
from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import socket
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

import numpy as np

from .core import iq_codec

logger = logging.getLogger("hf_timestd.archiver")

ARCHIVE_TOML = Path("/etc/sigmond/archive.toml")
DATA_ROOT = Path("/var/lib/timestd")
STATUS_PATH = Path("/var/lib/timestd/archive-status.json")
LOCK_PATH = Path("/run/timestd-archive/lock")   # RuntimeDirectory= of the unit
MIN_AGE_S = 120
ZSTD_LEVEL = 3                       # the recorder's (BinaryArchiveConfig.compression_level)
MAX_FAILURES = 3
_DAY_RE = re.compile(r"^(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])$")
_DRIVE_ERRNOS = {errno.EIO, errno.ENODEV, errno.ENXIO, errno.ENOSPC, errno.EROFS,
                 errno.ENOENT, errno.ESTALE}


class ArchiveIOError(Exception):
    """The drive failed (unplugged, I/O error, full): the run ends."""


class VerifyError(Exception):
    """A written file did not match its source; it was removed."""


# ---------------------------------------------------------------------------
# selecting
# ---------------------------------------------------------------------------

def closed_files(raw_root: Path, now: float, min_age_s: float = MIN_AGE_S
                 ) -> Iterator[Tuple[Path, Path]]:
    """(bin, sidecar) for every closed chunk, oldest day first, then channel,
    then start.  Closed: the sidecar exists and both files are unmodified for
    ``min_age_s``."""
    found: List[Tuple[str, str, str, Path, Path]] = []
    try:
        channels = sorted(p for p in raw_root.iterdir() if p.is_dir())
    except OSError:
        return
    for ch in channels:
        try:
            days = [d for d in ch.iterdir() if d.is_dir() and _DAY_RE.match(d.name)]
        except OSError:
            continue
        for d in days:
            try:
                bins = list(d.glob("*.bin.zst"))
            except OSError:
                continue
            for b in bins:
                j = b.with_name(b.name[:-len(".bin.zst")] + ".json")
                try:
                    if (now - b.stat().st_mtime < min_age_s
                            or now - j.stat().st_mtime < min_age_s):
                        continue
                except OSError:
                    continue          # no sidecar yet (or vanished): not closed
                found.append((d.name, ch.name, b.name, b, j))
    for _, _, _, b, j in sorted(found, key=lambda t: (t[0], t[1], t[2])):
        yield b, j


# ---------------------------------------------------------------------------
# verifying
# ---------------------------------------------------------------------------

def f16_within_rounding(src: np.ndarray, dec: np.ndarray) -> bool:
    """Every finite component of ``dec`` is within F16 rounding of ``src``,
    in scaled units: |d - s| <= 2**-10 * |s| + 2**-24.  NaN and infinity must
    sit at the same positions (and infinities share a sign)."""
    s = np.ascontiguousarray(src, dtype=np.complex64).view(np.float32).astype(np.float64)
    d = np.ascontiguousarray(dec, dtype=np.complex64).view(np.float32).astype(np.float64)
    if s.shape != d.shape:
        return False
    s *= iq_codec.F16_SCALE
    d *= iq_codec.F16_SCALE
    if not np.array_equal(np.isnan(s), np.isnan(d)):
        return False
    s_inf, d_inf = np.isinf(s), np.isinf(d)
    if not np.array_equal(s_inf, d_inf) or not np.array_equal(s[s_inf], d[d_inf]):
        return False
    fin = np.isfinite(s)
    return bool(np.all(np.abs(d[fin] - s[fin]) <= 2.0 ** -10 * np.abs(s[fin]) + 2.0 ** -24))


# ---------------------------------------------------------------------------
# copying one file
# ---------------------------------------------------------------------------

def _drive_error(exc: OSError) -> ArchiveIOError:
    return ArchiveIOError(f"{type(exc).__name__}: {exc}")


def _write_part(dst_dir: Path, name: str, data: bytes) -> Path:
    """Write ``.<name>.part`` in dst_dir and fsync it."""
    part = dst_dir / f".{name}.part"
    with open(part, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    return part


def _read_back(p: Path) -> bytes:
    """Read a just-written file from the drive, not from the page cache, so
    the check covers the drive and its bridge too."""
    fd = os.open(p, os.O_RDONLY)
    try:
        try:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        except (AttributeError, OSError):
            pass
        chunks = []
        while True:
            b = os.read(fd, 1 << 20)
            if not b:
                return b"".join(chunks)
            chunks.append(b)
    finally:
        os.close(fd)


def _fsync_dir(d: Path) -> None:
    fd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _unlink_quiet(*paths: Path) -> None:
    for p in paths:
        try:
            p.unlink()
        except OSError:
            pass


def copy_one(src_bin: Path, src_json: Path, dst_dir: Path, archive_dtype: str) -> str:
    """Copy one chunk to ``dst_dir``.  Returns 'copied' (byte for byte),
    'converted' (complex64 -> F16), 'kept_f32' (F16 would overflow),
    'skipped' (already there) or 'invalid' (the source cannot be read).  Raises
    ArchiveIOError on a drive error and VerifyError on a mismatch; neither
    leaves a partial file behind."""
    import zstandard as zstd

    dst_bin, dst_json = dst_dir / src_bin.name, dst_dir / src_json.name
    try:
        if dst_bin.exists() and dst_json.exists():
            return "skipped"
    except OSError as exc:
        raise _drive_error(exc)
    try:
        meta = json.loads(src_json.read_text())
        if not isinstance(meta, dict):
            raise ValueError("sidecar is not an object")
        blob = src_bin.read_bytes()
    except (OSError, ValueError) as exc:
        logger.warning("archiver: skipping %s: %s", src_bin, exc)
        return "invalid"
    src_dtype = meta.get("dtype", iq_codec.DTYPE_F32)

    outcome, out_blob, out_meta, samples = "copied", blob, meta, None
    if archive_dtype == "f16" and src_dtype == iq_codec.DTYPE_F32:
        try:
            raw = zstd.ZstdDecompressor().decompress(blob)
            samples = iq_codec.decode_iq(raw, iq_codec.DTYPE_F32, 1.0)
        except (zstd.ZstdError, ValueError) as exc:
            logger.warning("archiver: skipping %s: cannot decode: %s", src_bin, exc)
            return "invalid"
        enc = iq_codec.encode_f16(samples)
        if enc is None:
            outcome = "kept_f32"
        else:
            outcome = "converted"
            out_blob = zstd.ZstdCompressor(level=ZSTD_LEVEL).compress(enc)
            out_meta = dict(meta)          # f16_check travels with the data (mjh, 2026-11-13)
            out_meta["dtype"] = iq_codec.DTYPE_F16
            out_meta["scale"] = int(iq_codec.F16_SCALE)
            out_meta["converted_from"] = iq_codec.DTYPE_F32

    part_bin = part_json = None
    try:
        dst_dir.mkdir(parents=True, exist_ok=True)
        part_bin = _write_part(dst_dir, src_bin.name, out_blob)
        written = _read_back(part_bin)
        if outcome == "converted":
            try:
                dec = iq_codec.decode_iq(zstd.ZstdDecompressor().decompress(written),
                                         iq_codec.DTYPE_F16, iq_codec.F16_SCALE)
            except (zstd.ZstdError, ValueError) as exc:
                _unlink_quiet(part_bin)
                raise VerifyError(f"{src_bin}: F16 copy does not decode: {exc}")
            if dec.size != samples.size or not f16_within_rounding(samples, dec):
                _unlink_quiet(part_bin)
                raise VerifyError(f"{src_bin}: F16 copy is not within rounding of the source")
        elif hashlib.sha256(written).digest() != hashlib.sha256(blob).digest():
            _unlink_quiet(part_bin)
            raise VerifyError(f"{src_bin}: copy differs from the source")
        part_json = _write_part(dst_dir, src_json.name,
                                (json.dumps(out_meta, indent=2) + "\n").encode())
        os.replace(part_bin, dst_bin)
        os.replace(part_json, dst_json)            # sidecar last: it marks the file archived
        _fsync_dir(dst_dir)
    except VerifyError:
        raise
    except OSError as exc:
        _unlink_quiet(*(p for p in (part_bin, part_json) if p is not None))
        if dst_bin.exists() and not dst_json.exists():
            _unlink_quiet(dst_bin)
        raise _drive_error(exc)
    return outcome


# ---------------------------------------------------------------------------
# the drive: gate, usage, making room
# ---------------------------------------------------------------------------

def _load_config(path: Path) -> Optional[dict]:
    import tomllib
    try:
        return tomllib.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _is_mount(p: Path) -> bool:
    # Under x-systemd.automount this stat is what mounts a replugged drive.
    return os.path.ismount(p)


def _uuid_matches(mount: Path, uuid: str) -> bool:
    """The filesystem at ``mount`` is the one with this UUID: its device is the
    device /dev/disk/by-uuid/<uuid> names.  An automount point with the drive
    away has autofs's device, so it never matches."""
    try:
        return os.stat(f"/dev/disk/by-uuid/{uuid}").st_rdev == os.stat(mount).st_dev
    except OSError:
        return False


def _station() -> str:
    return socket.gethostname().split(".")[0]


def _usage(mount: Path) -> Tuple[int, int, float]:
    """(used, capacity, percent) as df's Use%: blocks in use over blocks a
    non-root user could use."""
    st = os.statvfs(mount)
    used = (st.f_blocks - st.f_bfree) * st.f_frsize
    cap = used + st.f_bavail * st.f_frsize
    return used, cap, (100.0 * used / cap if cap else 100.0)


def gate(cfg: Optional[dict]) -> Optional[str]:
    """Why the archiver must not touch the drive now, or None."""
    if not cfg:
        return "no archive drive configured"
    mount, uuid = cfg.get("mount"), cfg.get("uuid")
    if not mount or not uuid:
        return "archive.toml lacks mount or uuid"
    if not _is_mount(Path(mount)):
        return f"{mount} is not mounted (drive away)"
    if not _uuid_matches(Path(mount), str(uuid)):
        return f"the filesystem at {mount} is not UUID {uuid}"
    return None


def _protected(today: date) -> set:
    return {(today - timedelta(days=k)).strftime("%Y%m%d") for k in (0, 1)}


def _tree_bytes(path: Path) -> int:
    total = 0
    for f in [path, *path.rglob("*")]:
        try:
            total += f.lstat().st_blocks * 512
        except OSError:
            pass
    return total


def _within(p: Path, mount: Path) -> bool:
    try:
        Path(os.path.realpath(p)).relative_to(os.path.realpath(mount))
        return True
    except ValueError:
        return False


def _candidates(drive_root: Path, mount: Path, evict_first: List[Path], today: date
                ) -> List[Tuple[str, List[Path]]]:
    """(label, dirs) in eviction order: each evict_first folder's YYYYMMDD
    children, oldest first across all of them (the KPH draft: another
    program's days go before ours); then our own days, oldest first, each
    day across every channel at once.  Today and yesterday never appear."""
    keep = _protected(today)
    foreign: List[Tuple[str, str, Path]] = []
    for root in evict_first:
        root = Path(root)
        if not _within(root, mount):
            logger.warning("archiver: evict_first %s is not on the drive; ignored", root)
            continue
        try:
            for d in root.iterdir():
                if d.is_dir() and not d.is_symlink() and _DAY_RE.match(d.name) and d.name not in keep:
                    foreign.append((d.name, str(d), d))
        except OSError as exc:
            logger.warning("archiver: cannot list %s: %s", root, exc)
    own: Dict[str, List[Path]] = {}
    raw = drive_root / "raw_buffer"
    try:
        for ch in raw.iterdir():
            if ch.is_dir():
                for d in ch.iterdir():
                    if d.is_dir() and _DAY_RE.match(d.name) and d.name not in keep:
                        own.setdefault(d.name, []).append(d)
    except OSError:
        pass
    out = [(label, [d]) for _, label, d in sorted(foreign)]
    out += [(day, sorted(own[day])) for day in sorted(own)]
    return out


def make_room(drive_root: Path, mount: Path, high_water_pct: float, evict_first: List[Path],
              today: date, usage=None, dry_run: bool = False) -> dict:
    """Delete the oldest days until the drive is below ``high_water_pct``.
    Returns {'evicted': [labels], 'bytes_freed', 'percent', 'full'}; full is
    True when it is still at or above the mark with nothing left to delete."""
    usage = usage or _usage
    used, cap, pct = usage(mount)
    res = {"evicted": [], "bytes_freed": 0, "percent": pct, "full": False, "skipped": []}
    if pct < high_water_pct:
        return res
    sim = 0
    for label, dirs in _candidates(drive_root, mount, evict_first, today):
        size = sum(_tree_bytes(d) for d in dirs)
        if dry_run:
            sim += size
            pct = 100.0 * (used - sim) / cap if cap else 100.0
            logger.info("archiver: [dry run] would delete %s (%.2f GB)", label, size / 1e9)
        else:
            try:
                for d in dirs:
                    shutil.rmtree(d)
            except PermissionError as exc:
                # Not ours to delete (another user's files, or a hand run as
                # root left root-owned ones): skip it, never stall archiving.
                logger.warning("archiver: cannot delete %s (%s); skipping it", label, exc)
                res["skipped"].append(label)
                continue
            # any other OSError is a drive error: the run ends
            logger.info("archiver: deleted %s (%.2f GB) to stay below %s%%",
                        label, size / 1e9, high_water_pct)
            _, _, pct = usage(mount)
        res["evicted"].append(label)
        res["bytes_freed"] += size
        if pct < high_water_pct:
            break
    res["percent"] = pct
    res["full"] = pct >= high_water_pct
    return res


# ---------------------------------------------------------------------------
# one run
# ---------------------------------------------------------------------------

def _held(drive_root: Path, dtype: str, previous: dict, today: date) -> Dict[str, dict]:
    """Per channel: oldest, newest, days, bytes.  Day sizes older than
    yesterday are taken from the previous status (they no longer change), so
    a full drive is not walked every ten minutes."""
    cache = previous.get("day_bytes", {}) if isinstance(previous, dict) else {}
    keep = _protected(today)
    out: Dict[str, dict] = {}
    new_cache: Dict[str, int] = {}
    raw = drive_root / "raw_buffer"
    try:
        chans = sorted(c for c in raw.iterdir() if c.is_dir())
    except OSError:
        chans = []
    for ch in chans:
        days = sorted(d.name for d in ch.iterdir() if d.is_dir() and _DAY_RE.match(d.name))
        if not days:
            continue
        total = 0
        for day in days:
            key = f"{ch.name}/{day}"
            n = cache.get(key) if day not in keep else None
            if n is None:
                n = _tree_bytes(ch / day)
            if day not in keep:
                new_cache[key] = n
            total += n
        out[ch.name] = {"oldest": days[0], "newest": days[-1], "days": len(days),
                        "bytes": total, "dtype": dtype}
    previous["day_bytes_new"] = new_cache
    return out


def _orphans(raw_root: Path, now: float, min_age_s: float = 86400) -> int:
    """Chunks with no sidecar a day after they were written: never archived."""
    n = 0
    try:
        for b in raw_root.glob("*/*/*.bin.zst"):
            j = b.with_name(b.name[:-len(".bin.zst")] + ".json")
            try:
                if not j.exists() and now - b.stat().st_mtime > min_age_s:
                    n += 1
            except OSError:
                pass
    except OSError:
        pass
    return n


def _bytes_per_day(day_bytes: Dict[str, int]) -> Optional[float]:
    per_day: Dict[str, int] = {}
    for key, n in day_bytes.items():
        per_day[key.split("/")[1]] = per_day.get(key.split("/")[1], 0) + n
    return (sum(per_day.values()) / len(per_day)) if per_day else None


def _write_json(path: Path, doc: dict) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def run(config_path: Path = ARCHIVE_TOML, data_root: Path = DATA_ROOT,
        status_path: Path = STATUS_PATH, lock_path: Path = LOCK_PATH,
        today: Optional[date] = None, now: Optional[float] = None,
        dry_run: bool = False) -> dict:
    """One archiver pass.  Returns the status document (also written to
    ``status_path`` unless the pass was a dry run or found another running)."""
    today = today or datetime.now(timezone.utc).date()
    now = time.time() if now is None else now
    st = {"run_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
          "present": False, "state": "unconfigured", "channels": {}, "copied": 0,
          "converted": 0, "bytes_copied": 0, "behind": 0, "kept_f32": 0, "failures": 0,
          "last_error": None, "bytes_per_day": None, "evicted": [], "dry_run": dry_run,
          "percent": None, "invalid": 0, "orphans": 0, "evict_refused": 0}
    if os.geteuid() == 0 and not dry_run:
        st["state"] = "error"
        st["last_error"] = ("refusing to run as root: files it writes on the drive would be "
                            "root's (run as timestd: systemctl start timestd-archive)")
        return st
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock = open(lock_path, "a")
    except OSError as exc:
        st["state"], st["last_error"] = "error", f"cannot open the lock {lock_path}: {exc}"
        return st
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        st["state"] = "busy"
        return st
    try:
        try:
            previous = json.loads(status_path.read_text())
        except (OSError, ValueError):
            previous = {}
        cfg = _load_config(config_path)
        why = gate(cfg)
        if why:
            st["state"] = "unconfigured" if not cfg else "absent"
            st["last_error"] = None if not cfg else why
            if cfg:
                logger.info("archiver: %s; nothing to do", why)
            if not dry_run:
                _write_json(status_path, st)
            return st
        mount = Path(cfg["mount"])
        hw = float(cfg.get("high_water_pct", 90))
        dtype_cfg = str(cfg.get("archive_dtype", "f16"))
        evict_first = [Path(p) for p in (cfg.get("evict_first") or [])]
        drive_root = mount / "timestd" / _station()
        st["present"], st["state"] = True, "ok"
        raw_root = data_root / "raw_buffer"
        try:
            if not dry_run:
                drive_root.mkdir(parents=True, exist_ok=True)
                chans = sorted(c.name for c in raw_root.iterdir() if c.is_dir()) \
                    if raw_root.is_dir() else []
                _write_json(drive_root / "station.json", {
                    "station": _station(), "channels": chans,
                    "reporter": os.environ.get("SIGMOND_REPORTER_ID", ""),
                    "grid": os.environ.get("SIGMOND_GRID", ""),
                    "written_utc": st["run_utc"]})
            room = make_room(drive_root, mount, hw, evict_first, today, usage=_usage,
                             dry_run=dry_run)
            st["evicted"] = room["evicted"]
            refused = list(room.get("skipped", []))
            if dry_run:
                st["would_evict"] = room["evicted"]
            failures_path = drive_root / ".archive-failures.json"
            try:
                failures = json.loads(failures_path.read_text())
            except (OSError, ValueError):
                failures = {}
            pending = list(closed_files(raw_root, now))
            behind = 0
            if room["full"]:
                st["state"] = "full"
                behind = len([1 for b, _ in pending
                              if not (drive_root / b.relative_to(data_root)).exists()])
                pending = []
            for i, (b, j) in enumerate(pending):
                rel = str(b.relative_to(data_root))
                dst_dir = drive_root / b.parent.relative_to(data_root)
                if failures.get(rel, 0) >= MAX_FAILURES:
                    behind += 1
                    continue
                if dry_run:
                    if not (dst_dir / b.name).exists():
                        behind += 1
                    continue
                if i and i % 50 == 0:
                    room = make_room(drive_root, mount, hw, evict_first, today, usage=_usage)
                    st["evicted"] += room["evicted"]
                    refused += room.get("skipped", [])
                    if room["full"]:
                        st["state"] = "full"
                        behind += len(pending) - i
                        break
                try:
                    outcome = copy_one(b, j, dst_dir, dtype_cfg)
                except VerifyError as exc:
                    failures[rel] = failures.get(rel, 0) + 1
                    logger.error("archiver: %s (failure %d of %d)", exc, failures[rel], MAX_FAILURES)
                    behind += 1
                    continue
                except ArchiveIOError as exc:
                    st["state"], st["last_error"] = "error", str(exc)
                    logger.error("archiver: drive error, ending this run: %s", exc)
                    behind += len(pending) - i
                    break
                if outcome == "invalid":
                    st["invalid"] += 1
                    continue
                if outcome != "skipped":
                    st["copied"] += 1
                    st["bytes_copied"] += (dst_dir / b.name).stat().st_size
                    st["converted"] += outcome == "converted"
                    st["kept_f32"] += outcome == "kept_f32"
            st["behind"] = behind
            st["orphans"] = _orphans(raw_root, now)
            st["failures"] = sum(1 for n in failures.values() if n >= MAX_FAILURES)
            if not dry_run:
                if failures:
                    _write_json(failures_path, failures)
                if st["state"] == "ok":
                    room = make_room(drive_root, mount, hw, evict_first, today, usage=_usage)
                    st["evicted"] += room["evicted"]
                    refused += room.get("skipped", [])
                    if room["full"]:
                        st["state"] = "full"
            st["evict_refused"] = len(set(refused))
            if refused and st["state"] == "full" and not st["last_error"]:
                st["last_error"] = (f"cannot delete {len(set(refused))} day folder(s) on the drive "
                                    f"(permission denied), e.g. {sorted(set(refused))[0]}: make them "
                                    f"writable by timestd")
            st["percent"] = _usage(mount)[2]
            st["channels"] = _held(drive_root, iq_codec.DTYPE_F16 if dtype_cfg == "f16"
                                   else iq_codec.DTYPE_F32, previous, today)
            st["day_bytes"] = previous.pop("day_bytes_new", {})
            st["bytes_per_day"] = _bytes_per_day(st["day_bytes"]) or _bytes_per_day(
                {f"{c}/{v['newest']}": v["bytes"] // max(v["days"], 1)
                 for c, v in st["channels"].items()})
        except OSError as exc:
            st["state"], st["last_error"] = "error", f"{type(exc).__name__}: {exc}"
            logger.error("archiver: drive error, ending this run: %s", exc)
        if not dry_run:
            _write_json(status_path, st)
        logger.info("archiver: %s; copied %d (%d to F16, %d kept F32), %d behind, deleted %s",
                    st["state"], st["copied"], st["converted"], st["kept_f32"], st["behind"],
                    st["evicted"] or "nothing")
        return st
    finally:
        lock.close()


# ---------------------------------------------------------------------------
# export: an archived window back to F32 (design part 3)
# ---------------------------------------------------------------------------

def export_f32(src_root: Path, dst: Path, channel: str, start_utc: datetime,
               end_utc: datetime) -> int:
    """Write F32 ``.bin.zst`` chunks and sidecars, in the original layout, for
    every chunk of ``channel`` under ``src_root/raw_buffer`` whose start lies
    in [start_utc, end_utc).  ``src_root`` is a drive's ``timestd/<station>``
    folder (or any data root).  Returns the number of chunks written."""
    import zstandard as zstd
    t0, t1 = start_utc.timestamp(), end_utc.timestamp()
    n = 0
    base = Path(src_root) / "raw_buffer"
    ch_dir = (base if base.is_dir() else Path(src_root)) / channel
    day = start_utc.astimezone(timezone.utc).date()
    while day <= end_utc.astimezone(timezone.utc).date():
        d = ch_dir / day.strftime("%Y%m%d")
        for b in sorted(d.glob("*.bin.zst")) if d.is_dir() else []:
            stem = b.name[:-len(".bin.zst")]
            try:
                start = float(stem)
            except ValueError:
                continue
            if not (t0 <= start < t1):
                continue
            meta = json.loads(b.with_name(stem + ".json").read_text())
            raw = zstd.ZstdDecompressor().decompress(b.read_bytes())
            x = iq_codec.decode_iq(raw, meta.get("dtype", iq_codec.DTYPE_F32),
                                   float(meta.get("scale", 1.0)))
            out_dir = Path(dst) / channel / d.name
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / b.name).write_bytes(zstd.ZstdCompressor(level=ZSTD_LEVEL).compress(
                np.ascontiguousarray(x, dtype=np.complex64).tobytes()))
            out_meta = {k: v for k, v in meta.items() if k not in ("scale", "converted_from")}
            out_meta["dtype"] = iq_codec.DTYPE_F32
            (out_dir / f"{stem}.json").write_text(json.dumps(out_meta, indent=2) + "\n")
            n += 1
        day += timedelta(days=1)
    return n


# ---------------------------------------------------------------------------
# archive-before-evict: the SSD keeps a raw day until the drive holds it
# ---------------------------------------------------------------------------
# KPH2, 2026-11-14: the resource guardian evicted raw_buffer/20261112 while the
# archiver was blocked, and 835 of its 864 chunks were lost.  With an archive
# drive configured, every SSD eviction of raw IQ (resource guardian, quota
# manager) asks raw_day_may_leave() first.

EVICT_EMERGENCY_PERCENT = 95.0     # past this the recorder's own room wins


def raw_day_archived(data_root: Path, day: str, config_path: Optional[Path] = None) -> bool:
    """Every closed chunk of raw_buffer/*/<day>/ on the SSD has its sidecar on
    the archive drive.  False when the drive is away or not configured."""
    cfg = _load_config(Path(config_path or ARCHIVE_TOML))
    if gate(cfg):
        return False
    drive = Path(cfg["mount"]) / "timestd" / _station() / "raw_buffer"
    raw = Path(data_root) / "raw_buffer"
    try:
        for ch in raw.iterdir():
            d = ch / day
            if not d.is_dir():
                continue
            for b in d.glob("*.bin.zst"):
                stem = b.name[:-len(".bin.zst")]
                if not (drive / ch.name / day / f"{stem}.json").exists():
                    return False
    except OSError:
        return False
    return True


def raw_day_may_leave(data_root: Path, day: str, usage_percent: float,
                      config_path: Optional[Path] = None) -> bool:
    """May the SSD delete this raw day now?  Yes without an archive drive,
    yes once the drive holds every chunk, yes past the emergency mark."""
    path = Path(config_path or ARCHIVE_TOML)
    if not path.exists():
        return True
    if usage_percent >= EVICT_EMERGENCY_PERCENT:
        logger.warning("archive-before-evict: disk at %.1f%%; raw day %s may go even if not "
                       "on the archive drive", usage_percent, day)
        return True
    if raw_day_archived(data_root, day, path):
        return True
    logger.warning("archive-before-evict: keeping raw day %s on the SSD: not all of it is on "
                   "the archive drive yet", day)
    return False
