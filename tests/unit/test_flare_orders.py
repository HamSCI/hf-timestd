"""Flare orders: the GOES list to recording orders for B4 (stdlib module)."""
import json
import os
from datetime import datetime, timezone
from unittest import mock

import pytest

from hf_timestd.events import flare_orders as fo

B4 = {"id": "AC0G-B4", "lat": 38.9187367, "lon": -92.1277335,
      "paths": [list(fo.WWV), list(fo.WWVH)]}
NOW = datetime(2026, 11, 6, 20, 0, tzinfo=timezone.utc)
DATA = os.path.join(os.path.dirname(__file__), "data", "goes_xray_flares_7day.json")


def _utc(h, m=0):
    return datetime(2026, 11, 6, h, m, tzinfo=timezone.utc)


def _flare(cls, peak, begin=None, end="DEFAULT"):
    return {"begin_time": begin or peak, "max_time": peak, "max_class": cls,
            "end_time": peak if end == "DEFAULT" else end, "satellite": 19}


def test_class_values():
    assert fo.flare_class_value("M1.0") == 1e-5
    assert fo.flare_class_value("X2.3") == pytest.approx(2.3e-4)
    assert fo.flare_class_value("C9.9") < fo.flare_class_value("M1.0")


def test_solar_elevation_matches_hamsci_dsp():
    solar = pytest.importorskip("hamsci_dsp.ionosphere.solar")
    for hour in (18, 6):
        _, ref = solar.solar_position(_utc(hour), B4["lat"], B4["lon"])
        assert abs(fo.solar_elevation_deg(_utc(hour), B4["lat"], B4["lon"]) - ref) < 0.5
    assert fo.solar_elevation_deg(_utc(18), B4["lat"], B4["lon"]) > 0
    assert fo.solar_elevation_deg(_utc(6), B4["lat"], B4["lon"]) < 0


def test_midpoint_wwv_b4():
    lat, lon = fo.path_midpoint(B4["lat"], B4["lon"], *fo.WWV)
    assert 38.9 < lat < 40.7
    assert -105.0407 < lon < -92.1277


def test_night_flare_makes_no_order():
    fl = _flare("M5.0", "2026-11-06T06:00:00Z", end="2026-11-06T06:30:00Z")
    assert fo.orders_from_flares([fl], [B4], NOW) == []


def test_day_flare_makes_order():
    fl = _flare("M1.2", "2026-11-06T18:00:00Z", "2026-11-06T17:50:00Z", "2026-11-06T18:20:00Z")
    (order,) = fo.orders_from_flares([fl], [B4], NOW)
    assert order["start_utc"] == "2026-11-06T17:20:00Z"
    assert order["end_utc"] == "2026-11-06T19:50:00Z"
    assert order["event_id"] == "20261106T1800Z-flare-M1.2"
    assert order["stations"] == ["AC0G-B4"]
    assert order["source"] == fl
    assert order["created_utc"] == "2026-11-06T20:00:00Z"


def test_c_flare_ignored():
    fl = _flare("C9.9", "2026-11-06T18:00:00Z", end="2026-11-06T18:20:00Z")
    assert fo.orders_from_flares([fl], [B4], NOW) == []


def test_open_flare_waits():
    fl = _flare("M1.2", "2026-11-06T18:00:00Z", end=None)
    assert fo.orders_from_flares([fl], [B4], NOW) == []
    fl["end_time"] = "2026-11-06T18:20:00Z"
    assert len(fo.orders_from_flares([fl], [B4], NOW)) == 1


def test_duplicate_flare_first_wins():
    a = _flare("M1.2", "2026-11-06T18:00:20Z", end="2026-11-06T18:20:00Z")
    b = dict(a, satellite=18, max_time="2026-11-06T18:00:50Z")
    (order,) = fo.orders_from_flares([a, b], [B4], NOW)
    assert order["source"]["satellite"] == 19


def test_merge_is_idempotent():
    fl = _flare("M1.2", "2026-11-06T18:00:00Z", end="2026-11-06T18:20:00Z")
    first = fo.merge_orders({"version": 1, "orders": []},
                            fo.orders_from_flares([fl], [B4], NOW))
    later = datetime(2026, 11, 6, 21, 0, tzinfo=timezone.utc)
    again = fo.merge_orders(first, fo.orders_from_flares([fl], [B4], later))
    assert len(again["orders"]) == 1
    assert again["orders"][0]["created_utc"] == "2026-11-06T20:00:00Z"


def test_saved_feed_parses():
    with open(DATA) as f:
        flares = json.load(f)
    # The saved week holds one M flare, an M3.5 peaking 18:33Z, midday at B4.
    orders = fo.orders_from_flares(flares, [B4], NOW)
    assert [o["event_id"] for o in orders] == ["20261105T1833Z-flare-M3.5"]


