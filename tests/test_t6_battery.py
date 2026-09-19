"""Seven criteria, each with a named failure it catches."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hf_timestd.core.bpsk_edge_fine_stage import FineEdgeEstimate
from hf_timestd.core.t6_battery import T6Battery, BatteryThresholds

SR = 96000
K = 30


def _healthy(edge_rtp: int = 1_000_000 + 47916, **over) -> FineEdgeEstimate:
    kw = dict(
        edge_offset_samples=47916.0,
        edge_rtp=edge_rtp,
        edge_subsample=0.0,
        n_seconds_folded=K,
        plateau_amplitude=1.0,
        fit_rms=0.02,
        fold_retention=0.98,
        split_half_delta_samples=0.05,
        # A triangle-fidelity RESIDUAL: lower is better.  Real edges
        # measured 0.0008-0.0017 at 48.4 dB-Hz; pure noise 0.25-0.42.
        peak_prominence=0.001,
        apex_distance_samples=0.5,
        transition_width_samples=6.0,
    )
    kw.update(over)
    return FineEdgeEstimate(**kw)


def _battery() -> T6Battery:
    return T6Battery(sample_rate=SR, fold_seconds=K)


def _run(bat, ests, *, chain_ns=16_618_000, sigma_ms=0.001, cn0=70.0):
    """Feed estimates in order; return the final verdict."""
    v = None
    for e in ests:
        v = bat.evaluate(e, implied_chain_delay_ns=chain_ns,
                         reported_sigma_ms=sigma_ms, cn0_db_hz=cn0)
    return v


def _series(n: int, *, step: int = SR * K, **over):
    """n consecutive healthy blocks one fold apart."""
    base = 1_000_000 + 47916
    return [_healthy(edge_rtp=base + i * step, **over) for i in range(n)]


class TestHealthyPasses(unittest.TestCase):

    def test_a_clean_run_passes_every_criterion(self):
        v = _run(_battery(), _series(6))
        self.assertTrue(v.passed, v.failures)
        self.assertEqual(v.failures, ())


class TestEachCriterionCatchesItsFailure(unittest.TestCase):
    """Remove any one of these and its row below stops failing.  That is
    the test of the test -- see the mutation discipline in the spec."""

    def test_retention_catches_an_incoherent_chain(self):
        v = _run(_battery(), _series(6, fold_retention=0.006))
        self.assertFalse(v.passed)
        self.assertIn("retention", v.failures)

    def test_ruler_catches_a_pulse_walking_against_the_adc(self):
        # Blocks a full 5000 samples short of one fold apart.
        v = _run(_battery(), _series(6, step=SR * K - 5000))
        self.assertFalse(v.passed)
        self.assertIn("ruler", v.failures)

    def test_unimodality_catches_a_position_that_will_not_settle(self):
        # Vary the FOLD POSITION, not edge_rtp -- unimodality reads
        # edge_offset_samples, and moving edge_rtp would trip the ruler
        # instead, which is a different criterion's job.  400 samples is
        # 4.17 ms against a 1.0 ms tolerance.
        bat = _battery()
        base = 1_000_000 + 47916
        ests = [_healthy(edge_rtp=base + i * SR * K,
                         edge_offset_samples=47916.0 + (0.0 if i % 2 else 400.0))
                for i in range(6)]
        v = _run(bat, ests)
        self.assertFalse(v.passed)
        self.assertIn("unimodality", v.failures)
        self.assertNotIn("ruler", v.failures)

    def test_shape_catches_a_fold_holding_no_clean_flip(self):
        """Pure noise measured 0.25-0.42 on the fidelity residual; a
        real edge at B4's worst hour measured 0.0008-0.0017."""
        v = _run(_battery(), _series(6, peak_prominence=0.35))
        self.assertFalse(v.passed)
        self.assertIn("shape", v.failures)

    def test_shape_catches_a_nan_fidelity(self):
        v = _run(_battery(), _series(6, peak_prominence=float("nan")))
        self.assertFalse(v.passed)
        self.assertIn("shape", v.failures)

    def test_shape_also_catches_an_absent_transition(self):
        v = _run(_battery(), _series(6, transition_width_samples=900.0))
        self.assertFalse(v.passed)
        self.assertIn("shape", v.failures)

    def test_apex_displacement_fails_in_seeded_mode(self):
        """B4 locked onto a 20.000 ms lattice on 2026-09-04 and held it.
        A phantom repeats perfectly, so every repeatability criterion
        passes it; the apex distance is what catches it, measured at
        -1919 samples against 0.35-0.87 for an undisplaced edge."""
        bat = _battery()
        v = None
        for e in _series(6, apex_distance_samples=-1919.0):
            v = bat.evaluate(e, implied_chain_delay_ns=16_618_000,
                             reported_sigma_ms=0.001, cn0_db_hz=70.0,
                             search_mode="seeded")
        self.assertFalse(v.passed)
        self.assertIn("shape", v.failures)

    def test_apex_displacement_is_not_judged_in_bootstrap_mode(self):
        """In bootstrap the search centre and the apex derive from the
        same fold, so agreement proves nothing and must not be scored."""
        v = _run(_battery(), _series(6, apex_distance_samples=-1919.0))
        self.assertNotIn("shape", v.failures)

    def test_sigma_catches_a_broken_tier_claiming_to_be_a_wide_one(self):
        """AI6VN reported 477 ms on 2026-09-18, which widened the judge's
        cross-bench bound to ~2.4 s and let 284 ms through."""
        v = _run(_battery(), _series(6), sigma_ms=477.0)
        self.assertFalse(v.passed)
        self.assertIn("sigma", v.failures)

    def test_split_half_catches_a_wandering_apex(self):
        v = _run(_battery(), _series(6, split_half_delta_samples=120.0))
        self.assertFalse(v.passed)
        self.assertIn("split_half", v.failures)

    def test_split_half_fails_on_nan_rather_than_reading_it_as_agreement(self):
        """NaN means a sub-fold held no samples.  No evidence must never
        read as perfect agreement -- spec §4.6 and §5.2."""
        v = _run(_battery(), _series(6,
                                     split_half_delta_samples=float("nan")))
        self.assertFalse(v.passed)
        self.assertIn("split_half", v.failures)

    def test_plausibility_catches_a_gross_wrap(self):
        v = _run(_battery(), _series(6), chain_ns=400_000_000)
        self.assertFalse(v.passed)
        self.assertIn("plausibility", v.failures)


