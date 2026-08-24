"""[timing.t6_pps].labeling_convention — what a T6 label means.

``content`` (the default): a label answers "when did this energy reach
the antenna".  The only delay between the antenna and the sample is the
µs-class analog path, carried by ``delay_budget_ns``; everything after
the ADC — USB transfer, the 3.24 M-point FFT, filtering, scheduling — is
*pipeline latency*, not part of the label, so ``filter_group_delay_ns``
is not applied.  See docs/design/CONTENT_TIME_LABELING_CONVENTION.md.

``legacy``: the pre-2026-08-24 arithmetic, where a 16.618 ms constant
calibrated once against T4 was folded into every anchor.  Kept so a site
can flip back in one key without a redeploy, and so the change is
reversible independently of the repo split it ships with.

The two must differ by exactly the configured constant and nothing else.
"""
from __future__ import annotations

import pytest

from hf_timestd.core.core_recorder_v2 import CoreRecorderV2

settings = CoreRecorderV2._t6_fine_settings

MEASURED = 16_618_000          # what AC0G-B4 had configured


def test_content_is_the_default():
    s = settings({})
    assert s["labeling_convention"] == "content"


def test_content_does_not_apply_the_group_delay():
    s = settings({"filter_group_delay_ns": MEASURED})
    assert s["filter_group_delay_ns"] == 0


def test_content_remembers_what_it_ignored():
    """The operator must be able to see the retired value, not guess it."""
    s = settings({"filter_group_delay_ns": MEASURED})
    assert s["filter_group_delay_ns_configured"] == MEASURED


def test_legacy_applies_it_exactly_as_before():
    s = settings({"filter_group_delay_ns": MEASURED,
                  "labeling_convention": "legacy"})
    assert s["filter_group_delay_ns"] == MEASURED


def test_the_two_conventions_differ_by_exactly_the_constant():
    content = settings({"filter_group_delay_ns": MEASURED})
    legacy = settings({"filter_group_delay_ns": MEASURED,
                       "labeling_convention": "legacy"})
    assert legacy["filter_group_delay_ns"] - content["filter_group_delay_ns"] \
        == MEASURED
    # ...and in nothing else.
    ignore = {"filter_group_delay_ns", "labeling_convention"}
    assert {k: v for k, v in content.items() if k not in ignore} == \
           {k: v for k, v in legacy.items() if k not in ignore}


def test_the_analog_term_survives_both_conventions():
    """delay_budget_ns is the µs-class ε; the convention never touches it."""
    for conv in ("content", "legacy"):
        s = settings({"delay_budget_ns": 10_000, "labeling_convention": conv})
        assert s["delay_budget_ns"] == 10_000


def test_an_unknown_convention_is_refused_loudly():
    with pytest.raises(ValueError, match="labeling_convention"):
        settings({"labeling_convention": "whatever"})


def test_legacy_still_enforces_the_group_delay_bound():
    with pytest.raises(ValueError, match="filter_group_delay_ns"):
        settings({"filter_group_delay_ns": 300_000_000,
                  "labeling_convention": "legacy"})


def test_content_refuses_an_out_of_bound_value_too():
    """A nonsense constant is a config error whether or not it is applied."""
    with pytest.raises(ValueError, match="filter_group_delay_ns"):
        settings({"filter_group_delay_ns": 300_000_000})


# ── the judge's plane measurement follows the convention ────────────────

from hf_timestd.core.core_recorder_v2 import label_plane_measure_for


def test_content_lets_the_judge_measure_the_plane():
    assert label_plane_measure_for(
        {"t6_pps": {"labeling_convention": "content"}}) is True


def test_measuring_is_the_default_when_unspecified():
    assert label_plane_measure_for({"t6_pps": {}}) is True
    assert label_plane_measure_for({}) is True


def test_legacy_stops_the_judge_measuring_the_plane():
    """Under legacy the planes coincide, so a measured term would be T6's
    own error — correcting by it would blind the cross-bench gate.  Seen
    live on AC0G-B4: labels pinned legacy, tracker reporting a -3.77 ms
    "plane offset" that was really T6 residual plus a restart transient."""
    assert label_plane_measure_for(
        {"t6_pps": {"labeling_convention": "legacy"}}) is False


def test_the_recorder_uses_that_helper():
    """Guard against the wiring being inlined and drifting."""
    import inspect
    from hf_timestd.core.core_recorder_v2 import CoreRecorderV2
    assert "label_plane_measure_for(" in inspect.getsource(CoreRecorderV2.__init__)
