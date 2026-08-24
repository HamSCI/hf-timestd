"""wwv_constants re-exports station data from hamsci_dsp.stations.

Split design §5.2: pure station data becomes the hamsci-dsp
StationCatalog; ``wwv_constants`` re-exports for back-compat and keeps
timing schedules/thresholds locally.  These tests pin the re-export so
the two sources can never drift apart.
"""
import pytest

from hamsci_dsp.stations import BUILTIN_CATALOG
from hf_timestd.core import wwv_constants as wc


class TestStationDataComesFromTheCatalog:
    def test_station_locations_is_the_catalog_shape(self):
        assert wc.STATION_LOCATIONS == BUILTIN_CATALOG.locations()

    def test_scalar_coordinates_match_the_catalog(self):
        for name, (lat_attr, lon_attr) in {
            "WWV": ("WWV_LAT", "WWV_LON"),
            "WWVH": ("WWVH_LAT", "WWVH_LON"),
            "CHU": ("CHU_LAT", "CHU_LON"),
            "BPM": ("BPM_LAT", "BPM_LON"),
            "WWVB": ("WWVB_LAT", "WWVB_LON"),
        }.items():
            s = BUILTIN_CATALOG.get(name)
            assert getattr(wc, lat_attr) == pytest.approx(s.lat), name
            assert getattr(wc, lon_attr) == pytest.approx(s.lon), name

    def test_coordinate_tuples_match(self):
        assert wc.WWV_COORDINATES == BUILTIN_CATALOG.get("WWV").coordinates
        assert wc.BPM_COORDINATES == BUILTIN_CATALOG.get("BPM").coordinates

    def test_frequency_lists_match_the_catalog(self):
        assert tuple(wc.WWV_FREQUENCIES) == BUILTIN_CATALOG.get("WWV").frequencies_mhz
        assert tuple(wc.WWVH_FREQUENCIES) == BUILTIN_CATALOG.get("WWVH").frequencies_mhz
        assert tuple(wc.CHU_FREQUENCIES) == BUILTIN_CATALOG.get("CHU").frequencies_mhz
        assert tuple(wc.BPM_FREQUENCIES) == BUILTIN_CATALOG.get("BPM").frequencies_mhz

    def test_module_exposes_the_catalog_itself(self):
        # New consumers should reach the catalog through wwv_constants
        # during the shim era, then import hamsci_dsp.stations directly.
        assert wc.STATION_CATALOG is BUILTIN_CATALOG
