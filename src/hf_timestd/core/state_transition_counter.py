"""Counts a scope's state changes and logs them loudly, once.

Every change counts and returns its row.  The logging is rate-limited by
data minutes (the rows' own ``utc_minute``), never by a clock read: the
first change in a 60-minute window logs a WARNING; later changes in that
window log at INFO; the first observation at or after the window's end
logs one summary WARNING if the window saw more than one change.  A
channel flapping every minute therefore costs one WARNING an hour, not
one a minute.

A quiet change (one whose event another line already announced at
WARNING, such as a radiod restart) logs at INFO, and so does the rest of
its recovery: every change until the scope returns to the state it left.
A window that holds only such changes summarises at INFO, so a restart
costs no WARNING an hour later (ruling C, 2026-10-30, finding N-2).  A
change outside a recovery stays loud: the first one in a quiet window
logs the WARNING the window has not yet given, and its summary warns.
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
        # Has this window logged a WARNING, and did it see a change no
        # other line announced?  Only such a window summarises at WARNING.
        self._window_warned = False
        self._window_loud = False
        # The state a quiet change left: changes stay part of that event's
        # recovery until the scope returns to it.  None outside a recovery.
        self._recovering_to: Optional[str] = None

    @property
    def count(self) -> int:
        return self._count

    def observe(self, state: str, reason: str,
                utc_minute: int, quiet: bool = False) -> Optional[dict]:
        """Return a transition row when the state changed, else None.

        The first state seen is "startup": it returns a row but counts as
        no transition and logs no WARNING.  A repeated state returns None.

        ``quiet``: another line already carried this event's WARNING (a
        new radiod run, final review I-7), so the change logs at INFO.  It
        still counts and still opens the window, but the changes of its
        recovery log at INFO too, and a window of nothing else summarises
        at INFO (N-2).
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
        # Part of an announced event: the quiet change itself, or a step
        # of its recovery.  A new quiet change restarts the recovery from
        # the state it left, unless one is already under way.
        announced = quiet or self._recovering_to is not None
        if quiet and self._recovering_to is None:
            self._recovering_to = prev
        if self._recovering_to is not None and state == self._recovering_to:
            self._recovering_to = None          # recovered
        if self._window_start is None:
            self._window_start = int(utc_minute)
            self._window_changes = 0
            self._window_warned = False
            self._window_loud = False
        self._window_changes += 1
        if announced:
            logger.info(msg)
        else:
            self._window_loud = True
            if self._window_warned:
                logger.info(msg)
            else:
                self._window_warned = True
                logger.warning(msg)
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
            # A window of announced changes only (a restart and its
            # recovery) already gave its WARNING through another line.
            logger.log(
                logging.WARNING if self._window_loud else logging.INFO,
                f"T3 STATE {self.scope}: {self._window_changes} transitions "
                f"in the last {self.WINDOW_S // 60} min, now {self._state}")
        self._window_start = None
        self._window_changes = 0
        self._window_warned = False
        self._window_loud = False
