"""Acquisition runs on the fold's edge; the namer only says which second.

⛔ Read the fixture arithmetic before changing a number.  An earlier draft
of this file described two different edges at once -- `edge_rtp` implied an
RTP phase of 87916 samples while `edge_offset_samples` said 47916 -- so no
correct implementation could have satisfied it, and a wrong one did.  Every
constant below describes ONE edge, and the comment beside each says how it
was derived so the next reader can check it rather than trust it.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hf_timestd.core.core_recorder_v2 import CoreRecorderV2
from hf_timestd.core.bpsk_edge_fine_stage import FineEdgeEstimate
from hf_timestd.core.t6_battery import T6Battery, BatteryThresholds

SR = 96000
K = 30
NAMED_SECOND = 1_700_000_000
# B4's TS-1 injection constant.  1595.328 samples at 96 kHz.
CHAIN_DELAY_NS = 16_618_000
CHAIN_DELAY_SEC = CHAIN_DELAY_NS / 1e9

# --- ONE edge -----------------------------------------------------------
# 12_000_000 = 125 * 96000 exactly, so an RTP second boundary falls there.
# The edge sits round(0.016618 * 96000) = 1595 samples past it -- the same
# 16.618 ms the chain delay carries, which is what makes the firing instant
# land on an integer second below.
EDGE_RTP = 12_000_000 + 1595
# The SAME edge's fold-domain position: (edge_rtp - registration) mod SR
# with a registration congruent to 0 mod SR.  It agrees with the RTP phase
# only because of that choice; in the field the registration is arbitrary.
# The implementation must not read this field at all -- pinned by
# test_the_fold_domain_position_is_never_read.
EDGE_OFFSET_SAMPLES = 1595.0
# The matched filter's own edge, deliberately 500 samples (5.2 ms) away.
# The MF is the demoted coarse witness and has been measured a full 20 ms
# off the true apex (B4, 2026-09-04).  Every number the method computes
# must come from the fold's edge, never this one.
MF_EDGE_RTP = EDGE_RTP + 500

N_BLOCKS = 4
# _drive advances the edge by one fold block per call, so the last block's
# edge belongs to this second.
LAST_NAMED = NAMED_SECOND + (N_BLOCKS - 1) * K
LAST_EDGE_RTP = EDGE_RTP + (N_BLOCKS - 1) * SR * K


def _estimate(edge_rtp, **over):
    kw = dict(
        edge_offset_samples=EDGE_OFFSET_SAMPLES, edge_rtp=edge_rtp,
        edge_subsample=0.0,
        n_seconds_folded=K, plateau_amplitude=1.0, fit_rms=0.02,
        fold_retention=0.98, split_half_delta_samples=0.05,
        peak_prominence=0.001, apex_distance_samples=0.5,
        transition_width_samples=6.0,
    )
    kw.update(over)
    return FineEdgeEstimate(**kw)


def _result(chain_delay_ns=CHAIN_DELAY_NS):
    return SimpleNamespace(chain_delay_ns=chain_delay_ns)


def _recorder(named=NAMED_SECOND, battery=None):
    """A bare namespace carrying only what the method touches."""
    r = SimpleNamespace()
    for name in ("_t6_disambiguate_via_external_reference",
                 "_t6_say_once", "_t6_reported_sigma_ms",
                 "_t6_battery_forget_run"):
        setattr(r, name, getattr(CoreRecorderV2, name).__get__(r))
    r.T6_REPEAT_PERIOD_SEC = CoreRecorderV2.T6_REPEAT_PERIOD_SEC
    r._t6_say_once_at = {}
    # The namer names the second the given edge belongs to -- one fold
    # block later is K seconds later.  None models a cascade that cannot
    # place the edge within +-0.4 s of any integer.
    r._t6_name_integer_second = (
        (lambda _rtp: None) if named is None else
        (lambda rtp: named + int(round((rtp - EDGE_RTP) / SR)))
    )
    r._t6_battery = battery or T6Battery(sample_rate=SR, fold_seconds=K)
    r._t6_last_verdict = None
    r._t6_disambiguation_ns = 0
    r._t6_native_anchor = None
    r._t6_rate_reset = lambda _why: None
    r._t6_channel_info = SimpleNamespace(chain_delay_correction_ns=None)
    r._t6_calibrator = SimpleNamespace(sample_rate=SR,
                                       _last_edge_rtp=MF_EDGE_RTP)
    r._t6_last_fine_est = None
    r._t6_chain_delay_history = [16_618_000, 16_618_100, 16_617_900]
    return r


def _wall_of(rtp, wall_error_sec=0.0):
    """radiod's RTP->UTC mapping, as this fixture models it.

    Linear in RTP at 1/SR per sample, anchored so that EDGE_RTP maps to
    NAMED_SECOND + CHAIN_DELAY_SEC: the edge arrives one chain delay after
    the second it was named for, which is what a healthy station looks
    like.  `wall_error_sec` displaces the whole mapping, modelling radiod's
    own registration error -- the quantity the disambiguation corrects.
    """
    return (float(NAMED_SECOND) + (rtp - EDGE_RTP) / SR
            + CHAIN_DELAY_SEC + wall_error_sec)


def _drive(r, result=None, n=N_BLOCKS, wall_error_sec=0.0, **over):
    result = _result() if result is None else result

    def _fake_rtp_to_utc(rtp, _channel_info):
        return _wall_of(rtp, wall_error_sec)

    with mock.patch("ka9q.rtp_recorder.rtp_to_utc",
                    side_effect=_fake_rtp_to_utc):
        for i in range(n):
            r._t6_last_fine_est = _estimate(EDGE_RTP + i * SR * K, **over)
            r._t6_disambiguate_via_external_reference(result)


class TestAcquisitionUsesTheFold(unittest.TestCase):

    def test_a_passing_battery_captures_an_anchor(self):
        r = _recorder()
        _drive(r)
        self.assertIsNotNone(r._t6_native_anchor)
        self.assertTrue(r._t6_last_verdict.passed,
                        r._t6_last_verdict.failures)

    def test_a_failing_battery_captures_nothing(self):
        r = _recorder()
        _drive(r, fold_retention=0.006)
        self.assertIsNone(r._t6_native_anchor)
        self.assertIn("retention", r._t6_last_verdict.failures)

    def test_no_named_second_refuses_without_capturing(self):
        """_t6_name_integer_second returns None when neither NMEA nor the
        radiod-pair wall lands within 0.4 s of an integer."""
        r = _recorder(named=None)
        _drive(r)
        self.assertIsNone(r._t6_native_anchor)

    def test_the_plausibility_guard_still_refuses_a_gross_wrap(self):
        """radiod's mapping 450 ms from the named second.

        This is the half-second ambiguity the guard exists for: the namer
        holds the second, the mapping does not, and the implied chain
        delay comes out at 466.6 ms -- physically impossible for a cable
        and a channel filter.
        """
        r = _recorder()
        _drive(r, wall_error_sec=0.45)
        self.assertIsNone(r._t6_native_anchor)

    def test_the_anchor_is_built_on_the_named_second(self):
        r = _recorder()
        _drive(r)
        self.assertIsNotNone(r._t6_native_anchor)
        self.assertEqual(r._t6_native_anchor.captured_at_utc_ns,
                         LAST_NAMED * 1_000_000_000)

    def test_the_old_sigma_gate_is_no_longer_consulted(self):
        """_get_disambiguation_reference must not be called: its 10 us bar
        is the blocker this task removes."""
        r = _recorder()
        r._get_disambiguation_reference = mock.Mock(
            side_effect=AssertionError("old gate consulted"))
        _drive(r)
        r._get_disambiguation_reference.assert_not_called()

    def test_the_anchor_registers_the_folds_edge_not_the_matched_filters(self):
        """The fold locates the edge; the MF is only the trigger."""
        r = _recorder()
        _drive(r)
        self.assertEqual(r._t6_native_anchor.anchor_rtp,
                         LAST_EDGE_RTP & 0xFFFFFFFF)
        self.assertNotEqual(r._t6_native_anchor.anchor_rtp,
                            MF_EDGE_RTP & 0xFFFFFFFF)

    def test_the_fold_domain_position_is_never_read(self):
        """edge_offset_samples is indexed from the stage's last reset.

        Its origin is arbitrary and moves on every reset, so nothing the
        anchor carries may depend on it.  Drive a wildly wrong value and
        the anchor must not move by one nanosecond.
        """
        good = _recorder()
        _drive(good)
        wrong = _recorder()
        _drive(wrong, edge_offset_samples=77_777.0)
        self.assertEqual(wrong._t6_native_anchor, good._t6_native_anchor)


class TestTheDeviationArithmetic(unittest.TestCase):
    """The computation's core: a PPS fires ON the second it is named for."""

    def test_a_firing_instant_already_on_the_named_second_shifts_nothing(self):
        r = _recorder()
        _drive(r)
        self.assertEqual(r._t6_disambiguation_ns, 0)
        self.assertEqual(r._t6_native_anchor.chain_delay_ns, CHAIN_DELAY_NS)

    def test_a_firing_instant_three_samples_late_shifts_three_samples(self):
        r = _recorder()
        _drive(r, wall_error_sec=3.0 / SR)
        # +3 samples of deviation -> +3 samples of correction, ADDED to
        # the raw chain delay (the caller re-forms raw + disambiguation).
        self.assertEqual(r._t6_disambiguation_ns, round(3 * 1e9 / SR))
        self.assertEqual(r._t6_native_anchor.chain_delay_ns,
                         CHAIN_DELAY_NS + round(3 * 1e9 / SR))
        # And the property the sign exists to satisfy: once corrected,
        # the firing instant lands on the second it was named for.
        firing = (_wall_of(LAST_EDGE_RTP, 3.0 / SR)
                  - r._t6_native_anchor.chain_delay_ns / 1e9)
        self.assertAlmostEqual(firing, float(LAST_NAMED), places=7)

    def test_the_correction_is_signed_not_absolute(self):
        """Three samples EARLY must move the other way."""
        r = _recorder()
        _drive(r, wall_error_sec=-3.0 / SR)
        self.assertEqual(r._t6_disambiguation_ns, -round(3 * 1e9 / SR))
        firing = (_wall_of(LAST_EDGE_RTP, -3.0 / SR)
                  - r._t6_native_anchor.chain_delay_ns / 1e9)
        self.assertAlmostEqual(firing, float(LAST_NAMED), places=7)


