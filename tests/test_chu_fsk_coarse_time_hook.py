#!/usr/bin/env python3
"""Tests for the CHU FSK listener's coarse-time publication hook.

Exercises the translation from CHUFSKResult into a coarse_time.json
record without booting the full listener (which requires a radiod and
live RTP streams). We instantiate CHUFSKListener with a minimal config
and call `_publish_coarse_time` directly with a fake result."""

import json
import shutil
import tempfile
import unittest
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from unittest import mock


@dataclass
class _FakeChannel:
    description: str = "CHU_3330_FSK"


@dataclass
class _FakeResult:
    detected: bool = True
    decoded_day: Optional[int] = 113  # April 23 in non-leap 2026
    decoded_hour: Optional[int] = 14
    decoded_minute: Optional[int] = 32
    year: Optional[int] = 2026
    dut1_seconds: Optional[float] = 0.1
    tai_utc: Optional[int] = 37
    frames_decoded: int = 9
    decode_confidence: float = 0.92


def _make_listener(coarse_path: Path):
    """Create a CHUFSKListener with a CoarseTimeWriter pointed at our
    tmp path, bypassing the radiod-control dependency entirely."""
    from hf_timestd.core.chu_fsk_listener import CHUFSKListener
    from hf_timestd.core.coarse_time_writer import CoarseTimeWriter

    # CHUFSKListener's __init__ inspects control.ensure_channel signature,
    # reads config, builds channels. For the narrow slice we want to
    # test (_publish_coarse_time), we sidestep the constructor entirely
    # via object.__new__ and install only the attributes we need.
    listener = CHUFSKListener.__new__(CHUFSKListener)
    listener._coarse_time_writer = CoarseTimeWriter(path=coarse_path)
    return listener


class TestChuFskCoarseTimeHook(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.coarse_path = self.tmp / "coarse_time.json"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _read_coarse(self) -> dict:
        with self.coarse_path.open() as f:
            return json.load(f)

    def test_publish_writes_schema_v1_record_from_fsk_result(self) -> None:
        listener = _make_listener(self.coarse_path)
        listener._publish_coarse_time(_FakeChannel(), _FakeResult())
        payload = self._read_coarse()
        self.assertEqual(payload["schema"], "v1")
        self.assertEqual(payload["source"], "FSK")
        self.assertEqual(payload["station"], "CHU")
        # Day 113 of 2026 = April 23; + 14:32 -> 2026-04-23T14:32:00Z
        self.assertEqual(payload["coarse_utc"], "2026-04-23T14:32:00.000000Z")
        self.assertAlmostEqual(payload["max_error_sec"], 60.0)

    def test_missing_decoded_fields_is_silent_noop(self) -> None:
        """Partial Frame A decode (rare but possible) — refuse to publish."""
        listener = _make_listener(self.coarse_path)
        incomplete = _FakeResult(decoded_hour=None)
        listener._publish_coarse_time(_FakeChannel(), incomplete)
        self.assertFalse(self.coarse_path.exists())

    def test_missing_year_falls_back_to_current_system_year(self) -> None:
        """Frame B (year) may fail independently of Frame A. We still
        publish using the current system-clock year — an off-by-one-year
        error at the turn of the year is a known operator-understood
        limitation, not a reason to suppress the coarse-time signal."""
        listener = _make_listener(self.coarse_path)
        listener._publish_coarse_time(_FakeChannel(), _FakeResult(year=None))
        payload = self._read_coarse()
        self.assertIn("coarse_utc", payload)
        # We don't over-specify the year (system-dependent); just assert
        # the hour/minute part came through and the ISO-Z format is valid.
        self.assertTrue(payload["coarse_utc"].endswith("Z"))
        self.assertIn("T14:32:00", payload["coarse_utc"])

    def test_writer_disabled_is_noop(self) -> None:
        """If the writer failed to initialize, publish is a silent noop."""
        from hf_timestd.core.chu_fsk_listener import CHUFSKListener
        listener = CHUFSKListener.__new__(CHUFSKListener)
        listener._coarse_time_writer = None
        listener._publish_coarse_time(_FakeChannel(), _FakeResult())
        self.assertFalse(self.coarse_path.exists())

    def test_writer_exception_is_logged_not_raised(self) -> None:
        """If the writer raises, the listener swallows it rather than
        letting the decode loop die."""
        listener = _make_listener(self.coarse_path)
        # Force publish() to raise via a bad path parent.
        listener._coarse_time_writer.path = Path("/nonexistent/dir/coarse_time.json")
        # Should not raise.
        listener._publish_coarse_time(_FakeChannel(), _FakeResult())


if __name__ == "__main__":
    unittest.main()
