"""Phase 2 of the split: fusion runs no in-loop TEC science.

The 08-10 split plan (and the hamsci-physics separation plan) remove
TECEstimator from multi_broadcast_fusion: HF-derived TEC is a science
product (hamsci-physics), not a timing correction.  Fusion keeps the
GNSS-VTEC DB read and the HFPropagationModel.predict seam — both
consume hamsci-dsp — and nothing else from the TEC solver family.

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


def test_fusion_keeps_the_sanctioned_seams():
    text = SRC.read_text()
    # The two seams that STAY: GNSS-VTEC read and the propagation model.
    assert "_read_gnss_vtec" in text
    assert "HFPropagationModel" in text
