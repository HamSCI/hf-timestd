#!/usr/bin/env python3
"""
Unit tests for the M-M18 / M-M19 / M-M20 remediation in
``metrology_service.py``.

  * M-M18 — per-record HDF5 writes (50+/min/channel) replaced with
            ``write_measurements_batch`` calls.  Same heap-corruption
            risk the data contract called out for ``tick_phase``.
  * M-M19 — write failures upgraded from DEBUG to rate-limited
            WARNING via :py:meth:`MetrologyService._warn_write_failure`.
  * M-M20 — :py:meth:`MetrologyService._cleanup_processed_set` keys the
            horizon off the caller-supplied ring UTC, not
            :func:`time.time`.  In Fusion mode the OS clock can be
            hours adrift from the RTP-derived UTC.

These tests exercise the helpers directly rather than spinning up a
full RingBufferReader + IQ pipeline — the helpers carry the M-M
invariants on their own surface.
"""

from __future__ import annotations

import logging
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock


def _make_service(tmp_path: Path):
    """Construct a :class:`MetrologyService` with mocked engine/writers.

    Imported inside the helper because instantiation requires a
    ka9q-python ring reader's existence on the path; bypass the heavy
    deps by short-circuiting ``__init__`` with a bare instance and
    setting just the attributes the tests touch.
    """
    from hf_timestd.core.metrology_service import MetrologyService
    svc = MetrologyService.__new__(MetrologyService)  # skip __init__
    svc.channel_name = "WWV_10000"
    svc.processed_minutes: set = set()
    svc._last_write_warn_ts: Dict[str, float] = {}
    svc._WRITE_WARN_INTERVAL_SEC = 60.0
    return svc


# ---------------------------------------------------------------------
# M-M20 — ring-time-keyed cleanup
# ---------------------------------------------------------------------

