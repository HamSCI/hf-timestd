"""The sign contradiction gets watched, not acted on (task 17c).

AC0G-ND, 2026-09-07.  The anchor-direct FUSE feed engaged at 20:41Z and
21:21Z and both times reported the host clock SLOW -- +23..28 ms, then
+60.5 ms -- while four NTP witnesses put the host FAST by +13 and
+30 ms.  chrony followed FUSE and slewed the host the wrong way.  Which
of the two is right is still unknown, and it cannot be settled from the
station's own frame while the closure has every surface on one plane.

So fusion states the three numbers side by side, once a minute, at INFO,
whether or not the closure is on:

    anchor-direct WOULD feed (reference−system, +ve = host slow):
    anchor +60.5 ms | NTP pool -30.0 ms | ring−registration +0.0 ms

⛔ THIS FILE EXISTS TO PIN THE SIGNS.  Review C1 and C2 found two of the
three terms carrying the opposite convention to the one the line claimed,
and the first version of these tests could not see it: they asserted that
the median arithmetic worked and that the string formatted, never that a
number meant what it said.  So every test below states a PHYSICAL
situation -- the host is fast, the host is slow, the two planes agree --
and asserts the printed sign against it.

The three sources disagree natively and each is converted exactly once,
inside ``anchor_sign_line``:

    anchor    LabelPlaneAnchorSample.offset_s   reference − system  as-is
    NTP pool  chronyc -c sources field 7        system − reference  negated
    d_clock   FusedResult.d_clock_fused_ms      system − reference  negated
"""

import math

import pytest

from hf_timestd.core.chrony_stats import (
    parse_csv_sources,
    pool_median_system_minus_reference_ms,
)
from hf_timestd.core.multi_broadcast_fusion import anchor_sign_line

# `chronyc -c sources` on a station with four pool servers, one FUSE
# refclock and one unreachable server.  Fields: mode, state, name,
# stratum, poll, reach, lastRx, adjusted offset, measured offset, margin.
CSV = "\n".join(
    [
        "^,*,162.159.200.1,3,6,377,49,0.013100,0.013050,0.000516",
        "^,+,17.253.20.253,2,6,377,51,0.030200,0.030150,0.000701",
        "^,+,44.190.6.254,2,6,377,12,0.012400,0.012380,0.000480",
        "^,-,72.30.35.88,2,6,377,33,0.029900,0.029880,0.000900",
        "#,*,FUSE,0,4,377,3,-0.150000,-0.150000,0.001000",
        "^,?,10.0.0.9,0,6,0,-,0.000000,0.000000,0.000000",
    ]
)


def test_the_refclocks_are_not_witnesses():
    """FUSE is the thing under test, not a witness of it.

    Mode ``#`` is a local reference clock.  Leaving it in would let the
    registration vote in its own consensus check -- the exact
    circularity the whole line exists to break.
    """
    rows = parse_csv_sources(CSV)
    assert [r.address for r in rows] == [
        "162.159.200.1",
        "17.253.20.253",
        "44.190.6.254",
        "72.30.35.88",
    ]


def test_an_unreachable_server_does_not_vote():
    """``reach`` 0 means nothing has been heard; its 0.0 offset is not a
    measurement and would drag the median toward zero."""
    assert "10.0.0.9" not in [r.address for r in parse_csv_sources(CSV)]


def test_the_parsed_field_is_named_for_chronys_own_convention():
    """C1.  chrony: "Positive offsets indicate that the local clock is
    ahead of the source."  So a positive field means the host is FAST,
    which is ``system − reference`` -- and the attribute says so, because
    the previous name (``offset_s``, documented as ``reference −
    system``) is what let the sign invert unseen."""
    row = parse_csv_sources("^,*,1.2.3.4,3,6,377,49,0.030000,0.030000,0.0005")[0]
    assert row.offset_system_minus_reference_s == pytest.approx(0.030)
    assert not hasattr(row, "offset_s")


