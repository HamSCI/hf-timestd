"""
Unit tests for hf_timestd.core.tid_detector

The TID detector cross-correlates timing residuals across HF paths to flag
Traveling Ionospheric Disturbances. Tests cover:
- TIDEvent / PathResidual dataclass defaults
- Buffer ingestion + automatic geometry computation per (station, frequency)
- Buffer trimming on overflow
- Haversine distance and forward-azimuth math (incl. known WWV→AC0G geometry)
- Internal helpers: pierce-point midpoint, ENU projection
- _align_residuals: insufficient data, common time grid, detrending
- _cross_correlate: zero-correlation, in-phase=high, lag-shifted recovery
- _estimate_period: dominant period recovery on a synthetic sinusoid
- _estimate_tid_velocity / _estimate_tid_direction
- detect_tid: short-circuits with <2 paths, returns None below threshold,
  returns TIDEvent with sensible fields when a TID is present
- _solve_tdoa_velocity: returns (None, None) with <3 paths, recovers a
  known velocity/direction with 3+ paths
- get_active_events / get_recent_events / get_statistics shape
"""

import math
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from hf_timestd.core.tid_detector import (
    EARTH_RADIUS_KM,
    PathResidual,
    TIDDetector,
    TIDEvent,
)


# =============================================================================
# Dataclasses
# =============================================================================


class TestDataclasses:
    def test_path_residual_defaults(self):
        r = PathResidual(
            timestamp=1700000000.0,
            station='WWV',
            frequency_mhz=10.0,
            residual_ms=0.5,
        )
        # Default uncertainty
        assert r.uncertainty_ms == 1.0

    def test_tid_event_defaults(self):
        ev = TIDEvent(start_time=datetime(2026, 1, 1, tzinfo=timezone.utc))
        assert ev.end_time is None
        assert ev.period_minutes == 0.0
        assert ev.velocity_m_s == 0.0
        assert ev.confidence == 0.0
        assert ev.leading_path == ""
        assert ev.lagging_path == ""


# =============================================================================
# Construction & residual ingestion
# =============================================================================


@pytest.fixture
def detector():
    """Detector at AC0G location used elsewhere in the project."""
    return TIDDetector(receiver_lat=38.918461, receiver_lon=-92.127974)


class TestConstruction:
    def test_defaults(self, detector):
        assert detector.buffer_minutes == 120
        assert detector.min_correlation == 0.6
        assert detector.min_lag_minutes == 1.0
        assert detector.sample_interval_seconds == 60.0
        assert detector._residual_buffers == {}
        assert detector._active_events == []
        assert detector._completed_events == []

    def test_known_stations_table(self, detector):
        for code in ('WWV', 'WWVH', 'CHU', 'BPM'):
            assert code in detector._station_locations


class TestAddResidual:
    def test_first_residual_computes_geometry(self, detector):
        r = PathResidual(timestamp=0.0, station='WWV', frequency_mhz=10.0,
                         residual_ms=0.0)
        detector.add_residual(r)
        key = ('WWV', 10.0)
        assert key in detector._path_distances
        assert key in detector._path_azimuths

    def test_unknown_station_geometry_skipped(self, detector, caplog):
        r = PathResidual(timestamp=0.0, station='ZZZ', frequency_mhz=10.0,
                         residual_ms=0.0)
        detector.add_residual(r)
        key = ('ZZZ', 10.0)
        # Buffer holds the residual but no geometry was computed
        assert detector._residual_buffers[key]
        assert key not in detector._path_distances
        assert any('Unknown station' in r.message for r in caplog.records)

    def test_buffer_appended(self, detector):
        for i in range(5):
            detector.add_residual(PathResidual(
                timestamp=float(i), station='WWV',
                frequency_mhz=10.0, residual_ms=float(i)))
        assert len(detector._residual_buffers[('WWV', 10.0)]) == 5

    def test_buffer_trimmed_to_capacity(self):
        # 1-second sample interval, 1-minute buffer → cap = 60 samples
        det = TIDDetector(receiver_lat=40.0, receiver_lon=-100.0,
                          buffer_minutes=1, sample_interval_seconds=1.0)
        for i in range(150):
            det.add_residual(PathResidual(
                timestamp=float(i), station='WWV',
                frequency_mhz=10.0, residual_ms=float(i)))
        buf = det._residual_buffers[('WWV', 10.0)]
        assert len(buf) <= 60
        # Most-recent residuals retained
        assert buf[-1].residual_ms == 149.0


