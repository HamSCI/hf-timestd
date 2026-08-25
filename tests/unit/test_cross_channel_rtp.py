"""Relating two radiod channels' RTP counters (hf-timestd#42 precondition).

Numbers are the live AC0G-B4 sidecars from 2026-08-25, minute_boundary
1787670300 -- six 24 kHz metrology channels and the 96 kHz T6 channel.
"""
from __future__ import annotations

import pytest

from hf_timestd.core.cross_channel_rtp import (
    ChannelEpoch,
    PairObservation,
    epoch_offset_s,
    rtp_in_other_channel,
    same_counter_space,
)

# (name, fs, gps_time_ns, rtp_timesnap) -- straight off B4's sidecars
B4 = [
    ("SHARED_2500",  24000, 1471705614542577096, 1709896032),
    ("SHARED_5000",  24000, 1471705614623762332, 1709897952),
    ("SHARED_10000", 24000, 1471705614684177812, 1709899392),
    ("SHARED_15000", 24000, 1471705614743942306, 1709900832),
    ("WWV_20000",    24000, 1471705614804005195, 1709902272),
    ("WWV_25000",    24000, 1471705614864514581, 1709903712),
]


def _obs(row):
    _, fs, gps, snap = row
    return PairObservation(gps_time_ns=gps, rtp_timesnap=snap,
                           sample_rate_hz=fs)


class TestSiblingChannelsShareOneSpace:
    def test_the_six_metrology_channels_agree_to_2ms(self):
        """If origins were randomised per channel these would differ by
        hours.  They differ by the pair non-atomicity instead."""
        epochs = [epoch_offset_s(_obs(r)) for r in B4]
        assert (max(epochs) - min(epochs)) == pytest.approx(0.001937, abs=1e-5)

    def test_pairwise_same_counter_space(self):
        for r in B4[1:]:
            assert same_counter_space(_obs(B4[0]), _obs(r))

    def test_a_foreign_origin_is_detected(self):
        """A channel carrying its own origin must NOT read as a sibling."""
        far = PairObservation(
            gps_time_ns=B4[0][2], rtp_timesnap=B4[0][3] + 24000 * 3772,
            sample_rate_hz=24000)
        assert not same_counter_space(_obs(B4[0]), far)


class TestLeastLateEpoch:
    """The pair is read at status emission -- GPS_TIME live, RTP_TIMESNAP
    cached -- so its error is one-sided lateness.  Averaging converges on
    the wrong number; the minimum is the truest observation."""

    def test_minimum_wins_not_the_mean(self):
        truth = epoch_offset_s(_obs(B4[0]))
        e = ChannelEpoch()
        e.observe(_obs(B4[0]))
        for late_ms in (3.0, 120.0, 816.0, 12.0):
            e.observe(PairObservation(
                gps_time_ns=B4[0][2] + int(late_ms * 1e6),
                rtp_timesnap=B4[0][3], sample_rate_hz=24000))
        assert e.epoch_s == pytest.approx(truth)
        assert e.n == 5

    def test_a_later_better_observation_tightens_it(self):
        e = ChannelEpoch()
        e.observe(PairObservation(
            gps_time_ns=B4[0][2] + 500_000_000,
            rtp_timesnap=B4[0][3], sample_rate_hz=24000))
        first = e.epoch_s
        e.observe(_obs(B4[0]))
        assert e.epoch_s < first
        assert e.epoch_s == pytest.approx(epoch_offset_s(_obs(B4[0])))

    def test_empty_estimator_has_no_epoch(self):
        assert ChannelEpoch().epoch_s is None


