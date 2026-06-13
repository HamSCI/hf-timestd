"""Regression tests for iono_data_service remediation findings.

* P-H19 — the WAM-IPE S3 fallback must list the prefix and resolve the
  latest ipe05 .nc object; an XML body must never reach the NetCDF parser.
* P-H20 — the GIRO DIDBase response must be parsed by column name, and
  values range-validated against ionospheric physics.
* P-H22 — get_iono_params must not serve a stale WAM-IPE grid as current.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from hf_timestd.core import iono_data_service
from hf_timestd.core.iono_data_service import (
    IonoDataService, IonoGrid, GiroStation, GiroMeasurement,
    WAMIPE_S3_BASE_URL, WAMIPE_CACHE_MAX_AGE_S,
)


class _FakeResp:
    def __init__(self, *, status=200, content=b'', text='', content_type=''):
        self.status_code = status
        self.content = content
        self.text = text
        self.headers = {'content-type': content_type}


class _FakeRequests:
    """Minimal stand-in for the optional ``requests`` dependency."""
    RequestException = Exception

    def __init__(self, response):
        self._response = response
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(url)
        r = self._response
        return r(url, **kw) if callable(r) else r


@pytest.fixture
def service(tmp_path):
    # enable_iri_fallback=False keeps these unit tests hermetic: the no-grid
    # base is the in-module climatology, with no IonosphericModel / IRI /
    # space-weather dependency pulled in.
    return IonoDataService(
        cache_dir=str(tmp_path), enable_wamipe=False, enable_giro=False,
        enable_iri_fallback=False,
    )


# --- P-H19 — S3 fallback resolves a real .nc object -----------------------

_S3_LISTING = b"""<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <Name>noaa-nws-wam-ipe-pds</Name>
  <Contents><Key>v1.2/wfs.20260518/00/ipe05_20260518_000000.nc</Key></Contents>
  <Contents><Key>v1.2/wfs.20260518/00/ipe05_20260518_001500.nc</Key></Contents>
  <Contents><Key>v1.2/wfs.20260518/00/index.html</Key></Contents>
  <Contents><Key>v1.2/wfs.20260518/00/ipe05_20260518_000500.nc</Key></Contents>
</ListBucketResult>
"""


def test_s3_fallback_resolves_latest_nc(service, monkeypatch):
    monkeypatch.setattr(
        iono_data_service, '_requests',
        _FakeRequests(_FakeResp(content=_S3_LISTING)),
    )
    url = service._resolve_s3_latest_nc('v1.2/wfs.20260518')
    # Latest by timestamped key, .html ignored.
    assert url == (f"{WAMIPE_S3_BASE_URL}/"
                   f"v1.2/wfs.20260518/00/ipe05_20260518_001500.nc")


def test_s3_fallback_empty_listing_returns_none(service, monkeypatch):
    empty = b'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"/>'
    monkeypatch.setattr(
        iono_data_service, '_requests',
        _FakeRequests(_FakeResp(content=empty)),
    )
    assert service._resolve_s3_latest_nc('v1.2/wfs.20260518') is None


def test_xml_body_is_not_parsed_as_netcdf(service, monkeypatch):
    """An XML content-type (an S3 listing / error doc) must be rejected
    before the NetCDF parser, not handed to it."""
    monkeypatch.setattr(
        iono_data_service, '_requests',
        _FakeRequests(_FakeResp(content=_S3_LISTING,
                                content_type='application/xml')),
    )
    assert service._download_and_parse_wamipe('http://x/listing') is None


# --- P-H20 — GIRO parsed by column name + range-validated -----------------

def _giro(service, text):
    # GIRO measurement fetching now uses the shared net_fetch session
    # (service._session); stub it with a fake whose .get() returns `text`.
    service._session = _FakeRequests(_FakeResp(text=text))


def test_giro_parsed_by_header_column_name(service):
    _giro(service,
          "# Time CS foF2 QD hmF2 QD\n"
          "2026-05-18T12:00:00.000Z 100 8.5 // 280.0 //\n")
    m = service._fetch_giro_station_data('AB123')
    assert m is not None
    assert m.foF2_MHz == pytest.approx(8.5)
    assert m.hmF2_km == pytest.approx(280.0)


def test_giro_out_of_range_value_is_rejected(service):
    # hmF2 column corrupted to 5 km — physically impossible.
    _giro(service,
          "# Time CS foF2 QD hmF2 QD\n"
          "2026-05-18T12:00:00.000Z 100 8.5 // 5.0 //\n")
    assert service._fetch_giro_station_data('AB123') is None


def test_giro_headerless_positional_fallback(service):
    _giro(service, "2026-05-18T12:00:00.000Z 8.5 280.0 95\n")
    m = service._fetch_giro_station_data('AB123')
    assert m is not None
    assert m.foF2_MHz == pytest.approx(8.5)
    assert m.hmF2_km == pytest.approx(280.0)


# --- P-H22 — stale grid must fall back to climatology ---------------------

def _grid(age_s: float) -> IonoGrid:
    ts = datetime.now(timezone.utc) - timedelta(seconds=age_s)
    return IonoGrid(
        timestamp=ts, source='wamipe',
        lats=np.array([30.0, 40.0]), lons=np.array([-100.0, -90.0]),
        hmF2=np.full((2, 2), 300.0), NmF2=np.full((2, 2), 1e12),
        TEC=np.full((2, 2), 20.0),
    )


def test_fresh_grid_is_used(service):
    service._current_grid = _grid(age_s=60.0)
    point = service.get_iono_params(35.0, -95.0)
    assert 'wamipe' in point.source


def test_stale_grid_falls_back_to_climatology(service):
    service._current_grid = _grid(age_s=WAMIPE_CACHE_MAX_AGE_S + 600.0)
    point = service.get_iono_params(35.0, -95.0)
    assert 'wamipe' not in point.source


# --- P-M16 — temporal interpolation across the previous/current grids -----

def _uniform_grid(ts: datetime, hmF2: float, NmF2: float, TEC: float) -> IonoGrid:
    """A grid with spatially uniform fields, so interpolate() returns the
    constant and a test can isolate the *temporal* blend."""
    return IonoGrid(
        timestamp=ts, source='wamipe',
        lats=np.array([30.0, 40.0]), lons=np.array([-100.0, -90.0]),
        hmF2=np.full((2, 2), hmF2), NmF2=np.full((2, 2), NmF2),
        TEC=np.full((2, 2), TEC),
    )


def test_temporal_interpolation_blends_grids(service):
    """A query time between the two grids' valid times blends them."""
    now = datetime.now(timezone.utc)
    t_prev = now - timedelta(minutes=10)
    t_curr = now - timedelta(minutes=2)
    service._previous_grid = _uniform_grid(t_prev, 300.0, 1.0e12, 18.0)
    service._current_grid = _uniform_grid(t_curr, 320.0, 1.4e12, 26.0)

    # Query exactly midway between the two valid times → halfway blend.
    q = t_prev + (t_curr - t_prev) / 2
    point = service.get_iono_params(35.0, -95.0, q)
    assert point.hmF2_km == pytest.approx(310.0)
    assert point.TEC_TECU == pytest.approx(22.0)


