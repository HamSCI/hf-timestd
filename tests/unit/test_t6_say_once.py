"""`_t6_say_once` — one throttle for persistent-condition logging.

⛔ FOURTH instance of one defect class, all surfaced on 2026-09-18 by reading
a station's journal:

    a71781a   radiod health                     8,640 / day
    93293c1   T6 BPSK PPS LOCKED               99,127 / 30 min
    0b454e3   T6 estimate_stale                 3,105 / min
    this      the three T6 DISAMBIG surfaces        3 / 5 s

The shape never varies: a condition that PERSISTS, logged from a path that
runs per batch or per cycle. And the cost was never disk — each flood buried
the diagnostics we were reading at the time, and each fix made the next
fault legible. The DISAMBIG trio is what hid, on DASI-009.AI6VN, the fact
that its chain delay had gone rock-stable at 443218771 ns after rob fixed
the reference cable.

This exists so the fifth one routes through a shared helper instead of a
fifth private timestamp.
"""
from __future__ import annotations

import pytest

from hf_timestd.core.core_recorder_v2 import CoreRecorderV2


@pytest.fixture
def rec():
    """A recorder without __init__ — the helper touches one dict."""
    return CoreRecorderV2.__new__(CoreRecorderV2)


class TestThrottle:
    def test_first_call_speaks(self, rec):
        assert rec._t6_say_once("k") is True

    def test_second_call_inside_the_period_is_silent(self, rec):
        rec._t6_say_once("k")
        assert rec._t6_say_once("k") is False

    def test_a_flood_speaks_exactly_once(self, rec):
        spoke = sum(rec._t6_say_once("k") for _ in range(1000))
        assert spoke == 1, f"1000 calls produced {spoke} messages, want 1"

    def test_it_speaks_again_after_the_period(self, rec, monkeypatch):
        import hf_timestd.core.core_recorder_v2 as m
        t = {"v": 1000.0}
        monkeypatch.setattr(m.time, "monotonic", lambda: t["v"])
        assert rec._t6_say_once("k") is True
        t["v"] += rec.T6_REPEAT_PERIOD_SEC + 1
        assert rec._t6_say_once("k") is True, "throttled, not silenced"

    def test_distinct_keys_are_independent(self, rec):
        """A changing value is news and must not be masked by a stable one."""
        assert rec._t6_say_once("a") is True
        assert rec._t6_say_once("b") is True

    def test_an_explicit_period_is_honoured(self, rec, monkeypatch):
        import hf_timestd.core.core_recorder_v2 as m
        t = {"v": 0.0}
        monkeypatch.setattr(m.time, "monotonic", lambda: t["v"])
        assert rec._t6_say_once("k", period=10.0) is True
        t["v"] = 5.0
        assert rec._t6_say_once("k", period=10.0) is False
        t["v"] = 11.0
        assert rec._t6_say_once("k", period=10.0) is True

    def test_it_works_on_an_instance_that_never_ran_init(self, rec):
        """Several call sites are reached from paths unit tests construct via
        __new__; the helper must not assume __init__ created its state."""
        assert not hasattr(rec, "_t6_say_once_at")
        assert rec._t6_say_once("k") is True
        assert hasattr(rec, "_t6_say_once_at")


class TestTheSpammingSitesAreGated:
    """Guard the three surfaces that were flooding DASI-009."""

    def _src(self):
        from pathlib import Path
        return (Path(__file__).parents[2] / "src" / "hf_timestd" / "core"
                / "core_recorder_v2.py").read_text()

    @pytest.mark.parametrize("key", [
        "disambig_t4_gate",          # "T4 sigma exceeds gate"
        "disambig_no_authority",     # "no usable non-T6 timing authority"
        "disambig_dump:",            # the per-value DISAMBIG dump
    ])
    def test_site_is_throttled(self, key):
        assert key in self._src(), (
            f"the {key} surface is no longer routed through _t6_say_once; "
            f"it logs from a per-cycle path and the condition persists")

    def test_the_dump_is_keyed_on_the_VALUE(self):
        """A changing disambiguation is news; a stable one is not. Keying on
        the raw value prints immediately when it moves and throttles when it
        does not — which is what made AI6VN's nanosecond-stable value
        visible instead of drowning."""
        assert "disambig_dump:{result.chain_delay_ns}" in self._src()
