"""apply_registration turns a skewed label plane into the acquired plane
and hands the engine a BufferTiming with origin_source='acquired'."""

import dataclasses
import json
import logging
from types import SimpleNamespace

import numpy as np
import pytest

from hf_timestd.core.buffer_timing import BufferTiming, unix_ns_to_gps_time_ns
from hf_timestd.core.counter_epoch_tracker import CounterEpochTracker
from hf_timestd.core.metrology_service import MetrologyService
from hf_timestd.core.registration_acquirer import RegistrationAcquirer
from hf_timestd.core.registration_store import RegistrationStore
from tests.unit.synth_origins import ND_K0, counter_map_for
from tests.unit.synth_ticks import make_tick_audio, label_timing

SR = 24000
MIN = 1_800_000_000
T0 = MIN - 1.0


class _Engine:
    """Stand-in exposing the two helpers the service relies on."""

    sample_rate = SR

    def prepare_audio(self, iq):
        return np.asarray(iq, dtype=np.float64)

    def expected_delays_s(self, system_time, utc_minute):
        return {"WWV": 0.0125}


# Task 7: every channel names its own RTP counter through a counter map
# (ka9q-radio e5f790dd gives each channel its own start block).  This
# channel's minutes carry _OWN_MAP in their metadata, as the recorder
# publishes it into the ring.  The siblings below that fuse, adopt or
# resolve with this channel started on the same block (as four of ND's
# five 24 kHz channels do, k0 = 51), so their raw RTP equals this
# channel's and every value in these tests reads as before; they now say
# so through _SIB_MAP rather than by assumption.  Different start blocks
# are tested in test_metrology_service_counter_domains.py.
_OWN_MAP = counter_map_for(ND_K0[0], SR)
_SIB_MAP = counter_map_for(ND_K0[1], SR)


def _service(tmp_path):
    svc = MetrologyService.__new__(MetrologyService)  # bypass the heavy __init__
    svc.channel_name = "SHARED_10000"
    svc.sample_rate = SR
    svc.engine = _Engine()
    svc.acquirer = RegistrationAcquirer("SHARED_10000", SR)
    svc.reg_store = RegistrationStore(tmp_path / "reg", tmp_path / "registration.json")
    svc.epoch_tracker = CounterEpochTracker()
    return svc


def _raw_pair(k, walk):
    """Task 10: radiod's raw (GPS_TIME, RTP_TIMESNAP) pair for minute k,
    placing the buffer's first sample ``walk`` seconds after truth.  T3
    starts from this pair, never from the ring label, so a test that wants
    the acquirer to see a label ``walk`` late says so here."""
    utc_ns = int(round(T0)) * 10**9 + 60 * k * 10**9 + int(round(walk * 1e9))
    return {
        "raw_gps_time_ns": unix_ns_to_gps_time_ns(utc_ns),
        "raw_rtp_timesnap": 1_000_000 + k * 60 * SR,
    }


def _pair(k, walk=0.0):
    """Minute k's radiod (GPS_TIME, RTP_TIMESNAP) pair, as the epoch tracker
    takes it.  Task 10: the tracker observes radiod's raw pair."""
    raw = _raw_pair(k, walk)
    return {
        "gps_time_ns": raw["raw_gps_time_ns"],
        "rtp_timesnap": raw["raw_rtp_timesnap"],
        "sample_rate": SR,
    }


def _meta(k, walk=0.0):
    """Minute k's ring metadata: the ring anchor pair, radiod's raw pair
    (``walk`` seconds late) and this channel's counter map.  The ring
    anchor is the same pair here; T3 reads only the raw one."""
    return {**_pair(k, walk), **_raw_pair(k, walk), "counter_map": _OWN_MAP}


def _ticks(k, marker=True, seed=None):
    """Minute k's 62 s of WWV ticks at truth: the channel's own evidence,
    which ``measure_minute`` folds on the held plane (Task 10)."""
    return make_tick_audio(
        62, SR, T0 + 60 * k, {"WWV": 0.0125}, snr_db=20.0,
        seed=7 + k if seed is None else seed, marker=marker,
    )


def _minute(svc, k, walk=0.250, raw_walk=None, audio=None, t6=False):
    """Apply minute k.  ``walk``: the ring label's offset from truth.
    ``raw_walk``: radiod's raw pair's offset (default: the same).  ``t6``:
    the ring label is T6's plane."""
    label = label_timing(T0 + 60 * k, walk, SR)
    if t6:
        label.judge_tier = "T6"
    return svc.apply_registration(
        label,
        _ticks(k) if audio is None else audio,
        1_000_000 + k * 60 * SR,
        MIN + 60 * k,
        _meta(k, walk if raw_walk is None else raw_walk),
    )


def test_first_minute_bootstraps_then_acquires(tmp_path):
    """task-11b: an own acquisition with no ACQUIRED siblings is a
    CANDIDATE until the tick detector verifies it -- the plane is still
    applied to BufferTiming (origin_source="acquired") in the meantime,
    but the published state is CANDIDATE, not ACQUIRED."""
    svc = _service(tmp_path)
    audio = make_tick_audio(62, SR, T0, {"WWV": 0.0125}, snr_db=20.0)
    label = label_timing(T0, 0.250, SR)  # radiod pair 250 ms late
    bt = svc.apply_registration(
        label, audio, start_rtp=1_000_000, minute_utc=MIN, metadata=_meta(0, 0.250)
    )
    assert bt.origin_source == "acquired"
    assert bt.sample0_utc == pytest.approx(T0, abs=0.002)
    assert bt.origin_sigma_ms >= 1.0 and bt.counter_epoch_id.startswith("ep-")
    s = svc.reg_store.read_summary()
    assert s["state"] == "CANDIDATE" and s["raw_pair_residual_ms"] == pytest.approx(
        -250.0, abs=2.0
    )
    assert svc.acquirer.registration.verified is False


def test_verified_plane_publishes_acquired_next_minute(tmp_path):
    """task-11b, rewritten for Task 10: the acquisition minute publishes
    CANDIDATE.  In the next minute the channel's own ticks, folded on the
    held plane (``measure_minute``), verify it, and that minute publishes
    ACQUIRED.  (Was: a tick-like ``feed_back_ensembles`` ensemble for the
    plane's own station verified it.)"""
    svc = _service(tmp_path)
    bt0 = _minute(svc, 0, audio=_ticks(0, marker=False))
    assert bt0.origin_source == "acquired"
    assert svc.reg_store.read_summary()["state"] == "CANDIDATE"
    assert svc.acquirer.registration.verified is False
    bt1 = _minute(svc, 1, audio=_ticks(1, marker=False))
    assert svc.acquirer.registration.verified is True
    assert set(svc._last_arrivals) == {"WWV"}
    assert abs(svc._last_arrivals["WWV"].residual_ms) <= 1.0
    assert bt1.origin_source == "acquired"
    s = svc.reg_store.read_summary()
    assert s["state"] == "ACQUIRED"


