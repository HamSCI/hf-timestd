"""Counts a scope's state changes and logs them loudly, once.

Every change counts and returns its row.  The logging is rate-limited by
data minutes (the rows' own ``utc_minute``), never by a clock read: the
first change in a 60-minute window logs a WARNING; later changes in that
window log at INFO; the first observation at or after the window's end
logs one summary WARNING if the window saw more than one change.  A
channel flapping every minute therefore costs one WARNING an hour, not
one a minute.
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class StateTransitionCounter:
    WINDOW_S = 3600

    def __init__(self, scope: str):
        self.scope = scope
        self._state: Optional[str] = None
        self._count = 0
        self._window_start: Optional[int] = None
        self._window_changes = 0

    @property
    def count(self) -> int:
        return self._count

    def observe(self, state: str, reason: str,
                utc_minute: int) -> Optional[dict]:
        """Return a transition row when the state changed, else None.

        The first state seen is "startup": it returns a row but counts as
        no transition and logs no WARNING.  A repeated state returns None.
        """
        if self._state is None:
            self._state = state
            return {"kind": "transition", "scope": self.scope,
                    "from": None, "to": state, "reason": "startup",
                    "utc_minute": utc_minute, "count": self._count}
        self._close_window(utc_minute)
        if state == self._state:
            return None
        prev, self._state = self._state, state
        self._count += 1
        msg = f"T3 STATE {self.scope}: {prev} -> {state} ({reason})"
        if self._window_start is None:
            self._window_start = int(utc_minute)
            self._window_changes = 1
            logger.warning(msg)
        else:
            self._window_changes += 1
            logger.info(msg)
        return {"kind": "transition", "scope": self.scope,
                "from": prev, "to": state, "reason": reason,
                "utc_minute": utc_minute, "count": self._count}

    def _close_window(self, utc_minute: int) -> None:
        """End the open window at the first observation at or after its
        end; summarise it in one WARNING when it saw more than one change."""
        if (self._window_start is None
                or int(utc_minute) < self._window_start + self.WINDOW_S):
            return
        if self._window_changes > 1:
            logger.warning(
                f"T3 STATE {self.scope}: {self._window_changes} transitions "
                f"in the last {self.WINDOW_S // 60} min, now {self._state}")
        self._window_start = None
        self._window_changes = 0
