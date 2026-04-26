"""
Unit tests for hf_timestd.io.digital_rf_writer

A thin wrapper around the digital_rf library. Tests mock the underlying
DigitalRFWriter so we can verify our adapter without needing the C library
or filesystem state.
"""

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

import hf_timestd.io.digital_rf_writer as drf_writer
from hf_timestd.io.digital_rf_writer import DigitalRFWriter


@pytest.fixture
def mock_drf(monkeypatch):
    """Replace the digital_rf module reference with a mock factory."""
    fake = MagicMock()
    fake.DigitalRFWriter = MagicMock()
    monkeypatch.setattr(drf_writer, 'drf', fake)
    return fake


# =============================================================================
# Initialization
# =============================================================================


class TestInitialization:
    def test_raises_if_drf_missing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(drf_writer, 'drf', None)
        with pytest.raises(ImportError, match="digital_rf"):
            DigitalRFWriter(tmp_path / 'out', sample_rate=24000,
                            channel_name='ch0')

    def test_creates_output_directory(self, mock_drf, tmp_path):
        out = tmp_path / 'sub' / 'channel_dir'
        DigitalRFWriter(out, sample_rate=24000, channel_name='ch0')
        assert out.exists()
        assert out.is_dir()

    def test_passes_args_to_underlying_writer(self, mock_drf, tmp_path):
        out = tmp_path / 'out'
        DigitalRFWriter(
            out,
            sample_rate=24000,
            channel_name='WWV_10000',
            compression_level=5,
            files_per_directory=50,
            uuid='abcd',
        )
        mock_drf.DigitalRFWriter.assert_called_once()
        kwargs = mock_drf.DigitalRFWriter.call_args.kwargs
        assert kwargs['sample_rate_numerator'] == 24000
        assert kwargs['sample_rate_denominator'] == 1
        assert kwargs['compression_level'] == 5
        assert kwargs['is_complex'] is True
        assert kwargs['num_subchannels'] == 1
        assert kwargs['dtype'] == np.complex64
        assert kwargs['uuid_str'] == 'abcd'

    def test_initialization_failure_propagates(self, mock_drf, tmp_path):
        mock_drf.DigitalRFWriter.side_effect = RuntimeError("disk full")
        with pytest.raises(RuntimeError, match="disk full"):
            DigitalRFWriter(tmp_path / 'out', sample_rate=24000,
                            channel_name='ch0')


# =============================================================================
# write_samples
# =============================================================================


class TestWriteSamples:
    def test_passes_samples_and_index_through(self, mock_drf, tmp_path):
        w = DigitalRFWriter(tmp_path / 'out', sample_rate=24000,
                            channel_name='ch0')
        samples = np.zeros(100, dtype=np.complex64)
        n = w.write_samples(samples, timestamp_samples=12345)
        assert n == 100
        # Underlying writer received the samples and index
        w.writer.rf_write.assert_called_once()
        args, _ = w.writer.rf_write.call_args
        assert args[1] == 12345
        np.testing.assert_array_equal(args[0], samples)

    def test_casts_to_complex64(self, mock_drf, tmp_path):
        w = DigitalRFWriter(tmp_path / 'out', sample_rate=24000,
                            channel_name='ch0')
        # Pass complex128 input
        samples = np.zeros(50, dtype=np.complex128)
        w.write_samples(samples, timestamp_samples=0)
        passed = w.writer.rf_write.call_args.args[0]
        assert passed.dtype == np.complex64

    def test_returns_zero_when_writer_closed(self, mock_drf, tmp_path):
        w = DigitalRFWriter(tmp_path / 'out', sample_rate=24000,
                            channel_name='ch0')
        w.close()
        n = w.write_samples(np.zeros(10, dtype=np.complex64), 0)
        assert n == 0

    def test_returns_zero_on_exception_and_logs(self, mock_drf, tmp_path,
                                                 caplog):
        w = DigitalRFWriter(tmp_path / 'out', sample_rate=24000,
                            channel_name='ch0')
        w.writer.rf_write.side_effect = OSError("write failed")
        n = w.write_samples(np.zeros(10, dtype=np.complex64), 0)
        assert n == 0
        assert any('Error writing' in r.message for r in caplog.records)


# =============================================================================
# close()
# =============================================================================


class TestClose:
    def test_close_invokes_underlying_close(self, mock_drf, tmp_path):
        w = DigitalRFWriter(tmp_path / 'out', sample_rate=24000,
                            channel_name='ch0')
        w.close()
        w.writer.close.assert_called_once()
        assert w._is_open is False

    def test_close_idempotent(self, mock_drf, tmp_path):
        w = DigitalRFWriter(tmp_path / 'out', sample_rate=24000,
                            channel_name='ch0')
        w.close()
        # Second close is a no-op (writer already None or _is_open False)
        w.close()
        w.writer.close.assert_called_once()

    def test_close_handles_exception(self, mock_drf, tmp_path, caplog):
        w = DigitalRFWriter(tmp_path / 'out', sample_rate=24000,
                            channel_name='ch0')
        w.writer.close.side_effect = OSError("eio")
        # Should not propagate
        w.close()
        assert any('Error closing' in r.message for r in caplog.records)


# =============================================================================
# Context manager
# =============================================================================


class TestContextManager:
    def test_context_closes_on_exit(self, mock_drf, tmp_path):
        with DigitalRFWriter(tmp_path / 'out', sample_rate=24000,
                              channel_name='ch0') as w:
            assert w._is_open is True
        assert w._is_open is False

    def test_context_closes_on_exception(self, mock_drf, tmp_path):
        try:
            with DigitalRFWriter(tmp_path / 'out', sample_rate=24000,
                                  channel_name='ch0') as w:
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        assert w._is_open is False
