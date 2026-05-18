"""Regression tests for Increment 2 (M-H13): removal of the L3 Kalman.

The fusion pipeline used to cascade two filtering layers — per-broadcast
Kalman banks, then a second ("L3") Kalman over their weighted mean. Cascading
violates the second filter's white-innovation assumption. Increment 2 removed
the L3 Kalman: ``fuse()`` now outputs the WLS weighted mean directly, and
holdover / leap-second hold coast that output on the last LOCKED value.

These tests pin that contract so the L3 Kalman cannot quietly reappear.
"""

import json
import tempfile
import time
import unittest
from pathlib import Path

from hf_timestd.core.multi_broadcast_fusion import (
    BroadcastMeasurement,
    MultiBroadcastFusion,
)


def _measurement(station: str, freq_mhz: float, d_clock_ms: float,
                  mode: str = '1F') -> BroadcastMeasurement:
    """Build a synthetic broadcast measurement good enough to drive fuse()."""
    return BroadcastMeasurement(
        timestamp=time.time(),
        station=station,
        frequency_mhz=freq_mhz,
        d_clock_ms=d_clock_ms,
        propagation_delay_ms=10.0,
        propagation_mode=mode,
        confidence=0.9,
        snr_db=25.0,
        quality_grade='A',
        channel_name=f'{station}_{freq_mhz}',
        raw_arrival_time_ms=100.0,
        uncertainty_ms=1.0,
    )


# A 4-broadcast / 3-station cycle — enough coverage for the WLS branch to LOCK.
_MULTI_STATION = [
    _measurement('WWV', 10.0, 2.0),
    _measurement('WWV', 15.0, 2.2),
    _measurement('WWVH', 10.0, 1.8),
    _measurement('CHU', 7.85, 2.1),
]


class TestL3KalmanRemoved(unittest.TestCase):
    """The L3 Kalman machinery must be gone — method, state, persistence."""

    def test_kalman_update_method_removed(self) -> None:
        self.assertFalse(
            hasattr(MultiBroadcastFusion, '_kalman_update'),
            "_kalman_update (the L3 Kalman) must not exist",
        )

    def test_l3_kalman_state_attributes_removed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fusion = MultiBroadcastFusion(data_root=Path(td))
            for attr in ('kalman_state', 'kalman_P', 'kalman_initialized',
                         'kalman_state_l2', 'kalman_P_l2',
                         'kalman_convergence_threshold', 'measurement_window'):
                self.assertFalse(
                    hasattr(fusion, attr),
                    f"L3 Kalman attribute {attr!r} must not exist",
                )
            # Repurposed fusion-convergence attributes survive.
            self.assertEqual(fusion.kalman_n_updates, 0)
            self.assertFalse(fusion.kalman_converged)
            self.assertIsNone(fusion.last_locked_d_clock)

    def test_save_state_omits_l3_kalman_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fusion = MultiBroadcastFusion(data_root=Path(td))
            fusion.kalman_converged = True
            fusion._save_calibration()
            data = json.loads(fusion.calibration_file.read_text())
            self.assertNotIn('_kalman_state', data)
            self.assertNotIn('_kalman_state_l2', data)

    def test_load_state_ignores_legacy_kalman_blocks(self) -> None:
        """A calibration file written by the old code must still load."""
        with tempfile.TemporaryDirectory() as td:
            cal_file = Path(td) / 'calibration.json'
            # Mimic a pre-Increment-2 calibration file.
            cal_file.write_text(json.dumps({
                'WWV_10.00': {
                    'frequency_mhz': 10.0, 'offset_ms': 0.1,
                    'uncertainty_ms': 0.5, 'n_samples': 100,
                    'last_updated': time.time(), 'reference_station': 'CHU',
                },
                'CHU_7.85': {
                    'frequency_mhz': 7.85, 'offset_ms': -0.2,
                    'uncertainty_ms': 0.5, 'n_samples': 100,
                    'last_updated': time.time(), 'reference_station': 'CHU',
                },
                '_kalman_state': {
                    'offset_ms': 3.14, 'drift_ms_per_min': 0.0,
                    'covariance': [[1.0, 0.0], [0.0, 1e-6]],
                    'converged': True, 'n_updates': 200, 'initialized': True,
                },
                '_kalman_state_l2': {
                    'offset_ms': 2.71, 'drift_ms_per_min': 0.0,
                    'covariance': [[1.0, 0.0], [0.0, 1e-6]],
                    'converged': True, 'n_updates': 200, 'initialized': True,
                },
            }))
            fusion = MultiBroadcastFusion(data_root=Path(td),
                                          calibration_file=cal_file)
            # Calibrations load; the legacy Kalman blocks are silently ignored.
            self.assertIn('WWV_10.00', fusion.calibration)
            self.assertIn('CHU_7.85', fusion.calibration)
            self.assertFalse(hasattr(fusion, 'kalman_state'))


