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
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence, TYPE_CHECKING

if TYPE_CHECKING:
    from hf_timestd.core.bootstrap_coordinator import BootstrapCoordinator, BootstrapState
    from hf_timestd.core.chrony_refclock_gate import ChronyRefclockGate
    from hf_timestd.core.mdns_fusion_advertiser import MdnsFusionAdvertiser
    from hf_timestd.io.authority_snapshot_store import AuthoritySnapshotStore

log = logging.getLogger(__name__)

SCHEMA_VERSION = "v1"

# T-levels in descending authority order. This is the single source of
# truth for rank comparisons; helpers that need to ask "is X ranked higher
# than Y?" should consult this tuple.
T_LEVELS_RANKED: tuple = ("T6", "T5", "T4", "T3", "T2", "T1", "T0")

# Mapping from active T-level to the SHM refid hf-timestd feeds into chrony
# under that tier.  Used by _check_chrony_self_feedback (V7): when our
# cascade says we're active at tier X but chrony has marked the matching
# refid as #x (falseticker) or #? (unselectable), we want to know about
# it loudly rather than silently keep publishing a stale T-level.
#
# TSL1 (SHM unit 0) — LEGACY; raw L1 metrology, normally disabled
# FUSE (SHM unit 1) — fused calibrated L2 timing, written by fusion service
# HPPS (SHM unit 2) — direct BPSK PPS, written by core_recorder T6 path
TIER_SHM_REFID: Dict[str, str] = {
    "T6": "HPPS",
    "T3": "FUSE",
}

