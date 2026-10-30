import json
import tempfile
from pathlib import Path

import pytest

from hf_timestd.core.registration_acquirer import Registration
from hf_timestd.core.registration_store import (
    RegistrationStore,
    fuse_registrations,
    fuse_registrations_with_members,
)

from synth_origins import EPOCH_GPS_S, counter_map_for

SR = 24000
# Task 6: every channel starts its own RTP counter (ka9q-radio e5f790dd), so a
# test that fuses several channels must say how their counters relate.  Each
# member here gets the map of a channel that started at block 51: one shared
# origin, stated explicitly instead of assumed.
M51 = counter_map_for(51, SR)


def _run_map(offset_s):
    """A k0 = 51 map from a radiod run that started `offset_s` later."""
    return counter_map_for(51, SR, epoch_gps_s=EPOCH_GPS_S + offset_s)


def _reg(ch, utc_ref, sigma, epoch="ep-1", rtp_ref=1000, epoch_offset_s=float("nan"),
         cmap=M51):
    return Registration(
        counter_epoch_id=epoch,
        rtp_ref=rtp_ref,
        utc_ref=utc_ref,
        sample_rate=SR,
        sigma_ms=sigma,
        channel=ch,
        epoch_offset_s=epoch_offset_s,
        counter_map=cmap,
    )


def test_fuse_inverse_variance_and_outlier():
    regs = [_reg("a", 100.000, 1.0), _reg("b", 100.0005, 0.5), _reg("c", 100.0100, 0.5)]
    f = fuse_registrations(regs, at_rtp=1000, at_map=M51, domain="a")
    # c is 10 ms off the median -> rejected; a,b weighted 1:4
    assert f.utc_ref == pytest.approx(100.0004, abs=2e-5)
    assert f.sigma_ms == pytest.approx(1 / (1 + 4) ** 0.5, abs=1e-3)
    assert f.rtp_ref == 1000 and f.channel == "fused"


def test_fuse_unions_stations_and_roundtrips_them(tmp_path):
    regs = [
        Registration(
            "ep-1", 1000, 100.0, SR, 1.0, channel="a", stations=("WWV",), counter_map=M51
        ),
        Registration(
            "ep-1", 1000, 100.0, SR, 1.0, channel="b", stations=("WWVH", "WWV"),
            counter_map=M51,
        ),
    ]
    assert fuse_registrations(regs, 1000, M51, "a").stations == ("WWV", "WWVH")
    st = RegistrationStore(tmp_path / "reg", tmp_path / "registration.json")
    st.write_channel(regs[1], "ACQUIRED", {})
    assert st.read_siblings()[0].stations == ("WWVH", "WWV")


def test_fuse_rebases_to_common_rtp():
    regs = [_reg("a", 100.0, 1.0, rtp_ref=0), _reg("a2", 101.0, 1.0, rtp_ref=SR)]
    f = fuse_registrations(regs, at_rtp=2 * SR, at_map=M51, domain="a")
    assert f.utc_ref == pytest.approx(102.0)
    assert f.sigma_ms == pytest.approx(1.0 / 2**0.5, abs=1e-6)  # a2 took part


def test_fuse_callers_run_wins_and_empty_is_none():
    """Was ``majority_epoch_wins``: a plane from another radiod run (here
    ep-1, an earlier run) cannot convert into the caller's counter and
    drops, however its RTP happens to read."""
    regs = [
        _reg("c", 5.0, 1.0, "ep-1", cmap=_run_map(-7200.0)),
        _reg("a", 100.0, 1.0, "ep-2"),
        _reg("b", 100.0, 1.0, "ep-2"),
    ]
    assert fuse_registrations(regs, 1000, M51, "b").counter_epoch_id == "ep-2"
    assert fuse_registrations([], 1000) is None


