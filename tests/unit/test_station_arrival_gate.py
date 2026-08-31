"""Time-of-arrival gate for WWV/WWVH presence.

Measured on AC0G-B4 2026-08-31.  The discriminator assigned stations by
ORDERING alone -- which peak arrived first -- and never asked whether a
peak landed where geometry says it must.  Its geometric check only wrote
a log line: 1,331 "differs significantly from expected" warnings in six
hours, every one of them followed by the assignment proceeding anyway.
Result: conf=0.50 on 60-70% of shared-channel ensembles and label-vs-
delay disagreement of 34-79%.

Geometry makes this easy.  From EM38ww every plausible mode puts WWV
between 3.73 and 4.24 ms and WWVH between 22.02 and 22.82 ms -- windows
under a millisecond wide, 18 ms apart.  Multipath spreads arrivals
WITHIN a window; it cannot carry one across an 18 ms gap.  (The one
genuinely different path, long-path WWVH, lands ~111 ms out and is
nowhere near either window.)

Each station is judged INDEPENDENTLY against its own window, so all four
outcomes are expressible: both, one, the other, or NEITHER.  The old
model could only ever emit a pair, which is what manufactured the
phantom second station -- on SHARED_5000 every one of 297 WWVH-labelled
ensembles sat at the WWV delay.
"""
from __future__ import annotations

import pytest

from hf_timestd.core.station_arrival_gate import (
    can_discriminate,
    GateVerdict, arrival_windows, gate_arrivals,
)

# From the geographic predictor at EM38ww.
EXPECTED = {"WWV": 4.24, "WWVH": 22.82, "BPM": 39.66}


@pytest.fixture
def windows():
    return arrival_windows(EXPECTED)


class TestWindowsFollowGeometry:
    def test_the_windows_do_not_overlap(self, windows):
        assert windows["WWV"].max_ms < windows["WWVH"].min_ms

    def test_the_guard_band_is_large(self, windows):
        gap = windows["WWVH"].min_ms - windows["WWV"].max_ms
        assert gap > 10.0, "18 ms of separation must survive the tolerances"

    def test_every_plausible_wwv_mode_is_inside(self, windows):
        for mode_ms in (3.73, 3.81, 4.03, 4.24, 4.36):   # ground..3-hop E
            assert windows["WWV"].contains(mode_ms)

    def test_every_plausible_wwvh_mode_is_inside(self, windows):
        for mode_ms in (22.02, 22.06, 22.11, 22.21, 22.82):
            assert windows["WWVH"].contains(mode_ms)


class TestBothOneOrNeither:
    """The four outcomes the old forced-pair model could not express."""

    def test_both_present(self, windows):
        v = gate_arrivals([4.1, 22.5], windows)
        assert v.present == ("WWV", "WWVH")

    def test_only_wwv(self, windows):
        v = gate_arrivals([4.1], windows)
        assert v.present == ("WWV",)

    def test_only_wwvh(self, windows):
        v = gate_arrivals([22.5], windows)
        assert v.present == ("WWVH",)

    def test_neither(self, windows):
        """A dead band must report nothing, not invent a pair."""
        v = gate_arrivals([], windows)
        assert v.present == ()

    def test_an_arrival_in_no_window_names_no_station(self, windows):
        """An 11 ms peak is no station at all -- it must be reported
        unmatched, never assigned to whichever station is nearest."""
        v = gate_arrivals([11.0], windows)
        assert v.present == ()
        assert v.unmatched == (11.0,)


class TestBPMIsNamed:
    """BPM confounds precisely because the old model could not name it.

    Its tick tone is 1000 Hz -- identical to WWV -- so tone-based
    discrimination cannot separate them at all.  And in a forced pair the
    LATE peak becomes WWVH by construction, so BPM at ~39.7 ms was
    labelled WWVH, giving a residual of 39.7 - 22.96 = +16.7 ms.
    SHARED_2500's unexplained WWVH mode sits at +16.1 ms.  That mode is
    BPM wearing WWVH's name.

    Arrival time separates them by 35 ms, so naming BPM is easy once the
    gate looks at position instead of order.
    """

    def test_bpm_is_identified_not_absorbed_into_wwvh(self, windows):
        v = gate_arrivals([39.7], windows)
        assert v.present == ("BPM",)
        assert "WWVH" not in v.present

    def test_bpm_alongside_wwv_does_not_invent_wwvh(self, windows):
        """The exact 2026-08-31 SHARED_2500 confound: WWV plus BPM, which
        the pair model reported as WWV plus WWVH."""
        v = gate_arrivals([4.1, 39.7], windows)
        assert v.present == ("WWV", "BPM")

    def test_all_three_can_be_present(self, windows):
        v = gate_arrivals([4.1, 22.5, 39.7], windows)
        assert v.present == ("WWV", "WWVH", "BPM")

    def test_bpm_window_clears_wwvh_by_a_wide_margin(self, windows):
        gap = windows["BPM"].min_ms - windows["WWVH"].max_ms
        assert gap > 10.0


