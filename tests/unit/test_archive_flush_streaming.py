"""The chunk flush streams: bounded memory, the same bytes as the one-shot write.

N8UR, 2026-11-14: the kernel OOM-killed the core recorder at nearly every
10-minute boundary.  At the boundary every channel's flush worker copied
its whole chunk out of the scratch memmap (``tobytes``), then held the
whole compressed output too — about 2x the chunk per channel, six channels
at once, ~1.5 GB of anonymous memory.  The flush now feeds zstd in blocks.

The bytes on disk must not change: each test here builds the old one-shot
output as the oracle and compares bytes.
"""
import json
import threading
import time
import tracemalloc

import numpy as np
import pytest
import zstandard

from hf_timestd.core import binary_archive_writer as baw
from hf_timestd.core import f16_check
from hf_timestd.core.binary_archive_writer import BinaryArchiveConfig, BinaryArchiveWriter
from hf_timestd.core.iq_codec import DTYPE_F16, F16_SCALE, decode_iq, encode_f16


def _writer(tmp_path, *, sample_rate=100, file_duration_sec=120, name="testchan", **kw):
    cfg = BinaryArchiveConfig(
        channel_name=name, frequency_hz=10e6, sample_rate=sample_rate,
        output_dir=tmp_path / "raw_buffer", file_duration_sec=file_duration_sec,
        use_tiered_storage=False, compression="zstd", **kw)
    w = BinaryArchiveWriter(cfg)
    w._flush_stop.set()                       # flush by hand, not via the worker
    w._gps_time_unix = 1_000_000_200.0
    w._gps_time_ns_raw = int(w._gps_time_unix * 1e9)
    w._rtp_timesnap = 0
    w._check_disk_space = lambda *a, **k: True
    return w