def test_store_roundtrip_and_staleness(tmp_path):
    clock = [1000.0]
    st = RegistrationStore(
        tmp_path / "reg",
        tmp_path / "registration.json",
        stale_s=300.0,
        time_fn=lambda: clock[0],
    )
    st.write_channel(
        _reg("SHARED_10000", 100.0, 1.0), "ACQUIRED", {"correction_ms": -250.0}
    )
    st.write_channel(_reg("WWV_20000", 100.001, 2.0), "ACQUIRED", {})
    sibs = st.read_siblings(exclude_channel="SHARED_10000")
    assert [r.channel for r in sibs] == ["WWV_20000"]
    clock[0] += 301
    assert st.read_siblings() == []
    data = json.loads((tmp_path / "reg" / "SHARED_10000.json").read_text())
    assert data["correction_ms"] == -250.0 and data["state"] == "ACQUIRED"


def test_summary(tmp_path):
    st = RegistrationStore(tmp_path / "reg", tmp_path / "registration.json")
    st.write_summary(
        _reg("fused", 100.0, 0.7),
        ["SHARED_10000", "WWV_20000"],
        "ACQUIRED",
        {"raw_pair_residual_ms": 16.7},
    )
    s = st.read_summary()
    assert s["state"] == "ACQUIRED" and s["contributing"] == [
        "SHARED_10000",
        "WWV_20000",
    ]
    assert s["raw_pair_residual_ms"] == 16.7 and s["sigma_ms"] == 0.7


def test_bootstrap_summary_has_no_plane(tmp_path):
    st = RegistrationStore(tmp_path / "reg", tmp_path / "registration.json")
    st.write_summary(None, [], "BOOTSTRAP", {})
    s = st.read_summary()
    assert s["state"] == "BOOTSTRAP" and s["utc_ref"] is None


def test_bootstrap_channel_file_has_null_sigma_and_is_not_a_sibling(tmp_path):
    st = RegistrationStore(tmp_path / "reg", tmp_path / "registration.json")
    st.write_channel(_reg("WWV_25000", 0.0, float("inf")), "BOOTSTRAP", {})
    text = (tmp_path / "reg" / "WWV_25000.json").read_text()
    assert "Infinity" not in text and json.loads(text)["sigma_ms"] is None
    assert st.read_siblings() == []


def test_candidate_state_file_is_not_a_sibling(tmp_path):
    """task-11b: RegistrationStore.read_siblings already filters on
    ``state == "ACQUIRED"`` -- confirm a channel file written with the new
    CANDIDATE state (an own plane not yet verified by the tick detector)
    is not picked up as a sibling."""
    st = RegistrationStore(tmp_path / "reg", tmp_path / "registration.json")
    st.write_channel(_reg("WWV_25000", 100.0, 3.116), "CANDIDATE", {})
    text = (tmp_path / "reg" / "WWV_25000.json").read_text()
    assert json.loads(text)["state"] == "CANDIDATE"
    assert st.read_siblings() == []


def test_adopted_registration_is_not_a_sibling(tmp_path):
    """C1: a channel that merely ADOPTED a sibling's (or fusion's) plane
    must never re-enter another channel's fusion as if it were independent
    evidence -- otherwise the same measurement gets inverse-variance
    combined with itself and sigma is understated by sqrt(n_adopters+1)."""
    st = RegistrationStore(tmp_path / "reg", tmp_path / "registration.json")
    reg = Registration(
        "ep-1", 1000, 100.0, SR, 0.9, method="adopted", channel="SHARED_10000"
    )
    st.write_channel(reg, "ACQUIRED", {})
    assert st.read_siblings() == []
    assert st.read_siblings(exclude_channel="WWV_20000") == []


