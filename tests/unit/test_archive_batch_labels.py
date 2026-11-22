"""The raw archive places each batch by its FIRST delivered sample.

B4, 2026-11-22: StreamRecorderV2 labelled each archive batch with
``quality.last_rtp_timestamp``.  ka9q-python stamps that from each packet's
header before the resequencer runs, so it names the batch's last received
packet, not its first sample.  The writer treats the label as sample 0, so
every 24 kHz F32 batch landed 1440 samples (60 ms) late.  The resequencer's
``delivered_rtp_start`` names the first delivered sample; resolve_batch_rtp
prefers it, and the archive now uses it too.

A second writer fault rode along: a batch that crossed a chunk boundary
lost its tail, and the next chunk opened with a zero run.
"""
import json
import threading
import time
from types import SimpleNamespace

import numpy as np

from hf_timestd.core.binary_archive_writer import BinaryArchiveConfig, BinaryArchiveWriter
from hf_timestd.core.stream_recorder_v2 import StreamRecorderV2, StreamRecorderState

SR = 100
DUR = 10              # 1000 samples per chunk: small and fast


def _writer(tmp_path):
    cfg = BinaryArchiveConfig(
        channel_name="testchan", frequency_hz=10e6, sample_rate=SR,
        output_dir=tmp_path / "raw_buffer", file_duration_sec=DUR,
        use_tiered_storage=False, compression="none")
    w = BinaryArchiveWriter(cfg)
    # Flush by hand, not via the worker: stop it, and wake it with the
    # close() sentinel so it cannot take the first buffer we queue.
    w._flush_stop.set()
    w._flush_queue.put(None)
    w._flush_thread.join(timeout=5)
    # A recent, chunk-aligned epoch keeps the split detector quiet.
    w._gps_time_unix = float((int(time.time()) // DUR) * DUR - 5 * DUR)
    w._gps_time_ns_raw = int(w._gps_time_unix * 1e9)
    w._rtp_timesnap = 0
    w._check_disk_space = lambda *a, **k: True
    return w


def _pair(w, k):
    """A pair continuous with the mapping in force, in whole seconds."""
    from hf_timestd.core.leap_second import get_current_gps_leap_seconds
    snap = (w._rtp_timesnap + k * SR) & 0xFFFFFFFF
    gps_ns = (int(w._gps_time_unix) + k - 315964800 + get_current_gps_leap_seconds()) * 10**9
    assert w.add_timing_snapshot(gps_ns, snap)


def _drain(w):
    """Flush every queued buffer plus the open one; return {boundary: (data, side)}."""
    out = {}
    bufs = []
    while not w._flush_queue.empty():
        bufs.append(w._flush_queue.get_nowait())
    if w.current_buffer is not None:
        bufs.append(w.current_buffer)
        w.current_buffer = None
    for buf in bufs:
        assert w._flush_minute(buf)
        d = w._get_minute_dir(buf.minute_boundary)
        side = json.loads((d / f"{buf.minute_boundary}.json").read_text())
        data = np.fromfile(d / f"{buf.minute_boundary}.bin", dtype=np.complex64)
        out[buf.minute_boundary] = (data, side)
    return out


def _recorder(writer):
    r = StreamRecorderV2.__new__(StreamRecorderV2)
    r.state = StreamRecorderState.RECORDING
    r._lock = threading.Lock()
    r._tap_lock = threading.Lock()
    r._tap_callbacks = []
    r.batches_received = 0
    r.samples_received = 0
    r.samples_written = 0
    r.archive_writer = writer
    r.ring_buffer = None
    r.config = SimpleNamespace(description="testchan")
    return r


def _quality(**kw):
    base = dict(batch_gaps=[], has_gaps=False, total_gaps_filled=0,
                completeness_pct=100.0)
    base.update(kw)
    return SimpleNamespace(**base)


# ------------------------------------------------------------ batch label

def test_archive_places_sample_zero_at_delivered_rtp_start(tmp_path):
    w = _writer(tmp_path)
    r = _recorder(w)
    first = 30 * SR + 200            # well inside a chunk
    x = (np.arange(300) + 1).astype(np.complex64)
    # The last packet's header sits 120 samples past the first sample,
    # as it does for ka9q-python's pre-resequencer stamp.
    r._handle_samples(x, _quality(delivered_rtp_start=first,
                                  last_rtp_timestamp=first + 120))
    chunks = _drain(w)
    assert len(chunks) == 1
    (boundary, (data, side)), = chunks.items()
    pos = first - side["start_rtp_timestamp"]
    assert data[pos] == x[0]
    assert np.all(data[:pos] == 0)
    assert np.array_equal(data[pos:pos + len(x)], x)


def test_archive_falls_back_to_last_header_without_delivered_rtp_start(tmp_path):
    w = _writer(tmp_path)
    r = _recorder(w)
    label = 30 * SR + 200
    x = (np.arange(50) + 1).astype(np.complex64)
    r._handle_samples(x, _quality(last_rtp_timestamp=label))      # old ka9q-python
    r._handle_samples(x, _quality(last_rtp_timestamp=label + 50))
    (data, side), = _drain(w).values()
    pos = label - side["start_rtp_timestamp"]
    assert np.array_equal(data[pos:pos + 50], x)


def test_core_recorder_keeps_resolve_batch_rtp():
    from hf_timestd.core import batch_rtp, core_recorder_v2
    assert core_recorder_v2.resolve_batch_rtp is batch_rtp.resolve_batch_rtp


# ------------------------------------------------------------ straddle

def test_batch_straddling_a_chunk_boundary_splits_across_both(tmp_path):
    w = _writer(tmp_path)
    spc = w.samples_per_chunk
    base = 30 * SR                   # a chunk boundary
    n_batches = (2 * spc) // 300 + 1
    total = n_batches * 300
    x = (np.arange(total) + 1).astype(np.complex64)
    for k in range(n_batches):
        w.write_samples(x[k * 300:(k + 1) * 300], base + k * 300)
    chunks = _drain(w)
    bounds = sorted(chunks)
    assert len(bounds) == 3
    d0, s0 = chunks[bounds[0]]
    d1, s1 = chunks[bounds[1]]
    d2, _ = chunks[bounds[2]]
    assert len(d0) == spc and len(d1) == spc
    # No zero run at the start of the next chunk: its first sample is the
    # one right after the previous chunk's last.
    assert d1[0] == d0[-1] + 1
    assert d2[0] == d1[-1] + 1
    assert np.all(d1 != 0)
    # Every sample written exactly once, in order.
    assert np.array_equal(np.concatenate([d0, d1, d2]), x)
    assert s1["start_rtp_timestamp"] == s0["start_rtp_timestamp"] + spc


# ------------------------------------------------------------ sidecar

def test_new_sidecars_carry_the_first_sample_label(tmp_path):
    w = _writer(tmp_path)
    w.write_samples(np.ones(100, np.complex64), 30 * SR)
    (_, side), = _drain(w).values()
    assert side["batch_label"] == "first_sample"


def test_sidecar_counts_holes_honestly(tmp_path):
    w = _writer(tmp_path)
    base = 30 * SR
    w.write_samples(np.ones(100, np.complex64), base + 50)     # 50-sample lead hole
    w.write_samples(np.ones(100, np.complex64), base + 150)    # contiguous
    # A label jump holds the line until radiod's next pair; send one after
    # each jump (continuous with the mapping, so placement stays exact).
    w.write_samples(np.ones(100, np.complex64), base + 400)    # 150-sample hole
    _pair(w, 1)
    w.write_samples(np.ones(100, np.complex64), base + 450)    # overlaps 50
    _pair(w, 1)
    (_, side), = _drain(w).values()
    assert side["samples_written"] == 550                      # highest position, as before
    assert side["samples_covered"] == 350
    assert side["hole_samples"] == 200
    assert side["hole_count"] == 2
