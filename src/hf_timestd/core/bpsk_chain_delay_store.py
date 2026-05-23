"""Persist BPSK chain_delay across core-recorder restarts.

The BPSK PPS calibrators (both the legacy half-second matched filter and
the per-sample differential detector) report ``chain_delay_ns`` modulo
the half-second wrap of their boxcar template (MF) or modulo one second
of their integer-sample interpolation (diff).  At first lock a
disambiguation step resolves which integer-sample multiple of the wrap
is correct, by comparing the implied wall-time of the detected edge
against an external timing authority.

**The §4.5 RTP-reference invariant is preserved by THIS module, not
by the choice of disambiguation reference.**  The reference at first
lock comes from whichever non-T6 tier passes the sample-period-
aligned sigma gate in ``_get_disambiguation_reference`` — in practice
T4 chronyc tracking (T3 fusion's ms-scale σ is too wide for the
integer-sample shift to be reliable).  Once that one-shot pick is
made, this file records the resulting *effective chain_delay* — a
physical RF-path constant — and every subsequent cycle re-derives
its own disambig from that value with no host-wall-clock dependence.
The invariant lives in the persisted value, not in the bootstrap.

The problem: chrony's *Last offset* shifts continuously as it
disciplines the local clock.  Every restart re-runs this comparison
*against a different chrony state*, so each restart picks a different
``_t6_disambiguation_ns`` — observed live on bee1 2026-05-21: three
restarts within five minutes produced calibrations drifting 635 µs
across the sequence.  chrony then marks the post-restart TSL3 as a
false-ticker `x` until it adopts the new offset.

The physical chain delay (RX-888 + cables + filter + transmitter) is
*invariant* across restarts.  Persisting the last-known-good *effective*
chain_delay (= raw + disambiguation) lets the next process re-derive its
own ``_t6_disambiguation_ns`` purely from that invariant, with no
chrony dependence.

Storage format (one file per detector source — MF and diff can have
sub-sample biases between them that we keep separate)::

    /var/lib/timestd/bpsk_<source>_chain_delay.json
    {
      "schema": "v1",
      "saved_at_unix": 1779475864.0,
      "sample_rate": 96000,
      "effective_chain_delay_ns": 559121877,
      "source": "MF" | "diff"
    }

Freshness gate: 1 h.  Beyond that the underlying RF path may have been
re-cabled or radiod reconfigured; the persisted value is no longer
trustworthy and we fall back to the T4 reference walk.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# Persisted values older than this are ignored — re-disambiguation via
# the timing-tier hierarchy runs instead.  1 h is generous enough to
# survive routine restarts (OOM cycles, deploys) but short enough that
# RF-path changes (cable swap, radiod sample-rate change) don't get
# silently re-applied as if they were the same physical path.
DEFAULT_STALENESS_S = 3600.0

# Default directory.  /var/lib/timestd is the standard hf-timestd
# state root; tests override with tmp_path.
DEFAULT_STORE_DIR = Path("/var/lib/timestd")


@dataclass(frozen=True)
class PersistedChainDelay:
    saved_at_unix: float
    sample_rate: int
    effective_chain_delay_ns: int
    source: str


class ChainDelayStore:
    """Read/write persisted effective chain_delay for one detector source.

    Construct one per source string ("MF" or "diff"); the source becomes
    the filename suffix so MF and diff stores never collide.
    """

    def __init__(
        self,
        source: str,
        *,
        store_dir: Path = DEFAULT_STORE_DIR,
        staleness_s: float = DEFAULT_STALENESS_S,
    ) -> None:
        if source not in ("MF", "diff"):
            raise ValueError(f"source must be 'MF' or 'diff', got {source!r}")
        self.source = source
        self.staleness_s = staleness_s
        self.path = store_dir / f"bpsk_{source.lower()}_chain_delay.json"

    def load(self, *, now_unix: Optional[float] = None) -> Optional[PersistedChainDelay]:
        """Read the persisted state.  Returns ``None`` if absent, stale,
        malformed, or for a different source.  Never raises.
        """
        if now_unix is None:
            now_unix = time.time()
        try:
            raw = self.path.read_text()
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.warning(
                f"ChainDelayStore[{self.source}]: read failed ({exc}); "
                f"treating as absent"
            )
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning(
                f"ChainDelayStore[{self.source}]: malformed JSON ({exc}); "
                f"treating as absent"
            )
            return None
        try:
            if data.get("schema") != "v1":
                logger.warning(
                    f"ChainDelayStore[{self.source}]: unknown schema "
                    f"{data.get('schema')!r}; treating as absent"
                )
                return None
            if data.get("source") != self.source:
                logger.warning(
                    f"ChainDelayStore[{self.source}]: source mismatch "
                    f"(file says {data.get('source')!r}); treating as absent"
                )
                return None
            entry = PersistedChainDelay(
                saved_at_unix=float(data["saved_at_unix"]),
                sample_rate=int(data["sample_rate"]),
                effective_chain_delay_ns=int(data["effective_chain_delay_ns"]),
                source=str(data["source"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning(
                f"ChainDelayStore[{self.source}]: missing/invalid field "
                f"({exc}); treating as absent"
            )
            return None
        age_s = now_unix - entry.saved_at_unix
        if age_s > self.staleness_s:
            logger.info(
                f"ChainDelayStore[{self.source}]: persisted value is "
                f"{age_s:.0f}s old (> {self.staleness_s:.0f}s) — treating "
                f"as stale, falling back to T4 disambiguation"
            )
            return None
        if age_s < -60.0:
            # Future-dated by more than a clock-skew tolerance; the
            # local clock has moved backward since the save, so we
            # can't reason about freshness — be conservative.
            logger.warning(
                f"ChainDelayStore[{self.source}]: persisted value is "
                f"future-dated by {-age_s:.0f}s; treating as absent"
            )
            return None
        return entry

    def save(
        self,
        *,
        sample_rate: int,
        effective_chain_delay_ns: int,
        now_unix: Optional[float] = None,
    ) -> None:
        """Atomically write the new state.  Never raises on I/O — a
        persistence failure must not take the calibrator down.
        """
        if now_unix is None:
            now_unix = time.time()
        payload = {
            "schema": "v1",
            "saved_at_unix": float(now_unix),
            "sample_rate": int(sample_rate),
            "effective_chain_delay_ns": int(effective_chain_delay_ns),
            "source": self.source,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload))
            os.replace(tmp, self.path)
        except OSError as exc:
            logger.warning(
                f"ChainDelayStore[{self.source}]: save failed ({exc}); "
                f"continuing without persistence"
            )


def compute_disambiguation_ns(
    *,
    raw_chain_delay_ns: int,
    persisted_effective_chain_delay_ns: int,
    sample_rate: int,
) -> int:
    """Compute the disambiguation offset that aligns ``raw`` with
    ``persisted_effective`` to the nearest integer sample.

    The raw value from a freshly-restarted calibrator can sit anywhere
    inside the wrap (MF: half-second = sample_rate/2 samples; diff: one
    second = sample_rate samples).  The physical chain delay is
    invariant, so the correct disambiguation is the integer-sample
    shift that brings raw closest to the persisted effective.

    Returns ``disambiguation_ns`` such that
    ``raw + disambiguation_ns ≈ persisted_effective`` to within half a
    sample period.
    """
    sample_period_ns = 1e9 / sample_rate
    delta_ns = persisted_effective_chain_delay_ns - raw_chain_delay_ns
    shift_samples = round(delta_ns / sample_period_ns)
    return int(round(shift_samples * sample_period_ns))
