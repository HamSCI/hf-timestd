#!/usr/bin/env python3
"""Golden vectors for the radiod tick detector (ka9q-radio src/tick.c).

  make_golden.py synth --out DIR [--sets wwv_only,both,...]

Each set is <name>.cf32 (24 kHz, 120 s, raw little-endian complex64,
interleaved float32 I/Q), <name>.meta.json (rate, template mask,
second0_index) and <name>.golden.jsonl: one line per nominal second, the
same keys tick-replay prints, computed by a plain numpy reference of the C
arithmetic.  This reference exists to prove the C equal to it; it is not
hf-timestd's tick detector, and judges nothing.

The math, per nominal second S (the C does exactly this):

* profile_<t>[i] = sum_k s[S + win_off + i*step + k] * cos(2 pi f k / fs),
  k < L; f, L from the template (WWV 1000 Hz 5 cycles, WWVH 1200 Hz 6
  cycles, BPM 1000 Hz 10 cycles); win_off = -10 ms, 70 ms of lags at the
  step nearest 0.1 ms.  The template is rounded to float32, as the C holds it;
* peak_index_<t>: argmax |profile|, a parabola through it and its two
  neighbours (none at the window's ends), on the channel counter;
* peak_snr_db_<t>: peak power over the median power of lags more than 3 ms
  from the peak;
* marker_energy[m] = (|X(+f)|^2 + |X(-f)|^2) / N^2 over [S, S + 0.8 s),
  f = 1000, 1200, 1500 Hz;
* envelope[i] = sum_k h[k] |s[S + i*D + k - M]|, h a Hamming-windowed sinc
  (cutoff 250 Hz, M = 8 D, unity DC gain), D = fs / 1000; quantised to int16
  with scale float32(max|env| / 32767).

Reports exist for every S = second0_index + k*fs whose span ends inside the
file, as tick-replay emits them.  The synthetic signal: one carrier
(amplitude 1) AM-modulated by each station's ticks (a burst of its tone,
starting at the station's arrival after each second; none in seconds 29 and
59), its 100 Hz BCD subcarrier (200/500/800 ms pulses from +30 ms, a low level
otherwise, silent from -10 to +30 ms), an 800 ms marker at second 0 (1500 Hz in
minute 0, the station's tick tone in minute 1), plus complex Gaussian noise.
Rows carry truth_<t>_index (the tick's start, null when the second has none).
"""
import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

SR = 24_000
SECONDS = 120
SECOND0 = SR // 2          # first whole second, half a second into the file
MASK = 7                   # wwv | wwvh | bpm

TEMPLATES = [("wwv", 1000.0, 5), ("wwvh", 1200.0, 6), ("bpm", 1000.0, 10)]
MARKER_HZ = [1000.0, 1200.0, 1500.0]


def lround(x):
    """C lround for the positive values used here (half away from zero)."""
    return int(math.floor(x + 0.5)) if x >= 0 else -int(math.floor(-x + 0.5))


class Geometry:
    """The C detector's constants for one sample rate (tick_create)."""

    def __init__(self, fs):
        self.fs = fs
        self.step = max(1, lround(1e-4 * fs))
        self.win_off = -lround(0.010 * fs)
        self.nprofile = lround(0.070 * fs) // self.step
        self.excl = lround(0.003 * fs)
        self.decim = fs // 1000
        self.env_half = 8 * self.decim
        self.ntaps = 2 * self.env_half + 1
        self.marker_n = lround(0.8 * fs)
        self.tlen = [lround(cyc * fs / f) for _, f, cyc in TEMPLATES]
        self.tmpl = [np.cos(2 * np.pi * f * np.arange(n) / fs).astype(np.float32).astype(np.float64)
                     for (_, f, _), n in zip(TEMPLATES, self.tlen)]
        k = np.arange(self.ntaps)
        x = 2 * 250.0 / fs * (k - self.env_half)
        h = np.sinc(x) * (0.54 - 0.46 * np.cos(2 * np.pi * k / (self.ntaps - 1)))
        self.taps = h / h.sum()
        self.span_begin = min(self.win_off, -self.env_half)
        end = self.win_off + (self.nprofile - 1) * self.step + max(self.tlen)
        end = max(end, self.marker_n, 999 * self.decim + self.env_half + 1)
        self.span_end = end
        self.span_len = end - self.span_begin


