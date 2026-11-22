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


def _pair(w, k=1, near=None):
    """radiod's next pair, continuous with the mapping in force, built in
    whole seconds so the mapping stays exact.  ``near``: put its snap at
    the whole second at or below that counter (a pair on a held line)."""
    if near is not None:
        k = ((near - w._rtp_timesnap) & 0xFFFFFFFF) // SR
    snap = (w._rtp_timesnap + k * SR) & 0xFFFFFFFF
    gps_ns = (int(w._gps_time_unix) + k - GPS_MINUS_UNIX_S) * 10**9
    assert w.add_timing_snapshot(gps_ns, snap)


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
#
# A writer on its own has no pair source, so it never holds: these place at
# once, as before round 1.  The *_held variants give the writer a pair
# source that stays silent, then release through an adopted pair.

def _silent(w):
    """A pair source with no pair to offer (radiod's status not yet read)."""
    w.set_pair_provider(lambda: None)


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


def test_backward_label_held_then_released_never_overwrites(tmp_path):
    w = _writer(tmp_path)
    _silent(w)
    base = 30 * SR
    w.write_samples(_ramp(600), base)
    y = _ramp(600, start=10_000)
    w.write_samples(y, base + 200)
    assert w.get_stats()["held_samples"] == 600
    _pair(w, near=base + 800)                          # a pair on the held line
    (_, d, side), = _drain(w)
    assert np.array_equal(d[:600], _ramp(600))
    assert np.array_equal(d[600:800], y[400:])
    assert side["overlap_samples"] == 400
    assert side["samples_covered"] == 800


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


def test_label_behind_the_chunk_start_held_then_counted(tmp_path):
    w = _writer(tmp_path)
    _silent(w)
    spc = w.samples_per_chunk
    base = 30 * SR
    w.write_samples(_ramp(spc), base)
    w.write_samples(_ramp(300), base + spc)
    w.write_samples(_ramp(200, start=50_000), base + spc - 500)
    assert w.get_stats()["held_samples"] == 200
    _pair(w, near=base + spc - 300)
    chunks = _drain(w)
    assert len(chunks) == 2
    assert np.array_equal(chunks[0][1], _ramp(spc))
    assert chunks[1][2]["dropped_before_start"] == 200


def test_rebase_before_the_new_pair_opens_no_chunk_at_a_wrong_boundary(tmp_path):
    """radiod restarted: batches on the new RTP line arrive while radiod's
    status still shows the old pair.  The old pair maps them ~200 days
    away, and it does not lie on the held line, so it releases nothing.
    The new pair, once adopted, replays the line in order."""
    w = _writer(tmp_path)
    old_pair = (_gps_ns(w._gps_time_unix), w._rtp_timesnap)
    w.set_pair_provider(lambda: old_pair)
    base = 30 * SR
    w.write_samples(_ramp(300), base)
    t_resume = w._rtp_to_unix_time(base + 300)
    boundary = int(t_resume) // 10 * 10
    new_line = 0x9000_0000
    w.write_samples(_ramp(300, start=1000), new_line, rebased=True)
    w.write_samples(_ramp(300, start=1300), new_line + 300)
    assert w.current_buffer.write_pos == 300           # nothing placed yet
    assert w.get_stats()["held_samples"] == 600
    assert w.take_step_request() is None               # the old pair is off the line
    # The new pair: the new line resumes half a second after the old one
    # stopped, so counter new_line + 600 stands at t_resume + 6.5 s.
    w.add_timing_snapshot(_gps_ns(t_resume + 6.5), new_line + 600)
    w.write_samples(_ramp(300, start=1600), new_line + 600)
    stats = w.get_stats()
    assert stats["unplaced_samples"] == 0 and stats["held_samples"] == 0
    chunks = _drain(w)
    assert {s["minute_boundary"] for _, _, s in chunks} == {boundary, boundary + 10}
    assert sum(s["samples_covered"] for _, _, s in chunks) == 1200
    replayed = np.concatenate([d[d != 0] for stem, d, _ in chunks if stem != str(boundary)])
    assert np.array_equal(replayed, _ramp(900, start=1000))


def test_a_restored_old_pair_does_not_release_a_held_new_line(tmp_path):
    """I1: an anchor round trip restores pair A, the old line's, while a
    re-based line is held.  A's snap lies nowhere near the held line, so
    the hold stays; replaying under A would land the line in a wrong chunk."""
    w = _writer(tmp_path)
    _silent(w)
    pair_a = (_gps_ns(w._gps_time_unix), w._rtp_timesnap)
    w.write_samples(_ramp(300), 30 * SR)
    w.write_samples(_ramp(300), 0x9000_0000, rebased=True)
    w.restore_timing_mapping(*pair_a)
    assert w.get_stats()["held_samples"] == 300
    assert w.get_stats()["samples_written"] == 300


