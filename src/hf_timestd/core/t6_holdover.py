"""How far the T6 anchor may coast after the fine stage loses lock.

The anchor is a ``(rtp, utc)`` pair plus a sample rate.  Labelling a
sample is pure counter arithmetic against it, so the anchor does not go
stale the way a wall-clock reading does — it stays correct for as long
as the RTP counter remains continuous and advances at the rate the
anchor assumes.  **That rate is disciplined by the GPSDO**, which is a
frequency reference, not a time-of-day source: it cannot tell us when
the second boundary is, but it can hold one we already found.

So losing carrier lock does not invalidate the anchor.  It stops us
LEARNING, and our uncertainty grows at the rate we are unsure of the
rate.  Measured on AC0G-B4 2026-08-16 over a 900 s window:

    t6_residual_rate = -0.0004 +/- 0.0004 ppm

which is 1.44 us per hour.  A six-hour thunderstorm therefore costs
about 8.6 us of accumulated uncertainty against a frozen-anchor sigma
of ~800 us — three orders of magnitude smaller than the term it is
added to.  Reaching a millisecond takes roughly a month.

⇒ Coasting through weather is essentially free, and going dark instead
is throwing away a good clock because a signal got noisy.  That is why
this module exists: to replace an abrupt withdraw with a coast whose
uncertainty is stated honestly and grows on its own.

⚠ WHAT THIS MODULE DOES NOT LICENSE.  hf-timestd#14 was caused by a
DIFFERENT thing also called coasting: while unlocked, the calibrator
kept deriving fresh chain delays from noise and publishing them, so
each re-lock landed somewhere new (644.7 ms and 479.4 ms three minutes
apart) and chrony saw a source with a 203 ms standard deviation.  The
coast here admits NO new measurements at all — the anchor is frozen at
the last validated edge and only the counter advances.  A holdover that
accepts unvalidated edges is not a holdover; it is #14.
"""
from __future__ import annotations

import math
from typing import Optional, Tuple

# Stand-in when the residual rate has not been measured yet.  This is a
# refuse-to-claim bound, not a measurement: the estimator needs ~900 s
# of anchor lifetime, and it resets on every recapture, so a fresh or
# just-restarted station is genuinely in this state.  25x the value
# measured on B4 -- deliberately pessimistic, because an unmeasured
# oscillator must never be indistinguishable from a characterised one.
UNMEASURED_RATE_SIGMA_PPM = 0.01


def holdover_sigma_ns(
    sigma_at_freeze_ns: float,
    rate_sigma_ppm: Optional[float],
    elapsed_s: float,
) -> float:
    """1-sigma uncertainty of a frozen anchor ``elapsed_s`` into a coast.

    Two independent terms in quadrature: what the anchor was worth at
    the moment it froze, and the rate uncertainty integrated over the
    coast.  ``rate_sigma_ppm=None`` means unmeasured, which widens
    rather than narrows (see ``UNMEASURED_RATE_SIGMA_PPM``).
    """
    elapsed = max(0.0, float(elapsed_s))
    ppm = (
        UNMEASURED_RATE_SIGMA_PPM
        if rate_sigma_ppm is None
        else abs(float(rate_sigma_ppm))
    )
    # ppm -> fractional -> seconds of drift -> ns
    drift_ns = ppm * 1e-6 * elapsed * 1e9
    base_ns = float(sigma_at_freeze_ns)
    return (base_ns * base_ns + drift_ns * drift_ns) ** 0.5


