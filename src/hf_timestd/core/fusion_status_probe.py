"""
FusionStatusProbe — T3 authority probe.

⛔ Since 2026-10-22 it reports T3 UNAVAILABLE on every poll (class
docstring; review-fusion-kalman-20261022.md section 15, step 5).  The
gating below still runs and is reported in ``detail["fusion_status"]``.

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
from typing import Callable, Optional, Tuple

from hf_timestd.core.authority_manager import (
    ProbeResult,
    rtp_to_utc_offset_ms_from_d_clock,
)

log = logging.getLogger(__name__)



T3_CALIBRATED_REASON = (
    "fused D_clock is calibrated (zeroed against the host); not evidence "
    "about the clock (ruling 2026-10-22)"
)


class FusionStatusProbe:
    """T3 probe over fusion_status.json.  Reports T3 UNAVAILABLE, always.

    Michael ruled on 2026-10-22 (review-fusion-kalman-20261022.md section
    15) that the fused D_clock may not stand as evidence about the clock:
    the calibration pulls each broadcast's mean to zero, so the value
    reads near zero whatever the host clock does.  T3 goes dark, loudly,
    until a replacement proves itself (ruling 2; ruling 4 names the tick
    registration as the candidate).  So ``poll`` returns
    ``available=False`` with ``T3_CALIBRATED_REASON``, and never
    ``witness_only``: the authority manager then falls through to the next
    tier exactly as it does when no T3 probe exists.

    The probe still reads and validates the file.  ``detail
    ["fusion_status"]`` says what it found ("ok", or the refusal that used
    to be the reason), and ``offset_ms``/``sigma_ms`` carry the offset the
    file implies, in the ruled convention (rtp_to_utc = -d_clock, via
    ``rtp_to_utc_offset_ms_from_d_clock``).  The reason stays constant so
    the manager's demotion line goes out once, not on every change of
    fusion's state.
    """

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
        status, offset_ms, sigma_ms, detail = self._read()
        detail = dict(detail)
        detail["fusion_status"] = status
        return ProbeResult(
            self.t_level,
            available=False,
            reason=T3_CALIBRATED_REASON,
            offset_ms=offset_ms,
            sigma_ms=sigma_ms,
            detail=detail,
            frame="rtp",  # HF tick arrival vs expected, measured in RTP domain
        )

    def _read(self) -> Tuple[str, Optional[float], Optional[float], dict]:
        """(status, offset_ms, sigma_ms, detail) from the status file.

        ``status`` is "ok" for a file that once made T3 available, else
        the refusal that used to be the probe's reason.
        """
        try:
            with self.status_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return "fusion_status.json missing", None, None, {}
        except (OSError, json.JSONDecodeError) as e:
            return f"read error: {e}", None, None, {}

        if data.get("schema") != "v1":
            return f"unsupported schema: {data.get('schema')}", None, None, {}

        pub_str = data.get("utc_published")
        if not isinstance(pub_str, str):
            return "utc_published missing", None, None, {}
        try:
            pub = _parse_iso_z(pub_str)
        except ValueError as e:
            return f"utc_published parse: {e}", None, None, {}

        age_sec = (self.now_fn() - pub).total_seconds()
        if age_sec > self.freshness_sec:
            return (f"stale {age_sec:.0f}s > {self.freshness_sec:.0f}s",
                    None, None, {})

        fusion = data.get("fusion") or {}
        if not fusion.get("available"):
            return (str(fusion.get("reason", "fusion unavailable")),
                    None, None, {})

        kalman_state = str(fusion.get("kalman_state", "UNKNOWN"))
        try:
            d_clock_ms = float(fusion["d_clock_fused_ms"])
            sigma_ms = float(fusion["uncertainty_ms"])
        except (KeyError, TypeError, ValueError) as e:
            return f"missing/invalid offset fields: {e}", None, None, {}
        detail = {
            "stations_used": list(fusion.get("stations_used") or []),
            "quality_grade": fusion.get("quality_grade"),
            "kalman_state": kalman_state,
            "age_sec": round(age_sec, 3),
            # host minus UTC, and calibrated: shown, never published.
            "d_clock_fused_ms": d_clock_ms,
        }
        offset_ms = rtp_to_utc_offset_ms_from_d_clock(d_clock_ms)

        n_stations = int(fusion.get("n_stations", 0))
        if n_stations < self.min_stations:
            return (f"n_stations={n_stations} < {self.min_stations}",
                    offset_ms, sigma_ms, detail)
        if kalman_state not in ("ACQUIRING", "LOCKED"):
            return (f"kalman_state={kalman_state}",
                    offset_ms, sigma_ms, detail)
        return "ok", offset_ms, sigma_ms, detail


def _parse_iso_z(s: str) -> datetime:
    """Parse 'YYYY-MM-DDTHH:MM:SS.ffffffZ' into an aware UTC datetime."""
    if s.endswith("Z"):
        s = s[:-1]
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
