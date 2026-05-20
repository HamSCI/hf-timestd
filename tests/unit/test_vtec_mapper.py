"""
Regression tests for P-H10 in vtec_mapper.py.

The regional VTEC map fits a degree-2 2D polynomial to vTEC at ionospheric
pierce points. Three defects were addressed:

- The fit used a plain ``lstsq(rcond=None)``. With clustered IPPs the
  polynomial basis is nearly collinear, so the surface oscillated wildly
  off-cluster while the in-sample RMS still looked good. Tikhonov (ridge)
  regularisation now damps the unconstrained terms.
- The conditioning of the design matrix was never checked. ``generate_map``
  now computes it, reports it as ``condition_number``, warns when it is
  excessive, and folds it into ``confidence``.
- The grid was evaluated everywhere, including pure extrapolation far from
  any IPP. Cells outside the convex hull of the IPPs are now masked to NaN.
"""

import math

import numpy as np
import pytest

from hf_timestd.core.vtec_mapper import (
    VTECMapper,
    IPPMeasurement,
    MAX_CONDITION_NUMBER,
)


def _ipp(lat, lon, vtec, unc=2.0):
    return IPPMeasurement(
        station='WWV', frequency_mhz=10.0, ipp_lat=lat, ipp_lon=lon,
        stec_tecu=vtec * 1.3, vtec_tecu=vtec, mapping_factor=1.3,
        elevation_deg=45.0, uncertainty_tecu=unc,
    )


def _hexagon_ipps(center_lat=39.0, center_lon=-92.0, radius=4.0):
    """Seven well-spread IPPs (a hexagon plus its centre) with vTEC lying on
    a clean plane — a well-conditioned fitting problem."""
    meas = []
    for k in range(6):
        a = math.radians(60.0 * k)
        lat = center_lat + radius * math.sin(a)
        lon = center_lon + radius * math.cos(a)
        vtec = 25.0 + 0.4 * (lat - center_lat) + 0.25 * (lon - center_lon)
        meas.append(_ipp(lat, lon, vtec))
    meas.append(_ipp(center_lat, center_lon, 25.0))
    return meas


def _clustered_ipps(spread_deg, n=8, center_lat=39.0, center_lon=-92.0, seed=0):
    """n IPPs packed into a tiny ±spread_deg box — an ill-conditioned fit."""
    rng = np.random.default_rng(seed)
    return [_ipp(center_lat + rng.uniform(-spread_deg, spread_deg),
                 center_lon + rng.uniform(-spread_deg, spread_deg),
                 25.0 + rng.uniform(-2.0, 2.0)) for _ in range(n)]


class TestConvexHullMasking:
    def test_cells_outside_ipp_hull_are_masked(self):
        result = VTECMapper(receiver_lat=38.92, receiver_lon=-92.13).generate_map(_hexagon_ipps())
        assert result is not None
        grid = np.array(result.grid_vtec, dtype=float)

        # Far-corner cells are pure extrapolation -> NaN.
        assert math.isnan(grid[0, 0])
        # The grid carries both masked and interpolated cells.
        assert np.isnan(grid).any()
        assert np.isfinite(grid).any()

        # The cell at the IPP centroid is interpolated, near the input plane.
        gl = np.array(result.grid_lats)
        glon = np.array(result.grid_lons)
        i = int(np.argmin(np.abs(gl - 39.0)))
        j = int(np.argmin(np.abs(glon + 92.0)))
        assert math.isfinite(grid[i, j])
        assert 20.0 < grid[i, j] < 30.0

    def test_interpolated_cells_are_finite_and_nonnegative(self):
        result = VTECMapper(receiver_lat=38.92, receiver_lon=-92.13).generate_map(_hexagon_ipps())
        grid = np.array(result.grid_vtec, dtype=float)
        finite = grid[np.isfinite(grid)]
        assert finite.size > 0
        assert (finite >= 0.0).all()

    def test_collinear_ipps_fall_back_to_no_masking(self):
        # Five IPPs on a diagonal line: no convex hull exists, so no
        # interpolation domain can be defined — the map must still return a
        # result and simply evaluate every cell.
        meas = [_ipp(38.0 + 0.5 * k, -92.0 + 0.5 * k, 25.0 + k) for k in range(5)]
        result = VTECMapper(receiver_lat=38.92, receiver_lon=-92.13).generate_map(meas)
        assert result is not None
        grid = np.array(result.grid_vtec, dtype=float)
        assert np.isfinite(grid).all()


class TestConditioning:
    def test_condition_number_is_reported(self):
        result = VTECMapper(receiver_lat=38.92, receiver_lon=-92.13).generate_map(_hexagon_ipps())
        assert result.condition_number > 0.0
        assert math.isfinite(result.condition_number)

    def test_clustered_ipps_are_worse_conditioned_than_spread(self):
        spread = VTECMapper(receiver_lat=38.92, receiver_lon=-92.13).generate_map(_hexagon_ipps())
        clustered = VTECMapper(receiver_lat=38.92, receiver_lon=-92.13).generate_map(_clustered_ipps(0.02))
        assert clustered.condition_number > spread.condition_number
        assert clustered.condition_number > 1.0e3
        # Poor conditioning collapses the reported confidence.
        assert clustered.confidence < spread.confidence

    def test_collinear_ipps_log_ill_conditioned_warning(self, caplog):
        # Eight collinear IPPs: the degree-2 basis is rank-deficient on a
        # line, so cond explodes past MAX_CONDITION_NUMBER. The fit must warn
        # and still return a bounded result via the regularised (full-rank)
        # system.
        meas = [_ipp(36.0 + 0.8 * k, -96.0 + 0.8 * k, 22.0 + 0.5 * k)
                for k in range(8)]
        with caplog.at_level('WARNING'):
            result = VTECMapper(receiver_lat=38.92, receiver_lon=-92.13).generate_map(meas)
        assert result is not None
        assert any('ill-conditioned' in r.message for r in caplog.records)
        assert result.condition_number > MAX_CONDITION_NUMBER
        assert result.confidence == 0.0


class TestRegularization:
    def test_ill_posed_fit_returns_bounded_coefficients(self):
        # A plain lstsq on clustered IPPs yields a wildly oscillating surface;
        # the ridge keeps every polynomial coefficient finite and bounded.
        result = VTECMapper(receiver_lat=38.92, receiver_lon=-92.13).generate_map(_clustered_ipps(0.05))
        assert result is not None
        assert all(math.isfinite(c) for c in result.poly_coeffs)
        assert math.isfinite(result.rms_residual_tecu)

    def test_well_conditioned_fit_recovers_the_plane(self):
        # On a clean plane with well-spread IPPs the ridge bias is negligible:
        # the fit is tight and confident.
        result = VTECMapper(receiver_lat=38.92, receiver_lon=-92.13).generate_map(_hexagon_ipps())
        assert result.rms_residual_tecu < 1.0
        assert result.confidence > 0.5
