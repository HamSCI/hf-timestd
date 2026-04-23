"""
AuthorityManager — selects, cross-checks, and publishes the active
Timing Authority level per METROLOGY.md §4.5 / §4.6.

The manager is the single writer of /run/hf-timestd/authority.json and
the single policy layer above the existing chrony/NTP/mDNS transport.
It does not mutate the system clock (except via the bootstrap path
implemented in a later sub-commit); it only classifies, selects, and
publishes.

This module is intentionally free of service dependencies:
  - Probes are injected (anything matching the Probe protocol).
  - The A-level is provided via a callable so hardware detection can
    live elsewhere.
  - The "now" source is injectable for deterministic testing.

Concrete probes (FusionStatusProbe, chrony-based probes, BCD/FSK
bootstrap probe) live in their own modules and are composed by the
service entrypoint.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Protocol, Sequence, TYPE_CHECKING

if TYPE_CHECKING:
    from hf_timestd.core.bootstrap_coordinator import BootstrapCoordinator, BootstrapState

log = logging.getLogger(__name__)

SCHEMA_VERSION = "v1"

# T-levels in descending authority order. This is the single source of
# truth for rank comparisons; helpers that need to ask "is X ranked higher
# than Y?" should consult this tuple.
T_LEVELS_RANKED: tuple = ("T6", "T5", "T4", "T3", "T2", "T1", "T0")

# Cross-check disagreement thresholds per METROLOGY.md §4.5:
# expected_agreement = 3 * sqrt(sigma_A² + sigma_B²), FLOORED at these
# per-pair values so a noisy witness can't mask a real disagreement by
# inflating the combined sigma.
DEFAULT_PAIR_THRESHOLDS_MS: Dict[frozenset, float] = {
    frozenset({"T6", "T5"}): 0.050,  # 50 μs
    frozenset({"T3", "T4"}): 2.0,    # 2 ms
    frozenset({"T3", "T2"}): 5.0,    # 5 ms
}

# Asymmetric T3↔T2 rule: WAN NTP being wildly wrong vs Fusion is rare;
# Fusion being wildly wrong vs WAN NTP is a hardware/detection bug. If
# the two disagree by more than this, force T3 down regardless of the
# normal cross-check math.
ASYMMETRIC_T3_T2_FORCE_DOWN_MS = 1000.0

# Trust-based sigmas used when the active level is a system-clock
# discipline (T5/T4/T2/T1). Under the RTP-reference invariant these
# levels do not measure RTP→UTC directly, so we publish offset=0 with
# a sigma representative of the tier.
TRUST_SIGMA_MS: Dict[str, float] = {
    "T5": 0.010,   # ~10 μs — on-host GPS+PPS
    "T4": 2.0,     # ~2 ms — LAN GPS+PPS via NTP
    "T2": 20.0,    # ~20 ms — WAN NTP
    "T1": 1.0,     # GPSDO coast — rate perfect, phase frozen at last snapshot
}


@dataclass
class ProbeResult:
    """One tick of probe output. Fields other than `t_level`/`available`
    are optional; probes that don't measure RTP→UTC (chrony-based,
    trust-based) leave `offset_ms`/`sigma_ms` as None."""
    t_level: str
    available: bool
    offset_ms: Optional[float] = None
    sigma_ms: Optional[float] = None
    detail: Dict[str, object] = field(default_factory=dict)
    reason: Optional[str] = None


class Probe(Protocol):
    t_level: str
    def poll(self) -> ProbeResult: ...


@dataclass
class AuthorityState:
    """Snapshot of what the manager most recently decided."""
    a_level: str
    t_level_active: Optional[str]
    t_level_available: List[str]
    t_level_witnesses: List[str]
    rtp_to_utc_offset_ns: Optional[int]
    sigma_ns: Optional[int]
    stations_contributing: List[str]
    last_transition_utc: Optional[str]
    disagreement_flags: List[str]


class AuthorityManager:
    """Polls probes, selects the active T-level with hysteresis,
    cross-checks against lower-level witnesses, and atomically publishes
    authority.json per §4.5 schema v1.
    """

    def __init__(
        self,
        probes: Sequence[Probe],
        output_path: Path,
        a_level_provider: Callable[[], str],
        upgrade_hysteresis: int = 3,
        pair_thresholds_ms: Optional[Dict[frozenset, float]] = None,
        now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        bootstrap_coordinator: Optional["BootstrapCoordinator"] = None,
    ):
        self.probes = list(probes)
        self.output_path = Path(output_path)
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.a_level_provider = a_level_provider
        self.upgrade_hysteresis = upgrade_hysteresis
        self.pair_thresholds_ms = (
            pair_thresholds_ms if pair_thresholds_ms is not None
            else DEFAULT_PAIR_THRESHOLDS_MS
        )
        self.now_fn = now_fn
        self.bootstrap_coordinator = bootstrap_coordinator

        self._avail_counters: Dict[str, int] = {lvl: 0 for lvl in T_LEVELS_RANKED}
        self._t_active: Optional[str] = None
        self._last_transition_utc: Optional[str] = None
        self._last_bootstrap: Optional["BootstrapState"] = None

    def tick(self) -> AuthorityState:
        """Run one authority-decision cycle: poll probes, update
        hysteresis, select active, cross-check, publish. Intended to be
        called on a fixed cadence (default 30 s) from a service thread,
        or directly from tests.

        When a bootstrap coordinator is attached and reports the system
        clock is too far off to run normal probes, this method publishes
        a bootstrap-pending state and returns early without polling. The
        probes resume on the next tick once the coordinator reports
        complete (either because the gap closed on its own or because a
        chronyc makestep ran and brought us into range).
        """
        if self.bootstrap_coordinator is not None:
            self._last_bootstrap = self.bootstrap_coordinator.check_and_step(self.now_fn)
            if not self._last_bootstrap.complete:
                state = self._build_bootstrap_pending_state()
                self._write_state(state)
                return state

        results = self._poll_all()
        self._update_hysteresis(results)
        active = self._pick_active(results)
        active, witnesses, flags = self._cross_check(active, results)
        self._note_transition(active)
        state = self._build_state(results, active, witnesses, flags)
        self._write_state(state)
        return state

    def _build_bootstrap_pending_state(self) -> AuthorityState:
        """Authority state when the bootstrap coordinator has gated
        normal probing. No active level, no offset, but A-level and
        transition history preserved so consumers see continuity."""
        return AuthorityState(
            a_level=self.a_level_provider(),
            t_level_active=None,
            t_level_available=[],
            t_level_witnesses=[],
            rtp_to_utc_offset_ns=None,
            sigma_ns=None,
            stations_contributing=[],
            last_transition_utc=self._last_transition_utc,
            disagreement_flags=[],
        )

    def _poll_all(self) -> Dict[str, ProbeResult]:
        results: Dict[str, ProbeResult] = {}
        for p in self.probes:
            try:
                results[p.t_level] = p.poll()
            except Exception as e:
                log.exception("Probe %s raised: %s", p.t_level, e)
                results[p.t_level] = ProbeResult(
                    t_level=p.t_level, available=False,
                    reason=f"probe exception: {e}",
                )
        for lvl in T_LEVELS_RANKED:
            if lvl not in results:
                results[lvl] = ProbeResult(
                    t_level=lvl, available=False, reason="no probe configured",
                )
        return results

    def _update_hysteresis(self, results: Dict[str, ProbeResult]) -> None:
        for lvl, r in results.items():
            if r.available:
                self._avail_counters[lvl] += 1
            else:
                self._avail_counters[lvl] = 0

    def _pick_active(self, results: Dict[str, ProbeResult]) -> Optional[str]:
        for lvl in T_LEVELS_RANKED:
            if (
                results[lvl].available
                and self._avail_counters[lvl] >= self.upgrade_hysteresis
            ):
                return lvl
        return None

    def _cross_check(
        self, active: Optional[str], results: Dict[str, ProbeResult]
    ) -> tuple:
        """Returns (active, witnesses, disagreement_flags). May downgrade
        active per the majority-witness rule or the asymmetric T3↔T2 rule.
        """
        if active is None:
            return None, [], []

        active_result = results[active]
        witnesses: List[str] = []
        disagreement_flags: List[str] = []

        for lvl in T_LEVELS_RANKED:
            if lvl == active:
                continue
            r = results[lvl]
            if r.available and r.offset_ms is not None:
                witnesses.append(lvl)
                flag = self._check_pair(active, active_result, lvl, r)
                if flag:
                    disagreement_flags.append(flag)

        # Majority-witness downgrade: ≥ 2 witnesses agreeing with each
        # other AND disagreeing with active → active is the outlier.
        downgrade = self._maybe_majority_downgrade(active, active_result, witnesses, results)
        if downgrade:
            disagreement_flags.append(f"majority-downgrade:{active}->{downgrade}")
            active = downgrade
            active_result = results[active]

        # Asymmetric T3↔T2 rule: very large disagreement forces T3 down.
        if (
            active == "T3"
            and "T2" in witnesses
            and active_result.offset_ms is not None
            and results["T2"].offset_ms is not None
        ):
            diff = abs(active_result.offset_ms - results["T2"].offset_ms)
            if diff > ASYMMETRIC_T3_T2_FORCE_DOWN_MS:
                disagreement_flags.append(
                    f"asymmetric-T3-T2:{diff:.0f}ms>{ASYMMETRIC_T3_T2_FORCE_DOWN_MS:.0f}ms"
                )
                active = "T2"

        return active, witnesses, disagreement_flags

    def _check_pair(
        self, a: str, a_res: ProbeResult, b: str, b_res: ProbeResult,
    ) -> Optional[str]:
        """Return a disagreement flag string if |Δ| exceeds the combined
        CI (floored at the per-pair threshold), else None. If either side
        lacks a measured offset, returns None — cross-check is only
        meaningful when both sides measure RTP→UTC."""
        if a_res.offset_ms is None or b_res.offset_ms is None:
            return None
        diff = abs(a_res.offset_ms - b_res.offset_ms)
        sa = a_res.sigma_ms if a_res.sigma_ms is not None else TRUST_SIGMA_MS.get(a, 1.0)
        sb = b_res.sigma_ms if b_res.sigma_ms is not None else TRUST_SIGMA_MS.get(b, 1.0)
        rss = 3.0 * (sa * sa + sb * sb) ** 0.5
        floor = self.pair_thresholds_ms.get(frozenset({a, b}), 0.0)
        threshold = max(rss, floor)
        if diff > threshold:
            return f"{a}<->{b}:{diff:.3f}ms>{threshold:.3f}ms"
        return None

    def _maybe_majority_downgrade(
        self,
        active: str,
        a_res: ProbeResult,
        witnesses: List[str],
        results: Dict[str, ProbeResult],
    ) -> Optional[str]:
        if a_res.offset_ms is None:
            return None
        disagreeing = [
            w for w in witnesses
            if self._check_pair(active, a_res, w, results[w]) is not None
        ]
        if len(disagreeing) < 2:
            return None
        # Confirm the disagreeing witnesses agree with each other — if
        # they don't, there's no coherent alternative and we hold active.
        for i in range(len(disagreeing)):
            for j in range(i + 1, len(disagreeing)):
                w1, w2 = disagreeing[i], disagreeing[j]
                if self._check_pair(w1, results[w1], w2, results[w2]) is not None:
                    return None
        # Downgrade to the highest-ranked disagreeing witness.
        for lvl in T_LEVELS_RANKED:
            if lvl in disagreeing:
                return lvl
        return None

    def _note_transition(self, active: Optional[str]) -> None:
        if active != self._t_active:
            self._last_transition_utc = _iso_z(self.now_fn())
            self._t_active = active

    def _build_state(
        self,
        results: Dict[str, ProbeResult],
        active: Optional[str],
        witnesses: List[str],
        disagreement_flags: List[str],
    ) -> AuthorityState:
        available = [lvl for lvl in T_LEVELS_RANKED if results[lvl].available]

        offset_ns: Optional[int] = None
        sigma_ns: Optional[int] = None
        stations: List[str] = []

        if active in ("T3", "T6"):
            a_res = results[active]
            if a_res.offset_ms is not None:
                offset_ns = int(round(a_res.offset_ms * 1_000_000))
            if a_res.sigma_ms is not None:
                sigma_ns = int(round(a_res.sigma_ms * 1_000_000))
            st = a_res.detail.get("stations_used") if a_res.detail else None
            if isinstance(st, list):
                stations = [str(s) for s in st]
        elif active in TRUST_SIGMA_MS:
            # T5/T4/T2/T1 — trust-based: RTP-time is authoritative,
            # publish offset=0 with tier sigma so consumers know the
            # offset is a no-op, not missing data.
            offset_ns = 0
            sigma_ns = int(round(TRUST_SIGMA_MS[active] * 1_000_000))
        # active == "T0" or None → offset_ns / sigma_ns remain None

        return AuthorityState(
            a_level=self.a_level_provider(),
            t_level_active=active,
            t_level_available=available,
            t_level_witnesses=witnesses,
            rtp_to_utc_offset_ns=offset_ns,
            sigma_ns=sigma_ns,
            stations_contributing=stations,
            last_transition_utc=self._last_transition_utc,
            disagreement_flags=disagreement_flags,
        )

    def _write_state(self, state: AuthorityState) -> None:
        payload: dict = {
            "schema": SCHEMA_VERSION,
            "utc_published": _iso_z(self.now_fn()),
            "a_level": state.a_level,
            "t_level_active": state.t_level_active,
            "t_level_available": state.t_level_available,
            "t_level_witnesses": state.t_level_witnesses,
            "rtp_to_utc_offset_ns": state.rtp_to_utc_offset_ns,
            "sigma_ns": state.sigma_ns,
            "stations_contributing": state.stations_contributing,
            "last_transition_utc": state.last_transition_utc,
            "disagreement_flags": state.disagreement_flags,
        }

        # Additive v1 extension: include bootstrap block when the
        # coordinator has touched anything this tick. Omit entirely
        # when no coordinator is attached so legacy output is unchanged.
        bs = self._last_bootstrap
        if bs is not None:
            payload["bootstrap"] = {
                "complete": bs.complete,
                "reason": bs.reason,
                "delta_sec": bs.delta_sec,
                "stepped": bs.stepped,
                "coarse_source": bs.coarse.source if bs.coarse else None,
                "coarse_station": bs.coarse.station if bs.coarse else None,
            }
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                dir=str(self.output_path.parent),
                prefix=f".{self.output_path.name}.",
                suffix=".tmp",
                delete=False,
                encoding="utf-8",
            ) as tmp:
                json.dump(payload, tmp, separators=(",", ":"))
                tmp.flush()
                os.fsync(tmp.fileno())
                tmp_path = tmp.name
            os.replace(tmp_path, self.output_path)
        except OSError as e:
            log.warning("AuthorityManager: failed to write %s: %s", self.output_path, e)


def _iso_z(dt: datetime) -> str:
    return dt.isoformat(timespec="microseconds").replace("+00:00", "Z")
