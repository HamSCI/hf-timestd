"""The acquisition gate, driven through the REAL `_t6_on_samples`.

⛔ WHY THIS FILE EXISTS.  Every other T6 test calls the acquisition path
or the battery directly, and that is precisely how the central defect of
the whole acceptance-criteria design survived nine reviews: nothing drove
the production INTERLEAVE.  In production the matched filter locks after
about ten edges (~10 s), the first fold block lands at K = 30 s, and the
battery needs three blocks (~90 s).  So acquisition was entered once, at
t ~ 10 s, with no fine estimate in hand -- and the caller latched
`_t6_last_chain_delay_ns` anyway, closing the gate for good.  A healthy
station could never acquire, and no test noticed.

So the tests here feed real BPSK samples, in batches, to the real
calibrator, the real fine stage, the real battery and the real authority,
through `CoreRecorderV2._t6_on_samples`.  Nothing is primed that
production would not hold at that instant.

The one substitution is the clock: `time.monotonic` is driven from the
sample index, so the walk throttle and the stuck timer see stream time
rather than test-runner wall time.  That is production-shaped -- the
stream is real-time on a station -- and it makes the run deterministic.
"""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_bpsk_pps_calibrator_mf import _make_bpsk_signal
from hf_timestd.core import core_recorder_v2 as crv2
from hf_timestd.core.core_recorder_v2 import CoreRecorderV2
from hf_timestd.core.bpsk_edge_fine_stage import BpskEdgeFineStage
from hf_timestd.core.bpsk_pps_calibrator_mf import BpskPpsCalibratorMF
from hf_timestd.core.t6_anchor_authority import T6AnchorAuthority

SR = 96000
BATCH = 1920
K = 30
# The fine stage's own fixture edge, shared with
# tests/test_t6_acceptance_integration.py.  It must stay near mid-fold:
# criterion 4's triangle-fidelity residual reads 0.0014 there and climbs
# to 0.49 for an edge at 10 % of the fold, because |T(e)| is only a
# symmetric tent when the flip sits near the middle.  Measured
# 2026-09-19 across 9600 / 19200 / 24000 / 30000 / 47916 samples.
EDGE = 47916.1672
NAMED_BASE = 1_700_000_000
# B4's TS-1 injection constant, 16.618 ms.  The matched filter reports a
# raw chain delay of ~499 ms here -- its estimate is ambiguous modulo the
# template period -- and the disambiguation walk resolves that against
# the named second to the true 16.618 ms, which is exactly the job §3
# gives it.  Without the walk the ±250 ms plausibility guard refuses the
# raw value, so this fixture exercises the resolution rather than
# stepping around it.
CHAIN_DELAY_SEC = 0.016618
# ⛔ THE STREAM DOES NOT START ON A SECOND, and the fixture must not
# pretend it does.  Two different phases are in play and conflating them
# makes the fixture unbuildable:
#
#   * the MF's chain delay is `edge_rtp % sample_rate` -- the edge's
#     phase in the UTC-aligned RTP second, 16.618 ms on B4;
#   * the fold-domain position is `(edge_rtp - registration) % p`, with
#     the registration re-derived per block from the RTP of that block's
#     first sample -- so it depends on where the STREAM began, and is
#     arbitrary.
#
# Feeding the array index as the RTP ties them together, which is not
# how a station behaves and which forced the edge to be 499 ms into the
# second (criterion 7 then refuses it as a wrap).  Offsetting the RTP
# origin separates them again: the edge lands 16.618 ms into the RTP
# second AND near mid-fold, which is what B4 actually presents.
RTP0 = 1000 * SR + int(round(CHAIN_DELAY_SEC * SR)) - int(EDGE)
# 77 dB-Hz: AI6VN's injected pilot.  The point here is the INTERLEAVE, not
# the C/N0 cliff -- tests/test_t6_acquisition_cliff.py owns that.
CN0_DB_HZ = 77.0


def _noise_std_for(cn0_db_hz: float) -> float:
    snr = 10 ** ((cn0_db_hz - 10 * math.log10(SR)) / 10.0)
    return 1.0 / math.sqrt(2.0 * snr)


def _rtp_to_utc(rtp, _channel_info):
    """radiod's mapping, as a healthy station presents it.

    Anchored so the edge sample maps to NAMED_BASE + CHAIN_DELAY_SEC:
    it arrives one chain delay after the second the pulse fired on,
    which is what a chain delay MEANS.  The mapping is therefore
    UTC-aligned with the RTP counter, as radiod's is.
    """
    return (float(NAMED_BASE) + (rtp - RTP0 - EDGE) / SR
            + CHAIN_DELAY_SEC)