def _buffer(w, samples, fill=None):
    boundary = (int(w._gps_time_unix) // w.file_duration_sec) * w.file_duration_sec
    buf = w._start_new_minute(float(boundary), 0)
    buf.samples[:len(samples)] = samples
    buf.write_pos = len(samples) if fill is None else fill
    return buf, boundary


def _flush(w, samples, fill=None):
    buf, boundary = _buffer(w, samples, fill)
    assert w._flush_minute(buf)
    d = w._get_minute_dir(boundary)
    side = json.loads((d / f"{boundary}.json").read_text())
    return (d / f"{boundary}.bin.zst").read_bytes(), side


def _samples(n, seed=0, amp=1e-2):
    rng = np.random.default_rng(seed)
    return (amp * (rng.standard_normal(n) + 1j * rng.standard_normal(n))).astype(np.complex64)


def _one_shot(raw: bytes, level=3) -> bytes:
    """The pre-2026-11-14 write: one compress() call over the whole chunk."""
    return zstandard.ZstdCompressor(level=level, threads=1).compress(raw)


# ---------------------------------------------------------------- byte identity

@pytest.mark.parametrize("fill_frac", [1.0, 0.5, 0.0])
def test_complex64_bytes_match_one_shot(tmp_path, monkeypatch, fill_frac):
    monkeypatch.setattr(baw, "_STREAM_BLOCK_SAMPLES", 1000)   # many blocks, ragged tail
    w = _writer(tmp_path)
    x = _samples(w.samples_per_chunk)
    fill = int(w.samples_per_chunk * fill_frac)
    blob, side = _flush(w, x, fill=fill)
    assert blob == _one_shot(x[:fill].tobytes())
    assert side["dtype"] == "complex64"
    assert side["samples_written"] == fill


def test_complex64_bytes_match_one_shot_at_station_scale(tmp_path):
    """Real block size, a chunk many blocks long (24 kHz, 60 s)."""
    w = _writer(tmp_path, sample_rate=24000, file_duration_sec=60)
    x = _samples(w.samples_per_chunk, seed=3)
    blob, _ = _flush(w, x)
    assert blob == _one_shot(x.tobytes())


@pytest.mark.parametrize("fill_frac", [1.0, 0.37])
def test_f16_bytes_match_one_shot(tmp_path, monkeypatch, fill_frac):
    monkeypatch.setattr(baw, "_STREAM_BLOCK_SAMPLES", 999)
    w = _writer(tmp_path, archive_dtype="f16", f16_check=False)
    x = _samples(w.samples_per_chunk, seed=1)
    fill = int(w.samples_per_chunk * fill_frac)
    blob, side = _flush(w, x, fill=fill)
    assert blob == _one_shot(encode_f16(x[:fill]))
    assert side["dtype"] == DTYPE_F16
    assert side["scale"] == F16_SCALE


def test_f16_overflow_late_in_chunk_falls_back_to_the_same_f32_bytes(tmp_path, monkeypatch):
    """The overflow sits in the LAST block, after F16 bytes already streamed out."""
    monkeypatch.setattr(baw, "_STREAM_BLOCK_SAMPLES", 1000)
    w = _writer(tmp_path, archive_dtype="f16", f16_check=True)
    x = _samples(w.samples_per_chunk, seed=2)
    x[-3] = 20 + 0j
    blob, side = _flush(w, x)
    assert blob == _one_shot(x.tobytes())
    assert side["dtype"] == "complex64"
    assert side["f16_fallback"] == "overflow"
    assert "scale" not in side and "f16_check" not in side
    d = w._get_minute_dir(side["minute_boundary"])
    assert not list(d.glob("*.tmp")) and not list(d.glob("*.scratch"))


def test_f16_check_matches_the_whole_chunk_computation(tmp_path):
    w = _writer(tmp_path, archive_dtype="f16", f16_check=True)
    x = _samples(w.samples_per_chunk, seed=4)
    x += (0.3 * np.exp(2j * np.pi * 0.07 * np.arange(len(x)))).astype(np.complex64)
    _, side = _flush(w, x)
    n = int(w.config.sample_rate) * 60
    want = f16_check.measure(x[:n], decode_iq(encode_f16(x)[:n * 4], DTYPE_F16, F16_SCALE),
                             w.config.sample_rate)
    assert side["f16_check"] == json.loads(json.dumps(want))
    assert list(side).index("scale") < list(side).index("f16_check")


# ---------------------------------------------------------------- bounded memory

def _flush_peak_bytes(w, x):
    buf, _ = _buffer(w, x)
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        assert w._flush_minute(buf)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return peak


@pytest.mark.parametrize("dtype", ["complex64", "f16"])
def test_flush_peak_memory_does_not_scale_with_the_chunk(tmp_path, dtype):
    """A 300 s chunk at 24 kHz is 57.6 MB of complex64.  The one-shot flush
    peaked at >= 2x that (the tobytes copy plus the compressed output); the
    streaming flush stays at a few blocks whatever the chunk length."""
    w = _writer(tmp_path, sample_rate=24000, file_duration_sec=300,
                archive_dtype=dtype, f16_check=False)
    tile = _samples(1 << 20, seed=5)
    x = np.resize(tile, w.samples_per_chunk)
    peak = _flush_peak_bytes(w, x)
    chunk_bytes = 8 * w.samples_per_chunk
    assert peak < 32e6, f"flush peak {peak / 1e6:.1f} MB for a {chunk_bytes / 1e6:.1f} MB chunk"


def test_f16_checks_run_one_at_a_time_across_channels(tmp_path, monkeypatch):
    """The check holds two minute-long complex128 arrays plus Welch scratch,
    ~50 MB at 24 kHz.  Six channels close on the same boundary; run
    together, the checks alone cost ~300 MB.  One at a time costs one."""
    active = [0]
    worst = [0]
    lock = threading.Lock()
    real = f16_check.measure

    def slow_measure(*a, **k):
        with lock:
            active[0] += 1
            worst[0] = max(worst[0], active[0])
        time.sleep(0.05)
        try:
            return real(*a, **k)
        finally:
            with lock:
                active[0] -= 1

    monkeypatch.setattr(f16_check, "measure", slow_measure)
    jobs = []
    for c in range(4):
        w = _writer(tmp_path, name=f"chan{c}", archive_dtype="f16", f16_check=True)
        jobs.append((w, _buffer(w, _samples(w.samples_per_chunk, seed=c))[0]))
    results = []
    threads = [threading.Thread(target=lambda w=w, b=b: results.append(w._flush_minute(b)))
               for w, b in jobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [True] * 4
    assert worst[0] == 1


def test_zstd_compressions_run_one_at_a_time_across_channels(tmp_path, monkeypatch):
    """A threads=1 zstd context is ZSTDMT with one worker: ~50-60 MB of job
    buffers each, regardless of input size.  Six channels compressing at
    once on the boundary held ~0.4 GB in contexts alone."""
    active = [0]
    worst = [0]
    lock = threading.Lock()
    real = zstandard.ZstdCompressor

    class CountingCompressor:
        def __init__(self, *a, **k):
            self._c = real(*a, **k)

        def compressobj(self, *a, **k):
            inner = self._c.compressobj(*a, **k)
            with lock:
                active[0] += 1
                worst[0] = max(worst[0], active[0])

            class Obj:
                def compress(self, data):
                    time.sleep(0.002)
                    return inner.compress(data)

                def flush(self, *fa):
                    with lock:
                        active[0] -= 1
                    return inner.flush(*fa)
            return Obj()

    monkeypatch.setattr(zstandard, "ZstdCompressor", CountingCompressor)
    monkeypatch.setattr(baw, "_STREAM_BLOCK_SAMPLES", 500)
    jobs = []
    for c in range(4):
        w = _writer(tmp_path, name=f"chan{c}")
        jobs.append((w, _buffer(w, _samples(w.samples_per_chunk, seed=c))[0]))
    results = []
    threads = [threading.Thread(target=lambda w=w, b=b: results.append(w._flush_minute(b)))
               for w, b in jobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [True] * 4
    assert worst[0] == 1
