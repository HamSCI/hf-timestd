"""authority.json §18 states the anchor's own UTC (task 14a, 2026-09-07).

The other half of "every consumer reads it".  Task 13 put the verified
registration into the ring and into chrony; §18 -- the versioned surface
psk, wspr and meteor-scatter key off -- was still publishing
``gps_unix + judge_offset``, i.e. radiod's host-stamped pair with a
correction bolted on.  On a station whose judge best-bench is FusionBench
(host plane), a subscriber therefore still read a host-derived anchor
while the ticks said otherwise.

mjh's ruling: wherever a label-plane native anchor is in force, that
anchor is the ONLY source of RTP->UTC for every published surface.  The
judge stays a witness -- its verdict still measures the pair and still
drives violation and escalation -- but it no longer defines the plane.
"""

import json

import pytest

from hf_timestd.core.buffer_timing import unix_ns_to_gps_time_ns
from hf_timestd.core.native_anchor import LabelAnchor, NativeAnchor
from hf_timestd.core.offset_judge import BenchReading, OffsetJudge

SR = 24000
WALL0 = 1_800_000_000.0
KEY = ("hf-status.local", 0xABCD1234)
RTP_SNAP = 1000


class _FixedBench:
    def __init__(self, reading):
        self._reading = reading

    def poll(self):
        return self._reading


def _label(
    utc_ref=WALL0,
    rtp_ref=RTP_SNAP,
    sample_rate=SR,
    tier="T3",
    sigma_ns=1.0e6,
    epoch="ep-1",
):
    return LabelAnchor(
        anchor=NativeAnchor(
            anchor_rtp=rtp_ref,
            anchor_utc_ns=int(round(utc_ref * 1e9)),
            sample_rate_hz=sample_rate,
            chain_delay_ns=0,
            captured_at_utc_ns=int(round(utc_ref * 1e9)),
            captured_via_tier=tier,
        ),
        epoch_id=epoch,
        tier=tier,
        sigma_ns=sigma_ns,
    )


def _judge(
    tmp_path,
    *,
    host_wall,
    provider=None,
    bench_utc=None,
    bench_tier="T3",
    bench_sigma=600_000.0,
    pair_gps_unix=None,
):
    """A judge whose radiod pair is host-stamped at ``pair_gps_unix``."""
    reading = BenchReading(
        tier=bench_tier,
        utc=host_wall if bench_utc is None else bench_utc,
        sigma_ns=bench_sigma,
        mono=1000.0,
        plane="host",
    )
    judge = OffsetJudge(
        config={"enabled": True},
        benches=[_FixedBench(reading)],
        publish_path=tmp_path / "offset_judge.json",
        time_fn=lambda: host_wall,
        mono_fn=lambda: 1000.0,
        # Task 17a: these tests are ABOUT the anchor closure, which is now
        # opt-in and off by default, so they opt in explicitly.
        anchor_closure=True,
        t3_admitted=True,
    )
    if provider is not None:
        judge.set_label_anchor_provider(provider)
    pair_unix = host_wall if pair_gps_unix is None else pair_gps_unix
    judge.register_radiod_pair(
        KEY, unix_ns_to_gps_time_ns(int(round(pair_unix * 1e9))), RTP_SNAP, SR
    )
    judge.tick()
    return judge


def _section18(tmp_path):
    snap = json.loads((tmp_path / "offset_judge.json").read_text())
    key = f"{KEY[0]}/{KEY[1]:08x}"
    return snap["contract_v07"]["sources"][key]


# ── the sign trace ───────────────────────────────────────────────────


def test_utc_anchor_ns_follows_the_anchor_not_the_host_stamped_pair(tmp_path):
    """The AC0G-ND 2026-09-07 shape, at §18.

    The host clock is 150 ms fast, so radiod stamps GPS_TIME 150 ms fast
    and the pair claims sample RTP_SNAP was at WALL0 + 0.150.  The
    registration says it was at WALL0.  §18 must publish WALL0.
    """
    label = _label(utc_ref=WALL0)
    src = None
    _judge(
        tmp_path,
        host_wall=WALL0 + 0.150,
        pair_gps_unix=WALL0 + 0.150,  # radiod stamps from the host clock
        provider=lambda: label,
    )
    src = _section18(tmp_path)
    assert src["rtp_anchor_sample"] == RTP_SNAP
    # 1 us: double precision at Unix-epoch magnitude resolves ~200 ns.
    assert src["utc_anchor_ns"] == pytest.approx(int(WALL0 * 1e9), abs=1000)
    # and NOT the host-stamped pair, 150 ms away
    assert abs(src["utc_anchor_ns"] - int((WALL0 + 0.150) * 1e9)) > 149_000_000
    assert src["plane_source"] == "t3_registration"
    assert src["tier"] == "T3"
    assert src["sigma_ns"] == pytest.approx(1.0e6)
    assert src["rate_samples_per_utc_sec"] == SR


