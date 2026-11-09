#!/usr/bin/env python3
"""Prove the radiod C tick detector equal to the numpy reference.

  compare_golden.py DIR --replay PATH/tick-replay

For every <name>.golden.jsonl in DIR (written by make_golden.py), runs
``tick-replay DIR/<name>.cf32 RATE MASK SECOND0_INDEX`` (from
<name>.meta.json), pairs the reports by order, streaming both sides, and
checks per report:

* second_index, window_start_index, step, nprofile and complete exactly;
* each profile value within 1e-4 of the reference profile's largest
  magnitude (relative);
* peak_index within 0.01 sample;
* marker_energy within 1e-4 of the report's largest marker energy (relative);
* envelope within 1 int16 LSB, envelope_scale within 1e-4 relative.

* peak_snr_db within 0.001 dB.

Prints the per-set worst
values and exits 1 on any failure.
"""
import argparse
import itertools
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

PROFILE_REL = 1e-4
PEAK_TOL = 0.01
MARKER_REL = 1e-4
ENV_LSB = 1
SCALE_REL = 1e-4
SNR_DB_TOL = 1e-3
NAMES = ["wwv", "wwvh", "bpm"]


def compare_set(golden_path, replay):
    name = golden_path.name[: -len(".golden.jsonl")]
    meta = json.loads(golden_path.with_name(f"{name}.meta.json").read_text())
    cf32 = golden_path.with_name(f"{name}.cf32")
    names = [n for i, n in enumerate(NAMES) if meta["mask"] & (1 << i)]
    proc = subprocess.Popen([str(replay), str(cf32), str(meta["rate"]), str(meta["mask"]),
                             str(meta["second0_index"])], stdout=subprocess.PIPE, text=True)
    worst = dict(profile=0.0, peak=0.0, marker=0.0, envelope=0, scale=0.0, snr_db=0.0)
    failures = []
    n_gold = n_c = 0
    with open(golden_path) as gf:
        for i, (gl, cl) in enumerate(itertools.zip_longest(gf, proc.stdout)):
            if gl is None or cl is None:
                n_gold += gl is not None
                n_c += cl is not None
                continue
            n_gold += 1
            n_c += 1
            g, c = json.loads(gl), json.loads(cl)
            for k in ["second_index", "window_start_index", "step", "nprofile", "complete"]:
                if g[k] != c[k]:
                    failures.append(f"report {i}: {k} reference {g[k]!r} C {c[k]!r}")
            for t in names:
                gp = np.asarray(g[f"profile_{t}"], dtype=float)
                cp = np.asarray(c[f"profile_{t}"], dtype=float)
                if gp.shape != cp.shape:
                    failures.append(f"report {i}: profile_{t} shape {gp.shape} vs {cp.shape}")
                    continue
                big = float(np.max(np.hypot(gp[:, 0], gp[:, 1])))
                d = float(np.max(np.hypot(*(gp - cp).T))) / big if big > 0 else 0.0
                worst["profile"] = max(worst["profile"], d)
                if not d <= PROFILE_REL:
                    failures.append(f"report {i}: profile_{t} rel err {d:.2e}")
                dp = abs(g[f"peak_index_{t}"] - c[f"peak_index_{t}"])
                worst["peak"] = max(worst["peak"], dp)
                if not dp <= PEAK_TOL:
                    failures.append(f"report {i}: peak_index_{t} reference {g[f'peak_index_{t}']} "
                                    f"C {c[f'peak_index_{t}']}")
                dsnr = abs(g[f"peak_snr_db_{t}"] - c[f"peak_snr_db_{t}"])
                worst["snr_db"] = max(worst["snr_db"], dsnr)
                if not dsnr <= SNR_DB_TOL:
                    failures.append(f"report {i}: peak_snr_db_{t} reference {g[f'peak_snr_db_{t}']} "
                                    f"C {c[f'peak_snr_db_{t}']}")
            gm, cm = np.asarray(g["marker_energy"]), np.asarray(c["marker_energy"])
            big = float(np.max(np.abs(gm)))
            d = float(np.max(np.abs(gm - cm))) / big if big > 0 else 0.0
            worst["marker"] = max(worst["marker"], d)
            if not d <= MARKER_REL:
                failures.append(f"report {i}: marker_energy rel err {d:.2e}")
            de = int(np.max(np.abs(np.asarray(g["envelope"]) - np.asarray(c["envelope"]))))
            worst["envelope"] = max(worst["envelope"], de)
            if de > ENV_LSB:
                failures.append(f"report {i}: envelope off by {de} LSB")
            gs, cs = g["envelope_scale"], c["envelope_scale"]
            ds = abs(gs - cs) / gs if gs else abs(cs)
            worst["scale"] = max(worst["scale"], ds)
            if not ds <= SCALE_REL:
                failures.append(f"report {i}: envelope_scale reference {gs} C {cs}")
    if proc.wait() != 0:
        failures.append(f"tick-replay exited {proc.returncode}")
    if n_gold != n_c:
        failures.append(f"report count: reference {n_gold}, C {n_c}")
    return name, n_gold, worst, failures


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--replay", required=True)
    a = ap.parse_args(argv)
    sets = sorted(Path(a.dir).glob("*.golden.jsonl"))
    if not sets:
        print(f"no *.golden.jsonl in {a.dir}", file=sys.stderr)
        return 1
    bad = 0
    for gp in sets:
        name, n, w, failures = compare_set(gp, a.replay)
        status = "PASS" if not failures else "FAIL"
        print(f"{status} {name}: {n} reports; max profile rel {w['profile']:.2e}, "
              f"peak_index {w['peak']:.2e} sample, marker rel {w['marker']:.2e}, "
              f"envelope {w['envelope']} LSB, scale rel {w['scale']:.2e}, snr {w['snr_db']:.2e} dB")
        for f in failures[:20]:
            print(f"    {f}")
        if len(failures) > 20:
            print(f"    ... {len(failures) - 20} more")
        bad += bool(failures)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
