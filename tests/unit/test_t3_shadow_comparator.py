"""T3ShadowComparator: T3 against T6 in the sample plane (task 14).

T3's registration lives in one 24 kHz channel's RTP counter; T6's anchor in
the T6 channel's 96 kHz counter.  The two counters start at different input
blocks, so the comparator relates them only through each channel's
CounterMap: radiod's input-sample index, shared by every channel of a run.

Truth here: input sample ``i`` carries UTC ``U0 + i / F_IN``.
"""
from __future__ import annotations

from fractions import Fraction

import pytest

from hf_timestd.core.native_anchor import NativeAnchor
from hf_timestd.core.t3_shadow_comparator import T3ShadowComparator
from synth_origins import (
    EPOCH_GPS_S, F_IN, ILEN, counter_map_for, olen_for, rtp_at,
)

U0_NS = 1_800_000_000 * 10**9          # UTC of input sample 0
T6_K0, T3_K0 = 40, 51
O6, O3 = olen_for(96_000), olen_for(24_000)


def _truth_ns(input_index: int) -> int:
    # exact for the indices used here (whole microseconds)
    return U0_NS + input_index * 10**9 // F_IN


def _t6_anchor(block: int, k0: int = T6_K0, rate: int = 96_000) -> NativeAnchor:
    """A T6 anchor 4 ms (384 samples at 96 kHz) into input block ``block``."""
    rtp = (rtp_at(block, k0, O6) + 384) % 2**32
    idx = block * ILEN + 384 * ILEN // O6
    return NativeAnchor(anchor_rtp=rtp, anchor_utc_ns=_truth_ns(idx),
                        sample_rate_hz=rate, chain_delay_ns=0,
                        captured_at_utc_ns=_truth_ns(idx), captured_via_tier="T5")


def _t3_summary(block: int, cmap, err_s: float = 0.0003) -> dict:
    """A T3 summary 10 ms (240 samples at 24 kHz) into ``block``, ``err_s`` late."""
    rtp = (rtp_at(block, T3_K0, O3) + 240) % 2**32
    idx = block * ILEN + 240 * ILEN // O3
    return {
        "state": "ACQUIRED", "sigma_ms": 0.8, "rtp_domain": "SHARED_10000",
        "rtp_ref": rtp, "utc_ref": _truth_ns(idx) / 1e9 + err_s,
        "sample_rate": 24_000,
        "counter_map": cmap.to_dict() if cmap is not None else None,
        "_truth_ns": _truth_ns(idx),     # test-only; the comparator ignores it
    }


def _exact_ms(summary: dict) -> float:
    """utc_ref (a float, as the summary stores it) minus the truth, exactly.

    A float utc_ref near 1.8e9 s resolves only 0.24 us, so ``+0.3 ms``
    holds to that; the comparator's own arithmetic must hold to 1 ns
    against what the float actually says."""
    return float((Fraction(summary["utc_ref"]) - Fraction(summary["_truth_ns"], 10**9))
                 * 1000)


def _assert_ms(got, summary, nominal_ms):
    assert got == pytest.approx(nominal_ms, abs=1e-3)          # within 1 us
    assert got == pytest.approx(_exact_ms(summary), abs=1e-6)  # within 1 ns


def test_t3_minus_t6_in_the_sample_plane():
    m6 = counter_map_for(T6_K0, 96_000)
    m3 = counter_map_for(T3_K0, 24_000)
    s = _t3_summary(103, m3)
    d = T3ShadowComparator.t3_minus_t6_ms(s, _t6_anchor(102), m6)
    # T3 labels its sample 0.3 ms later than T6 does: positive
    _assert_ms(d, s, 0.3)
    early = _t3_summary(103, m3, err_s=-0.0021)
    _assert_ms(T3ShadowComparator.t3_minus_t6_ms(early, _t6_anchor(102), m6), early, -2.1)


def test_no_answer_across_runs_or_without_maps():
    m6 = counter_map_for(T6_K0, 96_000)
    m3 = counter_map_for(T3_K0, 24_000)
    anchor = _t6_anchor(102)
    assert T3ShadowComparator.t3_minus_t6_ms(_t3_summary(103, None), anchor, m6) is None
    assert T3ShadowComparator.t3_minus_t6_ms(_t3_summary(103, m3), anchor, None) is None
    assert T3ShadowComparator.t3_minus_t6_ms(None, anchor, m6) is None
    assert T3ShadowComparator.t3_minus_t6_ms(_t3_summary(103, m3), None, m6) is None
    # another radiod run: the T3 map's epoch sits 3 s away
    other = counter_map_for(T3_K0, 24_000, epoch_gps_s=EPOCH_GPS_S + 3.0)
    assert T3ShadowComparator.t3_minus_t6_ms(_t3_summary(103, other), anchor, m6) is None


def test_late_gps_time_within_the_skew_allowance_is_one_run():
    """radiod's GPS_TIME runs up to 816 ms late on B4; 2 s, not 0.5 s."""
    m6 = counter_map_for(T6_K0, 96_000)
    late = counter_map_for(T3_K0, 24_000, epoch_gps_s=EPOCH_GPS_S + 0.8)
    s = _t3_summary(103, late)
    _assert_ms(T3ShadowComparator.t3_minus_t6_ms(s, _t6_anchor(102), m6), s, 0.3)