def _recorder():
    """A CoreRecorderV2 wired with the REAL T6 stack.

    Only the surfaces that leave the process are stubbed: the anchor
    ledger (disk), the anomaly ring (memory, irrelevant here) and the
    chrony SHM (absent, as on a station with no SHM configured -- which
    is also the case that pins the sigma fix: that station's
    `_t6_chain_delay_history` never fills).
    """
    r = CoreRecorderV2.__new__(CoreRecorderV2)
    r._use_shared_multistream = True
    r._t6_first_sample_logged = True
    r._t5_pairing = None
    r._lb1421_probe = None
    r._t6_anomaly = None
    r._t6_zero_fill = None
    r._t6_shm = None
    r.recorders = {}
    r._t6_channel_info = SimpleNamespace(chain_delay_correction_ns=None,
                                         encoding=4)
    r._t6_config = {}
    r._t6_calibrator = BpskPpsCalibratorMF(
        sample_rate=SR, consecutive_required=10, edge_tolerance_samples=30)
    r._t6_fine_stage = BpskEdgeFineStage(sample_rate=SR, fold_seconds=K)
    r._t6_authority = T6AnchorAuthority(SR, 10_000)
    r._t6_authority_last_decision = None
    r._t6_battery = None
    r._t6_last_verdict = None
    r._t6_last_fine_est = None
    r._t6_native_anchor = None
    r._t6_suspect = ()
    r._t6_last_chain_delay_ns = None
    r._t6_mf_ever_locked = False
    r._t6_disambiguation_ns = 0
    r._t6_wrap_rejections = 0
    r._t6_initial_accept_rejections = 0
    r._t6_last_disambig_walk_wall = None
    r._t6_last_locked_wall = None
    r._t6_recent_raw = __import__('collections').deque(
        maxlen=CoreRecorderV2.T6_STEP_RECOVERY_WINDOW)
    r._t6_local_minus_source_history = __import__('collections').deque(
        maxlen=60)
    r._t6_chain_delay_history = __import__('collections').deque(maxlen=60)
    r._t6_rate_est = None
    r._t6_say_once_at = {}
    r._t6_epoch_last_obs = 0.0
    # Off-process surfaces only.
    r._t6_ledger_append = lambda *a, **k: None
    r._t6_capture_anomaly = lambda *a, **k: None
    r._t6_observe_channel_epochs = lambda *a, **k: None
    # The naming cascade is not what these tests exercise; §3.1 gives it
    # its own tolerance and tests/test_core_recorder_t6_fine_integration
    # covers it.  Name the second this edge truly belongs to.
    r._t6_name_integer_second = (
        lambda rtp: NAMED_BASE + int(round((rtp - RTP0 - EDGE) / SR)))
    r._fixture_latch_t = None
    return r


def _drive(r, duration_s, *, seed=11, cn0_db_hz=CN0_DB_HZ,
           drop_block=None, silence_after_s=None, rtp_base=None):
    """Feed `duration_s` of real BPSK through `_t6_on_samples`.

    `drop_block` (an index into the delivered fold blocks) makes the fine
    stage discard that one block, the way a registration-spread refusal
    does in the field.  Returns the recorder.
    """
    sig = _make_bpsk_signal(
        duration_s=duration_s, sample_rate=SR, edge_offset_samples=EDGE,
        noise_std=_noise_std_for(cn0_db_hz), seed=seed)
    if silence_after_s is not None:
        # The pulse source stops while samples keep flowing -- the shape
        # that pins the calibrator's cascade gate at pps_consecutive = 0.
        cut = int(silence_after_s * SR)
        rng = np.random.default_rng(seed + 1)
        n = len(sig) - cut
        sig = sig.copy()
        sig[cut:] = (rng.normal(0.0, _noise_std_for(cn0_db_hz), n)
                     + 1j * rng.normal(0.0, _noise_std_for(cn0_db_hz), n)
                     ).astype(sig.dtype)
    base = RTP0 if rtp_base is None else rtp_base
    clock = {"t": 0.0}
    real_finish = r._t6_fine_stage._finish_block
    state = {"n": 0}

    def _finish():
        est = real_finish()
        if est is not None:
            i = state["n"]
            state["n"] = i + 1
            if drop_block is not None and i == drop_block:
                return None
        return est

    r._t6_fine_stage._finish_block = _finish

    with mock.patch.object(crv2.time, 'monotonic',
                           side_effect=lambda: clock["t"]), \
            mock.patch('ka9q.rtp_recorder.rtp_to_utc',
                       side_effect=_rtp_to_utc):
        for i in range(0, len(sig) - BATCH + 1, BATCH):
            clock["t"] = i / SR
            q = SimpleNamespace(delivered_rtp_start=base + i,
                                last_rtp_timestamp=base + i, ssrc=1)
            r._t6_on_samples(sig[i:i + BATCH], q)
            # Stream time at which the acquisition gate first latched.
            # ⛔ The assertion that matters is WHEN, not whether: this
            # fixture's chain delay is physically plausible, so the
            # ±250 ms guard does not hold the gate and an unconditional
            # latch closes it at first MF lock -- about 10 s -- with the
            # same final state 100 s later.  Only the timestamp separates
            # the two.
            if (r._t6_last_chain_delay_ns is not None
                    and getattr(r, "_fixture_latch_t", None) is None):
                r._fixture_latch_t = clock["t"]
    return r


