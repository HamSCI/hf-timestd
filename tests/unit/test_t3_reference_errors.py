"""Per-minute T3 and T2 errors against the best reference (Task A3).

Every error reads "estimate minus truth".  A row is built here the way
``AuthorityManager._t3_shadow_minute_row`` writes it, with only the fields
``reference_errors`` reads.
"""
from __future__ import annotations

import json

import pytest

from hf_timestd.core.t3_reference_errors import ReferencePair, reference_errors


def _row(t3_minus_t6_ms=None, t3_vs=None, t4_vs=None, t5_vs=None, t2_vs=None,
         t4_offset_ms=None, t2_offset_ms=None):
    def vs(v):
        return None if v is None else {
            "shadow_residual_ns": v[0], "sigma_ns": 1e6, "vs_tier": v[1]}
    return {
        "kind": "minute", "utc_minute": 1_790_000_000,
        "t3_shadow": (None if t3_minus_t6_ms is None
                      else {"t3_minus_t6_ms": t3_minus_t6_ms}),
        "t3_vs_adopted": vs(t3_vs), "t4_vs_adopted": vs(t4_vs),
        "t5_vs_adopted": vs(t5_vs), "t2_vs_adopted": vs(t2_vs),
        "t4_offset_ms": t4_offset_ms, "t2_offset_ms": t2_offset_ms,
    }


def test_t6_with_t4_takes_t3_from_the_sample_plane_and_t2_from_the_host():
    row = _row(t3_minus_t6_ms=0.26, t4_offset_ms=0.02, t2_offset_ms=-0.49)
    assert reference_errors(row) == ReferencePair("T6", 0.26, pytest.approx(0.51))


def test_t6_without_t4_reads_t2_from_the_judge_when_it_names_t6():
    row = _row(t3_minus_t6_ms=0.26, t2_vs=(510_000.0, "T6"))
    assert reference_errors(row) == ReferencePair("T6", 0.26, pytest.approx(0.51))
    row = _row(t3_minus_t6_ms=0.26, t2_vs=(510_000.0, "T5"))
    assert reference_errors(row).t2_error_ms is None


def test_t5_reference_with_and_without_t4():
    row = _row(t3_vs=(300_000.0, "T5"), t4_offset_ms=0.0, t2_offset_ms=-0.4)
    assert reference_errors(row) == ReferencePair("T5", 0.3, pytest.approx(0.4))
    row = _row(t3_vs=(300_000.0, "T5"), t2_vs=(450_000.0, "T5"))
    assert reference_errors(row) == ReferencePair("T5", 0.3, pytest.approx(0.45))
    row = _row(t3_vs=(300_000.0, "T5"), t2_vs=(450_000.0, "T4"))
    assert reference_errors(row).t2_error_ms is None


def test_t4_only_station_uses_the_judge_difference():
    row = _row(t3_vs=(1_200_000.0, "T4"), t4_offset_ms=0.0, t2_offset_ms=-0.6)
    assert reference_errors(row) == ReferencePair("T4", 1.2, pytest.approx(0.6))


def test_t4_difference_of_two_judge_residuals_and_mismatched_tiers():
    row = _row(t3_vs=(1_500_000.0, "T6"), t4_vs=(300_000.0, "T6"),
               t4_offset_ms=0.0, t2_offset_ms=-0.6)
    assert reference_errors(row) == ReferencePair("T4", 1.2, pytest.approx(0.6))
    row = _row(t3_vs=(1_500_000.0, "T6"), t4_vs=(300_000.0, "T5"),
               t4_offset_ms=0.0, t2_offset_ms=-0.6)
    assert reference_errors(row) is None


def test_no_reference_yields_none():
    assert reference_errors(_row(t2_offset_ms=-0.6)) is None


