"""T3 label-plane anchor built from the verified registration (spec §11,
amendment 2026-09-07, revised by mjh the same day).

"Why do we compare things to the host clock?  It is not the ruler or the
standard but a product of FUSION.  Imagine there is no host clock at all,
but ONLY FUSION."  So the anchor is NOT "radiod's host-stamped pair plus a
judge offset": it is the registration itself, expressed exactly as T6
expresses its own anchor -- a NativeAnchor.
"""

import pytest

from hf_timestd.core.native_anchor import NativeAnchor, utc_ns_at_rtp
from hf_timestd.core.registration_acquirer import Registration
from hf_timestd.core.registration_store import (
    ADOPT_MIN_CORROBORATED_MINUTES,
    RegistrationStore,
)
from hf_timestd.core.t3_registration_anchor import T3RegistrationAnchor
from synth_origins import counter_map_for

SR = 24000
WALL0 = 1_800_000_000.0
# Task 6: each channel starts its own RTP counter; tests that fuse several
# channels state a shared origin explicitly (start block 51 for all).
M51 = counter_map_for(51, SR)


def _store(tmp_path, clock):
    return RegistrationStore(
        tmp_path / "reg",
        tmp_path / "registration.json",
        time_fn=lambda: clock[0],
    )


def _write(
    store,
    state,
    *,
    rtp_ref=1000,
    utc_ref=WALL0,
    sample_rate=SR,
    epoch="ep-1",
    sigma_ms=1.0,
    n_minutes=ADOPT_MIN_CORROBORATED_MINUTES,
    extra=None,
):
    """``n_minutes`` defaults to the adoption floor (task 16b), so these
    fixtures still describe a plane every surface may act on."""
    store.write_summary(
        Registration(
            epoch,
            rtp_ref=rtp_ref,
            utc_ref=utc_ref,
            sample_rate=sample_rate,
            sigma_ms=sigma_ms,
            n_minutes=n_minutes,
            channel="fused",
            verified=True,
        ),
        ["SHARED_10000", "WWV_15000"],
        state,
        dict(extra or {"counter_epoch_id": epoch}),
    )


def _anchor(tmp_path, clock, **kw):
    store = _store(tmp_path, clock)
    _write(store, "ACQUIRED", **kw)
    return T3RegistrationAnchor(
        store=store, time_fn=lambda: clock[0], anchor_closure=True, admitted=True
    )


# ── the anchor IS the registration ───────────────────────────────────


def test_acquired_registration_becomes_a_native_anchor(tmp_path):
    clock = [WALL0]
    holder = _anchor(tmp_path, clock)
    decision = holder.refresh(sample_rate=SR, t6_authoritative=False)
    assert decision.reason == "acquired"
    a = decision.anchor
    assert isinstance(a, NativeAnchor)
    assert a.anchor_rtp == 1000
    assert a.anchor_utc_ns == int(round(WALL0 * 1e9))
    assert a.sample_rate_hz == SR
    assert a.chain_delay_ns == 0  # no RF chain term: this IS the plane
    assert a.captured_via_tier == "T3"
    assert a.captured_at_utc_ns == int(round(WALL0 * 1e9))  # written_at, provenance
    assert decision.epoch_id == "ep-1"
    # The anchor projects by pure counter arithmetic, exactly like T6's.
    assert utc_ns_at_rtp(1000 + 10 * SR, a) == int(round((WALL0 + 10.0) * 1e9))


def test_t6_authoritative_wins_and_the_registration_is_ignored(tmp_path):
    """One registration per station: while a T6 native anchor is
    authoritative it owns the plane and T3 must not offer a second one."""
    clock = [WALL0]
    holder = _anchor(tmp_path, clock)
    decision = holder.refresh(sample_rate=SR, t6_authoritative=True)
    assert decision.anchor is None
    assert decision.reason == "t6_authoritative"


@pytest.mark.parametrize("state", ["BOOTSTRAP", "CANDIDATE", "CONFLICT", "WITNESS"])
def test_only_acquired_carries_a_verified_plane(tmp_path, state):
    """CANDIDATE is the acquired-but-UNVERIFIED plane (metrology_service
    publishes ACQUIRED in the summary only once a verified plane
    contributed); WITNESS means a T6 station publishes the plane without
    it driving metrology.  Neither may anchor the ring."""
    clock = [WALL0]
    store = _store(tmp_path, clock)
    _write(store, state)
    holder = T3RegistrationAnchor(
        store=store, time_fn=lambda: clock[0], anchor_closure=True, admitted=True
    )
    decision = holder.refresh(sample_rate=SR, t6_authoritative=False)
    assert decision.anchor is None
    assert decision.reason == f"state:{state}"


