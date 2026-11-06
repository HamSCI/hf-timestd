#!/usr/bin/env python3
"""wd30's side of event response.  Stdlib only; needs flare_orders.py beside it.

    events_server.py orders --stations stations.json --out /srv/wd_data/events/orders.json
    events_server.py verify --root /srv/wd_data/events/incoming

orders  runs flare_orders (GOES flare list -> the full orders.json), then writes
        each station its own orders, only those naming it, to
        incoming/<station>/orders/orders.json.
verify  checks each upload incoming/<station>/<event>/<station>/<client>/ against
        its SHA256SUMS.  Every listed file matches: write RECEIPT, one line,
        "<sha256 of SHA256SUMS>  verified <UTC>".  Otherwise write FAILED, one
        bad entry per line, and no RECEIPT.  A person removes FAILED to retry.
        It refuses any symlink in an upload and reads nothing it cannot open
        as a regular file.

A directory is ready when SHA256SUMS exists, its ctime is 60 s old (rsync -a
keeps the station's mtimes, so mtime says nothing about arrival), and no rsync
temp file (.name.XXXXXX) remains.  The station pushes SHA256SUMS last.
"""
import argparse
import fnmatch
import hashlib
import importlib.util
import json
import logging
import os
import re
import stat
import sys
import tempfile
from datetime import datetime, timezone

log = logging.getLogger("events_server")

SETTLE_SEC = 60
SUMS, RECEIPT, FAILED = "SHA256SUMS", "RECEIPT", "FAILED"
STATE_FILE = ".verify-state.json"
MAX_SUMS = 1 << 20
_LINE = re.compile(r"([0-9a-fA-F]{64}) [ *](.+)\Z")


def _load_flare_orders():
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (os.environ.get("FLARE_ORDERS_PY"),
              os.path.join(here, "flare_orders.py"),
              os.path.join(here, "..", "..", "src", "hf_timestd", "events",
                           "flare_orders.py")):
        if p and os.path.isfile(p):
            spec = importlib.util.spec_from_file_location("flare_orders", p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    raise FileNotFoundError("flare_orders.py not found beside events_server.py")


def _atomic_write(path, text, mode=0o644):
    """Write path through a mkstemp file in its own directory, then os.replace."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.lexists(tmp):
            os.remove(tmp)


def _open_regular(path):
    """Open path read-only without following a symlink; refuse a non-regular file."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular file")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _printable(rel):
    return rel.isprintable()


def _scan(d):
    """Walk d without following links.  Return (rsync_temp_seen, symlink paths)."""
    temp, links, stack = False, [], [""]
    while stack:
        rel = stack.pop()
        with os.scandir(os.path.join(d, rel)) as it:
            for e in it:
                r = os.path.join(rel, e.name)
                if e.is_symlink():
                    links.append(r)
                elif e.is_dir(follow_symlinks=False):
                    stack.append(r)
                elif fnmatch.fnmatchcase(e.name, ".*.??????"):
                    temp = True
    return temp, sorted(links)


def _read_sums(d):
    """SHA256SUMS bytes, or a string naming why it is unusable."""
    try:
        fd = _open_regular(os.path.join(d, SUMS))
    except OSError:
        return "SHA256SUMS is not a regular file"
    with os.fdopen(fd, "rb") as f:
        data = f.read(MAX_SUMS + 1)
    if len(data) > MAX_SUMS:
        return "SHA256SUMS is over 1 MiB"
    return data


