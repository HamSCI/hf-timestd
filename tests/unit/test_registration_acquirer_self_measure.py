"""Task 9: the acquirer measures its own plane (T3 stands alone).

``measure_minute`` folds this minute's tick envelopes on the plane the
acquirer itself holds -- never on T6's label plane -- and reports, per
station, the fold peak's position minus the expected delay.  The minute
marker, searched on the same plane, turns a whole second confirmed on two
consecutive minutes into a +/-1000 ms residual.
"""

import logging

import pytest

from hf_timestd.core import registration_acquirer as ra
from hf_timestd.core.registration_acquirer import RegistrationAcquirer
from synth_origins import counter_map_for
from synth_ticks import label_timing, make_tick_audio

SR = 24000
MIN = 1_800_000_000
T0 = MIN - 1.0  # buffer k starts 1 s before minute k (truth), lasts 62 s
D = {"WWV": 0.0125}


def _minute(k, walk_s=0.250, snr_db=20.0, stations=D, marker=True):
    t0 = T0 + 60 * k
    audio = make_tick_audio(
        62, SR, t0, stations, snr_db=snr_db, seed=7 + k, marker=marker
    )
    return audio, label_timing(t0, walk_s, SR), 1_000_000 + k * 60 * SR, MIN + 60 * k


def _acquired(snr_db=20.0, stations=D):
    """Acquire on minutes 0-2 with the label 250 ms late; return the acquirer
    and the last minute the acquisition was offered."""
    acq = RegistrationAcquirer("WWV_10000", SR)
    last = None
    for k in range(3):
        audio, label, rtp, m = _minute(k, snr_db=snr_db, stations=stations)
        acq.offer_minute(audio, label, rtp, m, stations, "ep-1")
        last = k
        if acq.state == acq.STATE_ACQUIRED:
            break
    assert acq.state == acq.STATE_ACQUIRED
    return acq, last


def _plane(acq, rtp=1_000_000):
    """The plane's UTC at one fixed RTP.  ``measure_minute`` re-anchors the
    held plane every minute (final review I-6), so ``utc_ref`` alone names
    a moving reference point, not the plane."""
    return acq.registration.sample0_utc_for(rtp)


def _measure(acq, k, snr_db=20.0, stations=D, marker=True):
    audio, _label, rtp, m = _minute(k, snr_db=snr_db, stations=stations, marker=marker)
    return acq.measure_minute(audio, rtp, m, stations)


def test_a_true_plane_measures_near_zero():
    acq, _ = _acquired()
    got = _measure(acq, 3)
    assert set(got) == {"WWV"}
    a = got["WWV"]
    assert abs(a.residual_ms) < 0.5
    assert a.sigma_ms <= 6.0
    assert a.station == "WWV"
    # arrival_rtp names the peak in the first full second of this buffer:
    # 12.5 ms after a UTC second, within the buffer's first two seconds.
    # The half-rise of the zero-phase envelope leads the true onset by
    # ~0.6 ms (15 samples measured), so allow 1 ms.
    rtp3 = 1_000_000 + 3 * 60 * SR
    into = a.arrival_rtp - rtp3
    assert 0 <= into < 2 * SR
    assert (into - 0.0125 * SR + SR / 2) % SR - SR / 2 == pytest.approx(0, abs=1e-3 * SR)


def test_a_plane_three_ms_off_measures_three_ms():
    acq, _ = _acquired()
    acq.registration.utc_ref += 0.003  # every label 3 ms late
    a = _measure(acq, 3)["WWV"]
    assert a.residual_ms == pytest.approx(+3.0, abs=0.3)


def test_the_residual_moves_the_plane_toward_truth():
    """The sign end to end: ``corroborate`` subtracts the residual, as it
    subtracts ``TickEdgeDetector.timing_error_ms``."""
    acq, _ = _acquired()
    truth = _plane(acq)
    acq.registration.utc_ref += 0.002
    assert acq.verify(ra.residuals_for_verify(_measure(acq, 3))) == "verified"
    before = abs(_plane(acq) - truth)
    assert acq.corroborate(ra.residuals_for_verify(_measure(acq, 4))) == "tightened"
    after = abs(_plane(acq) - truth)
    assert after < before / 4, (before, after)


def test_the_acquisition_minute_is_not_measured():
    acq, last = _acquired()
    assert _measure(acq, last) == {}