def test_marker_anchored_ensemble_also_verifies_the_plane(tmp_path):
    """task-11b fix round 1 (C1), rewritten for Task 10: the strongest
    channels, which hear the minute marker, must verify too.  Then the
    detector's marker-anchored ensembles had to count; now the
    self-measurement searches the marker on the held plane every minute
    (its run-up is the previous minute's tail), finds no whole second on
    a true plane, and verifies.  The channel file is ACQUIRED from the
    first measured minute on, and the marker never shifts the residual."""
    svc = _service(tmp_path)
    _minute(svc, 0)
    assert svc.reg_store.read_summary()["state"] == "CANDIDATE"
    for k in (1, 2, 3):
        bt = _minute(svc, k)
        assert svc.acquirer.registration.verified is True
        assert abs(svc._last_arrivals["WWV"].residual_ms) <= 1.0, (
            "a marker on a true plane must report no whole second")
        assert bt.origin_source == "acquired"
        assert svc.reg_store.read_summary()["state"] == "ACQUIRED"
    assert svc.acquirer._self_marker_k.get("WWV", (None, 0))[1] == 0, (
        "the marker was searched and found no whole second")


def test_marker_anchored_large_residual_rejects_not_verifies(tmp_path):
    """task-11b fix round 2 (N1), rewritten for Task 10: a tick-like
    measurement (sigma_1 small) whose residual is large is a rejection,
    never a verification.  The candidate plane sits 30 ms off the ticks;
    the next minute's self-measurement reads +30 ms at a tick-like sigma
    and must reject: the channel publishes BOOTSTRAP that same minute and
    the next, and the stale channel file is never offered as a sibling."""
    svc = _service(tmp_path)
    _minute(svc, 0)
    assert svc.reg_store.read_summary()["state"] == "CANDIDATE"
    svc.acquirer.registration.utc_ref += 0.030  # the plane labels 30 ms late
    bt1 = _minute(svc, 1)
    a = svc._last_arrivals["WWV"]
    assert a.residual_ms == pytest.approx(30.0, abs=1.0)
    assert a.sigma_ms <= RegistrationAcquirer.TIMING_SIGMA_MAX_MS
    assert svc.acquirer.state == RegistrationAcquirer.STATE_BOOTSTRAP
    assert bt1.origin_source == "label"
    assert svc.reg_store.read_summary()["state"] == "BOOTSTRAP"
    rng = np.random.default_rng(5)
    noise = 0.1 * rng.standard_normal(62 * SR)
    bt2 = _minute(svc, 2, audio=noise)
    assert bt2.origin_source == "label"
    s = svc.reg_store.read_summary()
    assert s["state"] == "BOOTSTRAP"
    assert svc.reg_store.read_siblings(
        exclude_channel="WWV_20000", states=("ACQUIRED", "WITNESS")) == []


def test_unverifiable_plane_publishes_bootstrap_next_minute(tmp_path, monkeypatch):
    """task-11b, rewritten for Task 10: a measurement that is not
    tick-like (sigma 14.6 ms, far past TIMING_SIGMA_MAX_MS) fails
    verification and resets the acquirer -- this minute and the next
    publish BOOTSTRAP.  (Was: the same junk ensemble through
    ``feed_back_ensembles``.)  The measurement is stubbed: a real fold
    that weak finds no peak at all."""
    from hf_timestd.core.registration_acquirer import MinuteArrival

    svc = _service(tmp_path)
    _minute(svc, 0)
    junk = {"WWV": MinuteArrival("WWV", residual_ms=2.0, sigma_ms=14.6,
                                 arrival_rtp=0.0, fold_snr_db=3.0)}
    monkeypatch.setattr(svc.acquirer, "measure_minute", lambda *a, **k: dict(junk))
    bt1 = _minute(svc, 1)
    assert svc.acquirer.state == RegistrationAcquirer.STATE_BOOTSTRAP
    assert bt1.origin_source == "label"
    assert svc.reg_store.read_summary()["state"] == "BOOTSTRAP"
    monkeypatch.undo()
    rng = np.random.default_rng(4)
    noise = 0.1 * rng.standard_normal(62 * SR)
    bt2 = _minute(svc, 2, audio=noise)
    assert bt2.origin_source == "label"
    assert svc.reg_store.read_summary()["state"] == "BOOTSTRAP"


def test_candidate_file_written_by_channel_a_is_not_adopted_by_channel_b(tmp_path):
    """task-11b: RegistrationStore.read_siblings already skips any file
    whose state isn't ACQUIRED -- confirm a CANDIDATE file (channel A's own
    unverified plane) is not picked up as a sibling by channel B."""
    svc_a = _service(tmp_path)
    audio = make_tick_audio(62, SR, T0, {"WWV": 0.0125}, snr_db=20.0)
    svc_a.apply_registration(
        label_timing(T0, 0.250, SR), audio, 1_000_000, MIN, _meta(0, 0.250)
    )
    assert svc_a.reg_store.read_summary()["state"] == "CANDIDATE"
    channel_file = tmp_path / "reg" / "SHARED_10000.json"
    assert json.loads(channel_file.read_text())["state"] == "CANDIDATE"

    svc_b = _service(tmp_path)
    svc_b.channel_name = "WWV_20000"
    svc_b.acquirer = RegistrationAcquirer("WWV_20000", SR)
    sibs = svc_b.reg_store.read_siblings(exclude_channel="WWV_20000")
    assert sibs == []


def test_bootstrap_leaves_the_label_plane_marked(tmp_path):
    svc = _service(tmp_path)
    rng = np.random.default_rng(3)
    noise = 0.1 * rng.standard_normal(62 * SR)
    bt = svc.apply_registration(
        label_timing(T0, 0.1, SR), noise, 1_000_000, MIN, _meta(0, 0.1)
    )
    assert bt.origin_source == "label" and bt.sample0_utc == T0 + 0.1
    assert svc.reg_store.read_summary()["state"] == "BOOTSTRAP"


def test_sibling_registration_is_adopted(tmp_path):
    svc = _service(tmp_path)
    from hf_timestd.core.registration_acquirer import Registration

    sib = Registration(
        counter_epoch_id=svc.epoch_tracker.observe(**_pair(0)),
        rtp_ref=1_000_000,
        utc_ref=T0,
        sample_rate=SR,
        sigma_ms=0.9,
        channel="WWV_20000",
        counter_map=_SIB_MAP,
    )
    svc.reg_store.write_channel(sib, "ACQUIRED", {})
    rng = np.random.default_rng(3)
    noise = 0.1 * rng.standard_normal(62 * SR)  # this channel hears nothing
    bt = svc.apply_registration(
        label_timing(T0, 0.3, SR), noise, 1_000_000, MIN, _meta(0, 0.3)
    )
    assert bt.origin_source == "acquired" and bt.sample0_utc == pytest.approx(
        T0, abs=1e-6
    )


