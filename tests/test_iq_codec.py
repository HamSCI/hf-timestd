"""iq_codec — F16 encode/decode of 24 kHz complex I/Q at a fixed 2^12 scale."""

import numpy as np
import pytest

from hf_timestd.core.iq_codec import (
    DTYPE_F16,
    DTYPE_F32,
    F16_SCALE,
    bytes_per_sample,
    decode_iq,
    encode_f16,
)


def test_roundtrip_error_far_below_signal():
    rng = np.random.default_rng(0)
    x = ((rng.standard_normal(240000) + 1j * rng.standard_normal(240000)) * 3.5e-6).astype(np.complex64)
    y = decode_iq(encode_f16(x), DTYPE_F16, F16_SCALE)
    assert y.dtype == np.complex64 and y.shape == x.shape
    sqnr = 10 * np.log10(np.sum(np.abs(x) ** 2) / np.sum(np.abs(x - y) ** 2))
    assert sqnr > 70.0


def test_overflow_returns_none():
    x = np.array([20.0 + 0j], dtype=np.complex64)  # 20 * 4096 > 65504
    assert encode_f16(x) is None


def test_overflow_boundary_is_binary16_max():
    assert encode_f16(np.array([65504.0 / 4096 + 0j], dtype=np.complex64)) is not None
    assert encode_f16(np.array([16.0 + 0j], dtype=np.complex64)) is None  # 65536 > 65504


def test_nan_inf_pass_through():
    x = np.array([np.nan + 1j, np.inf + 0j, 1e-5 + 0j], dtype=np.complex64)
    y = decode_iq(encode_f16(x), DTYPE_F16, F16_SCALE)
    assert np.isnan(y[0].real) and np.isinf(y[1].real)


def test_decode_complex64_is_identity():
    x = np.arange(4, dtype=np.float32).view(np.complex64)
    assert np.array_equal(decode_iq(x.tobytes(), DTYPE_F32), x)


def test_unknown_dtype_raises():
    with pytest.raises(ValueError):
        decode_iq(b"\0" * 8, "int16")


def test_bytes_per_sample():
    assert bytes_per_sample(DTYPE_F32) == 8 and bytes_per_sample(DTYPE_F16) == 4
