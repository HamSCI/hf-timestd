"""The position gate that catches what confidence cannot.

On AC0G-B4 over 2026-09-22 the estimator placed the TS-1 edge correctly 1,249
times out of 1,254 and five times landed tens to hundreds of milliseconds
away -- while looking MORE confident on the bad blocks (peak/median 6.90
against 4.35).  The pilot is injected ahead of the RX888, so its true position
cannot move; every one of those five was ours.

Two properties have to hold together, and they pull against each other:
the gate must reject a lone outlier, and it must NOT defend a wrong answer
forever when the edge genuinely re-bases (B4 sat on a phantom 20 ms lattice
for hours on 2026-09-04).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from edge_consistency_gate import (  # noqa: E402
    DEFAULT_REACQUIRE_AFTER, DEFAULT_TOLERANCE_SAMPLES,
    EdgeConsistencyGate, wrapped_distance,
)

SR = 96_000


def gate(**kw):
    return EdgeConsistencyGate(samples_per_second=SR, **kw)


class TestWrappedDistance:
    """⚠ The edge sits modulo the second, and a PPS edge likes to sit right
    at the boundary.  Naive subtraction would reject exactly those."""

    def test_straddling_the_second_boundary_is_near(self):
        assert wrapped_distance(95_999, 1, SR) == 2
        assert wrapped_distance(1, 95_999, SR) == 2

    def test_plain_separation(self):
        assert wrapped_distance(1000, 1004, SR) == 4

    def test_the_far_side_is_half_a_second(self):
        assert wrapped_distance(0, SR // 2, SR) == SR // 2

    def test_never_exceeds_half_the_period(self):
        for a in (0, 1, 47_000, 48_001, 95_999):
            assert wrapped_distance(a, 0, SR) <= SR / 2

    def test_rejects_a_nonsense_period(self):
        with pytest.raises(ValueError):
            wrapped_distance(1, 2, 0)


class TestTheFiveRealFailures:
    """The actual B4 jumps, in samples at 96 kHz, must all be refused."""

    # +113.09 ms, -184.34, -282.29, +83.35, +43.42
    JUMPS = (10_857, -17_697, -27_100, 8_001, 4_168)

    @pytest.mark.parametrize('jump', JUMPS)
    def test_each_measured_jump_is_rejected(self, jump):
        g = gate()
        g.check(66_749)
        for _ in range(4):
            g.check(66_749.04)          # the real scatter, 0.043 samples
        v = g.check(66_749 + jump)
        assert not v.accepted, f"{jump} samples slipped through"

    def test_a_jump_does_not_disturb_the_reference(self):
        g = gate()
        g.check(66_749)
        ref_before = g.reference
        g.check(66_749 + 10_857)
        assert g.reference == pytest.approx(ref_before)
        assert g.check(66_749.05).accepted


class TestItAcceptsRealMeasurements:

    def test_the_whole_night_of_good_blocks_passes(self):
        """0.043-sample scatter must not trip a 5-sample gate."""
        g = gate()
        import random
        random.seed(7)
        rejected = 0
        for _ in range(1249):
            if not g.check(66_749 + random.gauss(0, 0.043)).accepted:
                rejected += 1
        assert rejected == 0
        assert g.accepted_count == 1249

    def test_first_edge_always_accepted(self):
        v = gate().check(12_345)
        assert v.accepted and v.distance_samples is None

    def test_an_edge_at_the_second_boundary_is_fine(self):
        g = gate()
        g.check(95_999)
        assert g.check(1).accepted          # 2 samples away, wrapped
        assert g.check(95_998).accepted

    def test_slow_drift_is_followed(self):
        """A real edge may creep; the gate must track it, not fight it."""
        g = gate()
        pos = 50_000.0
        g.check(pos)
        for _ in range(200):
            pos += 0.2
            assert g.check(pos).accepted
        assert g.reference == pytest.approx(pos, abs=2.0)


class TestReacquisition:
    """⛔⛔ A gate that never releases defends a wrong answer forever."""

    def test_a_genuine_rebase_is_adopted(self):
        g = gate()
        g.check(66_749)
        new = 66_749 + 20_000
        verdicts = [g.check(new + i * 0.01) for i in range(DEFAULT_REACQUIRE_AFTER)]
        assert not verdicts[0].accepted
        assert verdicts[-1].accepted and verdicts[-1].reacquired
        assert g.reference == pytest.approx(new, abs=1.0)
        assert g.check(new + 0.05).accepted

    def test_scattered_noise_never_forms_a_quorum(self):
        """Outliers that disagree with EACH OTHER must not add up to a re-base."""
        g = gate()
        g.check(66_749)
        for jump in (10_000, -25_000, 40_000, 7_000, -13_000, 31_000):
            assert not g.check(66_749 + jump).accepted
        assert g.reference == pytest.approx(66_749)
        assert g.reacquire_count == 0

    def test_held_edges_do_not_pile_up_forever(self):
        """⚠ A station runs for months.  One retained entry per rejected edge
        and no discard is a slow leak in the timing path."""
        g = gate()
        g.check(66_749)
        import random
        random.seed(11)
        for _ in range(5_000):
            g.check(66_749 + random.uniform(1_000, 40_000))
        assert len(g._pending) <= DEFAULT_REACQUIRE_AFTER, (
            f"held {len(g._pending)} edges after 5,000 rejects")

    def test_one_outlier_then_recovery_never_reacquires(self):
        g = gate()
        g.check(66_749)
        assert not g.check(66_749 + 10_857).accepted
        for _ in range(5):
            assert g.check(66_749.03).accepted
        assert g.reacquire_count == 0

    def test_reacquire_after_one_is_honoured(self):
        g = gate(reacquire_after=1)
        g.check(100.0)
        v = g.check(50_000.0)
        assert v.accepted and v.reacquired


class TestConstruction:

    @pytest.mark.parametrize('kw', [
        dict(samples_per_second=0), dict(samples_per_second=-1),
    ])
    def test_rejects_a_nonsense_rate(self, kw):
        with pytest.raises(ValueError):
            EdgeConsistencyGate(**kw)

    @pytest.mark.parametrize('kw', [
        dict(tolerance_samples=0), dict(tolerance_samples=-5),
        dict(reacquire_after=0), dict(reacquire_after=-2),
    ])
    def test_rejects_nonsense_settings(self, kw):
        with pytest.raises(ValueError):
            gate(**kw)

    def test_defaults_match_wd_record(self):
        assert DEFAULT_TOLERANCE_SAMPLES == 5.0
        g = gate()
        assert g.tolerance_samples == 5.0
        assert g.reference is None
