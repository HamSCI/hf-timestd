"""
Unit tests for hf_timestd.core.wwv_bcd_encoder

The encoder generates 60-second IRIG-H templates for cross-correlation
detection of WWV/WWVH BCD time code. Tests cover:
- BCD pattern bit-level encoding (little-endian, position markers)
- Pulse-width waveform shape (200/500/800 ms)
- 100 Hz subcarrier modulation
- Round-trip with the existing WWVBCDDecoder
- Output length, dtype, and amplitude bounds
"""

from datetime import datetime, timezone

import numpy as np
import pytest

from hf_timestd.core.wwv_bcd_decoder import (
    POSITION_MARKERS as DECODER_MARKERS,
    PULSE_WIDTH_MARKER,
    PULSE_WIDTH_ONE,
    PULSE_WIDTH_ZERO,
    WWVBCDDecoder,
)
from hf_timestd.core.wwv_bcd_encoder import WWVBCDEncoder


# =============================================================================
# Class invariants
# =============================================================================


class TestModuleConstants:
    def test_position_markers_match_decoder(self):
        # Encoder/decoder must agree on which seconds carry position markers
        assert WWVBCDEncoder.POSITION_MARKERS == DECODER_MARKERS

    def test_constructor_defaults(self):
        enc = WWVBCDEncoder()
        assert enc.sample_rate == 20000
        assert enc.samples_per_second == 20000

    def test_constructor_custom_rate(self):
        enc = WWVBCDEncoder(sample_rate=16000)
        assert enc.sample_rate == 16000
        assert enc.samples_per_second == 16000


# =============================================================================
# BCD pattern generation (bit level)
# =============================================================================


class TestGenerateBCDPattern:
    @pytest.fixture
    def enc(self):
        return WWVBCDEncoder(sample_rate=24000)

    def _decode_le(self, bits):
        """Reassemble a little-endian BCD digit from bit list."""
        return sum((b & 1) << i for i, b in enumerate(bits))

    def test_minute_ones_at_seconds_10_to_13(self, enc):
        # Minute 37 → ones=7 (0111 LE) at 10-13
        code = enc._generate_bcd_pattern(minute=37, hour=12, day_of_year=100, year=26)
        assert self._decode_le(code[10:14]) == 7

    def test_minute_tens_at_seconds_15_to_17(self, enc):
        # Minute 37 → tens=3 (011 LE) at 15-17
        code = enc._generate_bcd_pattern(minute=37, hour=12, day_of_year=100, year=26)
        assert self._decode_le(code[15:18]) == 3

    def test_hour_ones_and_tens(self, enc):
        # Hour 23 → ones=3 (0011 LE) at 20-23, tens=2 (10 LE) at 25-26
        code = enc._generate_bcd_pattern(minute=0, hour=23, day_of_year=1, year=26)
        assert self._decode_le(code[20:24]) == 3
        assert self._decode_le(code[25:27]) == 2

    def test_day_of_year_three_digits(self, enc):
        # Day 366 → ones=6, tens=6, hundreds=3
        code = enc._generate_bcd_pattern(minute=0, hour=0, day_of_year=366, year=26)
        assert self._decode_le(code[30:34]) == 6
        assert self._decode_le(code[35:39]) == 6
        assert self._decode_le(code[40:43]) == 3

    def test_year_split_across_4_to_7_and_51_to_54(self, enc):
        # Year 26 → ones=6 at 4-7, tens=2 at 51-54
        code = enc._generate_bcd_pattern(minute=0, hour=0, day_of_year=1, year=26)
        assert self._decode_le(code[4:8]) == 6
        assert self._decode_le(code[51:55]) == 2

    def test_unused_bits_default_zero(self, enc):
        code = enc._generate_bcd_pattern(minute=37, hour=12, day_of_year=100, year=26)
        for s in (1, 2, 3, 8, 14, 18, 24, 27, 28, 34, 50, 55):
            assert code[s] == 0, f"unused second {s} should be 0"

    def test_pattern_has_60_elements(self, enc):
        code = enc._generate_bcd_pattern(minute=0, hour=0, day_of_year=1, year=0)
        assert len(code) == 60

    def test_all_zeros_for_zero_time(self, enc):
        # All-zero time fields should produce an all-zero pattern
        code = enc._generate_bcd_pattern(minute=0, hour=0, day_of_year=0, year=0)
        assert all(b == 0 for b in code)


# =============================================================================
# Pulse-width waveform
# =============================================================================


