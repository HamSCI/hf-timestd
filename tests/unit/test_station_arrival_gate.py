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
    classify_arrival,
    eligible_candidates,
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

    def test_an_arrival_before_every_floor_names_no_station(self, windows):
        """Earlier than WWV's floor is earlier than light allows for any
        candidate, so it is reported unmatched rather than assigned to
        whichever station happens to be nearest."""
        v = gate_arrivals([1.0], windows)
        assert v.present == ()
        assert v.unmatched == (1.0,)

    def test_a_late_arrival_is_scattered_not_unmatched(self, windows):
        """An 11 ms peak cannot be direct WWV and cannot be WWVH at all,
        but sidescattered WWV is physically open to it.  It is therefore
        evidence about WWV -- carried, and withheld from timing."""
        v = gate_arrivals([11.0], windows)
        assert v.present == ("WWV",)
        assert v.timing_usable == ()
        assert v.scattered["WWV"] == (11.0,)


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


class TestACandidateMustBeAbleToBeOnTheAir:
    """Geometry says WHERE a station would land; it cannot say WHETHER the
    station is transmitting.  Both must hold before we name it.

    BPM fails this two ways.  It alternates its time base -- minutes
    25-29 and 55-59 carry UT1, not UTC, and UT1 differs from UTC by DUT1
    up to 0.9 s, fifty times the whole span these windows cover.  And it
    does not broadcast around the clock on every frequency: on 2.5 MHz it
    is off 01-07Z, on 15 MHz it is on only 01-08Z.

    An arrival near 39.7 ms while BPM is off is still an arrival, and
    still worth reporting -- as unmatched.  What it must not do is
    acquire BPM's name.
    """

    ALL = {"WWV": 4.24, "WWVH": 22.82, "BPM": 39.66}

    def test_bpm_is_a_candidate_in_a_utc_minute_while_scheduled(self):
        c = eligible_candidates(self.ALL, utc_minute=10, utc_hour=12,
                                bpm_active_hours=set(range(24)))
        assert "BPM" in c

    @pytest.mark.parametrize("minute", [25, 27, 29, 55, 57, 59])
    def test_bpm_is_dropped_in_its_ut1_minutes(self, minute):
        c = eligible_candidates(self.ALL, utc_minute=minute, utc_hour=12,
                                bpm_active_hours=set(range(24)))
        assert "BPM" not in c

    def test_bpm_is_dropped_when_off_schedule(self):
        """2.5 MHz: off 01-07Z."""
        off = {0} | set(range(8, 24))
        assert "BPM" not in eligible_candidates(
            self.ALL, utc_minute=10, utc_hour=3, bpm_active_hours=off)
        assert "BPM" in eligible_candidates(
            self.ALL, utc_minute=10, utc_hour=9, bpm_active_hours=off)

    def test_the_us_stations_are_never_dropped_by_bpm_rules(self):
        c = eligible_candidates(self.ALL, utc_minute=27, utc_hour=3,
                                bpm_active_hours=set())
        assert set(c) == {"WWV", "WWVH"}

    def test_an_arrival_where_bpm_would_be_is_unmatched_when_bpm_is_off(self):
        c = eligible_candidates(self.ALL, utc_minute=27, utc_hour=12,
                                bpm_active_hours=set(range(24)))
        v = gate_arrivals([39.7], arrival_windows(c))
        assert v.present == ()
        assert v.unmatched == (39.7,)

    def test_unknown_time_keeps_every_candidate(self):
        """Absent knowledge of the minute we do not silently narrow."""
        assert set(eligible_candidates(self.ALL)) == {"WWV", "WWVH", "BPM"}


class TestScatterDelaysButNeverAccelerates:
    """The asymmetry is physical, so it belongs in the structure.

    Sidescatter and backscatter are real and can delay a tick
    substantially -- those paths are among the more interesting things on
    the air.  Nothing accelerates one.  A signal from station X can
    therefore never arrive before X's great-circle free-space time, and
    an arrival earlier than that floor is not X by any mechanism.

    So the early bound is HARD and the late bound is GENEROUS, bounded
    only by where the next station's own floor begins.  Between a
    station's modelled modes and that limit an arrival is possible-but-
    scattered: worth recording, and useless for timing, because a
    scattered path has no known length and therefore no known delay.
    """

    FLOORS = {"WWV": 3.73, "WWVH": 22.02, "BPM": 38.37}
    MODES = {"WWV": 4.24, "WWVH": 22.82, "BPM": 39.66}

    def test_nothing_arrives_before_the_free_space_floor(self):
        w = arrival_windows(self.MODES, floors_ms=self.FLOORS)
        assert classify_arrival(3.0, w) is None
        # 21.5 ms is earlier than WWVH's floor, so it can never be WWVH.
        # It remains physically open to scattered WWV, and that is what
        # the gate must say -- not silently the nearer station.
        st, kind = classify_arrival(21.5, w)
        assert st != "WWVH"
        assert (st, kind) == ("WWV", "scattered")

    def test_a_huge_tolerance_still_cannot_breach_the_floor(self):
        """Loosening the late side must not loosen the early side."""
        w = arrival_windows(self.MODES, floors_ms=self.FLOORS, late_ms=8.0)
        assert classify_arrival(3.0, w) is None

    def test_a_direct_arrival_is_usable_for_timing(self):
        w = arrival_windows(self.MODES, floors_ms=self.FLOORS)
        st, kind = classify_arrival(4.1, w)
        assert (st, kind) == ("WWV", "direct")

    def test_a_late_arrival_is_scattered_and_not_usable_for_timing(self):
        """The 8-22 ms population: too late for direct WWV, too early to
        be WWVH at all.  Sidescattered WWV is plausible; its path length
        is not knowable, so it must not carry a delay."""
        w = arrival_windows(self.MODES, floors_ms=self.FLOORS)
        st, kind = classify_arrival(15.0, w)
        assert (st, kind) == ("WWV", "scattered")

    def test_scatter_stops_at_the_next_stations_floor(self):
        w = arrival_windows(self.MODES, floors_ms=self.FLOORS)
        st, kind = classify_arrival(22.5, w)
        assert st == "WWVH" and kind == "direct"

    def test_only_direct_arrivals_reach_the_timing_verdict(self):
        w = arrival_windows(self.MODES, floors_ms=self.FLOORS)
        v = gate_arrivals([4.1, 15.0, 22.5], w)
        assert v.present == ("WWV", "WWVH")
        assert v.timing_usable == ("WWV", "WWVH")
        assert v.scattered == {"WWV": (15.0,)}

    def test_a_station_seen_only_by_scatter_is_present_but_untimed(self):
        w = arrival_windows(self.MODES, floors_ms=self.FLOORS)
        v = gate_arrivals([15.0], w)
        assert v.present == ("WWV",)
        assert v.timing_usable == ()
