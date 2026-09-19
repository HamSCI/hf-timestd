"""The fold's own evidence: retention, split-half, prominence, width,
apex agreement."""
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
        result = stage._split_half_delta(0.0)
        self.assertTrue(math.isnan(result), f"expected NaN, got {result!r}")


class TestProminenceAndWidth(unittest.TestCase):

    def test_prominence_separates_a_real_edge_from_pure_noise(self):
        """peak_prominence is now a triangle-fidelity RESIDUAL (fix-round-3):
        RMS(|T(e)| - ideal_triangle) / peak, where T(e) is the closed-form
        matched filter over the WHOLE derotated folded second.  For a
        clean single flip T(e) is exactly piecewise-linear, so this reads
        LOW for a real edge and HIGH for noise -- the opposite sense of a
        bare peak/median ratio (round 1 and round 2's attempts), which is
        PINNED at 2.000 for any clean flip (a triangle's median sits at
        half its peak) and so cannot separate anything by amplitude alone.

        Measured here (ten seeded pure-complex-Gaussian-noise trials, no
        BPSK signal at all -- one trial produced no estimate): noise reads
        0.25 - 0.42.  A real edge at 70 dB-Hz reads 0.0014; at a realistic
        worst-case 48.4 dB-Hz (T6_ACCEPTANCE_CRITERIA.md §4.3's C/N0
        floor, five seeded trials) it reads 0.0008 - 0.0017 -- roughly
        150-500x BELOW the noise floor at every C/N0 tested, a gap wide
        enough for a production threshold to sit in comfortably.  This is
        the number that makes criterion 4's shape check actually work at
        the design's stated worst hour; Task 1 still sets no threshold.
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
        noise_floor = min(noise_prominences)
        # A real edge's residual must sit well BELOW even noise's best
        # (smallest) reading -- the direction is inverted from a bare
        # ratio, so "separates" here means "stays low", not "stays high".
        self.assertLess(edge_est.peak_prominence, noise_floor * 0.1)

    def test_transition_width_matches_the_channel_filter(self):
        """+-25 kHz predicts 1/(2B) = 20 us = ~2 samples at 96 kHz.
        The fit band spans several samples either side, so allow room --
        the discriminating case is a lattice phantom, which has no
        transition at all and reads far wider."""
        est = _drive(BpskEdgeFineStage(sample_rate=SR))
        self.assertIsNotNone(est)
        self.assertGreater(est.transition_width_samples, 0.0)
        self.assertLess(est.transition_width_samples, 60.0)


def _apex_index(stage) -> int:
    """T(e)'s own apex over the whole folded second, independent of the
    zero-crossing fit -- recomputed the same way _compute_estimate does,
    reusing the stage's own closed-form-T helper."""
    avg = stage._last_avg_for_test
    phi = 0.5 * float(np.angle(np.mean(avg.astype(np.complex128) ** 2)))
    in_phase = np.real(avg * np.exp(-1j * phi))
    t = stage._closed_form_T(in_phase)
    return int(np.argmax(np.abs(t)))


class TestApexAgreement(unittest.TestCase):
    """apex_distance_samples targets a different failure than prominence:
    not "is there a real transition anywhere" but "does the position this
    estimate REPORTS agree with where the fold's own evidence peaks."
    Measured (fix-round-3): a real edge's apex_distance_samples is small
    (0.35 - 0.87 samples across the C/N0s and seeds used elsewhere in this
    file) -- but so is pure noise's (0.06 - 1.49 samples across the same
    ten seeded trials as the prominence test above).  The two ranges
    overlap: in ordinary (bootstrap-search) operation, both the reported
    edge and T(e)'s apex come from the SAME single fold via correlated
    methods, so they agree with each other whether or not there is a real
    edge to agree ON.  apex_distance_samples does not separate signal
    from noise, and was never meant to -- see the second test below for
    the failure it does catch.
    """

    def test_apex_agrees_with_the_reported_edge_on_a_real_signal(self):
        """Sanity check: normal operation (no external coarse offset
        forcing a different search window) keeps the two in agreement."""
        est = _drive(BpskEdgeFineStage(sample_rate=SR))
        self.assertIsNotNone(est)
        self.assertLess(abs(est.apex_distance_samples), 2.0)

    def test_apex_distance_catches_a_displaced_lock(self):
        """The failure criterion 4 exists for: on 2026-09-04 B4 locked
        onto a 20.000 ms lattice position away from the true apex -- a
        lock that fits cleanly (so prominence alone would not catch it)
        but disagrees with where the fold's own T(e) statistic peaks.
        Reproduced directly: drive a real edge, then ask what
        apex_distance_samples WOULD read if this estimate had reported a
        position ~20 ms (1920 samples at 96 kHz) away from the true one
        -- exactly what a lattice lock looks like from this check's point
        of view.  Measured: -1919.19 samples (~-19.99 ms) against ~0.81
        for the true position, so the check catches a lock this far off
        by roughly three orders of magnitude in reported distance.
        """
        stage = BpskEdgeFineStage(sample_rate=SR)
        est = _drive(stage)
        self.assertIsNotNone(est)
        p = SR
        apex_idx = _apex_index(stage)

        def distance(reported_edge: float) -> float:
            return ((apex_idx - reported_edge + p / 2) % p) - p / 2

        true_distance = distance(est.edge_offset_samples)
        displaced_edge = (est.edge_offset_samples + 1920.0) % p
        displaced_distance = distance(displaced_edge)

        self.assertLess(abs(true_distance), 2.0)
        self.assertGreater(abs(displaced_distance), 1900.0)


if __name__ == "__main__":
    unittest.main()