class TestPatternToWaveform:
    @pytest.fixture
    def enc(self):
        return WWVBCDEncoder(sample_rate=10000)  # smaller rate keeps test fast

    def test_length_is_60_seconds(self, enc):
        wf = enc._pattern_to_waveform([0] * 60)
        assert wf.shape == (60 * 10000,)

    def test_dtype_is_float32(self, enc):
        wf = enc._pattern_to_waveform([0] * 60)
        assert wf.dtype == np.float32

    def test_second_zero_is_silent(self, enc):
        # Comment in source: "Skip second 0 (no BCD subcarrier during minute beep)"
        pattern = [1] * 60
        wf = enc._pattern_to_waveform(pattern)
        assert np.all(wf[:enc.samples_per_second] == 0.0)

    def test_marker_pulse_is_800ms(self, enc):
        # Second 9 is a position marker → 800 ms HIGH
        pattern = [0] * 60
        wf = enc._pattern_to_waveform(pattern)
        sps = enc.samples_per_second
        marker_start = 9 * sps
        # First 800 ms = HIGH, last 200 ms = LOW
        high_window = wf[marker_start:marker_start + int(0.8 * sps)]
        low_window = wf[marker_start + int(0.8 * sps):marker_start + sps]
        assert np.all(high_window > 0.4)  # HIGH ≈ 0.501 (-6 dB)
        assert np.all(low_window < 0.1)   # LOW ≈ 0.0501 (-20 dB)

    def test_logic_one_pulse_is_500ms(self, enc):
        # Second 5 (not a marker) with bit=1 → 500 ms HIGH
        pattern = [0] * 60
        pattern[5] = 1
        wf = enc._pattern_to_waveform(pattern)
        sps = enc.samples_per_second
        sec_start = 5 * sps
        high_window = wf[sec_start:sec_start + int(0.5 * sps)]
        low_window = wf[sec_start + int(0.5 * sps):sec_start + sps]
        assert np.all(high_window > 0.4)
        assert np.all(low_window < 0.1)

    def test_logic_zero_pulse_is_200ms(self, enc):
        # Second 5 (not a marker) with bit=0 → 200 ms HIGH
        pattern = [0] * 60
        wf = enc._pattern_to_waveform(pattern)
        sps = enc.samples_per_second
        sec_start = 5 * sps
        high_window = wf[sec_start:sec_start + int(0.2 * sps)]
        low_window = wf[sec_start + int(0.2 * sps):sec_start + sps]
        assert np.all(high_window > 0.4)
        assert np.all(low_window < 0.1)

    def test_amplitude_levels_match_spec(self, enc):
        # HIGH = -6 dB ≈ 0.5012, LOW = HIGH/10 ≈ 0.0501
        wf = enc._pattern_to_waveform([1] * 60)
        # Find the HIGH portion of any non-marker second (e.g. second 5)
        sps = enc.samples_per_second
        high = wf[5 * sps + 100]  # well inside the HIGH window
        assert high == pytest.approx(10 ** (-6 / 20), rel=1e-5)

    def test_marker_seconds_use_marker_width(self, enc):
        # Verify EVERY position marker second produces an 800 ms HIGH window,
        # regardless of the bit assignment in the pattern
        pattern = [0] * 60
        wf = enc._pattern_to_waveform(pattern)
        sps = enc.samples_per_second
        for sec in WWVBCDEncoder.POSITION_MARKERS:
            if sec == 0:
                continue  # second 0 deliberately silent
            start = sec * sps
            assert wf[start + int(0.79 * sps)] > 0.4
            assert wf[start + int(0.81 * sps)] < 0.1


# =============================================================================
# 100 Hz modulation
# =============================================================================


class TestApply100HzModulation:
    @pytest.fixture
    def enc(self):
        return WWVBCDEncoder(sample_rate=24000)

    def test_modulation_zero_at_t0(self, enc):
        env = np.ones(100, dtype=np.float32)
        out = enc._apply_100hz_modulation(env)
        # sin(0) = 0
        assert out[0] == pytest.approx(0.0, abs=1e-6)

    def test_modulation_dominated_by_100hz(self, enc):
        # Constant envelope → output is pure 100 Hz sine; FFT peak at 100 Hz
        env = np.ones(enc.sample_rate, dtype=np.float32)  # 1 second
        out = enc._apply_100hz_modulation(env)
        spectrum = np.abs(np.fft.rfft(out))
        freqs = np.fft.rfftfreq(len(out), d=1.0 / enc.sample_rate)
        peak_freq = freqs[np.argmax(spectrum)]
        assert peak_freq == pytest.approx(100.0, abs=1.0)

    def test_modulation_preserves_envelope_amplitude(self, enc):
        # Output peak ≈ envelope peak (since |sin| ≤ 1)
        env = np.full(2 * enc.sample_rate, 0.5, dtype=np.float32)
        out = enc._apply_100hz_modulation(env)
        assert out.max() == pytest.approx(0.5, abs=1e-3)
        assert out.min() == pytest.approx(-0.5, abs=1e-3)


