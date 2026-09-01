import pytest

from hf_timestd.replay.window_source import WindowSource

# AC0G / B4, Columbia MO — the station the archive came from.
LAT, LON = 38.9187497, -92.1277207
MINUTE_1719Z = 1788196740  # 2026-08-31T17:19Z


@pytest.fixture
def source():
    return WindowSource(receiver_lat=LAT, receiver_lon=LON,
                        reference_sigma_ms=0.7)


def test_windows_use_the_corrected_predictor(source):
    """The archive's model_expected_ms is the CORRUPTED prediction.

    Replay must recompute, not reuse: 10 MHz once read WWV 8.49 / WWVH 44.49
    against a true 4.24 / 22.85.
    """
    windows = source.windows_for(MINUTE_1719Z, 10.0)
    wwv = windows["WWV"]
    centre = (wwv.min_ms + wwv.max_ms) / 2.0
    assert 3.5 < centre < 6.0, f"WWV centre {centre:.2f} ms is not physical"


def test_wwvh_and_bpm_separate_after_the_fix(source):
    """The 2x bug collapsed WWVH and BPM to 1.05 ms apart, forcing abstention."""
    windows = source.windows_for(MINUTE_1719Z, 10.0)
    wwvh = (windows["WWVH"].min_ms + windows["WWVH"].max_ms) / 2.0
    bpm = (windows["BPM"].min_ms + windows["BPM"].max_ms) / 2.0
    assert bpm - wwvh > 10.0


def test_eligibility_excludes_stations_off_this_frequency(source):
    eligible = source.eligible_for(MINUTE_1719Z, 20.0)
    assert "WWV" in eligible
    assert "WWVH" not in eligible  # WWVH does not transmit on 20 MHz


def test_unbuildable_windows_return_empty_rather_than_raise(source):
    """arrival_windows refuses an overlapping set; replay must not crash."""
    wide = WindowSource(receiver_lat=LAT, receiver_lon=LON,
                        reference_sigma_ms=50.0)
    assert wide.windows_for(MINUTE_1719Z, 10.0) == {}


def test_window_floors_are_never_faster_than_light():
    """Nothing accelerates a radio arrival: no window's min_ms may sit

    earlier than that station's free-space great-circle time.  C1 —
    windows_for() called arrival_windows() without floors_ms, so the floor
    fell back to `expected - 1.5ms - 3sigma`; at 10 MHz that put WWV's
    floor at 0.447 ms against a free-space time of 3.743 ms, an arrival
    3.3 ms earlier than light admitted as WWV.  reference_sigma_ms=0 here
    isolates the physical floor from the ruler-uncertainty slack that
    arrival_windows() separately subtracts from it.
    """
    source = WindowSource(receiver_lat=LAT, receiver_lon=LON,
                          reference_sigma_ms=0.0)
    windows = source.windows_for(MINUTE_1719Z, 10.0)
    assert windows, "expected non-empty windows at 10 MHz"
    for station, window in windows.items():
        freespace_ms = source._matrix.distance_km(station, 10.0) / 299.792458
        assert window.min_ms >= freespace_ms, (
            f"{station} window floor {window.min_ms:.3f} ms is faster than "
            f"light ({freespace_ms:.3f} ms)")
