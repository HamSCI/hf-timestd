"""Regression tests for S3: per-broadcast Kalman coast during a leap second.

When a CHU-FSK-detected TAI-UTC change is in effect (the leap-second-hold
window is open), every measurement is stepped by ~1 s. Increment 2 routed
that into the fusion-level holdover coast, but the per-broadcast Kalman
banks still ran a full `update()` on the stepped measurement — corrupting
their state.

S3: `_apply_broadcast_kalmans` coasts each per-broadcast Kalman (`predict()`,
not `update()`) for the duration of the hold.

M-M11 update: the hold state is now a timestamp window
(`_fsk_leap_second_hold_until`) rather than a per-cycle boolean
(`_fsk_leap_second_hold`).  These tests arm/disarm via
`_fsk_leap_second_hold_until` directly.
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
    )


class TestLeapSecondCoast(unittest.TestCase):

    def test_hold_coasts_a_converged_kalman(self) -> None:
        """A leap-second-stepped measurement must not pull the filter."""
        with tempfile.TemporaryDirectory() as td:
            fusion = MultiBroadcastFusion(data_root=Path(td))
            # Converge the WWV 10 MHz Kalman on a steady d_clock ≈ 2.0 ms.
            for _ in range(20):
                fusion._apply_broadcast_kalmans(
                    [_measurement('WWV', 10.0, 2.0)], feed='l2')
            kalman = fusion.broadcast_kalmans['l2:WWV_10000']
            self.assertLess(abs(kalman.state[0] - 2.0), 0.3)

            # Leap-second hold: a measurement stepped by ~1 s (+1000 ms).
            fusion._fsk_leap_second_hold_until = time.time() + 600  # arm 10-min window
            out = fusion._apply_broadcast_kalmans(
                [_measurement('WWV', 10.0, 1002.0)], feed='l2')

            # Coasted: the filter output and its internal state stay near 2.0,
            # NOT pulled toward the 1002 ms stepped measurement.
            self.assertLess(abs(out[0].d_clock_ms - 2.0), 0.5)
            self.assertLess(abs(kalman.state[0] - 2.0), 0.5)

    def test_filter_resumes_cleanly_after_hold(self) -> None:
        """Once the hold clears, the (uncorrupted) filter tracks normally."""
        with tempfile.TemporaryDirectory() as td:
            fusion = MultiBroadcastFusion(data_root=Path(td))
            for _ in range(20):
                fusion._apply_broadcast_kalmans(
                    [_measurement('WWV', 10.0, 2.0)], feed='l2')

            fusion._fsk_leap_second_hold_until = time.time() + 600  # arm 10-min window
            fusion._apply_broadcast_kalmans(
                [_measurement('WWV', 10.0, 1002.0)], feed='l2')

            fusion._fsk_leap_second_hold_until = 0.0  # disarm
            out = fusion._apply_broadcast_kalmans(
                [_measurement('WWV', 10.0, 2.0)], feed='l2')
            self.assertLess(abs(out[0].d_clock_ms - 2.0), 0.5)

    def test_hold_on_uninitialised_kalman_yields_high_uncertainty(self) -> None:
        """Coasting a never-seen broadcast gives a huge σ ⇒ ~zero fusion weight."""
        with tempfile.TemporaryDirectory() as td:
            fusion = MultiBroadcastFusion(data_root=Path(td))
            fusion._fsk_leap_second_hold_until = time.time() + 600  # arm 10-min window
            out = fusion._apply_broadcast_kalmans(
                [_measurement('CHU', 7.85, 5.0)], feed='l2')
            self.assertEqual(out[0].d_clock_ms, 0.0)
            self.assertGreaterEqual(out[0].kalman_uncertainty_ms, 100.0)


if __name__ == '__main__':
    unittest.main()
