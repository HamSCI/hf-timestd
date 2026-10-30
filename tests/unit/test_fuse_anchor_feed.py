"""The FUSE feed carries the anchor, not the host clock (spec §11.2,
revised 2026-09-07).

Today's FUSE sample is ``system_time − d_clock``.  d_clock is fusion's
estimate of the HOST clock's offset from the plane the measurements were
labelled on — so when the ring plane and the host clock are the same
thing (radiod stamps GPS_TIME from the host clock), d_clock ≈ 0 votes
"the host is right" however far the host has walked.  That is the
AC0G-ND 2026-09-07 mechanism.

The fix is not to add a correction to d_clock.  It is to hand chrony the
measurement pair the label-plane anchor already forms:

    reference_time = anchor's UTC of the newest arrived sample
    system_time    = the host clock AT THAT ARRIVAL INSTANT

chrony's own offset is then ``reference − system`` — the host clock's
error against the tick-aligned UTC, measured once, with no d_clock term
anywhere in it.  Nothing is summed, so nothing can be double-counted.
"""

import json

import pytest

from hf_timestd.core.offset_judge import (
    BenchReading,
    OffsetJudge,
    label_plane_chrony_sample,
    read_label_plane_anchor,
)

SR = 24000
WALL0 = 1_800_000_000.0
KEY = ("hf-status.local", 0xABCD1234)


class _FixedBench:
    def __init__(self, reading):
        self._reading = reading

    def poll(self):
        return self._reading


def _unix_to_gps_ns(unix_s: float) -> int:
    from hf_timestd.core.buffer_timing import unix_ns_to_gps_time_ns

    return unix_ns_to_gps_time_ns(int(round(unix_s * 1e9)))


def _judge(tmp_path, benches, wall, mono):
    judge = OffsetJudge(
        config={"enabled": True},
        benches=benches,
        publish_path=tmp_path / "offset_judge.json",
        time_fn=lambda: wall[0],
        mono_fn=lambda: mono[0],
        # Task 17a: these tests are ABOUT the anchor closure, which is now
        # opt-in and off by default, so they opt in explicitly.
        anchor_closure=True,
        t3_admitted=True,
    )
    judge.register_radiod_pair(KEY, _unix_to_gps_ns(wall[0]), 0, SR)
    return judge


def _published(tmp_path):
    return json.loads((tmp_path / "offset_judge.json").read_text())


# ── the judge publishes the anchor pair ──────────────────────────────


def test_the_label_plane_reading_is_published_as_an_anchor(tmp_path):
    """(utc, mono) of the label-plane bench IS the anchor's UTC of the
    newest arrived sample placed at its arrival instant — the shape
    HfAcquiredBench and NativeAnchorBench already form."""
    wall, mono = [WALL0], [1000.0]
    reading = BenchReading(
        tier="T3",
        utc=WALL0 - 0.150,
        sigma_ns=1_000_000.0,
        mono=998.0,
        detail={"bench": "hf_acquired"},
        plane="label",
    )
    judge = _judge(tmp_path, [_FixedBench(reading)], wall, mono)
    judge.tick()
    block = _published(tmp_path)["label_plane_anchor"]
    assert block["bench"] == "hf_acquired"
    assert block["tier"] == "T3"
    assert block["utc"] == pytest.approx(WALL0 - 0.150, abs=1e-9)
    assert block["mono"] == pytest.approx(998.0)
    assert block["sigma_ns"] == pytest.approx(1_000_000.0)
    assert block["age_s"] == pytest.approx(2.0)


def test_the_anchor_is_published_even_when_a_host_bench_is_selected(tmp_path):
    """hf_acquired stays a WITNESS in the judge's tier arbitration (mjh
    reverted the precedence change on 2026-09-07).  The anchor block must
    therefore NOT be "the selected bench" — it is the label plane's own
    statement, published whether or not the judge adopted it."""
    wall, mono = [WALL0], [1000.0]
    # The two agree to 0.5 ms so the cross-bench gate passes and the
    # TIGHTER host bench wins the tier arbitration, exactly as it does on
    # a live station where hf_acquired is only a witness.
    host = BenchReading(
        tier="T3", utc=WALL0, sigma_ns=600_000.0, mono=1000.0, plane="host"
    )
    label = BenchReading(
        tier="T3",
        utc=WALL0 - 0.0005,
        sigma_ns=1_000_000.0,
        mono=1000.0,
        detail={"bench": "hf_acquired"},
        plane="label",
    )
    judge = _judge(tmp_path, [_FixedBench(host), _FixedBench(label)], wall, mono)
    judge.tick()
    snap = _published(tmp_path)
    assert snap["judge"]["tier"] == "T3"
    assert snap["judge"]["sigma_ns"] == pytest.approx(600_000.0)  # host won
    assert snap["label_plane_anchor"]["bench"] == "hf_acquired"
    assert snap["label_plane_anchor"]["utc"] == pytest.approx(WALL0 - 0.0005)


