"""Task 6: registrations carry their counter; fusion goes through the
input-sample index.

Since ka9q-radio e5f790dd every radiod channel starts its own RTP counter.
ND measured k0 = 51, 51, 51, 51, 50 on its five 24 kHz channels, so one
channel sits 20 ms off the other four in raw RTP.  Fusion must convert
every member into the caller's counter through both counter maps, and must
drop any member it cannot convert.
"""
from __future__ import annotations

import pytest

from hf_timestd.core.registration_acquirer import Registration
from hf_timestd.core.registration_store import (
    RegistrationStore,
    fuse_registrations,
    fuse_registrations_with_members,
)
from synth_origins import (
    EPOCH_GPS_S,
    F_IN,
    ILEN,
    ND_K0,
    counter_map_for,
    olen_for,
    rtp_at,
)

SR = 24_000
OLEN = olen_for(SR)
REF_BLOCK = 100
ND_CHANNELS = ("SHARED_5000", "SHARED_10000", "SHARED_15000", "WWV_2500", "WWV_20000")
UTC0 = 1_790_000_000.0  # UTC of input sample 0, synthetic


def truth(block: int) -> float:
    """UTC of the start of input block `block`."""
    return UTC0 + block * ILEN / F_IN


def _plane(channel: str, k0: int, *, cmap="auto", domain="auto", utc_err_s=0.0,
           epoch_gps_s: float = EPOCH_GPS_S) -> Registration:
    """A plane exactly at truth (plus `utc_err_s`) in `channel`'s own counter."""
    if cmap == "auto":
        cmap = counter_map_for(k0, SR, epoch_gps_s=epoch_gps_s)
    return Registration(
        counter_epoch_id="ep-1",
        rtp_ref=rtp_at(REF_BLOCK, k0, OLEN),
        utc_ref=truth(REF_BLOCK) + utc_err_s,
        sample_rate=SR,
        sigma_ms=1.0,
        channel=channel,
        verified=True,
        counter_map=cmap,
        rtp_domain=channel if domain == "auto" else domain,
    )


def test_nd_siblings_fuse_to_truth_in_the_callers_counter():
    # five channels, k0 = ND_K0, each with a plane exactly at truth in its own
    # counter; fuse in the counter of the odd one out (WWV_20000, k0 = 50)
    regs = [_plane(ch, k0) for ch, k0 in zip(ND_CHANNELS, ND_K0)]
    m50 = counter_map_for(50, SR)
    at = rtp_at(20_000, 50, OLEN)
    fused, kept, _ = fuse_registrations_with_members(
        regs, at_rtp=at, at_map=m50, domain="WWV_20000"
    )
    assert fused.sample0_utc_for(at) == pytest.approx(truth(20_000), abs=1e-6)
    assert fused.rtp_domain == "WWV_20000" and fused.counter_map == m50
    assert sorted(kept) == sorted(ND_CHANNELS)
    # today: the four k0=51 planes project 20 ms off in this counter, out-vote
    # the correct own plane at the median, and the fused plane sits 20 ms
    # from truth


def test_member_without_a_map_never_combines_across_channels():
    own = _plane("SHARED_10000", 51)
    # the sibling sits at truth in its own counter, but names no map: nothing
    # can say where its counter starts relative to the caller's
    sib = _plane("WWV_20000", 50, cmap=None)
    m51 = counter_map_for(51, SR)
    _, kept, _ = fuse_registrations_with_members(
        [own, sib], at_rtp=rtp_at(200, 51, OLEN), at_map=m51, domain="SHARED_10000"
    )
    assert kept == ["SHARED_10000"]


def test_own_channel_without_a_map_still_projects_in_its_own_counter():
    own_no_map = _plane("SHARED_10000", 51, cmap=None)
    r = rtp_at(200, 51, OLEN)
    fused = fuse_registrations([own_no_map], at_rtp=r, domain="SHARED_10000")
    assert fused is not None
    assert fused.sample0_utc_for(r) == pytest.approx(truth(200), abs=1e-6)


def test_member_from_another_radiod_run_is_not_fused():
    own = _plane("SHARED_10000", 51)
    # sibling map with input_epoch_gps_s 3600 s away: an earlier radiod run.
    # Its input indices read the same, so only the run check can refuse it.
    sib = _plane("WWV_20000", 50, epoch_gps_s=EPOCH_GPS_S + 3600.0, utc_err_s=0.0005)
    m51 = counter_map_for(51, SR)
    _, kept, _ = fuse_registrations_with_members(
        [own, sib], at_rtp=rtp_at(200, 51, OLEN), at_map=m51, domain="SHARED_10000"
    )
    assert kept == ["SHARED_10000"]


def test_store_round_trips_the_counter_map_and_domain(tmp_path):
    st = RegistrationStore(tmp_path / "reg", tmp_path / "registration.json")
    m50 = counter_map_for(50, SR)
    st.write_channel(_plane("WWV_20000", 50), "ACQUIRED", {})
    [sib] = st.read_siblings(exclude_channel="SHARED_10000")
    assert sib.counter_map == m50 and sib.rtp_domain == "WWV_20000"


def test_witness_files_are_siblings_too(tmp_path):
    # a T6 station publishes its verified T3 plane as WITNESS; Task 10's
    # shadow record reads those planes as siblings
    st = RegistrationStore(tmp_path / "reg", tmp_path / "registration.json")
    st.write_channel(_plane("SHARED_5000", 51), "WITNESS", {})
    st.write_channel(_plane("WWV_2500", 51), "CANDIDATE", {})
    st.write_channel(_plane("X", 51), "ACQUIRED", {})
    sibs = st.read_siblings("X", states=("ACQUIRED", "WITNESS"))
    assert [r.channel for r in sibs] == ["SHARED_5000"]


def test_read_siblings_default_excludes_witness(tmp_path):
    # fix round 1: live T6 behaviour stays as it was until Task 10 asks
    # for WITNESS; a default that took it would hand every channel on a
    # T6 station siblings it never had
    st = RegistrationStore(tmp_path / "reg", tmp_path / "registration.json")
    st.write_channel(_plane("SHARED_5000", 51), "WITNESS", {})
    st.write_channel(_plane("WWV_2500", 51), "ACQUIRED", {})
    assert [r.channel for r in st.read_siblings("X")] == ["WWV_2500"]


def test_fusion_without_a_named_counter_raises():
    # fix round 1: with no domain and no map, a default once took the first
    # member's counter, so a siblings-only call read one sibling's plane at
    # the caller's raw RTP.  Fail safe instead.
    regs = [_plane("SHARED_10000", 51, cmap=None), _plane("WWV_20000", 50, cmap=None)]
    with pytest.raises(ValueError):
        fuse_registrations(regs, at_rtp=rtp_at(200, 51, OLEN), domain="")
