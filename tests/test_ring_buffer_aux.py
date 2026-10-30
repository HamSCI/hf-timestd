"""Ring buffer aux fields: radiod's raw pair and the channel's counter map."""
from __future__ import annotations

import uuid

import numpy as np
import pytest

pytest.importorskip("sysv_ipc")

from hamsci_dsp.counter_map import CounterMap
from hf_timestd.core.ring_buffer import HOT_EPOCH_COUNTER, RingBuffer
from hf_timestd.core.ring_buffer_reader import RingBufferReader

GPS_EPOCH_UNIX = 315964800.0
UTC0 = 1_800_000_000.0


@pytest.fixture
def ring():
    buf = RingBuffer.create(f"TAUX_{uuid.uuid4().hex[:12]}", sample_rate=24000,
                            ring_seconds=4)
    reader = RingBufferReader.attach(buf.channel_name)
    yield buf, reader
    reader.close()
    try:
        buf.destroy()
    except Exception:
        pass


def _fill(buf, reader, judged_gps_ns):
    buf.update_anchor(gps_time_ns=judged_gps_ns, rtp_timesnap=10_000)
    rtp = 10_000
    for s in range(0, 72000, 6000):
        x = np.arange(s, s + 6000, dtype=np.float32)
        buf.write_samples((x + 0j).astype(np.complex64), batch_first_rtp=rtp)
        rtp += 6000


def _judged(reader):
    return int((UTC0 - GPS_EPOCH_UNIX + reader._leap) * 1e9)


def _map():
    return CounterMap(rtp_ref=4_294_966_816, input_ref=-1_296_000 * 3,
                      ilen=1_296_000, olen=480, input_samprate=64_800_000,
                      input_epoch_gps_s=1_300_000_000.123, n_agree=4)


def test_raw_pair_reaches_the_reader_metadata(ring):
    buf, reader = ring
    judged = _judged(reader)
    _fill(buf, reader, judged)
    buf.update_raw_pair(judged - 777_000_000, 123_456)
    _, meta = reader.extract_interval(UTC0 + 1.0, 1.0)
    assert meta["raw_gps_time_ns"] == judged - 777_000_000
    assert meta["raw_rtp_timesnap"] == 123_456
    assert meta["gps_time_ns"] == judged


def test_counter_map_round_trips_through_the_ring(ring):
    buf, reader = ring
    _fill(buf, reader, _judged(reader))
    m = _map()
    buf.update_counter_map(m)
    _, meta = reader.extract_interval(UTC0 + 1.0, 1.0)
    assert meta["counter_map"] == m
    _, meta = reader.extract_samples(100)
    assert meta["counter_map"] == m
    buf.update_counter_map(None)
    _, meta = reader.extract_samples(100)
    assert meta["counter_map"] is None


def test_old_producer_reads_as_absent(ring):
    buf, reader = ring
    _fill(buf, reader, _judged(reader))
    _, meta = reader.extract_interval(UTC0 + 1.0, 1.0)
    assert meta["raw_gps_time_ns"] is None
    assert meta["raw_rtp_timesnap"] is None
    assert meta["counter_map"] is None


def test_aux_update_does_not_bump_the_anchor_epoch(ring):
    buf, reader = ring
    _fill(buf, reader, _judged(reader))
    before = int(buf._hot[HOT_EPOCH_COUNTER])
    buf.update_raw_pair(5, 6)
    buf.update_counter_map(_map())
    assert int(buf._hot[HOT_EPOCH_COUNTER]) == before
    assert reader.get_anchor()["epoch"] == before
