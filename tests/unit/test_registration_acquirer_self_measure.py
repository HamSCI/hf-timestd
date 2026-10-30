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
    truth = acq.registration.utc_ref
    acq.registration.utc_ref += 0.002
    assert acq.verify(ra.residuals_for_verify(_measure(acq, 3))) == "verified"
    before = abs(acq.registration.utc_ref - truth)
    assert acq.corroborate(ra.residuals_for_verify(_measure(acq, 4))) == "tightened"
    after = abs(acq.registration.utc_ref - truth)
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
    warnings = [r for r in caplog.records if "whole second" in r.getMessage()]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert acq.verify(ra.residuals_for_verify({"WWV": confirmed})) == "rejected"
    assert acq.state == acq.STATE_BOOTSTRAP


def test_a_one_second_plane_resets_corroboration():
    acq, _ = _acquired()
    acq.registration.verified = True
    acq.registration.utc_ref += 1.0
    outcomes = [
        acq.corroborate(ra.residuals_for_verify(_measure(acq, k))) for k in (3, 4, 5, 6)
    ]
    assert outcomes[-1] == "reacquire", outcomes
    assert acq.state == acq.STATE_BOOTSTRAP


def test_the_marker_is_never_applied_as_a_correction():
    acq, _ = _acquired()
    acq.registration.utc_ref += 1.0
    held = acq.registration.utc_ref
    for k in (3, 4, 5):
        _measure(acq, k)
    assert acq.registration.utc_ref == held


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
