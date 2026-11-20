"""T3 reaches nothing beyond its own shadow until admitted.

Michael's ruling B (2026-10-30): "close the paths to chrony until
admitted".  One switch, ``[timing.t3] admitted``, false by default.  Until
it reads true, the stand-alone T3 registration reaches nothing beyond its
own shadow record and its published summary:

1. the judge's label-plane tracker (N-1: with ``chain_delay_learn = false``
   the plane term behind every T6-versus-host residual);
2. the judge's ``label_plane_anchor`` (the ``anchor_closure`` path);
3. every surface where the registration places UTC: the ring's label
   anchor and the native anchor (``T3RegistrationAnchor``), §18's
   ``utc_anchor_ns``, metrology's labels, and the authority snapshot.

With the switch true, behaviour returns to 8efe42c, except that a
shadow-only bench still stays out of witness dissent.

Each "not admitted" test below builds every piece with its defaults, as a
station that never names the switch would.
"""

from __future__ import annotations

import json

import pytest

from hf_timestd.core.buffer_timing import unix_ns_to_gps_time_ns
from hf_timestd.core.native_anchor import LabelAnchor, NativeAnchor
from hf_timestd.core.offset_judge import BenchReading, OffsetJudge

SR = 24000
WALL0 = 1_800_000_000.0
KEY = ("hf-status.local", 0xABCD1234)
NOT_ADMITTED = "not admitted (shadow run pending)"
MODE_OFF = 'off ([timing.t3] admission = "off")'
T3_OFF_S = 0.010        # the stand-alone T3 sits 10 ms from truth


# ── the switch ───────────────────────────────────────────────────────


def test_admission_mode_reads_the_new_key_and_maps_the_old(caplog):
    from hf_timestd.core.t3_admission import (
        NOT_ADMITTED_REASON,
        AdmissionMode,
        admission_mode_from_config as f,
    )

    assert NOT_ADMITTED_REASON == NOT_ADMITTED
    assert f(None) is AdmissionMode.AUTO
    assert f({}) is AdmissionMode.AUTO
    assert f({"timing": {"t3": {"admission": "off"}}}) is AdmissionMode.OFF
    assert f({"timing": {"t3": {"admission": "on"}}}) is AdmissionMode.ON
    assert f({"timing": {"t3": {"admitted": True}}}) is AdmissionMode.ON
    assert f({"timing": {"t3": {"admitted": False}}}) is AdmissionMode.AUTO
    # The new key wins when both appear.
    assert f({"timing": {"t3": {"admission": "off", "admitted": True}}}) is AdmissionMode.OFF
    # Malformed values fall back to the default and log a WARNING.
    for bad in ({"timing": {"t3": {"admission": "onn"}}},
                {"timing": {"t3": {"admitted": "true"}}},
                {"timing": {"t3": "yes"}}):
        caplog.clear()
        with caplog.at_level("WARNING"):
            assert f(bad) is AdmissionMode.AUTO
        assert any(r.levelname == "WARNING" for r in caplog.records)
    # Task A7 removed the deprecated bool shim: each process builds one
    # T3AdmissionGate from the mode instead.
    import hf_timestd.core.t3_admission as ta
    assert not hasattr(ta, "t3_admitted")


def test_the_template_ships_admission_auto():
    from pathlib import Path

    import toml

    path = (Path(__file__).resolve().parents[2]
            / "config" / "timestd-config.toml.template")
    cfg = toml.loads(path.read_text(encoding="utf-8"))
    assert cfg["timing"]["t3"] == {"admission": "auto"}


def test_validate_warns_on_the_old_admitted_key():
    from hf_timestd.cli import retired_key_issues

    issues = retired_key_issues({"timing": {"t3": {"admitted": False}}})
    assert any("admitted" in i["message"] and "admission" in i["message"]
               for i in issues)


# ── the judge: a clock, benches, and one run ─────────────────────────


class _Clock:
    def __init__(self):
        self.mono = 1000.0

    def wall(self):
        return WALL0 + (self.mono - 1000.0)


