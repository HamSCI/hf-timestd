"""A whole-second naming slip must be caught by counter continuity.

Measured on AC0G-B4: three excursions across 2,176 consecutive T6 anchor
pairs, each +1 s then -1 s one anchor later, at 08:50:05Z, 15:05:02Z and
21:40:03Z on 2026-08-25.  TWO OF THE THREE were under the legacy
labelling convention, so this is endemic rather than an artifact of a
convention change.  Each lasts one anchor (~30 s) and self-corrects,
which is why it went unseen -- and why a single sample taken inside one
of those windows caused a working convention change to be rolled back.

The existing guard cannot see it:

    named = pps_utc_sec + round(edge_utc - pps_utc_sec)
    if abs(edge_utc - named) > 0.4: reject

when round() tips, `named` moves with it, so the residual is small again
and the guard passes.  It validates the answer against itself.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from hf_timestd.core.t6_naming_continuity import (
    predicted_edge_utc,
    reconcile_named_second,
)

SR = 96_000
# the live pair either side of the 15:05:02Z excursion
GOOD_RTP, GOOD_UTC_NS = 2_150_309_068, 1_787_670_271_000_011_838
STEP = 2_880_000            # 30 s at 96 kHz


def anchor(rtp=GOOD_RTP, utc_ns=GOOD_UTC_NS):
    return SimpleNamespace(anchor_rtp=rtp, anchor_utc_ns=utc_ns)


class TestPrediction:
    def test_carries_the_counter_forward(self):
        p = predicted_edge_utc(GOOD_RTP + STEP, anchor(), SR)
        assert p == pytest.approx(GOOD_UTC_NS / 1e9 + 30.0, abs=1e-6)

    def test_handles_the_32_bit_wrap(self):
        a = anchor(rtp=(1 << 32) - 1000)
        p = predicted_edge_utc(1000, a, SR)          # wrapped past zero
        assert p == pytest.approx(GOOD_UTC_NS / 1e9 + 2000 / SR, abs=1e-6)

    def test_no_anchor_means_no_prediction(self):
        assert predicted_edge_utc(GOOD_RTP, None, SR) is None


class TestSlipCorrection:
    def _named(self, offset_s=0):
        """The naming the fine stage would emit, plus an integer slip."""
        return int(round(GOOD_UTC_NS / 1e9 + 30.0)) + offset_s

    def test_the_live_plus_one_second_slip_is_corrected(self):
        named, slip = reconcile_named_second(
            self._named(+1), GOOD_RTP + STEP, anchor(), SR)
        assert slip == 1
        assert named == self._named(0)

    def test_a_minus_one_second_slip_is_corrected(self):
        named, slip = reconcile_named_second(
            self._named(-1), GOOD_RTP + STEP, anchor(), SR)
        assert slip == -1
        assert named == self._named(0)

    def test_a_correct_naming_is_untouched(self):
        named, slip = reconcile_named_second(
            self._named(0), GOOD_RTP + STEP, anchor(), SR)
        assert slip == 0 and named == self._named(0)

    def test_sub_second_disagreement_is_left_alone(self):
        """The fine stage owns the sub-second placement; this check has
        no standing there and must not round it away."""
        a = anchor(utc_ns=GOOD_UTC_NS + 300_000_000)     # 300 ms off
        named, slip = reconcile_named_second(
            self._named(0), GOOD_RTP + STEP, a, SR)
        assert slip == 0

    def test_first_acquisition_has_nothing_to_check_against(self):
        named, slip = reconcile_named_second(
            self._named(+1), GOOD_RTP + STEP, None, SR)
        assert slip == 0 and named == self._named(+1)

    def test_a_stale_anchor_is_not_trusted_to_predict(self):
        old = anchor(utc_ns=GOOD_UTC_NS - 10_000 * 10**9)
        named, slip = reconcile_named_second(
            self._named(+1), GOOD_RTP + STEP, old, SR)
        assert slip == 0

    def test_only_integer_seconds_are_ever_applied(self):
        for off in (-2, -1, 1, 2):
            named, slip = reconcile_named_second(
                self._named(off), GOOD_RTP + STEP, anchor(), SR)
            assert slip == off
            assert named == self._named(0)


class TestAgainstTheRecordedExcursions:
    """Replay the three measured excursions as anchor pairs."""

    CASES = [
        # (prev_rtp, prev_utc_ns, d_rtp, observed_d_utc_s)
        (2_150_309_068, 1_787_645_405_000_011_000, STEP, 31.0),
        (2_150_309_068, 1_787_670_302_000_011_941, STEP, 31.0),
        (2_150_309_068, 1_787_694_003_000_011_000, STEP, 31.0),
    ]

    def test_each_recorded_excursion_is_caught(self):
        for prev_rtp, prev_utc, drtp, dutc in self.CASES:
            a = anchor(rtp=prev_rtp, utc_ns=prev_utc)
            bad = int(round(prev_utc / 1e9 + dutc))
            named, slip = reconcile_named_second(bad, prev_rtp + drtp, a, SR)
            assert slip == 1, f"missed a +1 s slip on {prev_utc}"
            assert named == bad - 1

    def test_the_normal_30_second_advance_is_never_touched(self):
        for prev_rtp, prev_utc, drtp, _ in self.CASES:
            a = anchor(rtp=prev_rtp, utc_ns=prev_utc)
            ok = int(round(prev_utc / 1e9 + 30.0))
            named, slip = reconcile_named_second(ok, prev_rtp + drtp, a, SR)
            assert slip == 0 and named == ok