def test_a_plane_one_second_off_is_rejected_by_the_marker(caplog):
    acq, _ = _acquired()
    acq.registration.utc_ref += 1.0
    caplog.set_level(logging.WARNING)
    # Minute 3 has no run-up ahead of its label minute, so its marker
    # cannot be searched; minutes 4 and 5 can, and the second confirms.
    first = _measure(acq, 3)["WWV"]
    assert abs(first.residual_ms) < 0.5, "the fold alone cannot see a whole second"
    unconfirmed = _measure(acq, 4)["WWV"]
    assert abs(unconfirmed.residual_ms) < 0.5, "one minute of marker is not enough"
    confirmed = _measure(acq, 5)["WWV"]
    # r - 1000 k with k = -1: magnitude 1000 ms plus the fold's fraction,
    # whose noise (< 0.5 ms here) may sit on either side.
    assert confirmed.residual_ms == pytest.approx(+1000.0, abs=0.5)
    assert confirmed.residual_ms > 0, "a plane 1 s late must move earlier"
    assert acq.verify(ra.residuals_for_verify({"WWV": confirmed})) == "rejected"
    assert acq.state == acq.STATE_BOOTSTRAP
    # Task 10 (ruling 3): the event's one WARNING comes from the reset.
    warnings = [r for r in caplog.records if "whole second" in r.getMessage()]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert "registration reset" in warnings[0].getMessage()


def test_a_one_second_plane_resets_corroboration():
    acq, _ = _acquired()
    acq.registration.verified = True
    acq.registration.utc_ref += 1.0
    outcomes = [
        acq.corroborate(ra.residuals_for_verify(_measure(acq, k))) for k in (3, 4, 5, 6)
    ]
    # The minute that confirms the whole second resets at once (minute 5).
    assert outcomes[:3] == ["tightened", "tightened", "reacquire"], outcomes
    assert acq.state == acq.STATE_BOOTSTRAP


def test_the_marker_is_never_applied_as_a_correction():
    acq, _ = _acquired()
    acq.registration.utc_ref += 1.0
    held = _plane(acq)
    for k in (3, 4, 5):
        _measure(acq, k)
    assert _plane(acq) == held


def test_weak_minutes_fold_together():
    acq, _ = _acquired(snr_db=20.0)
    got = [_measure(acq, k, snr_db=-16.0) for k in (3, 4, 5)]
    assert got[0] == {}, "one weak minute must fold below the floor"
    assert "WWV" in got[2], "three minutes must lift the tick over the floor"
    assert abs(got[2]["WWV"].residual_ms) < 1.5


def _history_len(acq):
    return sum(len(v) for v in acq._fold_history.values())


def test_reset_and_adopt_clear_the_fold_history():
    acq, _ = _acquired()
    _measure(acq, 3)
    assert _history_len(acq) > 0
    reg = acq.registration
    acq.reset("test")
    assert _history_len(acq) == 0
    acq.adopt(reg)
    _measure(acq, 4)
    assert _history_len(acq) > 0
    acq.adopt(reg)
    assert _history_len(acq) == 0


def test_a_counter_map_step_clears_the_fold_history():
    acq, _ = _acquired()
    acq.note_counter_map(counter_map_for(51, SR))
    _measure(acq, 3)
    assert _history_len(acq) > 0
    acq.note_counter_map(counter_map_for(50, SR))  # one block: a step
    assert acq.state == acq.STATE_BOOTSTRAP
    assert _history_len(acq) == 0


# ── fix round 1 ───────────────────────────────────────────────────────

D_SHARED = {"WWV": 0.0125, "WWVH": 0.0370}


def _shared_minute(k, walk_s=0.250):
    """WWV with its minute marker, WWVH without (as at most US sites)."""
    t0 = T0 + 60 * k
    audio = make_tick_audio(62, SR, t0, {"WWV": D_SHARED["WWV"]}, snr_db=20.0,
                            seed=7 + k, marker=True)
    audio += make_tick_audio(62, SR, t0, {"WWVH": D_SHARED["WWVH"]}, snr_db=20.0,
                             seed=100 + k, marker=False)
    return audio, label_timing(t0, walk_s, SR), 1_000_000 + k * 60 * SR, MIN + 60 * k


