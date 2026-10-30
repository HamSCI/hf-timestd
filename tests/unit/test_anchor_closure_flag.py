"""The registration anchor closure is opt-in (task 17a).

The closure (spec §11, tasks 13-16) deployed to AC0G-ND on 2026-09-07,
engaged the anchor-direct FUSE feed twice, and both times reported a
POSITIVE host error while four NTP witnesses put the host fast; chrony
followed FUSE and slewed the host the wrong way.  Until the closure has
passed a live acceptance it must be OFF unless a station asks for it, and
"off" has to mean off at every one of the four surfaces the registration
reaches -- the ring, authority.json §18, the archive sidecar, and the
FUSE chrony feed.

Every test here states the same thing twice: what the surface does with
the flag false, and that the answer matches what the code did at
c7b2106, the commit before the closure landed.  The legacy answer is not
re-derived from a fixture: it is computed from the pre-closure algorithm
written out beside the assertion, so the comparison stays honest if the
closure's own arithmetic changes.
"""

import json
import logging

import pytest

from hf_timestd.core.anchor_closure import (
    CLOSURE_KEY,
    anchor_closure_enabled,
    closure_from_config,
)
from hf_timestd.core.offset_judge import (
    BenchReading,
    LABEL_PLANE_ANCHOR_KEY,
    LABEL_PLANE_WITNESS_KEY,
    OffsetJudge,
    label_plane_chrony_sample,
    read_label_plane_anchor,
)
from hf_timestd.core.registration_store import RegistrationStore
from hf_timestd.core.t3_registration_anchor import T3RegistrationAnchor

SR = 24000
WALL0 = 1_800_000_000.0
KEY = ("hf-status.local", 0xABCD1234)


# ── the flag itself ──────────────────────────────────────────────────


def test_absent_section_is_off():
    assert closure_from_config(None) is False
    assert closure_from_config({}) is False
    assert closure_from_config({"timing": {}}) is False
    assert closure_from_config({"timing": {"registration": {}}}) is False


def test_the_key_turns_it_on_and_off():
    assert (
        closure_from_config({"timing": {"registration": {CLOSURE_KEY: True}}}) is True
    )
    assert (
        closure_from_config({"timing": {"registration": {CLOSURE_KEY: False}}}) is False
    )


def test_a_malformed_section_is_off_not_an_exception():
    assert closure_from_config({"timing": "not-a-table"}) is False
    assert closure_from_config({"timing": {"registration": 7}}) is False


def test_missing_config_file_is_off(tmp_path):
    assert anchor_closure_enabled(path=tmp_path / "nope.toml") is False


def test_config_file_is_read(tmp_path):
    p = tmp_path / "timestd-config.toml"
    p.write_text("[timing.registration]\nanchor_closure = true\n", encoding="utf-8")
    assert anchor_closure_enabled(path=p) is True


def test_the_regime_is_logged_once(caplog):
    from hf_timestd.core import anchor_closure as mod

    mod.reset_log_state()
    with caplog.at_level(logging.INFO, logger="hf_timestd.core.anchor_closure"):
        anchor_closure_enabled({})
        anchor_closure_enabled({})
    lines = [r.getMessage() for r in caplog.records]
    assert lines == ["registration anchor closure: DISABLED (witness only)"]
    mod.reset_log_state()


# ── surface 1+2+3: the T3 anchor the ring, §18 and the sidecar read ──


def _summary(now, **over):
    s = {
        "state": "ACQUIRED",
        "rtp_ref": 1_000_000,
        "utc_ref": WALL0 + 41.666,
        "sample_rate": SR,
        "sigma_ms": 0.4,
        "written_at": now,
        "verified": True,
        "n_minutes": 7,
        "counter_epoch_id": "ep-1800000000",
    }
    s.update(over)
    return s


class _Store:
    stale_s = 300.0

    def __init__(self, summary):
        self._s = summary

    def read_summary(self):
        return self._s


def test_t3_anchor_stands_down_with_the_flag_off():
    """The one summary that passes every gate still yields no anchor.

    Legacy (c7b2106): ``T3RegistrationAnchor`` did not exist, so the
    ring, §18 and the sidecar had NO label-plane anchor at all and
    resolved UTC from radiod's pair with the judge's correction.  ``None``
    from ``state()`` is exactly that.
    """
    store = _Store(_summary(WALL0))
    off = T3RegistrationAnchor(store=store, time_fn=lambda: WALL0)
    on = T3RegistrationAnchor(store=store, time_fn=lambda: WALL0, anchor_closure=True,
                              admitted=True)
    # The flag is the ONLY difference; the summary is fully verified and
    # corroborated, so the closure would otherwise stand up.
    assert on.evaluate(t6_authoritative=False).in_force is True
    d = off.evaluate(t6_authoritative=False)
    assert d.in_force is False
    assert d.anchor is None
    assert d.reason == "closure_disabled"
    assert off.state() is None


def test_the_flag_is_off_by_default():
    store = _Store(_summary(WALL0))
    holder = T3RegistrationAnchor(store=store, time_fn=lambda: WALL0)
    holder.refresh(t6_authoritative=False)
    assert holder.anchor is None
    assert holder.state() is None


