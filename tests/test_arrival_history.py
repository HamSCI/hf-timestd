from hf_timestd.core.arrival_history import ArrivalHistory


def _settle(h, station="WWV", value=4.0, n=5):
    for _ in range(n):
        h.observe(station, value)


def test_empty_history_accepts_anything():
    """With no track yet there is nothing to disagree with."""
    h = ArrivalHistory(tolerance_ms=1.0, lookback=5, reacquire_after=3)
    assert h.accepts("WWV", 4.0) is True


def test_arrival_near_the_track_is_accepted():
    h = ArrivalHistory(tolerance_ms=1.0, lookback=5, reacquire_after=3)
    _settle(h)
    assert h.accepts("WWV", 4.5) is True


def test_lone_outlier_is_rejected():
    h = ArrivalHistory(tolerance_ms=1.0, lookback=5, reacquire_after=3)
    _settle(h)
    assert h.accepts("WWV", 40.0) is False


def test_sustained_disagreement_forces_reacquisition():
    """A gate that can never be overruled by evidence is the stale-lock bug."""
    h = ArrivalHistory(tolerance_ms=1.0, lookback=5, reacquire_after=3)
    _settle(h)

    assert h.accepts("WWV", 40.0) is False   # 1st
    assert h.accepts("WWV", 40.1) is False   # 2nd
    # third consecutive arrival agreeing with the others but not the track
    assert h.accepts("WWV", 40.2) is True    # re-acquire


def test_a_return_to_track_clears_the_reacquire_run():
    h = ArrivalHistory(tolerance_ms=1.0, lookback=5, reacquire_after=3)
    _settle(h)
    assert h.accepts("WWV", 40.0) is False
    assert h.accepts("WWV", 4.1) is True     # back on track, run resets
    assert h.accepts("WWV", 40.0) is False   # counts as the first again


def test_stations_are_tracked_independently():
    h = ArrivalHistory(tolerance_ms=1.0, lookback=5, reacquire_after=3)
    _settle(h, "WWV", 4.0)
    _settle(h, "WWVH", 23.0)
    assert h.accepts("WWVH", 23.2) is True
    assert h.accepts("WWVH", 4.0) is False
