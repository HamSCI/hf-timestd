"""The ordinal resolver names a second and nothing more."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hf_timestd.core.core_recorder_v2 import CoreRecorderV2


def _recorder():
    """A bare object carrying only what the resolver reads."""
    r = SimpleNamespace()
    r.T6_ORDINAL_MAX_SIGMA_MS = CoreRecorderV2.T6_ORDINAL_MAX_SIGMA_MS
    r._get_ordinal_reference = CoreRecorderV2._get_ordinal_reference.__get__(r)
    return r


def _chronyc(last_offset_s: float, rms_offset_s: float):
    out = (f"Last offset     : {last_offset_s} seconds\n"
           f"RMS offset      : {rms_offset_s} seconds\n")
    return mock.Mock(returncode=0, stdout=out)


class TestOrdinalResolver(unittest.TestCase):

    def test_the_threshold_is_a_fifth_of_the_plausibility_bound(self):
        self.assertEqual(CoreRecorderV2.T6_ORDINAL_MAX_SIGMA_MS, 50.0)

    def test_ai6vn_shaped_station_resolves(self):
        """No T5, no T3, T4 chronyc at 0.29 ms.  The old gate refused
        this station; the ordinal resolver must not."""
        r = _recorder()
        with mock.patch("pathlib.Path.read_text", side_effect=FileNotFoundError), \
             mock.patch("subprocess.run", return_value=_chronyc(0.000284, 0.00029)):
            ref = r._get_ordinal_reference()
        self.assertIsNotNone(ref)
        offset_ms, sigma_ms, tier = ref
        self.assertLess(sigma_ms, 50.0)
        self.assertEqual(tier, "T4")

    def test_a_20_ms_clock_resolves(self):
        """T2, WAN NTP.  The tier least able to do T6's job performs the
        only job T6 needs from it."""
        r = _recorder()
        with mock.patch("pathlib.Path.read_text", side_effect=FileNotFoundError), \
             mock.patch("subprocess.run", return_value=_chronyc(0.020, 0.020)):
            ref = r._get_ordinal_reference()
        self.assertIsNotNone(ref)

    def test_a_clock_worse_than_50_ms_is_refused(self):
        """A station holding no clock within 50 ms of UTC has a far
        larger problem than a T6 acquisition."""
        r = _recorder()
        with mock.patch("pathlib.Path.read_text", side_effect=FileNotFoundError), \
             mock.patch("subprocess.run", return_value=_chronyc(0.4, 0.4)):
            ref = r._get_ordinal_reference()
        self.assertIsNone(ref)

    def test_no_clock_at_all_returns_none(self):
        r = _recorder()
        with mock.patch("pathlib.Path.read_text", side_effect=FileNotFoundError), \
             mock.patch("subprocess.run", side_effect=OSError("no chronyc")):
            self.assertIsNone(r._get_ordinal_reference())

    def test_fusion_wins_when_it_is_available_and_sane(self):
        """A real HF antenna sharpens T3.  It makes an easy question
        easier and changes nothing else."""
        import json
        r = _recorder()
        payload = json.dumps({
            "schema": "v1",
            "fusion": {"available": True, "kalman_state": "LOCKED",
                       "d_clock_fused_ms": 1.25, "uncertainty_ms": 4.3},
        })
        with mock.patch("pathlib.Path.read_text", return_value=payload):
            ref = r._get_ordinal_reference()
        self.assertIsNotNone(ref)
        self.assertEqual(ref[2], "T3")

    def test_the_resolver_reports_sigma_it_does_not_hide_it(self):
        """Provenance survives: the caller records which source named
        the second."""
        r = _recorder()
        with mock.patch("pathlib.Path.read_text", side_effect=FileNotFoundError), \
             mock.patch("subprocess.run", return_value=_chronyc(0.000284, 0.00029)):
            offset_ms, sigma_ms, tier = r._get_ordinal_reference()
        self.assertAlmostEqual(sigma_ms, 0.29, places=3)
        self.assertAlmostEqual(offset_ms, -0.284, places=3)


if __name__ == "__main__":
    unittest.main()
