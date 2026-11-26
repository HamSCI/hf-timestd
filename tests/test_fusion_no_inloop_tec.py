"""Phase 2 of the split: fusion runs no in-loop TEC science.

The 08-10 split plan (and the hamsci-physics separation plan) remove
TECEstimator from multi_broadcast_fusion: HF-derived TEC is a science
product (hamsci-physics), not a timing correction.  Fusion keeps the
HFPropagationModel.predict seam and reads no science product (the
GNSS-VTEC read left in v3.96).

Source-scan style (like hamsci-dsp's import lint) so even lazy,
function-level wiring is caught.
"""
import re
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1]
       / "src" / "hf_timestd" / "core" / "multi_broadcast_fusion.py")


def _code_lines():
    for i, line in enumerate(SRC.read_text().split("\n"), 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        yield i, line


def test_fusion_does_not_use_the_tec_estimator():
    offenders = [
        f"{i}: {line.strip()[:90]}"
        for i, line in _code_lines()
        if re.search(r"TECEstimator|estimate_tec|tec_estimator", line)
    ]
    assert not offenders, (
        "fusion must not run in-loop TEC science (split Phase 2); "
        "found: " + "; ".join(offenders[:6]))


def test_fusion_reads_no_science_product():
    """Decision 10: GNSS VTEC belongs to hamsci-physics; the timing core reads none of its output."""
    code = "\n".join(line for _, line in _code_lines())
    assert "gnss_vtec" not in code and "last_vtec_tecu" not in code
    assert "HFPropagationModel" in code      # the propagation seam stays
