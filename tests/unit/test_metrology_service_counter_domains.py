"""Task 7: metrology hands each registration its own counter.

Since ka9q-radio e5f790dd every radiod channel starts its own RTP counter
(ND, 2026-10-06: k0 = 51, 51, 51, 51, 50), so one instant carries a
different RTP on each channel.  These tests run two channels one block
apart through ``MetrologyService`` and one shared store, and check that
every plane the service adopts, resolves, compares or holds names the
counter it lives in.
"""

import logging

import numpy as np
import pytest

from hf_timestd.core.buffer_timing import unix_ns_to_gps_time_ns
from hf_timestd.core.counter_epoch_tracker import CounterEpochTracker
from hf_timestd.core.registration_acquirer import Registration, RegistrationAcquirer
from tests.unit.synth_origins import EPOCH_GPS_S, counter_map_for, olen_for
from tests.unit.synth_ticks import label_timing, make_tick_audio
from tests.unit.test_metrology_service_registration import (
    MIN,
    SR,
    T0,
    _Engine,
    _service,
)

OLEN = olen_for(SR)  # 480 output samples per 20 ms block at 24 kHz
ACQ_LOG = "hf_timestd.core.registration_acquirer"
SVC_LOG = "hf_timestd.core.metrology_service"


def _start(k0: int, k: int) -> int:
    """The RTP, in a channel that started at block ``k0``, of minute ``k``'s
    first sample.  One instant for every channel: a channel that started one
    block later reads one block (480 samples) less."""
    return 1_000_000 + (50 - k0) * OLEN + k * 60 * SR


def _meta(k0: int, k: int, cmap=None, walk: float = 0.0) -> dict:
    """``walk``: radiod's raw pair puts the buffer's first sample this many
    seconds after truth (Task 10: T3 starts from the raw pair)."""
    utc_ns = int(round(T0)) * 10**9 + 60 * k * 10**9 + int(round(walk * 1e9))
    return {
        "gps_time_ns": 1_000_000_000_000 + k * 60_000_000_000,
        "rtp_timesnap": _start(k0, k),
        "raw_gps_time_ns": unix_ns_to_gps_time_ns(utc_ns),
        "raw_rtp_timesnap": _start(k0, k),
        "sample_rate": SR,
        "counter_map": counter_map_for(k0, SR) if cmap is None else cmap,
    }


def _channel(tmp_path, name: str):
    svc = _service(tmp_path)
    svc.channel_name = name
    svc.acquirer = RegistrationAcquirer(name, SR)
    return svc


def _noise(seed: int) -> np.ndarray:
    return 0.1 * np.random.default_rng(seed).standard_normal(62 * SR)


def _ticks(k: int, seed: int = 7) -> np.ndarray:
    return make_tick_audio(62, SR, T0 + 60 * k, {"WWV": 0.0125}, snr_db=20.0, seed=seed)


def _acquire(svc, k0: int):
    """Minute 0: this channel acquires from its own ticks; mark it
    verified.  Task 10 verifies on the channel's own ticks one minute
    later (test_metrology_service_registration covers that path); these
    tests start their own minute 1, so the setup stamps the outcome."""
    svc.apply_registration(
        label_timing(T0, 0.250, SR), _ticks(0), _start(k0, 0), MIN, _meta(k0, 0, walk=0.250)
    )
    assert svc.acquirer.state == RegistrationAcquirer.STATE_ACQUIRED
    svc.acquirer.registration.verified = True


def _sibling(channel, k0, k, utc, **kw) -> Registration:
    rtp = _start(k0, k)
    return Registration(
        "ep-sibling",
        rtp_ref=rtp,
        utc_ref=utc,
        sample_rate=SR,
        sigma_ms=0.9,
        channel=channel,
        stations=("WWV",),
        verified=True,
        counter_map=counter_map_for(k0, SR).rolled_forward(rtp),
        rtp_domain=channel,
        **kw,
    )


