"""Acquisition runs on the fold's edge; the namer only says which second.

⛔ TWO fixture defects have hidden real bugs in this file.  Read before
changing a number.

1. An early draft described two edges at once -- `edge_rtp` implied an RTP
   phase of 87916 samples while `edge_offset_samples` said 47916 -- so no
   correct implementation could satisfy it, and a wrong one did.
2. The next draft drove `_t6_disambiguate_via_external_reference` once per
   fold block, advancing the estimate each time.  The recorder never does
   that: the acquisition path runs only while `_t6_last_chain_delay_ns is
   None`, i.e. ONCE per first-lock, or repeatedly against the SAME
   estimate.  The manufactured cadence let a battery evaluated inside
   acquisition appear to accumulate a run of blocks it could never see in
   production, hiding a defect that made an anchor impossible.

So: `_feed_blocks` drives `_t6_evaluate_battery` at the fold cadence, which
is where blocks really arrive, and `_acquire` calls the acquisition path
ONCE, which is how often the recorder calls it.  Every constant below
describes ONE edge and says how it was derived.
"""
from __future__ import annotations

import math
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

# Three blocks is HISTORY_REQUIRED_BLOCKS: what the ruler and unimodality
# criteria need before they will report at all.
BLOCKS = 3
# _feed_blocks advances one fold block per call, so the last block's edge
# belongs to this second.
LAST_NAMED = NAMED_SECOND + (BLOCKS - 1) * K
LAST_EDGE_RTP = EDGE_RTP + (BLOCKS - 1) * SR * K


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
    """A bare namespace carrying only what the two methods touch."""
    r = SimpleNamespace()
    for name in ("_t6_disambiguate_via_external_reference",
                 "_t6_evaluate_battery", "_t6_say_once",
                 "_t6_reported_sigma_ms", "_t6_battery_forget_run"):
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
    r._t6_last_chain_delay_ns = None
    r._t6_native_anchor = None
    r._t6_rate_reset = lambda _why: None
    r._t6_channel_info = SimpleNamespace(chain_delay_correction_ns=None)
    r._t6_calibrator = SimpleNamespace(sample_rate=SR,
                                       _last_edge_rtp=MF_EDGE_RTP)
    r._t6_last_fine_est = None
    # ⛔ EMPTY, as production holds it before acquisition.  This list was
    # primed with three readings, and that fixture hid the circular
    # dependency in criterion 5 outright: the deque is appended to only
    # inside a branch requiring `_t6_native_anchor is not None`, so at
    # this instant the real recorder has NOTHING in it.  Priming it
    # supplied a sigma production could not have had, and the battery
    # passed here while failing on every station.
    r._t6_chain_delay_history = []
    # Fixture bookkeeping: which fold block comes next.  Successive
    # _feed_blocks calls CONTINUE the run, as the fine stage does --
    # restarting the edge sequence would hand the ruler a gap of zero
    # and fail a battery that production would have passed.
    r._fixture_next_block = 0
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


_KEEP = object()   # "caller said nothing", as distinct from "caller said None"


def _feed_blocks(r, n=BLOCKS, result=_KEEP, **over):
    """Fold blocks arriving at the fine stage's cadence.

    This is what the recorder does at `_t6_on_samples`: one estimate per
    completed fold block, one battery evaluation each, edges exactly one
    fold apart.  Leaves `_t6_last_fine_est` on the last block, as the
    recorder does.  `result=None` models the matched filter unlocked --
    `_maybe_result` returns None unless it is locked WITH an estimate.
    """
    result = _result() if result is _KEEP else result
    for _ in range(n):
        i = r._fixture_next_block
        r._fixture_next_block = i + 1
        est = _estimate(EDGE_RTP + i * SR * K, **over)
        r._t6_last_fine_est = est
        r._t6_evaluate_battery(est, result)


def _acquire(r, result=None, wall_error_sec=0.0, times=1):
    """The acquisition path, called as often as the recorder calls it.

    Once per first-lock.  `times` exists only for the tests that prove
    repetition changes nothing.
    """
    result = _result() if result is None else result

    def _fake_rtp_to_utc(rtp, _channel_info):
        return _wall_of(rtp, wall_error_sec)

    with mock.patch("ka9q.rtp_recorder.rtp_to_utc",
                    side_effect=_fake_rtp_to_utc):
        for _ in range(times):
            r._t6_disambiguate_via_external_reference(result)


