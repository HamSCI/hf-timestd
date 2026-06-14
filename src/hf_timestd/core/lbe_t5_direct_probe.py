"""
LbeT5DirectProbe — T5 authority probe sourced directly from LBE-1421.

Counterpart to BpskPpsProbe (T6).  Where BpskPpsProbe reads the
core-recorder's BPSK matched-filter state for T6 (TS-1 HF-injected
PPS, ns-class), this probe reads the LBE-1421 USB-NMEA reader's
state for T5 (GPS+PPS direct, µs-to-ms class via USB scheduling
jitter).

Architecture note: hf-timestd already runs an Lb1421T5Probe inside
timestd-core-recorder (for BPSK PPS disambig — see
project_hf_pps_t5_direct_2026-05-23).  That probe owns the device.
This LbeT5DirectProbe sits on the AuthorityRunner side and reads
the T5 status block core-recorder writes to its status file — no
second handle on /dev/lb1421-nmea.

When the t5_lbe1421 block is absent (the lb1421 reader isn't
attached, or core-recorder isn't running, or status file is
missing), the probe is unavailable — exactly the right
degradation, because losing core-recorder breaks the substrate
anyway.

Phase 2B semantics (active-tier capable):

  offset_ms: anchor-disagreement (signed) when core_recorder
             populates ``t5_lbe1421.anchor_offset_ns`` — the RTP
             anchor's UTC prediction minus NMEA's pps_utc_sec at the
             NMEA-read instant.  Falls back to 0.0 (Phase 2A trust-
             tier) when the field is absent — happens when the
             anchor is too stale to extrapolate, or core_recorder is
             at a pre-Phase-2B version.
  sigma_ms:  honest σ — max(floor, |offset|).  When the anchor is
             calm and the substrate residual is sub-floor, this
             collapses to ``sigma_floor_ms`` (default 5 ms — USB-
             NMEA scheduling jitter floor).  When the anchor has
             drifted (V1 anchor-staleness regime), σ widens to
             bound the observed disagreement honestly so downstream
             cross-checks aren't misled by an under-claim.  Mirrors
             ``9755e53`` BpskPpsProbe honest-σ pattern.

The ``available`` flag still tracks whether T5 fallback is even
possible — the t5_* columns in authority_snapshot remain the
operator observability surface.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from hf_timestd.core.authority_manager import ProbeResult

log = logging.getLogger(__name__)


class LbeT5DirectProbe:
    t_level = "T5"

    def __init__(
        self,
        status_path: Path = Path("/var/lib/timestd/status/core-recorder-status.json"),
        freshness_sec: float = 60.0,
        max_nmea_age_sec: float = 2.0,
        sigma_floor_ms: float = 5.0,
        now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        """
        Args:
            status_path: core-recorder-status.json path.  Same file
                BpskPpsProbe polls — co-located by design.
            freshness_sec: max age of the status file itself before T5
                is unavailable (core-recorder may have stalled).
            max_nmea_age_sec: max age of the LBE-1421 NMEA reading
                inside the file.  Default 2 s = one NMEA emission
                cycle + USB scheduling margin.  Beyond this the
                reading is stale (NMEA sentences arrive at 1 Hz; a
                missed cycle is operationally interesting).
            sigma_floor_ms: published sigma_ms — USB-NMEA scheduling
                jitter floor.  Default 5 ms is conservative for
                LBE-1421 over USB-CDC; operators can tighten with
                empirical measurement.  Phase 2A doesn't make this
                load-bearing — the value matters when Phase 2B wires
                T5 into active-tier selection.
        """
        self.status_path = Path(status_path)
        self.freshness_sec = float(freshness_sec)
        self.max_nmea_age_sec = float(max_nmea_age_sec)
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

        t5 = data.get("t5_lbe1421")
        if not isinstance(t5, dict):
            return ProbeResult(
                self.t_level, available=False,
                reason="t5_lbe1421 block missing (lb1421 probe not attached)",
            )

        if not t5.get("enabled"):
            return ProbeResult(
                self.t_level, available=False,
                reason="t5_lbe1421 disabled",
            )

        if not t5.get("valid_fix"):
            reason = t5.get("reason") or "no GPS fix"
            return ProbeResult(
                self.t_level, available=False,
                reason=f"no valid fix: {reason}",
            )

        nmea_age_raw = t5.get("age_sec")
        try:
            nmea_age = float(nmea_age_raw) if nmea_age_raw is not None else None
        except (TypeError, ValueError):
            nmea_age = None
        if nmea_age is None:
            return ProbeResult(
                self.t_level, available=False,
                reason="NMEA reading age missing",
            )
        if nmea_age > self.max_nmea_age_sec:
            return ProbeResult(
                self.t_level, available=False,
                reason=f"NMEA stale {nmea_age:.1f}s > {self.max_nmea_age_sec:.1f}s",
            )

        # T5 is available.  Phase 2B: forward the substrate-grounded
        # anchor-disagreement offset when core_recorder published it.
        # Otherwise fall back to Phase 2A trust-tier semantics (offset
        # 0, σ at the configured floor).
        anchor_offset_raw = t5.get("anchor_offset_ns")
        anchor_offset_ns: Optional[int]
        try:
            anchor_offset_ns = (
                int(anchor_offset_raw) if anchor_offset_raw is not None else None
            )
        except (TypeError, ValueError):
            anchor_offset_ns = None
        if anchor_offset_ns is not None:
            offset_ms = anchor_offset_ns / 1_000_000.0
            sigma_ms = max(self.sigma_floor_ms, abs(offset_ms))
        else:
            offset_ms = 0.0
            sigma_ms = self.sigma_floor_ms

        detail = {
            "pps_utc_sec": t5.get("pps_utc_sec"),
            "valid_fix": True,
            "nmea_age_sec": round(nmea_age, 3),
            "device": t5.get("device"),
            "status_age_sec": round(age_sec, 3),
            "sigma_floor_ms": self.sigma_floor_ms,
            "anchor_offset_ns": anchor_offset_ns,
            "anchor_age_sec": t5.get("anchor_age_sec"),
            # Phase 2B marker — tells AuthorityManager._build_state
            # that this probe's offset_ms is an RTP-substrate-grounded
            # measurement (NMEA-vs-anchor disagreement) suitable for
            # publishing as rtp_to_utc_offset_ns.  Set only when the
            # substrate actually supplied an anchor_offset_ns;
            # otherwise we're at Phase 2A trust-tier defaults and
            # the manager should publish offset=0 with TRUST_SIGMA_MS.
            "rtp_anchor_grounded": anchor_offset_ns is not None,
        }
        return ProbeResult(
            self.t_level,
            available=True,
            offset_ms=offset_ms,
            sigma_ms=sigma_ms,
            detail=detail,
            frame="rtp",  # anchor-vs-GPS-NMEA-truth (system-clock-independent)
        )


def _parse_iso(s: str) -> datetime:
    """Parse the ISO8601-with-Z timestamps core_recorder writes.

    Mirrors BpskPpsProbe._parse_iso semantics: accepts both trailing-Z
    and explicit +00:00 forms, always returns a tz-aware UTC datetime.
    """
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
