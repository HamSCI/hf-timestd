"""
FusionLoopMetrics — per-cycle instrumentation for timestd-fusion.

Captures wall-clock duration per named phase, RSS sampled via
resource.getrusage, watchdog budget consumption, and discrete events.
Two sinks per cycle:

  - /run/hf-timestd/fusion_metrics.json — atomic point-in-time snapshot
    (same tempfile + fsync + os.replace pattern as fusion_status.json).
  - One structured line to journald, prefixed `fusion_metrics`, so
    historical analysis can use `journalctl -u timestd-fusion
    -g '^fusion_metrics'`.  The line goes out at INFO on the first
    cycle, on any cycle that carries an event or spends at least
    ``SLOW_CYCLE_WD_PCT`` of the watchdog budget, and otherwise once
    per ``log_interval_s``; the cycles between log at DEBUG.  One INFO
    line per 8 s cycle had made it a top-three journal line on AC0G-B4
    (v3.74 row 11); the JSON snapshot still updates every cycle.

Used by the fusion parsimony/stability audit (see the fusion audit
memory). This is the measurement-phase data source that decides which
of the audit's red flags (monolith, memory, watchdog slack, chrony
restart dance) are real problems at deployment scale — before any
refactor touches the fusion loop.

Schema v1:

    {
      "schema": "v1",
      "utc_published": "2026-04-23T14:32:17.123456Z",
      "cycle_index": 42,
      "first_cycle": false,
      "loop_duration_sec": 2.341,
      "watchdog_budget_sec": 120.0,
      "watchdog_consumed_pct": 1.951,
      "rss_kb": 234567,
      "phases": {
        "fuse_l1": 0.82,
        "fuse_l2": 0.78,
        "hdf5_read": 0.42,
        "kalman_apply": 0.08,
        "calibration_apply": 0.01,
        "shm_write": 0.002,
        "fusion_status_write": 0.004,
        "chrony_stats": 0.001
      },
      "events": ["shm_reconnect_l1", "kalman_lock"],
      "counts": {
        "kalman_rows_fed": 2,
        "kalman_rows_reused": 286,
        "kalman_rows_refused": 0
      }
    }

``counts`` holds per-cycle tallies.  The per-broadcast Kalman banks add
the rows they consumed, the rows they served again from an earlier
cycle, and the rows they refused (older than the filter's last row and
never consumed, a duplicate, or non-finite); the refused count should
read zero.  ``totals`` holds cumulative tallies since the process
started (``kalman_rows_refused``), and the journal line carries them,
so one refusal outlives the cycle that saw it.
"""
from __future__ import annotations

import json
import logging
import os
import resource
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional

log = logging.getLogger(__name__)

SCHEMA_VERSION = "v1"
DEFAULT_PATH = Path("/run/hf-timestd/fusion_metrics.json")
DEFAULT_LOG_INTERVAL_S = 60.0
SLOW_CYCLE_WD_PCT = 25.0


