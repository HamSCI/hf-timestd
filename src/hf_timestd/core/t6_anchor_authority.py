"""T6 anchor authority — owns "is T6 the RTP→UTC anchor authority now".

Spec: docs/design/T6_ANCHOR_INVERSION_DESIGN.md §2, §4, §6.  When
AUTHORITATIVE, the anchor is built by the inversion:

    anchor_utc_ns = named_integer_second·1e9 + delay_budget_ns − subsample

The coarse cascade only NAMES the integer second (±0.5 s duty); its
noise cannot enter the sub-second value by construction.  All state
transitions are returned to the caller for loud logging — this module
never logs silently-consequential decisions itself, and it never
consults a wall clock (the injected ``now`` measures DEGRADED dwell
only).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

from hf_timestd.core.bpsk_edge_fine_stage import FineEdgeEstimate
from hf_timestd.core.native_anchor import NativeAnchor

_BILLION = 1_000_000_000
DELAY_BUDGET_BOUND_NS = 1_000_000  # ±1 ms hard physical bound (spec §5)


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
        edge_period_tolerance_ns: int = 5_000,
        fine_coarse_max_ms: float = 5.0,
        degraded_unlock_after_sec: float = 600.0,
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
        self.sample_rate_hz = int(sample_rate_hz)
        self.delay_budget_ns = int(delay_budget_ns)
        self.edge_period_tolerance_ns = int(edge_period_tolerance_ns)
        self.fine_coarse_max_ms = float(fine_coarse_max_ms)
        self.degraded_unlock_after_sec = float(degraded_unlock_after_sec)
        self._now = now
        self._state = T6AuthorityState.ACQUIRING
        self._anchor: Optional[NativeAnchor] = None
        self._prev_offset: Optional[float] = None
        self._degraded_since: Optional[float] = None

    @property
    def state(self) -> T6AuthorityState:
        return self._state

    def _wrapped_distance_samples(self, a: float, b: float) -> float:
        p = self.sample_rate_hz
        return abs((a - b + p / 2) % p - p / 2)

    def _check(
        self,
        est: FineEdgeEstimate,
        coarse_offset_samples: Optional[float],
        named_second_utc: Optional[int],
    ) -> tuple:
        v = []
        if self._prev_offset is not None:
            d_ns = (
                self._wrapped_distance_samples(
                    est.edge_offset_samples, self._prev_offset
                )
                / self.sample_rate_hz
                * 1e9
            )
            if d_ns > self.edge_period_tolerance_ns:
                v.append("edge_period")
        if coarse_offset_samples is not None:
            d_ms = (
                self._wrapped_distance_samples(
                    est.edge_offset_samples, coarse_offset_samples
                )
                / self.sample_rate_hz
                * 1e3
            )
            if d_ms > self.fine_coarse_max_ms:
                v.append("fine_coarse")
        if named_second_utc is None:
            v.append("naming_unavailable")
        return tuple(v)

    def _build_anchor(
        self, est: FineEdgeEstimate, named_second_utc: int
    ) -> NativeAnchor:
        sub_ns = int(round(est.edge_subsample * 1e9 / self.sample_rate_hz))
        return NativeAnchor(
            anchor_rtp=int(est.edge_rtp) & 0xFFFFFFFF,
            anchor_utc_ns=(named_second_utc * _BILLION + self.delay_budget_ns - sub_ns),
            sample_rate_hz=self.sample_rate_hz,
            chain_delay_ns=self.delay_budget_ns,
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

        if not violations:
            self._anchor = self._build_anchor(est, named_second_utc)
            self._prev_offset = est.edge_offset_samples
            self._degraded_since = None
            self._state = T6AuthorityState.AUTHORITATIVE
            return T6AnchorDecision(self._state, prev, self._anchor, ())

        if prev in (T6AuthorityState.ACQUIRING, T6AuthorityState.UNLOCKED):
            # Not yet authoritative — keep (re)acquiring.  Track the
            # offset so periodicity has a reference once estimates clean up.
            self._prev_offset = est.edge_offset_samples
            self._state = T6AuthorityState.ACQUIRING
            return T6AnchorDecision(self._state, prev, None, violations)

        # AUTHORITATIVE or DEGRADED with a violation → DEGRADED, hold
        # the last good anchor (GPSDO lets us coast), start/continue dwell.
        if self._degraded_since is None:
            self._degraded_since = self._now()
        if self._now() - self._degraded_since > self.degraded_unlock_after_sec:
            return self._unlock(prev, violations)
        self._state = T6AuthorityState.DEGRADED
        return T6AnchorDecision(self._state, prev, self._anchor, violations)

    def on_mf_unlock(self) -> T6AnchorDecision:
        return self._unlock(self._state, ("mf_unlock",))

    def _unlock(self, prev: T6AuthorityState, violations: tuple) -> T6AnchorDecision:
        self._state = T6AuthorityState.UNLOCKED
        self._anchor = None
        self._prev_offset = None
        self._degraded_since = None
        return T6AnchorDecision(self._state, prev, None, violations)
