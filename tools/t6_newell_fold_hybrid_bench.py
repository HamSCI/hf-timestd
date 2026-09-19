"""Would folding Newell's answers help?  Measured.

Reproduces the table in docs/design/T6_NEWELL_VS_FOLD.md 5b.

    .venv/bin/python tools/t6_newell_fold_hybrid_bench.py

⚡ The answer is no, and the "distinct positions per run" column says why: at
77 dB-Hz it reads 1.00.  Thirty detections, all the same integer.  A fold
reduces scatter by averaging VARIATION around a truth, and a clean per-sample
detector produces none — it rounds the same way every time because the edge
sits at the same sub-sample phase every time.  Its perfect repeatability is
exactly what forecloses improvement.

Writes nothing, touches no station.
"""
import importlib.util, math, sys
import numpy as np
HF='/home/mjh/hamsci/repos/hf-timestd'
sys.path.insert(0,HF+'/tests'); sys.path.insert(0,HF+'/src')
from test_bpsk_pps_calibrator_mf import _make_bpsk_signal
spec=importlib.util.spec_from_file_location("b",HF+"/tools/t6_fold_discriminant_bench.py")
B=importlib.util.module_from_spec(spec); spec.loader.exec_module(B)
SR=96_000; US=1e6/SR
def nf(c):
    s=10**((c-10*math.log10(SR))/10.0); return 1.0/math.sqrt(2.0*s)
def wrap(e): return (e+SR/2)%SR-SR/2

for cn0 in (77.0, 60.0):
    for K in (1, 30):
        single, avg, nuniq = [], [], []
        for i,frac in enumerate(np.linspace(0,0.999,12)):
            truth=47916+frac
            x=_make_bpsk_signal(duration_s=K+1.0, sample_rate=SR,
                                edge_offset_samples=truth,
                                noise_std=nf(cn0), seed=11+i)
            x=x[:(len(x)//SR)*SR]
            cand,_,_=B.newell(x,SR)
            mods=(cand%SR).astype(float)
            if len(mods)==0: continue
            nuniq.append(len(np.unique(mods)))
            single.append(wrap(mods[0]-truth))          # one detection
            avg.append(wrap(float(np.mean(mods))-truth)) # ALL of them, averaged
        def rms(e):
            e=np.array(e); return math.sqrt(float(np.mean(e**2)))
        print(f"C/N0 {cn0:>5.1f}  K={K:>3}  |  one detection RMS {rms(single)*US:>8.3f} us "
              f"|  averaged over the fold RMS {rms(avg)*US:>8.3f} us "
              f"|  distinct positions per run: {np.mean(nuniq):.2f}")
