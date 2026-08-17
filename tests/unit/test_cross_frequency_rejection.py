"""Within-station cross-frequency outlier rejection (hf-timestd#22).

One station's frequencies share a transmitter, an instant and very
nearly a path — only hop geometry differs, which is a millisecond or
two.  Measured on AC0G-B4, WWV's two EXCLUSIVE frequencies agree to
under 1 ms (20 MHz -0.53, 25 MHz +0.64).

So a frequency that disagrees with its own station's other frequencies
by ~18 ms is provably contaminated, and saying so needs no propagation
model at all.  That is the tightest constraint available, and it is
what catches co-channel capture between WWV / WWVH / BPM on the shared
frequencies.
"""
import pytest

from hf_timestd.core.multi_broadcast_fusion import (
    CROSS_FREQ_THRESHOLD_MS,
    BroadcastMeasurement,
    MultiBroadcastFusion,
)


def _m(station, freq_mhz, d_clock_ms, mode="1F"):
    return BroadcastMeasurement(
        timestamp=1786900000.0,
        station=station,
        frequency_mhz=freq_mhz,
        d_clock_ms=d_clock_ms,
        propagation_delay_ms=4.6,
        propagation_mode=mode,
        confidence=0.9,
        snr_db=15.0,
        quality_grade="B",
        channel_name=f"SHARED_{int(freq_mhz * 1000)}",
    )


def _reject(measurements):
    f = MultiBroadcastFusion.__new__(MultiBroadcastFusion)
    return f._reject_cross_frequency_outliers(measurements)


class TestCatchesCoChannelCapture:
    def test_the_measured_b4_case(self):
        """WWV, 2026-08-17 11:37Z.

        Both shared channels go: 5.0 MHz is the obvious capture
        (16.45 ms from its own station's median), and 10.0 MHz is 5.20 ms
        out — also contaminated, which the three-minute snapshot
        corroborates independently (WWV 10 MHz sat at +7.36/+7.10 while
        its clean channels read within half a millisecond).  The four
        survivors include BOTH exclusive frequencies.
        """
        ms = [
            _m("WWV", 2.5, +1.74), _m("WWV", 5.0, +17.58),
            _m("WWV", 10.0, +6.32), _m("WWV", 15.0, -0.25),
            _m("WWV", 20.0, -0.08), _m("WWV", 25.0, +0.51),
        ]
        kept, rejected = _reject(ms)
        assert sorted(r.frequency_mhz for r in rejected) == [5.0, 10.0]
        assert sorted(m.frequency_mhz for m in kept) == [2.5, 15.0, 20.0, 25.0]

    def test_a_clean_station_loses_nothing(self):
        ms = [
            _m("WWV", 10.0, +0.4), _m("WWV", 15.0, -0.25),
            _m("WWV", 20.0, -0.53), _m("WWV", 25.0, +0.64),
        ]
        kept, rejected = _reject(ms)
        assert rejected == []
        assert len(kept) == 4

    def test_rejection_is_per_station(self):
        """WWV's bad channel must not cost WWVH a good one."""
        ms = [
            _m("WWV", 2.5, +0.1), _m("WWV", 10.0, +0.2),
            _m("WWV", 20.0, +0.3), _m("WWV", 5.0, +17.6),
            _m("WWVH", 2.5, +0.4), _m("WWVH", 10.0, +0.5),
            _m("WWVH", 15.0, +0.6),
        ]
        kept, rejected = _reject(ms)
        assert [(r.station, r.frequency_mhz) for r in rejected] == [("WWV", 5.0)]
        assert {m.station for m in kept} == {"WWV", "WWVH"}

    def test_catches_the_stable_wwvh_capture_by_bpm(self):
        """WWVH sat at a stable +18.7 ms on 10 MHz — BPM's tick."""
        ms = [
            _m("WWVH", 2.5, -0.4), _m("WWVH", 5.0, -0.2),
            _m("WWVH", 10.0, +18.7), _m("WWVH", 15.0, +0.1),
        ]
        _kept, rejected = _reject(ms)
        assert [r.frequency_mhz for r in rejected] == [10.0]

    def test_bpm_is_checked_too(self):
        """BPM is excluded from the cross-frequency VALIDATION gate, but
        its own frequencies must still agree with each other."""
        ms = [
            _m("BPM", 2.5, -5.0), _m("BPM", 5.0, -5.2),
            _m("BPM", 10.0, -18.4), _m("BPM", 15.0, -4.9),
        ]
        _kept, rejected = _reject(ms)
        assert [r.frequency_mhz for r in rejected] == [10.0]


