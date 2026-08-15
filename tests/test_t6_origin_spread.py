"""The origin-spread tool must measure, or say plainly that it did not.

Three defects, all of the same family as the deploy.sh restart bug fixed
2026-08-15: the tool reported a clean run while having done nothing.

1. It required `sr=`, which only the T5 disambiguation log line emits.
   The external-reference lines carry no `sr=`, and the
   "already aligned within one sample" variant carries no `utc_ns=`
   either, so those anchors were dropped silently.
2. Its timestamp regex only matched journalctl's DEFAULT format.  Run
   with `-o short-iso` -- the obvious choice for a timing analysis --
   every line failed to match, and it printed "0 channel lifetime(s)
   found" and exited 0.
3. Zero usable anchors printed "cannot measure" and still exited 0, so
   a scripted run could not tell a measurement from a no-op.

A tool that cannot measure must FAIL, not report zero.
"""
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))

from t6_origin_spread import parse_log, segment_spread_ns, main

SR = 96_000
PERIOD_NS = (2**32 * 10**9) // SR


def _anchor_line(rtp, utc_ns, ts_style="default", sr=SR, utc=True):
    """A real T6 anchor log line in either journalctl output format."""
    if ts_style == "default":
        prefix = "Aug 15 01:34:24 AC0G-B4 timestd-core-recorder[2045223]:"
    else:  # -o short-iso
        prefix = ("2026-08-15T01:34:24+0000 AC0G-B4 "
                  "timestd-core-recorder[2045223]:")
    body = f"native_anchor: rtp={rtp}"
    if utc:
        body += f", utc_ns={utc_ns}"
    if sr:
        body += f", sr={sr}"
    body += ", tier=T5"
    return (f"{prefix} 2026-08-15 01:34:24,544 - INFO - T6 chain_delay "
            f"disambiguated against T5 (LB-1421 NMEA): {body}\n")


def _create_line(ts_style="default"):
    if ts_style == "default":
        prefix = "Aug 15 01:30:00 AC0G-B4 timestd-core-recorder[2045223]:"
    else:
        prefix = ("2026-08-15T01:30:00+0000 AC0G-B4 "
                  "timestd-core-recorder[2045223]:")
    return f"{prefix} 2026-08-15 01:30:00,000 - INFO - T6 BPSK PPS first samples\n"


def _origin_pair(delta_ns):
    """Two anchors whose origins differ by exactly delta_ns."""
    rtp0, utc0 = 1_000_000, 1_786_000_000_000_000_000
    return [(rtp0, utc0), (rtp0 + SR, utc0 + 10**9 + delta_ns)]


@pytest.mark.parametrize("style", ["default", "short-iso"])
def test_parses_both_journalctl_timestamp_formats(style):
    """`-o short-iso` used to match nothing and report success."""
    lines = [_create_line(style)]
    lines += [_anchor_line(r, u, style) for r, u in _origin_pair(0)]

    segs = parse_log(lines)

    assert len(segs) == 1
    assert len(segs[0]["anchors"]) == 2


def test_spread_of_two_known_origins():
    (r0, u0), (r1, u1) = _origin_pair(5_000)

    assert segment_spread_ns([(r0, u0, SR), (r1, u1, SR)]) == 5_000


def test_identical_origins_have_zero_spread():
    (r0, u0), (r1, u1) = _origin_pair(0)

    assert segment_spread_ns([(r0, u0, SR), (r1, u1, SR)]) == 0


def test_an_anchor_without_sr_is_counted_not_silently_dropped():
    """The external-reference log lines emit no sr=."""
    lines = [_create_line()]
    lines += [_anchor_line(r, u, sr=None) for r, u in _origin_pair(0)]

    segs = parse_log(lines)

    assert segs[0]["anchors"] == []
    assert segs[0]["unusable"] == 2


def test_an_anchor_without_utc_ns_is_counted_not_silently_dropped():
    """The 'already aligned within one sample' variant emits no utc_ns=."""
    lines = [_create_line()]
    lines += [_anchor_line(r, u, utc=False) for r, u in _origin_pair(0)]

    segs = parse_log(lines)

    assert segs[0]["anchors"] == []
    assert segs[0]["unusable"] == 2


def test_no_usable_anchors_exits_nonzero(tmp_path, capsys):
    """The whole point: a run that measured nothing must not look clean."""
    log = tmp_path / "j.log"
    log.write_text(_create_line() + _anchor_line(1, 2, sr=None))

    rc = main([str(log)])

    assert rc != 0
    assert "unusable" in capsys.readouterr().out.lower()


def test_a_real_measurement_exits_zero(tmp_path):
    log = tmp_path / "j.log"
    log.write_text(_create_line()
                   + "".join(_anchor_line(r, u) for r, u in _origin_pair(0)))

    assert main([str(log)]) == 0


def test_an_empty_log_exits_nonzero(tmp_path):
    """Nothing at all matched is the loudest possible no-op."""
    log = tmp_path / "j.log"
    log.write_text("some unrelated journal content\n")

    assert main([str(log)]) != 0


def test_every_anchor_log_line_the_recorder_emits_is_parseable():
    """Producer/consumer contract.

    Two of the three anchor log sites emitted a shape this tool could
    not use, and nothing caught it because the format strings were
    inline at each site.  One formatter, asserted against the tool's own
    regex, makes that impossible to reintroduce.
    """
    from hf_timestd.core.core_recorder_v2 import format_native_anchor_log
    from hf_timestd.core.native_anchor import NativeAnchor

    anchor = NativeAnchor(
        anchor_rtp=2148472777, anchor_utc_ns=1786757664016618000,
        sample_rate_hz=96_000, chain_delay_ns=16_628_000,
        captured_at_utc_ns=1786757664000000000, captured_via_tier="T5",
    )
    line = f"Aug 15 01:34:24 host svc[1]: INFO - blah; " \
           f"{format_native_anchor_log(anchor, 'T5')}\n"

    segs = parse_log([_create_line(), line])

    assert segs[0]["unusable"] == 0
    assert segs[0]["anchors"] == [(2148472777, 1786757664016618000, 96_000)]


def test_the_authoritative_fine_stage_anchor_is_logged_for_the_tool(caplog):
    """Post-inversion the fine-stage anchor IS the anchor.

    The coarse capture is logged and then superseded seconds later by a
    T6 recapture that was never logged in this shape, so origin spread
    could only ever see the anchor that stopped being used.
    """
    import logging
    from hf_timestd.core.core_recorder_v2 import CoreRecorderV2
    from hf_timestd.core.native_anchor import NativeAnchor
    from hf_timestd.core.t6_anchor_authority import (
        T6AnchorDecision, T6AuthorityState,
    )

    rec = CoreRecorderV2.__new__(CoreRecorderV2)
    rec._t6_rate_est = None
    rec._t6_arrival_floor = None
    anchor = NativeAnchor(
        anchor_rtp=2225172937, anchor_utc_ns=1786760000016628000,
        sample_rate_hz=96_000, chain_delay_ns=16_628_000,
        captured_at_utc_ns=1786760000000000000, captured_via_tier="T6",
    )
    decision = T6AnchorDecision(
        state=T6AuthorityState.AUTHORITATIVE,
        previous_state=T6AuthorityState.ACQUIRING,
        anchor=anchor, violations=(),
    )

    with caplog.at_level(logging.INFO):
        rec._t6_apply_authority_decision(decision)

    segs = parse_log([_create_line()] + [r.getMessage() + "\n"
                                         for r in caplog.records])
    assert segs[0]["unusable"] == 0
    assert (2225172937, 1786760000016628000, 96_000) in segs[0]["anchors"]