class TestAcquisitionUsesTheFold(unittest.TestCase):

    def test_a_passing_battery_captures_an_anchor(self):
        r = _recorder()
        _feed_blocks(r)
        _acquire(r)
        self.assertIsNotNone(r._t6_native_anchor)
        self.assertTrue(r._t6_last_verdict.passed,
                        r._t6_last_verdict.failures)

    def test_a_failing_battery_captures_nothing(self):
        r = _recorder()
        _feed_blocks(r, fold_retention=0.006)
        _acquire(r)
        self.assertIsNone(r._t6_native_anchor)
        self.assertIn("retention", r._t6_last_verdict.failures)

    def test_no_named_second_refuses_without_capturing(self):
        """_t6_name_integer_second returns None when neither NMEA nor the
        radiod-pair wall lands within 0.4 s of an integer."""
        r = _recorder(named=None)
        _feed_blocks(r)
        _acquire(r)
        self.assertIsNone(r._t6_native_anchor)

    def test_the_plausibility_guard_still_refuses_a_gross_wrap(self):
        """radiod's mapping 450 ms from the named second.

        This is the half-second ambiguity the guard exists for: the namer
        holds the second, the mapping does not, and the implied chain
        delay comes out at 466.6 ms -- physically impossible for a cable
        and a channel filter.
        """
        r = _recorder()
        _feed_blocks(r)
        _acquire(r, wall_error_sec=0.45)
        self.assertIsNone(r._t6_native_anchor)

    def test_the_anchor_is_built_on_the_named_second(self):
        r = _recorder()
        _feed_blocks(r)
        _acquire(r)
        self.assertEqual(r._t6_native_anchor.captured_at_utc_ns,
                         LAST_NAMED * 1_000_000_000)

    def test_the_old_sigma_gate_is_no_longer_consulted(self):
        """_get_disambiguation_reference must not be called: its 10 us bar
        is the blocker this task removes."""
        r = _recorder()
        r._get_disambiguation_reference = mock.Mock(
            side_effect=AssertionError("old gate consulted"))
        _feed_blocks(r)
        _acquire(r)
        r._get_disambiguation_reference.assert_not_called()

    def test_the_anchor_registers_the_folds_edge_not_the_matched_filters(self):
        """The fold locates the edge; the MF is only the trigger."""
        r = _recorder()
        _feed_blocks(r)
        _acquire(r)
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
        _feed_blocks(good)
        _acquire(good)
        wrong = _recorder()
        _feed_blocks(wrong, edge_offset_samples=77_777.0)
        _acquire(wrong)
        self.assertEqual(wrong._t6_native_anchor, good._t6_native_anchor)


