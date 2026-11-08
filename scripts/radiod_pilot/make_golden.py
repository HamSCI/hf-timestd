#!/usr/bin/env python3
"""Golden vectors for the radiod pilot-detector C port.

  make_golden.py synth --out DIR
  make_golden.py file IQ.cf32 --rate 96000 --out DIR

Each vector is <name>.cf32 (raw little-endian complex64, interleaved
float32 I/Q) plus <name>.golden.jsonl, one line per FineEdgeEstimate from
BpskEdgeFineStage(fold_seconds=30, search_window_ms=6.0), fed in
1740-sample batches with RTP = file sample index and no coarse offset
(bootstrap, then tracking).  Synthetic sets add truth_edge_offset_samples.
"""
import argparse
import dataclasses
import json
import math
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from hf_timestd.core.bpsk_edge_fine_stage import BpskEdgeFineStage  # noqa: E402
from tests.test_bpsk_edge_fine_stage import make_bpsk  # noqa: E402

SR = 96_000
SECONDS = 150
BATCH = 1740
KEYS = ["edge_rtp", "edge_subsample", "edge_offset_samples",
        "n_seconds_folded", "plateau_amplitude", "fit_rms", "fold_retention",
        "split_half_delta_samples", "peak_prominence",
        "transition_width_samples", "apex_distance_samples"]

# make_bpsk adds noise_rms * (N(0,1) + jN(0,1)) / sqrt(2): noise_rms is the
# COMPLEX rms (total power noise_rms**2).  C/N0 = A^2 / (noise_power / sr),
# so noise_rms = sqrt(sr / 10**(cn0/10)) at amp 1.0.
CN0_48_NOISE_RMS = math.sqrt(SR / 10 ** (48 / 10))

SYNTH = {
    "clean": dict(edge=41234.0, kw={}),
    "cn0_48": dict(edge=41234.0, kw=dict(noise_rms=CN0_48_NOISE_RMS)),
    "frac_0p37": dict(edge=41234.37, kw=dict(bandlimit_hz=25000)),
    "tilt": dict(edge=41234.0, kw=dict(tilt_per_s=0.02, phase=1.1)),
}


def replay(iq, rate):
    stage = BpskEdgeFineStage(int(rate), fold_seconds=30, search_window_ms=6.0)
    out = []
    for i in range(0, len(iq), BATCH):
        r = stage.process_samples(iq[i:i + BATCH], i & 0xFFFFFFFF)
        if r is not None:
            out.append(r)
    return out


def synth_iq(path, spec, chunk_s=10):
    """Write the synthetic capture in chunk_s-second pieces (bounded RAM).
    start_second keeps the polarity alternation continuous; each chunk
    draws its noise from a different seed."""
    with open(path, "wb") as fh:
        for k in range(SECONDS // chunk_s):
            kw = dict(spec["kw"], start_second=k * chunk_s, seed=1 + k)
            make_bpsk(SR, chunk_s, spec["edge"], **kw).astype("<c8").tofile(fh)


def write_set(out, name, rate, truth=None):
    iq = np.fromfile(out / f"{name}.cf32", dtype="<c8")
    with open(out / f"{name}.golden.jsonl", "w") as f:
        for e in replay(iq, rate):
            d = dataclasses.asdict(e)
            row = {k: d[k] for k in KEYS}
            if truth is not None:
                row["truth_edge_offset_samples"] = truth
            f.write(json.dumps(row) + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("synth")
    s.add_argument("--out", required=True)
    f = sub.add_parser("file")
    f.add_argument("iq")
    f.add_argument("--rate", type=int, default=SR)
    f.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    if a.cmd == "synth":
        for name, spec in SYNTH.items():
            synth_iq(out / f"{name}.cf32", spec)
            write_set(out, name, SR, truth=spec["edge"])
    else:
        name = Path(a.iq).stem
        if Path(a.iq).resolve() != (out / f"{name}.cf32").resolve():
            import shutil
            shutil.copyfile(a.iq, out / f"{name}.cf32")
        write_set(out, name, a.rate)


if __name__ == "__main__":
    main()