class _Bench:
    def __init__(self, clock, tier, sigma_ns, bench, *, offset_s=0.0,
                 plane="host", authoritative=None):
        self.clock, self.tier, self.sigma_ns = clock, tier, sigma_ns
        self.bench, self.offset_s, self.plane = bench, offset_s, plane
        self.authoritative = authoritative

    def poll(self):
        detail = {"bench": self.bench}
        if self.authoritative is not None:
            detail["authoritative"] = self.authoritative
        return BenchReading(tier=self.tier, utc=self.clock.wall() + self.offset_s,
                            sigma_ns=self.sigma_ns, mono=self.clock.mono,
                            detail=detail, plane=self.plane)


def _t3(clock):
    """An ACQUIRED, verified, corroborated registration: authoritative."""
    return _Bench(clock, "T3", 1_000_000.0, "hf_acquired", offset_s=T3_OFF_S,
                  plane="label", authoritative=True)


def _t4(clock):
    return _Bench(clock, "T4", 200_000.0, "chrony_lan")


def _t6(clock):
    return _Bench(clock, "T6", 10_000.0, "native_anchor", plane="label")


def _judge(tmp_path, clock, benches, *, ticks=12, **kw):
    cfg = {"enabled": True, "upgrade_polls": 1,
           "chain_delay_path": str(tmp_path / "chain_delay.json")}
    cfg.update(kw.pop("config", {}))
    judge = OffsetJudge(config=cfg, benches=benches,
                        publish_path=tmp_path / "offset_judge.json",
                        time_fn=clock.wall, mono_fn=lambda: clock.mono, **kw)
    judge.register_radiod_pair(
        KEY, unix_ns_to_gps_time_ns(int(round(WALL0 * 1e9))), 0, SR)
    for _ in range(ticks):
        judge.tick()
        clock.mono += 10.0
    return judge


def _tracker_offsets_ms(judge):
    return [off / 1e6 for _, off, _ in judge._label_plane._obs]


# ── 1. N-1: the label-plane tracker ──────────────────────────────────


def test_n1_unadmitted_t3_never_feeds_the_label_plane_tracker(tmp_path):
    """``chain_delay_learn = false``: T6 and T3 both fed the one tracker
    behind every T6-versus-host residual.  T3 10 ms off pulled that term,
    and through it could convict T6 and widen HPPS's precision."""
    clock = _Clock()
    judge = _judge(tmp_path, clock, [_t6(clock), _t4(clock), _t3(clock)],
                   config={"chain_delay_learn": False})
    offs = _tracker_offsets_ms(judge)
    assert offs, "T6 still feeds the tracker"
    assert all(abs(o) < 1.0 for o in offs), offs   # no 10 ms T3 entries
    # The record keeps T3's residual.
    assert "T3" in judge._shadow_residuals


def test_n1_unadmitted_t3_stays_out_with_learning_on_too(tmp_path):
    """Learning on, the tracker carries only non-T6 label tiers: T3 alone.
    Fed by T3 against the host, its term would read T3's own offset back,
    a calibration against the host clock."""
    clock = _Clock()
    judge = _judge(tmp_path, clock, [_t6(clock), _t4(clock), _t3(clock)])
    assert _tracker_offsets_ms(judge) == []


# ── 2. the anchor_closure path: label_plane_anchor ───────────────────


def test_unadmitted_t3_is_never_the_label_plane_anchor(tmp_path):
    clock = _Clock()
    judge = _judge(tmp_path, clock, [_t4(clock), _t3(clock)], ticks=3,
                   anchor_closure=True)
    published = json.loads((tmp_path / "offset_judge.json").read_text())
    assert published["label_plane_anchor"] is None
    assert published["label_plane_witness"] is None
    # ... while T6, which needs no admission, still anchors.
    clock = _Clock()
    _judge(tmp_path, clock, [_t6(clock), _t4(clock), _t3(clock)], ticks=3,
           anchor_closure=True)
    published = json.loads((tmp_path / "offset_judge.json").read_text())
    assert published["label_plane_anchor"]["tier"] == "T6"


# ── 3. every surface where the registration places UTC ───────────────


def _t3_label():
    return LabelAnchor(
        anchor=NativeAnchor(anchor_rtp=0, anchor_utc_ns=int(WALL0 * 1e9),
                            sample_rate_hz=SR, chain_delay_ns=0,
                            captured_at_utc_ns=int(WALL0 * 1e9),
                            captured_via_tier="T3"),
        epoch_id="ep-1", tier="T3", sigma_ns=1e6)