def test_coordinates_match_broadcast_specs():
    specs = pytest.importorskip("hf_timestd.core.broadcast_specs")
    assert fo.WWV == specs.STATION_COORDINATES[specs.Station.WWV]
    assert fo.WWVH == specs.STATION_COORDINATES[specs.Station.WWVH]


def test_main_fetch_failure_leaves_file(tmp_path):
    st = tmp_path / "st.json"
    st.write_text(json.dumps([B4]))
    out = tmp_path / "orders.json"
    out.write_text('{"version": 1, "orders": []}')
    with mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
        assert fo.main(["--stations", str(st), "--out", str(out)]) == 1
    assert out.read_text() == '{"version": 1, "orders": []}'


def test_main_writes_orders(tmp_path):
    st = tmp_path / "st.json"
    st.write_text(json.dumps([B4]))
    out = tmp_path / "orders.json"
    feed = tmp_path / "feed.json"
    feed.write_text(json.dumps([_flare("M1.2", "2026-11-06T18:00:00Z",
                                       end="2026-11-06T18:20:00Z")]))
    assert fo.main(["--stations", str(st), "--out", str(out),
                    "--url", feed.as_uri()]) == 0
    assert len(json.loads(out.read_text())["orders"]) == 1


def _order_at(h, m, cls="M1.2"):
    pk = "2026-11-06T%02d:%02d:00Z" % (h, m)
    return fo.orders_from_flares([_flare(cls, pk, end=pk)], [B4], NOW)


def test_station_sunlit_but_paths_dark_makes_no_order():
    # 13:00Z: sun up at B4, down at both path midpoints.
    assert fo.solar_elevation_deg(_utc(13), B4["lat"], B4["lon"]) > 0
    for p in B4["paths"]:
        mid = fo.path_midpoint(B4["lat"], B4["lon"], *p)
        assert fo.solar_elevation_deg(_utc(13), *mid) < 0
    assert _order_at(13, 0) == []


def test_station_dark_but_midpoint_lit_makes_no_order():
    # 23:10Z: sun down at B4, up at the WWV midpoint.
    assert fo.solar_elevation_deg(_utc(23, 10), B4["lat"], B4["lon"]) < 0
    mid = fo.path_midpoint(B4["lat"], B4["lon"], *fo.WWV)
    assert fo.solar_elevation_deg(_utc(23, 10), *mid) > 0
    assert _order_at(23, 10) == []


def test_m1_boundary():
    assert len(_order_at(18, 0, "M1.0")) == 1
    assert _order_at(18, 0, "M0.9") == []


def test_odd_classes():
    assert fo.flare_class_value("M1") == 1e-5
    assert fo.flare_class_value("X10") == pytest.approx(1e-3)
    assert len(_order_at(18, 0, "M1")) == 1
    assert len(_order_at(18, 0, "X10")) == 1
    for bad in ("", None, "MNaN", "Minf", "M", "Z5.0", 5):
        assert _order_at(18, 0, bad) == [], bad


def test_midpoint_antimeridian():
    lat, lon = fo.path_midpoint(0, 170, 0, -170)
    assert lat == pytest.approx(0, abs=1e-9)
    assert abs(abs(lon) - 180) < 1e-9
    lat, lon = fo.path_midpoint(10, 175, 10, -165)
    assert lon == pytest.approx(-175.0)


def test_main_rejects_non_list_feed(tmp_path):
    st = tmp_path / "st.json"
    st.write_text(json.dumps([B4]))
    out = tmp_path / "orders.json"
    out.write_text('{"version": 1, "orders": []}')
    feed = tmp_path / "feed.json"
    feed.write_text('{"error": "nope"}')
    assert fo.main(["--stations", str(st), "--out", str(out),
                    "--url", feed.as_uri()]) == 1
    assert out.read_text() == '{"version": 1, "orders": []}'


def test_main_write_failure_cleans_tmp(tmp_path):
    st = tmp_path / "st.json"
    st.write_text(json.dumps([B4]))
    out = tmp_path / "orders.json"
    feed = tmp_path / "feed.json"
    feed.write_text(json.dumps([_flare("M1.2", "2026-11-06T18:00:00Z",
                                       end="2026-11-06T18:20:00Z")]))
    with mock.patch("os.replace", side_effect=OSError("disk")):
        assert fo.main(["--stations", str(st), "--out", str(out),
                        "--url", feed.as_uri()]) == 1
    assert not out.exists()
    assert not (tmp_path / "orders.json.tmp").exists()
