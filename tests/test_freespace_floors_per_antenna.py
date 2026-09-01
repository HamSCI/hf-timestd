#!/usr/bin/env python3
"""The deployed engine's free-space floors must use the radiating antenna.

`arrival_windows` takes `floors_ms` — the great-circle free-space time to each
station — as a hard early bound, because nothing accelerates: an arrival
earlier than light is not that station by any mechanism. The engine computed
those floors from each station's SITE coordinate.

NIST publishes WWVH's four antennas separately, and every metrology channel is
one frequency, so the antenna that radiates a channel is knowable. The replay
harness already uses it. While the engine does not, the two adjudicate against
different windows — up to ~0.17 us apart on WWVH — and a cascade validated
off-station would not be the cascade running live.
"""

import pytest

from hamsci_dsp.stations import BUILTIN_CATALOG
from hf_timestd.core.metrology_engine import freespace_floors_ms

# AC0G / B4, Columbia MO.
LAT, LON = 38.9187497, -92.1277207


def test_floors_are_returned_for_the_live_stations():
    floors = freespace_floors_ms(LAT, LON, 10.0)
    assert {"WWV", "WWVH", "BPM"} <= set(floors)
    assert all(v > 0 for v in floors.values())


def test_wwvh_floor_tracks_the_antenna_for_that_frequency():
    """WWVH's four antennas sit apart; a 5 MHz channel must not be floored
    by the 15 MHz antenna's distance."""
    got = {f: freespace_floors_ms(LAT, LON, f)["WWVH"] for f in (2.5, 5.0, 10.0, 15.0)}
    assert len(set(got.values())) == 4, f"all four frequencies gave the same floor: {got}"


def test_wwv_has_one_floor_across_its_band():
    """NIST publishes only a SITE coordinate for WWV — no antenna table — so
    every WWV channel shares one floor. Inventing per-frequency values here
    would be fabricating precision."""
    got = {f: freespace_floors_ms(LAT, LON, f)["WWV"]
           for f in BUILTIN_CATALOG.get("WWV").frequencies_mhz}
    assert len(set(got.values())) == 1


def test_a_floor_is_never_later_than_the_site_distance_by_much():
    """Sanity: the antenna is on the same site, so the floor moves metres,
    not kilometres."""
    from hamsci_dsp.geometry import great_circle_km

    C = 299.792458
    site = great_circle_km(LAT, LON, *BUILTIN_CATALOG.get("WWVH").coordinates) / C
    for f in (2.5, 5.0, 10.0, 15.0):
        got = freespace_floors_ms(LAT, LON, f)["WWVH"]
        assert abs(got - site) < 0.001, f"{f} MHz floor moved {abs(got-site)*1000:.2f} us"


def test_retired_stations_get_no_floor():
    floors = freespace_floors_ms(LAT, LON, 10.0)
    assert "CHU" not in floors
