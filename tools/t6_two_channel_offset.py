#!/usr/bin/env python3
"""Time difference between two TS-1 BPSK pilots, by the production fold + fit.

    t6_two_channel_offset.py record --status WB6CXC-7-status.local \\
        --ref-hz 45375000 --test-hz 28600000 --seconds 300 --out DIR
    t6_two_channel_offset.py analyze DIR [--fold 30] [--no-cfo]

WHY
---
Paul Elliott (WB6CXC) runs two TS-1s.  One injects its BPSK PPS straight into
the RX888, as on every T6 station (84.225 MHz, aliased to 45.375 MHz at
129.6 Msps).  The other radiates an identically produced pilot on 28.6 MHz
from an antenna.  This tool decodes both with the SAME technique T6 uses --
``BpskEdgeFineStage``, the coherent fold plus zero-crossing fit
(docs/design/T6_NEWELL_VS_FOLD.md) -- and reports the time between the two
edges.

HOW THE TWO CHANNELS SHARE A TIMEBASE
-------------------------------------
Both channels come from one RX888, so they share its sample clock exactly.
They do NOT share an RTP origin: each radiod channel counts RTP from its own
first block (ops memory, RTP origin regression, 2026-10-06), so their RTP
stamps differ by a whole number of blocks.  A block holds
``olen = sample_rate * blocktime`` output samples (1920 at 96 kHz and 20 ms).

So the raw difference of the two edge positions equals the true difference
plus ``n * olen``.  This tool removes the ``n * olen`` term in two steps:

1. Reduce modulo 2**32 into a signed value.  The RTP counter is 32 bits wide,
   and each fold stage unwraps from wherever its own channel started.
2. Reduce modulo ``olen`` into (-olen/2, +olen/2].

⛔ Step 1 is exact only if the two channels started less than 2**31 samples
apart (6.2 h at 96 kHz).  2**32 is NOT a multiple of olen (it leaves 256 at
1920), so a channel that started hours before the other would leave a
256-sample error per wrap.  That is why ``record`` creates BOTH channels
itself, seconds apart, and never borrows hf-timestd's long-running T6 channel.

Step 2 assumes the true difference lies within half a block (+-10 ms).  The
two edges come from GPS-referenced TS-1s, so it should lie within
microseconds.

WHAT THE DIFFERENCE CONTAINS
----------------------------
Both channels use one preset, one sample rate and one filter, so radiod's
filter delay cancels.  What remains:
- the injected path: TS-1 #1, its cable, and the RX888 front end at 84.225 MHz
  (in the stopband of its 60 MHz low-pass);
- the radiated path: TS-1 #2, its transmit chain, the air path (3.3 ns per
  metre), the antenna and feedline, and the front end at 28.6 MHz;
- any difference between the two TS-1s' own timing.

THE NULL TEST -- run it first
-----------------------------
``record --test-hz 45375000`` creates two fresh channels on the SAME pilot.
They start at different blocks, so the raw difference shows ``n * olen``;
after reduction it must read near 0 us.  If it does not, nothing this tool
says about 28.6 MHz can be trusted.

CARRIER OFFSET
--------------
The fold sums 30 seconds coherently, so a carrier that drifts against the
RX888's clock cancels itself (fold_retention -> 0).  The injected pilot
shares the GPSDO, so it stays coherent.  A second TS-1 on its own reference
might not.  ``analyze`` estimates each channel's residual carrier frequency
from the squared signal (squaring removes the +-1 BPSK) and derotates it out
before folding.  It does the same to both channels.  A pure phase rotation
moves no edge.  ``--no-cfo`` skips it.
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
import threading
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from hf_timestd.core.bpsk_edge_fine_stage import BpskEdgeFineStage  # noqa: E402

WRAP = 1 << 32
CLIENT_ID = "t6-two-channel"
# Frames of radiod's main loop (~50/s at 20 ms): 60 s.  Refreshed every 15 s,
# so the channels vanish within a minute of this tool stopping, however it stops.
LIFETIME_FRAMES = 3000
REFRESH_S = 15.0


# --------------------------------------------------------------------- analysis

def edge_position(est) -> float:
    """An estimate's edge, in its own channel's unwrapped RTP samples."""
    return est.edge_rtp + est.edge_subsample


def reduce_difference(raw: float, olen: int) -> tuple[float, float]:
    """(signed mod-2**32 difference, difference reduced into one block).

    See the module docstring for why both steps are needed and when the
    first is exact.
    """
    d32 = ((raw + WRAP / 2) % WRAP) - WRAP / 2
    red = ((d32 + olen / 2) % olen) - olen / 2
    return d32, red


def carrier_offset_hz(x: np.ndarray, sr: int) -> float:
    """Residual carrier frequency of a BPSK signal, from x**2.

    Squaring removes the +-1 modulation and leaves a tone at twice the
    offset.  A zero-padded FFT finds its bin; a parabola through the peak
    and its neighbours refines it.
    """
    # Decimate first: the tone sits within a few Hz of 0, and a 1 kHz
    # rate keeps the FFT small.
    dec = max(1, sr // 1000)
    y = (x.astype(np.complex128) ** 2)
    n = len(y) // dec
    y = y[: n * dec].reshape(n, dec).mean(axis=1)
    fs = sr / dec
    nfft = 1 << int(np.ceil(np.log2(n * 4)))
    spec = np.abs(np.fft.fft(y, nfft))
    k = int(np.argmax(spec))
    a, b, c = spec[(k - 1) % nfft], spec[k], spec[(k + 1) % nfft]
    denom = a - 2 * b + c
    frac = 0.5 * (a - c) / denom if denom else 0.0
    f2 = ((k + frac + nfft / 2) % nfft - nfft / 2) * fs / nfft
    return f2 / 2.0


def derotate(x: np.ndarray, sr: int, f_hz: float) -> np.ndarray:
    t = np.arange(len(x)) / sr
    return (x * np.exp(-2j * np.pi * f_hz * t)).astype(np.complex64)


def fold_channel(x: np.ndarray, batches: list[tuple[int, int]], sr: int,
                 fold_seconds: int) -> list:
    """Feed the recording to the production stage exactly as the recorder
    does: batch by batch, each labelled with its delivered first-sample RTP."""
    stage = BpskEdgeFineStage(sample_rate=sr, fold_seconds=fold_seconds)
    out, i = [], 0
    for rtp, n in batches:
        e = stage.process_samples(x[i:i + n], rtp)
        if e is not None:
            out.append(e)
        i += n
    return out


def pair_blocks(ref: list, test: list, olen: int) -> list[dict]:
    """Difference block k of the test channel against block k of the ref."""
    rows = []
    for k, (r, t) in enumerate(zip(ref, test)):
        raw = edge_position(t) - edge_position(r)
        d32, red = reduce_difference(raw, olen)
        rows.append({
            "block": k,
            "raw_samples": raw,
            "blocks_apart": d32 / olen,
            "delta_samples": red,
            "ref_retention": r.fold_retention,
            "test_retention": t.fold_retention,
            "ref_fidelity": r.peak_prominence,
            "test_fidelity": t.peak_prominence,
        })
    return rows


def summarise(rows: list[dict], sr: int) -> dict:
    d = np.array([r["delta_samples"] for r in rows]) / sr * 1e6
    s = {"n": len(d), "mean_us": float(np.mean(d)) if len(d) else float("nan")}
    if len(d) >= 2:
        s["sd_us"] = float(np.std(d, ddof=1))
        s["sem_us"] = s["sd_us"] / np.sqrt(len(d))
    return s


def load_channel(directory: Path, name: str):
    x = np.fromfile(directory / f"{name}.c64", dtype=np.complex64)
    batches = [tuple(int(v) for v in line.split())
               for line in (directory / f"{name}.batches").read_text().splitlines()
               if line.strip()]
    total = sum(n for _, n in batches)
    if total != len(x):
        sys.exit(f"{name}: batches cover {total} samples, file holds {len(x)}")
    return x, batches


def analyze(directory: Path, fold_seconds: int, cfo: bool) -> dict:
    meta = json.loads((directory / "meta.json").read_text())
    sr, olen = int(meta["sample_rate"]), int(meta["olen"])
    ests, report = {}, {"meta": meta, "channels": {}}
    for name in ("ref", "test"):
        x, batches = load_channel(directory, name)
        f = carrier_offset_hz(x, sr) if cfo else 0.0
        if cfo:
            x = derotate(x, sr, f)
        ests[name] = fold_channel(x, batches, sr, fold_seconds)
        report["channels"][name] = {
            "hz": meta[f"{name}_hz"], "seconds": len(x) / sr,
            "carrier_offset_hz": f, "blocks": len(ests[name]),
        }
    rows = pair_blocks(ests["ref"], ests["test"], olen)
    report["blocks"] = rows
    report["summary"] = summarise(rows, sr)
    return report


def print_report(rep: dict) -> None:
    m, sr = rep["meta"], int(rep["meta"]["sample_rate"])
    print(f"ref  {m['ref_hz'] / 1e6:.6f} MHz   test {m['test_hz'] / 1e6:.6f} MHz   "
          f"{sr} Hz   block {m['olen']} samples")
    for name, c in rep["channels"].items():
        print(f"  {name:4s} {c['seconds']:7.1f} s   carrier offset "
              f"{c['carrier_offset_hz']:+.4f} Hz   fold blocks {c['blocks']}")
    print("\n  blk   blocks apart    delta (us)   retention ref/test   fidelity ref/test")
    for r in rep["blocks"]:
        print(f"  {r['block']:3d}  {r['blocks_apart']:+13.4f}  "
              f"{r['delta_samples'] / sr * 1e6:+12.4f}   "
              f"{r['ref_retention']:.4f} / {r['test_retention']:.4f}   "
              f"{r['ref_fidelity']:.2e} / {r['test_fidelity']:.2e}")
    s = rep["summary"]
    print(f"\n  test - ref = {s['mean_us']:+.4f} us"
          + (f"   sd {s['sd_us']:.4f} us   sem {s['sem_us']:.4f} us   n={s['n']}"
             if "sd_us" in s else f"   n={s['n']}"))
    print("  (positive: the test pilot's edge arrives LATER than the ref's)")
    print("  'blocks apart' should sit near a whole number; its fraction is the delta.")
    print("  Low retention (well under ~0.9) means the carrier did not stay coherent"
          " over the fold; that block's edge cannot be trusted.")


# ---------------------------------------------------------------------- record

class _Sink:
    """Appends one channel's samples and batch labels to disk."""

    def __init__(self, directory: Path, name: str):
        self.iq = open(directory / f"{name}.c64", "wb")
        self.labels = open(directory / f"{name}.batches", "w")
        self.samples = 0
        self.lock = threading.Lock()

    def __call__(self, samples, quality):
        rtp = getattr(quality, "delivered_rtp_start", None)
        if rtp is None:
            rtp = getattr(quality, "last_rtp_timestamp", None)
        if rtp is None or len(samples) == 0:
            return
        with self.lock:
            self.iq.write(np.asarray(samples, dtype=np.complex64).tobytes())
            self.labels.write(f"{int(rtp) & 0xFFFFFFFF} {len(samples)}\n")
            self.samples += len(samples)

    def close(self):
        with self.lock:
            self.iq.close()
            self.labels.close()