def reference_report(g, x, second, mask):
    """One second's report from x = the span [second + span_begin, second + span_end)."""
    row = {"window_start_index": second + g.win_off, "step": g.step, "nprofile": g.nprofile}
    o = g.win_off - g.span_begin
    for t, (name, _, _) in enumerate(TEMPLATES):
        if not mask & (1 << t):
            continue
        L = g.tlen[t]
        seg = x[o:o + (g.nprofile - 1) * g.step + L]
        prof = sliding_window_view(seg, L)[::g.step][:g.nprofile] @ g.tmpl[t]
        power = prof.real ** 2 + prof.imag ** 2
        k = int(np.argmax(power))
        delta = 0.0
        if 0 < k < g.nprofile - 1:
            a, b, d = np.sqrt(power[k - 1:k + 2])
            den = a - 2 * b + d
            if den < 0:
                delta = 0.5 * (a - d) / den
        idx = np.arange(g.nprofile)
        noise = np.median(power[np.abs(idx - k) * g.step > g.excl])
        snr = 10 * math.log10(power[k] / noise) if noise > 0 else (300.0 if power[k] > 0 else 0.0)
        row[f"profile_{name}"] = [[float(v.real), float(v.imag)] for v in prof]
        row[f"peak_index_{name}"] = float(second + g.win_off + (k + delta) * g.step)
        row[f"peak_snr_db_{name}"] = float(snr)
    s0 = -g.span_begin
    seg = x[s0:s0 + g.marker_n]
    n = np.arange(g.marker_n)
    energy = []
    for f in MARKER_HZ:
        w = 2 * np.pi * f / g.fs
        plus = seg @ np.exp(-1j * w * n)
        minus = seg @ np.exp(1j * w * n)
        energy.append(float((abs(plus) ** 2 + abs(minus) ** 2) / g.marker_n ** 2))
    row["marker_energy"] = energy
    mag = np.abs(x.astype(np.complex64)).astype(np.float64)
    first = s0 - g.env_half
    env = sliding_window_view(mag[first:first + 999 * g.decim + g.ntaps], g.ntaps)[::g.decim][:1000] @ g.taps
    big = float(np.max(np.abs(env)))
    if big == 0:
        row["envelope"] = [0] * 1000
        row["envelope_scale"] = 0.0
    else:
        scale = np.float32(big / 32767)
        q = np.clip(np.rint(env / float(scale)), -32767, 32767).astype(int)
        row["envelope"] = [int(v) for v in q]
        row["envelope_scale"] = float(scale)
    row["complete"] = True
    return row


# ---- synthesis -------------------------------------------------------------

def bcd_width_s(k):
    """A plausible WWV-style pulse width for whole second k: 800 ms markers
    at seconds 9, 19, ... and 0, else 500 or 200 ms from a fixed pattern."""
    som = k % 60
    if som % 10 == 9 or som == 0:
        return 0.8
    return 0.5 if (k * 2654435761 >> 7) & 1 else 0.2


def station_mod(n, st):
    """AM modulation of one station at absolute sample indices n."""
    fs = SR
    rel = n - SECOND0
    k = np.floor_divide(rel, fs)               # whole second (-1 before SECOND0)
    pos = (rel - k * fs) / fs                  # seconds into it
    som = np.mod(k, 60)
    minute = np.floor_divide(k, 60)
    mod = np.zeros(len(n))
    for delay, amp in st["paths"]:
        t = pos - delay
        tick = (som != 29) & (som != 59) & (som != 0) & (t >= 0) & (t < st["cycles"] / st["hz"])
        mod += np.where(tick, amp * np.cos(2 * np.pi * st["hz"] * t), 0.0)
    delay = st["paths"][0][0]
    t = pos - delay
    marker_hz = np.where(minute == 0, 1500.0, st["hz"])
    marker = (som == 0) & (t >= 0) & (t < 0.8)
    mod += np.where(marker, st["marker_amp"] * np.cos(2 * np.pi * marker_hz * t), 0.0)
    if st.get("bcd"):
        hi, lo = st["bcd"]
        uk, inv = np.unique(k, return_inverse=True)
        width = np.array([bcd_width_s(int(v)) for v in uk])[inv]
        tb = pos - delay
        level = np.where((tb >= 0.030) & (tb < 0.030 + width), hi,
                         np.where((tb >= 0.030 + width) & (tb < 0.990), lo, 0.0))
        mod += level * np.sin(2 * np.pi * 100.0 * tb)
    return mod