def test_a_whole_second_on_one_station_condemns_the_whole_plane():
    """CRITICAL 1 (review): WWV's marker confirms the plane 1 s late, WWVH
    has none.  The plane must reset, and must never move on the way."""
    acq = RegistrationAcquirer("SHARED_10000", SR)
    for k in range(3):
        audio, label, rtp, m = _shared_minute(k)
        acq.offer_minute(audio, label, rtp, m, D_SHARED, "ep-1")
        if acq.state == acq.STATE_ACQUIRED:
            break
    assert acq.state == acq.STATE_ACQUIRED
    assert set(acq.registration.stations) == {"WWV", "WWVH"}
    acq.registration.utc_ref += 1.0
    held = _plane(acq)
    seen_whole_second = False
    for k in range(3, 9):
        audio, _label, rtp, m = _shared_minute(k)
        got = acq.measure_minute(audio, rtp, m, D_SHARED)
        if got and abs(got["WWV"].residual_ms) > 500:
            seen_whole_second = True
            assert got["WWVH"].residual_ms == pytest.approx(
                got["WWV"].residual_ms, abs=1.0
            ), "a whole second belongs to the plane, not to one station"
        acq.corroborate(ra.residuals_for_verify(got))
        if acq.registration is None:
            break
        assert abs(_plane(acq) - held) < 0.005, (
            f"minute {k}: plane moved {1000 * (_plane(acq) - held):+.1f} ms"
        )
    assert seen_whole_second
    assert acq.state == acq.STATE_BOOTSTRAP


def test_conflicting_whole_seconds_measure_nothing(monkeypatch, caplog):
    acq, _ = _acquired(stations=D_SHARED)
    votes = {"WWV": -1, "WWVH": +1}
    monkeypatch.setattr(
        acq, "_confirmed_whole_second", lambda station, *a, **kw: votes.get(station, 0)
    )
    caplog.set_level(logging.WARNING)
    assert _measure(acq, 3, stations=D_SHARED) == {}
    assert _measure(acq, 4, stations=D_SHARED) == {}
    conflicts = [r for r in caplog.records if "disagree" in r.getMessage()]
    assert len(conflicts) == 1, [r.getMessage() for r in caplog.records]


@pytest.mark.parametrize("verified", [True, False])
def test_a_residual_past_half_a_second_resets_at_once(verified, caplog):
    """The hard guard alone: one station at +1000 ms among good ones."""
    acq, _ = _acquired()
    acq.registration.verified = verified
    acq.registration.stations = ("WWV", "WWVH")
    held = _plane(acq)
    reg = acq.registration
    caplog.set_level(logging.WARNING)
    out = acq.corroborate({"WWV": (+1000.2, 1.0), "WWVH": (0.1, 1.0), "BPM": (0.0, 1.0)})
    assert out in ("reacquire", "rejected")
    assert acq.state == acq.STATE_BOOTSTRAP
    assert reg.utc_ref == held, "the whole second never enters the mean"
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


def test_the_rolling_fold_follows_a_plane_move():
    """IMPORTANT 2 (review): after corroborate moves the plane 5 ms, the
    next minute reads the move in full, not a blend with older profiles."""
    acq, _ = _acquired()
    acq.registration.verified = True
    acq.registration.sigma_ms = 3.0  # 5 ms stays inside 3 sigma
    assert abs(_measure(acq, 3)["WWV"].residual_ms) < 0.5
    assert abs(_measure(acq, 4)["WWV"].residual_ms) < 0.5
    before = acq.registration.utc_ref
    assert acq.corroborate({"WWV": (+5.0, 1.0)}) == "tightened"
    assert acq.registration.utc_ref == pytest.approx(before - 0.005, abs=1e-6)
    assert _measure(acq, 5)["WWV"].residual_ms == pytest.approx(-5.0, abs=0.3)


def test_the_confirming_minute_logs_its_detail_once(caplog):
    """Minor 5, as Task 10's ruling 3 revised it: the first sighting and
    the confirmation both log at INFO, once each per episode.  The reset
    that the confirmed residual causes in ``verify`` / ``corroborate``
    carries the event's one WARNING (test_a_whole_second_event_logs_one_warning)."""
    acq, _ = _acquired()
    acq.registration.utc_ref += 1.0
    caplog.set_level(logging.INFO)
    _measure(acq, 3)
    _measure(acq, 4)  # first sighting
    first = [r for r in caplog.records if "whole second" in r.getMessage()]
    assert first and all(r.levelno < logging.WARNING for r in first)
    caplog.clear()
    _measure(acq, 5)  # confirmation
    detail = [r for r in caplog.records if "confirms on two consecutive" in r.getMessage()]
    assert len(detail) == 1 and detail[0].levelno == logging.INFO
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    caplog.clear()
    _measure(acq, 6)
    assert not [r for r in caplog.records if "whole second" in r.getMessage()]


