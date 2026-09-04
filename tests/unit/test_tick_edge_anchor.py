"""Step 0.5(b) (docs/design/HOST_CLOCK_INTEGRITY.md): the per-second tick
search is anchored on the measured minute-marker onset, not on the sample
the host-clock label names.

2026-09-04: with the search window (±20 ms) centred on the host label, a
host clock 380 ms off UTC put the real ticks outside every window; the
correlator returned threshold-level junk centred where it was told to look
and fusion admitted a d_clock of +2 ms while NTP read −500 ms.  These tests
build that minute synthetically and check that the anchored search finds
the ticks where the signal put them, reports the walk as the error it is,
falls back honestly when the anchor is wrong, and refuses to vouch for a
label-anchored ensemble whose scatter is the window's, not the ticks'.
"""
from __future__ import annotations

import numpy as np
import pytest

from hf_timestd.core.buffer_timing import BufferTiming
from hf_timestd.core.tick_edge_detector import EdgeEnsembleResult, TickEdgeDetector

SR = 24000
MINUTE = 1_800_000_000          # a UTC minute boundary (divisible by 60)
DELAY_S = 0.0045                # propagation delay, seconds
TRUE_SAMPLE0_UTC = MINUTE - 1.0  # buffer starts 1 s before the minute, truth


def _wwv_minute(noise_std: float = 0.05, seed: int = 7) -> np.ndarray:
    """62 s of AM audio: 5 ms 1000 Hz ticks at each second (skip 29, 59) and
    an 800 ms marker at second 0, each onset at utc_sec + DELAY_S in TRUTH."""
    rng = np.random.default_rng(seed)
    n = 62 * SR
    audio = noise_std * rng.standard_normal(n)
    t = np.arange(n) / SR
    for utc_sec in range(MINUTE - 1, MINUTE + 61):
        sim = utc_sec % 60
        if sim in (29, 59):
            continue
        dur = 0.800 if sim == 0 else 0.005
        onset = (utc_sec + DELAY_S) - TRUE_SAMPLE0_UTC
        i0 = int(round(onset * SR)); i1 = i0 + int(dur * SR)
        if 0 <= i0 and i1 <= n:
            audio[i0:i1] += np.cos(2 * np.pi * 1000.0 * t[i0:i1])
    return audio


def _timing(walk_s: float) -> BufferTiming:
    """The host label: sample0_utc off truth by walk_s (positive = the host
    clock reads late, so the label puts every sample later than it is)."""
    return BufferTiming(sample0_utc=TRUE_SAMPLE0_UTC + walk_s, sample_rate=SR,
                        source='rtp_gps', n_snapshots_used=1, jitter_ms=0.0)


def _true_marker_anchor():
    onset_sample = ((MINUTE + DELAY_S) - TRUE_SAMPLE0_UTC) * SR
    return (MINUTE, onset_sample)


@pytest.fixture(scope="module")
def audio():
    return _wwv_minute()


@pytest.fixture
def det():
    return TickEdgeDetector(sample_rate=SR)


class TestLabelAnchorUnderAWalk:
    def test_the_old_search_loses_the_ticks(self, det, audio):
        """Host label 300 ms off: the ±20 ms window around the label holds no
        tick.  Whatever it finds must not be admitted as timing."""
        res = det.detect_edges(audio, 'WWV', 0, _timing(0.300), DELAY_S,
                               is_dedicated_channel=True)
        if res is not None and res.n_detected >= 10:
            ok, _ = TickEdgeDetector.timing_admissible(res)
            assert not ok, (res.n_detected, res.sigma_single_ms, res.ensemble_timing_error_ms)
        else:
            assert res is None or res.n_detected < 10


class TestMarkerAnchorUnderAWalk:
    def test_anchored_search_finds_the_ticks_and_reports_the_walk(self, det, audio):
        res = det.detect_edges(audio, 'WWV', 0, _timing(0.300), DELAY_S,
                               is_dedicated_channel=True,
                               anchor_onset=_true_marker_anchor())
        assert res is not None
        assert res.anchor_source == 'minute_marker'
        assert res.n_detected >= 40, res.n_detected
        # The label put the samples 300 ms late, so every tick sits 300 ms
        # AFTER where the label expected it: the walk reads as +300 ms.
        assert abs(res.ensemble_timing_error_ms - 300.0) < 1.0, res.ensemble_timing_error_ms
        assert res.sigma_single_ms < 2.0
        ok, why = TickEdgeDetector.timing_admissible(res)
        assert ok, why

    def test_search_centres_follow_the_marker_grid(self, det, audio):
        res = det.detect_edges(audio, 'WWV', 0, _timing(0.300), DELAY_S,
                               is_dedicated_channel=True,
                               anchor_onset=_true_marker_anchor())
        _, anchor_sample = _true_marker_anchor()
        for tick in res.edges[:5]:
            expect = anchor_sample + (tick.utc_second - MINUTE) * SR
            assert abs(tick.search_center_sample - expect) <= 1
            # The label's own expectation sits 300 ms earlier.
            assert abs((tick.search_center_sample - tick.expected_sample) / SR - 0.300) < 0.001


