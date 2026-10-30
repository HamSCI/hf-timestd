"""The T3 registration anchors the RING (spec §11, 2026-09-07).

The ring is what metrology, authority.json §18 and every subscriber
resolve their UTC from.  Before this it carried radiod's host-stamped
pair plus the judge's correction; with a verified registration in force
it carries the registration itself, and ring-resolved UTC(rtp) is the
registration's own UTC(rtp) to the nanosecond.

The sign trace lives in ``test_ring_resolves_the_registration_plane``:
it goes through ``resolve_buffer_timing`` — the SAME resolver metrology
uses on a real ring — so a sign error cannot hide behind a test-local
re-derivation.
"""

import types

import pytest

from hf_timestd.core import buffer_timing as btm
from hf_timestd.core.native_anchor import LabelAnchor, NativeAnchor, utc_ns_at_rtp
from hf_timestd.core.offset_judge import OffsetVerdict
from hf_timestd.core.stream_recorder_v2 import StreamRecorderV2

SR = 24000
WALL0 = 1_800_000_000.0
KEY = ("hf-status.local", 0xABCD1234)


class FakeRing:
    """Captures update_anchor calls."""

    def __init__(self):
        self.anchors = []

    def update_anchor(self, gps_time_ns, rtp_timesnap):
        self.anchors.append((int(gps_time_ns), int(rtp_timesnap)))


class StubJudge:
    def __init__(self, verdict=None):
        self.verdict = verdict

    def offset_for(self, key, rtp):
        return self.verdict


def make_recorder(*, judge=None, ring=None, sample_rate=SR, provider=None):
    """__new__-bypassed StreamRecorderV2 with just the ring-anchor state
    (the established unit-test pattern in this suite)."""
    rec = StreamRecorderV2.__new__(StreamRecorderV2)
    rec.config = types.SimpleNamespace(
        description="TEST", sample_rate=sample_rate, ssrc=KEY[1]
    )
    rec.channel_info = None
    rec.archive_writer = None
    rec._offset_judge = judge
    rec._judge_source_key = KEY
    rec.ring_buffer = ring
    rec._ring_anchor_state = None
    rec._ring_label_anchor = None
    rec._label_anchor_provider = provider
    rec._control = None
    return rec


def t3_label(
    utc_ref=WALL0, rtp_ref=1000, sample_rate=SR, epoch="ep-1", tier="T3", sigma_ns=1e6
):
    """The provider's shape: a LabelAnchor (task 14)."""
    return LabelAnchor(
        anchor=t3_anchor(utc_ref, rtp_ref, sample_rate),
        epoch_id=epoch,
        tier=tier,
        sigma_ns=sigma_ns,
    )


def t3_anchor(utc_ref=WALL0, rtp_ref=1000, sample_rate=SR):
    return NativeAnchor(
        anchor_rtp=rtp_ref,
        anchor_utc_ns=int(round(utc_ref * 1e9)),
        sample_rate_hz=sample_rate,
        chain_delay_ns=0,
        captured_at_utc_ns=int(round(utc_ref * 1e9)),
        captured_via_tier="T3",
    )


def ring_utc(entry, rtp, sample_rate=SR):
    gps_ns, snap = entry
    return btm.resolve_buffer_timing(
        {"start_rtp_timestamp": rtp, "gps_time_ns": gps_ns, "rtp_timesnap": snap},
        sample_rate=sample_rate,
    ).sample0_utc


# ── the sign trace ───────────────────────────────────────────────────


def test_ring_resolves_the_registration_plane(caplog):
    """Ring-resolved UTC == the registration's UTC at the same RTP.

    radiod's pair is deliberately 150 ms WRONG here (the AC0G-ND
    2026-09-07 signature: the host clock, which stamps GPS_TIME, had
    walked 150 ms from four NTP witnesses).  A registration in force must
    make that pair irrelevant to the ring — no judge offset, no host
    comparison, no residual 150 ms.
    """
    label = t3_label()
    anchor = label.anchor
    ring = FakeRing()
    rec = make_recorder(
        judge=StubJudge(OffsetVerdict(-150e6, 1e6, "T3", 1.0, 1, False)),
        ring=ring,
        provider=lambda: label,
    )
    # radiod's own pair, host-stamped and 150 ms fast.
    raw_gps_ns = btm.unix_ns_to_gps_time_ns(int(round((WALL0 + 0.150) * 1e9)))
    rec._update_ring_anchor(raw_gps_ns, 1000)

    assert len(ring.anchors) == 1
    for rtp in (1000, 1000 + 10 * SR, 1000 - 3 * SR, 1000 + 3600 * SR):
        want = utc_ns_at_rtp(rtp & 0xFFFFFFFF, anchor) / 1e9
        assert ring_utc(ring.anchors[-1], rtp) == pytest.approx(want, abs=1e-6)
    # And specifically NOT the host-stamped plane the judge was correcting.
    assert ring_utc(ring.anchors[-1], 1000) == pytest.approx(WALL0, abs=1e-6)


