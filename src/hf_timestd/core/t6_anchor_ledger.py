"""Durable append-only ledger of every T6 native anchor.

The anchor tuple is the minimal durable record of T6 (mjh, 2026-08-24):

    anchor_utc_ns = named_second·1e9 + asserted_chain_delay − sub_ns

so a row carrying the raw components separately makes any future
recalibration of the asserted terms pure arithmetic over the ledger —
retroactive correction of every historical anchor with no raw-IQ
archive.  ``sub_ns``, the fine stage's measured term, is recoverable as
``named_second_utc_ns + chain_delay_ns − anchor_utc_ns``.

The ``labeling_convention`` column records which reference plane of
docs/design/MEASUREMENT_MODEL.md §1 the row's label sits in (§8 names the
planes ``measurand_plane`` and ``calibration_plane``); it selects nothing
else.

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

# Bumped when a row gains or changes meaning, so a reader can tell
# generations apart (hf-timestd#39: a sidecar with no version makes a
# corrected file indistinguishable from an uncorrected one).
SCHEMA = "t6-anchor/2"

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
        labeling_convention: Optional[str] = None,
        peer_rtp: Optional[int] = None,
        peer_rate_hz: Optional[int] = None,
        quality: Optional[dict] = None,
        label_drift_samples: Optional[int] = None,
        zero_fill: Optional[dict] = None,
    ) -> bool:
        """Append one anchor row.  Returns True when a line was written.

        Consecutive duplicates (same ``anchor_rtp``/``anchor_utc_ns``)
        are suppressed — the authority re-delivers the same anchor on
        repeated decisions between fold blocks.

        Schema v2 fields (all optional, all OMITTED when unknown — a
        missing measurement must never read as a measured zero):

        ``peer_rtp`` / ``peer_rate_hz``
            The same instant expressed in the METROLOGY channels' counter
            space.  ``anchor_rtp`` is in the T6 channel's own 96 kHz
            space, which does NOT relate to the 24 kHz channels by
            scaling — B4 measured a 362,095,021-sample (~3772 s) offset —
            so without this a reader holding the ledger and the metrology
            IQ cannot connect them.  Computed at record time, where
            millions of pair observations make the least-late floor
            converge (see ``cross_channel_rtp``).
        ``quality``
            The matched filter's own metrics (plateau amplitude, fit rms,
            seconds folded, subsample).  This is what archiving the IQ
            would have bought and the ledger cannot: you cannot re-run
            the MF later.  Both the 2026-05-23 sidelobe phantom and the
            2026-08-25 livelock were precise-looking anchors that were
            wrong; these fields let a reader JUDGE an anchor instead of
            inheriting it.
        ``label_drift_samples``
            Counter continuity since capture (the calibrator's
            ``_lbl_drift``).  An anchor only labels correctly if the
            ruler did not move underneath it.
        ``zero_fill``
            Cumulative radiod block drops on the T6 channel since start.
            Nothing else counts them: the channel is not archived, so it
            writes no raw-buffer sidecar and cannot appear in
            ``gap_hourly``.

        ``labeling_convention``
            ``"legacy"`` or ``"content"``.  Its absence is exactly what
            made the 2026-08-25 15:00–15:07 content window
            indistinguishable from its neighbours afterwards.
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
            "schema": SCHEMA,
        }
        # Omitted, never defaulted: see the docstring.
        for k, v in (
            ("labeling_convention", labeling_convention),
            ("peer_rtp", peer_rtp),
            ("peer_rate_hz", peer_rate_hz),
            ("quality", quality),
            ("label_drift_samples", label_drift_samples),
            ("zero_fill", zero_fill),
        ):
            if v is not None:
                row[k] = v
        day = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y%m%d")
        path = self.dir_path / f"t6-anchors-{day}.jsonl"
        try:
            line = json.dumps(row, separators=(",", ":")) + "\n"
            self.dir_path.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
        except (OSError, TypeError, ValueError) as exc:
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
