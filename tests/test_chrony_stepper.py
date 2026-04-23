#!/usr/bin/env python3
"""Unit tests for ChronyStepper."""

import subprocess
import unittest
from dataclasses import dataclass

from hf_timestd.core.chrony_stepper import ChronyStepper


@dataclass
class _FakeCompleted:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


def _runner(rc=0, stdout="", stderr="", raises=None):
    def _run(cmd, capture_output=False, text=False, timeout=None, check=False):
        if raises is not None:
            raise raises
        return _FakeCompleted(returncode=rc, stdout=stdout, stderr=stderr)
    return _run


class TestChronyStepper(unittest.TestCase):
    def test_dry_run_never_invokes_subprocess(self) -> None:
        def _explode(*a, **kw):
            raise AssertionError("subprocess should not be called in dry_run")
        stepper = ChronyStepper(dry_run=True, runner=_explode)
        r = stepper.makestep()
        self.assertTrue(r.success)
        self.assertEqual(r.reason, "dry_run")

    def test_successful_step(self) -> None:
        stepper = ChronyStepper(runner=_runner(rc=0, stdout="200 OK"))
        r = stepper.makestep()
        self.assertTrue(r.success)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.reason, "ok")

    def test_permission_denied_surfaces_message(self) -> None:
        # chronyc returns non-zero with "500 Not authorised" when the
        # control socket is not accessible to the running user.
        stepper = ChronyStepper(runner=_runner(
            rc=1, stderr="500 Not authorised"
        ))
        r = stepper.makestep()
        self.assertFalse(r.success)
        self.assertEqual(r.returncode, 1)
        self.assertIn("Not authorised", r.reason)

    def test_chronyc_missing_returns_failure(self) -> None:
        stepper = ChronyStepper(runner=_runner(raises=FileNotFoundError()))
        r = stepper.makestep()
        self.assertFalse(r.success)
        self.assertIn("not found", r.reason)

    def test_timeout_returns_failure(self) -> None:
        stepper = ChronyStepper(
            runner=_runner(raises=subprocess.TimeoutExpired(cmd="chronyc", timeout=10)),
        )
        r = stepper.makestep()
        self.assertFalse(r.success)
        self.assertIn("timeout", r.reason)

    def test_exec_error_returns_failure(self) -> None:
        stepper = ChronyStepper(runner=_runner(raises=OSError("bad file descriptor")))
        r = stepper.makestep()
        self.assertFalse(r.success)
        self.assertIn("exec error", r.reason)


if __name__ == "__main__":
    unittest.main()