class TestThePlausibilityGuardStandsAlone(unittest.TestCase):

    def test_the_guard_refuses_where_the_battery_would_not(self):
        """Isolate criterion 7's twin.

        The guard and BatteryThresholds.max_chain_delay_ns carry the same
        250 ms bound, so a gross wrap trips both and deleting the guard
        breaks no test.  Loosen the battery's copy to 1000 s and only the
        guard can refuse -- and because the guard returns BEFORE
        `evaluate` runs, a verdict of None proves it was the guard that
        did it.
        """
        loose = T6Battery(sample_rate=SR, fold_seconds=K,
                          thresholds=BatteryThresholds(
                              max_chain_delay_ns=1_000_000_000_000))
        r = _recorder(battery=loose)
        _drive(r, wall_error_sec=0.45)
        self.assertIsNone(r._t6_native_anchor)
        self.assertIsNone(r._t6_last_verdict,
                          "the battery ran; the guard should have returned "
                          "before it")

    def test_the_loosened_battery_really_would_have_passed(self):
        """Guards the test above against passing for the wrong reason."""
        loose = T6Battery(sample_rate=SR, fold_seconds=K,
                          thresholds=BatteryThresholds(
                              max_chain_delay_ns=1_000_000_000_000))
        est = _estimate(EDGE_RTP)
        for i in range(3):
            v = loose.evaluate(_estimate(EDGE_RTP + i * SR * K),
                               implied_chain_delay_ns=466_618_000,
                               reported_sigma_ms=0.0, cn0_db_hz=None)
        self.assertTrue(v.passed, v.failures)
        self.assertIsNotNone(est)


