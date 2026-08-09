"""Anchor-inversion wiring in core_recorder_v2 (spec §2, §4, §6).

Constructs CoreRecorderV2 via __new__ (established pattern in
test_core_recorder_t6_shared.py) and drives the new helper methods
directly.
"""
from collections import deque
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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


class TestUnlockReopensLegacyCascade:
    """Finding 1 (review of Task 5): the legacy T5/T4 cascade in
    ``_t6_on_samples`` only re-runs disambiguation when
    ``_t6_last_chain_delay_ns`` is None (its first-lock gate).  An
    authority-only UNLOCKED transition (e.g. DEGRADED dwell timeout while
    the MF itself stays locked) must reopen that gate too, or the
    HPPS/chrony feed goes dead until the fine stage independently
    re-acquires — a silent stall, not a loud fallback."""

    def test_unlock_from_authoritative_clears_legacy_cascade_gate(self, caplog):
        r = bare_recorder()
        r._t6_rate_reset = lambda reason: None
        d1 = r._t6_authority.on_fine_estimate(est(), 43_181.0, SECOND)
        r._t6_apply_authority_decision(d1)
        # Simulate the legacy cascade having a lock in place — exactly
        # the state that must be cleared so _t6_on_samples's first-lock
        # branch re-runs on the next cycle.
        r._t6_last_chain_delay_ns = 123_456
        r._t6_disambiguation_ns = 789
        r._t6_recent_raw = deque([1, 2, 3])
        d2 = r._t6_authority.on_mf_unlock()
        with caplog.at_level("WARNING"):
            r._t6_apply_authority_decision(d2)
        assert r._t6_last_chain_delay_ns is None
        assert r._t6_disambiguation_ns == 0
        assert len(r._t6_recent_raw) == 0

    def test_degraded_does_not_clear_legacy_cascade_gate(self, caplog):
        # Coasting (DEGRADED, anchor held) must NOT re-trigger the T5/T4
        # cascade — only a true UNLOCKED does.
        r = bare_recorder()
        r._t6_rate_reset = lambda reason: None
        d1 = r._t6_authority.on_fine_estimate(est(), 43_181.0, SECOND)
        r._t6_apply_authority_decision(d1)
        r._t6_last_chain_delay_ns = 123_456
        r._t6_disambiguation_ns = 789
        r._t6_recent_raw = deque([1, 2, 3])
        moved = 43_181.0 + 10e-6 * SR
        d2 = r._t6_authority.on_fine_estimate(
            est(offset=moved), moved, SECOND + 30)
        with caplog.at_level("WARNING"):
            r._t6_apply_authority_decision(d2)
        assert d2.state is T6AuthorityState.DEGRADED
        assert r._t6_last_chain_delay_ns == 123_456
        assert r._t6_disambiguation_ns == 789
        assert len(r._t6_recent_raw) == 3


def _bare_on_samples_recorder():
    """Minimal recorder able to survive a full ``_t6_on_samples`` call —
    mirrors ``_make_recorder_at_locked_state`` in
    test_core_recorder_t6_step_recovery.py.  The calibrator reports no
    result (unlocked/no data this batch) so the huge locked-branch is
    bypassed entirely; only the fine-stage feed block at the top of
    ``_t6_on_samples`` is under test."""
    cr = CoreRecorderV2.__new__(CoreRecorderV2)
    cr._use_shared_multistream = True
    cr._t6_first_sample_logged = True
    cr._t6_calibrator = MagicMock()
    cr._t6_calibrator.process_samples.return_value = None
    cr._t6_last_chain_delay_ns = None
    cr._t6_disambiguation_ns = 0
    cr._t6_wrap_rejections = 0
    cr._t6_recent_raw = deque(maxlen=CoreRecorderV2.T6_STEP_RECOVERY_WINDOW)
    cr._t6_last_locked_wall = None
    cr._t6_shm = None
    cr._t6_channel_info = None
    cr.recorders = {}
    return cr


class TestAuthorityCoarseGate:
    """Finding 3 (review of Task 5): after an MF reset/unlock,
    ``_chain_delay_samples`` goes None but ``BpskEdgeFineStage.reset()``
    does not clear its own internal ``_coarse_offset`` — so a
    stale-window fine estimate must never reach the authority while the
    calibrator has no live coarse offset, or the authority can claim
    AUTHORITATIVE with the fine_coarse invariant silently inert."""

    def test_authority_not_consulted_when_coarse_is_none(self):
        cr = _bare_on_samples_recorder()
        cr._t6_calibrator._chain_delay_samples = None  # MF unlocked/reset
        cr._t6_fine_stage = MagicMock()
        cr._t6_fine_stage.process_samples.return_value = est()
        cr._t6_authority = MagicMock()

        samples = MagicMock()
        quality = MagicMock(last_rtp_timestamp=0)
        cr._t6_on_samples(samples, quality)

        cr._t6_authority.on_fine_estimate.assert_not_called()

    def test_authority_consulted_when_coarse_is_live(self):
        cr = _bare_on_samples_recorder()
        cr._t6_calibrator._chain_delay_samples = 43_181.0  # live coarse
        cr._t6_fine_stage = MagicMock()
        cr._t6_fine_stage.process_samples.return_value = est()
        cr._t6_authority = MagicMock()

        samples = MagicMock()
        quality = MagicMock(last_rtp_timestamp=0)
        cr._t6_on_samples(samples, quality)

        cr._t6_authority.on_fine_estimate.assert_called_once()
        args = cr._t6_authority.on_fine_estimate.call_args.args
        assert args[1] == 43_181.0
