"""Publish T6's tie of the radiod input counter to UTC, for client programs.

``/run/hf-timestd/t6-anchor.json`` carries one input-sample index and the UTC
nanosecond it names, with the error of that tie.  chirp-recorder and
hfdl-recorder read it through ``hamsci_dsp.t6_anchor.read_t6_anchor``.  This
module writes exactly what that reader accepts.

Rules the reader enforces, and this writer meets:
  * ``updated_at`` is ISO UTC, whole seconds, trailing "Z".
  * Every number is finite; ``u_epoch_ns`` and the index are not negative.
  * ``anchor_input_index`` is null unless the T6 authority says AUTHORITATIVE.

The writer corrects nothing.  When T6 does not hold the plane, it publishes the
state and a null index; a client then writes no T6 time.  When no anchor or no
counter map exists, it writes nothing, and the old file ages out at the
reader's 120 s limit.
"""
from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .t3_shadow_comparator import t6_anchor_input_index

logger = logging.getLogger(__name__)

ANCHOR_PATH = Path("/run/hf-timestd/t6-anchor.json")
PUBLISH_INTERVAL_S = 60.0
ASSUMED_U_EPOCH_NS = 230.0     # the transport bound, when no sigma is measured
COVERAGE_K = 2.0


def build_t6_anchor(*, anchor, t6_map, authoritative: bool, authority_state: str,
                    sigma_ns: Optional[float], counter_epoch_id: str,
                    radiod_status: str, radiod_address: str = "",
                    now_utc_iso: str) -> dict:
    """The anchor document.

    ``radiod_status`` is the status name as configured, before discovery
    rewrites the address.  ``radiod_address`` is the address in use.
    """
    index = None
    if authoritative:
        index = t6_anchor_input_index(anchor, t6_map)
        if index is not None:
            index = float(index)
            if not math.isfinite(index) or index < 0:
                index = None
    measured = (isinstance(sigma_ns, (int, float)) and not isinstance(sigma_ns, bool)
                and math.isfinite(sigma_ns) and sigma_ns >= 0)
    return {
        "schema": 1,
        "updated_at": now_utc_iso,
        "radiod_status": radiod_status,
        "radiod_address": radiod_address,
        "t6_authority_state": authority_state,
        "anchor_input_index": index,
        "anchor_utc_ns": int(anchor.anchor_utc_ns),
        "input_samprate": int(t6_map.input_samprate),
        "input_epoch_gps_s": float(t6_map.input_epoch_gps_s),
        "counter_epoch_id": counter_epoch_id,
        "u_epoch_ns": float(sigma_ns) if measured else ASSUMED_U_EPOCH_NS,
        "k": COVERAGE_K,
        "p": None,
        "a_level_provenance": "measured" if measured else "assumed",
    }


def write_t6_anchor(doc: dict, path: Path = ANCHOR_PATH) -> None:
    """Atomic write: temp file in the same directory, fsync, rename.
    Mode 0644, so every consumer can read it."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".t6-anchor-",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _utc_iso_now() -> str:
    # The host clock only stamps the file's age; it enters no timing field.
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class T6AnchorPublisher:
    """One write per 60 s at most.  A failure logs and returns False."""

    def __init__(self, path: Path = ANCHOR_PATH,
                 mono_fn: Callable[[], float] = time.monotonic,
                 interval_s: float = PUBLISH_INTERVAL_S):
        self.path = Path(path)
        self._mono = mono_fn
        self._interval = interval_s
        self._last: Optional[float] = None
        self.write_failures = 0

    def maybe_publish(self, **kw) -> bool:
        now = self._mono()
        if self._last is not None and now - self._last < self._interval:
            return False
        self._last = now
        if kw.get("anchor") is None or kw.get("t6_map") is None:
            return False
        try:
            kw.setdefault("now_utc_iso", _utc_iso_now())
            write_t6_anchor(build_t6_anchor(**kw), self.path)
            return True
        except Exception as exc:  # noqa: BLE001 - never stop the recorder
            self.write_failures += 1
            logger.warning(f"t6-anchor.json not written ({self.path}): {exc}")
            return False
