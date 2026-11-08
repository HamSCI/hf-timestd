"""Golden vectors for the radiod pilot-detector C port (Task 2)."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "radiod_pilot" / "make_golden.py"


def run_make_golden(args):
    subprocess.run([sys.executable, str(SCRIPT), *args], check=True, cwd=ROOT)


def load_jsonl(p):
    return [json.loads(line) for line in Path(p).read_text().splitlines() if line]


def test_synth_set_recovers_truth(tmp_path):
    run_make_golden(["synth", "--out", str(tmp_path)])
    for name in ["clean", "cn0_48", "frac_0p37", "tilt"]:
        assert (tmp_path / f"{name}.cf32").stat().st_size == 96000 * 150 * 8
        rows = load_jsonl(tmp_path / f"{name}.golden.jsonl")
        assert len(rows) >= 3
        for r in rows[1:]:                      # first fold may be bootstrap
            err = r["edge_offset_samples"] - r["truth_edge_offset_samples"]
            # cn0_48 is noise-limited: the Python itself scatters +-0.4
            # sample there (measured), so the 0.01 bound holds only for the
            # noise-free sets; the noisy one gets a sanity bound.
            tol = 1.0 if name == "cn0_48" else 0.01
            assert abs(err) < tol, (name, err)
