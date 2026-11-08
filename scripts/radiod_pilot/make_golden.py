#!/usr/bin/env python3
"""Golden vectors for the radiod pilot-detector C port.

  make_golden.py synth --out DIR
  make_golden.py file IQ.cf32 --rate 96000 --out DIR

Each vector is <name>.cf32 (raw little-endian complex64, interleaved
float32 I/Q) plus <name>.golden.jsonl, one line per FineEdgeEstimate from
BpskEdgeFineStage(fold_seconds=30, search_window_ms=6.0), fed in
1740-sample batches with RTP = file sample index and no coarse offset
(bootstrap, then tracking).  Synthetic sets add truth_edge_offset_samples.

Each row also carries costas_quiet: BpskPpsCalibratorMF (costas_loop_bw_hz
1.0, the recorder and config-template default) runs over the same batches,
and a fold is quiet only if costas_locked held after every batch that fed
it.  Its gate starts unlocked, so the first fold never reads quiet.
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
from hf_timestd.core.bpsk_pps_calibrator_mf import BpskPpsCalibratorMF  # noqa: E402
from tests.test_bpsk_edge_fine_stage import make_bpsk  # noqa: E402

SR = 96_000
SECONDS = 150
BATCH = 1740
FOLD_SECONDS = 30
COSTAS_LOOP_BW_HZ = 1.0
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
    # Carrier phase swings out 5 rad and back over 12 s inside the third
    # fold (63-75 s): phi(t) = 5 sin^2(pi (t - 63) / 12)
    "excursion": dict(edge=41234.0, kw={},
                      excursion=dict(t0_s=63, len_s=12, rad=5.0)),
}


def replay(iq, rate):
    """Fine-stage estimates, each paired with the Costas gate's verdict
    on the fold it came from."""
    stage = BpskEdgeFineStage(int(rate), fold_seconds=FOLD_SECONDS,
                              search_window_ms=6.0)
    gate = BpskPpsCalibratorMF(int(rate), costas_loop_bw_hz=COSTAS_LOOP_BW_HZ,
                               fold_seconds=0)
    block = FOLD_SECONDS * int(rate)
    quiet = {}  # fold index -> costas_locked after every batch that fed it
    out = []
    for i in range(0, len(iq), BATCH):
        batch = iq[i:i + BATCH]
        gate.process_samples(batch, i & 0xFFFFFFFF)
        for k in range(i // block, (i + len(batch) - 1) // block + 1):
            quiet[k] = quiet.get(k, True) and gate.costas_locked
        r = stage.process_samples(batch, i & 0xFFFFFFFF)
        if r is not None:
            fold = (i + len(batch)) // block - 1  # the fold this batch closed
            out.append((r, quiet[fold]))
    return out


def synth_iq(path, spec, chunk_s=10):
    """Write the synthetic capture in chunk_s-second pieces (bounded RAM).
    start_second keeps the polarity alternation continuous; each chunk
    draws its noise from a different seed."""
    with open(path, "wb") as fh:
        for k in range(SECONDS // chunk_s):
            kw = dict(spec["kw"], start_second=k * chunk_s, seed=1 + k)
            iq = make_bpsk(SR, chunk_s, spec["edge"], **kw)
            exc = spec.get("excursion")
            if exc:
                t = k * chunk_s + np.arange(len(iq)) / SR
                x = (t - exc["t0_s"]) / exc["len_s"]
                inside = (x >= 0) & (x < 1)
                phi = np.where(inside, exc["rad"] * np.sin(np.pi * x) ** 2, 0.0)
                iq = iq * np.exp(1j * phi)
            iq.astype("<c8").tofile(fh)


def write_set(out, name, rate, truth=None):
    iq = np.fromfile(out / f"{name}.cf32", dtype="<c8")
    with open(out / f"{name}.golden.jsonl", "w") as f:
        for e, quiet in replay(iq, rate):
            d = dataclasses.asdict(e)
            row = {k: d[k] for k in KEYS}
            row["costas_quiet"] = bool(quiet)
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
