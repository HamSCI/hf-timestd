"""
ChronyRefclockGate — toggles the Fusion SHM refclock between enabled
and disabled (via `chronyc selectopts <refid> ±noselect`) based on
the current authority state.

The policy, per METROLOGY.md §4.6: enable the refclock only when the
active T-level is one where Fusion is actually producing a useful
UTC reference — T3 or T6. Any other state (T5/T4/T2/T1 active, or no
level active) disables the refclock so chrony stops offering it as
an upstream source to clients, and stops using it to discipline the
local system clock.

This is the runtime-mutable half of the chrony integration. Stratum,
refid, and precision remain static per-install (chrony does not expose
runtime setters for those) and follow the install-time convention
table in §4.6.

Safety properties:

  - Only issues subprocess calls on actual state transitions. Steady
    state adds no system call overhead.
  - All chronyc failures are caught and surfaced in GateResult.reason
    without raising. The authority manager's tick() keeps running.
  - Respects dry_run for first-time deployments and CI.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable, Optional

log = logging.getLogger(__name__)


@dataclass
class GateResult:
    target_state: str   # "enabled" | "disabled"
    applied: bool       # True iff a chronyc call happened this tick
    reason: str = ""


class ChronyRefclockGate:
    # Active T-levels for which the refclock is -noselect. See §4.6.
    ENABLED_T_LEVELS = ("T3", "T6")

    def __init__(
        self,
        refid: str = "HFSN",
        chronyc_bin: Optional[str] = None,
        dry_run: bool = False,
        timeout_sec: float = 5.0,
        runner: Optional[Callable[..., subprocess.CompletedProcess]] = None,
    ):
        self.refid = refid
        self.chronyc_bin = chronyc_bin or shutil.which("chronyc") or "chronyc"
        self.dry_run = bool(dry_run)
        self.timeout_sec = float(timeout_sec)
        self._run = runner or subprocess.run
        self._last_state: Optional[str] = None  # "enabled"|"disabled" after a successful apply

    def apply(self, t_level_active: Optional[str]) -> GateResult:
        target = "enabled" if t_level_active in self.ENABLED_T_LEVELS else "disabled"
        if target == self._last_state:
            return GateResult(target_state=target, applied=False, reason="no change")

        flag = "-noselect" if target == "enabled" else "+noselect"

        if self.dry_run:
            self._last_state = target
            return GateResult(target_state=target, applied=False, reason=f"dry_run:{flag}")

        try:
            proc = self._run(
                [self.chronyc_bin, "selectopts", self.refid, flag],
                capture_output=True, text=True,
                timeout=self.timeout_sec, check=False,
            )
        except FileNotFoundError:
            return GateResult(target_state=target, applied=False, reason="chronyc not found")
        except subprocess.TimeoutExpired:
            return GateResult(target_state=target, applied=False, reason="chronyc timeout")
        except OSError as e:
            return GateResult(target_state=target, applied=False, reason=f"exec error: {e}")

        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip().splitlines()[-1:] or ["(no stderr)"]
            return GateResult(
                target_state=target, applied=False,
                reason=f"chronyc exit {proc.returncode}: {stderr[0]}",
            )

        # Successful apply — latch state.
        self._last_state = target
        return GateResult(target_state=target, applied=True, reason=f"applied {flag}")
