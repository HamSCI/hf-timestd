#!/usr/bin/env python3
"""Log radiod's tick reports to one .npz per channel per UTC minute.

  listen_ticks.py --group ac0g-b1-a-pilot-data.local --ssrc 10000 --ssrc 10001 \\
                  --out DIR [--iface enp1s0]

radiod sends a channel's tick group with its periodic (data-group) status,
to the channel's DATA multicast group on port 5006, once per report per
destination.  Per channel and UTC minute the logger writes

  DIR/ticks-SSRC-YYYYMMDDTHHMMZ.npz
      profiles   complex64 [seconds, 3 templates (wwv, wwvh, bpm), nprofile]
                 zeros where a template sent nothing (see nprofile)
      envelope   float32   [seconds, 1000]   NaN where absent
      markers    float32   [seconds, 3]      energy at 1000, 1200, 1500 Hz; NaN if absent
      peaks      float32   [seconds, 3, 2]   [offset in input samples from window start,
                                              SNR dB]; offset NaN for incomplete or
                                              absent templates
      flags, step, nprofile   int32 [seconds] / [seconds, 3]
      second_utc, window_start   int64 [seconds]   ns on the GPS epoch / input counter
  DIR/index-SSRC-YYYYMMDD.jsonl
      one row per second, no arrays, written as the report arrives

Duplicates (same ssrc and tick_second_utc, e.g. a second destination) are
dropped.  At most two minutes are buffered per channel.  A minute is written
once the next minute is 5 s old or its second 59 has arrived (so a reordered
second 59 still lands in it), when the socket goes quiet, and at exit.
Writing merges with any file already there for that channel and minute (new
rows win), so a restart or a quiet-socket flush loses nothing.  An existing
file that cannot be read (truncated, empty, junk) is renamed to
`NAME.corrupt-<UTC stamp>` and the minute is written fresh; the listener goes on.  A report more
than one minute behind the newest goes to the index only, marked "late".
"""
import argparse
import json
import socket
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from radiod_pilot import listen  # noqa: E402
from radiod_ticks import tlv_ticks  # noqa: E402

NS = 10**9
TEMPLATES = tlv_ticks.TEMPLATES
RECENT_KEEP = 600            # seconds of dedupe memory per channel


