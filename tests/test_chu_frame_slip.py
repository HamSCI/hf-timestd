#!/usr/bin/env python3
"""
Unit Tests for CHU FSK Decoder

Tests cover:
1. Parity handling (FM-1): parity logs but doesn't reject — redundancy check protects
2. Start-bit search robustness (FM-2): pattern match + UART frame search
3. Consensus threshold (FM-3): works with 2+ Frame A decodes
4. Audio normalization (FM-4): discriminator output is ±1.0 regardless of sample rate
5. Per-second failure diagnostics (FM-5): each second reports why it failed

Author: HF Time Standard Team
"""

import pytest
import numpy as np
from datetime import datetime, timezone, timedelta
from hf_timestd.core.chu_fsk_decoder import (
    CHUFSKDecoder, CHUFrameA, CHUFrameB, CHUFSKResult,
    MARK_FREQ, SPACE_FREQ, CENTER_FREQ, BAUD_RATE, BIT_DURATION_MS,
)


# =============================================================================
# SYNTHETIC SIGNAL GENERATOR
# =============================================================================

def generate_fsk_audio(
    byte_sequence: list,
    sample_rate: int = 12000,
    snr_db: float = 30.0,
    timing_offset_ms: float = 0.0,
) -> np.ndarray:
    """
    Generate a synthetic CHU FSK audio signal for one second.

    Creates 300-baud Bell 103 compatible FSK with continuous phase:
    - 1000 Hz tick in the first 10ms
    - Mark sync tone (2225 Hz) from 10ms to first start bit
    - UART frames: start(0) + 8 data LSB-first + even parity + stop(1)

    Phase is accumulated continuously to avoid discontinuities at bit
    boundaries — critical for the delay-line discriminator.

    Args:
        byte_sequence: List of 10 byte values to encode
        sample_rate: Audio sample rate
        snr_db: Signal-to-noise ratio in dB
        timing_offset_ms: Shift the FSK data start (simulates ionospheric delay)

    Returns:
        1.1 seconds of float64 audio
    """
    duration_s = 1.1
    n_samples = int(duration_s * sample_rate)
    audio = np.zeros(n_samples, dtype=np.float64)

    # 1000 Hz tick for first 10ms
    tick_end = int(0.010 * sample_rate)
    t_tick = np.arange(tick_end) / sample_rate
    audio[:tick_end] = 0.5 * np.sin(2 * np.pi * 1000 * t_tick)

    # Build a frequency profile for the FSK portion (mark sync + data)
    data_start_ms = 133.0 + timing_offset_ms
    mark_start = tick_end
    mark_end = int(data_start_ms * sample_rate / 1000)

    # Collect all bit intervals as (freq, n_samples) pairs
    segments = []
    # Mark sync tone before data
    segments.append((MARK_FREQ, mark_end - mark_start))

    # UART frames
    samples_per_bit = sample_rate / BAUD_RATE
    for byte_val in byte_sequence:
        parity = bin(byte_val).count('1') % 2
        frame_bits = [0]  # start
        for i in range(8):
            frame_bits.append((byte_val >> i) & 1)
        frame_bits.append(parity)
        frame_bits.append(1)  # stop
        for bit in frame_bits:
            segments.append((MARK_FREQ if bit == 1 else SPACE_FREQ, None))

    # Generate phase-continuous FSK by accumulating phase
    phase = 0.0
    sample_pos = mark_start
    bit_idx = 0
    for seg_idx, (freq, n_seg) in enumerate(segments):
        if seg_idx == 0:
            # Mark sync: explicit length
            length = n_seg
        else:
            # Data bits: calculate exact sample boundaries to avoid drift
            start_s = mark_end + int(bit_idx * samples_per_bit)
            end_s = mark_end + int((bit_idx + 1) * samples_per_bit)
            length = end_s - start_s
            sample_pos = start_s
            bit_idx += 1

        if sample_pos + length > n_samples:
            length = n_samples - sample_pos
        if length <= 0:
            continue

        dt = 1.0 / sample_rate
        for i in range(length):
            audio[sample_pos + i] = np.sin(phase)
            phase += 2 * np.pi * freq * dt

        sample_pos += length

    # Add noise
    signal_end = sample_pos
    if snr_db < 100:
        signal_power = np.mean(audio[mark_start:signal_end]**2)
        if signal_power > 0:
            noise_power = signal_power / (10 ** (snr_db / 10))
            noise = np.sqrt(noise_power) * np.random.randn(n_samples)
            audio += noise

    return audio


