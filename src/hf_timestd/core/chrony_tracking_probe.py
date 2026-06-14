"""
ChronyTrackingProbe — polls `chronyc -n -c sources` to decide whether a
specific T-level's configured source is reachable and disciplined.

One instance is created per T-level that consumes a chrony source:
T5 (on-host GPS+PPS refclock), T4 (LAN GPS+PPS NTP peer), T2 (WAN NTP).
The service-side factory wires each instance with a source_matcher
tailored to the operator's configured peer names / refids.

The probe reports chrony's measured offset against the peer as
`offset_ms`, with the level's tier sigma from TRUST_SIGMA_MS. That turns
a nominally trust-based level into a cross-checkable witness against
measured levels (T3/T6) without requiring an independent RTP→UTC
observation.

Availability gating (METROLOGY.md §4.5 "T5/T4/T2 probe"):
  - the matching source must be in a healthy state (`*`/`+`), AND
  - its `reach` must be non-zero (a healthy state with reach 0 is a
    transient/bug — a source cannot be genuinely selected or combined
    on zero successful polls), AND
  - when ``max_error_ms`` is configured for the tier, the last-sample
    error margin must be within it ("RMS within tier limit").  Left
    unset by default: the AuthorityManager cross-check (the σ-widening
    / TIMING_DISAGREEMENT path) already catches a witness whose offset
    has drifted, so an unconditional error ceiling here would only risk
    dropping noisy-but-usable witnesses and reducing cross-check
    coverage.  "Offset stable" over time is likewise assessed by that
    cross-check layer, not by this single-sample probe.

Failure modes (reported via ProbeResult.reason):
  - chronyc missing or non-executable
  - subprocess timeout or non-zero exit
  - no source matches the configured matcher
  - matching source present but state not in healthy_state_chars
  - matching healthy source but reach == 0 (unreachable)
  - matching source error margin exceeds max_error_ms (when configured)
  - matching source offset field unparseable
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from typing import Callable, List, Optional

from hf_timestd.core.authority_manager import (
    TRUST_SIGMA_MS,
    ProbeResult,
)

log = logging.getLogger(__name__)


class ChronyTrackingProbe:
    # `chronyc -n -c sources` column positions (no header row).
    # See chronyc(1); format stable since chrony 2.x.
    _IDX_MODE = 0     # '^' server, '=' peer, '#' refclock
    _IDX_STATE = 1    # '*' synced, '+' combined, '-' backup, '?' unreach, 'x' falseticker, '~' skew
    _IDX_NAME = 2
    _IDX_STRATUM = 3
    _IDX_POLL = 4
    _IDX_REACH = 5
    _IDX_LAST_RX = 6
    _IDX_OFFSET = 7   # last sample offset, seconds
    _IDX_ERROR = 8    # last sample error margin, seconds

    def __init__(
        self,
        t_level: str,
        source_matcher: Callable[[dict], bool],
        healthy_state_chars: str = "*+",
        chronyc_bin: Optional[str] = None,
        timeout_sec: float = 5.0,
        max_error_ms: Optional[float] = None,
        runner: Optional[Callable[..., subprocess.CompletedProcess]] = None,
    ):
        """
        Args:
            t_level: which T-level this probe represents.
            source_matcher: callable taking a parsed source row dict and
                returning True if this source counts.
            healthy_state_chars: state chars accepted as "healthy."
            chronyc_bin: path to the chronyc binary; defaults to
                `shutil.which("chronyc")`.
            timeout_sec: subprocess timeout.
            max_error_ms: optional last-sample error-margin ceiling
                ("RMS within tier limit", METROLOGY.md §4.5).  None (the
                default) disables the check — see the module docstring
                for why this is opt-in rather than tier-derived.
            runner: optional subprocess.run-compatible callable for tests.
        """
        self.t_level = t_level
        self.source_matcher = source_matcher
        self.healthy_state_chars = healthy_state_chars
        self.chronyc_bin = chronyc_bin or shutil.which("chronyc") or "chronyc"
        self.timeout_sec = float(timeout_sec)
        self.max_error_ms = float(max_error_ms) if max_error_ms is not None else None
        self._run = runner or subprocess.run

    def poll(self) -> ProbeResult:
        try:
            proc = self._run(
                [self.chronyc_bin, "-n", "-c", "sources"],
                capture_output=True, text=True,
                timeout=self.timeout_sec, check=False,
            )
        except FileNotFoundError:
            return ProbeResult(self.t_level, available=False, reason="chronyc not found")
        except subprocess.TimeoutExpired:
            return ProbeResult(self.t_level, available=False, reason="chronyc timeout")
        except OSError as e:
            return ProbeResult(self.t_level, available=False, reason=f"chronyc exec error: {e}")

        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip().splitlines()[-1:] or ["(no stderr)"]
            return ProbeResult(
                self.t_level, available=False,
                reason=f"chronyc exit {proc.returncode}: {stderr[0]}",
            )

        rows = self._parse(proc.stdout or "")
        matching = [r for r in rows if self._safe_match(r)]
        if not matching:
            return ProbeResult(self.t_level, available=False, reason="no matching source")

        healthy = [r for r in matching if r.get("state") in self.healthy_state_chars]
        if not healthy:
            states = ",".join(r.get("state", "?") for r in matching)
            return ProbeResult(
                self.t_level, available=False,
                reason=f"matching sources unhealthy: states={states}",
            )

        # §4.5: a healthy state with reach 0 is a transient/bug — drop it.
        reachable = [r for r in healthy if _reach_nonzero(r.get("reach"))]
        if not reachable:
            reaches = ",".join(str(r.get("reach")) for r in healthy)
            return ProbeResult(
                self.t_level, available=False,
                reason=f"matching sources unreachable: reach={reaches}",
            )

        chosen = reachable[0]
        try:
            offset_s = float(chosen["offset_s"])
        except (KeyError, TypeError, ValueError) as e:
            return ProbeResult(
                self.t_level, available=False,
                reason=f"offset parse error: {e}",
            )

        # §4.5: optional last-sample error-margin ceiling.
        error_ms: Optional[float]
        try:
            err_raw = chosen.get("error_s")
            error_ms = abs(float(err_raw)) * 1000.0 if err_raw is not None else None
        except (TypeError, ValueError):
            error_ms = None
        if (
            self.max_error_ms is not None
            and error_ms is not None
            and error_ms > self.max_error_ms
        ):
            return ProbeResult(
                self.t_level, available=False,
                reason=(
                    f"error margin {error_ms:.3f}ms > "
                    f"{self.max_error_ms:.3f}ms"
                ),
            )

        return ProbeResult(
            t_level=self.t_level,
            available=True,
            offset_ms=offset_s * 1000.0,
            sigma_ms=TRUST_SIGMA_MS.get(self.t_level, 1.0),
            detail={
                "name": chosen.get("name"),
                "stratum": chosen.get("stratum"),
                "state": chosen.get("state"),
                "reach": chosen.get("reach"),
                "error_ms": error_ms,
            },
        )

    def _safe_match(self, row: dict) -> bool:
        try:
            return bool(self.source_matcher(row))
        except Exception as e:
            log.debug("source_matcher raised on row %r: %s", row, e)
            return False

    def _parse(self, csv_output: str) -> List[dict]:
        rows: List[dict] = []
        for line in csv_output.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) <= self._IDX_ERROR:
                continue
            rows.append({
                "mode": parts[self._IDX_MODE],
                "state": parts[self._IDX_STATE],
                "name": parts[self._IDX_NAME],
                "stratum": parts[self._IDX_STRATUM],
                "reach": parts[self._IDX_REACH],
                "offset_s": parts[self._IDX_OFFSET],
                "error_s": parts[self._IDX_ERROR],
            })
        return rows


def _reach_nonzero(reach: object) -> bool:
    """True if chrony's reach register is non-zero.

    `chronyc -c sources` prints reach as an octal register (e.g. "377"
    = last 8 polls all succeeded, "0" = no recent success).  We only
    need zero-vs-nonzero, and that distinction is base-independent, so a
    plain int() parse suffices.  Be lenient on a parse failure (treat as
    reachable): reach is reliably formatted, and a format quirk should
    not silently drop an otherwise-healthy witness."""
    try:
        return int(str(reach).strip() or "0") != 0
    except (TypeError, ValueError):
        return True


# ----- convenience matchers -----

def match_by_names(names: List[str]) -> Callable[[dict], bool]:
    """Match rows whose `name` equals any of the given names (case-insensitive)."""
    lowered = {n.lower() for n in names}
    def _m(row: dict) -> bool:
        return (row.get("name") or "").lower() in lowered
    return _m


def match_refclock(refid: Optional[str] = None) -> Callable[[dict], bool]:
    """Match refclock sources (mode '#'), optionally filtered by refid."""
    def _m(row: dict) -> bool:
        if row.get("mode") != "#":
            return False
        if refid is None:
            return True
        return (row.get("name") or "").upper() == refid.upper()
    return _m


def match_any_server_not_in(excluded_names: List[str]) -> Callable[[dict], bool]:
    """Match server sources (mode '^') whose name is not in the excluded
    list. Use for the T2 (WAN NTP witness) probe when T4 peers are
    separately configured."""
    excluded = {n.lower() for n in excluded_names}
    def _m(row: dict) -> bool:
        if row.get("mode") != "^":
            return False
        return (row.get("name") or "").lower() not in excluded
    return _m