def test_read_siblings_skips_a_schema_incomplete_file(tmp_path):
    clock = [5000.0]
    st = RegistrationStore(
        tmp_path / "reg",
        tmp_path / "registration.json",
        time_fn=lambda: clock[0],
    )
    # Write a valid ACQUIRED file for channel "a"
    st.write_channel(_reg("a", 100.0, 1.0), "ACQUIRED", {})
    # Write an incomplete JSON file for channel "b" (missing rtp_ref)
    # Use written_at=5000.0 (same as current clock) so it's not stale
    (tmp_path / "reg").mkdir(parents=True, exist_ok=True)
    (tmp_path / "reg" / "b.json").write_text(
        json.dumps(
            {
                "channel": "b",
                "state": "ACQUIRED",
                "utc_ref": 1.0,
                "sigma_ms": 1.0,
                "written_at": 5000.0,
            }
        )
    )
    # read_siblings must return exactly the "a" registration
    # (b is skipped by the KeyError guard since it's missing rtp_ref)
    sibs = st.read_siblings()
    assert len(sibs) == 1
    assert sibs[0].channel == "a"


def test_write_failure_is_counted(tmp_path):
    # Create a file at the directory path so mkdir cannot create it
    bad_dir = tmp_path / "bad_file.txt"
    bad_dir.write_text("not a directory")
    st = RegistrationStore(bad_dir / "reg", tmp_path / "registration.json")
    st.write_channel(_reg("test", 100.0, 1.0), "ACQUIRED", {})
    assert st.write_failures == 1


# ── C1, then Task 6: one run by the counter maps, not by epoch id string ─


def test_fuse_clusters_by_offset_across_differently_named_epochs():
    """C1 (final review): metrology runs one process per channel, each with
    its own CounterEpochTracker sampling the ring anchor at its own phase,
    so two channels in ONE physical counter epoch carry two different
    ``counter_epoch_id`` strings.  The old majority-epoch filter saw every
    count tied at 1, ``max`` returned the first key in insertion order, and
    every other channel was discarded -- inverse-variance combination across
    channels (spec §4.6) silently degraded to "pick one channel".  Cluster by
    the implied offset instead: two 1.0 ms planes must fuse to 0.707 ms."""
    off = 1_000_000_000.0
    regs = [
        _reg("a", 100.0, 1.0, epoch="ep-A", epoch_offset_s=off),
        _reg("b", 100.0, 1.0, epoch="ep-B", epoch_offset_s=off + 0.0019),
    ]
    f = fuse_registrations(regs, at_rtp=1000, at_map=M51, domain="a")
    assert f.sigma_ms == pytest.approx(1.0 / 2**0.5, abs=1e-6)
    assert f.utc_ref == pytest.approx(100.0)


def test_fuse_rejects_a_channel_from_a_different_counter_space():
    """The other half of C1: a counter map from a radiod run hours away is a
    different RTP counter space and must NOT be fused in, however its id
    string happens to be spelled."""
    off = 1_000_000_000.0
    regs = [
        _reg("a", 100.0, 1.0, epoch="ep-A", epoch_offset_s=off),
        _reg("b", 100.0, 1.0, epoch="ep-A", epoch_offset_s=off + 0.0019),
        _reg("c", 100.0, 0.2, epoch="ep-A", epoch_offset_s=off + 69732.0,
             cmap=_run_map(69732.0)),
    ]
    fused, kept, _waiting = fuse_registrations_with_members(
        regs, at_rtp=1000, at_map=M51, domain="a"
    )
    assert [r for r in kept] == ["a", "b"]
    assert fused.sigma_ms == pytest.approx(1.0 / 2**0.5, abs=1e-6)


def test_fuse_keeps_the_callers_run_not_insertion_order():
    """Was ``offset_tie_goes_to_the_cluster_with_the_smallest_sigma``.  Two
    planes from two radiod runs: fusion keeps the one in the CALLER's run,
    whichever landed first in insertion order (the defect the string-id
    ``max`` had) and whichever is tighter -- the other run's counter cannot
    be converted into the caller's at all."""
    off = 1_000_000_000.0
    run_b = _run_map(4000.0)
    regs = [
        _reg("a", 100.0, 1.0, epoch_offset_s=off),
        _reg("b", 200.0, 0.25, epoch_offset_s=off + 4000.0, cmap=run_b),
    ]
    fused, kept, _waiting = fuse_registrations_with_members(
        regs, at_rtp=1000, at_map=run_b, domain="b"
    )
    assert kept == ["b"] and fused.utc_ref == pytest.approx(200.0)
    fused_a, kept_a, _ = fuse_registrations_with_members(
        regs, at_rtp=1000, at_map=M51, domain="a"
    )
    assert kept_a == ["a"] and fused_a.utc_ref == pytest.approx(100.0)