def test_section18_refuses_an_unadmitted_t3_label_anchor(tmp_path):
    clock = _Clock()
    judge = _judge(tmp_path, clock, [_t4(clock)], ticks=1, anchor_closure=True)
    judge.set_label_anchor_provider(_t3_label)
    st = list(judge._sources.values())[0]
    assert judge._label_anchor_for_locked(st) is None


def _acquired_store(tmp_path, clock):
    from hf_timestd.core.registration_acquirer import Registration
    from hf_timestd.core.registration_store import (
        ADOPT_MIN_CORROBORATED_MINUTES,
        RegistrationStore,
    )

    st = RegistrationStore(tmp_path / "reg", tmp_path / "registration.json",
                           time_fn=lambda: clock[0])
    st.write_summary(
        Registration("ep-1", rtp_ref=1000, utc_ref=WALL0, sample_rate=SR,
                     sigma_ms=1.0, n_minutes=ADOPT_MIN_CORROBORATED_MINUTES,
                     channel="fused", rtp_domain="SHARED_10000", verified=True),
        ["SHARED_10000"], "ACQUIRED", {"counter_epoch_id": "ep-1"})
    return st


def test_the_ring_and_native_anchors_refuse_an_unadmitted_t3(tmp_path):
    """The ring, §18 and the archive sidecar all label from this holder."""
    from hf_timestd.core.t3_registration_anchor import T3RegistrationAnchor

    clock = [WALL0]
    holder = T3RegistrationAnchor(store=_acquired_store(tmp_path, clock),
                                  time_fn=lambda: clock[0], anchor_closure=True)
    decision = holder.refresh(t6_authoritative=False)
    assert holder.state() is None and holder.anchor is None
    assert decision.reason == NOT_ADMITTED


def test_the_recorder_holds_no_t3_native_anchor_unadmitted(tmp_path):
    from hf_timestd.core.core_recorder_v2 import CoreRecorderV2

    clock = [WALL0]
    store = _acquired_store(tmp_path, clock)
    rec = CoreRecorderV2.__new__(CoreRecorderV2)
    rec._t6_native_anchor = None
    rec._t6_authority_status = lambda: None
    rec._t3_native_anchor = None
    rec._t3_anchor_holder = None
    rec._anchor_closure = True
    import hf_timestd.core.registration_store as rs

    orig = rs.RegistrationStore
    try:
        rs.RegistrationStore = lambda *a, **k: store
        rec._refresh_t3_native_anchor()
    finally:
        rs.RegistrationStore = orig
    assert rec._t3_native_anchor is None
    assert rec._t3_label_anchor_state() is None
    assert rec._label_anchor_state() is None


# ── metrology: an unadmitted T3 labels nothing ───────────────────────


def _metrology(tmp_path):
    from tests.unit.test_metrology_service_registration import _service

    svc = _service(tmp_path)
    svc._AUTHORITY_JSON_PATH = tmp_path / "no-authority.json"   # no T6
    # The shared harness admits T3 (it describes 8efe42c); this one has
    # no gate at all, which reads closed.
    svc.__dict__.pop("_t3_gate", None)
    return svc


def test_metrology_hands_back_the_label_plane_unadmitted(tmp_path):
    """A T3-only station: the channel acquires and verifies, publishes
    ACQUIRED for the shadow record, and labels nothing."""
    from tests.unit.test_metrology_service_registration import _minute

    svc = _metrology(tmp_path)
    for k in range(4):
        bt = _minute(svc, k, walk=0.250)
    assert svc.acquirer.registration is not None
    assert svc.acquirer.registration.verified is True
    assert svc.reg_store.read_summary()["state"] == "ACQUIRED"
    assert bt.origin_source == "label"
    assert bt.judge_tier != "T3"
    # the label plane, 250 ms late, not the acquired truth
    from tests.unit.test_metrology_service_registration import T0
    assert bt.sample0_utc == pytest.approx(T0 + 180 + 0.250, abs=1e-6)


# ── the authority snapshot ───────────────────────────────────────────


