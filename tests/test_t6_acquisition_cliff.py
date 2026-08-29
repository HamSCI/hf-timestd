"""The acquisition cliff, pinned.

The MF calibrator peak-picks against 0.5*_peak_running and locks itself
out once noise maxima approach half the real edge peak -- measured as a
stochastic cliff near 58-59 dB-Hz, below which the outcome depends on
the noise realisation. B4's T6 channel reached 48.5 dB-Hz on the
evening of 2026-08-28 and reported ACQUIRING all night.

This test pins the property the folding work exists to deliver: the
folded path still acquires where the per-second MF does not.
"""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_bpsk_pps_calibrator_mf import _make_bpsk_signal
from hf_timestd.core.bpsk_edge_fine_stage import BpskEdgeFineStage
from hf_timestd.core.bpsk_pps_calibrator_mf import BpskPpsCalibratorMF

SR = 96000
BATCH = 1920
EDGE = 47916.1672
# B4's worst measured C/N0, evening of 2026-08-28 (rf_gain -4.2 dB).
B4_WORST_NIGHT_CN0_DB_HZ = 48.5


def _noise_std_for(cn0_db_hz: float) -> float:
    snr = 10 ** ((cn0_db_hz - 10 * math.log10(SR)) / 10.0)
    return 1.0 / math.sqrt(2.0 * snr)


def _signal(cn0_db_hz: float, duration_s: float, seed: int) -> np.ndarray:
    return _make_bpsk_signal(
        duration_s=duration_s, sample_rate=SR, edge_offset_samples=EDGE,
        noise_std=_noise_std_for(cn0_db_hz), seed=seed,
    )


@pytest.mark.slow
class TestAcquisitionCliff(unittest.TestCase):

    def test_folded_stage_acquires_at_b4_worst_night(self):
        for seed in (11, 23, 37):
            stage = BpskEdgeFineStage(sample_rate=SR)
            sig = _signal(B4_WORST_NIGHT_CN0_DB_HZ, 91.0, seed)
            last = None
            for i in range(0, len(sig), BATCH):
                est = stage.process_samples(sig[i:i + BATCH], i)
                if est is not None:
                    last = est
            self.assertIsNotNone(last, f"no estimate at seed {seed}")
            err = (last.edge_offset_samples - EDGE + SR / 2) % SR - SR / 2
            err_us = abs(err) / SR * 1e6
            self.assertLess(err_us, 200.0, f"seed {seed}")
            # Precision gate, tighter than the acquisition/cliff pin above.
            # Measured at 48.5 dB-Hz on 2026-08-29: 0.50 / 0.50 / 2.73 us for
            # seeds 11 / 23 / 37. 20 us leaves ~7x headroom over the worst
            # observed seed -- tight enough to catch a tenfold degradation,
            # loose enough not to flake on noise realisation.
            self.assertLess(
                err_us, 20.0,
                f"seed {seed}: folded stage still acquired but its "
                f"precision degraded well beyond the measured floor "
                f"({err_us:.2f} us vs a 20 us gate) -- investigate as a "
                f"regression, do not just raise this bound",
            )

    def test_matched_filter_alone_does_not_acquire_there(self):
        """The premise. If this ever starts passing, the MF improved and
        this file's thresholds need re-deriving -- do not just delete it."""
        acquired = 0
        for seed in (11, 23, 37):
            cal = BpskPpsCalibratorMF(
                sample_rate=SR, consecutive_required=10,
                edge_tolerance_samples=30,
            )
            sig = _signal(B4_WORST_NIGHT_CN0_DB_HZ, 60.0, seed)
            edges = 0
            last_ok = -1
            for i in range(0, len(sig), BATCH):
                r = cal.process_samples(sig[i:i + BATCH], i)
                if r is not None and r.pps_ok != last_ok:
                    last_ok = r.pps_ok
                    edges += 1
            if edges > 3:
                acquired += 1
        self.assertLess(acquired, 3, "MF acquired at every seed")


if __name__ == "__main__":
    unittest.main()