class TestEvidenceDiscipline(unittest.TestCase):

    def test_a_single_block_cannot_pass_the_multi_block_criteria(self):
        """Ruler, unimodality and split-half need history.  One block
        must report 'not yet', never 'fine'."""
        v = _run(_battery(), _series(1))
        self.assertFalse(v.passed)

    def test_reset_discards_history(self):
        bat = _battery()
        _run(bat, _series(6))
        bat.reset()
        v = _run(bat, _series(1))
        self.assertFalse(v.passed)

    def test_the_verdict_reports_every_criterion_measured(self):
        v = _run(_battery(), _series(6))
        for name in ("retention", "ruler", "unimodality", "shape",
                     "sigma", "split_half", "plausibility"):
            self.assertIn(name, v.criteria)

    def test_thresholds_are_injectable_for_tests_only(self):
        loose = BatteryThresholds(min_fold_retention=0.001)
        bat = T6Battery(sample_rate=SR, fold_seconds=K, thresholds=loose)
        v = _run(bat, _series(6, fold_retention=0.006))
        self.assertNotIn("retention", v.failures)


class TestNaNNeverReadsAsHealthy(unittest.TestCase):
    """A NaN in any evidence field is 'the fold could not compute this',
    never 'the fold is fine'.  In bare Python, `nan < x` and `nan > x`
    are both False, so a threshold comparison silently PASSES a NaN
    unless it is checked for explicitly.  Each fixture here holds
    exactly one NaN field with everything else healthy (six blocks, so
    ruler and unimodality are already satisfied) -- so, unlike an
    'everything NaN' fixture, only the field under test can be
    responsible for the failure."""

    def test_retention_nan_fails_rather_than_reading_as_healthy(self):
        v = _run(_battery(), _series(6, fold_retention=float("nan")))
        self.assertFalse(v.passed)
        self.assertIn("retention", v.failures)

    def test_shape_transition_width_nan_fails_rather_than_reading_as_healthy(self):
        v = _run(_battery(),
                 _series(6, transition_width_samples=float("nan")))
        self.assertFalse(v.passed)
        self.assertIn("shape", v.failures)

    def test_shape_apex_distance_nan_fails_in_seeded_mode(self):
        bat = _battery()
        v = None
        for e in _series(6, apex_distance_samples=float("nan")):
            v = bat.evaluate(e, implied_chain_delay_ns=16_618_000,
                             reported_sigma_ms=0.001, cn0_db_hz=70.0,
                             search_mode="seeded")
        self.assertFalse(v.passed)
        self.assertIn("shape", v.failures)

    def test_unimodality_nan_position_fails_rather_than_reading_as_healthy(self):
        # Only ONE block's fold position is NaN (index 2, not 0); edge_rtp
        # stays on the normal ladder throughout, so ruler cannot be what
        # fails.  The index matters: with a bare `max(diffs)` over a
        # single-NaN, otherwise-identical list, `x > current_max` is
        # always False once `current_max` is NaN, so a NaN landing FIRST
        # in iteration order happens to survive into the result by
        # accident, while one at index 1, 2, 4 or 5 is silently dropped
        # and `max()` returns 0.0 -- a fully healthy-looking spread.
        # Index 0 would pass even against the unfixed code for the wrong
        # reason; index 2 does not.
        bat = _battery()
        base = 1_000_000 + 47916
        ests = [
            _healthy(
                edge_rtp=base + i * SR * K,
                edge_offset_samples=(float("nan") if i == 2 else 47916.0),
            )
            for i in range(6)
        ]
        v = _run(bat, ests)
        self.assertFalse(v.passed)
        self.assertIn("unimodality", v.failures)

    def test_plausibility_chain_delay_nan_fails_without_raising(self):
        # implied_chain_delay_ns is documented as int, but a caller
        # could still hand back NaN; `int(nan)` raises ValueError, which
        # must never escape `evaluate` -- see the fix in criterion 7.
        v = _run(_battery(), _series(6), chain_ns=float("nan"))
        self.assertFalse(v.passed)
        self.assertIn("plausibility", v.failures)

    def test_evaluate_raises_no_exception_when_every_evidence_field_is_nan(self):
        nan = float("nan")
        est = _healthy(
            fold_retention=nan,
            split_half_delta_samples=nan,
            peak_prominence=nan,
            apex_distance_samples=nan,
            transition_width_samples=nan,
        )
        bat = _battery()
        try:
            v = bat.evaluate(est, implied_chain_delay_ns=nan,
                             reported_sigma_ms=nan, cn0_db_hz=70.0,
                             search_mode="seeded")
        except Exception as exc:  # the thing under test is: no exception
            self.fail(f"evaluate() raised {exc!r} on all-NaN evidence "
                      f"instead of returning a failing verdict")
        self.assertFalse(v.passed)


if __name__ == "__main__":
    unittest.main()
