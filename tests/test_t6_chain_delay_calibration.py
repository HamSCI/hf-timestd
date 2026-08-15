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
