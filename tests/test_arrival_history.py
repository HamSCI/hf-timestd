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


class TestPerStationTolerance:
    """Calibration gave a different tolerance per station, not one number.

    Measured over 3M archived arrivals, after keys 1 and 2 had filtered them:
    the p95 minute-to-minute step is 4.86 ms for WWV, 6.03 for WWVH and 8.69
    for BPM. One shared value either starves WWV or lets BPM's real variation
    look like an outlier — 11,528 km over three hops moves more than 1,122 km
    over one.
    """

    def _h(self):
        return ArrivalHistory(
            tolerance_ms={"WWV": 5.0, "WWVH": 6.0, "BPM": 9.0},
            lookback=10, reacquire_after=3)

    def _settle(self, h, station, value):
        for _ in range(5):
            h.observe(station, value)

    def test_each_station_gets_its_own_tolerance(self):
        h = self._h()
        self._settle(h, "WWV", 4.0)
        self._settle(h, "BPM", 40.0)
        # 7 ms away: inside BPM's 9 ms, outside WWV's 5 ms
        assert h.accepts("BPM", 47.0) is True
        assert h.accepts("WWV", 11.0) is False

    def test_a_station_absent_from_the_mapping_still_works(self):
        """An unmapped station must not crash or silently admit everything."""
        h = self._h()
        self._settle(h, "WWVB", 4.0)
        assert h.accepts("WWVB", 4.2) is True
        assert h.accepts("WWVB", 400.0) is False

    def test_a_plain_float_still_works(self):
        """The scalar form stays valid — the replay CLI passes one."""
        h = ArrivalHistory(tolerance_ms=2.0, lookback=5, reacquire_after=3)
        for _ in range(5):
            h.observe("WWV", 4.0)
        assert h.accepts("WWV", 5.5) is True
        assert h.accepts("WWV", 40.0) is False