class _Probe:
    def __init__(self, result):
        self.t_level = result.t_level
        self.result = result

    def poll(self):
        return self.result


class _Summary:
    def __init__(self, summary):
        self.summary = summary
        self.stale_s = 300.0

    def read_summary(self):
        return self.summary

    def summary_age_s(self, s):
        return 1.0


_ACQUIRED = {"state": "ACQUIRED", "utc_ref": WALL0, "sigma_ms": 1.0,
             "counter_epoch_id": "ep-1", "contributing": ["SHARED_10000"]}


def _authority(tmp_path, *, t3_available, **kw):
    from hf_timestd.core.authority_manager import AuthorityManager, ProbeResult

    probes = [
        _Probe(ProbeResult("T3", t3_available, offset_ms=0.1, sigma_ms=1.0,
                           frame="rtp",
                           reason=None if t3_available else "probe says no")),
        _Probe(ProbeResult("T2", True, offset_ms=6.0, sigma_ms=20.0)),
    ]
    return AuthorityManager(
        probes=probes, output_path=tmp_path / "authority.json",
        a_level_provider=lambda: "A1", upgrade_hysteresis=1,
        registration_store=_Summary(_ACQUIRED), **kw)


def test_unadmitted_t3_publishes_no_authority(tmp_path):
    # Task A6: t3_admitted=False builds the policy in mode off, and the
    # published reason names the mode.
    m = _authority(tmp_path, t3_available=True, t3_admitted=False)
    for _ in range(3):
        state = m.tick()
    assert state.t_level_active == "T2"
    published = json.loads((tmp_path / "authority.json").read_text())
    assert published["t_level_active"] == "T2"
    assert published["t3_unavailable_reason"] == MODE_OFF
    assert published["registration"]["admission"] == MODE_OFF
    # The registration block keeps the record, and claims no source.
    assert published["registration"]["state"] == "ACQUIRED"
    assert published["registration"]["source"] == "label"


def test_registration_block_claims_no_source_unadmitted(tmp_path):
    from hf_timestd.core.authority_manager import registration_block

    assert registration_block(_Summary(_ACQUIRED))["source"] == "label"


def test_the_timing_chain_record_claims_no_source_unadmitted(tmp_path, monkeypatch):
    import hf_timestd.core.authority_manager as am
    from hf_timestd.core.timing_chain_publisher import publish_chains

    real = am.registration_block
    monkeypatch.setattr(am, "registration_block",
                        lambda *a, **k: real(_Summary(_ACQUIRED), *a[1:], **k))
    from hf_timestd.core.t3_admission import AdmissionMode

    # Task A7: the chain record goes out once, at start, so only mode ON
    # claims a source there; auto's per-minute truth lives in
    # authority.json.  The default reads auto.
    publish_chains([], tmp_path / "chains.json")
    doc = json.loads((tmp_path / "chains.json").read_text())
    assert doc["registration"]["source"] == "label"
    assert doc["t3_admission_mode"] == "auto"
    for mode, source in ((AdmissionMode.AUTO, "label"), (AdmissionMode.OFF, "label"),
                         (AdmissionMode.ON, "hf_acquired")):
        publish_chains([], tmp_path / "chains.json", t3_admission_mode=mode)
        doc = json.loads((tmp_path / "chains.json").read_text())
        assert doc["registration"]["source"] == source
        assert doc["t3_admission_mode"] == mode.value


# ── 4. admitted: behaviour as at 8efe42c ─────────────────────────────


def test_admitted_t3_feeds_the_tracker_and_anchors(tmp_path):
    clock = _Clock()
    judge = _judge(tmp_path, clock, [_t6(clock), _t4(clock), _t3(clock)],
                   config={"chain_delay_learn": False}, t3_admitted=True)
    assert any(abs(o - T3_OFF_S * 1e3) < 0.5 for o in _tracker_offsets_ms(judge))
    clock = _Clock()
    _judge(tmp_path, clock, [_t4(clock), _t3(clock)], ticks=3,
           anchor_closure=True, t3_admitted=True)
    published = json.loads((tmp_path / "offset_judge.json").read_text())
    assert published["label_plane_anchor"]["bench"] == "hf_acquired"


