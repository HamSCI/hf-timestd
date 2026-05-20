"""Regression tests for P-H15: curved-Earth MUF in propagation_model.

The MUF feasibility gate used the flat-Earth secant law, foF2/sin(elev). On a
curved Earth the reflecting layer curves away from the launch point, so the
ray's incidence angle i0 is steeper — sin(i0) = R*cos(elev)/(R+h) — and the
true MUF, foF2/cos(i0), is strictly lower. The flat-Earth value overestimates
the MUF and mis-gates high-band short-path modes as feasible.
"""

import math
from datetime import datetime, timezone

import pytest

from hf_timestd.core.propagation_model import HFPropagationModel, EARTH_RADIUS_KM

_T = datetime(2026, 3, 15, 18, 0, 0, tzinfo=timezone.utc)
_STATIONS = ('WWV', 'WWVH', 'CHU', 'BPM')
_FREQS = (5.0, 10.0, 15.0)


def _curved_muf(f_critical, elev_deg, height_km):
    R = EARTH_RADIUS_KM
    sin_i0 = min(1.0, R * math.cos(math.radians(elev_deg)) / (R + height_km))
    cos_i0 = math.sqrt(max(0.0, 1.0 - sin_i0 ** 2))
    return f_critical / max(cos_i0, 1e-3)


class TestCurvedEarthMUF:
    def setup_method(self):
        self.model = HFPropagationModel(
            receiver_lat=38.92, receiver_lon=-92.13,
            enable_realtime=False,  # parametric fallback — deterministic, no I/O
        )

    def _feasible_f_arrivals(self):
        """Every feasible F-layer arrival across stations/frequencies."""
        for station in _STATIONS:
            for freq in _FREQS:
                pred = self.model.predict(station, freq, _T)
                for a in pred.arrivals:
                    if a.is_feasible and a.mode.layer == 'F':
                        yield a

    def test_muf_matches_curved_earth_secant_law(self):
        # For an F-layer arrival, foF2_MHz carries the reflecting-layer
        # critical frequency and reflection_height_km carries h.
        for a in self._feasible_f_arrivals():
            expected = _curved_muf(a.foF2_MHz, a.elevation_angle_deg,
                                   a.reflection_height_km)
            assert a.muf_MHz == pytest.approx(expected, rel=1e-6)

    def test_curved_muf_is_below_flat_earth_muf(self):
        # The flat-Earth secant law foF2/sin(elev) is an overestimate; the
        # curved-Earth MUF is strictly lower at every oblique elevation.
        checked = 0
        for a in self._feasible_f_arrivals():
            # Genuinely oblique, above-horizon arrivals only: near-vertical
            # (>=89°) coincides with flat-Earth, and the flat-Earth law is
            # undefined for the negative elevations a multi-hop geometry can
            # produce.
            if not (0.0 < a.elevation_angle_deg < 89.0):
                continue
            flat = a.foF2_MHz / math.sin(math.radians(a.elevation_angle_deg))
            assert a.muf_MHz < flat
            checked += 1
        assert checked > 0  # at least one oblique arrival was exercised

    def test_muf_is_finite_at_the_horizon(self):
        # The old flat-Earth law diverged as elev -> 0; the curved law stays
        # finite (sin(i0) <= R/(R+h) < 1, so cos(i0) > 0).
        muf = _curved_muf(8.0, 0.0, 300.0)
        assert math.isfinite(muf)
        # ~3.4x foF2 at the horizon for a 300 km layer.
        assert 3.0 * 8.0 < muf < 3.7 * 8.0
