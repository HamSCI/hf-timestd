"""Newell's per-sample detector against the fold, measured on KNOWN truth.

Reproduces the table in docs/design/T6_NEWELL_VS_FOLD.md.

    .venv/bin/python tools/t6_newell_vs_fold_bench.py

⚠ Synthetic by necessity, not convenience.  Accuracy needs truth, and a
capture has none — on recorded IQ you can measure how well estimates agree
and how tightly they repeat, which are both PRECISION.  Neither says whether
the answer sits where the edge actually was.

⚡ The sub-sample phase sweep carries the whole argument.  A quantised
estimator repeats perfectly at any ONE phase — which is what
T6_EDGE_METHODS_COMPARED.md 8b measured as 128/128 bit-identical — and is
wrong by a different amount at each.  Hold the phase fixed and Newell looks
flawless; sweep it and its irreducible 3.01 us appears.

Writes nothing, touches no station, ~4 minutes.
"""
import importlib.util, math, sys
import numpy as np
HF = '/home/mjh/hamsci/repos/hf-timestd'
sys.path.insert(0, HF + '/tests'); sys.path.insert(0, HF + '/src')
from test_bpsk_pps_calibrator_mf import _make_bpsk_signal
from hf_timestd.core.bpsk_edge_fine_stage import BpskEdgeFineStage
spec = importlib.util.spec_from_file_location("bench", HF + "/tools/t6_fold_discriminant_bench.py")
B = importlib.util.module_from_spec(spec); spec.loader.exec_module(B)

SR, BATCH = 96_000, 1920
def noise_for(c):
    snr = 10 ** ((c - 10*math.log10(SR)) / 10.0); return 1.0/math.sqrt(2.0*snr)
def wrap(e, sr=SR): return (e + sr/2) % sr - sr/2
US = 1e6/SR

def fine_stage_pos(x, K):
    st = BpskEdgeFineStage(sample_rate=SR, fold_seconds=K)
    last = None
    for i in range(0, len(x), BATCH):
        e = st.process_samples(x[i:i+BATCH], i)
        if e is not None: last = e
    return None if last is None else last.edge_offset_samples

print(f"{'C/N0':>6} {'K':>3} | {'NEWELL alone':^22} | {'magdiff+fold':^22} | {'FINE STAGE (ships)':^22}")
print(f"{'':>6} {'':>3} | {'sd':>10} {'RMS':>10} | {'sd':>10} {'RMS':>10} | {'sd':>10} {'RMS':>10}")
print("-"*90)
for cn0, K in ((77.0,30), (60.0,30), (48.4,30), (48.4,60)):
    ne, me, fe = [], [], []
    for i, frac in enumerate(np.linspace(0.0, 0.999, 12)):
        truth = 47916 + frac
        x = _make_bpsk_signal(duration_s=K+1.5, sample_rate=SR,
                              edge_offset_samples=truth,
                              noise_std=noise_for(cn0), seed=11+i)
        xt = x[:(len(x)//SR)*SR]
        _, pos, _ = B.newell(xt, SR)
        if pos is not None: ne.append(wrap(pos - truth))
        p, _ = B.subsample_peak(B.magdiff_fold(xt, SR)); me.append(wrap(p - truth))
        f = fine_stage_pos(x, K)
        if f is not None: fe.append(wrap(f - truth))
    def st(e):
        if not e: return (float('nan'),)*2
        b, s = float(np.mean(e)), float(np.std(e)); return s, math.sqrt(b*b+s*s)
    ns_, nr = st(ne); ms, mr = st(me); fs, fr = st(fe)
    print(f"{cn0:>6.1f} {K:>3} | {ns_*US:>9.3f}u {nr*US:>9.3f}u "
          f"| {ms*US:>9.3f}u {mr*US:>9.3f}u | {fs*US:>9.3f}u {fr*US:>9.3f}u  (n={len(fe)}/12)")
