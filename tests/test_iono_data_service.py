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
    IonoDataService, IonoGrid, WAMIPE_S3_BASE_URL, WAMIPE_CACHE_MAX_AGE_S,
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
    return IonoDataService(
        cache_dir=str(tmp_path), enable_wamipe=False, enable_giro=False,
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

def _giro(monkeypatch, text):
    monkeypatch.setattr(
        iono_data_service, '_requests',
        _FakeRequests(_FakeResp(text=text)),
    )


def test_giro_parsed_by_header_column_name(service, monkeypatch):
    _giro(monkeypatch,
          "# Time CS foF2 QD hmF2 QD\n"
          "2026-05-18T12:00:00.000Z 100 8.5 // 280.0 //\n")
    m = service._fetch_giro_station_data('AB123')
    assert m is not None
    assert m.foF2_MHz == pytest.approx(8.5)
    assert m.hmF2_km == pytest.approx(280.0)


def test_giro_out_of_range_value_is_rejected(service, monkeypatch):
    # hmF2 column corrupted to 5 km — physically impossible.
    _giro(monkeypatch,
          "# Time CS foF2 QD hmF2 QD\n"
          "2026-05-18T12:00:00.000Z 100 8.5 // 5.0 //\n")
    assert service._fetch_giro_station_data('AB123') is None


def test_giro_headerless_positional_fallback(service, monkeypatch):
    _giro(monkeypatch, "2026-05-18T12:00:00.000Z 8.5 280.0 95\n")
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


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
