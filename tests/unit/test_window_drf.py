"""Cut a time window from the raw buffer into Digital RF."""
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest
import zstandard

from hf_timestd.events.window_drf import window_files, write_window_drf

RATE = 24000
NSAMP = 48          # tiny files stand in for the 600 s ones
CH = "WWV_20000"


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


def ts(*a):
    return int(utc(*a).timestamp())


def make_file(root, channel, start, seed, written=NSAMP):
    rng = np.random.default_rng(seed)
    x = (rng.standard_normal(written) + 1j * rng.standard_normal(written)).astype(np.complex64)
    day = root / channel / datetime.fromtimestamp(start, timezone.utc).strftime("%Y%m%d")
    day.mkdir(parents=True, exist_ok=True)
    (day / f"{start}.bin.zst").write_bytes(zstandard.ZstdCompressor().compress(x.tobytes()))
    side = {"minute_boundary": start, "channel_name": channel, "frequency_hz": 20000000,
            "sample_rate": RATE, "samples_written": written, "samples_expected": NSAMP,
            "gap_count": 0, "station": {"callsign": "AC0G", "latitude": 38.9},
            "timing": {"chain": "sysclock@1", "engineering": {"judge_tier": "T6"}}}
    (day / f"{start}.json").write_text(json.dumps(side))
    return x, side


ORDER = {"event_id": "20261106T1200Z-flare-M1.0", "kind": "flare",
         "start_utc": "2026-11-06T00:05:00Z", "end_utc": "2026-11-06T00:25:00Z"}


def test_window_files_overlap(tmp_path):
    starts = [ts(2026, 11, 6, 0, m) for m in (0, 10, 20)]
    for i, s in enumerate(starts):
        make_file(tmp_path, CH, s, i)
    found, missing = window_files(tmp_path, CH, utc(2026, 11, 6, 0, 5), utc(2026, 11, 6, 0, 25))
    assert found == starts and missing == []


def test_window_across_midnight(tmp_path):
    starts = [ts(2026, 11, 5, 23, 50), ts(2026, 11, 6, 0, 0), ts(2026, 11, 6, 0, 10)]
    for i, s in enumerate(starts):
        make_file(tmp_path, CH, s, i)
    found, missing = window_files(tmp_path, CH, utc(2026, 11, 5, 23, 55), utc(2026, 11, 6, 0, 15))
    assert found == starts and missing == []


def test_missing_files_recorded(tmp_path):
    from digital_rf import DigitalRFReader
    starts = [ts(2026, 11, 6, 0, m) for m in (0, 10, 20)]
    data = {s: make_file(tmp_path, CH, s, i)[0] for i, s in enumerate(starts)}
    (tmp_path / CH / "20261106" / f"{starts[1]}.bin.zst").unlink()
    (tmp_path / CH / "20261106" / f"{starts[1]}.json").unlink()
    found, missing = window_files(tmp_path, CH, utc(2026, 11, 6, 0, 5), utc(2026, 11, 6, 0, 25))
    assert found == [starts[0], starts[2]] and missing == [starts[1]]

    out = tmp_path / "out"
    res = write_window_drf(tmp_path, [CH], ORDER, out)
    assert res["channels"][CH]["missing"] == [starts[1]]
    assert res["channels"][CH]["files"] == 2
    assert json.loads((out / "order.json").read_text())["missing"] == {CH: [starts[1]]}
    r = DigitalRFReader(str(out))
    for s in (starts[0], starts[2]):
        got = r.read_vector(s * RATE, NSAMP, CH)
        assert np.array_equal(got, data[s])
    # the gap stays a gap: nothing was written at the missing file's index
    blocks = r.get_continuous_blocks(starts[0] * RATE, starts[2] * RATE + NSAMP, CH)
    assert starts[1] * RATE not in blocks


def test_short_file_leaves_gap(tmp_path):
    from digital_rf import DigitalRFReader
    s = ts(2026, 11, 6, 0, 0)
    x, _ = make_file(tmp_path, CH, s, 1, written=30)
    out = tmp_path / "out"
    write_window_drf(tmp_path, [CH], ORDER, out)
    got = DigitalRFReader(str(out)).read_vector(s * RATE, 30, CH)
    assert np.array_equal(got, x)


def test_drf_roundtrip(tmp_path):
    from digital_rf import DigitalMetadataReader, DigitalRFReader
    starts = [ts(2026, 11, 6, 0, m) for m in (0, 10)]
    made = {s: make_file(tmp_path, CH, s, i) for i, s in enumerate(starts)}
    out = tmp_path / "out"
    write_window_drf(tmp_path, [CH], ORDER, out)
    r = DigitalRFReader(str(out))
    assert r.get_channels() == [CH]
    for s in starts:
        assert np.array_equal(r.read_vector(s * RATE, NSAMP, CH), made[s][0])
    md = DigitalMetadataReader(str(out / CH / "metadata"))
    for s in starts:
        rec = md.read(s * RATE, s * RATE + 1)[s * RATE]
        assert json.loads(str(rec["sidecar_json"])) == made[s][1]
        assert str(rec["event_id"]) == ORDER["event_id"]
        assert json.loads(str(rec["order_json"]))["kind"] == "flare"


def test_sha256sums_cover_all(tmp_path):
    for i, m in enumerate((0, 10)):
        make_file(tmp_path, CH, ts(2026, 11, 6, 0, m), i)
    out = tmp_path / "out"
    write_window_drf(tmp_path, [CH], ORDER, out)
    lines = (out / "SHA256SUMS").read_text().splitlines()
    paths = [l.split("  ", 1)[1] for l in lines]
    assert paths == sorted(paths) and len(set(paths)) == len(paths)
    on_disk = sorted(str(p.relative_to(out)) for p in out.rglob("*")
                     if p.is_file() and p.name != "SHA256SUMS")
    assert paths == on_disk
    for l in lines:
        h, rel = l.split("  ", 1)
        assert h == hashlib.sha256((out / rel).read_bytes()).hexdigest()
    assert "order.json" in paths
    r = subprocess.run(["sha256sum", "-c", "SHA256SUMS"], cwd=out, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