def test_sibling_only_fusion_without_maps_yields_no_plane(tmp_path):
    """Task 6 fix round 1.  This channel hears nothing, and one sibling on
    another channel holds an ACQUIRED plane with no counter map.  Its
    rtp_ref lives in WWV_20000's counter, which starts at its own block
    (ka9q-radio e5f790dd), so read at this channel's raw RTP it lands
    wherever the two origins differ -- here 20 ms, ND's k0 51 against 50.
    Nothing can convert it, so the service must publish no plane rather
    than adopt that one."""
    svc = _service(tmp_path)
    from hf_timestd.core.registration_acquirer import Registration

    sib = Registration(
        counter_epoch_id=svc.epoch_tracker.observe(**_pair(0)),
        rtp_ref=1_000_000,
        utc_ref=T0 + 0.020,
        sample_rate=SR,
        sigma_ms=0.9,
        channel="WWV_20000",
        verified=True,
        n_minutes=5,
    )
    svc.reg_store.write_channel(sib, "ACQUIRED", {})
    rng = np.random.default_rng(3)
    noise = 0.1 * rng.standard_normal(62 * SR)  # this channel hears nothing
    bt = svc.apply_registration(
        label_timing(T0, 0.3, SR), noise, 1_000_000, MIN, _meta(0, 0.3)
    )
    assert bt.origin_source == "label"
    assert svc.acquirer.registration is None
    assert svc.reg_store.read_summary()["state"] == "BOOTSTRAP"


def test_shared_channel_ambiguity_resolved_by_same_site_sibling(tmp_path):
    svc = _service(tmp_path)

    class _Eng(_Engine):
        def expected_delays_s(self, system_time, utc_minute):
            return {"WWV": 0.0125, "BPM": 0.0465}  # shared 1000 Hz band, 34 ms apart

    svc.engine = _Eng()
    from hf_timestd.core.registration_acquirer import Registration

    epoch = svc.epoch_tracker.observe(**_pair(0))
    sib = Registration(
        epoch,
        rtp_ref=1_000_000,
        utc_ref=T0 + 0.0005,
        sample_rate=SR,
        sigma_ms=0.9,
        channel="WWV_20000",
        counter_map=_SIB_MAP,
        stations=("WWV",),
    )
    svc.reg_store.write_channel(sib, "ACQUIRED", {})
    audio = make_tick_audio(
        62, SR, T0, {"WWV": 0.0125}, snr_db=20.0
    )  # only WWV audible
    bt = svc.apply_registration(
        label_timing(T0, 0.1, SR), audio, 1_000_000, MIN, _meta(0, 0.1)
    )
    assert bt.origin_source == "acquired" and bt.sample0_utc == pytest.approx(
        T0, abs=0.002
    )
    assert svc.acquirer.registration.stations == (
        "WWV",
    )  # resolved, not merely adopted
    assert "SHARED_10000" in svc.reg_store.read_summary()["contributing"]


def test_feed_back_reacquires_on_sustained_residual(tmp_path, caplog):
    """task-11b, rewritten for Task 10: corroborate's own sustained-residual
    rule, driven by the self-measurement.  A verified plane that drifts
    40 ms off its ticks is held one minute and given up the next; the
    give-up logs exactly one WARNING, the reset's (Task 9 review, ruling
    3: no "registration residual sustained" line beside it).  (Was: two
    +40 ms ensembles through ``feed_back_ensembles``.)"""
    svc = _service(tmp_path)
    _minute(svc, 0)
    _minute(svc, 1)
    assert svc.acquirer.registration.verified is True
    svc.acquirer.registration.utc_ref += 0.040
    _minute(svc, 2)
    assert svc.acquirer.state == RegistrationAcquirer.STATE_ACQUIRED  # held
    caplog.clear()
    caplog.set_level(logging.INFO)
    _minute(svc, 3)
    assert svc.acquirer.state == RegistrationAcquirer.STATE_BOOTSTRAP
    loud = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(loud) == 1 and "registration reset" in loud[0], loud
    assert "sustained" not in caplog.text


# ── Fix round 1 (task-8-review.md / task-8-fix1-brief.md) ──────────────


def test_t6_authoritative_plane_is_not_replaced(tmp_path):
    """C3: T6 must win.  ``judge_tier == "T6"`` on the incoming plane (the
    sidecar/replay shape) means the ring anchor already carries T6's
    correction -- apply_registration must publish a WITNESS, not overwrite
    the origin.  Task 10: the plane verifies on its own ticks, so the
    WITNESS comes in the first measured minute; T6's plane is handed back
    untouched in every minute."""
    svc = _service(tmp_path)
    for k in (0, 1):
        # T6 says truth + 10 ms; radiod's raw pair sits at truth.
        bt = _minute(svc, k, walk=0.010, raw_walk=0.0, t6=True)
        assert bt.origin_source == "label"
        assert bt.sample0_utc == pytest.approx(
            T0 + 60 * k + 0.010, abs=1e-9
        )  # T6's plane, untouched
        if k == 0:
            # T6 agrees within 10 ms, and verifies nothing
            assert svc.reg_store.read_summary()["state"] == "CANDIDATE"
    s = svc.reg_store.read_summary()
    assert s["state"] == "WITNESS"
    # acquired ~T0, label T0+10ms -> residual (acquired - label) ~ -10 ms
    assert s["residual_vs_t6_ms"] == pytest.approx(-10.0, abs=2.0)


def test_t6_authoritative_small_residual_vs_t6_is_witness(tmp_path):
    """task-11b fix round 3, rewritten for Task 10: the CANDIDATE gate
    applies on T6 stations too, and T6 never opens it.  A plane 0.8 ms
    from T6 is still a CANDIDATE in its acquisition minute; the channel's
    own ticks verify it the next minute -> WITNESS.  (Was: the residual
    against T6's plane verified it.)"""
    svc = _service(tmp_path)
    bt = _minute(svc, 0, walk=0.0008, raw_walk=0.0, t6=True)
    assert bt.origin_source == "label"
    s = svc.reg_store.read_summary()
    assert s["state"] == "CANDIDATE", "agreement with T6 verifies nothing"
    assert abs(s["residual_vs_t6_ms"]) <= RegistrationAcquirer.VERIFY_MAX_RESIDUAL_MS
    assert svc.acquirer.registration.verified is False
    bt = _minute(svc, 1, walk=0.0008, raw_walk=0.0, t6=True)
    assert bt.origin_source == "label"
    s = svc.reg_store.read_summary()
    assert s["state"] == "WITNESS"
    assert svc.acquirer.registration.verified is True


def test_t6_authoritative_large_residual_vs_t6_is_candidate_not_witness(tmp_path):
    """task-11b fix round 3, rewritten for Task 10: an unverified plane on
    a T6 station is a CANDIDATE, not a WITNESS, and HfAcquiredBench.poll()
    must treat that summary as silence (CANDIDATE is not in
    _LIVE_STATES), so a phantom never reaches the judge on a T6 station.
    The phantom here sits 40 ms off its own ticks.  Its acquisition minute
    is a CANDIDATE (T6 agrees with it, and verifies nothing); its first
    measured minute rejects it.  The bench stays silent throughout.
    (Was: a plane 40 ms off T6's plane was the phantom.)"""
    from hf_timestd.core.offset_judge import HfAcquiredBench

    svc = _service(tmp_path)
    bench = HfAcquiredBench(
        provider=lambda: {"SHARED_10000": (1_000_000, 1000.0, SR)},
        store=svc.reg_store,
        mono_fn=lambda: 1000.0,
    )
    # T6 sits 40 ms late and the raw pair at truth, so acquisition lands
    # at truth; move the plane onto T6's, a phantom its own ticks disagree
    # with, before it is measured.
    bt = _minute(svc, 0, walk=0.040, raw_walk=0.0, t6=True)
    assert bt.origin_source == "label"
    svc.acquirer.registration.utc_ref += 0.040
    s = svc.reg_store.read_summary()
    assert s["state"] == "CANDIDATE"
    assert svc.acquirer.registration.verified is False
    assert bench.poll() is None
    bt = _minute(svc, 1, walk=0.040, raw_walk=0.0, t6=True)
    assert bt.origin_source == "label"
    assert svc._last_arrivals["WWV"].residual_ms == pytest.approx(40.0, abs=1.0)
    assert svc.acquirer.state == RegistrationAcquirer.STATE_BOOTSTRAP
    assert svc.reg_store.read_summary()["state"] == "BOOTSTRAP"
    assert bench.poll() is None


