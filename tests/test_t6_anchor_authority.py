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


def est(offset=43_181.0, rtp=1_000_000, sub=0.25, n=30):
    return FineEdgeEstimate(
        edge_offset_samples=offset, edge_rtp=rtp, edge_subsample=sub,
        n_seconds_folded=n, plateau_amplitude=30.0, fit_rms=0.05,
    )


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
        d = auth.on_fine_estimate(est(), 43_181.0, SECOND)
        assert d.previous_state is T6AuthorityState.ACQUIRING
        assert d.state is T6AuthorityState.AUTHORITATIVE
        assert d.violations == ()
        assert d.anchor is not None
        assert d.anchor.captured_via_tier == "T6"

    def test_anchor_math(self, auth):
        d = auth.on_fine_estimate(est(sub=0.25), 43_181.0, SECOND)
        a = d.anchor
        assert a.anchor_rtp == 1_000_000
        # sample at edge_rtp acquired sub/SR BEFORE the true edge instant
        expected = SECOND * 10**9 + BUDGET - round(0.25 * 1e9 / SR)
        assert a.anchor_utc_ns == expected
        assert a.chain_delay_ns == BUDGET

    def test_naming_unavailable_while_acquiring_stays_acquiring(self, auth):
        d = auth.on_fine_estimate(est(), 43_181.0, None)
        assert d.state is T6AuthorityState.ACQUIRING
        assert "naming_unavailable" in d.violations
        assert d.anchor is None

    def test_edge_period_violation_degrades_and_holds_anchor(self, auth):
        d1 = auth.on_fine_estimate(est(offset=43_181.0), 43_181.0, SECOND)
        # next block's edge moved 10 µs within the second (> 5 µs tol)
        moved = 43_181.0 + 10e-6 * SR
        d2 = auth.on_fine_estimate(est(offset=moved), moved, SECOND + 30)
        assert d2.state is T6AuthorityState.DEGRADED
        assert "edge_period" in d2.violations
        assert d2.anchor == d1.anchor  # held, not replaced

    def test_fine_coarse_violation_degrades(self, auth):
        auth.on_fine_estimate(est(), 43_181.0, SECOND)
        # coarse 6 ms away from fine (> 5 ms tol); period still clean
        d = auth.on_fine_estimate(
            est(), 43_181.0 + 0.006 * SR, SECOND + 30)
        assert d.state is T6AuthorityState.DEGRADED
        assert "fine_coarse" in d.violations

    def test_degraded_recovers_on_clean_estimate(self, auth):
        auth.on_fine_estimate(est(), 43_181.0, SECOND)
        auth.on_fine_estimate(est(), 43_181.0 + 0.006 * SR, SECOND + 30)
        d = auth.on_fine_estimate(est(), 43_181.0, SECOND + 60)
        assert d.state is T6AuthorityState.AUTHORITATIVE

    def test_degraded_past_dwell_unlocks(self, auth):
        auth.on_fine_estimate(est(), 43_181.0, SECOND)
        auth.on_fine_estimate(est(), 43_181.0 + 0.006 * SR, SECOND + 30)
        auth._test_clock.t += 601.0
        d = auth.on_fine_estimate(
            est(), 43_181.0 + 0.006 * SR, SECOND + 660)
        assert d.state is T6AuthorityState.UNLOCKED
        assert d.anchor is None

    def test_mf_unlock_from_authoritative_unlocks(self, auth):
        auth.on_fine_estimate(est(), 43_181.0, SECOND)
        d = auth.on_mf_unlock()
        assert d.state is T6AuthorityState.UNLOCKED
        assert d.anchor is None

    def test_unlocked_reacquires_on_next_clean_estimate(self, auth):
        auth.on_fine_estimate(est(), 43_181.0, SECOND)
        auth.on_mf_unlock()
        d = auth.on_fine_estimate(est(), 43_181.0, SECOND + 60)
        assert d.previous_state is T6AuthorityState.UNLOCKED
        assert d.state is T6AuthorityState.AUTHORITATIVE

    def test_missing_coarse_skips_fine_coarse_check(self, auth):
        d = auth.on_fine_estimate(est(), None, SECOND)
        assert d.state is T6AuthorityState.AUTHORITATIVE

    def test_period_check_wraps_across_the_second(self, auth):
        auth.on_fine_estimate(est(offset=2.0), 2.0, SECOND)
        # SR-1 ≡ −1 sample: wrapped distance 3 samples ≈ 31 µs > tol; but
        # 2.0 → 2.0 + 0.3 samples (≈3 µs) must NOT degrade.
        d = auth.on_fine_estimate(est(offset=2.3), 2.3, SECOND + 30)
        assert d.state is T6AuthorityState.AUTHORITATIVE
