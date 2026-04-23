#!/usr/bin/env python3
"""Unit tests for CoarseTimeFileSource."""

import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hf_timestd.core.coarse_time_source import CoarseTimeFileSource


def _good_payload(**overrides) -> dict:
    base = {
        "schema": "v1",
        "utc_published": "2026-04-23T12:00:00.000000Z",
        "source": "BCD",
        "station": "WWV",
        "coarse_utc": "2026-04-23T11:59:00.000000Z",
        "max_error_sec": 1.0,
        "freshness_sec": 60.0,
    }
    base.update(overrides)
    return base


class TestCoarseTimeFileSource(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "coarse_time.json"
        self.now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, payload: dict) -> None:
        with self.path.open("w") as f:
            json.dump(payload, f)

    def _src(self) -> CoarseTimeFileSource:
        return CoarseTimeFileSource(path=self.path, now_fn=lambda: self.now)

    def test_happy_path_parses_all_fields(self) -> None:
        self._write(_good_payload())
        obs = self._src().read()
        self.assertIsNotNone(obs)
        self.assertEqual(obs.source, "BCD")
        self.assertEqual(obs.station, "WWV")
        self.assertAlmostEqual(obs.max_error_sec, 1.0)
        self.assertEqual(obs.utc, datetime(2026, 4, 23, 11, 59, 0, tzinfo=timezone.utc))

    def test_missing_file_returns_none(self) -> None:
        self.assertIsNone(self._src().read())

    def test_corrupt_json_returns_none(self) -> None:
        self.path.write_text("{not json}")
        self.assertIsNone(self._src().read())

    def test_unsupported_schema_returns_none(self) -> None:
        self._write(_good_payload(schema="v2"))
        self.assertIsNone(self._src().read())

    def test_stale_publication_returns_none(self) -> None:
        # utc_published one hour old with default 60 s freshness
        self.now = self.now + timedelta(hours=1)
        self._write(_good_payload())
        self.assertIsNone(self._src().read())

    def test_missing_required_field_returns_none(self) -> None:
        payload = _good_payload()
        del payload["coarse_utc"]
        self._write(payload)
        self.assertIsNone(self._src().read())

    def test_explicit_freshness_overrides_default(self) -> None:
        # Publisher declares a large freshness window; old publication still
        # passes.
        self.now = self.now + timedelta(minutes=10)
        self._write(_good_payload(freshness_sec=3600))
        self.assertIsNotNone(self._src().read())


if __name__ == "__main__":
    unittest.main()
