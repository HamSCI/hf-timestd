"""
FusionLoopMetrics — per-cycle instrumentation for timestd-fusion.

Captures wall-clock duration per named phase, RSS sampled via
resource.getrusage, watchdog budget consumption, and discrete events.
Two sinks per cycle:

  - /run/hf-timestd/fusion_metrics.json — atomic point-in-time snapshot
    (same tempfile + fsync + os.replace pattern as fusion_status.json).
  - One structured INFO line to journald, prefixed `fusion_metrics`,
    so historical analysis can use `journalctl -u timestd-fusion
    -g '^fusion_metrics'`.

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
      "events": ["shm_reconnect_l1", "kalman_lock"]
    }
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
from typing import Dict, Iterator, List

log = logging.getLogger(__name__)

SCHEMA_VERSION = "v1"
DEFAULT_PATH = Path("/run/hf-timestd/fusion_metrics.json")


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
    ):
        self.watchdog_sec = float(watchdog_sec)
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
        self._cycle_start = time.monotonic()

    def start_cycle(self) -> None:
        """Call at the top of each fusion loop iteration. Resets
        per-cycle state; cycle_index is incremented at emit time."""
        self._cycle_start = time.monotonic()
        self._phases = {}
        self._events = []

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
        }

        self._log_structured(payload)
        self._atomic_write(payload)
        self._cycle_index += 1
        return payload

    def _log_structured(self, payload: dict) -> None:
        phases = payload["phases"]
        phase_str = " ".join(f"{k}={v:.3f}" for k, v in sorted(phases.items()))
        events_str = ",".join(payload["events"]) if payload["events"] else "-"
        log.info(
            "fusion_metrics cycle=%d loop=%.3fs wd_pct=%.1f rss_kb=%d %s events=%s",
            payload["cycle_index"],
            payload["loop_duration_sec"],
            payload["watchdog_consumed_pct"],
            payload["rss_kb"],
            phase_str,
            events_str,
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
