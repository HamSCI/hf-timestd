"""Regression tests for P-C2: IonosphericModel.update_calibration_from_ionogram.

The method was dead on arrival — it referenced three identifiers that do not
exist (`base_heights.hmF2_km`, `self._get_grid_key`, `self.calibration_history`)
and called `get_layer_heights` with positionally-swapped arguments, so the
highest-quality calibration anchor (ionosonde, confidence 1.0) raised
AttributeError / TypeError on its first call.

Fixed in c9117b3 (Tier-1 remediation). These tests exercise the path — which
the finding explicitly asked for — so the crash cannot silently regress.
"""

import unittest
from datetime import datetime, timezone

from hf_timestd.core.ionospheric_model import IonosphericModel

_TS = datetime(2026, 5, 18, 15, 0, 0, tzinfo=timezone.utc)


class TestIonogramCalibration(unittest.TestCase):

    def test_anchor_is_stored_without_crashing(self) -> None:
        model = IonosphericModel()
        # P-C2: this used to raise on the very first call.
        model.update_calibration_from_ionogram(
            latitude=40.0, longitude=-105.0, timestamp=_TS,
            measured_hmF2_km=320.0, confidence=1.0)

        # The anchor lands in the shared per-location calibration store.
        entries = model._calibration_data["40_-105"]
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual(e.implied_hmF2_km, 320.0)   # the measured height
        self.assertEqual(e.confidence, 1.0)
        # offset = measured - model-predicted, clamped to ±150 km
        expected = max(-150.0, min(150.0, e.implied_hmF2_km - e.predicted_hmF2_km))
        self.assertAlmostEqual(e.offset_km, expected, places=6)

        stats = model.get_calibration_stats(40.0, -105.0)
        self.assertEqual(stats['n_entries'], 1)

    def test_extreme_measurement_offset_is_clamped(self) -> None:
        model = IonosphericModel()
        # Physically-impossible heights ⇒ raw offset far beyond ±150 km.
        model.update_calibration_from_ionogram(
            latitude=10.0, longitude=20.0, timestamp=_TS,
            measured_hmF2_km=2000.0)
        self.assertEqual(model._calibration_data["10_20"][0].offset_km, 150.0)

        model.update_calibration_from_ionogram(
            latitude=10.0, longitude=20.0, timestamp=_TS,
            measured_hmF2_km=-1000.0)
        self.assertEqual(model._calibration_data["10_20"][1].offset_km, -150.0)


if __name__ == '__main__':
    unittest.main()
