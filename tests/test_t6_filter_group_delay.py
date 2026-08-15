"""radiod's channel-filter group delay is a separate term from the TS-1 path.

T6_ANCHOR_INVERSION_DESIGN §5 bounds ``delay_budget_ns`` to ±1 ms on the
stated belief that "the analog TS-1→ADC path plus channel-filter group
delay is microseconds to sub-millisecond", and the config template
records the group-delay term as "0 pending fleet characterisation".

Characterised on AC0G-B4 2026-08-15 against T4, n=90 judge publications
over 15 min once the arrival-floor fix had removed the transport latency
that used to swamp it: **16.618 ms**, IQR 1.41 ms.  That is 16x the hard
bound, and it is consistent with the coarse path's own note that radiod
filter group delay is "the dominant contribution (up to ~150 ms at
narrow filter widths)".

The ±1 ms bound is not wrong -- it is right about the TS-1 modulator
path, and it exists to stop timestamp error being absorbed into the
budget.  Widening it to fit the group delay would destroy exactly that
property.  So the group delay becomes its own named, separately bounded
term, and the modulator bound keeps its meaning.
"""
import pytest

from hf_timestd.core.bpsk_edge_fine_stage import FineEdgeEstimate
from hf_timestd.core.core_recorder_v2 import CoreRecorderV2
from hf_timestd.core.t6_anchor_authority import (
    DELAY_BUDGET_BOUND_NS, FILTER_GROUP_DELAY_BOUND_NS, T6AnchorAuthority,
)

SR = 96_000
SECOND = 1_700_000_000
BUDGET = 10_000            # TS-1 modulator path, microseconds
GROUP = 16_618_000         # measured radiod filter group delay on B4


def est(offset=43_181.0, rtp=1_000_000, sub=0.25, n=30):
    return FineEdgeEstimate(
        edge_offset_samples=offset, edge_rtp=rtp, edge_subsample=sub,
        n_seconds_folded=n, plateau_amplitude=30.0, fit_rms=0.05,
    )


def phase(e):
    return (e.edge_rtp + e.edge_subsample) % SR


def test_group_delay_enters_the_anchor():
    auth = T6AnchorAuthority(sample_rate_hz=SR, delay_budget_ns=BUDGET,
                             filter_group_delay_ns=GROUP)
    e = est()

    d = auth.on_fine_estimate(e, phase(e), SECOND)

    assert d.anchor is not None
    sub_ns = int(round(e.edge_subsample * 1e9 / SR))
    assert d.anchor.anchor_utc_ns == (
        SECOND * 1_000_000_000 + BUDGET + GROUP - sub_ns)
    # The anchor's chain delay is the WHOLE asserted path, both terms.
    assert d.anchor.chain_delay_ns == BUDGET + GROUP


def test_group_delay_defaults_to_zero_so_existing_sites_are_unchanged():
    auth = T6AnchorAuthority(sample_rate_hz=SR, delay_budget_ns=BUDGET)
    e = est()

    d = auth.on_fine_estimate(e, phase(e), SECOND)

    assert d.anchor.chain_delay_ns == BUDGET


def test_group_delay_is_not_limited_by_the_modulator_bound():
    """The whole point: 16.6 ms is legal as group delay, illegal as budget."""
    assert GROUP > DELAY_BUDGET_BOUND_NS
    T6AnchorAuthority(sample_rate_hz=SR, delay_budget_ns=BUDGET,
                      filter_group_delay_ns=GROUP)


def test_modulator_budget_bound_still_refuses():
    """The guard that caught this must keep its teeth."""
    with pytest.raises(ValueError, match="delay_budget_ns"):
        T6AnchorAuthority(sample_rate_hz=SR,
                          delay_budget_ns=DELAY_BUDGET_BOUND_NS + 1)


def test_group_delay_has_its_own_physical_bound():
    """Unbounded would just move the hole rather than close it.  radiod
    filter group delay reaches ~150 ms at narrow widths, so the bound
    matches the coarse path's ±250 ms plausibility limit."""
    with pytest.raises(ValueError, match="filter_group_delay_ns"):
        T6AnchorAuthority(
            sample_rate_hz=SR, delay_budget_ns=BUDGET,
            filter_group_delay_ns=FILTER_GROUP_DELAY_BOUND_NS + 1)


# ────────────────────────────────────────────────────────────────────
# Config plumbing
# ────────────────────────────────────────────────────────────────────

def test_fine_settings_parses_the_group_delay():
    s = CoreRecorderV2._t6_fine_settings({"filter_group_delay_ns": GROUP})

    assert s["filter_group_delay_ns"] == GROUP


def test_fine_settings_defaults_the_group_delay_to_zero():
    s = CoreRecorderV2._t6_fine_settings({})

    assert s["filter_group_delay_ns"] == 0


def test_fine_settings_refuses_an_out_of_bound_group_delay():
    with pytest.raises(ValueError, match="filter_group_delay_ns"):
        CoreRecorderV2._t6_fine_settings(
            {"filter_group_delay_ns": FILTER_GROUP_DELAY_BOUND_NS + 1})
