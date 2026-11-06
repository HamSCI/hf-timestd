"""The keeper pass: ship closed event windows to the server, delete after RECEIPT.

Every five minutes on a station (`hf-timestd events run`):

  1. pull orders.json from the server;
  2. for each order for this station whose window closed `settle_minutes` ago
     and that has no entry in state.json: cut the window out of the raw buffer
     into Digital RF (`write_window_drf`) under <staging>/<event_id>/;
  3. rsync it to <remote_root>/<event_id>/<station_id>/hf-timestd/, SHA256SUMS
     last, in its own call (the server verifies a directory once SHA256SUMS
     exists);
  4. when the server's RECEIPT names the sha256 of our own SHA256SUMS, mark the
     event done and delete <staging>/<event_id>/.

State, in <staging>/state.json, maps event_id to one of
    staged | uploaded | done | failed:<reason>
Only `staged` and `uploaded` count as in flight; at most one window is in
flight at a time.  A `failed:*` state keeps its staging copy and is never
retried automatically; a person decides.

Deletion rules (this module deletes data):
  * the only thing it ever removes is the directory <staging>/<event_id>/ and
    the files inside it;
  * it never removes anything under raw_root, and refuses a configuration where
    staging and raw_root nest;
  * an event_id must be one plain path component.
"""
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .window_drf import write_window_drf

log = logging.getLogger(__name__)

IN_FLIGHT = ("staged", "uploaded")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
_REQUIRED = ("station_id", "remote_root", "staging", "raw_root")
_TS = "%Y-%m-%dT%H:%M:%SZ"
PULL_TIMEOUT = 300
PUSH_TIMEOUT = 6 * 3600


class ConfigError(Exception):
    pass


class StateError(Exception):
    pass


def validate_config(cfg: dict) -> dict:
    for k in _REQUIRED:
        if not cfg.get(k) or not isinstance(cfg[k], str):
            raise ConfigError("events.toml: missing or invalid %r" % k)
    if "channels" in cfg and not (isinstance(cfg["channels"], list)
                                  and all(isinstance(c, str) and _SAFE_ID.match(c)
                                          for c in cfg["channels"])):
        raise ConfigError("events.toml: channels must be a list of names")
    for k in ("settle_minutes", "bwlimit_kbps"):
        if k in cfg and (isinstance(cfg[k], bool) or not isinstance(cfg[k], (int, float))
                         or cfg[k] < 0):
            raise ConfigError("events.toml: %s must be a number >= 0" % k)
    stg, raw = Path(cfg["staging"]).resolve(), Path(cfg["raw_root"]).resolve()
    if stg == raw or stg in raw.parents or raw in stg.parents:
        raise ConfigError("events.toml: staging and raw_root must not nest")
    return cfg


class Transport:
    def pull(self, remote_path, local_path) -> bool:
        raise NotImplementedError

    def push(self, local_dir, remote_dir) -> bool:
        raise NotImplementedError

    def fetch_receipt(self, remote_dir, local_path, name="RECEIPT") -> bool:
        """Fetch remote_dir/<name> (RECEIPT or FAILED); False if absent or on error."""
        raise NotImplementedError


class LocalTransport(Transport):
    """rsync between local paths; what the tests use, and the base of RsyncTransport."""

    def __init__(self, cfg=None):
        self.cfg = cfg or {}

    # hooks -------------------------------------------------------------
    def _remote(self, path) -> str:
        return str(path)

    def _opts(self) -> list:
        return []

    # -------------------------------------------------------------------
    def _rsync(self, args, src, dst, timeout) -> bool:
        cmd = ["rsync", "-a", "--partial"] + self._opts() + list(args) + [src, dst]
        try:
            return subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE,
                                  timeout=timeout).returncode == 0
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("rsync failed to run: %s", exc)
            return False

    def pull(self, remote_path, local_path) -> bool:
        return self._rsync([], self._remote(remote_path), str(local_path), PULL_TIMEOUT)

    def push(self, local_dir, remote_dir) -> bool:
        src = str(local_dir).rstrip("/") + "/"
        dst = self._remote(str(remote_dir).rstrip("/") + "/")
        # everything but SHA256SUMS first, then SHA256SUMS alone
        if not self._rsync(["--mkpath", "--exclude=/SHA256SUMS"], src, dst, PUSH_TIMEOUT):
            return False
        return self._rsync(["--mkpath"], src + "SHA256SUMS",
                           self._remote(str(remote_dir).rstrip("/") + "/SHA256SUMS"),
                           PUSH_TIMEOUT)

    def fetch_receipt(self, remote_dir, local_path, name="RECEIPT") -> bool:
        return self._rsync([], self._remote(str(remote_dir).rstrip("/") + "/" + name),
                           str(local_path), PULL_TIMEOUT)


