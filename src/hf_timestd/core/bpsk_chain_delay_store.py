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
      "schema": "v2",
      "saved_at_unix": 1779475864.0,
      "sample_rate": 96000,
      "effective_chain_delay_ns": 559121877,
      "source": "MF" | "diff",

      // schema v2 additions — the hf-timestd-native (RTP, UTC) anchor
      // captured at first lock.  Lets subsequent restarts skip
      // re-disambiguation entirely AND lets the SHM push / authority
      // publication consult a pure substrate value instead of
      // ka9q.rtp_to_wallclock(), which rides radiod's host-clock-
      // derived (gps_time, rtp_timesnap) anchor.  See
      // ``hf_timestd.core.native_anchor`` and
      // ``docs/ARCHITECTURE-FIRST-PRINCIPLES.md`` §1.
      "anchor_rtp": 2107252660,           // 32-bit RTP of the MF-detected PPS edge
      "anchor_utc_ns": 1780000000000000000,
      "chain_delay_ns": 559121877,        // = effective_chain_delay_ns; kept here for the anchor's pure-function use
      "captured_at_utc_ns": 1779999999000000000,
      "captured_via_tier": "T5"           // or "T4" / "T3"
    }

Backward compatibility: a v1 file (no anchor fields) is loaded with
``anchor=None``; the caller falls through to the disambig path and
captures a fresh anchor at first lock, then re-saves as v2.  A v2
file read by a v1-only client trips the existing
``data.get("schema") != "v1"`` reject path and is treated as absent
— which is safe (causes a re-disambig, never a wrong-value
acceptance).

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

from hf_timestd.core.native_anchor import NativeAnchor

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
    # Schema v2 additions — None when loaded from a v1 file.
    anchor: "Optional[NativeAnchor]" = None
    schema: str = "v1"


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
            schema = data.get("schema")
            if schema not in ("v1", "v2"):
                logger.warning(
                    f"ChainDelayStore[{self.source}]: unknown schema "
                    f"{schema!r}; treating as absent"
                )
                return None
            if data.get("source") != self.source:
                logger.warning(
                    f"ChainDelayStore[{self.source}]: source mismatch "
                    f"(file says {data.get('source')!r}); treating as absent"
                )
                return None
            anchor: Optional[NativeAnchor] = None
            if schema == "v2":
                try:
                    anchor = NativeAnchor.from_json({
                        "anchor_rtp": data["anchor_rtp"],
                        "anchor_utc_ns": data["anchor_utc_ns"],
                        "sample_rate_hz": data["sample_rate"],
                        "chain_delay_ns": data["chain_delay_ns"],
                        "captured_at_utc_ns": data["captured_at_utc_ns"],
                        "captured_via_tier": data["captured_via_tier"],
                    })
                except (KeyError, TypeError, ValueError) as exc:
                    logger.warning(
                        f"ChainDelayStore[{self.source}]: v2 anchor "
                        f"fields malformed ({exc}); falling back to v1 "
                        f"chain_delay-only interpretation"
                    )
                    anchor = None
            entry = PersistedChainDelay(
                saved_at_unix=float(data["saved_at_unix"]),
                sample_rate=int(data["sample_rate"]),
                effective_chain_delay_ns=int(data["effective_chain_delay_ns"]),
                source=str(data["source"]),
                anchor=anchor,
                schema=schema,
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
        anchor: Optional[NativeAnchor] = None,
        now_unix: Optional[float] = None,
    ) -> None:
        """Atomically write the new state.  Never raises on I/O — a
        persistence failure must not take the calibrator down.

        When ``anchor`` is supplied, writes schema v2 with the
        hf-timestd-native (RTP, UTC) anchor embedded.  When ``anchor``
        is ``None``, writes schema v1 for backward compatibility with
        any older readers (none in-tree once this commit lands, but
        the field is kept optional during the transition).
        """
        if now_unix is None:
            now_unix = time.time()
        if anchor is not None:
            payload = {
                "schema": "v2",
                "saved_at_unix": float(now_unix),
                "sample_rate": int(sample_rate),
                "effective_chain_delay_ns": int(effective_chain_delay_ns),
                "source": self.source,
                **anchor.to_json(),
            }
        else:
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
