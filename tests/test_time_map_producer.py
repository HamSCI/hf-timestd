"""One function from what the recorder knows to the TimeMap it publishes.
TIMING_PROVENANCE_MODEL §3.1 / §3.1.1; MEASUREMENT_MODEL §6.4, §8."""
from __future__ import annotations

from datetime import datetime, timezone

from hamsci_dsp.timing import AuthoritySnapshot
from hf_timestd.core.time_map_producer import TimeMapInputs, TimeMapProducer

T0 = 1_788_537_251_999_997_458
HC_FAULT = {"verdict": "fault", "reason": "T2 disagrees by 11679.5 ms (> 1000 ms)",
            "witnesses": {"T2": {"kind": "pair_ms", "value": 11679.507, "bound": 60.0, "exceeded": True}},
            "since_utc": "2026-09-04T02:47:12.000000Z"}


def _snap(host_clock=None, a_level="A1"):
    return AuthoritySnapshot(
        utc_published=datetime(2026, 9, 4, 16, 10, tzinfo=timezone.utc), a_level=a_level,
        t_level_active="T6", t_level_available=["T6"], t_level_witnesses=["T2"],
        rtp_to_utc_offset_ns=0, sigma_ns=4093, stations_contributing=[],
        last_transition_utc=None, disagreement_flags=[], governor_radiod="AC0G-B4-status.local",
        host_clock=host_clock,
    )


def _inputs(**kw):
    base = dict(counter_space="AC0G-B4-status.local/T6_96000", counter_epoch_id="pair-1",
                f_s_hz=96_000, measured_at_utc_ns=T0,
                gps_time_ns=T0 - 12_000_000, rtp_timesnap=2_150_318_060,
                engineering={"judge_age_s": 0.3})
    base.update(kw)
    return TimeMapInputs(**base)


def test_credible_anchor_yields_native_anchor_map_with_observed_a_level():
    p = TimeMapProducer(snapshot_fn=lambda: _snap(host_clock={"verdict": "ok", "witnesses": {}}))
    m = p.build(_inputs(anchor_rtp=2_150_319_213, anchor_utc_ns=T0, anchor_sigma_ns=4093,
                        lock_credible=True, judge_tier="T6"))
    assert m.origin == "native_anchor" and m.u_epoch_ns == 4093
    assert (m.a_level, m.a_level_provenance) == ("A1", "observed")
    assert m.engineering["judge_tier"] == "T6"
    assert m.engineering["host_clock"]["verdict"] == "ok"
    assert m.utc_ns_at(2_150_319_213) == T0


def test_uncredible_anchor_does_not_fall_back_to_the_pair_silently():
    # A wrong edge registers nothing; the pair would be a different chain and
    # the reader must see the refusal, not a quiet downgrade.
    p = TimeMapProducer(snapshot_fn=lambda: _snap())
    m = p.build(_inputs(anchor_rtp=1, anchor_utc_ns=T0, anchor_sigma_ns=4000, lock_credible=False))
    assert m.origin is None and "lock_not_credible" in m.reason


def test_no_anchor_yields_sysclock_map_bounded_by_the_host_clock():
    p = TimeMapProducer(snapshot_fn=lambda: _snap(host_clock=HC_FAULT))
    m = p.build(_inputs())
    assert m.origin == "sysclock" and m.chain == "sysclock@1"
    assert m.u_epoch_ns == 11_679_507_000 and m.k == 1
    assert (m.n0, m.t0_utc_ns) == (2_150_318_060, T0 - 12_000_000)


def test_no_snapshot_means_assumed_a_level_and_no_host_clock():
    p = TimeMapProducer(snapshot_fn=lambda: None, a_level_config="A1")
    m = p.build(_inputs())
    assert (m.a_level, m.a_level_provenance) == ("A1", "assumed")
    assert m.u_epoch_ns == 8_030_000
    assert "host_clock" not in m.engineering


def test_nothing_at_all_is_a_null_map_with_a_reason():
    p = TimeMapProducer(snapshot_fn=lambda: None)
    m = p.build(_inputs(gps_time_ns=None, rtp_timesnap=None))
    assert m.origin is None and "no anchor" in m.reason and "no radiod pair" in m.reason


def test_a_raising_snapshot_fn_never_reaches_the_recorder():
    def boom():
        raise RuntimeError("authority.json unreadable")
    p = TimeMapProducer(snapshot_fn=boom, a_level_config="A0")
    m = p.build(_inputs())
    assert m.origin == "sysclock" and m.a_level_provenance == "assumed"
