#!/usr/bin/env python3
"""Log radiod's pilot reports and channel anchors to JSON lines.

  listen.py --status ac0g-b1-a-pilot-data.local --status 239.133.73.189 \\
            --ssrc 45375 --anchor-ssrc 206674315 --out ledger-a.jsonl \\
            [--iface enp1s0]

Joins the multicast groups with a plain socket and decodes each status
packet.  radiod sends a channel's periodic status (every ``update``
blocks) to the channel's DATA group, port 5006; the status group carries
only replies to polls.  Two kinds of row, each the decoded packet plus ``kind``
and ``logged_at_unix``:

* ``kind: "pilot"``: every packet for ``--ssrc`` that carries a pilot
  report (a PILOT_* group beside the anchor).  Unthrottled, so each row
  also carries that packet's GPS_TIME/RTP_TIMESNAP/INPUT_SAMPLES.
* ``kind: "anchor"``: a packet for ``--ssrc`` without a pilot report, or
  for any ``--anchor-ssrc``, that carries the channel's
  INPUT_EPOCH/ANCHOR_RTP/ANCHOR_INPUT_SAMPLE.  At most one per SSRC
  every ``--anchor-every`` seconds, except that a new epoch, or a tie
  that moved (the anchor's input index no longer follows from the last
  logged anchor by the decimation), is logged at once.

Meant to run for days: socket and write errors go to stderr and the
loop carries on.  Runs until interrupted.
"""
import argparse
import ipaddress
import json
import socket
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from radiod_pilot import tlv  # noqa: E402

DEFAULT_STATUS_PORT = 5006
SILENCE_S = 60.0


def parse_status(text: str, resolve=None) -> tuple:
    """``GROUP[:PORT]`` -> (multicast IPv4 address, port).  ValueError if bad.

    GROUP may be a name (``ac0g-b1-a-status.local``) or a dotted address.
    """
    resolve = resolve or (lambda h: socket.getaddrinfo(
        h, None, socket.AF_INET, socket.SOCK_DGRAM)[0][4][0])
    host, sep, port_s = text.rpartition(":")
    if not sep:
        host, port_s = text, str(DEFAULT_STATUS_PORT)
    if not host:
        raise ValueError(f"no group in {text!r}")
    try:
        port = int(port_s)
    except ValueError:
        raise ValueError(f"bad port {port_s!r}") from None
    if not 0 < port < 65536:
        raise ValueError(f"port {port} out of range")
    try:
        addr = str(ipaddress.IPv4Address(host))
    except ValueError:
        addr = None
    try:
        addr = addr or resolve(host)
    except (OSError, IndexError) as e:
        raise ValueError(f"cannot resolve {host!r}: {e}") from None
    if not ipaddress.IPv4Address(addr).is_multicast:
        raise ValueError(f"{host} -> {addr} is not a multicast group")
    return addr, port


def membership_request(group: str, iface) -> bytes:
    """IP_ADD_MEMBERSHIP argument: any interface, an address, or a name."""
    g = socket.inet_aton(group)
    if not iface:
        return g + socket.inet_aton("0.0.0.0")
    try:
        return g + socket.inet_aton(str(ipaddress.IPv4Address(iface)))
    except ValueError:
        pass
    try:
        idx = socket.if_nametoindex(iface)
    except OSError:
        raise ValueError(f"no interface {iface!r}") from None
    return struct.pack("4s4si", g, socket.inet_aton("0.0.0.0"), idx)


IP_MULTICAST_ALL = getattr(socket, "IP_MULTICAST_ALL", 49)
IP_PKTINFO = getattr(socket, "IP_PKTINFO", 8)


