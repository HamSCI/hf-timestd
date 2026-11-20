"""Does T3 hold admission this minute?  The automatic admission policy (A5).

The authority manager builds one shadow row a minute.  This policy reads
each row as a ``MinuteEvidence`` and returns an ``AdmissionDecision``: whether
T3 may label the station's data, by which route, and why.

Route 1 (a station with a reference, T4, T5 or T6): over a rolling window,
T3 must beat T2 against the reference by the median and by the 95th
percentile of the absolute error (10-30 section 9, criterion 6).  A change
of that verdict takes effect only after it has held for ``hold_s``.  The
station's clock stability (``ClockStabilityEstimator``, fed by T3's own
phase) sets the window and the hold.

Safety outranks everything else.  T3 never names the wrong second and never
jumps by 0.5 s or more: such a minute withdraws T3 at once, clears the
window and restarts the fill, in every mode but OFF (criterion 4).

Pure: no file I/O and no clock reads.  The only time the policy knows comes
from each minute's ``utc_minute`` stamp, which names minutes and never
enters timing arithmetic.
"""
from __future__ import annotations

import dataclasses
import logging
import math
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Iterable, NamedTuple, Optional

import numpy as np

from hf_timestd.core.clock_stability import (
    HOLD_MIN_S, WINDOW_MAX_S, WINDOW_MIN_S, ClockStability,
    ClockStabilityEstimator, PhasePoint)
from hf_timestd.core.t3_admission import AdmissionMode
from hf_timestd.core.t3_reference_errors import ReferencePair, reference_errors
from hf_timestd.core.t3_sigma_honesty import SigmaHonesty

logger = logging.getLogger(__name__)

# Builder choice: the fill needs at least this many pairs, and at least one
# per two minutes of the window (need = max(FILL_MIN_PAIRS, window_s // 120)).
FILL_MIN_PAIRS = 20
# Spec (criterion 4): a jump of 0.5 s or more withdraws T3 at once.  Applies
# to |T3 error| against the reference and to a phase step within one run.
JUMP_MS = 500.0
# Builder choice: the policy recomputes ClockStability at most once per
# this many seconds of utc_minute (the estimator's TDEV costs a day of math).
STABILITY_RECOMPUTE_S = 600

# A utc_minute that from_row could not read.  observe() ignores it.
INVALID_MINUTE = -1

ROUTE_REFERENCE = "reference"
ROUTE_SIGMA = "sigma"          # Phase B (route 2); nothing produces it yet
ROUTE_FORCED = "forced"

# Reason strings.  Tests and the published block pin them verbatim.
REASON_ADMITTED = "admitted by reference ({ref})"
REASON_ADMITTED_HELD = "admitted by reference ({ref}), held: no reference for {m} min"
REASON_T2_BETTER = ("withheld: T2 tracks {ref} better (median {t3:.2f} vs "
                    "{t2:.2f} ms, p95 {t3p:.2f} vs {t2p:.2f} ms)")
REASON_FILLING = "withheld: window filling ({n} of {need} minutes)"
REASON_NO_T2 = "withheld: no T2 to compare against"
REASON_NO_REFERENCE = "withheld: no reference, and no fleet calibration yet"
REASON_WRONG_SECOND = "withdrawn: T3 named the wrong second ({err:.0f} ms against {ref})"
REASON_JUMPED = "withdrawn: T3 jumped {step:.0f} ms"
REASON_UNRESOLVED = "withheld: T3 has not named the second"
REASON_OFF = 'off ([timing.t3] admission = "off")'
REASON_FORCED = 'forced on ([timing.t3] admission = "on"), for testing only'
REASON_PENDING = "; change pending {s} of {hold} s"


