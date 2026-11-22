"""The archive writer under label faults: splits, wraps, backward labels, re-bases.

Review round 1 of the first-sample label fix (2026-11-22):

- I1: a batch split across a chunk boundary must leave each chunk the gap
  fill that lies inside it.
- I2: the restored-pair age handed to the Offset Judge counts from the
  newest sample, label + len(samples), not from the batch's first sample.
- I3: ka9q-python 3.28.0 re-bases the label after a backward RTP jump or a
  sequence re-sync (STREAM_INTERRUPTION).  The writer never overwrites a
  sample it already wrote, never drops a batch without counting it, and
  never replaces a chunk file already on disk.
"""
import json
import threading
import time
from types import SimpleNamespace

import numpy as np

from hf_timestd.core.binary_archive_writer import BinaryArchiveConfig, BinaryArchiveWriter
from hf_timestd.core.leap_second import get_current_gps_leap_seconds
from hf_timestd.core.stream_recorder_v2 import StreamRecorderV2, StreamRecorderState

SR = 100
GPS_MINUS_UNIX_S = 315964800 - get_current_gps_leap_seconds()


def _writer(tmp_path, dur=10, timesnap=0):
    cfg = BinaryArchiveConfig(
        channel_name="testchan", frequency_hz=10e6, sample_rate=SR,
        output_dir=tmp_path / "raw_buffer", file_duration_sec=dur,
        use_tiered_storage=False, compression="none")
    w = BinaryArchiveWriter(cfg)
    w._flush_stop.set()
    w._flush_queue.put(None)
    w._flush_thread.join(timeout=5)
    # A recent, chunk-aligned epoch keeps the split detector quiet.
    w._gps_time_unix = float((int(time.time()) // dur) * dur - 5 * dur)
    w._gps_time_ns_raw = int(w._gps_time_unix * 1e9)
    w._rtp_timesnap = timesnap
    w._last_rtp_timesnap = timesnap
    w._check_disk_space = lambda *a, **k: True
    return w


def _gps_ns(unix_s):
    """radiod's GPS_TIME (ns since the GPS epoch) for a Unix time."""
    return int(round((unix_s - GPS_MINUS_UNIX_S) * 1e9))


def _drain(w):
    """Flush every queued buffer plus the open one, in order.

    Returns a list of (stem, data, sidecar) for every chunk file on disk."""
    bufs = []
    while not w._flush_queue.empty():
        bufs.append(w._flush_queue.get_nowait())
    if w.current_buffer is not None:
        bufs.append(w.current_buffer)
        w.current_buffer = None
    for buf in bufs:
        assert w._flush_minute(buf)
    out = []
    for side_path in sorted(w.archive_dir.rglob("*.json")):
        stem = side_path.name[:-len(".json")]
        side = json.loads(side_path.read_text())
        data = np.fromfile(side_path.with_name(stem + ".bin"), dtype=np.complex64)
        out.append((stem, data, side))
    return out


def _ramp(n, start=1):
    return (np.arange(n) + start).astype(np.complex64)


# ------------------------------------------------------------ I1: gaps

def test_split_batch_leaves_its_tail_fill_to_the_next_chunk(tmp_path):
    w = _writer(tmp_path, dur=30)                      # 3000-sample chunks
    spc = w.samples_per_chunk
    base = 300 * SR                                    # a chunk boundary
    w.write_samples(_ramp(spc - 500), base)
    # 2000 samples: 500 close chunk N, then 1500 in chunk N+1 of which
    # 1200 are a zero fill (too long for the writer to interpolate).
    x = _ramp(2000, start=spc - 500 + 1)
    x[600:1800] = 0
    w.write_samples(x, base + spc - 500, gap_samples=1200)
    (s0, d0, side0), (s1, d1, side1) = _drain(w)
    assert side0["gap_samples"] == 0 and side0["gap_count"] == 0
    assert side1["gap_samples"] == 1200 and side1["gap_count"] == 1
    assert np.count_nonzero(d1 == 0) == 1200


def test_split_batch_with_fill_on_both_sides_divides_it(tmp_path):
    w = _writer(tmp_path, dur=30)
    spc = w.samples_per_chunk
    base = 300 * SR
    w.write_samples(_ramp(spc - 2000), base)
    x = _ramp(4000, start=spc - 2000 + 1)
    x[100:1300] = 0                                    # in chunk N
    x[2500:3700] = 0                                   # in chunk N+1
    w.write_samples(x, base + spc - 2000, gap_samples=2400)
    (_, _, side0), (_, _, side1) = _drain(w)
    assert (side0["gap_samples"], side0["gap_count"]) == (1200, 1)
    assert (side1["gap_samples"], side1["gap_count"]) == (1200, 1)


# ------------------------------------------------------------ I2: pair age

def test_restored_pair_age_counts_from_the_newest_sample(tmp_path):
    w = _writer(tmp_path)
    label = 30 * SR + 200
    w.write_samples(_ramp(480), label)
    snap = label - 100
    # The counter stood at the snap 100 samples before the batch's first
    # sample, so 100 + 480 samples before its newest.
    assert w._restored_pair_age_s(snap) == (100 + 480) / SR


# ------------------------------------------------------------ wrap and seam

def test_straddle_across_the_rtp_wrap(tmp_path):
    snap = 2**32 - 1234
    w = _writer(tmp_path, timesnap=snap)
    spc = w.samples_per_chunk
    base = (snap + 30 * SR) & 0xFFFFFFFF               # a chunk boundary
    total = 3 * 300 + 2 * spc
    x = _ramp(total)
    k = 0
    while k < total:
        w.write_samples(x[k:k + 300], (base + k) & 0xFFFFFFFF)
        k += 300
    chunks = _drain(w)
    assert np.array_equal(np.concatenate([d for _, d, _ in chunks]), x)
    starts = [s["start_rtp_timestamp"] for _, _, s in chunks]
    assert starts[1] == (starts[0] + spc) & 0xFFFFFFFF
    assert starts[2] == (starts[1] + spc) & 0xFFFFFFFF


def test_mapping_nudge_at_the_seam_conserves_every_sample(tmp_path):
    """A pair below the step threshold, adopted mid-chunk, moves the next
    chunk's start_rtp by a few samples.  The seam may lose or leave a few
    samples, but each one gets counted."""
    w = _writer(tmp_path)
    spc = w.samples_per_chunk
    base = 30 * SR
    w.write_samples(_ramp(spc - 100), base)
    # Same GPS_TIME, timesnap 3 samples later: the mapping moves the next
    # boundary 3 samples later on the counter, so the remainder's first 3
    # samples fall before the new chunk's start.
    w.add_timing_snapshot(_gps_ns(w._gps_time_unix), w._rtp_timesnap + 3)
    w.write_samples(_ramp(300, start=spc - 99), base + spc - 100)
    chunks = _drain(w)
    kept = sum(s["samples_covered"] for _, _, s in chunks)
    lost = sum(s["dropped_before_start"] + s["overlap_samples"] for _, _, s in chunks)
    assert kept + lost == spc - 100 + 300
    assert lost == 3


# ------------------------------------------------------------ I3: backward labels

def test_backward_label_inside_the_chunk_never_overwrites(tmp_path):
    w = _writer(tmp_path)
    base = 30 * SR
    w.write_samples(_ramp(600), base)
    # The label steps back 400: the first 400 positions already hold data.
    y = _ramp(600, start=10_000)
    w.write_samples(y, base + 200)
    (_, d, side), = _drain(w)
    assert np.array_equal(d[:600], _ramp(600))         # untouched
    assert np.array_equal(d[600:800], y[400:])         # the new part lands
    assert side["overlap_samples"] == 400
    assert side["samples_covered"] == 800
    assert w.get_stats()["overlap_samples"] == 400


def test_label_behind_the_chunk_start_is_counted_not_lost_silently(tmp_path):
    w = _writer(tmp_path)
    spc = w.samples_per_chunk
    base = 30 * SR
    w.write_samples(_ramp(spc), base)                  # chunk N, complete
    w.write_samples(_ramp(300), base + spc)            # chunk N+1 opens
    w.write_samples(_ramp(200, start=50_000), base + spc - 500)   # back into N
    chunks = _drain(w)
    assert len(chunks) == 2
    _, d0, side0 = chunks[0]
    _, d1, side1 = chunks[1]
    assert np.array_equal(d0, _ramp(spc))              # N untouched
    assert side1["dropped_before_start"] == 200
    assert w.get_stats()["dropped_before_start"] == 200


def test_rebase_before_the_new_pair_opens_no_chunk_at_a_wrong_boundary(tmp_path):
    """radiod restarted: batches on the new RTP line arrive while the writer
    still holds the old pair.  The old pair maps them hours away.  The writer
    holds them out (counted), and places the line once the new pair lands."""
    w = _writer(tmp_path)
    base = 30 * SR
    w.write_samples(_ramp(300), base)
    t_resume = w._rtp_to_unix_time(base + 300)
    new_line = 0x9000_0000                             # the old pair maps it ~200 days back
    w.write_samples(_ramp(300, start=1000), new_line)
    w.write_samples(_ramp(300, start=1300), new_line + 300)
    assert w.get_stats()["unplaced_samples"] == 600
    # The new pair: counter new_line + 600 stands at t_resume + 2 s, in
    # the same ten seconds as the first batch.
    w.add_timing_snapshot(_gps_ns(t_resume + 2.0), new_line + 600)
    w.write_samples(_ramp(300, start=1600), new_line + 600)
    chunks = _drain(w)
    boundaries = {s["minute_boundary"] for _, _, s in chunks}
    assert boundaries == {int(t_resume) // 10 * 10}
    assert sum(s["samples_covered"] for _, _, s in chunks) == 600


def test_reopened_boundary_never_replaces_the_earlier_file(tmp_path):
    """A pair step mid-chunk flushes the open chunk; the next batch lands in
    the same ten seconds and opens a chunk at the same boundary.  It used to
    replace the first file on disk."""
    w = _writer(tmp_path)
    base = 30 * SR
    w.write_samples(_ramp(300), base)
    t_next = w._rtp_to_unix_time(base + 300)
    # A 0.5 s step (over the 0.25 s threshold): same counter, new time.
    w.add_timing_snapshot(_gps_ns(t_next + 0.5), base + 300)
    w.write_samples(_ramp(300, start=5000), base + 300)
    chunks = _drain(w)
    assert len(chunks) == 2
    (s0, d0, side0), (s1, d1, side1) = chunks
    boundary = side0["minute_boundary"]
    assert side1["minute_boundary"] == boundary
    assert s0 == str(boundary) and s1 == f"{boundary}.seg2"
    assert side1["segment"] == 2
    assert np.array_equal(d0, _ramp(300))
    assert np.count_nonzero(d1) == 300 and d1[d1 != 0][0] == 5000


def test_reopened_boundary_keeps_its_own_scratch_file(tmp_path):
    w = _writer(tmp_path)
    base = 30 * SR
    w.write_samples(_ramp(300), base)
    first = w.current_buffer
    w.add_timing_snapshot(_gps_ns(w._rtp_to_unix_time(base + 300) + 0.5), base + 300)
    w.write_samples(_ramp(300), base + 300)
    second = w.current_buffer
    assert first.scratch_path != second.scratch_path
    assert first.scratch_path.exists() and second.scratch_path.exists()


# ------------------------------------------------------------ fallback marker

def test_fallback_label_says_so_in_the_sidecar(tmp_path):
    w = _writer(tmp_path)
    r = StreamRecorderV2.__new__(StreamRecorderV2)
    r.state = StreamRecorderState.RECORDING
    r._lock = threading.Lock()
    r._tap_lock = threading.Lock()
    r._tap_callbacks = []
    r.batches_received = r.samples_received = r.samples_written = 0
    r.archive_writer = w
    r.ring_buffer = None
    r.config = SimpleNamespace(description="testchan")
    q = SimpleNamespace(batch_gaps=[], has_gaps=False, last_rtp_timestamp=30 * SR)
    r._handle_samples(_ramp(100), q)
    (_, _, side), = _drain(w)
    assert side["batch_label"] == "last_packet"