def test_nan_and_missing_offsets_yield_no_pair():
    row = _row(t3_minus_t6_ms=0.26, t4_offset_ms=None, t2_offset_ms=None)
    assert reference_errors(row).t2_error_ms is None
    assert reference_errors(_row(t3_minus_t6_ms=float("nan"))) is None
    row = _row(t3_minus_t6_ms=0.26, t4_offset_ms=0.0,
               t2_offset_ms=float("nan"))
    assert reference_errors(row).t2_error_ms is None


def test_a_boolean_counts_as_missing():
    assert reference_errors(_row(t3_minus_t6_ms=True)) is None
    row = _row(t3_minus_t6_ms=0.26, t4_offset_ms=True, t2_offset_ms=0.1)
    assert reference_errors(row).t2_error_ms is None


def test_a_missing_t3_error_at_the_best_reference_does_not_fall_through():
    # T6 is on the row but its t3_minus_t6_ms is NaN; a T5 residual exists.
    row = _row(t3_minus_t6_ms=float("nan"), t3_vs=(300_000.0, "T5"),
               t4_offset_ms=0.0, t2_offset_ms=-0.4)
    assert reference_errors(row) is None


def test_garbage_rows_do_not_raise():
    assert reference_errors({}) is None
    assert reference_errors({"t3_shadow": "x", "t3_vs_adopted": 5}) is None
    row = _row(t3_minus_t6_ms=0.26)
    json.dumps(row)
    assert reference_errors(row) == ReferencePair("T6", 0.26, None)


# ── final review I-2: the judge adopted T3 ───────────────────────────
#
# Once T3 holds admission the judge may adopt it.  Then T3 has no shadow
# residual of its own, and every other bench's *_vs_adopted names "T3":
# ref_ns = ref - T3, so T3 - ref = -ref_ns and T2 - ref = t2_ns - ref_ns.


def test_t4_station_with_t3_adopted_reads_t3_from_the_t4_residual():
    # T4 sits 1.2 ms after T3: T3's error against T4 reads -1.2 ms.
    row = _row(t4_vs=(1_200_000.0, "T3"), t4_offset_ms=0.0, t2_offset_ms=-0.6,
               t2_vs=(5_000_000.0, "T3"))
    # T2 keeps the host plane on a T4 station, whatever the judge says.
    assert reference_errors(row) == ReferencePair(
        "T4", pytest.approx(-1.2), pytest.approx(0.6))


def test_t5_station_with_t3_adopted_reads_both_errors_from_the_judge():
    # T5 sits 0.3 ms before T3 (ref - T3 = -0.3); T2 sits 0.15 ms after
    # T3 (T2 - T3 = +0.15).  T3 - T5 = +0.3; T2 - T5 = 0.15 - (-0.3) = 0.45.
    row = _row(t5_vs=(-300_000.0, "T3"), t2_vs=(150_000.0, "T3"))
    assert reference_errors(row) == ReferencePair(
        "T5", pytest.approx(0.3), pytest.approx(0.45))
    # With T4 present T2 keeps the host plane.
    row = _row(t5_vs=(-300_000.0, "T3"), t2_vs=(150_000.0, "T3"),
               t4_offset_ms=0.0, t2_offset_ms=-0.4)
    assert reference_errors(row) == ReferencePair(
        "T5", pytest.approx(0.3), pytest.approx(0.4))
    # A T2 residual against another bench gives no T2 error.
    row = _row(t5_vs=(-300_000.0, "T3"), t2_vs=(150_000.0, "T5"))
    assert reference_errors(row) == ReferencePair("T5", pytest.approx(0.3), None)


def test_t3_adopted_prefers_t5_over_t4_and_a_bad_t5_does_not_fall_through():
    row = _row(t5_vs=(-300_000.0, "T3"), t4_vs=(1_200_000.0, "T3"),
               t4_offset_ms=0.0, t2_offset_ms=-0.4)
    assert reference_errors(row).reference == "T5"
    row = _row(t5_vs=(float("nan"), "T3"), t4_vs=(1_200_000.0, "T3"),
               t4_offset_ms=0.0, t2_offset_ms=-0.4)
    assert reference_errors(row) is None
