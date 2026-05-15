"""Tracemalloc-based memory-growth diagnostic for the fusion loop.

The 2026-04-23 fusion audit identified "memory growth" as a real red
flag, and the 2026-05-15 measurement-phase confirmed it empirically
(245 MB/h before any fix, 203 MB/h after the _iri_cache eviction fix).
Static code review found the obvious unbounded structures; the
remaining ~200 MB/h needs runtime allocation-site evidence to pin down.

Usage:
  Enable via environment variable HF_TIMESTD_TRACEMALLOC=1 (and
  optionally HF_TIMESTD_TRACEMALLOC_INTERVAL=N for cycle cadence
  between diff snapshots; default 200, i.e. ~33 min at 10 s/cycle),
  and HF_TIMESTD_TRACEMALLOC_FRAMES=N for the stack depth captured per
  allocation (default 3 — enough to attribute fusion's allocations to
  the fusion-side caller without the extreme overhead of deep traces).
  Costs scale with frames: frames=1 ~1-2% CPU, frames=3 ~5-10%,
  frames=25 several × the normal loop time. Empirically frames=25
  pushed fusion's 1.9 s p50 loop past the 120 s watchdog on bee1
  2026-05-15 — the diagnostic blew up the service. With frames=3 and
  interval=200 the overhead is bearable but **the operator should also
  temporarily extend systemd WatchdogSec** (e.g. via a drop-in
  raising it to 300 s) during a diagnosis window to leave headroom
  for HDF5 reads + tracemalloc bookkeeping.
  When disabled, the diagnostic object is None and the per-cycle
  tick() is a no-op.

Output:
  Every interval cycles, take_snapshot() compares to the previous
  snapshot and logs the top-10 lines with the largest size delta
  via the INFO logger. Format mirrors fusion_loop_metrics so the
  journal export is grep-friendly:

    fusion_tracemalloc cycle=N delta_kb=X total_kb=Y top=[
      file:line +XX.X KB (count +N), ...
    ]

  At process exit (or first call), a baseline snapshot is also
  logged with TOP-10 absolute allocators so you can compare growth
  vs steady-state working set.
"""

from __future__ import annotations

import logging
import os
import tracemalloc
from typing import Optional

logger = logging.getLogger(__name__)


def _flag_enabled() -> bool:
    """Read HF_TIMESTD_TRACEMALLOC env var. Truthy values enable."""
    val = os.environ.get("HF_TIMESTD_TRACEMALLOC", "").strip().lower()
    return val in ("1", "true", "yes", "on")


def _interval_cycles() -> int:
    """Read HF_TIMESTD_TRACEMALLOC_INTERVAL. Default 200 cycles."""
    try:
        n = int(os.environ.get("HF_TIMESTD_TRACEMALLOC_INTERVAL", "200"))
        return max(10, n)
    except ValueError:
        return 200


def _frames_depth() -> int:
    """Read HF_TIMESTD_TRACEMALLOC_FRAMES. Default 3 (cheap-enough)."""
    try:
        n = int(os.environ.get("HF_TIMESTD_TRACEMALLOC_FRAMES", "3"))
        return max(1, min(n, 50))
    except ValueError:
        return 3


class TracemallocDiagnostic:
    """Per-cycle tracemalloc snapshot + diff logger.

    Construction starts tracemalloc tracing (25-frame depth, enough to
    distinguish call sites in fusion's nested code paths). tick()
    advances the cycle counter and triggers a snapshot+diff every
    `interval` calls. The diagnostic is intentionally lossy — it logs
    deltas, not full snapshots — so the journal doesn't drown in MB of
    per-line allocation traces.
    """

    def __init__(self, interval: Optional[int] = None, frames: Optional[int] = None):
        self._interval = interval if interval is not None else _interval_cycles()
        self._frames = frames if frames is not None else _frames_depth()
        self._cycle = 0
        self._previous_snapshot: Optional[tracemalloc.Snapshot] = None
        tracemalloc.start(frames)
        logger.info(
            "fusion_tracemalloc enabled: interval=%d cycles, frames=%d (each Python "
            "allocation gets a stack trace; expect ~1%% CPU overhead)",
            self._interval, self._frames,
        )
        # First snapshot serves as the baseline — log top absolute
        # allocators once so steady-state working set is visible.
        self._previous_snapshot = tracemalloc.take_snapshot()
        self._log_top(self._previous_snapshot.statistics('lineno'), label='baseline_abs', kind='abs')

    def tick(self) -> None:
        """Call once per fusion cycle. Triggers a snapshot+diff at
        the configured interval; otherwise a fast no-op increment."""
        self._cycle += 1
        if self._cycle % self._interval != 0:
            return
        if self._previous_snapshot is None:
            self._previous_snapshot = tracemalloc.take_snapshot()
            return
        current = tracemalloc.take_snapshot()
        diff = current.compare_to(self._previous_snapshot, key_type='lineno')
        self._log_top(diff, label=f'cycle={self._cycle}', kind='delta')
        self._previous_snapshot = current

    def _log_top(self, stats, label: str, kind: str, top_n: int = 10) -> None:
        """Format and log the top-N statistics entries."""
        total_kb = sum(s.size for s in stats) / 1024.0
        top = stats[:top_n]
        if kind == 'delta':
            # For delta snapshots, size_diff/count_diff matter
            parts = [
                f"{_short_origin(s.traceback)} {s.size_diff/1024:+8.1f} KB (count {s.count_diff:+d})"
                for s in top
            ]
            total_delta_kb = sum(s.size_diff for s in stats) / 1024.0
            logger.info(
                "fusion_tracemalloc %s delta_kb=%.1f total_kb=%.1f top:\n  %s",
                label, total_delta_kb, total_kb, '\n  '.join(parts),
            )
        else:
            parts = [
                f"{_short_origin(s.traceback)} {s.size/1024:8.1f} KB (count {s.count})"
                for s in top
            ]
            logger.info(
                "fusion_tracemalloc %s total_kb=%.1f top:\n  %s",
                label, total_kb, '\n  '.join(parts),
            )


def _short_origin(tb) -> str:
    """Format the topmost frame as relative-path:line."""
    if not tb:
        return "<unknown>"
    frame = tb[0]
    # Show only the last path component + line to keep log lines compact.
    fname = frame.filename.rsplit('/', 2)
    short = '/'.join(fname[-2:]) if len(fname) >= 2 else frame.filename
    return f"{short}:{frame.lineno}"


def maybe_create() -> Optional['TracemallocDiagnostic']:
    """Factory: return a TracemallocDiagnostic if env var is set, else None.

    Callers wire this in alongside FusionLoopMetrics:

        td = maybe_create()
        # ... in main loop ...
        if td is not None: td.tick()

    Idempotent against multiple calls only if tracemalloc isn't already
    started by something else (we don't try to coexist with another
    tracer). If called twice, the second call no-ops with a warning.
    """
    if not _flag_enabled():
        return None
    if tracemalloc.is_tracing():
        logger.warning(
            "fusion_tracemalloc: tracemalloc already running; skipping "
            "second initialisation"
        )
        return None
    return TracemallocDiagnostic()