def test_the_ring_anchor_is_the_registrations_own_pair():
    """No arithmetic beyond the GPS-epoch change of variable: the stored
    rtp_timesnap IS rtp_ref and the stored gps_time_ns IS utc_ref."""
    label = t3_label(utc_ref=WALL0 + 0.4321, rtp_ref=7_777_777)
    anchor = label.anchor
    ring = FakeRing()
    rec = make_recorder(ring=ring, provider=lambda: label)
    rec._update_ring_anchor(12345, 999)
    gps_ns, snap = ring.anchors[-1]
    assert snap == 7_777_777
    assert gps_ns == btm.unix_ns_to_gps_time_ns(anchor.anchor_utc_ns)


# ── the gates ────────────────────────────────────────────────────────


def test_without_a_registration_the_judge_path_is_untouched():
    """No provider (or a silent one) ⇒ byte-identical pre-amendment
    behaviour: radiod's pair with the judge's correction folded in."""
    ring = FakeRing()
    raw_gps_ns = 1_400_000_000_000_000_000
    rec = make_recorder(
        judge=StubJudge(OffsetVerdict(50e6, 1e6, "T4", 1.0, 1, False)),
        ring=ring,
        provider=lambda: None,
    )
    rec._update_ring_anchor(raw_gps_ns, 5000)
    assert ring.anchors == [(raw_gps_ns + int(50e6), 5000)]

    ring2 = FakeRing()
    rec2 = make_recorder(
        judge=StubJudge(OffsetVerdict(50e6, 1e6, "T4", 1.0, 1, False)),
        ring=ring2,
        provider=None,
    )
    rec2._update_ring_anchor(raw_gps_ns, 5000)
    assert ring2.anchors == [(raw_gps_ns + int(50e6), 5000)]


def test_a_foreign_counter_domain_is_refused():
    """The registration's rtp_ref is stamped in ONE counter domain.  A
    96 kHz channel must not be anchored from a 24 kHz registration."""
    ring = FakeRing()
    raw_gps_ns = 1_400_000_000_000_000_000
    rec = make_recorder(
        judge=StubJudge(OffsetVerdict(0.0, 1e6, "T4", 1.0, 1, False)),
        ring=ring,
        sample_rate=96000,
        provider=lambda: t3_label(),
    )
    rec._update_ring_anchor(raw_gps_ns, 5000)
    assert ring.anchors == [(raw_gps_ns, 5000)]


def test_a_broken_provider_never_disturbs_the_ring():
    def boom():
        raise RuntimeError("nope")

    ring = FakeRing()
    raw_gps_ns = 1_400_000_000_000_000_000
    rec = make_recorder(
        judge=StubJudge(OffsetVerdict(0.0, 1e6, "T4", 1.0, 1, False)),
        ring=ring,
        provider=boom,
    )
    rec._update_ring_anchor(raw_gps_ns, 5000)
    assert ring.anchors == [(raw_gps_ns, 5000)]


# ── re-derivation ────────────────────────────────────────────────────


def test_plane_moves_inside_hysteresis_leave_the_ring_alone():
    state = {"label": t3_label()}
    ring = FakeRing()
    rec = make_recorder(ring=ring, provider=lambda: state["label"])
    rec._update_ring_anchor(1_400_000_000_000_000_000, 1000)
    assert len(ring.anchors) == 1
    # 2 ms — inside RING_REANCHOR_MIN_DELTA_NS.
    state["label"] = t3_label(utc_ref=WALL0 + 0.002)
    rec._reanchor_ring_if_offset_drifted()
    assert len(ring.anchors) == 1


def test_a_moved_plane_re_registers_the_ring():
    state = {"label": t3_label()}
    ring = FakeRing()
    rec = make_recorder(ring=ring, provider=lambda: state["label"])
    rec._update_ring_anchor(1_400_000_000_000_000_000, 1000)
    state["label"] = t3_label(utc_ref=WALL0 + 0.050)
    rec._reanchor_ring_if_offset_drifted()
    assert len(ring.anchors) == 2
    assert ring_utc(ring.anchors[-1], 1000) == pytest.approx(WALL0 + 0.050, abs=1e-6)


def test_a_new_counter_epoch_re_registers_even_at_the_same_plane():
    """A counter-epoch change invalidates rtp_ref outright: the plane
    comparison would be arithmetic across two different counters, so the
    epoch alone forces the re-registration."""
    state = {"label": t3_label()}
    ring = FakeRing()
    rec = make_recorder(ring=ring, provider=lambda: state["label"])
    rec._update_ring_anchor(1_400_000_000_000_000_000, 1000)
    # same utc_ref, new counter epoch
    state["label"] = t3_label(rtp_ref=555_000, epoch="ep-2")
    rec._reanchor_ring_if_offset_drifted()
    assert len(ring.anchors) == 2
    assert ring.anchors[-1][1] == 555_000