def test_fuse_needs_no_epoch_offset_when_the_maps_name_the_run():
    """Was ``falls_back_to_the_id_string_when_offsets_are_unknown``.  A
    registration with an unknown ``epoch_offset_s`` (NaN) used to group by
    its id string.  Since Task 6 the counter maps name the run, so the
    offset no longer matters: c, from another run, drops."""
    regs = [
        _reg("a", 100.0, 1.0, "ep-2"),
        _reg("b", 100.0, 1.0, "ep-2"),
        _reg("c", 5.0, 1.0, "ep-1", cmap=_run_map(3600.0)),
    ]
    fused, kept, _waiting = fuse_registrations_with_members(
        regs, at_rtp=1000, at_map=M51, domain="a"
    )
    assert fused.counter_epoch_id == "ep-2" and kept == ["a", "b"]


def test_fuse_members_are_what_fusion_actually_kept():
    """C1's third consequence: the summary's ``contributing`` claimed N
    contributors for a one-channel answer.  ``fuse_registrations_with_members``
    returns the channels the outlier test kept, so the caller can publish
    the truth."""
    off = 1_000_000_000.0
    regs = [
        _reg("a", 100.000, 1.0, epoch_offset_s=off),
        _reg("b", 100.0005, 0.5, epoch_offset_s=off),
        _reg("c", 100.0100, 0.5, epoch_offset_s=off),
    ]
    fused, kept, _waiting = fuse_registrations_with_members(
        regs, at_rtp=1000, at_map=M51, domain="a"
    )
    assert kept == ["a", "b"]  # c is 10 ms off the median
    assert fused.epoch_offset_s == pytest.approx(off)


def test_store_round_trips_the_epoch_offset(tmp_path):
    st = RegistrationStore(tmp_path / "reg", tmp_path / "registration.json")
    st.write_channel(
        _reg("WWV_20000", 100.0, 1.0, epoch_offset_s=1_234.5), "ACQUIRED", {}
    )
    data = json.loads((tmp_path / "reg" / "WWV_20000.json").read_text())
    assert data["epoch_offset_s"] == pytest.approx(1_234.5)
    assert st.read_siblings()[0].epoch_offset_s == pytest.approx(1_234.5)


def test_store_writes_null_for_an_unknown_epoch_offset(tmp_path):
    """strict JSON: NaN is not a JSON number.  It must round-trip as null
    -> NaN, and a null must not make the file unreadable as a sibling."""
    st = RegistrationStore(tmp_path / "reg", tmp_path / "registration.json")
    st.write_channel(_reg("WWV_20000", 100.0, 1.0), "ACQUIRED", {})
    text = (tmp_path / "reg" / "WWV_20000.json").read_text()
    assert "NaN" not in text and json.loads(text)["epoch_offset_s"] is None
    import math

    assert math.isnan(st.read_siblings()[0].epoch_offset_s)


# ── Task 16b: the fused plane's corroboration is its WEAKEST member's ──


OFF = 1_000_000_000.0


def _member(ch, utc_ref, n_minutes, sigma=1.0):
    return Registration(
        counter_epoch_id="ep-1",
        rtp_ref=1000,
        utc_ref=utc_ref,
        sample_rate=SR,
        sigma_ms=sigma,
        n_minutes=n_minutes,
        channel=ch,
        verified=True,
        epoch_offset_s=OFF,
        counter_map=M51,
    )


