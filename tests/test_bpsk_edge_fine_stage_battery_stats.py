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
        seconds add in destructive interference (measured 0.016).  Asserted
        unconditionally: a regression that stops the stage producing any
        estimate under a rotating carrier must fail this test, not pass it
        having checked nothing."""
        est = _drive(BpskEdgeFineStage(sample_rate=SR), carrier_freq_hz=0.5)
        self.assertIsNotNone(est)
        self.assertLess(est.fold_retention, 0.30)


class TestSplitHalfAgreement(unittest.TestCase):

    def test_disjoint_halves_agree_on_a_clean_signal(self):
        """Two 30 s folds agreed to 41 ns on captured IQ (n=2)."""
        est = _drive(BpskEdgeFineStage(sample_rate=SR))
        self.assertIsNotNone(est)
        self.assertLess(abs(est.split_half_delta_samples), 4.0)

    def test_an_empty_sub_fold_reports_nan_not_perfect_agreement(self):
        """0.0 is the most FAVOURABLE value split_half_delta_samples can
        report -- the two folds landing on the same sample.  A parity bug
        that routes every sample into one sub-fold and leaves the other
        empty must not read as that perfect agreement: it is an absence
        of evidence, so it must come back as NaN.  Reproduces the
        reviewer's repro directly: one sub-fold has data, the other's
        counts are all zero (as a parity bug would leave them), and
        _split_half_delta is called on that state directly."""
        stage = BpskEdgeFineStage(sample_rate=SR)
        stage._acc_even[:] = 1.0 + 0j
        stage._cnt_even[:] = 1
        # _cnt_odd is left at its post-reset all-zero state.
        result = stage._split_half_delta(0.0, 0.0)
        self.assertTrue(math.isnan(result), f"expected NaN, got {result!r}")


class TestProminenceAndWidth(unittest.TestCase):

    def test_prominence_separates_a_real_edge_from_pure_noise(self):
        """peak_prominence must discriminate real content from the null,
        not merely clear a fixed constant -- a fixed bound of 5.0 turned
        out to pass on no signal at all (pure complex Gaussian noise
        measured 4.2-5.5 across ten seeded trials here; the reviewer's
        own run separately measured 4.4-6.3).  That floor is simply the
        order statistic of a ~1150-sample |diff| array: some sample reads
        several times the median by chance alone, with nothing to detect.

        A real edge at a healthy 70 dB-Hz C/N0 reads ~40 -- roughly 7-10x
        the noise ceiling measured below, comfortably material.  At a
        realistic worst-case 48.4 dB-Hz (T6_ACCEPTANCE_CRITERIA.md §4.3's
        C/N0 floor) five seeded trials read 4.9-5.5: indistinguishable
        from the noise floor above.  Task 1 reports these numbers for the
        later sweep-derived production threshold; it does not set one --
        this test only asserts the healthy-C/N0 case clearly separates
        from noise, which is all Task 1 needs to prove the field carries
        real information.
        """
        edge_est = _drive(BpskEdgeFineStage(sample_rate=SR))
        self.assertIsNotNone(edge_est)

        noise_prominences = []
        n = int(31.0 * SR)
        std = _noise_std_for(70.0)
        for seed in range(100, 110):
            rng = np.random.default_rng(seed)
            noise_sig = (rng.normal(0.0, std, n)
                         + 1j * rng.normal(0.0, std, n)).astype(np.complex64)
            stage = BpskEdgeFineStage(sample_rate=SR)
            last = None
            for i in range(0, len(noise_sig), BATCH):
                r = stage.process_samples(noise_sig[i:i + BATCH], i)
                if r is not None:
                    last = r
            if last is not None:
                noise_prominences.append(last.peak_prominence)

        self.assertTrue(noise_prominences, "no noise trial produced an estimate")
        noise_ceiling = max(noise_prominences)
        self.assertGreater(edge_est.peak_prominence, noise_ceiling * 2.0)

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
