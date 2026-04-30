"""Tests for QualitySnapshotWriter.

The writer is the bridge between the recorder's in-memory StreamQuality
and sigmond's CLI subcommand.  Recorders are mocked — these tests don't
spin up RadiodStream; they verify the payload shape, atomicity, delta-
rate math, and degradation paths (no data, stream restart, write errors).
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from hf_timestd.core.quality_snapshot import (
    QualitySnapshotWriter, SCHEMA_VERSION,
)


def _quality(**kw) -> MagicMock:
    """Build a MagicMock standing in for ka9q.StreamQuality."""
    q = MagicMock()
    defaults = dict(
        rtp_packets_received=0, rtp_packets_lost=0,
        rtp_packets_late=0, rtp_packets_duplicate=0,
        rtp_packets_resequenced=0, total_gaps_filled=0,
        total_gap_events=0, total_samples_delivered=0,
        completeness_pct=100.0,
    )
    defaults.update(kw)
    for k, v in defaults.items():
        setattr(q, k, v)
    return q


def _recorder(description: str, frequency_hz: int, ssrc: int, *,
              state: str = "RECORDING",
              session_start_time: float = 0.0,
              quality=None) -> MagicMock:
    rec = MagicMock()
    rec.config = MagicMock(frequency_hz=frequency_hz, ssrc=ssrc,
                           description=description)
    rec.state = MagicMock(value=state)
    rec.session_start_time = session_start_time
    rec.last_quality = quality
    return rec


class PayloadShapeTests(unittest.TestCase):
    def test_basic_shape(self):
        recs = {"WWV_5000": _recorder("WWV_5000", 5_000_000, 1234,
                                       quality=_quality(
                                           rtp_packets_received=1000,
                                           rtp_packets_lost=2,
                                           completeness_pct=99.8))}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "quality.json"
            w = QualitySnapshotWriter(recs, path=str(path),
                                      clock=lambda: 100.0)
            w.tick()
            payload = json.loads(path.read_text())

        self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
        self.assertEqual(payload["client"], "hf-timestd")
        self.assertEqual(payload["captured_at"], 100.0)
        self.assertEqual(len(payload["recorders"]), 1)
        r = payload["recorders"][0]
        self.assertEqual(r["description"], "WWV_5000")
        self.assertEqual(r["frequency_hz"], 5_000_000)
        self.assertEqual(r["ssrc"], 1234)
        self.assertEqual(r["packets_received_total"], 1000)
        self.assertEqual(r["packets_lost_total"], 2)
        self.assertEqual(r["completeness_pct"], 99.8)
        self.assertEqual(r["stream_state"], "RECORDING")

    def test_no_data_recorder_emits_minimal_entry(self):
        recs = {"WWV_5000": _recorder("WWV_5000", 5_000_000, 1234,
                                       quality=None)}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "q.json"
            QualitySnapshotWriter(recs, path=str(path),
                                  clock=lambda: 100.0).tick()
            payload = json.loads(path.read_text())
        r = payload["recorders"][0]
        self.assertTrue(r["no_data"])
        self.assertNotIn("packets_lost_total", r)
        self.assertNotIn("completeness_pct", r)

    def test_no_double_total_in_field_names(self):
        # Regression: ka9q-python's StreamQuality already prefixes some
        # cumulative counters with "total_" (total_gaps_filled,
        # total_gap_events, total_samples_delivered).  The writer must
        # NOT produce "total_*_total" doubled names — schema is
        # cumulative-suffix only.
        recs = {"X": _recorder("X", 1, 1, quality=_quality(
            total_gaps_filled=2400, total_gap_events=5,
            total_samples_delivered=86400000))}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "q.json"
            QualitySnapshotWriter(recs, path=str(path),
                                  clock=lambda: 100.0).tick()
            r = json.loads(path.read_text())["recorders"][0]
        # New, clean names exist:
        self.assertEqual(r["gaps_filled_total"], 2400)
        self.assertEqual(r["gap_events_total"], 5)
        self.assertEqual(r["samples_delivered_total"], 86400000)
        # Old, doubled names must not:
        self.assertNotIn("total_gaps_filled_total", r)
        self.assertNotIn("total_gap_events_total", r)
        self.assertNotIn("total_samples_delivered_total", r)

    def test_uptime_computed_from_session_start_time(self):
        recs = {"X": _recorder("X", 1, 1,
                               session_start_time=50.0,
                               quality=_quality())}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "q.json"
            QualitySnapshotWriter(recs, path=str(path),
                                  clock=lambda: 100.0).tick()
            payload = json.loads(path.read_text())
        self.assertEqual(payload["recorders"][0]["uptime_seconds"], 50.0)


class DeltaRateTests(unittest.TestCase):
    def test_first_tick_rate_is_zero(self):
        recs = {"X": _recorder("X", 1, 1, quality=_quality(
            rtp_packets_lost=10))}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "q.json"
            QualitySnapshotWriter(recs, path=str(path),
                                  clock=lambda: 100.0).tick()
            payload = json.loads(path.read_text())
        r = payload["recorders"][0]
        self.assertEqual(r["packets_lost_total"], 10)
        self.assertEqual(r["packets_lost_rate"], 0.0)

    def test_second_tick_computes_rate(self):
        # Tick 1 at t=100 with 10 lost; tick 2 at t=160 with 40 lost.
        # +30 over 60s = 0.5/s.
        rec = _recorder("X", 1, 1, quality=_quality(rtp_packets_lost=10))
        recs = {"X": rec}
        clocks = iter([100.0, 160.0])
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "q.json"
            w = QualitySnapshotWriter(recs, path=str(path),
                                      clock=lambda: next(clocks))
            w.tick()
            rec.last_quality = _quality(rtp_packets_lost=40)
            w.tick()
            payload = json.loads(path.read_text())
        r = payload["recorders"][0]
        self.assertEqual(r["packets_lost_total"], 40)
        self.assertEqual(r["packets_lost_rate"], 0.5)

    def test_stream_restart_clamps_rate(self):
        # Counter went 100 → 5 (stream restarted, counter reset).  Rate
        # must be 0, not negative.
        rec = _recorder("X", 1, 1, quality=_quality(rtp_packets_lost=100))
        recs = {"X": rec}
        clocks = iter([100.0, 160.0])
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "q.json"
            w = QualitySnapshotWriter(recs, path=str(path),
                                      clock=lambda: next(clocks))
            w.tick()
            rec.last_quality = _quality(rtp_packets_lost=5)
            w.tick()
            payload = json.loads(path.read_text())
        self.assertEqual(payload["recorders"][0]["packets_lost_rate"], 0.0)

    def test_zero_interval_no_rate(self):
        rec = _recorder("X", 1, 1, quality=_quality(rtp_packets_lost=1))
        recs = {"X": rec}
        clocks = iter([100.0, 100.0])
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "q.json"
            w = QualitySnapshotWriter(recs, path=str(path),
                                      clock=lambda: next(clocks))
            w.tick()
            rec.last_quality = _quality(rtp_packets_lost=10)
            w.tick()
            payload = json.loads(path.read_text())
        self.assertEqual(payload["recorders"][0]["packets_lost_rate"], 0.0)


class SummaryTests(unittest.TestCase):
    def test_aggregates_across_recorders(self):
        recs = {
            "A": _recorder("A", 1, 1, quality=_quality(
                rtp_packets_lost=5, completeness_pct=99.0)),
            "B": _recorder("B", 2, 2, quality=_quality(
                rtp_packets_lost=10, completeness_pct=98.5)),
        }
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "q.json"
            QualitySnapshotWriter(recs, path=str(path),
                                  clock=lambda: 100.0).tick()
            payload = json.loads(path.read_text())
        s = payload["summary"]
        self.assertEqual(s["recorder_count"], 2)
        self.assertEqual(s["recorders_with_data"], 2)
        self.assertEqual(s["total_packets_lost"], 15)
        self.assertEqual(s["min_completeness_pct"], 98.5)

    def test_summary_when_all_recorders_no_data(self):
        recs = {"A": _recorder("A", 1, 1, quality=None),
                "B": _recorder("B", 2, 2, quality=None)}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "q.json"
            QualitySnapshotWriter(recs, path=str(path),
                                  clock=lambda: 100.0).tick()
            payload = json.loads(path.read_text())
        s = payload["summary"]
        self.assertEqual(s["recorder_count"], 2)
        self.assertEqual(s["recorders_with_data"], 0)
        self.assertIsNone(s["min_completeness_pct"])


class AtomicityTests(unittest.TestCase):
    def test_replace_is_atomic_via_tmp_then_rename(self):
        # We can't easily race the writer in a unit test, but we can
        # verify the .tmp file is gone (replaced) after a successful
        # write and the final file exists.
        recs = {"A": _recorder("A", 1, 1, quality=_quality())}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "q.json"
            QualitySnapshotWriter(recs, path=str(path),
                                  clock=lambda: 100.0).tick()
            self.assertTrue(path.exists())
            self.assertFalse(path.with_suffix(".json.tmp").exists())

    def test_creates_parent_directory(self):
        recs = {"A": _recorder("A", 1, 1, quality=_quality())}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "nested" / "subdir" / "q.json"
            QualitySnapshotWriter(recs, path=str(path),
                                  clock=lambda: 100.0).tick()
            self.assertTrue(path.exists())

    def test_collection_failure_does_not_crash(self):
        # If one recorder's attribute access blows up, the writer logs
        # and skips the tick — the recorder main loop must not die.
        bad = MagicMock()
        # Make attribute access raise:
        type(bad).config = property(
            lambda self: (_ for _ in ()).throw(RuntimeError("simulated")))
        recs = {"bad": bad}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "q.json"
            w = QualitySnapshotWriter(recs, path=str(path),
                                      clock=lambda: 100.0)
            # Must not raise
            w.tick()
            # And no file written
            self.assertFalse(path.exists())


if __name__ == '__main__':
    unittest.main()