def _num(v: Any) -> Optional[float]:
    """A finite number, else None.  A boolean or a NaN counts as missing."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def _minute(v: Any) -> int:
    if isinstance(v, bool):
        return INVALID_MINUTE
    if isinstance(v, int):
        return v if v >= 0 else INVALID_MINUTE
    if isinstance(v, float) and math.isfinite(v) and v >= 0 and v == int(v):
        return int(v)
    return INVALID_MINUTE


def _need(window_s: int) -> int:
    return max(FILL_MIN_PAIRS, int(window_s) // 120)


def _unmeasured() -> ClockStability:
    return ClockStability(tau_cross_s=None, window_s=WINDOW_MIN_S,
                          hold_s=HOLD_MIN_S, drift_ns_per_s=None, n_points=0)


@dataclass(frozen=True)
class MinuteEvidence:
    utc_minute: int
    pair: Optional[ReferencePair]
    phase: Optional[PhasePoint]
    t3_sigma_ms: Optional[float]
    t2_sigma_ms: Optional[float]
    whole_second_unresolved: bool

    @classmethod
    def from_row(cls, row: dict) -> "MinuteEvidence":
        """Evidence from one shadow minute row
        (``AuthorityManager._t3_shadow_minute_row``).  Never raises.

        The phase needs both ``t3_phase_ms`` and ``t3_run_id`` as finite,
        non-boolean numbers.  A malformed ``utc_minute`` reads as
        ``INVALID_MINUTE``, which ``observe`` ignores."""
        minute, pair, phase = INVALID_MINUTE, None, None
        t3_sigma = t2_sigma = None
        unresolved = False
        try:
            if isinstance(row, dict):
                minute = _minute(row.get("utc_minute"))
                pair = reference_errors(row)
                ph, run = _num(row.get("t3_phase_ms")), _num(row.get("t3_run_id"))
                if ph is not None and run is not None and minute != INVALID_MINUTE:
                    phase = PhasePoint(utc_minute=minute, phase_ms=ph, run_id=run)
                t3 = row.get("t3")
                if isinstance(t3, dict):
                    t3_sigma = _num(t3.get("sigma_ms"))
                    unresolved = t3.get("whole_second_unresolved") is True
                t2_sigma = _num(row.get("t2_sigma_ms"))
        except Exception:  # noqa: BLE001 -- a bad row yields empty evidence
            pass
        return cls(utc_minute=minute, pair=pair, phase=phase,
                   t3_sigma_ms=t3_sigma, t2_sigma_ms=t2_sigma,
                   whole_second_unresolved=unresolved)


@dataclass(frozen=True)
class AdmissionDecision:
    mode: AdmissionMode
    admitted: bool
    route: Optional[str]           # "reference", "sigma", "forced" or None
    reason: str
    reference: Optional[str]
    n_pairs: int
    t3_median_ms: Optional[float]
    t3_p95_ms: Optional[float]
    t2_median_ms: Optional[float]
    t2_p95_ms: Optional[float]
    stability: ClockStability
    since_utc_minute: Optional[int]   # minute of the last change of admitted
    transitions: int

    @classmethod
    def initial(cls, mode: AdmissionMode) -> "AdmissionDecision":
        """The decision before any minute.  ON admits from the start; AUTO
        and OFF start closed."""
        stab = _unmeasured()
        if mode is AdmissionMode.ON:
            admitted, route, reason = True, ROUTE_FORCED, REASON_FORCED
        elif mode is AdmissionMode.OFF:
            admitted, route, reason = False, None, REASON_OFF
        else:
            admitted, route = False, None
            reason = REASON_FILLING.format(n=0, need=_need(stab.window_s))
        return cls(mode=mode, admitted=admitted, route=route, reason=reason,
                   reference=None, n_pairs=0, t3_median_ms=None,
                   t3_p95_ms=None, t2_median_ms=None, t2_p95_ms=None,
                   stability=stab, since_utc_minute=None, transitions=0)

    def to_block(self) -> dict:
        """``authority.json``'s ``t3_admission`` block and the minute row's
        ``admission`` field.  Strict JSON: a non-finite float reads null.
        ``stability`` keeps ClockStability's field names, so a reader can
        rebuild it (``clock_stability_from_block``)."""
        def clean(v):
            if isinstance(v, float) and not math.isfinite(v):
                return None
            return v
        stab = {k: clean(v) for k, v in dataclasses.asdict(self.stability).items()}
        return {
            "mode": self.mode.value,
            "admitted": bool(self.admitted),
            "route": self.route,
            "reason": self.reason,
            "reference": self.reference,
            "n_pairs": int(self.n_pairs),
            "t3_median_ms": clean(self.t3_median_ms),
            "t3_p95_ms": clean(self.t3_p95_ms),
            "t2_median_ms": clean(self.t2_median_ms),
            "t2_p95_ms": clean(self.t2_p95_ms),
            "stability": stab,
            "since_utc_minute": self.since_utc_minute,
            "transitions": int(self.transitions),
        }


def clock_stability_from_block(d: Any) -> Optional[ClockStability]:
    """Rebuild ``ClockStability`` from a block's ``stability`` dict.  None
    when the dict is missing or malformed.  Never raises."""
    def as_int(v, optional=False):
        if v is None and optional:
            return True, None
        if isinstance(v, bool) or not isinstance(v, int):
            return False, None
        return True, v
    try:
        if not isinstance(d, dict):
            return None
        ok_t, tau = as_int(d.get("tau_cross_s"), optional=True)
        ok_w, window = as_int(d.get("window_s"))
        ok_h, hold = as_int(d.get("hold_s"))
        ok_n, n = as_int(d.get("n_points"))
        drift_raw = d.get("drift_ns_per_s")
        drift = _num(drift_raw)
        ok_d = drift_raw is None or drift is not None
        if not (ok_t and ok_w and ok_h and ok_n and ok_d):
            return None
        return ClockStability(tau_cross_s=tau, window_s=window, hold_s=hold,
                              drift_ns_per_s=drift, n_points=n)
    except Exception:  # noqa: BLE001
        return None


class _Pair(NamedTuple):
    minute: int
    reference: str
    t3: float
    t2: float


class T3AdmissionPolicy:
    """One shadow minute in, one decision out."""

    def __init__(self, mode: AdmissionMode) -> None:
        self._mode = mode
        self._estimator = ClockStabilityEstimator()
        self._honesty = SigmaHonesty()
        self._stability = _unmeasured()
        self._stability_at: Optional[int] = None
        # Complete pairs (both errors), kept for WINDOW_MAX_S so a window
        # that grows can use them.  A jump empties it.
        self._pairs: Deque[_Pair] = deque()
        self._last_minute: Optional[int] = None
        self._last_phase: Optional[PhasePoint] = None
        self._last_pair_minute: Optional[int] = None
        # A jump holds T3 withdrawn until this minute, in every mode.
        self._jump_until: Optional[int] = None
        self._jump_reason: Optional[str] = None
        # Hysteresis: the minute the wanted state began to differ.
        self._pending_since: Optional[int] = None
        # The reason the standing state last stood on its own.
        self._standing_reason: str
        self._seeding = False
        self._decision = AdmissionDecision.initial(mode)
        self._standing_reason = self._decision.reason

    @property
    def decision(self) -> AdmissionDecision:
        return self._decision

    @property
    def honesty(self) -> SigmaHonesty:
        return self._honesty

    # ------------------------------------------------------------------ seed

    def seed(self, rows: Iterable[dict]) -> int:
        """Warm-start from shadow minute rows, oldest first.  Returns how
        many rows the policy used.

        A row in the shape written before this change (one lacking the
        ``"t3_phase_ms"`` key) is skipped: it carries no phase, so it would
        teach the policy a window with no stability behind it.  A row whose
        minute repeats or runs backward is not counted.  Changes of
        ``admitted`` during the seed log at INFO, not WARNING: they replay
        history that the previous process already announced.  Never
        raises."""
        used = 0
        self._seeding = True
        try:
            for row in rows:
                try:
                    if not isinstance(row, dict) or "t3_phase_ms" not in row:
                        continue
                    before = self._last_minute
                    self.observe(MinuteEvidence.from_row(row))
                    if self._last_minute != before:
                        used += 1
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001 -- non-iterable input
            pass
        finally:
            self._seeding = False
        return used

    # --------------------------------------------------------------- observe

    def observe(self, ev: MinuteEvidence) -> AdmissionDecision:
        try:
            return self._observe(ev)
        except Exception as exc:  # noqa: BLE001 -- never raise into the tick
            logger.error("T3 admission policy failed on minute %r: %r",
                         getattr(ev, "utc_minute", None), exc)
            return self._decision

    def _observe(self, ev: MinuteEvidence) -> AdmissionDecision:
        now = ev.utc_minute
        if (isinstance(now, bool) or not isinstance(now, int)
                or now == INVALID_MINUTE or now < 0):
            return self._decision
        # The host clock stepped back, or a row repeats: nothing new.
        if self._last_minute is not None and now <= self._last_minute:
            return self._decision
        self._last_minute = now

        # Evidence feeds, in every mode: the shadow record keeps running.
        if ev.phase is not None:
            self._estimator.add(ev.phase)
        if ev.pair is not None:
            self._honesty.add(now, ev.pair, ev.t3_sigma_ms)
        if (self._stability_at is None
                or now - self._stability_at >= STABILITY_RECOMPUTE_S):
            self._stability = self._estimator.stability()
            self._stability_at = now
        stab = self._stability
        hold = int(stab.hold_s)

        jump = self._jump_reason_for(ev)
        if ev.phase is not None:
            self._last_phase = ev.phase

        # The window takes this minute's pair only when it is complete and
        # trustworthy: not a jump, and not a minute T3 has not named.
        complete = (ev.pair is not None and ev.pair.t2_error_ms is not None
                    and _num(ev.pair.t3_error_ms) is not None
                    and _num(ev.pair.t2_error_ms) is not None)
        if jump is not None:
            self._pairs.clear()
            self._last_pair_minute = None
        elif complete and not ev.whole_second_unresolved:
            self._pairs.append(_Pair(now, ev.pair.reference,
                                     float(ev.pair.t3_error_ms),
                                     float(ev.pair.t2_error_ms)))
            self._last_pair_minute = now
        while self._pairs and self._pairs[0].minute <= now - WINDOW_MAX_S:
            self._pairs.popleft()

        window = [p for p in self._pairs if p.minute > now - stab.window_s]
        stats = self._stats(window)
        reference = (window[-1].reference if window
                     else (ev.pair.reference if ev.pair is not None else None))

        if jump is not None:
            self._jump_until = now + hold
            self._jump_reason = jump

        mode = self._mode
        if mode is AdmissionMode.OFF:
            self._settle(now, False, None, REASON_OFF, immediate=True)
        elif self._jump_until is not None and now < self._jump_until:
            # The safety rule outranks the test switch: ON waits out the
            # hold too.  The withdrawal names itself for the whole hold.
            self._settle(now, False, None, self._jump_reason, immediate=True)
        elif ev.whole_second_unresolved:
            # Controller ruling: withhold at once, in AUTO and ON alike
            # (conservative: T3 never names the wrong second).  The window
            # stays (no error was seen), but the minute adds no pair and
            # any pending change restarts.
            self._settle(now, False, None, REASON_UNRESOLVED, immediate=True)
        elif mode is AdmissionMode.ON:
            self._settle(now, True, ROUTE_FORCED, REASON_FORCED, immediate=True)
        else:
            self._auto(now, ev, complete, window, stats, reference, stab)

        if self._jump_until is not None and now >= self._jump_until:
            self._jump_until = None
            self._jump_reason = None

        d = self._decision
        self._decision = dataclasses.replace(
            d, reference=reference, n_pairs=len(window),
            t3_median_ms=stats[0], t3_p95_ms=stats[1],
            t2_median_ms=stats[2], t2_p95_ms=stats[3], stability=stab)
        return self._decision

    def _jump_reason_for(self, ev: MinuteEvidence) -> Optional[str]:
        """The withdrawal reason when this minute jumps, else None."""
        if ev.pair is not None:
            err = _num(ev.pair.t3_error_ms)
            if err is not None and abs(err) >= JUMP_MS:
                return REASON_WRONG_SECOND.format(err=err, ref=ev.pair.reference)
        last = self._last_phase
        if (ev.phase is not None and last is not None
                and ev.phase.run_id == last.run_id):
            # Decision: "consecutive" means the last phase point seen in the
            # same run, however many minutes lie between (a gap cannot hide
            # a step).  A run change starts a new phase origin: no jump.
            step = ev.phase.phase_ms - last.phase_ms
            if math.isfinite(step) and abs(step) >= JUMP_MS:
                return REASON_JUMPED.format(step=step)
        return None

    @staticmethod
    def _stats(window):
        if not window:
            return None, None, None, None
        e3 = np.abs(np.array([p.t3 for p in window], float))
        e2 = np.abs(np.array([p.t2 for p in window], float))
        return (float(np.median(e3)), float(np.percentile(e3, 95)),
                float(np.median(e2)), float(np.percentile(e2, 95)))

    def _auto(self, now, ev, complete, window, stats, reference, stab):
        need = _need(stab.window_s)
        n = len(window)
        d = self._decision
        if n >= need:
            t3m, t3p, t2m, t2p = stats
            if t3m < t2m and t3p < t2p:
                self._settle(now, True, ROUTE_REFERENCE,
                             REASON_ADMITTED.format(ref=reference),
                             immediate=False)
            else:
                self._settle(now, False, None, REASON_T2_BETTER.format(
                    ref=reference, t3=t3m, t2=t2m, t3p=t3p, t2p=t2p),
                    immediate=False)
            return
        # Too few pairs to judge.  Reference loss: an admission by reference
        # with no change pending holds for one window after the last pair.
        if (d.admitted and d.route == ROUTE_REFERENCE
                and self._pending_since is None and not complete
                and self._last_pair_minute is not None
                and now - self._last_pair_minute < stab.window_s):
            ref = d.reference or reference
            m = (now - self._last_pair_minute) // 60
            self._settle(now, True, ROUTE_REFERENCE,
                         REASON_ADMITTED_HELD.format(ref=ref, m=m),
                         immediate=True)
            return
        if ev.pair is None:
            # Phase B: route 2 (admission by sigma, ROUTE_SIGMA, against the
            # fleet calibration) goes here.  Phase A has no calibration.
            reason = REASON_NO_REFERENCE
        elif ev.pair.t2_error_ms is None:
            reason = REASON_NO_T2
        else:
            reason = REASON_FILLING.format(n=n, need=need)
        # Decision: missing evidence withholds at once, without waiting a
        # hold (conservative; the reference-loss hold above already gave an
        # admitted station one window of grace).
        self._settle(now, False, None, reason, immediate=True)

    def _settle(self, now: int, want: bool, route: Optional[str], reason: str,
                *, immediate: bool) -> None:
        d = self._decision
        if want == d.admitted:
            self._pending_since = None
            self._standing_reason = reason
            self._decision = dataclasses.replace(d, route=route, reason=reason)
            return
        hold = int(self._stability.hold_s)
        if not immediate:
            if self._pending_since is None:
                self._pending_since = now
            elapsed = now - self._pending_since
            if elapsed < hold:
                self._decision = dataclasses.replace(
                    d, reason=self._standing_reason
                    + REASON_PENDING.format(s=elapsed, hold=hold))
                return
        self._pending_since = None
        self._standing_reason = reason
        self._decision = dataclasses.replace(
            d, admitted=want, route=route, reason=reason,
            since_utc_minute=now, transitions=d.transitions + 1)
        level = logging.INFO if self._seeding else logging.WARNING
        logger.log(level, "T3 admission %s at utc_minute %d: %s%s",
                   "granted" if want else "withdrawn", now, reason,
                   " (seed replay)" if self._seeding else "")