class TestCleanupProcessedSet(unittest.TestCase):
    def test_uses_now_utc_horizon_not_wall_clock(self, tmp_path=None):
        """If the OS clock is wildly off (Fusion mode), the horizon
        must be computed from the ring-derived UTC passed in."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            svc = _make_service(Path(td))
            # Ring time = 2026-05-19 12:00:00 UTC ≈ 1779_796_800
            ring_now = 1779_796_800.0
            # Populate with three minutes spanning the horizon:
            #   * 2 h ago: prune
            #   * 30 min ago: keep
            #   * "now": keep
            svc.processed_minutes.update({
                int(ring_now) - 7200,   # 2 h ago — must prune
                int(ring_now) - 1800,   # 30 min ago — must keep
                int(ring_now),          # now — must keep
            })
            svc._cleanup_processed_set(now_utc=ring_now)
            # Only the ≤1 h entries survive.
            self.assertNotIn(int(ring_now) - 7200, svc.processed_minutes)
            self.assertIn(int(ring_now) - 1800, svc.processed_minutes)
            self.assertIn(int(ring_now), svc.processed_minutes)

    def test_legacy_no_arg_falls_back_to_wall_clock(self, tmp_path=None):
        """Calling without ``now_utc`` keeps the legacy behaviour
        (uses :func:`time.time`) so older call sites and tests don't
        break."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            svc = _make_service(Path(td))
            now = time.time()
            svc.processed_minutes.update({
                int(now) - 10_000,  # well past horizon
                int(now),
            })
            svc._cleanup_processed_set()  # legacy form
            self.assertNotIn(int(now) - 10_000, svc.processed_minutes)
            self.assertIn(int(now), svc.processed_minutes)

    def test_unbounded_growth_when_os_clock_lags_ring_time(self, tmp_path=None):
        """The whole reason for M-M20: when OS clock is *behind* the
        ring UTC by hours (Fusion-mode bootstrap), the old wall-clock
        horizon would never reach the minutes already in the set, so
        nothing was pruned.  Passing ring time fixes that."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            svc = _make_service(Path(td))
            ring_now = 1779_796_800.0
            # Pretend the OS clock is two days behind the ring.
            wall_now = ring_now - 2 * 86400
            # 100 old minutes that the legacy wall-clock horizon would
            # never reach (they're all "in the future" relative to wall_now).
            for k in range(100):
                svc.processed_minutes.add(int(ring_now) - 7200 - 60 * k)

            # Legacy form (wall clock): would NOT prune anything because
            # every entry is in the wall-clock future.  We can't easily
            # monkeypatch time.time mid-test, so instead pin the M-M20
            # behaviour directly: ring-time-keyed cleanup *does* prune.
            svc._cleanup_processed_set(now_utc=ring_now)
            # Every entry was ≥ 2 h before ring_now → must all be gone.
            self.assertEqual(len(svc.processed_minutes), 0)


# ---------------------------------------------------------------------
# M-M19 — rate-limited write-failure warning
# ---------------------------------------------------------------------

class TestRateLimitedWarning(unittest.TestCase):
    def test_first_failure_warns_at_warning_level(self, tmp_path=None):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            svc = _make_service(Path(td))
            with self.assertLogs("hf_timestd", level="WARNING") as cm:
                svc._warn_write_failure("detection_attempts", RuntimeError("boom"))
            # Must include the product name and the exception message.
            self.assertTrue(any(
                "detection_attempts" in r.message and "boom" in r.message
                and r.levelno == logging.WARNING
                for r in cm.records
            ))

    def test_repeated_failures_within_interval_are_suppressed(self, tmp_path=None):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            svc = _make_service(Path(td))
            # Burst 5 failures.  Only the first should emit; the rest
            # are counted into the "suppressed" tally for the next
            # un-suppressed emission.
            for _ in range(5):
                svc._warn_write_failure("all_arrivals", RuntimeError("backend down"))

            # Move past the interval and warn again — must now report
            # the suppressed count.
            svc._last_write_warn_ts["all_arrivals"] = (
                time.time() - svc._WRITE_WARN_INTERVAL_SEC - 1
            )
            with self.assertLogs("hf_timestd", level="WARNING") as cm:
                svc._warn_write_failure("all_arrivals", RuntimeError("still down"))
            self.assertTrue(any(
                "suppressed 4" in r.message for r in cm.records
            ))

    def test_different_products_rate_limit_independently(self, tmp_path=None):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            svc = _make_service(Path(td))
            with self.assertLogs("hf_timestd", level="WARNING") as cm:
                svc._warn_write_failure("detection_attempts", RuntimeError("a"))
                svc._warn_write_failure("all_arrivals", RuntimeError("b"))
            # Both products' first failures emit (independent rate-limits).
            self.assertTrue(any("detection_attempts" in r.message for r in cm.records))
            self.assertTrue(any("all_arrivals" in r.message for r in cm.records))


# ---------------------------------------------------------------------
# M-M18 — batched writes
# ---------------------------------------------------------------------

class _RecordingWriter:
    """Captures whether write_measurement or write_measurements_batch
    was called, and with what payload."""

    def __init__(self):
        self.per_record_calls: int = 0
        self.batches: List[List[Dict[str, Any]]] = []

    def write_measurement(self, rec):  # noqa: D401
        self.per_record_calls += 1

    def write_measurements_batch(self, recs):  # noqa: D401
        self.batches.append(list(recs))


class TestBatchedWrites(unittest.TestCase):
    """Pins M-M18 by exercising the source-of-truth invariants on the
    metrology_service.py text: every all_arrivals / detection_attempts
    write site routes through ``write_measurements_batch``, and the
    error handler calls the rate-limited helper rather than
    ``logger.debug``.
    """

    def test_source_uses_batch_writers_for_high_volume_products(self):
        from hf_timestd.core import metrology_service
        src = Path(metrology_service.__file__).read_text()

        # No per-record write_measurement(... attempt ...) loop survives
        # at the M-M18 sites.  We assert that the new structures exist:
        self.assertIn(
            "self.attempts_writer.write_measurements_batch(attempt_batch)",
            src,
        )
        self.assertIn(
            "self.all_arrivals_writer.write_measurements_batch(arrival_batch)",
            src,
        )
        self.assertIn(
            "self.all_arrivals_writer.write_measurements_batch(edge_batch)",
            src,
        )

    def test_source_routes_write_failures_through_rate_limit_helper(self):
        from hf_timestd.core import metrology_service
        src = Path(metrology_service.__file__).read_text()

        # The contract requires WARNING-level for these.  No DEBUG
        # "Failed to write" lines survive on the data-product write sites.
        self.assertIn('self._warn_write_failure("detection_attempts"', src)
        self.assertIn('self._warn_write_failure("all_arrivals"', src)
        self.assertIn('self._warn_write_failure("tick_phase"', src)
        self.assertNotIn('logger.debug(f"Failed to write attempt record', src)
        self.assertNotIn('logger.debug(f"Failed to write all_arrivals record', src)
        self.assertNotIn('logger.debug(f"Failed to write edge tick record', src)
        self.assertNotIn('logger.debug(f"Failed to write CLEAN record', src)
        self.assertNotIn('logger.debug(f"Failed to write tick phase batch', src)


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
