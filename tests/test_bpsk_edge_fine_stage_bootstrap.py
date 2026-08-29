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


if __name__ == "__main__":
    unittest.main()
