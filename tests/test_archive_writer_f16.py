"""BinaryArchiveWriter: F16 chunks behind archive_dtype, F32 otherwise."""
import json
import logging

import numpy as np
import pytest
import zstandard

from hf_timestd.core.binary_archive_writer import BinaryArchiveConfig, BinaryArchiveWriter
from hf_timestd.core.iq_codec import F16_SCALE, decode_iq


def _writer(tmp_path, **kw):
    cfg = BinaryArchiveConfig(
        channel_name="testchan", frequency_hz=10e6, sample_rate=100,
        output_dir=tmp_path / "raw_buffer", file_duration_sec=2,
        use_tiered_storage=False, **kw)
    w = BinaryArchiveWriter(cfg)
    w._gps_time_unix = 1_000_000.0
    w._gps_time_ns_raw = int(w._gps_time_unix * 1e9)
    w._rtp_timesnap = 0
    w._check_disk_space = lambda *a, **k: True   # hermetic: the devbox disk is near full
    return w


def _flush(w, samples, fill=None):
    boundary = (int(w._gps_time_unix) // w.file_duration_sec) * w.file_duration_sec
    buf = w._start_new_minute(float(boundary), 0)
    buf.samples[:len(samples)] = samples
    buf.write_pos = len(samples) if fill is None else fill
    assert w._flush_minute(buf)
    d = w._get_minute_dir(boundary)
    side = json.loads((d / f"{boundary}.json").read_text())
    return d, boundary, side


def _samples(n):
    rng = np.random.default_rng(0)
    return (1e-3 * (rng.standard_normal(n) + 1j * rng.standard_normal(n))).astype(np.complex64)


def test_f16_chunk_sidecar_and_size(tmp_path):
    w = _writer(tmp_path, compression="zstd", archive_dtype="f16", f16_check=False)
    x = _samples(w.samples_per_chunk)
    d, b, side = _flush(w, x)
    assert side["dtype"] == "complex-f16"
    assert side["scale"] == F16_SCALE
    raw = zstandard.ZstdDecompressor().decompress((d / f"{b}.bin.zst").read_bytes())
    assert len(raw) == 4 * w.samples_per_chunk
    np.testing.assert_allclose(decode_iq(raw, side["dtype"], side["scale"]), x, atol=1e-6)


def test_partial_chunk_f16_sample_count(tmp_path):
    w = _writer(tmp_path, compression="zstd", archive_dtype="f16", f16_check=False)
    half = w.samples_per_chunk // 2
    d, b, side = _flush(w, _samples(half))
    raw = zstandard.ZstdDecompressor().decompress((d / f"{b}.bin.zst").read_bytes())
    assert side["samples_written"] == half
    assert side["samples_written"] * 4 == len(raw)


def test_overflow_chunk_stays_f32(tmp_path, caplog):
    w = _writer(tmp_path, compression="zstd", archive_dtype="f16", f16_check=False)
    x = _samples(w.samples_per_chunk)
    x[3] = 20 + 0j
    with caplog.at_level(logging.WARNING):
        d, b, side = _flush(w, x)
    assert side["dtype"] == "complex64"
    assert side["f16_fallback"] == "overflow"
    assert "scale" not in side
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
    raw = zstandard.ZstdDecompressor().decompress((d / f"{b}.bin.zst").read_bytes())
    np.testing.assert_array_equal(np.frombuffer(raw, dtype=np.complex64), x)


@pytest.mark.parametrize("comp", ["lz4", "none"])
def test_lz4_and_none_ignore_f16(tmp_path, comp):
    w = _writer(tmp_path, compression=comp, archive_dtype="f16")
    d, b, side = _flush(w, _samples(w.samples_per_chunk))
    assert side["dtype"] == "complex64"
    assert "scale" not in side and "f16_check" not in side


def test_default_unchanged(tmp_path):
    w = _writer(tmp_path, compression="zstd")
    d, b, side = _flush(w, _samples(w.samples_per_chunk))
    assert side["dtype"] == "complex64"
    assert "scale" not in side and "f16_check" not in side and "f16_fallback" not in side


def test_f16_check_lands_in_sidecar(tmp_path):
    w = _writer(tmp_path, compression="zstd", archive_dtype="f16")
    w.config.sample_rate = 100
    d, b, side = _flush(w, _samples(w.samples_per_chunk))
    assert set(side["f16_check"]) == {"err_over_floor_db_max", "carrier_dphi_rms_mrad", "seconds"}


def test_unknown_archive_dtype_rejected(tmp_path):
    with pytest.raises(ValueError, match="archive_dtype"):
        _writer(tmp_path, archive_dtype="float16")


def test_silent_chunk_sidecar_is_strict_json(tmp_path):
    w = _writer(tmp_path, compression="zstd", archive_dtype="f16")
    d, b, _ = _flush(w, np.zeros(w.samples_per_chunk, dtype=np.complex64))

    def refuse(name):
        raise AssertionError(f"non-JSON constant {name}")

    side = json.loads((d / f"{b}.json").read_text(), parse_constant=refuse)
    assert side["dtype"] == "complex-f16"
    assert side["f16_check"]["err_over_floor_db_max"] is None
    assert (d / f"{b}.bin.zst").exists()