def test_t6_authoritative_via_authority_json_on_the_live_ring_path(tmp_path):
    """C3, second path: the live ring never populates ``judge_tier`` on
    BufferTiming (ring_buffer_reader's metadata carries no "timing" block --
    confirmed by reading resolve_buffer_timing and ring_buffer_reader.
    extract_interval), so on that path the tier must come from
    AuthorityManager's own /run/hf-timestd/authority.json.  Task 10: the
    WITNESS comes once the plane verifies on its own ticks (minute 1)."""
    svc = _service(tmp_path)
    auth_path = tmp_path / "authority.json"
    auth_path.write_text(json.dumps({"t_level_active": "T6"}))
    svc._AUTHORITY_JSON_PATH = auth_path
    for k in (0, 1):
        label = label_timing(T0 + 60 * k, 0.010, SR)
        assert label.judge_tier is None  # the live-ring shape: never populated
        bt = svc.apply_registration(
            label, _ticks(k), 1_000_000 + k * 60 * SR, MIN + 60 * k, _meta(k, 0.0))
        assert bt.origin_source == "label" and bt.sample0_utc == label.sample0_utc
        assert svc.reg_store.read_summary()["state"] == (
            "CANDIDATE" if k == 0 else "WITNESS")


def test_missing_authority_json_is_not_authoritative(tmp_path):
    """Absent file -> not authoritative (no crash, normal acquisition)."""
    svc = _service(tmp_path)
    svc._AUTHORITY_JSON_PATH = tmp_path / "does_not_exist.json"
    audio = make_tick_audio(62, SR, T0, {"WWV": 0.0125}, snr_db=20.0)
    bt = svc.apply_registration(
        label_timing(T0, 0.1, SR), audio, 1_000_000, MIN, _meta(0, 0.1)
    )
    assert bt.origin_source == "acquired"


def test_adopted_plane_does_not_re_enter_fusion(tmp_path):
    """C1: adopt() must stamp this channel's own name and method="adopted",
    and an adopted plane must never contribute back into its own fusion
    (mathematically: fusing an echo of a sibling alongside that same
    sibling understates sigma) nor appear as "fused" in contributing.
    Four-minute probe from the review: one sibling sigma 0.9ms, this
    channel hears nothing every minute -> summary sigma must stay 0.9ms."""
    svc = _service(tmp_path)
    from hf_timestd.core.registration_acquirer import Registration

    sib = Registration(
        counter_epoch_id=svc.epoch_tracker.observe(**_pair(0)),
        rtp_ref=1_000_000,
        utc_ref=T0,
        sample_rate=SR,
        sigma_ms=0.9,
        channel="WWV_20000",
        counter_map=_SIB_MAP,
    )
    svc.reg_store.write_channel(sib, "ACQUIRED", {})
    rng = np.random.default_rng(3)
    for k in range(4):
        noise = 0.1 * rng.standard_normal(62 * SR)  # this channel hears nothing, ever
        svc.apply_registration(
            label_timing(T0, 0.3, SR),
            noise,
            1_000_000 + k * 60 * SR,
            MIN + k * 60,
            _meta(k, 0.3),
        )
    assert svc.acquirer.registration.channel == "SHARED_10000"
    assert svc.acquirer.registration.method == "adopted"
    s = svc.reg_store.read_summary()
    assert s["sigma_ms"] == pytest.approx(0.9, abs=1e-6)
    assert "fused" not in s["contributing"]


def test_apply_registration_failure_keeps_the_label_plane(tmp_path, monkeypatch):
    """I1: a raising acquirer must not wedge the minute -- apply_registration
    logs and returns the ORIGINAL (label) plane unchanged."""
    svc = _service(tmp_path)

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(svc.acquirer, "offer_minute", _boom)
    label = label_timing(T0, 0.1, SR)
    audio = make_tick_audio(62, SR, T0, {"WWV": 0.0125}, snr_db=20.0)
    bt = svc.apply_registration(label, audio, 1_000_000, MIN, _meta(0, 0.1))
    assert bt is label
    assert bt.origin_source == "label" and bt.sample0_utc == T0 + 0.1


def test_sibling_conflict_reverts_to_label_and_flags_conflict(tmp_path):
    """I2: this channel's own ACQUIRED plane disagreeing with its sibling
    (nothing within FUSE_OUTLIER_MS of the combined median) must revert to
    the label plane and say CONFLICT, with a warning naming the
    disagreement.

    final review I6: CONFLICT is a per-CHANNEL verdict, and
    registration.json is one file that N independent processes rewrite each
    minute.  Publishing CONFLICT as the STATION's state let one channel out
    of six flap the summary that HfAcquiredBench and authority.json both
    read.  So the channel file says CONFLICT, and the summary names the
    channels involved under `conflicts` while its own state stays derived
    from the fused result.

    fuse_registrations' median can only return an empty "keep" set for an
    EVEN total (an odd count always keeps at least the middle value, which
    sits at distance 0 from itself) -- so the minimal, reliable
    reproduction is exactly two planes (this channel's own + one sibling),
    not the review's "two siblings" text verbatim.

    task-11b fix round 1 (I1): CONFLICT requires two independently-TRUSTED
    planes disagreeing -- an unverified own CANDIDATE instead defers to a
    real sibling's plane (adopt), it does not conflict with it.  So this
    scenario needs the own plane marked verified first, exactly as the
    ruling's own words draw the line ("own verified -> fuse own +
    siblings (as now)")."""
    svc = _service(tmp_path)
    from hf_timestd.core.registration_acquirer import Registration

    audio = make_tick_audio(62, SR, T0, {"WWV": 0.0125}, snr_db=20.0)
    svc.apply_registration(label_timing(T0, 0.0, SR), audio, 1_000_000, MIN, _meta(0, 0.0))
    assert svc.acquirer.state == RegistrationAcquirer.STATE_ACQUIRED
    svc.acquirer.registration.verified = True
    own_epoch = svc.acquirer.registration.counter_epoch_id
    sib = Registration(
        own_epoch,
        rtp_ref=1_000_000,
        utc_ref=T0 + 0.020,
        sample_rate=SR,
        sigma_ms=0.9,
        channel="WWV_20000",
        counter_map=_SIB_MAP,
    )
    svc.reg_store.write_channel(sib, "ACQUIRED", {})
    audio1 = make_tick_audio(62, SR, T0 + 60, {"WWV": 0.0125}, snr_db=20.0, seed=8)
    bt = svc.apply_registration(
        label_timing(T0 + 60, 0.0, SR),
        audio1,
        1_000_000 + 60 * SR,
        MIN + 60,
        _meta(1, 0.0),
    )
    assert bt.origin_source == "label"
    ch = json.loads((tmp_path / "reg" / "SHARED_10000.json").read_text())
    assert ch["state"] == "CONFLICT"
    s = svc.reg_store.read_summary()
    assert s["state"] == "BOOTSTRAP"  # no fused plane this minute
    assert s["conflicts"] == ["SHARED_10000", "WWV_20000"]
    assert s["contributing"] == []


