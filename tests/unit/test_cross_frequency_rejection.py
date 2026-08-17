"""Anchored cross-frequency outlier rejection (hf-timestd#22).

WWV, WWVH and BPM all transmit on 2.5 / 5 / 10 / 15 MHz, and their tick
detectors capture each other there.  **WWV also transmits on 20 and
25 MHz, where it is the only station** — those channels cannot be
contaminated, and on AC0G-B4 they agree to under 1 ms while the shared
channels scatter 8-19 ms.

An earlier attempt used the median of ALL a station's frequencies as the
reference.  That inverts: a median is robust only below 50 % contamination,
and when three of WWV's six frequencies were captured the median landed
BETWEEN the clusters and the rejection threw away 20 and 25 MHz — the
ground truth.  It degraded FUSE from 12 us to 265 us Std Dev before it was
reverted.

So the reference must be anchored OUTSIDE the contaminable set, and
exclusive channels must never themselves be rejected.
"""
import pytest

from hf_timestd.core.multi_broadcast_fusion import (
    CROSS_FREQ_THRESHOLD_MS,
    SHARED_FREQS_MHZ,
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
        channel_name=f"CH_{int(freq_mhz * 1000)}",
    )


def _reject(measurements):
    f = MultiBroadcastFusion.__new__(MultiBroadcastFusion)
    return f._reject_cross_frequency_outliers(measurements)


class TestNeverDiscardsTheGroundTruth:
    """Regression for the reverted attempt (36e6145)."""

    def test_the_exact_b4_failure_keeps_20_and_25(self):
        """B4 12:5xZ: half of WWV's frequencies captured.  A plain median
        landed at +6.66 ms and rejected 20 and 25 MHz.  Anchored, the
        exclusive pair defines the reference and the captured shared
        channels go instead."""
        ms = [
            _m("WWV", 2.5, +1.71), _m("WWV", 5.0, +15.19),
            _m("WWV", 10.0, +12.0), _m("WWV", 15.0, +12.10),
            _m("WWV", 20.0, +0.75), _m("WWV", 25.0, -0.64),
        ]
        kept, rejected = _reject(ms)
        assert sorted(r.frequency_mhz for r in rejected) == [5.0, 10.0, 15.0]
        assert {20.0, 25.0} <= {m.frequency_mhz for m in kept}

    def test_an_exclusive_channel_is_never_rejected(self):
        """Even a wild exclusive reading stays: it cannot be a co-channel
        capture, so rejecting it would be discarding evidence."""
        ms = [
            _m("WWV", 20.0, +0.0), _m("WWV", 25.0, +0.2),
            _m("WWV", 10.0, +0.1),
        ]
        _kept, rejected = _reject(ms)
        assert rejected == []

    def test_majority_contamination_does_not_flip_the_verdict(self):
        """Five of six captured — a consensus estimator would invert."""
        ms = [
            _m("WWV", 2.5, +18.0), _m("WWV", 5.0, +18.1),
            _m("WWV", 10.0, +18.2), _m("WWV", 15.0, +18.3),
            _m("WWV", 20.0, +0.1), _m("WWV", 25.0, -0.1),
        ]
        kept, rejected = _reject(ms)
        assert sorted(r.frequency_mhz for r in rejected) == [2.5, 5.0, 10.0, 15.0]
        assert sorted(m.frequency_mhz for m in kept) == [20.0, 25.0]


