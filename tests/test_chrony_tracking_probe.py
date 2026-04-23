#!/usr/bin/env python3
"""Unit tests for ChronyTrackingProbe."""

import subprocess
import unittest
from dataclasses import dataclass
from typing import List, Optional

from hf_timestd.core.chrony_tracking_probe import (
    ChronyTrackingProbe,
    match_any_server_not_in,
    match_by_names,
    match_refclock,
)


@dataclass
class _FakeCompleted:
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0


def _fake_runner(stdout: str = "", returncode: int = 0, raises=None):
    """Return a callable matching subprocess.run signature."""
    def _run(cmd, capture_output=False, text=False, timeout=None, check=False):
        if raises is not None:
            raise raises
        return _FakeCompleted(stdout=stdout, returncode=returncode)
    return _run


# Realistic `chronyc -n -c sources` sample — three NTP servers and one refclock.
# Format: mode,state,name,stratum,poll,reach,lastRx,offset,measErr,estErr
SAMPLE_SOURCES = (
    "^,*,192.168.1.80,1,6,377,23,0.000123000,0.000210000,0.000450000\n"
    "^,+,time.nist.gov,2,10,377,145,0.003200000,0.004100000,0.009100000\n"
    "^,+,pool.ntp.org,3,10,377,99,0.015000000,0.016000000,0.020000000\n"
    "#,*,GPS,0,4,377,12,-0.000002000,0.000003000,0.000005000\n"
)


class TestChronyTrackingProbe(unittest.TestCase):
    # ----- matchers -----

    def test_match_by_names_is_case_insensitive(self) -> None:
        m = match_by_names(["TimeServer.LAN", "192.168.1.80"])
        self.assertTrue(m({"name": "timeserver.lan"}))
        self.assertTrue(m({"name": "192.168.1.80"}))
        self.assertFalse(m({"name": "time.nist.gov"}))

    def test_match_refclock_gates_on_mode_hash(self) -> None:
        m_any = match_refclock()
        self.assertTrue(m_any({"mode": "#", "name": "GPS"}))
        self.assertFalse(m_any({"mode": "^", "name": "192.168.1.80"}))

    def test_match_refclock_with_refid_is_case_insensitive(self) -> None:
        m = match_refclock("gps")
        self.assertTrue(m({"mode": "#", "name": "GPS"}))
        self.assertFalse(m({"mode": "#", "name": "PPS"}))

    def test_match_any_server_excludes_named_peers(self) -> None:
        m = match_any_server_not_in(["192.168.1.80", "timeserver.lan"])
        self.assertTrue(m({"mode": "^", "name": "time.nist.gov"}))
        self.assertFalse(m({"mode": "^", "name": "192.168.1.80"}))
        self.assertFalse(m({"mode": "#", "name": "GPS"}))  # not a server

    # ----- poll: success paths -----

    def test_t4_probe_finds_matching_named_peer(self) -> None:
        probe = ChronyTrackingProbe(
            t_level="T4",
            source_matcher=match_by_names(["192.168.1.80"]),
            runner=_fake_runner(stdout=SAMPLE_SOURCES),
        )
        r = probe.poll()
        self.assertTrue(r.available)
        # 0.000123 s = 0.123 ms
        self.assertAlmostEqual(r.offset_ms, 0.123, places=3)
        self.assertEqual(r.detail["name"], "192.168.1.80")
        self.assertEqual(r.detail["state"], "*")

    def test_t5_probe_finds_refclock(self) -> None:
        probe = ChronyTrackingProbe(
            t_level="T5",
            source_matcher=match_refclock(),
            runner=_fake_runner(stdout=SAMPLE_SOURCES),
        )
        r = probe.poll()
        self.assertTrue(r.available)
        # -0.000002 s = -0.002 ms
        self.assertAlmostEqual(r.offset_ms, -0.002, places=4)

    def test_t2_probe_uses_exclusion_matcher(self) -> None:
        probe = ChronyTrackingProbe(
            t_level="T2",
            source_matcher=match_any_server_not_in(["192.168.1.80"]),
            runner=_fake_runner(stdout=SAMPLE_SOURCES),
        )
        r = probe.poll()
        self.assertTrue(r.available)
        # Should pick the first healthy remaining server
        self.assertIn(r.detail["name"], ("time.nist.gov", "pool.ntp.org"))

    # ----- poll: failure paths -----

    def test_chronyc_missing(self) -> None:
        probe = ChronyTrackingProbe(
            t_level="T4",
            source_matcher=match_by_names(["x"]),
            runner=_fake_runner(raises=FileNotFoundError()),
        )
        r = probe.poll()
        self.assertFalse(r.available)
        self.assertIn("chronyc not found", r.reason or "")

    def test_chronyc_timeout(self) -> None:
        probe = ChronyTrackingProbe(
            t_level="T4",
            source_matcher=match_by_names(["x"]),
            runner=_fake_runner(raises=subprocess.TimeoutExpired(cmd="chronyc", timeout=5)),
        )
        r = probe.poll()
        self.assertFalse(r.available)
        self.assertIn("timeout", r.reason or "")

    def test_chronyc_nonzero_exit(self) -> None:
        probe = ChronyTrackingProbe(
            t_level="T4",
            source_matcher=match_by_names(["x"]),
            runner=_fake_runner(stdout="", returncode=1),
        )
        r = probe.poll()
        self.assertFalse(r.available)
        self.assertIn("exit 1", r.reason or "")

    def test_no_matching_source(self) -> None:
        probe = ChronyTrackingProbe(
            t_level="T4",
            source_matcher=match_by_names(["nonexistent.local"]),
            runner=_fake_runner(stdout=SAMPLE_SOURCES),
        )
        r = probe.poll()
        self.assertFalse(r.available)
        self.assertIn("no matching source", r.reason or "")

    def test_matching_source_unhealthy_state(self) -> None:
        # Same peer but state is '?' (unreachable).
        crafted = "^,?,192.168.1.80,1,6,0,9999,0.000000000,0.000000000,0.000000000\n"
        probe = ChronyTrackingProbe(
            t_level="T4",
            source_matcher=match_by_names(["192.168.1.80"]),
            runner=_fake_runner(stdout=crafted),
        )
        r = probe.poll()
        self.assertFalse(r.available)
        self.assertIn("unhealthy", r.reason or "")

    def test_short_rows_are_skipped_not_crashed(self) -> None:
        probe = ChronyTrackingProbe(
            t_level="T4",
            source_matcher=match_by_names(["192.168.1.80"]),
            runner=_fake_runner(stdout="^,*,short_row\n" + SAMPLE_SOURCES),
        )
        r = probe.poll()
        self.assertTrue(r.available)

    def test_source_matcher_exception_treats_row_as_non_match(self) -> None:
        def exploder(row):
            raise RuntimeError("kaboom")
        probe = ChronyTrackingProbe(
            t_level="T4",
            source_matcher=exploder,
            runner=_fake_runner(stdout=SAMPLE_SOURCES),
        )
        r = probe.poll()
        self.assertFalse(r.available)
        self.assertIn("no matching source", r.reason or "")


if __name__ == "__main__":
    unittest.main()
