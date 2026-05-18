"""Regression tests for M-H16 / M-H17: single, coherent outlier rejection.

M-H16: `fuse()` ran outlier rejection twice — once (MAD on CALIBRATED values,
3.5σ) and again via `_reject_outliers` (MAD on RAW d_clock_ms, 3.0σ). Raw
values carry 30-60 ms inter-broadcast offsets that swamp the MAD, so the
second pass let real outliers slip through.

M-H17: `_reject_outliers` paired a weighted median with an unweighted MAD —
statistically incoherent.

Fix: the redundant raw-value pass and the `_reject_outliers` method were
removed. A single pre-fusion pass on calibrated residuals remains.
"""

import tempfile
import time
import unittest
from pathlib import Path

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


def _fuse(measurements):
    with tempfile.TemporaryDirectory() as td:
        fusion = MultiBroadcastFusion(data_root=Path(td))
        fusion._read_latest_measurements = lambda *a, **k: list(measurements)
        return fusion.fuse(skip_write=True)


class TestSingleOutlierPass(unittest.TestCase):

    def test_reject_outliers_method_removed(self) -> None:
        """The incoherent raw-value second pass (M-H16/M-H17) is gone."""
        self.assertFalse(hasattr(MultiBroadcastFusion, '_reject_outliers'))

    def test_gross_outlier_is_rejected(self) -> None:
        # Three broadcasts cluster near 2 ms; CHU is a gross 50 ms outlier.
        result = _fuse([
            _measurement('WWV', 10.0, 2.0),
            _measurement('WWV', 15.0, 2.1),
            _measurement('WWVH', 10.0, 1.9),
            _measurement('CHU', 7.85, 50.0),
        ])
        self.assertIsNotNone(result)
        # The single calibrated-residual pass rejects the outlier ...
        self.assertGreaterEqual(result.outliers_rejected, 1)
        # ... so the fused estimate stays with the cluster, not pulled to 50 ms.
        self.assertLess(abs(result.d_clock_fused_ms - 2.0), 1.0)

    def test_consistent_cluster_rejects_nothing(self) -> None:
        result = _fuse([
            _measurement('WWV', 10.0, 2.00),
            _measurement('WWV', 15.0, 2.05),
            _measurement('WWVH', 10.0, 1.95),
        ])
        self.assertIsNotNone(result)
        self.assertEqual(result.outliers_rejected, 0)


if __name__ == '__main__':
    unittest.main()
