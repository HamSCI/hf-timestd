"""hf-timestd#29 -- give the witnesses an actuator.

The station has repeatedly had every witness right and nothing happen:
~5,000 CRITICAL/day for four days (#28, #21), and on 2026-08-25 T6 held
authority for 3.4 h while 26 ms wrong with T4/T3/T5 all dissenting.  No
conflict was raised because `cross_bench_conflict` gates tier
ADVANCEMENT and T6 is already top tier.

Numbers below are the live offset_judge.json from that morning.
"""
from __future__ import annotations

import pytest

from hf_timestd.core.witness_dissent import (
    DissentWatch,
    Witness,
    evaluate,
    from_shadow_residuals,
)

# AC0G-B4 2026-08-25 11:51Z, verbatim
B4_SHADOWS = {
    "T4": {"shadow_residual_ns": -26_153_087.6, "sigma_ns": 650_134.0},
    "T3": {"shadow_residual_ns": -26_094_675.1, "sigma_ns": 3_195_463.2},
    "T5": {"shadow_residual_ns": -26_157_617.6, "sigma_ns": 25_000_000.0},
}
B4_BENCH_SIGMA = 836_509.0


class TestTheIncidentIsConvicted:
    def test_b4_2026_08_25_is_detected(self):
        d = from_shadow_residuals(B4_SHADOWS, B4_BENCH_SIGMA)
        assert d is not None
        # median of the two witnesses tight enough to convict
        assert d.implied_error_ns == pytest.approx(-26_123_881.35, rel=1e-6)

    def test_the_sigma_floor_replaces_a_false_error_bar(self):
        """The bench published 0.837 ms while being 26 ms wrong."""
        d = from_shadow_residuals(B4_SHADOWS, B4_BENCH_SIGMA)
        assert d.sigma_floor_ns == pytest.approx(26_123_881.35, rel=1e-6)
        assert d.sigma_floor_ns > 30 * B4_BENCH_SIGMA

    def test_the_witnesses_agreed_with_each_other(self):
        """That mutual agreement is what convicts the BENCH rather than
        any one witness -- T4 and T3 sat within 58 us of each other."""
        d = from_shadow_residuals(B4_SHADOWS, B4_BENCH_SIGMA)
        assert d.spread_ns == pytest.approx(58_412.5, abs=100)
        assert set(d.tiers) == {"T4", "T3"}

    def test_t5_abstains_because_its_own_sigma_is_too_wide(self):
        """A witness cannot convict beyond its own resolution.  T5's
        sigma is 25 ms -- comparable to the 26 ms error it would be
        reporting -- so it says nothing here, correctly.  This is #29's
        asymmetry in reverse: a witness must be tighter than the
        excursion to bound it, and T5 is not.

        Quorum is still met by T4 (0.65 ms) and T3 (3.2 ms), which is
        why the verdict stands without it."""
        d = from_shadow_residuals(B4_SHADOWS, B4_BENCH_SIGMA)
        assert "T5" not in d.tiers
        t5 = B4_SHADOWS["T5"]
        bound = 5.0 * ((t5["sigma_ns"] ** 2 + B4_BENCH_SIGMA ** 2) ** 0.5)
        assert abs(t5["shadow_residual_ns"]) < bound


class TestItDoesNotConvictOnWeakEvidence:
    def test_one_witness_cannot_convict(self):
        """The lone dissenter may itself be the broken one."""
        assert evaluate(
            [Witness("T4", -26_000_000.0, 650_000.0)], B4_BENCH_SIGMA
        ) is None

    def test_witnesses_that_disagree_with_each_other_prove_nothing(self):
        """Large but scattered residuals are noise, not a verdict."""
        ws = [Witness("T4", -26_000_000.0, 650_000.0),
              Witness("T3", +31_000_000.0, 650_000.0)]
        assert evaluate(ws, B4_BENCH_SIGMA) is None

    def test_a_healthy_bench_is_not_convicted(self):
        """The same morning after recovery: sub-ms residuals."""
        ok = {
            "T4": {"shadow_residual_ns": -490_000.0, "sigma_ns": 650_134.0},
            "T3": {"shadow_residual_ns": -206_000.0, "sigma_ns": 3_195_463.2},
            "T5": {"shadow_residual_ns": -442_000.0, "sigma_ns": 25_000_000.0},
        }
        assert from_shadow_residuals(ok, 847_000.0) is None

    def test_malformed_rows_are_skipped_not_fatal(self):
        bad = dict(B4_SHADOWS)
        bad["T9"] = {"sigma_ns": None}
        assert from_shadow_residuals(bad, B4_BENCH_SIGMA) is not None
        assert from_shadow_residuals({}, B4_BENCH_SIGMA) is None

    def test_a_wide_witness_needs_a_bigger_excursion(self):
        """#29's asymmetry: a +-10 ms witness still catches 70 ms, but
        must not fire on 2 ms."""
        near = [Witness("A", 2_000_000.0, 10_000_000.0),
                Witness("B", 2_000_000.0, 10_000_000.0)]
        assert evaluate(near, 800_000.0) is None
        far = [Witness("A", 70_000_000.0, 10_000_000.0),
               Witness("B", 70_000_000.0, 10_000_000.0)]
        assert evaluate(far, 800_000.0) is not None


class TestDwell:
    def _d(self):
        return from_shadow_residuals(B4_SHADOWS, B4_BENCH_SIGMA)

    def test_sustained_dissent_acts(self):
        w = DissentWatch(dwell_s=120.0)
        assert w.observe(self._d(), 0.0) is None
        assert w.observe(self._d(), 119.0) is None
        assert w.observe(self._d(), 120.0) is not None

    def test_a_transient_does_not_act(self):
        w = DissentWatch(dwell_s=120.0)
        w.observe(self._d(), 0.0)
        w.observe(self._d(), 60.0)
        assert w.observe(None, 61.0) is None
        assert w.observe(self._d(), 62.0) is None       # dwell restarts
        assert w.observe(self._d(), 181.0) is None
        assert w.observe(self._d(), 182.0) is not None

    def test_it_keeps_asserting_while_the_fault_stands(self):
        """3.4 hours of it, on the day this was written."""
        w = DissentWatch(dwell_s=120.0)
        w.observe(self._d(), 0.0)
        for t in range(120, 3600, 30):
            assert w.observe(self._d(), float(t)) is not None
