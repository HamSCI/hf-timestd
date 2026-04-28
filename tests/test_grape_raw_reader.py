"""Tests for RawBinaryReader chunk-index discovery.

Before this work, ``read_minute`` guessed chunk durations from a
hardcoded list ``(600, 300, 900, 3600)``.  Any other ``file_duration_sec``
the recorder happened to write produced silent gap reads — zeros fed
into the decimator with no error.

These tests pin down the new directory-scan behavior:

  - legacy 1-minute files still work;
  - 600s chunks are indexed correctly (every minute resolves to the
    correct slice offset);
  - non-standard durations like 1200s (which the old heuristic would
    have missed entirely) resolve correctly;
  - missing data minutes return ``(None, None)`` cleanly;
  - the directory scan is cached (one ``glob`` per ``day_dir`` per
    reader, regardless of how many ``read_minute`` calls).
"""

from __future__ import annotations

import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = str(REPO_ROOT / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from hf_timestd.grape.raw_reader import RawBinaryReader  # noqa: E402


SAMPLE_RATE = 24000


def _write_chunk(day_dir: Path, chunk_ts: int,
                 duration_sec: int = 60,
                 sample_rate: int = SAMPLE_RATE,
                 fill_value: complex = 1 + 0j) -> None:
    """Drop a .bin + .json pair representing a chunk at chunk_ts."""
    day_dir.mkdir(parents=True, exist_ok=True)
    n_samples = sample_rate * duration_sec
    samples = np.full(n_samples, fill_value, dtype=np.complex64)
    (day_dir / f"{chunk_ts}.bin").write_bytes(samples.tobytes())
    sidecar = {
        "sample_rate": sample_rate,
        "file_duration_sec": duration_sec,
        # The recorder's real sidecar has many more fields; we only
        # need the two the reader consults.
    }
    (day_dir / f"{chunk_ts}.json").write_text(json.dumps(sidecar))


def _reader_for(data_root: Path, channel: str = "WWV 10000",
                hot_root: Path = None) -> RawBinaryReader:
    return RawBinaryReader(
        data_root, channel,
        hot_buffer_root=hot_root or (data_root / "no-hot"),
    )


class ChunkIndexTests(unittest.TestCase):
    def _channel_dir(self, root: Path,
                     channel: str = "WWV 10000") -> Path:
        return root / "raw_buffer" / channel.replace(" ", "_")

    def test_legacy_one_minute_files_indexed(self):
        with tempfile.TemporaryDirectory() as d:
            d_path = Path(d)
            day_dir = self._channel_dir(d_path) / "20250428"
            # Three consecutive minutes, 60s each.
            for ts in (1745798400, 1745798460, 1745798520):
                _write_chunk(day_dir, ts, duration_sec=60)
            reader = _reader_for(d_path)
            index = reader._chunk_index_for(day_dir)
            self.assertEqual(set(index.keys()),
                             {1745798400, 1745798460, 1745798520})
            for ts, (stem, offset) in index.items():
                self.assertEqual(stem, str(ts))
                self.assertEqual(offset, 0)

    def test_600s_chunks_indexed_at_every_minute(self):
        with tempfile.TemporaryDirectory() as d:
            d_path = Path(d)
            day_dir = self._channel_dir(d_path) / "20250428"
            chunk_ts = 1745798400  # aligned to 10-min boundary
            _write_chunk(day_dir, chunk_ts, duration_sec=600)
            reader = _reader_for(d_path)
            index = reader._chunk_index_for(day_dir)
            self.assertEqual(len(index), 10)
            for i in range(10):
                minute_ts = chunk_ts + 60 * i
                stem, offset = index[minute_ts]
                self.assertEqual(stem, str(chunk_ts))
                self.assertEqual(offset, 60 * i)

    def test_non_standard_1200s_chunks_indexed(self):
        # The OLD heuristic (600, 300, 900, 3600) would miss 1200s
        # chunks entirely — every minute would silently resolve to
        # "gap".  The new scan-based reader handles any duration.
        with tempfile.TemporaryDirectory() as d:
            d_path = Path(d)
            day_dir = self._channel_dir(d_path) / "20250428"
            chunk_ts = 1745798400
            _write_chunk(day_dir, chunk_ts, duration_sec=1200)
            reader = _reader_for(d_path)
            index = reader._chunk_index_for(day_dir)
            self.assertEqual(len(index), 20)
            stem, offset = index[chunk_ts + 60 * 19]
            self.assertEqual(stem, str(chunk_ts))
            self.assertEqual(offset, 60 * 19)

    def test_missing_dir_returns_empty_index(self):
        with tempfile.TemporaryDirectory() as d:
            reader = _reader_for(Path(d))
            self.assertEqual(reader._chunk_index_for(
                Path(d) / "absent" / "20250428"), {})

    def test_unparseable_duration_skipped_with_warning(self):
        with tempfile.TemporaryDirectory() as d:
            d_path = Path(d)
            day_dir = self._channel_dir(d_path) / "20250428"
            day_dir.mkdir(parents=True)
            # Sidecar with garbage duration.
            (day_dir / "1745798400.bin").write_bytes(b"\x00" * 100)
            (day_dir / "1745798400.json").write_text(
                json.dumps({"sample_rate": 24000, "file_duration_sec": -5}))
            # Plus a sane chunk so we know the scan continued past
            # the bad one.
            _write_chunk(day_dir, 1745798460, duration_sec=60)
            reader = _reader_for(d_path)
            index = reader._chunk_index_for(day_dir)
            self.assertNotIn(1745798400, index)
            self.assertIn(1745798460, index)

    def test_index_is_cached(self):
        with tempfile.TemporaryDirectory() as d:
            d_path = Path(d)
            day_dir = self._channel_dir(d_path) / "20250428"
            _write_chunk(day_dir, 1745798400, duration_sec=600)
            reader = _reader_for(d_path)

            with mock.patch.object(
                Path, "glob",
                side_effect=Path.glob, autospec=True,
            ) as glob_spy:
                reader._chunk_index_for(day_dir)
                reader._chunk_index_for(day_dir)
                reader._chunk_index_for(day_dir)
            # Exactly one glob call across three lookups.
            self.assertEqual(glob_spy.call_count, 1)


class ReadMinuteTests(unittest.TestCase):
    def _channel_dir(self, root: Path,
                     channel: str = "WWV 10000") -> Path:
        return root / "raw_buffer" / channel.replace(" ", "_")

    def test_read_minute_legacy_returns_correct_samples(self):
        with tempfile.TemporaryDirectory() as d:
            d_path = Path(d)
            day_dir = self._channel_dir(d_path) / "20250428"
            ts = 1745798400
            _write_chunk(day_dir, ts, duration_sec=60,
                         fill_value=2 + 3j)
            reader = _reader_for(d_path)
            samples, meta = reader.read_minute(ts)
            self.assertIsNotNone(samples)
            self.assertEqual(samples.dtype, np.complex64)
            self.assertEqual(samples.shape, (SAMPLE_RATE * 60,))
            self.assertTrue(np.all(samples == np.complex64(2 + 3j)))
            self.assertEqual(meta["sample_rate"], SAMPLE_RATE)

    def test_read_minute_slices_correct_offset_in_600s_chunk(self):
        with tempfile.TemporaryDirectory() as d:
            d_path = Path(d)
            day_dir = self._channel_dir(d_path) / "20250428"
            chunk_ts = 1745798400
            # Build a chunk whose value tracks sample index so we can
            # verify slicing without relying on uniform fill.
            day_dir.mkdir(parents=True, exist_ok=True)
            n = SAMPLE_RATE * 600
            ramp = np.arange(n, dtype=np.float32).astype(np.complex64)
            (day_dir / f"{chunk_ts}.bin").write_bytes(ramp.tobytes())
            (day_dir / f"{chunk_ts}.json").write_text(json.dumps({
                "sample_rate": SAMPLE_RATE,
                "file_duration_sec": 600,
            }))
            reader = _reader_for(d_path)
            # Read the 5th minute (index 4): expect samples[24000*240..24000*300].
            target_minute = chunk_ts + 4 * 60
            samples, meta = reader.read_minute(target_minute)
            self.assertEqual(samples.shape, (SAMPLE_RATE * 60,))
            expected_start = SAMPLE_RATE * 4 * 60
            self.assertEqual(int(samples[0].real), expected_start)
            self.assertEqual(int(samples[-1].real),
                             expected_start + SAMPLE_RATE * 60 - 1)

    def test_read_minute_returns_none_for_uncovered_minute(self):
        with tempfile.TemporaryDirectory() as d:
            d_path = Path(d)
            day_dir = self._channel_dir(d_path) / "20250428"
            _write_chunk(day_dir, 1745798400, duration_sec=60)
            reader = _reader_for(d_path)
            # 10 minutes later — no chunk covers it.
            samples, meta = reader.read_minute(1745798400 + 600)
            self.assertIsNone(samples)
            self.assertIsNone(meta)

    def test_read_minute_handles_1200s_chunk(self):
        with tempfile.TemporaryDirectory() as d:
            d_path = Path(d)
            day_dir = self._channel_dir(d_path) / "20250428"
            chunk_ts = 1745798400
            _write_chunk(day_dir, chunk_ts, duration_sec=1200,
                         fill_value=7 + 0j)
            reader = _reader_for(d_path)
            # 19th minute inside the 1200s chunk.
            samples, meta = reader.read_minute(chunk_ts + 19 * 60)
            self.assertIsNotNone(samples)
            self.assertEqual(samples.shape, (SAMPLE_RATE * 60,))
            self.assertTrue(np.all(samples == np.complex64(7 + 0j)))


class GetAvailableMinutesTests(unittest.TestCase):
    def _channel_dir(self, root: Path,
                     channel: str = "WWV 10000") -> Path:
        return root / "raw_buffer" / channel.replace(" ", "_")

    def test_filters_to_requested_day(self):
        with tempfile.TemporaryDirectory() as d:
            d_path = Path(d)
            channel = self._channel_dir(d_path)
            day = channel / "20250428"
            prev_day = channel / "20250427"
            # Day-start UTC 20250428 = 1745798400 (assuming wall test);
            # verify by computing.
            from datetime import datetime, timezone
            day_start = int(datetime(2025, 4, 28, tzinfo=timezone.utc).timestamp())
            # Two minutes inside 2025-04-28.
            _write_chunk(day, day_start, duration_sec=60)
            _write_chunk(day, day_start + 60, duration_sec=60)
            # Plus a chunk in 2025-04-27 right at the end of that day.
            _write_chunk(prev_day, day_start - 60, duration_sec=60)

            reader = _reader_for(d_path)
            mins = reader.get_available_minutes('20250428')
            self.assertIn(day_start, mins)
            self.assertIn(day_start + 60, mins)
            self.assertNotIn(day_start - 60, mins)


if __name__ == "__main__":
    unittest.main()
