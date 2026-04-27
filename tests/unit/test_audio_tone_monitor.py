"""
Unit tests for hf_timestd.core.audio_tone_monitor

AudioToneMonitor measures power at 400/500/600/700/1000/1200 Hz on a minute
of IQ samples and infers which station's BCD signature dominates. Tests cover:
- Construction
- AudioToneAnalysis dataclass shape
- _measure_tone_power: zero on empty band, additivity in band
- analyze_minute on hand-built signals:
  * Empty/silent input (insufficient data → empty result)
  * Strong 500 Hz tone → power_500_hz_db high, intermod analysis sensible
- analyze_bcd_tone_correlation placeholder return shape
"""

import numpy as np
import pytest

from hf_timestd.core.audio_tone_monitor import (
    AudioToneAnalysis,
    AudioToneMonitor,
    analyze_bcd_tone_correlation,
)


# =============================================================================
# Construction & dataclass
# =============================================================================


class TestConstruction:
    def test_constructor_defaults(self):
        m = AudioToneMonitor('WWV_10000')
        assert m.channel_name == 'WWV_10000'
        assert m.sample_rate == 20000
        # Tone table includes the canonical six monitored tones
        for name in ('400_hz', '500_hz', '600_hz', '700_hz',
                     '1000_hz', '1200_hz'):
            assert name in m.tone_freqs

    def test_custom_sample_rate(self):
        m = AudioToneMonitor('WWV_10000', sample_rate=12000)
        assert m.sample_rate == 12000


class TestAudioToneAnalysis:
    def test_construction_with_required_fields(self):
        a = AudioToneAnalysis(
            minute_boundary=1700000000,
            power_500_hz_db=10.0, power_600_hz_db=-5.0,
            power_400_hz_db=2.0, power_700_hz_db=-2.0,
            power_1000_hz_db=20.0, power_1200_hz_db=-10.0,
            ratio_500_600_db=15.0, ratio_400_700_db=4.0,
            wwv_intermod_500_to_600_db=2.0,
            wwvh_intermod_600_to_500_db=-2.0,
            intermod_dominant_station='WWV',
            intermod_confidence=0.5,
        )
        assert a.minute_boundary == 1700000000
        assert a.intermod_dominant_station == 'WWV'


# =============================================================================
# _measure_tone_power
# =============================================================================


class TestMeasureTonePower:
    @pytest.fixture
    def monitor(self):
        return AudioToneMonitor('test', sample_rate=20000)

    def test_zero_when_band_outside_freqs(self, monitor):
        # 1 Hz frequency resolution
        freqs = np.arange(0, 1000, dtype=float)
        fft_power = np.ones(len(freqs))
        # Target way outside the frequency range
        assert monitor._measure_tone_power(fft_power, freqs, 5000) == 0.0

    def test_sums_within_bandwidth(self, monitor):
        # Place equal power on every bin; the integration should equal
        # the bandwidth (10 bins).
        freqs = np.arange(0, 1000, dtype=float)
        fft_power = np.ones(len(freqs))
        # ±5 Hz around 500 → bins 495..505 (11 bins inclusive)
        result = monitor._measure_tone_power(fft_power, freqs, 500)
        assert 10 <= result <= 11


# =============================================================================
# analyze_minute
# =============================================================================


class TestAnalyzeMinute:
    @pytest.fixture
    def monitor(self):
        # Use a smaller sample rate so synthetic IQ stays cheap
        return AudioToneMonitor('test', sample_rate=20000)

    def test_empty_signal_returns_empty_result(self, monitor):
        # Empty IQ → no segments fit → _empty_result() path
        iq = np.zeros(10, dtype=np.complex64)
        result = monitor.analyze_minute(iq, minute_boundary=0)
        # Sentinel values from _empty_result
        assert result.power_500_hz_db == -99.0
        assert result.power_600_hz_db == -99.0
        assert result.intermod_dominant_station is None
        assert result.intermod_confidence == 0.0

    def test_strong_500hz_tone_yields_high_power(self, monitor):
        # 60 seconds of synthetic IQ with a 500 Hz AM modulation on the
        # carrier. The AudioToneMonitor demodulates by |IQ| and detects
        # tones in the audio band.
        sr = monitor.sample_rate
        n = 60 * sr
        t = np.arange(n) / sr
        # Carrier at DC + 500 Hz AM modulation
        envelope = 1.0 + 0.5 * np.sin(2 * np.pi * 500 * t)
        iq = envelope.astype(np.complex64)
        # Pick a minute boundary in WWV_ONLY_500/600 schedule territory.
        # Minute 4 is in TONE_SCHEDULE_500_600 with WWV=500, WWVH=600.
        minute_boundary = 4 * 60
        result = monitor.analyze_minute(iq, minute_boundary)
        # 500 Hz peak should dominate over 600 Hz noise floor
        assert result.power_500_hz_db > result.power_600_hz_db
        assert result.minute_boundary == minute_boundary


# =============================================================================
# analyze_bcd_tone_correlation
# =============================================================================


class TestAnalyzeBCDToneCorrelation:
    def test_returns_zero_correlations_placeholder(self):
        iq = np.zeros(60 * 20000, dtype=np.complex64)
        result = analyze_bcd_tone_correlation(iq, sample_rate=20000,
                                                minute_timestamp=0.0)
        assert result == {
            'bcd_500hz_correlation': 0.0,
            'bcd_600hz_correlation': 0.0,
        }