class FusionLoopMetrics:
    """Per-cycle accumulator + multi-sink emitter.

    Single-thread use only: the fusion main loop constructs one
    instance before entering the while-True and calls start_cycle /
    phase / mark_event / finalize_and_emit from the same thread. The
    authority thread does not touch this object.
    """

    def __init__(
        self,
        watchdog_sec: float,
        path: Path = DEFAULT_PATH,
        log_interval_s: float = DEFAULT_LOG_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.watchdog_sec = float(watchdog_sec)
        self.log_interval_s = float(log_interval_s)
        self._clock = clock
        self._last_info_s: Optional[float] = None
        self.path = Path(path)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            # /run/hf-timestd is created by the service unit's
            # RuntimeDirectory=; in unit tests the parent is writable.
            log.debug("FusionLoopMetrics: cannot mkdir %s", self.path.parent)

        self._cycle_index = 0
        self._phases: Dict[str, float] = {}
        self._events: List[str] = []
        self._counts: Dict[str, int] = {}
        self._totals: Dict[str, int] = {}   # cumulative; never reset
        self._cycle_start = time.monotonic()

    def start_cycle(self) -> None:
        """Call at the top of each fusion loop iteration. Resets
        per-cycle state; cycle_index is incremented at emit time."""
        self._cycle_start = time.monotonic()
        self._phases = {}
        self._events = []
        self._counts = {}

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        """Accumulate wall-clock time spent in a named block. Safe to
        re-enter the same name within a cycle — durations sum."""
        t0 = time.monotonic()
        try:
            yield
        finally:
            elapsed = time.monotonic() - t0
            self._phases[name] = self._phases.get(name, 0.0) + elapsed

    def record_phase(self, name: str, seconds: float) -> None:
        """Add an already-measured duration to a named phase without
        using a context manager. Useful when the block being measured
        is a large try/except that would otherwise require reformatting
        many lines. Callers do `t0 = time.monotonic(); ...work...;
        metrics.record_phase("name", time.monotonic() - t0)`."""
        self._phases[name] = self._phases.get(name, 0.0) + float(seconds)

    def mark_event(self, name: str) -> None:
        """Record a discrete event (e.g. 'shm_reconnect_l1')."""
        self._events.append(name)

    def add_count(self, name: str, n: int = 1) -> None:
        """Add n to a per-cycle tally (e.g. 'kalman_rows_refused')."""
        self._counts[name] = self._counts.get(name, 0) + int(n)

    def set_total(self, name: str, value: int) -> None:
        """Record a cumulative tally (e.g. rows refused since start)."""
        self._totals[name] = int(value)

    def finalize_and_emit(self) -> Dict[str, object]:
        """Compute loop duration, sample RSS, emit to disk + journald.
        Returns the payload dict for inspection/testing."""
        loop_sec = time.monotonic() - self._cycle_start
        rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        wd_pct = (
            (loop_sec / self.watchdog_sec * 100.0)
            if self.watchdog_sec > 0 else 0.0
        )

        payload: Dict[str, object] = {
            "schema": SCHEMA_VERSION,
            "utc_published": _iso_z(datetime.now(timezone.utc)),
            "cycle_index": self._cycle_index,
            "first_cycle": self._cycle_index == 0,
            "loop_duration_sec": round(loop_sec, 6),
            "watchdog_budget_sec": self.watchdog_sec,
            "watchdog_consumed_pct": round(wd_pct, 3),
            "rss_kb": int(rss_kb),
            "phases": {k: round(v, 6) for k, v in self._phases.items()},
            "events": list(self._events),
            "counts": dict(self._counts),
            "totals": dict(self._totals),
        }

        self._log_structured(payload)
        self._atomic_write(payload)
        self._cycle_index += 1
        return payload

    def _log_structured(self, payload: dict) -> None:
        phases = payload["phases"]
        phase_str = " ".join(f"{k}={v:.3f}" for k, v in sorted(phases.items()))
        events_str = ",".join(payload["events"]) if payload["events"] else "-"
        totals = payload.get("totals") or {}
        totals_str = (" totals=" + ",".join(
            f"{k}:{v}" for k, v in sorted(totals.items()))) if totals else ""
        now = self._clock()
        notable = (
            bool(payload["events"])
            or payload["watchdog_consumed_pct"] >= SLOW_CYCLE_WD_PCT
        )
        if (notable or self._last_info_s is None
                or now - self._last_info_s >= self.log_interval_s):
            level = logging.INFO
            self._last_info_s = now
        else:
            level = logging.DEBUG
        log.log(
            level,
            "fusion_metrics cycle=%d loop=%.3fs wd_pct=%.1f rss_kb=%d %s events=%s%s",
            payload["cycle_index"],
            payload["loop_duration_sec"],
            payload["watchdog_consumed_pct"],
            payload["rss_kb"],
            phase_str,
            events_str,
            totals_str,
        )

    def _atomic_write(self, payload: dict) -> None:
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                dir=str(self.path.parent),
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
                encoding="utf-8",
            ) as tmp:
                json.dump(payload, tmp, separators=(",", ":"))
                tmp.flush()
                os.fsync(tmp.fileno())
                tmp_path = tmp.name
            os.replace(tmp_path, self.path)
        except OSError as e:
            log.warning("FusionLoopMetrics: failed to write %s: %s", self.path, e)


def _iso_z(dt: datetime) -> str:
    return dt.isoformat(timespec="microseconds").replace("+00:00", "Z")