def test_the_pool_median_stays_in_chronys_convention():
    """The conversion happens once, at the line -- not here.

    Rows 13.1, 30.2, 12.4, 29.9 ms with the local clock AHEAD of each:
    the median is +21.5 ms in chrony's sense, meaning the host is fast by
    21.5 ms.
    """
    assert pool_median_system_minus_reference_ms(CSV) == pytest.approx(21.5)


def test_no_witnesses_is_none_not_zero():
    """A consensus of zero would read as "the pool agrees the host is
    fine" -- the one thing the pool did not say on 2026-09-07."""
    assert pool_median_system_minus_reference_ms("") is None
    assert (
        pool_median_system_minus_reference_ms("#,*,FUSE,0,4,377,3,-0.15,-0.15,0.001")
        is None
    )


def test_a_malformed_row_is_skipped_not_fatal():
    csv = "\n".join(
        [
            "garbage",
            "^,*,1.2.3.4,3,6,377,49,not-a-number,0.0,0.0",
            "^,*,5.6.7.8,3,6,377,49,0.020000,0.020000,0.0005",
        ]
    )
    assert pool_median_system_minus_reference_ms(csv) == pytest.approx(20.0)


# ── the line itself: every assertion names a physical situation ──────


def test_the_line_names_its_own_convention():
    """A reader must not have to know which way each term ran."""
    line = anchor_sign_line(0.0, 0.0, 0.0)
    assert line.startswith(
        "anchor-direct WOULD feed (reference−system, +ve = host slow):"
    )


def test_the_nd_2121z_episode_prints_as_a_contradiction():
    """C1, the acceptance case.

    The anchor called the host SLOW by 60.5 ms.  The NTP pool had the
    local clock AHEAD by 30 ms -- the host FAST -- which chrony reports
    as +30.0.  In one convention those are opposite signs, and the line
    has to show it: at 5038f6d both printed positive and the line read as
    agreement, arguing FOR the closure on the evidence that condemned it.
    """
    line = anchor_sign_line(
        anchor_reference_minus_system_ms=+60.5,
        pool_median_system_minus_reference_ms=+30.0,
        d_clock_fused_ms=None,
    )
    assert "anchor +60.5 ms" in line
    assert "NTP pool -30.0 ms" in line
    # the whole point: opposite signs, ~90 ms apart
    assert " +60.5 ms" in line and " -30.0 ms" in line


def test_when_both_agree_the_signs_agree():
    """The control for the test above: a pool that really does call the
    host slow prints the same sign as the anchor."""
    line = anchor_sign_line(
        anchor_reference_minus_system_ms=+60.5,
        pool_median_system_minus_reference_ms=-58.0,
        d_clock_fused_ms=None,
    )
    assert "anchor +60.5 ms" in line
    assert "NTP pool +58.0 ms" in line


def test_the_third_term_is_zero_when_the_planes_agree():
    """C2.  ``ring − registration`` must not carry the host clock.

    A host 150 ms fast, measured against a ring plane that IS the
    registration: d_clock = sys − ring = +150 ms, anchor = reg − sys =
    -150 ms.  The two planes agree, so the term is 0 -- however far the
    host has walked.  At 5038f6d the code computed ``d_clock − anchor``,
    printed +300.0 ms here, and reported a 300 ms plane disagreement that
    did not exist.
    """
    line = anchor_sign_line(
        anchor_reference_minus_system_ms=-150.0,
        pool_median_system_minus_reference_ms=None,
        d_clock_fused_ms=+150.0,
    )
    assert "anchor -150.0 ms" in line
    assert "ring−registration +0.0 ms" in line


@pytest.mark.parametrize("host_error_ms", [0.0, -150.0, +60.5, +1000.0])
def test_the_third_term_ignores_the_host_at_any_walk(host_error_ms):
    """The same identity across four host errors, so the cancellation is
    tested as an identity and not as one lucky arithmetic case.

    ``host_error_ms`` is the host's error in the printed convention
    (reference − system), so the anchor reports it directly and d_clock,
    running the other way against the same plane, reports its negative.
    """
    line = anchor_sign_line(
        anchor_reference_minus_system_ms=host_error_ms,
        pool_median_system_minus_reference_ms=None,
        d_clock_fused_ms=-host_error_ms,
    )
    assert "ring−registration +0.0 ms" in line


