"""The (reference_time, system_time) pair pushed to chrony as HPPS.

Regression cover for hf-timestd#18: the pair used to be built by
subtracting exact RTP arithmetic from a wall clock read at PUSH time,
which silently published the stream's arrival latency (measured 13-45 ms
on AC0G-B4) as clock error.
"""
import math

import pytest

from hf_timestd.core.t6_arrival_floor import FloorEstimate
from hf_timestd.core.t6_shm_pair import (
    PRECISION_CEILING,
    PRECISION_FLOOR,
    t6_shm_system_time,
)


def _floor(offset_s, sigma_ns=1_000_000.0):
    return FloorEstimate(
        offset_s=offset_s, sigma_ns=sigma_ns, n=110, span_s=2.0
    )


class TestFloorPath:
    def test_inverts_the_floor_map_into_host_time(self):
        # floor says utc(mono) = mono + 100.0, so the edge labelled
        # utc=1000.0 happened at mono=900.0.  The host clock read
        # wall_now - (mono_now - 900.0) at that moment.
        pair = t6_shm_system_time(
            edge_label_utc_s=1000.0,
            floor=_floor(100.0),
            mono_now=950.0,
            wall_now=5000.0,
            fallback_system_time=999.0,
        )
        assert pair.source == "floor"
        # mono_at_edge = 1000.0 - 100.0 = 900.0
        # system_time  = 900.0 + (5000.0 - 950.0) = 4950.0
        assert pair.system_time == pytest.approx(4950.0, abs=1e-9)

    def test_push_lateness_does_not_move_the_pair(self):
        """The defect, stated as a test.

        Same edge, same floor, but the push runs 30 ms later.  The pair
        must be identical: when the push happens is not information
        about the clock.
        """
        kwargs = dict(
            edge_label_utc_s=1000.0,
            floor=_floor(100.0),
            fallback_system_time=0.0,
        )
        prompt = t6_shm_system_time(mono_now=950.0, wall_now=5000.0, **kwargs)
        late = t6_shm_system_time(
            mono_now=950.030, wall_now=5000.030, **kwargs
        )
        assert late.system_time == pytest.approx(prompt.system_time, abs=1e-9)

    def test_b4_regression_the_measured_worst_case(self):
        """AC0G-B4 2026-08-16: push_lag 548.6 ms gave -44.6 ms.

        Reconstructed as the old code saw it: the edge is 548.6 ms in
        the past at push time, and the newest buffered sample carries
        ~510 ms of that as exact RTP arithmetic, leaving ~38.6 ms of
        arrival latency the old construction published as clock error.
        """
        edge_utc = 1000.0
        # A perfectly disciplined host: floor offset maps mono->utc with
        # no error at all, so an honest pair must report ~zero offset.
        floor_offset = 100.0
        mono_now = (edge_utc - floor_offset) + 0.5486
        wall_now = edge_utc + 0.5486

        old_style_sys_at_edge = wall_now - 0.510  # push_wall - delta/sr
        old_offset_ms = (edge_utc - old_style_sys_at_edge) * 1e3
        assert old_offset_ms == pytest.approx(-38.6, abs=0.1)  # the defect

        pair = t6_shm_system_time(
            edge_label_utc_s=edge_utc,
            floor=_floor(floor_offset),
            mono_now=mono_now,
            wall_now=wall_now,
            fallback_system_time=old_style_sys_at_edge,
        )
        new_offset_ms = (edge_utc - pair.system_time) * 1e3
        assert new_offset_ms == pytest.approx(0.0, abs=1e-6)


class TestFallback:
    def test_no_floor_yields_the_caller_s_value(self):
        pair = t6_shm_system_time(
            edge_label_utc_s=1000.0,
            floor=None,
            mono_now=950.0,
            wall_now=5000.0,
            fallback_system_time=4999.5,
        )
        assert pair.source == "pushwall"
        assert pair.system_time == pytest.approx(4999.5, abs=1e-9)

    def test_fallback_does_not_claim_floor_precision(self):
        """A fallback pair carries the transport bound, not a fiction."""
        pair = t6_shm_system_time(
            edge_label_utc_s=1000.0,
            floor=None,
            mono_now=950.0,
            wall_now=5000.0,
            fallback_system_time=4999.5,
        )
        # 25 ms bound is far wider than the -14 (61 us) the code used to
        # assert unconditionally.
        assert pair.precision > -14
        assert pair.sigma_ns == pytest.approx(25_000_000.0)


class TestPrecision:
    def test_derived_from_measured_sigma(self):
        pair = t6_shm_system_time(
            edge_label_utc_s=1000.0,
            floor=_floor(100.0, sigma_ns=1_000_000.0),  # 1 ms
            mono_now=950.0,
            wall_now=5000.0,
            fallback_system_time=0.0,
        )
        # log2(0.001) = -9.97; conservative truncation toward zero, as
        # multi_broadcast_fusion.py does for FUSE.
        assert pair.precision == -9
        assert 2 ** pair.precision > 0.001  # never claims better than measured

    def test_never_claims_the_hardcoded_61us(self):
        """hf-timestd#18: precision=-14 was asserted regardless of sigma."""
        pair = t6_shm_system_time(
            edge_label_utc_s=1000.0,
            floor=_floor(100.0, sigma_ns=1_445_000.0),  # B4 measured
            mono_now=950.0,
            wall_now=5000.0,
            fallback_system_time=0.0,
        )
        assert pair.precision != -14
        assert pair.precision > -14  # wider, i.e. honest

    @pytest.mark.parametrize(
        "sigma_ns,expected",
        [
            (1.0, PRECISION_FLOOR),            # absurdly good -> clamped
            (10_000_000_000.0, PRECISION_CEILING),  # absurdly bad -> clamped
        ],
    )
    def test_clamped_to_the_publishable_range(self, sigma_ns, expected):
        pair = t6_shm_system_time(
            edge_label_utc_s=1000.0,
            floor=_floor(100.0, sigma_ns=sigma_ns),
            mono_now=950.0,
            wall_now=5000.0,
            fallback_system_time=0.0,
        )
        assert pair.precision == expected

    def test_matches_the_fuse_formula(self):
        """Same derivation FUSE already uses, so the two feeds are
        comparable in chrony's own selection maths."""
        for sigma_ns in (200_000.0, 800_000.0, 3_000_000.0, 20_000_000.0):
            pair = t6_shm_system_time(
                edge_label_utc_s=1000.0,
                floor=_floor(100.0, sigma_ns=sigma_ns),
                mono_now=950.0,
                wall_now=5000.0,
                fallback_system_time=0.0,
            )
            fuse_style = max(
                PRECISION_FLOOR,
                min(PRECISION_CEILING, int(math.log2(sigma_ns / 1e9))),
            )
            assert pair.precision == fuse_style
