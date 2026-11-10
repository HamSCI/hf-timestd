#!/usr/bin/env python3
"""Record a radiod channel and label every sample with UTC from the TS-1 pilot.

  label_record.py --channel 285821745 --status 239.133.73.189 \\
                  --pilot-status ac0g-b1-a-pilot-data.local --pilot-ssrc 45375 \\
                  --start 2026-11-08T23:40:00Z --seconds 60 --out DIR

Writes DIR/<ssrc>-<start>.cf32 (complex64, little-endian I,Q) holding the
samples whose UTC falls in [start, start + seconds), and a JSON sidecar
beside it that names the first sample's UTC, the epoch, the anchor and the
pilot report used, and how the label was made.

How a sample gets its UTC (all on radiod's input-sample counter):

1. The channel's own ANCHOR pair (ANCHOR_RTP, ANCHOR_INPUT_SAMPLE) names
   block k's first sample on both counters.  Output sample r then sits at
   input index ``in(r) = A_in + s32(r - A_rtp) * decim``.
2. The channel filter delays content by f_c = (FILTER_FIR_LENGTH - 1) / 2
   input samples (linear phase, overlap-save; ka9q-radio pilot_chan.c
   filter_delay_s).  So the signal in output sample r reached the ADC at
   input index ``in(r) - f_c``.
3. The latest LOCKED pilot report of the same INPUT_EPOCH (from the pilot
   channel's status) puts the TS-1 edge, raw, at PILOT_EDGE_SAMPLE +
   PILOT_EDGE_FRAC, which also runs one filter delay f_p late (the
   pilot channel reports f_p as PILOT_FILTER_DELAY).  The edge's content
   reached the ADC at ``E - f_p`` and radiod names its second
   PILOT_EDGE_UTC (GPS scale).
4. One GPSDO clocks the ADC, so the counter runs at INPUT_SAMPRATE per UTC
   second:  ``utc(r) = named + chain + ((in(r) - f_c) - (E - f_p)) / fs_in``.

When both channels share the front end's filter length, f_c = f_p and the
two delays cancel; the sidecar states both, and the labels never depend on
it silently.  ``chain`` (--chain-delay-ns, default 0) is the time from the
TS-1 edge leaving at the whole second to its arrival at the ADC; radiod
knows nothing of it, so it is the operator's assertion and is recorded.

The label is the "content" convention: the UTC at which the signal held in
a sample reached the ADC (plus nothing for the antenna-to-injector path).

Needs only the standard library, so it runs under any python3.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import select
import socket
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from radiod_pilot import tlv  # noqa: E402
from radiod_pilot.listen import (  # noqa: E402
    IP_MULTICAST_ALL, membership_request, parse_status)

GPS_TO_UNIX_NS = (315964800 - 18) * 10**9   # ka9q-radio timing.h; 18 leap seconds
STATUS_PORT = 5006
ENC_F32LE = 4
LABELING_CONVENTION = ("content: UTC at which the signal held in the sample reached "
                       "the ADC, taking the TS-1 edge to reach the ADC at the named "
                       "second + chain_delay_ns")
FORMULA = ("utc(r) = named_second + chain_delay + ((A_in + s32(r - A_rtp)*decim - f_chan)"
           " - (edge_sample + edge_frac - f_pilot)) / input_samprate")


def s32(x: int) -> int:
    x &= 0xFFFFFFFF
    return x - 0x100000000 if x >= 0x80000000 else x


# ---------------------------------------------------------------- RTP

def parse_rtp(pkt: bytes):
    """(ssrc, seq, timestamp, payload) or None for a non-RTP packet."""
    if len(pkt) < 12 or (pkt[0] >> 6) != 2:
        return None
    cc = pkt[0] & 0x0F
    seq, ts, ssrc = struct.unpack(">HII", pkt[2:12])
    off = 12 + 4 * cc
    if pkt[0] & 0x10:                      # header extension
        if len(pkt) < off + 4:
            return None
        off += 4 + 4 * struct.unpack(">H", pkt[off + 2:off + 4])[0]
    end = len(pkt)
    if pkt[0] & 0x20:                      # padding
        end -= pkt[-1]
    if off > end:
        return None
    return ssrc, seq, ts, pkt[off:end]


def assemble(packets, ref_rtp: int, frame_bytes: int = 8):
    """Packets -> (first output index, bytearray, gaps).

    ``packets`` holds (rtp timestamp, payload) in any order.  Each
    timestamp becomes an output index relative to ``ref_rtp`` (signed 32
    bit, so it survives the counter's wrap).  Missing spans are zero-filled
    and returned as gaps [(start index, frames)]; a duplicate is dropped.
    """
    if not packets:
        return None, bytearray(), []
    items = sorted({s32(ts - ref_rtp): p for ts, p in packets}.items())
    first = items[0][0]
    last = max(k + len(p) // frame_bytes for k, p in items)
    buf = bytearray((last - first) * frame_bytes)
    covered = 0
    gaps = []
    for k, p in items:
        n = len(p) // frame_bytes
        if k > first + covered:
            gaps.append((first + covered, k - first - covered))
        o = (k - first) * frame_bytes
        buf[o:o + n * frame_bytes] = p[:n * frame_bytes]
        covered = max(covered, k - first + n)
    return first, buf, gaps


# ------------------------------------------------------------- labels

def chan_filter_delay_input(status: dict) -> float:
    """Channel filter delay in input samples: (FILTER_FIR_LENGTH - 1) / 2."""
    m = status.get("filter_fir_length")
    if not m:
        raise ValueError("channel status carries no FILTER_FIR_LENGTH")
    return (m - 1) / 2.0


def decimation(status: dict) -> int:
    i, o = status.get("input_samprate"), status.get("output_samprate")
    if not i or not o or i % o:
        raise ValueError(f"no integer decimation from {i} / {o}")
    return i // o


def anchors_agree(anchors, decim: int):
    """True when every (epoch, A_rtp, A_in) of one epoch names the same tie."""
    if not anchors:
        return False
    e0, r0, i0 = anchors[0]
    return all(e == e0 and tlv.rtp_to_input(r, r0, i0, decim) == i for e, r, i in anchors)


def select_report(reports, epoch: int):
    """The latest locked pilot report of ``epoch``, or None."""
    ok = [r for r in reports if r.get("pilot_lock") and r.get("input_epoch") == epoch
          and "pilot_edge_sample" in r]
    return ok[-1] if ok else None


class Labeller:
    """UTC of output index k (k = s32(rtp - A_rtp)) on one channel.

    ``offset_ns(k)`` returns ns after the report's named second, as a float;
    ``named_unix_ns`` is that second as an integer.  Kept apart so the
    absolute label never passes through a float.
    """

    def __init__(self, *, anchor_input: int, decim: int, input_samprate: int,
                 f_chan: float, edge_sample: int, edge_frac: float, f_pilot: float,
                 edge_utc_gps_ns: int, chain_delay_ns: float = 0.0):
        self.anchor_input = anchor_input
        self.decim = decim
        self.fs_in = input_samprate
        self.f_chan = f_chan
        self.f_pilot = f_pilot
        self.edge_sample = edge_sample
        self.edge_frac = edge_frac
        self.named_unix_ns = edge_utc_gps_ns + GPS_TO_UNIX_NS
        self.chain_delay_ns = chain_delay_ns
        # input samples from the edge's content to output index 0's content
        self._base = (anchor_input - edge_sample) - (f_chan - f_pilot) - edge_frac

    def input_index(self, k: int) -> int:
        """radiod's input index for output index k (filter delay included)."""
        return self.anchor_input + k * self.decim

    def offset_ns(self, k: float) -> float:
        return self.chain_delay_ns + (self._base + k * self.decim) * 1e9 / self.fs_in

    def utc_ns(self, k: float) -> tuple:
        """(integer ns, fractional ns in [0, 1))."""
        off = self.offset_ns(k)
        whole = math.floor(off)
        return self.named_unix_ns + whole, off - whole

    def first_index_at_or_after(self, t_unix_ns: int) -> int:
        """Smallest output index whose label is >= t."""
        need = (t_unix_ns - self.named_unix_ns - self.chain_delay_ns) * self.fs_in / 1e9 - self._base
        k = math.ceil(need / self.decim)
        while self.offset_ns(k - 1) >= t_unix_ns - self.named_unix_ns:
            k -= 1
        while self.offset_ns(k) < t_unix_ns - self.named_unix_ns:
            k += 1
        return k

    @classmethod
    def from_status(cls, chan_status: dict, report: dict, chain_delay_ns: float = 0.0):
        decim = decimation(chan_status)
        if report.get("input_samprate") != chan_status.get("input_samprate"):
            raise ValueError("pilot and channel report different input sample rates")
        if report.get("input_epoch") != chan_status.get("input_epoch"):
            raise ValueError("pilot report and anchor come from different epochs")
        f_pilot_s = report.get("pilot_filter_delay")
        if f_pilot_s is None:
            raise ValueError("pilot report carries no PILOT_FILTER_DELAY")
        return cls(anchor_input=chan_status["anchor_input_sample"], decim=decim,
                   input_samprate=chan_status["input_samprate"],
                   f_chan=chan_filter_delay_input(chan_status),
                   edge_sample=report["pilot_edge_sample"],
                   edge_frac=report["pilot_edge_frac"],
                   f_pilot=f_pilot_s * report["input_samprate"],
                   edge_utc_gps_ns=report["pilot_edge_utc"],
                   chain_delay_ns=chain_delay_ns)


def cut(first_k: int, buf: bytes, lab: Labeller, start_unix_ns: int, seconds: int,
        frame_bytes: int = 8):
    """The slice of ``buf`` (index ``first_k`` onwards) labelled in
    [start, start + seconds): (k0, bytes), or ValueError when the buffer
    does not cover the whole window."""
    k0 = lab.first_index_at_or_after(start_unix_ns)
    k1 = lab.first_index_at_or_after(start_unix_ns + seconds * 10**9)
    last = first_k + len(buf) // frame_bytes
    if k0 < first_k or k1 > last:
        raise ValueError(f"recording covers output index {first_k}..{last}, "
                         f"window needs {k0}..{k1}")
    return k0, bytes(buf[(k0 - first_k) * frame_bytes:(k1 - first_k) * frame_bytes])


def old_pair_offset_ns(status: dict, k_rtp: int, label_unix_ns: int, label_frac: float) -> float:
    """radiod's existing label minus ours for output sample at RTP k_rtp:
    GPS_TIME + s32(rtp - RTP_TIMESNAP) / fs_out, as a client reads it today."""
    whole = status["gps_time"] + GPS_TO_UNIX_NS - label_unix_ns    # exact, in int
    return whole + s32(k_rtp - status["rtp_timesnap"]) * 1e9 / status["output_samprate"] \
        - label_frac


def build_sidecar(*, chan_status: dict, report: dict, lab: Labeller, k0: int, n_frames: int,
                  start_unix_ns: int, seconds: int, anchors, gaps, n_packets: int,
                  data_dest, reports_seen: int, report_spread_ns) -> dict:
    a_rtp = chan_status["anchor_rtp"]
    rtp0 = (a_rtp + k0) & 0xFFFFFFFF
    u_int, u_frac = lab.utc_ns(k0)
    return {
        "schema": "radiod-pilot-label/1",
        "labeling_convention": LABELING_CONVENTION,
        "formula": FORMULA,
        "ssrc": chan_status.get("ssrc"),
        "frequency_hz": chan_status.get("frequency"),
        "output_samprate": chan_status.get("output_samprate"),
        "input_samprate": chan_status.get("input_samprate"),
        "decimation": lab.decim,
        "sample_format": "complex64 little-endian (I, Q float32)",
        "data_dest": list(data_dest) if data_dest else None,
        "window": {"start_unix_ns": start_unix_ns, "seconds": seconds,
                   "start_utc": iso(start_unix_ns)},
        "n_samples": n_frames,
        "first_sample": {
            "rtp": rtp0,
            "output_index_from_anchor": k0,
            "radiod_input_index": lab.input_index(k0),
            "content_input_index": lab.input_index(k0) - lab.f_chan,
            "utc_unix_ns": u_int,
            "utc_frac_ns": u_frac,
            "utc": iso(u_int),
            "after_window_start_ns": (u_int - start_unix_ns) + u_frac,
            "old_pair_minus_label_ns": old_pair_offset_ns(chan_status, rtp0, u_int, u_frac)
            if "gps_time" in chan_status and "rtp_timesnap" in chan_status else None,
        },
        "sample_period_ns": 1e9 / chan_status["output_samprate"],
        "epoch": chan_status["input_epoch"],
        "anchor": {"rtp": a_rtp, "input_sample": chan_status["anchor_input_sample"],
                   "n_seen": len(anchors), "all_agree": anchors_agree(anchors, lab.decim)},
        "filter_delay": {
            "channel_input_samples": lab.f_chan,
            "channel_s": lab.f_chan / lab.fs_in,
            "channel_source": "(FILTER_FIR_LENGTH - 1) / 2 from the channel's status",
            "filter_fir_length": chan_status.get("filter_fir_length"),
            "pilot_input_samples": lab.f_pilot,
            "pilot_s": report.get("pilot_filter_delay"),
            "pilot_source": "PILOT_FILTER_DELAY from the pilot report",
            "handling": "subtracted from both positions: the channel's sample and the raw "
                        "edge each run one filter delay late; the label uses their difference",
            "net_input_samples": lab.f_chan - lab.f_pilot,
        },
        "chain_delay_ns": lab.chain_delay_ns,
        "pilot_report": {k: report.get(k) for k in (
            "ssrc", "input_epoch", "pilot_edge_sample", "pilot_edge_frac", "pilot_edge_utc",
            "pilot_host_time", "pilot_edge_sigma", "pilot_snr", "pilot_lock",
            "pilot_fold_count", "pilot_filter_delay", "pilot_battery", "received_unix",
            "group")},
        "pilot_report_named_unix_ns": lab.named_unix_ns,
        "pilot_reports_seen": reports_seen,
        "pilot_report_spread_ns": report_spread_ns,
        "packets": n_packets,
        "gaps": [{"output_index": g, "frames": n} for g, n in gaps],
        "gap_frames_in_window": sum(max(0, min(g + n, k0 + n_frames) - max(g, k0))
                                    for g, n in gaps),
    }


def report_spread_ns(lab: Labeller, chan_status: dict, reports, k: int, chain_delay_ns: float):
    """How far the other locked reports of the epoch would move the label of
    index k: (min, max) ns relative to the chosen report, over distinct folds."""
    base = lab.utc_ns(k)
    out = []
    seen = set()
    for r in reports:
        key = (r.get("pilot_edge_sample"), r.get("pilot_edge_frac"))
        if not r.get("pilot_lock") or r.get("input_epoch") != chan_status["input_epoch"] \
                or key in seen:
            continue
        seen.add(key)
        other = Labeller.from_status(chan_status, r, chain_delay_ns).utc_ns(k)
        out.append((other[0] - base[0]) + (other[1] - base[1]))
    return {"n_folds": len(out), "min": min(out), "max": max(out)} if out else None


def iso(unix_ns: int) -> str:
    s, ns = divmod(unix_ns, 10**9)
    return dt.datetime.fromtimestamp(s, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + f".{ns:09d}Z"


def parse_start(text: str) -> int:
    """UTC ISO time (whole second) or 'next' -> unix ns."""
    if text == "next":
        return (int(time.time()) + 5) * 10**9
    t = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    if t.microsecond:
        raise ValueError("start must be a whole UTC second")
    return int(t.timestamp()) * 10**9


# ---------------------------------------------------------------- I/O

def _mcast_socket(port: int, groups, iface=None):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except (AttributeError, OSError):
        pass
    s.setsockopt(socket.IPPROTO_IP, IP_MULTICAST_ALL, 0)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
    s.bind(("", port))
    for g in groups:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership_request(g, iface))
    s.setblocking(False)
    return s


def record(a) -> int:
    start_ns = parse_start(a.start)
    chan_group = parse_status(a.status)[0]
    pilot_group = parse_status(a.pilot_status)[0]
    st = _mcast_socket(STATUS_PORT, sorted({chan_group, pilot_group}), a.iface)
    log = sys.stderr
    print(f"label_record: ssrc {a.channel} status {chan_group}, pilot {a.pilot_ssrc} on "
          f"{pilot_group}; window {iso(start_ns)} + {a.seconds} s", file=log, flush=True)

    chan_status = None
    chan_full = {}
    anchors, reports, statuses = [], [], []
    data_sock = None
    data_dest = None
    packets = []
    t_begin = start_ns / 1e9 - a.margin
    t_end = start_ns / 1e9 + a.seconds + a.margin
    deadline_status = time.time() + 30
    while True:
        now = time.time()
        if now >= t_end and chan_status is not None and select_report(
                [r for r in reports if r["received_unix"] >= t_end - a.margin],
                chan_status["input_epoch"]):
            break
        if now > t_end + 60:
            print("label_record: no locked pilot report after the window", file=log)
            return 3
        if chan_status is None and now > deadline_status:
            print("label_record: no status for the channel in 30 s", file=log)
            return 3
        socks = [st] + ([data_sock] if data_sock else [])
        r, _, _ = select.select(socks, [], [], 0.5)
        for s in r:
            while True:
                try:
                    pkt = s.recv(65536)
                except BlockingIOError:
                    break
                if s is st:
                    try:
                        d = tlv.decode(pkt)
                    except ValueError:
                        continue
                    d["received_unix"] = time.time()
                    if d.get("ssrc") == a.pilot_ssrc and "pilot_edge_sample" in d:
                        reports.append(d)
                    if d.get("ssrc") == a.channel and all(
                            k in d for k in ("input_epoch", "anchor_rtp", "anchor_input_sample")):
                        # A short status updates the anchor; fields only a full one carries
                        # (encoding, data group, filter length) persist from the last full one
                        if "output_encoding" in d:
                            chan_full = d
                        d = {**chan_full, **d}
                        chan_status = d
                        statuses.append(d)
                        if t_begin <= d["received_unix"] <= t_end:
                            anchors.append((d["input_epoch"], d["anchor_rtp"],
                                            d["anchor_input_sample"]))
                        # radiod's periodic short status carries the anchor but not the
                        # encoding or data group; only a full status (a poll reply) does
                        if data_sock is None and "output_encoding" in d:
                            if d.get("output_encoding") != ENC_F32LE or d.get("output_channels") != 2:
                                print(f"label_record: channel encoding {d.get('output_encoding')} "
                                      f"channels {d.get('output_channels')}; need F32LE IQ",
                                      file=log)
                                return 2
                            data_dest = d["data_dest"]
                            data_sock = _mcast_socket(data_dest[1], [data_dest[0]], a.iface)
                            print(f"label_record: data {data_dest[0]}:{data_dest[1]}",
                                  file=log, flush=True)
                else:
                    if not t_begin <= time.time() <= t_end:
                        continue
                    p = parse_rtp(pkt)
                    if p and p[0] == a.channel:
                        packets.append((p[2], p[3]))

    epochs = {e for e, _, _ in anchors}
    if len(epochs) != 1:
        print(f"label_record: INPUT_EPOCH changed during the recording {sorted(epochs)}; "
              "refusing to label across it", file=log)
        return 4
    if not anchors_agree(anchors, decimation(chan_status)):
        print("label_record: the channel's anchors disagree within one epoch", file=log)
        return 4
    report = select_report(reports, chan_status["input_epoch"])
    if report is None:
        print("label_record: no locked pilot report of the channel's epoch", file=log)
        return 4
    lab = Labeller.from_status(chan_status, report, a.chain_delay_ns)
    first_k, buf, gaps = assemble(packets, chan_status["anchor_rtp"])
    if first_k is None:
        print("label_record: no RTP packets", file=log)
        return 4
    try:
        k0, data = cut(first_k, buf, lab, start_ns, a.seconds)
    except ValueError as e:
        print(f"label_record: {e}", file=log)
        return 4
    n = len(data) // 8
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{a.channel}-{iso(start_ns)[:19].replace('-', '').replace(':', '')}Z"
    (out / f"{stem}.cf32").write_bytes(data)
    side = build_sidecar(chan_status=chan_status, report=report, lab=lab, k0=k0, n_frames=n,
                         start_unix_ns=start_ns, seconds=a.seconds, anchors=anchors, gaps=gaps,
                         n_packets=len(packets), data_dest=data_dest,
                         reports_seen=len(reports),
                         report_spread_ns=report_spread_ns(lab, chan_status, reports, k0,
                                                           a.chain_delay_ns))
    side["file"] = f"{stem}.cf32"
    side["channel_status"] = {k: v for k, v in chan_status.items()
                              if k not in ("pilot_battery",)}
    with open(out / f"{stem}.json", "w") as f:
        json.dump(side, f, indent=1, default=str)
    if a.keep_status:
        with open(out / f"{stem}.pilot-reports.jsonl", "w") as f:
            for r in reports:
                f.write(json.dumps(r) + "\n")
        with open(out / f"{stem}.status.jsonl", "w") as f:
            for r in statuses:
                f.write(json.dumps(r) + "\n")
    print(f"label_record: wrote {out / stem}.cf32: {n} samples, first at "
          f"{side['first_sample']['utc']} (+{side['first_sample']['utc_frac_ns']:.3f} ns), "
          f"{side['gap_frames_in_window']} zero-filled", file=log)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--channel", type=int, required=True, help="SSRC to record")
    ap.add_argument("--status", required=True,
                    help="group where the channel's periodic status arrives (its data group)")
    ap.add_argument("--pilot-status", required=True, help="the pilot channel's data group")
    ap.add_argument("--pilot-ssrc", type=int, default=45375)
    ap.add_argument("--start", required=True, help="UTC, whole second (ISO) or 'next'")
    ap.add_argument("--seconds", type=int, required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--chain-delay-ns", type=float, default=0.0,
                    help="TS-1 edge to ADC, asserted (default 0)")
    ap.add_argument("--margin", type=float, default=3.0,
                    help="seconds captured either side of the window (host clock)")
    ap.add_argument("--iface")
    ap.add_argument("--keep-status", action="store_true",
                    help="also write every pilot report and channel status received")
    a = ap.parse_args(argv)
    return record(a)


if __name__ == "__main__":
    sys.exit(main())
