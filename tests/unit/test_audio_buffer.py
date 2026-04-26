"""
Unit tests for hf_timestd.core.audio_buffer

AudioBuffer takes IQ samples, AM-demodulates, downsamples to 8 kHz, and
writes to a circular int16 PCM file plus a metadata sidecar.

Tests cover:
- Constructor creates the buffer file pre-zeroed and writes a meta file
- Resampling and rational ratio computation (gcd-based)
- Wrap-around in the circular buffer (split write across buffer end)
- Empty input is a no-op
- NaN/Inf inputs are sanitized
- Silence (all-zero) input doesn't divide-by-zero
- AudioBufferManager creates buffers on demand and dispatches writes
"""

import struct

import numpy as np
import pytest

from hf_timestd.core.audio_buffer import (
    AUDIO_SAMPLE_RATE,
    BUFFER_SAMPLES,
    BUFFER_SECONDS,
    AudioBuffer,
    AudioBufferManager,
)


# =============================================================================
# Module constants
# =============================================================================


class TestModuleConstants:
    def test_buffer_samples_consistent(self):
        assert BUFFER_SAMPLES == AUDIO_SAMPLE_RATE * BUFFER_SECONDS

    def test_audio_rate_is_8khz(self):
        # Documented expectation in the module
        assert AUDIO_SAMPLE_RATE == 8000


# =============================================================================
# AudioBuffer
# =============================================================================


@pytest.fixture
def buffer(tmp_path):
    # 24 kHz IQ → 8 kHz audio (downsample 3:1)
    return AudioBuffer('WWV_10000', str(tmp_path), input_sample_rate=24000)


class TestAudioBufferConstruction:
    def test_files_created(self, buffer, tmp_path):
        assert buffer.buffer_file.exists()
        assert buffer.meta_file.exists()
        assert buffer.buffer_file.parent.name == 'audio_buffers'

    def test_buffer_file_pre_zeroed(self, buffer):
        data = buffer.buffer_file.read_bytes()
        # 5 seconds × 8 kHz × 2 bytes per int16 = 80 000 bytes
        assert len(data) == BUFFER_SAMPLES * 2
        assert all(b == 0 for b in data)

    def test_meta_file_format(self, buffer):
        raw = buffer.meta_file.read_bytes()
        # uint32 write_pos, uint32 sample_rate, uint32 buffer_samples, double timestamp
        assert len(raw) == 4 + 4 + 4 + 8
        write_pos, sample_rate, buf_samples, _timestamp = struct.unpack(
            '<IIId', raw)
        assert write_pos == 0
        assert sample_rate == AUDIO_SAMPLE_RATE
        assert buf_samples == BUFFER_SAMPLES

    def test_initial_state(self, buffer):
        assert buffer.write_pos == 0
        assert buffer.input_sample_rate == 24000
        assert buffer.output_sample_rate == AUDIO_SAMPLE_RATE


# =============================================================================
# write_iq behavior
# =============================================================================


class TestWriteIQ:
    def test_empty_input_is_noop(self, buffer):
        before_pos = buffer.write_pos
        buffer.write_iq(np.array([], dtype=np.complex64))
        assert buffer.write_pos == before_pos

    def test_one_second_of_iq_advances_write_pos(self, buffer):
        # 1 second at 24 kHz IQ → ~1 second at 8 kHz audio
        n_in = 24000
        # Use a sinusoid so the AM envelope is non-trivial
        t = np.arange(n_in) / 24000.0
        iq = (np.cos(2 * np.pi * 1000 * t) + 1j * np.sin(2 * np.pi * 1000 * t)
              ).astype(np.complex64)
        buffer.write_iq(iq)
        # After resampling to 8 kHz we expect ~8000 output samples
        assert 7900 <= buffer.write_pos <= 8100

    def test_handles_nan_and_inf_inputs(self, buffer):
        # nan_to_num path: NaN/Inf in input must not crash
        iq = np.full(24000, complex(float('nan'), float('nan')),
                     dtype=np.complex64)
        iq[100:200] = complex(float('inf'), 0)
        buffer.write_iq(iq)
        # write_pos advanced — sanitisation succeeded
        assert buffer.write_pos > 0

    def test_silence_does_not_divide_by_zero(self, buffer):
        # All-zero IQ → max amplitude is zero; the divide-by-zero guard
        # should keep audio silent and not crash
        iq = np.zeros(24000, dtype=np.complex64)
        buffer.write_iq(iq)
        # Buffer file remains all zeros
        data = buffer.buffer_file.read_bytes()
        # First 8000 samples × 2 bytes = 16000 bytes should still be zero
        assert all(b == 0 for b in data[:16000])

    def test_circular_wrap_around(self, buffer):
        # First fill the buffer almost to its end, then write enough samples
        # to require a split write across the boundary.
        buffer.write_pos = BUFFER_SAMPLES - 1000  # 1000 samples to end
        # Generate IQ that produces ~3000 audio samples
        n_in = 9000  # 3 audio seconds at 8 kHz
        t = np.arange(n_in) / 24000.0
        iq = (np.cos(2 * np.pi * 500 * t) + 1j * np.sin(2 * np.pi * 500 * t)
              ).astype(np.complex64)
        buffer.write_iq(iq)
        # Wrapped around: write_pos < BUFFER_SAMPLES - 1000
        assert buffer.write_pos < BUFFER_SAMPLES - 1000

    def test_meta_file_updated_after_write(self, buffer):
        # write_iq updates the meta file's write_pos
        iq = np.ones(24000, dtype=np.complex64)
        buffer.write_iq(iq)
        raw = buffer.meta_file.read_bytes()
        write_pos, sample_rate, buf_samples, _ = struct.unpack('<IIId', raw)
        assert write_pos == buffer.write_pos
        assert sample_rate == AUDIO_SAMPLE_RATE
        assert buf_samples == BUFFER_SAMPLES


# =============================================================================
# AudioBufferManager
# =============================================================================


class TestAudioBufferManager:
    def test_creates_buffer_on_first_get(self, tmp_path):
        mgr = AudioBufferManager(data_root=str(tmp_path), sample_rate=24000)
        buf = mgr.get_buffer('WWV_10000')
        assert isinstance(buf, AudioBuffer)
        assert 'WWV_10000' in mgr.buffers

    def test_singleton_per_channel(self, tmp_path):
        mgr = AudioBufferManager(data_root=str(tmp_path), sample_rate=24000)
        a = mgr.get_buffer('WWV_10000')
        b = mgr.get_buffer('WWV_10000')
        assert a is b

    def test_separate_buffers_per_channel(self, tmp_path):
        mgr = AudioBufferManager(data_root=str(tmp_path), sample_rate=24000)
        a = mgr.get_buffer('WWV_10000')
        b = mgr.get_buffer('CHU_3330')
        assert a is not b
        assert a.channel_name != b.channel_name

    def test_write_iq_routes_to_channel_buffer(self, tmp_path):
        mgr = AudioBufferManager(data_root=str(tmp_path), sample_rate=24000)
        mgr.write_iq('WWV_10000', np.ones(24000, dtype=np.complex64))
        assert mgr.buffers['WWV_10000'].write_pos > 0
