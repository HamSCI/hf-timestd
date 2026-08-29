"""Fine stage acquiring its own edge with no matched-filter seed."""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_bpsk_pps_calibrator_mf import _make_bpsk_signal
from hf_timestd.core.bpsk_edge_fine_stage import BpskEdgeFineStage

SR = 96000
BATCH = 1920
EDGE = 47916.1672


def _noise_std_for(cn0_db_hz: float) -> float:
    """Per-component complex-noise sigma giving this C/N0 at SR."""
    snr = 10 ** ((cn0_db_hz - 10 * math.log10(SR)) / 10.0)
    return 1.0 / math.sqrt(2.0 * snr)


def _drive(stage, cn0_db_hz=55.0, duration_s=31.0, edge=EDGE, seed=11):
    sig = _make_bpsk_signal(
        duration_s=duration_s, sample_rate=SR, edge_offset_samples=edge,
        noise_std=_noise_std_for(cn0_db_hz), seed=seed,
    )
    last = None
    for i in range(0, len(sig), BATCH):
        est = stage.process_samples(sig[i:i + BATCH], i)
        if est is not None:
            last = est
    return last


class TestBootstrapAcquisition(unittest.TestCase):

    def test_produces_an_estimate_with_no_coarse_ever_set(self):
        """Today this returns None forever -- the coarse seed is a veto
        held by the stage that fails first."""
        stage = BpskEdgeFineStage(sample_rate=SR)
        est = _drive(stage)
        self.assertIsNotNone(est)

    def test_bootstrapped_estimate_lands_on_the_edge(self):
        stage = BpskEdgeFineStage(sample_rate=SR)
        est = _drive(stage)
        err_samples = (est.edge_offset_samples - EDGE + SR / 2) % SR - SR / 2
        self.assertLess(abs(err_samples) / SR * 1e6, 100.0)

    def test_mode_is_reported_as_bootstrap(self):
        stage = BpskEdgeFineStage(sample_rate=SR)
        _drive(stage)
        self.assertEqual(stage._last_search_mode, "bootstrap")

    def test_a_coarse_seed_still_takes_precedence(self):
        """Regression: seeded behaviour is unchanged, and is preferred
        because a ±6 ms window is more selective than a whole second."""
        stage = BpskEdgeFineStage(sample_rate=SR)
        stage.set_coarse_offset_samples(EDGE)
        est = _drive(stage)
        self.assertIsNotNone(est)
        self.assertEqual(stage._last_search_mode, "seeded")


class TestTrackingAndConfirmation(unittest.TestCase):

    def test_does_not_self_seed_before_confirmation(self):
        """One bootstrap block is not enough. Self-seeding a wrong
        crossing is how a displaced reference gets cemented -- the same
        failure STEP_CONFIRM_EDGES guards in the MF."""
        stage = BpskEdgeFineStage(sample_rate=SR)
        _drive(stage, duration_s=31.0)          # exactly one fold block
        self.assertIsNone(stage._own_offset_rtp)

    def test_self_seeds_after_confirming_blocks_agree(self):
        stage = BpskEdgeFineStage(sample_rate=SR)
        _drive(stage, duration_s=91.0)          # three fold blocks
        self.assertIsNotNone(stage._own_offset_rtp)

    def test_tracks_from_its_own_offset_once_confirmed(self):
        stage = BpskEdgeFineStage(sample_rate=SR)
        _drive(stage, duration_s=121.0)         # four fold blocks
        self.assertEqual(stage._last_search_mode, "tracking")

    def test_disagreeing_bootstraps_are_not_promoted(self):
        """Feed blocks whose edges differ by far more than the tolerance;
        the stage must stay in bootstrap rather than adopt either."""
        stage = BpskEdgeFineStage(sample_rate=SR)
        for k, edge in enumerate((10000.0, 40000.0, 70000.0)):
            sig = _make_bpsk_signal(
                duration_s=30.0, sample_rate=SR, edge_offset_samples=edge,
                noise_std=_noise_std_for(55.0), seed=11 + k,
            )
            for i in range(0, len(sig), BATCH):
                stage.process_samples(sig[i:i + BATCH], i + k * len(sig))
        self.assertIsNone(stage._own_offset_rtp)

    def test_a_second_with_no_edge_yields_no_estimate(self):
        """Spec §6: absence of an estimate must stay visible as absence.
        A constant second has no polarity transition to find."""
        stage = BpskEdgeFineStage(sample_rate=SR)
        flat = np.ones(SR * 31, dtype=np.complex64)
        produced = []
        for i in range(0, len(flat), BATCH):
            est = stage.process_samples(flat[i:i + BATCH], i)
            if est is not None:
                produced.append(est)
        self.assertEqual(produced, [])
        self.assertGreater(stage._failed_blocks, 0)

    def test_demotes_to_bootstrap_after_repeated_failures(self):
        stage = BpskEdgeFineStage(sample_rate=SR)
        _drive(stage, duration_s=121.0)
        self.assertIsNotNone(stage._own_offset_rtp)
        for _ in range(3):
            stage._note_block_failed()
        self.assertIsNone(stage._own_offset_rtp)


