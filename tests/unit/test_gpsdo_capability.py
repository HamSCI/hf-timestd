"""T5 eligibility is DERIVED from what gpsdo-monitor reports, never typed.

Why this module exists (2026-09-16).  `[timing] lb1421_enabled` was a hand-set
boolean named after one model.  AC0G-ND carried it `true` for its entire life
against an LBE-Mini, which has no PPS at all, so the T5 probe reported
``enabled: true, valid_fix: false, "no reading yet"`` forever and every surface
read that as "T5 is on".  Meanwhile gpsdo-monitor was already publishing the
discriminator on both stations:

    B4   lbe-1421   pps_study {enabled: True,  edges: 60, period_ms_p50: 1000.084}
    ND   lbe-mini   pps_study {enabled: False, edges: 0}

So the capability is knowable without asking the operator.  A GPSDO always
disciplines the ADC (the A axis); only some of them can place a second (the T
axis), and only those are T5 sources.
"""
from __future__ import annotations

import pytest

from hf_timestd.core.gpsdo_capability import (
    T5Capability, resolve_t5_capability, t5_enabled_from_config,
)

NOW = 1_000_000.0


def _iso(epoch):
    """gpsdo-monitor's real `written_utc` spelling: ISO-8601 with a Z."""
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.") + f"{int(epoch % 1 * 1000):03d}Z"


def _doc(*, model, pps_enabled, edges, fix="3D", written=None, reason=None):
    """A gpsdo-monitor per-device document.

    ⚠ CAPTURED from a live station, not invented. `written_utc` is an ISO-8601
    STRING. The first cut of these tests used a float, the code matched the
    fixture, and every REAL document then parsed as "age unknown" and was
    rejected as stale. Deploying is what found it.
    """
    if written is None:
        written = _iso(NOW - 5.0)
    if reason is None:
        reason = ("pll_locked && gps_fix=3D && antenna_ok && pps_present && fresh"
                  if pps_enabled else "pll_locked && gps_fix=3D && fresh")
    return {
        "schema": 1,
        "written_utc": written,
        "device": {"model": model, "serial": "TEST123"},
        "health": {"gps_fix": fix, "pll_locked": True},
        "a_level_hint": "A1",
        "a_level_reason": reason,
        "pps_study": {"enabled": pps_enabled, "window_sec": 60, "edges": edges,
                      "period_ms_p50": 1000.08 if edges else None},
    }


def _b4():   return _doc(model="lbe-1421", pps_enabled=True,  edges=60)
def _mini(): return _doc(model="lbe-mini", pps_enabled=False, edges=0)


class TestDerivedFromTheProbe:
    def test_a_pps_capable_device_is_a_t5_source(self):
        cap = resolve_t5_capability(_b4(), now=NOW)
        assert cap.available is True
        assert cap.model == "lbe-1421"

    def test_a_device_with_no_pps_is_not_a_t5_source(self):
        """The ND/AI6VN case. A Mini disciplines the ADC but cannot place a second."""
        cap = resolve_t5_capability(_mini(), now=NOW)
        assert cap.available is False
        assert "no pps" in cap.reason.lower()

    def test_a_device_with_no_pps_can_still_NAME_a_second(self):
        """The Mini streams UBX time-of-day, so it can name a second even though
        it cannot place one. That is not T5, and must not be reported as T5 --
        but it is exactly what T6 needs for second-of-day disambiguation."""
        cap = resolve_t5_capability(_mini(), now=NOW)
        assert cap.available is False
        assert cap.names_second is True

    def test_pps_enabled_but_zero_edges_is_not_available(self):
        """`enabled` is a setting; `edges` is evidence. Trust the evidence --
        the whole bug being fixed here was a setting believed over a measurement."""
        cap = resolve_t5_capability(
            _doc(model="lbe-1421", pps_enabled=True, edges=0), now=NOW)
        assert cap.available is False

    def test_a_stale_document_is_not_available(self):
        """gpsdo-monitor stalled. An old yes must never read as a current yes."""
        cap = resolve_t5_capability(
            _doc(model="lbe-1421", pps_enabled=True, edges=60, written=_iso(NOW - 3600)),
            now=NOW)
        assert cap.available is False
        assert "stale" in cap.reason.lower()

    def test_a_missing_document_is_not_available_and_says_so(self):
        cap = resolve_t5_capability(None, now=NOW)
        assert cap.available is False
        assert cap.reason


