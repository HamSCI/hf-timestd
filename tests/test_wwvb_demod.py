"""Unit tests for hf_timestd.core.wwvb_demod.

All tests use synthesized IQ from `synthesize_wwvb_iq` — deterministic
and don't require captured data.  Live-signal validation is operational
tooling (`scripts/wwvb_live_tap.py`), not a pytest fixture, because
WWVB never gets archived (it's metrology-only, same as the T6 BPSK
PPS injection — see project_hf_timestd_chu_offair_wwvb.md).
"""

from __future__ import annotations

import datetime as _dt

import numpy as np
import pytest

from hf_timestd.core.wwvb_demod import (
    DemodResult,
    amplitude_envelope,
    decode_iq,
    estimate_carrier_offset,
    extract_pm_bits,
    find_second_boundaries,
    find_sync_positions,
    phases_to_bits,
    synthesize_wwvb_iq,
)
from hf_timestd.core.wwvb_protocol import (
    DstState,
    LeapSecond,
    SYNC_T_BITS,
    encode_time_frame,
)


UTC = _dt.timezone.utc
SAMPLE_RATE = 24000.0


# =============================================================================
# Synthesized-signal unit tests
# =============================================================================

class TestCarrierOffsetEstimate:

    def test_zero_offset_yields_zero(self):
        # A clean carrier at DC should give offset ≈ 0 within
        # the FFT bin resolution (= 1/duration ≈ 0.2 Hz for 5 s).
        n = int(SAMPLE_RATE * 5)
        iq = np.ones(n, dtype=np.complex64)
        offset = estimate_carrier_offset(iq, SAMPLE_RATE)
        assert abs(offset) < 0.3

    def test_positive_offset_recovered(self):
        # Carrier 2 Hz above DC.
        n = int(SAMPLE_RATE * 5)
        t = np.arange(n) / SAMPLE_RATE
        iq = np.exp(2j * np.pi * 2.0 * t).astype(np.complex64)
        offset = estimate_carrier_offset(iq, SAMPLE_RATE)
        assert abs(offset - 2.0) < 0.3


class TestAmplitudeEnvelope:

    def test_envelope_tracks_am_drops(self):
        # Build a 2-second AM-only IQ: amplitude drops to 0.4 for the
        # first 200 ms of each second.  Envelope should show that.
        n = int(SAMPLE_RATE * 2)
        iq = np.ones(n, dtype=np.complex64)
        for k in range(2):
            s = k * int(SAMPLE_RATE)
            iq[s:s + int(0.2 * SAMPLE_RATE)] = 0.4
        env = amplitude_envelope(iq, SAMPLE_RATE, smooth_ms=2.0)
        # Mid-high region around 600 ms in: amplitude should be ~1.0
        mid = env[int(0.6 * SAMPLE_RATE)]
        # Mid-low region at 100 ms in: should be ~0.4
        low = env[int(0.1 * SAMPLE_RATE)]
        assert mid > 0.9
        assert low < 0.5


class TestSecondBoundaryDetection:

    def test_finds_boundaries_at_one_second_intervals(self):
        # 5 seconds of synthesized IQ with the standard AM drop pattern.
        bits = [0] * 60
        iq = synthesize_wwvb_iq(bits, sample_rate=SAMPLE_RATE)
        # Take just the first 5 seconds to keep the test fast.
        iq5 = iq[: int(5 * SAMPLE_RATE)]
        env = amplitude_envelope(iq5, SAMPLE_RATE)
        boundaries = find_second_boundaries(env, SAMPLE_RATE)
        # Expect ~5 boundaries near samples 0, 24000, 48000, 72000, 96000.
        assert 4 <= boundaries.size <= 6
        spacings = np.diff(boundaries)
        # Spacings within ±10 ms of 1 s (= 240 samples at 24 kHz).
        assert np.all(np.abs(spacings - int(SAMPLE_RATE)) < 240)