def _minute_of(second_utc_ns: int) -> int:
    return ((second_utc_ns + tlv_ticks.GPS_TO_UNIX_NS) // NS) // 60


def _stamp(minute: int, fmt: str) -> str:
    return time.strftime(fmt, time.gmtime(minute * 60))


GRACE_S = 5     # a minute stays open until the next one is this many seconds old


class _Channel:
    def __init__(self):
        self.open = {}          # minute -> {second_utc_ns: decoded dict}; at most two
        self.newest = None      # newest minute seen
        self.seen = {}          # second_utc_ns -> None, insertion-ordered recent memory


def _second_of_minute(second_utc_ns: int) -> int:
    return ((second_utc_ns + tlv_ticks.GPS_TO_UNIX_NS) // NS) % 60


def _rows_from_npz(path):
    """Rebuild per-second decoded dicts from a minute file this logger wrote.

    Returns (rows, ok); ok is False when the file cannot be read."""
    rows = {}
    try:
        with np.load(path) as z:
            secs = z["second_utc"]
            for i, s in enumerate(secs):
                d = {"tick_second_utc": int(s), "tick_flags": int(z["flags"][i]),
                     "tick_step": int(z["step"][i]), "tick_window_start": int(z["window_start"][i])}
                for k, name in enumerate(TEMPLATES):
                    n = int(z["nprofile"][i, k])
                    if n:
                        d[f"tick_profile_{name}"] = z["profiles"][i, k, :n].copy()
                if not np.isnan(z["envelope"][i]).all():
                    d["tick_envelope"] = z["envelope"][i].copy()
                if not np.isnan(z["markers"][i]).all():
                    d["tick_markers"] = z["markers"][i].copy()
                if not np.isnan(z["peaks"][i]).all():
                    d["tick_peaks"] = z["peaks"][i].copy()
                rows[int(s)] = d
    except Exception as e:      # BadZipFile, EOFError, OSError, ValueError, KeyError, ...
        print(f"listen_ticks: cannot read {path}: {e!r}", file=sys.stderr, flush=True)
        return {}, False
    return rows, True


class TickLogger:
    def __init__(self, out_dir, err=sys.stderr):
        self.out = Path(out_dir)
        self.out.mkdir(parents=True, exist_ok=True)
        self.err = err
        self.chan = {}
        self.index_files = {}   # path -> open file

    # -- index ---------------------------------------------------------
    def _index_row(self, ssrc, d, minute, late, group, logged_at):
        row = {
            "ssrc": ssrc, "second_utc": d["tick_second_utc"],
            "unix_second": (d["tick_second_utc"] + tlv_ticks.GPS_TO_UNIX_NS) // NS,
            "window_start": d.get("tick_window_start"), "step": d.get("tick_step"),
            "flags": d.get("tick_flags"), "complete": d.get("tick_complete"),
            "used_pilot": d.get("tick_used_pilot"), "templates": d.get("tick_templates"),
            "nprofile": {n: int(len(d[f"tick_profile_{n}"])) for n in TEMPLATES
                         if f"tick_profile_{n}" in d},
            "peaks": None if "tick_peaks" not in d else
            [[None if not np.isfinite(x) else float(x) for x in r] for r in d["tick_peaks"]],
            "markers": None if "tick_markers" not in d else
            [None if not np.isfinite(x) else float(x) for x in d["tick_markers"]],
            "has_envelope": "tick_envelope" in d,
            "npz": f"ticks-{ssrc}-{_stamp(minute, '%Y%m%dT%H%MZ')}.npz",
            "late": late, "group": group, "logged_at_unix": logged_at,
        }
        path = self.out / f"index-{ssrc}-{_stamp(minute, '%Y%m%d')}.jsonl"
        f = self.index_files.get(path)
        if f is None:
            if len(self.index_files) > 8:       # a day or two per channel
                for old in list(self.index_files):
                    self.index_files.pop(old).close()
            f = self.index_files[path] = open(path, "a", buffering=1)
        f.write(json.dumps(row) + "\n")

    # -- npz -----------------------------------------------------------
    def _write_minute(self, ssrc, minute, new_rows):
        if not new_rows:
            return
        path = self.out / f"ticks-{ssrc}-{_stamp(minute, '%Y%m%dT%H%MZ')}.npz"
        rows = {}
        if path.exists():
            rows, ok = _rows_from_npz(path)
            if not ok:      # keep the unreadable file aside, never overwrite it silently
                aside = path.with_name(f"{path.name}.corrupt-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}")
                try:
                    path.replace(aside)
                    print(f"listen_ticks: moved unreadable {path.name} to {aside.name}",
                          file=self.err, flush=True)
                except OSError as e:
                    print(f"listen_ticks: cannot move {path}: {e}", file=self.err, flush=True)
        rows.update(new_rows)           # new wins on duplicates
        secs = sorted(rows)
        n = len(secs)
        npro = np.zeros((n, 3), np.int32)
        for i, s in enumerate(secs):
            for k, name in enumerate(TEMPLATES):
                p = rows[s].get(f"tick_profile_{name}")
                npro[i, k] = 0 if p is None else len(p)
        width = int(npro.max()) if npro.size else 0
        prof = np.zeros((n, 3, width), np.complex64)
        env = np.full((n, tlv_ticks.ENVELOPE_LEN), np.nan, np.float32)
        mk = np.full((n, 3), np.nan, np.float32)
        pk = np.full((n, 3, 2), np.nan, np.float32)
        flags = np.zeros(n, np.int32)
        step = np.zeros(n, np.int32)
        wstart = np.zeros(n, np.int64)
        for i, s in enumerate(secs):
            d = rows[s]
            for k, name in enumerate(TEMPLATES):
                p = d.get(f"tick_profile_{name}")
                if p is not None:
                    prof[i, k, :len(p)] = p
            if "tick_envelope" in d:
                env[i] = d["tick_envelope"]
            if "tick_markers" in d:
                mk[i] = d["tick_markers"]
            if "tick_peaks" in d:
                pk[i] = d["tick_peaks"]
            flags[i] = d.get("tick_flags", 0)
            step[i] = d.get("tick_step", 0)
            wstart[i] = d.get("tick_window_start", 0)
        tmp = path.with_suffix(".tmp")
        try:
            with open(tmp, "wb") as f:
                np.savez(f, profiles=prof, envelope=env, markers=mk, peaks=pk, flags=flags,
                         step=step, nprofile=npro, second_utc=np.array(secs, np.int64),
                         window_start=wstart)
            tmp.replace(path)
        except OSError as e:
            print(f"listen_ticks: write {path}: {e}", file=self.err, flush=True)

    # -- input ---------------------------------------------------------
    def add(self, d: dict, group=None, now=None) -> bool:
        """Take one decoded packet; True when it was a new tick report we logged."""
        ssrc = d.get("ssrc")
        sec = d.get("tick_second_utc")
        if ssrc is None or sec is None:
            return False
        ch = self.chan.setdefault(ssrc, _Channel())
        if sec in ch.seen:
            return False
        ch.seen[sec] = None
        while len(ch.seen) > RECENT_KEEP:
            ch.seen.pop(next(iter(ch.seen)))
        minute = _minute_of(sec)
        now = time.time() if now is None else now
        if ch.newest is None or minute > ch.newest:
            ch.newest = minute
        late = minute < ch.newest - 1       # older than the minute held back
        if not late:
            ch.open.setdefault(minute, {})[sec] = d
            self._close_settled(ssrc, ch)
        try:
            self._index_row(ssrc, d, minute, late, group, now)
        except OSError as e:
            print(f"listen_ticks: index: {e}", file=self.err, flush=True)
        return True

    def _close_settled(self, ssrc, ch):
        """Write minutes that can take no more: older than the previous one, or the
        previous one once second 59 is in or the newest minute is GRACE_S old."""
        cur = ch.open.get(ch.newest, {})
        aged = any(_second_of_minute(x) >= GRACE_S for x in cur)
        for m in sorted(ch.open):
            if m == ch.newest:
                continue
            done = m < ch.newest - 1 or aged or any(
                _second_of_minute(x) == 59 for x in ch.open[m])
            if done:
                self._write_minute(ssrc, m, ch.open.pop(m))

    def flush(self):
        """Write every buffered minute (socket quiet, or exiting)."""
        for ssrc, ch in self.chan.items():
            for m in sorted(ch.open):
                self._write_minute(ssrc, m, ch.open[m])
            ch.open = {}

    def close(self):
        self.flush()
        for f in self.index_files.values():
            f.close()
        self.index_files.clear()


def run(sock, logger: TickLogger, ssrcs, err=sys.stderr, sleep_fn=time.sleep) -> int:
    """Read packets until KeyboardInterrupt; return the number of reports logged."""
    ssrcs = set(ssrcs)
    n = 0
    while True:
        try:
            pkt, group = listen.receive(sock)
        except KeyboardInterrupt:
            return n
        except socket.timeout:
            logger.flush()
            print(f"listen_ticks: no status packet in {listen.SILENCE_S:.0f} s",
                  file=err, flush=True)
            continue
        except OSError as e:
            print(f"listen_ticks: recvmsg: {e}", file=err, flush=True)
            sleep_fn(1.0)
            continue
        try:
            d = tlv_ticks.decode_ticks(pkt)
        except ValueError:
            continue
        if d.get("ssrc") not in ssrcs:
            continue
        if logger.add(d, group):
            n += 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--group", required=True, action="append", metavar="GROUP[:PORT]",
                    help="the channels' DATA multicast group, name or address "
                         f"(port {listen.DEFAULT_STATUS_PORT}); repeatable, one port for all")
    ap.add_argument("--ssrc", required=True, type=int, action="append",
                    help="a tick channel's SSRC (repeatable)")
    ap.add_argument("--out", required=True, metavar="DIR")
    ap.add_argument("--iface", help="interface name or address for the multicast join")
    a = ap.parse_args(argv)
    try:
        addrs = [listen.parse_status(x) for x in a.group]
    except ValueError as e:
        ap.error(f"--group: {e}")
    ports = {p for _, p in addrs}
    if len(ports) != 1:
        ap.error(f"--group: one port for all groups, got {sorted(ports)}")
    port = ports.pop()
    groups = [g for g, _ in addrs]
    try:
        sock = listen.open_socket(groups, port, a.iface)
    except ValueError as e:
        ap.error(f"--iface: {e}")
    except OSError as e:
        ap.error(f"cannot join {groups} port {port}: {e}")
    try:
        logger = TickLogger(a.out)
    except OSError as e:
        ap.error(f"--out: {e}")
    print(f"listen_ticks: {groups} port {port}, ssrcs {a.ssrc} -> {a.out}",
          file=sys.stderr, flush=True)
    try:
        run(sock, logger, a.ssrc)
    finally:
        logger.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
