"""
Unit tests for hf_timestd.core.standard_signal_generator

StandardTimeSignalGenerator synthesizes ground-truth audio for ticks,
markers, BCD, and CHU AFSK across all four broadcast stations.
"""

import numpy as np
import pytest

from hf_timestd.core.standard_signal_generator import (
    SignalConfig,
    StandardTimeSignalGenerator,
)


# =============================================================================
# Module data
# =============================================================================


class TestSignalConfig:
    def test_construction_with_defaults(self):
        c = SignalConfig(tick_freq=1000.0, tick_duration_sec=0.005,
                          marker_freq=1000.0, marker_duration_sec=0.8)
        assert c.bcd_enabled is False
        assert c.afsk_enabled is False
        assert c.name == ""


class TestStationConfigs:
    def test_all_four_stations_have_configs(self):
        configs = StandardTimeSignalGenerator.STATION_CONFIGS
        assert set(configs) == {'WWV', 'WWVH', 'CHU', 'BPM'}

    def test_wwv_uses_1000hz_tones(self):
        c = StandardTimeSignalGenerator.STATION_CONFIGS['WWV']
        assert c.tick_freq == 1000.0
        assert c.bcd_enabled is True

    def test_wwvh_uses_1200hz_tones(self):
        c = StandardTimeSignalGenerator.STATION_CONFIGS['WWVH']
        assert c.tick_freq == 1200.0
        assert c.bcd_enabled is True

    def test_chu_has_afsk_enabled(self):
        c = StandardTimeSignalGenerator.STATION_CONFIGS['CHU']
        assert c.afsk_enabled is True
        assert c.bcd_enabled is False


# =============================================================================
# generate_tone
# =============================================================================


class TestGenerateTone:
    @pytest.fixture
    def gen(self):
        return StandardTimeSignalGenerator(sample_rate=20000)

    def test_returns_signal_and_phase(self, gen):
        sig, phase = gen.generate_tone(frequency=1000.0, duration_sec=0.1)
        assert isinstance(sig, np.ndarray)
        assert isinstance(phase, float)
        # Phase wraps to [0, 2π)
        assert 0 <= phase < 2 * np.pi

    def test_signal_length_matches_duration(self, gen):
        sig, _ = gen.generate_tone(1000.0, 0.5)
        # 0.5 s × 20 kHz = 10 000 samples
        assert len(sig) == 10000

    def test_signal_is_sinusoidal(self, gen):
        sig, _ = gen.generate_tone(1000.0, 0.1)
        # Single-frequency tone → max amplitude ≈ 1.0
        assert sig.max() <= 1.0
        assert sig.min() >= -1.0
        # FFT peak at the right frequency
        spectrum = np.abs(np.fft.rfft(sig))
        freqs = np.fft.rfftfreq(len(sig), 1.0 / 20000)
        peak_freq = freqs[np.argmax(spectrum)]
        assert peak_freq == pytest.approx(1000.0, abs=20.0)

    def test_phase_continuity(self, gen):
        # End phase of one chunk is the start phase of the next
        sig1, phase1 = gen.generate_tone(1000.0, 0.1, phase=0.0)
        sig2, _ = gen.generate_tone(1000.0, 0.1, phase=phase1)
        # Last sample of sig1 and first of sig2 should be smoothly connected
        # (not necessarily equal, but no large discontinuity)
        # A naive concatenation without phase continuity would have a jump.
        full = np.concatenate([sig1, sig2])
        # The mid-join derivative should be similar to surrounding samples
        edge_diff = abs(full[len(sig1)] - full[len(sig1) - 1])
        # Compare to typical inter-sample diff
        avg_diff = np.mean(np.abs(np.diff(full[:len(sig1)])))
        # Allow up to 5x typical due to small-angle approximations
        assert edge_diff < avg_diff * 5


# =============================================================================
# generate_tick
# =============================================================================


class TestGenerateTick:
    @pytest.fixture
    def gen(self):
        return StandardTimeSignalGenerator(sample_rate=20000)

    def test_unknown_station_raises(self, gen):
        with pytest.raises(ValueError, match="Unknown station"):
            gen.generate_tick('XYZ')

    def test_wwv_standard_tick_5ms(self, gen):
        tick = gen.generate_tick('WWV', 'standard')
        # 5 ms × 20 kHz = 100 samples
        assert len(tick) == 100

    def test_wwv_minute_marker_800ms(self, gen):
        marker = gen.generate_tick('WWV', 'minute')
        assert len(marker) == int(0.8 * 20000)

    def test_chu_standard_tick_300ms(self, gen):
        tick = gen.generate_tick('CHU', 'standard')
        assert len(tick) == int(0.3 * 20000)

    def test_chu_hour_tick_1000ms(self, gen):
        tick = gen.generate_tick('CHU', 'hour')
        # CHU hour tick is 1.0 s
        assert len(tick) == 20000

    def test_bpm_ut1_tick_100ms(self, gen):
        tick = gen.generate_tick('BPM', 'bpm_ut1')
        # 100 ms × 20 kHz = 2 000 samples
        assert len(tick) == 2000

    def test_tick_has_ramp(self, gen):
        # Edge ramp prevents click — first and last samples should be ~0
        tick = gen.generate_tick('WWV', 'minute')
        assert abs(tick[0]) < 0.1
        assert abs(tick[-1]) < 0.1


