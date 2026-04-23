"""
ChronyStepper — invokes `chronyc makestep` to step the system clock
into the HF-tone detection window at bootstrap.

This is the one place in hf-timestd that actively mutates the system
clock (via chrony). It runs exactly when the BootstrapCoordinator says
the clock is far enough off to block Fusion startup but near enough that
stepping is safe. Guardrails:

  - The coordinator enforces a max step magnitude (default 1 hour).
    Anything larger is treated as "something else is wrong" and does
    not trigger a step.
  - `chronyc makestep` requires control-socket access. In standard
    Debian chrony installs, /run/chrony/chronyd.sock is group-owned by
    `chrony`; deployment must add the `timestd` user to that group.
    The stepper surfaces permission errors cleanly rather than silently
    failing.
  - A dry_run mode exists for integration tests and first-time
    deployments; no subprocess is invoked when enabled.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable, Optional

log = logging.getLogger(__name__)


@dataclass
class StepResult:
    success: bool
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    reason: str = ""


class ChronyStepper:
    """Subprocess wrapper for `chronyc makestep`."""

    def __init__(
        self,
        chronyc_bin: Optional[str] = None,
        timeout_sec: float = 10.0,
        dry_run: bool = False,
        runner: Optional[Callable[..., subprocess.CompletedProcess]] = None,
    ):
        self.chronyc_bin = chronyc_bin or shutil.which("chronyc") or "chronyc"
        self.timeout_sec = float(timeout_sec)
        self.dry_run = bool(dry_run)
        self._run = runner or subprocess.run

    def makestep(self) -> StepResult:
        if self.dry_run:
            return StepResult(success=True, reason="dry_run")

        try:
            proc = self._run(
                [self.chronyc_bin, "makestep"],
                capture_output=True, text=True,
                timeout=self.timeout_sec, check=False,
            )
        except FileNotFoundError:
            return StepResult(success=False, reason="chronyc not found")
        except subprocess.TimeoutExpired:
            return StepResult(success=False, reason="chronyc timeout")
        except OSError as e:
            return StepResult(success=False, reason=f"chronyc exec error: {e}")

        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()

        if proc.returncode != 0:
            # Typical failure: "500 Not authorised" when control socket
            # access is denied. Surface the reason clearly.
            tail = stderr.splitlines()[-1:] or stdout.splitlines()[-1:] or ["(no output)"]
            return StepResult(
                success=False,
                returncode=proc.returncode,
                stdout=stdout, stderr=stderr,
                reason=f"chronyc exit {proc.returncode}: {tail[0]}",
            )

        return StepResult(
            success=True,
            returncode=0,
            stdout=stdout, stderr=stderr,
            reason="ok",
        )
