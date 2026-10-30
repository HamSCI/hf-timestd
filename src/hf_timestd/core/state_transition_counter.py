"""Counts a scope's state changes and logs each one once at WARNING."""
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class StateTransitionCounter:
    def __init__(self, scope: str):
        self.scope = scope
        self._state: Optional[str] = None
        self._count = 0

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
        if state == self._state:
            return None
        prev, self._state = self._state, state
        self._count += 1
        logger.warning(
            f"T3 STATE {self.scope}: {prev} -> {state} ({reason})")
        return {"kind": "transition", "scope": self.scope,
                "from": prev, "to": state, "reason": reason,
                "utc_minute": utc_minute, "count": self._count}
