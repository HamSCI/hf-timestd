# tests/test_admission_cascade.py
import pytest

from hf_timestd.core.admission_cascade import (
    AdmissionState, ChannelState, ObservedArrival, adjudicate_channel,
)
from hf_timestd.core.station_arrival_gate import StationWindow


def _win(station, lo, hi, scatter=None):
    """StationWindow has FOUR fields.  `max_ms` ends the modelled direct
    modes; `scatter_max_ms` runs out to where another station could own the
    arrival.  Between them lies DEGRADED: physically this station, but not
    usable for timing."""
    return StationWindow(station=station, min_ms=lo, max_ms=hi,
                         scatter_max_ms=scatter if scatter is not None else hi)


def test_clean_arrival_in_one_window_is_admitted():
    windows = {"WWV": _win("WWV", 3.0, 5.0), "WWVH": _win("WWVH", 22.0, 24.0)}
    arrivals = [ObservedArrival(arrival_ms=4.0, corr_snr_db=20.0)]

    verdict = adjudicate_channel(
        windows=windows, arrivals=arrivals, eligible={"WWV", "WWVH"},
        floor_snr_db=10.0, history_ok=lambda station, ms: True,
    )

    assert verdict.stations["WWV"].state is AdmissionState.ADMITTED
    assert verdict.stations["WWV"].arrival_ms == 4.0
    # WWVH got nothing above the floor in its window
    assert verdict.stations["WWVH"].state is AdmissionState.BELOW_FLOOR
    assert verdict.channel_state is ChannelState.CHANNEL_PARTIAL
    assert verdict.admitted_count == 1


def test_all_three_absent_is_channel_silent():
    windows = {"WWV": _win("WWV", 3.0, 5.0), "WWVH": _win("WWVH", 22.0, 24.0),
               "BPM": _win("BPM", 39.0, 41.0)}
    # one arrival, but under the floor — nothing counts
    arrivals = [ObservedArrival(arrival_ms=4.0, corr_snr_db=2.0)]

    verdict = adjudicate_channel(
        windows=windows, arrivals=arrivals, eligible={"WWV", "WWVH", "BPM"},
        floor_snr_db=10.0, history_ok=lambda station, ms: True,
    )

    assert all(v.state is AdmissionState.BELOW_FLOOR
               for v in verdict.stations.values())
    assert verdict.channel_state is ChannelState.CHANNEL_SILENT
    assert verdict.admitted_count == 0


def test_above_floor_outside_every_window_is_unidentified():
    """Energy arrived; no station's window claims it.  It belongs to none."""
    windows = {"WWV": _win("WWV", 3.0, 5.0), "WWVH": _win("WWVH", 22.0, 24.0)}
    arrivals = [ObservedArrival(arrival_ms=13.0, corr_snr_db=30.0)]

    verdict = adjudicate_channel(
        windows=windows, arrivals=arrivals, eligible={"WWV", "WWVH"},
        floor_snr_db=10.0, history_ok=lambda station, ms: True,
    )

    assert verdict.channel_state is ChannelState.CHANNEL_UNIDENTIFIED
    assert verdict.admitted_count == 0
    assert verdict.unclaimed_ms == [13.0]


def test_history_rejection_yields_inconsistent_not_a_value():
    windows = {"WWV": _win("WWV", 3.0, 5.0)}
    arrivals = [ObservedArrival(arrival_ms=4.0, corr_snr_db=20.0)]

    verdict = adjudicate_channel(
        windows=windows, arrivals=arrivals, eligible={"WWV"},
        floor_snr_db=10.0, history_ok=lambda station, ms: False,
    )

    assert verdict.stations["WWV"].state is AdmissionState.INCONSISTENT
    assert verdict.stations["WWV"].arrival_ms is None
    assert verdict.admitted_count == 0


def test_ineligible_station_is_not_below_floor():
    """BPM off-schedule says nothing about the ionosphere."""
    windows = {"WWV": _win("WWV", 3.0, 5.0), "BPM": _win("BPM", 39.0, 41.0)}
    arrivals = [ObservedArrival(arrival_ms=4.0, corr_snr_db=20.0)]

    verdict = adjudicate_channel(
        windows=windows, arrivals=arrivals, eligible={"WWV"},
        floor_snr_db=10.0, history_ok=lambda station, ms: True,
    )

    assert verdict.stations["BPM"].state is AdmissionState.NOT_ELIGIBLE


def test_scattered_arrival_is_degraded_not_admitted():
    """Inside admits() but outside contains(): this station, unusable."""
    windows = {"WWV": _win("WWV", 3.0, 5.0, scatter=12.0)}
    arrivals = [ObservedArrival(arrival_ms=9.0, corr_snr_db=25.0)]

    verdict = adjudicate_channel(
        windows=windows, arrivals=arrivals, eligible={"WWV"},
        floor_snr_db=10.0, history_ok=lambda station, ms: True,
    )

    assert verdict.stations["WWV"].state is AdmissionState.DEGRADED
    assert verdict.stations["WWV"].arrival_ms is None


