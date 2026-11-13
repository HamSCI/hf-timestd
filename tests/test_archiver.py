"""archiver.py: copy closed raw IQ to the archive drive (design 2026-11-12, part 2)."""
import errno
import hashlib
import json
import os
import time
from pathlib import Path

import numpy as np
import pytest
import zstandard as zstd

from hf_timestd import archiver as A
from hf_timestd.core import iq_codec


def _write_chunk(day_dir: Path, start: int, samples: np.ndarray, dtype: str = "complex64",
                 extra: dict | None = None, age_s: float = 600) -> tuple:
    day_dir.mkdir(parents=True, exist_ok=True)
    if dtype == "complex64":
        raw = np.ascontiguousarray(samples, dtype=np.complex64).tobytes()
    else:
        raw = iq_codec.encode_f16(samples)
    b = day_dir / f"{start}.bin.zst"
    j = day_dir / f"{start}.json"
    b.write_bytes(zstd.ZstdCompressor(level=3).compress(raw))
    meta = {"minute_boundary": start, "samples_written": len(samples),
            "sample_rate": 24000, "dtype": dtype, "compression": "zstd",
            "f16_check": {"seconds": 60}}
    if dtype == iq_codec.DTYPE_F16:
        meta["scale"] = iq_codec.F16_SCALE
    meta.update(extra or {})
    j.write_text(json.dumps(meta))
    t = time.time() - age_s
    os.utime(b, (t, t))
    os.utime(j, (t, t))
    return b, j


def _noise(n=24000, seed=1, amp=3.5e-3):
    rng = np.random.default_rng(seed)
    return (amp * (rng.standard_normal(n) + 1j * rng.standard_normal(n))).astype(np.complex64)


def _decode(path: Path, meta: dict) -> np.ndarray:
    raw = zstd.ZstdDecompressor().decompress(path.read_bytes())
    return iq_codec.decode_iq(raw, meta.get("dtype", "complex64"), float(meta.get("scale", 1.0)))


def test_f32_source_converted(tmp_path):
    src = tmp_path / "ssd" / "WWV_10000" / "20261110"
    x = _noise()
    b, j = _write_chunk(src, 1762732800, x)
    dst = tmp_path / "drive" / "WWV_10000" / "20261110"
    assert A.copy_one(b, j, dst, "f16") == "converted"
    meta = json.loads((dst / j.name).read_text())
    assert meta["dtype"] == "complex-f16"
    assert meta["scale"] == 4096
    assert meta["converted_from"] == "complex64"
    assert "f16_check" not in meta
    assert meta["samples_written"] == len(x)
    assert A.f16_within_rounding(x, _decode(dst / b.name, meta))


def test_f16_source_byte_copy(tmp_path):
    src = tmp_path / "ssd" / "C" / "20261110"
    b, j = _write_chunk(src, 1762732800, _noise(), dtype=iq_codec.DTYPE_F16)
    dst = tmp_path / "drive" / "C" / "20261110"
    assert A.copy_one(b, j, dst, "f16") == "copied"
    assert (dst / b.name).read_bytes() == b.read_bytes()
    assert json.loads((dst / j.name).read_text()) == json.loads(j.read_text())


def test_complex64_mode_byte_copy_sha(tmp_path):
    src = tmp_path / "ssd" / "C" / "20261110"
    b, j = _write_chunk(src, 1762732800, _noise())
    dst = tmp_path / "drive" / "C" / "20261110"
    assert A.copy_one(b, j, dst, "complex64") == "copied"
    h = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    assert h(dst / b.name) == h(b)


def test_overflow_kept_f32(tmp_path):
    src = tmp_path / "ssd" / "C" / "20261110"
    x = _noise()
    x[3] = 20.0 + 0j
    b, j = _write_chunk(src, 1762732800, x)
    dst = tmp_path / "drive" / "C" / "20261110"
    assert A.copy_one(b, j, dst, "f16") == "kept_f32"
    meta = json.loads((dst / j.name).read_text())
    assert meta.get("dtype", "complex64") == "complex64"
    assert (dst / b.name).read_bytes() == b.read_bytes()


def test_open_files_skipped(tmp_path):
    raw = tmp_path / "raw_buffer"
    _write_chunk(raw / "C" / "20261110", 1762732800, _noise(), age_s=600)
    _write_chunk(raw / "C" / "20261110", 1762732860, _noise(), age_s=30)      # still young
    d = raw / "C" / "20261110"
    (d / "1762732920.bin.zst").write_bytes(b"x")                                # no sidecar
    os.utime(d / "1762732920.bin.zst", (time.time() - 900,) * 2)
    got = [p[0].name for p in A.closed_files(raw, time.time())]
    assert got == ["1762732800.bin.zst"]


def test_closed_files_oldest_day_first(tmp_path):
    raw = tmp_path / "raw_buffer"
    _write_chunk(raw / "B" / "20261111", 1762819200, _noise())
    _write_chunk(raw / "A" / "20261110", 1762732800, _noise())
    days = [p[0].parent.name for p in A.closed_files(raw, time.time())]
    assert days == ["20261110", "20261111"]


def test_bad_sidecar_skipped(tmp_path):
    src = tmp_path / "ssd" / "C" / "20261110"
    b, j = _write_chunk(src, 1762732800, _noise())
    j.write_text("{not json")
    dst = tmp_path / "drive" / "C" / "20261110"
    assert A.copy_one(b, j, dst, "f16") == "skipped"
    assert not dst.exists() or not any(dst.iterdir())


def test_existing_destination_skipped(tmp_path):
    src = tmp_path / "ssd" / "C" / "20261110"
    b, j = _write_chunk(src, 1762732800, _noise())
    dst = tmp_path / "drive" / "C" / "20261110"
    assert A.copy_one(b, j, dst, "f16") == "converted"
    assert A.copy_one(b, j, dst, "f16") == "skipped"


def test_part_file_never_left_on_verify_fail(tmp_path, monkeypatch):
    src = tmp_path / "ssd" / "C" / "20261110"
    b, j = _write_chunk(src, 1762732800, _noise())
    dst = tmp_path / "drive" / "C" / "20261110"
    monkeypatch.setattr(A, "f16_within_rounding", lambda a, b: False)
    with pytest.raises(A.VerifyError):
        A.copy_one(b, j, dst, "f16")
    assert list(dst.iterdir()) == []


def test_copy_error_ends_run_cleanly(tmp_path, monkeypatch):
    src = tmp_path / "ssd" / "C" / "20261110"
    b, j = _write_chunk(src, 1762732800, _noise())
    dst = tmp_path / "drive" / "C" / "20261110"

    def boom(*a, **k):
        raise OSError(errno.ENODEV, "No such device")
    monkeypatch.setattr(A, "_write_part", boom)
    with pytest.raises(A.ArchiveIOError):
        A.copy_one(b, j, dst, "f16")
    assert not any(p.name.endswith(".part") for p in dst.rglob("*")) if dst.exists() else True
    assert b.exists() and j.exists()


def test_rounding_bound():
    x = _noise()
    y = iq_codec.decode_iq(iq_codec.encode_f16(x), iq_codec.DTYPE_F16, iq_codec.F16_SCALE)
    assert A.f16_within_rounding(x, y)
    z = y.copy()
    z[10] += 1e-3
    assert not A.f16_within_rounding(x, z)
    n = x.copy()
    n[5] = np.nan
    assert not A.f16_within_rounding(n, y)          # NaN positions must match
