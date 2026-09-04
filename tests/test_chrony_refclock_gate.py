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


class TestHostClockWithdrawal(unittest.TestCase):
    """Step 0.5 (HOST_CLOCK_INTEGRITY.md): the verdict withdraws FUSE.

    2026-09-04: FUSE measured the clock chrony steered with it and both
    stations walked seconds off UTC while chrony reported 0.1 ms.  The
    verdict saw it from independent witnesses; the gate now acts on it."""

    def _gate(self, clear_sec=600.0, **kw):
        run, calls = _record_runner()
        self.t = 1000.0
        gate = ChronyRefclockGate(refid="FUSE", runner=run, chronyc_bin="/bin/chronyc",
                                  host_clock_clear_sec=clear_sec,
                                  now_fn=lambda: self.t, **kw)
        return gate, calls

    def test_fault_withdraws_even_at_t6(self) -> None:
        gate, calls = self._gate()
        gate.apply("T6", "ok")
        r = gate.apply("T6", "fault")
        self.assertEqual(r.target_state, "disabled")
        self.assertTrue(r.applied)
        self.assertIn("host_clock:fault", r.reason)
        self.assertEqual(calls[-1], ("/bin/chronyc", "selectopts", "FUSE", "+noselect"))
        self.assertTrue(gate.host_clock_withdrawn)

    def test_suspect_withdraws_at_t3(self) -> None:
        gate, calls = self._gate()
        r = gate.apply("T3", "suspect")
        self.assertEqual(r.target_state, "disabled")
        self.assertIn("host_clock:suspect", r.reason)

    def test_ok_must_hold_for_clear_sec_before_reoffering(self) -> None:
        gate, calls = self._gate(clear_sec=600.0)
        gate.apply("T3", "fault")
        self.t += 60
        r = gate.apply("T3", "ok")
        self.assertEqual(r.target_state, "disabled")
        self.assertIn("host_clock:clearing", r.reason)
        self.t += 300
        r = gate.apply("T3", "ok")
        self.assertEqual(r.target_state, "disabled")
        self.t += 300  # 600 s of ok held
        r = gate.apply("T3", "ok")
        self.assertEqual(r.target_state, "enabled")
        self.assertTrue(r.applied)
        self.assertIn("host_clock:cleared", r.reason)
        self.assertEqual(calls[-1], ("/bin/chronyc", "selectopts", "FUSE", "-noselect"))
        self.assertFalse(gate.host_clock_withdrawn)

    def test_a_relapse_restarts_the_clear_timer(self) -> None:
        gate, _ = self._gate(clear_sec=600.0)
        gate.apply("T3", "fault")
        self.t += 500
        gate.apply("T3", "ok")
        gate.apply("T3", "suspect")   # relapse
        self.t += 500
        r = gate.apply("T3", "ok")    # only 0 s of the new ok streak
        self.assertEqual(r.target_state, "disabled")
        self.t += 600
        r = gate.apply("T3", "ok")
        self.assertEqual(r.target_state, "enabled")

    def test_unwitnessed_holds_the_current_state(self) -> None:
        gate, calls = self._gate()
        gate.apply("T3", "ok")
        n = len(calls)
        r = gate.apply("T3", "unwitnessed")
        self.assertEqual(r.target_state, "enabled")
        self.assertEqual(len(calls), n)
        gate.apply("T3", "fault")
        self.t += 10_000
        r = gate.apply("T3", "unwitnessed")   # no ok seen: stays withdrawn
        self.assertEqual(r.target_state, "disabled")
        self.assertIn("host_clock:held", r.reason)

    def test_tier_rule_still_applies_after_clear(self) -> None:
        gate, _ = self._gate(clear_sec=0.0)
        gate.apply("T3", "fault")
        r = gate.apply("T4", "ok")
        self.assertEqual(r.target_state, "disabled")   # cleared, but T4 disables anyway

    def test_flag_off_ignores_the_verdict(self) -> None:
        gate, _ = self._gate(withdraw_on_host_clock=False)
        r = gate.apply("T3", "fault")
        self.assertEqual(r.target_state, "enabled")
        self.assertFalse(gate.host_clock_withdrawn)

    def test_no_verdict_behaves_as_before(self) -> None:
        gate, calls = self._gate()
        r = gate.apply("T3")
        self.assertEqual(r.target_state, "enabled")
        self.assertEqual(r.reason, "applied -noselect")
        r = gate.apply("T3")
        self.assertEqual(r.reason, "no change")

    def test_default_refid_is_the_live_fusion_refclock(self) -> None:
        self.assertEqual(ChronyRefclockGate(dry_run=True).refid, "FUSE")
