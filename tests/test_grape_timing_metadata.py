"""The GRAPE product must carry the timing authority it was recorded under.

Every minute of every decimated product shipped `uncertainty_ms = 999.9`,
`quality_grade = "X"`, `d_clock_ms = 0.0` — verified across all 1440
minutes of AC0G-B4 20260814.  Not staleness: a field-name mismatch.

`decimation_pipeline` read `meta.get('uncertainty_ms', 999.9)` and
`meta.get('quality_grade', 'X')` from the raw chunk sidecar, but
`binary_archive_writer` has never written those keys.  It writes a
`timing` block: `offset_ns`, `offset_sigma_ns`, `judge_tier`.  So the
defaults won every time, for every tier, and the science product was
disconnected from the timing chain entirely — including from the fusion
calibration, which had real ~0.5 ms uncertainties all along.

With T6 authoritative the sidecar now carries offset_sigma_ns = 982496
(0.98 ms), which is grade A on the L2 ladder (A <2 ms, B <4, C <8, D
otherwise).  Shipping "X" for that is throwing away the answer.

This maps forward only.  Products already written are not rewritten.
"""
import pytest

from hf_timestd.grape.decimation_pipeline import timing_from_sidecar


def sidecar(**timing):
    return {"channel_name": "SHARED_5000", "timing": dict(timing)}


def test_uncertainty_and_offset_come_from_the_timing_block():
    """The live AC0G-B4 values, 2026-08-15 under T6."""
    meta = sidecar(offset_ns=3536564.66, offset_sigma_ns=982495.95,
                   judge_tier="T6")

    d_clock_ms, uncertainty_ms, grade = timing_from_sidecar(meta)

    assert d_clock_ms == pytest.approx(3.53656466)
    assert uncertainty_ms == pytest.approx(0.98249595)
    assert grade == "A"


def test_absent_timing_block_keeps_the_unknown_sentinels():
    """A chunk recorded with no verdict must still say so honestly."""
    assert timing_from_sidecar({"channel_name": "x"}) == (0.0, 999.9, "X")


def test_absent_sidecar_keeps_the_unknown_sentinels():
    assert timing_from_sidecar(None) == (0.0, 999.9, "X")


@pytest.mark.parametrize("sigma_ms,expected", [
    (0.98, "A"),    # T6
    (1.99, "A"),
    (2.00, "B"),
    (3.99, "B"),
    (4.00, "C"),
    (7.99, "C"),
    (8.00, "D"),
    (25.0, "D"),    # the old placeholder would have graded D, not X
])
def test_grade_follows_the_l2_uncertainty_ladder(sigma_ms, expected):
    meta = sidecar(offset_ns=0.0, offset_sigma_ns=sigma_ms * 1e6)

    assert timing_from_sidecar(meta)[2] == expected


def test_a_timing_block_without_a_sigma_is_not_graded():
    """Partial provenance is worse than none if it is graded anyway."""
    meta = sidecar(offset_ns=1234.0, judge_tier="T6")

    assert timing_from_sidecar(meta) == (0.0, 999.9, "X")


def test_explicit_legacy_keys_still_win():
    """Any producer that already writes the flat keys keeps working."""
    meta = {"uncertainty_ms": 1.25, "d_clock_ms": -0.5,
            "quality_grade": "B",
            "timing": {"offset_ns": 9e9, "offset_sigma_ns": 9e9}}

    assert timing_from_sidecar(meta) == (-0.5, 1.25, "B")