def test_a_higher_tier_label_plane_wins_the_anchor_block(tmp_path):
    """T6's native anchor outranks the T3 registration: one station, one
    registration, and T6 is the tighter ruler."""
    wall, mono = [WALL0], [1000.0]
    t3 = BenchReading(
        tier="T3",
        utc=WALL0 - 0.150,
        sigma_ns=1e6,
        mono=1000.0,
        detail={"bench": "hf_acquired"},
        plane="label",
    )
    t6 = BenchReading(
        tier="T6",
        utc=WALL0 - 0.010,
        sigma_ns=5e4,
        mono=1000.0,
        detail={"anchor_tier": "T6"},
        plane="label",
    )
    judge = _judge(tmp_path, [_FixedBench(t3), _FixedBench(t6)], wall, mono)
    judge.tick()
    block = _published(tmp_path)["label_plane_anchor"]
    assert block["tier"] == "T6"
    assert block["utc"] == pytest.approx(WALL0 - 0.010)


def test_no_label_plane_bench_publishes_no_anchor(tmp_path):
    wall, mono = [WALL0], [1000.0]
    host = BenchReading(tier="T4", utc=WALL0, sigma_ns=25e6, mono=1000.0)
    judge = _judge(tmp_path, [_FixedBench(host)], wall, mono)
    judge.tick()
    assert _published(tmp_path)["label_plane_anchor"] is None


# ── the reader turns it into a chrony sample ─────────────────────────


def _write_anchor(path, **kw):
    block = {
        "bench": "hf_acquired",
        "tier": "T3",
        "plane": "label",
        "utc": WALL0 - 2.0,
        "mono": 998.0,
        "sigma_ns": 1_000_000.0,
        "age_s": 2.0,
    }
    block.update(kw)
    path.write_text(
        json.dumps({"schema": "offset-judge-v1", "label_plane_anchor": block})
    )


def test_the_sample_is_the_host_error_at_the_arrival_instant(tmp_path):
    """The whole sign trace, with no d_clock term to double-count.

    True UTC at the instant fusion asks is WALL0; the host clock reads
    WALL0 + 0.150, so it has walked 150 ms FAST.  The anchor labels the
    sample that arrived 2 s of monotonic ago, and its label is that
    arrival's true UTC: WALL0 − 2.0.  The host clock read
    WALL0 + 0.150 − 2.0 at that same instant.  chrony's own arithmetic is
    offset = clockTimeStamp − receiveTimeStamp = reference − system =
    −150 ms, and a negative offset slews the host BACKWARDS.  Correct.
    """
    path = tmp_path / "offset_judge.json"
    _write_anchor(path)
    sample = label_plane_chrony_sample(
        path,
        time_fn=lambda: WALL0 + 0.150,
        mono_fn=lambda: 1000.0,
    )
    assert sample is not None
    assert sample.bench == "hf_acquired" and sample.tier == "T3"
    # 1 us tolerances: double precision at Unix-epoch magnitude resolves
    # ~200 ns, and chrony's own SHM struct is microsecond-granular anyway.
    assert sample.reference_time == pytest.approx(WALL0 - 2.0, abs=1e-6)
    assert sample.system_time == pytest.approx(WALL0 + 0.150 - 2.0, abs=1e-6)
    assert sample.offset_s == pytest.approx(-0.150, abs=1e-6)
    assert sample.age_s == pytest.approx(2.0)


def test_a_correct_host_clock_yields_a_zero_offset(tmp_path):
    """The regime must be a no-op on a host that is already right —
    otherwise it would be steering, not measuring."""
    path = tmp_path / "offset_judge.json"
    _write_anchor(path)
    sample = label_plane_chrony_sample(
        path, time_fn=lambda: WALL0, mono_fn=lambda: 1000.0
    )
    assert sample.offset_s == pytest.approx(0.0, abs=1e-6)


def test_the_sample_carries_no_d_clock_term(tmp_path):
    """Guard against double counting: the sample is a function of the
    anchor and the host clock ALONE.  Fusion's d_clock cannot appear in
    it because the reader never sees fusion_status.json."""
    path = tmp_path / "offset_judge.json"
    _write_anchor(path)
    first = label_plane_chrony_sample(
        path, time_fn=lambda: WALL0, mono_fn=lambda: 1000.0
    )
    (tmp_path / "fusion_status.json").write_text(
        json.dumps({"fusion": {"d_clock_fused_ms": 999.0}})
    )
    second = label_plane_chrony_sample(
        path, time_fn=lambda: WALL0, mono_fn=lambda: 1000.0
    )
    assert first.offset_s == second.offset_s