def test_missing_summary_is_no_anchor(tmp_path):
    clock = [WALL0]
    store = _store(tmp_path, clock)
    holder = T3RegistrationAnchor(
        store=store, time_fn=lambda: clock[0], anchor_closure=True, admitted=True
    )
    decision = holder.refresh(sample_rate=SR, t6_authoritative=False)
    assert decision.anchor is None and decision.reason == "no_summary"


def test_stale_summary_is_dropped(tmp_path):
    """read_summary returns the last file it finds however old (store
    F1/F2).  A registration whose writer died is not a live plane."""
    clock = [WALL0]
    holder = _anchor(tmp_path, clock)
    assert holder.refresh(sample_rate=SR, t6_authoritative=False).anchor is not None
    clock[0] += 301.0  # store stale_s default is 300 s
    decision = holder.refresh(sample_rate=SR, t6_authoritative=False)
    assert decision.anchor is None and decision.reason == "stale"


def test_sample_rate_mismatch_is_refused(tmp_path):
    """rtp_ref is stamped in ONE counter domain.  A channel running at a
    different configured rate is a different counter entirely
    (cross_channel_rtp.py); silence beats a wrong plane."""
    clock = [WALL0]
    holder = _anchor(tmp_path, clock)
    decision = holder.refresh(sample_rate=96000, t6_authoritative=False)
    assert decision.anchor is None and decision.reason == "sample_rate_mismatch"


def test_incomplete_summary_is_refused(tmp_path):
    clock = [WALL0]
    store = _store(tmp_path, clock)
    store.write_summary(None, [], "ACQUIRED", {})  # utc_ref/rtp_ref null
    holder = T3RegistrationAnchor(
        store=store, time_fn=lambda: clock[0], anchor_closure=True, admitted=True
    )
    decision = holder.refresh(sample_rate=SR, t6_authoritative=False)
    assert decision.anchor is None and decision.reason == "incomplete"


def test_a_moved_registration_yields_a_moved_anchor(tmp_path):
    clock = [WALL0]
    store = _store(tmp_path, clock)
    _write(store, "ACQUIRED")
    holder = T3RegistrationAnchor(
        store=store, time_fn=lambda: clock[0], anchor_closure=True, admitted=True
    )
    first = holder.refresh(sample_rate=SR, t6_authoritative=False).anchor
    clock[0] += 60.0
    _write(store, "ACQUIRED", utc_ref=WALL0 + 0.020)  # plane moved 20 ms
    second = holder.refresh(sample_rate=SR, t6_authoritative=False).anchor
    assert second.anchor_utc_ns - first.anchor_utc_ns == 20_000_000


def test_counter_epoch_change_is_reported(tmp_path):
    clock = [WALL0]
    store = _store(tmp_path, clock)
    _write(store, "ACQUIRED", epoch="ep-1")
    holder = T3RegistrationAnchor(
        store=store, time_fn=lambda: clock[0], anchor_closure=True, admitted=True
    )
    assert holder.refresh(sample_rate=SR, t6_authoritative=False).epoch_id == "ep-1"
    _write(store, "ACQUIRED", epoch="ep-2", rtp_ref=7777)
    d = holder.refresh(sample_rate=SR, t6_authoritative=False)
    assert d.epoch_id == "ep-2" and d.anchor.anchor_rtp == 7777


def test_a_read_failure_never_raises(tmp_path):
    class _Boom:
        stale_s = 300.0

        def read_summary(self):
            raise OSError("boom")

    holder = T3RegistrationAnchor(
        store=_Boom(), time_fn=lambda: WALL0, anchor_closure=True, admitted=True
    )
    decision = holder.refresh(sample_rate=SR, t6_authoritative=False)
    assert decision.anchor is None and decision.reason == "read_failed"


def test_station_wide_refresh_accepts_the_registrations_own_domain(tmp_path):
    """The station-wide refresh names no channel: it accepts whatever
    counter domain the registration was acquired in, and each consumer
    matches its own configured rate before using the anchor."""
    clock = [WALL0]
    store = _store(tmp_path, clock)
    _write(store, "ACQUIRED", sample_rate=96000)
    holder = T3RegistrationAnchor(
        store=store, time_fn=lambda: clock[0], anchor_closure=True, admitted=True
    )
    decision = holder.refresh(t6_authoritative=False)
    assert decision.reason == "acquired"
    assert decision.anchor.sample_rate_hz == 96000
    label = holder.state()
    assert label.anchor is decision.anchor and label.epoch_id == "ep-1"
    assert label.tier == "T3" and label.matches_rate(96000)