# =============================================================================
# CHU AFSK
# =============================================================================


class TestGenerateCHUAFSK:
    @pytest.fixture
    def gen(self):
        return StandardTimeSignalGenerator(sample_rate=20000)

    def test_generates_audio_for_one_byte(self, gen):
        sig = gen.generate_chu_afsk([0x55])
        # 11 bits per byte (start + 8 data + 2 stop) × ~3.33 ms each = 36.67 ms
        # → ~733 samples at 20 kHz
        assert 700 <= len(sig) <= 750

    def test_generates_audio_for_multiple_bytes(self, gen):
        n = 10
        sig = gen.generate_chu_afsk([0xFF] * n)
        # Roughly n × 733 samples
        assert 7000 <= len(sig) <= 7500

    def test_signal_within_unit_amplitude(self, gen):
        sig = gen.generate_chu_afsk([0x55, 0xAA])
        assert np.max(np.abs(sig)) <= 1.0


# =============================================================================
# Frame builders
# =============================================================================


class TestFrameBuilders:
    @pytest.fixture
    def gen(self):
        return StandardTimeSignalGenerator(sample_rate=20000)

    def test_swap_nibbles(self, gen):
        # 0x12 -> 0x21
        assert gen._swap_nibbles(0x12) == 0x21
        assert gen._swap_nibbles(0x00) == 0x00
        assert gen._swap_nibbles(0xFF) == 0xFF

    def test_create_frame_a_returns_10_bytes(self, gen):
        frame = gen._create_chu_frame_a(day=42, hour=12, minute=34, second=32)
        assert len(frame) == 10
        # Bytes 5-9 = redundancy = bytes 0-4 (Frame A redundancy is identity)
        assert frame[5:10] == frame[0:5]

    def test_create_frame_b_returns_10_bytes(self, gen):
        frame = gen._create_chu_frame_b(year=2026, dut1=0.2, tai_utc=37)
        assert len(frame) == 10
        # Frame B redundancy = bitwise NOT of bytes 0-4
        for raw, redun in zip(frame[0:5], frame[5:10]):
            assert (raw ^ redun) == 0xFF


# =============================================================================
# generate_second_combined
# =============================================================================


class TestGenerateSecondCombined:
    @pytest.fixture
    def gen(self):
        return StandardTimeSignalGenerator(sample_rate=20000)

    def test_returns_one_second_of_audio(self, gen):
        sig = gen.generate_second_combined(
            station='WWV', second=15, minute=10, hour=12,
            day=100, year=2026,
        )
        assert len(sig) == 20000

    def test_second_29_skips_tick_for_wwv(self, gen):
        # WWV/WWVH skip ticks on second 29 — but BCD subcarrier is still there
        sig = gen.generate_second_combined(
            station='WWV', second=29, minute=10, hour=12,
            day=100, year=2026,
        )
        assert len(sig) == 20000
        # Total signal energy should still be non-zero (BCD)
        assert np.sum(sig ** 2) > 0

    def test_chu_fsk_seconds_have_data(self, gen):
        # Second 32 carries CHU Frame A AFSK data
        sig = gen.generate_second_combined(
            station='CHU', second=32, minute=10, hour=12,
            day=100, year=2026,
        )
        assert len(sig) == 20000
        # Most of the second should have non-zero audio
        assert np.sum(sig ** 2) > 100

    def test_normalization_caps_amplitude(self, gen):
        sig = gen.generate_second_combined(
            station='WWV', second=0, minute=0, hour=0,
            day=1, year=2026,
        )
        # Output is normalized to [-1, 1]
        assert np.max(np.abs(sig)) <= 1.0


# =============================================================================
# generate_minute
# =============================================================================


class TestGenerateMinute:
    @pytest.fixture
    def gen(self):
        # Smaller sample rate → faster tests
        return StandardTimeSignalGenerator(sample_rate=8000)

    def test_returns_60_seconds(self, gen):
        sig = gen.generate_minute('BPM', minute=10)
        assert len(sig) == 60 * 8000

    def test_wwv_minute_8_uses_test_signal(self, gen):
        # Minute 8 is the WWV scientific test signal
        sig = gen.generate_minute('WWV', minute=8)
        # Padded to 60 s
        assert len(sig) == 60 * 8000

    def test_wwvh_minute_44_uses_test_signal(self, gen):
        sig = gen.generate_minute('WWVH', minute=44)
        assert len(sig) == 60 * 8000
