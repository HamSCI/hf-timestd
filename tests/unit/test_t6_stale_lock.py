"""The lock itself must be re-validated against GPS, not just each candidate.

AC0G-B4 2026-08-25: the fine stage held ``_t6_last_chain_delay_ns`` =
225,754,278 ns while T5 implied ~149,000,000 ns -- contradicted by
76.8 ms, continuously, for hours.  Step-recovery correctly refused every
individual candidate (chain_delay is an RF-path constant, so a cluster
implying a different one is usually the matched filter's boxcar
sidelobe), but nothing in the state machine ever asked whether the LOCK
was still corroborated.  Reject -> clear recent_raw -> 60 more
rejections -> reject, at ~1 Hz, with no state that could converge.

The escape must not become a re-run of the 2026-05-23 phantom walk
(HPPS +216 ms), so it discriminates by MAGNITUDE: a contradiction too
large to be T5 noise but too small to be the ±0.5 s sidelobe.
"""
from __future__ import annotations

import pytest

from hf_timestd.core.t6_stale_lock import (
    SIDELOBE_NS,
    STALE_LOCK_BOUND_NS,
    STALE_LOCK_DWELL_S,
    StaleLockWatch,
    contradiction_is_escapable,
)


class TestBand:
    def test_b4_contradiction_is_escapable(self):
        """The observed pathology: 76.8 ms."""
        assert contradiction_is_escapable(-76_829_212)

    def test_t5_noise_is_not_escapable(self):
        """T5's own spread was ~6 ms across seconds; 5 ms is the existing
        per-candidate sanity threshold.  Neither may trip the escape."""
        assert not contradiction_is_escapable(5_000_000)
        assert not contradiction_is_escapable(-6_000_000)

    def test_the_sidelobe_is_never_escapable(self):
        """2026-05-23: the boxcar template's sidelobe sits at ±0.5 s.  A
        contradiction there means the CANDIDATE is phantom and the lock
        is good -- escaping would throw away a working lock."""
        assert not contradiction_is_escapable(SIDELOBE_NS)
        assert not contradiction_is_escapable(-SIDELOBE_NS)
        assert not contradiction_is_escapable(-216_000_000 - SIDELOBE_NS / 2)

    def test_band_edges(self):
        assert not contradiction_is_escapable(STALE_LOCK_BOUND_NS)
        assert contradiction_is_escapable(STALE_LOCK_BOUND_NS + 1_000_000)


class TestDwell:
    def _watch(self):
        return StaleLockWatch()

    def test_sustained_contradiction_escapes(self):
        w = self._watch()
        assert not w.observe(-76_829_212, now_s=0.0)
        assert not w.observe(-76_829_212, now_s=STALE_LOCK_DWELL_S - 1.0)
        assert w.observe(-76_829_212, now_s=STALE_LOCK_DWELL_S)

    def test_a_transient_does_not_escape(self):
        """Packet-loss bursts and fades must not trip it."""
        w = self._watch()
        w.observe(-76_829_212, now_s=0.0)
        w.observe(-76_829_212, now_s=30.0)
        # cleared -- back inside the bound
        assert not w.observe(1_000_000, now_s=31.0)
        # and the dwell restarts from scratch
        assert not w.observe(-76_829_212, now_s=32.0)
        assert not w.observe(-76_829_212, now_s=32.0 + STALE_LOCK_DWELL_S - 1)
        assert w.observe(-76_829_212, now_s=32.0 + STALE_LOCK_DWELL_S)

    def test_sidelobe_never_accumulates_dwell(self):
        w = self._watch()
        for t in range(0, int(STALE_LOCK_DWELL_S) * 3, 10):
            assert not w.observe(SIDELOBE_NS, now_s=float(t))

    def test_cooldown_prevents_immediate_refire(self):
        """After escaping, re-acquisition needs room; without a cooldown a
        lock that re-forms wrong would oscillate at the dwell period."""
        w = self._watch()
        w.observe(-76_829_212, now_s=0.0)
        assert w.observe(-76_829_212, now_s=STALE_LOCK_DWELL_S)
        # immediately contradicted again -- must not fire again yet
        assert not w.observe(-76_829_212, now_s=STALE_LOCK_DWELL_S + 1.0)
        assert not w.observe(
            -76_829_212, now_s=STALE_LOCK_DWELL_S * 2 - 1.0)

    def test_none_contradiction_is_inert(self):
        """T5 unavailable must neither escape nor accumulate dwell."""
        w = self._watch()
        assert not w.observe(None, now_s=0.0)
        assert not w.observe(None, now_s=STALE_LOCK_DWELL_S * 2)
        assert not w.observe(-76_829_212, now_s=STALE_LOCK_DWELL_S * 2)
