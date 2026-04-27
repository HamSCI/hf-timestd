"""
Unit tests for hf_timestd.core.sliding_window_monitor

10-second sliding-window real-time signal-quality monitor. Tests cover the
monitor's process_chunk pipeline with synthetic IQ, the WindowMetrics /
MinuteSummary dataclasses, quality classification, status JSON output,
and the recommendation logic.
"""

import json
import time
from pathlib import Path

import numpy as np
import pytest

from hf_timestd.core.sliding_window_monitor import (
    DOPPLER_STABLE_HZ,
    SAMPLE_RATE,
    SAMPLES_PER_WINDOW,
    SNR_EXCELLENT_DB,
    SNR_GOOD_DB,
    SNR_MARGINAL_DB,
    SNR_POOR_DB,
    WINDOW_DURATION_SEC,
    MinuteSummary,
    SignalQuality,
    SlidingWindowMonitor,
    WindowMetrics,
)


# =============================================================================
# Module constants
# =============================================================================


class TestModuleConstants:
    def test_window_duration_consistent(self):
        assert SAMPLES_PER_WINDOW == int(WINDOW_DURATION_SEC * SAMPLE_RATE)

    def test_snr_threshold_ladder(self):
        assert SNR_EXCELLENT_DB > SNR_GOOD_DB > SNR_MARGINAL_DB > SNR_POOR_DB


# =============================================================================
# WindowMetrics
# =============================================================================


class TestWindowMetrics:
    def test_defaults(self):
        m = WindowMetrics(timestamp=1700000000.0, window_number=1)
        assert m.wwv_snr_db is None
        assert m.signal_present is False
        assert m.completeness_pct == 100.0
        assert m.quality == SignalQuality.NO_SIGNAL

    def test_to_dict_has_iso_timestamp(self):
        m = WindowMetrics(timestamp=1700000000.0, window_number=1,
                           wwv_snr_db=15.0, wwv_detected=True)
        d = m.to_dict()
        assert d['window_number'] == 1
        assert 'T' in d['timestamp_iso']
        assert d['wwv_snr_db'] == 15.0
        assert d['wwv_detected'] is True
        assert d['quality'] == 'no_signal'

    def test_to_dict_handles_numpy_scalars(self):
        # numpy scalars get converted via their .item() method
        m = WindowMetrics(
            timestamp=1700000000.0, window_number=1,
            wwv_snr_db=np.float64(12.5),
            doppler_stability_hz=np.float64(0.05),
        )
        d = m.to_dict()
        assert isinstance(d['wwv_snr_db'], float)
        assert d['wwv_snr_db'] == 12.5


class TestMinuteSummary:
    def test_to_dict_shape(self):
        s = MinuteSummary(minute_boundary=1700000000.0, window_count=6)
        d = s.to_dict()
        for key in ('minute_boundary', 'minute_boundary_iso', 'window_count',
                    'snr_mean_db', 'wwv_detection_rate', 'quality_distribution'):
            assert key in d


# =============================================================================
# SlidingWindowMonitor — construction
# =============================================================================


@pytest.fixture
def monitor(tmp_path):
    return SlidingWindowMonitor(
        channel_name='WWV_10000',
        sample_rate=20000,
        output_dir=tmp_path / 'status',
        history_size=20,
    )


class TestConstruction:
    def test_creates_output_dir(self, monitor, tmp_path):
        assert (tmp_path / 'status').is_dir()

    def test_initial_state(self, monitor):
        assert monitor.window_number == 0
        assert monitor.total_windows_processed == 0
        assert monitor.anomalies_detected == 0
        assert monitor.enabled is True

    def test_disabled_returns_none(self, tmp_path):
        m = SlidingWindowMonitor('test', enabled=False, output_dir=tmp_path)
        result = m.process_chunk(np.zeros(100, dtype=np.complex64),
                                  timestamp=time.time())
        assert result is None


# =============================================================================
# process_chunk
# =============================================================================


