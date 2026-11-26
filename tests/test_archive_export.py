"""archive export-f32: an archived window back to F32 (design part 3)."""
import json
from datetime import datetime, timezone

import numpy as np
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


def test_export_keeps_fill_runs(tmp_path):
    src = tmp_path / "ssd" / "raw_buffer" / "WWV_10000" / "20251112"
    root = tmp_path / "drive"
    drive = root / "raw_buffer" / "WWV_10000" / "20251112"
    x = _noise(); x[100:200] = 0
    b, j = _write_chunk(src, 1762905600, x,
                        extra={"fill_runs": [[100, 200]], "gap_fill": "zeros"})
    A.copy_one(b, j, drive, "f16")
    A.export_f32(root, tmp_path / "out", "WWV_10000",
                 datetime.fromtimestamp(1762905600, timezone.utc),
                 datetime.fromtimestamp(1762905660, timezone.utc))
    out = tmp_path / "out" / "WWV_10000" / "20251112"
    meta = json.loads((out / "1762905600.json").read_text())
    assert meta["fill_runs"] == [[100, 200]] and meta["dtype"] == "complex64"
    raw = zstd.ZstdDecompressor().decompress((out / "1762905600.bin.zst").read_bytes())
    assert np.all(iq_codec.decode_iq(raw, "complex64")[100:200] == 0)


def test_export_window_bounds(tmp_path):
    root, _ = _archive(tmp_path)
    n = A.export_f32(root, tmp_path / "out", "WWV_10000",
                     datetime.fromtimestamp(1762905660, timezone.utc),
                     datetime.fromtimestamp(1762905720, timezone.utc))
    assert n == 1
    assert [p.name for p in (tmp_path / "out" / "WWV_10000" / "20251112").glob("*.json")] \
        == ["1762905660.json"]


def test_export_carries_a_segment_under_its_own_name(tmp_path):
    """A chunk reopened at its boundary has a <boundary>.seg<N> file; the
    export used to parse the stem as a number and drop it."""
    src = tmp_path / "ssd" / "raw_buffer" / "WWV_10000" / "20251112"
    root = tmp_path / "drive"
    drive = root / "raw_buffer" / "WWV_10000" / "20251112"
    for stem, seed in (("1762905600", 1), ("1762905600.seg2", 2)):
        b, j = _write_chunk(src, stem, _noise(seed=seed))
        A.copy_one(b, j, drive, "f16")
    n = A.export_f32(root, tmp_path / "out", "WWV_10000",
                     datetime.fromtimestamp(1762905600, timezone.utc),
                     datetime.fromtimestamp(1762905660, timezone.utc))
    assert n == 2
    assert sorted(p.name for p in (tmp_path / "out" / "WWV_10000" / "20251112").glob("*.json")) \
        == ["1762905600.json", "1762905600.seg2.json"]
