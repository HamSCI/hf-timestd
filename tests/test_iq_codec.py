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


def _reference_f16(x):
    """The spec's encode, unblocked: (x * 4096).astype('<f2') on the float32 view."""
    return (x.view(np.float32) * np.float32(F16_SCALE)).astype("<f2").tobytes()


def test_blocked_encode_matches_reference_across_blocks(monkeypatch):
    from hf_timestd.core import iq_codec
    monkeypatch.setattr(iq_codec, "_BLOCK", 4)     # 4 float32 components per block
    rng = np.random.default_rng(1)
    x = (rng.standard_normal(11) + 1j * rng.standard_normal(11)).astype(np.complex64)
    x[2] = np.nan + 1j          # components 4, 5: block boundary
    x[5] = 1 + 1j * np.inf      # component 11: last of a block
    got = encode_f16(x)
    assert got == _reference_f16(x)


def test_blocked_encode_overflow_in_a_later_block(monkeypatch):
    from hf_timestd.core import iq_codec
    monkeypatch.setattr(iq_codec, "_BLOCK", 4)
    x = np.full(9, 1e-3 + 1e-3j, dtype=np.complex64)
    x[8] = 1e-3 + 20j            # the last, partial block overflows
    assert encode_f16(x) is None
    x[8] = -np.inf + 1e-3j       # inf alone never falls back
    assert encode_f16(x) == _reference_f16(x)


def test_encode_peak_memory_is_bounded():
    """A 600 s chunk once peaked at 3.25x its own size; blocked, under 2x."""
    import tracemalloc
    x = np.full(2_000_000, 1e-3 + 2e-3j, dtype=np.complex64)
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        out = encode_f16(x)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert out == _reference_f16(x)
    assert peak < 2 * x.nbytes


def test_decode_f32_is_zero_copy():
    x = np.arange(8, dtype=np.float32).view(np.complex64)
    raw = x.tobytes()
    got = decode_iq(raw, DTYPE_F32)
    assert np.shares_memory(got, np.frombuffer(raw, dtype=np.uint8))
    assert got.dtype == np.complex64
    np.testing.assert_array_equal(got, x)


def test_decode_f16_returns_a_fresh_writable_array():
    x = np.array([0.5 + 0.25j, -1 + 2j], dtype=np.complex64)
    raw = encode_f16(x)
    got = decode_iq(raw, DTYPE_F16, F16_SCALE)
    assert got.flags.writeable
    assert not np.shares_memory(got, np.frombuffer(raw, dtype=np.uint8))
    np.testing.assert_array_equal(got, x)


def test_decode_f32_with_a_scale_still_divides():
    x = np.array([2 + 4j], dtype=np.complex64)
    np.testing.assert_array_equal(decode_iq(x.tobytes(), DTYPE_F32, 2.0),
                                  np.array([1 + 2j], dtype=np.complex64))