class TestItCannotManufactureAStation:
    def test_one_arrival_never_yields_two_stations(self, windows):
        """SHARED_5000: 297 of 297 WWVH labels sat at the WWV delay."""
        v = gate_arrivals([4.15], windows)
        assert v.present == ("WWV",)
        assert "WWVH" not in v.present

    def test_two_arrivals_in_the_same_window_are_one_station(self, windows):
        """Two hops of the same signal are one station, not two."""
        v = gate_arrivals([3.8, 4.2], windows)
        assert v.present == ("WWV",)


class TestToleranceIsExplicit:
    def test_a_wider_tolerance_admits_an_unusual_path(self):
        w = arrival_windows(EXPECTED, late_ms=8.0)
        assert w["WWV"].contains(11.0)

    def test_tolerances_that_would_merge_the_windows_are_refused(self):
        with pytest.raises(ValueError):
            arrival_windows(EXPECTED, late_ms=20.0)


class TestThreePathsOnOneFrequency:
    """The three candidates are three INDEPENDENT paths from one receiver.

    WWV 1119 km, WWVH 6600 km, BPM 11504 km -- observed on the same
    frequency at the same instant, so every instrumental term is
    common-mode and cancels between them.  Watching each wax and wane is
    a propagation measurement, which needs the matched arrival per
    station, not merely the fact of presence.
    """

    def test_each_present_station_carries_its_arrival(self, windows):
        v = gate_arrivals([4.1, 22.5, 39.7], windows)
        assert v.matched["WWV"] == (4.1,)
        assert v.matched["WWVH"] == (22.5,)
        assert v.matched["BPM"] == (39.7,)

    def test_absent_stations_carry_nothing(self, windows):
        v = gate_arrivals([22.5], windows)
        assert set(v.matched) == {"WWVH"}

    def test_multiple_hops_of_one_path_are_all_kept(self, windows):
        """Two modes of the same path are one station but two arrivals,
        and the spread between them is itself propagation information."""
        v = gate_arrivals([3.8, 4.2], windows)
        assert v.present == ("WWV",)
        assert v.matched["WWV"] == (3.8, 4.2)


class TestItDegradesWithTheRuler:
    """The gate is only as good as the clock the arrivals are measured on.

    ToA is referenced to radiod's RTP/GPS pair corrected onto the Offset
    Judge's adopted bench.  While that bench is T6 the correction carries
    about 1 ms of sigma and an 18 ms separation is trivially resolvable.
    If T6 drops -- as it did for 6 h 23 m on 2026-08-31 -- the judge falls
    to a lower tier and the ruler gets coarser WITHOUT the arrivals
    looking any different.  A gate that ignored that would keep answering
    confidently on a ruler that could no longer tell the stations apart.

    So the windows widen with the reference sigma, and when they would
    overlap the gate refuses.  Declining to answer is the correct output;
    a confident answer from a ruler that cannot resolve the question is
    not.
    """

    def test_a_good_ruler_still_discriminates(self):
        w = arrival_windows(EXPECTED, reference_sigma_ms=1.03)   # T6, live
        assert w["WWV"].max_ms < w["WWVH"].min_ms
        assert can_discriminate(EXPECTED, reference_sigma_ms=1.03)

    def test_windows_widen_with_the_reference_sigma(self):
        tight = arrival_windows(EXPECTED, reference_sigma_ms=0.0)
        loose = arrival_windows(EXPECTED, reference_sigma_ms=2.0)
        assert loose["WWV"].max_ms > tight["WWV"].max_ms
        assert loose["WWV"].min_ms < tight["WWV"].min_ms

    def test_a_coarse_ruler_refuses_rather_than_guessing(self):
        """At 5 ms of reference sigma the windows swallow the 18 ms gap."""
        assert not can_discriminate(EXPECTED, reference_sigma_ms=5.0)
        with pytest.raises(ValueError):
            arrival_windows(EXPECTED, reference_sigma_ms=5.0)

    def test_the_cutoff_is_reported_not_hidden(self):
        """An operator must be able to see WHY the gate stopped."""
        try:
            arrival_windows(EXPECTED, reference_sigma_ms=5.0)
        except ValueError as exc:
            assert "overlap" in str(exc)
            assert "WWV" in str(exc)
        else:
            pytest.fail("expected a refusal")