def test_adopted_sibling_plane_is_right_in_the_adopters_counter(tmp_path):
    """WWV_20000 (k0 = 50) acquires from its ticks; SHARED_10000 (k0 = 51)
    hears noise only and adopts.  The adopted plane must name the same
    instant in SHARED_10000's own counter.  Read at raw RTP, it lands one
    block (20 ms) off."""
    svc_a = _channel(tmp_path, "WWV_20000")
    svc_b = _channel(tmp_path, "SHARED_10000")
    _acquire(svc_a, 50)
    svc_a.apply_registration(
        label_timing(T0 + 60, 0.250, SR), _ticks(1, seed=8), _start(50, 1), MIN + 60,
        _meta(50, 1, walk=0.250),
    )
    truth_a = svc_a.acquirer.registration.sample0_utc_for(_start(50, 1))
    assert truth_a == pytest.approx(T0 + 60, abs=0.002)

    bt = svc_b.apply_registration(
        label_timing(T0 + 60, 0.3, SR), _noise(3), _start(51, 1), MIN + 60, _meta(51, 1, walk=0.3)
    )
    reg = svc_b.acquirer.registration
    assert reg is not None and reg.method == "adopted"
    assert reg.sample0_utc_for(_start(51, 1)) == pytest.approx(truth_a, abs=1e-6)
    assert bt.origin_source == "acquired"
    assert bt.sample0_utc == pytest.approx(truth_a, abs=1e-6)
    # the adopter's plane names its own counter, with a map rolled to it
    assert reg.rtp_domain == "SHARED_10000"
    assert reg.counter_map == counter_map_for(51, SR).rolled_forward(reg.rtp_ref)
    # both files carry the counter, so a third channel could convert either
    import json

    a_file = json.loads((tmp_path / "reg" / "WWV_20000.json").read_text())
    assert a_file["state"] == "ACQUIRED"
    assert a_file["rtp_domain"] == "WWV_20000"
    assert a_file["counter_map"] is not None
    summary = svc_b.reg_store.read_summary()
    assert summary["rtp_domain"] == "SHARED_10000"
    assert summary["counter_map"] is not None


def test_sibling_resolves_ambiguity_in_its_own_counter(tmp_path):
    """As ``test_shared_channel_ambiguity_resolved_by_same_site_sibling``,
    but the sibling started one block earlier (k0 50 against 51).  Read at
    raw RTP the sibling's plane sits 20 ms off, past SAME_SITE_AGREE_MS,
    and the ambiguity stays open."""
    svc = _channel(tmp_path, "SHARED_10000")

    class _Eng(_Engine):
        def expected_delays_s(self, system_time, utc_minute):
            return {"WWV": 0.0125, "BPM": 0.0465}  # shared band, 34 ms apart

    svc.engine = _Eng()
    svc.reg_store.write_channel(_sibling("WWV_20000", 50, 0, T0), "ACQUIRED", {})
    bt = svc.apply_registration(
        label_timing(T0, 0.1, SR), _ticks(0), _start(51, 0), MIN, _meta(51, 0, walk=0.1)
    )
    reg = svc.acquirer.registration
    assert reg is not None and reg.method != "adopted"
    assert reg.stations == ("WWV",)  # resolved, not merely adopted
    # The resolved plane is this channel's own fold (the sibling names only
    # the station and the whole second), which reads this synthetic minute
    # 0.6 ms late; 1 ms bounds that and still sits 19 ms inside the block.
    assert reg.sample0_utc_for(_start(51, 0)) == pytest.approx(T0, abs=0.001)
    assert bt.origin_source == "acquired"