def may_coast(
    anchor_frozen: bool,
    rtp_continuous: bool,
    sigma_ns: float,
    max_sigma_ns: float,
) -> Tuple[bool, str]:
    """Whether a holdover is still standing on something real.

    Ordered most-fundamental first, and the reason is returned so the
    log can say which precondition failed rather than just going quiet.

    ``rtp_continuous`` is not a formality.  The coast rests entirely on
    the RTP counter advancing at the GPSDO's rate; a radiod restart
    re-bases that counter, at which point the frozen anchor points into
    a different numbering and no amount of accumulated precision
    substitutes for it.  Refuse immediately -- do not wait for sigma to
    grow, because sigma is not measuring that failure.
    """
    if not anchor_frozen:
        return False, "no frozen anchor to coast on"
    if not rtp_continuous:
        return False, "rtp counter discontinuity — the ruler was re-based"
    if float(sigma_ns) > float(max_sigma_ns):
        return False, (
            "holdover sigma %.3f ms exceeds the %.3f ms bound"
            % (float(sigma_ns) / 1e6, float(max_sigma_ns) / 1e6)
        )
    return True, "ok"


def holdover_named_second(
    floor_offset_s: float,
    mono_now: float,
    chain_delay_ns: int,
) -> int:
    """The most recent true second boundary, named without an edge.

    A coast has no fresh edge to name a second from — and must not use
    one, because an edge detected during the outage is unvalidated
    (hf-timestd#14).  The frozen anchor plus the arrival floor are
    enough: the floor maps monotonic to the anchor's LABEL space, and a
    label is a sampling instant, so the PPS that produced it fired
    ``chain_delay`` EARLIER.  Subtract to reach firing space, then take
    the boundary below.

    Returns the integer second in firing space; feed it to
    ``t6_shm_system_time`` as ``edge_label_utc_s`` exactly as the
    authoritative path feeds ``pps_firing_utc``.
    """
    label_space_now = float(floor_offset_s) + float(mono_now)
    firing_space_now = label_space_now - float(chain_delay_ns) / 1e9
    return int(math.floor(firing_space_now))


# A coast moves the arrival-floor offset only at the residual rate --
# microseconds per hour, ~1 ms in a month.  An RTP re-base moves it by
# seconds.  Those are separated by orders of magnitude, so this
# threshold is not load-bearing; it just has to sit in the gap.
RULER_TOLERANCE_S = 0.050


def coast_ruler_intact(
    floor_offset_s: float,
    floor_offset_at_freeze_s: Optional[float],
    tolerance_s: float = RULER_TOLERANCE_S,
) -> bool:
    """Whether the counter the coast rests on is still the same one.

    ``utc_ns_at_rtp`` labels samples by counting from the anchor, so a
    radiod restart re-bases the counter underneath a frozen anchor and
    every label shifts.  The arrival-floor offset is precisely the
    quantity the coast consumes, so a jump in it is the most direct
    evidence available that the ruler changed -- no channel status, no
    published epoch (which is itself only good to ~400 ms, see
    ka9q-python#4), nothing else to go stale.
    """
    if floor_offset_at_freeze_s is None:
        return False
    return abs(
        float(floor_offset_s) - float(floor_offset_at_freeze_s)
    ) <= float(tolerance_s)


# What a coast on a NON-fine-stage anchor is worth.  The legacy coarse
# cascade re-derives an anchor during a T6 outage (tier "T5"), and that
# is a real anchor -- it asserts its chain delay from config rather than
# measuring it per edge, so it deserves a far wider claim than the fine
# stage's.  Same refuse-to-claim constant the bench uses with no floor
# at all: honest, and wide enough that chrony will never prefer it to a
# healthy FUSE.
COARSE_ANCHOR_SIGMA_NS = 25_000_000.0


def coast_sigma0_ns(
    floor_sigma_ns: float, captured_via_tier: Optional[str]
) -> float:
    """Sigma to start a coast from, given what produced the anchor.

    chrony adjudicates, so a coast stays AVAILABLE and states its worth
    rather than going dark -- going dark is us doing chrony's job badly,
    and it forces the watchdog to restart the recorder, which destroys
    the very anchor the coast needs.
    """
    if captured_via_tier == "T6":
        return float(floor_sigma_ns)
    return max(float(floor_sigma_ns), COARSE_ANCHOR_SIGMA_NS)
