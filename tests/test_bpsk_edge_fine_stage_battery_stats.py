"""The fold's own evidence: retention, split-half, prominence, width."""
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


def _drive(stage, cn0_db_hz=70.0, duration_s=31.0, edge=EDGE, seed=11,
           carrier_freq_hz=0.0):
    sig = _make_bpsk_signal(
        duration_s=duration_s, sample_rate=SR, edge_offset_samples=edge,
        noise_std=_noise_std_for(cn0_db_hz), seed=seed,
        carrier_freq_hz=carrier_freq_hz,
    )
    last = None
    for i in range(0, len(sig), BATCH):
        est = stage.process_samples(sig[i:i + BATCH], i)
        if est is not None:
            last = est
    return last


class TestFoldRetention(unittest.TestCase):

    def test_coherent_chain_retains_nearly_all_amplitude(self):
        """AI6VN after rob's cabling fix measured 0.9998; B4 reads 0.98."""
        est = _drive(BpskEdgeFineStage(sample_rate=SR))
        self.assertIsNotNone(est)
        self.assertGreater(est.fold_retention, 0.90)

    def test_incoherent_carrier_collapses_retention(self):
        """A carrier rotating between seconds makes the fold cancel its
        own signal -- AI6VN measured 0.006 with REF IN on the internal
        10 MHz.  An exact-integer-Hz offset returns to the same phase
        every second and stays fold-coherent (measured retention 0.998
        at 1 Hz here -- the fold aliases any whole-Hz rotation to zero,
        same as a stopped clock reading right twice a day); 0.5 Hz
        instead flips phase by a half turn each second, so consecutive
        seconds add in destructive interference (measured 0.016)."""
        est = _drive(BpskEdgeFineStage(sample_rate=SR), carrier_freq_hz=0.5)
        if est is not None:
            self.assertLess(est.fold_retention, 0.30)


class TestSplitHalfAgreement(unittest.TestCase):

    def test_disjoint_halves_agree_on_a_clean_signal(self):
        """Two 30 s folds agreed to 41 ns on captured IQ (n=2)."""
        est = _drive(BpskEdgeFineStage(sample_rate=SR))
        self.assertIsNotNone(est)
        self.assertLess(abs(est.split_half_delta_samples), 4.0)


class TestProminenceAndWidth(unittest.TestCase):

    def test_prominence_is_large_on_a_real_edge(self):
        """The magnitude-difference discriminant read 105x on captured IQ."""
        est = _drive(BpskEdgeFineStage(sample_rate=SR))
        self.assertIsNotNone(est)
        self.assertGreater(est.peak_prominence, 5.0)

    def test_transition_width_matches_the_channel_filter(self):
        """+-25 kHz predicts 1/(2B) = 20 us = ~2 samples at 96 kHz.
        The fit band spans several samples either side, so allow room --
        the discriminating case is a lattice phantom, which has no
        transition at all and reads far wider."""
        est = _drive(BpskEdgeFineStage(sample_rate=SR))
        self.assertIsNotNone(est)
        self.assertGreater(est.transition_width_samples, 0.0)
        self.assertLess(est.transition_width_samples, 60.0)


if __name__ == "__main__":
    unittest.main()
