"""T6 enabled with no chain-delay calibration must be loud at startup.

`chain_delay_calib_s` is ASSERTED from config and never derived
(T6_ORIGIN_ASSERTION_DESIGN §5 — deriving it from radiod's advertised
wall clock made the correction a function of the mapping it was
correcting, producing 58 different origins in one night on AC0G-B4).

The default is 0.0.  An unset knob therefore does not fail, warn at
startup, or degrade: it silently asserts that the RF path from the TS-1
BPSK modulator through coax, RX-888 ADC, radiod DSP and out to RTP takes
exactly zero time.  Every sample label is then early by the full chain
delay.

Measured on B4 2026-08-15: the anchor was pinned to an EXACT integer
second (utc_ns=1786756113000000000) while the capture log recorded
`residual=+15.863 ms, effective_chain_delay=0 ns`, and T6 read
-16.098 ms against T4.  The knob is not documented in
config/timestd-config.toml.template either, so nothing points a new site
at it -- it had been firing a "REPORTED ONLY" warning for days.

This does NOT start deriving the value; it only refuses to be quiet
about asserting zero.
"""
import pytest

from hf_timestd.cli import t6_group_delay_issue
from hf_timestd.core.core_recorder_v2 import t6_chain_delay_uncalibrated


def test_enabled_t6_without_a_calibration_is_flagged():
    assert t6_chain_delay_uncalibrated({"enabled": True}) is True


def test_enabled_t6_with_an_explicit_zero_is_still_flagged():
    """Zero is the silent default; an operator who means it should say so
    in the log rather than have it pass unnoticed."""
    assert t6_chain_delay_uncalibrated(
        {"enabled": True, "chain_delay_calib_s": 0.0}) is True


def test_calibrated_t6_is_not_flagged():
    assert t6_chain_delay_uncalibrated(
        {"enabled": True, "chain_delay_calib_s": 0.016098}) is False


def test_disabled_t6_is_not_flagged():
    """No T6, no anchor, nothing to mis-label."""
    assert t6_chain_delay_uncalibrated(
        {"enabled": False, "chain_delay_calib_s": 0.0}) is False


# ────────────────────────────────────────────────────────────────────
# Fresh-install surface: `hf-timestd validate`
# ────────────────────────────────────────────────────────────────────

def test_validate_flags_an_armed_t6_with_no_measured_group_delay():
    """A DASI image ships filter_group_delay_ns unset, i.e. zero.

    Zero is SAFE but never right: with an honest sigma the cross-bench
    gate blocks T6 loudly (AC0G-B4 sat at T4 with CRITICAL every 60 s
    until it was measured), so the site degrades rather than lying.  It
    just never gets T6.  validate is the sigmond contract surface a
    fresh install actually reads, so it must say so there.
    """
    issue = t6_group_delay_issue({"timing": {"t6_pps": {"enabled": True}}})

    # Under the content-time convention (the default since 2026-08-24) an
    # unset group delay is CORRECT: the constant is retired, and only the
    # µs-class analog term (delay_budget_ns) belongs in the anchor.
    assert issue is None


def test_validate_warns_when_a_retired_group_delay_is_still_configured():
    """A leftover 16.618 ms constant is now the thing worth saying.

    It is inert under the content convention, but a site that still
    carries it is one `labeling_convention = "legacy"` away from
    re-applying 16.6 ms to every label, and the operator should be told
    the key did nothing rather than assume it did.
    """
    issue = t6_group_delay_issue(
        {"timing": {"t6_pps": {"enabled": True,
                               "filter_group_delay_ns": 16_618_000}}})

    assert issue is not None
    assert issue["severity"] == "warn"
    assert "filter_group_delay_ns" in issue["message"]
    assert "content" in issue["message"]


def test_legacy_convention_keeps_the_original_measure_it_warning():
    """A site that opted back into legacy still needs the constant."""
    issue = t6_group_delay_issue(
        {"timing": {"t6_pps": {"enabled": True,
                               "labeling_convention": "legacy"}}})

    assert issue is not None
    assert issue["severity"] == "warn"
    assert "filter_group_delay_ns" in issue["message"]


def test_legacy_convention_is_quiet_once_measured():
    issue = t6_group_delay_issue(
        {"timing": {"t6_pps": {"enabled": True,
                               "labeling_convention": "legacy",
                               "filter_group_delay_ns": 16_618_000}}})

    assert issue is None


def test_validate_is_quiet_when_t6_is_disabled():
    issue = t6_group_delay_issue({"timing": {"t6_pps": {"enabled": False}}})

    assert issue is None


def test_validate_is_quiet_when_the_fine_stage_is_off():
    """Without the fine stage the anchor comes from the coarse cascade,
    where chain_delay_calib_s is the knob instead."""
    issue = t6_group_delay_issue(
        {"timing": {"t6_pps": {"enabled": True, "fine_stage_enabled": False}}})

    assert issue is None
