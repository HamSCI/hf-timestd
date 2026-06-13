#!/usr/bin/env python3
"""Unit tests for the external-data fetchers (GIRO + space weather).

These lock in the parser behaviour that the live audit (2026-06-13) fixed:
  - GIRO station list is HTML, not whitespace columns.
  - GIRO measurements come from DIDBGetValues (name-indexed columns).
  - WAM-IPE must never fabricate a grid from constant defaults.
  - SWPC / DSD / GFZ space-weather formats parse to physical values.

They run fully offline — no network — by feeding saved/synthetic payloads
to the pure parsing functions.
"""

import unittest
from datetime import datetime, timezone

from hf_timestd.core.iono_data_service import IonoDataService
from hf_timestd.core.space_weather import SpaceWeatherService


# A trimmed but format-faithful DIDBFastStationList row (two stations).
GIRO_STATION_HTML = """
<html><head><title>DIDBase Station list</title></head><body><table>
<tr><td><big>1</big></td>
<td><big><a href="http://lgdc.uml.edu:80/common/DIDBYearListForStation?ursiCode=BC840">BC840</a></big></td>
<td><big>BOULDER</big></td><td><big> 40.00</big></td><td><big> 254.70</big></td></tr>
<tr><td><big>2</big></td>
<td><big><a href="http://lgdc.uml.edu:80/common/DIDBYearListForStation?ursiCode=AH223">AH223</a></big></td>
<td><big>AHMEDABAD</big></td><td><big> 23.00</big></td><td><big> 72.50</big></td></tr>
</table></body></html>
"""

# Representative DIDBGetValues response: '#' metadata, a '#'-prefixed column
# header naming the requested chars, then ISO-timestamped data rows with QD
# flags between values.
DIDB_VALUES = """\
# Lowell GIRO Data Center / DIDBase
# License: CC-BY-NC-SA 4.0
# Location: BOULDER
#
#Time                     CS   foF2 QD   hmF2 QD
2026-06-13T02:30:00.000Z 100  5.20 //  280.5 //
2026-06-13T02:45:00.000Z  90  5.35 //  278.1 //
"""


class TestGiroStationParse(unittest.TestCase):
    def test_html_parse_normalises_longitude(self):
        stations = IonoDataService._parse_didbase_station_html(GIRO_STATION_HTML)
        self.assertEqual(len(stations), 2)
        by_code = {s.code: s for s in stations}
        self.assertIn("BC840", by_code)
        # 254.70 E must normalise to -105.30
        self.assertAlmostEqual(by_code["BC840"].longitude, -105.30, places=2)
        self.assertAlmostEqual(by_code["BC840"].latitude, 40.00, places=2)
        self.assertEqual(by_code["AH223"].name, "AHMEDABAD")
        self.assertAlmostEqual(by_code["AH223"].longitude, 72.50, places=2)

    def test_error_page_yields_no_stations(self):
        self.assertEqual(
            IonoDataService._parse_didbase_station_html("<html>503</html>"), []
        )

    def test_bundled_fallback_loads(self):
        svc = IonoDataService(cache_dir="/tmp/test_ext_iono", enable_wamipe=False)
        stations = svc._load_bundled_stations()
        self.assertGreater(len(stations), 50)
        # all coordinates physical, longitudes normalised
        for s in stations:
            self.assertTrue(-90 <= s.latitude <= 90)
            self.assertTrue(-180 <= s.longitude < 180.001)


