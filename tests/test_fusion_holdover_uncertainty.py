"""Regression test for M-H15: holdover uncertainty must reach the output.

`fuse()` computes a per-cycle `uncertainty` in the LOCKED/holdover branch —
the WLS uncertainty when locked, the dropout-grown holdover uncertainty
otherwise. A later line then did `uncertainty = measurement_uncertainty`,
overwriting it with the static a-priori RSS budget. The holdover branch's
growth model was therefore discarded: the uncertainty sent to Chrony did not
grow during a signal dropout.

The fix RSS-combines the branch term with the non-statistical budget
components (systematic, propagation, jitter, tone, multipath) instead of
discarding it.
"""

import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

from hf_timestd.core.multi_broadcast_fusion import (
    BroadcastMeasurement,
    MultiBroadcastFusion,
)


def _measurement(station: str, freq_mhz: float,
                  d_clock_ms: float) -> BroadcastMeasurement:
    return BroadcastMeasurement(
        timestamp=time.time(),
        station=station,
        frequency_mhz=freq_mhz,
        d_clock_ms=d_clock_ms,
        propagation_delay_ms=10.0,
        propagation_mode='1F',
        confidence=0.9,
        snr_db=25.0,
        quality_grade='A',
        channel_name=f'{station}_{freq_mhz}',
        raw_arrival_time_ms=100.0,
        uncertainty_ms=1.0,
        kalman_uncertainty_ms=1.0,
    )


def _holdover_uncertainty(dropout_minutes: float,
                          base_uncertainty: float = 0.1) -> float:
    """Reported `uncertainty_ms` of a single-station holdover cycle following
    a simulated dropout of the given duration."""
    with tempfile.TemporaryDirectory() as td:
        fusion = MultiBroadcastFusion(data_root=Path(td))
        # Simulate a prior multi-station LOCK that ended `dropout` ago.
        fusion.last_locked_d_clock = 2.0
        fusion.last_valid_fusion_time = time.time() - dropout_minutes * 60.0
        fusion.last_valid_fusion_uncertainty = base_uncertainty
        # A single station ⇒ no cross-validation ⇒ holdover.
        fusion._read_latest_measurements = lambda *a, **k: [
            _measurement('WWV', 10.0, 2.0)
        ]
        result = fusion.fuse(skip_write=True)
        assert fusion.holdover_mode, "expected holdover with a single station"
        return result.uncertainty_ms


class TestHoldoverUncertaintyGrows(unittest.TestCase):

    def test_uncertainty_grows_with_dropout_duration(self) -> None:
        dropouts_min = [1.0, 120.0, 1200.0, 12000.0]
        uncertainties = [_holdover_uncertainty(d) for d in dropouts_min]

        # Monotonically non-decreasing with dropout duration ...
        for shorter, longer in zip(uncertainties, uncertainties[1:]):
            self.assertGreaterEqual(longer, shorter)
        # ... and a long dropout is materially more uncertain than a brief one.
        # Pre-M-H15 every value here was identical — the static RSS budget,
        # which has no dependence on dropout duration.
        self.assertGreater(uncertainties[-1], uncertainties[0])

    def test_holdover_uncertainty_is_finite_and_positive(self) -> None:
        u = _holdover_uncertainty(60.0)
        self.assertTrue(np.isfinite(u))
        self.assertGreater(u, 0.0)


if __name__ == '__main__':
    unittest.main()
