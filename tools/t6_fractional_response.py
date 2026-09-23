#!/usr/bin/env python3
"""Where does our estimator put an edge that falls between samples?

Scott Newell's fractional-delay plots (2026-09-22) show the classic result:
a delay of D samples has impulse response sinc(n-D), so an INTEGER delay lands
on the sinc's zeros and yields one tap, while a HALF-sample delay lands between
them and spreads as 1/pi(n-D).  Phil Karn: "the complex sinc is continuous but
you're sampling it at different points."

⛔ We do no such thing.  Nothing in hf-timestd applies a fractional delay by
phase ramp; the estimator never shifts the signal.  It folds K seconds, removes
the residual carrier by squaring, and fits the transition.  So the Nyquist-bin
asymmetry that makes a phase-ramp delay complex cannot arise here.

But the underlying question transfers, and it is the one worth asking of any
sub-sample estimator: DOES THE ANSWER DEPEND ON WHERE THE EDGE FALLS BETWEEN
SAMPLES?  A bias that varies with fractional position is a systematic, not
noise -- it would not show up as scatter, and averaging would not remove it.

So sweep the true edge across one sample interval and report estimate minus
truth.  Noise-free isolates the systematic; a C/N0 run shows what survives it.
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))

from test_bpsk_pps_calibrator_mf import _make_bpsk_signal  # noqa: E402
from hf_timestd.core.bpsk_edge_fine_stage import BpskEdgeFineStage  # noqa: E402

SR = 96_000
BATCH = 1920


def noise_std_for(cn0_db_hz: float, sr: int = SR) -> float:
    snr = 10 ** ((cn0_db_hz - 10 * math.log10(sr)) / 10.0)
    return 1.0 / math.sqrt(2.0 * snr)


def estimate(edge: float, *, duration_s: float, noise_std: float, seed: int):
    sig = _make_bpsk_signal(duration_s=duration_s, sample_rate=SR,
                            edge_offset_samples=edge,
                            noise_std=noise_std, seed=seed)
    stage = BpskEdgeFineStage(sample_rate=SR)
    last = None
    for i in range(0, len(sig), BATCH):
        est = stage.process_samples(sig[i:i + BATCH], i)
        if est is not None:
            last = est
    return last


def wrap(x: float, period: int = SR) -> float:
    return (x + period / 2) % period - period / 2


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', type=float, default=47916.0,
                    help='integer part of the true edge')
    ap.add_argument('--steps', type=int, default=11)
    ap.add_argument('--duration', type=float, default=35.0)
    ap.add_argument('--cn0', type=float, default=None,
                    help='dB-Hz; omit for noise-free')
    ap.add_argument('--seed', type=int, default=11)
    a = ap.parse_args()

    nstd = 0.0 if a.cn0 is None else noise_std_for(a.cn0)
    label = 'noise-free' if a.cn0 is None else f'C/N0 {a.cn0} dB-Hz'
    print(f"# fractional-position response, {label}, "
          f"{a.duration:.0f}s folds, seed {a.seed}")
    print(f"# {'frac':>6} {'true':>14} {'estimated':>14} "
          f"{'error_samples':>14} {'error_us':>10}")
    fracs, errs = [], []
    for k in range(a.steps):
        frac = k / (a.steps - 1) if a.steps > 1 else 0.0
        true = a.base + frac
        est = estimate(true, duration_s=a.duration, noise_std=nstd, seed=a.seed)
        if est is None:
            print(f"  {frac:6.3f} {true:14.4f} {'NO ESTIMATE':>14}")
            continue
        err = wrap(est.edge_offset_samples - true)
        fracs.append(frac); errs.append(err)
        print(f"  {frac:6.3f} {true:14.4f} {est.edge_offset_samples:14.4f} "
              f"{err:14.5f} {err / SR * 1e6:10.3f}")
    if errs:
        e = np.array(errs)
        print(f"#\n# error over the sweep: mean {e.mean():+.5f} samples "
              f"({e.mean()/SR*1e6:+.3f} us), "
              f"peak-to-peak {np.ptp(e):.5f} samples ({np.ptp(e)/SR*1e6:.3f} us)")
        print(f"# A flat line means the answer does not care where the edge "
              f"falls between samples.")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