def test_counter_map_step_resets_the_plane(tmp_path, caplog):
    """A map one block away on the same radiod run means this channel's
    counter no longer maps where the held plane assumed: the plane is 20 ms
    wrong.  It must reset, loudly, naming the step."""
    svc = _channel(tmp_path, "SHARED_10000")
    _acquire(svc, 51)
    stepped = counter_map_for(52, SR)  # same run, intercept one block later
    with caplog.at_level(logging.INFO, logger=ACQ_LOG):
        svc.apply_registration(
            label_timing(T0 + 60, 0.3, SR), _noise(4), _start(51, 1), MIN + 60,
            _meta(51, 1, cmap=stepped, walk=0.3),
        )
    resets = [r for r in caplog.records if "registration reset" in r.getMessage()]
    assert len(resets) == 1
    assert "counter map moved 1 block" in resets[0].getMessage()
    assert resets[0].levelno >= logging.WARNING
    assert svc.acquirer.state == RegistrationAcquirer.STATE_BOOTSTRAP
    assert svc.acquirer.registration is None


def test_late_gps_time_on_the_map_keeps_the_plane(tmp_path, caplog):
    """The counters still agree, but the map's epoch reads 0.7 s late, as
    a late GPS_TIME packet makes it (B4 shows up to 816 ms).
    ``CounterMap.same_run`` alone calls that another run; counter
    continuity does not.  The plane stays, and carries the new map rolled
    to its own ``rtp_ref``."""
    svc = _channel(tmp_path, "SHARED_10000")
    _acquire(svc, 51)
    held = svc.acquirer.registration
    late = counter_map_for(51, SR, epoch_gps_s=EPOCH_GPS_S + 0.7)
    assert not counter_map_for(51, SR).same_run(late)  # the trap
    with caplog.at_level(logging.INFO, logger=ACQ_LOG):
        svc.apply_registration(
            label_timing(T0 + 60, 0.250, SR), _ticks(1, seed=8), _start(51, 1),
            MIN + 60, _meta(51, 1, cmap=late, walk=0.250),
        )
    assert not any("registration reset" in r.getMessage() for r in caplog.records)
    reg = svc.acquirer.registration
    assert reg is held
    assert reg.counter_map == late.rolled_forward(reg.rtp_ref)
    assert reg.rtp_domain == "SHARED_10000"


def test_counter_map_from_another_radiod_run_resets_the_plane(tmp_path, caplog):
    """Same start block, so the counters map identically, but the map's
    epoch sits an hour away: radiod restarted.  The held plane belongs to
    the old run and must reset."""
    svc = _channel(tmp_path, "SHARED_10000")
    _acquire(svc, 51)
    restarted = counter_map_for(51, SR, epoch_gps_s=EPOCH_GPS_S + 3600.0)
    # The raw pair moves with the run (final review I-4: a map that names
    # another run than the raw pair reads as absent).
    meta = _meta(51, 1, cmap=restarted, walk=0.3)
    meta["raw_gps_time_ns"] += 3600 * 10**9
    with caplog.at_level(logging.INFO, logger=ACQ_LOG):
        svc.apply_registration(
            label_timing(T0 + 60, 0.3, SR), _noise(5), _start(51, 1), MIN + 60,
            meta,
        )
    resets = [r for r in caplog.records if "registration reset" in r.getMessage()]
    assert len(resets) == 1
    assert "another radiod run" in resets[0].getMessage()
    assert svc.acquirer.registration is None