class RsyncTransport(LocalTransport):
    """rsync over ssh to cfg['remote'], key cfg['ssh_key']."""

    def _remote(self, path) -> str:
        return "%s:%s" % (self.cfg["remote"], path)

    def _opts(self) -> list:
        opts = ["-e", "ssh -i %s -o BatchMode=yes" % self.cfg["ssh_key"]]
        bw = int(self.cfg.get("bwlimit_kbps", 0) or 0)
        if bw:
            opts.append("--bwlimit=%d" % bw)
        return opts


# ---------------------------------------------------------------------------

def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, _TS).replace(tzinfo=timezone.utc)


def _sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_state(staging: Path) -> dict:
    p = staging / "state.json"
    if not p.exists():
        return {}
    try:
        st = json.loads(p.read_text())
        if not isinstance(st, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in st.items()):
            raise ValueError("not a map of strings")
    except (OSError, ValueError) as exc:
        raise StateError("state.json unreadable (%s)" % exc)
    return st


def _save_state(staging: Path, state: dict):
    tmp = staging / "state.json.tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, staging / "state.json")


def _event_dir(staging: Path, event_id):
    """<staging>/<event_id>, or None if event_id is not one plain component."""
    if not isinstance(event_id, str) or not _SAFE_ID.match(event_id) or ".." in event_id:
        return None
    return staging / event_id


def _remove_staging(cfg, staging: Path, event_id: str):
    """Delete <staging>/<event_id>/ and nothing else."""
    d = _event_dir(staging, event_id)
    if d is None or d.is_symlink() or not d.is_dir():
        return
    real, root = d.resolve(), staging.resolve()
    raw = Path(cfg["raw_root"]).resolve()
    if real.parent != root or real == raw or raw in real.parents or real in raw.parents:
        raise RuntimeError("refusing to delete %s" % real)
    shutil.rmtree(real)


def _channels(cfg) -> list:
    if cfg.get("channels"):
        return list(cfg["channels"])
    raw = Path(cfg["raw_root"])
    if not raw.is_dir():
        return []
    return sorted(p.name for p in raw.iterdir()
                  if p.is_dir() and not p.name.startswith("."))


def _remote_dir(cfg, event_id) -> str:
    return "%s/%s/%s/hf-timestd" % (cfg["remote_root"].rstrip("/"), event_id,
                                    cfg["station_id"])