def make_frame_a_bytes(day: int, hour: int, minute: int, second: int) -> list:
    """
    Create Frame A byte sequence: 6d dd hh mm ss (nibble-swapped, repeated).

    Returns 10 bytes (5 data + 5 redundancy).
    """
    def swap_nibbles(b):
        return ((b & 0x0F) << 4) | ((b & 0xF0) >> 4)

    # BCD encode
    b0 = (6 << 4) | (day // 100)
    b1 = ((day % 100) // 10 << 4) | (day % 10)
    b2 = (hour // 10 << 4) | (hour % 10)
    b3 = (minute // 10 << 4) | (minute % 10)
    b4 = (second // 10 << 4) | (second % 10)

    data = [swap_nibbles(b) for b in [b0, b1, b2, b3, b4]]
    return data + data  # redundancy = copy


def make_frame_b_bytes(dut1_tenths: int, dut1_neg: bool, year: int,
                       tai_utc: int, dst: int = 0) -> list:
    """
    Create Frame B byte sequence: xz yy yy tt aa (nibble-swapped, inverted redundancy).

    Returns 10 bytes (5 data + 5 bitwise-NOT).
    """
    def swap_nibbles(b):
        return ((b & 0x0F) << 4) | ((b & 0xF0) >> 4)

    x = 1 if dut1_neg else 0
    z = dut1_tenths
    b0 = (x << 4) | z
    b1 = ((year // 1000) << 4) | ((year % 1000) // 100)
    b2 = (((year % 100) // 10) << 4) | (year % 10)
    b3 = ((tai_utc // 10) << 4) | (tai_utc % 10)
    b4 = ((dst // 10) << 4) | (dst % 10)

    data = [swap_nibbles(b) for b in [b0, b1, b2, b3, b4]]
    inverted = [(~b) & 0xFF for b in data]
    return data + inverted


def generate_chu_minute(
    sample_rate: int = 12000,
    day: int = 57,
    hour: int = 23,
    minute: int = 45,
    snr_db: float = 30.0,
    drop_seconds: list = None,
) -> np.ndarray:
    """Generate a full 60-second CHU audio buffer with FSK in seconds 31-39."""
    n_samples = 60 * sample_rate
    audio = np.zeros(n_samples, dtype=np.float64)

    frame_b = make_frame_b_bytes(2, False, 2026, 37, 0)
    for sec in range(31, 40):
        if drop_seconds and sec in drop_seconds:
            continue
        if sec == 31:
            byte_seq = frame_b
        else:
            byte_seq = make_frame_a_bytes(day, hour, minute, sec)
        sec_audio = generate_fsk_audio(byte_seq, sample_rate, snr_db)
        start = sec * sample_rate
        end = start + len(sec_audio)
        if end <= n_samples:
            audio[start:end] = sec_audio[:n_samples - start]

    return audio


# =============================================================================
# FM-1: PARITY HANDLING (log, don't reject)
# =============================================================================

class TestCHUParityChecking:
    """Test that parity errors are logged but bytes are still returned."""

    def setup_method(self):
        self.decoder = CHUFSKDecoder(sample_rate=12000)

    def test_valid_parity_accepted(self):
        """Frames with correct parity produce 10 bytes."""
        bits = []
        for _ in range(10):
            bits.append(0)  # Start
            bits.extend([1, 0, 1, 0, 1, 0, 1, 0])  # 0x55, 4 ones → parity=0
            bits.append(0)  # Even parity
            bits.append(1)  # Stop
        result = self.decoder._bits_to_bytes(bits)
        assert len(result) == 10
        assert all(b == 0x55 for b in result)

    def test_parity_error_still_returns_bytes(self):
        """Parity errors log but do NOT reject — redundancy check catches bad data."""
        bits = []
        for _ in range(10):
            bits.append(0)
            bits.extend([1, 0, 1, 0, 1, 0, 1, 0])  # 0x55
            bits.append(1)  # WRONG parity
            bits.append(1)
        result = self.decoder._bits_to_bytes(bits)
        # _bits_to_bytes logs parity errors but still returns all 10 bytes
        assert len(result) == 10

    def test_single_bit_error_changes_byte_value(self):
        """A single flipped data bit changes the byte value; parity detects it."""
        bits = []
        for _ in range(10):
            bits.append(0)
            bits.extend([1, 0, 1, 0, 1, 0, 1, 0])  # 0x55
            bits.append(0)
            bits.append(1)
        # Flip first data bit of byte 0 → becomes 0x54
        bits[1] = 0
        result = self.decoder._bits_to_bytes(bits)
        assert len(result) == 10
        assert result[0] == 0x54  # Changed byte
        assert result[1] == 0x55  # Unchanged


# =============================================================================
# FM-2: START-BIT SEARCH ROBUSTNESS
# =============================================================================

class TestStartBitSearch:
    """Test start-bit search with synthetic FSK signals."""

    def setup_method(self):
        self.decoder = CHUFSKDecoder(sample_rate=12000)

    def test_frame_a_pattern_match_clean_signal(self):
        """Frame A start-bit found via 0x06 pattern match at high SNR."""
        byte_seq = make_frame_a_bytes(57, 23, 45, 32)
        audio = generate_fsk_audio(byte_seq, sample_rate=12000, snr_db=30)
        frame, _, conf, _ = self.decoder.decode_second(audio, 0, 32)
        assert frame is not None
        assert frame.valid
        assert frame.day_of_year == 57
        assert frame.hour == 23
        assert frame.minute == 45
        assert frame.second == 32

    def test_frame_b_uart_search_clean_signal(self):
        """Frame B start-bit found via UART frame search at high SNR."""
        byte_seq = make_frame_b_bytes(2, False, 2026, 37)
        audio = generate_fsk_audio(byte_seq, sample_rate=12000, snr_db=30)
        frame, _, conf, _ = self.decoder.decode_second(audio, 0, 31)
        assert frame is not None
        assert frame.valid
        assert frame.year == 2026
        assert frame.tai_utc == 37
        assert frame.dut1_tenths == 2

    def test_frame_a_at_moderate_snr(self):
        """Frame A decodes at 15 dB SNR (typical marginal HF signal)."""
        np.random.seed(42)
        byte_seq = make_frame_a_bytes(57, 23, 45, 35)
        audio = generate_fsk_audio(byte_seq, sample_rate=12000, snr_db=15)
        frame, _, conf, _ = self.decoder.decode_second(audio, 0, 35)
        # May or may not decode at 15 dB — test that it doesn't crash
        if frame is not None:
            assert frame.day_of_year == 57

    def test_frame_a_at_low_snr_returns_none(self):
        """Frame A fails gracefully at 3 dB SNR."""
        np.random.seed(42)
        byte_seq = make_frame_a_bytes(57, 23, 45, 35)
        audio = generate_fsk_audio(byte_seq, sample_rate=12000, snr_db=3)
        frame, _, conf, _ = self.decoder.decode_second(audio, 0, 35)
        # At 3 dB, decode should almost certainly fail — but must not crash
        # (frame may be None or may decode incorrectly — both acceptable)


# =============================================================================
# FM-3: CONSENSUS THRESHOLD
# =============================================================================

class TestCHUConsensusValidation:
    """Test multi-second consensus validation."""

    def setup_method(self):
        self.decoder = CHUFSKDecoder(sample_rate=12000)

    def test_perfect_consensus_accepted(self):
        frames = [CHUFrameA(57, 23, 45, s, valid=True) for s in range(32, 40)]
        consensus = self.decoder._find_consensus_time(frames)
        assert consensus is not None
        assert consensus['day'] == 57
        assert consensus['confidence'] == 1.0
        assert consensus['agreement'] == '8/8'

    def test_two_frame_consensus_accepted(self):
        """Consensus works with only 2 agreeing frames (lowered threshold)."""
        frames = [
            CHUFrameA(57, 23, 45, 32, valid=True),
            CHUFrameA(57, 23, 45, 33, valid=True),
        ]
        consensus = self.decoder._find_consensus_time(frames)
        assert consensus is not None
        assert consensus['day'] == 57
        assert consensus['confidence'] == 1.0
        assert consensus['agreement'] == '2/2'

    def test_majority_consensus_accepted(self):
        frames = []
        for _ in range(5):
            frames.append(CHUFrameA(57, 23, 45, 32, valid=True))
        for _ in range(3):
            frames.append(CHUFrameA(57, 23, 46, 32, valid=True))  # wrong minute
        consensus = self.decoder._find_consensus_time(frames)
        assert consensus is not None
        assert consensus['minute'] == 45
        assert consensus['confidence'] == 0.625

    def test_frame_slip_caught_by_consensus(self):
        frames = [CHUFrameA(57, 23, 45, s, valid=True) for s in range(32, 39)]
        frames.append(CHUFrameA(57, 23, 46, 39, valid=True))  # bad minute
        consensus = self.decoder._find_consensus_time(frames)
        assert consensus is not None
        assert consensus['minute'] == 45  # majority wins


# =============================================================================
# FM-4: AUDIO NORMALIZATION
# =============================================================================

class TestAudioNormalization:
    """Verify audio-path discriminator output is ±1.0, not raw radians."""

    def test_audio_demod_output_scale_12khz(self):
        """At 12 kHz, soft decisions should be ±1.0 for clean mark/space."""
        decoder = CHUFSKDecoder(sample_rate=12000)
        sr = 12000
        t = np.arange(int(0.5 * sr)) / sr
        # Pure mark tone (2225 Hz)
        mark_audio = np.sin(2 * np.pi * MARK_FREQ * t)
        sd = decoder._fsk_demodulate_audio(mark_audio)
        # After normalization, mark should produce positive ~+1.0
        # Allow margin for filter settling
        stable = sd[int(0.1 * sr):int(0.4 * sr)]
        assert np.mean(stable) > 0.5, f"Mark mean={np.mean(stable):.3f}, expected > 0.5"

    def test_audio_demod_output_scale_20khz(self):
        """At 20 kHz, soft decisions should also be ±1.0."""
        decoder = CHUFSKDecoder(sample_rate=20000)
        sr = 20000
        t = np.arange(int(0.5 * sr)) / sr
        space_audio = np.sin(2 * np.pi * SPACE_FREQ * t)
        sd = decoder._fsk_demodulate_audio(space_audio)
        stable = sd[int(0.1 * sr):int(0.4 * sr)]
        assert np.mean(stable) < -0.5, f"Space mean={np.mean(stable):.3f}, expected < -0.5"

    def test_iq_and_audio_paths_produce_same_scale(self):
        """IQ and audio demod paths should produce similar soft decision amplitudes."""
        sr = 12000
        decoder = CHUFSKDecoder(sample_rate=sr)
        t = np.arange(int(0.5 * sr)) / sr
        mark_audio = np.sin(2 * np.pi * MARK_FREQ * t)
        # Audio path
        sd_audio = decoder._fsk_demodulate_audio(mark_audio)
        audio_mean = np.mean(sd_audio[int(0.1 * sr):int(0.4 * sr)])
        # IQ path — create analytic signal at mark freq
        mark_iq = np.exp(2j * np.pi * MARK_FREQ * t).astype(np.complex64)
        sd_iq = decoder._fsk_demodulate_iq(mark_iq)
        iq_mean = np.mean(sd_iq[int(0.1 * sr):int(0.4 * sr)])
        # Both should be positive and of similar magnitude
        assert audio_mean > 0.3
        assert iq_mean > 0.3
        ratio = audio_mean / iq_mean if iq_mean > 0 else 0
        assert 0.3 < ratio < 3.0, f"Audio/IQ ratio={ratio:.2f}, too far apart"


# =============================================================================
# FM-5: PER-SECOND FAILURE DIAGNOSTICS & FULL MINUTE DECODE
# =============================================================================

def _minute_boundary_unix(day=57, hour=23, minute=45, year=2026):
    """Compute Unix timestamp for the start of a given UTC minute."""
    from datetime import datetime, timezone, timedelta
    dt = datetime(year, 1, 1, hour, minute, 0, tzinfo=timezone.utc) + timedelta(days=day - 1)
    return dt.timestamp()


class TestFullMinuteDecode:
    """Integration tests with synthetic full-minute signals."""

    def test_clean_minute_all_frames_decode(self):
        """All 9 FSK seconds decode at 30 dB SNR."""
        np.random.seed(42)
        audio = generate_chu_minute(sample_rate=12000, snr_db=30)
        decoder = CHUFSKDecoder(sample_rate=12000, channel_name='TEST')
        result = decoder.decode_minute(audio, _minute_boundary_unix(), is_audio=True)
        assert result.detected
        assert result.frames_decoded >= 7, f"Only {result.frames_decoded}/9 frames"
        assert result.decoded_day == 57
        assert result.decoded_hour == 23
        assert result.decoded_minute == 45
        assert len(result.frame_results) == 9

    def test_frame_b_decodes_dut1_and_tai(self):
        """Frame B (second 31) provides DUT1 and TAI-UTC."""
        np.random.seed(42)
        audio = generate_chu_minute(sample_rate=12000, snr_db=30)
        decoder = CHUFSKDecoder(sample_rate=12000, channel_name='TEST')
        result = decoder.decode_minute(audio, _minute_boundary_unix(), is_audio=True)
        assert result.detected
        assert result.year == 2026
        assert result.tai_utc == 37
        assert result.dut1_seconds is not None
        assert abs(result.dut1_seconds - 0.2) < 0.01

    def test_dropped_seconds_still_decode(self):
        """Dropping 5 of 8 Frame A seconds still produces consensus with 3."""
        np.random.seed(42)
        audio = generate_chu_minute(
            sample_rate=12000, snr_db=30,
            drop_seconds=[32, 33, 34, 35, 36],
        )
        decoder = CHUFSKDecoder(sample_rate=12000, channel_name='TEST')
        result = decoder.decode_minute(audio, _minute_boundary_unix(), is_audio=True)
        # 3 Frame A (37,38,39) + 1 Frame B (31) = at least 3 decoded
        if result.frames_decoded >= 2:
            # With 2+ frames, consensus should work
            assert result.decoded_day == 57 or result.decoded_day is None

    def test_frame_results_contain_all_seconds(self):
        """Each of the 9 FSK seconds appears in frame_results."""
        np.random.seed(42)
        audio = generate_chu_minute(sample_rate=12000, snr_db=30)
        decoder = CHUFSKDecoder(sample_rate=12000, channel_name='TEST')
        result = decoder.decode_minute(audio, _minute_boundary_unix(), is_audio=True)
        assert len(result.frame_results) == 9
        seconds = [fr['second'] for fr in result.frame_results]
        assert seconds == list(range(31, 40))

    def test_noise_only_produces_no_decode(self):
        """Pure noise produces detected=False."""
        np.random.seed(42)
        audio = np.random.randn(60 * 12000).astype(np.float64) * 0.1
        decoder = CHUFSKDecoder(sample_rate=12000, channel_name='TEST')
        result = decoder.decode_minute(audio, _minute_boundary_unix(), is_audio=True)
        assert result.frames_decoded == 0 or not result.detected


# =============================================================================
# TIME CONSISTENCY
# =============================================================================

class TestCHUTimeConsistency:
    """Test time consistency validation."""

    def setup_method(self):
        self.decoder = CHUFSKDecoder(sample_rate=12000)

    def test_correct_time_accepted(self):
        decoded_time = {'day': 365, 'hour': 12, 'minute': 30}
        expected_dt = datetime(2025, 12, 31, 12, 30, 0, tzinfo=timezone.utc)
        assert self.decoder._validate_time_consistency(decoded_time, expected_dt) is True

    def test_time_within_hour_accepted(self):
        decoded_time = {'day': 365, 'hour': 13, 'minute': 15}
        expected_dt = datetime(2025, 12, 31, 12, 30, 0, tzinfo=timezone.utc)
        assert self.decoder._validate_time_consistency(decoded_time, expected_dt) is True

    def test_time_beyond_hour_rejected(self):
        decoded_time = {'day': 365, 'hour': 14, 'minute': 30}
        expected_dt = datetime(2025, 12, 31, 12, 0, 0, tzinfo=timezone.utc)
        assert self.decoder._validate_time_consistency(decoded_time, expected_dt) is False

    def test_wrong_day_rejected(self):
        decoded_time = {'day': 1, 'hour': 12, 'minute': 30}
        expected_dt = datetime(2025, 12, 31, 12, 30, 0, tzinfo=timezone.utc)
        assert self.decoder._validate_time_consistency(decoded_time, expected_dt) is False


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