def test_admitted_t3_reaches_section18_and_the_ring(tmp_path):
    from hf_timestd.core.t3_admission_gate import T3AdmissionGate
    from hf_timestd.core.t3_registration_anchor import T3RegistrationAnchor

    clock = _Clock()
    judge = _judge(tmp_path, clock, [_t4(clock)], ticks=1, anchor_closure=True,
                   t3_admitted=True)
    judge.set_label_anchor_provider(_t3_label)
    st = list(judge._sources.values())[0]
    assert judge._label_anchor_for_locked(st) is not None
    wall = [WALL0]
    holder = T3RegistrationAnchor(store=_acquired_store(tmp_path, wall),
                                  time_fn=lambda: wall[0], anchor_closure=True,
                                  gate=T3AdmissionGate.fixed(True))
    assert holder.refresh(t6_authoritative=False).reason == "acquired"
    assert holder.state() is not None and holder.state().tier == "T3"


def test_admitted_t3_labels_metrology(tmp_path):
    from tests.unit.test_metrology_service_registration import T0, _minute

    from hf_timestd.core.t3_admission_gate import T3AdmissionGate

    svc = _metrology(tmp_path)
    svc._t3_gate = T3AdmissionGate.fixed(True)
    for k in range(4):
        bt = _minute(svc, k, walk=0.250)
    assert bt.origin_source == "acquired"
    assert bt.judge_tier == "T3"
    assert bt.sample0_utc == pytest.approx(T0 + 180, abs=0.002)


def test_admitted_t3_may_hold_authority(tmp_path):
    from hf_timestd.core.authority_manager import registration_block

    m = _authority(tmp_path, t3_available=True, t3_admitted=True)
    for _ in range(3):
        state = m.tick()
    assert state.t_level_active == "T3"
    published = json.loads((tmp_path / "authority.json").read_text())
    assert "t3_unavailable_reason" not in published
    assert published["registration"]["source"] == "hf_acquired"
    assert registration_block(_Summary(_ACQUIRED), admitted=True)["source"] == (
        "hf_acquired")
    # An unavailable probe still says its own reason.
    m = _authority(tmp_path, t3_available=False, t3_admitted=True)
    m.tick()
    published = json.loads((tmp_path / "authority.json").read_text())
    assert published["t3_unavailable_reason"] == "probe says no"


@pytest.mark.parametrize("authoritative", [False, True])
def test_admitted_t3_dissents_only_when_authoritative(tmp_path, authoritative):
    """Task A8: one decision drives the vote too.  Admitted, hf_acquired is
    no longer shadow-only and votes in witness dissent, but only while its
    reading is authoritative: a WITNESS-state plane the one gate refuses
    still never convicts T6 (builder choice, the closed side)."""
    from tests.unit.test_shadow_only_never_dissents import (
        OFF_S as _OFF_S,
        _Bench as _DBench,
        _Clock as _DClock,
        _hpps_floor,
        _t4 as _dt4,
    )

    clock = _DClock()
    t6 = _DBench(clock, "T6", 10_000.0, "native_anchor", plane="label")
    t3 = _DBench(clock, "T3", 1_000_000.0, "hf_acquired", offset_s=_OFF_S,
                 plane="label", authoritative=authoritative)
    judge = OffsetJudge(config={"enabled": True, "upgrade_polls": 1},
                        benches=[t6, _dt4(clock), t3],
                        publish_path=tmp_path / "offset_judge.json",
                        time_fn=clock.wall, mono_fn=lambda: clock.mono,
                        t3_admitted=True)
    judge.register_radiod_pair(
        KEY, unix_ns_to_gps_time_ns(int(round(WALL0 * 1e9))), 0, SR)
    for _ in range(3):
        judge.tick()
        clock.mono += 10.0
    clock.departure_s = 0.010
    for _ in range(21):
        judge.tick()
        clock.mono += 10.0
    assert judge._best.tier == "T6"
    assert "T3" in judge._shadow_residuals
    if authoritative:
        assert judge._dissent is not None and "T3" in judge._dissent.tiers
        assert _hpps_floor(judge) is not None
    else:
        assert judge._dissent is None
        assert _hpps_floor(judge) is None