def test_conflict_deltas_are_named_in_the_callers_counter(tmp_path, caplog):
    """CONFLICT names each sibling's delta from this channel's own plane.
    A sibling at truth on another origin must read +0.0 ms, not the 20 ms
    its raw RTP implies.  Four planes: own and WWV_20000 (k0 50) at truth,
    two k0-51 siblings 10 ms late, so the median sits 5 ms from every
    member and fusion keeps none."""
    svc = _channel(tmp_path, "SHARED_10000")
    _acquire(svc, 51)
    truth = svc.acquirer.registration.sample0_utc_for(_start(51, 1))
    svc.reg_store.write_channel(_sibling("WWV_20000", 50, 1, truth), "ACQUIRED", {})
    for ch in ("WWV_15000", "WWV_25000"):
        svc.reg_store.write_channel(_sibling(ch, 51, 1, truth + 0.010), "ACQUIRED", {})
    with caplog.at_level(logging.WARNING, logger=SVC_LOG):
        bt = svc.apply_registration(
            label_timing(T0 + 60, 0.250, SR), _ticks(1, seed=8), _start(51, 1),
            MIN + 60, _meta(51, 1, walk=0.250),
        )
    assert bt.origin_source == "label"
    warns = [r.getMessage() for r in caplog.records if "disagree" in r.getMessage()]
    assert len(warns) == 1
    assert "WWV_20000=+0.0ms" in warns[0]
    assert "WWV_15000=+10.0ms" in warns[0] and "WWV_25000=+10.0ms" in warns[0]
    s = svc.reg_store.read_summary()
    assert s["conflicts"] == ["SHARED_10000", "WWV_15000", "WWV_20000", "WWV_25000"]


# ── Fix round 1 ───────────────────────────────────────────────────────


def _adopter_with_sibling(tmp_path, sib_map_epoch_s=EPOCH_GPS_S):
    """SHARED_10000 (k0 51) hears noise; WWV_20000 (k0 50) holds a verified
    plane at T0 + 60 for minute 1, with its counter map."""
    svc = _channel(tmp_path, "SHARED_10000")
    rtp = _start(50, 1)
    sib = _sibling("WWV_20000", 50, 1, T0 + 60)
    sib.counter_map = counter_map_for(50, SR, epoch_gps_s=sib_map_epoch_s).rolled_forward(rtp)
    # the adopter's own epoch id, so adoption itself never trips the
    # acquirer's counter-epoch reset and hides what is under test
    meta = _meta(51, 1)
    sib.counter_epoch_id = CounterEpochTracker().observe(
        meta["raw_gps_time_ns"], meta["raw_rtp_timesnap"], SR)
    svc.reg_store.write_channel(sib, "ACQUIRED", {})
    return svc, sib


def test_withdrawn_map_stops_every_sibling_fusing(tmp_path, caplog):
    """I-1: the recorder publishes None once its map has gone stale.  The
    held map must go with it: past half the signed-32 reach a map rolled
    from its old rtp_ref rolls the wrong way and every sibling converts
    about 49.7 h off.  After the withdrawal no sibling may fuse."""
    svc, _sib = _adopter_with_sibling(tmp_path)
    bt1 = svc.apply_registration(
        label_timing(T0 + 60, 0.3, SR), _noise(3), _start(51, 1), MIN + 60, _meta(51, 1, walk=0.3)
    )
    assert bt1.origin_source == "acquired"  # adopted while the map stood
    for k in (2, 3, 4):
        meta = _meta(51, k)
        meta["counter_map"] = None
        bt = svc.apply_registration(
            label_timing(T0 + 60 * k, 0.3, SR), _noise(10 + k), _start(51, k),
            MIN + 60 * k, meta,
        )
        assert bt.origin_source == "label"
        s = svc.reg_store.read_summary()
        assert s["contributing"] == []
        assert s["state"] == "BOOTSTRAP"
    assert svc.acquirer.counter_map is None
    assert svc.acquirer.registration is None


def test_map_out_of_reach_of_the_minute_is_refused(tmp_path):
    """I-1 backstop: a map whose rtp_ref sits more than 2**30 samples from
    this minute's start RTP is not used, even if it keeps arriving."""
    svc, _sib = _adopter_with_sibling(tmp_path)
    stale = counter_map_for(51, SR)  # rtp_ref near 23520
    far_start = (stale.rtp_ref + 2**30 + 60 * SR) % 2**32
    bt = svc.apply_registration(
        label_timing(T0 + 60, 0.3, SR), _noise(3), far_start, MIN + 60,
        _meta(51, 1, cmap=stale, walk=0.3),
    )
    assert bt.origin_source == "label"
    assert svc.reg_store.read_summary()["contributing"] == []