class TestNoWalk:
    def test_label_anchor_still_works_when_the_clock_is_right(self, det, audio):
        res = det.detect_edges(audio, 'WWV', 0, _timing(0.0), DELAY_S,
                               is_dedicated_channel=True)
        assert res is not None and res.anchor_source == 'host_label'
        assert res.n_detected >= 40
        assert abs(res.ensemble_timing_error_ms) < 1.0
        assert TickEdgeDetector.timing_admissible(res)[0]

    def test_marker_anchor_agrees_with_the_label_when_both_are_right(self, det, audio):
        res = det.detect_edges(audio, 'WWV', 0, _timing(0.0), DELAY_S,
                               is_dedicated_channel=True,
                               anchor_onset=_true_marker_anchor())
        assert res.anchor_source == 'minute_marker'
        assert abs(res.ensemble_timing_error_ms) < 1.0


class TestWrongAnchor:
    def test_a_sidelobe_marker_is_not_confirmed_and_the_label_pass_stands(self, det, audio):
        """A marker locked 370 ms early (the bee1 2026-05-20 case) would drag
        the grid between the ticks.  The ticks refuse it; the label pass,
        with a correct clock, still finds them at 0 ms — so the engine's
        edge-vs-corr cross-check keeps its teeth."""
        sec, onset = _true_marker_anchor()
        res = det.detect_edges(audio, 'WWV', 0, _timing(0.0), DELAY_S,
                               is_dedicated_channel=True,
                               anchor_onset=(sec, onset - 0.370 * SR))
        assert res is not None
        assert res.anchor_source == 'host_label'
        assert res.n_detected >= 40
        assert abs(res.ensemble_timing_error_ms) < 1.0


class TestTimingAdmissible:
    def _res(self, anchor, sigma):
        return EdgeEnsembleResult(station='WWV', frequency_hz=1000.0, minute_number=0,
                                  ensemble_timing_error_ms=2.0, ensemble_uncertainty_ms=0.5,
                                  ensemble_n_edges=40, n_attempted=57, n_detected=40, n_clean=40,
                                  mean_edge_snr_db=9.5, confidence=0.8,
                                  anchor_source=anchor, sigma_single_ms=sigma)

    def test_marker_anchored_is_admissible(self):
        assert TickEdgeDetector.timing_admissible(self._res('minute_marker', 11.0))[0]

    def test_label_anchored_with_tick_like_scatter_is_admissible(self):
        assert TickEdgeDetector.timing_admissible(self._res('host_label', 2.5))[0]

    def test_label_anchored_with_window_like_scatter_is_refused(self):
        ok, why = TickEdgeDetector.timing_admissible(self._res('host_label', 11.0))
        assert not ok and 'window' in why


class TestServiceAdmissibility:
    def test_service_refuses_window_like_label_anchored_ticks(self):
        from hf_timestd.core.metrology_service import MetrologyService
        ok, why = MetrologyService.tick_measurement_admissible(
            n_edges=40, mean_snr_db=12.0, min_snr_db=10.0,
            anchor_source='host_label', sigma_single_ms=11.0)
        assert not ok and 'window' in why
        ok, _ = MetrologyService.tick_measurement_admissible(
            n_edges=40, mean_snr_db=12.0, min_snr_db=10.0,
            anchor_source='minute_marker', sigma_single_ms=11.0)
        assert ok
        # Old callers that pass no anchor keep the old answer.
        assert MetrologyService.tick_measurement_admissible(40, 12.0, 10.0)[0]


class TestEngineAnchors:
    def test_marker_anchor_from_measurements(self, tmp_path):
        from hf_timestd.core.metrology_engine import MetrologyEngine
        eng = MetrologyEngine(raw_buffer_dir=tmp_path / "raw", output_dir=tmp_path / "out",
                              channel_name="WWV_20000", frequency_hz=20_000_000,
                              receiver_grid="EM38", precise_lat=38.9, precise_lon=-92.1)
        ms = [
            {'station': 'WWV', 'detected': True, 'utc_second': MINUTE, 'arrival_ms': 1004.5, 'corr_snr_db': 14.0},
            {'station': 'WWV', 'detected': True, 'utc_second': MINUTE, 'arrival_ms': 1300.0, 'corr_snr_db': 6.0},   # weaker duplicate
            {'station': 'WWV', 'detected': True, 'utc_second': MINUTE + 7, 'arrival_ms': 8004.5, 'corr_snr_db': 20.0},  # not second 0
            {'station': 'WWVH', 'detected': False, 'utc_second': MINUTE, 'arrival_ms': 1020.0, 'corr_snr_db': 3.0},
            {'station': 'BPM', 'detected': True, 'utc_second': MINUTE, 'arrival_ms': 1040.0,
             'detection_method': 'edge_ensemble', 'corr_snr_db': 9.0},  # a synth, not a marker
        ]
        anchors = eng._minute_marker_anchors(ms)
        assert set(anchors) == {'WWV'}
        sec, sample = anchors['WWV']
        assert sec == MINUTE
        assert abs(sample - 1004.5 * eng.sample_rate / 1000.0) < 1e-6
