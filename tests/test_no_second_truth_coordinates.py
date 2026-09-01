#!/usr/bin/env python3
"""Station coordinates resolve through the catalogue, or they are a second truth.

WWV's position lived in eight places across three repos with five distinct
latitudes, 1.93 us of predicted delay apart.  The correct value has been in
this repo the whole time, in `broadcast_specs.py`, with NIST's figure cited in
the comment above it — while `timing_consistency_validator` and
`models/broadcast` carried the superseded one.

`hamsci_dsp.stations.BUILTIN_CATALOG` is the single source, and
`core/wwv_constants.py` is this repo's lookup onto it.  Anything that keeps its
own copy is a fact waiting to diverge, so compare them.
"""

import pytest

from hamsci_dsp.stations import BUILTIN_CATALOG


def _cat(name):
    return BUILTIN_CATALOG.get(name).coordinates


def test_wwv_constants_still_derives_from_the_catalogue():
    from hf_timestd.core import wwv_constants as wc

    assert (wc.WWV_LAT, wc.WWV_LON) == _cat("WWV")
    assert (wc.WWVH_LAT, wc.WWVH_LON) == _cat("WWVH")
    assert (wc.BPM_LAT, wc.BPM_LON) == _cat("BPM")


def test_timing_consistency_validator_agrees_with_the_catalogue():
    from hf_timestd.core.timing_consistency_validator import STATION_LOCATIONS

    for name, coords in STATION_LOCATIONS.items():
        assert coords == _cat(name), f"{name} disagrees with the catalogue"


def test_broadcast_specs_agrees_with_the_catalogue():
    from hf_timestd.core.broadcast_specs import STATION_COORDINATES

    for station, coords in STATION_COORDINATES.items():
        name = station.value if hasattr(station, "value") else str(station)
        assert coords == _cat(name), f"{name} disagrees with the catalogue"


def test_default_broadcast_stations_agree_with_the_catalogue():
    from hf_timestd.models.broadcast import DEFAULT_STATIONS

    for s in DEFAULT_STATIONS:
        want = _cat(s.name)
        assert (s.latitude, s.longitude) == want, (
            f"{s.name} disagrees with the catalogue")