class TestPhaseClustering:

    def test_alternating_bits_recovered(self):
        # Synthesized signal with alternating PM bits.
        bits_in = ([0, 1] * 30)
        iq = synthesize_wwvb_iq(bits_in, sample_rate=SAMPLE_RATE)
        env = amplitude_envelope(iq, SAMPLE_RATE)
        boundaries = find_second_boundaries(env, SAMPLE_RATE)
        mean_iq = extract_pm_bits(iq, boundaries, SAMPLE_RATE)
        bits_out, _ = phases_to_bits(mean_iq)
        # Polarity may be flipped — accept either.
        match_upright = int(np.sum(bits_out == bits_in[: bits_out.size]))
        match_flipped = int(np.sum(bits_out != bits_in[: bits_out.size]))
        assert max(match_upright, match_flipped) >= bits_out.size - 2


class TestSyncFinding:

    def test_finds_sync_at_start_of_bit_stream(self):
        # Build a bit stream that starts with sync_T followed by junk.
        bits = np.array(list(SYNC_T_BITS) + [0] * 50, dtype=np.uint8)
        hits = find_sync_positions(bits, SYNC_T_BITS, max_errors=0)
        assert (0, 0, False) in hits

    def test_finds_inverted_sync(self):
        inv = [1 - b for b in SYNC_T_BITS]
        bits = np.array(inv + [0] * 50, dtype=np.uint8)
        hits = find_sync_positions(bits, SYNC_T_BITS, max_errors=0)
        # Should match inverted at position 0.
        assert any(h == (0, 0, True) for h in hits)


# =============================================================================
# End-to-end synthesized decode
# =============================================================================

class TestSynthesizedEndToEnd:
    """Whole pipeline against a synthesized minute frame."""

    def _encode_and_demod(self, when, dst_state, leap_second, snr_db=None):
        frame_bits = encode_time_frame(when, dst_state, leap_second)
        iq = synthesize_wwvb_iq(
            frame_bits, sample_rate=SAMPLE_RATE, snr_db=snr_db,
        )
        return decode_iq(iq, sample_rate=SAMPLE_RATE)

    def test_clean_signal_decodes_to_correct_utc(self):
        when = _dt.datetime(2026, 5, 27, 11, 50, tzinfo=UTC)
        result = self._encode_and_demod(when, DstState.IN_EFFECT, LeapSecond.NONE)
        assert isinstance(result, DemodResult)
        assert len(result.frames) >= 1
        frame = result.frames[0].frame
        assert frame.minute_of_frame == when
        assert frame.dst_state == DstState.IN_EFFECT
        assert frame.parity_errors == 0
        assert frame.sync_errors == 0

    def test_carrier_offset_does_not_break_decode(self):
        # Add a 0.1 Hz residual offset before decode; the FFT estimate
        # should remove it.
        when = _dt.datetime(2026, 5, 27, 11, 50, tzinfo=UTC)
        frame_bits = encode_time_frame(when)
        iq = synthesize_wwvb_iq(frame_bits, sample_rate=SAMPLE_RATE)
        t = np.arange(iq.size) / SAMPLE_RATE
        offset_hz = 0.1
        iq_shifted = (iq * np.exp(2j * np.pi * offset_hz * t)
                      ).astype(np.complex64)
        result = decode_iq(iq_shifted, sample_rate=SAMPLE_RATE)
        assert len(result.frames) >= 1
        assert result.frames[0].frame.minute_of_frame == when

    def test_snr_20db_decode(self):
        # 20 dB SNR is very benign — should decode without errors.
        when = _dt.datetime(2026, 5, 27, 11, 50, tzinfo=UTC)
        result = self._encode_and_demod(
            when, DstState.NOT_IN_EFFECT, LeapSecond.NONE, snr_db=20.0,
        )
        assert len(result.frames) >= 1
        assert result.frames[0].frame.minute_of_frame == when


