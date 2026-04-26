"""
Unit tests for hf_timestd.core.wwv_bcd_decoder

Covers the IRIG-H time-code decoder for WWV/WWVH:
- WWVBCDResult dataclass and string formatting
- Pulse-width classification (0/1/marker boundaries)
- BCD digit decoding (little-endian, marker rejection, unknown rejection)
- Pulse width measurement on synthetic envelopes
- End-to-end decode of a hand-built AM-modulated 100 Hz signal carrying
  a known time
- decode_minute behavior on insufficient samples / weak signal
"""

from datetime import datetime

import numpy as np
import pytest

from hf_timestd.core.wwv_bcd_decoder import (
    POSITION_MARKERS,
    PULSE_WIDTH_MARKER,
    PULSE_WIDTH_ONE,
    PULSE_WIDTH_ZERO,
    THRESHOLD_ONE_MARKER,
    THRESHOLD_ZERO_ONE,
    WWVBCDDecoder,
    WWVBCDResult,
)


# =============================================================================
# Synthetic IRIG-H signal generator (mirrors what the decoder expects)
# =============================================================================


def _bcd_le(value: int, nbits: int) -> list[int]:
    """Little-endian BCD: LSB first."""
    return [(value >> i) & 1 for i in range(nbits)]


def _build_pulse_widths(dt: datetime) -> list[int]:
    """Build the 60-second IRIG-H pulse-width plan for a given UTC time.

    Position markers (800 ms) at seconds 0,9,19,29,39,49,59. Data fields are
    encoded as 200 ms (logic 0) or 500 ms (logic 1) per IRIG-H. Unused
    seconds default to 200 ms (logic 0).
    """
    widths = [PULSE_WIDTH_ZERO] * 60
    for marker in POSITION_MARKERS:
        widths[marker] = PULSE_WIDTH_MARKER

    def emit(value: int, start: int, nbits: int):
        for i, bit in enumerate(_bcd_le(value, nbits)):
            widths[start + i] = PULSE_WIDTH_ONE if bit else PULSE_WIDTH_ZERO

    minute = dt.minute
    hour = dt.hour
    day = dt.timetuple().tm_yday
    year_2digit = dt.year % 100

    # Year ones at 4-7, tens at 51-54
    emit(year_2digit % 10, 4, 4)
    emit(year_2digit // 10, 51, 4)

    # Minute ones at 10-13, tens at 15-17 (3 bits)
    emit(minute % 10, 10, 4)
    emit(minute // 10, 15, 3)

    # Hour ones at 20-23, tens at 25-26 (2 bits)
    emit(hour % 10, 20, 4)
    emit(hour // 10, 25, 2)

    # Day ones at 30-33, tens at 35-38, hundreds at 40-42 (3 bits)
    emit(day % 10, 30, 4)
    emit((day // 10) % 10, 35, 4)
    emit(day // 100, 40, 3)

    return widths


def synth_iq(sample_rate: int, dt: datetime) -> np.ndarray:
    """Generate a synthetic IRIG-H IQ signal for `dt`.

    Mirrors the WWV/WWVH AM model: a (DC) carrier plus a 100 Hz subcarrier
    whose amplitude is gated on/off by the IRIG-H pulse mask. After |IQ|
    demodulation and a 50–150 Hz bandpass, the decoder sees the 100 Hz
    envelope rise and fall on the IRIG-H pulse boundaries.
    """
    pulse_widths = _build_pulse_widths(dt)
    n = 60 * sample_rate
    mask = np.zeros(n, dtype=np.float64)
    for sec, width_ms in enumerate(pulse_widths):
        start = sec * sample_rate
        length = int(width_ms / 1000 * sample_rate)
        mask[start:start + length] = 1.0
    t = np.arange(n) / sample_rate
    iq = (1.0 + mask * np.sin(2 * np.pi * 100 * t))
    return iq.astype(np.complex64)


# =============================================================================
# WWVBCDResult dataclass
# =============================================================================


class TestWWVBCDResult:
    def test_default_state_is_undetected(self):
        r = WWVBCDResult()
        assert r.detected is False
        assert r.markers_found == 0
        assert r.markers_expected == 7
        assert r.decoded_minute is None
        assert r.dut1_seconds is None

    def test_str_when_not_detected(self):
        r = WWVBCDResult()
        assert "Not detected" in str(r)

    def test_str_when_detected(self):
        r = WWVBCDResult(
            detected=True,
            decoded_day=42,
            decoded_hour=12,
            decoded_minute=34,
            decoded_year=26,
            dut1_tenths=3,
            dut1_negative=False,
            decode_confidence=0.87,
        )
        s = str(r)
        # Spot-check that the canonical fields make it into the string
        assert "Day 042" in s
        assert "12:34" in s
        assert "year 26" in s
        assert "+0.3" in s
        assert "0.87" in s

    def test_dut1_seconds_positive(self):
        r = WWVBCDResult(dut1_tenths=4, dut1_negative=False)
        assert r.dut1_seconds == pytest.approx(0.4)

    def test_dut1_seconds_negative(self):
        r = WWVBCDResult(dut1_tenths=7, dut1_negative=True)
        assert r.dut1_seconds == pytest.approx(-0.7)

    def test_dut1_seconds_none_when_unset(self):
        assert WWVBCDResult().dut1_seconds is None


# =============================================================================
# Pulse classification thresholds
# =============================================================================


class TestPulseClassification:
    @pytest.fixture
    def decoder(self):
        return WWVBCDDecoder(sample_rate=24000)

    def test_too_short_is_unknown(self, decoder):
        assert decoder._classify_pulse(0) == '?'
        assert decoder._classify_pulse(50) == '?'

    def test_zero_at_canonical_width(self, decoder):
        assert decoder._classify_pulse(PULSE_WIDTH_ZERO) == '0'

    def test_one_at_canonical_width(self, decoder):
        assert decoder._classify_pulse(PULSE_WIDTH_ONE) == '1'

    def test_marker_at_canonical_width(self, decoder):
        assert decoder._classify_pulse(PULSE_WIDTH_MARKER) == 'P'

    def test_threshold_zero_one_boundary(self, decoder):
        # Just below the 0/1 boundary → still 0
        assert decoder._classify_pulse(THRESHOLD_ZERO_ONE - 1) == '0'
        # Just above → 1
        assert decoder._classify_pulse(THRESHOLD_ZERO_ONE + 1) == '1'

    def test_threshold_one_marker_boundary(self, decoder):
        assert decoder._classify_pulse(THRESHOLD_ONE_MARKER - 1) == '1'
        assert decoder._classify_pulse(THRESHOLD_ONE_MARKER + 1) == 'P'

    def test_too_long_is_unknown(self, decoder):
        assert decoder._classify_pulse(950) == '?'


# =============================================================================
# BCD digit decoding (little-endian)
# =============================================================================


class TestBCDDigitDecoding:
    @pytest.fixture
    def decoder(self):
        return WWVBCDDecoder(sample_rate=24000)

    @pytest.mark.parametrize("bits,expected", [
        (['0', '0', '0', '0'], 0),
        (['1', '0', '0', '0'], 1),   # LSB first
        (['0', '1', '0', '0'], 2),
        (['1', '1', '0', '0'], 3),
        (['0', '0', '0', '1'], 8),
        (['1', '0', '0', '1'], 9),
    ])
    def test_4bit_little_endian(self, decoder, bits, expected):
        assert decoder._decode_bcd_digit(bits, 4) == expected

    def test_3bit_digit(self, decoder):
        # Hundreds-of-day field is 3 bits
        assert decoder._decode_bcd_digit(['1', '1', '1'], 3) == 7
        assert decoder._decode_bcd_digit(['0', '0', '1'], 3) == 4

    def test_marker_in_data_field_is_invalid(self, decoder):
        # A position marker showing up where data is expected → cannot decode
        assert decoder._decode_bcd_digit(['1', 'P', '0', '0'], 4) is None

    def test_unknown_pulse_is_invalid(self, decoder):
        assert decoder._decode_bcd_digit(['1', '?', '0', '0'], 4) is None

    def test_too_few_bits_returns_none(self, decoder):
        assert decoder._decode_bcd_digit(['1', '0'], 4) is None


# =============================================================================
# Pulse width measurement on synthetic envelopes
# =============================================================================


class TestPulseWidthMeasurement:
    @pytest.fixture
    def decoder(self):
        return WWVBCDDecoder(sample_rate=24000)

    def _make_envelope(self, decoder, pulse_widths_ms):
        """Build a 60-second envelope with each second containing a square
        pulse of the given width.

        The threshold inside `_measure_pulse_widths` is `percentile(env, 95) * 0.4`,
        so the envelope must contain enough high-amplitude pulse samples for
        that percentile to fall on the pulse plateau (1.0) rather than the
        floor. The test arrays pulse-fill all 60 seconds, so p95 ≈ 1.0 and
        the threshold separates the high portions cleanly from the low.
        """
        sr = decoder.sample_rate
        envelope = np.zeros(60 * sr, dtype=np.float64)
        for sec, width_ms in enumerate(pulse_widths_ms):
            if width_ms <= 0:
                continue
            start = sec * sr
            length = int(width_ms * sr / 1000)
            envelope[start:start + length] = 1.0
        return envelope

    def test_measures_canonical_widths(self, decoder):
        # Repeat a 200/500/800 cycle for every second so p95 sits on the plateau.
        widths = [200, 500, 800] * 20
        env = self._make_envelope(decoder, widths)
        measured = decoder._measure_pulse_widths(env)

        # Integration-based measurement; allow a few ms of slack
        for i, expected in enumerate(widths):
            assert measured[i] == pytest.approx(expected, abs=20), \
                f"second {i}: expected ~{expected} ms, got {measured[i]:.1f}"

    def test_returns_60_widths(self, decoder):
        env = self._make_envelope(decoder, [200] * 60)
        measured = decoder._measure_pulse_widths(env)
        assert len(measured) == 60

    def test_pulses_round_trip_to_correct_classification(self, decoder):
        # Every second carries a real pulse so p95 lands on the plateau.
        cycle = [PULSE_WIDTH_MARKER, PULSE_WIDTH_ZERO, PULSE_WIDTH_ONE]
        widths = (cycle * 20)
        env = self._make_envelope(decoder, widths)
        measured = decoder._measure_pulse_widths(env)
        cls = [decoder._classify_pulse(w) for w in measured[:6]]
        assert cls == ['P', '0', '1', 'P', '0', '1']


# =============================================================================
# End-to-end encode → decode round trip
# =============================================================================


class TestSyntheticDecode:
    """Decode a hand-built AM-modulated 100 Hz signal carrying a known time."""

    @pytest.fixture
    def sample_rate(self):
        # 24 kHz matches the production decoder default
        return 24000

    @pytest.fixture
    def decoder(self, sample_rate):
        return WWVBCDDecoder(sample_rate=sample_rate)

    @pytest.mark.parametrize("dt", [
        datetime(2026, 1, 26, 16, 30, 0),
        datetime(2026, 6, 15, 23, 45, 0),  # day 166
        datetime(2026, 12, 31, 0, 0, 0),   # day 365 — boundary
    ])
    def test_decode_recovers_minute_hour_day_year(self, sample_rate, decoder, dt):
        iq = synth_iq(sample_rate, dt)
        result = decoder.decode_minute(iq)

        assert result.detected, f"decode failed for {dt.isoformat()}"
        assert result.decoded_minute == dt.minute
        assert result.decoded_hour == dt.hour
        assert result.decoded_day == dt.timetuple().tm_yday
        assert result.decoded_year == dt.year % 100

    def test_all_markers_found_on_clean_signal(self, sample_rate, decoder):
        iq = synth_iq(sample_rate, datetime(2026, 4, 26, 12, 0, 0))
        result = decoder.decode_minute(iq)
        # Position markers at 0,9,19,29,39,49,59 — should all decode cleanly
        assert result.markers_found == len(POSITION_MARKERS)

    def test_confidence_high_on_clean_signal(self, sample_rate, decoder):
        iq = synth_iq(sample_rate, datetime(2026, 4, 26, 12, 0, 0))
        result = decoder.decode_minute(iq)
        # Clean synthetic → confidence should be near 1.0
        assert result.decode_confidence > 0.85

    def test_pulse_widths_populated(self, sample_rate, decoder):
        iq = synth_iq(sample_rate, datetime(2026, 4, 26, 12, 0, 0))
        result = decoder.decode_minute(iq)
        assert len(result.pulse_widths_ms) == 60


# =============================================================================
# Edge cases
# =============================================================================


class TestDecoderEdgeCases:
    @pytest.fixture
    def decoder(self):
        return WWVBCDDecoder(sample_rate=24000)

    def test_insufficient_samples_returns_undetected(self, decoder):
        # Less than 90% of a minute → bail
        short_iq = np.zeros(int(0.5 * 60 * 24000), dtype=np.complex64)
        result = decoder.decode_minute(short_iq)
        assert result.detected is False

    def test_pure_noise_does_not_falsely_decode(self, decoder):
        np.random.seed(42)
        n = 60 * 24000
        noise = (np.random.randn(n) + 1j * np.random.randn(n)).astype(np.complex64)
        result = decoder.decode_minute(noise)
        # Noise must not produce a detected decode
        assert result.detected is False

    def test_decode_partial_dispatches_to_decode_minute(self, decoder):
        # Smoke test: decode_partial accepts the same input shape as decode_minute
        n = 60 * 24000
        zeros = np.zeros(n, dtype=np.complex64)
        result = decoder.decode_partial(zeros)
        assert isinstance(result, WWVBCDResult)


# =============================================================================
# Module-level constants
# =============================================================================


class TestModuleConstants:
    def test_position_markers_exact(self):
        assert POSITION_MARKERS == [0, 9, 19, 29, 39, 49, 59]

    def test_pulse_width_ordering(self):
        # Canonical IRIG-H pulse widths
        assert PULSE_WIDTH_ZERO < PULSE_WIDTH_ONE < PULSE_WIDTH_MARKER

    def test_thresholds_lie_between_canonical_widths(self):
        assert PULSE_WIDTH_ZERO < THRESHOLD_ZERO_ONE < PULSE_WIDTH_ONE
        assert PULSE_WIDTH_ONE < THRESHOLD_ONE_MARKER < PULSE_WIDTH_MARKER
