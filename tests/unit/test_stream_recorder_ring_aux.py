"""The recorder writes radiod's raw pair and the channel's counter map to the ring."""
import types

from hf_timestd.core.stream_recorder_v2 import StreamRecorderV2


class FakeRing:
    def __init__(self):
        self.anchors = []

    def update_anchor(self, gps_time_ns, rtp_timesnap):
        self.anchors.append((int(gps_time_ns), int(rtp_timesnap)))


def make_recorder(*, ring=None):
    rec = StreamRecorderV2.__new__(StreamRecorderV2)
    rec.config = types.SimpleNamespace(description="TEST", sample_rate=24000, ssrc=1)
    rec.channel_info = None
    rec.archive_writer = None
    rec._offset_judge = None
    rec._judge_source_key = ("hf-status.local", 1)
    rec.ring_buffer = ring
    rec._ring_anchor_state = None
    rec._ring_label_anchor = None
    rec._label_anchor_provider = None
    rec._control = None
    return rec


def test_update_ring_anchor_writes_the_raw_pair_beside_the_judged_one():
    class RawRing(FakeRing):
        def __init__(self):
            super().__init__()
            self.raw = []

        def update_raw_pair(self, gps_time_ns, rtp_timesnap):
            self.raw.append((int(gps_time_ns), int(rtp_timesnap)))

    ring = RawRing()
    rec = make_recorder(ring=ring)
    rec._update_ring_anchor(5_000_000_000, 1234)
    assert ring.raw == [(5_000_000_000, 1234)]
    assert ring.anchors  # the judged path still ran


def test_publish_counter_map_reaches_the_ring_and_never_raises():
    got = []

    class R(FakeRing):
        def update_counter_map(self, cmap):
            got.append(cmap)

    rec = make_recorder(ring=R())
    rec.publish_counter_map("MAP")
    assert got == ["MAP"]

    class Boom(FakeRing):
        def update_counter_map(self, cmap):
            raise OSError("disk full")

    rec.ring_buffer = Boom()
    rec.publish_counter_map("MAP")      # no raise
    rec.ring_buffer = None
    rec.publish_counter_map("MAP")
