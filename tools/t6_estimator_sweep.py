#!/usr/bin/env python3
"""T6 offline estimator harness — integration-length sweep.

Reads a raw int16 I/Q capture of the T6 BPSK-PPS channel (as written by
`pcmrecord -r`) and measures, for a range of antisymmetric-MF half-lengths N,
the sharpness of the correlation feature (%% of peak per ms) and the
per-second scatter of the resulting edge estimate.

Feature sharpness improves as 1/N while SNR degrades as 1/sqrt(N); the sweep
locates where timing precision actually optimises. The deployed calibrator
uses N = sample_rate//2 (the first row).

Usage: t6_sweep.py <capture.raw> [sample_rate]
"""
import sys
import numpy as np

path = sys.argv[1]
sr = int(sys.argv[2]) if len(sys.argv) > 2 else 96000

i16 = np.fromfile(path, dtype=np.int16)
x = i16[0::2].astype(np.float64) + 1j * i16[1::2].astype(np.float64)
x -= x.mean()
print(f"loaded {len(x)} samples = {len(x)/sr:.2f} s @ {sr} Hz")

# BPSK carrier recovery: square strips the +-1 modulation, leaving 2f
seg = x[: min(len(x), sr * 8)]
sq = seg ** 2
sq -= sq.mean()
fr = np.fft.fftshift(np.fft.fftfreq(len(sq), 1.0 / sr))
f0 = fr[int(np.argmax(np.fft.fftshift(np.abs(np.fft.fft(sq)))))] / 2.0
print(f"residual carrier: {f0:+.4f} Hz -> derotating")
x *= np.exp(-2j * np.pi * f0 * np.arange(len(x)) / sr)

csum = np.concatenate(([0.0 + 0.0j], np.cumsum(x)))

def mf(N):
    """Antisymmetric MF, same form as the deployed calibrator:
       y[i] = sum(x[i+1:i+N+1]) - sum(x[i-N:i])  -> magnitude"""
    idx = np.arange(N, len(x) - N)
    yc = (csum[idx + N + 1] - csum[idx + 1]) - (csum[idx] - csum[idx - N])
    return idx, np.abs(yc)

# Coarse anchors from the deployed configuration (N = sr/2), one per second
Nc = sr // 2
idc, yc = mf(Nc)
anchors = []
for k in range(len(yc) // sr):
    w = yc[k * sr:(k + 1) * sr]
    if len(w) == sr:
        anchors.append(idc[k * sr + int(np.argmax(w))])
anchors = np.array(anchors)
# keep only anchors that sit ~1 s apart (drop the aliased/edge ones)
if len(anchors) > 2:
    dt = np.diff(anchors)
    good = np.concatenate(([True], np.abs(dt - sr) < 0.2 * sr))
    anchors = anchors[good]
print(f"coarse anchors: {len(anchors)}\n")

print(f"{'N':>8} {'span':>10} {'slope %/ms':>12} {'scatter us':>12} {'jitter us':>11}")
print("-" * 58)
GUARD = int(0.005 * sr)
for N in (sr // 2, sr // 10, sr // 100, sr // 500, sr // 1000, sr // 5000, sr // 20000):
    if N < 4:
        continue
    idx, y = mf(N)
    base = idx[0]
    pos = []
    for a in anchors:
        lo, hi = a - GUARD - base, a + GUARD - base
        if lo < 0 or hi >= len(y):
            continue
        w_ = y[lo:hi]
        j = int(np.argmax(w_))
        # quadratic (3-point) interpolation for the sub-sample peak position
        frac = 0.0
        if 0 < j < len(w_) - 1:
            y0, y1, y2 = w_[j-1], w_[j], w_[j+1]
            den = y0 - 2*y1 + y2
            if den != 0:
                frac = 0.5 * (y0 - y2) / den
        pos.append(lo + j + frac + base - a)
    if len(pos) < 3:
        continue
    pos = np.array(pos, dtype=np.float64)
    a = anchors[len(anchors) // 2] - base
    w = int(0.002 * sr)
    g = float("nan")
    if a - w > 0 and a + w < len(y):
        s = y[a - w:a + w]
        xs = (np.arange(len(s)) - w) / sr * 1000.0
        h = len(s) // 2
        g = abs(np.polyfit(xs[:h], s[:h], 1)[0]) / s.max() * 100
    print(f"{N:>8} {2*N/sr*1000:>8.2f}ms {g:>12.3f} "
          f"{np.std(pos)/sr*1e6:>12.1f} {np.std(np.diff(pos))/sr*1e6:>11.1f}")
