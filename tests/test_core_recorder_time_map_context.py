"""The recorder hands the archive writer what it knows about the channel's
registration: the T6 anchor, whether its lock is credible, the counter space.

Phase 1 note: the BPSK PPS channel is provisioned with no archive, so no
archived chunk is the T6 counter space; every archived channel therefore
receives no anchor and registers the sysclock chain.  The T6 branch is here
for the day the T6 stream is archived, and is tested as such."""
from types import SimpleNamespace
from unittest.mock import MagicMock

from hf_timestd.core.core_recorder_v2 import CoreRecorderV2

T0 = 1_788_537_251_999_997_458


def _bare(anchor=None, state="AUTHORITATIVE", violations=(), sigma=4093):
    cr = CoreRecorderV2.__new__(CoreRecorderV2)
    cr._t6_native_anchor = anchor
    cr._t6_config = {"description": "T6_96000"}
    cr.status_address = "AC0G-B4-status.local"
    cr._t6_authority_status = MagicMock(return_value={
        "state": state, "violations": list(violations), "sigma_ns": sigma})
    return cr


def _anchor(rtp=2_150_319_213):
    return SimpleNamespace(anchor_rtp=rtp, anchor_utc_ns=T0, sample_rate_hz=96_000)


def test_t6_channel_with_authoritative_anchor_is_credible():
    ctx = _bare(anchor=_anchor()).time_map_context("T6_96000")
    assert ctx["anchor_rtp"] == 2_150_319_213 and ctx["anchor_utc_ns"] == T0
    assert ctx["anchor_sigma_ns"] == 4093 and ctx["lock_credible"] is True
    assert ctx["counter_space"] == "AC0G-B4-status.local/T6_96000"


def test_acquiring_state_is_not_credible():
    ctx = _bare(anchor=_anchor(1), state="ACQUIRING").time_map_context("T6_96000")
    assert ctx["lock_credible"] is False and ctx["anchor_rtp"] == 1


def test_violations_make_an_authoritative_anchor_not_credible():
    ctx = _bare(anchor=_anchor(), violations=["fine_coarse_disagree"]).time_map_context("T6_96000")
    assert ctx["lock_credible"] is False


def test_other_channels_carry_no_anchor():
    ctx = _bare(anchor=_anchor()).time_map_context("SHARED_10000")
    assert ctx["anchor_rtp"] is None and ctx["lock_credible"] is False
    assert ctx["counter_space"] == "AC0G-B4-status.local/SHARED_10000"


def test_a_raising_authority_status_is_not_credible_and_does_not_raise():
    cr = _bare(anchor=_anchor())
    cr._t6_authority_status = MagicMock(side_effect=RuntimeError("boom"))
    ctx = cr.time_map_context("T6_96000")
    assert ctx["anchor_rtp"] == 2_150_319_213 and ctx["lock_credible"] is False