WWV = dict(hz=1000.0, cycles=5, marker_amp=0.5, bcd=(0.15, 0.03))
WWVH = dict(hz=1200.0, cycles=6, marker_amp=0.5, bcd=(0.15, 0.03))

SYNTH = {
    "wwv_only": dict(stations=[dict(WWV, paths=[(4.2e-3, 0.8)])], noise=0.02, phase=0.4,
                     truth={"wwv": 4.2e-3}),
    "both": dict(stations=[dict(WWV, paths=[(4.2e-3, 0.8)]),
                           dict(WWVH, paths=[(29.0e-3, 0.5)])], noise=0.02, phase=-0.9,
                 truth={"wwv": 4.2e-3, "wwvh": 29.0e-3}),
    "faded": dict(stations=[dict(WWVH, paths=[(29.0e-3, 0.3)])], noise=0.1, phase=2.2,
                  truth={"wwvh": 29.0e-3}),
    "multipath": dict(stations=[dict(WWV, paths=[(4.2e-3, 1.0 * 0.8), (5.1e-3, 0.6 * 0.8)])],
                      noise=0.03, phase=1.4, truth={}),
    "noise": dict(stations=[], noise=0.1, phase=0.0, truth={}),
}


def synth_iq(path, spec, seed, chunk=SR):
    """Write the capture a second at a time (bounded RAM); each chunk draws
    its noise from its own seed."""
    total = SECONDS * SR
    with open(path, "wb") as fh:
        for c, start in enumerate(range(0, total, chunk)):
            n = np.arange(start, min(start + chunk, total))
            mod = np.zeros(len(n))
            for st in spec["stations"]:
                mod += station_mod(n, st)
            iq = np.exp(1j * spec["phase"]) * (1 + mod)
            rng = np.random.default_rng(seed * 100_000 + c)
            iq = iq + spec["noise"] * (rng.standard_normal(len(n)) + 1j * rng.standard_normal(len(n))) / np.sqrt(2)
            iq.astype("<c8").tofile(fh)


def truth_index(spec, second):
    """Tick start per template for this second, None when it carries none."""
    som = ((second - SECOND0) // SR) % 60
    out = {}
    for name, _, _ in TEMPLATES:
        d = spec["truth"].get(name)
        out[f"truth_{name}_index"] = (None if d is None or som in (0, 29, 59)
                                      else second + d * SR)
    return out


def write_golden(out, name, rate, mask, second0, spec=None):
    g = Geometry(rate)
    cf32 = out / f"{name}.cf32"
    nsamp = cf32.stat().st_size // 8
    (out / f"{name}.meta.json").write_text(json.dumps(
        {"rate": rate, "mask": mask, "second0_index": second0}) + "\n")
    with open(cf32, "rb") as fh, open(out / f"{name}.golden.jsonl", "w") as f:
        second = second0
        while second + g.span_end <= nsamp:
            begin = second + g.span_begin
            fh.seek(begin * 8)
            x = np.fromfile(fh, dtype="<c8", count=g.span_len).astype(np.complex128)
            row = reference_report(g, x, second, mask)
            row["second_index"] = second
            if spec is not None:
                row.update(truth_index(spec, second))
            f.write(json.dumps(row) + "\n")
            second += rate


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("synth")
    s.add_argument("--out", required=True)
    s.add_argument("--sets", default=",".join(SYNTH), help="comma-separated subset of " + ",".join(SYNTH))
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for i, name in enumerate(SYNTH):
        if name not in a.sets.split(","):
            continue
        synth_iq(out / f"{name}.cf32", SYNTH[name], seed=i + 1)
        write_golden(out, name, SR, MASK, SECOND0, SYNTH[name])
        print(f"wrote {name}", file=sys.stderr)


if __name__ == "__main__":
    main()