class TestAnchoring:
    def test_clean_shared_channels_survive(self):
        ms = [
            _m("WWV", 20.0, -0.53), _m("WWV", 25.0, +0.64),
            _m("WWV", 10.0, +0.4), _m("WWV", 15.0, -0.25),
        ]
        _kept, rejected = _reject(ms)
        assert rejected == []

    def test_one_anchor_alone_is_not_trusted(self):
        """Measured on B4, WWV's exclusive channels run at ~9 dB SNR
        (20 MHz med 9.2 max 10.2; 25 MHz med 9.2 max 9.6) — the WEAKEST
        signals available, against 25.6 dB on 5 MHz.  A lone weak
        detection could be a false peak, and anchoring on it would
        reject the good shared channels: the reverted failure again,
        by another route.  An SNR threshold cannot separate them (they
        are uniformly ~9 dB), but MUTUAL AGREEMENT can — two independent
        9 dB detections landing sub-ms apart is not noise."""
        ms = [
            _m("WWV", 20.0, +0.2), _m("WWV", 5.0, +17.6),
            _m("WWV", 10.0, +0.3),
        ]
        _kept, rejected = _reject(ms)
        assert rejected == []

    def test_two_agreeing_anchors_are_trusted(self):
        ms = [
            _m("WWV", 20.0, +0.2), _m("WWV", 25.0, +0.4),
            _m("WWV", 5.0, +17.6), _m("WWV", 10.0, +0.3),
        ]
        _kept, rejected = _reject(ms)
        assert [r.frequency_mhz for r in rejected] == [5.0]

    def test_disagreeing_anchors_refuse_to_judge(self):
        """If the exclusive channels do not agree with each other, we have
        no trustworthy reference and must not reject anything."""
        ms = [
            _m("WWV", 20.0, +0.0), _m("WWV", 25.0, +12.0),
            _m("WWV", 5.0, +17.6),
        ]
        _kept, rejected = _reject(ms)
        assert rejected == []


class TestStationsWithoutAnExclusiveFrequency:
    """WWVH and BPM transmit ONLY on shared frequencies, so no anchor
    exists and internal consistency cannot identify the culprit.  Those
    need the arrival-time gate instead; here we must refuse."""

    def test_wwvh_is_left_alone(self):
        ms = [
            _m("WWVH", 2.5, -0.4), _m("WWVH", 5.0, -0.2),
            _m("WWVH", 10.0, +18.7), _m("WWVH", 15.0, +0.1),
        ]
        _kept, rejected = _reject(ms)
        assert rejected == []

    def test_bpm_is_left_alone(self):
        ms = [
            _m("BPM", 2.5, -5.0), _m("BPM", 5.0, -5.2),
            _m("BPM", 10.0, -18.4), _m("BPM", 15.0, -4.9),
        ]
        _kept, rejected = _reject(ms)
        assert rejected == []

    def test_wwv_rejection_does_not_touch_other_stations(self):
        ms = [
            _m("WWV", 20.0, +0.1), _m("WWV", 25.0, +0.2),
            _m("WWV", 5.0, +17.6),
            _m("WWVH", 10.0, +18.7), _m("WWVH", 15.0, +0.1),
        ]
        kept, rejected = _reject(ms)
        assert [(r.station, r.frequency_mhz) for r in rejected] == [("WWV", 5.0)]
        assert sum(1 for m in kept if m.station == "WWVH") == 2


class TestScopeAndSafety:
    def test_shared_set_is_the_wwv_wwvh_bpm_overlap(self):
        assert SHARED_FREQS_MHZ == frozenset({2.5, 5.0, 10.0, 15.0})

    def test_modes_without_a_real_model_prediction_are_skipped(self):
        ms = [
            _m("WWV", 20.0, +0.1), _m("WWV", 25.0, +0.2),
            _m("WWV", 5.0, +18.0, mode="vacuum_fallback"),
        ]
        _kept, rejected = _reject(ms)
        assert rejected == []

    def test_a_nan_anchor_leaves_too_few_to_cross_check(self):
        """NaN drops 25 MHz, leaving one anchor — which the two-anchor
        rule then declines to trust rather than judging on it alone."""
        ms = [
            _m("WWV", 20.0, +0.1), _m("WWV", 25.0, float("nan")),
            _m("WWV", 5.0, +17.6),
        ]
        _kept, rejected = _reject(ms)
        assert rejected == []

    def test_nan_on_a_shared_channel_is_simply_ignored(self):
        ms = [
            _m("WWV", 20.0, +0.1), _m("WWV", 25.0, +0.2),
            _m("WWV", 10.0, float("nan")), _m("WWV", 5.0, +17.6),
        ]
        _kept, rejected = _reject(ms)
        assert [r.frequency_mhz for r in rejected] == [5.0]

    def test_threshold_is_the_shared_constant(self):
        ms = [
            _m("WWV", 20.0, 0.0), _m("WWV", 25.0, 0.0),
            _m("WWV", 5.0, CROSS_FREQ_THRESHOLD_MS + 0.5),
            _m("WWV", 10.0, CROSS_FREQ_THRESHOLD_MS - 0.5),
        ]
        _kept, rejected = _reject(ms)
        assert [r.frequency_mhz for r in rejected] == [5.0]

    def test_empty_input(self):
        assert _reject([]) == ([], [])
