"""
BpskPpsProbe — T6 authority probe.

Reads /var/lib/timestd/status/core-recorder-status.json (written by
timestd-core-recorder) and translates the embedded ``l6_pps`` block
into a ProbeResult for the AuthorityManager. T6 outranks T5 in
T_LEVELS_RANKED, so when this probe reports available, the manager
promotes the active level to T6 (subject to upgrade hysteresis) and
publishes the BPSK-calibrated sigma instead of the fusion-only sigma.

The probe is deliberately strict so an injector glitch can't masquerade
as a high-authority source:
  - status file missing/unparseable → unavailable
  - status timestamp stale beyond ``freshness_sec`` → unavailable
  - ``l6_pps.enabled == false`` → unavailable
  - ``l6_pps.locked == false`` → unavailable
  - ``pps_consecutive < min_consecutive`` → unavailable (rides over
    a single bursty noise edge but drops T6 during sustained noise)

offset_ms is forwarded from core-recorder's ``local_minus_source_ns``
field (the residual Δ that the TSL3 SHM math computes at every push,
i.e. the value chrony observes as the TSL3 source offset).  This is
the Pattern B publication channel — see
``docs/TIMING-PIPELINE-WIRING.md`` §4.1 + §9 step 1.

Sign convention is ``local_clock − source_UTC`` (positive when the
local clock reads after the source's view of UTC), consistent with
ChronyTrackingProbe.  When the system is well-disciplined Δ is
sub-µs; when the anchor is stale (V1) Δ inflates to whatever
accumulated error the anchor inherited.

The published sigma_ms (default 0.050 ms / 50 µs) captures the
calibration uncertainty (quantization + matched-filter jitter); it
matches the reserved {T6,T5} cross-check threshold and is consistent
with the half-quantization-step bias at 16 kHz sample rate.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from hf_timestd.core.authority_manager import ProbeResult

log = logging.getLogger(__name__)


class BpskPpsProbe:
    t_level = "T6"

    def __init__(
        self,
        status_path: Path = Path("/var/lib/timestd/status/core-recorder-status.json"),
        freshness_sec: float = 60.0,
        min_consecutive: int = 30,
        sigma_ms: float = 0.050,
        now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.status_path = Path(status_path)
        self.freshness_sec = float(freshness_sec)
        self.min_consecutive = int(min_consecutive)
        self.sigma_ms = float(sigma_ms)
        self.now_fn = now_fn

    def poll(self) -> ProbeResult:
        try:
            with self.status_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return ProbeResult(
                self.t_level, available=False,
                reason="core-recorder-status.json missing",
            )
        except (OSError, json.JSONDecodeError) as e:
            return ProbeResult(
                self.t_level, available=False,
                reason=f"read error: {e}",
            )

        ts_str = data.get("timestamp")
        if not isinstance(ts_str, str):
            return ProbeResult(
                self.t_level, available=False,
                reason="status timestamp missing",
            )
        try:
            ts = _parse_iso(ts_str)
        except ValueError as e:
            return ProbeResult(
                self.t_level, available=False,
                reason=f"timestamp parse: {e}",
            )

        age_sec = (self.now_fn() - ts).total_seconds()
        if age_sec > self.freshness_sec:
            return ProbeResult(
                self.t_level, available=False,
                reason=f"stale {age_sec:.0f}s > {self.freshness_sec:.0f}s",
            )

        l6 = data.get("l6_pps")
        if not isinstance(l6, dict):
            return ProbeResult(
                self.t_level, available=False,
                reason="l6_pps block missing",
            )

        if not l6.get("enabled"):
            return ProbeResult(
                self.t_level, available=False,
                reason="l6_pps disabled",
            )
        if not l6.get("locked"):
            return ProbeResult(
                self.t_level, available=False,
                reason="not locked",
            )

        consec = int(l6.get("pps_consecutive", 0))
        if consec < self.min_consecutive:
            return ProbeResult(
                self.t_level, available=False,
                reason=f"pps_consecutive={consec} < {self.min_consecutive}",
            )

        # Pattern B: forward the SHM residual Δ as offset_ms.
        # See docstring + docs/TIMING-PIPELINE-WIRING.md §4.1 / §9.
        residual_ns_raw = l6.get("local_minus_source_ns")
        if residual_ns_raw is None:
            # The producer is the same hf-timestd version we are; this
            # field should always be present once a TSL3 SHM push has
            # happened.  Missing → cold start, no push yet, or schema
            # skew.  Either way the cascade can't use a missing offset.
            return ProbeResult(
                self.t_level, available=False,
                reason="local_minus_source_ns missing — no TSL3 SHM push yet",
            )
        try:
            residual_ns = int(residual_ns_raw)
        except (TypeError, ValueError):
            return ProbeResult(
                self.t_level, available=False,
                reason=f"local_minus_source_ns unparseable: {residual_ns_raw!r}",
            )

        detail = {
            "pps_ok": int(l6.get("pps_ok", 0)),
            "pps_noise": int(l6.get("pps_noise", 0)),
            "pps_consecutive": consec,
            "chain_delay_ns": l6.get("chain_delay_ns"),
            "local_minus_source_ns": residual_ns,
            "age_sec": round(age_sec, 3),
        }
        # V1 fix layer 2 — forward drift-monitor flags into the
        # ProbeResult detail so they appear in authority.json and any
        # downstream consumer (Layer 3 re-capture trigger, sigmond
        # health dashboard) can observe T6 degradation without parsing
        # the upstream status file directly.  Block is None on
        # pre-Layer-2 producers — treated as "no signal yet", not a
        # failure.
        drift_monitor = l6.get("drift_monitor")
        if isinstance(drift_monitor, dict):
            detail["drift_monitor"] = drift_monitor

        return ProbeResult(
            self.t_level,
            available=True,
            offset_ms=residual_ns / 1_000_000.0,
            sigma_ms=self.sigma_ms,
            detail=detail,
        )


def _parse_iso(s: str) -> datetime:
    """Parse an ISO-8601 timestamp; ensure tz-aware UTC."""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
