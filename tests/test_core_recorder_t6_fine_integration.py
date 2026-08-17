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
    r._t5_pairing = None
    r._t6_native_anchor = None
    r._t6_authority = T6AnchorAuthority(SR, 10_000)
    r._t6_authority_last_decision = None
    return r


def est(rtp=1_000_000, offset=43_181.0, sub=0.0):
    return FineEdgeEstimate(
        edge_offset_samples=offset, edge_rtp=rtp, edge_subsample=sub,
        n_seconds_folded=30, plateau_amplitude=30.0, fit_rms=0.05,
    )


def phase(e):
    """RTP-domain edge phase — the domain the authority compares in
    (final-review Finding 1).  ``edge_offset_samples`` is the fine
    stage's fold-domain diagnostic and is deliberately NOT the same
    number."""
    return (e.edge_rtp + e.edge_subsample) % SR


def nmea_recorder(now_wall, now_mono=5_000.0, arrival_rtp=1_000_000,
                  pps_utc_sec=SECOND, arrival_mono=None):
    """Recorder wired with a fresh NMEA reading and a real T5RtpPairing
    driven by fake clocks, so the NMEA naming path is exercised end to
    end without touching the host clock."""
    from hf_timestd.core.t5_rtp_pairing import T5RtpPairing
    r = bare_recorder()
    r._lb1421_probe = SimpleNamespace(
        get_latest=lambda **kw: SimpleNamespace(pps_utc_sec=pps_utc_sec))
    r._t5_pairing = T5RtpPairing(time_fn=lambda: now_wall,
                                 mono_fn=lambda: now_mono,
                                 source="t6")
    r._t5_pairing.note_arrival(
        arrival_rtp,
        mono=now_mono if arrival_mono is None else arrival_mono)
    r._t6_fine_stage = SimpleNamespace(sample_rate=SR, blocks_discarded=0)
    r._t6_calibrator = SimpleNamespace(sample_rate=SR)
    return r


