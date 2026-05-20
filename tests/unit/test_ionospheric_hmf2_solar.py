"""Regression tests for P-H16: hmF2 solar-activity sign in ionospheric_model.

The parametric layer-height model's solar term was

    solar_term = -HMF2_SOLAR_FACTOR * (f107 - 100)

which drives hmF2 DOWN at solar maximum. Observationally the F2 peak RISES
with solar flux (the layer expands with increased ionization) — and the sign
even contradicted HMF2_SOLAR_FACTOR's own "height increase per 100 SFU"
definition. The sign is now positive; hmF2 increases with F10.7.
"""

from datetime import datetime, timezone

import pytest

from hf_timestd.core.ionospheric_model import IonosphericModel, HMF2_SOLAR_FACTOR

_T = datetime(2026, 3, 15, 18, 0, 0, tzinfo=timezone.utc)


def _model():
    return IonosphericModel(enable_iri=False, enable_calibration=False)


def test_hmf2_rises_with_solar_flux():
    model = _model()
    low = model._get_parametric_heights(_T, 40.0, -95.0, f107=70.0)
    high = model._get_parametric_heights(_T, 40.0, -95.0, f107=250.0)
    assert high.hmF2 > low.hmF2


def test_solar_term_magnitude_matches_factor():
    # Only F10.7 differs, so the hmF2 delta is purely the solar term.
    model = _model()
    low = model._get_parametric_heights(_T, 40.0, -95.0, f107=70.0)
    high = model._get_parametric_heights(_T, 40.0, -95.0, f107=250.0)
    expected = HMF2_SOLAR_FACTOR * (250.0 - 70.0)
    assert (high.hmF2 - low.hmF2) == pytest.approx(expected, abs=1e-6)


def test_pivot_flux_gives_no_solar_shift():
    # f107 = 100 is the pivot (f107 - 100 == 0); it must match the no-flux case.
    model = _model()
    none_f107 = model._get_parametric_heights(_T, 40.0, -95.0, f107=None)
    pivot = model._get_parametric_heights(_T, 40.0, -95.0, f107=100.0)
    assert none_f107.hmF2 == pytest.approx(pivot.hmF2, abs=1e-6)