def test_held_plane_keeps_a_map_that_answers_at_its_rtp_ref(tmp_path):
    """I-2: maps keep arriving, continuous, across more than 2**31 samples
    while one plane is held.  The plane's published map must still give the
    right input-sample index at the plane's own rtp_ref."""
    acq = RegistrationAcquirer("SHARED_10000", SR)
    base = counter_map_for(51, SR)
    rtp0 = _start(51, 0)
    acq.note_counter_map(base)
    acq.adopt(Registration("ep-x", rtp_ref=rtp0, utc_ref=T0, sample_rate=SR,
                           sigma_ms=1.0, channel="fused", verified=True))
    truth = base.input_index(rtp0)
    assert acq.registration.counter_map.input_index(rtp0) == truth
    m = base
    for _ in range(12):  # 12 x 2**28 = 3 x 2**30 samples, past 2**31
        m = m.rolled_forward((m.rtp_ref + 2**28) % 2**32)
        acq.note_counter_map(m)
        assert acq.registration is not None
        assert acq.registration.counter_map.input_index(rtp0) == truth


def test_unconvertible_sibling_does_not_conflict_with_a_candidate(tmp_path, caplog):
    """I-3: an own CANDIDATE and one ACQUIRED sibling with no map.  The
    sibling cannot convert, so it is no sibling at all: the candidate is
    applied (so it can be verified), never CONFLICT.  Task 10: minute 1
    hears nothing, so the plane stays a CANDIDATE and is applied; minute 2
    verifies it on the channel's own ticks.  (Was: verified through
    ``feed_back_ensembles`` after minute 1.)"""
    svc = _channel(tmp_path, "SHARED_10000")
    svc.apply_registration(
        label_timing(T0, 0.250, SR), _ticks(0), _start(51, 0), MIN, _meta(51, 0, walk=0.250)
    )
    assert svc.acquirer.registration.verified is False
    mapless = _sibling("WWV_20000", 50, 1, T0 + 60)
    mapless.counter_map = None
    svc.reg_store.write_channel(mapless, "ACQUIRED", {})
    with caplog.at_level(logging.WARNING, logger=SVC_LOG):
        bt = svc.apply_registration(
            label_timing(T0 + 60, 0.250, SR), _noise(8), _start(51, 1),
            MIN + 60, _meta(51, 1, walk=0.250),
        )
    assert bt.origin_source == "acquired"
    assert svc.acquirer.registration.verified is False
    import json

    ch = json.loads((tmp_path / "reg" / "SHARED_10000.json").read_text())
    assert ch["state"] == "CANDIDATE"
    assert svc.reg_store.read_summary()["conflicts"] == []
    assert not any("disagree" in r.getMessage() for r in caplog.records)
    with caplog.at_level(logging.WARNING, logger=SVC_LOG):
        bt = svc.apply_registration(
            label_timing(T0 + 120, 0.250, SR), _ticks(2, seed=9), _start(51, 2),
            MIN + 120, _meta(51, 2, walk=0.250),
        )
    assert bt.origin_source == "acquired"
    assert svc.acquirer.registration.verified is True
    assert svc.reg_store.read_summary()["conflicts"] == []
    assert not any("disagree" in r.getMessage() for r in caplog.records)


