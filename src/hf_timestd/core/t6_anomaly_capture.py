"""Keep the T6 IQ only around anomalies, not continuously.

The archival decision (mjh, 2026-08-25) is that the anchor ledger stands
in for the T6 sample stream: continuous 96 kHz complex64 costs ~60 GB/day
against the ledger's 0.37 MB/day, measured on AC0G-B4.

But a ledger row is the matched filter's OUTPUT, and the MF is exactly
what has been wrong: the 2026-05-23 sidelobe phantom and the 2026-08-25
livelock were both precise-looking anchors that were wrong, and neither
could be re-derived from a ledger.  Raw IQ is the only thing that lets a
later reader re-run the filter.

So keep a short window around the rare moments when the anchor path
misbehaves.  60 s at 96 kHz complex64 is ~46 MB; twenty events a day
stays under 1 GB, about 1.5 % of archiving continuously.

⚠ **Rate limiting is not optional.**  The 2026-08-25 livelock re-entered
its failure branch at ~1 Hz for hours.  A trigger without a floor on the
interval would have written thousands of dumps and filled the disk —
turning a diagnostic into an outage.  Both a minimum interval and a daily
cap are enforced, and suppression is COUNTED and reported rather than
silent, so nobody reads an absent dump as an absent anomaly.

The ring holds the window BEFORE the trigger.  An anomaly's cause is in
its past, and a pre-trigger ring needs no post-trigger state machine.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

DEFAULT_DIR = Path("/var/lib/timestd/state/t6-anomaly")

# Enough to re-run the fold that produced the estimate, plus context.
DEFAULT_WINDOW_S = 60.0

# Floor on the interval between dumps.  Sized against the livelock: a
# 1 Hz failure branch must produce at most one dump per interval.
DEFAULT_MIN_INTERVAL_S = 900.0

# Backstop on a pathology that outlives the interval floor.
DEFAULT_MAX_PER_DAY = 20


class AnomalyCapture:
    """Rolling pre-trigger ring of T6 IQ, dumped on demand under a budget."""

    def __init__(
        self,
        dir_path: Path = DEFAULT_DIR,
        *,
        sample_rate_hz: int = 96_000,
        window_s: float = DEFAULT_WINDOW_S,
        min_interval_s: float = DEFAULT_MIN_INTERVAL_S,
        max_per_day: int = DEFAULT_MAX_PER_DAY,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self.dir_path = Path(dir_path)
        self.sample_rate_hz = int(sample_rate_hz)
        self.window_samples = int(round(float(window_s) * sample_rate_hz))
        self.min_interval_s = float(min_interval_s)
        self.max_per_day = int(max_per_day)
        self._now = now_fn
        self._ring: deque = deque()
        self._held = 0
        self._last_dump: Optional[float] = None
        self._day: Optional[str] = None
        self._today = 0
        self.suppressed = 0

    # ---------------------------------------------------------------- ring
    def add(self, samples) -> None:
        """Append one batch, evicting whole batches past the window."""
        n = len(samples)
        if n <= 0:
            return
        self._ring.append(samples)
        self._held += n
        while self._ring and self._held - len(self._ring[0]) >= self.window_samples:
            self._held -= len(self._ring.popleft())

    @property
    def held_samples(self) -> int:
        return self._held

    # ------------------------------------------------------------- budget
    def _roll_day(self, now: float) -> None:
        day = time.strftime("%Y%m%d", time.gmtime(now))
        if day != self._day:
            self._day = day
            self._today = 0

    def may_dump(self, now: Optional[float] = None) -> bool:
        """Whether the budget permits a dump right now.

        Pure decision, separated from I/O so the policy that stands
        between a 1 Hz pathology and a full disk is directly testable.
        """
        now = float(self._now() if now is None else now)
        self._roll_day(now)
        if self._today >= self.max_per_day:
            return False
        if (self._last_dump is not None
                and (now - self._last_dump) < self.min_interval_s):
            return False
        return True

    # --------------------------------------------------------------- dump
    def trigger(self, reason: str, now: Optional[float] = None) -> Optional[Path]:
        """Write the ring out.  Returns the path, or None if suppressed.

        Suppression is counted and logged, never silent: an absent dump
        must not be mistaken for an absent anomaly.
        """
        now = float(self._now() if now is None else now)
        if not self._ring:
            return None
        if not self.may_dump(now):
            self.suppressed += 1
            if self.suppressed == 1 or self.suppressed % 100 == 0:
                logger.warning(
                    "T6 anomaly capture SUPPRESSED (%s): %d suppressed so "
                    "far, %d/%d dumps used today, %.0f s since the last. "
                    "The anomaly is real; only the capture was skipped.",
                    reason, self.suppressed, self._today, self.max_per_day,
                    0.0 if self._last_dump is None else now - self._last_dump,
                )
            return None
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
        safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in reason)
        path = self.dir_path / f"t6-anomaly-{stamp}-{safe}.iq"
        try:
            self.dir_path.mkdir(parents=True, exist_ok=True)
            with open(path, "wb") as f:
                for batch in self._ring:
                    f.write(memoryview(batch).cast("B")
                            if hasattr(batch, "__buffer__")
                            else bytes(batch))
        except (OSError, TypeError, ValueError) as exc:
            logger.warning(
                "T6 anomaly capture: cannot write %s (%s) — anomaly '%s' "
                "not captured.", path, exc, reason)
            return None
        self._last_dump = now
        self._today += 1
        logger.warning(
            "T6 anomaly capture: wrote %.1f s of IQ to %s (reason=%s, "
            "%d/%d today). Raw samples are the only way to re-run the "
            "matched filter on this event.",
            self._held / float(self.sample_rate_hz), path, reason,
            self._today, self.max_per_day,
        )
        return path
