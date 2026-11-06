#!/usr/bin/env python3
"""wd30's side of event response.  Stdlib only; needs flare_orders.py beside it.

    events_server.py orders --stations stations.json --out <root>/orders/orders.json
    events_server.py verify --root /srv/wd_data/events

orders  runs flare_orders (GOES flare list -> orders.json).
verify  checks each station upload <root>/<event>/<station>/<client>/ against
        its SHA256SUMS.  Every listed file matches: write RECEIPT, one line,
        "<sha256 of SHA256SUMS>  verified <UTC>".  Otherwise write FAILED, one
        bad path per line, and no RECEIPT.  A person removes FAILED to retry.

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
import sys
from datetime import datetime, timezone

log = logging.getLogger("events_server")

SETTLE_SEC = 60
SUMS, RECEIPT, FAILED = "SHA256SUMS", "RECEIPT", "FAILED"
STATE_FILE = ".verify-state.json"
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


def _atomic_write(path, text):
    tmp = "%s.tmp%d" % (path, os.getpid())
    try:
        with open(tmp, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _has_rsync_temp(d):
    for _, _, files in os.walk(d):
        for n in files:
            if fnmatch.fnmatchcase(n, ".*.??????"):
                return True
    return False


def _check(d, sums_bytes):
    """Return the list of bad paths (empty means every listed file matches)."""
    bad, listed = [], 0
    root = os.path.realpath(d)
    for raw in sums_bytes.decode("utf-8", "replace").splitlines():
        if not raw.strip():
            continue
        m = _LINE.match(raw)
        if not m:
            bad.append("unparseable line: %s" % raw[:120])
            continue
        want, rel = m.group(1).lower(), m.group(2)
        listed += 1
        if rel.startswith("/") or ".." in rel.split("/"):
            bad.append(rel)
            continue
        p = os.path.join(d, rel)
        if not os.path.realpath(p).startswith(root + os.sep) or not os.path.isfile(p):
            bad.append(rel)
            continue
        h = hashlib.sha256()
        try:
            with open(p, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
        except OSError:
            bad.append(rel)
            continue
        if h.hexdigest() != want:
            bad.append(rel)
    if listed == 0 and not bad:
        bad.append("SHA256SUMS lists no files")
    return bad


def _dirs(root):
    for ev in sorted(os.listdir(root)):
        if ev == "orders" or ev.startswith("."):
            continue
        for st in _sub(os.path.join(root, ev)):
            for cl in _sub(st):
                yield cl


def _sub(p):
    try:
        return [os.path.join(p, n) for n in sorted(os.listdir(p))
                if os.path.isdir(os.path.join(p, n)) and not os.path.islink(os.path.join(p, n))]
    except OSError:
        return []


def verify(root, now=None):
    """One pass.  Returns the number of verdicts written."""
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
        sums = os.path.join(d, SUMS)
        if os.path.exists(os.path.join(d, RECEIPT)) or os.path.exists(os.path.join(d, FAILED)):
            continue
        try:
            st = os.stat(sums)
        except OSError:
            continue                          # still arriving; SHA256SUMS comes last
        age = now.timestamp() - st.st_ctime
        if age < SETTLE_SEC:
            condition(d, "waiting: SHA256SUMS under %d s old" % SETTLE_SEC)
            continue
        if _has_rsync_temp(d):
            condition(d, "waiting: rsync temp files present")
            continue
        with open(sums, "rb") as f:
            data = f.read()
        bad = _check(d, data)
        if bad:
            _atomic_write(os.path.join(d, FAILED), "".join(b + "\n" for b in bad))
            log.warning("%s: FAILED, %d bad path(s): %s", d, len(bad), "; ".join(bad[:5]))
        else:
            line = "%s  verified %s\n" % (hashlib.sha256(data).hexdigest(),
                                          now.strftime("%Y-%m-%dT%H:%M:%SZ"))
            _atomic_write(os.path.join(d, RECEIPT), line)
            log.info("%s: verified, RECEIPT written", d)
        wrote += 1
    if seen != conds:
        try:
            _atomic_write(sp, json.dumps(seen, sort_keys=True))
        except OSError as exc:
            log.warning("cannot save %s: %s", sp, exc)
    return wrote


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("orders", help="GOES flares -> orders.json")
    o.add_argument("--stations", required=True)
    o.add_argument("--out", required=True)
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
        return fo.main(fwd)
    try:
        verify(args.root)
    except OSError as exc:
        print("events_server: %s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
