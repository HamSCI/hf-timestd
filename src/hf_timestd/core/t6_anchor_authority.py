"""T6 anchor authority — owns "is T6 the RTP→UTC anchor authority now".

Spec: docs/design/T6_ANCHOR_INVERSION_DESIGN.md §2, §4, §6.  When
AUTHORITATIVE, the anchor is built by the inversion:

    anchor_utc_ns = named_integer_second·1e9 + delay_budget_ns − subsample

The coarse cascade only NAMES the integer second (±0.5 s duty); its
noise cannot enter the sub-second value by construction.  All state
transitions are returned to the caller for loud logging — this module
never logs silently-consequential decisions itself, and it never
consults a wall clock (the injected ``now`` is monotonic and measures
only DEGRADED dwell, estimate staleness and log throttling).

Two entry points drive it: ``on_fine_estimate`` (edge-triggered, once
per fold block) and ``on_tick`` (level-triggered, every batch), the
latter existing so that the *absence* of estimates is itself detected
rather than freezing the last anchor forever.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, Optional

from hf_timestd.core.bpsk_edge_fine_stage import FineEdgeEstimate
from hf_timestd.core.native_anchor import NativeAnchor

logger = logging.getLogger(__name__)

_WRAP32 = 1 << 32


def _wrapped_signed32(delta: int) -> int:
    """Map a mod-2^32 RTP difference to signed [-2^31, 2^31)."""
    d = delta & 0xFFFFFFFF
    return d - _WRAP32 if d >= (1 << 31) else d


_BILLION = 1_000_000_000
DELAY_BUDGET_BOUND_NS = 1_000_000  # ±1 ms hard physical bound (spec §5)
# radiod's channel-filter group delay is a DIFFERENT physical term from
# the TS-1 modulator path, and a much larger one: the coarse cascade
# already notes it is "the dominant contribution (up to ~150 ms at
# narrow filter widths)".  Spec §5 folded it into the ±1 ms budget on
# the belief it was sub-millisecond, and the template carried it as
# "0 pending fleet characterisation"; AC0G-B4 measured 16.618 ms
# against T4 on 2026-08-15 (n=90 over 15 min).  It gets its own name
# and its own bound so the ±1 ms budget keeps guarding what it was
# built to guard — absorbed timestamp error.
FILTER_GROUP_DELAY_BOUND_NS = 250_000_000  # ±250 ms, as the coarse path
# Estimates arrive once per fold block; three missed blocks is an
# unambiguous stall, not jitter.
ESTIMATE_STALE_INTERVALS = 3.0
# Throttle for the "still ACQUIRING" visibility warning.
ACQUIRING_WARN_PERIOD_SEC = 300.0


class T6AuthorityState(str, Enum):
    ACQUIRING = "ACQUIRING"
    AUTHORITATIVE = "AUTHORITATIVE"
    DEGRADED = "DEGRADED"
    UNLOCKED = "UNLOCKED"


@dataclass(frozen=True)
class T6AnchorDecision:
    state: T6AuthorityState
    previous_state: T6AuthorityState
    anchor: Optional[NativeAnchor]
    violations: tuple


class T6AnchorAuthority:
    def __init__(
        self,
        sample_rate_hz: int,
        delay_budget_ns: int,
        filter_group_delay_ns: int = 0,
        edge_period_tolerance_ns: int = 5_000,
        fine_coarse_max_ms: float = 5.0,
        degraded_unlock_after_sec: float = 600.0,
        fine_fold_seconds: float = 30.0,
        now: Callable[[], float] = time.monotonic,
    ):
        if abs(int(delay_budget_ns)) > DELAY_BUDGET_BOUND_NS:
            raise ValueError(
                f"delay_budget_ns={delay_budget_ns} exceeds the ±1 ms "
                f"physical bound: the analog TS-1→ADC path plus channel-"
                f"filter group delay is microseconds to sub-millisecond "
                f"(docs/design/T6_ANCHOR_INVERSION_DESIGN.md §5); a "
                f"larger value is absorbing timestamp error, not "
                f"measuring a chain delay."
            )
        if abs(int(filter_group_delay_ns)) > FILTER_GROUP_DELAY_BOUND_NS:
            raise ValueError(
                f"filter_group_delay_ns={filter_group_delay_ns} exceeds "
                f"the ±250 ms physical bound: radiod's channel-filter "
                f"group delay reaches ~150 ms only at the narrowest "
                f"widths, so a larger value is absorbing timestamp "
                f"error, not measuring a filter."
            )
        self.sample_rate_hz = int(sample_rate_hz)
        self.delay_budget_ns = int(delay_budget_ns)
        self.filter_group_delay_ns = int(filter_group_delay_ns)
        self.edge_period_tolerance_ns = int(edge_period_tolerance_ns)
        self.fine_coarse_max_ms = float(fine_coarse_max_ms)
        self.degraded_unlock_after_sec = float(degraded_unlock_after_sec)
        self.fine_fold_seconds = float(fine_fold_seconds)
        self._now = now
        self._state = T6AuthorityState.ACQUIRING
        self._anchor: Optional[NativeAnchor] = None
        # What the last invariant check MEASURED (see ``_check``).
        # Public and read-only to callers; empty until the first check.
        self.last_check_metrics: dict = {}
        # (edge_rtp, edge_subsample) of the last estimate used as the
        # periodicity reference — kept in raw counter form, NOT as a
        # mod-SR phase, so the check survives the 32-bit RTP wrap.
        self._prev_edge: Optional[tuple] = None
        self._degraded_since: Optional[float] = None
        # Liveness (spec §6, expose-don't-stall): when the estimate
        # stream dies while the MF stays locked, nothing edge-triggered
        # would ever fire again and the anchor would freeze silently.
        self._last_estimate_at: Optional[float] = None
        # `_last_estimate_at` moves only when an estimate is ACCEPTED, so
        # `estimate_stale` cannot distinguish "the fine stage went quiet" from
        # "every estimate was refused".  Those are opposite faults with
        # opposite fixes, and on 2026-08-31 the ambiguity cost B4 6h23m of T6
        # that only a restart cleared.  Track what ARRIVED, and why it was
        # turned away, so the next occurrence reports itself.
        self._last_estimate_seen_at: Optional[float] = None
        self._estimates_seen: int = 0
        self._rejected_since_accept: int = 0
        self._rejection_reasons: Dict[str, int] = {}
        self._last_acquiring_warn_at: Optional[float] = None

    @property
    def state(self) -> T6AuthorityState:
        return self._state

    def _wrapped_distance_samples(self, a: float, b: float) -> float:
        p = self.sample_rate_hz
        return abs((a - b + p / 2) % p - p / 2)

    def _period_deviation_samples(self, est: FineEdgeEstimate, prev: tuple) -> float:
        """How far this edge sits from an exact whole number of seconds
        after the previous one, in samples, wrapped to ±SR/2.

        Computed from the **signed 32-bit RTP delta**, not from two
        mod-SR phases.  ``2**32 % 96000 == 23296``, so at every RTP
        counter wrap (~12.4 h at 96 kHz) the mod-SR phase jumps 23 296
        samples — 242.7 ms — with the physical edge completely
        unmoved.  Comparing phases therefore false-fired ``edge_period``
        at every wrap, and because the stored reference stuck while
        violating, the authority sat DEGRADED for the full 600 s dwell
        and then UNLOCKED, dropping a perfectly good anchor and
        thrashing the cascade, once per wrap.  The counter delta wraps
        the same way the counter does, so the deviation stays correct
        for any inter-estimate gap under ~6 h (half the wrap period).
        """
        prev_rtp, prev_sub = prev
        p = self.sample_rate_hz
        d = float(_wrapped_signed32(int(est.edge_rtp) - int(prev_rtp)))
        d += float(est.edge_subsample) - float(prev_sub)
        return abs((d + p / 2) % p - p / 2)

    def edge_phase_rtp(self, est: FineEdgeEstimate) -> float:
        """Edge position within the second in the **RTP domain**.

        ``est.edge_offset_samples`` is in the fine stage's *fold*
        domain (samples since that stage's own start, mod the sample
        rate) and is offset from the RTP domain by an arbitrary
        per-block registration — see the domain note in
        ``bpsk_edge_fine_stage``.  Every quantity this class compares
        against (the MF coarse offset, and the previous estimate,
        which may have been produced under a different registration)
        is RTP-domain, so the estimate is converted here from the
        fields that *are* RTP-domain: ``edge_rtp + edge_subsample``.
        """
        return (int(est.edge_rtp) + float(est.edge_subsample)) % self.sample_rate_hz

    def note_estimate_seen(self, violations: tuple) -> None:
        """Record that an estimate ARRIVED, and how it was judged.

        Called for every fine estimate regardless of verdict — that is the
        point.  An accepted estimate clears the rejection run; a refused one
        adds its reasons to it.
        """
        self._last_estimate_seen_at = self._now()
        self._estimates_seen += 1
        if not violations:
            self._rejected_since_accept = 0
            self._rejection_reasons = {}
            return
        self._rejected_since_accept += 1
        for name in violations:
            self._rejection_reasons[name] = (
                self._rejection_reasons.get(name, 0) + 1)

    def stale_diagnosis(self) -> Dict[str, object]:
        """Why is there no accepted estimate?

        ``verdict`` is the part that matters:
          ``absent``   — nothing arrived; look at the fine stage.
          ``rejected`` — estimates arrived and every one was refused; look at
                         the invariant named in ``rejection_reasons``.  The
                         fine stage will NOT self-correct here, because from
                         its side the blocks are succeeding.
        """
        now = self._now()
        since_seen = (None if self._last_estimate_seen_at is None
                      else now - self._last_estimate_seen_at)
        arriving = (self._last_estimate_seen_at is not None
                    and self._rejected_since_accept > 0)
        return {
            "verdict": "rejected" if arriving else "absent",
            "estimates_arriving": arriving,
            "estimates_seen": self._estimates_seen,
            "rejected_since_accept": self._rejected_since_accept,
            "rejection_reasons": dict(self._rejection_reasons),
            "since_seen_sec": since_seen,
            "since_accept_sec": (None if self._last_estimate_at is None
                                 else now - self._last_estimate_at),
        }

    def _check(
        self,
        est: FineEdgeEstimate,
        coarse_offset_samples: Optional[float],
        named_second_utc: Optional[int],
    ) -> tuple:
        v = []
        # Every invariant records WHAT IT MEASURED, not just whether it
        # tripped.  A violation name alone cannot distinguish a 5.1 ms
        # breach from a 500 ms one, on the check that decides whether
        # the station's highest timing tier may publish at all --
        # 2026-08-17: `fine_coarse` fired five times in a day and the
        # magnitude was unobtainable from any log or status surface.
        # Read via ``last_check_metrics``; the transition log prints it.
        metrics = {}
        phase = self.edge_phase_rtp(est)
        if self._prev_edge is not None:
            d_ns = (
                self._period_deviation_samples(est, self._prev_edge)
                / self.sample_rate_hz
                * 1e9
            )
            metrics["edge_period_us"] = d_ns / 1e3
            if d_ns > self.edge_period_tolerance_ns:
                v.append("edge_period")
        # fine_coarse stays a phase comparison: the MF coarse offset is
        # itself a mod-SR phase of the same wrapped counter, so both
        # sides move together at a wrap and the check is immune.
        if coarse_offset_samples is not None:
            d_ms = (
                self._wrapped_distance_samples(
                    phase, float(coarse_offset_samples) % self.sample_rate_hz
                )
                / self.sample_rate_hz
                * 1e3
            )
            metrics["fine_coarse_ms"] = d_ms
            metrics["fine_coarse_max_ms"] = self.fine_coarse_max_ms
            if d_ms > self.fine_coarse_max_ms:
                v.append("fine_coarse")
        else:
            # T6 can now reach AUTHORITATIVE on folded estimates alone,
            # so this check may simply not run.  Record that positively:
            # an unrun check must never read as a passed one.
            metrics["fine_coarse_unverified"] = True
        if named_second_utc is None:
            v.append("naming_unavailable")
        self.last_check_metrics = metrics
        return tuple(v)

    def _build_anchor(
        self, est: FineEdgeEstimate, named_second_utc: int
    ) -> NativeAnchor:
        sub_ns = int(round(est.edge_subsample * 1e9 / self.sample_rate_hz))
        # Both asserted terms of the RF path: the TS-1 modulator chain
        # (microseconds) and radiod's channel-filter group delay
        # (milliseconds).  Separately bounded, summed here.
        asserted_ns = self.delay_budget_ns + self.filter_group_delay_ns
        return NativeAnchor(
            anchor_rtp=int(est.edge_rtp) & 0xFFFFFFFF,
            anchor_utc_ns=(named_second_utc * _BILLION + asserted_ns - sub_ns),
            sample_rate_hz=self.sample_rate_hz,
            chain_delay_ns=asserted_ns,
            captured_at_utc_ns=named_second_utc * _BILLION,
            captured_via_tier="T6",
        )

    def on_fine_estimate(
        self,
        est: FineEdgeEstimate,
        coarse_offset_samples: Optional[float],
        named_second_utc: Optional[int],
    ) -> T6AnchorDecision:
        prev = self._state
        violations = self._check(est, coarse_offset_samples, named_second_utc)
        self.note_estimate_seen(violations)

        if not violations:
            self._anchor = self._build_anchor(est, named_second_utc)
            self._prev_edge = (int(est.edge_rtp), float(est.edge_subsample))
            self._degraded_since = None
            self._last_estimate_at = self._now()
            self._state = T6AuthorityState.AUTHORITATIVE
            return T6AnchorDecision(self._state, prev, self._anchor, ())

        if prev in (T6AuthorityState.ACQUIRING, T6AuthorityState.UNLOCKED):
            # Not yet authoritative — keep (re)acquiring.  Track the
            # offset so periodicity has a reference once estimates clean up.
            self._prev_edge = (int(est.edge_rtp), float(est.edge_subsample))
            self._state = T6AuthorityState.ACQUIRING
            self._note_acquiring_violation(violations)
            return T6AnchorDecision(self._state, prev, None, violations)

        # AUTHORITATIVE or DEGRADED with a violation → DEGRADED, hold
        # the last good anchor (GPSDO lets us coast), start/continue dwell.
        if self._degraded_since is None:
            self._degraded_since = self._now()
        if self._now() - self._degraded_since > self.degraded_unlock_after_sec:
            return self._unlock(prev, violations)
        self._state = T6AuthorityState.DEGRADED
        return T6AnchorDecision(self._state, prev, self._anchor, violations)

    def estimate_stale_after_sec(
        self, expected_interval_sec: Optional[float] = None
    ) -> float:
        """How long without an accepted estimate counts as stale."""
        interval = (
            self.fine_fold_seconds
            if expected_interval_sec is None
            else float(expected_interval_sec)
        )
        return ESTIMATE_STALE_INTERVALS * interval

    def on_tick(
        self, expected_interval_sec: Optional[float] = None
    ) -> Optional[T6AnchorDecision]:
        """Liveness invariant — call on every batch (spec §6).

        The rest of this class is edge-triggered on fine estimates.  If
        estimates stop arriving while the MF stays locked (discarded
        fold blocks, a swallowed exception in the fine-stage feed, a
        mis-seeded search window finding nothing), nothing would ever
        re-evaluate the state and the authority would sit
        AUTHORITATIVE forever with a frozen anchor still feeding
        chrony.  That is exactly the detect-and-stall failure the spec
        forbids, so absence of estimates is itself a violation:
        ``estimate_stale`` → DEGRADED, and the normal DEGRADED dwell
        then carries it to UNLOCKED and the legacy cascade.

        Returns None when there is nothing to report (the common case —
        this runs on the sample hot path and must stay cheap).
        """
        if self._state not in (
            T6AuthorityState.AUTHORITATIVE,
            T6AuthorityState.DEGRADED,
        ):
            return None
        now = self._now()
        if self._last_estimate_at is None:
            # Reached AUTHORITATIVE/DEGRADED without a timestamped
            # estimate (defensive) — start the clock now.
            self._last_estimate_at = now
            return None
        if now - self._last_estimate_at <= self.estimate_stale_after_sec(
            expected_interval_sec
        ):
            return None

        prev = self._state
        diag = self.stale_diagnosis()
        logger.warning(
            "T6 estimate_stale: %s — %d estimate(s) seen, %d refused since "
            "the last accepted one, reasons=%s, last seen %ss ago, last "
            "accepted %ss ago",
            "estimates ARE arriving and every one is being refused"
            if diag["verdict"] == "rejected"
            else "no estimates are arriving at all",
            diag["estimates_seen"], diag["rejected_since_accept"],
            diag["rejection_reasons"],
            None if diag["since_seen_sec"] is None else f"{diag['since_seen_sec']:.0f}",
            None if diag["since_accept_sec"] is None else f"{diag['since_accept_sec']:.0f}",
        )
        if self._degraded_since is None:
            self._degraded_since = now
        if now - self._degraded_since > self.degraded_unlock_after_sec:
            return self._unlock(prev, ("estimate_stale",))
        self._state = T6AuthorityState.DEGRADED
        return T6AnchorDecision(self._state, prev, self._anchor, ("estimate_stale",))

    def _note_acquiring_violation(self, violations: tuple) -> None:
        """Throttled visibility for acquiring-forever.

        ACQUIRING with repeated violations is silent by design (no
        state change, no anchor), so a permanently-broken estimator
        looks identical to a cold start.  Say so at WARNING, at most
        once per ACQUIRING_WARN_PERIOD_SEC.
        """
        if not violations:
            return
        now = self._now()
        last = self._last_acquiring_warn_at
        if last is not None and now - last < ACQUIRING_WARN_PERIOD_SEC:
            return
        self._last_acquiring_warn_at = now
        logger.warning(
            "T6 anchor authority: still ACQUIRING — estimates keep "
            "violating invariants (%s).  No T6 anchor is being "
            "published; the legacy T5/T4 cascade is carrying the "
            "chrony feed.  (Repeated at most every %.0f s.)",
            ", ".join(violations),
            ACQUIRING_WARN_PERIOD_SEC,
        )

    def on_mf_unlock(self) -> T6AnchorDecision:
        return self._unlock(self._state, ("mf_unlock",))

    def _unlock(self, prev: T6AuthorityState, violations: tuple) -> T6AnchorDecision:
        self._state = T6AuthorityState.UNLOCKED
        self._anchor = None
        self._prev_edge = None
        self._degraded_since = None
        self._last_estimate_at = None
        return T6AnchorDecision(self._state, prev, None, violations)
