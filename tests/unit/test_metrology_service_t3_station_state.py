"""The station summary's state on a T6 station, and who may overwrite it.

AC0G-B4's 36-hour T3 shadow record (2026-10-31/11-01) traced three
defects to ``MetrologyService``:

* 42 of 63 station drops to CANDIDATE: the T6 branch took WITNESS or
  CANDIDATE from the writer's own acquirer while it published the FUSED
  plane, often a verified sibling's.
* 21 BOOTSTRAP minutes: a channel that had lost its own plane published
  ``fused=None`` over a summary its siblings had just written.
* 36 of 37 WITNESS->ACQUIRED drops: one authority tick without T6 sent
  every channel down the non-T6 path at once.
"""

import dataclasses
import json

import pytest

from hf_timestd.core.registration_acquirer import Registration, RegistrationAcquirer
from tests.unit.test_metrology_service_registration import (
    MIN,
    SR,
    T0,
    _meta,
    _minute,
    _service,
    _SIB_MAP,
    _ticks,
)
from tests.unit.synth_ticks import label_timing


# ── fix 3: the T6 branch names the state of the plane it publishes ────


def _fused_like(svc, verified):
    """A sibling-only plane at truth in this channel's counter."""
    return Registration(
        svc.acquirer.registration.counter_epoch_id, rtp_ref=1_000_000 + 60 * SR,
        utc_ref=T0 + 60, sample_rate=SR, sigma_ms=0.9, n_minutes=40,
        channel="fused", stations=("WWV",), verified=verified,
        counter_map=_SIB_MAP, rtp_domain=svc.channel_name,
    )


@pytest.mark.parametrize("own_verified,fused_verified,expect", [
    (False, True, "WITNESS"),
    (True, False, "CANDIDATE"),
])
def test_t6_state_follows_the_fused_plane_not_the_writer(
        tmp_path, monkeypatch, own_verified, fused_verified, expect):
    """The summary carries the FUSED plane, so its WITNESS/CANDIDATE must
    be the fused plane's verification.  B4 2026-11-01: a shared channel
    whose own plane was still a candidate published its verified
    siblings' plane, n continuing, as CANDIDATE -- 42 of 63 station drops
    to CANDIDATE.  The reverse case keeps an unverified fused plane from
    reaching the summary as WITNESS."""
    svc = _service(tmp_path)
    _minute(svc, 0, walk=0.0, raw_walk=0.0, t6=True)
    svc.acquirer.registration.verified = own_verified
    fused = _fused_like(svc, fused_verified)
    real = svc._fuse_minute

    def fake_fuse(own, convertible, resolved, start_rtp, at_map):
        real(own, convertible, resolved, start_rtp, at_map)
        return (svc.acquirer.registration, False, fused, ["WWV_20000"], [], False)

    monkeypatch.setattr(svc, "_fuse_minute", fake_fuse)
    monkeypatch.setattr(svc, "_route_arrivals", lambda arrivals: None)
    _minute(svc, 1, walk=0.0, raw_walk=0.0, t6=True)
    assert svc.acquirer.registration.verified is own_verified
    s = svc.reg_store.read_summary()
    assert s["state"] == expect
    assert s["verified"] is fused_verified
    # The channel file carries the OWN plane: siblings read a WITNESS file
    # as verified evidence, so it keeps the own plane's state.
    ch = json.loads((tmp_path / "reg" / "SHARED_10000.json").read_text())
    assert ch["state"] == ("WITNESS" if own_verified else "CANDIDATE")


# ── fix 4: nothing to publish never overwrites a sibling's plane ─────


def _store_at(tmp_path, now):
    from hf_timestd.core.registration_store import RegistrationStore

    return RegistrationStore(tmp_path / "reg", tmp_path / "registration.json",
                             time_fn=lambda: now[0])


def test_store_names_another_writers_fresh_plane(tmp_path):
    """The stopgap's one question: does a FRESH plane written by ANOTHER
    channel stand in the summary?  Fresh means within the store's
    ``stale_s``, the age past which a sibling's file stops counting as
    live evidence."""
    now = [1000.0]
    store = _store_at(tmp_path, now)
    assert store.fresh_plane_from_another_writer("B") is None  # nothing yet
    plane = Registration("ep", rtp_ref=1, utc_ref=T0, sample_rate=SR,
                         sigma_ms=1.0, verified=True, n_minutes=5)
    store.write_summary(plane, ["A"], "WITNESS", {}, writer="A")
    assert store.read_summary()["writer"] == "A"
    assert store.fresh_plane_from_another_writer("B") == "A"
    assert store.fresh_plane_from_another_writer("A") is None  # its own
    now[0] += store.stale_s + 1.0
    assert store.fresh_plane_from_another_writer("B") is None  # stale
    now[0] = 1000.0
    store.write_summary(None, [], "BOOTSTRAP", {}, writer="A")
    assert store.fresh_plane_from_another_writer("B") is None  # no plane


