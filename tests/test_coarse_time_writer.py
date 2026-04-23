#!/usr/bin/env python3
"""Unit tests for CoarseTimeWriter — producer side of the schema v1
contract consumed by CoarseTimeFileSource."""

import json
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from hf_timestd.core.coarse_time_source import CoarseTimeFileSource
from hf_timestd.core.coarse_time_writer import SCHEMA_VERSION, CoarseTimeWriter


class TestCoarseTimeWriter(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "coarse_time.json"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _read(self) -> dict:
        with self.path.open() as f:
            return json.load(f)

    def test_publish_emits_schema_v1_and_core_fields(self) -> None:
        writer = CoarseTimeWriter(path=self.path, freshness_sec=120.0)
        writer.publish(
            source="FSK",
            station="CHU",
            coarse_utc=datetime(2026, 4, 23, 14, 32, 0, tzinfo=timezone.utc),
            max_error_sec=60.0,
            utc_published=datetime(2026, 4, 23, 14, 32, 45, tzinfo=timezone.utc),
        )
        payload = self._read()
        self.assertEqual(payload["schema"], SCHEMA_VERSION)
        self.assertEqual(payload["source"], "FSK")
        self.assertEqual(payload["station"], "CHU")
        self.assertEqual(payload["coarse_utc"], "2026-04-23T14:32:00.000000Z")
        self.assertEqual(payload["utc_published"], "2026-04-23T14:32:45.000000Z")
        self.assertAlmostEqual(payload["max_error_sec"], 60.0)
        self.assertAlmostEqual(payload["freshness_sec"], 120.0)

    def test_emitted_file_is_consumable_by_the_source(self) -> None:
        """Round-trip check: the writer's output must be parseable by
        CoarseTimeFileSource, which is the authority manager's reader."""
        writer = CoarseTimeWriter(path=self.path, freshness_sec=120.0)
        pub = datetime(2026, 4, 23, 14, 32, 45, tzinfo=timezone.utc)
        coarse = datetime(2026, 4, 23, 14, 32, 0, tzinfo=timezone.utc)
        writer.publish(
            source="FSK", station="CHU",
            coarse_utc=coarse, max_error_sec=60.0,
            utc_published=pub,
        )
        src = CoarseTimeFileSource(
            path=self.path,
            now_fn=lambda: pub,  # read "immediately" to pass freshness
        )
        obs = src.read()
        self.assertIsNotNone(obs)
        self.assertEqual(obs.utc, coarse)
        self.assertEqual(obs.source, "FSK")
        self.assertEqual(obs.station, "CHU")
        self.assertAlmostEqual(obs.max_error_sec, 60.0)

    def test_atomic_write_leaves_no_temp_files(self) -> None:
        writer = CoarseTimeWriter(path=self.path)
        writer.publish(
            source="BCD", station="WWV",
            coarse_utc=datetime(2026, 4, 23, 12, 0, 0, tzinfo=timezone.utc),
            max_error_sec=60.0,
        )
        leftovers = [p for p in self.tmp.iterdir() if p.name != "coarse_time.json"]
        self.assertEqual(leftovers, [])

    def test_repeated_publish_replaces_not_appends(self) -> None:
        writer = CoarseTimeWriter(path=self.path)
        t0 = datetime(2026, 4, 23, 12, 0, 0, tzinfo=timezone.utc)
        t1 = datetime(2026, 4, 23, 12, 1, 0, tzinfo=timezone.utc)
        writer.publish(source="FSK", station="CHU", coarse_utc=t0, max_error_sec=60.0)
        writer.publish(source="FSK", station="CHU", coarse_utc=t1, max_error_sec=60.0)
        payload = self._read()
        self.assertEqual(payload["coarse_utc"], "2026-04-23T12:01:00.000000Z")

    def test_naive_datetime_is_interpreted_as_utc(self) -> None:
        writer = CoarseTimeWriter(path=self.path)
        naive = datetime(2026, 4, 23, 12, 0, 0)  # no tzinfo
        writer.publish(source="FSK", station="CHU", coarse_utc=naive, max_error_sec=60.0)
        payload = self._read()
        self.assertTrue(payload["coarse_utc"].endswith("Z"))
        self.assertIn("2026-04-23T12:00:00", payload["coarse_utc"])


if __name__ == "__main__":
    unittest.main()