class TestFusionOutputIsWlsMean(unittest.TestCase):
    """fuse() output is the WLS weighted mean — no L3 temporal smoothing."""

    def test_locked_cycle_outputs_weighted_mean(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fusion = MultiBroadcastFusion(data_root=Path(td))
            fusion._read_latest_measurements = lambda *a, **k: list(_MULTI_STATION)
            result = fusion.fuse(skip_write=True)

            self.assertIsNotNone(result)
            self.assertFalse(fusion.holdover_mode)
            self.assertEqual(result.kalman_state, 'LOCKED')
            # On the very first cycle there is no history. The fused value must
            # therefore be a pure combination of this cycle's inputs — i.e.
            # bracketed by them. An L3 Kalman anchored elsewhere need not be.
            d_clocks = [m.d_clock_ms for m in _MULTI_STATION]
            self.assertGreaterEqual(result.d_clock_fused_ms, min(d_clocks))
            self.assertLessEqual(result.d_clock_fused_ms, max(d_clocks))
            # The LOCKED output is recorded as the holdover/coast anchor.
            self.assertAlmostEqual(fusion.last_locked_d_clock,
                                   result.d_clock_fused_ms, places=9)

    def test_kalman_n_updates_counts_fusion_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fusion = MultiBroadcastFusion(data_root=Path(td))
            fusion._read_latest_measurements = lambda *a, **k: list(_MULTI_STATION)
            self.assertEqual(fusion.kalman_n_updates, 0)
            fusion.fuse(skip_write=True)
            fusion.fuse(skip_write=True)
            self.assertEqual(fusion.kalman_n_updates, 2)


class TestHoldoverCoast(unittest.TestCase):
    """Holdover coasts the fused output on the last LOCKED value (S2)."""

    def test_single_station_cycle_coasts_on_last_locked(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fusion = MultiBroadcastFusion(data_root=Path(td))

            # Cycle 1: multi-station LOCK establishes the anchor.
            fusion._read_latest_measurements = lambda *a, **k: list(_MULTI_STATION)
            locked = fusion.fuse(skip_write=True)
            anchor = fusion.last_locked_d_clock
            self.assertFalse(fusion.holdover_mode)

            # Cycle 2: a lone station reporting a wild 99 ms spike.
            fusion._read_latest_measurements = lambda *a, **k: [
                _measurement('WWV', 10.0, 99.0)
            ]
            held = fusion.fuse(skip_write=True)

            self.assertTrue(fusion.holdover_mode)
            # The output coasts on the anchor — the 99 ms spike is NOT emitted.
            self.assertAlmostEqual(held.d_clock_fused_ms, anchor, places=9)
            self.assertAlmostEqual(held.d_clock_fused_ms,
                                   locked.d_clock_fused_ms, places=9)
            # Uncertainty grows during holdover rather than tracking the spike.
            self.assertGreater(held.uncertainty_ms, locked.uncertainty_ms)


if __name__ == '__main__':
    unittest.main()
