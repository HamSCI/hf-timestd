"""Task A7: one admission gate per process, read from ``authority.json``.

The fusion service's authority manager decides T3's admission each
minute and publishes it as ``authority.json``'s ``t3_admission`` block.
Every other process (the recorder, each metrology channel) follows that
decision through one :class:`T3AdmissionGate`.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import pytest

from hf_timestd.core.t3_admission import AdmissionMode
from hf_timestd.core.t3_admission_gate import T3AdmissionGate

WALL0 = 1_800_000_000.0


class _Mono:
    def __init__(self):
        self.t = 5000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def _block(admitted, reason):
    return {
        "mode": "auto", "admitted": admitted, "route": None, "reason": reason,
        "reference": None, "n_pairs": 0, "t3_median_ms": None, "t3_p95_ms": None,
        "t2_median_ms": None, "t2_p95_ms": None,
        "stability": {"tau_cross_s": 600, "window_s": 2400, "hold_s": 600,
                      "drift_ns_per_s": 1.5, "n_points": 300},
        "since_utc_minute": None, "transitions": 0,
    }


def _write(tmp_path, *, admitted, published, reason="admitted: T3 beats T2"):
    (tmp_path / "authority.json").write_text(json.dumps({
        "schema_version": 1,
        "utc_published": published,
        "t_level_active": "T2",
        "t3_admission": _block(admitted, reason),
    }))


def _gate(tmp_path, mode, clock):
    return T3AdmissionGate(mode, tmp_path / "authority.json", mono_fn=clock)


def test_gate_closes_on_missing_stale_or_malformed_file(tmp_path):
    clock = _Mono()
    g = _gate(tmp_path, AdmissionMode.AUTO, clock)
    g.refresh(); assert g.admitted() is False                       # missing
    assert "no fresh admission decision" in g.reason()
    _write(tmp_path, admitted=True, published="t0"); g.refresh()
    assert g.admitted() is False                                    # first sight: not yet fresh
    clock.advance(30)
    _write(tmp_path, admitted=True, published="t1"); g.refresh()
    assert g.admitted() is True
    assert g.reason() == "admitted: T3 beats T2"
    clock.advance(121); g.refresh(); assert g.admitted() is False   # stale
    clock.advance(30)
    _write(tmp_path, admitted=True, published="t2"); g.refresh()
    assert g.admitted() is True                                     # a new write: fresh again
    (tmp_path / "authority.json").write_text("{not json"); g.refresh()
    assert g.admitted() is False                                    # malformed, no raise
    assert g.clock() is None


def test_a_block_without_the_decision_reads_closed(tmp_path):
    """An authority.json from before Task A6 carries no t3_admission."""
    clock = _Mono()
    g = _gate(tmp_path, AdmissionMode.AUTO, clock)
    for stamp in ("t0", "t1"):
        (tmp_path / "authority.json").write_text(
            json.dumps({"utc_published": stamp, "t_level_active": "T2"}))
        g.refresh()
        clock.advance(30)
    assert g.admitted() is False


def test_staleness_needs_no_refresh(tmp_path):
    """A process whose refresh stops calling still closes on time."""
    clock = _Mono()
    g = _gate(tmp_path, AdmissionMode.AUTO, clock)
    for stamp in ("t0", "t1"):
        _write(tmp_path, admitted=True, published=stamp); g.refresh()
    assert g.admitted() is True
    clock.advance(121)
    assert g.admitted() is False


def test_on_follows_a_fresh_withdrawal_and_holds_otherwise(tmp_path):
    clock = _Mono()
    g = _gate(tmp_path, AdmissionMode.ON, clock)
    g.refresh(); assert g.admitted() is True
    assert "forced" in g.reason()
    for stamp in ("t0", "t1"):
        _write(tmp_path, admitted=False, published=stamp,
               reason="withdrawn: T3 jumped 700 ms")
        g.refresh()
        clock.advance(30)
    assert g.admitted() is False and g.reason() == "withdrawn: T3 jumped 700 ms"
    clock.advance(200); g.refresh()
    assert g.admitted() is True                                     # stale: forced again


def test_off_ignores_the_file(tmp_path):
    clock = _Mono()
    g = _gate(tmp_path, AdmissionMode.OFF, clock)
    for stamp in ("t0", "t1", "t2"):
        _write(tmp_path, admitted=True, published=stamp); g.refresh()
        clock.advance(30)
    assert g.admitted() is False
    assert g.reason() == 'off ([timing.t3] admission = "off")'
    assert g.clock() is None


def test_admitted_reads_no_file(tmp_path, monkeypatch):
    clock = _Mono()
    g = _gate(tmp_path, AdmissionMode.AUTO, clock)
    for stamp in ("t0", "t1"):
        _write(tmp_path, admitted=True, published=stamp); g.refresh()

    def boom(*a, **k):
        raise AssertionError("admitted() touched the file")

    monkeypatch.setattr(Path, "read_text", boom)
    monkeypatch.setattr(Path, "read_bytes", boom)
    monkeypatch.setattr("builtins.open", boom)
    assert g.admitted() is True
    assert g.reason() == "admitted: T3 beats T2"
    stab = g.clock()
    assert stab is not None and stab.window_s == 2400 and stab.hold_s == 600
    # refresh() itself never raises, even when reading fails.
    g.refresh()
    assert g.admitted() is False


def test_failures_log_once_per_streak(tmp_path, caplog):
    clock = _Mono()
    g = _gate(tmp_path, AdmissionMode.AUTO, clock)
    with caplog.at_level(logging.INFO, logger="hf_timestd.core.t3_admission_gate"):
        for _ in range(5):
            g.refresh()
        missing = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(missing) == 1
        caplog.clear()
        for _ in range(5):
            (tmp_path / "authority.json").write_text("{not json")
            g.refresh()
        assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 1


def test_fixed_gates(tmp_path):
    assert T3AdmissionGate.fixed(True).admitted() is True
    assert T3AdmissionGate.fixed(False).admitted() is False
    g = T3AdmissionGate.fixed(True)
    g.refresh()
    assert g.admitted() is True


# ── callers ──────────────────────────────────────────────────────────


def test_metrology_follows_the_gate_minute_by_minute(tmp_path):
    from tests.unit.test_metrology_service_registration import T0, _minute
    from tests.unit.test_t3_admission import _metrology

    clock = _Mono()
    svc = _metrology(tmp_path)
    svc._t3_gate = T3AdmissionGate(AdmissionMode.AUTO, tmp_path / "authority.json",
                                   mono_fn=clock)
    for k in range(4):
        _write(tmp_path, admitted=False, published=f"m{k}",
               reason="withheld: filling the window")
        bt = _minute(svc, k, walk=0.250)
        clock.advance(60)
    assert svc.acquirer.registration is not None
    assert svc.acquirer.registration.verified is True
    assert bt.origin_source == "label"
    _write(tmp_path, admitted=True, published="m4")
    bt = _minute(svc, 4, walk=0.250)
    assert bt.origin_source == "acquired"
    assert bt.judge_tier == "T3"
    assert bt.sample0_utc == pytest.approx(T0 + 240, abs=0.002)


def test_the_recorder_anchor_follows_the_gate(tmp_path, monkeypatch):
    from hf_timestd.core.core_recorder_v2 import CoreRecorderV2
    from hf_timestd.core.registration_acquirer import Registration
    from hf_timestd.core.registration_store import (
        ADOPT_MIN_CORROBORATED_MINUTES,
        RegistrationStore,
    )
    import hf_timestd.core.registration_store as rs

    now = time.time()
    store = RegistrationStore(tmp_path / "reg", tmp_path / "registration.json",
                              time_fn=lambda: now)
    store.write_summary(
        Registration("ep-1", rtp_ref=1000, utc_ref=now, sample_rate=24000,
                     sigma_ms=1.0, n_minutes=ADOPT_MIN_CORROBORATED_MINUTES,
                     channel="fused", rtp_domain="SHARED_10000", verified=True),
        ["SHARED_10000"], "ACQUIRED", {"counter_epoch_id": "ep-1"})
    monkeypatch.setattr(rs, "RegistrationStore", lambda *a, **k: store)

    clock = _Mono()
    rec = CoreRecorderV2.__new__(CoreRecorderV2)
    rec._t6_native_anchor = None
    rec._t6_authority_status = lambda: None
    rec._t3_native_anchor = None
    rec._t3_anchor_holder = None
    rec._anchor_closure = True
    rec._t3_gate = T3AdmissionGate(AdmissionMode.AUTO, tmp_path / "authority.json",
                                   mono_fn=clock)

    _write(tmp_path, admitted=True, published="m0")
    rec._refresh_t3_native_anchor()
    assert rec._t3_native_anchor is None                 # first sight: not fresh
    clock.advance(60)
    _write(tmp_path, admitted=True, published="m1")
    rec._refresh_t3_native_anchor()
    assert rec._t3_native_anchor is not None
    assert rec._t3_label_anchor_state().tier == "T3"
    clock.advance(60)
    _write(tmp_path, admitted=False, published="m2",
           reason="withdrawn: T3 jumped 700 ms")
    rec._refresh_t3_native_anchor()
    assert rec._t3_native_anchor is None
    assert rec._t3_anchor_holder.decision.reason == "withdrawn: T3 jumped 700 ms"
