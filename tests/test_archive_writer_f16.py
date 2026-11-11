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


def test_sidecar_lands_before_the_bin(tmp_path, monkeypatch):
    """A crash between the two renames must never leave a bin without its sidecar."""
    import os
    w = _writer(tmp_path, compression="zstd", archive_dtype="f16", f16_check=False)
    real_replace = os.replace
    seen = []

    def checked_replace(src, dst):
        dst = str(dst)
        if dst.endswith(".bin.zst"):
            json_path = dst[:-len(".bin.zst")] + ".json"
            seen.append(os.path.exists(json_path))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", checked_replace)
    _flush(w, _samples(w.samples_per_chunk))
    assert seen == [True]


def test_tiered_move_sidecar_before_bin(tmp_path, monkeypatch):
    import shutil
    from hf_timestd.core.tiered_storage import TieredStorageConfig, TieredStorageManager
    m = TieredStorageManager(TieredStorageConfig(
        cold_buffer_root=tmp_path / "cold", hot_buffer_root=tmp_path / "hot",
        auto_configure=False, num_channels=1))
    day = m.hot_root / "chan" / "20261111"
    day.mkdir(parents=True)
    (day / "100.bin.zst").write_bytes(b"x")
    (day / "100.json").write_text("{}")
    order = []
    real_move = shutil.move
    monkeypatch.setattr(shutil, "move",
                        lambda s, d: (order.append(os.path.basename(s)), real_move(s, d))[1])
    import os
    m._move_to_cold(day / "100.bin.zst", "chan", "20261111")
    assert order == ["100.json", "100.bin.zst"]
    assert (m.cold_root / "chan" / "20261111" / "100.bin.zst").exists()


def _recorder(tmp_path, monkeypatch, recorder):
    """A CoreRecorderV2 with its radiod-facing parts stubbed out."""
    import ka9q.utils

    import hf_timestd.core.core_recorder_v2 as crv
    import hf_timestd.core.offset_judge as oj

    class _Dummy:
        def __init__(self, *a, **k):
            pass

        def start(self):
            pass

        def __getattr__(self, name):
            return lambda *a, **k: None

    monkeypatch.setattr(oj, "OffsetJudge", _Dummy)
    monkeypatch.setattr(ka9q.utils, "resolve_multicast_address",
                        lambda *a, **k: "239.1.1.1")
    monkeypatch.setattr(crv, "RadiodControl", _Dummy)
    monkeypatch.setattr(crv, "CounterMapProbe", _Dummy)
    return crv.CoreRecorderV2({
        "output_dir": str(tmp_path / "rec"),
        "ka9q": {"status": "hf-status.local"},
        "recorder": recorder})


@pytest.mark.parametrize("comp", ["lz4", "none"])
def test_f16_without_zstd_warns_once_at_recorder_startup(tmp_path, monkeypatch, caplog, comp):
    with caplog.at_level(logging.WARNING):
        _recorder(tmp_path, monkeypatch, {"archive_dtype": "f16", "compression": comp})
    warns = [r.getMessage() for r in caplog.records
             if r.levelno == logging.WARNING and "archive_dtype" in r.getMessage()]
    assert len(warns) == 1 and comp in warns[0]


def test_f16_with_zstd_no_recorder_warning(tmp_path, monkeypatch, caplog):
    with caplog.at_level(logging.WARNING):
        _recorder(tmp_path, monkeypatch, {"archive_dtype": "f16", "compression": "zstd"})
    assert not [r for r in caplog.records if "archive_dtype" in r.getMessage()]


def test_writer_leaves_the_warning_to_the_recorder(tmp_path, caplog):
    """Six channels' writers must not repeat the startup warning six times."""
    with caplog.at_level(logging.WARNING):
        _writer(tmp_path, compression="lz4", archive_dtype="f16")
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_zstd_f16_startup_is_quiet_and_names_the_format(tmp_path, caplog):
    with caplog.at_level(logging.INFO):
        _writer(tmp_path, compression="zstd", archive_dtype="f16")
    msgs = [r.getMessage() for r in caplog.records]
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    fmt = [m for m in msgs if "Format:" in m]
    assert fmt and "complex-f16" in fmt[0] and "zstd" in fmt[0]


def test_default_startup_names_complex64(tmp_path, caplog):
    with caplog.at_level(logging.INFO):
        _writer(tmp_path, compression="zstd")
    fmt = [r.getMessage() for r in caplog.records if "Format:" in r.getMessage()]
    assert fmt and "complex64" in fmt[0] and "complex-f16" not in fmt[0]
