"""archive export-f32: an archived window back to F32 (design part 3)."""
import json
from datetime import datetime, timezone

import zstandard as zstd

from hf_timestd import archiver as A
from hf_timestd.core import iq_codec
from tests.test_archiver import _noise, _write_chunk


def _archive(tmp_path):
    src = tmp_path / "ssd" / "raw_buffer" / "WWV_10000" / "20251112"
    drive = tmp_path / "drive" / "raw_buffer" / "WWV_10000" / "20251112"
    xs = {}
    for s in (1762905600, 1762905660, 1762905720):
        x = _noise(seed=s % 97)
        b, j = _write_chunk(src, s, x)
        A.copy_one(b, j, drive, "f16")
        xs[s] = x
    return tmp_path / "drive", xs


def test_export_roundtrip(tmp_path):
    root, xs = _archive(tmp_path)
    out = tmp_path / "out"
    n = A.export_f32(root, out, "WWV_10000",
                     datetime.fromtimestamp(1762905600, timezone.utc),
                     datetime.fromtimestamp(1762905780, timezone.utc))
    assert n == 3
    for s, x in xs.items():
        meta = json.loads((out / "WWV_10000" / "20251112" / f"{s}.json").read_text())
        assert meta["dtype"] == "complex64"
        assert "scale" not in meta and "converted_from" not in meta
        raw = zstd.ZstdDecompressor().decompress(
            (out / "WWV_10000" / "20251112" / f"{s}.bin.zst").read_bytes())
        assert A.f16_within_rounding(x, iq_codec.decode_iq(raw, "complex64"))


def test_export_window_bounds(tmp_path):
    root, _ = _archive(tmp_path)
    n = A.export_f32(root, tmp_path / "out", "WWV_10000",
                     datetime.fromtimestamp(1762905660, timezone.utc),
                     datetime.fromtimestamp(1762905720, timezone.utc))
    assert n == 1
    assert [p.name for p in (tmp_path / "out" / "WWV_10000" / "20251112").glob("*.json")] \
        == ["1762905660.json"]