def test_the_pair_in_force_releases_a_line_it_lies_on_despite_the_dedupe(tmp_path):
    w = _writer(tmp_path)
    _silent(w)
    _pair(w)                                           # pair in force: snap 100
    w.write_samples(_ramp(50), 0)
    w.write_samples(_ramp(50, start=51), 49)           # backward one: a break
    assert w.get_stats()["held_samples"] == 50
    gps_ns = (int(w._gps_time_unix) - GPS_MINUS_UNIX_S) * 10**9
    assert not w.add_timing_snapshot(gps_ns, w._rtp_timesnap)   # deduped ...
    assert w.get_stats()["held_samples"] == 0                   # ... yet released
    assert w.get_stats()["overlap_samples"] == 1


# ------------------------------------------------------------ the hold, writer side

def _feed(w, rtp, n_batches, size=50, start=1):
    for k in range(n_batches):
        w.write_samples(_ramp(size, start=start + k * size), rtp)
        rtp += size
    return rtp


def test_small_forward_jump_places_at_once_without_any_pair(tmp_path):
    """C1: a forward jump the mapping still describes is no break."""
    w = _writer(tmp_path)
    _silent(w)
    rtp = _feed(w, 30 * SR, 5)
    _feed(w, rtp + 200, 40, start=1000)
    s = w.get_stats()
    assert s["held_samples"] == 0 and s["unplaced_samples"] == 0
    assert s["samples_written"] == 45 * 50


def test_hold_past_its_bound_drops_only_the_overflow(tmp_path):
    w = _writer(tmp_path)
    _silent(w)
    bound = int(w.LABEL_HOLD_SECONDS * SR)             # 1000 samples
    rtp = _feed(w, 30 * SR, 2)
    rtp = _feed(w, rtp - 7, 24, start=1000)            # backward: 1200 held, 200 over
    s = w.get_stats()
    assert s["held_samples"] == bound
    assert s["unplaced_samples"] == 200
    _pair(w, near=rtp)
    s = w.get_stats()
    assert s["samples_written"] + s["overlap_samples"] == 100 + bound
    assert s["unplaced_samples"] == 200


def test_close_counts_samples_still_held(tmp_path):
    w = _writer(tmp_path)
    _silent(w)
    rtp = _feed(w, 30 * SR, 2)
    _feed(w, rtp - 7, 4)
    with w._lock:
        w._drop_held("test")
    assert w.get_stats()["unplaced_samples"] == 200
    assert w.get_stats()["held_samples"] == 0


# ------------------------------------------------------------ the hold, recorder side
#
# Driven as StreamRecorderV2 drives the writer in production: batches go
# through _handle_samples, radiod's pair sits in a ChannelInfo the listener
# refreshes, and nothing calls add_timing_snapshot by hand after the seed.

class _ChannelInfo:
    """The listener-refreshed ChannelInfo: one atomic (gps_time, rtp_timesnap)."""

    def __init__(self, pair):
        self.set(pair)

    def set(self, pair):
        self._pair = pair
        self.gps_time, self.rtp_timesnap = pair

    def get_anchor(self):
        return self._pair


