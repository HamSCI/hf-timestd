"""Golden vectors for the radiod tick-detector C core (tick plan, Task 1)."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts" / "radiod_ticks"
SR = 24_000
TOL = 0.02e-3 * SR          # 0.02 ms in samples
REPLAY = Path(os.environ.get("TICK_REPLAY",
                             ROOT.parent / "ka9q-radio" / "src" / "tick-replay"))


@pytest.fixture(scope="module")
def golden(tmp_path_factory):
    out = tmp_path_factory.mktemp("ticks")
    subprocess.run([sys.executable, str(SCRIPTS / "make_golden.py"), "synth", "--out", str(out),
                    "--sets", "wwv_only,both"], check=True, cwd=ROOT)
    return out


def load_jsonl(p):
    with open(p) as f:
        return [json.loads(line) for line in f if line.strip()]


@pytest.mark.parametrize("name,stations", [("wwv_only", ["wwv"]), ("both", ["wwv", "wwvh"])])
def test_reference_recovers_truth(golden, name, stations):
    assert (golden / f"{name}.cf32").stat().st_size == SR * 120 * 8
    rows = load_jsonl(golden / f"{name}.golden.jsonl")
    assert len(rows) == 119
    checked = 0
    for r in rows:
        assert r["complete"] and r["nprofile"] == 840 and r["step"] == 2
        assert len(r["envelope"]) == 1000
        for t in ["wwv", "wwvh", "bpm"]:      # every template reports, present or not
            assert len(r[f"profile_{t}"]) == 840
        for t in stations:
            truth = r[f"truth_{t}_index"]
            if truth is None:                  # seconds 0, 29, 59
                continue
            err = r[f"peak_index_{t}"] - truth
            assert abs(err) <= TOL, (name, t, r["second_index"], err)
            checked += 1
    assert checked == len(stations) * (119 - 5)   # seconds 0, 60 (markers), 29, 89, 59: no tick


@pytest.mark.skipif(not REPLAY.exists(), reason=f"no tick-replay at {REPLAY}")
def test_c_matches_reference(golden):
    run = subprocess.run([sys.executable, str(SCRIPTS / "compare_golden.py"), str(golden),
                          "--replay", str(REPLAY)], capture_output=True, text=True, cwd=ROOT)
    assert run.returncode == 0, run.stdout + run.stderr
