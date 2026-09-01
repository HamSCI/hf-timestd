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