class TestTheProductionInterleave(unittest.TestCase):
    """⛔ The defect the whole task turned on.  Drive the real callback."""

    def test_the_matched_filter_locks_long_before_the_first_fold_block(self):
        """The premise, measured rather than asserted.

        If this ever stops holding -- if the MF took longer to lock than
        the battery takes to accumulate -- the acquisition ordering
        changes and the fix below stops being load-bearing.  Do not
        delete this: re-derive it.
        """
        r = _recorder()
        marks = []
        real = r.__class__._t6_disambiguate_via_external_reference

        def _spy(result):
            marks.append(("acquire", r._t6_last_fine_est is not None))
            return real(r, result)

        r._t6_disambiguate_via_external_reference = _spy
        _drive(r, 31.0)
        self.assertTrue(marks, "acquisition never attempted at all")
        self.assertFalse(
            marks[0][1],
            "the first acquisition attempt already had a fine estimate; "
            "the MF no longer locks ahead of the fold and this file's "
            "ordering premise needs re-deriving")

    def test_acquisition_eventually_captures_an_anchor(self):
        """⛔ THE TEST THAT WAS MISSING.

        The gate used to latch `_t6_last_chain_delay_ns` on the first
        locked cycle regardless of outcome, so this ran once at t ~ 10 s
        with no fine estimate, logged `no_fine_estimate`, and shut
        itself out.  120 s of a healthy 77 dB-Hz pilot produced no
        anchor at all.
        """
        r = _drive(_recorder(), 125.0)
        self.assertIsNotNone(
            r._t6_last_chain_delay_ns,
            "125 s of a healthy pilot and acquisition captured nothing; "
            "the gate closed before the battery could speak")
        self.assertIsNotNone(r._t6_native_anchor)
        self.assertTrue(r._t6_last_verdict.passed,
                        r._t6_last_verdict.failures)
        # The walk resolved the MF's raw estimate onto the named second.
        self.assertAlmostEqual(
            r._t6_last_chain_delay_ns / 1e9, CHAIN_DELAY_SEC, places=4)
        # ⛔ AND NOT BEFORE THE BATTERY COULD SPEAK.  The matched filter
        # locks around 10 s; the battery needs three fold blocks.  A gate
        # that latches on first lock reaches the same final state and
        # only this bound tells the two apart.
        self.assertIsNotNone(r._fixture_latch_t)
        # 3K minus one batch: the third block completes on the batch
        # that STARTS at 89.98 s, and the clock is read at batch start.
        self.assertGreaterEqual(
            r._fixture_latch_t, 3 * K - BATCH / SR,
            f"the gate latched at t={r._fixture_latch_t:.1f} s, before "
            f"the battery had its three blocks -- acquisition closed on "
            f"an opinion the battery had not yet formed")

    def test_the_gate_stays_open_until_the_anchor_is_taken(self):
        """31 s: the MF is locked and one fold block has landed, but the
        battery is two blocks short.  The gate must still be open."""
        r = _drive(_recorder(), 31.0)
        # ⚠ NOT `_t6_native_anchor`.  The authority captures its OWN
        # anchor on block 1 without consulting the battery (§3.2.2), so
        # an anchor existing here proves nothing about the gate.  The
        # latch is the gate.
        self.assertIsNone(
            r._t6_last_chain_delay_ns,
            "the gate latched while acquisition had captured nothing; "
            "nothing but an abnormal event will reopen it")

    def test_a_station_with_no_shm_still_acquires(self):
        """`_t6_shm` is None throughout, so `_t6_chain_delay_history`
        never receives a single reading -- the old sigma route could not
        produce a number on this station at any point in its life."""
        r = _drive(_recorder(), 125.0)
        self.assertEqual(len(r._t6_chain_delay_history), 0)
        self.assertIsNotNone(r._t6_last_chain_delay_ns)
        self.assertNotIn("sigma", r._t6_last_verdict.failures)

    def test_a_cold_start_is_never_marked_suspect(self):
        """"Still accumulating" is not "suspect"."""
        r = _drive(_recorder(), 125.0)
        self.assertEqual(
            r._t6_suspect, (),
            f"a healthy cold start reported T6 SUSPECT: {r._t6_suspect}")

    def test_the_open_gate_does_not_walk_on_every_batch(self):
        """⛔ An open gate is a flood risk.  The branch re-runs on every
        locked batch -- ~50/s at 96 kHz / 1920 samples -- and each walk
        asks the T5 probe and can emit log lines.  Holding it open for
        the ~90 s the battery needs therefore has to be throttled to
        T6_DISAMBIG_RETRY_INTERVAL_SEC, or the fix trades an unacquirable
        T6 for the persistent-condition flood this project keeps
        rediscovering.
        """
        r = _recorder()
        walks = []
        real = r.__class__._t6_disambiguate_via_external_reference
        r._t6_disambiguate_via_external_reference = (
            lambda result: (walks.append(1), real(r, result))[1])
        _drive(r, 95.0)
        ceiling = int(95.0 / CoreRecorderV2.T6_DISAMBIG_RETRY_INTERVAL_SEC) + 4
        self.assertLessEqual(
            len(walks), ceiling,
            f"{len(walks)} disambiguation walks in 95 s of stream; the "
            f"retry throttle is not holding the open gate")
        self.assertGreater(len(walks), 1, "the gate never retried at all")

    def test_one_dropped_fold_block_does_not_mark_the_ruler(self):
        """The fine stage discards block 2 the way a registration-spread
        refusal does.  The inter-block gap is then two folds, and the
        criterion must read that as a DROPPED BLOCK."""
        r = _drive(_recorder(), 155.0, drop_block=1)
        self.assertIsNotNone(r._t6_last_verdict)
        self.assertNotIn("ruler", r._t6_last_verdict.failures)
        self.assertGreaterEqual(
            r._t6_last_verdict.criteria.get("ruler_folds_skipped", 0), 1,
            "the fixture did not actually drop a block")