def test_the_anchor_projects_by_pure_counter_arithmetic(tmp_path):
    """§18 names rtp_anchor_sample, which is radiod's snap and NOT the
    registration's rtp_ref.  The published UTC is the anchor's own label
    for that counter value -- projected, never re-derived from a clock."""
    label = _label(utc_ref=WALL0, rtp_ref=RTP_SNAP - 3 * SR)  # 3 s earlier
    _judge(
        tmp_path,
        host_wall=WALL0 + 0.150,
        pair_gps_unix=WALL0 + 0.150,
        provider=lambda: label,
    )
    src = _section18(tmp_path)
    assert src["utc_anchor_ns"] == pytest.approx(int((WALL0 + 3.0) * 1e9), abs=1000)


def test_a_t6_anchor_reports_its_own_tier_and_sigma(tmp_path):
    label = _label(tier="T6", sigma_ns=50_000.0)
    _judge(tmp_path, host_wall=WALL0, provider=lambda: label)
    src = _section18(tmp_path)
    assert src["tier"] == "T6"
    assert src["sigma_ns"] == pytest.approx(50_000.0)
    assert src["plane_source"] == "t6_native"


# ── the legacy path is untouched ─────────────────────────────────────


def test_without_an_anchor_section18_is_the_judged_pair(tmp_path):
    """Byte-identical pre-task-14 behaviour: gps_unix + the judge's
    offset, with the judge's own tier and sigma."""
    _judge(tmp_path, host_wall=WALL0, pair_gps_unix=WALL0 - 1.0)
    src = _section18(tmp_path)
    # the pair was 1 s behind; the bench put truth at WALL0, so the
    # judge's offset is +1 s and the published anchor lands at truth
    assert src["utc_anchor_ns"] == pytest.approx(int(WALL0 * 1e9), abs=2_000_000)
    assert src["plane_source"] == "radiod_pair_judged"
    assert src["tier"] == "T3" and src["sigma_ns"] == pytest.approx(600_000.0)
    assert src["offset_ns"] == pytest.approx(1e9, abs=2e6)


def test_a_foreign_counter_domain_falls_back_to_the_judged_pair(tmp_path):
    """T6's anchor lives in the 96 kHz BPSK counter; it must never label
    a 24 kHz archive source (cross_channel_rtp.py)."""
    label = _label(tier="T6", sample_rate=96000, sigma_ns=50_000.0)
    _judge(tmp_path, host_wall=WALL0, pair_gps_unix=WALL0, provider=lambda: label)
    src = _section18(tmp_path)
    assert src["plane_source"] == "radiod_pair_judged"
    assert src["tier"] == "T3"


def test_a_raising_provider_falls_back_to_the_judged_pair(tmp_path):
    def boom():
        raise RuntimeError("nope")

    _judge(tmp_path, host_wall=WALL0, pair_gps_unix=WALL0, provider=boom)
    src = _section18(tmp_path)
    assert src["plane_source"] == "radiod_pair_judged"


def test_the_anchor_publishes_even_before_the_judge_has_a_verdict(tmp_path):
    """The anchor does not need the judge to be true.  With no bench
    answering there is no verdict, and the legacy field would be null --
    but a registered plane can still state the source's UTC."""
    label = _label(utc_ref=WALL0)
    judge = OffsetJudge(
        config={"enabled": True},
        benches=[],  # nothing answers
        publish_path=tmp_path / "offset_judge.json",
        time_fn=lambda: WALL0,
        mono_fn=lambda: 1000.0,
        # Task 17a: these tests are ABOUT the anchor closure, which is now
        # opt-in and off by default, so they opt in explicitly.
        anchor_closure=True,
        t3_admitted=True,
    )
    judge.set_label_anchor_provider(lambda: label)
    judge.register_radiod_pair(
        KEY, unix_ns_to_gps_time_ns(int(WALL0 * 1e9)), RTP_SNAP, SR
    )
    judge.tick()
    src = _section18(tmp_path)
    assert src["utc_anchor_ns"] == pytest.approx(int(WALL0 * 1e9), abs=1000)
    assert src["snapshot_age_s"] is None  # no verdict -> no judge age
    assert src["offset_ns"] is None
    assert src["plane_source"] == "t3_registration"


def test_the_judge_stays_a_witness(tmp_path):
    """§18 reports the anchor, and the judge's own diagnostic block still
    reports the judge: the offset it measured against radiod's pair is
    intact, so the disagreement stays visible instead of being erased by
    the surface that now ignores it."""
    label = _label(utc_ref=WALL0)
    _judge(
        tmp_path,
        host_wall=WALL0 + 0.150,
        pair_gps_unix=WALL0 + 0.150,
        provider=lambda: label,
    )
    snap = json.loads((tmp_path / "offset_judge.json").read_text())
    key = f"{KEY[0]}/{KEY[1]:08x}"
    diag = snap["sources"][key]
    assert diag["offset_ns"] == pytest.approx(0.0, abs=2e6)  # host bench
    assert diag["tier"] == "T3"
    assert snap["judge"]["sigma_ns"] == pytest.approx(600_000.0)
