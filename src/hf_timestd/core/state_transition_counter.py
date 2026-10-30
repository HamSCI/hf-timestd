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
its recovery: every change until the scope returns to the state it left,
and never longer than ``RECOVERY_S`` after the quiet change.  A window
that holds only such changes summarises at INFO, so a restart costs no
WARNING an hour later (ruling C, 2026-10-30, finding N-2).  A change
outside a recovery stays loud: the first one in a quiet window logs the
WARNING the window has not yet given, and its summary warns.

A caller that knows the event's identity (the radiod run, by its counter
map's ``input_epoch_gps_s``) passes it as ``run_id``.  A run identity not
seen before opens a recovery that lasts the whole ``RECOVERY_S`` whatever
states pass, because several writers report one event out of step
(fix round 1, I-2).
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class StateTransitionCounter:
    WINDOW_S = 3600
    # Fix round 1, I-1: the longest a recovery may last, from the quiet
    # change (or the new run identity) that opened it.  One hour: a
    # restart's channels re-acquire in minutes, and the hour matches the
    # window, so at most one window's WARNINGs can go to INFO.
    RECOVERY_S = 3600
    # Two run identities this close (s) name one run (the 2 s skew
    # allowance radiod's late GPS_TIME packets need).
    RUN_ID_TOL = 2.0
    # How many run identities to remember, so a lagging writer that still
    # names the old run never reads as a new one.
    RUN_IDS_KEPT = 8

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
        # When the recovery began (data minutes), and whether a new run
        # identity opened it (then only RECOVERY_S ends it).
        self._recovery_start: Optional[int] = None
        self._recovery_by_run = False
        self._run_ids: list = []

    @property
    def count(self) -> int:
        return self._count

    def _end_recovery(self) -> None:
        self._recovering_to = None
        self._recovery_start = None
        self._recovery_by_run = False

    def _in_recovery(self) -> bool:
        return self._recovery_start is not None

    def _note_run_id(self, run_id: Optional[float], utc_minute: int) -> None:
        """Remember ``run_id``; a never-seen one (after the first) opens
        a run recovery from this minute."""
        if run_id is None:
            return
        try:
            rid = float(run_id)
        except (TypeError, ValueError):
            return
        if any(abs(rid - r) <= self.RUN_ID_TOL for r in self._run_ids):
            return
        first = not self._run_ids
        self._run_ids = (self._run_ids + [rid])[-self.RUN_IDS_KEPT:]
        if first:
            return
        if not self._in_recovery():
            self._recovering_to = self._state
        self._recovery_start = int(utc_minute)
        self._recovery_by_run = True

    def observe(self, state: str, reason: str,
                utc_minute: int, quiet: bool = False,
                run_id: Optional[float] = None) -> Optional[dict]:
        """Return a transition row when the state changed, else None.

        The first state seen is "startup": it returns a row but counts as
        no transition and logs no WARNING.  A repeated state returns None.

        ``quiet``: another line already carried this event's WARNING (a
        new radiod run, final review I-7), so the change logs at INFO.  It
        still counts and still opens the window, but the changes of its
        recovery log at INFO too, and a window of nothing else summarises
        at INFO (N-2).
        """
        if (self._recovery_start is not None
                and int(utc_minute) >= self._recovery_start + self.RECOVERY_S):
            self._end_recovery()                # I-1: the bound
        if self._state is None:
            self._state = state
            self._note_run_id(run_id, utc_minute)
            return {"kind": "transition", "scope": self.scope,
                    "from": None, "to": state, "reason": "startup",
                    "utc_minute": utc_minute, "count": self._count}
        self._close_window(utc_minute)
        self._note_run_id(run_id, utc_minute)
        if state == self._state:
            return None
        prev, self._state = self._state, state
        self._count += 1
        msg = f"T3 STATE {self.scope}: {prev} -> {state} ({reason})"
        # Part of an announced event: the quiet change itself, or a step
        # of its recovery.  A new quiet change restarts the recovery from
        # the state it left, unless one is already under way.
        announced = quiet or self._in_recovery()
        if quiet and not self._in_recovery():
            self._recovering_to = prev
            self._recovery_start = int(utc_minute)
        if (self._in_recovery() and not self._recovery_by_run
                and state == self._recovering_to):
            self._end_recovery()                # recovered
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
