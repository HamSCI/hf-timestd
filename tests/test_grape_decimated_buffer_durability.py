"""Tests for DecimatedBuffer crash-durability primitives.

Before this fix:

  - ``write_minute()`` did `seek + write` with no `fsync`, so a
    crash between the call and the next page-cache writeback left
    the day file with old zeros at the minute's offset while the
    in-memory metadata claimed it was valid.
  - ``_save_metadata()`` opened the JSON file in `'w'` mode and
    dumped directly into it; a crash mid-write left a truncated
    JSON catalog that ``_load_metadata()`` would silently discard
    (returning a fresh empty DayMetadata) — losing every prior
    minute record for that day.

These tests pin down the new behaviour: ``write_minute`` fsyncs
the bin file, ``_save_metadata`` writes via tmp + atomic rename
+ fsync, ``_create_day_file`` fsyncs after preallocation, and a
partial ``.tmp`` orphan from a simulated mid-write crash never
corrupts the canonical metadata file.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = str(REPO_ROOT / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from hf_timestd.grape.decimated_buffer import DecimatedBuffer  # noqa: E402


def _minute_ts(year: int = 2025, month: int = 4, day: int = 28,
               hour: int = 0, minute: int = 0) -> float:
    return float(datetime(year, month, day, hour, minute,
                          tzinfo=timezone.utc).timestamp())


def _iq(value: complex = 1 + 0j) -> np.ndarray:
    return np.full(600, value, dtype=np.complex64)


class WriteMinuteFsyncTests(unittest.TestCase):
    """write_minute must fsync the bin file before returning."""

    def test_calls_fsync_on_the_bin_file(self):
        with tempfile.TemporaryDirectory() as d:
            buf = DecimatedBuffer(Path(d), "WWV 10000")
            real_fsync = os.fsync
            calls: list[int] = []

            def tracking_fsync(fd):
                calls.append(fd)
                return real_fsync(fd)

            with mock.patch("hf_timestd.grape.decimated_buffer.os.fsync",
                            side_effect=tracking_fsync) as m:
                ok = buf.write_minute(_minute_ts(), _iq())

            self.assertTrue(ok)
            # At least one fsync (write_minute) — the day-file create
            # may have added a second one.
            self.assertGreaterEqual(m.call_count, 1)


class SaveMetadataAtomicityTests(unittest.TestCase):
    def test_writes_via_tmp_and_renames(self):
        # Drive a write_minute → flush_metadata cycle, then verify
        # the canonical .json exists and no orphan .tmp remains.
        with tempfile.TemporaryDirectory() as d:
            buf = DecimatedBuffer(Path(d), "WWV 10000")
            buf.write_minute(_minute_ts(), _iq())
            buf.flush_metadata()

            decimated_dir = Path(d) / "products" / "WWV_10000" / "decimated"
            files = {p.name for p in decimated_dir.iterdir()}
            self.assertIn("20250428_meta.json", files)
            self.assertFalse(any(f.endswith(".tmp") for f in files),
                             f"orphan .tmp left behind: {files}")

    def test_partial_tmp_does_not_corrupt_canonical_json(self):
        # Simulate a crash exactly between the tmp write and the
        # rename: drop a corrupt <date>_meta.json.tmp and confirm
        # _load_metadata still reads the canonical file cleanly.
        with tempfile.TemporaryDirectory() as d:
            buf = DecimatedBuffer(Path(d), "WWV 10000")
            buf.write_minute(_minute_ts(), _iq(), gap_samples=10)
            buf.flush_metadata()

            decimated_dir = Path(d) / "products" / "WWV_10000" / "decimated"
            (decimated_dir / "20250428_meta.json.tmp").write_text(
                "this is not valid json"
            )

            loaded = buf._load_metadata("20250428")
            self.assertEqual(loaded.channel, "WWV 10000")
            self.assertIn("0", loaded.minutes)
            self.assertEqual(loaded.minutes["0"]["gap_samples"], 10)

    def test_save_metadata_failure_leaves_canonical_intact(self):
        # If json.dump raises mid-write to the .tmp, the canonical
        # file must not be touched.
        with tempfile.TemporaryDirectory() as d:
            buf = DecimatedBuffer(Path(d), "WWV 10000")
            # Establish a good baseline catalog.
            buf.write_minute(_minute_ts(), _iq(), gap_samples=1)
            buf.flush_metadata()
            decimated_dir = Path(d) / "products" / "WWV_10000" / "decimated"
            canonical = decimated_dir / "20250428_meta.json"
            baseline = canonical.read_bytes()

            # Force a write-failure on the next save.
            buf.write_minute(_minute_ts(minute=1), _iq(), gap_samples=2)
            with mock.patch("json.dump",
                            side_effect=RuntimeError("disk full")):
                with self.assertRaises(RuntimeError):
                    buf.flush_metadata()

            # Canonical file is unchanged; it still reflects the
            # last successful save.
            self.assertEqual(canonical.read_bytes(), baseline)


class CreateDayFileFsyncTests(unittest.TestCase):
    def test_create_day_file_calls_fsync(self):
        with tempfile.TemporaryDirectory() as d:
            buf = DecimatedBuffer(Path(d), "WWV 10000")
            real_fsync = os.fsync
            calls: list[int] = []

            def tracking_fsync(fd):
                calls.append(fd)
                return real_fsync(fd)

            with mock.patch("hf_timestd.grape.decimated_buffer.os.fsync",
                            side_effect=tracking_fsync):
                # First write triggers _create_day_file.
                buf.write_minute(_minute_ts(), _iq())
            # At minimum: one fsync from create_day_file, one from
            # write_minute.  Tolerate more (e.g. dir fsync from any
            # incidental metadata save).
            self.assertGreaterEqual(len(calls), 2)


class RoundTripTests(unittest.TestCase):
    """Smoke: the durability changes don't break the read/write cycle."""

    def test_write_then_read_returns_same_samples(self):
        with tempfile.TemporaryDirectory() as d:
            buf = DecimatedBuffer(Path(d), "WWV 10000")
            buf.write_minute(_minute_ts(), _iq(value=3 + 4j),
                             gap_samples=0)
            buf.flush_metadata()
            samples, meta = buf.read_minute(_minute_ts())
            self.assertIsNotNone(samples)
            self.assertEqual(samples.shape, (600,))
            self.assertTrue(np.all(samples == np.complex64(3 + 4j)))
            self.assertEqual(meta["minute_index"], 0)

    def test_metadata_survives_simulated_restart(self):
        # Write → flush → drop the in-memory cache → re-read.
        # Simulates daemon restart between metadata flush and next read.
        with tempfile.TemporaryDirectory() as d:
            buf = DecimatedBuffer(Path(d), "WWV 10000")
            buf.write_minute(_minute_ts(), _iq(), gap_samples=42)
            buf.flush_metadata()

            buf._metadata_cache.clear()  # forget what we know
            buf._metadata_dirty.clear()

            samples, meta = buf.read_minute(_minute_ts())
            self.assertIsNotNone(samples)
            self.assertEqual(meta["gap_samples"], 42)


if __name__ == "__main__":
    unittest.main()
