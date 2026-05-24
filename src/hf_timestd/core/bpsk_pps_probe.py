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

sigma_ms is the honest annotation uncertainty for the T6 label —
the larger of three components:

  1. **Matched-filter jitter** — the producer's 60-sample rolling
     std-dev of ``chain_delay_ns`` (the BPSK matched filter's
     per-PPS edge-position estimate), published as
     ``l6_pps.chain_delay_ns_std_ns``.  This is the physical noise
     of the BPSK PPS measurement itself.

  2. **Substrate residual** — ``|local_minus_source_ns|`` in ms.
     This is the per-cycle bias of our model: how far our
     RTP-projected UTC label sits from the BPSK source's integer
     PPS second.  In nominal operation it is sub-µs; in the V1
     anchor-staleness regime it can inflate to hundreds of ms.
     A σ that ignores this bias publishes optimism the substrate
     contradicts.

  3. **Calibration floor** — ``sigma_floor_ms``, the irreducible
     calibration uncertainty (antenna cable thermal drift, BPSK
     detector bias, half-quantization-step) that the observed
     jitter cannot directly see.

The published sigma is ``max(jitter, residual, floor)``.  This
bounds the total label error (bias + noise) honestly: when the
anchor is fresh and the substrate cross-check is small the
floor or jitter dominates and σ is sub-µs; when the V1
anchor-staleness regime fires σ inflates to match the actual
residual so downstream consumers see the truth without having
to parse the breach flags.

Substrate evaluation 2026-05-24 found median |residual| ≈ 5 µs
when T6 is active, with p99 ≈ 294 ms — see
``docs/T6-ANNOTATION-VALUE-2026-05-24.md``.  The previous σ
publication (jitter only, clamped to floor) misrepresented the
p99 tail by ~5 orders of magnitude.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from hf_timestd.core.authority_manager import ProbeResult

log = logging.getLogger(__name__)


class BpskPpsProbe:
    t_level = "T6"

    def __init__(
        self,
        status_path: Path = Path("/var/lib/timestd/status/core-recorder-status.json"),
        freshness_sec: float = 60.0,
        min_consecutive: int = 30,
        sigma_floor_ms: float = 0.001,
        now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        """
        Args:
            sigma_floor_ms: Minimum sigma we'll publish.  Observed
                std-dev can go arbitrarily small in calm windows, but
                we always have systematic calibration uncertainty
                (antenna cable thermal drift, BPSK detector bias, half-
                quantization-step) that the observed jitter doesn't
                see.  1 µs (0.001 ms) is conservative for the
                LB-1421 + TS1 + RX-888 chain at 16 kHz sample rate
                (half-quantization is 31 µs but our matched-filter
                resolves well below that floor in clean conditions).
                If observed std exceeds this, we publish observed.
        """
        self.status_path = Path(status_path)
        self.freshness_sec = float(freshness_sec)
        self.min_consecutive = int(min_consecutive)
        self.sigma_floor_ms = float(sigma_floor_ms)
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

        # Sigma from observed jitter — producer publishes a rolling
        # std-dev of chain_delay_ns (≈60 samples / 1 min at 1 Hz);
        # we clamp from below by the floor.  Pre-jitter producers omit
        # the field — fall back to the floor (matches prior hardcoded
        # behavior so authority cross-checks stay stable on mixed
        # versions).
        std_ns_raw = l6.get("chain_delay_ns_std_ns")
        std_window_raw = l6.get("chain_delay_ns_window")
        std_ns: Optional[float]
        std_window: Optional[int]
        try:
            std_ns = float(std_ns_raw) if std_ns_raw is not None else None
        except (TypeError, ValueError):
            std_ns = None
        try:
            std_window = int(std_window_raw) if std_window_raw is not None else None
        except (TypeError, ValueError):
            std_window = None
        # Honest σ: max of measurement noise (matched-filter jitter),
        # model bias (substrate residual), and calibration floor.
        # See class docstring for rationale.
        jitter_ms = (std_ns / 1_000_000.0) if std_ns is not None else 0.0
        residual_ms = abs(residual_ns) / 1_000_000.0
        sigma_ms = max(jitter_ms, residual_ms, self.sigma_floor_ms)

        # Diagnostic: local_minus_source_ns std (post-anchor computation
        # stability, NOT the physical σ).  Forwarded as-is for the
        # debugging view; not used for the published sigma_ms.
        lms_std_raw = l6.get("local_minus_source_ns_std_ns")
        try:
            lms_std_ns = float(lms_std_raw) if lms_std_raw is not None else None
        except (TypeError, ValueError):
            lms_std_ns = None

        detail = {
            "pps_ok": int(l6.get("pps_ok", 0)),
            "pps_noise": int(l6.get("pps_noise", 0)),
            "pps_consecutive": consec,
            "chain_delay_ns": l6.get("chain_delay_ns"),
            "chain_delay_ns_std_ns": std_ns,
            "chain_delay_ns_window": std_window,
            "local_minus_source_ns": residual_ns,
            "local_minus_source_ns_std_ns": lms_std_ns,
            "sigma_floor_ms": self.sigma_floor_ms,
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
            sigma_ms=sigma_ms,
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