def test_adopted_plane_whose_donor_loses_its_map_takes_the_donor_reset(tmp_path, caplog):
    """I-3: the donor's file is still fresh and ACQUIRED, but it no longer
    carries a map, so nothing converts.  That is a donor gone, not a
    disagreement: reset to BOOTSTRAP, never CONFLICT."""
    svc, sib = _adopter_with_sibling(tmp_path)
    svc.apply_registration(
        label_timing(T0 + 60, 0.3, SR), _noise(3), _start(51, 1), MIN + 60, _meta(51, 1, walk=0.3)
    )
    assert svc.acquirer.registration.method == "adopted"
    sib.counter_map = None
    svc.reg_store.write_channel(sib, "ACQUIRED", {})
    with caplog.at_level(logging.INFO):
        bt = svc.apply_registration(
            label_timing(T0 + 120, 0.3, SR), _noise(4), _start(51, 2), MIN + 120,
            _meta(51, 2, walk=0.3),
        )
    assert bt.origin_source == "label"
    assert svc.acquirer.state == RegistrationAcquirer.STATE_BOOTSTRAP
    assert any("donor expired" in r.getMessage() for r in caplog.records)
    import json

    ch = json.loads((tmp_path / "reg" / "SHARED_10000.json").read_text())
    assert ch["state"] != "CONFLICT"
    assert svc.reg_store.read_summary()["conflicts"] == []


def test_sibling_map_with_a_late_epoch_still_converts(tmp_path):
    """Ruling: one tolerance for "same run".  The acquirer keeps its own
    plane through a 0.7 s epoch shift (a late GPS_TIME packet); a sibling
    whose map reads 0.7 s late must convert too, not drop silently."""
    svc, _sib = _adopter_with_sibling(tmp_path, sib_map_epoch_s=EPOCH_GPS_S + 0.7)
    bt = svc.apply_registration(
        label_timing(T0 + 60, 0.3, SR), _noise(3), _start(51, 1), MIN + 60, _meta(51, 1, walk=0.3)
    )
    assert bt.origin_source == "acquired"
    assert bt.sample0_utc == pytest.approx(T0 + 60, abs=1e-6)
    assert svc.reg_store.read_summary()["contributing"] == ["WWV_20000"]


def test_map_too_far_from_the_held_one_resets_loudly(caplog):
    """I-1/I-2 companion: a new map more than 2**30 samples from the held
    one cannot be compared with it (the signed-32 reading would alias), so
    continuity cannot be shown and the held plane resets, loudly."""
    acq = RegistrationAcquirer("SHARED_10000", SR)
    base = counter_map_for(51, SR)
    acq.note_counter_map(base)
    acq.adopt(Registration("ep-x", rtp_ref=_start(51, 0), utc_ref=T0, sample_rate=SR,
                           sigma_ms=1.0, channel="fused", verified=True))
    far = base.rolled_forward((base.rtp_ref + 2**30 + 2**20) % 2**32)
    with caplog.at_level(logging.INFO, logger=ACQ_LOG):
        acq.note_counter_map(far)
    resets = [r for r in caplog.records if "registration reset" in r.getMessage()]
    assert len(resets) == 1 and "past reach" in resets[0].getMessage()
    assert resets[0].levelno >= logging.WARNING
    assert acq.registration is None


# ── Final review I-4: the ring's map lags its raw pair after a restart ──


def _map_fitting(k0: int, meta: dict, shift_s: float = 0.0):
    """Channel k0's map, its epoch set where ``meta``'s raw pair puts the
    run (less ``shift_s``): the map the recorder would publish for it."""
    import dataclasses

    from tests.unit.synth_origins import F_IN

    m = counter_map_for(k0, SR)
    implied = (meta["raw_gps_time_ns"] / 1e9
               - m.input_index(meta["raw_rtp_timesnap"]) / F_IN)
    return dataclasses.replace(m, input_epoch_gps_s=implied - shift_s)