# ── Task 10: rulings carried from the Task 9 review ──────────────────


def test_half_a_second_is_not_a_whole_second():
    """Ruling 1: a fold peak exactly half a second from its expected delay
    wraps to +500.0 ms.  That is no whole second, and must not trip the
    whole-second guard; a true whole second reads near 1000 ms."""
    acq, _ = _acquired()
    acq.registration.verified = True
    held = _plane(acq)
    assert acq.corroborate({"WWV": (+500.0, 1.0)}) == "held"
    assert acq.state == acq.STATE_ACQUIRED
    assert acq.registration.utc_ref == held
    assert acq.corroborate({"WWV": (+1000.0, 1.0)}) == "reacquire"
    assert acq.state == acq.STATE_BOOTSTRAP


def test_bpm_dut1_never_trips_the_whole_second_guard():
    """Ruling 2: BPM may carry DUT1, up to 0.9 s.  A BPM residual is no
    verdict on the plane: it neither trips the guard nor enters the mean."""
    acq, _ = _acquired()
    acq.registration.verified = True
    held = _plane(acq)
    assert acq.corroborate({"WWV": (0.0, 1.0), "BPM": (+950.0, 1.0)}) == "tightened"
    assert acq.state == acq.STATE_ACQUIRED
    assert acq.registration.utc_ref == pytest.approx(held, abs=1e-9)


def test_a_whole_second_event_logs_one_warning(caplog):
    """Ruling 3: one event, one WARNING, and it comes from the reset.  The
    confirming minute's detail (marker offset, SNR) logs at INFO."""
    acq, _ = _acquired()
    acq.registration.verified = True
    acq.registration.utc_ref += 1.0
    caplog.set_level(logging.INFO)
    outcomes = []
    for k in (3, 4, 5):
        outcomes.append(acq.corroborate(ra.residuals_for_verify(_measure(acq, k))))
    assert outcomes[-1] == "reacquire", outcomes
    loud = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(loud) == 1, loud
    assert "registration reset" in loud[0] and "whole second" in loud[0]
    detail = [r for r in caplog.records
              if "confirms on two consecutive minutes" in r.getMessage()]
    assert len(detail) == 1 and detail[0].levelno == logging.INFO


def test_a_sustained_residual_reset_is_one_warning(caplog):
    """Ruling 3, the other reset ``corroborate`` makes: the residual held
    past 3 sigma for two minutes.  The service used to add its own
    "residual sustained" WARNING; the reset itself now carries it."""
    acq, _ = _acquired()
    acq.registration.verified = True
    caplog.set_level(logging.INFO)
    assert acq.corroborate({"WWV": (+40.0, 0.5)}) == "held"
    assert acq.corroborate({"WWV": (+40.0, 0.5)}) == "reacquire"
    loud = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(loud) == 1 and "registration reset" in loud[0], loud


def _condemn_a_late_second(acq):
    """Acquire on one minute whose label runs 1 s late (no marker window
    fits one minute, so the raw label names the second), then let the
    self-measurement condemn the plane."""
    audio, label, rtp, m = _minute(0, walk_s=1.0)
    acq.offer_minute(audio, label, rtp, m, D, "ep-1")
    assert acq.state == acq.STATE_ACQUIRED
    assert acq.registration.sample0_utc_for(rtp) == pytest.approx(T0 + 1.0, abs=0.002)
    for k in (1, 2, 3):
        res = ra.residuals_for_verify(_measure(acq, k))
        if acq.registration.verified:
            acq.corroborate(res)
        else:
            acq.verify(res)
        if acq.state == acq.STATE_BOOTSTRAP:
            return k
    raise AssertionError("the marker never condemned the plane")


