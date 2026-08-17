"""How far T6 may coast on the GPSDO ruler after losing lock.

The anchor is a (rtp, utc) pair plus a sample rate.  While the RTP
counter stays continuous the GPSDO disciplines its RATE, so a frozen
anchor keeps labelling samples correctly; what grows is only our
uncertainty about the rate.  Measured on AC0G-B4 2026-08-16:
``t6_residual_rate = -0.0004 +/- 0.0004 ppm`` over 900 s.
"""
import pytest

from hf_timestd.core.t6_holdover import (
    coast_ruler_intact,
    holdover_named_second,
    holdover_sigma_ns,
    may_coast,
)


class TestSigmaLaw:
    def test_at_zero_elapsed_it_is_the_frozen_sigma(self):
        assert holdover_sigma_ns(800_000.0, 0.0004, 0.0) == pytest.approx(
            800_000.0
        )

    def test_rate_uncertainty_integrates_over_time(self):
        """0.0004 ppm for an hour is 1.44 us — the whole point."""
        got = holdover_sigma_ns(0.0, 0.0004, 3600.0)
        assert got == pytest.approx(1440.0, rel=1e-6)

    def test_a_six_hour_storm_is_dominated_by_the_frozen_sigma(self):
        """The measured answer to 'how long can we safely coast'.

        Base 800 us against 8.6 us of accumulated rate uncertainty:
        coasting through weather is essentially free, so this must NOT
        materially widen.
        """
        base = 800_000.0
        got = holdover_sigma_ns(base, 0.0004, 6 * 3600.0)
        assert got == pytest.approx(base, rel=1e-3)
        assert got > base  # but it must still be strictly honest

    def test_terms_add_in_quadrature(self):
        got = holdover_sigma_ns(3000.0, 0.0004, 3600.0)  # 3 us base, 1.44 us
        assert got == pytest.approx((3000.0**2 + 1440.0**2) ** 0.5)

    def test_monotonically_non_decreasing(self):
        prev = -1.0
        for t in (0.0, 1.0, 60.0, 3600.0, 86400.0, 30 * 86400.0):
            got = holdover_sigma_ns(500_000.0, 0.0004, t)
            assert got >= prev
            prev = got

    def test_reaching_one_millisecond_takes_weeks_not_hours(self):
        """Sanity-check the operational claim this feature rests on."""
        base = 0.0
        assert holdover_sigma_ns(base, 0.0004, 24 * 3600.0) < 40_000.0
        assert holdover_sigma_ns(base, 0.0004, 29 * 86400.0) > 900_000.0


class TestRefusesToUnderstate:
    def test_unknown_rate_uncertainty_is_not_treated_as_zero(self):
        """An unmeasured rate must never look like a perfect one.

        ``t6_residual_rate`` is null until ~900 s of anchor lifetime has
        accumulated (and after every recapture), so this is the state a
        fresh or just-restarted station is actually in.
        """
        unknown = holdover_sigma_ns(500_000.0, None, 3600.0)
        measured = holdover_sigma_ns(500_000.0, 0.0004, 3600.0)
        assert unknown > measured

    def test_negative_elapsed_cannot_shrink_sigma(self):
        assert holdover_sigma_ns(500_000.0, 0.0004, -10.0) == pytest.approx(
            500_000.0
        )