def _drive_at(stage, edge, duration_s, rtp0, cn0_db_hz=55.0, seed=11,
              violations=None):
    """Drive ``duration_s`` of signal whose edge sits at ``edge``, with the
    stream's RTP starting at ``rtp0`` so successive calls stay continuous.

    ``violations``, when given, is reported to the stage as the
    authority's verdict after every block that produced an estimate —
    what the recorder does in production.
    """
    sig = _make_bpsk_signal(
        duration_s=duration_s, sample_rate=SR, edge_offset_samples=edge,
        noise_std=_noise_std_for(cn0_db_hz), seed=seed,
    )
    last = None
    for i in range(0, len(sig), BATCH):
        est = stage.process_samples(sig[i:i + BATCH], rtp0 + i)
        if est is not None:
            last = est
            if violations is not None:
                stage.note_authority_violations(violations)
    return last


class TestOwnOffsetEscapeHatch(unittest.TestCase):
    """C1: the recovery paths must be able to make the stage let go.

    ``reset()`` runs at every fold-block boundary, so it deliberately
    keeps the tracking offset.  The three recorder recovery sites
    (stale-lock abandonment, stuck-unlock, step-recovery) need a way to
    say "the position you inherited is repudiated" — otherwise a
    displaced MF lock is *harder* to shed after self-acquisition than
    before it: the MF unlocks, the authority UNLOCKs, and the fine
    stage keeps localising at the same wrong place.
    """

    def test_clear_own_offset_drops_the_tracking_position(self):
        stage = BpskEdgeFineStage(sample_rate=SR)
        _drive(stage, duration_s=121.0)
        self.assertIsNotNone(stage._own_offset_rtp)
        stage.clear_own_offset()
        self.assertIsNone(stage._own_offset_rtp)

    def test_clear_own_offset_forces_a_fresh_confirmation(self):
        """Dropping the offset but keeping the history would let the very
        next block re-promote the repudiated position."""
        stage = BpskEdgeFineStage(sample_rate=SR)
        _drive(stage, duration_s=121.0)
        stage.clear_own_offset()
        self.assertEqual(list(stage._bootstrap_history), [])

    def test_reset_alone_still_keeps_the_tracking_position(self):
        """The escape hatch must NOT live inside ``reset()``: reset runs
        at every block boundary and clearing there would fight the
        recorder's per-batch re-seed."""
        stage = BpskEdgeFineStage(sample_rate=SR)
        _drive(stage, duration_s=121.0)
        own = stage._own_offset_rtp
        stage.reset()
        self.assertEqual(stage._own_offset_rtp, own)

    def test_a_repudiated_position_is_abandoned_and_the_true_edge_found(self):
        """The B4 displaced-lock family, end to end at stage level.

        The stage is seeded at P1 and adopts it.  The lock is then
        repudiated and the true edge is elsewhere (P2).  Before
        ``clear_own_offset`` the stage went on searching +-6 ms around
        P1 and produced NOTHING for ever; after it, one bootstrap block
        finds P2.
        """
        P1 = EDGE
        P2 = EDGE + 20_000.0
        stage = BpskEdgeFineStage(sample_rate=SR)
        stage.set_coarse_offset_samples(P1)
        _drive_at(stage, P1, 120.0, 0)
        self.assertEqual(stage._last_search_mode, "seeded")
        self.assertIsNotNone(stage._own_offset_rtp)

        # Exactly what the three recovery sites do.
        stage.clear_coarse_offset()
        stage.clear_own_offset()
        stage.reset()

        est = _drive_at(stage, P2, 30.0, 120 * SR)
        self.assertEqual(stage._last_search_mode, "bootstrap")
        self.assertIsNotNone(est, "no estimate after abandoning the lock")
        err = (est.edge_offset_samples - P2 + SR / 2) % SR - SR / 2
        self.assertLess(abs(err) / SR * 1e6, 100.0)