# =============================================================================
# encode_minute (top-level)
# =============================================================================


class TestEncodeMinute:
    @pytest.fixture
    def enc(self):
        return WWVBCDEncoder(sample_rate=24000)

    def test_envelope_only_length_and_dtype(self, enc):
        ts = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp()
        wf = enc.encode_minute(ts, envelope_only=True)
        assert wf.shape == (60 * enc.sample_rate,)
        assert wf.dtype == np.float32

    def test_modulated_length_and_bounds(self, enc):
        ts = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc).timestamp()
        wf = enc.encode_minute(ts, envelope_only=False)
        assert wf.shape == (60 * enc.sample_rate,)
        # AM-modulated envelope: |out| ≤ |envelope max| ≈ 0.5012
        assert wf.max() <= 0.51
        assert wf.min() >= -0.51

    def test_different_minutes_yield_different_templates(self, enc):
        t1 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp()
        t2 = datetime(2026, 1, 1, 0, 1, 0, tzinfo=timezone.utc).timestamp()
        wf1 = enc.encode_minute(t1, envelope_only=True)
        wf2 = enc.encode_minute(t2, envelope_only=True)
        # Templates encode different minute values; some sample must differ
        assert not np.array_equal(wf1, wf2)


# =============================================================================
# 8/12-bit BCD helpers (currently unused by encode_minute, but exposed)
# =============================================================================


class TestBCDHelpers:
    @pytest.fixture
    def enc(self):
        return WWVBCDEncoder()

    def test_to_bcd_8bit_range(self, enc):
        # 99 → tens=9 (1001), ones=9 (1001) → [1,0,0,1, 1,0,0,1]
        assert enc._to_bcd_8bit(99) == [1, 0, 0, 1, 1, 0, 0, 1]

    def test_to_bcd_8bit_zero(self, enc):
        assert enc._to_bcd_8bit(0) == [0] * 8

    def test_to_bcd_8bit_intermediate(self, enc):
        # 37 → tens=3 (0011), ones=7 (0111)
        assert enc._to_bcd_8bit(37) == [0, 0, 1, 1, 0, 1, 1, 1]

    def test_to_bcd_12bit_full_range(self, enc):
        # 999 → hundreds=9, tens=9, ones=9 → 12 ones
        assert enc._to_bcd_12bit(999) == [1, 0, 0, 1, 1, 0, 0, 1, 1, 0, 0, 1]

    def test_to_bcd_12bit_zero(self, enc):
        assert enc._to_bcd_12bit(0) == [0] * 12

    def test_to_bcd_12bit_three_digits(self, enc):
        # 366 → hundreds=3 (0011), tens=6 (0110), ones=6 (0110)
        assert enc._to_bcd_12bit(366) == [0, 0, 1, 1, 0, 1, 1, 0, 0, 1, 1, 0]


# =============================================================================
# Encoder ↔ Decoder round trip
# =============================================================================


class TestRoundTripWithDecoder:
    """Encoder output decoded by WWVBCDDecoder must recover the input time."""

    @pytest.fixture
    def sample_rate(self):
        return 24000

    @pytest.fixture
    def encoder(self, sample_rate):
        return WWVBCDEncoder(sample_rate=sample_rate)

    @pytest.fixture
    def decoder(self, sample_rate):
        return WWVBCDDecoder(sample_rate=sample_rate)

    @pytest.mark.parametrize("dt", [
        datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 4, 26, 12, 34, 0, tzinfo=timezone.utc),
        datetime(2026, 6, 15, 23, 45, 0, tzinfo=timezone.utc),  # day 166
    ])
    def test_modulated_signal_decodes(self, encoder, decoder, dt):
        # Real WWV-shaped signal: AM-modulated 100 Hz subcarrier on a DC carrier
        ts = dt.timestamp()
        envelope = encoder.encode_minute(ts, envelope_only=True)
        # Use envelope as the AM modulation index on the 100 Hz subcarrier
        # — same model as our test_wwv_bcd_decoder synth_iq helper.
        t = np.arange(len(envelope)) / encoder.sample_rate
        iq = (1.0 + envelope * np.sin(2 * np.pi * 100 * t)).astype(np.complex64)

        result = decoder.decode_minute(iq)
        assert result.detected, f"decode failed for {dt.isoformat()}"
        assert result.decoded_minute == dt.minute
        assert result.decoded_hour == dt.hour
        assert result.decoded_day == dt.timetuple().tm_yday
        assert result.decoded_year == dt.year % 100