def run_once(cfg: dict, now: datetime, transport: Transport) -> list:
    """One keeper pass.  Returns a log line per action."""
    lines = []

    def say(msg):
        lines.append(msg)
        log.info(msg)

    staging = Path(cfg["staging"])
    staging.mkdir(parents=True, exist_ok=True)
    lock = open(staging / ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        say("another keeper pass holds the lock; skipping")
        lock.close()
        return lines
    try:
        try:
            state = _load_state(staging)
        except StateError as exc:
            say("%s; refusing to act until a person fixes it" % exc)
            return lines
        _tend_in_flight(cfg, transport, staging, state, say)
        _stage_next(cfg, now, transport, staging, state, say)
    finally:
        lock.close()
    return lines


def _tend_in_flight(cfg, transport, staging, state, say):
    for eid in sorted(state):
        st = state[eid]
        if st not in IN_FLIGHT:
            continue
        d = _event_dir(staging, eid)
        if d is None or not (d / "SHA256SUMS").is_file():
            state[eid] = "failed:staging-missing"
            _save_state(staging, state)
            say("%s: staging copy missing; state failed:staging-missing" % eid)
            continue
        rdir = _remote_dir(cfg, eid)
        if st == "staged":
            if transport.push(d, rdir):
                state[eid] = "uploaded"
                _save_state(staging, state)
                say("%s: uploaded to %s" % (eid, rdir))
            else:
                say("%s: upload failed; staging kept, will retry" % eid)
            continue
        # uploaded: wait for the server's verdict
        failed_local = d / "FAILED.remote"
        receipt_local = d / "RECEIPT.remote"
        for p in (failed_local, receipt_local):
            if p.exists():
                p.unlink()
        if transport.fetch_receipt(rdir, failed_local, "FAILED"):
            state[eid] = "failed:server-verify"
            _save_state(staging, state)
            say("%s: server wrote FAILED; state failed:server-verify, staging kept: %s"
                % (eid, failed_local.read_text(errors="replace").strip()[:200]))
            continue
        if not transport.fetch_receipt(rdir, receipt_local, "RECEIPT"):
            say("%s: no receipt yet" % eid)
            continue
        try:
            first = receipt_local.read_text().splitlines()[0].split()
            theirs = first[0].lower() if first else ""
        except (OSError, IndexError):
            theirs = ""
        ours = _sha256(d / "SHA256SUMS")
        if theirs != ours:
            state[eid] = "failed:receipt-mismatch"
            _save_state(staging, state)
            say("%s: receipt hash %r != ours %s; state failed:receipt-mismatch, staging kept"
                % (eid, theirs[:64], ours))
            continue
        state[eid] = "done"
        _save_state(staging, state)
        _remove_staging(cfg, staging, eid)
        say("%s: receipt matches; state done, staging deleted" % eid)


def _stage_next(cfg, now, transport, staging, state, say):
    orders_local = staging / "orders.json"
    if orders_local.exists():
        orders_local.unlink()
    orders_remote = "%s/orders/orders.json" % cfg["remote_root"].rstrip("/")
    if not transport.pull(orders_remote, orders_local):
        say("orders: could not pull %s; will retry next pass" % orders_remote)
        return
    try:
        doc = json.loads(orders_local.read_text())
        if doc.get("version") != 1 or not isinstance(doc.get("orders"), list):
            raise ValueError("unexpected version or shape")
    except (OSError, ValueError, AttributeError) as exc:
        say("orders: unusable orders.json (%s); skipping" % exc)
        return

    settle = timedelta(minutes=float(cfg.get("settle_minutes", 15)))
    station = cfg["station_id"]
    ready = []
    for o in doc["orders"]:
        try:
            eid = o["event_id"]
            if station not in o["stations"] or eid in state:
                continue
            if not any(r.get("client") == "hf-timestd" for r in o.get("record", [])):
                continue
            if _event_dir(staging, eid) is None:
                say("orders: skipping unsafe event_id %r" % (eid,))
                continue
            if _parse(o["end_utc"]) + settle > now:
                continue
            _parse(o["start_utc"])
            ready.append(o)
        except (KeyError, TypeError, ValueError, AttributeError):
            say("orders: skipping a malformed order")
    if not ready:
        return
    ready.sort(key=lambda o: (o["end_utc"], o["event_id"]))

    waiting = [e for e, s in state.items() if s in IN_FLIGHT]
    if waiting:
        say("%s awaiting its receipt; %d ready order(s) wait" % (waiting[0], len(ready)))
        return

    o = ready[0]
    eid = o["event_id"]
    channels = _channels(cfg)
    if not channels:
        say("%s: no channel directories under %s; nothing to cut" % (eid, cfg["raw_root"]))
        return
    d = _event_dir(staging, eid)
    _remove_staging(cfg, staging, eid)           # a cut that crashed before state was written
    try:
        res = write_window_drf(cfg["raw_root"], channels, o, d)
    except Exception as exc:
        say("%s: cutting the window failed (%s); will retry" % (eid, exc))
        _remove_staging(cfg, staging, eid)
        return
    state[eid] = "staged"
    _save_state(staging, state)
    miss = sum(len(c["missing"]) for c in res["channels"].values())
    say("%s: staged %d channel(s), %d bytes, %d missing file(s)"
        % (eid, len(channels), res["bytes"], miss))
    rdir = _remote_dir(cfg, eid)
    if transport.push(d, rdir):
        state[eid] = "uploaded"
        _save_state(staging, state)
        say("%s: uploaded to %s" % (eid, rdir))
    else:
        say("%s: upload failed; staging kept, will retry" % eid)
