"""BinaryArchiveReader and the Digital RF export decode F16 chunks by sidecar."""
import json
from datetime import datetime, timezone

import numpy as np
import zstandard

from hf_timestd.core.binary_archive_writer import (
    BinaryArchiveConfig, BinaryArchiveReader, BinaryArchiveWriter)
from hf_timestd.core.iq_codec import DTYPE_F16, F16_SCALE, encode_f16
from hf_timestd.events.window_drf import write_window_drf


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


def _flush_at(w, boundary, samples):
    buf = w._start_new_minute(float(boundary), 0)
    buf.samples[:len(samples)] = samples
    buf.write_pos = len(samples)
    assert w._flush_minute(buf)


def _samples(n, seed=0):
    rng = np.random.default_rng(seed)
    return (1e-3 * (rng.standard_normal(n) + 1j * rng.standard_normal(n))).astype(np.complex64)


def _rel_rms(got, want):
    return np.sqrt(np.mean(np.abs(got - want) ** 2)) / np.sqrt(np.mean(np.abs(want) ** 2))


def _reader(tmp_path):
    return BinaryArchiveReader(tmp_path / "raw_buffer", "testchan")


def test_read_chunk_f16_amplitude(tmp_path):
    w = _writer(tmp_path, compression="zstd", archive_dtype="f16", f16_check=False)
    x = _samples(w.samples_per_chunk)
    _flush_at(w, 1_000_000, x)
    got, side = _reader(tmp_path).read_chunk(1_000_000)
    assert got.dtype == np.complex64
    assert side["dtype"] == DTYPE_F16
    assert _rel_rms(got, x) < 1e-3
    np.testing.assert_array_equal(_reader(tmp_path).read_minute(1_000_000), got)


def test_mixed_day_reads_both(tmp_path):
    x = _samples(200)
    f32 = _writer(tmp_path, compression="zstd", f16_check=False)
    _flush_at(f32, 1_000_000, x)
    f16 = _writer(tmp_path, compression="zstd", archive_dtype="f16", f16_check=False)
    _flush_at(f16, 1_000_002, x)
    r = _reader(tmp_path)
    a, sa = r.read_chunk(1_000_000)
    b, sb = r.read_chunk(1_000_002)
    assert sa["dtype"] == "complex64" and sb["dtype"] == DTYPE_F16
    assert a.dtype == b.dtype == np.complex64
    np.testing.assert_array_equal(a, x)
    assert _rel_rms(b, x) < 1e-3


def test_sample_rate_from_sidecar(tmp_path):
    day = tmp_path / "raw_buffer" / "testchan" / "19700112"
    day.mkdir(parents=True)
    x = _samples(8)
    (day / "1000000.bin.zst").write_bytes(zstandard.ZstdCompressor().compress(x.tobytes()))
    (day / "1000000.json").write_text(json.dumps({"sample_rate": 24000}))
    r = _reader(tmp_path)
    assert r.sample_rate == 20000          # fallback until a sidecar speaks
    r.read_chunk(1_000_000)
    assert r.sample_rate == 24000


def _make_chunk(root, channel, start, x, f16):
    day = root / channel / datetime.fromtimestamp(start, timezone.utc).strftime("%Y%m%d")
    day.mkdir(parents=True, exist_ok=True)
    side = {"sample_rate": 24000, "samples_written": len(x)}
    if f16:
        raw = encode_f16(x)
        side.update(dtype=DTYPE_F16, scale=F16_SCALE)
    else:
        raw = x.tobytes()
        side.update(dtype="complex64")
    (day / f"{start}.bin.zst").write_bytes(zstandard.ZstdCompressor().compress(raw))
    (day / f"{start}.json").write_text(json.dumps(side))


def test_window_drf_spans_f32_and_f16(tmp_path):
    from digital_rf import DigitalMetadataReader, DigitalRFReader
    ch = "WWV_20000"
    s0 = int(datetime(2026, 11, 6, 0, 0, tzinfo=timezone.utc).timestamp())
    starts = [s0, s0 + 600]
    xs = [_samples(48, seed=1), _samples(48, seed=2)]
    _make_chunk(tmp_path, ch, starts[0], xs[0], f16=False)
    _make_chunk(tmp_path, ch, starts[1], xs[1], f16=True)
    order = {"event_id": "e", "start_utc": "2026-11-06T00:00:00Z",
             "end_utc": "2026-11-06T00:20:00Z"}
    out = tmp_path / "out"
    res = write_window_drf(tmp_path, [ch], order, out)
    assert res["channels"][ch]["files"] == 2
    r = DigitalRFReader(str(out))
    for s, x in zip(starts, xs):
        got = r.read_vector(s * 24000, 48, ch)
        assert got.dtype == np.complex64
        assert _rel_rms(got, x) < 1e-3
    md = DigitalMetadataReader(str(out / ch / "metadata"))
    recs = md.read(starts[0] * 24000, starts[1] * 24000 + 1)
    first = recs[starts[0] * 24000]
    assert list(first["stored_dtype"]) == ["complex64", DTYPE_F16]
    assert list(first["stored_scale"]) == [1.0, F16_SCALE]


def test_window_drf_uniform_f16_metadata_is_scalar(tmp_path):
    from digital_rf import DigitalMetadataReader
    ch = "WWV_20000"
    s0 = int(datetime(2026, 11, 6, 0, 0, tzinfo=timezone.utc).timestamp())
    _make_chunk(tmp_path, ch, s0, _samples(48), f16=True)
    order = {"event_id": "e", "start_utc": "2026-11-06T00:00:00Z",
             "end_utc": "2026-11-06T00:10:00Z"}
    write_window_drf(tmp_path, [ch], order, tmp_path / "out")
    md = DigitalMetadataReader(str(tmp_path / "out" / ch / "metadata"))
    rec = md.read(s0 * 24000, s0 * 24000 + 1)[s0 * 24000]
    assert rec["stored_dtype"] == DTYPE_F16
    assert rec["stored_scale"] == F16_SCALE
