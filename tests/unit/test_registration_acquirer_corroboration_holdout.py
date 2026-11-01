"""corroborate averages only the stations that agree with the held plane.

AC0G-B4, 2026-11-01 06:16Z, SHARED_5000: a weak WWVH fold peak (13.7 dB,
residual -403.9 ms, sigma ~3 ms) sat far from a young plane (n = 2) while
WWV agreed with it.  One far station among several was neither acted on
nor left out: it joined the weighted mean and dragged the plane by
-w*e/(w+n), from 3.167 to 17.042 ms (+13.9 ms).  The shadow record shows
the same night jumps of +13 to +15 ms on other channels.
"""

import logging

import pytest

from hf_timestd.core.registration_acquirer import (
    CROSS_SITE_AGREE_MS,
    Registration,
    RegistrationAcquirer,
)

SR = 24000
T0 = 1_800_000_000 - 1.0


def _plane(n_minutes=2):
    acq = RegistrationAcquirer("SHARED_5000", SR)
    acq._state = acq.STATE_ACQUIRED
    acq._reg = Registration(
        counter_epoch_id="ep-1", rtp_ref=1_000_000, utc_ref=T0, sample_rate=SR,
        sigma_ms=1.0, channel="SHARED_5000", n_minutes=n_minutes,
        stations=("WWV", "WWVH"), verified=True,
    )
    return acq


def _moved_ms(acq, before):
    return (before - acq.registration.utc_ref) * 1000.0


def test_a_far_station_is_held_out_not_blended():
    """The 06:16Z minute: WWV agrees, WWVH sits 404 ms off.  The plane
    moves on WWV's evidence alone, and WWVH is named with its residual."""
    acq = _plane(n_minutes=2)
    before = acq.registration.utc_ref
    assert acq.corroborate({"WWV": (0.3, 1.0), "WWVH": (-403.9, 3.0)}) == "tightened"
    # WWV alone: w_new 1, w_old 2 -> a third of +0.3 ms
    assert _moved_ms(acq, before) == pytest.approx(0.1, abs=1e-3)
    assert acq.last_held_out == {"WWVH": -403.9}
    assert acq.held_out_counts == {"WWVH": 1}


def test_cross_site_stations_that_disagree_are_not_blended():
    """Both inside the 3-sigma threshold, but WWV and WWVH differ by more
    than CROSS_SITE_AGREE_MS: one correction never blends them.  The one
    nearer the held plane corroborates; the other is held out."""
    acq = _plane(n_minutes=2)
    before = acq.registration.utc_ref
    e_wwv, e_wwvh = 1.5, -2.8
    assert abs(e_wwv - e_wwvh) > CROSS_SITE_AGREE_MS
    assert acq.corroborate({"WWV": (e_wwv, 1.0), "WWVH": (e_wwvh, 1.0)}) == "tightened"
    assert _moved_ms(acq, before) == pytest.approx(e_wwv / 3.0, abs=1e-3)
    assert acq.last_held_out == {"WWVH": e_wwvh}


def test_cross_site_stations_that_agree_still_blend():
    """Agreement within CROSS_SITE_AGREE_MS: both enter the mean, as before."""
    acq = _plane(n_minutes=2)
    before = acq.registration.utc_ref
    assert acq.corroborate({"WWV": (1.0, 1.0), "WWVH": (-1.0, 1.0)}) == "tightened"
    assert _moved_ms(acq, before) == pytest.approx(0.0, abs=1e-3)
    assert acq.last_held_out == {}


def test_two_far_stations_still_reacquire():
    """Hold-out never weakens the reset rule: two far stations for two
    minutes still condemn the plane."""
    acq = _plane(n_minutes=10)
    assert acq.corroborate({"WWV": (30.0, 1.0), "WWVH": (30.5, 1.0)}) == "held"
    assert acq.corroborate({"WWV": (30.2, 1.0), "WWVH": (30.1, 1.0)}) == "reacquire"


def test_a_hold_out_logs_once_on_entry_and_once_on_exit(caplog):
    acq = _plane(n_minutes=10)
    caplog.set_level(logging.INFO)
    for _ in range(4):
        acq.corroborate({"WWV": (0.1, 1.0), "WWVH": (-200.0, 3.0)})
    acq.corroborate({"WWV": (0.1, 1.0), "WWVH": (0.2, 3.0)})
    acq.corroborate({"WWV": (0.1, 1.0), "WWVH": (0.2, 3.0)})
    lines = [r.getMessage() for r in caplog.records if "held out" in r.getMessage()
             or "corroborates again" in r.getMessage()]
    assert len(lines) == 2, lines
    assert "WWVH" in lines[0] and "-200.0" in lines[0]
    assert acq.held_out_counts == {"WWVH": 4}


def test_a_minute_without_the_station_does_not_end_its_episode(caplog):
    acq = _plane(n_minutes=10)
    caplog.set_level(logging.INFO)
    acq.corroborate({"WWV": (0.1, 1.0), "WWVH": (-200.0, 3.0)})
    acq.corroborate({"WWV": (0.1, 1.0)})  # WWVH not heard
    assert acq.last_held_out == {}
    acq.corroborate({"WWV": (0.1, 1.0), "WWVH": (-201.0, 3.0)})
    lines = [r.getMessage() for r in caplog.records if "WWVH" in r.getMessage()]
    assert len(lines) == 1, lines
