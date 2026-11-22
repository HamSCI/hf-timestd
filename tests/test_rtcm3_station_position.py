"""RTCM3Adapter's station position: a receiver that is not a surveyed base sends
RTCM 1005/1006 with a zero antenna position.  AC0G-ND's LG290P did so, and
ecef_to_geodetic(0, 0, 0) divided by zero, crash-looping timestd-vtec
(2026-11-21).  An implausible position is now ignored; until a plausible one
arrives the adapter uses the station position from config, when given.

Coordinates here are made up (a point in Kansas), not any station's.
"""

import math
from types import SimpleNamespace

import pytest

from hf_timestd.core import rtcm3_adapter
from hf_timestd.core.broadcast_ephemeris import ecef_to_geodetic, geodetic_to_ecef
from hf_timestd.core.rtcm3_adapter import RTCM3Adapter

LAT, LON = 38.5, -98.0


def _arp(x, y, z):
    return SimpleNamespace(identity='1005', DF025=x, DF026=y, DF027=z)


def _adapter(monkeypatch, msgs, **kw):
    a = RTCM3Adapter(**kw)
    it = iter(msgs)
    monkeypatch.setattr(a, '_frames', lambda data: [b'f'] * len(msgs))
    a._parse = lambda frame: next(it)
    return a


def test_geodetic_round_trip():
    x, y, z = geodetic_to_ecef(LAT, LON, 300.0)
    lat, lon, h = ecef_to_geodetic(x, y, z)
    assert math.degrees(lat) == pytest.approx(LAT, abs=1e-7)
    assert math.degrees(lon) == pytest.approx(LON, abs=1e-7)
    assert h == pytest.approx(300.0, abs=1e-3)


def test_zero_position_raises_valueerror_not_zerodivision():
    with pytest.raises(ValueError):
        ecef_to_geodetic(0.0, 0.0, 0.0)


def test_zero_arp_is_ignored(monkeypatch):
    a = _adapter(monkeypatch, [_arp(0.0, 0.0, 0.0)])
    list(a.process_data(b'x'))
    assert a.station_ecef is None
    assert 'implausible' in a.status()


def test_plausible_arp_is_taken(monkeypatch):
    pos = geodetic_to_ecef(LAT, LON, 0.0)
    a = _adapter(monkeypatch, [_arp(*pos)])
    list(a.process_data(b'x'))
    assert a.station_ecef == pytest.approx(pos)
    assert a.status().count('known') == 1


def test_config_position_used_until_a_plausible_arp(monkeypatch):
    cfg = geodetic_to_ecef(LAT, LON, 0.0)
    a = _adapter(monkeypatch, [_arp(0.0, 0.0, 0.0)], fallback_ecef=cfg)
    assert a.station_ecef == pytest.approx(cfg)
    assert 'from config' in a.status()
    list(a.process_data(b'x'))          # the zero ARP does not displace it
    assert a.station_ecef == pytest.approx(cfg)
    real = geodetic_to_ecef(LAT + 0.01, LON, 250.0)
    a2 = _adapter(monkeypatch, [_arp(*real)], fallback_ecef=cfg)
    list(a2.process_data(b'x'))         # a plausible ARP wins over config
    assert a2.station_ecef == pytest.approx(real)


def test_implausible_fallback_is_refused():
    with pytest.raises(ValueError):
        RTCM3Adapter(fallback_ecef=(0.0, 0.0, 0.0))


def test_plausible_bounds():
    assert rtcm3_adapter.plausible_ecef(geodetic_to_ecef(LAT, LON, 0.0))
    assert rtcm3_adapter.plausible_ecef(geodetic_to_ecef(LAT, LON, 8000.0))
    assert not rtcm3_adapter.plausible_ecef((0.0, 0.0, 0.0))
    assert not rtcm3_adapter.plausible_ecef((1.0, 2.0, 3.0))
    assert not rtcm3_adapter.plausible_ecef((2.0e7, 0.0, 0.0))


def test_config_station_ecef():
    pos = rtcm3_adapter.config_station_ecef({'station': {'latitude': LAT, 'longitude': LON}})
    assert pos == pytest.approx(geodetic_to_ecef(LAT, LON, 0.0))
    assert rtcm3_adapter.config_station_ecef({}) is None
    assert rtcm3_adapter.config_station_ecef({'station': {'latitude': 'x', 'longitude': LON}}) is None
    assert rtcm3_adapter.config_station_ecef({'station': {'latitude': 120.0, 'longitude': LON}}) is None