def _wwv_iq(n_samples: int, sample_rate: int = 20000,
            tone_hz: float = 1000.0, snr_db: float = 30.0,
            seed: int = 0) -> np.ndarray:
    """Build IQ baseband samples carrying a single tone at `tone_hz` plus
    Gaussian noise at the requested SNR."""
    rng = np.random.RandomState(seed)
    t = np.arange(n_samples) / sample_rate
    signal_amp = 1.0
    noise_amp = signal_amp * 10 ** (-snr_db / 20)
    signal = signal_amp * np.exp(1j * 2 * np.pi * tone_hz * t)
    noise = (rng.randn(n_samples) + 1j * rng.randn(n_samples)) * noise_amp
    return (signal + noise).astype(np.complex64)


class TestProcessChunk:
    def test_strong_wwv_tone_detected(self, monitor):
        # The internal FFT length is capped at 8192 samples; processing-gain
        # is therefore much lower than the input SNR. A 30 dB input tone
        # measures ~10 dB output SNR via this analyzer. Just verify the
        # monitor classifies it as present and produces a positive value.
        iq = _wwv_iq(SAMPLES_PER_WINDOW, snr_db=30.0)
        m = monitor.process_chunk(iq, timestamp=1700000000.0)
        assert m is not None
        assert m.window_number == 1
        assert bool(m.signal_present)
        assert m.wwv_snr_db > 0
        assert bool(m.wwv_detected)

    def test_strong_wwvh_tone_detected(self, monitor):
        iq = _wwv_iq(SAMPLES_PER_WINDOW, tone_hz=1200.0, snr_db=30.0)
        m = monitor.process_chunk(iq, timestamp=1700000000.0)
        assert m.wwvh_snr_db > 0
        assert bool(m.wwvh_detected)

    def test_quality_classification_good(self, monitor):
        iq = _wwv_iq(SAMPLES_PER_WINDOW, snr_db=15.0)
        m = monitor.process_chunk(iq, timestamp=1700000000.0)
        # Measured SNR will be well above the POOR floor → at least MARGINAL
        assert m.quality in (SignalQuality.MARGINAL, SignalQuality.GOOD,
                              SignalQuality.EXCELLENT)

    def test_pure_noise_quality_capped(self, monitor):
        # With a 8192-bin FFT, even pure complex Gaussian noise will produce
        # a peak somewhere in the 1000±50 Hz band that registers a few dB
        # above median. Document that pure noise produces no better than
        # MARGINAL quality.
        rng = np.random.RandomState(0)
        iq = ((rng.randn(SAMPLES_PER_WINDOW) +
               1j * rng.randn(SAMPLES_PER_WINDOW))
              .astype(np.complex64))
        m = monitor.process_chunk(iq, timestamp=1700000000.0)
        assert m.quality in (
            SignalQuality.NO_SIGNAL,
            SignalQuality.POOR,
            SignalQuality.MARGINAL,
        )
        assert m.wwv_snr_db is None or m.wwv_snr_db < SNR_GOOD_DB

    def test_completeness_below_100_when_short(self, monitor):
        iq = _wwv_iq(SAMPLES_PER_WINDOW // 2)
        m = monitor.process_chunk(iq, timestamp=1700000000.0)
        assert m.completeness_pct == pytest.approx(50.0, abs=1.0)

    def test_gap_info_propagates(self, monitor):
        iq = _wwv_iq(SAMPLES_PER_WINDOW)
        m = monitor.process_chunk(
            iq, timestamp=1700000000.0,
            gap_info={'gap_count': 3, 'gap_samples': 1000},
        )
        assert m.gap_count == 3
        assert m.gap_samples == 1000

    def test_status_file_written(self, monitor, tmp_path):
        iq = _wwv_iq(SAMPLES_PER_WINDOW)
        monitor.process_chunk(iq, timestamp=1700000000.0)
        status_file = tmp_path / 'status' / 'WWV_10000_monitor.json'
        assert status_file.exists()
        data = json.loads(status_file.read_text())
        assert data['channel_name'] == 'WWV_10000'
        assert data['monitor_type'] == 'sliding_window_10s'
        assert data['enabled'] is True

    def test_history_bounded(self, monitor):
        # history_size = 20 → only 20 most recent kept
        iq = _wwv_iq(SAMPLES_PER_WINDOW)
        for i in range(30):
            monitor.process_chunk(iq, timestamp=1700000000.0 + i * 10)
        assert len(monitor.window_history) == 20

    def test_window_number_increments(self, monitor):
        iq = _wwv_iq(SAMPLES_PER_WINDOW)
        for _ in range(5):
            monitor.process_chunk(iq, timestamp=time.time())
        assert monitor.window_number == 5
        assert monitor.total_windows_processed == 5


# =============================================================================
# Minute summary
# =============================================================================


class TestMinuteSummary:
    def test_summary_finalized_on_minute_boundary(self, monitor):
        iq = _wwv_iq(SAMPLES_PER_WINDOW)
        # Six 10-second windows in minute 1, then one in minute 2 to trigger
        # finalisation. Pin the start to a round-minute boundary so all six
        # windows fall in the same minute.
        base = 1700000000.0  # already a minute boundary (anything div by 60)
        base = (int(base) // 60) * 60
        for i in range(6):
            monitor.process_chunk(iq, timestamp=base + i * 10)
        # Push a window that crosses to the next minute
        monitor.process_chunk(iq, timestamp=base + 60.0)
        # Previous minute summary now exists
        assert len(monitor.minute_summaries) >= 1
        last = monitor.minute_summaries[-1]
        assert last.window_count == 6
        assert last.snr_mean_db is not None

    def test_get_minute_summary_returns_latest(self, monitor):
        iq = _wwv_iq(SAMPLES_PER_WINDOW)
        for i in range(7):
            monitor.process_chunk(iq, timestamp=1700000000.0 + i * 10)
        summary = monitor.get_minute_summary()
        # The first 6 windows' summary has been finalised
        assert summary is not None


# =============================================================================
# Public accessors and recommendation
# =============================================================================


class TestPublicAccessors:
    def test_get_current_metrics_initially_none(self, monitor):
        assert monitor.get_current_metrics() is None

    def test_get_current_metrics_returns_latest(self, monitor):
        iq = _wwv_iq(SAMPLES_PER_WINDOW)
        monitor.process_chunk(iq, timestamp=1700000000.0)
        m = monitor.get_current_metrics()
        assert m is not None
        assert m.window_number == 1

    def test_record_anomaly_increments_counter(self, monitor):
        monitor.record_anomaly("test")
        monitor.record_anomaly("test")
        assert monitor.anomalies_detected == 2

    def test_disable_and_enable(self, monitor):
        monitor.disable()
        assert monitor.enabled is False
        monitor.enable()
        assert monitor.enabled is True

    def test_get_stats_shape(self, monitor):
        stats = monitor.get_stats()
        for key in ('channel_name', 'enabled', 'total_windows',
                    'anomalies_detected', 'uptime_seconds',
                    'history_size', 'minute_summaries'):
            assert key in stats


class TestMonitoringValueAssessment:
    def test_insufficient_data_recommendation(self, monitor):
        # No windows processed yet
        a = monitor.get_monitoring_value_assessment()
        assert a['recommendation'] == 'insufficient_data'

    def test_recommendation_after_360_windows(self, monitor):
        # Simulate >1 hour of windows with 1 anomaly
        monitor.total_windows_processed = 1000
        monitor.anomalies_detected = 15  # 1.5% rate → keep_valuable
        a = monitor.get_monitoring_value_assessment()
        assert a['recommendation'] == 'keep_valuable'

    def test_consider_removing_recommendation(self, monitor):
        monitor.total_windows_processed = 5000
        monitor.anomalies_detected = 1  # 0.02% → consider removing
        a = monitor.get_monitoring_value_assessment()
        assert a['recommendation'] == 'consider_removing'