class TestDidbValuesParse(unittest.TestCase):
    def test_latest_row_name_indexed(self):
        r = IonoDataService._parse_didb_characteristics(DIDB_VALUES)
        self.assertIsNotNone(r)
        foF2, hmF2, conf = r
        self.assertAlmostEqual(foF2, 5.35, places=2)   # last row
        self.assertAlmostEqual(hmF2, 278.1, places=1)
        self.assertAlmostEqual(conf, 0.90, places=2)   # CS=90 → 0.90

    def test_shuffled_columns_still_correct(self):
        txt = "#Time hmF2 QD foF2 QD CS\n2026-06-13T02:45:00.000Z 278.1 // 5.35 // 90\n"
        foF2, hmF2, conf = IonoDataService._parse_didb_characteristics(txt)
        self.assertAlmostEqual(foF2, 5.35, places=2)
        self.assertAlmostEqual(hmF2, 278.1, places=1)

    def test_out_of_range_rejected(self):
        txt = "#Time CS foF2 hmF2\n2026-06-13T02:45:00.000Z 90 999 9999\n"
        self.assertIsNone(IonoDataService._parse_didb_characteristics(txt))

    def test_html_error_rejected(self):
        self.assertIsNone(
            IonoDataService._parse_didb_characteristics("<html>503</html>")
        )

    def test_header_only_no_rows(self):
        self.assertIsNone(
            IonoDataService._parse_didb_characteristics("#Time CS foF2 hmF2\n")
        )


class TestWamipeNeverFabricates(unittest.TestCase):
    def test_no_wamipe_source_when_disabled(self):
        svc = IonoDataService(cache_dir="/tmp/test_ext_iono2", enable_wamipe=False,
                              enable_iri_fallback=False)
        p = svc.get_iono_params(40.0, -105.0, datetime.now(timezone.utc))
        self.assertNotIn("wamipe", p.source)
        # with IRI fallback off and no grid, must be the climatology base
        self.assertTrue(p.source.startswith("climatological_fallback"))


class TestSpaceWeatherParse(unittest.TestCase):
    def setUp(self):
        # Construct without touching the network or the singleton.
        self.svc = SpaceWeatherService(cache_dir="/tmp/test_ext_sw")

    def test_swpc_summary(self):
        self.svc._get_json = lambda url: [{"flux": 128, "time_tag": "2026-06-12T20:00:00"}]
        r = self.svc._fetch_f107_swpc_summary()
        self.assertEqual(r[0], 128.0)
        self.assertEqual(r[2], "swpc:summary")

    def test_swpc_summary_rejects_garbage(self):
        self.svc._get_json = lambda url: [{"flux": 99999, "time_tag": "x"}]
        self.assertIsNone(self.svc._fetch_f107_swpc_summary())

    def test_swpc_dsd_last_row(self):
        dsd = (":Product: daily-solar-indices.txt\n"
               "# header\n"
               "2026 06 11  127     81      485\n"
               "2026 06 12  128    113      430\n")
        self.svc._get_text = lambda url: dsd
        r = self.svc._fetch_f107_swpc_dsd()
        self.assertEqual(r[0], 128.0)
        self.assertEqual(r[2], "swpc:dsd")

    def test_swpc_planetary_kp_latest(self):
        self.svc._get_json = lambda url: [
            {"time_tag": "2026-06-12T21:00:00", "Kp": 3.0, "a_running": 15},
            {"time_tag": "2026-06-13T00:00:00", "Kp": 4.0, "a_running": 22},
        ]
        kp, ap, t, src = self.svc._fetch_kp_ap_swpc()
        self.assertEqual(kp, 4.0)   # latest by time_tag
        self.assertEqual(ap, 22.0)
        self.assertEqual(src, "swpc:planetary-k")

    def test_gfz_fallback_last_non_null(self):
        self.svc._get_json = lambda url: {
            "Kp": [2.0, 2.667, None],
            "datetime": ["2026-06-12T18:00:00Z", "2026-06-12T21:00:00Z",
                         "2026-06-13T00:00:00Z"],
        }
        kp, t, src = self.svc._fetch_kp_gfz()
        self.assertEqual(kp, 2.667)
        self.assertEqual(src, "gfz")

    def test_getters_default_when_empty(self):
        self.assertEqual(self.svc.get_f107(default=111.0), 111.0)
        self.assertIsNone(self.svc.get_f107(default=None))


if __name__ == "__main__":
    unittest.main()
