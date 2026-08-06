#!/usr/bin/env python3
"""Recover the actual TS-1 polarity-flip waveform by coherent averaging.

Finds the per-second edges with the deployed-form MF, then averages the raw
IQ around them (phase-aligned to the pre-edge carrier). The width of the
resulting transition is what physically limits timing -- receiver bandwidth,
or the transmitter's own pulse shaping.
"""
import sys
import numpy as np

path, sr = sys.argv[1], 96000
i16 = np.fromfile(path, dtype=np.int16)
x = i16[0::2].astype(np.float64) + 1j * i16[1::2].astype(np.float64)
x -= x.mean()
sq = x[: sr * 8] ** 2; sq -= sq.mean()
fr = np.fft.fftshift(np.fft.fftfreq(len(sq), 1.0 / sr))
f0 = fr[int(np.argmax(np.fft.fftshift(np.abs(np.fft.fft(sq)))))] / 2.0
x *= np.exp(-2j * np.pi * f0 * np.arange(len(x)) / sr)
print(f"{len(x)/sr:.1f} s, residual carrier {f0:+.4f} Hz removed")

N = sr // 2
cs = np.concatenate(([0.0 + 0.0j], np.cumsum(x)))
idx = np.arange(N, len(x) - N)
y = np.abs((cs[idx + N + 1] - cs[idx + 1]) - (cs[idx] - cs[idx - N]))
edges = []
for k in range(len(y) // sr):
    w = y[k * sr:(k + 1) * sr]
    if len(w) == sr:
        edges.append(idx[k * sr + int(np.argmax(w))])
edges = np.array(edges)
edges = edges[(edges > sr // 4) & (edges < len(x) - sr // 4)]
print(f"{len(edges)} edges found\n")

W = int(0.020 * sr)                       # +-20 ms window
acc = np.zeros(2 * W + 1, dtype=np.complex128)
for e in edges:
    seg = x[e - W:e + W + 1].copy()
    pre = seg[: W // 2]
    ph = np.angle(pre.sum())              # align pre-edge carrier phase to 0
    acc += seg * np.exp(-1j * ph)
acc /= len(edges)
re = acc.real
ms = (np.arange(len(acc)) - W) / sr * 1000.0

lo, hi = re[: W // 2].mean(), re[-W // 2:].mean()
print(f"pre-edge level {lo:+.1f}, post-edge level {hi:+.1f}  (coherent avg of {len(edges)} edges)")
span = hi - lo
if span != 0:
    f10 = lo + 0.10 * span; f90 = lo + 0.90 * span
    cross = lambda t: ms[int(np.argmin(np.abs(re - t)))]
    print(f"10-90% transition: {cross(f10):+.3f} ms -> {cross(f90):+.3f} ms "
          f"= {abs(cross(f90)-cross(f10)):.3f} ms rise")
print("\naveraged transition, Re{s} every 0.5 ms:")
step = int(0.0005 * sr)
for i in range(0, len(acc), step):
    b = int((re[i] - min(re)) / (max(re) - min(re) + 1e-12) * 44)
    print(f"  {ms[i]:+7.2f} ms |{'#'*b:<44}| {re[i]:+9.1f}")