class TestAuthorityVerdictFeedback(unittest.TestCase):
    """C2: spec §3.3's other half — ``edge_period`` rejections count.

    A position can fit cleanly on every block and still be rejected by
    the authority for violating ``edge_period``.  Without this feedback
    the stage keeps tracking it and re-installs it the moment the
    authority re-acquires, so the demotion contract is only half
    honoured and C1's escape hatch has nothing to escape from.
    """

    def _tracking_stage(self):
        stage = BpskEdgeFineStage(sample_rate=SR)
        _drive_at(stage, EDGE, 120.0, 0, violations=())
        self.assertIsNotNone(stage._own_offset_rtp)
        return stage

    def test_edge_period_rejections_demote_to_bootstrap(self):
        stage = self._tracking_stage()
        _drive_at(stage, EDGE, 90.0, 120 * SR,
                  violations=("edge_period",))
        self.assertIsNone(
            stage._own_offset_rtp,
            "a position rejected for edge_period on every block was "
            "still being tracked")

    def test_fine_coarse_rejections_do_not_demote(self):
        """The witness disagreeing is a different signal, and since this
        work it is deliberately non-fatal."""
        stage = self._tracking_stage()
        own = stage._own_offset_rtp
        _drive_at(stage, EDGE, 90.0, 120 * SR,
                  violations=("fine_coarse",))
        self.assertEqual(stage._own_offset_rtp, own)

    def test_a_clean_verdict_clears_the_failure_run(self):
        """Demotion is on CONSECUTIVE failures, per the spec."""
        stage = self._tracking_stage()
        stage._note_block_failed()
        stage._note_block_failed()
        _drive_at(stage, EDGE, 30.0, 120 * SR, violations=())
        self.assertEqual(stage._failed_blocks, 0)

    def test_a_clean_fit_alone_does_not_clear_the_run(self):
        """The defect this closes: ``_note_block_estimate`` used to zero
        the counter on any successful FIT, before the authority had
        said anything, so consecutive rejections never accumulated."""
        stage = self._tracking_stage()
        stage._note_block_failed()
        self.assertEqual(stage._failed_blocks, 1)
        _drive_at(stage, EDGE, 30.0, 120 * SR,
                  violations=("edge_period",))
        self.assertEqual(stage._failed_blocks, 2)


class TestCoarseLifecycle(unittest.TestCase):

    def test_clear_coarse_offset_drops_the_seeded_window(self):
        """After an MF unlock the old window is stale; searching it is
        how a stale-window estimate reaches the authority (Finding 3)."""
        stage = BpskEdgeFineStage(sample_rate=SR)
        stage.set_coarse_offset_samples(EDGE)
        self.assertIsNotNone(stage.coarse_offset_fold_domain(0))
        stage.clear_coarse_offset()
        self.assertIsNone(stage.coarse_offset_fold_domain(0))

    def test_clear_coarse_offset_keeps_our_own_offset(self):
        """Different fields. Losing the MF must not cost us our own
        confirmed position -- that is the whole point of the change."""
        stage = BpskEdgeFineStage(sample_rate=SR)
        _drive(stage, duration_s=121.0)
        own = stage._own_offset_rtp
        self.assertIsNotNone(own)
        stage.clear_coarse_offset()
        self.assertEqual(stage._own_offset_rtp, own)


if __name__ == "__main__":
    unittest.main()
