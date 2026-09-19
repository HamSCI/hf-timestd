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
        "disambig_dump:",            # the per-value DISAMBIG dump
        # The third of the original trio, "disambig_no_authority", was
        # RETIRED with the reference gate it announced: acquisition no
        # longer asks a non-T6 tier for the sub-second term, so there is
        # no "no usable authority" condition left to report.  Its two
        # successors on the same per-cycle path are guarded here instead
        # -- the surface moved, the flood risk did not.
        "ordinal_unnamed",           # the cascade named no integer second
        "battery_refused",           # the self-consistency battery failed
    ])
    def test_site_is_throttled(self, key):
        assert key in self._src(), (
            f"the {key} surface is no longer routed through _t6_say_once; "
            f"it logs from a per-cycle path and the condition persists")

    def test_the_dump_key_is_QUANTISED(self):
        """A disambiguation that MOVES is news; jitter is not.

        ⛔ Keyed on raw nanoseconds first, which defeated the throttle:
        DASI-009's value jitters ~100 ns cycle to cycle (443218595,
        443218624, 443218668, 443218698), so every cycle minted a fresh key
        and the fix took 12 lines/min only to 8. A wrap error is half a
        second; nothing finer than a microsecond deserves an instant line.
        """
        src = self._src()
        assert "disambig_dump:{result.chain_delay_ns // 1000}" in src, (
            "the DISAMBIG dump key is not quantised; nanosecond jitter will "
            "mint a fresh key every cycle and defeat the throttle")

    def test_jitter_within_the_quantum_shares_one_key(self, rec):
        """The behaviour the quantisation buys, at the helper level."""
        base = 443_218_595
        spoke = sum(rec._t6_say_once(f"d:{(base + n) // 1000}")
                    for n in (0, 29, 73, 103))
        assert spoke == 1, f"{spoke} keys for 103 ns of jitter, want 1"

    def test_a_real_move_still_speaks_at_once(self, rec):
        """Quantising must not mask a genuine step."""
        assert rec._t6_say_once(f"d:{443_218_595 // 1000}") is True
        assert rec._t6_say_once(f"d:{444_000_000 // 1000}") is True