def test_candidate_own_defers_to_verified_sibling_no_conflict(tmp_path):
    """task-11b fix round 1 (I1): an own CANDIDATE (unverified) plane is not
    independent evidence to fuse against a real sibling -- unlike the
    CONFLICT test above (own VERIFIED, genuine disagreement), an unverified
    own plane defers to a verified sibling's plane even 250 ms away: the
    NEXT minute's applied plane is the sibling's, state ACQUIRED, no
    CONFLICT."""
    svc = _service(tmp_path)
    from hf_timestd.core.registration_acquirer import Registration

    audio = make_tick_audio(62, SR, T0, {"WWV": 0.0125}, snr_db=20.0)
    bt0 = svc.apply_registration(
        label_timing(T0, 0.0, SR), audio, 1_000_000, MIN, _meta(0, 0.0)
    )
    assert bt0.origin_source == "acquired"
    assert svc.acquirer.registration.verified is False  # still a CANDIDATE

    sib = Registration(
        svc.acquirer.registration.counter_epoch_id,
        rtp_ref=1_000_000,
        utc_ref=T0 + 0.250,  # 250 ms away -- would CONFLICT if own were trusted
        sample_rate=SR,
        sigma_ms=0.9,
        channel="WWV_20000",
        counter_map=_SIB_MAP,
        stations=("WWV",),
        verified=True,
    )
    svc.reg_store.write_channel(sib, "ACQUIRED", {})

    audio1 = make_tick_audio(62, SR, T0 + 60, {"WWV": 0.0125}, snr_db=20.0, seed=8)
    bt1 = svc.apply_registration(
        label_timing(T0 + 60, 0.0, SR), audio1, 1_000_000 + 60 * SR, MIN + 60, _meta(1, 0.0)
    )
    assert bt1.origin_source == "acquired"
    assert bt1.sample0_utc == pytest.approx(T0 + 60 + 0.250, abs=0.002)
    assert svc.acquirer.registration.method == "adopted"
    assert svc.acquirer.registration.verified is True
    s = svc.reg_store.read_summary()
    assert s["state"] == "ACQUIRED"


def test_adopted_plane_with_expired_donor_returns_to_bootstrap(tmp_path, caplog):
    """New Important (re-review, fix round 2): CONFLICT requires siblings
    to disagree WITH.  The C1 fix (excluding an adopted `own` from
    fusion_inputs) meant that once the donor's file goes stale (or
    disappears), fusion_inputs is empty, fuse_registrations([]) is None,
    and the old code read that as "channels disagree" -- publishing
    CONFLICT and the label plane every minute forever, with a WARNING
    naming nothing (empty parentheses), because ACQUIRED short-circuits
    offer_minute and a label-plane ensemble never reaches corroborate.
    An orphaned adopted plane must instead reset to BOOTSTRAP so the
    channel tries its own signal (or re-adopts a fresh sibling) next
    minute."""
    svc = _service(tmp_path)
    clock = [1_000_000.0]
    svc.reg_store = RegistrationStore(
        tmp_path / "reg", tmp_path / "registration.json", time_fn=lambda: clock[0]
    )
    from hf_timestd.core.registration_acquirer import Registration

    # minute 1: this channel hears nothing and adopts a sibling's plane.
    sib = Registration(
        counter_epoch_id=svc.epoch_tracker.observe(**_pair(0)),
        rtp_ref=1_000_000,
        utc_ref=T0,
        sample_rate=SR,
        sigma_ms=0.9,
        channel="WWV_20000",
        counter_map=_SIB_MAP,
    )
    svc.reg_store.write_channel(sib, "ACQUIRED", {})
    rng = np.random.default_rng(3)
    noise1 = 0.1 * rng.standard_normal(62 * SR)
    bt1 = svc.apply_registration(
        label_timing(T0, 0.3, SR), noise1, 1_000_000, MIN, _meta(0, 0.3)
    )
    assert bt1.origin_source == "acquired"
    assert svc.acquirer.registration.channel == "SHARED_10000"
    assert svc.acquirer.registration.method == "adopted"

    # advance the clock past the sibling file's staleness window (300 s).
    clock[0] += 301.0

    # minute 2: the donor's file is now stale -> read_siblings drops it ->
    # fusion_inputs is empty -> ORPHAN, not CONFLICT.
    noise2 = 0.1 * rng.standard_normal(62 * SR)
    with caplog.at_level("WARNING", logger="hf_timestd.core.metrology_service"):
        bt2 = svc.apply_registration(
            label_timing(T0 + 60, 0.3, SR),
            noise2,
            1_000_000 + 60 * SR,
            MIN + 60,
            _meta(1, 0.3),
        )
    assert bt2.origin_source == "label"
    s = svc.reg_store.read_summary()
    assert s["state"] == "BOOTSTRAP"
    assert svc.acquirer.state == RegistrationAcquirer.STATE_BOOTSTRAP
    # no "sibling registrations disagree ... ()" warning naming nothing
    assert not any("own plane ()" in r.getMessage() for r in caplog.records)
    assert not any(
        r.levelno >= logging.WARNING and "CONFLICT" in r.getMessage()
        for r in caplog.records
    )

    # minute 3: audible ticks -> re-acquires from this channel's OWN signal.
    audio3 = make_tick_audio(62, SR, T0 + 120, {"WWV": 0.0125}, snr_db=20.0, seed=9)
    bt3 = svc.apply_registration(
        label_timing(T0 + 120, 0.0, SR),
        audio3,
        1_000_000 + 120 * SR,
        MIN + 120,
        _meta(2, 0.0),
    )
    assert bt3.origin_source == "acquired"
    assert svc.acquirer.state == RegistrationAcquirer.STATE_ACQUIRED
    assert svc.acquirer.registration.method != "adopted"


def test_host_label_ensemble_does_not_reach_corroborate(tmp_path, monkeypatch):
    """I3, widened by Task 10: no detector ensemble reaches ``verify`` or
    ``corroborate`` any more, whatever anchored it.  Once only the
    host-label anchor was filtered out; now the self-measurement is the
    sole route.  That includes a marker-anchored ensemble reporting an
    unwrapped whole second (Task 9 review, ruling 4): it carries none of
    the acquirer's SNR and two-minute bars, and must never reach the
    whole-second guard."""
    svc = _service(tmp_path)
    _minute(svc, 0)
    _minute(svc, 1)
    held = svc.acquirer.registration
    utc_ref = held.utc_ref
    seen = []
    monkeypatch.setattr(svc.acquirer, "corroborate", lambda res: seen.append(res) or "held")
    monkeypatch.setattr(svc.acquirer, "verify", lambda res: seen.append(res) or "pending")
    ensembles = [
        SimpleNamespace(station="WWV", ensemble_timing_error_ms=40.0,
                        sigma_single_ms=0.5, anchor_source=src)
        for src in ("host_label", "acquired", "minute_marker")
    ] + [
        SimpleNamespace(station="WWV", ensemble_timing_error_ms=1000.0,
                        sigma_single_ms=0.01, anchor_source="minute_marker")
    ]
    svc.feed_back_ensembles(ensembles)
    assert seen == []
    assert svc.acquirer.registration is held and held.utc_ref == utc_ref


