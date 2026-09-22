"""The boundary flag, against the REAL writer.

ARCHIVE_BOUNDARY_ON_THE_EDGE.md §5 step 1 rests on one property: with the flag
off, boundaries land exactly where they land today.  test_archive_boundary.py
proves that for the pure function.  This proves it for the writer that
actually cuts the files — two boxes leave for McMurdo and neither should meet
new behaviour first.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hf_timestd.core.binary_archive_writer import (  # noqa: E402
    BinaryArchiveConfig, BinaryArchiveWriter,
)

SR = 24_000                  # the archived WWV channels' rate
T0 = 1_790_000_000           # a round UTC second
TIMESNAP = 2_150_318_060


def _writer(tmp_path, *, on_pulse=False):
    cfg = BinaryArchiveConfig(
        channel_name="SHARED_10000", frequency_hz=10_000_000.0,
        sample_rate=SR, output_dir=tmp_path / "raw", file_duration_sec=600,
        boundary_on_pulse=on_pulse,
    )
    w = BinaryArchiveWriter(cfg)
    w._gps_time_unix = float(T0)
    w._gps_time_ns_raw = T0 * 10**9
    w._rtp_timesnap = TIMESNAP
    w._timing_locked = True
    return w


def _boundary(w, when=None):
    w._start_new_minute(float(when if when is not None else T0 + 601),
                        TIMESNAP, None)
    return w._current_buffer.start_rtp if hasattr(w, '_current_buffer') else \
        w._last_boundary_placement.chunk_boundary_rtp


class TestFlagOffChangesNothing:

    def test_off_with_a_pulse_present_is_identical_to_no_pulse(self, tmp_path):
        """⛔ THE property.  A station that acquires T6 must cut its files
        exactly where a station without T6 cuts them, until we say otherwise."""
        a = _writer(tmp_path / "a")
        _boundary(a)
        without = a._last_boundary_placement.chunk_boundary_rtp

        b = _writer(tmp_path / "b")
        b.set_pulse_edge_rtp(TIMESNAP + 9_137)      # a pulse well off the anchor
        _boundary(b)
        with_pulse = b._last_boundary_placement.chunk_boundary_rtp

        assert with_pulse == without

    def test_off_still_records_what_the_pulse_would_have_said(self, tmp_path):
        w = _writer(tmp_path)
        w.set_pulse_edge_rtp(TIMESNAP + 9_137)
        _boundary(w)
        p = w._last_boundary_placement
        assert p.source == "anchor"
        assert p.shadow_delta_samples is not None
        assert p.chunk_boundary_rtp == p.anchor_boundary_rtp
        assert p.pulse_boundary_rtp != p.anchor_boundary_rtp

    def test_the_shadow_never_exceeds_half_a_second(self, tmp_path):
        w = _writer(tmp_path)
        for off in (1, SR - 1, 7 * SR + 33, 2**31):
            w.set_pulse_edge_rtp((TIMESNAP + off) & 0xFFFFFFFF)
            _boundary(w)
            d = w._last_boundary_placement.shadow_delta_samples
            assert -SR // 2 <= d <= SR // 2


class TestFlagOnMoves(object):

    def test_on_places_the_cut_on_the_pulse(self, tmp_path):
        w = _writer(tmp_path, on_pulse=True)
        w.set_pulse_edge_rtp(TIMESNAP + 137)
        _boundary(w)
        p = w._last_boundary_placement
        assert p.source == "pulse"
        assert p.chunk_boundary_rtp == p.pulse_boundary_rtp
        assert p.chunk_boundary_rtp != p.anchor_boundary_rtp

    def test_on_without_a_pulse_keeps_the_anchor(self, tmp_path):
        """A station with no TS-1 is unaffected by the flag."""
        w = _writer(tmp_path, on_pulse=True)
        _boundary(w)
        assert w._last_boundary_placement.source == "anchor"


class TestTheSidecarDeclaresIt:

    def test_boundary_fields_are_available_for_the_sidecar(self, tmp_path):
        w = _writer(tmp_path)
        w.set_pulse_edge_rtp(TIMESNAP + 400)
        w.set_bpsk_metadata(16_618_000, applied=False)
        _boundary(w)
        f = w._boundary_fields()
        assert f["boundary_source"] == "anchor"
        assert f["boundary_chain_delay_applied"] is False
        assert "boundary_pulse_minus_anchor_samples" in f

    def test_no_provider_still_returns_exactly_what_it_did(self, tmp_path):
        """⛔ A chunk that has no timing block today must not grow one.
        test_offset_judge pins this; step 1 changes nothing."""
        w = _writer(tmp_path)
        w.set_pulse_edge_rtp(TIMESNAP + 400)
        _boundary(w)
        assert w._time_map_provider is None
        assert w._chunk_timing_block(None, chunk_boundary_utc_ns=T0 * 10**9) is None

    def test_a_writer_with_no_placement_adds_nothing(self, tmp_path):
        """Never invent provenance for a chunk that has none."""
        w = _writer(tmp_path)
        assert w._boundary_fields() == {}

    def test_the_helper_survives_a_half_built_object(self):
        """⚠ Tests and older objects skip __init__; provenance must not take
        recording down with it."""
        w = BinaryArchiveWriter.__new__(BinaryArchiveWriter)
        assert w._boundary_fields() == {}