def test_only_admitted_carries_a_value():
    """The invariant: six of seven states emit nothing."""
    windows = {"WWV": _win("WWV", 3.0, 5.0)}
    arrivals = [ObservedArrival(arrival_ms=99.0, corr_snr_db=30.0)]

    verdict = adjudicate_channel(
        windows=windows, arrivals=arrivals, eligible={"WWV"},
        floor_snr_db=10.0, history_ok=lambda station, ms: True,
    )

    for v in verdict.stations.values():
        if v.state is not AdmissionState.ADMITTED:
            assert v.arrival_ms is None


def test_ambiguous_overlapping_windows():
    """When two eligible stations' windows overlap, an arrival inside both
    is AMBIGUOUS with arrival_ms=None.  Windows are hand-built to overlap
    because arrival_windows() raises ValueError on any overlap — this test
    verifies the cascade defends against overlapping window sources."""
    # Hand-build overlapping windows (arrival_windows would refuse them)
    win1 = StationWindow(station="WWV", min_ms=3.0, max_ms=8.0, scatter_max_ms=8.0)
    win2 = StationWindow(station="WWVH", min_ms=6.0, max_ms=10.0, scatter_max_ms=10.0)
    windows = {"WWV": win1, "WWVH": win2}
    arrivals = [ObservedArrival(arrival_ms=7.0, corr_snr_db=25.0)]

    verdict = adjudicate_channel(
        windows=windows, arrivals=arrivals, eligible={"WWV", "WWVH"},
        floor_snr_db=10.0, history_ok=lambda station, ms: True,
    )

    # Both stations see the same arrival; neither can claim it
    assert verdict.stations["WWV"].state is AdmissionState.AMBIGUOUS
    assert verdict.stations["WWV"].arrival_ms is None
    assert verdict.stations["WWVH"].state is AdmissionState.AMBIGUOUS
    assert verdict.stations["WWVH"].arrival_ms is None
    assert verdict.admitted_count == 0


def test_ineligible_overlapping_window_does_not_veto():
    """An off-schedule station's overlapping window must not force an
    eligible station into AMBIGUOUS.  Only eligible stations can contest."""
    win1 = StationWindow(station="WWV", min_ms=3.0, max_ms=8.0, scatter_max_ms=8.0)
    win2 = StationWindow(station="BPM", min_ms=6.0, max_ms=10.0, scatter_max_ms=10.0)
    windows = {"WWV": win1, "BPM": win2}
    arrivals = [ObservedArrival(arrival_ms=7.0, corr_snr_db=25.0)]

    verdict = adjudicate_channel(
        windows=windows, arrivals=arrivals, eligible={"WWV"},
        floor_snr_db=10.0, history_ok=lambda station, ms: True,
    )

    # BPM is ineligible, so its window does not contest.  WWV gets admitted.
    assert verdict.stations["WWV"].state is AdmissionState.ADMITTED
    assert verdict.stations["WWV"].arrival_ms == 7.0
    assert verdict.stations["BPM"].state is AdmissionState.NOT_ELIGIBLE


def test_arrival_at_window_boundaries_is_contained():
    """Arrivals exactly at min_ms and max_ms are inside the window."""
    windows = {"WWV": _win("WWV", 3.0, 5.0)}
    arrivals = [
        ObservedArrival(arrival_ms=3.0, corr_snr_db=20.0),
        ObservedArrival(arrival_ms=5.0, corr_snr_db=20.0),
    ]

    verdict = adjudicate_channel(
        windows=windows, arrivals=arrivals, eligible={"WWV"},
        floor_snr_db=10.0, history_ok=lambda station, ms: True,
    )

    # Both boundary arrivals are contained; pick the one with higher SNR
    # (they have equal SNR, so max() picks the first one by default)
    assert verdict.stations["WWV"].state is AdmissionState.ADMITTED
    assert verdict.stations["WWV"].arrival_ms in [3.0, 5.0]


def test_highest_snr_chosen_from_multiple_arrivals_in_window():
    """When a station's window contains multiple above-floor arrivals,
    the one with the highest corr_snr_db is chosen."""
    windows = {"WWV": _win("WWV", 3.0, 5.0)}
    arrivals = [
        ObservedArrival(arrival_ms=4.0, corr_snr_db=15.0),
        ObservedArrival(arrival_ms=4.2, corr_snr_db=25.0),  # highest
        ObservedArrival(arrival_ms=4.5, corr_snr_db=18.0),
    ]

    verdict = adjudicate_channel(
        windows=windows, arrivals=arrivals, eligible={"WWV"},
        floor_snr_db=10.0, history_ok=lambda station, ms: True,
    )

    assert verdict.stations["WWV"].state is AdmissionState.ADMITTED
    assert verdict.stations["WWV"].arrival_ms == 4.2