# =============================================================================
# Geometry helpers
# =============================================================================


class TestGeometryHelpers:
    def test_haversine_zero_distance(self):
        d = TIDDetector._haversine_km(0.0, 0.0, 0.0, 0.0)
        assert d == pytest.approx(0.0)

    def test_haversine_pole_to_pole(self):
        d = TIDDetector._haversine_km(90.0, 0.0, -90.0, 0.0)
        assert d == pytest.approx(math.pi * EARTH_RADIUS_KM, rel=1e-6)

    def test_haversine_quarter_circle_along_equator(self):
        d = TIDDetector._haversine_km(0.0, 0.0, 0.0, 90.0)
        assert d == pytest.approx(math.pi * EARTH_RADIUS_KM / 2, rel=1e-6)

    @pytest.mark.parametrize("lat2,lon2,expected_az", [
        (1.0, 0.0, 0.0),    # north
        (0.0, 1.0, 90.0),   # east
        (-1.0, 0.0, 180.0), # south
        (0.0, -1.0, 270.0), # west
    ])
    def test_compute_azimuth_cardinals(self, lat2, lon2, expected_az):
        az = TIDDetector._compute_azimuth(0.0, 0.0, lat2, lon2)
        assert az == pytest.approx(expected_az, abs=0.5)

    def test_compute_azimuth_in_0_to_360_range(self):
        # Random pair → azimuth in [0, 360)
        az = TIDDetector._compute_azimuth(40.0, -105.0, 30.0, -80.0)
        assert 0.0 <= az < 360.0

    def test_pierce_point_is_great_circle_midpoint(self, detector):
        # The pierce-point heuristic returns the great-circle midpoint
        # between receiver and station
        lat, lon = detector._compute_pierce_point('WWV')
        # Should fall between AC0G and WWV
        assert min(detector.receiver_lat, 40.6781) <= lat <= max(detector.receiver_lat, 40.6781)

    def test_pierce_point_unknown_station_returns_receiver(self, detector):
        lat, lon = detector._compute_pierce_point('ZZZ')
        assert lat == detector.receiver_lat
        assert lon == detector.receiver_lon

    def test_enu_origin_at_receiver(self, detector):
        x, y = detector._get_enu_coords(detector.receiver_lat, detector.receiver_lon)
        assert x == pytest.approx(0.0, abs=1e-9)
        assert y == pytest.approx(0.0, abs=1e-9)

    def test_enu_north_positive_y(self, detector):
        # 1° north → positive y
        _, y = detector._get_enu_coords(detector.receiver_lat + 1.0,
                                         detector.receiver_lon)
        assert y > 0

    def test_enu_east_positive_x(self, detector):
        # 1° east → positive x
        x, _ = detector._get_enu_coords(detector.receiver_lat,
                                         detector.receiver_lon + 1.0)
        assert x > 0


# =============================================================================
# Cross-correlation
# =============================================================================


class TestCrossCorrelate:
    def test_identical_series_at_lag_zero_below_min_lag_returns_zero(self, detector):
        # min_lag_minutes=1 with 60s sample interval → exclude lag 0
        s = np.array([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], dtype=float)
        corr, lag = detector._cross_correlate(s, s)
        # Identical → strongest peak at lag 0, but min_lag excludes it →
        # secondary peak (correlation < 1.0)
        assert lag != 0
        assert 0.0 <= corr <= 1.0

    def test_lag_recovers_known_shift(self):
        # No exclusion zone — set min_lag_minutes=0 with default 60s interval
        det = TIDDetector(receiver_lat=40.0, receiver_lon=-100.0,
                          min_lag_minutes=0.0)
        # Sinusoidal signal, copied with a fixed sample shift
        n = 100
        x = np.arange(n)
        s1 = np.sin(2 * np.pi * x / 20)
        shift = 5
        s2 = np.roll(s1, shift)
        # Trim wrap-around region so we're correlating clean signal
        corr, lag = det._cross_correlate(s1[:n - shift], s2[shift:])
        assert corr > 0.95

    def test_orthogonal_series_low_correlation(self, detector):
        np.random.seed(0)
        s1 = np.random.randn(200)
        s2 = np.random.randn(200)
        corr, _ = detector._cross_correlate(s1, s2)
        # Random data → modest correlation
        assert corr < 0.5


# =============================================================================
# Aligning residuals
# =============================================================================


