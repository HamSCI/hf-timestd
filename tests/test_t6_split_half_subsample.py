"""Criterion 6 must resolve the wander it polices.

``split_half_delta_samples`` compares the even-second sub-fold's edge
against the odd-second sub-fold's.  T6_ACCEPTANCE_CRITERIA.md §4 gives it
one job: separate a stable edge from the wandering apex behind B4's ~270
tier transitions a day.

Captured B4 signal on 2026-09-20 measured the fold's own block-to-block
scatter at 0.0251-0.0657 samples (0.26-0.68 us).  A check that reports
only whole samples cannot see a wander of that size -- it reads exactly
0.0000 until the two sub-folds land on different samples, by which point
the disagreement already exceeds 5 us.  These tests pin the resolution to
the quantity being policed rather than to the sample grid.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hf_timestd.core.bpsk_edge_fine_stage import BpskEdgeFineStage

P = 9600            # a small rate keeps the arrays cheap; >= 8000 is enforced
AMP = 1.0
RAMP = 1.0          # transition half-width, in samples


def _flip(p: int, edge: float, amp: float = AMP, ramp: float = RAMP):
    """A single polarity flip whose zero crossing sits exactly at ``edge``.

    Negative before, positive after, joined by a linear ramp -- so the
    crossing carries a true sub-sample position rather than snapping to
    the nearest index.
    """
    n = np.arange(p, dtype=np.float64)
    return amp * np.clip((n - edge) / ramp, -1.0, 1.0)


def _stage_with(even_edge: float, odd_edge: float, p: int = P):
    """A stage whose two sub-folds hold flips at the given positions."""
    stage = BpskEdgeFineStage(sample_rate=p, fold_seconds=30)
    stage._acc_even = _flip(p, even_edge).astype(np.complex128)
    stage._acc_odd = _flip(p, odd_edge).astype(np.complex128)
    stage._cnt_even = np.ones(p, dtype=np.int64)
    stage._cnt_odd = np.ones(p, dtype=np.int64)
    return stage


class TestSplitHalfResolvesSubSample(unittest.TestCase):
    def test_reports_a_sub_sample_disagreement(self):
        """0.3 of a sample apart must not read as perfect agreement."""
        stage = _stage_with(4000.0, 4000.3)
        d = stage._split_half_delta(0.0)
        self.assertAlmostEqual(d, -0.3, places=2,
                               msg=f"sub-sample disagreement read as {d}")

    def test_resolves_below_the_measured_block_scatter(self):
        """0.0657 samples is B4's dawn block-to-block sd -- the check must
        see a disagreement that small, or it cannot police it."""
        stage = _stage_with(4000.0, 4000.0657)
        d = stage._split_half_delta(0.0)
        self.assertAlmostEqual(d, -0.0657, places=3,
                               msg=f"block-scale disagreement read as {d}")

    def test_agreement_still_reads_near_zero(self):
        """Two identical sub-folds must still report agreement."""
        stage = _stage_with(4000.25, 4000.25)
        d = stage._split_half_delta(0.0)
        self.assertAlmostEqual(d, 0.0, places=6)

    def test_sign_follows_even_minus_odd(self):
        """The docstring promises even minus odd; keep that contract."""
        stage = _stage_with(4000.4, 4000.0)
        self.assertGreater(stage._split_half_delta(0.0), 0.0)

    def test_matches_the_production_localiser(self):
        """The check must measure the edge the way the estimate does.

        Not a restatement of the implementation: it pins the property the
        criterion depends on.  If the two ever localise differently, a
        split-half delta would report the gap between two estimators as
        though it were the edge moving.
        """
        stage = _stage_with(4000.0, 4000.42)
        direct = []
        for ip in (np.real(stage._acc_even), np.real(stage._acc_odd)):
            apex = float(np.argmax(np.abs(stage._closed_form_T(ip))))
            direct.append(stage._fit_edge(ip, apex).edge_offset)
        self.assertAlmostEqual(stage._split_half_delta(0.0),
                               direct[0] - direct[1], places=9)

    def test_resolves_a_pair_straddling_the_fold_origin(self):
        """Two sub-folds either side of the seam differ by 20, not by p.

        The global apex picks each sub-fold's own edge before the local
        fit refines it, so a position near the fold origin stays separable
        from the fold's own wrap discontinuity.
        """
        stage = _stage_with(10.0, P - 10.0)
        self.assertAlmostEqual(stage._split_half_delta(0.0), 20.0, places=3)

    def test_two_noise_folds_do_not_agree(self):
        """The criterion's whole value: pure noise must NOT read as a
        stable edge.

        Seeding both sub-folds from one shared search centre -- the
        obvious way to write this -- pins them to the same window and
        makes noise agree to 0.1-0.9 samples, inside the healthy range at
        48.4 dB-Hz.  The global search is what keeps them apart.
        """
        rng = np.random.default_rng(7)
        stage = BpskEdgeFineStage(sample_rate=P, fold_seconds=30)
        stage._acc_even = (rng.normal(0, 1, P) + 1j * rng.normal(0, 1, P))
        stage._acc_odd = (rng.normal(0, 1, P) + 1j * rng.normal(0, 1, P))
        stage._cnt_even = np.ones(P, dtype=np.int64)
        stage._cnt_odd = np.ones(P, dtype=np.int64)
        d = abs(stage._split_half_delta(0.0))
        self.assertGreater(d, 50.0,
                           msg=f"two noise folds agreed to {d} samples")

    def test_empty_sub_fold_is_nan_not_agreement(self):
        """§4.4: no evidence must never be spelled as the favourable 0.0."""
        stage = _stage_with(4000.0, 4000.0)
        stage._cnt_odd = np.zeros(P, dtype=np.int64)
        self.assertTrue(np.isnan(stage._split_half_delta(0.0)))


if __name__ == "__main__":
    unittest.main()