@pytest.mark.parametrize("age_s", [30.1, 120.0])
def test_a_stale_anchor_is_refused(tmp_path, age_s):
    path = tmp_path / "offset_judge.json"
    _write_anchor(path, mono=1000.0 - age_s)
    assert (
        label_plane_chrony_sample(path, time_fn=lambda: WALL0, mono_fn=lambda: 1000.0)
        is None
    )


def test_an_arrival_in_the_future_is_refused(tmp_path):
    """A monotonic timestamp ahead of now means the file came from
    another boot (or a clock nobody understands).  Refuse it."""
    path = tmp_path / "offset_judge.json"
    _write_anchor(path, mono=1000.5)
    assert (
        label_plane_chrony_sample(path, time_fn=lambda: WALL0, mono_fn=lambda: 1000.0)
        is None
    )


def test_missing_and_malformed_files_are_refused(tmp_path):
    missing = tmp_path / "nope.json"
    assert read_label_plane_anchor(missing) is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert read_label_plane_anchor(bad) is None
    wrong_schema = tmp_path / "wrong.json"
    wrong_schema.write_text(json.dumps({"schema": "v99", "label_plane_anchor": {}}))
    assert read_label_plane_anchor(wrong_schema) is None
    no_block = tmp_path / "none.json"
    no_block.write_text(
        json.dumps({"schema": "offset-judge-v1", "label_plane_anchor": None})
    )
    assert read_label_plane_anchor(no_block) is None


def test_a_host_plane_block_is_refused(tmp_path):
    """Belt and braces: only a label plane may drive this regime."""
    path = tmp_path / "offset_judge.json"
    _write_anchor(path, plane="host")
    assert read_label_plane_anchor(path) is None


# ── the SHM precision the sample claims ─────────────────────────────


@pytest.mark.parametrize(
    "sigma_ns,expected",
    [
        (1_000_000.0, -9),  # 1 ms -> log2 -9.97, truncated toward zero
        (25_000_000.0, -5),  # 25 ms -> log2 -5.32
        (50.0, -20),  # clamped: never claims better than 1 us
        (500_000_000.0, -4),  # clamped: never claims worse than 62 ms
    ],
)
def test_shm_precision_is_clamped_like_the_legacy_feed(tmp_path, sigma_ns, expected):
    path = tmp_path / "offset_judge.json"
    _write_anchor(path, sigma_ns=sigma_ns)
    sample = label_plane_chrony_sample(
        path, time_fn=lambda: WALL0, mono_fn=lambda: 1000.0
    )
    assert sample.shm_precision == expected


# ── what fusion_status.json says about the regime ────────────────────


def test_fusion_status_publishes_the_regime(tmp_path):
    from hf_timestd.core.fusion_status_writer import FusionStatusWriter

    w = FusionStatusWriter(tmp_path / "fusion_status.json", 8.0)
    w.update(None, chrony_fed=False, skip_reasons=[])
    payload = json.loads((tmp_path / "fusion_status.json").read_text())
    # Default (legacy) regime, so existing consumers see no change.
    assert payload["chrony_gate"]["feed_regime"] == "fusion_d_clock"
    assert payload["chrony_gate"]["anchor_offset_ms"] is None

    _write_anchor(tmp_path / "offset_judge.json")
    sample = label_plane_chrony_sample(
        tmp_path / "offset_judge.json",
        time_fn=lambda: WALL0 + 0.150,
        mono_fn=lambda: 1000.0,
    )
    w.update(None, chrony_fed=True, skip_reasons=[], anchor_sample=sample)
    payload = json.loads((tmp_path / "fusion_status.json").read_text())
    gate = payload["chrony_gate"]
    assert gate["feed_regime"] == "anchor"
    assert gate["anchor_bench"] == "hf_acquired"
    assert gate["anchor_tier"] == "T3"
    assert gate["anchor_offset_ms"] == pytest.approx(-150.0, abs=1e-3)
    assert gate["anchor_age_s"] == pytest.approx(2.0)


def test_the_sample_names_the_arrival_it_came_from(tmp_path):
    """The fusion loop cycles faster than the judge ticks (8 s vs 10 s),
    so the same arrival is seen more than once.  ``anchor_mono`` is the
    measurement's identity: the loop feeds one arrival once, because
    correlated samples offered to chrony as independent ones understate
    the refclock's dispersion."""
    path = tmp_path / "offset_judge.json"
    _write_anchor(path)
    first = label_plane_chrony_sample(
        path, time_fn=lambda: WALL0, mono_fn=lambda: 1000.0
    )
    later = label_plane_chrony_sample(
        path, time_fn=lambda: WALL0 + 8.0, mono_fn=lambda: 1008.0
    )
    assert first.anchor_mono == later.anchor_mono == 998.0
    # Same arrival, so the pair still describes the SAME instant even
    # though it was read 8 s later.
    assert later.reference_time == first.reference_time
    assert later.system_time == pytest.approx(first.system_time, abs=1e-6)
    assert later.age_s == pytest.approx(10.0)