def test_counter_epoch_change_on_the_live_ring_reacquires(tmp_path):
    """I3: a genuine radiod re-anchor mid-stream resets the acquirer to
    BOOTSTRAP and, at good SNR, lets it re-acquire within the same minute --
    exercised through the service's own apply_registration, not just the
    acquirer directly.

    final review, I3: the jump here used to be 0.7 s, which is B4's
    measured PAIR SKEW, not a counter-space change -- the tracker no longer
    opens an epoch for lateness alone.  A real re-anchor moves the mapping
    by hours, so this asks for 19.4 h."""
    svc = _service(tmp_path)
    audio0 = make_tick_audio(62, SR, T0, {"WWV": 0.0125}, snr_db=20.0)
    bt0 = svc.apply_registration(
        label_timing(T0, 0.0, SR), audio0, 1_000_000, MIN, _meta(0, 0.0)
    )
    assert bt0.origin_source == "acquired"
    epoch0 = svc.acquirer.registration.counter_epoch_id
    meta1 = dict(_meta(1))
    # radiod re-anchors 19.4 h away -> a new counter space.  Task 10: the
    # epoch tracker observes radiod's raw pair, so that is where it moves.
    meta1["raw_gps_time_ns"] = meta1["raw_gps_time_ns"] + int(69732e9)
    audio1 = make_tick_audio(62, SR, T0 + 60, {"WWV": 0.0125}, snr_db=20.0, seed=8)
    bt1 = svc.apply_registration(
        label_timing(T0 + 60, 0.0, SR),
        audio1,
        1_000_000 + 60 * SR,
        MIN + 60,
        meta1,
    )
    assert svc.acquirer.registration is not None
    epoch1 = svc.acquirer.registration.counter_epoch_id
    assert epoch1 != epoch0
    assert bt1.origin_source == "acquired"  # re-acquired within the same minute


def test_no_timing_buffer_returns_unchanged_with_no_store_write(
    tmp_path, monkeypatch, caplog
):
    """I3: the no_timing guard -- returned unchanged, no store write, and
    the acquirer never even offered the minute.

    Given teeth (fix round 2): a 10-sample buffer let the *previous*
    version of this test pass for the wrong reason -- with the guard
    deleted, the unsafe path raised ValueError out of sosfiltfilt on the
    too-short buffer, and I1's blanket except caught it and returned the
    same object, so the assertions held even with no guard at all.  A
    full 62 s clean 20 dB audio buffer means nothing raises on its own;
    offer_minute is monkeypatched to explode if the guard is ever
    removed; and asserting no ERROR was logged rules out "the guard is
    gone but I1's except silently saved us" as an explanation for a pass.
    """
    svc = _service(tmp_path)

    def _must_not_be_offered(*a, **k):
        raise AssertionError("must not be offered")

    monkeypatch.setattr(svc.acquirer, "offer_minute", _must_not_be_offered)
    bt_in = BufferTiming(
        sample0_utc=0.0,
        sample_rate=SR,
        source="no_timing",
        n_snapshots_used=0,
        jitter_ms=float("inf"),
    )
    audio = make_tick_audio(62, SR, T0, {"WWV": 0.0125}, snr_db=20.0)
    with caplog.at_level("ERROR", logger="hf_timestd.core.metrology_service"):
        bt_out = svc.apply_registration(bt_in, audio, 1_000_000, MIN, _meta(0))
    assert bt_out is bt_in
    assert svc.reg_store.read_summary() is None
    assert not (tmp_path / "reg").exists()
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)


def test_resolve_before_adopt_precedence(tmp_path, monkeypatch):
    """I3: resolve-before-adopt precedence -- monkeypatch adopt to raise;
    the ambiguity-resolution test must still pass because adopt is never
    reached when resolve_ambiguity already named the station."""
    svc = _service(tmp_path)

    class _Eng(_Engine):
        def expected_delays_s(self, system_time, utc_minute):
            return {"WWV": 0.0125, "BPM": 0.0465}

    svc.engine = _Eng()
    from hf_timestd.core.registration_acquirer import Registration

    epoch = svc.epoch_tracker.observe(**_pair(0))
    sib = Registration(
        epoch,
        rtp_ref=1_000_000,
        utc_ref=T0 + 0.0005,
        sample_rate=SR,
        sigma_ms=0.9,
        channel="WWV_20000",
        counter_map=_SIB_MAP,
        stations=("WWV",),
    )
    svc.reg_store.write_channel(sib, "ACQUIRED", {})

    def _boom(reg):
        raise AssertionError(
            "adopt must not run when resolve_ambiguity already acquired"
        )

    monkeypatch.setattr(svc.acquirer, "adopt", _boom)
    audio = make_tick_audio(62, SR, T0, {"WWV": 0.0125}, snr_db=20.0)
    bt = svc.apply_registration(
        label_timing(T0, 0.1, SR), audio, 1_000_000, MIN, _meta(0, 0.1)
    )
    assert bt.origin_source == "acquired" and bt.sample0_utc == pytest.approx(
        T0, abs=0.002
    )


# ── C1 (final review): fusion crosses PROCESSES, not id strings ───────


def test_sibling_in_the_same_counter_space_under_a_different_id_still_fuses(tmp_path):
    """C1: metrology runs one process per channel, each with its own
    CounterEpochTracker, so two channels in ONE physical counter epoch
    routinely write two different ``counter_epoch_id`` strings.  The old
    majority-string filter then discarded every channel but one, and per-radiod
    inverse-variance fusion (spec §4.6) never happened in production at all.
    Two 1.0 ms-class planes in one counter space must fuse tighter than
    either, and ``contributing`` must name both."""
    from hf_timestd.core.registration_acquirer import Registration

    svc = _service(tmp_path)
    audio = make_tick_audio(62, SR, T0, {"WWV": 0.0125}, snr_db=20.0)
    svc.apply_registration(label_timing(T0, 0.0, SR), audio, 1_000_000, MIN, _meta(0, 0.0))
    svc.acquirer.registration.verified = True
    own_sigma = svc.acquirer.registration.sigma_ms
    own_offset = svc.epoch_tracker.epoch_offset_s
    assert svc.acquirer.registration.epoch_offset_s == pytest.approx(own_offset)

    sib = Registration(
        "ep-named-by-another-process",  # a DIFFERENT string, same counter space
        rtp_ref=1_000_000,
        utc_ref=T0 + 0.0005,
        sample_rate=SR,
        sigma_ms=1.0,
        channel="WWV_20000",
        counter_map=_SIB_MAP,
        stations=("WWV",),
        verified=True,
        epoch_offset_s=own_offset + 0.0019,  # the measured cross-channel spread
    )
    svc.reg_store.write_channel(sib, "ACQUIRED", {})

    audio1 = make_tick_audio(62, SR, T0 + 60, {"WWV": 0.0125}, snr_db=20.0, seed=8)
    bt = svc.apply_registration(
        label_timing(T0 + 60, 0.0, SR), audio1, 1_000_000 + 60 * SR, MIN + 60, _meta(1, 0.0)
    )
    assert bt.origin_source == "acquired"
    s = svc.reg_store.read_summary()
    assert sorted(s["contributing"]) == ["SHARED_10000", "WWV_20000"]
    assert s["sigma_ms"] < min(own_sigma, 1.0)
    assert s["epoch_offset_s"] == pytest.approx(own_offset, abs=1e-6)


