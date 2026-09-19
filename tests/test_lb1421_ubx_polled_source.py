"""A POLLED naming source is not an EMITTED one.

An NMEA RMC sentence arrives within a second of the boundary it names, so
its staleness and the host clock's error are the same quantity and one
window judges both.  A UBX NAV-PVT solution is read on gpsdo-monitor's
probe interval (10 s default), so a perfectly good reading is routinely
seconds old.  Measured on DASI-009.AI6VN 2026-09-19: the reading came back
+3.964 s and was refused as a host-clock fault, while the device's own tAcc
claimed 6 ns.
"""
from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hf_timestd.core.lb1421_t5_probe import Lb1421T5Probe

SERIAL = "9DC7A57A42"


def _doc(*, pps_utc_sec, naming_source, mono_at_read, fix_age=0.0):
    return {
        "schema": "v1",
        "written_utc": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        "device": {"model": "lbe-mini", "serial": SERIAL},
        "health": {
            "gps_fix": "3D", "sats_used": 12, "fix_age_sec": fix_age,
            "pps_utc_sec": pps_utc_sec,
            "nmea_host_monotonic_at_read": mono_at_read,
            "naming_source": naming_source,
            "naming_sigma_ns": 6,
        },
        "outputs": {"pps_enabled": False},
        "pps_study": {"enabled": False, "edges": 0},
    }


def _boundary_consistent(age_s: float, *, gps_offset_s: float = 0.0):
    """Build the pair gpsdo-monitor actually publishes.

    ⚠ NOT `int(time.time()) - age`.  That truncates up to a second, and the
    leftover fraction lands straight in the host-clock residual the probe
    measures — so a fixture built that way passes or fails on whatever
    sub-second phase the suite happens to run at.  The first version of this
    file did exactly that and was green alone, red under the full suite.

    The publisher pairs an INTEGER second with the monotonic at which THAT
    SECOND BEGAN, so the fraction is carried explicitly and the residual is
    zero by construction.  `gps_offset_s` shifts GPS against the host clock
    to model a host that really is wrong.
    """
    t0 = time.time()
    mono0 = time.monotonic()
    frac = t0 - int(t0)
    pps = int(t0) - int(age_s) - int(gps_offset_s)
    began = mono0 - frac - (age_s - int(age_s)) - int(age_s)
    return pps, began


class _Harness(unittest.TestCase):

    def _read(self, doc):
        with TemporaryDirectory() as d:
            p = Path(d) / f"{SERIAL}.json"
            p.write_text(json.dumps(doc))
            probe = Lb1421T5Probe(run_dir=d, serial=SERIAL)
            return probe._read_once()


class TestPolledUbxIsAgedNotRefused(_Harness):

    def test_a_seconds_old_ubx_reading_is_still_valid(self):
        """The AI6VN case.  The second began 4 s ago by the monotonic, and
        the host clock agrees with GPS, so the reading is good."""
        pps, began = _boundary_consistent(4.0)
        r = self._read(_doc(
            pps_utc_sec=pps, naming_source="ubx-nav-pvt", mono_at_read=began,
        ))
        self.assertIsNotNone(r)
        self.assertTrue(r.valid_fix, f"host_minus_gps_s={r.host_minus_gps_s}")
        self.assertLess(abs(r.host_minus_gps_s), 0.5)

    def test_a_genuinely_wrong_host_clock_is_still_caught(self):
        """The check must keep doing its job: ageing must not excuse a host
        clock that really is off by whole seconds."""
        pps, began = _boundary_consistent(4.0, gps_offset_s=30.0)
        r = self._read(_doc(
            pps_utc_sec=pps, naming_source="ubx-nav-pvt", mono_at_read=began,
        ))
        self.assertIsNotNone(r)
        self.assertFalse(r.valid_fix)
        self.assertGreater(abs(r.host_minus_gps_s), 1.0)

    def test_an_ancient_ubx_reading_is_refused_as_stale(self):
        """Ageing is not a licence to trust a reading forever; the true age
        still bounds it."""
        pps, began = _boundary_consistent(600.0)
        r = self._read(_doc(
            pps_utc_sec=pps, naming_source="ubx-nav-pvt", mono_at_read=began,
        ))
        self.assertIsNotNone(r)
        self.assertFalse(r.valid_fix)


class TestTheNmeaPathIsUntouched(_Harness):

    def test_a_fresh_nmea_reading_still_validates(self):
        r = self._read(_doc(
            pps_utc_sec=int(time.time()),
            naming_source="nmea-rmc",
            mono_at_read=time.monotonic(),
        ))
        self.assertIsNotNone(r)
        self.assertTrue(r.valid_fix)

    def test_a_stale_nmea_reading_is_refused_by_the_old_window(self):
        """The NMEA branch must keep its original behaviour: for an EMITTED
        source, seconds of delay IS a host-clock disagreement."""
        r = self._read(_doc(
            pps_utc_sec=int(time.time()) - 4,
            naming_source="nmea-rmc",
            mono_at_read=time.monotonic() - 4.0,
        ))
        self.assertIsNotNone(r)
        self.assertFalse(r.valid_fix)

    def test_a_document_with_no_naming_source_takes_the_nmea_branch(self):
        """Older publishers emit no naming_source.  They must not silently
        acquire the polled-source tolerance."""
        d = _doc(pps_utc_sec=int(time.time()) - 4, naming_source=None,
                 mono_at_read=time.monotonic() - 4.0)
        d["health"].pop("naming_source")
        r = self._read(d)
        self.assertIsNotNone(r)
        self.assertFalse(r.valid_fix)


if __name__ == "__main__":
    unittest.main()
