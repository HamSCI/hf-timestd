"""A device may NAME a second without being able to PLACE one.

Two different questions, collapsed onto one boolean until now:

    "which integer second is this?"   ±0.5 s   -> naming, the T axis
    "where is the second boundary?"   µs + PPS -> T5

`lb1421_enabled` gated both, so a station holding a device that answers the
first refused to ask it, and published `naming_unavailable` while the answer
sat on its own USB bus.  Measured on DASI-009.AI6VN 2026-09-18: T6 found the
edge and could not name its second — no HF antenna (no T3), no LAN GPS (no
T4), and an LBE-mini whose UBX time-of-day nothing consulted.

⚠ The load-bearing test in this file is
`test_a_namer_does_NOT_light_the_t5_bench`.  The obvious implementation —
widening the `attach_lb1421_probe` gate — would light the T5 bench on a
device with no PPS, which is precisely the AC0G-ND defect
(`gpsdo_capability`) in a new costume.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from hf_timestd.core.core_recorder_v2 import CoreRecorderV2


def _bare_recorder():
    """A recorder without __init__ — this suite touches only the slots."""
    r = CoreRecorderV2.__new__(CoreRecorderV2)
    r._lb1421_probe = None
    r._t6_second_namer = None
    return r


class _Probe:
    def __init__(self, tag):
        self.tag = tag
        self.started = False

    def start(self):
        self.started = True

    def get_latest(self):
        return SimpleNamespace(pps_utc_sec=1789689116, valid_fix=True,
                               host_monotonic_at_read=1.0, tag=self.tag)


class TestTheSlotsStaySeparate:
    def test_nothing_wired_names_nothing(self):
        assert CoreRecorderV2._t6_naming_probe(_bare_recorder()) is None

    def test_a_namer_is_used_for_naming(self):
        r = _bare_recorder()
        r.attach_second_namer(_Probe("namer"))
        assert r._t6_naming_probe().tag == "namer"

    def test_a_real_t5_probe_wins_over_a_namer(self):
        """A T5 device both names and places; it should not be displaced."""
        r = _bare_recorder()
        r.attach_second_namer(_Probe("namer"))
        r.attach_lb1421_probe(_Probe("t5"))
        assert r._t6_naming_probe().tag == "t5"

    def test_a_t5_probe_alone_still_names(self):
        """The pre-existing path must be untouched."""
        r = _bare_recorder()
        r.attach_lb1421_probe(_Probe("t5"))
        assert r._t6_naming_probe().tag == "t5"

    def test_a_namer_does_NOT_light_the_t5_bench(self):
        """⛔ THE POINT OF THE SEPARATE SLOT.

        `_t5_lbe1421_product` reads `_lb1421_probe`, and its own docstring
        says "lb1421_enabled=true alone ... is sufficient to light the T5
        bench". A PPS-less device reaching that slot would publish a T5 that
        can never yield a reading — the ND defect.
        """
        r = _bare_recorder()
        r.attach_second_namer(_Probe("namer"))
        assert r._lb1421_probe is None, (
            "attach_second_namer leaked into the T5 probe slot; that slot "
            "lights the T5 bench and this device has no PPS")


class TestOnlyTheNamingPathReadsTheNamer:
    """`_t6_second_namer` has exactly one sanctioned reader.

    A source-level guard, because the failure it prevents is someone
    reaching for the convenient attribute from a T5 path later.
    """

    def _src(self):
        from pathlib import Path
        return (Path(__file__).parents[2] / "src" / "hf_timestd" / "core"
                / "core_recorder_v2.py").read_text()

    def test_the_namer_slot_is_read_only_by_its_accessor(self):
        src = self._src()
        # Code references only: an attribute access or a getattr name.
        # Prose in a docstring mentions it too (``_t6_second_namer``) and is
        # not a read — the first cut of this test counted those.
        reads = [
            l.strip() for l in src.splitlines()
            if ("self._t6_second_namer" in l
                or "'_t6_second_namer'" in l
                or '"_t6_second_namer"' in l)
            and not l.strip().startswith("#")
            and "self._t6_second_namer = " not in l
        ]
        # The only surviving read lives in _t6_naming_probe.
        assert len(reads) == 1, (
            f"_t6_second_namer is read in {len(reads)} places: {reads}. "
            f"Only _t6_naming_probe may read it.")
        assert "getattr(self, '_t6_second_namer'" in reads[0]

    def test_both_entry_points_attach_it(self):
        """The systemd unit runs the MODULE, not the CLI. A fix applied only
        to cli.py is dead code in production — that happened on 2026-09-16."""
        from pathlib import Path
        root = Path(__file__).parents[2] / "src" / "hf_timestd"
        for rel in ("cli.py", "core/core_recorder_v2.py"):
            assert "attach_second_namer" in (root / rel).read_text(), (
                f"{rel} never attaches a second-namer")

    def test_neither_entry_point_builds_the_probe_itself(self):
        """Both must go through the shared helper, for the same reason."""
        from pathlib import Path
        root = Path(__file__).parents[2] / "src" / "hf_timestd"
        for rel in ("cli.py", "core/core_recorder_v2.py"):
            src = (root / rel).read_text()
            i = src.find("attach_second_namer(recorder")
            assert i > 0, f"{rel} does not call the shared helper"


class TestTheHelperIsHonestAboutBeingInert:
    def test_it_reports_attachment(self):
        from hf_timestd.core.gpsdo_capability import attach_second_namer
        r = _bare_recorder()
        ok = attach_second_namer(r, "/nonexistent-for-test", None)
        assert ok is True
        assert r._t6_second_namer is not None
        assert r._lb1421_probe is None

    def test_a_failure_to_attach_never_raises(self):
        """Startup path: a broken probe must not stop the recorder."""
        from hf_timestd.core.gpsdo_capability import attach_second_namer

        class Hostile:
            def attach_second_namer(self, probe):
                raise RuntimeError("no")
        assert attach_second_namer(Hostile(), "/tmp", None) is False


class TestTheNamingPathActuallyConsultsTheNamer:
    """⛔ The hole mutation-testing found.

    Every other test here checks the plumbing — that the slots stay apart
    and both entry points wire them. None exercised the path, so reverting
    `_t6_name_second_via_nmea` to read `_lb1421_probe` alone passed clean.
    That revert IS the feature being removed.

    Behavioural, not source-level: a probe that records whether it was
    asked. If the naming path reads only the T5 slot it returns at the
    first gate and never asks.
    """

    class _RecordingProbe:
        def __init__(self):
            self.asked = False

        def get_latest(self):
            self.asked = True
            return None            # keeps the path short; the ask is the point

    def _recorder_with_namer(self):
        r = _bare_recorder()
        probe = self._RecordingProbe()
        r.attach_second_namer(probe)
        # Naming needs a pairing; __init__ builds one unconditionally, so a
        # station without T5 still has it. Supply a stand-in.
        r._t5_pairing = SimpleNamespace(latest_arrival=None)
        return r, probe

    def test_naming_asks_the_namer_when_there_is_no_t5_probe(self):
        r, probe = self._recorder_with_namer()
        CoreRecorderV2._t6_name_second_via_nmea(r, edge_rtp=12345)
        assert probe.asked, (
            "_t6_name_second_via_nmea never consulted the second-namer — it "
            "is reading _lb1421_probe directly again, which is the whole "
            "defect: a station that can name a second refuses to.")

    def test_naming_still_asks_a_real_t5_probe(self):
        """The pre-existing path must keep working unchanged."""
        r = _bare_recorder()
        probe = self._RecordingProbe()
        r.attach_lb1421_probe(probe)
        r._t5_pairing = SimpleNamespace(latest_arrival=None)
        CoreRecorderV2._t6_name_second_via_nmea(r, edge_rtp=12345)
        assert probe.asked

    def test_naming_asks_nobody_when_nothing_is_wired(self):
        r = _bare_recorder()
        r._t5_pairing = SimpleNamespace(latest_arrival=None)
        assert CoreRecorderV2._t6_name_second_via_nmea(r, edge_rtp=1) is None