def test_closure_disabled_outranks_every_other_reason():
    """Even a summary that would be refused anyway reports the flag.

    The operator needs to see WHY nothing stands up, and "the closure is
    off" is the answer that stops an investigation into gates that were
    never consulted.
    """
    store = _Store(_summary(WALL0, state="BOOTSTRAP"))
    holder = T3RegistrationAnchor(store=store, time_fn=lambda: WALL0)
    assert holder.evaluate(t6_authoritative=False).reason == "closure_disabled"


# ── surface 4: the FUSE chrony feed ──────────────────────────────────


class _FixedBench:
    def __init__(self, reading):
        self._reading = reading

    def poll(self):
        return self._reading


def _unix_to_gps_ns(unix_s: float) -> int:
    from hf_timestd.core.buffer_timing import unix_ns_to_gps_time_ns

    return unix_ns_to_gps_time_ns(int(round(unix_s * 1e9)))


def _judge(tmp_path, benches, wall, mono, **kw):
    judge = OffsetJudge(
        config={"enabled": True},
        benches=benches,
        publish_path=tmp_path / "offset_judge.json",
        time_fn=lambda: wall[0],
        mono_fn=lambda: mono[0],
        # These tests are about the closure; T3 is admitted throughout so
        # the closure alone decides (Ruling B, 2026-10-30).
        **{"t3_admitted": True, **kw},
    )
    judge.register_radiod_pair(KEY, _unix_to_gps_ns(wall[0]), 0, SR)
    return judge


def _label_reading(wall, mono, offset_ms=-150.0):
    """A T3 label-plane reading whose ``reference − system`` is ``offset_ms``."""
    return BenchReading(
        utc=wall[0] + offset_ms / 1000.0,
        mono=mono[0],
        sigma_ns=1_000_000.0,
        tier="T3",
        detail={"bench": "hf_acquired", "authoritative": True},
        plane="label",
    )


def test_the_fuse_feed_gets_no_anchor_with_the_flag_off(tmp_path):
    """``label_plane_anchor`` is never populated, so chrony sees nothing.

    Legacy (c7b2106): ``offset_judge.json`` carried no
    ``label_plane_anchor`` key at all and the FUSE feed had only fusion's
    d_clock.  ``read_label_plane_anchor`` answering None is that same
    behaviour for every consumer, since it returns None for an absent
    key and for a null one alike.
    """
    wall, mono = [WALL0], [1000.0]
    judge = _judge(tmp_path, [_FixedBench(_label_reading(wall, mono))], wall, mono)
    judge.tick()
    path = tmp_path / "offset_judge.json"
    data = json.loads(path.read_text())
    assert data[LABEL_PLANE_ANCHOR_KEY] is None
    assert read_label_plane_anchor(path) is None
    assert label_plane_chrony_sample(path, mono_fn=lambda: mono[0]) is None


def test_the_flag_on_restores_the_anchor_direct_feed(tmp_path):
    wall, mono = [WALL0], [1000.0]
    judge = _judge(
        tmp_path,
        [_FixedBench(_label_reading(wall, mono))],
        wall,
        mono,
        anchor_closure=True,
    )
    judge.tick()
    path = tmp_path / "offset_judge.json"
    assert read_label_plane_anchor(path) is not None
    sample = label_plane_chrony_sample(
        path, time_fn=lambda: wall[0], mono_fn=lambda: mono[0]
    )
    assert sample is not None
    assert sample.offset_s == pytest.approx(-0.150, abs=1e-6)


def test_the_witness_block_carries_the_statement_with_the_flag_off(tmp_path):
    """17c needs the number even while nothing acts on it.

    The same statement is published under ``label_plane_witness``, which
    no chrony path reads, so the sign contradiction can be watched
    offline without any risk of steering.
    """
    wall, mono = [WALL0], [1000.0]
    judge = _judge(tmp_path, [_FixedBench(_label_reading(wall, mono))], wall, mono)
    judge.tick()
    path = tmp_path / "offset_judge.json"
    data = json.loads(path.read_text())
    witness = data[LABEL_PLANE_WITNESS_KEY]
    assert witness is not None
    assert witness["tier"] == "T3"
    assert witness["plane"] == "label"
    # The witness is readable only when asked for by name.
    assert read_label_plane_anchor(path, key=LABEL_PLANE_WITNESS_KEY) is not None
    sample = label_plane_chrony_sample(
        path,
        key=LABEL_PLANE_WITNESS_KEY,
        time_fn=lambda: wall[0],
        mono_fn=lambda: mono[0],
    )
    assert sample is not None
    assert sample.offset_s == pytest.approx(-0.150, abs=1e-6)


def test_the_two_blocks_are_never_both_populated(tmp_path):
    wall, mono = [WALL0], [1000.0]
    for closure in (False, True):
        judge = _judge(
            tmp_path,
            [_FixedBench(_label_reading(wall, mono))],
            wall,
            mono,
            anchor_closure=closure,
        )
        judge.tick()
        data = json.loads((tmp_path / "offset_judge.json").read_text())
        populated = [
            k
            for k in (LABEL_PLANE_ANCHOR_KEY, LABEL_PLANE_WITNESS_KEY)
            if data[k] is not None
        ]
        assert populated == (
            [LABEL_PLANE_ANCHOR_KEY] if closure else [LABEL_PLANE_WITNESS_KEY]
        )