def test_a_condemned_second_names_the_next_acquisitions_second():
    """Task 10: the host may name the whole second only when no broadcast
    can, and a broadcast namer that disagrees overrules it.  Once the
    marker has condemned a plane built on the raw label's second, the next
    acquisition from that label takes the marker's second, not the raw
    label's again (which would cycle: acquire, condemn, reset, forever)."""
    acq = RegistrationAcquirer("WWV_10000", SR)
    k = _condemn_a_late_second(acq)
    audio, label, rtp, m = _minute(k + 1, walk_s=1.0)
    acq.offer_minute(audio, label, rtp, m, D, "ep-1")
    assert acq.state == acq.STATE_ACQUIRED
    assert acq.registration.sample0_utc_for(rtp) == pytest.approx(T0 + 60 * (k + 1), abs=0.002)


def test_a_new_counter_epoch_forgets_the_condemned_second():
    """The overruling second belongs to the raw pair that was condemned; a
    new counter epoch is a new pair, and the raw label names its second
    again."""
    acq = RegistrationAcquirer("WWV_10000", SR)
    k = _condemn_a_late_second(acq)
    audio, label, rtp, m = _minute(k + 1, walk_s=1.0)
    acq.offer_minute(audio, label, rtp, m, D, "ep-2")
    assert acq.state == acq.STATE_ACQUIRED
    assert acq.registration.sample0_utc_for(rtp) == pytest.approx(
        T0 + 60 * (k + 1) + 1.0, abs=0.002)


# ── Task 10 fix round 1 ───────────────────────────────────────────────


def test_an_unresolved_marker_does_not_rebuild_the_condemned_second(monkeypatch):
    """Minor 3: the next acquisition's buffer holds a marker, but its
    decision is unresolved (here: too weak to name a second).  The
    remembered second still governs; otherwise the plane is rebuilt on the
    raw label's second, which the broadcast already condemned."""
    acq = RegistrationAcquirer("WWV_10000", SR)
    k = _condemn_a_late_second(acq)
    monkeypatch.setattr(ra, "marker_in_envelope", lambda *a, **kw: (1.0125, 12.0))
    monkeypatch.setattr(
        ra, "whole_second_from_marker",
        lambda *a, **kw: ra.WholeSecondDecision(
            k_int=0, k_raw=-1, agrees=True, unresolved=True, reason="snr"),
    )
    audio, label, rtp, m = _minute(k + 1, walk_s=1.0)
    acq.offer_minute(audio, label, rtp, m, D, "ep-1")
    assert acq.state == acq.STATE_ACQUIRED
    assert acq.registration.sample0_utc_for(rtp) == pytest.approx(T0 + 60 * (k + 1), abs=0.002)


def test_sixty_second_buffers_recover_a_raw_pair_one_second_late():
    """Minor 8: the live ring hands over [minute, minute + 60 s), never
    the 62 s the other tests use, so one minute holds no marker window
    at acquisition and the run-up comes only from the previous tail.  A
    raw label 1 s late must still end on a verified plane at truth."""
    acq = RegistrationAcquirer("WWV_10000", SR)
    states = []
    for k in range(8):
        t0 = MIN + 60 * k  # truth: the buffer starts on the minute
        audio = make_tick_audio(60, SR, t0, D, snr_db=20.0, seed=7 + k)
        rtp = 1_000_000 + k * 60 * SR
        acq.offer_minute(audio, label_timing(t0, 1.0, SR), rtp, MIN + 60 * k, D, "ep-1")
        res = ra.residuals_for_verify(acq.measure_minute(audio, rtp, MIN + 60 * k, D))
        if res and acq.registration is not None:
            (acq.corroborate if acq.registration.verified else acq.verify)(res)
        states.append(acq.state)
    assert acq.state == acq.STATE_ACQUIRED and acq.registration.verified, states
    assert acq.registration.sample0_utc_for(1_000_000 + 7 * 60 * SR) == pytest.approx(
        MIN + 420, abs=0.002)


def test_a_whole_second_on_an_adopted_plane_resets_quietly(caplog):
    """Fix round 2, MINOR 4: the service names an adopted plane's
    condemnation as a CONFLICT with its donors, which carries the
    event's one WARNING; the guard's own reset stays at INFO there."""
    acq, _ = _acquired()
    reg = acq.registration
    reg.verified = True  # adopted planes are verified (their donors were)
    acq.adopt(reg)
    assert acq.registration.method == "adopted"
    caplog.set_level(logging.INFO)
    assert acq.corroborate({"WWV": (+1000.0, 1.0)}) == "reacquire"
    assert acq.state == acq.STATE_BOOTSTRAP
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
