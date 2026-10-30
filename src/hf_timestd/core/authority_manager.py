"""
AuthorityManager — selects, cross-checks, and publishes the active
Timing Authority level per METROLOGY.md §4.5 / §4.6.

The manager is the single writer of /run/hf-timestd/authority.json and
the single policy layer above the existing chrony/NTP/mDNS transport.
It never mutates the system clock; it only classifies, selects, and
publishes.  D_clock is a derived quantity handed to chrony, and chrony
steps (MEASUREMENT_MODEL.md §7.1).

This module is intentionally free of service dependencies:
  - Probes are injected (anything matching the Probe protocol).
  - The A-level is provided via a callable so hardware detection can
    live elsewhere.
  - The "now" source is injectable for deterministic testing.

Concrete probes (FusionStatusProbe, chrony-based probes, the BPSK-PPS
and LB-1421 probes) live in their own modules and are composed by the
service entrypoint.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import math
import os
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence, Tuple, TYPE_CHECKING

from hf_timestd.core.host_clock_integrity import (
    DEFAULT_FAULT_MS as HOST_CLOCK_DEFAULT_FAULT_MS,
    DEFAULT_RATE_SUSPECT_PPM as HOST_CLOCK_DEFAULT_RATE_SUSPECT_PPM,
    HostClockAlarm,
    assess as assess_host_clock,
)

if TYPE_CHECKING:
    from hf_timestd.core.mdns_fusion_advertiser import MdnsFusionAdvertiser
    from hf_timestd.io.authority_snapshot_store import AuthoritySnapshotStore
    from hf_timestd.core.frontend_probe import FrontendProbe
    from hf_timestd.core.registration_store import RegistrationStore
    from hf_timestd.core.t3_shadow_record import T3ShadowRecord

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
# HPPS (SHM unit 2) — direct BPSK PPS, written by core_recorder T6 path.
# T3 feeds no refclock: FUSE (SHM unit 1) carried fusion's calibrated
# D_clock and RETIRED on 2026-10-22 (review-fusion-kalman-20261022.md
# section 15, ruling 5), so a chrony without FUSE is the expected state.
TIER_SHM_REFID: Dict[str, str] = {
    "T6": "HPPS",
}

# Chrony source-state characters indicating chrony accepts the source as
# usable for synchronisation.  Matches the convention in
# ChronyTrackingProbe.healthy_state_chars.
CHRONY_HEALTHY_STATES = "*+"

# chrony's own config: the main file plus its standard include directory.
# _check_chrony_self_feedback reads these (never chronyc selectdata, which
# needs the privileged socket hf-timestd doesn't have -- verified on B4:
# "501 Not authorised") to learn whether a refid is declared `noselect`.
CHRONY_MAIN_CONF = Path("/etc/chrony/chrony.conf")
CHRONY_CONF_D = Path("/etc/chrony/conf.d")


def _default_chrony_conf_paths() -> List[Path]:
    """Chrony's main conf file plus every ``conf.d/*.conf``, in read
    order.  Re-globbed on each call rather than cached at import time,
    since conf.d gains/loses drop-ins independently of this process."""
    paths: List[Path] = [CHRONY_MAIN_CONF]
    try:
        paths.extend(sorted(CHRONY_CONF_D.glob("*.conf")))
    except OSError:
        pass
    return paths


def _refid_carries_noselect(refid: str, conf_paths: Sequence[Path]) -> bool:
    """True if any ``refclock`` line in ``conf_paths`` declares ``refid``
    (case-insensitively, via ``refid <NAME>``) with a bare ``noselect``
    option token.  ``# ...`` comments are stripped before tokenising, so
    a `noselect` mentioned only in a comment does not count.  Missing or
    unreadable files are skipped silently -- this must never raise."""
    target = refid.upper()
    found = False
    for path in conf_paths:
        try:
            text = Path(path).read_text()
        except OSError:
            continue
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            tokens = line.split()
            if not tokens or tokens[0].lower() != "refclock":
                continue
            try:
                refid_idx = next(
                    i for i, t in enumerate(tokens) if t.lower() == "refid")
            except StopIteration:
                continue
            if refid_idx + 1 >= len(tokens):
                continue
            if tokens[refid_idx + 1].upper() != target:
                continue
            found = any(t.lower() == "noselect" for t in tokens)
    return found


# Cross-check disagreement thresholds per METROLOGY.md §4.5:
# expected_agreement = 3 * sqrt(sigma_A² + sigma_B²), FLOORED at these
# per-pair values so a noisy witness can't mask a real disagreement by
# inflating the combined sigma.
DEFAULT_PAIR_THRESHOLDS_MS: Dict[frozenset, float] = {
    # T6 is ns-class; T5 is USB-bus-jitter floored at µs-to-ms (METROLOGY.md
    # §4.5). The floor must be sized to T5's combined uncertainty so we alarm
    # only on disagreement beyond what USB transport explains — 5 ms per the
    # §4.5 table. (Was 50 µs, ~100× too tight: it alarmed on T5's normal
    # jitter, manufacturing spurious TIMING_DISAGREEMENT flags.)
    frozenset({"T6", "T5"}): 5.0,    # 5 ms
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
    # T5 is GPS+PPS *delivered over USB* (LBE-1421 USB-NMEA); its accuracy is
    # USB-bus-jitter floored at µs-to-ms (METROLOGY.md §4.5), not the ~10 µs a
    # kernel-PPS path would give. 10 µs here was overconfident by ~100× — it
    # understated published T5 uncertainty and (via the cross-check) over-
    # tightened the T6↔T5 floor. 1 ms is a defensible USB-NMEA typical; the
    # LbeT5DirectProbe reports its own measured sigma when available and
    # overrides this fallback.
    "T5": 1.0,     # ~1 ms — USB-delivered GPS+PPS (bus-jitter floored)
    "T4": 2.0,     # ~2 ms — LAN GPS+PPS via NTP
    "T2": 20.0,    # ~20 ms — WAN NTP
    "T1": 1.0,     # GPSDO coast — rate perfect, phase frozen at last snapshot
}

# Tiers whose offset is GPS/PPS-disciplined AND derived from the RTP
# stream (system-clock-INDEPENDENT): T6 (BPSK-PPS) and T5 (GPS+PPS).
# When one of these is the active tier and is publishing an rtp-frame
# offset, a sysclock-frame witness (chrony T4/T2) that disagrees almost
# always means the *system clock* drifted (e.g. SHM discipline lost),
# NOT that the GPS-disciplined offset is wrong — so such a witness is
# advisory only (it raises a clearly-labelled flag but does not widen
# the published sigma or contribute to a downgrade). Same-frame (rtp)
# witnesses still fully cross-check these tiers. Lower-trust rtp tiers
# like T3 (Fusion, ms-class, fallible, ranks below T4) are deliberately
# NOT protected — a sysclock sanity-check on Fusion is wanted.
GPS_DISCIPLINED_RTP_TIERS = frozenset({"T6", "T5"})


def rtp_to_utc_offset_ms_from_d_clock(d_clock_ms: float) -> float:
    """The T3 rung's offset from a D_clock, in the ruled convention.

    d_clock means HOST minus UTC: the metrology engine writes
    ``timing_error = arrival_on_ring - expected_delay``, and the ring is
    stamped from the host clock.  ``rtp_to_utc_offset_ns`` means UTC
    minus host: ``label_utc = rtp_time + rtp_to_utc_offset_ns``
    (MEASUREMENT_MODEL.md; Michael's ruling of 2026-10-22, review-fusion-
    kalman-20261022.md section 15).  So the offset is MINUS d_clock.  A
    host 5 ms fast reads d_clock +5 ms and needs an offset of -5 ms.

    Every T3 source converts through here, so a future one (ruling 4:
    the tick registration) inherits the right sign.  Until 2026-10-22
    the fusion probe handed +d_clock to the rung, and the calibration
    that zeroes d_clock hid the inversion.
    """
    return -float(d_clock_ms)


@dataclass
class ProbeResult:
    """One tick of probe output. Fields other than `t_level`/`available`
    are optional; probes that don't measure RTP→UTC (chrony-based,
    trust-based) leave `offset_ms`/`sigma_ms` as None.

    ``frame`` names the reference clock the offset is measured against:
      - ``"rtp"``: the active anchor's UTC error judged against a signal
        in the RTP stream (BPSK-PPS / GPS-NMEA / HF ticks) —
        system-clock-INDEPENDENT. Set by BpskPpsProbe (T6),
        LbeT5DirectProbe (T5), FusionStatusProbe (T3).
      - ``"sysclock"``: chrony's ``local_clock − source`` — depends on
        the host clock's discipline. Set by ChronyTrackingProbe
        (T5-via-refclock / T4 / T2). Default.
    Cross-checking two offsets in different frames compares different
    clocks' errors; see _witness_drives_consequences (METROLOGY §4.5)."""
    t_level: str
    available: bool
    offset_ms: Optional[float] = None
    sigma_ms: Optional[float] = None
    detail: Dict[str, object] = field(default_factory=dict)
    reason: Optional[str] = None
    frame: str = "sysclock"
    # ⛔ AC0G-ND, 2026-09-03.  A source can carry a perfectly good MEASUREMENT
    # while chrony refuses to SELECT it, and the two must not be conflated.
    # `trust` on the FUSE refclock made chrony mark all three WAN servers
    # falsetickers (`^x`) at -12.2 s.  The T2 probe requires state `*`/`+`, so
    # T2 went unavailable, the witness set came back EMPTY, and the asymmetric
    # T3↔T2 rule below — which exists precisely to catch a wildly-wrong Fusion —
    # could not fire.  `trust` had disabled the check written to catch it.
    #
    # A falseticker verdict on a WAN server, when a local refclock is trusted,
    # says nothing about that server's quality: it records only that it
    # disagrees with the trusted refclock, which is exactly the disagreement we
    # need to see.  So such a result witnesses (`witness_only`) but is never
    # selectable — `_pick_active` reads `available` alone, so a falseticker can
    # never become the active tier.
    witness_only: bool = False


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
    #: The host-clock verdict (host_clock_integrity.HostClockVerdict.to_dict()
    #: plus ``since_utc``).  Separate from the tier decision on purpose: a
    #: system-clock witness disagreeing with a GPS-disciplined anchor says
    #: the CLOCK drifted, and until 2026-09-04 that sentence ended at
    #: ":advisory".  None when no probe ran this tick.
    host_clock: Optional[Dict[str, Any]] = None
    #: Why T3 is unavailable this tick, or None while it is available.
    #: Since 2026-10-22 the fusion probe reports T3 unavailable by ruling
    #: (review section 15, step 5), and a T3-only station falls to T2;
    #: this field is where authority.json says so.
    t3_unavailable_reason: Optional[str] = None


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
        governor_radiod_provider: Optional[Callable[[], Optional[str]]] = None,
        mdns_advertiser: Optional["MdnsFusionAdvertiser"] = None,
        snapshot_store: Optional["AuthoritySnapshotStore"] = None,
        frontend_probe: Optional["FrontendProbe"] = None,
        demote_t6_on_breach: bool = False,
        demote_t6_on_breach_min_cycles: int = 3,
        host_clock_rate_provider: Optional[Callable[[], Optional[float]]] = None,
        host_clock_fault_ms: float = HOST_CLOCK_DEFAULT_FAULT_MS,
        host_clock_rate_suspect_ppm: float = HOST_CLOCK_DEFAULT_RATE_SUSPECT_PPM,
        host_clock_alarm_repeat_sec: float = 3600.0,
        t3_shadow_record: Optional["T3ShadowRecord"] = None,
        judge_status_path: Path = Path("/run/hf-timestd/offset_judge.json"),
        registration_store: Optional["RegistrationStore"] = None,
        t3_admitted: bool = False,
    ):
        self.probes = list(probes)
        # Ruling B (Michael, 2026-10-30): ``[timing.t3] admitted``.  Until
        # it reads true, T3 publishes no authority: its probe result reads
        # unavailable with ``NOT_ADMITTED_REASON``, so a station without T6
        # falls through to T2, and the registration block claims no
        # source.  False by default: a caller that forgets it gets the
        # closed regime.
        self.t3_admitted = bool(t3_admitted)
        # Task 15: the per-minute T3 shadow row (stream ``minutes``).  None =
        # no rows and no station transition counter.  ``judge_status_path``
        # is the offset judge's publication, read once per minute row for its
        # ``t3_shadow`` block and the T3/T4/T5 shadow residuals.
        # ``registration_store`` is the store _write_state reads the T3
        # summary from, ONCE per tick: authority.json's registration block and
        # the row's detail fields (waiting, n_minutes, ...) both come from that
        # one read.  None builds the default store on first use.
        self.t3_shadow_record = t3_shadow_record
        self.judge_status_path = Path(judge_status_path)
        self._registration_store = registration_store
        self._t3_station_transitions = None
        if t3_shadow_record is not None:
            from hf_timestd.core.state_transition_counter import (
                StateTransitionCounter,
            )
            self._t3_station_transitions = StateTransitionCounter("station")
        self._t3_shadow_last_minute: Optional[int] = None
        # The summary _write_state read this tick, beside the block it built.
        self._published_summary: Optional[dict] = None
        # Row bookkeeping: ``seq`` counts this process's minute rows and
        # ``process_id`` names the process, so a reader can tell a repeated
        # minute (host-clock step back), a gap and a restart apart.  A random
        # id, not a clock read.
        self._t3_shadow_seq = 0
        self._t3_shadow_process_id = uuid.uuid4().hex[:12]
        # True while minute-row failures run in a streak: the first logs an
        # ERROR, the rest DEBUG, and a success ends the streak.
        self._t3_shadow_failing = False
        # Host-clock integrity (host_clock_integrity.py).  Three witnesses
        # feed one verdict: the cross-check's own sysclock-vs-rtp pair
        # numbers, the LB-1421 host-versus-GPS gap riding in T5's detail,
        # and the PPS-rate figure this provider returns (GpsdoProbe.
        # host_clock_rate_ppm when gpsdo-monitor runs).  None = no rate
        # witness.  The verdict never touches tier selection.
        self.host_clock_rate_provider = host_clock_rate_provider
        self.host_clock_fault_ms = float(host_clock_fault_ms)
        self.host_clock_rate_suspect_ppm = float(host_clock_rate_suspect_ppm)
        self._host_clock_alarm = HostClockAlarm(repeat_sec=host_clock_alarm_repeat_sec)
        # (|delta_ms|, bound_ms) per sysclock witness of an rtp-frame active
        # tier, gathered by _cross_check on each tick.
        self._host_clock_pairs: Dict[str, Tuple[float, float]] = {}
        self.output_path = Path(output_path)
        # NOT created here.  Construction must be side-effect free: building
        # a manager to inspect it (which probes registered, what a config
        # resolves to) should never require write access to a production
        # runtime directory.  The directory is created by the write, which
        # already degrades an OSError to a logged warning — a station that
        # cannot write its authority file should say so and keep running.
        self.a_level_provider = a_level_provider
        self.upgrade_hysteresis = upgrade_hysteresis
        self.pair_thresholds_ms = (
            pair_thresholds_ms if pair_thresholds_ms is not None
            else DEFAULT_PAIR_THRESHOLDS_MS
        )
        self.now_fn = now_fn
        self.governor_radiod_provider = governor_radiod_provider
        self.mdns_advertiser = mdns_advertiser
        # V1 fix layer 4 — long-term observability.  When configured,
        # every tick mirrors the published state + per-probe detail
        # into a local SQLite DB so the per-cycle history (otherwise
        # overwritten in authority.json) is queryable hours/days
        # later.  None = no archiving (legacy behaviour preserved).
        self.snapshot_store = snapshot_store
        # The registration block the last _write_state published, so the
        # snapshot row records the same counter_epoch_id authority.json did.
        self._published_registration: Optional[dict] = None
        # Receiver operating point.  radiod's RX888 AGC re-adjusts the
        # analog front-end gain once per second from TOTAL band power,
        # and 0.52 dB of the T6 pilot's C/N0 rides on every dB of it
        # (B4, 2026-08-28).  Recording it beside the measurement is what
        # makes a T6 residual attributable to it later.  None = not
        # configured, columns stay NULL.
        self.frontend_probe = frontend_probe
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
        # Phase 2B — consecutive ticks where T6 was the picked tier
        # AND drift_monitor.sustained_breach was True.  Drives the
        # demote-on-breach hysteresis; resets when the breach clears
        # or T6 stops being the picked tier.
        self._t6_consecutive_breach_ticks: int = 0
        # T6 anchor-authority state as of the last poll, forwarded by
        # BpskPpsProbe (spec §4 of the anchor-inversion design).  None
        # until a producer publishes it.
        self._t6_authority_state: Optional[str] = None
        self._t6_suspect_criteria: Optional[str] = None
        self._t6_battery_blocks: Optional[int] = None
        self._t6_hpps_publishing: Optional[bool] = None
        self._t6_hpps_publish_mode: Optional[str] = None
        self._t6_authority_violations: Optional[List[str]] = None
        # The T3 unavailability reason last announced, so the demotion
        # line goes out at start and on change, never once per tick.
        # The sentinel differs from every real value, None included.
        self._t3_reason_announced: object = _UNANNOUNCED

    def _close_unadmitted_t3(self, results: Dict[str, ProbeResult]) -> None:
        """Ruling B (2026-10-30): an unadmitted T3 holds no rung.

        Rewrites the T3 result in place, before hysteresis and selection,
        as unavailable with ``NOT_ADMITTED_REASON``.  The probe's own
        reason, and any offset it measured, stay in the result for the
        snapshot and the shadow row.  A no-op once admitted, and when no
        T3 probe is registered."""
        if self.t3_admitted:
            return
        if not any(getattr(p, "t_level", None) == "T3" for p in self.probes):
            return
        from hf_timestd.core.t3_admission import NOT_ADMITTED_REASON
        r = results.get("T3")
        if r is None:
            return
        detail = dict(r.detail or {})
        detail.setdefault("probe_available", bool(r.available))
        if r.reason is not None:
            detail.setdefault("probe_reason", r.reason)
        results["T3"] = dataclasses.replace(
            r, available=False, reason=NOT_ADMITTED_REASON, detail=detail)

    def _note_t3(self, results: Dict[str, ProbeResult]) -> Optional[str]:
        """Announce T3's state once, at start and on any change.

        Returns the reason T3 is unavailable this tick (None while it is
        available, or when no T3 probe is registered at all).  The fusion probe's reason is constant by design
        (fusion_status_probe.T3_CALIBRATED_REASON), so on a running
        station this logs one WARNING at start and nothing after.
        """
        if not any(getattr(p, "t_level", None) == "T3" for p in self.probes):
            # No T3 probe registered (a test rig, or a manager built for
            # one tier): nothing was demoted, so nothing to announce.
            return None
        r = results.get("T3")
        reason = None if (r is not None and r.available) else (
            (r.reason if r is not None else None) or "no T3 result")
        if reason != self._t3_reason_announced:
            self._t3_reason_announced = reason
            if reason is None:
                log.warning("T3 available again: the T3 rung may hold "
                            "authority")
            else:
                log.warning(
                    "T3 UNAVAILABLE: %s.  Authority falls through to the "
                    "next tier (T2 on a T3-only station).  This line "
                    "repeats only when the reason changes.", reason)
        return reason

    def tick(self) -> AuthorityState:
        """Run one authority-decision cycle: poll probes, update
        hysteresis, select active, cross-check, publish. Intended to be
        called on a fixed cadence (default 30 s) from a service thread,
        or directly from tests.
        """
        results = self._poll_all()
        self._close_unadmitted_t3(results)
        self._note_t6_authority(results.get("T6"))
        self._update_hysteresis(results)
        active = self._pick_active(results)
        # Phase 2B — demote T6→T5 when the drift monitor reports a
        # sustained breach for ``demote_t6_on_breach_min_cycles``
        # consecutive ticks.  No-op when the feature flag is off,
        # which is the default.  Flag accumulates here so the cross-
        # check downstream sees the post-demotion active.
        active, demote_flag = self._maybe_demote_breached_t6(active, results)
        active_pre_xcheck = active
        active, witnesses, flags = self._cross_check(active, results)
        if demote_flag is not None:
            flags = list(flags) + [demote_flag]
        # V7: append a chrony-feedback flag if chrony has rejected the
        # SHM segment we feed for the active tier.  Silent no-op when
        # chronyc is unavailable.
        feedback_flag = self._check_chrony_self_feedback(active)
        if feedback_flag is not None:
            flags = list(flags) + [feedback_flag]

        # METROLOGY.md §4.5: a single-witness disagreement raises a flag but
        # does NOT downgrade (deliberate — one noisy lower witness must not
        # demote a higher-precision tier; majority / asymmetric rules handle
        # genuine outliers). To keep that conservatism from silently shipping
        # an overconfident offset, when the active tier is *kept* despite an
        # unresolved disagreement we (a) widen the published uncertainty to
        # cover the discrepancy and (b) emit the canonical TIMING_DISAGREEMENT
        # alarm so consumers/watchdogs can react. A resolved downgrade
        # (active changed) needs neither — the adjudicated tier is trusted.
        inflate_ns = 0
        if active is not None and active == active_pre_xcheck:
            dis_ms = self._active_disagreement_ms(active, results, witnesses)
            if dis_ms > 0.0:
                inflate_ns = int(math.ceil(dis_ms * 1_000_000))
                flags = list(flags) + ["TIMING_DISAGREEMENT"]

        self._note_transition(active)
        host_clock = self._assess_host_clock(results)
        state = self._build_state(results, active, witnesses, flags, inflate_ns,
                                  host_clock=host_clock)
        state.t3_unavailable_reason = self._note_t3(results)
        self._write_state(state)
        self._write_snapshot(state, results)
        self._write_t3_shadow_minute(state, results)
        self._apply_mdns_advertiser(state)
        return state

    def _write_t3_shadow_minute(
        self,
        state: AuthorityState,
        results: Dict[str, ProbeResult],
    ) -> None:
        """Task 15: one ``minute`` row per UTC minute, plus station-level
        T3 transition rows, into the ``minutes`` shadow stream.

        The station counter watches the published registration state on
        EVERY tick, so a change and its reversal between two minute rows
        still count.  The row itself goes out on the first tick of each
        new minute: it reads the judge file once and appends once.

        The minute comes from ``now_fn``.  In production that is the host
        clock (``datetime.now``), and that is acceptable here only because
        the value is a record stamp: it names the row (``utc_minute``), its
        day file, and the transition counter's log window, and it enters no
        timing arithmetic.  Nothing computed from it reaches authority.json,
        the snapshot, chrony or any tier decision.  A host-clock step at
        worst repeats or skips one row; ``seq`` and ``process_id`` let a
        reader see it.

        Never raises: the record is diagnostic, and the tick has already
        published authority.json.  A failure streak logs one ERROR, so a bug
        in the row cannot silently cost the whole record.
        """
        if self.t3_shadow_record is None:
            return
        try:
            # Record stamp only -- see the docstring.
            utc_minute = int(self.now_fn().timestamp()) // 60 * 60
            reg = self._published_registration
            counter = self._t3_station_transitions
            if counter is not None and isinstance(reg, dict):
                # Ruling C (N-2): a summary written in a minute that saw a
                # radiod run change belongs to the restart that metrology
                # already announced, once, at WARNING.
                summary = self._published_summary or {}
                t_row = counter.observe(
                    str(reg.get("state")),
                    f"published registration, source {reg.get('source')}",
                    utc_minute,
                    quiet=summary.get("run_change") is True)
                if t_row is not None:
                    self.t3_shadow_record.append(t_row)
            if utc_minute == self._t3_shadow_last_minute:
                return
            self._t3_shadow_last_minute = utc_minute
            row = self._t3_shadow_minute_row(state, results, utc_minute)
            self.t3_shadow_record.append(row)
            self._t3_shadow_seq += 1
            self._t3_shadow_failing = False
        except Exception as exc:  # noqa: BLE001 -- diagnostic only
            if not self._t3_shadow_failing:
                log.error("T3 shadow minute row failed: %r; the minutes "
                          "record is incomplete until a row succeeds", exc)
            else:
                log.debug("T3 shadow minute row failed: %r", exc)
            self._t3_shadow_failing = True

    def _t3_shadow_minute_row(
        self,
        state: AuthorityState,
        results: Dict[str, ProbeResult],
        utc_minute: int,
    ) -> Dict[str, Any]:
        reg = self._published_registration if isinstance(
            self._published_registration, dict) else {}
        summary = self._published_summary or {}
        judge = self._read_judge_status() or {}
        t3_shadow = judge.get("t3_shadow")
        residuals = judge.get("shadow_residuals")
        if not isinstance(residuals, dict):
            residuals = {}

        def vs_adopted(tier: str) -> Optional[dict]:
            v = residuals.get(tier)
            return v if isinstance(v, dict) else None

        def offset(level: str) -> Optional[float]:
            r = results.get(level)
            v = None if r is None else r.offset_ms
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                return None
            # A NaN would fail the whole row (strict JSON); null keeps it.
            return float(v) if math.isfinite(v) else None

        def frame(level: str) -> Optional[str]:
            r = results.get(level)
            return None if r is None or r.offset_ms is None else r.frame

        def last_rx(level: str) -> Optional[float]:
            r = results.get(level)
            v = None if r is None else (r.detail or {}).get("last_rx_s")
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                return None
            return v if math.isfinite(v) else None

        counter = self._t3_station_transitions
        return {
            "kind": "minute",
            "utc_minute": utc_minute,
            "seq": self._t3_shadow_seq,
            "process_id": self._t3_shadow_process_id,
            # T3's registration as this tick published it (authority.json's
            # block), with the summary's detail fields beside it.
            "t3": {
                "state": reg.get("state"),
                "sigma_ms": reg.get("sigma_ms"),
                "contributing": reg.get("contributing"),
                "waiting": summary.get("waiting"),
                "dropped": summary.get("dropped"),
                "n_minutes": summary.get("n_minutes"),
                "verified": summary.get("verified"),
                "whole_second_unresolved": summary.get(
                    "whole_second_unresolved"),
                "rtp_domain": summary.get("rtp_domain"),
                "counter_epoch_id": reg.get("counter_epoch_id"),
                # The block's own age; with state STALE it marks detail
                # fields a dead writer left behind.
                "age_s": reg.get("age_s"),
            },
            "t3_shadow": t3_shadow if isinstance(t3_shadow, dict) else None,
            # The judge's shadow residuals, raw, from the same single read:
            # each bench's UTC estimate minus the adopted bench's, at one
            # instant (offset_judge._cross_bench_delta_ns).  T4 reaches the
            # sample plane only through this host link, so T3 against T4 is
            # t3_vs_adopted - t4_vs_adopted when both name one vs_tier.
            "t3_vs_adopted": vs_adopted("T3"),
            "t4_vs_adopted": vs_adopted("T4"),
            "t5_vs_adopted": vs_adopted("T5"),
            # All three offsets come from THIS tick's probe results, so T2
            # and T4 describe the same instant.  Sign conventions, raw:
            #   t6: BpskPpsProbe local_minus_source_ns / 1e6 -- the anchor's
            #       label of the PPS edge minus the GPS second it marks
            #       (reading minus truth; frame "rtp").
            #   t4, t2: ChronyTrackingProbe, chrony's last-sample offset,
            #       local clock minus source (chrony: "Positive offsets
            #       indicate that the local clock is ahead of the source").
            # So t2 = host - S2 and t4 = host - S4.  T2's error with T4 as
            # truth, reading minus truth like T6 and t3_minus_t6_ms, is
            # S2 - S4 = t4_offset_ms - t2_offset_ms.  ``offset_signs`` says
            # all of this in the row itself.
            "t6_offset_ms": offset("T6"),
            "t4_offset_ms": offset("T4"),
            "t2_offset_ms": offset("T2"),
            "offset_frames": {"t6": frame("T6"), "t4": frame("T4"),
                              "t2": frame("T2")},
            "offset_signs": T3_SHADOW_OFFSET_SIGNS,
            # Seconds since chrony last heard the source (chronyc LastRx):
            # a WAN sample can sit up to a poll interval old.
            "t4_last_rx_s": last_rx("T4"),
            "t2_last_rx_s": last_rx("T2"),
            "host_clock_verdict": (state.host_clock or {}).get("verdict"),
            "t3_transitions_total": (counter.count
                                     if counter is not None else None),
        }

    def _registration_store_or_default(self) -> "RegistrationStore":
        if self._registration_store is None:
            from hf_timestd.core.registration_store import (
                RegistrationStore as _RegistrationStore,
            )
            self._registration_store = _RegistrationStore()
        return self._registration_store

    def _read_judge_status(self) -> Optional[dict]:
        """offset_judge.json, read once.  None when missing or malformed."""
        try:
            d = json.loads(self.judge_status_path.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else None
        except Exception as exc:  # noqa: BLE001
            log.debug("T3 shadow: judge status unreadable: %r", exc)
            return None

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
        self._host_clock_pairs = {}
        if active is None:
            return None, [], []

        active_result = results[active]
        witnesses: List[str] = []
        disagreement_flags: List[str] = []

        for lvl in T_LEVELS_RANKED:
            if lvl == active:
                continue
            r = results[lvl]
            # A witness needs a MEASUREMENT, not chrony's vote — see
            # ProbeResult.witness_only.
            if (r.available or r.witness_only) and r.offset_ms is not None:
                witnesses.append(lvl)
                # A sysclock-frame witness against an rtp-frame active tier
                # measures the HOST CLOCK's error, whatever it says about
                # the anchor.  Keep the number for the host-clock verdict
                # before the advisory tag below files it away.  Taken here,
                # against the pre-demotion active, so AC0G-ND's
                # "T3<->T2:4179ms ... demoted-to:none" still counts.
                if (getattr(active_result, "frame", "sysclock") == "rtp"
                        and getattr(r, "frame", "sysclock") == "sysclock"
                        and active_result.offset_ms is not None):
                    self._host_clock_pairs[lvl] = (
                        abs(active_result.offset_ms - r.offset_ms),
                        self._pair_threshold_ms(active, active_result, lvl, r),
                    )
                flag = self._check_pair(active, active_result, lvl, r)
                if flag:
                    # Cross-frame disagreement against a GPS-disciplined
                    # rtp-active tier is advisory: surfaced for operators
                    # but it does not widen sigma or drive a downgrade
                    # (it reflects system-clock drift, not an error in the
                    # published anchor offset). See
                    # _witness_drives_consequences / METROLOGY §4.5.
                    if not self._witness_drives_consequences(
                        active, active_result, r
                    ):
                        flag = flag + ":advisory"
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
                if results["T2"].available:
                    active = "T2"
                else:
                    # T2 could only WITNESS (chrony marked it a falseticker
                    # because a trusted refclock outvotes it).  Selecting a
                    # source chrony will not steer to would be worse than
                    # holding no authority, so fall to the best tier that is
                    # genuinely available below T3 — else none at all, which is
                    # the honest state and lets chrony age the stale Fusion
                    # sample out and fall back to the pool on its own.
                    below = T_LEVELS_RANKED[T_LEVELS_RANKED.index("T3") + 1:]
                    active = next(
                        (l for l in below if results[l].available), None)
                    disagreement_flags.append(
                        f"asymmetric-T3-T2:witness-only-T2:demoted-to:{active or 'none'}")

        return active, witnesses, disagreement_flags

    def _check_chrony_self_feedback(
        self,
        active: Optional[str],
        conf_paths: Optional[Sequence[Path]] = None,
    ) -> Optional[str]:
        """V7 — verify chrony's verdict on the SHM segment we feed for the
        active tier.  If chrony has rejected our source (state ``#x``
        falseticker, ``#?`` unselectable, etc.) while authority is
        claiming the tier as active, return a disagreement flag.

        §7.1.1 ships every refclock (HPPS; FUSE until it retired on
        2026-10-22) `noselect`, so chrony
        never selects them and reports their state as `?` permanently;
        that is a configuration fact, not chrony's verdict on our
        source, so a `noselect` refid is exempt from this check except
        for the "missing from chronyc sources entirely" case, which
        still means chrony isn't consuming the segment at all.

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

        if conf_paths is None:
            conf_paths = _default_chrony_conf_paths()
        noselect = _refid_carries_noselect(refid, conf_paths)

        for line in (proc.stdout or "").splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 3:
                continue
            mode, state, name = parts[0], parts[1], parts[2]
            if mode != "#":
                continue  # refclocks only; "^" rows are NTP peers
            if name.upper() != refid.upper():
                continue
            if noselect:
                # chrony never selects this source by our own
                # configuration; its state is uninformative.
                return None
            if state in CHRONY_HEALTHY_STATES:
                return None
            return f"chrony-rejected-{refid}:state={state}"

        # Refid not present in chrony's source list at all — chrony either
        # isn't configured to consume our SHM segment, or the segment
        # hasn't seen its first sample yet.  Surface as a distinct flag,
        # even when the refid is noselect: chrony not consuming the
        # segment stays worth saying regardless.
        return f"chrony-missing-{refid}"

    def _check_pair(
        self, a: str, a_res: ProbeResult, b: str, b_res: ProbeResult,
    ) -> Optional[str]:
        """Return a disagreement flag string if |Δ| exceeds the combined
        CI (floored at the per-pair threshold), else None. If either side
        lacks a measured offset, returns None — cross-check needs a
        measured offset on both sides.

        Frame note: the offsets being differenced are not all in one
        reference frame (see ProbeResult.frame). T6, T5-direct and T3
        (Fusion) are rtp-frame — anchor-vs-truth residuals measured in the
        RTP stream, system-clock-independent. Chrony T5/T4/T2 are
        sysclock-frame (local_clock − source). _check_pair just computes
        the raw |Δ| and threshold for ALL pairs (so disagreements are
        always surfaced); whether a given pair's disagreement is allowed
        to widen sigma / drive a downgrade is decided separately by
        _witness_drives_consequences (P7): a sysclock witness against a
        GPS-disciplined rtp-active tier (T6/T5) is advisory-only, because
        the difference then reflects system-clock drift, not an error in
        the published anchor offset. See METROLOGY.md §4.5."""
        if a_res.offset_ms is None or b_res.offset_ms is None:
            return None
        diff = abs(a_res.offset_ms - b_res.offset_ms)
        threshold = self._pair_threshold_ms(a, a_res, b, b_res)
        if diff > threshold:
            return f"{a}<->{b}:{diff:.3f}ms>{threshold:.3f}ms"
        return None

    def _pair_threshold_ms(
        self, a: str, a_res: ProbeResult, b: str, b_res: ProbeResult,
    ) -> float:
        """The combined 3-sigma CI of a pair, floored at its configured
        per-pair threshold — the bound _check_pair judges |delta| against,
        exposed so the host-clock verdict can cite the same number."""
        sa = a_res.sigma_ms if a_res.sigma_ms is not None else TRUST_SIGMA_MS.get(a, 1.0)
        sb = b_res.sigma_ms if b_res.sigma_ms is not None else TRUST_SIGMA_MS.get(b, 1.0)
        rss = 3.0 * (sa * sa + sb * sb) ** 0.5
        floor = self.pair_thresholds_ms.get(frozenset({a, b}), 0.0)
        return max(rss, floor)

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
            and self._witness_drives_consequences(active, a_res, results[w])
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

    def _witness_drives_consequences(
        self, active: str, active_res: ProbeResult, w_res: ProbeResult,
    ) -> bool:
        """Whether witness ``w`` may widen the published sigma or drive a
        downgrade of ``active`` (vs. being advisory-only).

        A GPS-disciplined, rtp-frame active tier (T6/T5) is shielded from
        sysclock-frame witnesses: when the system clock drifts away from
        the GPS-disciplined anchor (e.g. SHM discipline lost), a chrony
        witness disagrees by the drift amount even though the published
        anchor offset is fine — that must not inflate its sigma or demote
        it. Same-frame (rtp) witnesses still fully cross-check it, and
        every other active tier keeps full cross-check force (so e.g.
        Fusion is still sanity-checked by chrony). See METROLOGY §4.5."""
        if (active in GPS_DISCIPLINED_RTP_TIERS
                and getattr(active_res, "frame", "sysclock") == "rtp"
                and getattr(w_res, "frame", "sysclock") != "rtp"):
            return False
        return True

    def _active_disagreement_ms(
        self, active: str, results: Dict[str, ProbeResult], witnesses: List[str],
    ) -> float:
        """Largest |Δ| (ms) between the active tier and any witness that
        disagrees with it past the cross-check threshold AND is allowed to
        drive consequences (same-frame, or active not GPS-disciplined-rtp).
        0.0 when the active tier has no measured offset or no qualifying
        witness disagrees. Used to widen the published uncertainty on a
        kept-but-contested offset (§4.5)."""
        a_res = results[active]
        if a_res.offset_ms is None:
            return 0.0
        worst = 0.0
        for w in witnesses:
            r = results[w]
            if r.offset_ms is None:
                continue
            if self._check_pair(active, a_res, w, r) is None:
                continue
            if not self._witness_drives_consequences(active, a_res, r):
                continue
            worst = max(worst, abs(a_res.offset_ms - r.offset_ms))
        return worst

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
        inflate_ns: int = 0,
        host_clock: Optional[Dict[str, Any]] = None,
    ) -> AuthorityState:
        available = [lvl for lvl in T_LEVELS_RANKED if results[lvl].available]

        offset_ns: Optional[int] = None
        sigma_ns: Optional[int] = None
        stations: List[str] = []

        if active in ("T3", "T6"):
            # Contract: a T3 probe's offset_ms is ALREADY rtp_to_utc
            # (UTC minus host), converted from d_clock with
            # rtp_to_utc_offset_ms_from_d_clock.  Published as it stands.
            a_res = results[active]
            # T6 with a captured native anchor publishes the anchor-
            # derived ``rtp_to_utc_offset_ns`` directly — that's the
            # substrate-honest offset, bridging ka9q's host-clock
            # rtp_to_wallclock to the native anchor without the per-
            # edge MF jitter that ``offset_ms`` (= local_minus_source_ns)
            # carries.  When present, prefer it.  Falls back to the
            # offset_ms path when the anchor isn't captured yet (cold
            # start) or on T3.  See
            # ``CoreRecorderV2._compute_rtp_to_utc_offset_ns`` and
            # ``docs/TIMING-PIPELINE-WIRING.md`` §4 / §5.4.
            anchor_offset_ns = (
                a_res.detail.get("rtp_to_utc_offset_ns")
                if a_res.detail else None
            )
            if active == "T6" and isinstance(anchor_offset_ns, int):
                offset_ns = anchor_offset_ns
            elif a_res.offset_ms is not None:
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

        # §4.5 hardening: when the active tier was kept despite an unresolved
        # cross-check disagreement, widen the published uncertainty to cover
        # the discrepancy so consumers don't trust a contested offset at full
        # precision. Only widens (never narrows) and never touches the offset
        # itself or the tier selection.
        if offset_ns is not None and inflate_ns > (sigma_ns or 0):
            sigma_ns = inflate_ns

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
            host_clock=host_clock,
        )

    def _assess_host_clock(self, results: Dict[str, ProbeResult]) -> Dict[str, Any]:
        """One verdict on the host clock from the witnesses this tick carried,
        and the log line it earns.  See host_clock_integrity.py for why.

        Never raises: a rate provider that fails counts as no rate witness.
        Never touches tier selection.
        """
        gps_delta: Optional[float] = None
        t5 = results.get("T5")
        if t5 is not None and t5.detail:
            raw = t5.detail.get("host_minus_gps_s")
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                gps_delta = float(raw)

        rate_ppm: Optional[float] = None
        if self.host_clock_rate_provider is not None:
            try:
                raw = self.host_clock_rate_provider()
            except Exception as exc:
                log.debug("host_clock_rate_provider raised: %s", exc)
                raw = None
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                rate_ppm = float(raw)

        verdict = assess_host_clock(
            pair_disagreements=self._host_clock_pairs,
            gps_second_delta_s=gps_delta,
            rate_ppm=rate_ppm,
            fault_ms=self.host_clock_fault_ms,
            rate_suspect_ppm=self.host_clock_rate_suspect_ppm,
        )
        now = self.now_fn()
        event = self._host_clock_alarm.update(verdict, now=now.timestamp())
        since = self._host_clock_alarm.since
        since_utc = (
            _iso_z(datetime.fromtimestamp(since, tz=timezone.utc))
            if since is not None else None
        )
        if event in ("enter", "repeat"):
            log.critical(
                "HOST CLOCK %s: %s — since %s. The tier decision stands "
                "(the anchor is judged separately); this is the HOST clock, "
                "which every sysclock-frame label and every consumer on this "
                "station inherits. Witnesses: %s",
                verdict.verdict.upper(), verdict.reason, since_utc,
                ", ".join(
                    f"{w.name}={w.value:+.3f}{'ms' if w.kind == 'pair_ms' else 's' if w.kind == 'gps_second_s' else 'ppm'}"
                    for w in verdict.witnesses),
            )
        elif event == "clear":
            log.info("HOST CLOCK cleared: %s", verdict.reason)

        out = verdict.to_dict()
        out["since_utc"] = since_utc
        return out

    def _note_t6_authority(self, r: Optional[ProbeResult]) -> None:
        """Latch the T6 anchor-authority state from this tick's probe
        result so ``_write_state`` can publish it.

        Coverage limit, stated honestly: ``BpskPpsProbe.poll`` returns
        early — with an empty ``detail`` — whenever the status file is
        missing/stale, ``t6_pps`` is disabled, the MF is not locked, or
        ``local_minus_source_ns`` is absent.  Those early returns carry
        no authority state, so ``authority.json`` reflects only the
        states observable while the probe is otherwise healthy: in
        practice DEGRADED (and ACQUIRING/AUTHORITATIVE) *while the MF
        stays locked* — which is exactly the anchor-inversion failure
        mode that was previously invisible.  An MF-unlock UNLOCKED
        transition drops ``locked`` to false, so it shows up as the
        probe going unavailable rather than as a published
        ``t6_authority_state``; that transition remains visible in the
        recorder's status JSON and in the journal (every transition is
        logged at WARNING by ``_t6_apply_authority_decision``)."""
        d = (r.detail or {}) if r is not None else {}
        state = d.get("authority_state")
        self._t6_authority_state = state if isinstance(state, str) else None
        viol = d.get("authority_violations")
        self._t6_authority_violations = (
            [str(v) for v in viol] if isinstance(viol, list) else None
        )
        # Whether the producer believes it is feeding HPPS.  Published
        # so hpps-watchdog can distinguish a wedged push gate (believes
        # it is pushing, chrony sees nothing) from an honest withdrawal,
        # and stop restarting the recorder into the second case -- a
        # restart destroys the anchor a coast rests on.
        pub = d.get("hpps_publishing")
        self._t6_hpps_publishing = pub if isinstance(pub, bool) else None
        mode = d.get("hpps_publish_mode")
        self._t6_hpps_publish_mode = mode if isinstance(mode, str) else None
        # Spec §5.1: a failed self-consistency guardrail on a RUNNING T6
        # marks the assertion suspect and alarms; it never demotes.  The
        # mark reached the authority_snapshot record but not this file,
        # so an operator had no queryable state at all -- only a WARNING
        # in the journal -- and "the operator decides" cannot mean
        # "the operator greps".  Measured on AC0G-B4 2026-09-19: the
        # columns were absent from authority.json and from sqlite alike.
        sus = d.get("suspect_criteria")
        self._t6_suspect_criteria = sus if isinstance(sus, str) else None
        blocks = d.get("battery_blocks")
        self._t6_battery_blocks = (
            int(blocks) if isinstance(blocks, (int, float)) else None)

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

        # Additive v1 extension: the T6 anchor-authority state
        # (ACQUIRING / AUTHORITATIVE / DEGRADED / UNLOCKED) and its
        # named invariant violations, per §4 of the anchor-inversion
        # design — consumers of authority.json can see the T6 anchor
        # degrade without opening the recorder's status file.  Omitted
        # entirely when the producer publishes no state, so legacy
        # output is byte-compatible.
        if self._t6_authority_state is not None:
            payload["t6_authority_state"] = self._t6_authority_state
            if self._t6_authority_violations is not None:
                payload["t6_authority_violations"] = (
                    self._t6_authority_violations)
        if getattr(self, "_t6_hpps_publishing", None) is not None:
            payload["t6_hpps_publishing"] = self._t6_hpps_publishing
            if getattr(self, "_t6_hpps_publish_mode", None) is not None:
                payload["t6_hpps_publish_mode"] = self._t6_hpps_publish_mode
        # The self-consistency battery's verdict on a running T6.  Empty
        # string means healthy; a comma-joined criterion list means the
        # tier is still asserting but has marked itself suspect.  Omitted
        # entirely when the producer publishes neither, so legacy output
        # stays byte-compatible.
        if getattr(self, "_t6_suspect_criteria", None) is not None:
            payload["t6_suspect_criteria"] = self._t6_suspect_criteria
        if getattr(self, "_t6_battery_blocks", None) is not None:
            payload["t6_battery_blocks"] = self._t6_battery_blocks

        # Additive v1 extension: the host-clock verdict.  Present on every
        # normal tick (verdict "unwitnessed" when nothing reported) so a
        # consumer can tell "no witness" from "no such field".  Omitted on
        # any path where no probe ran.
        if state.host_clock is not None:
            payload["host_clock"] = state.host_clock

        # Additive v1 extension: why T3 is unavailable (review section 15,
        # step 5).  Since 2026-10-22 the fused D_clock may not hold the T3
        # rung, so a T3-only station runs on T2 and this field says why.
        # Omitted while T3 is available.
        if getattr(state, "t3_unavailable_reason", None) is not None:
            payload["t3_unavailable_reason"] = state.t3_unavailable_reason

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

        # Additive v1 extension: the T3 registration's provenance (spec §7).
        # Computed once per publish; a RegistrationStore failure (missing
        # file, unreadable JSON, unwritable /run) must never block
        # authority.json itself, so it's caught and the block just falls
        # back to its no-summary shape.
        # One summary read per tick feeds both this block and the T3 shadow
        # row's detail fields, so the two never describe different files.
        self._published_registration = None
        self._published_summary = None
        try:
            store = self._registration_store_or_default()
            summary = store.read_summary()
            payload["registration"] = registration_block(
                store, summary=summary, admitted=self.t3_admitted)
            self._published_registration = payload["registration"]
            self._published_summary = summary if isinstance(summary, dict) else None
        except Exception as e:
            log.debug("registration_block raised: %s", e)

        try:
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
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

        ``results`` is ``None`` when no probe ran
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
        # The host-clock verdict as four flat columns (hamsci-dsp
        # authority_snapshot_store, 2026-09-04): the verdict, when the
        # episode began, and the two epoch witnesses.  On 2026-09-04 B4 ran
        # 11.6 s slow for thirteen hours with nothing in the table saying so.
        hc = state.host_clock or {}
        witnesses = hc.get("witnesses") or {}
        snapshot["host_clock_verdict"] = hc.get("verdict")
        snapshot["host_clock_since_utc"] = hc.get("since_utc")
        snapshot["host_clock_t2_ms"] = (witnesses.get("T2") or {}).get("value")
        snapshot["host_clock_lb1421_s"] = (witnesses.get("lb1421") or {}).get("value")
        # The radiod counter epoch this tick's registration names; a change
        # between rows is a radiod re-base (v3.73 row 14).
        snapshot["counter_epoch_id"] = (self._published_registration or {}).get(
            "counter_epoch_id")

        if self.governor_radiod_provider is not None:
            try:
                governor = self.governor_radiod_provider()
                if governor:
                    snapshot["governor_radiod"] = str(governor)
            except Exception:
                pass

        if self.frontend_probe is not None:
            try:
                snapshot.update(self.frontend_probe.sample())
            except Exception:
                # The probe's own contract is best-effort, but timing
                # authority outranks its provenance either way.
                pass

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
    snapshot["t6_fold_blocks_discarded"] = d.get("fold_blocks_discarded")
    snapshot["t6_fold_seconds"] = d.get("fold_seconds")
    # Spec §8's "held on folded estimates alone" evidence.  NULL on
    # producers that do not publish them — an absent field must not be
    # recorded as a verified check.
    snapshot["t6_fine_search_mode"] = d.get("fine_search_mode")
    _unverified = d.get("fine_coarse_unverified")
    snapshot["t6_fine_coarse_unverified"] = (
        None if _unverified is None else (1 if _unverified else 0)
    )
    # Spec §5.1: a failed self-consistency guardrail on a RUNNING T6
    # marks the assertion suspect rather than demoting it.  Comma-joined
    # failing criteria, empty (never None) when healthy or when the
    # producer predates this field, so a missing column reads the same
    # as a clean bill of health rather than as unknown.
    snapshot["t6_suspect_criteria"] = d.get("suspect_criteria") or ""
    snapshot["t6_battery_blocks"] = int(d.get("battery_blocks") or 0)
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
    snapshot["t5_anchor_age_sec"] = d.get("anchor_age_sec")


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


_UNANNOUNCED = object()
_UNREAD = object()

# The sign of every offset in a T3 shadow minute row, carried in each row so
# the analysis never has to guess.  "Reading minus truth" throughout.
T3_SHADOW_OFFSET_SIGNS: Dict[str, str] = {
    "t6_offset_ms": "T6 label of the PPS edge minus the GPS second",
    "t4_offset_ms": "host minus LAN GPS source (chrony local - source)",
    "t2_offset_ms": "host minus WAN source (chrony local - source)",
    "t2_error": "T2 error with T4 as truth = t4_offset_ms - t2_offset_ms",
    "t3_minus_t6_ms": "T3 label minus T6 label",
    "t3_vs_adopted": "T3 UTC estimate minus adopted bench's, ns",
    "t4_vs_adopted": "T4 UTC estimate minus adopted bench's, ns",
    "t5_vs_adopted": "T5 UTC estimate minus adopted bench's, ns",
    "t3_minus_t4": ("t3_vs_adopted.shadow_residual_ns - "
                    "t4_vs_adopted.shadow_residual_ns, same vs_tier"),
}


def _iso_z(dt: datetime) -> str:
    return dt.isoformat(timespec="microseconds").replace("+00:00", "Z")


def registration_block(
    store: Optional["RegistrationStore"] = None,
    summary: Any = _UNREAD,
    *,
    admitted: bool = False,
) -> dict:
    """The T3 origin's provenance for authority.json (spec §7).

    ACQUIRED names the tick train itself as the source
    (``source="hf_acquired"``): the second boundary comes from the
    station's own fold+template estimate, held in the RTP frame.
    Every other state — including WITNESS, where T6's plane is the one
    in force and the acquired plane only corroborates it — reports
    ``source="label"``, because a labelled boundary (or T6's) still
    governs the published offset.  WITNESS additionally carries
    ``residual_vs_t6_ms`` when the summary has it, so a station can be
    seen agreeing with T6 without its plane replacing T6's.

    ``read_summary()`` returns the last summary file it finds exactly as
    written, however old — it has no staleness check of its own (unlike
    ``read_siblings``, which does).  A dead metrology writer would
    otherwise leave this block reporting the writer's last live state
    (``source="hf_acquired"`` included) forever.  So this function applies
    the store's own window itself: once ``summary_age_s`` exceeds
    ``store.stale_s`` the block reports ``state="STALE"`` and
    ``source="label"``, while still carrying the last summary's
    ``counter_epoch_id``/``sigma_ms``/``contributing``/``stations`` so a
    reader can see what died and when (review F1/F2, task 10).  Every
    returned block, in every state, carries ``age_s`` — ``None`` only when
    there is no summary at all.

    ``admitted`` (Ruling B, 2026-10-30): ``[timing.t3] admitted``.  Until it
    reads true, an ACQUIRED plane governs nothing, so the block keeps
    ``source="label"`` and adds ``"admission": NOT_ADMITTED_REASON``.  The
    state and the detail fields stay as published, for the record.
    """
    from .registration_store import RegistrationStore as _RegistrationStore

    st = store or _RegistrationStore()
    # ``summary`` lets a caller that already read the file pass it in, so
    # one read serves several consumers (the T3 shadow row).
    s = st.read_summary() if summary is _UNREAD else summary
    if not s:
        return {
            "source": "label",
            "state": "UNKNOWN",
            "sigma_ms": None,
            "counter_epoch_id": None,
            "raw_pair_residual_ms": None,
            "contributing": [],
            "stations": [],
            "age_s": None,
        }
    age_s = st.summary_age_s(s)
    state = s.get("state", "UNKNOWN")
    stale = age_s > st.stale_s
    acquired = state == "ACQUIRED" and s.get("utc_ref") is not None and not stale
    withheld = acquired and not admitted
    block = {
        "source": "hf_acquired" if acquired and not withheld else "label",
        "state": "STALE" if stale else state,
        "sigma_ms": s.get("sigma_ms"),
        "counter_epoch_id": s.get("counter_epoch_id"),
        "raw_pair_residual_ms": s.get("raw_pair_residual_ms"),
        "contributing": list(s.get("contributing", [])),
        "stations": list(s.get("stations", [])),
        "age_s": age_s,
    }
    if state == "WITNESS" and not stale and "residual_vs_t6_ms" in s:
        block["residual_vs_t6_ms"] = s.get("residual_vs_t6_ms")
    if withheld:
        from hf_timestd.core.t3_admission import NOT_ADMITTED_REASON
        block["admission"] = NOT_ADMITTED_REASON
    return block