class TestConfigResolution:
    def test_unset_config_follows_the_probe(self):
        assert t5_enabled_from_config({}, resolve_t5_capability(_b4(), now=NOW))[0] is True
        assert t5_enabled_from_config({}, resolve_t5_capability(_mini(), now=NOW))[0] is False

    def test_explicit_off_beats_a_capable_device(self):
        """An operator disabling T5 on a capable station is a legitimate choice."""
        on, _ = t5_enabled_from_config({"t5_enabled": False},
                                       resolve_t5_capability(_b4(), now=NOW))
        assert on is False

    def test_the_legacy_spelling_is_still_honoured(self):
        """`lb1421_enabled` is deployed on live stations; it must keep working."""
        on, _ = t5_enabled_from_config({"lb1421_enabled": False},
                                       resolve_t5_capability(_b4(), now=NOW))
        assert on is False

    def test_forcing_t5_on_a_pps_less_device_is_REFUSED_not_obeyed(self):
        """ND's actual state for its whole life. Obeying it produced a T5 that
        reported enabled and never yielded a reading; refusing it says why."""
        on, why = t5_enabled_from_config({"t5_enabled": True},
                                         resolve_t5_capability(_mini(), now=NOW))
        assert on is False
        assert "no pps" in why.lower()

    def test_forcing_it_on_with_no_probe_at_all_is_allowed(self):
        """No gpsdo-monitor is not evidence of no PPS. An operator who says the
        device is there gets to be believed -- refusing here would break any
        station without the probe running."""
        on, _ = t5_enabled_from_config({"t5_enabled": True},
                                       resolve_t5_capability(None, now=NOW))
        assert on is True


class TestBothEntryPointsAgree:
    """The systemd unit runs `python -m hf_timestd.core.core_recorder_v2`, NOT
    `hf-timestd daemon`. The T5 enable logic lived in BOTH, and on 2026-09-16 a
    fix applied only to cli.py was deployed, restarted, and changed nothing —
    because the unit never calls the CLI. Lock it: neither entry point may
    re-derive the switch for itself.
    """

    def _src(self, rel):
        from pathlib import Path as P
        return (P(__file__).parents[2] / "src" / "hf_timestd" / rel).read_text()

    def test_neither_entry_point_rederives_the_boolean(self):
        for rel in ("cli.py", "core/core_recorder_v2.py"):
            src = self._src(rel)
            assert "resolve_t5_for_config" in src, f"{rel} must use the resolver"
            assert "timing_section.get('lb1421_enabled'" not in src, (
                f"{rel} re-derives the T5 switch instead of asking the resolver")

    def test_the_resolver_returns_everything_a_caller_needs(self):
        """So no caller has a reason to reach for the config keys itself."""
        from hf_timestd.core.gpsdo_capability import resolve_t5_for_config
        enabled, why, cap, run_dir, serial = resolve_t5_for_config(
            {"lb1421_gpsdo_run_dir": "/nonexistent-for-test"})
        assert enabled is False and why
        assert cap.probe_present is False       # absence is not a refusal
        assert str(run_dir) == "/nonexistent-for-test"


class TestTheRealWireFormat:
    """Regression: gpsdo-monitor writes an ISO-8601 string."""

    def test_iso8601_written_utc_is_understood(self):
        cap = resolve_t5_capability(
            _doc(model="lbe-1421", pps_enabled=True, edges=60,
                 written="2026-09-17T00:07:34.025Z"),
            now=__import__("datetime").datetime(
                2026, 9, 17, 0, 7, 40,
                tzinfo=__import__("datetime").timezone.utc).timestamp())
        assert cap.available is True, cap.reason

    def test_a_numeric_epoch_still_works(self):
        cap = resolve_t5_capability(
            _doc(model="lbe-1421", pps_enabled=True, edges=60, written=NOW - 5.0),
            now=NOW)
        assert cap.available is True, cap.reason

    def test_an_unparseable_stamp_is_stale_not_fresh(self):
        cap = resolve_t5_capability(
            _doc(model="lbe-1421", pps_enabled=True, edges=60, written="not-a-date"),
            now=NOW)
        assert cap.available is False and "stale" in cap.reason.lower()