def test_temporal_interpolation_clamps_after_window(service):
    """A query after the current grid's valid time uses the current grid
    unchanged — the blend never extrapolates."""
    now = datetime.now(timezone.utc)
    service._previous_grid = _uniform_grid(
        now - timedelta(minutes=10), 300.0, 1.0e12, 18.0)
    service._current_grid = _uniform_grid(
        now - timedelta(minutes=2), 320.0, 1.4e12, 26.0)

    point = service.get_iono_params(35.0, -95.0, now)  # after t_curr
    assert point.hmF2_km == pytest.approx(320.0)


# --- P-M16 — grid validation: ascending coords, finite physical fields ----

def test_validate_grid_normalises_descending_latitude(service):
    """NetCDF latitude stored north-to-south is reordered to strictly
    ascending, with the field arrays reordered to match."""
    grid = IonoGrid(
        timestamp=datetime.now(timezone.utc), source='wamipe',
        lats=np.array([40.0, 30.0]),                       # descending
        lons=np.array([-100.0, -90.0]),
        hmF2=np.array([[310.0, 311.0], [320.0, 321.0]]),   # row 0 = lat 40
        NmF2=np.full((2, 2), 1e12), TEC=np.full((2, 2), 20.0),
    )
    v = service._validate_grid(grid)
    assert v is not None
    assert list(v.lats) == [30.0, 40.0]
    # The row for lat=30 must carry lat=40's original neighbour values.
    assert list(v.hmF2[0]) == [320.0, 321.0]
    assert list(v.hmF2[1]) == [310.0, 311.0]


def test_validate_grid_replaces_fill_values(service):
    """NaN / sentinel fill cells are replaced with the field median."""
    hmF2 = np.full((2, 2), 300.0)
    hmF2[0, 0] = np.nan
    hmF2[1, 1] = 9.99e36                  # classic NetCDF fill sentinel
    grid = IonoGrid(
        timestamp=datetime.now(timezone.utc), source='wamipe',
        lats=np.array([30.0, 40.0]), lons=np.array([-100.0, -90.0]),
        hmF2=hmF2, NmF2=np.full((2, 2), 1e12), TEC=np.full((2, 2), 20.0),
    )
    v = service._validate_grid(grid)
    assert v is not None
    assert np.all(np.isfinite(v.hmF2))
    assert np.all((v.hmF2 >= 100.0) & (v.hmF2 <= 600.0))


def test_validate_grid_rejects_all_bad_field(service):
    """A field with no valid cell at all rejects the whole grid."""
    grid = IonoGrid(
        timestamp=datetime.now(timezone.utc), source='wamipe',
        lats=np.array([30.0, 40.0]), lons=np.array([-100.0, -90.0]),
        hmF2=np.full((2, 2), np.nan), NmF2=np.full((2, 2), 1e12),
        TEC=np.full((2, 2), 20.0),
    )
    assert service._validate_grid(grid) is None


# --- P-M16 — GIRO correction uses great-circle km distance ----------------

def test_giro_correction_is_dateline_safe(service):
    """Great-circle distance: a GIRO station 2° away across the ±180°
    dateline is near (~222 km), not the 358° the old degree-Euclidean
    distance computed — so its correction is applied at near-full weight."""
    now = datetime.now(timezone.utc)
    service._giro_stations = [
        GiroStation(code='XYZ', name='Dateline', latitude=0.0, longitude=-179.0)
    ]
    service._giro_measurements = {
        'XYZ': GiroMeasurement(
            station_code='XYZ', timestamp=now,
            foF2_MHz=9.0, hmF2_km=320.0, confidence=1.0,
        )
    }
    corr = service._get_giro_correction(0.0, 179.0, now)
    assert corr is not None
    hmF2, foF2, weight = corr
    assert hmF2 == pytest.approx(320.0)
    assert weight > 0.8  # ~222 km away → near full weight


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
