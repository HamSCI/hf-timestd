"""Periodic StreamQuality snapshot writer.

Surfaces per-recorder RTP loss / completeness data to a file readable
by sigmond's `hf-timestd quality --json` CLI subcommand.  The daemon
writes; the CLI reads.  No IPC; the file IS the contract.

Why a file rather than IPC: matches sigmond's existing
`<binary> inventory --json` / `validate --json` pattern (subprocess
spawning a fresh CLI process), keeps the daemon decoupled from
sigmond, and lets the CLI run usefully even when the daemon is dead
(it returns a stale snapshot with `stale_seconds` set).

Design: stateful object, no thread.  `tick()` is called from the
core_recorder main loop on a fixed cadence.  Coupling to the main
loop is intentional — if the loop hangs, the snapshot goes stale,
which sigmond uses as a daemon-health signal.

See `tasks/plan-stream-quality-surface.md` (in the sigmond repo) for
the full design rationale.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


SCHEMA_VERSION = 1
DEFAULT_PATH = "/run/hf-timestd/quality.json"


class QualitySnapshotWriter:
    """Per-recorder quality snapshot, written atomically to ``path``.

    Counters from ka9q-python's ``StreamQuality`` are cumulative since
    stream start; this class tracks the previous snapshot per recorder
    to emit per-second rates alongside the totals.

    Stream restarts are detected by a current-total < previous-total
    drop and yield a 0 rate (rather than a negative rate).
    """

    def __init__(self, recorders: Dict[str, Any], *,
                 path: str = DEFAULT_PATH,
                 instance: str = "default",
                 clock: Callable[[], float] = time.time):
        self._recorders = recorders
        self._path = Path(path)
        self._instance = instance
        self._clock = clock
        # description -> {captured_at, packets_received, packets_lost, ...}
        self._previous: Dict[str, Dict[str, Any]] = {}

    def tick(self) -> None:
        """Snapshot every recorder, atomic-write to disk.

        Swallows write errors at WARN — a missing /run directory or
        permission glitch must not crash the recorder's main loop.
        """
        now = self._clock()
        try:
            entries = [self._snapshot_one(desc, rec, now)
                       for desc, rec in self._recorders.items()]
        except Exception:                            # noqa: BLE001
            logger.exception("quality snapshot collection failed")
            return

        payload = {
            "schema_version": SCHEMA_VERSION,
            "captured_at":    now,
            "instance":       self._instance,
            "client":         "hf-timestd",
            "recorders":      entries,
            "summary":        self._summarise(entries),
        }
        try:
            self._atomic_write(payload)
        except OSError as e:
            logger.warning("quality snapshot write failed: %s", e)

    # ------------------------------------------------------------------
    # Per-recorder snapshot + delta-rate
    # ------------------------------------------------------------------

    def _snapshot_one(self, description: str, recorder: Any,
                      now: float) -> Dict[str, Any]:
        cfg = getattr(recorder, "config", None)
        entry: Dict[str, Any] = {
            "description":  description,
            "frequency_hz": getattr(cfg, "frequency_hz", None),
            "ssrc":         getattr(cfg, "ssrc", None),
            "stream_state": _stream_state(recorder),
            "uptime_seconds": _uptime_seconds(recorder, now),
        }

        q = getattr(recorder, "last_quality", None)
        if q is None:
            entry["no_data"] = True
            return entry

        # Cumulative counters from StreamQuality.  Internal keys drop
        # any "total_" prefix that ka9q-python applied — the suffix
        # below makes the public schema "<name>_total", and we don't
        # want "total_X_total" doubling-up.
        cur = {
            "packets_received":    int(getattr(q, "rtp_packets_received", 0) or 0),
            "packets_lost":        int(getattr(q, "rtp_packets_lost", 0) or 0),
            "packets_late":        int(getattr(q, "rtp_packets_late", 0) or 0),
            "packets_duplicate":   int(getattr(q, "rtp_packets_duplicate", 0) or 0),
            "packets_resequenced": int(getattr(q, "rtp_packets_resequenced", 0) or 0),
            "gaps_filled":         int(getattr(q, "total_gaps_filled", 0) or 0),
            "gap_events":          int(getattr(q, "total_gap_events", 0) or 0),
            "samples_delivered":   int(getattr(q, "total_samples_delivered", 0) or 0),
        }
        entry.update({f"{k}_total": v for k, v in cur.items()})
        entry["completeness_pct"] = float(
            getattr(q, "completeness_pct", 0.0) or 0.0
        )

        # Delta rates against previous snapshot (if any)
        prev = self._previous.get(description)
        rates = self._compute_rates(prev, cur, now)
        entry.update(rates)

        # Stash current cumulative for next tick.
        self._previous[description] = {"captured_at": now, **cur}
        return entry

    @staticmethod
    def _compute_rates(prev: Optional[Dict[str, Any]],
                       cur: Dict[str, int],
                       now: float) -> Dict[str, float]:
        """Per-second rates for the loss-relevant counters.

        Stream restart (cur < prev) clamps to 0 rather than a negative.
        """
        rate_keys = (
            "packets_lost", "packets_late",
            "packets_duplicate", "packets_resequenced",
        )
        out = {f"{k}_rate": 0.0 for k in rate_keys}
        if not prev:
            return out
        interval = now - float(prev.get("captured_at", 0.0) or 0.0)
        if interval <= 0:
            return out
        for k in rate_keys:
            delta = cur[k] - int(prev.get(k, 0) or 0)
            if delta < 0:
                delta = 0                            # stream restarted
            out[f"{k}_rate"] = round(delta / interval, 4)
        return out

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    @staticmethod
    def _summarise(entries: list[Dict[str, Any]]) -> Dict[str, Any]:
        """Aggregate fields for a one-line health read."""
        with_data = [e for e in entries if not e.get("no_data")]
        if not with_data:
            return {
                "recorder_count":         len(entries),
                "recorders_with_data":    0,
                "min_completeness_pct":   None,
                "total_packets_lost":     0,
                "max_packets_lost_rate":  0.0,
            }
        return {
            "recorder_count":        len(entries),
            "recorders_with_data":   len(with_data),
            "min_completeness_pct":  min(e["completeness_pct"]
                                         for e in with_data),
            "total_packets_lost":    sum(e["packets_lost_total"]
                                         for e in with_data),
            "max_packets_lost_rate": max(e["packets_lost_rate"]
                                         for e in with_data),
        }

    # ------------------------------------------------------------------
    # Atomic write
    # ------------------------------------------------------------------

    def _atomic_write(self, payload: Dict[str, Any]) -> None:
        """Write to a sibling .tmp then ``os.replace``.

        os.replace is POSIX-atomic on the same filesystem, so a CLI
        reading concurrently never observes a partial file.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        os.replace(tmp, self._path)


# ---------------------------------------------------------------------------
# Recorder-attribute accessors — tolerant of partial mocks in tests.
# ---------------------------------------------------------------------------

def _stream_state(recorder: Any) -> str:
    state = getattr(recorder, "state", None)
    if state is None:
        return "UNKNOWN"
    return getattr(state, "value", str(state))


def _uptime_seconds(recorder: Any, now: float) -> float:
    start = getattr(recorder, "session_start_time", None)
    if not start:
        return 0.0
    return round(now - float(start), 2)
