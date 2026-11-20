"""One T3 admission gate per process, read from ``authority.json``.

The fusion service's authority manager decides each minute whether the
stand-alone T3 holds admission (``T3AdmissionPolicy``) and publishes the
decision as ``authority.json``'s ``t3_admission`` block, every 30 s tick,
next to ``utc_published``.  Every other process -- the recorder and each
metrology channel -- follows that decision through one gate.  A jump
withdrawal reaches every surface after three lags add up: up to a minute
until the next shadow row decides it, up to one authority write interval
until ``authority.json`` shows it, and up to one gate refresh interval
until a process reads it.  At the defaults (one row a minute, 30 s
writes, 60 s refreshes) the worst case comes to about two and a half
minutes.

The gate reads the file only in :meth:`T3AdmissionGate.refresh`, which
never raises.  :meth:`admitted`, :meth:`reason` and :meth:`clock` read
attributes and the injected monotonic clock only, so they are safe on a
hot path.

Freshness uses no wall clock.  The gate counts the block as fresh only
after it has seen ``utc_published`` change at least once since it
started (a file left behind by a dead fusion service never changes), and
only while that last change lies within ``stale_s`` of ``mono_fn()``.
The manager writes every 30 s by default, so the default 120 s spans four
ticks; ``gate_stale_s`` stretches it to four of a longer configured
interval.

The rules, by mode::

    off   never admitted; the file goes unread
    on    a fresh block decides; without one, admitted (forced, testing)
    auto  a fresh block decides; without one, not admitted

A fresh block decides in mode ``on`` too, so a jump withdrawal closes T3
there as well.
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .authority_config import authority_interval_sec, authority_manager_section
from .clock_stability import ClockStability
from .t3_admission import NOT_ADMITTED_REASON, AdmissionMode
from .t3_admission_policy import (
    REASON_FORCED,
    REASON_OFF,
    clock_stability_from_block,
)

logger = logging.getLogger(__name__)

#: Where ``AuthorityManager`` writes.  The fusion service builds its
#: runner with ``build_authority_runner_from_config``'s default
#: ``authority_output_path``, which no config key overrides; this is the
#: same path.
DEFAULT_AUTHORITY_PATH = Path("/run/hf-timestd/authority.json")

#: Four of the manager's 30 s ticks, and the floor of ``gate_stale_s``.
DEFAULT_STALE_S = 120.0
#: ``gate_stale_s`` allows this many authority write intervals.
STALE_INTERVALS = 4


def gate_stale_s(config: Optional[dict]) -> float:
    """The gate's ``stale_s`` for a process whose config names the
    authority manager's interval: ``max(DEFAULT_STALE_S, STALE_INTERVALS *
    interval_sec)``, so a slower manager does not read as a dead one.  It
    reads ``[timing.authority_manager] interval_sec`` the way the runner
    does (``authority_config``).  A malformed, non-finite or non-positive
    interval logs a WARNING and gives ``DEFAULT_STALE_S``.  Never raises."""
    try:
        # float(True) reads 1.0; a boolean names no interval.
        if isinstance(authority_manager_section(config).get("interval_sec"),
                      bool):
            raise ValueError("a boolean")
        interval = authority_interval_sec(config)
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("not a positive finite number")
    except Exception as exc:  # noqa: BLE001 -- the default, never a raise
        logger.warning(
            "T3 admission gate: [timing.authority_manager] interval_sec "
            "unusable (%s); stale after %.0f s", exc, DEFAULT_STALE_S)
        return DEFAULT_STALE_S
    return max(DEFAULT_STALE_S, STALE_INTERVALS * interval)


REASON_NO_BLOCK = (
    "withheld: no fresh admission decision from the authority "
    "(authority.json missing, stale or malformed)"
)
REASON_FORCED_NO_BLOCK = (
    REASON_FORCED + "; no fresh admission decision from the authority"
)

_UNSEEN = object()


@dataclass(frozen=True)
class _Snapshot:
    """What the readers see, swapped in as ONE attribute by ``refresh``.

    The recorder's revalidation tick refreshes the gate while the offset
    judge's tick thread and the writer read it.  With the block, its
    change time and the clock in separate attributes, a reader between
    two writes could pair a new decision with an old clock.  One
    reference read is atomic, so each reader sees one refresh's state
    whole (A8 fix round 1, M-3)."""
    block: Optional[dict] = None
    changed_at: Optional[float] = None
    clock: Optional[ClockStability] = None


_EMPTY = _Snapshot()


class T3AdmissionGate:
    """The process's view of the published T3 admission decision."""

    def __init__(
        self,
        mode: AdmissionMode,
        authority_path: Path = DEFAULT_AUTHORITY_PATH,
        stale_s: float = DEFAULT_STALE_S,
        mono_fn: Callable[[], float] = time.monotonic,
    ):
        self._mode = AdmissionMode(mode)
        self._path = Path(authority_path)
        self._stale_s = float(stale_s)
        self._mono = mono_fn
        # Set only by fixed(): the answer, and refresh() reads nothing.
        self._fixed: Optional[bool] = None
        # The last utc_published seen.  Only refresh() reads or writes it.
        self._last_published: object = _UNSEEN
        # The last well-formed t3_admission block, the monotonic time
        # utc_published last changed, and the published clock stability:
        # one immutable snapshot, replaced whole (see _Snapshot).
        self._snap: _Snapshot = _EMPTY
        # The current failure streak's kind ("missing", "malformed", ...),
        # None while the file reads well.  One log line per streak.
        self._failure: Optional[str] = None

    @classmethod
    def fixed(cls, admitted: bool) -> "T3AdmissionGate":
        """A gate that answers ``admitted`` forever and reads no file.

        For tests and for callers with no authority file."""
        g = cls(AdmissionMode.ON if admitted else AdmissionMode.OFF)
        g._fixed = bool(admitted)
        return g

    @property
    def mode(self) -> AdmissionMode:
        return self._mode

    @property
    def authority_path(self) -> Path:
        return self._path

    @property
    def stale_s(self) -> float:
        return self._stale_s

    # ── the answer: no I/O ───────────────────────────────────────────

    def _fresh(self, snap: _Snapshot) -> bool:
        if snap.block is None or snap.changed_at is None:
            return False
        try:
            age = self._mono() - snap.changed_at
        except Exception:  # noqa: BLE001 -- a broken clock reads stale
            return False
        return 0 <= age <= self._stale_s

    def _fresh_block(self) -> Optional[dict]:
        snap = self._snap
        return snap.block if self._fresh(snap) else None

    def admitted(self) -> bool:
        if self._fixed is not None:
            return self._fixed
        if self._mode is AdmissionMode.OFF:
            return False
        block = self._fresh_block()
        if block is not None:
            return bool(block["admitted"])
        return self._mode is AdmissionMode.ON

    def reason(self) -> str:
        if self._fixed is not None:
            return "admitted (fixed)" if self._fixed else NOT_ADMITTED_REASON
        if self._mode is AdmissionMode.OFF:
            return REASON_OFF
        block = self._fresh_block()
        if block is not None:
            return str(block["reason"])
        if self._mode is AdmissionMode.ON:
            return REASON_FORCED_NO_BLOCK
        return REASON_NO_BLOCK

    def clock(self) -> Optional[ClockStability]:
        """The published clock stability, from a fresh block only."""
        if self._fixed is not None or self._mode is AdmissionMode.OFF:
            return None
        snap = self._snap
        return snap.clock if self._fresh(snap) else None

    # ── the only I/O ─────────────────────────────────────────────────

    def refresh(self) -> None:
        """Re-read ``authority.json``.  Never raises."""
        if self._fixed is not None or self._mode is AdmissionMode.OFF:
            return
        try:
            self._refresh()
        except Exception as exc:  # noqa: BLE001 -- failure closes, never raises
            self._fail("unreadable", f"{type(exc).__name__}: {exc}")

    def _refresh(self) -> None:
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self._fail("missing", "no such file")
            return
        try:
            doc = json.loads(text)
        except ValueError as exc:
            self._fail("malformed", f"not JSON ({exc})")
            return
        if not isinstance(doc, dict) or doc.get("utc_published") is None:
            self._fail("malformed", "no utc_published")
            return
        block = doc.get("t3_admission")
        if (not isinstance(block, dict)
                or not isinstance(block.get("admitted"), bool)
                or not isinstance(block.get("reason"), str)):
            self._fail("no_decision", "no well-formed t3_admission block")
            return
        published = doc["utc_published"]
        changed_at = self._snap.changed_at
        if self._last_published is _UNSEEN:
            # First sight since start (or since a failure): a file a dead
            # fusion service left behind looks just like this, so it does
            # not count as fresh until it changes.
            self._last_published = published
        elif published != self._last_published:
            self._last_published = published
            changed_at = self._mono()
        clock = clock_stability_from_block(block.get("stability"))
        # One assignment: readers see the old state or the new, whole.
        self._snap = _Snapshot(block=block, changed_at=changed_at, clock=clock)
        if self._failure is not None:
            logger.info("T3 admission gate: %s reads again", self._path)
            self._failure = None

    def _fail(self, kind: str, detail: str) -> None:
        # Drop the block at once, and forget the last stamp: after any
        # failure the gate wants to see a new write again before it trusts
        # the file (builder choice, the closed side: it costs one refresh
        # interval after a blip).
        self._snap = _EMPTY
        self._last_published = _UNSEEN
        if self._failure != kind:
            self._failure = kind
            logger.warning(
                "T3 admission gate: %s %s (%s); T3 follows mode %s without "
                "a published decision", self._path, kind, detail,
                self._mode.value)
