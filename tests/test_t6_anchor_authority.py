"""T6 anchor authority state machine.

Spec: docs/design/T6_ANCHOR_INVERSION_DESIGN.md §4, §6.
"""
import pytest

from hf_timestd.core.bpsk_edge_fine_stage import FineEdgeEstimate
from hf_timestd.core.t6_anchor_authority import (
    T6AnchorAuthority, T6AuthorityState,
)

SR = 96_000
SECOND = 1_700_000_000
BUDGET = 10_000

# Final-review Finding 1: the authority's comparands (the MF coarse
# offset, and the previous estimate — possibly produced under a
# different fold registration) are RTP-domain, so it derives the edge
# phase from ``edge_rtp + edge_subsample`` and NOT from the fold-domain
# ``edge_offset_samples``.  These tests therefore move ``rtp``/``sub``
# to move the edge, and pass a coarse in the RTP domain.  The previous
# tests moved ``offset`` and passed the same number as coarse, which
# only agreed with the RTP domain because ``est()``'s default rtp
# happened to be unrelated to the check — the mixed-domain premise was
# the bug.


def est(offset=43_181.0, rtp=1_000_000, sub=0.25, n=30):
    return FineEdgeEstimate(
        edge_offset_samples=offset, edge_rtp=rtp, edge_subsample=sub,
        n_seconds_folded=n, plateau_amplitude=30.0, fit_rms=0.05,
    )


def phase(e):
    """RTP-domain edge phase of an estimate — what the authority checks
    against, and therefore the matching 'perfect' coarse offset."""
    return (e.edge_rtp + e.edge_subsample) % SR


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def auth():
    clock = FakeClock()
    a = T6AnchorAuthority(SR, BUDGET, now=clock)
    a._test_clock = clock
    return a


class TestDelayBudgetBound:
    def test_budget_beyond_1ms_refuses_at_construction(self):
        with pytest.raises(ValueError, match="delay_budget"):
            T6AnchorAuthority(SR, 1_000_001)
        with pytest.raises(ValueError, match="delay_budget"):
            T6AnchorAuthority(SR, -1_000_001)

    def test_budget_within_bound_accepted(self):
        T6AnchorAuthority(SR, 999_999)


