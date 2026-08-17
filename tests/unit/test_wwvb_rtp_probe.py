"""WWVB RTP-advance probe (hf-timestd#23).

The WWVB decode buffer re-anchors every batch because the consumer's
continuity check assumes RTP advances one tick per delivered sample:

    expected = anchor_rtp + buf_samples

Measured on AC0G-B4, the arriving RTP is short of that by a CONSTANT
fraction of the batch, at both sample rates tested:

    sr=24000  batch 3600  deficit 1620  ->  0.450
    sr=12000  batch 2640  deficit 1200  ->  0.455

So one of the two terms is wrong by a fixed factor.  This probe compares
the RTP advance BETWEEN consecutive batches against the sample count the
previous batch actually delivered, which separates "we appended more
samples than arrived" from "RTP does not count samples 1:1".
"""
import pytest

from hf_timestd.core.core_recorder_v2 import CoreRecorderV2


def _probe(prev_rtp, prev_len, rtp0):
    r = CoreRecorderV2.__new__(CoreRecorderV2)
    return r._wwvb_rtp_advance_report(prev_rtp, prev_len, rtp0)


class TestReportsOnlyWhenTheyDisagree:
    def test_matching_advance_reports_nothing(self):
        assert _probe(1000, 2640, 1000 + 2640) is None

    def test_no_previous_batch_reports_nothing(self):
        assert _probe(None, None, 5000) is None

    def test_a_deficit_is_reported(self):
        r = _probe(1000, 2640, 1000 + 1440)
        assert r is not None
        assert r["sample_len"] == 2640
        assert r["rtp_delta"] == 1440
        assert r["deficit"] == 1200


class TestTheNumbersThatDiscriminate:
    def test_ratio_is_rtp_advance_per_delivered_sample(self):
        """~0.55 is the measured signature; the ratio is what says
        whether the mismatch is proportional or an alignment offset."""
        r = _probe(0, 2640, 1440)
        assert r["ratio"] == pytest.approx(1440 / 2640)

    def test_the_measured_b4_case_at_24k(self):
        r = _probe(0, 3600, 1980)
        assert r["deficit"] == 1620
        assert r["ratio"] == pytest.approx(0.55, abs=0.01)

    def test_the_measured_b4_case_at_12k(self):
        r = _probe(0, 2640, 1440)
        assert r["deficit"] == 1200
        assert r["ratio"] == pytest.approx(0.545, abs=0.01)

    def test_an_excess_advance_is_also_reported(self):
        """A gap (real loss) advances RTP MORE than the samples
        delivered — the opposite sign, and it must not be silently
        lumped in with the deficit."""
        r = _probe(0, 2640, 5000)
        assert r["deficit"] == 2640 - 5000
        assert r["deficit"] < 0


class TestCounterWrap:
    def test_advance_across_the_32_bit_wrap(self):
        prev = (1 << 32) - 100
        r = _probe(prev, 2640, 1340)   # wrapped: advance 1440
        assert r["rtp_delta"] == 1440
        assert r["deficit"] == 1200

    def test_exact_match_across_the_wrap_reports_nothing(self):
        prev = (1 << 32) - 100
        assert _probe(prev, 2640, (prev + 2640) & 0xFFFFFFFF) is None
