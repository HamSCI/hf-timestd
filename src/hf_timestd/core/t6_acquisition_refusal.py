"""A T6 acquisition refusal that a recorder restart cannot cure.

T6 refuses to acquire when the best placement of its edge puts it more
than ±250 ms from its second.  Without T5 or a qualified T4 that
placement rests on radiod's per-channel (GPS_TIME, RTP_TIMESNAP) pair,
which radiod drew from the host clock when it created the channel.  The
pair belongs to radiod and survives a restart of timestd-core-recorder,
so restarting the recorder meets the same pair and the same refusal.
Only radiod redrawing its tie, or a reference that places the edge
(T5, or a qualified T4), moves it.

hpps-watchdog restarts the recorder whenever HPPS goes quiet.  On
AC0G-ND (2026-10-27 to 10-31) T6 refused every acquisition, HPPS stayed
dark, and the watchdog restarted the recorder about 41 times a day,
curing nothing and opening a data gap each time.  This record lets the
recorder say, in its status file, that it is refusing and that a
restart will not help.

Criterion 7 refreshes the record once per fold block (30 s at K = 30)
while it persists, so a record that stopped being refreshed has stopped
being true: ``current`` reports it only while the last refusal stays younger
than ``CURRENT_FOR_S``.  That bound also keeps a stale record from
shielding a recorder that has since wedged for some other reason.
"""
from __future__ import annotations

from typing import Optional

__all__ = ["T6AcquisitionRefusal", "REFUSAL_PLACEMENT_IMPLAUSIBLE"]

#: The one refusal reason recorded today.  A recorder restart keeps
#: radiod's pair, so it cannot cure this one.
REFUSAL_PLACEMENT_IMPLAUSIBLE = "placement_implausible"

_RESTART_CURES = {REFUSAL_PLACEMENT_IMPLAUSIBLE: False}


class T6AcquisitionRefusal:
    """Entry, refresh and exit of one persistent refusal condition.

    ``note`` returns True on entry only and ``clear``/``expire`` return
    the summary on exit only, so the caller logs each edge once and the
    refresh in between stays silent.
    """

    #: Five fold blocks at K = 30: one discarded block must not end it.
    CURRENT_FOR_S = 150.0

    def __init__(self) -> None:
        self._reset()

    def _reset(self) -> None:
        self.reason: Optional[str] = None
        self.since_mono: Optional[float] = None
        self.since_utc: Optional[float] = None
        self.last_mono: Optional[float] = None
        self.count = 0
        self.placement_ns: Optional[int] = None
        self.t4: Optional[str] = None
        self.reference: Optional[str] = None

    @property
    def active(self) -> bool:
        return self.reason is not None

    def note(self, reason: str, placement_ns: Optional[int], mono: float,
             wall: float, t4: Optional[str] = None,
             reference: Optional[str] = None) -> bool:
        """Record one refusal; True when this one opens the condition."""
        entering = self.reason != reason
        if entering:
            self._reset()
            self.reason = str(reason)
            self.since_mono = float(mono)
            self.since_utc = float(wall)
        self.last_mono = float(mono)
        self.count += 1
        self.placement_ns = (None if placement_ns is None
                             else int(placement_ns))
        self.t4 = t4
        self.reference = reference
        return entering

    def clear(self, mono: float) -> Optional[dict]:
        """Close the condition; its summary when one was open, else None."""
        if not self.active:
            return None
        summary = self._summary(mono)
        self._reset()
        return summary

    def expire(self, mono: float) -> Optional[dict]:
        """Close the condition if no refusal refreshed it lately."""
        if (self.active and self.last_mono is not None
                and float(mono) - self.last_mono > self.CURRENT_FOR_S):
            return self.clear(mono)
        return None

    def current(self, mono: float) -> Optional[dict]:
        """The status-file record while the refusal stays current."""
        if not self.active or self.last_mono is None:
            return None
        if float(mono) - self.last_mono > self.CURRENT_FOR_S:
            return None
        return self._summary(mono)

    def _summary(self, mono: float) -> dict:
        return {
            "reason": self.reason,
            "since_utc": self.since_utc,
            "duration_s": (None if self.since_mono is None
                           else round(float(mono) - self.since_mono, 1)),
            "refusals": self.count,
            "placement_ms": (None if self.placement_ns is None
                             else round(self.placement_ns / 1e6, 3)),
            "placed_by": self.reference,
            "t4_reference": self.t4,
            "restart_cures": _RESTART_CURES.get(self.reason, True),
        }
