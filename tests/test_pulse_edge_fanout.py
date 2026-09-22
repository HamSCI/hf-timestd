"""Handing the T6 edge to each archive writer, in that writer's counters.

ARCHIVE_BOUNDARY_ON_THE_EDGE.md step 2.  Every property here exists because
getting it wrong produces a measurement that looks fine and means nothing:
a rejected estimate written into the shadow, a stale snap pairing, or a
silent None where a number was expected.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hf_timestd.core.core_recorder_v2 import CoreRecorderV2  # noqa: E402

SR96, SR24 = 96_000, 24_000
GPS = 1_474_144_017_000_000_000


class FakeWriter:
    def __init__(self):
        self.edge = "never called"

    def set_pulse_edge_rtp(self, v):
        self.edge = v


def _ci(snap, rate, gps=GPS):
    return types.SimpleNamespace(rtp_timesnap=snap, sample_rate=rate, gps_time=gps)


def _rec(snap, rate, gps=GPS):
    return types.SimpleNamespace(archive_writer=FakeWriter(),
                                 channel_info=_ci(snap, rate, gps))


def _core(t6_ci, recorders):
    c = CoreRecorderV2.__new__(CoreRecorderV2)
    c._t6_channel_info = t6_ci
    c.recorders = recorders
    return c


def _est(edge_rtp):
    return types.SimpleNamespace(edge_rtp=edge_rtp)


class TestItTransfersIntoEachChannel:

    def test_the_edge_arrives_converted(self):
        r = _rec(500_000, SR24)
        c = _core(_ci(1_000_000, SR96), {"SHARED_10000": r})
        # a pulse 4 s past the 96 kHz snap
        c._feed_pulse_edge_to_writers(_est(1_000_000 + 4 * SR96))
        assert r.archive_writer.edge == 500_000 + 4 * SR24

    def test_each_channel_gets_its_own_answer(self):
        a, b = _rec(500_000, SR24), _rec(900_000, SR24)
        c = _core(_ci(1_000_000, SR96), {"a": a, "b": b})
        c._feed_pulse_edge_to_writers(_est(1_000_000 + SR96))
        assert a.archive_writer.edge == 500_000 + SR24
        assert b.archive_writer.edge == 900_000 + SR24


class TestItFailsToNoneNeverToAGuess:
    """⚠ An absent measurement must never read as agreement."""

    def test_a_rejected_estimate_clears_the_edge(self):
        """⛔ The caller passes None when the authority refused it."""
        r = _rec(500_000, SR24)
        c = _core(_ci(1_000_000, SR96), {"x": r})
        c._feed_pulse_edge_to_writers(None)
        assert r.archive_writer.edge is None

    def test_no_t6_channel_clears_the_edge(self):
        r = _rec(500_000, SR24)
        c = _core(None, {"x": r})
        c._feed_pulse_edge_to_writers(_est(1_000_000))
        assert r.archive_writer.edge is None

    def test_a_channel_without_info_is_skipped_to_none(self):
        r = _rec(500_000, SR24)
        r.channel_info = None
        c = _core(_ci(1_000_000, SR96), {"x": r})
        c._feed_pulse_edge_to_writers(_est(1_000_000 + SR96))
        assert r.archive_writer.edge is None

    def test_stale_snaps_are_refused(self):
        """⛔ The two snaps ALIGN the counter spaces.  Pairing a fresh one
        with a stale one is how a first measurement of this nearly condemned
        the design (2026-09-22)."""
        r = _rec(500_000, SR24, gps=GPS + 400_000_000)      # 400 ms adrift
        c = _core(_ci(1_000_000, SR96, gps=GPS), {"x": r})
        c._feed_pulse_edge_to_writers(_est(1_000_000 + SR96))
        assert r.archive_writer.edge is None

    def test_a_normal_couple_of_ms_apart_is_fine(self):
        """Measured spread between channels is ~2 ms; that must pass."""
        r = _rec(500_000, SR24, gps=GPS + 2_000_000)
        c = _core(_ci(1_000_000, SR96, gps=GPS), {"x": r})
        c._feed_pulse_edge_to_writers(_est(1_000_000 + SR96))
        assert r.archive_writer.edge == 500_000 + SR24

    def test_a_writer_without_the_setter_is_left_alone(self):
        r = _rec(500_000, SR24)
        r.archive_writer = object()
        c = _core(_ci(1_000_000, SR96), {"x": r})
        c._feed_pulse_edge_to_writers(_est(1_000_000))   # must not raise

    def test_a_recorder_with_no_writer_is_left_alone(self):
        r = _rec(500_000, SR24)
        r.archive_writer = None
        c = _core(_ci(1_000_000, SR96), {"x": r})
        c._feed_pulse_edge_to_writers(_est(1_000_000))   # must not raise

    @pytest.mark.parametrize('rate', [0, None, -1])
    def test_a_nonsense_rate_clears_rather_than_raises(self, rate):
        r = _rec(500_000, rate)
        c = _core(_ci(1_000_000, SR96), {"x": r})
        c._feed_pulse_edge_to_writers(_est(1_000_000 + SR96))
        assert r.archive_writer.edge is None