class TestNaming:
    """Final-review Finding 2.  The old NMEA branch computed
    ``pps_utc_sec + round(wall − pps_utc_sec)``, which for an integer
    ``pps_utc_sec`` is identically ``round(wall)``: NMEA contributed
    nothing, and a radiod-pair ``wall`` error beyond ±0.5 s (seen in
    fleet history) named the wrong second undetected.  These tests make
    the NMEA path *distinguishable* from ``round(wall)`` by putting the
    two on opposite sides of a second boundary."""

    def test_names_from_nmea_when_probe_fresh(self):
        # NMEA + arrival pairing put the edge at SECOND + 0.30 s.
        r = nmea_recorder(now_wall=float(SECOND) + 0.30)
        with patch('ka9q.rtp_recorder.rtp_to_utc',
                   return_value=float(SECOND) + 0.080):
            assert r._t6_name_integer_second(1_000_000) == SECOND

    def test_nmea_beats_a_radiod_pair_wall_in_the_wrong_second(self, caplog):
        # The distinguishing case: the radiod-pair wall estimate is
        # 0.65 s past the NMEA-derived edge UTC, so round(wall) would
        # name SECOND+1.  The NMEA-attested second must win, and the
        # disagreement must be reported (spec §6 invariant 5).
        r = nmea_recorder(now_wall=float(SECOND) + 0.30)
        with caplog.at_level("WARNING"):
            with patch('ka9q.rtp_recorder.rtp_to_utc',
                       return_value=float(SECOND) + 0.95):
                assert r._t6_name_integer_second(1_000_000) == SECOND
        assert any("disagrees" in m for m in caplog.messages)
        # Same wall, no NMEA → the fallback names the wrong second, which
        # is precisely what the old code did unconditionally.
        r2 = bare_recorder()
        with patch('ka9q.rtp_recorder.rtp_to_utc',
                   return_value=float(SECOND) + 0.95):
            assert r2._t6_name_integer_second(1_000_000) == SECOND + 1

    def test_nmea_path_needs_no_radiod_wallclock_at_all(self):
        # rtp_to_wallclock is the quantity the inversion exists to
        # bypass; a total failure of it must not stop T6 naming.
        r = nmea_recorder(now_wall=float(SECOND) + 0.30)
        with patch('ka9q.rtp_recorder.rtp_to_utc', return_value=None):
            assert r._t6_name_integer_second(1_000_000) == SECOND

    def test_edge_before_the_arrival_is_named_by_rtp_arithmetic(self):
        # Edge 0.6 s of RTP counter before the paired arrival, arrival
        # wall SECOND + 1.30 → edge UTC SECOND + 0.70 → second SECOND+1.
        r = nmea_recorder(now_wall=float(SECOND) + 1.30,
                          arrival_rtp=1_000_000 + int(0.6 * SR))
        with patch('ka9q.rtp_recorder.rtp_to_utc', return_value=None):
            assert r._t6_name_integer_second(1_000_000) == SECOND + 1

    def test_nmea_residual_beyond_0p4s_returns_none(self):
        # Edge lands 0.45 s from any integer second — outside the ±0.4 s
        # margin of invariant 3, so naming refuses rather than guess.
        r = nmea_recorder(now_wall=float(SECOND) + 0.45)
        with patch('ka9q.rtp_recorder.rtp_to_utc', return_value=None):
            assert r._t6_name_integer_second(1_000_000) is None

    def test_stale_arrival_falls_back_to_wall(self):
        # Arrival older than T5RtpPairing.ARRIVAL_MAX_AGE_S — the pairing
        # is refused and the cascade drops to the radiod-pair estimate.
        r = nmea_recorder(now_wall=float(SECOND) + 0.30,
                          arrival_mono=5_000.0 - 60.0)
        with patch('ka9q.rtp_recorder.rtp_to_utc',
                   return_value=float(SECOND) - 0.120):
            assert r._t6_name_integer_second(1_000_000) == SECOND
        with patch('ka9q.rtp_recorder.rtp_to_utc', return_value=None):
            assert r._t6_name_integer_second(1_000_000) is None

    def test_host_nmea_disagreement_refuses_the_nmea_pairing(self):
        # Host wall 30 s ahead of the NMEA second: outside the
        # attestation window, so the NMEA branch refuses (it would
        # otherwise emit a poisoned name) and the fallback answers.
        r = nmea_recorder(now_wall=float(SECOND) + 30.30)
        with patch('ka9q.rtp_recorder.rtp_to_utc',
                   return_value=float(SECOND) + 0.080):
            assert r._t6_name_integer_second(1_000_000) == SECOND

    def test_falls_back_to_wall_rounding_without_probe(self):
        r = bare_recorder()
        with patch('ka9q.rtp_recorder.rtp_to_utc',
                   return_value=float(SECOND) - 0.120):
            assert r._t6_name_integer_second(1_000_000) == SECOND

    def test_residual_beyond_0p4s_returns_none(self):
        r = bare_recorder()
        with patch('ka9q.rtp_recorder.rtp_to_utc',
                   return_value=float(SECOND) + 0.45):
            assert r._t6_name_integer_second(1_000_000) is None

    def test_wallclock_unavailable_returns_none(self):
        r = bare_recorder()
        with patch('ka9q.rtp_recorder.rtp_to_utc', return_value=None):
            assert r._t6_name_integer_second(1_000_000) is None


