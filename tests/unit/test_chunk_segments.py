"""Readers see a chunk's later segments, <boundary>.seg<N>, and join them.

A chunk reopened at a boundary already on disk (a recorder restart mid-chunk,
a pair step over 0.25 s) never replaces the earlier file: the writer names the
later one ``<boundary>.seg<N>``.  Every reader used to parse the stem as an
integer and skip it, so the samples written after the restart never reached
GRAPE, the event exports or storage tiering.  On B4, 2026-11-23, 45 segment
files (552 MB) sat in /dev/shm, never moved to disk.
"""
import json
import time
from datetime import datetime, timedelta, timezone

import numpy as np

from hf_timestd.core.binary_archive_writer import (
    BinaryArchiveConfig, BinaryArchiveReader, BinaryArchiveWriter)
from hf_timestd.core.chunk_segments import chunk_stem, join_segments, parse_chunk_name
from hf_timestd.core.leap_second import get_current_gps_leap_seconds
from hf_timestd.core.tiered_storage import TieredStorageConfig, TieredStorageManager
from hf_timestd.events.window_drf import window_files

SR = 100
GPS_MINUS_UNIX_S = 315964800 - get_current_gps_leap_seconds()


def _writer(tmp_path, dur=10):
    cfg = BinaryArchiveConfig(
        channel_name="testchan", frequency_hz=10e6, sample_rate=SR,
        output_dir=tmp_path / "raw_buffer", file_duration_sec=dur,
        use_tiered_storage=False, compression="none")
    w = BinaryArchiveWriter(cfg)
    w._flush_stop.set()
    w._flush_queue.put(None)
    w._flush_thread.join(timeout=5)
    w._gps_time_unix = float((int(time.time()) // dur) * dur - 5 * dur)
    w._gps_time_ns_raw = int(w._gps_time_unix * 1e9)
    w._rtp_timesnap = 0
    w._last_rtp_timesnap = 0
    w._check_disk_space = lambda *a, **k: True
    return w


def _drain(w):
    bufs = []
    while not w._flush_queue.empty():
        bufs.append(w._flush_queue.get_nowait())
    if w.current_buffer is not None:
        bufs.append(w.current_buffer)
        w.current_buffer = None
    for buf in bufs:
        assert w._flush_minute(buf)


def _gps_ns(unix_s):
    return int(round((unix_s - GPS_MINUS_UNIX_S) * 1e9))


def _ramp(n, start=1):
    return (np.arange(n) + start).astype(np.complex64)


def _two_segments(tmp_path):
    """A pair step 3 s into a 10 s chunk: 300 samples in the file, then 300
    more in <boundary>.seg2, placed after a 0.5 s step."""
    w = _writer(tmp_path)
    base = 30 * SR
    w.write_samples(_ramp(300), base)
    w.add_timing_snapshot(_gps_ns(w._rtp_to_unix_time(base + 300) + 0.5), base + 300)
    w.write_samples(_ramp(300, start=5000), base + 300)
    _drain(w)
    sides = sorted(w.archive_dir.rglob("*.json"))
    boundary = json.loads(sides[0].read_text())["minute_boundary"]
    return w, boundary


# ------------------------------------------------------------ names

def test_parse_chunk_name_reads_segment_and_extension():
    assert parse_chunk_name("1795417200.bin.zst") == (1795417200, 1, ".bin.zst")
    assert parse_chunk_name("1795417200.seg2.bin.zst") == (1795417200, 2, ".bin.zst")
    assert parse_chunk_name("1795417200.seg12.json") == (1795417200, 12, ".json")
    assert parse_chunk_name("1795417200.bin") == (1795417200, 1, ".bin")
    assert parse_chunk_name("1795417200.bin.zst.tmp") is None
    assert parse_chunk_name("notes.json") is None
    assert chunk_stem(1795417200, 1) == "1795417200"
    assert chunk_stem(1795417200, 3) == "1795417200.seg3"


# ------------------------------------------------------------ the writer says what it wrote

def test_sidecar_lists_the_runs_it_wrote(tmp_path):
    w, boundary = _two_segments(tmp_path)
    day = next(w.archive_dir.rglob(f"{boundary}.json")).parent
    s1 = json.loads((day / f"{boundary}.json").read_text())
    s2 = json.loads((day / f"{boundary}.seg2.json").read_text())
    assert s1["covered_runs"] == [[0, 300]]
    (a, b), = s2["covered_runs"]
    assert b - a == 300 and a >= 300


# ------------------------------------------------------------ joining

def _side(runs=None, **kw):
    d = {"sample_rate": SR, "start_rtp_timestamp": 1000, "samples_written": 0}
    if runs is not None:
        d["covered_runs"] = runs
    d.update(kw)
    return d


def test_join_takes_each_segment_where_it_wrote():
    a = np.zeros(10, np.complex64); a[:4] = 1
    b = np.zeros(10, np.complex64); b[6:] = 2
    x, meta = join_segments([(1, a, _side([[0, 4]], samples_written=4)),
                             (2, b, _side([[6, 10]], samples_written=10))])
    np.testing.assert_array_equal(x, [1, 1, 1, 1, 0, 0, 2, 2, 2, 2])
    assert meta["samples_written"] == 10
    assert meta["samples_covered"] == 8 and meta["hole_samples"] == 2
    assert [s["segment"] for s in meta["segments"]] == [1, 2]
    assert [s["samples_taken"] for s in meta["segments"]] == [4, 4]


def test_join_keeps_the_earlier_segment_where_both_wrote():
    """The writer keeps the earlier sample on an overlap; the reader agrees,
    and counts what it refused."""
    a = np.full(6, 1, np.complex64)
    b = np.full(8, 2, np.complex64)
    x, meta = join_segments([(1, a, _side([[0, 6]])), (2, b, _side([[4, 8]]))])
    np.testing.assert_array_equal(x, [1, 1, 1, 1, 1, 1, 2, 2])
    assert meta["segments"][1]["samples_refused"] == 2


def test_join_reports_how_far_the_segments_labels_disagree():
    """Each segment places samples by its own pair.  The reader keeps that
    placement and names the disagreement; it does not correct it."""
    a = np.ones(4, np.complex64)
    b = np.zeros(8, np.complex64); b[6:] = 2
    _, meta = join_segments([(1, a, _side([[0, 4]], start_rtp_timestamp=3094121225)),
                             (2, b, _side([[6, 8]], start_rtp_timestamp=3094097350))])
    assert meta["segments"][1]["start_rtp_offset_samples"] == 3094097350 - 3094121225


def test_join_without_runs_takes_the_nonzero_samples():
    """Segments written before the sidecar carried covered_runs (B4, 11-22 and
    11-23): unwritten positions hold zeros."""
    a = np.zeros(10, np.complex64); a[:3] = 1
    b = np.zeros(10, np.complex64); b[5:] = 2
    x, _ = join_segments([(1, a, _side()), (2, b, _side())])
    np.testing.assert_array_equal(x, [1, 1, 1, 0, 0, 2, 2, 2, 2, 2])


def test_join_skips_a_segment_at_another_sample_rate():
    a = np.ones(4, np.complex64)
    b = np.full(8, 2, np.complex64)
    x, meta = join_segments([(1, a, _side([[0, 4]])),
                             (2, b, _side([[0, 8]], sample_rate=SR * 2))])
    np.testing.assert_array_equal(x, a)
    assert meta["segments"][1]["skipped"] == "sample_rate 200 != 100"


def test_one_segment_comes_back_untouched():
    a = np.ones(4, np.complex64)
    side = _side([[0, 4]])
    x, meta = join_segments([(1, a, side)])
    assert x is a and meta is side


# ------------------------------------------------------------ BinaryArchiveReader

def test_reader_joins_the_segments_of_a_chunk(tmp_path):
    w, boundary = _two_segments(tmp_path)
    r = BinaryArchiveReader(tmp_path / "raw_buffer", "testchan")
    day = datetime.fromtimestamp(boundary, timezone.utc).strftime("%Y%m%d")
    assert r.get_available_minutes(day) == [boundary]
    x, meta = r.read_chunk(boundary)
    assert np.array_equal(x[:300], _ramp(300))
    assert np.count_nonzero(x) == 600 and x[x != 0][300] == 5000
    assert [s["segment"] for s in meta["segments"]] == [1, 2]
    assert [s["segment"] for s in r.read_metadata(boundary)["segments"]] == [1, 2]


def test_reader_reads_a_lone_segment(tmp_path):
    """The first file can be gone (evicted, or still in the other tier)."""
    w, boundary = _two_segments(tmp_path)
    for f in w.archive_dir.rglob(f"{boundary}.*"):
        if parse_chunk_name(f.name)[1] == 1:
            f.unlink()
    r = BinaryArchiveReader(tmp_path / "raw_buffer", "testchan")
    x, meta = r.read_chunk(boundary)
    assert np.count_nonzero(x) == 300 and meta["segment"] == 2


# ------------------------------------------------------------ window_drf

def test_window_counts_a_boundary_with_only_a_segment(tmp_path):
    w, boundary = _two_segments(tmp_path)
    day = next(w.archive_dir.rglob(f"{boundary}.json")).parent
    for ext in (".bin", ".json"):
        (day / f"{boundary}{ext}").unlink()
    # window_files steps in 600 s files; put the lone segment on a 600 s boundary.
    b600 = (boundary // 600) * 600
    for f in list(day.iterdir()):
        f.rename(day / f.name.replace(str(boundary), str(b600)))
    t = datetime.fromtimestamp(b600, timezone.utc)
    found, missing = window_files(tmp_path / "raw_buffer", "testchan", t, t + timedelta(seconds=1))
    assert found == [b600] and missing == []


# ------------------------------------------------------------ tiered storage

def test_tiering_moves_a_segment_with_its_own_sidecar(tmp_path):
    hot, cold = tmp_path / "hot", tmp_path / "cold"
    day = hot / "raw_buffer" / "testchan" / "20261123"
    day.mkdir(parents=True)
    old = int(time.time()) - 3 * 3600
    for stem in (str(old), f"{old}.seg2"):
        (day / f"{stem}.bin.zst").write_bytes(b"x")
        (day / f"{stem}.json").write_text(json.dumps({"stem": stem}))
    m = TieredStorageManager(TieredStorageConfig(
        hot_buffer_root=hot, cold_buffer_root=cold, auto_configure=False,
        hot_minutes=10, archive_to_cold=True, file_duration_sec=600))
    m._archive_old_minutes()
    cold_day = cold / "raw_buffer" / "testchan" / "20261123"
    assert sorted(p.name for p in day.iterdir()) == []
    for stem in (str(old), f"{old}.seg2"):
        assert (cold_day / f"{stem}.bin.zst").exists()
        assert json.loads((cold_day / f"{stem}.json").read_text()) == {"stem": stem}


def test_the_joined_chunk_reuses_the_first_segments_array():
    """A 10-minute chunk at 24 kHz is 115 MB.  Joining holds the joined chunk
    and the segment being added, not every segment plus a new array."""
    a = np.zeros(10, np.complex64); a[:4] = 1
    b = np.zeros(10, np.complex64); b[6:] = 2
    x, _ = join_segments([(1, a, _side([[0, 4]])), (2, b, _side([[6, 10]]))])
    assert np.shares_memory(x, a)