def _recorder(tmp_path):
    from unittest.mock import MagicMock
    from hf_timestd.core.stream_recorder_v2 import StreamRecorderConfig
    cfg = StreamRecorderConfig(
        ssrc=None, frequency_hz=10e6, sample_rate=SR, preset="iq", encoding=4,
        description="testchan", output_dir=tmp_path, receiver_grid="AA00aa",
        station_config={}, archive=True, ring_seconds=0, compression="none",
        archive_dtype="complex64", file_duration_sec=10)
    r = StreamRecorderV2(config=cfg, control=MagicMock())
    w = r.archive_writer
    w._flush_stop.set()
    w._flush_queue.put(None)
    w._flush_thread.join(timeout=5)
    w._check_disk_space = lambda *a, **k: True
    r.state = StreamRecorderState.RECORDING
    # The seed, as _create_channel does it: radiod's pair at channel create.
    t0 = float((int(time.time()) // 10) * 10 - 50)
    r.channel_info = _ChannelInfo((_gps_ns(t0), 0))
    w.add_timing_snapshot(*r.channel_info.get_anchor())
    return r, w


def _on_line(w, rtp):
    """radiod's pair for counter ``rtp`` under the writer's mapping."""
    return (_gps_ns(w._rtp_to_unix_time(rtp)), rtp)


def _stream(r, rtp, n_batches, size=50, start=1, rebased_first=False, ci_follows=None):
    from ka9q import GapSource
    for k in range(n_batches):
        gaps = []
        if rebased_first and k == 0:
            gaps = [SimpleNamespace(source=GapSource.STREAM_INTERRUPTION, duration_samples=0)]
        if ci_follows is not None:
            ci_follows(rtp)
        r._handle_samples(_ramp(size, start=start + k * size), SimpleNamespace(
            batch_gaps=gaps, has_gaps=bool(gaps), delivered_rtp_start=rtp,
            total_gaps_filled=0, completeness_pct=100.0))
        rtp += size
    return rtp


def _accounted(s):
    return (s["samples_written"] + s["overlap_samples"] + s["dropped_before_start"]
            + s["unplaced_samples"] + s["held_samples"] + s["stale_drops"]
            + s["timing_drops"])


def test_recorder_small_forward_jump_keeps_archiving(tmp_path):
    """C1, the reviewer's stall probe: one forward jump, then minutes of
    stream and no pair.  Every sample gets written."""
    r, w = _recorder(tmp_path)
    rtp = _stream(r, 30 * SR, 10)
    _stream(r, rtp + 200, 600, start=10_000)           # 300 s more
    s = w.get_stats()
    assert s["samples_written"] == 610 * 50
    assert s["unplaced_samples"] == 0 and s["held_samples"] == 0


def test_recorder_long_forward_jump_on_the_same_line_releases_at_once(tmp_path):
    """An outage past every fill bound, the counter still on its line:
    radiod's current pair continues the mapping, so the first held batch
    releases."""
    r, w = _recorder(tmp_path)
    rtp = _stream(r, 30 * SR, 10)
    resume = rtp + w.FORWARD_BREAK_SAMPLES + 500
    r.channel_info.set(_on_line(w, resume + 25))
    _stream(r, resume, 20, start=10_000, rebased_first=True)
    s = w.get_stats()
    assert s["samples_written"] == 30 * 50
    assert s["unplaced_samples"] == 0 and s["held_samples"] == 0


def test_recorder_backward_resync_loses_nothing_with_the_host_clock_200_s_off(tmp_path):
    r, w = _recorder(tmp_path)
    w._host_time = lambda: time.time() + 200.0
    rtp = _stream(r, 30 * SR, 5)
    r.channel_info.set(_on_line(w, rtp + 100))
    rtp = _stream(r, rtp - 1, 10, start=1000, rebased_first=True)
    _stream(r, rtp, 30, start=5000)
    s = w.get_stats()
    assert s["unplaced_samples"] == 0 and s["held_samples"] == 0
    assert s["overlap_samples"] == 1
    assert s["samples_written"] + s["overlap_samples"] == 45 * 50


def test_recorder_radiod_restart_adopted_promptly_by_the_poll(tmp_path):
    """radiod restarts: the stream resumes on a new counter, and for a few
    batches ChannelInfo still shows the old pair.  Then radiod's new pairs
    arrive, about one per 0.5 s.  The hold reads them, the recorder's
    revalidation adopts the step on the second agreeing read (no GPSDO
    here), and the held line replays under it.  Nothing is lost."""
    r, w = _recorder(tmp_path)
    rtp = _stream(r, 30 * SR, 10)                      # 5 s on the old line
    t_stop = w._rtp_to_unix_time(rtp)
    new0 = 0x9000_0000
    t_new0 = t_stop + 2.0                              # radiod back 2 s later

    def radiod_status(line_rtp):
        # After 3 batches (1.5 s), radiod's new pair shows, refreshed on
        # every other batch at the newest sample.
        k = (line_rtp - new0) // 50
        if k >= 3 and k % 2 == 1:
            r.channel_info.set((_gps_ns(t_new0 + (line_rtp - new0) / SR), line_rtp))
    _stream(r, new0, 40, start=100_000, rebased_first=True, ci_follows=radiod_status)
    s = w.get_stats()
    assert s["held_samples"] == 0 and s["unplaced_samples"] == 0
    assert w._rtp_timesnap >= new0                     # the step was adopted
    assert _accounted(s) == 50 * 50
    assert s["samples_written"] >= 50 * 50 - 2         # at most a seam sample or two
    chunks = _drain(w)
    lo, hi = int(t_stop) - 20, int(t_new0) + 30
    assert all(lo <= side["minute_boundary"] <= hi for _, _, side in chunks)
    # The new line sits in order, right where the new pair puts it.
    new = np.concatenate([d[d.real >= 100_000] for _, d, _ in chunks])
    assert np.array_equal(new, _ramp(len(new), start=100_000 + 2000 - len(new)))


def test_abandoned_buffers_are_counted(tmp_path):
    w = _writer(tmp_path)
    spc = w.samples_per_chunk
    rtp = 30 * SR
    for k in range(w._flush_queue.maxsize + 1):        # one more than the queue holds
        w.write_samples(_ramp(spc), rtp + k * spc)
    s = w.get_stats()
    assert s["abandoned_chunks"] == 1
    assert s["abandoned_samples"] == spc


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


# ------------------------------------------------------------ the arbiter under a hold
#
# Round 3 re-review, I1-r3: the hold's revalidation and the 60 s tick can read
# one ChannelInfo pair twice, and consecutive status pairs 0.5 s apart agree
# almost by construction.  Confirmation needs a second read at least five
# seconds of counter after the first.

def _restart(r, w, status, n=60):
    rtp = _stream(r, 30 * SR, 10)
    t_stop = w._rtp_to_unix_time(rtp)
    new0 = 0x9000_0000
    t_new0 = t_stop + 2.0
    _stream(r, new0, n, start=100_000, rebased_first=True,
            ci_follows=lambda line: status(line, new0, t_new0))
    return new0, t_new0


def test_one_status_pair_read_twice_does_not_adopt(tmp_path):
    """P4: radiod's first pair after a restart shows once.  The hold's
    revalidation holds it; the 60 s tick re-reads the same pair before the
    listener refreshes.  That is one read, not two."""
    r, w = _recorder(tmp_path)
    rtp = _stream(r, 30 * SR, 10)
    t_new0 = w._rtp_to_unix_time(rtp) + 2.0
    new0 = 0x9000_0000
    r.channel_info.set((_gps_ns(t_new0), new0))
    rtp_new = _stream(r, new0, 2, start=100_000, rebased_first=True)
    before = w._rtp_timesnap
    r.revalidate_radiod_pair()                         # the tick, same ChannelInfo
    assert w._rtp_timesnap == before                   # not adopted
    # A second pair, five seconds of counter later, confirms.
    later = new0 + 5 * SR
    r.channel_info.set((_gps_ns(t_new0 + 5.0), later))
    _stream(r, rtp_new, 8, start=100_100)
    assert w._rtp_timesnap == later
    s = w.get_stats()
    assert s["held_samples"] == 0 and s["unplaced_samples"] == 0
    assert _accounted(s) == 20 * 50


def test_a_biased_early_pair_does_not_stick(tmp_path):
    """P3: radiod's first pairs after the restart sit 0.6 s off, later ones
    true.  The adopted mapping must be the true one, and every sample is
    counted (the longer wait may overflow the 10 s hold)."""
    r, w = _recorder(tmp_path)

    def status(line, new0, t_new0):
        k = (line - new0) // 50
        if k >= 3 and k % 2 == 1:
            bias = 0.6 if k < 12 else 0.0
            r.channel_info.set((_gps_ns(t_new0 + bias + (line - new0) / SR), line))
    new0, t_new0 = _restart(r, w, status)
    off = w._gps_time_unix - (t_new0 + (w._rtp_timesnap - new0) / SR)
    assert abs(off) < 0.01, f"adopted mapping {off:+.3f} s off"
    for _ in range(3):
        r.revalidate_radiod_pair()
    off = w._gps_time_unix - (t_new0 + (w._rtp_timesnap - new0) / SR)
    assert abs(off) < 0.01
    s = w.get_stats()
    assert s["held_samples"] == 0
    assert _accounted(s) == 70 * 50


# ------------------------------------------------------------ the watchdog off the write path

def test_watchdog_capture_does_not_block_the_write_path(tmp_path, monkeypatch):
    """m1-r3: a restart's mapping jump makes the watchdog run gpspipe and
    chronyc (up to 15 s).  That must happen off the writer lock and off the
    receive callback."""
    from hf_timestd.core import radiod_timing_watchdog as wd
    gate = threading.Event()
    started = threading.Event()

    def slow_capture(self, *a, **k):
        started.set()
        gate.wait(10)
    monkeypatch.setattr(wd.RadiodTimingWatchdog, "_capture", slow_capture)
    monkeypatch.setattr(wd, "_WATCHDOG", None)
    w = _writer(tmp_path)
    w.write_samples(_ramp(100), 30 * SR)
    try:
        t0 = time.monotonic()
        # A 2562 s jump: radiod restarted, well past the watchdog's 2 s floor.
        w.add_timing_snapshot(_gps_ns(w._gps_time_unix + 2562.0), 12_000)
        took = time.monotonic() - t0
        assert started.wait(5)                         # the capture did run ...
        assert took < 1.0, f"the pair adoption waited {took:.1f}s on the capture"
        t0 = time.monotonic()
        w.write_samples(_ramp(100), 12_000)            # ... and writing goes on
        assert time.monotonic() - t0 < 1.0
    finally:
        gate.set()

