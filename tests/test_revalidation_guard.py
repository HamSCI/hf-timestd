"""Revalidation must read radiod's pair atomically, never from torn legacy fields."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from ka9q.discovery import ChannelInfo
from hf_timestd.core.stream_recorder_v2 import StreamRecorderConfig, StreamRecorderV2

SAMPLE_RATE = 24_000
REF_GPS_NS = 2_000_000_000
REF_RTP = 48_000


def _torn_ci():
    ci = ChannelInfo.__new__(ChannelInfo)
    ci._anchor_pair = (REF_GPS_NS, REF_RTP)             # consistent pair
    ci.gps_time, ci.rtp_timesnap = 3_000_000_000, REF_RTP   # torn: +1 s
    return ci


def test_revalidation_ignores_torn_legacy_fields():
    cfg = StreamRecorderConfig(
        ssrc=None, frequency_hz=7_850_000, sample_rate=SAMPLE_RATE, preset='iq',
        encoding=4, agc_enable=0, gain=0.0, description='TEST_CHU_7850',
        output_dir=Path('/tmp'), receiver_grid='AA00aa', station_config={},
        archive=False, ring_seconds=0,
    )
    sr = StreamRecorderV2(config=cfg, control=MagicMock())

    def evaluate_pair(gps, rtp):
        # fresh_utc - implied_utc; the leap offset cancels against the
        # mapping adopted from (REF_GPS_NS, REF_RTP).
        return gps / 1e9 - (REF_GPS_NS / 1e9 + (rtp - REF_RTP) / SAMPLE_RATE)

    writer = MagicMock()
    writer.evaluate_pair.side_effect = evaluate_pair
    sr.archive_writer = writer
    sr.channel_info = _torn_ci()

    sr.revalidate_radiod_pair()

    writer.evaluate_pair.assert_called_once_with(REF_GPS_NS, REF_RTP)
    writer.add_timing_snapshot.assert_not_called()
