"""Append-only JSONL ledger for WWVB decode passes and decoded frames.

One line per decode pass (kind="pass") records the bulk DSP summary
(buffer size, magnitude, residual carrier offset, second/bit counts,
frame count).  One line per detected frame (kind="frame") records the
decoded minute UTC, DST/leap fields, parity/sync error counts, polarity,
and the wallclock-vs-decoded offset.

The point is to answer two questions over many days of operation:

    1. "When does our WWVB decode actually work?" — diurnal availability
       at this site/antenna.  Pass lines plotted over time make this
       trivial; frame lines confirm successful decodes.
    2. "When it works, how does it compare?" — `vs_wallclock_s` is the
       difference (decoded_minute_utc − host_wallclock_at_decode_time),
       which carries the WWVB skywave propagation delay (~3-10 ms,
       diurnally varying) plus host clock error.  Useful as an
       independent cross-check on the rest of the timing hierarchy
       before any propagation-delay calibration is wired in.

Files rotate daily by UTC date.  Append-only with line-buffered writes
so a crash never loses more than the last partial line.  No formatting
beyond compact JSON — postprocess with jq or pandas as needed.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import threading
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

UTC = _dt.timezone.utc


def _jsonable(x: float) -> Optional[float]:
    """NaN/inf -> None.  ``json.dumps`` emits a bare ``NaN``, which is not valid
    JSON and breaks jq — the postprocessor this module's docstring recommends.
    An unmeasurable SNR is honestly absent, not a number."""
    x = float(x)
    return None if (x != x or x in (float("inf"), float("-inf"))) else x


class WwvbLedger:
    """Daily-rotated JSONL writer.

    Thread-safe; the decode loop calls record_pass / record_frame from a
    single worker thread today, but the lock keeps it correct if that
    changes.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._date: Optional[_dt.date] = None
        self._fh = None

    def _open_for(self, when: _dt.datetime) -> None:
        date = when.date()
        if self._fh is not None and self._date == date:
            return
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug(f"ledger rotate close: {exc}")
        path = self._root / f"{date.isoformat()}.jsonl"
        # line-buffered append; a crash only loses the partial trailing line
        self._fh = open(path, "a", buffering=1)
        self._date = date

    def _write(self, payload: dict) -> None:
        with self._lock:
            try:
                now = _dt.datetime.now(UTC)
                self._open_for(now)
                self._fh.write(json.dumps(payload, separators=(",", ":")) + "\n")
            except Exception as exc:
                logger.warning(f"wwvb_ledger write failed: {exc}")

    def record_pass(
        self,
        *,
        ts: _dt.datetime,
        buffer_s: float,
        mean_amp: float,
        carrier_offset_hz: float,
        seconds_detected: int,
        bits: int,
        frames: int,
        frames_implausible: int = 0,
        snr_db: float = float("nan"),
    ) -> None:
        """One row per decode pass — success or not.

        ``snr_db`` is the field that lets a blind pass be told from a working
        one.  Nothing else here does: measured over 432 passes on 2026-08-19,
        ``carrier_offset_hz`` read 0.0 on all ten decodes and on 211 of the 422
        blind passes, and ``seconds_detected`` read *higher* when blind (up to
        95) than on any real decode (max 90).  A source that cannot report its
        own blindness must not join the Fusion pool.
        """
        self._write({
            "ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "kind": "pass",
            "buffer_s": round(buffer_s, 2),
            "mean_amp": float(mean_amp),
            "carrier_offset_hz": round(float(carrier_offset_hz), 4),
            "seconds_detected": int(seconds_detected),
            "bits": int(bits),
            "frames": int(frames),
            "frames_implausible": int(frames_implausible),
            "snr_db": _jsonable(snr_db),
        })

    def record_frame(
        self,
        *,
        ts: _dt.datetime,
        minute_of_frame: _dt.datetime,
        dst_state: Optional[str],
        leap_second: Optional[str],
        parity_errors: int,
        sync_errors: int,
        inverted_polarity: bool,
        mean_amp: float,
        plausible: bool = True,
    ) -> None:
        """One row per detected frame.

        Implausible frames are recorded, not dropped — the garbage rate is
        itself evidence about the decoder, and hiding it would make the ledger
        look healthier than the receiver is.
        """
        vs_wallclock_s = (minute_of_frame - ts).total_seconds()
        self._write({
            "ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "kind": "frame",
            "minute": minute_of_frame.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "dst": dst_state or "?",
            "leap": leap_second or "?",
            "parity_errors": int(parity_errors),
            "sync_errors": int(sync_errors),
            "inverted": bool(inverted_polarity),
            "vs_wallclock_s": round(vs_wallclock_s, 2),
            "mean_amp": float(mean_amp),
            "plausible": bool(plausible),
        })

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.close()
                except Exception as exc:  # pragma: no cover
                    logger.debug(f"ledger close: {exc}")
                self._fh = None
                self._date = None


__all__ = ["WwvbLedger"]