def record(args) -> None:
    from ka9q import Encoding, RadiodControl, RadiodStream

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=False)
    olen = int(round(args.sample_rate * args.blocktime_ms / 1000))
    # One client id per channel.  ka9q-python derives the SSRC from the
    # request and the client id, so in the null test (both at 45.375 MHz) a
    # shared id would hand back ONE channel twice.  Either id also keeps these
    # channels apart from hf-timestd's own T6 channel, which this tool never
    # touches.
    controls = {name: RadiodControl(args.status, client_id=f"{CLIENT_ID}-{name}")
                for name in ("ref", "test")}

    channels = {}
    for name, hz in (("ref", args.ref_hz), ("test", args.test_hz)):
        # Same request for both, so radiod gives them one filter and one delay.
        channels[name] = controls[name].ensure_channel(
            frequency_hz=hz, preset="iq", sample_rate=args.sample_rate,
            encoding=Encoding.F32, agc_enable=0, gain=args.gain_db,
            low_edge=-args.half_width_hz, high_edge=args.half_width_hz,
            timeout=30.0, lifetime=LIFETIME_FRAMES)
    if channels["ref"].ssrc == channels["test"].ssrc:
        sys.exit("ref and test resolved to ONE channel; nothing to compare")

    meta = {
        "ref_hz": args.ref_hz, "test_hz": args.test_hz,
        "sample_rate": args.sample_rate, "olen": olen,
        "blocktime_ms": args.blocktime_ms, "gain_db": args.gain_db,
        "half_width_hz": args.half_width_hz, "status": args.status,
        "ssrc": {k: c.ssrc for k, c in channels.items()},
        "encoding": {k: c.encoding for k, c in channels.items()},
        "started_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    sinks = {k: _Sink(out, k) for k in channels}
    streams = {k: RadiodStream(channel=c, on_samples=sinks[k],
                               samples_per_packet=200,
                               resequence_buffer_size=256)
               for k, c in channels.items()}
    for s in streams.values():
        s.start()

    deadline = time.monotonic() + args.seconds
    next_refresh = time.monotonic() + REFRESH_S
    try:
        while time.monotonic() < deadline:
            time.sleep(1.0)
            if time.monotonic() >= next_refresh:
                for k, c in channels.items():
                    controls[k].set_channel_lifetime(c.ssrc, LIFETIME_FRAMES)
                next_refresh += REFRESH_S
            got = {k: s.samples / args.sample_rate for k, s in sinks.items()}
            print(f"\r  ref {got['ref']:7.1f} s   test {got['test']:7.1f} s",
                  end="", flush=True)
    finally:
        print()
        for s in streams.values():
            s.stop()
        for s in sinks.values():
            s.close()
        # Let them expire within a second or two rather than a minute.
        for k, c in channels.items():
            try:
                controls[k].set_channel_lifetime(c.ssrc, 100)
            except Exception:  # noqa: BLE001
                pass
        meta["stopped_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        (out / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(f"recorded into {out}; now: {Path(__file__).name} analyze {out}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="verb", required=True)

    r = sub.add_parser("record", help="create both channels and record their IQ")
    r.add_argument("--status", required=True, help="radiod status address")
    r.add_argument("--ref-hz", type=int, default=45_375_000)
    r.add_argument("--test-hz", type=int, required=True)
    r.add_argument("--seconds", type=int, default=300)
    r.add_argument("--out", required=True, help="new directory for the recording")
    # The production T6 channel's settings ([timing.t6_pps]).
    r.add_argument("--sample-rate", type=int, default=96_000)
    r.add_argument("--half-width-hz", type=float, default=25_000)
    r.add_argument("--gain-db", type=float, default=0.0)
    r.add_argument("--blocktime-ms", type=float, default=20.0,
                   help="radiod's blocktime; the default unless radiod.conf sets one")

    a = sub.add_parser("analyze", help="fold both channels and difference the edges")
    a.add_argument("directory")
    a.add_argument("--fold", type=int, default=30, help="fold seconds (production: 30)")
    a.add_argument("--no-cfo", action="store_true",
                   help="skip the carrier-offset correction")
    a.add_argument("--json", action="store_true", help="print the report as JSON")

    args = ap.parse_args()
    if args.verb == "record":
        record(args)
        return 0
    rep = analyze(Path(args.directory), args.fold, not args.no_cfo)
    if args.json:
        print(json.dumps(rep, indent=2, default=float))
    else:
        print_report(rep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
