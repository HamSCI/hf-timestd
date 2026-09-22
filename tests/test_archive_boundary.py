"""Where a stored chunk begins.

Step 1 of ARCHIVE_BOUNDARY_ON_THE_EDGE.md §5: land the code with the flag
off, and PROVE that off means byte-identical boundaries to today.  That
property carries the whole deployment — two boxes leave for McMurdo and none
of them should meet this behaviour first.

⚠ The wrap is the subtle part.  2**32 % 96000 == 23296, so comparing two
mod-rate phases across a counter wrap jumps 23,296 samples with the edge
physically unmoved.  T6AnchorAuthority suffered exactly that once per wrap
before it switched to a signed 32-bit delta; these tests pin that we never
reintroduce it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hf_timestd.core.archive_boundary import (  # noqa: E402
    PLANE_ANCHOR, PLANE_INJECTION, SOURCE_ANCHOR, SOURCE_PULSE,
    place_boundary, wrapped_signed32,
)

SR = 96_000
SR24 = 24_000


class TestOffIsByteIdenticalToToday:
    """⛔ The property the first deployment rests on."""

    @pytest.mark.parametrize('anchor', [0, 1, 12_345, 2**31, 2**32 - 1, 3_799_660_626])
    @pytest.mark.parametrize('edge', [None, 0, 7_229, 2**32 - 5, 1_234_567])
    def test_flag_off_returns_the_anchor_unchanged(self, anchor, edge):
        p = place_boundary(anchor, SR, edge_rtp=edge, use_pulse=False)
        assert p.chunk_boundary_rtp == (anchor & 0xFFFFFFFF)
        assert p.source == SOURCE_ANCHOR
        assert p.plane == PLANE_ANCHOR

    def test_off_still_reports_what_the_pulse_would_have_said(self):
        """The shadow number is the entire point of shipping it off."""
        p = place_boundary(1_000_000, SR, edge_rtp=1_000_400, use_pulse=False)
        assert p.chunk_boundary_rtp == 1_000_000       # unchanged
        assert p.shadow_delta_samples == 400           # but measured
        assert p.pulse_boundary_rtp == 1_000_400

    def test_no_pulse_reports_no_shadow_rather_than_zero(self):
        """⚠ A missing measurement must never read as agreement."""
        p = place_boundary(1_000_000, SR, edge_rtp=None, use_pulse=False)
        assert p.shadow_delta_samples is None
        assert p.pulse_boundary_rtp is None

    def test_no_pulse_with_the_flag_ON_still_uses_the_anchor(self):
        """A station without a TS-1 keeps today's behaviour, flag or no flag."""
        p = place_boundary(1_000_000, SR, edge_rtp=None, use_pulse=True)
        assert p.chunk_boundary_rtp == 1_000_000
        assert p.source == SOURCE_ANCHOR


class TestTheWrap:
    """2**32 % 96000 == 23296 — the trap that cost the authority once a wrap."""

    def test_signed32_folds_a_near_wrap_difference(self):
        assert wrapped_signed32(1) == 1
        assert wrapped_signed32(-1 & 0xFFFFFFFF) == -1
        assert wrapped_signed32(0x80000000) == -2**31

    def test_an_edge_just_past_the_wrap_is_near_not_far(self):
        anchor = 0xFFFFFFF0          # 16 short of the wrap
        edge = 0x00000010            # 16 past it — 32 samples apart
        p = place_boundary(anchor, SR, edge_rtp=edge, use_pulse=True)
        assert p.shadow_delta_samples == 32
        assert p.chunk_boundary_rtp == 0x00000010

    def test_the_23296_artefact_never_appears(self):
        """A phase-based implementation reports 23,296 here.  A delta-based
        one reports the truth."""
        anchor = 0xFFFFFFFF
        edge = (anchor + 5) & 0xFFFFFFFF
        p = place_boundary(anchor, SR, edge_rtp=edge, use_pulse=False)
        assert p.shadow_delta_samples == 5
        assert abs(p.shadow_delta_samples) != 23_296


class TestSnapping:

    def test_reduces_to_the_nearest_second_not_the_first(self):
        """An edge many seconds away still places the boundary nearby."""
        p = place_boundary(1_000_000, SR, edge_rtp=1_000_000 + 37 * SR + 250,
                           use_pulse=True)
        assert p.shadow_delta_samples == 250
        assert p.chunk_boundary_rtp == 1_000_250

    def test_snaps_backwards_when_that_is_nearer(self):
        p = place_boundary(1_000_000, SR, edge_rtp=1_000_000 + SR - 100,
                           use_pulse=True)
        assert p.shadow_delta_samples == -100
        assert p.chunk_boundary_rtp == 1_000_000 - 100

    def test_never_moves_more_than_half_a_second(self):
        import random
        random.seed(3)
        for _ in range(500):
            a = random.randrange(0, 2**32)
            e = random.randrange(0, 2**32)
            p = place_boundary(a, SR, edge_rtp=e, use_pulse=True)
            assert -SR // 2 <= p.shadow_delta_samples <= SR // 2

    def test_honours_the_channel_rate(self):
        """⛔ The archived channels run 24 kHz, the TS-1 channel 96 kHz."""
        p = place_boundary(1_000_000, SR24, edge_rtp=1_000_000 + SR24 + 60,
                           use_pulse=True)
        assert p.shadow_delta_samples == 60
        for _ in range(50):
            q = place_boundary(1_000_000, SR24, edge_rtp=1_050_000, use_pulse=True)
            assert -SR24 // 2 <= q.shadow_delta_samples <= SR24 // 2


class TestBothPlanesRecorded:
    """mjh, 2026-09-22: record both, apply neither."""

    def test_chain_delay_is_recorded_and_never_applied(self):
        p = place_boundary(1_000_000, SR, edge_rtp=1_000_300,
                           use_pulse=True, chain_delay_ns=16_618_000)
        assert p.chain_delay_ns == 16_618_000
        assert p.chain_delay_applied is False
        # the boundary sits on the pulse, uncorrected
        assert p.chunk_boundary_rtp == 1_000_300

    def test_sidecar_names_the_source_and_the_plane(self):
        f = place_boundary(1_000_000, SR, edge_rtp=1_000_300, use_pulse=True,
                           chain_delay_ns=16_618_000).sidecar_fields()
        assert f["boundary_source"] == SOURCE_PULSE
        assert f["boundary_plane"] == PLANE_INJECTION
        assert f["boundary_chain_delay_applied"] is False
        assert f["boundary_chain_delay_ns"] == 16_618_000
        assert f["boundary_pulse_minus_anchor_samples"] == 300

    def test_sidecar_of_an_anchor_placed_chunk_says_so(self):
        f = place_boundary(1_000_000, SR, edge_rtp=None).sidecar_fields()
        assert f["boundary_source"] == SOURCE_ANCHOR
        assert f["boundary_plane"] == PLANE_ANCHOR
        assert "boundary_pulse_rtp" not in f
        assert "boundary_pulse_minus_anchor_samples" not in f

    def test_every_sidecar_value_is_json_safe(self):
        import json
        for kw in (dict(edge_rtp=None),
                   dict(edge_rtp=1_000_300),
                   dict(edge_rtp=1_000_300, use_pulse=True, chain_delay_ns=16_618_000)):
            json.dumps(place_boundary(1_000_000, SR, **kw).sidecar_fields())


class TestConstruction:

    @pytest.mark.parametrize('rate', [0, -1])
    def test_rejects_a_nonsense_rate(self, rate):
        with pytest.raises(ValueError):
            place_boundary(1_000, rate)