def test_anchor_rate_must_match_its_map():
    m6 = counter_map_for(T6_K0, 96_000)
    m3 = counter_map_for(T3_K0, 24_000)
    wrong = _t6_anchor(102, rate=24_000)
    assert T3ShadowComparator.t3_minus_t6_ms(_t3_summary(103, m3), wrong, m6) is None


FAR = 1_200_000     # blocks: 2.3e9 output samples at 96 kHz, past 2**31


def test_far_from_a_stale_map_refuses_rather_than_aliases():
    m6 = counter_map_for(T6_K0, 96_000)
    m3 = counter_map_for(T3_K0, 24_000)
    s = _t3_summary(FAR + 1, m3)
    assert T3ShadowComparator.t3_minus_t6_ms(s, _t6_anchor(FAR), m6) is None
    # just past reach (2**30) but short of the alias point: still refused
    near = 600_000      # 1.15e9 output samples at 96 kHz
    assert T3ShadowComparator.t3_minus_t6_ms(
        _t3_summary(near + 1, m3), _t6_anchor(near), m6) is None


def test_maps_held_near_their_rtps_answer_far_out():
    blocks = tuple(range(FAR - 2, FAR + 3))
    m6 = counter_map_for(T6_K0, 96_000, blocks=blocks)
    m3 = counter_map_for(T3_K0, 24_000, blocks=blocks)
    s = _t3_summary(FAR + 1, m3)
    _assert_ms(T3ShadowComparator.t3_minus_t6_ms(s, _t6_anchor(FAR), m6), s, 0.3)


def test_block_keeps_the_chain_delay_beside_the_raw_difference():
    m6 = counter_map_for(T6_K0, 96_000)
    m3 = counter_map_for(T3_K0, 24_000)
    anchor = _t6_anchor(102)
    s = _t3_summary(103, m3)
    b = T3ShadowComparator.block(s, anchor, m6, chain_delay_ms=1.25)
    _assert_ms(b["t3_minus_t6_ms"], s, 0.3)                    # NOT 0.3 - 1.25
    assert b["t6_chain_delay_ms"] == 1.25
    assert b["t6_anchor_input_index"] == pytest.approx(102 * ILEN + 384 * ILEN // O6)
    assert b["t6_anchor_utc_ns"] == anchor.anchor_utc_ns
    assert b["t3_rtp_domain"] == "SHARED_10000"
    assert b["t3_state"] == "ACQUIRED"
    assert b["t3_sigma_ms"] == 0.8
    empty = T3ShadowComparator.block(None, None, None, chain_delay_ms=None)
    assert empty["t3_minus_t6_ms"] is None and empty["t6_chain_delay_ms"] is None
    assert empty["t6_anchor_input_index"] is None and empty["t3_state"] is None


# ── the recorder's provider ─────────────────────────────────────────

def _recorder(tmp_path, summary, anchor, t6_map, chain_ns=None):
    import json as _json
    from types import SimpleNamespace
    from hf_timestd.core.core_recorder_v2 import CoreRecorderV2
    from hf_timestd.core.registration_store import RegistrationStore

    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "registration.json"
    if summary is not None:
        path.write_text(_json.dumps(summary))
    rec = CoreRecorderV2.__new__(CoreRecorderV2)
    rec._t3_shadow_store = RegistrationStore(tmp_path / "reg", path)
    rec._t6_native_anchor = anchor
    rec._t6_counter_map = t6_map
    record = (SimpleNamespace(offset_ns=chain_ns) if chain_ns is not None else None)
    rec._offset_judge = SimpleNamespace(
        chain_delay_store=SimpleNamespace(record=record))
    return rec


def test_recorder_provider_builds_the_block(tmp_path):
    m6 = counter_map_for(T6_K0, 96_000)
    m3 = counter_map_for(T3_K0, 24_000)
    s = _t3_summary(103, m3)
    rec = _recorder(tmp_path, s, _t6_anchor(102), m6, chain_ns=1_250_000.0)
    b = rec._t3_shadow_state()
    _assert_ms(b["t3_minus_t6_ms"], s, 0.3)
    assert b["t6_chain_delay_ms"] == 1.25
    assert _recorder(tmp_path / "x", None, None, m6)._t3_shadow_state() is None


def test_recorder_pins_the_t6_map_at_the_anchor_as_the_live_map_moves(tmp_path):
    """The 60 s refresh carries the live map hours past the anchor.  The
    map pinned while it still lay within reach keeps answering."""
    anchor = _t6_anchor(102)
    m6 = counter_map_for(T6_K0, 96_000)
    blocks = tuple(range(FAR - 2, FAR + 3))
    m3_far = counter_map_for(T3_K0, 24_000, blocks=blocks)
    s = _t3_summary(FAR + 1, m3_far)
    rec = _recorder(tmp_path, s, anchor, m6)
    rec._t3_shadow_state()                                  # pins near the anchor
    rec._t6_counter_map = counter_map_for(T6_K0, 96_000, blocks=blocks)
    _assert_ms(rec._t3_shadow_state()["t3_minus_t6_ms"], s, 0.3)
    # without the pin, the live map lies past reach of the anchor: refused
    fresh = _recorder(tmp_path / "y", s, anchor,
                      counter_map_for(T6_K0, 96_000, blocks=blocks))
    assert fresh._t3_shadow_state()["t3_minus_t6_ms"] is None
    # a new radiod run drops the pin
    rec._t6_counter_map = counter_map_for(T6_K0, 96_000, blocks=blocks,
                                          epoch_gps_s=EPOCH_GPS_S + 3600)
    assert rec._t3_shadow_state()["t3_minus_t6_ms"] is None
