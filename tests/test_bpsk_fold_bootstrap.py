"""Tests for the folded-second bootstrap edge locator."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hf_timestd.core.bpsk_fold_bootstrap import bootstrap_edge_index

SR = 96000


def _folded_second(edge: int, amplitude: float = 1.0,
                   noise_std: float = 0.0, seed: int = 5,
                   invert: bool = False) -> np.ndarray:
    """A derotated folded second: -A on [0, edge), +A on [edge, p).

    That is the real shape — measured on BpskEdgeFineStage's own fold,
    spec §3.2. The wrap from +A back to -A lies between index p-1 and 0
    and is deliberately NOT represented as an interior feature.
    """
    x = np.full(SR, amplitude, dtype=np.float64)
    x[:edge] = -amplitude
    if invert:
        x = -x
    if noise_std > 0:
        x = x + np.random.default_rng(seed).normal(0, noise_std, size=SR)
    return x


class TestBootstrapEdgeIndex(unittest.TestCase):

    def test_locates_a_clean_mid_second_edge(self):
        self.assertEqual(bootstrap_edge_index(_folded_second(47916)), 47916)

    def test_locates_an_edge_near_the_fold_origin(self):
        """The case a plain CUSUM gets structurally wrong (spec §3.1)."""
        self.assertEqual(bootstrap_edge_index(_folded_second(300)), 300)

    def test_locates_an_edge_near_the_fold_end(self):
        self.assertEqual(bootstrap_edge_index(_folded_second(95700)), 95700)

    def test_polarity_invariant(self):
        """Global sign is set by an arbitrary sign-alternation phase, so
        the statistic must not assume which way the transition runs."""
        self.assertEqual(
            bootstrap_edge_index(_folded_second(47916, invert=True)), 47916,
        )

    def test_within_one_sample_under_realistic_fold_noise(self):
        """noise_std 0.15 is the per-bin residual of a 30 s fold at
        B4's worst measured night C/N0 (48.5 dB-Hz)."""
        for edge in (300, 47916, 95700):
            found = bootstrap_edge_index(
                _folded_second(edge, noise_std=0.15, seed=edge)
            )
            self.assertLessEqual(abs(found - edge), 1, f"edge={edge}")

    def test_rejects_input_too_short_to_have_an_interior_edge(self):
        with self.assertRaises(ValueError):
            bootstrap_edge_index(np.array([1.0]))


if __name__ == "__main__":
    unittest.main()