class TestAnchorOwnership:
    def test_authoritative_decision_installs_t6_anchor(self, caplog):
        r = bare_recorder()
        r._t6_rate_reset = lambda reason: None
        d = r._t6_authority.on_fine_estimate(est(), phase(est()), SECOND)
        with caplog.at_level("WARNING"):
            r._t6_apply_authority_decision(d)
        assert r._t6_native_anchor is d.anchor
        assert r._t6_native_anchor.captured_via_tier == "T6"
        # transition ACQUIRING→AUTHORITATIVE is loud
        assert any("AUTHORITATIVE" in m for m in caplog.messages)

    def test_unlock_invalidates_anchor_loudly(self, caplog):
        r = bare_recorder()
        r._t6_rate_reset = lambda reason: None
        d1 = r._t6_authority.on_fine_estimate(est(), phase(est()), SECOND)
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
        d1 = r._t6_authority.on_fine_estimate(est(), phase(est()), SECOND)
        r._t6_apply_authority_decision(d1)
        # Edge moved one sample (10.4 µs) in the RTP domain — where the
        # periodicity invariant lives (final-review Finding 1).
        moved = est(rtp=1_000_001)
        d2 = r._t6_authority.on_fine_estimate(
            moved, phase(moved), SECOND + 30)
        with caplog.at_level("WARNING"):
            r._t6_apply_authority_decision(d2)
        assert r._t6_native_anchor is d1.anchor
        assert any("DEGRADED" in m and "edge_period" in m
                   for m in caplog.messages)

    def test_same_state_clean_updates_anchor_without_warning(self, caplog):
        r = bare_recorder()
        r._t6_rate_reset = lambda reason: None
        d1 = r._t6_authority.on_fine_estimate(est(), phase(est()), SECOND)
        r._t6_apply_authority_decision(d1)
        d2 = r._t6_authority.on_fine_estimate(
            est(rtp=1_000_000 + 30 * SR), phase(est()), SECOND + 30)
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
        d1 = r._t6_authority.on_fine_estimate(est(), phase(est()), SECOND)
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
        d1 = r._t6_authority.on_fine_estimate(est(), phase(est()), SECOND)
        r._t6_apply_authority_decision(d1)
        r._t6_last_chain_delay_ns = 123_456
        r._t6_disambiguation_ns = 789
        r._t6_recent_raw = deque([1, 2, 3])
        # Edge moved one sample (10.4 µs) in the RTP domain — where the
        # periodicity invariant lives (final-review Finding 1).
        moved = est(rtp=1_000_001)
        d2 = r._t6_authority.on_fine_estimate(
            moved, phase(moved), SECOND + 30)
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
    does not clear its own internal ``_coarse_offset_rtp`` — so a
    stale-window fine estimate must never reach the authority while the
    calibrator has no live coarse offset, or the authority can claim
    AUTHORITATIVE with the fine_coarse invariant silently inert."""

    def test_authority_not_consulted_when_coarse_is_none(self):
        cr = _bare_on_samples_recorder()
        cr._t6_calibrator._chain_delay_samples = None  # MF unlocked/reset
        cr._t6_fine_stage = MagicMock()
        cr._t6_fine_stage.process_samples.return_value = est()
        cr._t6_authority = MagicMock()
        cr._t6_authority.on_tick.return_value = None  # liveness: nothing due

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
        cr._t6_authority.on_tick.return_value = None  # liveness: nothing due

        samples = MagicMock()
        quality = MagicMock(last_rtp_timestamp=0)
        cr._t6_on_samples(samples, quality)

        cr._t6_authority.on_fine_estimate.assert_called_once()
        args = cr._t6_authority.on_fine_estimate.call_args.args
        assert args[1] == 43_181.0


class TestAuthorityStatus:
    def test_status_none_when_authority_absent(self):
        r = CoreRecorderV2.__new__(CoreRecorderV2)
        r._t6_authority = None
        assert r._t6_authority_status() is None

    def test_status_reflects_authoritative_state(self):
        r = bare_recorder()
        r._t6_rate_reset = lambda reason: None
        r._t6_fine_stage = SimpleNamespace(blocks_discarded=2)
        r._compute_rtp_to_utc_offset_ns = lambda: -80_000_000
        d = r._t6_authority.on_fine_estimate(est(), phase(est()), SECOND)
        r._t6_apply_authority_decision(d)
        s = r._t6_authority_status()
        assert s['state'] == "AUTHORITATIVE"
        assert s['violations'] == []
        assert s['delay_budget_ns'] == 10_000
        assert s['anchor_tier'] == "T6"
        assert s['blocks_discarded'] == 2
        assert s['t6_vs_radiod_pair_ms'] == pytest.approx(-80.0)
        # Latched by the naming path; None until it has run.
        assert s['naming_vs_radiod_pair_s'] is None
        r._t6_naming_vs_radiod_pair_s = 0.65
        assert r._t6_authority_status()['naming_vs_radiod_pair_s'] == 0.65

    def test_status_before_first_estimate(self):
        r = bare_recorder()
        r._t6_fine_stage = SimpleNamespace(blocks_discarded=0)
        r._compute_rtp_to_utc_offset_ns = lambda: None
        s = r._t6_authority_status()
        assert s['state'] == "ACQUIRING"
        assert s['anchor_tier'] is None
        assert s['t6_vs_radiod_pair_ms'] is None


class TestLivenessWiring:
    """Finding 3 wiring: ``on_tick`` must run on every batch, including
    the batch where the fine-stage block itself blew up — a swallowed
    exception is one of the ways estimates stop arriving."""

    def _cr(self):
        cr = _bare_on_samples_recorder()
        cr._t6_calibrator._chain_delay_samples = None
        cr._t6_fine_stage = MagicMock()
        cr._t6_fine_stage.process_samples.return_value = None
        cr._t6_authority = MagicMock()
        cr._t6_authority.on_tick.return_value = None
        return cr

    def test_tick_called_on_every_batch(self):
        cr = self._cr()
        cr._t6_on_samples(MagicMock(), MagicMock(last_rtp_timestamp=0))
        cr._t6_authority.on_tick.assert_called_once()

    def test_tick_called_even_when_the_fine_stage_raises(self):
        cr = self._cr()
        cr._t6_fine_stage.process_samples.side_effect = RuntimeError("boom")
        cr._t6_on_samples(MagicMock(), MagicMock(last_rtp_timestamp=0))
        cr._t6_authority.on_tick.assert_called_once()

    def test_stale_decision_is_applied(self):
        cr = self._cr()
        applied = []
        cr._t6_apply_authority_decision = applied.append
        sentinel = object()
        cr._t6_authority.on_tick.return_value = sentinel
        cr._t6_on_samples(MagicMock(), MagicMock(last_rtp_timestamp=0))
        assert applied == [sentinel]


class TestAuthorityStateReachesTheProbe:
    """Spec §4 (final-review Finding 4): authority transitions must
    reach authority.json, not just the recorder's own status file.  The
    only block ``BpskPpsProbe`` reads is ``t6_pps``, so the state rides
    there and the probe forwards it in ``ProbeResult.detail``."""

    def _status(self, tmp_path, **extra):
        import json
        from datetime import datetime, timezone
        p = tmp_path / "core-recorder-status.json"
        block = {
            'enabled': True, 'locked': True, 'pps_consecutive': 20,
            'local_minus_source_ns': 2384,
        }
        block.update(extra)
        p.write_text(json.dumps({
            'timestamp': datetime.now(timezone.utc).isoformat(),
            't6_pps': block,
        }))
        return p

    def test_probe_forwards_authority_state_and_violations(self, tmp_path):
        # NOTE: this used to assert available is True on a DEGRADED
        # producer, which locked hf-timestd#14 in as correct.  The point
        # of the test is that the keys are FORWARDED into detail, so it
        # now exercises that on the state where T6 is genuinely usable;
        # TestT6MustNotBeOfferedWhileUnlocked covers the rest.
        from hf_timestd.core.bpsk_pps_probe import BpskPpsProbe
        p = self._status(tmp_path, authority_state="AUTHORITATIVE",
                         authority_violations=[])
        r = BpskPpsProbe(status_path=p).poll()
        assert r.available is True
        assert r.detail["authority_state"] == "AUTHORITATIVE"
        assert r.detail["authority_violations"] == []

    def test_probe_omits_the_keys_on_a_producer_without_them(self, tmp_path):
        from hf_timestd.core.bpsk_pps_probe import BpskPpsProbe
        r = BpskPpsProbe(status_path=self._status(tmp_path)).poll()
        assert "authority_state" not in r.detail
        assert "authority_violations" not in r.detail


class TestT6MustNotBeOfferedWhileUnlocked:
    """hf-timestd#14 — T6 coasted instead of withdrawing.

    ``locked`` is matched-filter ACQUISITION and stays true straight
    through a Costas phase excursion, so the probe kept offering T6 as
    an authority while the calibrator was accepting no edges and coasting
    on a stale chain delay.  Both signals it needed (``costas_locked``,
    ``authority_state``) were already published in the same block and
    simply never consumed.

    Observed on AC0G-B4 2026-08-16 under thunderstorm sferics:
    ``t_level_active: "T6"`` published alongside
    ``t6_authority_state: "DEGRADED"``, while chrony marked HPPS a
    falseticker at Std Dev 177 ms.
    """

    def _status(self, tmp_path, **extra):
        import json
        from datetime import datetime, timezone
        p = tmp_path / "core-recorder-status.json"
        block = {
            'enabled': True, 'locked': True, 'pps_consecutive': 20,
            'local_minus_source_ns': 2384,
        }
        block.update(extra)
        p.write_text(json.dumps({
            'timestamp': datetime.now(timezone.utc).isoformat(),
            't6_pps': block,
        }))
        return p

    def _poll(self, tmp_path, **extra):
        from hf_timestd.core.bpsk_pps_probe import BpskPpsProbe
        return BpskPpsProbe(status_path=self._status(tmp_path, **extra)).poll()

    def test_costas_unlocked_is_not_available(self, tmp_path):
        r = self._poll(tmp_path, costas_locked=False)
        assert r.available is False
        assert "costas" in r.reason.lower()

    def test_costas_locked_is_available(self, tmp_path):
        assert self._poll(tmp_path, costas_locked=True).available is True

    def test_legacy_calibrator_without_a_costas_loop_stays_permissive(
            self, tmp_path):
        # None = non-MF calibrator, which has no Costas loop at all.
        # Silencing it would be a regression, not a fix.
        assert self._poll(tmp_path, costas_locked=None).available is True

    @pytest.mark.parametrize("state", ["DEGRADED", "UNLOCKED", "ACQUIRING"])
    def test_non_authoritative_anchor_is_not_available(self, tmp_path, state):
        r = self._poll(tmp_path, authority_state=state)
        assert r.available is False
        assert state in r.reason

    def test_authoritative_anchor_is_available(self, tmp_path):
        assert self._poll(
            tmp_path, authority_state="AUTHORITATIVE").available is True

    def test_producer_without_the_authority_key_stays_permissive(
            self, tmp_path):
        # Additive key; older producers and a disabled fine stage omit
        # it.  Absent means "not reported", not "unavailable".
        assert self._poll(tmp_path).available is True

    def test_costas_gate_fires_even_when_the_authority_looks_healthy(
            self, tmp_path):
        # The two gates are independent: the authority can still read
        # AUTHORITATIVE for a dwell period after the carrier drops.
        r = self._poll(tmp_path, costas_locked=False,
                       authority_state="AUTHORITATIVE")
        assert r.available is False


class TestHppsWithdrawsInsteadOfCoasting:
    """hf-timestd#14 — the SHM push gate.

    ``_t6_native_anchor is not None`` was the only guard on feeding
    chrony.  That is insufficient: the UNLOCKED handler nulls the anchor,
    but the coarse cascade immediately re-captures one via T5 from the
    same MF edge, so the guard reopens while the carrier is still lost.
    Measured on AC0G-B4 2026-08-16: pushes continued through DEGRADED and
    UNLOCKED windows with per-push offsets scattered over -37..+54 ms.
    """

    def _cr(self, costas_locked=True, state=T6AuthorityState.AUTHORITATIVE):
        cr = CoreRecorderV2.__new__(CoreRecorderV2)
        cr._t6_calibrator = SimpleNamespace(costas_locked=costas_locked)
        cr._t6_authority = SimpleNamespace(state=state)
        return cr

    def test_publishes_when_authoritative_and_carrier_locked(self):
        assert self._cr()._t6_hpps_publishable() is True

    @pytest.mark.parametrize("state", [
        T6AuthorityState.DEGRADED,
        T6AuthorityState.UNLOCKED,
        T6AuthorityState.ACQUIRING,
    ])
    def test_withdraws_while_the_anchor_is_not_authoritative(self, state):
        assert self._cr(state=state)._t6_hpps_publishable() is False

    def test_withdraws_while_the_costas_loop_is_unlocked(self):
        assert self._cr(costas_locked=False)._t6_hpps_publishable() is False

    def test_costas_gate_is_independent_of_the_authority_gate(self):
        # The authority holds AUTHORITATIVE for a dwell period after the
        # carrier drops; the push must stop at the first of the two.
        cr = self._cr(costas_locked=False,
                      state=T6AuthorityState.AUTHORITATIVE)
        assert cr._t6_hpps_publishable() is False

    def test_legacy_calibrator_without_a_costas_loop_still_publishes(self):
        # costas_locked None = non-MF calibrator; only an explicit False
        # is a carrier-recovery fault.
        assert self._cr(costas_locked=None)._t6_hpps_publishable() is True

    def test_missing_authority_object_does_not_silence_the_push(self):
        cr = CoreRecorderV2.__new__(CoreRecorderV2)
        cr._t6_calibrator = SimpleNamespace(costas_locked=True)
        cr._t6_authority = None
        assert cr._t6_hpps_publishable() is True

    def test_transition_is_logged_once_each_way_not_per_push(self, caplog):
        import logging
        cr = self._cr()
        with caplog.at_level(logging.WARNING):
            for _ in range(5):
                cr._t6_hpps_publishable()
            cr._t6_calibrator.costas_locked = False
            for _ in range(5):
                cr._t6_hpps_publishable()
            cr._t6_calibrator.costas_locked = True
            for _ in range(5):
                cr._t6_hpps_publishable()
        msgs = [r.getMessage() for r in caplog.records]
        withdrawn = [m for m in msgs if "WITHDRAWN" in m]
        resumed = [m for m in msgs if "RESUMED" in m]
        assert len(withdrawn) == 1, "withdrawal must not log per push"
        assert len(resumed) == 1, "resumption must not log per push"

    def test_a_healthy_first_call_logs_nothing(self, caplog):
        # Publishing is the steady state — there is nothing to "resume"
        # on the first call, and a spurious RESUMED at every startup
        # would train operators to ignore the line that matters.
        import logging
        with caplog.at_level(logging.WARNING):
            assert self._cr()._t6_hpps_publishable() is True
        assert not [r for r in caplog.records if "HPPS" in r.getMessage()]

    def test_a_faulted_first_call_still_reports(self, caplog):
        import logging
        cr = self._cr(state=T6AuthorityState.UNLOCKED)
        with caplog.at_level(logging.WARNING):
            cr._t6_hpps_publishable()
        assert any("WITHDRAWN" in r.getMessage() for r in caplog.records)

    def test_withdrawal_names_the_reason(self, caplog):
        import logging
        cr = self._cr(state=T6AuthorityState.UNLOCKED)
        with caplog.at_level(logging.WARNING):
            cr._t6_hpps_publishable()
        assert any("UNLOCKED" in r.getMessage() for r in caplog.records)


class TestHoldoverCoastsInsteadOfWithdrawing:
    """Growing-sigma coast (successor to the #14 abrupt withdraw).

    Losing carrier lock stops us LEARNING; it does not invalidate the
    anchor.  The RTP counter is GPSDO-disciplined, so a frozen anchor
    keeps labelling correctly and only our uncertainty grows — measured
    at 0.0004 ppm on AC0G-B4, i.e. 1.44 us/hour.  Going dark through a
    six-hour storm throws away a clock that drifted 8.6 us.
    """

    def _floor(self, offset_s=100.0, sigma_ns=800_000.0):
        return SimpleNamespace(
            offset_s=offset_s, sigma_ns=sigma_ns, n=110, span_s=2.0)

    def _cr(self, costas_locked=True, state=T6AuthorityState.AUTHORITATIVE,
            anchor=object(), rate_sigma_ppm=0.0004):
        cr = CoreRecorderV2.__new__(CoreRecorderV2)
        cr._t6_calibrator = SimpleNamespace(costas_locked=costas_locked)
        cr._t6_authority = SimpleNamespace(state=state)
        cr._t6_native_anchor = anchor
        cr._t6_rate_est = SimpleNamespace(
            current=SimpleNamespace(sigma_ppm=rate_sigma_ppm))
        return cr

    def test_healthy_state_is_live(self):
        cr = self._cr()
        mode, _sigma, _r = cr._t6_publish_mode(self._floor(), 1000.0)
        assert mode == "live"

    def test_degraded_with_a_frozen_anchor_coasts(self):
        cr = self._cr(state=T6AuthorityState.DEGRADED)
        cr._t6_publish_mode(self._floor(), 1000.0)          # freeze point
        mode, sigma, _r = cr._t6_publish_mode(self._floor(), 1060.0)
        assert mode == "holdover"
        assert sigma == pytest.approx(800_000.0, rel=1e-3)

    def test_a_storm_length_coast_barely_widens_sigma(self):
        """Six hours of sferics must not meaningfully degrade the claim."""
        cr = self._cr(state=T6AuthorityState.DEGRADED)
        cr._t6_publish_mode(self._floor(), 1000.0)
        mode, sigma, _r = cr._t6_publish_mode(self._floor(), 1000.0 + 6 * 3600)
        assert mode == "holdover"
        assert sigma == pytest.approx(800_000.0, rel=1e-3)
        assert sigma > 800_000.0

    def test_carrier_loss_alone_coasts_rather_than_going_dark(self):
        """The #14 case, now handled by coasting: the authority can read
        AUTHORITATIVE for a dwell after the carrier drops."""
        cr = self._cr(costas_locked=False)
        cr._t6_publish_mode(self._floor(), 1000.0)
        mode, _s, _r = cr._t6_publish_mode(self._floor(), 1030.0)
        assert mode == "holdover"

    def test_no_frozen_anchor_still_refuses(self):
        cr = self._cr(state=T6AuthorityState.UNLOCKED, anchor=None)
        mode, _s, reason = cr._t6_publish_mode(self._floor(), 1000.0)
        assert mode is None
        assert "anchor" in reason

    def test_rtp_rebase_refuses_however_short_the_coast(self):
        cr = self._cr(state=T6AuthorityState.DEGRADED)
        cr._t6_publish_mode(self._floor(offset_s=100.0), 1000.0)
        mode, _s, reason = cr._t6_publish_mode(
            self._floor(offset_s=3712.5), 1001.0)
        assert mode is None
        assert "rtp" in reason.lower()

    def test_returning_to_health_clears_the_freeze_point(self):
        cr = self._cr(state=T6AuthorityState.DEGRADED)
        cr._t6_publish_mode(self._floor(), 1000.0)
        cr._t6_authority = SimpleNamespace(
            state=T6AuthorityState.AUTHORITATIVE)
        assert cr._t6_publish_mode(self._floor(), 1010.0)[0] == "live"
        # A later coast must measure from the NEW freeze, not the old one.
        cr._t6_authority = SimpleNamespace(state=T6AuthorityState.DEGRADED)
        cr._t6_publish_mode(self._floor(), 1020.0)
        _m, sigma, _r = cr._t6_publish_mode(self._floor(), 1021.0)
        assert sigma == pytest.approx(800_000.0, rel=1e-6)

    def test_publishable_stays_true_for_live_and_holdover(self):
        assert self._cr()._t6_hpps_publishable() is True

    def test_publishable_false_without_an_anchor(self):
        cr = self._cr(state=T6AuthorityState.UNLOCKED, anchor=None)
        assert cr._t6_hpps_publishable() is False


class TestHoldoverPushNamesTheSecondFromTheAnchor:
    """The coast must never build its pair from an edge detected during
    the outage — that is precisely hf-timestd#14.  It names the second
    from the frozen anchor via the arrival floor instead."""

    def _cr(self, offset_s=100.0, mono=900.5, sigma_ns=800_000.0):
        cr = CoreRecorderV2.__new__(CoreRecorderV2)
        cr._t6_native_anchor = SimpleNamespace(chain_delay_ns=0)
        cr._t6_arrival_floor = SimpleNamespace(
            estimate=lambda m, record=True: SimpleNamespace(
                offset_s=offset_s, sigma_ns=sigma_ns, n=110, span_s=2.0))
        cr._t5_pairing = SimpleNamespace(now_mono=lambda: mono)
        cr._t6_holdover_sigma_ns = sigma_ns
        cr.pushes = []
        cr._t6_shm = SimpleNamespace(
            update=lambda **kw: cr.pushes.append(kw))
        return cr

    def test_reference_time_is_the_named_second_not_an_edge(self):
        cr = self._cr(offset_s=100.0, mono=900.5)
        cr._t6_push_holdover()
        assert len(cr.pushes) == 1
        assert cr.pushes[0]["reference_time"] == 1000.0

    def test_pushes_once_per_second_of_coast(self):
        cr = self._cr(offset_s=100.0, mono=900.5)
        cr._t6_push_holdover()
        cr._t6_push_holdover()
        cr._t6_push_holdover()
        assert len(cr.pushes) == 1

    def test_advances_when_the_named_second_advances(self):
        cr = self._cr(offset_s=100.0, mono=900.5)
        cr._t6_push_holdover()
        cr._t5_pairing = SimpleNamespace(now_mono=lambda: 901.5)
        cr._t6_push_holdover()
        assert [p["reference_time"] for p in cr.pushes] == [1000.0, 1001.0]

    def test_precision_reflects_the_growing_holdover_sigma(self):
        tight = self._cr(sigma_ns=800_000.0)
        tight._t6_push_holdover()
        wide = self._cr(sigma_ns=4_000_000.0)
        wide._t6_push_holdover()
        assert wide.pushes[0]["precision"] > tight.pushes[0]["precision"]

    def test_silent_without_an_arrival_floor(self):
        cr = self._cr()
        cr._t6_arrival_floor = SimpleNamespace(
            estimate=lambda m, record=True: None)
        cr._t6_push_holdover()
        assert cr.pushes == []


class TestCoastTransitionsAreLoud:
    """expose-don't-correct: dropping from a measured clock to an
    extrapolated one is a state change an operator must see."""

    def _cr(self, state=T6AuthorityState.DEGRADED):
        cr = CoreRecorderV2.__new__(CoreRecorderV2)
        cr._t6_calibrator = SimpleNamespace(costas_locked=True)
        cr._t6_authority = SimpleNamespace(state=state)
        cr._t6_native_anchor = SimpleNamespace(chain_delay_ns=0)
        cr._t6_rate_est = SimpleNamespace(
            current=SimpleNamespace(sigma_ppm=0.0004))
        cr._t6_arrival_floor = SimpleNamespace(
            estimate=lambda m, record=True: SimpleNamespace(
                offset_s=100.0, sigma_ns=800_000.0, n=110, span_s=2.0))
        cr._t5_pairing = SimpleNamespace(now_mono=lambda: 1000.0)
        return cr

    def test_entering_a_coast_says_so_once(self, caplog):
        import logging
        cr = self._cr()
        with caplog.at_level(logging.WARNING):
            cr._t6_hpps_publishable()
            cr._t6_hpps_publishable()
        coasting = [r for r in caplog.records
                    if "COASTING" in r.getMessage()]
        assert len(coasting) == 1
        assert "sigma" in coasting[0].getMessage()

    def test_leaving_a_coast_says_so(self, caplog):
        import logging
        cr = self._cr()
        cr._t6_hpps_publishable()
        cr._t6_authority = SimpleNamespace(
            state=T6AuthorityState.AUTHORITATIVE)
        with caplog.at_level(logging.WARNING):
            cr._t6_hpps_publishable()
        assert any("coast ended" in r.getMessage() for r in caplog.records)