def test_old_runs_map_beside_a_new_raw_pair_converts_no_sibling(tmp_path, caplog):
    """radiod restarted an hour into a run.  The recorder re-seeded the
    raw pair at once but still publishes the old run's map, and
    WWV_20000's file still holds a fresh plane from the old run.

    Old run: this instant sat D = 3600 s of samples later in every
    counter.  Read through the old map at the new counter, the sibling's
    plane lands an hour off; adopting it stamps garbage verified.  The
    map must read as absent until one fits the raw pair."""
    d = 3600 * SR
    svc = _channel(tmp_path, "SHARED_10000")
    meta = _meta(51, 1, walk=0.3)               # the new run's raw pair
    old_map = _map_fitting(51, meta, shift_s=3600.0)
    meta["counter_map"] = old_map
    sib_rtp = (_start(50, 1) + d) % 2**32
    sib_map = dataclasses_replace(counter_map_for(50, SR),
                                  input_epoch_gps_s=old_map.input_epoch_gps_s)
    sib = _sibling("WWV_20000", 50, 1, T0 + 60)
    sib.rtp_ref = sib_rtp
    sib.counter_map = sib_map.rolled_forward(sib_rtp)
    sib.counter_epoch_id = CounterEpochTracker().observe(
        meta["raw_gps_time_ns"], meta["raw_rtp_timesnap"], SR)
    svc.reg_store.write_channel(sib, "ACQUIRED", {})
    with caplog.at_level(logging.INFO, logger=SVC_LOG):
        bt = svc.apply_registration(
            label_timing(T0 + 60, 0.3, SR), _noise(3), _start(51, 1), MIN + 60, meta)
    assert bt.origin_source == "label"
    assert svc.acquirer.registration is None
    assert svc.reg_store.read_summary()["contributing"] == []
    assert any("another radiod run than the raw pair" in r.getMessage()
               for r in caplog.records)


def test_map_fitting_the_raw_pair_within_two_seconds_stands(tmp_path):
    """Control: a map whose epoch sits 1.5 s from the raw pair's (a late
    GPS_TIME packet) is the same run; the sibling converts and is
    adopted at truth."""
    svc = _channel(tmp_path, "SHARED_10000")
    meta = _meta(51, 1, walk=0.0)
    meta["counter_map"] = _map_fitting(51, meta, shift_s=1.5)
    sib = _sibling("WWV_20000", 50, 1, T0 + 60)
    sib.counter_map = dataclasses_replace(
        counter_map_for(50, SR),
        input_epoch_gps_s=meta["counter_map"].input_epoch_gps_s,
    ).rolled_forward(sib.rtp_ref)
    sib.counter_epoch_id = CounterEpochTracker().observe(
        meta["raw_gps_time_ns"], meta["raw_rtp_timesnap"], SR)
    svc.reg_store.write_channel(sib, "ACQUIRED", {})
    bt = svc.apply_registration(
        label_timing(T0 + 60, 0.0, SR), _noise(3), _start(51, 1), MIN + 60, meta)
    assert bt.origin_source == "acquired"
    assert bt.sample0_utc == pytest.approx(T0 + 60, abs=1e-6)
    assert svc.reg_store.read_summary()["contributing"] == ["WWV_20000"]


def dataclasses_replace(obj, **kw):
    import dataclasses

    return dataclasses.replace(obj, **kw)


# ── Final review I-8 (provisional): fusion drops a verified own plane ──


def test_a_dropped_verified_own_plane_is_named_and_logged_once(tmp_path, caplog):
    """This channel's ticks verified its plane at truth; two siblings agree
    10 ms late.  The median rule keeps the siblings and drops the own
    plane.  The rule stays (Michael's ruling pending), but the drop must
    show: one WARNING per episode, and ``dropped`` beside ``contributing``
    in the summary and the channel file."""
    import json

    svc = _channel(tmp_path, "SHARED_10000")
    _acquire(svc, 51)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=SVC_LOG):
        for k in (1, 2, 3):
            truth = svc.acquirer.registration.sample0_utc_for(_start(51, k))
            for ch in ("WWV_15000", "WWV_25000"):
                svc.reg_store.write_channel(
                    _sibling(ch, 51, k, truth + 0.010), "ACQUIRED", {})
            svc.apply_registration(
                label_timing(T0 + 60 * k, 0.250, SR), _ticks(k, seed=8 + k),
                _start(51, k), MIN + 60 * k, _meta(51, k, walk=0.250))
            s = svc.reg_store.read_summary()
            assert s["contributing"] == ["WWV_15000", "WWV_25000"]
            assert s["dropped"] == ["SHARED_10000"]
            ch = json.loads((tmp_path / "reg" / "SHARED_10000.json").read_text())
            assert ch["dropped"] == ["SHARED_10000"]
    drops = [r for r in caplog.records
             if r.levelno == logging.WARNING and "dropped" in r.getMessage()]
    assert len(drops) == 1, [r.getMessage() for r in caplog.records]
    assert "+10.0 ms" in drops[0].getMessage() or "-10.0 ms" in drops[0].getMessage()