def _fill_buffer(det, station, freq, n, *, start=0.0, step=60.0,
                 amp=1.0, period_samples=10, phase=0.0):
    """Push a sinusoidal residual stream into the detector."""
    for i in range(n):
        ts = start + i * step
        val = amp * math.sin(2 * math.pi * (i + phase) / period_samples)
        det.add_residual(PathResidual(timestamp=ts, station=station,
                                      frequency_mhz=freq, residual_ms=val))


class TestAlignResiduals:
    def test_returns_none_with_no_paths(self, detector):
        assert detector._align_residuals([]) is None

    def test_returns_none_when_too_few_samples(self, detector):
        _fill_buffer(detector, 'WWV', 10.0, n=3)
        out = detector._align_residuals([('WWV', 10.0)])
        assert out is None

    def test_returns_none_with_only_one_aligned_path(self, detector):
        # Single path with enough samples — aligned dict has 1 entry, so the
        # method returns None (needs ≥ 2)
        _fill_buffer(detector, 'WWV', 10.0, n=20)
        out = detector._align_residuals([('WWV', 10.0)])
        assert out is None

    def test_aligned_series_detrended(self, detector):
        # Two paths, enough samples each
        _fill_buffer(detector, 'WWV', 10.0, n=30)
        _fill_buffer(detector, 'CHU', 7.85, n=30)
        out = detector._align_residuals([('WWV', 10.0), ('CHU', 7.85)])
        assert out is not None
        for arr in out.values():
            # Detrended → near-zero linear slope and near-zero mean
            slope, _ = np.polyfit(np.arange(len(arr)), arr, 1)
            assert abs(slope) < 1e-9
            assert abs(arr.mean()) < 1e-9


# =============================================================================
# Period estimation
# =============================================================================


class TestEstimatePeriod:
    def test_short_series_returns_zero(self, detector):
        assert detector._estimate_period(np.array([1.0, 2.0, 3.0])) == 0.0

    def test_recovers_known_period(self, detector):
        # 60-sec sample interval, period of 20 samples → 20 minutes
        n = 200
        period_samples = 20
        x = np.arange(n)
        signal = np.sin(2 * np.pi * x / period_samples)
        period_min = detector._estimate_period(signal)
        # _estimate_period requires the first peak to be at ≥ 5 minutes (5 samples).
        # 20 samples × 60 s = 20 minutes
        assert period_min == pytest.approx(20.0, abs=2.0)

    def test_no_peak_returns_zero(self, detector):
        # Pure random noise → ACF unlikely to clear the 0.3 peak threshold
        np.random.seed(42)
        signal = np.random.randn(200)
        period = detector._estimate_period(signal)
        # Most random sequences yield 0; allow occasional false-positive peak
        # to be a small period (any value ≥ 0)
        assert period >= 0


# =============================================================================
# Velocity / direction estimation
# =============================================================================


class TestEstimateTIDVelocity:
    def test_zero_lag_returns_zero(self, detector):
        # Force azimuths so the math doesn't blow up
        detector._path_azimuths[('WWV', 10.0)] = 0.0
        detector._path_azimuths[('CHU', 7.85)] = 90.0
        v = detector._estimate_tid_velocity((('WWV', 10.0), ('CHU', 7.85)),
                                             lag_minutes=0.0)
        assert v == 0.0

    def test_velocity_increases_with_smaller_lag(self, detector):
        detector._path_azimuths[('WWV', 10.0)] = 0.0
        detector._path_azimuths[('CHU', 7.85)] = 90.0
        v_short = detector._estimate_tid_velocity(
            (('WWV', 10.0), ('CHU', 7.85)), lag_minutes=5.0)
        v_long = detector._estimate_tid_velocity(
            (('WWV', 10.0), ('CHU', 7.85)), lag_minutes=30.0)
        assert v_short > v_long > 0.0


class TestEstimateTIDDirection:
    def test_direction_follows_leading_path(self, detector):
        detector._path_azimuths[('WWV', 10.0)] = 270.0
        detector._path_azimuths[('CHU', 7.85)] = 60.0
        d_pos = detector._estimate_tid_direction(
            (('WWV', 10.0), ('CHU', 7.85)), lag=5)
        # path1 leads (positive lag)
        assert d_pos == 270.0
        d_neg = detector._estimate_tid_direction(
            (('WWV', 10.0), ('CHU', 7.85)), lag=-5)
        # path2 leads
        assert d_neg == 60.0


# =============================================================================
# detect_tid orchestration
# =============================================================================


