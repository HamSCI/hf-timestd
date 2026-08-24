"""Durable append-only ledger of every T6 native anchor.

The anchor tuple is the minimal durable record of T6 (mjh, 2026-08-24):

    anchor_utc_ns = named_second·1e9 + asserted_chain_delay − sub_ns

so a row carrying the raw components separately makes any future
recalibration of the asserted terms pure arithmetic over the ledger —
retroactive correction of every historical anchor with no raw-IQ
archive.  ``sub_ns``, the fine stage's measured term, is recoverable as
``named_second_utc_ns + chain_delay_ns − anchor_utc_ns``.

Until this module the tuples lived only in 300 s-throttled journal
lines, which rotate: on AC0G-B4 everything before 2026-08-23 was
already gone the first time anyone wanted the history.

Format: one JSON object per line, one file per UTC day
(``t6-anchors-YYYYMMDD.jsonl``) under ``DEFAULT_DIR``.  ~250 B per
anchor at one fine estimate per 30 s fold ≈ 0.7 MB/day.  The directory
lives under ``state/`` which the resource guardian does not evict
(quota pressure targets ``raw_buffer``/``phase2``).

Hot-path discipline: ``append`` never raises — a ledger failure must
never take down the calibrator path.  Failures log once per process.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

DEFAULT_DIR = Path("/var/lib/timestd/state/t6-anchor-ledger")


class T6AnchorLedger:
    def __init__(
        self,
        dir_path: Path = DEFAULT_DIR,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self.dir_path = Path(dir_path)
        self._now = now_fn
        self._last_key: Optional[tuple] = None
        self._warned = False

    def append(
        self,
        anchor,
        *,
        authority_state: Optional[str] = None,
        delay_budget_ns: Optional[int] = None,
        filter_group_delay_ns: Optional[int] = None,
    ) -> bool:
        """Append one anchor row.  Returns True when a line was written.

        Consecutive duplicates (same ``anchor_rtp``/``anchor_utc_ns``)
        are suppressed — the authority re-delivers the same anchor on
        repeated decisions between fold blocks.
        """
        if anchor is None:
            return False
        key = (int(anchor.anchor_rtp), int(anchor.anchor_utc_ns))
        if key == self._last_key:
            return False
        now = float(self._now())
        row = {
            "logged_at_unix": now,
            "anchor_rtp": int(anchor.anchor_rtp),
            "anchor_utc_ns": int(anchor.anchor_utc_ns),
            "named_second_utc_ns": int(anchor.captured_at_utc_ns),
            "sample_rate_hz": int(anchor.sample_rate_hz),
            "chain_delay_ns": int(anchor.chain_delay_ns),
            "captured_via_tier": str(anchor.captured_via_tier),
            "authority_state": authority_state,
            "delay_budget_ns": delay_budget_ns,
            "filter_group_delay_ns": filter_group_delay_ns,
        }
        day = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y%m%d")
        path = self.dir_path / f"t6-anchors-{day}.jsonl"
        try:
            self.dir_path.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, separators=(",", ":")) + "\n")
        except OSError as exc:
            if not self._warned:
                self._warned = True
                logger.warning(
                    "T6 anchor ledger: cannot write %s (%s) — anchors "
                    "will not be persisted this run.  (Logged once.)",
                    path, exc,
                )
            return False
        self._last_key = key
        return True