def _check(d, sums_bytes):
    """Return the list of bad entries (empty means every listed file matches)."""
    bad, listed = [], 0
    root = os.path.realpath(d)
    for n, raw in enumerate(sums_bytes.decode("utf-8", "replace").splitlines(), 1):
        if not raw.strip():
            continue
        m = _LINE.match(raw)
        if not m:
            bad.append("line %d: unparseable" % n)
            continue
        want, rel = m.group(1).lower(), m.group(2)
        listed += 1
        label = rel if _printable(rel) else "line %d" % n
        # An absolute path would make os.path.join ignore d, so this check stays.
        if rel.startswith("/") or ".." in rel.split("/"):
            bad.append(label)
            continue
        p = os.path.join(d, rel)
        if not os.path.realpath(p).startswith(root + os.sep):
            bad.append(label)
            continue
        h = hashlib.sha256()
        try:
            fd = _open_regular(p)
            with os.fdopen(fd, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
        except OSError:
            bad.append(label)
            continue
        if h.hexdigest() != want:
            bad.append(label)
    if listed == 0 and not bad:
        bad.append("SHA256SUMS lists no files")
    return bad


def _sub(p, skip=()):
    """Real subdirectories of p (symlinks never count), sorted."""
    try:
        with os.scandir(p) as it:
            return sorted(e.path for e in it
                          if e.is_dir(follow_symlinks=False) and not e.name.startswith(".")
                          and e.name not in skip)
    except OSError:
        return []


def _dirs(root):
    """root/<station>/<event>/<station>/<client>, the layout under incoming/."""
    for stn in _sub(root):
        for ev in _sub(stn, skip=("orders",)):
            for stn2 in _sub(ev):
                yield from _sub(stn2)


def _judge(d, now, condition):
    """Verify one directory.  Returns True if a verdict was written."""
    sums = os.path.join(d, SUMS)
    if os.path.lexists(os.path.join(d, RECEIPT)) or os.path.lexists(os.path.join(d, FAILED)):
        return False
    try:
        st = os.lstat(sums)
    except OSError:
        return False                          # still arriving; SHA256SUMS comes last
    if now.timestamp() - st.st_ctime < SETTLE_SEC:
        condition(d, "waiting: SHA256SUMS under %d s old" % SETTLE_SEC)
        return False
    temp, links = _scan(d)
    if temp:
        condition(d, "waiting: rsync temp files present")
        return False
    if links:
        bad = ["symlink: %s" % (r if _printable(r) else "(unprintable name)") for r in links]
        data = None
    else:
        data = _read_sums(d)
        bad = [data] if isinstance(data, str) else _check(d, data)
    if bad:
        _atomic_write(os.path.join(d, FAILED), "".join(b + "\n" for b in bad))
        log.warning("%s: FAILED, %d bad entr%s: %s", d, len(bad),
                    "y" if len(bad) == 1 else "ies", "; ".join(bad[:5]))
    else:
        line = "%s  verified %s\n" % (hashlib.sha256(data).hexdigest(),
                                      now.strftime("%Y-%m-%dT%H:%M:%SZ"))
        _atomic_write(os.path.join(d, RECEIPT), line)
        log.info("%s: verified, RECEIPT written", d)
    return True


def verify(root, now=None):
    """One pass over root (the incoming/ directory).  Returns verdicts written."""
    now = now or datetime.now(timezone.utc)
    sp = os.path.join(root, STATE_FILE)
    try:
        with open(sp) as f:
            conds = json.load(f)
        if not isinstance(conds, dict):
            conds = {}
    except (OSError, ValueError):
        conds = {}
    seen, wrote = {}, 0

    def condition(d, text):
        """Record a persistent condition; log only when it changes."""
        seen[d] = text
        if conds.get(d) != text:
            log.info("%s: %s", d, text)

    for d in _dirs(root):
        try:
            wrote += _judge(d, now, condition)
        except Exception as exc:              # one bad directory never stops the pass
            condition(d, "error: %s: %s" % (type(exc).__name__, exc))
    if seen != conds:
        try:
            _atomic_write(sp, json.dumps(seen, sort_keys=True))
        except OSError as exc:
            log.warning("cannot save %s: %s", sp, exc)
    return wrote


def _write_station_orders(incoming, station_id, orders):
    """Write incoming/<id>/orders/orders.json without following a planted symlink."""
    sdir = os.path.join(incoming, station_id)
    if not os.path.isdir(sdir) or os.path.islink(sdir):
        log.info("%s: no station directory yet; skipping its orders", station_id)
        return
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if not os.path.lexists(os.path.join(sdir, "orders")):
        os.mkdir(os.path.join(sdir, "orders"))
    try:
        dfd = os.open(os.path.join(sdir, "orders"), flags)
    except OSError as exc:
        log.warning("%s: orders directory unusable (%s); skipped", station_id, exc)
        return
    tmp = ".orders.json.%d" % os.getpid()
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644,
                     dir_fd=dfd)
        with os.fdopen(fd, "w") as f:
            json.dump({"version": 1, "orders": orders}, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, "orders.json", src_dir_fd=dfd, dst_dir_fd=dfd)
    finally:
        try:
            os.unlink(tmp, dir_fd=dfd)
        except OSError:
            pass
        os.close(dfd)


def station_orders(stations_path, full_path, incoming):
    """Split the full orders file into one file per station."""
    with open(stations_path) as f:
        stations = json.load(f)
    with open(full_path) as f:
        doc = json.load(f)
    for s in stations:
        sid = s["id"]
        mine = [o for o in doc.get("orders", []) if sid in (o.get("stations") or [])]
        _write_station_orders(incoming, sid, mine)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("orders", help="GOES flares -> orders.json")
    o.add_argument("--stations", required=True)
    o.add_argument("--out", required=True,
                   help="full orders.json, outside every station's root")
    o.add_argument("--incoming", help="per-station roots (default: <out dir>/incoming)")
    g = o.add_mutually_exclusive_group()
    g.add_argument("--url")
    g.add_argument("--feed", help="local JSON file standing in for the feed")
    v = sub.add_parser("verify", help="check uploads, write RECEIPT or FAILED")
    v.add_argument("--root", required=True)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.cmd == "orders":
        fo = _load_flare_orders()
        fwd = ["--stations", args.stations, "--out", args.out]
        if args.feed:
            fwd += ["--url", "file://" + os.path.abspath(args.feed)]
        elif args.url:
            fwd += ["--url", args.url]
        rc = fo.main(fwd)
        if rc:
            return rc
        try:
            station_orders(args.stations, args.out, args.incoming or os.path.join(
                os.path.dirname(os.path.abspath(args.out)), "incoming"))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print("events_server: per-station orders: %s" % exc, file=sys.stderr)
            return 1
        return 0
    try:
        verify(args.root)
    except OSError as exc:
        print("events_server: %s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
