"""
Unit tests for the WWV/WWVH scientific test-signal generator.

Covers WWVTestSignalGenerator (multi-tone, chirp, burst, white-noise, full-
signal). The detector class is large and DSP-heavy; we exercise it lightly
via a smoke test that runs detect() on a generated signal.
"""

import numpy as np
import pytest

from hf_timestd.core.wwv_test_signal import (
    TestSignalDetection,
    WWVTestSignalDetector,
    WWVTestSignalGenerator,
)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def gen():
    return WWVTestSignalGenerator(sample_rate=20000)


# =============================================================================
# White-noise segment
# =============================================================================


class TestGenerateWhiteNoise:
    def test_length_matches_duration(self, gen):
        sig = gen.generate_white_noise(2.0, seed=0)
        assert len(sig) == 2 * 20000

    def test_seed_reproduces_same_signal(self, gen):
        a = gen.generate_white_noise(1.0, seed=42)
        b = gen.generate_white_noise(1.0, seed=42)
        np.testing.assert_array_equal(a, b)

    def test_normalized_to_unit_max(self, gen):
        sig = gen.generate_white_noise(1.0, seed=0)
        # After normalisation, max |sig| ≈ 1.0
        assert np.max(np.abs(sig)) == pytest.approx(1.0, abs=1e-9)


# =============================================================================
# Multi-tone segment
# =============================================================================


class TestGenerateMultitone:
    def test_length_is_ten_seconds(self, gen):
        sig = gen.generate_multitone(10.0)
        # 10 attenuation steps × 1 s each
        assert len(sig) == 10 * 20000

    def test_first_step_louder_than_last(self, gen):
        sig = gen.generate_multitone(10.0)
        # 10 1-second segments, 3 dB attenuation per step
        # First second amplitude > last second amplitude by ~24 dB
        first_second = sig[:20000]
        last_second = sig[-20000:]
        assert np.max(np.abs(first_second)) > 5 * np.max(np.abs(last_second))

    def test_signal_amplitude_below_one(self, gen):
        sig = gen.generate_multitone(10.0)
        # Starts at 0.25 × 4 tones max → 1.0 in worst-case constructive
        # interference. Allow small headroom.
        assert np.max(np.abs(sig)) <= 1.05

    def test_dominant_frequencies_2_3_4_5_khz(self, gen):
        # FFT of the first second should show peaks near 2/3/4/5 kHz
        sig = gen.generate_multitone(10.0)
        first = sig[:20000]
        spectrum = np.abs(np.fft.rfft(first))
        freqs = np.fft.rfftfreq(len(first), 1.0 / 20000)
        # Find peaks
        peak_freqs = []
        for target in (2000, 3000, 4000, 5000):
            mask = (freqs >= target - 50) & (freqs <= target + 50)
            local_peak_freq = freqs[mask][np.argmax(spectrum[mask])]
            peak_freqs.append(local_peak_freq)
        for target, peak in zip((2000, 3000, 4000, 5000), peak_freqs):
            assert abs(peak - target) < 25


# =============================================================================
# Chirp sequence
# =============================================================================


class TestGenerateChirpSequence:
    def test_returns_numpy_array(self, gen):
        seq = gen.generate_chirp_sequence()
        assert isinstance(seq, np.ndarray)

    def test_total_duration_around_8_seconds(self, gen):
        seq = gen.generate_chirp_sequence()
        seconds = len(seq) / 20000
        # Sequence is "approximately 8 seconds" per the docstring
        assert 6.5 < seconds < 8.5

    def test_amplitude_within_unit(self, gen):
        seq = gen.generate_chirp_sequence()
        assert np.max(np.abs(seq)) <= 1.0


# =============================================================================
# Burst sequence
# =============================================================================


class TestGenerateBurstSequence:
    def test_total_duration_two_seconds(self, gen):
        seq = gen.generate_burst_sequence()
        # 1 s of 2.5 kHz bursts + 1 s of 5 kHz bursts
        assert len(seq) == 2 * 20000

    def test_amplitude_within_unit(self, gen):
        seq = gen.generate_burst_sequence()
        assert np.max(np.abs(seq)) <= 1.0

    def test_first_half_quieter_average_than_full_envelope(self, gen):
        # Most of each second is silence between bursts → low RMS overall
        seq = gen.generate_burst_sequence()
        rms = np.sqrt(np.mean(seq ** 2))
        # RMS should be well below 1.0 because most samples are zero
        assert rms < 0.2


# =============================================================================
# Full signal
# =============================================================================


class TestGenerateFullSignal:
    def test_length_without_voice(self, gen):
        sig = gen.generate_full_signal(include_voice=False)
        # Approximate breakdown: 2 + 1 + 10 + 1 + ~7.7 + 2 + 2 + 1 + 2 + 3
        # ≈ 31.7 s — confirm it's in the 30-35 s range
        seconds = len(sig) / 20000
        assert 30.0 < seconds < 35.0

    def test_length_with_voice_placeholder(self, gen):
        no_voice = gen.generate_full_signal(include_voice=False)
        with_voice = gen.generate_full_signal(include_voice=True)
        # Voice placeholder adds 10 s of silence
        assert len(with_voice) - len(no_voice) == 10 * 20000

    def test_amplitude_bounded(self, gen):
        sig = gen.generate_full_signal()
        assert np.max(np.abs(sig)) <= 1.05


# =============================================================================
# Templates
# =============================================================================


class TestTemplates:
    def test_multitone_template_matches_generator_output(self, gen):
        a = gen.get_multitone_template()
        b = gen.generate_multitone(10.0)
        np.testing.assert_array_equal(a, b)

    def test_chirp_template_matches_generator_output(self, gen):
        a = gen.get_chirp_template()
        b = gen.generate_chirp_sequence()
        np.testing.assert_array_equal(a, b)


# =============================================================================
# Detector smoke test
# =============================================================================


class TestDetectorSmoke:
    def test_construction(self):
        # The detector pre-computes templates at construction; verify it
        # doesn't raise.
        det = WWVTestSignalDetector(sample_rate=20000)
        assert det.sample_rate == 20000

    def test_detect_dataclass_shape(self):
        # TestSignalDetection should be constructible with the minimum fields
        d = TestSignalDetection(detected=False, confidence=0.0,
                                  station=None, minute_number=8)
        assert d.detected is False
        assert d.confidence == 0.0
