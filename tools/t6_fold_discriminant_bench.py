#!/usr/bin/env python3
"""Bench three T6 edge discriminants against captured IQ.

Capture on a station (writes ~46 MB/min to tmpfs, delete afterwards):

    pcmrecord --ssrc <T6_SSRC> --catmode --raw <MCAST> > /tmp/t6.iq

Then run this against the file. It reports, for each discriminant, where the
edge lands modulo the sample rate and how stable that answer is.

Written 2026-09-18 from DASI-009.AI6VN, 89 s of the 45.375 MHz TS-1 channel.
Result on that capture:

    Newell (per-sample phase jump)   89/89 candidates, ONE position, 18845
    complex fold (our current path)  CANCELS — 0.6-1.8% of signal amplitude
    magnitude-difference + fold      18844.7051, sd 95 ns/s, 105x peak/median

⚠ The complex fold cancelling is the finding that matters. Coherent folding
across seconds needs the residual carrier removed first, which is why the
present chain needs Costas at all. The magnitude difference is carrier-free,
so it folds without that dependency — and it landed on Newell's sample
exactly, with no interpolation needed to agree.
"""
from __future__ import annotations
import sys
import numpy as np

SR = 96_000


def load(path, sr=SR):
    x = np.fromfile(path, dtype=np.complex64)
    return x[:(len(x) // sr) * sr]


def newell(x, sr=SR):
    """Per-sample phase jump, accept 90-270 deg. No processing gain."""
    d = np.abs(np.diff(np.angle(x)))
    d = np.minimum(d, 2 * np.pi - d)
    cand = np.flatnonzero(d > np.pi / 2) + 1
    mod = cand % sr
    vals, counts = np.unique(mod, return_counts=True)
    return cand, (vals[counts.argmax()] if len(vals) else None), len(cand)


def complex_fold(x, sr=SR, alternate=True, first=+1):
    """Our current path: sign-alternated coherent complex average."""
    sec = x.reshape(-1, sr)
    n = sec.shape[0]
    s = (np.array([first * (-1) ** k for k in range(n)], dtype=float)
         if alternate else np.ones(n))
    return (sec * s[:, None]).mean(axis=0)


def magdiff_fold(x, sr=SR, seconds=None):
    """Method 5's discriminant, folded. Carrier-free."""
    d = np.abs(np.diff(x, prepend=x[0]))
    rows = d.reshape(-1, sr)
    if seconds:
        rows = rows[:seconds]
    return rows.mean(axis=0)


def subsample_peak(f):
    """Parabolic interpolation on the three samples about the max."""
    i = int(np.argmax(f))
    a, b, c = f[(i - 1) % len(f)], f[i], f[(i + 1) % len(f)]
    den = a - 2 * b + c
    frac = 0.0 if den == 0 else 0.5 * (a - c) / den
    return i + frac, f[i] / np.median(f)


def main(path):
    x = load(path)
    n = len(x) // SR
    print(f"{n} s of complex64 at {SR} Hz\n")

    _, pos, count = newell(x)
    print(f"  newell            {count} candidates ({count/n:.2f}/s), "
          f"modal position {pos}")

    for lbl, kw in (("no alternation", dict(alternate=False)),
                    ("alternate +", dict(alternate=True, first=+1)),
                    ("alternate -", dict(alternate=True, first=-1))):
        f = complex_fold(x, **kw)
        print(f"  complex fold ({lbl:14s}) amplitude "
              f"{np.abs(f).mean()/np.abs(x).mean():.4f} of signal")

    p, snr = subsample_peak(magdiff_fold(x))
    print(f"\n  magdiff+fold      {p:.4f} samples  ({p/SR*1e3:.6f} ms), "
          f"peak/median {snr:.0f}x")
    if pos is not None:
        print(f"                    offset from newell: {p - pos:+.4f} samples")

    rows = np.abs(np.diff(x, prepend=x[0])).reshape(-1, SR)
    per = np.array([subsample_peak(r)[0] for r in rows])
    print(f"  per-second sd     {per.std():.4f} samples = "
          f"{per.std()/SR*1e9:.0f} ns  (n={len(per)})")
    print(f"  predicted /30 s   {per.std()/np.sqrt(30)/SR*1e9:.0f} ns")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/t6.iq")
