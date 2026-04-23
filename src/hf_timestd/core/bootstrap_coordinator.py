"""
BootstrapCoordinator — breaks the circular dependency between the
authority manager and the system clock at startup.

The 2026-04-20 incident showed the need for this: when chrony lost
every usable source, the system clock drifted ~107 s, hf-timestd could
not find WWV/CHU ticks in the audio buffer (they were outside its
search window), and Fusion silently held a stale "0 ms" correction
that kept telling chrony everything was fine.

The coordinator watches for this shape:
  1. A system-clock-independent coarse UTC is available (BCD/FSK via
     CoarseTimeSource).
  2. The system clock differs from it by more than a threshold (default
     5 s).
  3. The magnitude is within a bounded maximum step (default 1 h).

When all three hold, it issues a single `chronyc makestep` to bring
the clock into the detection window. Thereafter Fusion's normal
tick-edge detection can proceed, and the authority manager's T3 probe
will report healthy.

This module is invoked on every authority-manager tick. It is
stateless by design — each tick is an independent decision — so it
naturally re-engages if the clock drifts badly again mid-life (e.g.,
chrony dies and time coasts off).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from hf_timestd.core.chrony_stepper import ChronyStepper
from hf_timestd.core.coarse_time_source import (
    CoarseTimeObservation,
    CoarseTimeSource,
)

log = logging.getLogger(__name__)


@dataclass
class BootstrapState:
    """Ephemeral result from one coordinator tick."""
    complete: bool
    reason: str
    delta_sec: Optional[float] = None
    stepped: bool = False
    coarse: Optional[CoarseTimeObservation] = None


class BootstrapCoordinator:
    """Compares system clock against coarse UTC and issues a step when
    the gap is large enough to block Fusion but small enough to step
    safely.
    """

    def __init__(
        self,
        coarse_source: CoarseTimeSource,
        stepper: ChronyStepper,
        threshold_sec: float = 90.0,
        max_step_sec: float = 3600.0,
    ):
        """
        Args:
            coarse_source: Publisher-independent UTC source.
            stepper: chronyc makestep wrapper.
            threshold_sec: Minimum |system_clock - coarse_utc| that
                triggers a step. Default 90 s reflects the minute-level
                precision of the CHU FSK / WWV BCD producers (max_error
                ~60 s in the producer contract) plus margin for the
                few seconds of decode/publish latency. Finer-precision
                coarse sources can safely lower this at deployment.
            max_step_sec: Refuse to step if |delta| exceeds this.
                Default 1 h keeps us from stepping through a config
                or signal-source misconfiguration (e.g., wrong year
                from a Frame-B decode) that would otherwise destroy
                state on downstream consumers.
        """
        self.coarse_source = coarse_source
        self.stepper = stepper
        self.threshold_sec = float(threshold_sec)
        self.max_step_sec = float(max_step_sec)

    def check_and_step(
        self,
        now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> BootstrapState:
        coarse = self.coarse_source.read()
        if coarse is None:
            return BootstrapState(complete=False, reason="no_coarse_time")

        delta_sec = (now_fn() - coarse.utc).total_seconds()

        # Within threshold → no step needed; we're already bootstrap-complete.
        if abs(delta_sec) < self.threshold_sec:
            return BootstrapState(
                complete=True, reason="within_threshold",
                delta_sec=delta_sec, coarse=coarse,
            )

        # Outside safe step range → something is structurally wrong;
        # refuse to step. Surface the delta so operators can see it.
        if abs(delta_sec) > self.max_step_sec:
            log.warning(
                "Bootstrap refuses to step: |delta|=%.1fs > max_step=%.1fs; "
                "system clock or coarse-time source is seriously misconfigured.",
                abs(delta_sec), self.max_step_sec,
            )
            return BootstrapState(
                complete=False, reason="unable_too_far",
                delta_sec=delta_sec, coarse=coarse,
            )

        # Gap in the safe range → step.
        log.info(
            "Bootstrap: system clock differs from coarse UTC by %+.3fs "
            "(%s %s); invoking chronyc makestep.",
            delta_sec, coarse.source, coarse.station,
        )
        result = self.stepper.makestep()
        if not result.success:
            log.warning("Bootstrap step failed: %s", result.reason)
            return BootstrapState(
                complete=False, reason=f"step_failed:{result.reason}",
                delta_sec=delta_sec, coarse=coarse,
            )

        return BootstrapState(
            complete=True, reason="stepped",
            delta_sec=delta_sec, stepped=True, coarse=coarse,
        )