def test_the_third_term_reports_a_real_plane_gap():
    """A ring plane 25 ms from the registration, on a host 150 ms fast.

    sys − ring = +150, reg − sys = -175, so ring − reg = -(150 - 175) =
    +25: the ring plane reads 25 ms LATER than the registration.
    """
    line = anchor_sign_line(
        anchor_reference_minus_system_ms=-175.0,
        pool_median_system_minus_reference_ms=None,
        d_clock_fused_ms=+150.0,
    )
    assert "ring−registration +25.0 ms" in line


def test_an_unavailable_term_reads_as_such_rather_than_as_zero():
    line = anchor_sign_line(+60.5, None, None)
    assert "anchor +60.5 ms" in line
    assert "NTP pool n/a" in line
    assert "ring−registration n/a" in line


def test_a_nan_term_reads_as_unavailable():
    line = anchor_sign_line(1.0, math.nan, math.nan)
    assert "NTP pool n/a" in line
    assert "ring−registration n/a" in line


def test_the_line_is_written_once_a_minute():
    from hf_timestd.core.multi_broadcast_fusion import (
        ANCHOR_SIGN_LOG_INTERVAL_S,
    )

    assert ANCHOR_SIGN_LOG_INTERVAL_S == 60.0


def test_the_witness_block_reaches_the_line_with_the_closure_off(tmp_path):
    """End to end: judge (closure off) → witness block → the line.

    This is the path a station that never opted in actually runs, and it
    is the one that has to work — the whole purpose of 17c is evidence
    from a station nothing is steering.
    """
    from hf_timestd.core.offset_judge import (
        BenchReading,
        LABEL_PLANE_ANCHOR_KEY,
        LABEL_PLANE_WITNESS_KEY,
        OffsetJudge,
        label_plane_chrony_sample,
    )
    from hf_timestd.core.buffer_timing import unix_ns_to_gps_time_ns

    wall, mono = 1_800_000_000.0, 1000.0
    # The ND 21:21Z geometry: the anchor called the host SLOW by 60.5 ms,
    # i.e. reference − system = +60.5 ms, i.e. the anchor's UTC for the
    # arrival ran LATER than the host clock did (review M7 -- the first
    # version of this test narrated this sign backwards).
    anchor_ms = +60.5
    judge = OffsetJudge(
        config={"enabled": True},
        benches=[
            type(
                "B",
                (),
                {
                    "poll": lambda _s: BenchReading(
                        utc=wall + anchor_ms / 1000.0,
                        mono=mono,
                        sigma_ns=1e6,
                        tier="T3",
                        detail={"bench": "hf_acquired", "authoritative": True},
                        plane="label",
                    )
                },
            )()
        ],
        publish_path=tmp_path / "offset_judge.json",
        time_fn=lambda: wall,
        mono_fn=lambda: mono,
        # A T3 statement on the witness key needs T3 admitted (Ruling B).
        t3_admitted=True,
    )
    judge.register_radiod_pair(
        ("h", 1), unix_ns_to_gps_time_ns(int(wall * 1e9)), 0, 24000
    )
    judge.tick()
    path = tmp_path / "offset_judge.json"
    # Nothing on the key chrony reads...
    assert (
        label_plane_chrony_sample(
            path, key=LABEL_PLANE_ANCHOR_KEY, mono_fn=lambda: mono
        )
        is None
    )
    # ...and the statement intact on the one it does not.
    sample = label_plane_chrony_sample(
        path,
        key=LABEL_PLANE_WITNESS_KEY,
        time_fn=lambda: wall,
        mono_fn=lambda: mono,
    )
    assert sample is not None
    assert sample.offset_s * 1e3 == pytest.approx(anchor_ms, abs=0.01)
    # ...and the pool, in chrony's convention, with the host 30 ms ahead.
    line = anchor_sign_line(sample.offset_s * 1e3, +30.0, None)
    assert "anchor +60.5 ms" in line
    assert "NTP pool -30.0 ms" in line
