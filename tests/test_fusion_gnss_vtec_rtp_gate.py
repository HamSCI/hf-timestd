"""Regression test for M-H14: GNSS-VTEC must not move D_clock.

The station's registration comes from radiod's GPS_TIME/RTP_TIMESNAP pair
plus the Offset Judge's correction (docs/design/MEASUREMENT_MODEL.md).
That reference sits tighter than any ionospheric model, so applying a
model-derived GNSS-VTEC TEC correction to it would inject model error into
a better reference (`METROLOGY_PHYSICS_SPLIT`).  The GNSS-VTEC block in
`fuse()` therefore never mutates `m.d_clock_ms`; it tags
`propagation_mode` with GNSS_VALIDATED and nudges `confidence`.

M-H14 was remediated in c9117b3 (Tier-1 remediation).  Until 2026-09-04 the
block held a FUSION-authority branch that DID apply the correction, gated on
the `[timing] authority` key; that key and branch retired with the residue
audit (RESIDUE_AUDIT_2026-09-04 §3.4-3.5), and the contrast test that
exercised them went with it.  This test pins what remains, so a future
refactor cannot silently re-introduce the leak.
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


class TestGnssVtecGate(unittest.TestCase):

    @staticmethod
    def _run():
        """Run one fuse() cycle with a fresh GNSS-VTEC reading and return the
        measurements' d_clock before/after, plus their propagation modes."""
        with tempfile.TemporaryDirectory() as td:
            fusion = MultiBroadcastFusion(data_root=Path(td))
            # Low-frequency broadcasts ⇒ the 1/f² TEC term is far above the
            # 0.1 ms threshold the retired branch used, so a re-introduced
            # correction would show as a visible D_clock change.
            measurements = [
                _measurement('WWVH', 15.0, 2.0),
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

    def test_gnss_vtec_does_not_mutate_d_clock(self) -> None:
        before, after, modes = self._run()
        # The invariant: the GNSS-VTEC block must not touch D_clock.
        self.assertEqual(after, before)
        # ... and it must still have RUN — as a cross-check (GNSS_VALIDATED) —
        # so the assertion above is not vacuously true.
        self.assertTrue(all('GNSS_VALIDATED' in m for m in modes))
        self.assertFalse(any('GNSS_TEC' in m for m in modes))

    def test_constructor_no_longer_takes_an_authority_switch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(TypeError):
                MultiBroadcastFusion(data_root=Path(td), is_rtp_authority=False)


if __name__ == '__main__':
    unittest.main()
