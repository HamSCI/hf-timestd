#!/usr/bin/env python3
"""Unit tests for ChronyRefclockGate."""

import subprocess
import unittest
from dataclasses import dataclass
from typing import List, Tuple

from hf_timestd.core.chrony_refclock_gate import ChronyRefclockGate


@dataclass
class _FakeCompleted:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


def _record_runner(rc=0, stderr=""):
    """Runner that records the commands it was asked to run."""
    calls: List[Tuple[str, ...]] = []

    def _run(cmd, capture_output=False, text=False, timeout=None, check=False):
        calls.append(tuple(cmd))
        return _FakeCompleted(returncode=rc, stderr=stderr)

    return _run, calls


def _raising_runner(exc):
    def _run(cmd, capture_output=False, text=False, timeout=None, check=False):
        raise exc
    return _run


class TestChronyRefclockGate(unittest.TestCase):
    # ----- transitions -----

    def test_first_apply_at_t3_enables_refclock(self) -> None:
        run, calls = _record_runner()
        gate = ChronyRefclockGate(refid="HFSN", runner=run)
        result = gate.apply("T3")
        self.assertEqual(result.target_state, "enabled")
        self.assertTrue(result.applied)
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0][-3:], ("selectopts", "HFSN", "-noselect"),
            f"unexpected command: {calls[0]}",
        )

    def test_first_apply_at_t4_disables_refclock(self) -> None:
        run, calls = _record_runner()
        gate = ChronyRefclockGate(refid="HFSN", runner=run)
        result = gate.apply("T4")
        self.assertEqual(result.target_state, "disabled")
        self.assertTrue(result.applied)
        self.assertEqual(calls[0][-3:], ("selectopts", "HFSN", "+noselect"))

    def test_first_apply_at_none_disables_refclock(self) -> None:
        run, calls = _record_runner()
        gate = ChronyRefclockGate(refid="HFSN", runner=run)
        result = gate.apply(None)
        self.assertEqual(result.target_state, "disabled")
        self.assertTrue(result.applied)

    def test_steady_state_makes_no_subprocess_calls(self) -> None:
        run, calls = _record_runner()
        gate = ChronyRefclockGate(refid="HFSN", runner=run)
        gate.apply("T3")  # applied
        for _ in range(5):
            r = gate.apply("T3")
            self.assertFalse(r.applied)
            self.assertEqual(r.reason, "no change")
        self.assertEqual(len(calls), 1)

    def test_transition_from_t3_to_t4_disables(self) -> None:
        run, calls = _record_runner()
        gate = ChronyRefclockGate(refid="HFSN", runner=run)
        gate.apply("T3")
        result = gate.apply("T4")
        self.assertEqual(result.target_state, "disabled")
        self.assertTrue(result.applied)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][-1], "+noselect")

    def test_t6_also_enables(self) -> None:
        run, calls = _record_runner()
        gate = ChronyRefclockGate(refid="HFSN", runner=run)
        result = gate.apply("T6")
        self.assertTrue(result.applied)
        self.assertEqual(calls[0][-1], "-noselect")

    # ----- failure handling -----

    def test_chronyc_missing_returns_unapplied_with_reason(self) -> None:
        gate = ChronyRefclockGate(
            refid="HFSN",
            runner=_raising_runner(FileNotFoundError()),
        )
        result = gate.apply("T3")
        self.assertFalse(result.applied)
        self.assertIn("chronyc not found", result.reason)
        # Failed apply must NOT latch the state — next call re-tries.
        result2 = gate.apply("T3")
        self.assertFalse(result2.applied)

    def test_permission_denied_is_not_latched(self) -> None:
        run, calls = _record_runner(rc=1, stderr="500 Not authorised")
        gate = ChronyRefclockGate(refid="HFSN", runner=run)
        r1 = gate.apply("T3")
        self.assertFalse(r1.applied)
        self.assertIn("Not authorised", r1.reason)
        # Still disabled state — next transition attempt is made.
        r2 = gate.apply("T3")
        self.assertFalse(r2.applied)
        # Both calls were attempted (state did not latch on failure).
        self.assertEqual(len(calls), 2)

    def test_chronyc_timeout(self) -> None:
        gate = ChronyRefclockGate(
            refid="HFSN",
            runner=_raising_runner(subprocess.TimeoutExpired(cmd="chronyc", timeout=5)),
        )
        result = gate.apply("T3")
        self.assertFalse(result.applied)
        self.assertIn("timeout", result.reason)

    def test_dry_run_latches_state_without_calling_chronyc(self) -> None:
        def _explode(*a, **kw):
            raise AssertionError("subprocess should not be called in dry_run")
        gate = ChronyRefclockGate(refid="HFSN", dry_run=True, runner=_explode)
        r1 = gate.apply("T3")
        self.assertEqual(r1.target_state, "enabled")
        self.assertFalse(r1.applied)   # dry_run reports applied=False but DOES latch
        self.assertIn("dry_run", r1.reason)
        r2 = gate.apply("T3")
        self.assertEqual(r2.reason, "no change")  # proves dry_run latched


if __name__ == "__main__":
    unittest.main()