class TestTransitions:
    def test_first_clean_estimate_becomes_authoritative(self, auth):
        e = est()
        d = auth.on_fine_estimate(e, phase(e), SECOND)
        assert d.previous_state is T6AuthorityState.ACQUIRING
        assert d.state is T6AuthorityState.AUTHORITATIVE
        assert d.violations == ()
        assert d.anchor is not None
        assert d.anchor.captured_via_tier == "T6"

    def test_anchor_math(self, auth):
        e = est(sub=0.25)
        d = auth.on_fine_estimate(e, phase(e), SECOND)
        a = d.anchor
        assert a.anchor_rtp == 1_000_000
        # sample at edge_rtp acquired sub/SR BEFORE the true edge instant
        expected = SECOND * 10**9 + BUDGET - round(0.25 * 1e9 / SR)
        assert a.anchor_utc_ns == expected
        assert a.chain_delay_ns == BUDGET

    def test_naming_unavailable_while_acquiring_stays_acquiring(self, auth):
        e = est()
        d = auth.on_fine_estimate(e, phase(e), None)
        assert d.state is T6AuthorityState.ACQUIRING
        assert "naming_unavailable" in d.violations
        assert d.anchor is None

    def test_edge_period_violation_degrades_and_holds_anchor(self, auth):
        e1 = est()
        d1 = auth.on_fine_estimate(e1, phase(e1), SECOND)
        # next block's edge moved one sample (10.4 µs) within the second
        # — above the 5 µs tolerance.  Moved in the RTP domain, which is
        # where the periodicity invariant lives.
        e2 = est(rtp=1_000_001)
        d2 = auth.on_fine_estimate(e2, phase(e2), SECOND + 30)
        assert d2.state is T6AuthorityState.DEGRADED
        assert "edge_period" in d2.violations
        assert d2.anchor == d1.anchor  # held, not replaced

    def test_fine_coarse_violation_degrades(self, auth):
        e = est()
        auth.on_fine_estimate(e, phase(e), SECOND)
        # coarse 6 ms away from fine (> 5 ms tol); period still clean
        d = auth.on_fine_estimate(
            e, phase(e) + 0.006 * SR, SECOND + 30)
        assert d.state is T6AuthorityState.DEGRADED
        assert "fine_coarse" in d.violations

    def test_degraded_recovers_on_clean_estimate(self, auth):
        e = est()
        auth.on_fine_estimate(e, phase(e), SECOND)
        auth.on_fine_estimate(e, phase(e) + 0.006 * SR, SECOND + 30)
        d = auth.on_fine_estimate(e, phase(e), SECOND + 60)
        assert d.state is T6AuthorityState.AUTHORITATIVE

    def test_degraded_past_dwell_unlocks(self, auth):
        e = est()
        auth.on_fine_estimate(e, phase(e), SECOND)
        auth.on_fine_estimate(e, phase(e) + 0.006 * SR, SECOND + 30)
        auth._test_clock.t += 601.0
        d = auth.on_fine_estimate(
            e, phase(e) + 0.006 * SR, SECOND + 660)
        assert d.state is T6AuthorityState.UNLOCKED
        assert d.anchor is None

    def test_mf_unlock_from_authoritative_unlocks(self, auth):
        e = est()
        auth.on_fine_estimate(e, phase(e), SECOND)
        d = auth.on_mf_unlock()
        assert d.state is T6AuthorityState.UNLOCKED
        assert d.anchor is None

    def test_unlocked_reacquires_on_next_clean_estimate(self, auth):
        e = est()
        auth.on_fine_estimate(e, phase(e), SECOND)
        auth.on_mf_unlock()
        d = auth.on_fine_estimate(e, phase(e), SECOND + 60)
        assert d.previous_state is T6AuthorityState.UNLOCKED
        assert d.state is T6AuthorityState.AUTHORITATIVE

    def test_missing_coarse_skips_fine_coarse_check(self, auth):
        d = auth.on_fine_estimate(est(), None, SECOND)
        assert d.state is T6AuthorityState.AUTHORITATIVE

    def test_period_check_wraps_across_the_second(self, auth):
        # Phases 95_999.9 and 0.1 straddle the second boundary: the
        # wrapped distance is 0.2 samples (≈2 µs, clean), while a naive
        # |a − b| would read 95_999.8 samples and degrade.
        e1 = est(rtp=960_000, sub=-0.1)
        assert phase(e1) == pytest.approx(SR - 0.1)
        auth.on_fine_estimate(e1, phase(e1), SECOND)
        e2 = est(rtp=1_056_000, sub=0.1)
        assert phase(e2) == pytest.approx(0.1)
        d = auth.on_fine_estimate(e2, phase(e2), SECOND + 30)
        assert d.state is T6AuthorityState.AUTHORITATIVE