def test_dropped_is_empty_when_everyone_fuses(tmp_path):
    svc = _channel(tmp_path, "SHARED_10000")
    _acquire(svc, 51)
    truth = svc.acquirer.registration.sample0_utc_for(_start(51, 1))
    svc.reg_store.write_channel(_sibling("WWV_15000", 51, 1, truth), "ACQUIRED", {})
    svc.apply_registration(
        label_timing(T0 + 60, 0.250, SR), _ticks(1, seed=9), _start(51, 1),
        MIN + 60, _meta(51, 1, walk=0.250))
    assert svc.reg_store.read_summary()["dropped"] == []


def _drop_minutes(svc, minutes, late_s):
    """Run ``minutes`` with the two siblings ``late_s(k)`` seconds late."""
    for k in minutes:
        truth = svc.acquirer.registration.sample0_utc_for(_start(51, k))
        for ch in ("WWV_15000", "WWV_25000"):
            svc.reg_store.write_channel(
                _sibling(ch, 51, k, truth + late_s(k)), "ACQUIRED", {})
        svc.apply_registration(
            label_timing(T0 + 60 * k, 0.250, SR), _ticks(k, seed=8 + k),
            _start(51, k), MIN + 60 * k, _meta(51, k, walk=0.250))


def test_a_plane_flickering_at_the_outlier_bound_warns_once(tmp_path, caplog):
    """Final re-review M-3 (ruling C, 2026-10-30): an own plane sitting near
    FUSE_OUTLIER_MS falls out of fusion one minute and back in the next.
    Without hysteresis each re-entry closed the episode and the next drop
    warned again: a WARNING every other minute."""
    svc = _channel(tmp_path, "SHARED_10000")
    _acquire(svc, 51)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=SVC_LOG):
        _drop_minutes(svc, range(1, 9), lambda k: 0.010 if k % 2 else 0.0)
    drops = [r for r in caplog.records
             if r.levelno == logging.WARNING and "dropped" in r.getMessage()]
    assert len(drops) == 1, [r.getMessage() for r in caplog.records]
    assert not [r for r in caplog.records if "keeps this channel" in r.getMessage()]


def test_a_drop_episode_ends_after_the_plane_stays_kept(tmp_path, caplog):
    """Control: kept long enough, the episode closes once at INFO, and a
    later drop is a new episode with its own WARNING."""
    from hf_timestd.core.metrology_service import MetrologyService

    n = MetrologyService._OWN_DROP_CLEAR_MIN
    svc = _channel(tmp_path, "SHARED_10000")
    _acquire(svc, 51)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=SVC_LOG):
        _drop_minutes(svc, [1], lambda k: 0.010)
        _drop_minutes(svc, range(2, 2 + n), lambda k: 0.0)
        _drop_minutes(svc, [2 + n], lambda k: 0.010)
    msgs = [(r.levelno, r.getMessage()) for r in caplog.records]
    drops = [m for lv, m in msgs if lv == logging.WARNING and "dropped" in m]
    keeps = [m for lv, m in msgs if lv == logging.INFO and "keeps this channel" in m]
    assert len(drops) == 2, msgs
    assert len(keeps) == 1, msgs