class TestTheBatteryForgetsARepudiatedRun(unittest.TestCase):

    def test_reset_makes_the_history_criteria_report_not_yet(self):
        r = _recorder()
        _drive(r)
        self.assertTrue(r._t6_last_verdict.passed)

        r._t6_battery_forget_run("test")
        self.assertIsNone(r._t6_last_verdict)

        _drive(r, n=1)
        v = r._t6_last_verdict
        self.assertEqual(v.criteria["blocks"], 1)
        self.assertIn("ruler", v.failures)
        self.assertIn("unimodality", v.failures)

    def test_without_reset_the_stale_run_would_judge_the_new_one(self):
        """The failure the reset prevents, stated as the counterfactual."""
        r = _recorder()
        _drive(r)
        # A new acquisition whose edge is NOT one fold block after the
        # old run's last block: the ruler sees an impossible gap.
        _drive(r, n=1)
        self.assertIn("ruler", r._t6_last_verdict.failures)

    def test_a_missing_battery_is_not_an_error(self):
        r = _recorder()
        r._t6_battery = None
        r._t6_battery_forget_run("test")   # must not raise

    def test_every_stage_repudiation_drops_the_battery_run(self):
        """Wiring check: the sites that repudiate the fine stage's own
        position must drop the battery's run with it, or the ruler will
        refuse the next lock over a gap it cannot explain."""
        src = (Path(__file__).resolve().parent.parent / "src" / "hf_timestd"
               / "core" / "core_recorder_v2.py").read_text()
        repudiations = src.count("self._t6_fine_stage.clear_own_offset()")
        forgets = src.count("self._t6_battery_forget_run(")
        self.assertGreater(repudiations, 0)
        self.assertEqual(forgets, repudiations, (
            f"{repudiations} sites repudiate the fine stage's position but "
            f"{forgets} drop the battery's block history"))


class TestReportedSigma(unittest.TestCase):

    def test_sigma_comes_from_the_chain_delay_history(self):
        r = _recorder()
        sigma = r._t6_reported_sigma_ms()
        self.assertIsNotNone(sigma)
        self.assertLess(sigma, 1.0)

    def test_too_few_samples_reports_none(self):
        r = _recorder()
        r._t6_chain_delay_history = [16_618_000]
        self.assertIsNone(r._t6_reported_sigma_ms())


if __name__ == "__main__":
    unittest.main()