class TestTheBatteryIsJudgedWhereBlocksArrive(unittest.TestCase):
    """The defect this class exists for.

    `T6Battery.evaluate` appends one history entry per CALL, and criteria
    2 and 3 need three entries one fold apart.  Acquisition runs once per
    first-lock, so a battery evaluated there sees one block -- or N copies
    of one block -- and can never pass.  The evaluation belongs where fold
    blocks land.
    """

    def test_acquisition_never_evaluates_the_battery(self):
        """⚠ COUNT the calls, do not raise from a stub.

        `_t6_disambiguate_via_external_reference` wraps its whole body in
        `except Exception` and logs, so an AssertionError raised from a
        mock inside it is swallowed and the test passes regardless.
        """
        r = _recorder()
        _feed_blocks(r)
        before = list(r._t6_battery._edges)
        calls = []
        real = r._t6_battery.evaluate

        def _counting(*a, **kw):
            calls.append(1)
            return real(*a, **kw)

        r._t6_battery.evaluate = _counting
        _acquire(r, times=6)
        self.assertEqual(calls, [], "acquisition evaluated the battery")
        self.assertEqual(list(r._t6_battery._edges), before)

    def test_the_evaluation_is_wired_to_the_fold_block_site(self):
        """Structural guard on the split, since the fixture drives the
        two methods directly and would not notice them being re-joined."""
        src = (Path(__file__).resolve().parent.parent / "src" / "hf_timestd"
               / "core" / "core_recorder_v2.py").read_text()
        self.assertIn("self._t6_evaluate_battery(fine, result)", src,
                      "the fold-block site no longer evaluates the battery")
        acquisition = src[
            src.index("def _t6_disambiguate_via_external_reference"):
            src.index("def _wait_for_chrony_settled")]
        self.assertNotIn("self._t6_evaluate_battery(", acquisition, (
            "the acquisition path evaluates the battery again; it runs "
            "once per first-lock and cannot assemble a run of blocks"))

    def test_acquisition_with_no_blocks_yet_holds_without_capturing(self):
        r = _recorder()
        r._t6_last_fine_est = _estimate(EDGE_RTP)
        _acquire(r)
        self.assertIsNone(r._t6_native_anchor)
        self.assertIsNone(r._t6_last_verdict)

    def test_repeating_the_call_does_not_manufacture_a_run(self):
        """Six calls against one estimate -- the other cadence the
        recorder can produce -- must not add up to a passing battery."""
        r = _recorder()
        r._t6_last_fine_est = _estimate(EDGE_RTP)
        _acquire(r, times=6)
        self.assertIsNone(r._t6_native_anchor)
        self.assertEqual(len(r._t6_battery._edges), 0)

    def test_the_hold_resolves_itself_as_blocks_accumulate(self):
        r = _recorder()
        r._t6_last_fine_est = _estimate(EDGE_RTP)
        _acquire(r)
        self.assertIsNone(r._t6_native_anchor)

        _feed_blocks(r, n=1)
        _acquire(r)
        self.assertIsNone(r._t6_native_anchor)
        self.assertIn("ruler", r._t6_last_verdict.failures)

        _feed_blocks(r, n=BLOCKS)
        _acquire(r)
        self.assertIsNotNone(r._t6_native_anchor)

    def test_the_holding_message_is_throttled(self):
        """A station that never acquires must not log per cycle."""
        r = _recorder()
        r._t6_last_fine_est = _estimate(EDGE_RTP)
        _acquire(r, times=200)
        self.assertIn("battery_no_verdict", r._t6_say_once_at)
        self.assertFalse(r._t6_say_once("battery_no_verdict"),
                         "the holding message is not throttled")


class TestARefusedEdgeDoesNotMoveTheClock(unittest.TestCase):
    """`_t6_disambiguation_ns` is recorder state the CALLER folds into
    `effective_chain_delay` and latches into `_t6_last_chain_delay_ns`,
    whether or not an anchor was captured.  So a refusal that writes it
    lets the battery reject an edge and the edge move the clock anyway."""

    PRIOR = 12_345

    def test_a_battery_refusal_leaves_the_shift_untouched(self):
        r = _recorder()
        r._t6_disambiguation_ns = self.PRIOR
        # Retention 0.006: the missing-reference-cable fault measured at
        # AI6VN, and the exact failure criterion 1 exists to catch.
        _feed_blocks(r, fold_retention=0.006)
        _acquire(r, wall_error_sec=0.030)
        self.assertIsNone(r._t6_native_anchor)
        self.assertEqual(r._t6_disambiguation_ns, self.PRIOR)

    def test_a_plausibility_refusal_leaves_the_shift_untouched(self):
        r = _recorder()
        r._t6_disambiguation_ns = self.PRIOR
        _feed_blocks(r)
        _acquire(r, wall_error_sec=0.45)
        self.assertIsNone(r._t6_native_anchor)
        self.assertEqual(r._t6_disambiguation_ns, self.PRIOR)

    def test_an_unnamed_second_leaves_the_shift_untouched(self):
        r = _recorder(named=None)
        r._t6_disambiguation_ns = self.PRIOR
        _feed_blocks(r)
        _acquire(r, wall_error_sec=0.030)
        self.assertEqual(r._t6_disambiguation_ns, self.PRIOR)

    def test_a_captured_anchor_does_commit_the_shift(self):
        """The positive control: refusal must be the only thing that
        withholds the write."""
        r = _recorder()
        r._t6_disambiguation_ns = self.PRIOR
        _feed_blocks(r)
        _acquire(r, wall_error_sec=3.0 / SR)
        self.assertIsNotNone(r._t6_native_anchor)
        self.assertEqual(r._t6_disambiguation_ns, round(3 * 1e9 / SR))