def test_a_withdrawn_registration_falls_back_to_the_judged_pair():
    """BOOTSTRAP / CONFLICT / stale / T6-authoritative: the provider goes
    silent and the ring must return to radiod's pair with the judge's
    correction rather than freeze on a plane nobody is maintaining."""
    state = {"live": True}
    label = t3_label()
    ring = FakeRing()
    raw_gps_ns = btm.unix_ns_to_gps_time_ns(int(round((WALL0 + 0.150) * 1e9)))
    rec = make_recorder(
        judge=StubJudge(OffsetVerdict(-150e6, 1e6, "T3", 1.0, 1, False)),
        ring=ring,
        provider=lambda: (label if state["live"] else None),
    )
    rec._update_ring_anchor(raw_gps_ns, 1000)
    assert len(ring.anchors) == 1
    state["live"] = False
    rec._reanchor_ring_if_offset_drifted()
    assert len(ring.anchors) == 2
    assert ring.anchors[-1] == (raw_gps_ns + int(-150e6), 1000)


# ── the GPS-epoch change of variable ─────────────────────────────────


@pytest.mark.parametrize("unix_s", [WALL0, 1_400_000_000.0, 1_600_000_000.5])
def test_unix_to_gps_round_trips(unix_s):
    unix_ns = int(round(unix_s * 1e9))
    gps_ns = btm.unix_ns_to_gps_time_ns(unix_ns)
    assert gps_ns > unix_ns - 315964800 * 10**9  # GPS runs AHEAD of UTC
    back = btm._gps_snapshot_to_utc({"gps_time_ns": gps_ns})
    assert back == pytest.approx(unix_s, abs=1e-6)


# ── the recorder-level wiring ────────────────────────────────────────


def _core(store, t6_auth=None):
    """__new__-bypassed CoreRecorderV2 carrying only the T3 anchor state."""
    from hf_timestd.core.core_recorder_v2 import CoreRecorderV2
    from hf_timestd.core.t3_registration_anchor import T3RegistrationAnchor

    rec = CoreRecorderV2.__new__(CoreRecorderV2)
    rec._t6_native_anchor = None if t6_auth is None else t3_anchor()
    rec._t6_authority_last_decision = None
    rec._t6_authority_status = lambda: t6_auth
    rec._t3_native_anchor = None
    rec._t3_anchor_holder = T3RegistrationAnchor(store=store, anchor_closure=True, admitted=True)
    return rec


def _acquired_store(tmp_path, utc_ref=WALL0):
    from hf_timestd.core.registration_acquirer import Registration
    from hf_timestd.core.registration_store import (
        ADOPT_MIN_CORROBORATED_MINUTES,
        RegistrationStore,
    )

    store = RegistrationStore(tmp_path / "reg", tmp_path / "registration.json")
    store.write_summary(
        Registration(
            "ep-1",
            rtp_ref=1000,
            utc_ref=utc_ref,
            sample_rate=SR,
            sigma_ms=1.0,
            # task 16b: corroborated long enough for every surface to act
            n_minutes=ADOPT_MIN_CORROBORATED_MINUTES,
            channel="fused",
            verified=True,
        ),
        ["SHARED_10000"],
        "ACQUIRED",
        {"counter_epoch_id": "ep-1"},
    )
    return store


def test_recorder_publishes_the_registration_as_the_provider_state(tmp_path):
    rec = _core(_acquired_store(tmp_path))
    assert rec._t3_label_anchor_state() is None  # before the first refresh
    rec._refresh_t3_native_anchor()
    state = rec._t3_label_anchor_state()
    assert state is not None
    assert state.epoch_id == "ep-1" and state.tier == "T3"
    assert state.anchor.captured_via_tier == "T3"
    assert state.sigma_ns == pytest.approx(1.0e6)  # floored delay-model bound
    assert rec._t3_native_anchor is state.anchor


def test_recorder_stands_down_while_t6_is_authoritative(tmp_path):
    rec = _core(
        _acquired_store(tmp_path), t6_auth={"state": "AUTHORITATIVE", "violations": []}
    )
    assert rec._t6_anchor_is_authoritative() is True
    rec._refresh_t3_native_anchor()
    assert rec._t3_native_anchor is None
    assert rec._t3_label_anchor_state() is None


def test_a_t6_anchor_in_violation_does_not_displace_the_registration(tmp_path):
    """`_t6_native_anchor is not None` is not authority: the coarse
    cascade re-captures one from the same MF edge while the carrier is
    still lost (hf-timestd#14)."""
    rec = _core(
        _acquired_store(tmp_path),
        t6_auth={"state": "DEGRADED", "violations": ["carrier"]},
    )
    assert rec._t6_anchor_is_authoritative() is False
    rec._refresh_t3_native_anchor()
    assert rec._t3_native_anchor is not None