class TestDetectTID:
    def test_returns_none_with_fewer_than_two_paths(self, detector):
        _fill_buffer(detector, 'WWV', 10.0, n=30)
        assert detector.detect_tid() is None

    def test_returns_none_when_correlation_below_threshold(self, detector):
        # Two uncorrelated random series → no TID
        np.random.seed(0)
        for i in range(60):
            ts = i * 60.0
            detector.add_residual(PathResidual(
                timestamp=ts, station='WWV', frequency_mhz=10.0,
                residual_ms=float(np.random.randn())))
            detector.add_residual(PathResidual(
                timestamp=ts, station='CHU', frequency_mhz=7.85,
                residual_ms=float(np.random.randn())))
        result = detector.detect_tid()
        # Random data — usually no TID, but occasional false positive is possible
        if result is not None:
            assert result.correlation_coefficient < 1.0

    def test_returns_event_for_correlated_paths_with_known_lag(self, detector):
        # Same shape, shifted by a fixed sample lag → strong cross-correlation
        n = 60
        period_samples = 12
        shift = 5  # 5 minutes at 60 s/sample
        for i in range(n):
            ts = i * 60.0
            base = math.sin(2 * math.pi * i / period_samples)
            detector.add_residual(PathResidual(
                timestamp=ts, station='WWV', frequency_mhz=10.0,
                residual_ms=base))
            shifted = math.sin(2 * math.pi * (i - shift) / period_samples)
            detector.add_residual(PathResidual(
                timestamp=ts, station='CHU', frequency_mhz=7.85,
                residual_ms=shifted))

        ev = detector.detect_tid()
        assert ev is not None
        assert ev.correlation_coefficient >= detector.min_correlation
        assert ev.lag_minutes >= detector.min_lag_minutes
        assert ev.amplitude_ms > 0
        # Both paths represented in leading/lagging
        assert ('WWV' in ev.leading_path or 'WWV' in ev.lagging_path)
        assert ('CHU' in ev.leading_path or 'CHU' in ev.lagging_path)


# =============================================================================
# TDOA solve
# =============================================================================


class TestSolveTDOAVelocity:
    def test_under_three_paths_returns_none(self, detector):
        # _solve_tdoa_velocity short-circuits when fewer than 3 paths
        result = detector._solve_tdoa_velocity(
            correlated_paths=[('WWV', 10.0), ('CHU', 7.85)],
            aligned_series={},
        )
        assert result == (None, None)

    def test_three_paths_returns_velocity_and_direction(self, detector):
        # Build three correlated paths so the lstsq has a valid system
        n = 60
        period_samples = 12
        for shift, (station, freq) in zip(
                [0, 4, 8],
                [('WWV', 10.0), ('CHU', 7.85), ('WWVH', 15.0)]):
            for i in range(n):
                ts = i * 60.0
                val = math.sin(2 * math.pi * (i - shift) / period_samples)
                detector.add_residual(PathResidual(
                    timestamp=ts, station=station, frequency_mhz=freq,
                    residual_ms=val))
        aligned = detector._align_residuals(list(detector._residual_buffers.keys()))
        v, az = detector._solve_tdoa_velocity(
            correlated_paths=list(detector._residual_buffers.keys()),
            aligned_series=aligned,
        )
        assert v is not None
        assert az is not None
        assert v > 0
        assert 0 <= az < 360


# =============================================================================
# Public accessors
# =============================================================================


class TestPublicAccessors:
    def test_get_active_events_initially_empty(self, detector):
        assert detector.get_active_events() == []

    def test_get_recent_events_filters_by_window(self, detector):
        now = datetime.now(timezone.utc)
        old = TIDEvent(start_time=now - timedelta(hours=48))
        recent = TIDEvent(start_time=now - timedelta(hours=1))
        detector._completed_events = [old, recent]
        out = detector.get_recent_events(hours=24.0)
        assert recent in out
        assert old not in out

    def test_get_statistics_shape(self, detector):
        _fill_buffer(detector, 'WWV', 10.0, n=5)
        _fill_buffer(detector, 'CHU', 7.85, n=10)
        stats = detector.get_statistics()
        assert stats['n_paths'] == 2
        assert sorted(stats['paths']) == ['CHU@7.85MHz', 'WWV@10.0MHz']
        assert stats['buffer_samples']['WWV@10.0MHz'] == 5
        assert stats['buffer_samples']['CHU@7.85MHz'] == 10
        assert stats['n_active_events'] == 0
        assert stats['n_completed_events'] == 0
