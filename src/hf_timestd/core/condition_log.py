"""Log a persisting condition once, then as a periodic count.

A producer that logs the same condition on every cycle floods the
journal and buries the faults that matter.  On AC0G-B4 (v3.73 row 18)
timestd-fusion wrote 124,000 lines an hour this way, and journald
dropped two days of history to make room.

``ConditionLog`` gives each condition a key, such as a channel name.

- The first ``note()`` for a key logs the caller's message at the
  caller's level.  A WARNING stays a WARNING.
- Later notes count silently.  Once ``summary_interval_s`` has passed
  since the last line for that key, the next note logs one summary at
  the same level: how many repeats, and the latest message.
- When no note has arrived for ``clear_after_s``, ``sweep()`` logs one
  "cleared" line at the same level, with the total count and the count
  since the last line.  ``note()`` sweeps too, so a caller that notes
  on every cycle needs no separate sweep; a caller that may go quiet
  should call ``sweep()`` once per cycle.

The count never drops: every occurrence appears in a first line, a
summary or a clear line.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional


def _span(seconds: float) -> str:
    if seconds >= 60.0:
        return f"{seconds / 60.0:.0f} min"
    return f"{seconds:.0f} s"


@dataclass
class _Condition:
    level: int
    first_s: float
    last_seen_s: float
    last_report_s: float
    total: int = 1
    since_report: int = 0
    latest: str = ""


class ConditionLog:
    """Rate-limit repeated log lines per condition key."""

    def __init__(
        self,
        logger: logging.Logger,
        *,
        summary_interval_s: float = 600.0,
        clear_after_s: Optional[float] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.logger = logger
        self.summary_interval_s = float(summary_interval_s)
        self.clear_after_s = float(
            summary_interval_s if clear_after_s is None else clear_after_s
        )
        self._clock = clock
        self._active: Dict[str, _Condition] = {}

    def note(self, key: str, message: str, level: int = logging.WARNING) -> bool:
        """Record one occurrence of ``key``.  Return True if it logged."""
        now = self._clock()
        self.sweep(now)
        cond = self._active.get(key)
        if cond is None:
            self._active[key] = _Condition(
                level=level, first_s=now, last_seen_s=now,
                last_report_s=now, latest=message,
            )
            self.logger.log(level, message)
            return True
        cond.total += 1
        cond.since_report += 1
        cond.last_seen_s = now
        cond.latest = message
        if now - cond.last_report_s < self.summary_interval_s:
            return False
        self.logger.log(
            cond.level,
            f"{key}: still present; {cond.since_report} more in the last "
            f"{_span(now - cond.last_report_s)} ({cond.total} since it began "
            f"{_span(now - cond.first_s)} ago). Latest: {message}",
        )
        cond.since_report = 0
        cond.last_report_s = now
        return True

    def sweep(self, now: Optional[float] = None) -> None:
        """Log and forget every condition absent for ``clear_after_s``."""
        if now is None:
            now = self._clock()
        for key in [k for k, c in self._active.items()
                    if now - c.last_seen_s >= self.clear_after_s]:
            cond = self._active.pop(key)
            self.logger.log(
                cond.level,
                f"{key}: cleared; none for {_span(now - cond.last_seen_s)}, "
                f"after {cond.total} occurrences over "
                f"{_span(cond.last_seen_s - cond.first_s)} "
                f"({cond.since_report} since the last report)",
            )

    def active_keys(self) -> List[str]:
        return sorted(self._active)
