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


def sigma_is_corrupt(sigma_ns) -> bool:
    """A sigma that is present but not a finite, non-negative number.
    None is absent, not corrupt."""
    if sigma_ns is None:
        return False
    if isinstance(sigma_ns, bool) or not isinstance(sigma_ns, (int, float)):
        return True
    return not (math.isfinite(sigma_ns) and sigma_ns >= 0)


def build_t6_anchor(*, anchor, t6_map, authoritative: bool, authority_state: str,
                    sigma_ns: Optional[float], counter_epoch_id: str,
                    radiod_status: str, radiod_address: str = "",
                    now_utc_iso: str) -> dict:
    """The anchor document.

    An authoritative anchor with a corrupt sigma gets a null index, so the
    reader refuses the file; the tie's error is unknown, and no number is
    invented for it.  ``radiod_status`` is the status name as configured, before discovery
    rewrites the address.  ``radiod_address`` is the address in use.
    """
    index = None
    if authoritative and not sigma_is_corrupt(sigma_ns):
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


# A registration summary older than this no longer speaks for the run.  The
# registration re-fuses every 60 s; three missed ticks mean its writer is gone.
SUMMARY_MAX_AGE_S = 180.0
EPOCH_TOL_S = 0.5


class CounterEpochIdHolder:
    """Chooses ``counter_epoch_id`` once per counter-map epoch and holds it.

    The registration's id counts only while its summary is fresh and names
    the same epoch within 0.5 s.  Otherwise the id is ``input-<epoch>``.  The
    choice never changes within a run, so a summary that comes and goes
    cannot flip the id; a new epoch chooses again.
    """

    def __init__(self):
        self._held = None          # (epoch_gps_s, id)

    def get(self, t6_map, summary: Optional[dict], summary_age_s: Optional[float]) -> str:
        epoch = float(t6_map.input_epoch_gps_s)
        if self._held is not None and abs(self._held[0] - epoch) <= EPOCH_TOL_S:
            return self._held[1]
        chosen = f"input-{epoch:.3f}"
        try:
            reg_id = (summary or {}).get("counter_epoch_id")
            reg_epoch = ((summary or {}).get("counter_map") or {}).get("input_epoch_gps_s")
            if (reg_id and isinstance(reg_epoch, (int, float))
                    and summary_age_s is not None
                    and 0 <= summary_age_s <= SUMMARY_MAX_AGE_S
                    and abs(reg_epoch - epoch) <= EPOCH_TOL_S):
                chosen = str(reg_id)
        except Exception:  # noqa: BLE001
            pass
        self._held = (epoch, chosen)
        return chosen


class T6AnchorPublisher:
    """One write per 60 s at most.  A failure returns False.  The log speaks
    on a change of state only: one WARNING when writes start failing, one INFO
    when they recover; one WARNING when a corrupt sigma first blanks the index."""

    def __init__(self, path: Path = ANCHOR_PATH,
                 mono_fn: Callable[[], float] = time.monotonic,
                 interval_s: float = PUBLISH_INTERVAL_S):
        self.path = Path(path)
        self._mono = mono_fn
        self._interval = interval_s
        self._last: Optional[float] = None
        self._failing = False
        self._sigma_warned = False
        self.write_failures = 0

    def maybe_publish(self, **kw) -> bool:
        now = self._mono()
        if self._last is not None and now - self._last < self._interval:
            return False
        self._last = now
        if kw.get("anchor") is None or kw.get("t6_map") is None:
            return False
        corrupt = bool(kw.get("authoritative")) and sigma_is_corrupt(kw.get("sigma_ns"))
        if corrupt and not self._sigma_warned:
            logger.warning(f"t6-anchor.json: authoritative anchor with corrupt "
                           f"sigma {kw.get('sigma_ns')!r}; index withheld")
        self._sigma_warned = corrupt
        try:
            kw.setdefault("now_utc_iso", _utc_iso_now())
            write_t6_anchor(build_t6_anchor(**kw), self.path)
        except Exception as exc:  # noqa: BLE001 - never stop the recorder
            self.write_failures += 1
            if not self._failing:
                logger.warning(f"t6-anchor.json not written ({self.path}): {exc}")
            self._failing = True
            return False
        if self._failing:
            logger.info(f"t6-anchor.json writes recovered after "
                        f"{self.write_failures} failure(s)")
        self._failing = False
        return True
