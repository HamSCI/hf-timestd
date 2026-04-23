#!/usr/bin/env python3
"""Tests for MetrologyEngine's coarse-time publication hook.

Exercises the translation from a CHU FSK decode result into a
coarse_time.json record without booting the full engine (which pulls
in receiver coords, ionospheric models, IQ buffers). We instantiate
MetrologyEngine via object.__new__ and install only the attributes
the hook touches.
"""

import json
import shutil
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class _FakeResult:
    detected: bool = True
    decoded_day: Optional[int] = 113  # April 23 (non-leap 2026)
    decoded_hour: Optional[int] = 14
    decoded_minute: Optional[int] = 32
    year: Optional[int] = 2026
    dut1_seconds: Optional[float] = 0.1
    tai_utc: Optional[int] = 37
    frames_decoded: int = 9
    decode_confidence: float = 0.92


def _make_engine(channel_name: str, coarse_path: Optional[Path]):
    """Bypass the MetrologyEngine constructor (which needs a full DSP
    stack) and install only what `_publish_coarse_time` reads."""
    from hf_timestd.core.metrology_engine import MetrologyEngine
    from hf_timestd.core.coarse_time_writer import CoarseTimeWriter

    engine = MetrologyEngine.__new__(MetrologyEngine)
    engine.channel_name = channel_name
    engine._coarse_time_writer = (
        CoarseTimeWriter(path=coarse_path) if coarse_path is not None else None
    )
    return engine


class TestMetrologyCoarseTimeHook(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.coarse_path = self.tmp / "coarse_time.json"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _read_coarse(self) -> dict:
        with self.coarse_path.open() as f:
            return json.load(f)

    def test_publish_writes_schema_v1_record_from_fsk_result(self) -> None:
        engine = _make_engine("CHU_3330", self.coarse_path)
        engine._publish_coarse_time(_FakeResult())
        payload = self._read_coarse()
        self.assertEqual(payload["schema"], "v1")
        self.assertEqual(payload["source"], "FSK")
        self.assertEqual(payload["station"], "CHU")
        # Day 113 of 2026 = April 23; + 14:32 -> 2026-04-23T14:32:00Z
        self.assertEqual(payload["coarse_utc"], "2026-04-23T14:32:00.000000Z")
        self.assertAlmostEqual(payload["max_error_sec"], 60.0)

    def test_station_derived_from_channel_name(self) -> None:
        """WWV/WWVH channels don't decode CHU FSK in production, but the
        station-name helper is shared — confirm non-CHU names still map
        through cleanly if the hook ever fires off-label."""
        engine = _make_engine("WWV_10000", self.coarse_path)
        engine._publish_coarse_time(_FakeResult())
        payload = self._read_coarse()
        self.assertEqual(payload["station"], "WWV")

    def test_missing_decoded_fields_is_silent_noop(self) -> None:
        """Partial Frame A decode (rare but possible) — refuse to publish."""
        engine = _make_engine("CHU_3330", self.coarse_path)
        incomplete = _FakeResult(decoded_hour=None)
        engine._publish_coarse_time(incomplete)
        self.assertFalse(self.coarse_path.exists())

    def test_missing_year_falls_back_to_current_system_year(self) -> None:
        """Frame B (year) may fail independently of Frame A. We still
        publish using the current system-clock year — an off-by-one-year
        error at the turn of the year is a known operator-understood
        limitation, not a reason to suppress the coarse-time signal."""
        engine = _make_engine("CHU_3330", self.coarse_path)
        engine._publish_coarse_time(_FakeResult(year=None))
        payload = self._read_coarse()
        self.assertIn("coarse_utc", payload)
        self.assertTrue(payload["coarse_utc"].endswith("Z"))
        self.assertIn("T14:32:00", payload["coarse_utc"])

    def test_writer_disabled_is_noop(self) -> None:
        """If the writer failed to initialize, publish is a silent noop."""
        engine = _make_engine("CHU_3330", coarse_path=None)
        engine._publish_coarse_time(_FakeResult())
        self.assertFalse(self.coarse_path.exists())

    def test_writer_exception_is_logged_not_raised(self) -> None:
        """If the writer raises, the engine swallows it rather than
        letting the decode loop die."""
        engine = _make_engine("CHU_3330", self.coarse_path)
        # Force publish() to raise via a bad path parent.
        engine._coarse_time_writer.path = Path("/nonexistent/dir/coarse_time.json")
        engine._publish_coarse_time(_FakeResult())


if __name__ == "__main__":
    unittest.main()
