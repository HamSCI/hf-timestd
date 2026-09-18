"""The estimate_stale warning is throttled; staleness persists.

⛔ Measured on DASI-009.AI6VN 2026-09-18: **3,105 lines per minute**, 57,823
in twenty minutes, and the journal climbed 185 MB → 376 MB in the two hours
after a vacuum.

`_check_estimate_liveness` runs per batch, and the staleness condition
(`now - last_estimate_at > stale_after`) stays true for as long as no
estimate is accepted — so the warning fired on every batch, ~50/s at 96 kHz.

The cost is not disk. It is that this station's diagnostics were buried
under it while we were reading them. Third instance of the same defect class
in one codebase: a71781a (radiod health, 8,640/day), 93293c1 (the T6 lock
message, 99,127 per 30 min), and this.

Throttled rather than edge-triggered, mirroring `_note_acquiring_violation`:
the diagnosis text changes as counters advance, so a periodic restatement
earns its place — one per batch does not.
"""
from __future__ import annotations

import logging

import pytest

from hf_timestd.core.t6_anchor_authority import (
    ACQUIRING_WARN_PERIOD_SEC,
    T6AnchorAuthority,
    T6AuthorityState,
)


SR = 96000
BUDGET = 200


@pytest.fixture
def auth():
    """An authority sitting in DEGRADED with a stale estimate."""
    a = T6AnchorAuthority(SR, BUDGET)
    a._state = T6AuthorityState.DEGRADED
    return a


def _liveness(auth, expected=1.0):
    """`on_tick` is where the staleness check and its warning live."""
    return auth.on_tick(expected)


class TestThrottling:
    def test_many_batches_in_one_period_warn_ONCE(self, auth, caplog):
        """The live defect: ~50 batches per second, each one a warning."""
        clock = {"t": 1000.0}
        auth._now = lambda: clock["t"]
        auth._last_estimate_at = 0.0
        seen = 0
        with caplog.at_level(logging.WARNING,
                             logger="hf_timestd.core.t6_anchor_authority"):
            for _ in range(500):                   # 10 s of batches at 50/s
                auth._degraded_since = clock["t"]
                _liveness(auth)
                clock["t"] += 0.02
            seen = sum(1 for r in caplog.records
                       if "estimate_stale" in r.getMessage())
        assert seen == 1, (
            f"500 batches inside one throttle window produced {seen} "
            f"warnings; the staleness condition persists, so this must "
            f"emit once per {ACQUIRING_WARN_PERIOD_SEC:.0f} s, not per batch")

    def test_it_speaks_again_after_the_period(self, auth, caplog):
        """Throttled, not silenced — a persistent fault must keep saying so."""
        clock = {"t": 1000.0}
        auth._now = lambda: clock["t"]
        auth._last_estimate_at = 0.0
        with caplog.at_level(logging.WARNING,
                             logger="hf_timestd.core.t6_anchor_authority"):
            for _ in range(4):
                auth._degraded_since = clock["t"]
                _liveness(auth)
                clock["t"] += ACQUIRING_WARN_PERIOD_SEC + 1.0
            seen = sum(1 for r in caplog.records
                       if "estimate_stale" in r.getMessage())
        assert seen == 4, f"expected one warning per period, got {seen}"

    def test_the_message_says_it_is_throttled(self, auth, caplog):
        """An operator must not read one line per 5 min as one event per 5 min."""
        auth._now = lambda: 1000.0
        auth._last_estimate_at = 0.0
        auth._degraded_since = 1000.0
        with caplog.at_level(logging.WARNING,
                             logger="hf_timestd.core.t6_anchor_authority"):
            _liveness(auth)
        msgs = [r.getMessage() for r in caplog.records
                if "estimate_stale" in r.getMessage()]
        assert msgs and "Repeated at most" in msgs[0], (
            "the throttled message must say it is throttled")


class TestTheSourceIsThrottled:
    def _block(self):
        from pathlib import Path
        src = (Path(__file__).parents[2] / "src" / "hf_timestd" / "core"
               / "t6_anchor_authority.py").read_text()
        i = src.index('"T6 estimate_stale:')
        return src[i - 1400:i + 200]

    def test_a_throttle_guards_the_warning(self):
        assert "_last_stale_warn_at" in self._block(), (
            "the estimate_stale warning has no throttle; the staleness "
            "condition persists, so it will fire on every batch (~50/s)")