def test_contributing_names_only_what_fusion_kept(tmp_path):
    """C1's third consequence: ``contributing`` was built from every sibling
    read off disk, so the summary claimed corroboration from a channel the
    outlier test had just thrown out."""
    from hf_timestd.core.registration_acquirer import Registration

    svc = _service(tmp_path)
    audio = make_tick_audio(62, SR, T0, {"WWV": 0.0125}, snr_db=20.0)
    svc.apply_registration(label_timing(T0, 0.0, SR), audio, 1_000_000, MIN, _meta(0, 0.0))
    svc.acquirer.registration.verified = True
    own_offset = svc.epoch_tracker.epoch_offset_s
    for ch, delta in (("WWV_20000", 0.0005), ("WWV_25000", 0.020)):
        svc.reg_store.write_channel(
            Registration(
                "ep-x",
                rtp_ref=1_000_000,
                utc_ref=T0 + delta,
                sample_rate=SR,
                sigma_ms=1.0,
                channel=ch,
                counter_map=_SIB_MAP,
                stations=("WWV",),
                verified=True,
                epoch_offset_s=own_offset,
            ),
            "ACQUIRED",
            {},
        )
    audio1 = make_tick_audio(62, SR, T0 + 60, {"WWV": 0.0125}, snr_db=20.0, seed=8)
    svc.apply_registration(
        label_timing(T0 + 60, 0.0, SR), audio1, 1_000_000 + 60 * SR, MIN + 60, _meta(1, 0.0)
    )
    s = svc.reg_store.read_summary()
    assert sorted(s["contributing"]) == ["SHARED_10000", "WWV_20000"]
    assert "WWV_25000" not in s["contributing"]


# ── I4 (final review): an unverified witness plane must re-acquire ────


def test_witness_plane_that_disagrees_with_t6_is_kept_and_recorded(tmp_path, caplog):
    """Task 10 reverses I4.  T3 stands alone: a plane its own ticks
    verify stays a WITNESS however far T6's plane sits from it.  The
    residual against T6 is recorded every minute, and nothing resets on
    it.  (Was: VERIFY_MAX_MINUTES of disagreement with T6 reset the plane,
    B4's 77 resets in 6 hours.)"""
    svc = _service(tmp_path)
    caplog.set_level(logging.INFO)
    n = RegistrationAcquirer.VERIFY_MAX_MINUTES + 2
    for k in range(n):
        _minute(svc, k, walk=0.040, raw_walk=0.0, t6=True)  # T6 says +40 ms
        s = svc.reg_store.read_summary()
        assert s["residual_vs_t6_ms"] == pytest.approx(-40.0, abs=1.0)
        assert s["state"] == ("CANDIDATE" if k == 0 else "WITNESS")
    assert svc.acquirer.state == RegistrationAcquirer.STATE_ACQUIRED
    assert "registration reset" not in caplog.text


def test_unverified_witness_plane_whose_ticks_disagree_reacquires(tmp_path):
    """What I4 guarded, now on the plane's own evidence: an unverified
    witness plane must not park.  A plane 40 ms off its own ticks is
    rejected in its first measured minute, and the channel re-acquires
    from its ticks on the next."""
    svc = _service(tmp_path)
    _minute(svc, 0, walk=0.0, raw_walk=0.0, t6=True)
    svc.acquirer.registration.utc_ref += 0.040
    _minute(svc, 1, walk=0.0, raw_walk=0.0, t6=True)
    assert svc.acquirer.state == RegistrationAcquirer.STATE_BOOTSTRAP
    assert svc.acquirer.registration is None
    assert svc.reg_store.read_summary()["state"] == "BOOTSTRAP"
    _minute(svc, 2, walk=0.0, raw_walk=0.0, t6=True)
    assert svc.acquirer.state == RegistrationAcquirer.STATE_ACQUIRED
    assert svc.acquirer.registration.sample0_utc_for(1_000_000 + 2 * 60 * SR) == (
        pytest.approx(T0 + 120, abs=0.002))


def test_witness_plane_that_agrees_with_t6_is_never_reset(tmp_path):
    """The companion case: a verified witness plane must survive
    indefinitely -- the residual against T6 is the free measurement of the
    acquisition method's accuracy that spec §6 asks for.  Task 10: the
    acquisition minute is a CANDIDATE; every minute after it a WITNESS."""
    svc = _service(tmp_path)
    for k in range(RegistrationAcquirer.VERIFY_MAX_MINUTES + 2):
        _minute(svc, k, walk=0.0008, raw_walk=0.0, t6=True)  # T6 says +0.8 ms
        assert svc.reg_store.read_summary()["state"] == (
            "CANDIDATE" if k == 0 else "WITNESS")
    assert svc.acquirer.state == RegistrationAcquirer.STATE_ACQUIRED


def test_a_witness_plane_that_comes_back_into_agreement_is_not_reset(tmp_path):
    """T6 wandering out of agreement and back never resets the plane, not
    even after VERIFY_MAX_MINUTES consecutive minutes 40 ms away (Task 10:
    nothing T6 says ever does)."""
    svc = _service(tmp_path)
    n = RegistrationAcquirer.VERIFY_MAX_MINUTES
    walks = [0.040] * (n + 1) + [0.0008] * n
    for k, walk in enumerate(walks):
        _minute(svc, k, walk=walk, raw_walk=0.0, t6=True)
    assert svc.acquirer.state == RegistrationAcquirer.STATE_ACQUIRED
    assert svc.reg_store.read_summary()["state"] == "WITNESS"


def test_a_waiting_channel_is_named_in_the_summary(tmp_path):
    """16c: the newcomer's exclusion must be visible, not silent -- a
    reader (station-web, the sidecar) sees which channels are serving
    their two corroborated minutes outside the fused plane."""
    from hf_timestd.core.registration_acquirer import Registration
    from hf_timestd.core.registration_store import ADOPT_MIN_CORROBORATED_MINUTES

    assert ADOPT_MIN_CORROBORATED_MINUTES == 2
    svc = _service(tmp_path)
    # this channel acquires and verifies its own plane first (Task 10: on
    # its own ticks, in minute 1), with no sibling on disk to adopt:
    # verified, nothing corroborated yet
    _minute(svc, 0)
    _minute(svc, 1)
    assert svc.acquirer.registration.verified is True
    assert svc.acquirer.registration.n_minutes == 0
    # NOW a long-corroborated sibling appears, same counter space
    svc.reg_store.write_channel(
        Registration(
            svc.epoch_tracker.observe(**_pair(0)),
            rtp_ref=1_000_000,
            utc_ref=T0,
            sample_rate=SR,
            sigma_ms=1.0,
            n_minutes=17,
            channel="WWV_20000",
            counter_map=_SIB_MAP,
            stations=("WWV",),
            verified=True,
        ),
        "ACQUIRED",
        {},
    )

    _minute(svc, 2)
    s = svc.reg_store.read_summary()
    assert s["waiting"] == ["SHARED_10000"], "this channel corroborated too little yet"
    assert s["contributing"] == ["WWV_20000"]
    assert s["n_minutes"] == 17
    assert s["state"] == "ACQUIRED"