class TestStuckRecoveryStillReachesTheAccumulationWindow(unittest.TestCase):
    """⛔ The other reader of `_t6_last_chain_delay_ns` at this call site.

    Stuck-recovery used to gate on `_t6_last_chain_delay_ns is not None`,
    which was a fair proxy for "the MF has locked at least once" while
    that variable latched on the first locked cycle.  It no longer does
    -- it latches only once an anchor is captured, ~90 s later -- so a
    calibrator that pinned itself unlocked during the battery's
    accumulation window would never have been reset.  The condition now
    reads its own `_t6_mf_ever_locked`.
    """

    @staticmethod
    def _go_stuck(r, resets):
        """The calibrator stops producing a result while samples keep
        flowing -- the cascade-gate state the recovery exists for."""
        r._t6_calibrator.process_samples = lambda *a, **k: None
        r._t6_fine_stage = None
        quiet = np.zeros(BATCH, dtype=np.complex64)
        base_t = (r._t6_last_locked_wall or 0.0)
        clock = {"t": base_t + CoreRecorderV2.T6_STUCK_TIMEOUT_SEC + 1.0}
        with mock.patch.object(crv2.time, 'monotonic',
                               side_effect=lambda: clock["t"]), \
                mock.patch('ka9q.rtp_recorder.rtp_to_utc',
                           side_effect=_rtp_to_utc):
            for j in range(2):
                q = SimpleNamespace(delivered_rtp_start=RTP0 + j * BATCH,
                                    last_rtp_timestamp=RTP0 + j * BATCH,
                                    ssrc=1)
                r._t6_on_samples(quiet, q)
        return resets

    @staticmethod
    def _spy_reset(r, resets):
        real = r._t6_calibrator.reset
        r._t6_calibrator.reset = lambda: (resets.append(1), real())[1]

    def test_a_calibrator_stuck_unlocked_before_acquisition_is_reset(self):
        r = _drive(_recorder(), 20.0)     # MF locked, battery far short
        self.assertTrue(r._t6_mf_ever_locked)
        self.assertIsNone(r._t6_last_chain_delay_ns,
                          "the fixture acquired; it is not testing the "
                          "pre-acquisition window any more")
        resets = []
        self._spy_reset(r, resets)
        self._go_stuck(r, resets)
        self.assertTrue(
            resets,
            "the calibrator sat stuck unlocked past "
            f"{CoreRecorderV2.T6_STUCK_TIMEOUT_SEC:.0f} s before any "
            "anchor existed and stuck-recovery never fired")

    def test_a_station_that_never_locked_is_not_reset(self):
        """The half stuck-recovery must not lose: nothing to recover
        from yet."""
        r = _recorder()
        self.assertFalse(r._t6_mf_ever_locked)
        resets = []
        self._spy_reset(r, resets)
        self._go_stuck(r, resets)
        self.assertFalse(resets)


if __name__ == "__main__":
    unittest.main()
