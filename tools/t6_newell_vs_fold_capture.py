#!/usr/bin/env python3
"""Newell's per-sample detector vs the production fold, on identical samples.

⚠ A capture carries no truth (T6_NEWELL_VS_FOLD.md §3), so everything here
measures PRECISION -- how tightly each method repeats -- never accuracy.
Both methods see the same bytes, so the comparison between them is fair even
though neither is anchored.
"""
from __future__ import annotations
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from hf_timestd.core.bpsk_edge_fine_stage import BpskEdgeFineStage  # noqa: E402

SR = 96_000
BATCH = 1800          # what the recorder actually delivers
K = 30                # fold_seconds, the shipped default


def newell_candidates(x):
    """Per-sample phase jump, accept 90-270 deg. Verbatim from
    tools/t6_fold_discriminant_bench.py -- no processing gain."""
    d = np.abs(np.diff(np.angle(x)))
    d = np.minimum(d, 2 * np.pi - d)
    return np.flatnonzero(d > np.pi / 2) + 1


def newell_per_second(x, sr=SR, window=None):
    """One answer per second, the way a tracking implementation would.

    After acquisition any real detector restricts its search: it knows
    roughly where the edge is and rejects jumps far from it. `window` is
    the half-width in samples. Without it, the strongest jump in the
    second is taken, which is what an unaided detector must do.
    """
    cand = newell_candidates(x)
    mod = cand % sr
    vals, counts = np.unique(mod, return_counts=True)
    modal = int(vals[counts.argmax()])

    d = np.abs(np.diff(np.angle(x)))
    d = np.minimum(d, 2 * np.pi - d)

    n_sec = len(x) // sr
    out, missing = [], 0
    for s in range(n_sec):
        lo, hi = s * sr, (s + 1) * sr
        c = cand[(cand >= lo) & (cand < hi)]
        if window is not None:
            off = (c - lo)
            keep = np.abs(((off - modal + sr // 2) % sr) - sr // 2) <= window
            c = c[keep]
        if len(c) == 0:
            out.append(np.nan)
            missing += 1
            continue
        best = c[np.argmax(d[c - 1])]        # strongest jump in the window
        out.append(float((best - lo)))
    return np.array(out), modal, missing


def fold_estimates(x, sr=SR, k=K):
    """The shipped stage, bootstrap mode, fed as the recorder feeds it."""
    stage = BpskEdgeFineStage(sample_rate=sr, fold_seconds=k)
    ests = []
    for i in range(0, len(x) - BATCH + 1, BATCH):
        e = stage.process_samples(x[i:i + BATCH], rtp_timestamp=i)
        if e is not None:
            ests.append(e)
    return ests


def wrapped_spread(v, sr=SR):
    """sd about the circular mean, in samples (the edge lives mod 1 s)."""
    v = v[np.isfinite(v)]
    if len(v) < 2:
        return np.nan, np.nan, 0
    ang = 2 * np.pi * v / sr
    m = np.angle(np.mean(np.exp(1j * ang))) % (2 * np.pi) * sr / (2 * np.pi)
    dev = ((v - m + sr / 2) % sr) - sr / 2
    return float(np.std(dev)), float(m), len(v)


def report(path):
    x = np.fromfile(path, dtype=np.complex64)
    x = x[:(len(x) // SR) * SR]
    n_sec = len(x) // SR
    print(f"\n{'=' * 72}\n{Path(path).name}   {n_sec} s @ {SR} Hz")

    cand = newell_candidates(x)
    print(f"\n  Newell raw candidates      {len(cand)}  "
          f"({len(cand) / n_sec:.1f}/s -- one real edge per second exists)")

    for label, win in (("unaided (whole second)", None),
                       ("tracking (+-30 samp)", 30)):
        pos, modal, miss = newell_per_second(x, window=win)
        sd, mean, n = wrapped_spread(pos)
        print(f"\n  Newell, {label}")
        print(f"    seconds with an edge     {n}/{n_sec}   (missing {miss})")
        print(f"    modal position           {modal} samples")
        if n >= 2:
            print(f"    per-pulse sd             {sd:.3f} samples "
                  f"= {sd / SR * 1e6:.2f} us")
            print(f"    sd of the mean of {n:<3d}    "
                  f"{sd / np.sqrt(n) / SR * 1e6:.3f} us")

    ests = fold_estimates(x)
    print(f"\n  Production fold (K={K}, bootstrap)")
    print(f"    blocks completed         {len(ests)}")
    if ests:
        ph = np.array([e.edge_rtp % SR + e.edge_subsample for e in ests])
        sd, mean, n = wrapped_spread(ph)
        for i, e in enumerate(ests):
            print(f"    block {i}: pos={ph[i]:12.4f}  retention={e.fold_retention:.4f}"
                  f"  tri_resid={e.peak_prominence:.6f}"
                  f"  split_half={e.split_half_delta_samples:+.4f}"
                  f"  width={e.transition_width_samples:.2f}"
                  f"  apex_d={e.apex_distance_samples:+.3f}"
                  f"  fit_rms={e.fit_rms:.4g}")
        if n >= 2:
            print(f"    block-to-block sd        {sd:.4f} samples "
                  f"= {sd / SR * 1e6:.3f} us")

        pos30, _, _ = newell_per_second(x, window=30)
        sdN, meanN, nN = wrapped_spread(pos30)
        if nN >= 2 and n >= 1:
            delta = ((mean - meanN + SR / 2) % SR) - SR / 2
            print(f"\n  Agreement  fold - Newell(tracking mean) = "
                  f"{delta:+.4f} samples = {delta / SR * 1e6:+.3f} us")
            if n >= 2:
                print(f"  Precision ratio  Newell per-pulse sd / fold block sd "
                      f"= {sdN / sd:.1f}x")


if __name__ == "__main__":
    for p in sys.argv[1:]:
        report(p)