class TestMayCoast:
    """The preconditions for a coast, as opposed to hf-timestd#14.

    A holdover is only sound while the ruler it rests on is intact:
    a frozen validated anchor, and an RTP counter that has not been
    re-based underneath it.
    """

    def test_permitted_when_anchor_frozen_and_counter_continuous(self):
        ok, reason = may_coast(
            anchor_frozen=True, rtp_continuous=True,
            sigma_ns=800_000.0, max_sigma_ns=5_000_000.0,
        )
        assert ok is True
        assert reason == "ok"

    def test_refused_without_a_frozen_anchor(self):
        ok, reason = may_coast(
            anchor_frozen=False, rtp_continuous=True,
            sigma_ns=1000.0, max_sigma_ns=5_000_000.0,
        )
        assert ok is False
        assert "anchor" in reason

    def test_rtp_discontinuity_refuses_however_good_the_sigma(self):
        """A radiod restart re-bases the counter: the ruler is gone, and
        no amount of accumulated precision substitutes for it."""
        ok, reason = may_coast(
            anchor_frozen=True, rtp_continuous=False,
            sigma_ns=1.0, max_sigma_ns=5_000_000.0,
        )
        assert ok is False
        assert "rtp" in reason.lower()

    def test_refused_once_sigma_exceeds_the_bound(self):
        ok, reason = may_coast(
            anchor_frozen=True, rtp_continuous=True,
            sigma_ns=5_000_001.0, max_sigma_ns=5_000_000.0,
        )
        assert ok is False
        assert "sigma" in reason

    def test_bound_is_inclusive(self):
        ok, _ = may_coast(
            anchor_frozen=True, rtp_continuous=True,
            sigma_ns=5_000_000.0, max_sigma_ns=5_000_000.0,
        )
        assert ok is True

    def test_reason_names_the_first_failing_precondition(self):
        """Reasons are logged; a coast refused for two reasons should
        report the more fundamental one."""
        ok, reason = may_coast(
            anchor_frozen=False, rtp_continuous=False,
            sigma_ns=9e9, max_sigma_ns=5_000_000.0,
        )
        assert ok is False
        assert "anchor" in reason


class TestNamedSecondWithoutAnEdge:
    """A coast must name the second from the FROZEN anchor, never from a
    fresh edge — accepting unvalidated edges while unlocked is exactly
    what hf-timestd#14 was."""

    def test_names_the_most_recent_boundary_in_pps_firing_space(self):
        # floor maps mono -> label as label = mono + 100.0
        n = holdover_named_second(
            floor_offset_s=100.0, mono_now=900.5, chain_delay_ns=0
        )
        assert n == 1000

    def test_chain_delay_is_SUBTRACTED_to_reach_firing_space(self):
        """Labels are sampling instants; the PPS fired chain_delay
        EARLIER.  Sign errors here have bitten this codebase before."""
        # label-space now = 1000.010; firing space = 1000.010 - 0.016618
        # = 999.993 -> the most recent boundary is 999, not 1000.
        n = holdover_named_second(
            floor_offset_s=100.0, mono_now=900.010,
            chain_delay_ns=16_618_000,
        )
        assert n == 999

    def test_just_after_a_boundary_names_that_boundary(self):
        n = holdover_named_second(
            floor_offset_s=100.0, mono_now=900.001, chain_delay_ns=0
        )
        assert n == 1000

    def test_just_before_a_boundary_names_the_previous_one(self):
        n = holdover_named_second(
            floor_offset_s=100.0, mono_now=899.999, chain_delay_ns=0
        )
        assert n == 999

    def test_advances_exactly_once_per_second_of_coast(self):
        seen = {
            holdover_named_second(100.0, 900.0 + i * 0.1, 0)
            for i in range(30)
        }
        assert seen == {1000, 1001, 1002}

    def test_returns_an_int(self):
        n = holdover_named_second(100.0, 900.5, 0)
        assert isinstance(n, int)


class TestRulerIntact:
    """A radiod restart re-bases the RTP counter under the frozen
    anchor, so ``utc_ns_at_rtp`` starts returning labels from a
    different numbering and the arrival-floor offset jumps by seconds.
    That offset is exactly what the coast consumes, so watching it is
    the most direct continuity check available."""

    def test_intact_while_the_offset_only_drifts(self):
        # A month of coasting at the measured rate moves it ~1 ms.
        assert coast_ruler_intact(100.001, 100.000) is True

    def test_broken_when_the_offset_jumps_by_seconds(self):
        assert coast_ruler_intact(3712.5, 100.000) is False

    def test_broken_in_either_direction(self):
        assert coast_ruler_intact(100.000 - 5.0, 100.000) is False

    def test_tolerance_is_generous_against_drift_but_not_a_rebase(self):
        """Anything a real coast produces must pass; a re-base must not.
        The gap between them is many orders of magnitude, so the exact
        threshold is not load-bearing."""
        assert coast_ruler_intact(100.0 + 0.001, 100.0) is True   # 1 ms
        assert coast_ruler_intact(100.0 + 1.0, 100.0) is False    # 1 s

    def test_no_freeze_reference_refuses(self):
        """Without a recorded freeze point there is nothing to compare,
        so the coast cannot be shown to be sound."""
        assert coast_ruler_intact(100.0, None) is False
