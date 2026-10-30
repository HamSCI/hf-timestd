"""The verified registration IS the T3 anchor (spec §11, 2026-09-07).

Live on AC0G-ND, 2026-09-07: six channels acquired and verified a
registration from the received WWV/WWVH tick train, corroborated it
against the 800 ms minute marker every minute, and held it to a
millisecond — while the host clock walked 150 ms away from four NTP
witnesses and the psk decodes died.  The ticks were right and nothing
carried them to the anchor the clients read.

Michael's ruling on the shape of the fix (2026-09-07):

    "Why do we compare things to the host clock?  It is not the ruler or
    the standard but a product of FUSION.  Imagine there is no host clock
    at all, but ONLY FUSION."

So this module does NOT express the anchor as "radiod's host-stamped
(GPS_TIME, RTP_TIMESNAP) pair plus a judge offset".  It expresses the
registration the way T6 already expresses its own anchor — as a
:class:`~hf_timestd.core.native_anchor.NativeAnchor`, a captured
(RTP, UTC) pairing from which every label is pure counter arithmetic:

    utc_ns(rtp) = anchor_utc_ns + (rtp − anchor_rtp) × 10⁹ / sample_rate_hz

``chain_delay_ns`` is 0 because the registration is already a plane, not
an edge: ``utc_ref`` is the UTC of the sample at ``rtp_ref``, with the
propagation model (``expected_delay − fold_position``) already removed by
the acquirer.  There is no second RF term to subtract.

The gates below are all liveness and provenance — never a comparison
against the host clock:

* a T6 native anchor that is AUTHORITATIVE owns the plane, and one
  station publishes ONE registration (mjh, 2026-09-04), so T3 stands
  down while T6 stands up;
* every gate in :func:`registration_store.registration_refusal` --
  state ACQUIRED, ``verified`` true, fresh against the store's own
  stale window, counter domain matching.  That function is THE one gate
  (fix round 1, review finding C-1): the FUSE chrony feed asks it too,
  so a plane refused for the ring can never reach the host clock
  instead.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .native_anchor import LabelAnchor, NativeAnchor
from .registration_acquirer import ORIGIN_SIGMA_FLOOR_MS
from .registration_store import DEFAULT_STALE_S, registration_refusal

logger = logging.getLogger(__name__)

# The summary state that means "acquired AND verified".
VERIFIED_STATE = "ACQUIRED"


@dataclass(frozen=True)
class T3AnchorDecision:
    """One evaluation of the registration as an anchor.

    ``reason`` names the outcome for the log and for status publication:
    ``acquired`` when the anchor stands, otherwise which gate refused it.
    """

    anchor: Optional[NativeAnchor]
    epoch_id: Optional[str]
    reason: str
    # What the plane honestly claims, ns.  The registration's own
    # sigma_ms floored at ORIGIN_SIGMA_FLOOR_MS: the fused sigma is the
    # plane's REPEATABILITY, while a consumer publishing §18 acts on it
    # as an ACCURACY claim, and the accuracy of an
    # ``expected_delay - fold_position`` origin is bounded by the delay
    # model -- common-mode across channels, so fusing N does not shrink
    # it (the same floor HfAcquiredBench applies).
    sigma_ns: float = 0.0
    # The channel whose counter ``anchor_rtp`` is in, from the summary's
    # ``rtp_domain``.  "" when the summary names none: the anchor then labels
    # no channel at all, because nothing says which counter it belongs to.
    rtp_domain: Optional[str] = None

    @property
    def in_force(self) -> bool:
        return self.anchor is not None


class T3RegistrationAnchor:
    """Holds the station's T3 label-plane anchor, re-derived on demand.

    Cheap to call: one ``read_summary`` (a tmpfs JSON read) per refresh.
    Never raises — a station with no registration simply has no T3
    anchor, which is the pre-amendment behaviour.
    """

    def __init__(
        self,
        store=None,
        time_fn: Callable[[], float] = time.time,
        *,
        anchor_closure: bool = False,
        admitted: bool = False,
    ):
        if store is None:
            from .registration_store import RegistrationStore

            store = RegistrationStore()
        self._store = store
        self._time = time_fn
        # Task 17a: the closure is opt-in, and OFF is the default here as
        # well as in the config.  A future call site that forgets to pass
        # the flag gets the safe regime, not the one that steered
        # AC0G-ND's clock the wrong way on 2026-09-07.
        self._closure = bool(anchor_closure)
        # Ruling B (2026-10-30): ``[timing.t3] admitted``.  Until it reads
        # true the registration anchors nothing, closure or not.  False by
        # default, for the same reason as the closure.
        self._admitted = bool(admitted)
        self._decision = T3AnchorDecision(None, None, "not_evaluated")

    # ── the current answer ───────────────────────────────────────────

    @property
    def decision(self) -> T3AnchorDecision:
        return self._decision

    @property
    def anchor(self) -> Optional[NativeAnchor]:
        return self._decision.anchor

    def state(self) -> Optional[LabelAnchor]:
        """The :class:`LabelAnchor` in force, or None.

        This is the provider shape every consumer takes -- the ring, §18
        and the archive sidecar -- so all three label from one object
        (task 14).
        """
        d = self._decision
        if d.anchor is None:
            return None
        return LabelAnchor(
            anchor=d.anchor,
            epoch_id=d.epoch_id,
            tier="T3",
            sigma_ns=float(d.sigma_ns),
            rtp_domain=d.rtp_domain,
        )

    # ── evaluation ──────────────────────────────────────────────────

    def refresh(
        self, *, t6_authoritative: bool, sample_rate: Optional[int] = None
    ) -> T3AnchorDecision:
        """Re-derive the anchor and log any change of regime."""
        decision = self.evaluate(
            sample_rate=sample_rate, t6_authoritative=t6_authoritative
        )
        prev = self._decision
        self._decision = decision
        self._log_transition(prev, decision)
        return decision

    def evaluate(
        self, *, t6_authoritative: bool, sample_rate: Optional[int] = None
    ) -> T3AnchorDecision:
        """Pure evaluation — no state, no logging.

        ``sample_rate`` names a specific counter domain to accept; None
        (the station-wide refresh) accepts the registration's own domain
        and leaves per-channel matching to the consumer, which knows its
        own configured rate (``StreamRecorderV2._label_anchor_state``).
        """
        # Task 17a, ahead of every other gate: the operator needs to see
        # that the closure itself is off, not the name of a gate that was
        # never consulted.
        if not self._closure:
            return T3AnchorDecision(None, None, "closure_disabled")
        if not self._admitted:
            from .t3_admission import NOT_ADMITTED_REASON
            return T3AnchorDecision(None, None, NOT_ADMITTED_REASON)
        if t6_authoritative:
            return T3AnchorDecision(None, None, "t6_authoritative")
        try:
            summary = self._store.read_summary()
        except Exception:  # noqa: BLE001 — a dead store is not an anchor
            return T3AnchorDecision(None, None, "read_failed")
        if not summary:
            return T3AnchorDecision(None, None, "no_summary")
        epoch_id = summary.get("counter_epoch_id")
        epoch_id = None if epoch_id is None else str(epoch_id)
        # THE one gate (fix round 1, C-1): the same predicate the FUSE
        # feed's label-plane anchor now asks, so a plane refused here can
        # never reach chrony instead.
        refusal = registration_refusal(
            summary,
            now=self._time(),
            stale_s=float(getattr(self._store, "stale_s", DEFAULT_STALE_S)),
            sample_rate=sample_rate,
        )
        if refusal is not None:
            return T3AnchorDecision(None, epoch_id, refusal)
        rtp_ref = int(summary["rtp_ref"])
        utc_ref = float(summary["utc_ref"])
        reg_rate = int(summary["sample_rate"])
        written_at = float(summary.get("written_at", 0.0))
        sigma_ms = max(float(summary["sigma_ms"]), ORIGIN_SIGMA_FLOOR_MS)
        anchor = NativeAnchor(
            anchor_rtp=rtp_ref & 0xFFFFFFFF,
            anchor_utc_ns=int(round(utc_ref * 1e9)),
            sample_rate_hz=reg_rate,
            # The registration is a PLANE, not an edge: utc_ref already
            # names the UTC of the sample at rtp_ref with the propagation
            # model removed.  No analog chain term remains to subtract.
            chain_delay_ns=0,
            # Provenance only (the wrap-epoch disambiguator): when this
            # registration was published, not a claim about the plane.
            captured_at_utc_ns=int(round(written_at * 1e9)),
            captured_via_tier="T3",
        )
        return T3AnchorDecision(anchor, epoch_id, "acquired", sigma_ns=sigma_ms * 1e6,
            rtp_domain=str(summary.get("rtp_domain") or ""),
        )

    # ── logging ─────────────────────────────────────────────────────

    def _log_transition(self, prev: T3AnchorDecision, now: T3AnchorDecision) -> None:
        if prev.reason == now.reason and prev.epoch_id == now.epoch_id:
            return
        if now.anchor is not None:
            logger.info(
                "T3 anchor: the verified registration IS the anchor "
                "(rtp_ref=%d, utc_ref=%.6f, %d Hz, epoch=%s)",
                now.anchor.anchor_rtp,
                now.anchor.anchor_utc_ns / 1e9,
                now.anchor.sample_rate_hz,
                now.epoch_id,
            )
        elif prev.anchor is not None:
            logger.warning(
                "T3 anchor: WITHDRAWN (%s) — the ring falls back to "
                "radiod's pair with the judge's correction",
                now.reason,
            )
        else:
            logger.debug("T3 anchor: none (%s)", now.reason)