# Chrony source-state characters indicating chrony accepts the source as
# usable for synchronisation.  Matches the convention in
# ChronyTrackingProbe.healthy_state_chars.
CHRONY_HEALTHY_STATES = "*+"

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
        chrony_gate: Optional["ChronyRefclockGate"] = None,
        governor_radiod_provider: Optional[Callable[[], Optional[str]]] = None,
        mdns_advertiser: Optional["MdnsFusionAdvertiser"] = None,
        snapshot_store: Optional["AuthoritySnapshotStore"] = None,
        demote_t6_on_breach: bool = False,
        demote_t6_on_breach_min_cycles: int = 3,
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
        self.chrony_gate = chrony_gate
        self.governor_radiod_provider = governor_radiod_provider
        self.mdns_advertiser = mdns_advertiser
        # V1 fix layer 4 — long-term observability.  When configured,
        # every tick mirrors the published state + per-probe detail
        # into a local SQLite DB so the per-cycle history (otherwise
        # overwritten in authority.json) is queryable hours/days
        # later.  None = no archiving (legacy behaviour preserved).
        self.snapshot_store = snapshot_store
        # Phase 2B — when True AND T6's drift_monitor reports a
        # sustained breach for ``demote_t6_on_breach_min_cycles``
        # consecutive ticks AND T5 is available past
        # ``upgrade_hysteresis``, the manager demotes the active
        # tier from T6 to T5 for as long as the breach persists.
        # Default False preserves Phase 2A behaviour byte-for-byte;
        # operator opt-in is the Phase 2C cutover.
        self.demote_t6_on_breach = bool(demote_t6_on_breach)
        self.demote_t6_on_breach_min_cycles = int(demote_t6_on_breach_min_cycles)

        self._avail_counters: Dict[str, int] = {lvl: 0 for lvl in T_LEVELS_RANKED}
        self._t_active: Optional[str] = None
        self._last_transition_utc: Optional[str] = None
        self._last_bootstrap: Optional["BootstrapState"] = None
        # Phase 2B — consecutive ticks where T6 was the picked tier
        # AND drift_monitor.sustained_breach was True.  Drives the
        # demote-on-breach hysteresis; resets when the breach clears
        # or T6 stops being the picked tier.
        self._t6_consecutive_breach_ticks: int = 0

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
                self._write_snapshot(state, results=None)
                self._apply_chrony_gate(state.t_level_active)
                self._apply_mdns_advertiser(state)
                return state

        results = self._poll_all()
        self._update_hysteresis(results)
        active = self._pick_active(results)
        # Phase 2B — demote T6→T5 when the drift monitor reports a
        # sustained breach for ``demote_t6_on_breach_min_cycles``
        # consecutive ticks.  No-op when the feature flag is off,
        # which is the default.  Flag accumulates here so the cross-
        # check downstream sees the post-demotion active.
        active, demote_flag = self._maybe_demote_breached_t6(active, results)
        active, witnesses, flags = self._cross_check(active, results)
        if demote_flag is not None:
            flags = list(flags) + [demote_flag]
        # V7: append a chrony-feedback flag if chrony has rejected the
        # SHM segment we feed for the active tier.  Silent no-op when
        # chronyc is unavailable.
        feedback_flag = self._check_chrony_self_feedback(active)
        if feedback_flag is not None:
            flags = list(flags) + [feedback_flag]
        self._note_transition(active)
        state = self._build_state(results, active, witnesses, flags)
        self._write_state(state)
        self._write_snapshot(state, results)
        self._apply_chrony_gate(state.t_level_active)
        self._apply_mdns_advertiser(state)
        return state

    def _apply_mdns_advertiser(self, state: AuthorityState) -> None:
        """Let the mDNS advertiser react to the current state. Publish /
        withdraw is decided by the advertiser's own policy (T3/T6 eligible)
        so this method is just the dispatch point."""
        if self.mdns_advertiser is None:
            return
        governor = None
        if self.governor_radiod_provider is not None:
            try:
                governor = self.governor_radiod_provider()
            except Exception as e:
                log.debug("governor_radiod_provider raised: %s", e)
                governor = None
        try:
            result = self.mdns_advertiser.apply(state, governor)
        except Exception as e:
            log.exception("mDNS advertiser raised: %s", e)
            return
        if result.applied:
            log.info(
                "mDNS advertiser: %s (%s)", result.target_state, result.reason,
            )
        elif result.reason and result.reason != "no change":
            log.warning(
                "mDNS advertiser unapplied: target=%s reason=%s",
                result.target_state, result.reason,
            )

    def _apply_chrony_gate(self, t_level_active: Optional[str]) -> None:
        """Update chrony's view of the Fusion SHM refclock based on the
        current active T-level (§4.6)."""
        if self.chrony_gate is None:
            return
        try:
            result = self.chrony_gate.apply(t_level_active)
        except Exception as e:
            log.exception("Chrony refclock gate raised: %s", e)
            return
        if result.applied:
            log.info(
                "Chrony refclock gate: %s (%s)", result.target_state, result.reason,
            )
        elif result.reason and result.reason != "no change":
            # Soft failures (chronyc not found, timeout, permission denied)
            # are worth flagging once per transition; noisy in steady state
            # otherwise so we rely on the "no change" fast-path above.
            log.warning(
                "Chrony refclock gate unapplied: target=%s reason=%s",
                result.target_state, result.reason,
            )

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

    def _maybe_demote_breached_t6(
        self,
        active: Optional[str],
        results: Dict[str, ProbeResult],
    ) -> tuple:
        """Phase 2B — demote T6→T5 when T6's drift monitor reports a
        sustained breach for ``demote_t6_on_breach_min_cycles``
        consecutive ticks AND T5 is available past hysteresis.

        Returns ``(active_after_demotion, disagreement_flag_or_None)``.
        The flag, when emitted, lands in ``state.disagreement_flags``
        so downstream consumers (sigmond TUI, snapshot store) can
        observe that the active T5 cycle was triggered by a T6 breach
        rather than a normal T6 unavailability.

        State machine:

        * counter increments while T6 is picked AND
          ``T6.detail.drift_monitor.sustained_breach`` is True;
        * counter resets to 0 in every other case (T6 not picked,
          breach cleared, drift_monitor missing).

        Default flag-off (``demote_t6_on_breach == False``) makes
        this method a no-op while still maintaining the counter for
        post-hoc telemetry.
        """
        # Always maintain the breach counter so observability is
        # consistent regardless of whether the feature flag is on.
        is_t6_breached_this_tick = False
        if active == "T6":
            t6_res = results.get("T6")
            if t6_res is not None and t6_res.detail:
                dm = t6_res.detail.get("drift_monitor")
                if isinstance(dm, dict) and dm.get("sustained_breach"):
                    is_t6_breached_this_tick = True
        if is_t6_breached_this_tick:
            self._t6_consecutive_breach_ticks += 1
        else:
            self._t6_consecutive_breach_ticks = 0

        if not self.demote_t6_on_breach:
            return active, None
        if active != "T6":
            return active, None
        if self._t6_consecutive_breach_ticks < self.demote_t6_on_breach_min_cycles:
            return active, None
        t5_res = results.get("T5")
        if t5_res is None or not t5_res.available:
            return active, None
        if self._avail_counters.get("T5", 0) < self.upgrade_hysteresis:
            return active, None
        flag = (
            f"demote-t6-breach->t5:"
            f"{self._t6_consecutive_breach_ticks}cycles"
        )
        return "T5", flag

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

    def _check_chrony_self_feedback(self, active: Optional[str]) -> Optional[str]:
        """V7 — verify chrony's verdict on the SHM segment we feed for the
        active tier.  If chrony has rejected our source (state ``#x``
        falseticker, ``#?`` unselectable, etc.) while authority is
        claiming the tier as active, return a disagreement flag.

        Silently no-ops when chronyc is missing, times out, or returns
        garbage — we never want this check to fail the cascade.  An
        operator running hf-timestd without a local chrony deployment
        won't be alarmed.

        See docs/TIMING-PIPELINE-WIRING.md V7 for context.
        """
        if active is None:
            return None
        refid = TIER_SHM_REFID.get(active)
        if refid is None:
            # Tier isn't fed via local SHM (T2/T4/T5 are chrony-tracked
            # external peers; T1/T0 don't produce a refclock).  Nothing to
            # cross-check.
            return None
        try:
            proc = subprocess.run(
                ["chronyc", "-n", "-c", "sources"],
                capture_output=True,
                text=True,
                timeout=2.0,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return None
        if proc.returncode != 0:
            return None

        for line in (proc.stdout or "").splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 3:
                continue
            mode, state, name = parts[0], parts[1], parts[2]
            if mode != "#":
                continue  # refclocks only; "^" rows are NTP peers
            if name.upper() != refid.upper():
                continue
            if state in CHRONY_HEALTHY_STATES:
                return None
            return f"chrony-rejected-{refid}:state={state}"

        # Refid not present in chrony's source list at all — chrony either
        # isn't configured to consume our SHM segment, or the segment
        # hasn't seen its first sample yet.  Surface as a distinct flag.
        return f"chrony-missing-{refid}"

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
            # T5/T4/T2/T1 — trust-based.  Phase 2B: when a probe
            # carries the RTP-substrate-grounded marker in its detail
            # (e.g., LbeT5DirectProbe forwarding an anchor
            # disagreement measured against the ka9q anchor), honor
            # the probe's offset_ms / sigma_ms as the published
            # rtp_to_utc_offset_ns.  Without the marker (e.g.,
            # ChronyTrackingProbe at a T5 site without LBE-1421, T4
            # LAN NTP peer, T2 WAN), fall through to legacy trust-
            # tier defaults so the probe's chrony-residual offset_ms
            # is not mis-republished as an RTP-anchor disagreement.
            a_res = results[active]
            anchor_grounded = (
                a_res.detail.get("rtp_anchor_grounded") is True
                if a_res.detail else False
            )
            if anchor_grounded and a_res.offset_ms is not None:
                offset_ns = int(round(a_res.offset_ms * 1_000_000))
            else:
                offset_ns = 0
            if anchor_grounded and a_res.sigma_ms is not None:
                sigma_ns = int(round(a_res.sigma_ms * 1_000_000))
            else:
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

        # Additive v1 extension: governor_radiod names which radiod's
        # RTP timebase this Fusion offset is computed against (§4.5.1
        # multi-radiod clarification). Omitted when no provider is
        # configured so legacy output is byte-compatible.
        if self.governor_radiod_provider is not None:
            try:
                governor = self.governor_radiod_provider()
            except Exception as e:
                log.debug("governor_radiod_provider raised: %s", e)
                governor = None
            if governor:
                payload["governor_radiod"] = str(governor)

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
            # authority.json is the canonical service-discovery artifact
            # for consumer clients (wspr-recorder, psk-recorder, hfdl-
            # recorder, etc.).  NamedTemporaryFile defaults to mode
            # 0600, which silently blocks every non-timestd consumer
            # from reading it — the symptom is the
            # "hf-timestd authority unavailable — standalone fallback"
            # WARNING in client logs.  Make it world-readable here.
            os.chmod(tmp_path, 0o644)
            os.replace(tmp_path, self.output_path)
        except OSError as e:
            log.warning("AuthorityManager: failed to write %s: %s", self.output_path, e)

    def _write_snapshot(
        self,
        state: AuthorityState,
        results: Optional[Dict[str, ProbeResult]],
    ) -> None:
        """V1 layer 4 — mirror this tick's state + per-probe detail
        into the long-term observability store.

        The store is optional (None = legacy no-op).  Failure inside
        the store doesn't propagate — it logs and returns.  The
        authority.json write above has already succeeded; a stale or
        missing row here is an observability gap, not a service
        failure.

        ``results`` is ``None`` only on the bootstrap-pending path
        (no probes were polled).  In that case the snapshot still
        records the published state but the per-probe detail columns
        land as NULL.
        """
        if self.snapshot_store is None:
            return

        snapshot: Dict[str, Any] = {
            "utc_published": _iso_z(self.now_fn()),
            "schema_version": SCHEMA_VERSION,
            "a_level": state.a_level,
            "t_level_active": state.t_level_active,
            "t_level_available": list(state.t_level_available),
            "t_level_witnesses": list(state.t_level_witnesses),
            "rtp_to_utc_offset_ns": state.rtp_to_utc_offset_ns,
            "sigma_ns": state.sigma_ns,
            "stations_contributing": list(state.stations_contributing),
            "last_transition_utc": state.last_transition_utc,
            "disagreement_flags": list(state.disagreement_flags),
        }

        if self.governor_radiod_provider is not None:
            try:
                governor = self.governor_radiod_provider()
                if governor:
                    snapshot["governor_radiod"] = str(governor)
            except Exception:
                pass

        bs = self._last_bootstrap
        if bs is not None:
            snapshot["bootstrap_complete"] = 1 if bs.complete else 0
            snapshot["bootstrap_reason"] = bs.reason
            snapshot["bootstrap_delta_sec"] = bs.delta_sec

        if results is not None:
            _flatten_t6(snapshot, results.get("T6"))
            _flatten_t5(snapshot, results.get("T5"))
            _flatten_t4(snapshot, results.get("T4"))
            _flatten_t3(snapshot, results.get("T3"))

        try:
            self.snapshot_store.insert(snapshot)
        except Exception as exc:
            log.warning(
                "AuthorityManager: snapshot store raised: %s", exc,
            )


def _flatten_t6(snapshot: Dict[str, Any], r: Optional[ProbeResult]) -> None:
    """Pull BpskPpsProbe detail into the flat snapshot columns,
    including Layer 2 drift_monitor + Layer 3 recapture fields."""
    if r is None:
        return
    snapshot["t6_available"] = 1 if r.available else 0
    snapshot["t6_reason"] = r.reason
    snapshot["t6_offset_ms"] = r.offset_ms
    snapshot["t6_sigma_ms"] = r.sigma_ms
    d = r.detail or {}
    snapshot["t6_local_minus_source_ns"] = d.get("local_minus_source_ns")
    snapshot["t6_pps_ok"] = d.get("pps_ok")
    snapshot["t6_pps_noise"] = d.get("pps_noise")
    snapshot["t6_pps_consecutive"] = d.get("pps_consecutive")
    snapshot["t6_chain_delay_ns"] = d.get("chain_delay_ns")
    dm = d.get("drift_monitor")
    if isinstance(dm, dict):
        snapshot["t6_anchor_discontinuity"] = (
            1 if dm.get("anchor_discontinuity") else 0
        )
        snapshot["t6_sustained_breach"] = (
            1 if dm.get("sustained_breach") else 0
        )
        snapshot["t6_anchor_residual_samples"] = dm.get(
            "anchor_residual_samples"
        )
        snapshot["t6_breach_duration_sec"] = dm.get("breach_duration_sec")
        snapshot["t6_recapture_count"] = dm.get("recapture_count")
        snapshot["t6_last_recapture_reason"] = dm.get("last_recapture_reason")
        snapshot["t6_last_recapture_age_sec"] = dm.get(
            "last_recapture_age_sec"
        )


def _flatten_t5(snapshot: Dict[str, Any], r: Optional[ProbeResult]) -> None:
    """Pull LbeT5DirectProbe (or ChronyTrackingProbe-T5) detail into
    the flat snapshot columns.  The probe-shape is generic, so this
    works for either source; the substrate-specific fields
    (valid_fix, pps_utc_sec, nmea_age_sec) only populate when the
    probe is LbeT5DirectProbe (the others leave them None)."""
    if r is None:
        return
    snapshot["t5_available"] = 1 if r.available else 0
    snapshot["t5_offset_ms"] = r.offset_ms
    snapshot["t5_sigma_ms"] = r.sigma_ms
    d = r.detail or {}
    valid_fix = d.get("valid_fix")
    if valid_fix is not None:
        snapshot["t5_valid_fix"] = 1 if valid_fix else 0
    snapshot["t5_pps_utc_sec"] = d.get("pps_utc_sec")
    snapshot["t5_nmea_age_sec"] = d.get("nmea_age_sec")


def _flatten_t4(snapshot: Dict[str, Any], r: Optional[ProbeResult]) -> None:
    if r is None:
        return
    snapshot["t4_available"] = 1 if r.available else 0
    snapshot["t4_offset_ms"] = r.offset_ms
    snapshot["t4_sigma_ms"] = r.sigma_ms


def _flatten_t3(snapshot: Dict[str, Any], r: Optional[ProbeResult]) -> None:
    if r is None:
        return
    snapshot["t3_available"] = 1 if r.available else 0
    snapshot["t3_offset_ms"] = r.offset_ms
    snapshot["t3_sigma_ms"] = r.sigma_ms
    d = r.detail or {}
    snapshot["t3_kalman_state"] = d.get("kalman_state")


def _iso_z(dt: datetime) -> str:
    return dt.isoformat(timespec="microseconds").replace("+00:00", "Z")
