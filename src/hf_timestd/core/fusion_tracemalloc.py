"""GC-sampling memory-growth diagnostic for the fusion loop.

The 2026-04-23 fusion audit identified "memory growth" as a real red
flag, and the 2026-05-15 measurement-phase confirmed it empirically
(245 MB/h before any fix, 203 MB/h after _iri_cache eviction).

The module is named ``fusion_tracemalloc`` for historical reasons —
the first iteration used Python's ``tracemalloc``. That approach
proved unworkable on bee1: per-allocation stack tracing made fusion's
1.9 s p50 loop balloon to ~150 s (80×) due to the HDF5-heavy
workload's allocation rate, which blew the WatchdogSec budget even
after extending it. Replaced with a sampling approach using
``gc.get_objects()`` — zero overhead between snapshots, and the
per-snapshot cost (one walk of all Python objects) is paid once per
N cycles instead of on every allocation.

The trade-off: we lose file:line attribution for allocation sites.
What we gain instead is *type-level* growth visibility — e.g.,
"500 MB more bytes in numpy.ndarray objects across this window" —
which is usually enough to point at the leaking subsystem.

Usage:
  Enable via env vars:
    HF_TIMESTD_TRACEMALLOC=1                  — enable
    HF_TIMESTD_TRACEMALLOC_INTERVAL=N         — cycles between samples
                                                 (default 50; first
                                                 diff after ~8 min)
  Output:
    Every interval cycles, take a sample of gc.get_objects() bucketed
    by type, and log the top-10 growers vs the previous sample as
    ``fusion_objgrowth cycle=X delta_kb=+Y top: type +A KB (+N obj)...``.

Per-snapshot cost: ~1-2 s for a fusion process with ~1 M Python
objects (walks gc.get_objects() once, calls sys.getsizeof on each).
Comfortable within WatchdogSec=120 budget; no drop-in needed.
"""

from __future__ import annotations

import gc
import logging
import os
import sys
from collections import defaultdict
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)


def _flag_enabled() -> bool:
    """Read HF_TIMESTD_TRACEMALLOC env var. Truthy values enable."""
    val = os.environ.get("HF_TIMESTD_TRACEMALLOC", "").strip().lower()
    return val in ("1", "true", "yes", "on")


def _interval_cycles() -> int:
    """Read HF_TIMESTD_TRACEMALLOC_INTERVAL. Default 50 cycles."""
    try:
        n = int(os.environ.get("HF_TIMESTD_TRACEMALLOC_INTERVAL", "50"))
        return max(10, n)
    except ValueError:
        return 50


def _take_snapshot() -> Dict[str, Tuple[int, int]]:
    """Walk gc.get_objects() and return {type_name: (count, total_size_bytes)}.

    A gc.collect() is forced first so transient unreachable objects don't
    skew the count. sys.getsizeof is called per-object — this gives the
    *shallow* size (no recursion into contents), which is what we want
    for type-level growth attribution (the references each object holds
    show up under their own types anyway).
    """
    gc.collect()
    buckets: Dict[str, Tuple[int, int]] = defaultdict(lambda: (0, 0))
    counts: Dict[str, int] = defaultdict(int)
    sizes: Dict[str, int] = defaultdict(int)
    for obj in gc.get_objects():
        # Some C-extension types raise on type(obj) — guard against weird cases.
        try:
            t = type(obj).__name__
            s = sys.getsizeof(obj)
        except Exception:
            continue
        counts[t] += 1
        sizes[t] += s
    return {t: (counts[t], sizes[t]) for t in counts}


class TracemallocDiagnostic:
    """Periodic gc-snapshot diff logger. Name kept for backwards
    compatibility with the original tracemalloc-based design."""

    def __init__(self, interval: Optional[int] = None):
        self._interval = interval if interval is not None else _interval_cycles()
        self._cycle = 0
        self._previous: Optional[Dict[str, Tuple[int, int]]] = None
        logger.info(
            "fusion_objgrowth enabled: interval=%d cycles. Per-snapshot cost ~1-2 s "
            "(walks gc.get_objects). First diff fires at cycle %d.",
            self._interval, self._interval,
        )
        # First snapshot is the baseline — log top absolute types so the
        # steady-state working set is visible.
        self._previous = _take_snapshot()
        self._log_top_abs(self._previous, label='baseline_abs')

    def tick(self) -> None:
        """Call once per fusion cycle. Triggers a snapshot+diff at
        the configured interval; otherwise a fast no-op increment."""
        self._cycle += 1
        if self._cycle % self._interval != 0:
            return
        current = _take_snapshot()
        if self._previous is not None:
            self._log_top_delta(self._previous, current, label=f'cycle={self._cycle}')
        self._previous = current

    @staticmethod
    def _log_top_abs(snap: Dict[str, Tuple[int, int]], label: str, top_n: int = 10) -> None:
        total_kb = sum(s for _, s in snap.values()) / 1024.0
        # Sort by total bytes descending.
        top = sorted(snap.items(), key=lambda kv: -kv[1][1])[:top_n]
        parts = [
            f"{name:24s} {size/1024:9.1f} KB (count {count})"
            for name, (count, size) in top
        ]
        logger.info(
            "fusion_objgrowth %s total_kb=%.1f top:\n  %s",
            label, total_kb, '\n  '.join(parts),
        )

    @staticmethod
    def _log_top_delta(
        prev: Dict[str, Tuple[int, int]],
        curr: Dict[str, Tuple[int, int]],
        label: str,
        top_n: int = 10,
    ) -> None:
        all_types = set(prev) | set(curr)
        deltas = []
        for t in all_types:
            pc, ps = prev.get(t, (0, 0))
            cc, cs = curr.get(t, (0, 0))
            deltas.append((t, cc - pc, cs - ps))
        # Sort by size delta descending — biggest growers first.
        deltas.sort(key=lambda x: -x[2])
        top_growers = deltas[:top_n]
        # Also surface the biggest shrinkers for symmetry / sanity.
        bottom_shrinkers = sorted(deltas, key=lambda x: x[2])[:3]

        total_size_delta_kb = sum(d[2] for d in deltas) / 1024.0
        total_now_kb = sum(s for _, s in curr.values()) / 1024.0

        grower_parts = [
            f"{name:24s} {size_d/1024:+9.1f} KB (count {count_d:+d})"
            for name, count_d, size_d in top_growers
        ]
        shrinker_parts = [
            f"{name:24s} {size_d/1024:+9.1f} KB (count {count_d:+d})"
            for name, count_d, size_d in bottom_shrinkers
        ]
        logger.info(
            "fusion_objgrowth %s delta_kb=%+.1f total_kb=%.1f top_growers:\n  %s\nbottom_shrinkers:\n  %s",
            label, total_size_delta_kb, total_now_kb,
            '\n  '.join(grower_parts),
            '\n  '.join(shrinker_parts),
        )


def maybe_create() -> Optional['TracemallocDiagnostic']:
    """Factory: return a TracemallocDiagnostic if env var is set, else None."""
    if not _flag_enabled():
        return None
    return TracemallocDiagnostic()
