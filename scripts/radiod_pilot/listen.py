#!/usr/bin/env python3
"""Log radiod's pilot reports for one channel to JSON lines.

  listen.py --status 239.1.2.3:5006 --ssrc 10000 --out pilot.jsonl

Joins the status multicast group with a plain socket, decodes each
status packet for the SSRC and writes one line for every packet that
carries a pilot report (a PILOT_* group beside the anchor), adding
``logged_at_unix``.  Runs until interrupted.
"""
import argparse
import json
import socket
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from radiod_pilot import tlv  # noqa: E402


def open_socket(group: str, port: int) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except (AttributeError, OSError):
        pass
    s.bind(("", port))
    mreq = struct.pack("4s4s", socket.inet_aton(group), socket.inet_aton("0.0.0.0"))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    return s


def has_pilot_report(d: dict) -> bool:
    return "pilot_edge_sample" in d


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--status", required=True, metavar="MCAST:PORT")
    ap.add_argument("--ssrc", required=True, type=int)
    ap.add_argument("--out", required=True, metavar="FILE.jsonl")
    a = ap.parse_args(argv)
    group, _, port = a.status.rpartition(":")
    sock = open_socket(group, int(port))
    with open(a.out, "a", buffering=1) as f:
        try:
            while True:
                pkt, _ = sock.recvfrom(65536)
                try:
                    d = tlv.decode(pkt)
                except ValueError:
                    continue
                if d.get("ssrc") != a.ssrc or not has_pilot_report(d):
                    continue
                d["logged_at_unix"] = time.time()
                f.write(json.dumps(d) + "\n")
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
