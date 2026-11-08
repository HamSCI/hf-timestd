#!/usr/bin/env python3
"""Prove the radiod C pilot detector equal to the Python fine stage.

  compare_golden.py DIR --replay PATH/pilot-replay [--rate 96000]

For every <name>.golden.jsonl in DIR (written by make_golden.py), runs
``pilot-replay DIR/<name>.cf32 RATE``, pairs the folds by order and checks:

* |Δ edge_offset_samples| <= 0.001 sample (about 10 ns at 96 kHz);
* each battery field within 1 % relative or 1e-6 absolute, whichever is
  looser.  Both sides NaN (the C prints null) counts as agreement.

Prints the per-set worst |Δ| and exits 1 on any failure.
"""
import argparse
import json
import math
import subprocess
import sys
from pathlib import Path

EDGE_TOL = 0.001
REL_TOL = 0.01
ABS_TOL = 1e-6
BATTERY = ["plateau_amplitude", "fit_rms", "fold_retention",
           "split_half_delta_samples", "peak_prominence",
           "transition_width_samples", "apex_distance_samples"]


def _num(v):
    return float("nan") if v is None else float(v)


def field_ok(a, b):
    a, b = _num(a), _num(b)
    if math.isnan(a) or math.isnan(b):
        return math.isnan(a) and math.isnan(b)
    return abs(a - b) <= max(ABS_TOL, REL_TOL * abs(a))


def compare_set(golden_path, replay, rate):
    name = golden_path.name[: -len(".golden.jsonl")]
    cf32 = golden_path.with_name(f"{name}.cf32")
    gold = [json.loads(line) for line in golden_path.read_text().splitlines() if line]
    run = subprocess.run([str(replay), str(cf32), str(rate)],
                         capture_output=True, text=True, check=True)
    c = [json.loads(line) for line in run.stdout.splitlines() if line]
    failures = []
    if len(c) != len(gold):
        failures.append(f"fold count: python {len(gold)}, C {len(c)}")
    worst_edge = 0.0
    worst_field = {k: 0.0 for k in BATTERY}
    for i, (g, r) in enumerate(zip(gold, c)):
        d = abs(g["edge_offset_samples"] - r["edge_offset_samples"])
        worst_edge = max(worst_edge, d)
        if not d <= EDGE_TOL:
            failures.append(f"fold {i}: |Δedge| {d:.3e} > {EDGE_TOL}")
        if g["edge_rtp"] != r["edge_rtp"]:
            failures.append(f"fold {i}: edge_rtp {g['edge_rtp']} vs {r['edge_rtp']}")
        if g["n_seconds_folded"] != r["n_seconds_folded"]:
            failures.append(f"fold {i}: n_seconds_folded differs")
        for k in BATTERY:
            a, b = _num(g[k]), _num(r[k])
            if not (math.isnan(a) or math.isnan(b)):
                worst_field[k] = max(worst_field[k], abs(a - b))
            if not field_ok(g[k], r[k]):
                failures.append(f"fold {i}: {k} python {g[k]!r} C {r[k]!r}")
    return name, len(gold), worst_edge, worst_field, failures


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--replay", required=True)
    ap.add_argument("--rate", type=int, default=96000)
    a = ap.parse_args(argv)
    sets = sorted(Path(a.dir).glob("*.golden.jsonl"))
    if not sets:
        print(f"no *.golden.jsonl in {a.dir}", file=sys.stderr)
        return 1
    bad = 0
    for g in sets:
        name, n, we, wf, failures = compare_set(g, a.replay, a.rate)
        status = "PASS" if not failures else "FAIL"
        fields = " ".join(f"{k}={v:.2e}" for k, v in wf.items())
        print(f"{status} {name}: {n} folds, max|Δedge| {we:.3e} sample; max|Δ| {fields}")
        for f in failures:
            print(f"    {f}")
        bad += bool(failures)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