# ── task 14c: the summary's `verified` flag is truthful, and gates ────


def test_an_unverified_plane_is_refused_even_in_the_acquired_state(tmp_path):
    """Both conditions, not either.  The STATE is the network-wide view;
    the FLAG is the fused plane's own provenance.  Before task 14c the
    flag was left at its dataclass default, so `false` appeared in every
    station summary ever written and had to be ignored."""
    clock = [WALL0]
    store = _store(tmp_path, clock)
    store.write_summary(
        Registration(
            "ep-1",
            rtp_ref=1000,
            utc_ref=WALL0,
            sample_rate=SR,
            sigma_ms=1.0,
            channel="fused",
            verified=False,
        ),
        ["SHARED_10000"],
        "ACQUIRED",
        {"counter_epoch_id": "ep-1"},
    )
    holder = T3RegistrationAnchor(
        store=store, time_fn=lambda: clock[0], anchor_closure=True, admitted=True
    )
    decision = holder.refresh(t6_authoritative=False)
    assert decision.anchor is None and decision.reason == "unverified"


def test_a_summary_with_no_verified_key_fails_closed(tmp_path):
    """An older metrology process, or a schema-incomplete write.  One
    revalidation tick of legacy behaviour beats anchoring the station on
    a plane whose provenance nobody asserted."""

    class _Old:
        stale_s = 300.0

        def read_summary(self):
            return {
                "state": "ACQUIRED",
                "counter_epoch_id": "ep-1",
                "rtp_ref": 1000,
                "utc_ref": WALL0,
                "sample_rate": SR,
                "sigma_ms": 1.0,
                "written_at": WALL0,
            }

    holder = T3RegistrationAnchor(
        store=_Old(), time_fn=lambda: WALL0, anchor_closure=True, admitted=True
    )
    assert holder.refresh(t6_authoritative=False).reason == "unverified"


def test_fusion_carries_verified_from_every_member_it_keeps():
    """task 14c: `all`, not `any`.  A fused plane inherits the weakest
    provenance among its members -- an unverified member's origin could
    be a fold-lattice phantom, and inverse-variance combination cannot
    detect that."""
    from hf_timestd.core.registration_store import fuse_registrations_with_members

    def reg(channel, verified, utc_ref=WALL0):
        return Registration(
            "ep-1",
            rtp_ref=0,
            utc_ref=utc_ref,
            sample_rate=SR,
            sigma_ms=1.0,
            channel=channel,
            verified=verified,
            epoch_offset_s=0.0,
            counter_map=M51,
        )

    both, kept, _w = fuse_registrations_with_members(
        [reg("a", True), reg("b", True)], 0, M51, "a"
    )
    assert len(kept) == 2 and both.verified is True

    mixed, kept, _w = fuse_registrations_with_members(
        [reg("a", True), reg("b", False)], 0, M51, "a"
    )
    assert len(kept) == 2 and mixed.verified is False

    neither, _k, _w = fuse_registrations_with_members(
        [reg("a", False), reg("b", False)], 0, M51, "a"
    )
    assert neither.verified is False


def test_the_summary_round_trips_a_verified_fused_plane(tmp_path):
    """End to end through the file: fusion sets it, the store writes it,
    the anchor gate reads it."""
    from hf_timestd.core.registration_store import fuse_registrations_with_members

    clock = [WALL0]
    store = _store(tmp_path, clock)
    members = [
        Registration(
            "ep-1",
            rtp_ref=0,
            utc_ref=WALL0,
            sample_rate=SR,
            sigma_ms=1.0,
            n_minutes=ADOPT_MIN_CORROBORATED_MINUTES,
            channel=c,
            verified=True,
            epoch_offset_s=0.0,
            counter_map=M51,
        )
        for c in ("SHARED_10000", "WWV_15000")
    ]
    fused, kept, _w = fuse_registrations_with_members(
        members, 1000, M51, "SHARED_10000"
    )
    assert kept == ["SHARED_10000", "WWV_15000"]
    store.write_summary(fused, kept, "ACQUIRED", {"counter_epoch_id": "ep-1"})
    assert store.read_summary()["verified"] is True
    holder = T3RegistrationAnchor(
        store=store, time_fn=lambda: clock[0], anchor_closure=True, admitted=True
    )
    assert holder.refresh(t6_authoritative=False).reason == "acquired"