class TestRefusesToGuess:
    def test_two_frequencies_cannot_identify_the_culprit(self):
        """A disagreement between two says one is wrong, not which."""
        ms = [_m("WWV", 10.0, +0.2), _m("WWV", 5.0, +17.6)]
        kept, rejected = _reject(ms)
        assert rejected == []
        assert len(kept) == 2

    def test_a_single_frequency_is_left_alone(self):
        ms = [_m("WWV", 20.0, +9.9)]
        kept, rejected = _reject(ms)
        assert rejected == []
        assert len(kept) == 1

    def test_modes_without_a_real_model_prediction_are_skipped(self):
        """vacuum_fallback/FALLBACK have no skywave prediction, so their
        d_clock legitimately differs — the existing validation skips
        them for exactly this reason and so must the rejection."""
        ms = [
            _m("WWV", 10.0, +0.2), _m("WWV", 15.0, +0.3),
            _m("WWV", 20.0, +0.1),
            _m("WWV", 2.5, +18.0, mode="vacuum_fallback"),
        ]
        _kept, rejected = _reject(ms)
        assert rejected == []

    def test_threshold_is_the_shared_constant(self):
        """One number, not two: the gate and the rejection must agree
        on what 'frequency-independent' means."""
        just_over = CROSS_FREQ_THRESHOLD_MS + 0.5
        ms = [
            _m("WWV", 10.0, 0.0), _m("WWV", 15.0, 0.0),
            _m("WWV", 20.0, 0.0), _m("WWV", 5.0, just_over),
        ]
        _kept, rejected = _reject(ms)
        assert [r.frequency_mhz for r in rejected] == [5.0]

    def test_within_threshold_survives(self):
        ms = [
            _m("WWV", 10.0, 0.0), _m("WWV", 15.0, 0.0),
            _m("WWV", 20.0, 0.0),
            _m("WWV", 5.0, CROSS_FREQ_THRESHOLD_MS - 0.5),
        ]
        _kept, rejected = _reject(ms)
        assert rejected == []


class TestRobustness:
    def test_a_median_is_used_so_one_outlier_cannot_drag_the_reference(self):
        """With a mean reference, an 18 ms outlier pulls the centre far
        enough that a good channel can be rejected instead."""
        ms = [
            _m("WWV", 10.0, 0.0), _m("WWV", 15.0, 0.0),
            _m("WWV", 20.0, 0.0), _m("WWV", 5.0, +18.0),
        ]
        kept, rejected = _reject(ms)
        assert [r.frequency_mhz for r in rejected] == [5.0]
        assert all(abs(m.d_clock_ms) < 1.0 for m in kept)

    def test_nan_measurements_do_not_poison_the_median(self):
        ms = [
            _m("WWV", 10.0, 0.1), _m("WWV", 15.0, 0.2),
            _m("WWV", 20.0, 0.3), _m("WWV", 5.0, float("nan")),
        ]
        kept, rejected = _reject(ms)
        assert rejected == []
        assert len(kept) == 4

    def test_empty_input(self):
        assert _reject([]) == ([], [])


class TestGateHonoursTheRejection:
    """The gate and the rejection must not re-derive scope independently.

    Observed on B4 2026-08-17 12:38: the rejection dropped WWV 5.0 and
    10.0 MHz, and the gate in the SAME fuse() call then reported
    "[2.5MHz=+1.81, 5.0MHz=+18.34, 10.0MHz=+9.40]" and failed on a
    16.52 ms spread — the very measurements just rejected.  The two
    blocks apply different mode filters, so they disagree about which
    measurements are in scope, and the gate can never clear.
    """

    def _fusion(self):
        return MultiBroadcastFusion.__new__(MultiBroadcastFusion)

    def test_gate_ignores_frequencies_the_rejection_dropped(self):
        f = self._fusion()
        ms = [
            _m("WWV", 2.5, +1.81), _m("WWV", 5.0, +18.34),
            _m("WWV", 10.0, +9.40), _m("WWV", 15.0, +1.9),
            _m("WWV", 20.0, +2.0), _m("WWV", 25.0, +2.1),
        ]
        kept, rejected = f._reject_cross_frequency_outliers(ms)
        assert {r.frequency_mhz for r in rejected} == {5.0, 10.0}
        # The gate re-derives its own scope from the FULL list; it must
        # still honour what was rejected.
        valid, reason, _dev = f._validate_cross_frequency_d_clock(ms)
        assert valid, reason

    def test_gate_still_fails_on_a_genuine_unrejected_spread(self):
        """Honouring the rejection must not make the gate toothless: a
        spread among SURVIVING frequencies must still fail."""
        f = self._fusion()
        # Deviations of 3 ms survive rejection (<= 5 ms from the median)
        # but the RANGE is 6 ms, which the gate must still fail on.
        ms = [
            _m("WWV", 2.5, -3.0), _m("WWV", 15.0, 0.0),
            _m("WWV", 20.0, +3.0),
        ]
        _kept, rejected = f._reject_cross_frequency_outliers(ms)
        assert rejected == []
        valid, reason, _dev = f._validate_cross_frequency_d_clock(ms)
        assert not valid

    def test_a_fresh_instance_rejects_nothing_by_default(self):
        f = self._fusion()
        ms = [_m("WWV", 2.5, 0.0), _m("WWV", 5.0, +18.0), _m("WWV", 10.0, 0.0)]
        valid, _reason, _dev = f._validate_cross_frequency_d_clock(ms)
        assert not valid  # no rejection run yet — gate sees everything