def test_fused_n_minutes_is_the_minimum_over_the_kept_members():
    """A fused plane inherits the weakest provenance among the members it
    kept -- ``verified`` already works that way (task-14c), and
    ``n_minutes`` must too.  Taking the MAXIMUM let one channel's long
    history vouch for a sibling with a shorter one."""
    regs = [_member("a", 100.0, 17), _member("b", 100.0005, 5)]
    fused, kept, waiting = fuse_registrations_with_members(regs, 1000, M51, "a")
    assert kept == ["a", "b"] and waiting == []
    assert fused.n_minutes == 5
    # an outlier fusion DROPS cannot hold the number down
    regs.append(_member("c", 100.010, 3))  # 10 ms off the median
    fused2, kept2, _w2 = fuse_registrations_with_members(regs, 1000, M51, "a")
    assert kept2 == ["a", "b"] and fused2.n_minutes == 5


# ── Task 16c: an uncorroborated newcomer waits OUTSIDE the plane ──────


def test_an_uncorroborated_newcomer_waits_outside_the_fused_plane():
    """ND re-acquires a channel several times an hour.  Taking the
    minimum over every kept member would drop the whole station's fused
    ``n_minutes`` to 0 each time and stand every surface down for two
    minutes.  So when anyone clears
    ``ADOPT_MIN_CORROBORATED_MINUTES``, fusion combines only those: the
    newcomer serves its two minutes outside the plane and joins when it
    qualifies."""
    from hf_timestd.core.registration_store import (
        ADOPT_MIN_CORROBORATED_MINUTES,
        registration_is_authoritative,
    )

    five = [
        _member("a", 100.0, 2),
        _member("b", 100.0, 4),
        _member("c", 100.0, 9),
        _member("d", 100.0, 17),
        _member("e", 100.0, 30),
    ]
    # the newcomer sits 2 ms away -- inside FUSE_OUTLIER_MS, so only the
    # corroboration floor keeps it out, and its absence is measurable
    newcomer = _member("f", 100.002, 0)
    fused, kept, waiting = fuse_registrations_with_members(
        five + [newcomer], 1000, M51, "a"
    )
    assert kept == ["a", "b", "c", "d", "e"]
    assert waiting == ["f"]
    assert fused.utc_ref == pytest.approx(100.0, abs=1e-9)
    assert fused.n_minutes == ADOPT_MIN_CORROBORATED_MINUTES  # min of the five

    st = RegistrationStore(
        Path(tempfile.mkdtemp()) / "reg", Path(tempfile.mkdtemp()) / "registration.json"
    )
    st.write_summary(fused, kept, "ACQUIRED", {"waiting": waiting})
    import time as _time

    assert registration_is_authoritative(
        st.read_summary(), now=_time.time(), sample_rate=SR
    )

    # two minutes later the newcomer qualifies and joins
    joined, kept2, waiting2 = fuse_registrations_with_members(
        five + [_member("f", 100.002, ADOPT_MIN_CORROBORATED_MINUTES)], 1000, M51, "a"
    )
    assert kept2 == ["a", "b", "c", "d", "e", "f"] and waiting2 == []
    assert joined.utc_ref > fused.utc_ref  # its plane now moves the answer
    assert joined.n_minutes == ADOPT_MIN_CORROBORATED_MINUTES


def test_when_nobody_clears_the_floor_every_member_still_fuses():
    """Bootstrap: no plane has corroborated anything yet.  Fusion keeps
    combining them all -- the station-wide gate refuses the summary on
    ``n_minutes`` anyway, so the fused plane stays a candidate rather
    than vanishing."""
    regs = [_member("a", 100.0, 0), _member("b", 100.0005, 1)]
    fused, kept, waiting = fuse_registrations_with_members(regs, 1000, M51, "a")
    assert kept == ["a", "b"] and waiting == []
    assert fused.n_minutes == 0
