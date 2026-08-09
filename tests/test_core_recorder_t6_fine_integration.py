"""Anchor-inversion wiring in core_recorder_v2 (spec §2, §4, §6).

Constructs CoreRecorderV2 via __new__ (established pattern in
test_core_recorder_t6_shared.py) and drives the new helper methods
directly.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hf_timestd.core.core_recorder_v2 import CoreRecorderV2
from hf_timestd.core.bpsk_edge_fine_stage import FineEdgeEstimate
from hf_timestd.core.t6_anchor_authority import (
    T6AnchorAuthority, T6AuthorityState,
)

SR = 96_000
SECOND = 1_700_000_000


def bare_recorder():
    r = CoreRecorderV2.__new__(CoreRecorderV2)
    r._t6_channel_info = SimpleNamespace()  # opaque; rtp_to_wallclock mocked
    r._lb1421_probe = None
    r._t6_native_anchor = None
    r._t6_authority = T6AnchorAuthority(SR, 10_000)
    r._t6_authority_last_decision = None
    return r


def est(rtp=1_000_000, offset=43_181.0):
    return FineEdgeEstimate(
        edge_offset_samples=offset, edge_rtp=rtp, edge_subsample=0.0,
        n_seconds_folded=30, plateau_amplitude=30.0, fit_rms=0.05,
    )


class TestNaming:
    def test_names_from_nmea_when_probe_fresh(self):
        r = bare_recorder()
        r._lb1421_probe = SimpleNamespace(
            get_latest=lambda: SimpleNamespace(pps_utc_sec=SECOND))
        # radiod-pair wall estimate is 80 ms off the true second — naming
        # must still round to the NMEA-attested second.
        with patch('ka9q.rtp_recorder.rtp_to_wallclock',
                   return_value=float(SECOND) + 0.080):
            assert r._t6_name_integer_second(1_000_000) == SECOND

    def test_falls_back_to_wall_rounding_without_probe(self):
        r = bare_recorder()
        with patch('ka9q.rtp_recorder.rtp_to_wallclock',
                   return_value=float(SECOND) - 0.120):
            assert r._t6_name_integer_second(1_000_000) == SECOND

    def test_residual_beyond_0p4s_returns_none(self):
        r = bare_recorder()
        with patch('ka9q.rtp_recorder.rtp_to_wallclock',
                   return_value=float(SECOND) + 0.45):
            assert r._t6_name_integer_second(1_000_000) is None

    def test_wallclock_unavailable_returns_none(self):
        r = bare_recorder()
        with patch('ka9q.rtp_recorder.rtp_to_wallclock', return_value=None):
            assert r._t6_name_integer_second(1_000_000) is None


class TestAnchorOwnership:
    def test_authoritative_decision_installs_t6_anchor(self, caplog):
        r = bare_recorder()
        r._t6_rate_reset = lambda reason: None
        d = r._t6_authority.on_fine_estimate(est(), 43_181.0, SECOND)
        with caplog.at_level("WARNING"):
            r._t6_apply_authority_decision(d)
        assert r._t6_native_anchor is d.anchor
        assert r._t6_native_anchor.captured_via_tier == "T6"
        # transition ACQUIRING→AUTHORITATIVE is loud
        assert any("AUTHORITATIVE" in m for m in caplog.messages)

    def test_unlock_invalidates_anchor_loudly(self, caplog):
        r = bare_recorder()
        r._t6_rate_reset = lambda reason: None
        d1 = r._t6_authority.on_fine_estimate(est(), 43_181.0, SECOND)
        r._t6_apply_authority_decision(d1)
        d2 = r._t6_authority.on_mf_unlock()
        with caplog.at_level("WARNING"):
            r._t6_apply_authority_decision(d2)
        assert r._t6_native_anchor is None
        assert any("UNLOCKED" in m and "mf_unlock" in m
                   for m in caplog.messages)

    def test_degraded_holds_anchor_and_names_violation(self, caplog):
        r = bare_recorder()
        r._t6_rate_reset = lambda reason: None
        d1 = r._t6_authority.on_fine_estimate(est(), 43_181.0, SECOND)
        r._t6_apply_authority_decision(d1)
        moved = 43_181.0 + 10e-6 * SR
        d2 = r._t6_authority.on_fine_estimate(
            est(offset=moved), moved, SECOND + 30)
        with caplog.at_level("WARNING"):
            r._t6_apply_authority_decision(d2)
        assert r._t6_native_anchor is d1.anchor
        assert any("DEGRADED" in m and "edge_period" in m
                   for m in caplog.messages)

    def test_same_state_clean_updates_anchor_without_warning(self, caplog):
        r = bare_recorder()
        r._t6_rate_reset = lambda reason: None
        d1 = r._t6_authority.on_fine_estimate(est(), 43_181.0, SECOND)
        r._t6_apply_authority_decision(d1)
        d2 = r._t6_authority.on_fine_estimate(
            est(rtp=1_000_000 + 30 * SR), 43_181.0, SECOND + 30)
        with caplog.at_level("WARNING"):
            caplog.clear()
            r._t6_apply_authority_decision(d2)
        assert r._t6_native_anchor is d2.anchor
        assert caplog.messages == []
