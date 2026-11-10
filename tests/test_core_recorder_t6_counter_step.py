"""A suspected input-counter step withdraws the T6 anchor at once.

B4 2026-11-10 01:36Z: a Proxmox snapshot froze the VM and radiod lost
~116 ms of samples with no USB error.  The calibrator held its lock for
two 60 s folds, and the authority kept publishing the stale anchor
(AUTHORITATIVE, then coasting DEGRADED) for ~110 s.
"""
from __future__ import annotations

import collections
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from hf_timestd.core.bpsk_edge_fine_stage import FineEdgeEstimate
from hf_timestd.core.core_recorder_v2 import CoreRecorderV2
from hf_timestd.core.t6_anchor_authority import T6AnchorAuthority, T6AuthorityState

SR = 96_000


def _authoritative():
    a = T6AnchorAuthority(SR, 10_000)
    e = FineEdgeEstimate(edge_offset_samples=43_181.0, edge_rtp=1_000_000,
                         edge_subsample=0.25, n_seconds_folded=30,
                         plateau_amplitude=30.0, fit_rms=0.05, fold_retention=0.98)
    d = a.on_fine_estimate(e, (e.edge_rtp + e.edge_subsample) % SR, 1_700_000_000)
    assert d.state is T6AuthorityState.AUTHORITATIVE
    return a, d.anchor


def _recorder():
    cr = CoreRecorderV2.__new__(CoreRecorderV2)
    cr._t6_authority, cr._t6_native_anchor = _authoritative()
    cr._t6_calibrator = SimpleNamespace(step_suspected=False)
    cr._t6_capture_anomaly = MagicMock()
    cr._t6_last_chain_delay_ns = 32_000_000
    cr._t6_disambiguation_ns = 5
    cr._t6_recent_raw = collections.deque()
    return cr


def test_no_suspicion_leaves_the_anchor_alone():
    cr = _recorder()
    cr._t6_check_counter_step()
    assert cr._t6_authority.state is T6AuthorityState.AUTHORITATIVE
    assert cr._t6_native_anchor is not None


def test_rising_suspicion_withdraws_anchor_and_reopens_cascade():
    cr = _recorder()
    cr._t6_calibrator.step_suspected = True
    cr._t6_check_counter_step()
    assert cr._t6_authority.state is T6AuthorityState.UNLOCKED
    assert cr._t6_native_anchor is None
    assert cr._t6_last_chain_delay_ns is None


def test_suspicion_is_edge_triggered():
    cr = _recorder()
    cr._t6_calibrator.step_suspected = True
    cr._t6_check_counter_step()
    cr._t6_apply_authority_decision = MagicMock()
    cr._t6_check_counter_step()          # still suspected: no second decision
    cr._t6_apply_authority_decision.assert_not_called()


def test_cascade_waits_while_a_step_is_suspected():
    """The first-lock branch must not disambiguate the stale lock."""
    cr = CoreRecorderV2.__new__(CoreRecorderV2)
    cr._use_shared_multistream = True
    cr._t6_first_sample_logged = True
    cr._t6_calibrator = MagicMock()
    cr._t6_calibrator.consecutive_required = 10
    cr._t6_calibrator.step_suspected = True
    cr._t6_calibrator.process_samples.return_value = SimpleNamespace(
        locked=True, chain_delay_ns=232_766_454, chain_delay_samples=0.0,
        pps_consecutive=0, pps_ok=0, pps_noise=0)
    cr._t6_last_chain_delay_ns = None
    cr._t6_disambiguation_ns = 0
    cr._t6_wrap_rejections = 0
    cr._t6_recent_raw = collections.deque(maxlen=CoreRecorderV2.T6_STEP_RECOVERY_WINDOW)
    cr._t6_last_locked_wall = None
    cr._t6_last_disambig_walk_wall = None
    cr._t6_shm = None
    cr._t6_channel_info = None
    cr.recorders = {}
    cr._t6_on_samples(MagicMock(), MagicMock(last_rtp_timestamp=0))
    assert cr._t6_last_disambig_walk_wall is None, "disambiguated a suspect lock"
    assert cr._t6_last_chain_delay_ns is None