def test_a_channel_without_a_plane_leaves_a_siblings_summary_standing(tmp_path):
    """B4 shadow record: 21 BOOTSTRAP minutes came from a channel that had
    lost its own plane publishing fused=None over the plane its siblings
    had just written -- last writer wins.  The channel still writes its
    own file, and says there whose summary it left standing."""
    svc = _service(tmp_path)
    plane = Registration("ep", rtp_ref=1, utc_ref=T0, sample_rate=SR,
                         sigma_ms=1.0, verified=True, n_minutes=5)
    svc.reg_store.write_summary(plane, ["WWV_20000"], "ACQUIRED", {},
                                writer="WWV_20000")
    # No ticks, no siblings: this channel has nothing to publish.
    import numpy as np
    svc.apply_registration(label_timing(T0, 0.0, SR), np.zeros(62 * SR),
                           1_000_000, MIN, _meta(0, 0.0))
    s = svc.reg_store.read_summary()
    assert s["state"] == "ACQUIRED" and s["writer"] == "WWV_20000"
    ch = json.loads((tmp_path / "reg" / "SHARED_10000.json").read_text())
    assert ch["state"] == "BOOTSTRAP"
    assert ch["summary_left_to"] == "WWV_20000"


def test_a_channel_withdraws_its_own_plane_from_the_summary(tmp_path):
    """The writer of the standing plane may still stand it down: when its
    own plane goes, its BOOTSTRAP reaches the summary (task 16b, "a reset
    stands every surface down the same minute")."""
    svc = _service(tmp_path)
    _minute(svc, 0, walk=0.0, raw_walk=0.0)
    assert svc.reg_store.read_summary()["writer"] == "SHARED_10000"
    import numpy as np
    svc.acquirer.reset("test")
    svc.apply_registration(label_timing(T0 + 60, 0.0, SR), np.zeros(62 * SR),
                           1_000_000 + 60 * SR, MIN + 60, _meta(1, 0.0))
    s = svc.reg_store.read_summary()
    assert s["state"] == "BOOTSTRAP" and s["writer"] == "SHARED_10000"
    ch = json.loads((tmp_path / "reg" / "SHARED_10000.json").read_text())
    assert ch["summary_left_to"] is None


# ── fix 5: one authority tick without T6 does not switch the path ────


def _auth(tmp_path, svc):
    path = tmp_path / "authority.json"
    svc._AUTHORITY_JSON_PATH = path

    def say(level):
        path.write_text(json.dumps({"t_level_active": level}))
    return say


def test_t6_blinks_shorter_than_the_debounce_keep_the_t6_path(tmp_path):
    """B4 shadow record: 36 of 37 WITNESS->ACQUIRED drops came from T6
    leaving authority.json for one probe tick at a 10-minute archive
    boundary.  The manager's upgrade hysteresis (3 ticks of 30 s) keeps
    t_level_active off T6 for up to 90 s after one bad tick, so a
    once-a-minute reader sees at most two consecutive reads without it.
    Two such reads stay on the T6 path; the third switches."""
    svc = _service(tmp_path)
    say = _auth(tmp_path, svc)
    bt = label_timing(T0, 0.0, SR)
    say("T6")
    assert svc._t6_authoritative(bt) is True
    say("T4")
    assert svc._t6_authoritative(bt) is True
    assert svc._t6_authoritative(bt) is True
    say("T6")
    assert svc._t6_authoritative(bt) is True  # the blink ended: count resets
    say("T4")
    reads = [svc._t6_authoritative(bt) for _ in range(svc.T6_ABSENT_READS_TO_LEAVE)]
    assert reads == [True] * (svc.T6_ABSENT_READS_TO_LEAVE - 1) + [False]
    assert svc._t6_authoritative(bt) is False


def test_t6_return_switches_back_at_once(tmp_path):
    """The return needs no debounce here: the authority manager publishes
    T6 again only after its own upgrade hysteresis."""
    svc = _service(tmp_path)
    say = _auth(tmp_path, svc)
    bt = label_timing(T0, 0.0, SR)
    say("T4")
    assert svc._t6_authoritative(bt) is False  # startup takes what it reads
    say("T6")
    assert svc._t6_authoritative(bt) is True


def test_a_t6_blink_keeps_the_station_witness(tmp_path):
    """End to end: a verified WITNESS plane stays WITNESS through a
    one-minute T6 blink, and the summary counts the absent read."""
    svc = _service(tmp_path)
    say = _auth(tmp_path, svc)
    say("T6")
    for k in (0, 1):
        svc.apply_registration(label_timing(T0 + 60 * k, 0.0, SR), _ticks(k),
                               1_000_000 + k * 60 * SR, MIN + 60 * k, _meta(k, 0.0))
    assert svc.reg_store.read_summary()["state"] == "WITNESS"
    say("T4")
    svc.apply_registration(label_timing(T0 + 120, 0.0, SR), _ticks(2),
                           1_000_000 + 120 * SR, MIN + 120, _meta(2, 0.0))
    s = svc.reg_store.read_summary()
    assert s["state"] == "WITNESS"
    assert s["t6_absent_reads"] == 1
