"""
FusionStatusProbe — T3 authority probe.

Reads /run/hf-timestd/fusion_status.json (written by timestd-fusion via
FusionStatusWriter) and translates it into a ProbeResult for the
AuthorityManager. Structurally this is the authority manager's primary
input for the multi-station HF Fusion level.

The probe is deliberately strict:
  - Unknown schema versions → unavailable.
  - Stale publication timestamp → unavailable (coupling rule in §4.5:
    if the fusion service hangs, the status file ages out and T3 drops).
  - fusion.available == false → unavailable.
  - n_stations < min_stations → unavailable (single-station Fusion has
    no cross-validation leg).
  - kalman_state not in {"ACQUIRING","LOCKED"} → unavailable.

ACQUIRING (not-yet-converged) is accepted as available — verified safe
(2026-06-14) because the forwarded ``uncertainty_ms`` is the producer's
honest statistical+systematic+propagation budget
(``multi_broadcast_fusion`` ~L4374), NOT a fixed value, and the
statistical leg is naturally larger during early acquisition (fewer
measurements). It is published as the tier sigma, so consumers see the
honest (wider) uncertainty rather than a false-precise lock. The
AuthorityManager's 3-tick upgrade hysteresis is the second guard: T3
must pass its probe for ~3 consecutive minutes before becoming active,
by which point ACQUIRING (≥10 Kalman updates) has typically converged to
LOCKED. REACQUIRING (< 10 updates, post-restart) is still rejected.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from hf_timestd.core.authority_manager import ProbeResult

log = logging.getLogger(__name__)


class FusionStatusProbe:
    t_level = "T3"

    def __init__(
        self,
        status_path: Path = Path("/run/hf-timestd/fusion_status.json"),
        freshness_sec: float = 60.0,
        min_stations: int = 2,
        now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.status_path = Path(status_path)
        self.freshness_sec = float(freshness_sec)
        self.min_stations = int(min_stations)
        self.now_fn = now_fn

    def poll(self) -> ProbeResult:
        try:
            with self.status_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return ProbeResult(self.t_level, available=False, reason="fusion_status.json missing")
        except (OSError, json.JSONDecodeError) as e:
            return ProbeResult(self.t_level, available=False, reason=f"read error: {e}")

        if data.get("schema") != "v1":
            return ProbeResult(
                self.t_level, available=False,
                reason=f"unsupported schema: {data.get('schema')}",
            )

        pub_str = data.get("utc_published")
        if not isinstance(pub_str, str):
            return ProbeResult(self.t_level, available=False, reason="utc_published missing")
        try:
            pub = _parse_iso_z(pub_str)
        except ValueError as e:
            return ProbeResult(self.t_level, available=False, reason=f"utc_published parse: {e}")

        age_sec = (self.now_fn() - pub).total_seconds()
        if age_sec > self.freshness_sec:
            return ProbeResult(
                self.t_level, available=False,
                reason=f"stale {age_sec:.0f}s > {self.freshness_sec:.0f}s",
            )

        fusion = data.get("fusion") or {}
        if not fusion.get("available"):
            return ProbeResult(
                self.t_level, available=False,
                reason=str(fusion.get("reason", "fusion unavailable")),
            )

        n_stations = int(fusion.get("n_stations", 0))
        if n_stations < self.min_stations:
            return ProbeResult(
                self.t_level, available=False,
                reason=f"n_stations={n_stations} < {self.min_stations}",
            )

        kalman_state = str(fusion.get("kalman_state", "UNKNOWN"))
        if kalman_state not in ("ACQUIRING", "LOCKED"):
            return ProbeResult(
                self.t_level, available=False,
                reason=f"kalman_state={kalman_state}",
            )

        try:
            offset_ms = float(fusion["d_clock_fused_ms"])
            sigma_ms = float(fusion["uncertainty_ms"])
        except (KeyError, TypeError, ValueError) as e:
            return ProbeResult(
                self.t_level, available=False,
                reason=f"missing/invalid offset fields: {e}",
            )

        return ProbeResult(
            self.t_level,
            available=True,
            offset_ms=offset_ms,
            sigma_ms=sigma_ms,
            detail={
                "stations_used": list(fusion.get("stations_used") or []),
                "quality_grade": fusion.get("quality_grade"),
                "kalman_state": kalman_state,
                "age_sec": round(age_sec, 3),
            },
            frame="rtp",  # HF tick arrival vs expected, measured in RTP domain
        )


def _parse_iso_z(s: str) -> datetime:
    """Parse 'YYYY-MM-DDTHH:MM:SS.ffffffZ' into an aware UTC datetime."""
    if s.endswith("Z"):
        s = s[:-1]
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
