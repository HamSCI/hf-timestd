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


class TestTheActuator:
    """The floor must reach the field chrony actually weighs.

    Publishing `witness_dissent` to offset_judge.json is still only
    observation -- #29's whole point is that correct, ignored alarms are
    the failure mode.  The floor has to widen the SHM precision.
    """

    def test_precision_widens_when_the_floor_bites(self):
        from hf_timestd.core.t6_shm_pair import precision_from_sigma_ns
        d = from_shadow_residuals(B4_SHADOWS, B4_BENCH_SIGMA)
        honest = precision_from_sigma_ns(B4_BENCH_SIGMA)
        widened = precision_from_sigma_ns(d.sigma_floor_ns)
        # SHM precision is log2(seconds), so a wider sigma is a LARGER
        # (less negative) exponent -- chrony then weighs HPPS lower.
        assert widened > honest
        assert 2 ** widened > 2 ** honest * 20

    def test_a_healthy_bench_keeps_its_own_precision(self):
        ok = {
            "T4": {"shadow_residual_ns": -490_000.0, "sigma_ns": 650_134.0},
            "T3": {"shadow_residual_ns": -206_000.0, "sigma_ns": 3_195_463.2},
        }
        assert from_shadow_residuals(ok, 847_000.0) is None

    def test_the_recorder_accessor_is_none_without_a_judge(self):
        """getattr-guarded: test harnesses build via __new__, and a
        missing judge must not take the push path down."""
        from hf_timestd.core.core_recorder_v2 import CoreRecorderV2
        r = CoreRecorderV2.__new__(CoreRecorderV2)
        assert r._t6_dissent_sigma_floor_ns() is None

    def test_the_recorder_accessor_reads_a_live_verdict(self):
        from hf_timestd.core.core_recorder_v2 import CoreRecorderV2
        from types import SimpleNamespace
        r = CoreRecorderV2.__new__(CoreRecorderV2)
        d = from_shadow_residuals(B4_SHADOWS, B4_BENCH_SIGMA)
        r._offset_judge = SimpleNamespace(_dissent=d)
        assert r._t6_dissent_sigma_floor_ns() == pytest.approx(
            26_123_881.35, rel=1e-6)


class TestTheBenchsOwnClaimMustNotSetTheBound:
    """Found in the field on AC0G-B4 2026-08-26, not by these tests.

    Two fixes from 2026-08-25 defeated each other.  The provenance sigma
    gate (368a39c) correctly widened the bench to COARSE_ANCHOR_SIGMA_NS
    = 25 ms once it fell back to a T5-captured anchor.  The dissent bound
    was k*sqrt(witness^2 + bench^2), so that honest widening pushed the
    bound to ~125 ms — and a REAL 56.5 ms error, with all three witnesses
    agreeing to 250 us, could no longer convict:

        anchor_tier T5, sigma 25.0 ms
        shadows  T4 +56.522  T3 +56.776  T5 +56.557 ms
        witness_dissent: None          <- wrong

    Using the suspect's own claim to set the threshold for doubting it is
    circular, and it is the exact failure this module exists to catch,
    one level up.  The bound belongs to the WITNESSES: what limits the
    judgement is their resolution, not the bench's opinion of itself.
    """

    LIVE = {
        "T4": {"shadow_residual_ns": 56_522_000.0, "sigma_ns": 650_000.0},
        "T3": {"shadow_residual_ns": 56_776_000.0, "sigma_ns": 3_195_000.0},
        "T5": {"shadow_residual_ns": 56_557_000.0, "sigma_ns": 25_000_000.0},
    }

    def test_a_wide_bench_claim_no_longer_blocks_conviction(self):
        d = from_shadow_residuals(self.LIVE, 25_000_000.0)
        assert d is not None, "the 2026-08-26 field case must convict"
        assert set(d.tiers) == {"T4", "T3"}

    def test_the_verdict_is_independent_of_the_bench_claim(self):
        """Same evidence, any claim: the witnesses decide."""
        verdicts = [
            from_shadow_residuals(self.LIVE, s)
            for s in (1e5, 8.4e5, 25e6, 60e6)
        ]
        assert all(v is not None for v in verdicts)
        assert len({v.implied_error_ns for v in verdicts}) == 1

    def test_the_floor_still_exceeds_the_widened_claim(self):
        d = from_shadow_residuals(self.LIVE, 25_000_000.0)
        assert d.sigma_floor_ns > 25_000_000.0

    def test_the_2026_08_25_incident_still_convicts_identically(self):
        d = from_shadow_residuals(B4_SHADOWS, B4_BENCH_SIGMA)
        assert set(d.tiers) == {"T4", "T3"}       # T5 still abstains
        assert d.implied_error_ns == pytest.approx(-26_123_881.35, rel=1e-6)

    def test_a_healthy_bench_is_still_not_convicted(self):
        ok = {
            "T4": {"shadow_residual_ns": -173_000.0, "sigma_ns": 650_000.0},
            "T3": {"shadow_residual_ns": -384_000.0, "sigma_ns": 3_195_000.0},
        }
        assert from_shadow_residuals(ok, 1_252_000.0) is None
        assert from_shadow_residuals(ok, 25_000_000.0) is None