class TestMappingBetweenChannels:
    def test_round_trip_through_a_rate_change(self):
        """24 kHz -> 96 kHz -> 24 kHz must land back on the sample."""
        src_epoch = epoch_offset_s(_obs(B4[4]))       # WWV_20000
        dst_epoch = src_epoch - 3772.0                # T6's distinct origin
        rtp24 = B4[4][3]
        near = src_epoch + rtp24 / 24000

        rtp96 = rtp_in_other_channel(rtp24, src_epoch, 24000,
                                     dst_epoch, 96000, near)
        back = rtp_in_other_channel(rtp96, dst_epoch, 96000,
                                    src_epoch, 24000, near)
        assert back == rtp24

    def test_a_sample_maps_to_the_same_instant(self):
        src_epoch = epoch_offset_s(_obs(B4[4]))
        dst_epoch = src_epoch - 3772.0
        rtp24 = B4[4][3]
        near = src_epoch + rtp24 / 24000

        rtp96 = rtp_in_other_channel(rtp24, src_epoch, 24000,
                                     dst_epoch, 96000, near)
        # Reconstructing UTC from a 32-bit counter needs the same wrap
        # lift -- the counter alone fixes UTC only modulo 2**32/fs.
        period96 = (1 << 32) / 96000
        utc96 = dst_epoch + rtp96 / 96000
        utc96 += round((near - utc96) / period96) * period96
        assert utc96 == pytest.approx(near, abs=1.0 / 96000)

    def test_wrap_is_resolved_by_the_utc_hint(self):
        """A 32-bit counter fixes UTC only modulo 2**32/fs (49.7 h at
        24 kHz).  The hint picks the epoch; it needs only seconds."""
        src_epoch = epoch_offset_s(_obs(B4[4]))
        rtp24 = B4[4][3]
        true_utc = src_epoch + rtp24 / 24000
        a = rtp_in_other_channel(rtp24, src_epoch, 24000,
                                 src_epoch, 24000, true_utc)
        b = rtp_in_other_channel(rtp24, src_epoch, 24000,
                                 src_epoch, 24000, true_utc + 30.0)
        assert a == b == rtp24


class TestEpochBaseIsAFootgun:
    """radiod's GPS_TIME is in the GPS epoch; an anchor's utc_ns is UNIX.

    They differ by ~315,964,685 s.  Feeding a UNIX-epoch `near_utc_s`
    against GPS-epoch channel epochs sent the wrap lift haywire and put
    the first live B4 row 68,779 s (19 h) out.  `near_utc_s` MUST be in
    the same base as the channel epochs, and omitting it means "take the
    source counter at face value" -- which is what a caller mapping a
    CURRENT anchor wants, since there is no ambiguity to resolve.
    """

    def test_omitting_the_hint_takes_the_counter_at_face_value(self):
        src_epoch = epoch_offset_s(_obs(B4[4]))
        dst_epoch = src_epoch - 3772.0
        rtp24 = B4[4][3]

        no_hint = rtp_in_other_channel(rtp24, src_epoch, 24000,
                                       dst_epoch, 96000)
        same_base = rtp_in_other_channel(
            rtp24, src_epoch, 24000, dst_epoch, 96000,
            src_epoch + rtp24 / 24000)
        assert no_hint == same_base

    def test_an_integer_rate_ratio_absorbs_a_bad_lift(self):
        """Measured, and the reason a base mix-up did NOT corrupt the
        first live row: the lift adds k*(2**32/src_rate) seconds, which
        at dst_rate = 4*src_rate is k*4*2**32 destination samples -- an
        exact multiple of 2**32, so the mask removes it.

        This is luck, not design.  It does not hold for a non-integer
        ratio, which is why the base requirement is documented rather
        than relied upon."""
        src_epoch = epoch_offset_s(_obs(B4[4]))
        dst_epoch = src_epoch - 3772.0
        rtp24 = B4[4][3]
        right = rtp_in_other_channel(rtp24, src_epoch, 24000,
                                     dst_epoch, 96000)
        bad_base = rtp_in_other_channel(
            rtp24, src_epoch, 24000, dst_epoch, 96000,
            src_epoch + rtp24 / 24000 + 315_964_685.0)   # UNIX-vs-GPS
        assert right == bad_base
