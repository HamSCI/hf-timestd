"""Regression tests for P-H13 in propagation_model.py.

The propagation model computes a 1-sigma uncertainty internally, but the
arrival-window schema and the propagation-contract tier table work in 3-sigma.
The fields carried no sigma convention in their names: ModeArrival.uncertainty_ms
was 1-sigma, while PropagationPrediction.primary_uncertainty_ms was 1-sigma in
the normal path but a 3-sigma magnitude (15.0 ms) in the vacuum-fallback path.
Two consumers disagreed — metrology_engine read it as 1-sigma,
arrival_pattern_matrix as 3-sigma.

The fields are now sigma-explicit: *_1sigma_ms holds the estimate, and an
*_3sigma_ms property derives exactly 3x for the schema / contract convention.
"""

from datetime import datetime, timezone

import pytest

from hf_timestd.core.propagation_model import (
    HFPropagationModel,
    ModeArrival,
    PropagationMode,
    PropagationPrediction,
)


def test_mode_arrival_3sigma_is_three_times_1sigma():
    ma = ModeArrival(
        mode=PropagationMode(1, 'F', '1F'),
        delay_ms=10.0, geometric_delay_ms=9.0, iono_delay_ms=1.0,
        path_length_km=3000.0, reflection_height_km=300.0,
        elevation_angle_deg=30.0, is_feasible=True,
        uncertainty_1sigma_ms=2.5,
    )
    assert ma.uncertainty_3sigma_ms == pytest.approx(7.5)


def test_prediction_3sigma_is_three_times_1sigma():
    pred = PropagationPrediction(
        station='WWV', frequency_mhz=10.0,
        timestamp=datetime(2026, 3, 15, 18, tzinfo=timezone.utc),
        distance_km=1000.0,
    )
    pred.primary_uncertainty_1sigma_ms = 4.0
    assert pred.primary_uncertainty_3sigma_ms == pytest.approx(12.0)


def test_default_uncertainty_is_5ms_1sigma_15ms_3sigma():
    # The default reflects the "no model" case: 5 ms 1-sigma -> 15 ms 3-sigma,
    # the value the vacuum fallback historically reported as a bare 15.0.
    pred = PropagationPrediction(
        station='WWV', frequency_mhz=10.0,
        timestamp=datetime(2026, 3, 15, 18, tzinfo=timezone.utc),
        distance_km=1000.0,
    )
    assert pred.primary_uncertainty_1sigma_ms == pytest.approx(5.0)
    assert pred.primary_uncertainty_3sigma_ms == pytest.approx(15.0)


class TestRealPredictionSigmaConsistency:
    def setup_method(self):
        self.model = HFPropagationModel(
            receiver_lat=38.92, receiver_lon=-92.13,
            enable_realtime=False,  # parametric fallback only — no I/O
        )

    def test_every_arrival_3sigma_tracks_1sigma(self):
        pred = self.model.predict(
            'WWV', 10.0, datetime(2026, 3, 15, 18, tzinfo=timezone.utc))
        for arr in pred.arrivals:
            assert arr.uncertainty_3sigma_ms == pytest.approx(
                3.0 * arr.uncertainty_1sigma_ms)
        assert pred.primary_uncertainty_3sigma_ms == pytest.approx(
            3.0 * pred.primary_uncertainty_1sigma_ms)

    def test_primary_uncertainty_is_positive(self):
        pred = self.model.predict(
            'WWV', 10.0, datetime(2026, 3, 15, 18, tzinfo=timezone.utc))
        assert pred.primary_uncertainty_1sigma_ms > 0.0
        assert pred.primary_uncertainty_3sigma_ms > 0.0