class TestRtpDomainComparison:
    """Final-review Finding 1 (authority half): ``_check`` must compare
    the estimate and the coarse offset in the SAME domain.
    ``edge_offset_samples`` is fold-domain (offset from the RTP domain
    by an arbitrary per-block registration); the MF coarse is
    RTP-domain.  Comparing them directly made the fine–coarse invariant
    meaningless — it passed or failed on the registration, not on
    estimator agreement."""

    def test_fine_coarse_passes_when_rtp_phases_agree_despite_fold_offset(
        self, auth
    ):
        # Production shape: fold offset 43_181 (stage-relative), RTP
        # phase 40_000.25, MF coarse = the RTP phase.  The two
        # estimators agree perfectly; the old fold-vs-RTP comparison
        # saw a 33 ms gap and would have refused to go AUTHORITATIVE.
        e = est(offset=43_181.0, rtp=1_000_000, sub=0.25)
        assert abs(e.edge_offset_samples - phase(e)) > 0.005 * SR
        d = auth.on_fine_estimate(e, phase(e), SECOND)
        assert d.state is T6AuthorityState.AUTHORITATIVE
        assert d.violations == ()

    def test_fine_coarse_still_fires_on_a_real_rtp_domain_gap(self, auth):
        e = est()
        d = auth.on_fine_estimate(e, phase(e) + 0.0051 * SR, SECOND)
        assert "fine_coarse" in d.violations

    def test_period_survives_a_registration_change(self, auth):
        # Two consecutive blocks whose fold registrations differ (a
        # stage reset between them): the fold-domain offsets differ by
        # thousands of samples while the physical edge has not moved.
        # The RTP-domain phase is what must be compared.
        e1 = est(offset=43_181.0, rtp=1_000_000, sub=0.0)
        auth.on_fine_estimate(e1, phase(e1), SECOND)
        e2 = est(offset=11_000.0, rtp=1_000_000 + 30 * SR, sub=0.0)
        d = auth.on_fine_estimate(e2, phase(e2), SECOND + 30)
        assert d.state is T6AuthorityState.AUTHORITATIVE
        assert d.violations == ()


class TestRtpWrapPeriodicity:
    """Re-review finding: comparing two mod-SR phases made the
    periodicity check false-fire at every 32-bit RTP wrap.
    ``2**32 % 96000 == 23296``, so the phase jumps 23 296 samples
    (242.7 ms) at a wrap with the physical edge unmoved — a
    deterministic false DEGRADED, 600 s dwell, then UNLOCKED and a
    dropped anchor, once every 12.43 h.  The deviation is now taken
    from the signed 32-bit counter delta, which wraps with the
    counter."""

    def test_phase_really_does_jump_at_the_wrap(self):
        # Guards the premise: if this ever stops holding the test below
        # is no longer testing anything.
        assert (2**32) % SR == 23_296

    def test_wrap_with_edge_unmoved_stays_authoritative(self, auth):
        # Last estimate before the wrap, then one after: the edge has
        # advanced by exact multiples of SR, so it has not moved at all.
        before = est(rtp=(2**32 - 5 * SR) & 0xFFFFFFFF, sub=0.25)
        d1 = auth.on_fine_estimate(before, phase(before), SECOND)
        assert d1.state is T6AuthorityState.AUTHORITATIVE
        after = est(rtp=(2**32 - 5 * SR + 30 * SR) & 0xFFFFFFFF, sub=0.25)
        assert after.edge_rtp < before.edge_rtp  # counter really wrapped
        # The old phase comparison saw 23_296 samples of "movement" here.
        assert auth._wrapped_distance_samples(
            phase(after), phase(before)) == pytest.approx(23_296.0)
        d2 = auth.on_fine_estimate(after, phase(after), SECOND + 30)
        assert d2.violations == ()
        assert d2.state is T6AuthorityState.AUTHORITATIVE

    def test_real_step_across_the_wrap_still_degrades(self, auth):
        before = est(rtp=(2**32 - 5 * SR) & 0xFFFFFFFF, sub=0.25)
        auth.on_fine_estimate(before, phase(before), SECOND)
        # Same wrap, but the edge genuinely moved ~10 µs (0.96 samples).
        after = est(rtp=(2**32 - 5 * SR + 30 * SR + 1) & 0xFFFFFFFF, sub=0.25)
        d = auth.on_fine_estimate(after, phase(after), SECOND + 30)
        assert "edge_period" in d.violations
        assert d.state is T6AuthorityState.DEGRADED

    def test_sub_sample_motion_across_the_wrap_is_measured(self, auth):
        before = est(rtp=(2**32 - 5 * SR) & 0xFFFFFFFF, sub=-0.4)
        auth.on_fine_estimate(before, phase(before), SECOND)
        # +0.3 samples ≈ 3.1 µs, inside the 5 µs tolerance.
        after = est(rtp=(2**32 - 5 * SR + 30 * SR) & 0xFFFFFFFF, sub=-0.1)
        d = auth.on_fine_estimate(after, phase(after), SECOND + 30)
        assert d.state is T6AuthorityState.AUTHORITATIVE