class TestTheDeviationArithmetic(unittest.TestCase):
    """The computation's core: a PPS fires ON the second it is named for."""

    def test_a_firing_instant_already_on_the_named_second_shifts_nothing(self):
        r = _recorder()
        _feed_blocks(r)
        _acquire(r)
        self.assertEqual(r._t6_disambiguation_ns, 0)
        self.assertEqual(r._t6_native_anchor.chain_delay_ns, CHAIN_DELAY_NS)

    def test_a_firing_instant_three_samples_late_shifts_three_samples(self):
        r = _recorder()
        _feed_blocks(r)
        _acquire(r, wall_error_sec=3.0 / SR)
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
        _feed_blocks(r)
        _acquire(r, wall_error_sec=-3.0 / SR)
        self.assertEqual(r._t6_disambiguation_ns, -round(3 * 1e9 / SR))
        firing = (_wall_of(LAST_EDGE_RTP, -3.0 / SR)
                  - r._t6_native_anchor.chain_delay_ns / 1e9)
        self.assertAlmostEqual(firing, float(LAST_NAMED), places=7)

    def test_the_subsample_fraction_is_carried_not_discarded(self):
        """edge_rtp is the rounded integer; edge_subsample is what the
        rounding threw away.  2.4 samples of mapping error plus 0.4 of
        sub-sample is 2.8 -- three samples.  Discard the fraction and it
        rounds to two."""
        r = _recorder()
        _feed_blocks(r, edge_subsample=0.4)
        _acquire(r, wall_error_sec=2.4 / SR)
        self.assertEqual(r._t6_disambiguation_ns, round(3 * 1e9 / SR))


class TestThePlausibilityGuardStandsAlone(unittest.TestCase):

    def _loose(self):
        """A battery whose criterion 7 cannot fire, so only the ±250 ms
        guard can refuse a gross wrap."""
        return T6Battery(sample_rate=SR, fold_seconds=K,
                         thresholds=BatteryThresholds(
                             max_chain_delay_ns=1_000_000_000_000))

    def test_the_guard_refuses_where_the_battery_would_not(self):
        """Isolate criterion 7's twin.

        The guard and BatteryThresholds.max_chain_delay_ns carry the same
        250 ms bound, so a gross wrap trips both and deleting the guard
        breaks no test.  Loosen the battery's copy to 1000 s and only the
        guard can refuse -- and the standing verdict, asserted PASSED
        below, shows the battery would have let it through.
        """
        r = _recorder(battery=self._loose())
        _feed_blocks(r)
        _acquire(r, wall_error_sec=0.45)
        self.assertTrue(r._t6_last_verdict.passed,
                        r._t6_last_verdict.failures)
        self.assertIsNone(r._t6_native_anchor)


