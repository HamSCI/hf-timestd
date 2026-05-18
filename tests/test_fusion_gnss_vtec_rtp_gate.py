"""Regression test for M-H14: GNSS-VTEC must not correct D_clock in RTP mode.

In RTP mode the GPS+PPS reference already pins D_clock to ~50 µs — tighter
than any ionospheric model. Applying a model-derived GNSS-VTEC TEC correction
there would inject iono model error into a better reference, violating
`METROLOGY_PHYSICS_SPLIT`. The GNSS-VTEC block in `fuse()` therefore mutates
`m.d_clock_ms` only when `not is_rtp_authority`; in RTP mode it is a
cross-check only (tags `propagation_mode`/`confidence`, no D_clock change).

M-H14 was remediated in c9117b3 (Tier-1 remediation); this test pins the
invariant so a future refactor cannot silently re-introduce the leak.
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


class TestGnssVtecRtpGate(unittest.TestCase):

    @staticmethod
    def _run(is_rtp_authority: bool):
        """Run one fuse() cycle with a fresh GNSS-VTEC reading and return the
        measurements' d_clock before/after, plus their propagation modes."""
        with tempfile.TemporaryDirectory() as td:
            fusion = MultiBroadcastFusion(
                data_root=Path(td), is_rtp_authority=is_rtp_authority)
            # Low-frequency broadcasts ⇒ the 1/f² TEC correction is well above
            # the 0.1 ms apply threshold, so Fusion mode demonstrably corrects.
            measurements = [
                _measurement('CHU', 3.33, 2.0),
                _measurement('WWV', 5.0, 2.0),
                _measurement('WWV', 10.0, 2.0),
            ]
            fusion._read_latest_measurements = lambda *a, **k: list(measurements)
            # Passthrough: return the SAME objects so an in-place mutation by
            # the GNSS-VTEC block is observable afterwards.
            fusion._apply_broadcast_kalmans = lambda *a, **k: measurements
            # A fresh GNSS VTEC reading far from the modelled TEC.
            fusion._read_gnss_vtec = lambda: (2.0, time.time())

            before = [m.d_clock_ms for m in measurements]
            fusion.fuse(skip_write=True)
            after = [m.d_clock_ms for m in measurements]
            modes = [m.propagation_mode for m in measurements]
            return before, after, modes

    def test_rtp_mode_does_not_mutate_d_clock(self) -> None:
        before, after, modes = self._run(is_rtp_authority=True)
        # The invariant: in RTP mode the GNSS-VTEC block must not touch D_clock.
        self.assertEqual(after, before)
        # ... and it must still have RUN — as a cross-check (GNSS_VALIDATED) —
        # so the assertion above is not vacuously true.
        self.assertTrue(all('GNSS_VALIDATED' in m for m in modes))
        self.assertFalse(any('GNSS_TEC' in m for m in modes))

    def test_fusion_mode_applies_correction(self) -> None:
        # The contrast that gives the RTP test its meaning: in Fusion mode the
        # same VTEC discrepancy DOES correct D_clock.
        before, after, modes = self._run(is_rtp_authority=False)
        self.assertNotEqual(after, before)
        self.assertTrue(all('GNSS_TEC' in m for m in modes))


if __name__ == '__main__':
    unittest.main()