def test_every_summary_carries_a_waiting_key(tmp_path):
    """16c: present on every summary, empty when nobody waits."""
    svc = _service(tmp_path)
    audio = make_tick_audio(62, SR, T0, {"WWV": 0.0125}, snr_db=20.0)
    svc.apply_registration(label_timing(T0, 0.0, SR), audio, 1_000_000, MIN, _meta(0, 0.0))
    assert svc.reg_store.read_summary()["waiting"] == []
    svc.reg_store.write_summary(None, [], "BOOTSTRAP", {})
    assert svc.reg_store.read_summary().get("waiting", []) == []


def test_every_summary_carries_a_conflicts_key(tmp_path):
    """I6: readers (station-web, the provenance sidecar) can rely on the
    key being there, empty when nothing disagrees."""
    svc = _service(tmp_path)
    audio = make_tick_audio(62, SR, T0, {"WWV": 0.0125}, snr_db=20.0)
    svc.apply_registration(label_timing(T0, 0.0, SR), audio, 1_000_000, MIN, _meta(0, 0.0))
    assert svc.reg_store.read_summary()["conflicts"] == []


def test_conflict_does_not_hide_the_summary_state_from_the_bench(tmp_path):
    """I6: HfAcquiredBench is unchanged -- it answers on ACQUIRED and
    WITNESS and stays silent on anything else.  A CONFLICT summary used to
    silence it for the whole station because of one channel; now the state
    it reads is the one the fused result justifies, and the conflict is
    named beside it."""
    from hf_timestd.core.offset_judge import HfAcquiredBench

    svc = _service(tmp_path)
    svc.reg_store.write_summary(
        None, [], "BOOTSTRAP", {"conflicts": ["SHARED_10000", "WWV_20000"]}
    )
    bench = HfAcquiredBench(
        provider=lambda: {"SHARED_10000": (1_000_000, 1000.0, SR)},
        store=svc.reg_store,
        mono_fn=lambda: 1000.0,
    )
    assert bench.poll() is None
    assert svc.reg_store.read_summary()["conflicts"] == [
        "SHARED_10000",
        "WWV_20000",
    ]


# ── M1 (final review): the two edge-result attributes, named apart ────


def test_process_minute_data_feeds_back_the_flat_edge_result_list(tmp_path):
    """M1: ``MetrologyEngine`` carries ``_last_edge_results`` (a dict keyed
    by station, for the tick writer) and ``edge_results_this_minute`` (the
    flat list the acquirer's feed-back path wants).  Those two used to
    differ by a single underscore, and ``_process_minute_data`` reads both
    a few dozen lines apart -- a name that invited exactly the wrong
    getattr, silently starving verify()/corroborate().  This walks the real
    seam with a stub engine that exposes ONLY the flat list, so a rename on
    one side and not the other fails here."""
    svc = _service(tmp_path)
    seen = []
    svc.feed_back_ensembles = seen.append
    svc.writer = None
    svc.tick_writer = None
    svc.channel_name = "SHARED_10000"
    ensembles = [SimpleNamespace(station="WWV", anchor_source="acquired")]

    class _StubEngine(_Engine):
        edge_results_this_minute = ensembles
        _last_edge_results = {}

        def process_minute(self, **kwargs):
            return []

    svc.engine = _StubEngine()
    svc._process_minute_data(
        minute_boundary=MIN,
        iq_samples=np.zeros(10),
        system_time=float(MIN),
        rtp_timestamp=1_000_000,
        metadata=_meta(0),
        buffer_timing=label_timing(T0, 0.0, SR),
    )
    assert seen == [ensembles]


# ── Task 16b: adoption hysteresis — two corroborated minutes ──────────


def _run_minute(svc, k):
    """One live minute: apply (Task 10: the self-measurement verifies or
    corroborates inside it) and read the summary's verdict."""
    import time as _time

    from hf_timestd.core.registration_store import registration_refusal

    _minute(svc, k)
    s = svc.reg_store.read_summary()
    return (
        s["state"],
        s["n_minutes"],
        registration_refusal(s, now=_time.time(), sample_rate=SR),
    )


def test_adoption_waits_for_two_corroborated_minutes(tmp_path):
    """16b: on ND at 20:36Z the recorder anchored the ring and the FUSE
    feed on a plane in its FIRST minute (n_minutes 0), and corroboration
    only threw it out six minutes later -- by which time FUSE had told
    chrony the host ran 23-28 ms fast.  A plane must now show
    ADOPT_MIN_CORROBORATED_MINUTES corroborated minutes before any
    surface may act on it.  Task 10: the channel's own ticks verify
    (minute 1) and corroborate (minutes 2, 3)."""
    from hf_timestd.core.registration_store import (
        ADOPT_MIN_CORROBORATED_MINUTES,
        registration_is_authoritative,
    )

    assert ADOPT_MIN_CORROBORATED_MINUTES == 2
    svc = _service(tmp_path)
    seen = [_run_minute(svc, k) for k in range(4)]
    assert [v[0] for v in seen] == ["CANDIDATE", "ACQUIRED", "ACQUIRED", "ACQUIRED"]
    assert [v[1] for v in seen] == [0, 0, 1, 2]
    assert [v[2] for v in seen] == [
        "state:CANDIDATE",
        "uncorroborated",
        "uncorroborated",
        None,
    ]
    import time as _time

    assert registration_is_authoritative(
        svc.reg_store.read_summary(), now=_time.time(), sample_rate=SR
    )


def test_a_reset_stands_every_surface_down_the_same_minute(tmp_path):
    """16b: a plane the acquirer gives up on must not keep labelling for
    the rest of the minute.  The summary drops to BOOTSTRAP in the minute
    the reset happens, and no publish along the way was authoritative.
    Task 10: the plane drifts 40 ms off its own ticks after verification;
    the first far minute holds, the second gives the plane up."""
    import time as _time

    from hf_timestd.core.registration_store import registration_refusal

    svc = _service(tmp_path)
    seen = [_run_minute(svc, 0), _run_minute(svc, 1)]  # acquire, verify
    svc.acquirer.registration.utc_ref += 0.040  # tick-like, but 40 ms off
    seen.append(_run_minute(svc, 2))  # first far minute: held
    assert svc.acquirer.state == RegistrationAcquirer.STATE_ACQUIRED
    seen.append(_run_minute(svc, 3))  # second: reacquire
    assert [v[2] for v in seen] == [
        "state:CANDIDATE",
        "uncorroborated",
        "uncorroborated",
        "state:BOOTSTRAP",
    ]
    assert svc.acquirer.state == RegistrationAcquirer.STATE_BOOTSTRAP
    s = svc.reg_store.read_summary()
    assert s["state"] == "BOOTSTRAP", "the summary must stand down the SAME minute"
    assert registration_refusal(s, now=_time.time()) == "state:BOOTSTRAP"
