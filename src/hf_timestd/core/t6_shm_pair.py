"""Build the (reference_time, system_time) pair chrony reads as HPPS.

A refclock sample is a claim about SIMULTANEITY: "when the host clock
read ``system_time``, true time was ``reference_time``".  Everything
here exists to make the second half of that sentence true.

``reference_time`` is easy — the anchor labels the TS-1 edge by pure
counter arithmetic and the named second follows.  ``system_time`` is the
hard half, because the edge is discovered in a sample that reached us
some time after it was created, and that latency is not constant.

The construction this module replaces read the wall clock at PUSH time
and backed off the exact RTP interval to the newest buffered sample:

    _sys_at_edge = _push_wall - _delta / _sr

The arithmetic is exact — the RTP counter is GPSDO-locked — but it is
anchored to the wrong instant.  ``_push_wall`` is read at push time
while the newest buffered sample ARRIVED earlier, so the construction
assumes ``host_time(rtp_buf[-1]) == _push_wall`` and the difference
survives whole into ``system_time``.  Measured on AC0G-B4 2026-08-16
that difference was 13-45 ms, it tracked push lateness with slope -1,
and it was the entire error chrony saw on HPPS while the same anchor
read through the bench was accurate to 0.8 ms.  See hf-timestd#18.

The fix is the filter the bench already applies: ``ArrivalFloorTracker``
(``t6_arrival_floor``) keeps the least-delayed arrival in a rolling
window, which is the honest monotonic->UTC map — latency is bounded
below by the physical path, and everything above that floor is queueing
to be discarded.  ``NativeAnchorBench.poll`` consumes it as
``utc = floor.offset_s + arrival_mono``; this module inverts the same
map to ask when, on the host clock, the edge occurred.

    mono_at_edge = edge_label_utc - floor.offset_s
    system_time  = mono_at_edge + (wall_now - mono_now)

``wall_now``/``mono_now`` must be sampled adjacently by the caller: the
pair is the CLOCK_MONOTONIC->CLOCK_REALTIME offset, and reading it is
the single sanctioned wall-clock observation at the facade boundary
(``docs/ARCHITECTURE-FIRST-PRINCIPLES.md`` §5).  Nothing here feeds back
into the anchor.

Precision is DERIVED, never asserted.  The push used to publish a
hardcoded ``-14`` (61 us) regardless of what the pair was actually
worth; against B4's measured 1.4 ms scatter that is a 26x overclaim, and
it is the origin of the ``+/- 55us`` column that has now caused three
separate misdiagnoses.  FUSE already does this correctly
(``multi_broadcast_fusion.py``), so HPPS uses the same derivation and
the two feeds stay comparable inside chrony's own selection maths.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from .t6_arrival_floor import FloorEstimate

# Publishable precision range, matching the FUSE feed: 2**-20 = 1 us,
# 2**-4 = 62 ms.  More negative is a better claim.
PRECISION_FLOOR = -20
PRECISION_CEILING = -4

# What we fall back to claiming when the floor has no estimate yet.  The
# same conservative transport bound the bench carries with no floor
# (``NativeAnchorBench.LATENCY_SIGMA_FLOOR_NS``): honest, wide enough to
# lose selection against any real source, and publishable.
FALLBACK_SIGMA_NS = 25_000_000.0


@dataclass(frozen=True)
class ShmPair:
    """The system_time half of a refclock sample, and what it is worth.

    ``source`` is ``"floor"`` for the corrected construction and
    ``"pushwall"`` when the caller's fallback was used — the caller
    warns on the latter, because it re-introduces the latency this
    module exists to remove.
    """

    system_time: float
    precision: int
    source: str
    sigma_ns: float


def precision_from_sigma_ns(sigma_ns: float) -> int:
    """log2(seconds), clamped — the derivation FUSE uses.

    ``int()`` truncates toward zero, so a negative exponent rounds to
    the WIDER claim.  That is the conservative direction and it keeps
    this identical to the FUSE feed.
    """
    sigma_s = max(float(sigma_ns), 1.0) / 1e9
    raw = int(math.log2(sigma_s))
    return max(PRECISION_FLOOR, min(PRECISION_CEILING, raw))


def t6_shm_system_time(
    edge_label_utc_s: float,
    floor: Optional[FloorEstimate],
    mono_now: float,
    wall_now: float,
    fallback_system_time: float,
) -> ShmPair:
    """Host-clock reading at the TS-1 edge, with an honest precision.

    ``edge_label_utc_s`` is the anchor's UTC label for the edge (the
    unrounded ``pps_firing_utc``), NOT the named second — inverting the
    label keeps our own sub-second residual inside the reported offset
    instead of hiding it.

    ``mono_now`` and ``wall_now`` must be read adjacently, and
    ``fallback_system_time`` is the caller's pre-existing value, used
    only when the floor has no estimate.
    """
    if floor is None:
        return ShmPair(
            system_time=float(fallback_system_time),
            precision=precision_from_sigma_ns(FALLBACK_SIGMA_NS),
            source="pushwall",
            sigma_ns=FALLBACK_SIGMA_NS,
        )

    mono_at_edge = float(edge_label_utc_s) - float(floor.offset_s)
    return ShmPair(
        system_time=mono_at_edge + (float(wall_now) - float(mono_now)),
        precision=precision_from_sigma_ns(floor.sigma_ns),
        source="floor",
        sigma_ns=float(floor.sigma_ns),
    )