def open_socket(groups, port: int, iface=None) -> socket.socket:
    """One socket joined to every group in ``groups``, all on ``port``.

    IP_MULTICAST_ALL off: Linux otherwise hands a socket bound to the
    wildcard address every group any process on the host has joined,
    and B1-a and B1-b both send SSRC 45375.  IP_PKTINFO on, so each row
    can name the group its packet came to.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except (AttributeError, OSError):
        pass
    for opt in (IP_MULTICAST_ALL, IP_PKTINFO):
        try:
            s.setsockopt(socket.IPPROTO_IP, opt, 0 if opt == IP_MULTICAST_ALL else 1)
        except OSError:
            pass
    s.bind(("", port))
    for g in groups:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership_request(g, iface))
    s.settimeout(SILENCE_S)
    return s


def receive(sock):
    """(packet, destination group or None)."""
    data, anc, _flags, _addr = sock.recvmsg(65536, 256)
    group = None
    for level, typ, cdata in anc:
        if level == socket.IPPROTO_IP and typ == IP_PKTINFO and len(cdata) >= 12:
            group = socket.inet_ntoa(cdata[8:12])
    return data, group


def has_pilot_report(d: dict) -> bool:
    return "pilot_edge_sample" in d


def has_anchor(d: dict) -> bool:
    return all(k in d for k in ("input_epoch", "anchor_rtp", "anchor_input_sample"))


def _decim(d: dict):
    i, o = d.get("input_samprate"), d.get("output_samprate")
    if i and o and i % o == 0:
        return i // o
    return None


class RowFilter:
    """Chooses which decoded packets become rows."""

    def __init__(self, pilot_ssrc: int, anchor_ssrcs, anchor_every_s: float):
        self.pilot_ssrc = pilot_ssrc
        self.anchor_ssrcs = set(anchor_ssrcs) | {pilot_ssrc}
        self.anchor_every_s = anchor_every_s
        self._last = {}     # ssrc -> (time, epoch, anchor_rtp, anchor_input)

    def _anchor_due(self, d: dict, now: float) -> bool:
        last = self._last.get(d["ssrc"])
        if last is None:
            return True
        t, epoch, a_rtp, a_in = last
        if d["input_epoch"] != epoch:
            return True
        decim = _decim(d)
        if decim is not None and tlv.rtp_to_input(
                d["anchor_rtp"], a_rtp, a_in, decim) != d["anchor_input_sample"]:
            return True
        return now - t >= self.anchor_every_s

    def select(self, d: dict, now: float):
        ssrc = d.get("ssrc")
        if ssrc == self.pilot_ssrc and has_pilot_report(d):
            kind = "pilot"
        elif ssrc in self.anchor_ssrcs and has_anchor(d) and self._anchor_due(d, now):
            kind = "anchor"
        else:
            return None
        if has_anchor(d):
            self._last[ssrc] = (now, d["input_epoch"], d["anchor_rtp"], d["anchor_input_sample"])
        d["kind"] = kind
        d["logged_at_unix"] = now
        return d


def run(sock, out, filt: RowFilter, now_fn=time.time, err=sys.stderr,
        sleep_fn=time.sleep) -> int:
    """Read packets until KeyboardInterrupt; return the number of rows written."""
    written = 0
    while True:
        try:
            pkt, group = receive(sock)
        except KeyboardInterrupt:
            return written
        except socket.timeout:
            print(f"listen: no status packet in {SILENCE_S:.0f} s", file=err, flush=True)
            continue
        except OSError as e:
            print(f"listen: recvmsg: {e}", file=err, flush=True)
            sleep_fn(1.0)
            continue
        try:
            d = tlv.decode(pkt)
        except ValueError:
            continue
        row = filt.select(d, now_fn())
        if row is None:
            continue
        if group is not None:
            row["group"] = group
        try:
            out.write(json.dumps(row) + "\n")
            out.flush()
            written += 1
        except OSError as e:
            print(f"listen: write: {e}", file=err, flush=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--status", required=True, action="append", metavar="GROUP[:PORT]",
                    help="status multicast group, name or address (port "
                         f"{DEFAULT_STATUS_PORT}); repeatable, one port for all.  "
                         "radiod sends a channel's periodic status to its DATA group")
    ap.add_argument("--ssrc", required=True, type=int, help="the pilot channel's SSRC")
    ap.add_argument("--anchor-ssrc", type=int, action="append", default=[],
                    help="also log anchor rows for this SSRC (repeatable)")
    ap.add_argument("--anchor-every", type=float, default=1.0, metavar="S",
                    help="seconds between anchor rows per SSRC (default 1)")
    ap.add_argument("--iface", help="interface name or address for the multicast join")
    ap.add_argument("--out", required=True, metavar="FILE.jsonl")
    a = ap.parse_args(argv)
    try:
        addrs = [parse_status(x) for x in a.status]
    except ValueError as e:
        ap.error(f"--status: {e}")
    ports = {p for _, p in addrs}
    if len(ports) != 1:
        ap.error(f"--status: one port for all groups, got {sorted(ports)}")
    port = ports.pop()
    groups = [g for g, _ in addrs]
    try:
        sock = open_socket(groups, port, a.iface)
    except ValueError as e:
        ap.error(f"--iface: {e}")
    except OSError as e:
        ap.error(f"cannot join {groups} port {port}: {e}")
    filt = RowFilter(a.ssrc, a.anchor_ssrc, a.anchor_every)
    print(f"listen: {a.status} = {groups} port {port}, pilot {a.ssrc}, anchors {a.anchor_ssrc}, "
          f"-> {a.out}", file=sys.stderr, flush=True)
    with open(a.out, "a", buffering=1) as f:
        run(sock, f, filt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