class TestEstimateLiveness:
    """Final-review Finding 3: the authority is edge-triggered on fine
    estimates.  If estimates stop while the MF stays locked (the
    dominant symptom of the domain bug, plus discarded blocks and
    swallowed exceptions), nothing re-evaluates the state and the
    anchor freezes AUTHORITATIVE forever, still feeding chrony —
    detect-and-stall, forbidden by spec §6."""

    def test_no_tick_decision_while_fresh(self, auth):
        e = est()
        auth.on_fine_estimate(e, phase(e), SECOND)
        auth._test_clock.t += 3 * 30.0 - 1.0
        assert auth.on_tick() is None
        assert auth.state is T6AuthorityState.AUTHORITATIVE

    def test_stale_estimates_degrade_with_named_violation(self, auth):
        e = est()
        auth.on_fine_estimate(e, phase(e), SECOND)
        auth._test_clock.t += 3 * 30.0 + 1.0
        d = auth.on_tick()
        assert d is not None
        assert d.previous_state is T6AuthorityState.AUTHORITATIVE
        assert d.state is T6AuthorityState.DEGRADED
        assert d.violations == ("estimate_stale",)
        # Anchor held while coasting (GPSDO), not silently dropped.
        assert d.anchor is not None

    def test_stale_beyond_dwell_unlocks(self, auth):
        e = est()
        auth.on_fine_estimate(e, phase(e), SECOND)
        auth._test_clock.t += 3 * 30.0 + 1.0
        auth.on_tick()
        auth._test_clock.t += 601.0
        d = auth.on_tick()
        assert d.state is T6AuthorityState.UNLOCKED
        assert d.anchor is None

    def test_tick_is_inert_while_acquiring(self, auth):
        auth._test_clock.t += 100_000.0
        assert auth.on_tick() is None
        assert auth.state is T6AuthorityState.ACQUIRING

    def test_fresh_estimate_clears_staleness(self, auth):
        e = est()
        auth.on_fine_estimate(e, phase(e), SECOND)
        auth._test_clock.t += 3 * 30.0 + 1.0
        assert auth.on_tick() is not None
        auth.on_fine_estimate(e, phase(e), SECOND + 30)
        assert auth.state is T6AuthorityState.AUTHORITATIVE
        auth._test_clock.t += 10.0
        assert auth.on_tick() is None

    def test_stale_window_follows_fold_length(self):
        clock = FakeClock()
        a = T6AnchorAuthority(SR, BUDGET, fine_fold_seconds=8.0, now=clock)
        e = est()
        a.on_fine_estimate(e, phase(e), SECOND)
        clock.t += 3 * 8.0 - 1.0
        assert a.on_tick() is None
        clock.t += 2.0
        assert a.on_tick().violations == ("estimate_stale",)

    def test_explicit_expected_interval_overrides_fold_length(self, auth):
        e = est()
        auth.on_fine_estimate(e, phase(e), SECOND)
        auth._test_clock.t += 10.0
        assert auth.on_tick(expected_interval_sec=1.0) is not None


class TestAcquiringVisibility:
    def test_repeated_acquiring_violations_warn_throttled(self, auth, caplog):
        e = est()
        with caplog.at_level("WARNING"):
            for _ in range(5):
                auth.on_fine_estimate(e, phase(e), None)
        warns = [m for m in caplog.messages if "ACQUIRING" in m]
        assert len(warns) == 1
        auth._test_clock.t += 301.0
        with caplog.at_level("WARNING"):
            auth.on_fine_estimate(e, phase(e), None)
        warns = [m for m in caplog.messages if "ACQUIRING" in m]
        assert len(warns) == 2