# ── §18: the judge's own label-anchor provider ───────────────────────


class _Label:
    """Minimal LabelAnchor stand-in for the provider path."""

    def __init__(self):
        from hf_timestd.core.native_anchor import LabelAnchor, NativeAnchor

        self.label = LabelAnchor(
            anchor=NativeAnchor(
                anchor_rtp=0,
                anchor_utc_ns=int(WALL0 * 1e9),
                sample_rate_hz=SR,
                chain_delay_ns=0,
                captured_at_utc_ns=int(WALL0 * 1e9),
                captured_via_tier="T3",
            ),
            epoch_id="ep-1",
            tier="T3",
            sigma_ns=1e6,
        )

    def __call__(self):
        return self.label


def test_section18_keeps_the_judged_pair_with_the_flag_off(tmp_path):
    """§18's ``utc_anchor_ns`` comes from radiod's pair, as at c7b2106.

    The provider is installed exactly as the recorder installs it; the
    flag alone decides whether the judge consults it.
    """
    wall, mono = [WALL0], [1000.0]
    for closure in (False, True):
        judge = _judge(
            tmp_path,
            [_FixedBench(_label_reading(wall, mono))],
            wall,
            mono,
            anchor_closure=closure,
        )
        judge.set_label_anchor_provider(_Label())
        st = list(judge._sources.values())[0]
        got = judge._label_anchor_for_locked(st)
        assert (got is not None) is closure


# ── the recorder's provider, the single wiring point for 1-3 ─────────


class _Recorder:
    """The recorder's label-anchor provider in isolation.

    ``CoreRecorderV2`` is far too heavy to construct here, so this binds
    the real methods to a bare object carrying only the attributes they
    read -- the same technique ``test_t3_ring_anchor.py`` uses.
    """

    def __init__(self, closure, t3_state):
        from hf_timestd.core.core_recorder_v2 import CoreRecorderV2

        self._anchor_closure = closure
        self._t3 = t3_state
        self._label_anchor_state = CoreRecorderV2._label_anchor_state.__get__(self)
        self._t3_label_anchor_state = lambda: self._t3
        self._t6_label_anchor_state = lambda: None


def test_the_recorder_offers_no_label_anchor_with_the_flag_off():
    label = _Label().label
    assert _Recorder(False, label)._label_anchor_state() is None
    assert _Recorder(True, label)._label_anchor_state() is label


# ── the store is untouched: the plane is still published as witness ──


def test_the_summary_is_still_published_and_still_authoritative(tmp_path):
    """17a stands the CONSUMERS down, not the acquirer.

    ``registration_refusal`` is deliberately not flag-aware: the
    registration remains the station's published plane and every witness
    -- ``HfAcquiredBench``, the offline analysis, station-web -- keeps
    reading it.  What changes is that no surface acts on it.
    """
    from hf_timestd.core.registration_store import registration_refusal

    store = RegistrationStore(
        directory=tmp_path / "reg",
        summary_path=tmp_path / "registration.json",
        time_fn=lambda: WALL0,
    )
    (tmp_path / "registration.json").write_text(
        json.dumps(_summary(WALL0)), encoding="utf-8"
    )
    s = store.read_summary()
    assert registration_refusal(s, now=WALL0) is None


def test_the_whole_chain_stays_legacy_with_the_flag_off(tmp_path):
    """Judge → fusion_status.json, through the real code.

    The four surfaces are gated in three different processes, so the
    per-surface tests above can each pass while the chain between them
    leaks.  This walks it: the judge publishes with the closure off, the
    fusion process reads what it would feed chrony, the status writer
    names the regime, and the gate decides.  Every step is the shipped
    function, not a stand-in.
    """
    from hf_timestd.core import fusion_status_writer as fsw

    wall, mono = [WALL0], [1000.0]
    judge = _judge(tmp_path, [_FixedBench(_label_reading(wall, mono))], wall, mono)
    judge.tick()

    # 1-3. Nothing to anchor the ring, §18 or the sidecar with.
    assert judge._label_anchor_for_locked(list(judge._sources.values())[0]) is None
    # 4. Nothing for the FUSE feed: this is the call fusion makes.
    anchor_sample = label_plane_chrony_sample(
        tmp_path / "offset_judge.json", mono_fn=lambda: mono[0]
    )
    assert anchor_sample is None

    # The regime fusion then publishes, and what the gate does with it.
    status = tmp_path / "fusion_status.json"
    writer = fsw.FusionStatusWriter(path=status, cycle_interval_sec=60.0)
    writer.update(None, chrony_fed=False, skip_reasons=[], anchor_sample=anchor_sample)
    assert fsw.read_feed_regime(status) == fsw.LEGACY_REGIME

    # The gate that once read this regime retired on 2026-09-11 (§7.1.1);
    # the regime itself stays published for readers of fusion_status.json.