class TestSigmaEvidence(unittest.TestCase):
    """⛔ Evidence the station could not compute must never read as
    evidence that it is healthy.  `or 0.0` made criterion 5 inert."""

    def test_an_unavailable_sigma_fails_criterion_5(self):
        """Both routes empty.  Since the 2026-09-19 circularity fix the
        recorder prefers the BATTERY's own retained block positions, so
        starving only the chain-delay deque no longer starves criterion
        5 -- one block starves both, because the battery needs two
        positions to state a spread."""
        r = _recorder()
        r._t6_chain_delay_history = [16_618_000]      # < 2 samples
        _feed_blocks(r, n=1)                          # < 2 positions
        self.assertIn("sigma", r._t6_last_verdict.failures)
        _acquire(r)
        self.assertIsNone(r._t6_native_anchor)

    def test_sigma_comes_from_the_fold_when_no_anchor_exists(self):
        """⛔ THE CIRCULAR DEPENDENCY, pinned.

        `_t6_chain_delay_history` is appended to only inside a branch
        requiring `_t6_native_anchor is not None`.  Reading sigma from it
        alone meant the battery needed an anchor and the anchor needed
        the battery: three healthy blocks gave
        `failures=('sigma',), sigma=nan` and no anchor, forever.  A
        station with no chrony SHM configured never escaped it at all.

        So: EMPTY deque, no anchor, three healthy blocks -- and criterion
        5 must still be evidenced, from the fold's own block-to-block
        scatter.
        """
        r = _recorder()
        r._t6_chain_delay_history = []                # as production holds it
        self.assertIsNone(r._t6_native_anchor)
        _feed_blocks(r)
        self.assertNotIn("sigma", r._t6_last_verdict.failures)
        self.assertTrue(math.isfinite(r._t6_last_verdict.sigma_ms))
        self.assertTrue(r._t6_last_verdict.passed,
                        r._t6_last_verdict.failures)
        _acquire(r)
        self.assertIsNotNone(r._t6_native_anchor)

    def test_a_legitimate_zero_sigma_is_not_swallowed(self):
        """`or 0.0` also could not tell 'no reading' from a real 0.0."""
        r = _recorder()
        r._t6_chain_delay_history = [16_618_000] * 5   # std exactly 0.0
        _feed_blocks(r)                 # every block on ONE position: also 0.0
        self.assertEqual(r._t6_last_verdict.sigma_ms, 0.0)
        self.assertNotIn("sigma", r._t6_last_verdict.failures)

    def test_a_broken_battery_holds_acquisition_without_raising(self):
        """The battery is advisory and sits inside the fine-stage block,
        whose failures must not reach the authority call after it.  So a
        broken battery drops its verdict (acquisition holds) and raises
        nothing."""
        r = _recorder()
        _feed_blocks(r)
        self.assertTrue(r._t6_last_verdict.passed)

        def _boom(*_a, **_k):
            raise RuntimeError("battery exploded")

        r._t6_battery = SimpleNamespace(evaluate=_boom)
        _feed_blocks(r, n=1)                      # must not raise
        self.assertIsNone(r._t6_last_verdict)
        _acquire(r)
        self.assertIsNone(r._t6_native_anchor)

    def test_no_chain_delay_measurement_fails_plausibility(self):
        """result is None unless the MF is locked WITH an estimate."""
        r = _recorder()
        _feed_blocks(r, result=None)
        self.assertIn("plausibility", r._t6_last_verdict.failures)


class TestTheBatteryForgetsARepudiatedRun(unittest.TestCase):

    def test_reset_makes_the_history_criteria_report_not_yet(self):
        r = _recorder()
        _feed_blocks(r)
        self.assertTrue(r._t6_last_verdict.passed)

        r._t6_battery_forget_run("test")
        self.assertIsNone(r._t6_last_verdict)

        _feed_blocks(r, n=1)
        v = r._t6_last_verdict
        self.assertEqual(v.criteria["blocks"], 1)
        self.assertIn("ruler", v.failures)
        self.assertIn("unimodality", v.failures)

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

    def test_the_fold_is_preferred_over_the_chain_delay_history(self):
        """Order of preference, pinned.  The fold's own scatter needs no
        anchor; the chain-delay history cannot exist without one."""
        r = _recorder()
        # Two blocks in, so the battery retains two positions 40 samples
        # apart -- 0.4167 ms at 96 kHz -- while the deque carries a much
        # wider 0.1 ms-scale spread.  Whichever number comes back names
        # the route.
        _feed_blocks(r, n=1)
        _feed_blocks(r, n=1, edge_offset_samples=EDGE_OFFSET_SAMPLES + 40.0)
        r._t6_chain_delay_history = [0, 100_000_000]   # would be 50 ms
        sigma = r._t6_reported_sigma_ms()
        self.assertIsNotNone(sigma)
        self.assertAlmostEqual(sigma, 40.0 / SR * 1000.0 / math.sqrt(2),
                               places=6)

    def test_the_chain_delay_history_still_answers_when_the_fold_cannot(self):
        r = _recorder()
        r._t6_chain_delay_history = [16_618_000, 16_618_100, 16_617_900]
        sigma = r._t6_reported_sigma_ms()
        self.assertIsNotNone(sigma)
        self.assertLess(sigma, 1.0)

    def test_too_few_samples_on_both_routes_reports_none(self):
        r = _recorder()
        r._t6_chain_delay_history = [16_618_000]
        self.assertIsNone(r._t6_reported_sigma_ms())


if __name__ == "__main__":
    unittest.main()
