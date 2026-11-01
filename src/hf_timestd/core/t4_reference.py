"""May the host clock place T6's edge before T6 can place it itself?

T6 names its PPS edge's second through the coarse cascade and judges the
chain delay that edge implies against a ±250 ms physical bound.  Before
acquisition the only chain delay in hand reads the edge's phase in
radiod's RTP counter, so it means something only when that counter's
origin sits within a few milliseconds of UTC.  A radiod start can draw an
origin 0.3-0.5 s off (AC0G-ND, 2026-10-27 to 10-31: -462.6, +438.4,
-284.0, +473.4 ms), and with no T5 nothing else could say where the edge
fell.  ND ran on T4 for 4.5 days while its T4 held about 0.2 ms.

A host clock disciplined by a LAN GPS + PPS server can answer that one
question -- where in the second the edge fell, to tens of milliseconds --
because the T5 naming path already pairs the T6 stream's arrivals with
the host clock (``CoreRecorderV2._t6_name_second_via_nmea``).  This
module decides whether the host clock qualifies.  It answers yes or no
and gives the reason for a no; it corrects nothing.

What qualifies, and why each gate:

* **Tier T4.**  ``ChronyBench`` calls the host T4 only when chrony's
  selected source is a LAN peer or a local refclock; a WAN selection is
  T2 (~1-50 ms; K3LR's pool selection carries 36 ms of root delay),
  and T2 never qualifies.
* **Sigma at most 1 ms.**  ``ChronyBench`` states its sigma as
  root dispersion + root delay / 2 + |RMS offset|.  1 ms equals
  ``LabelPlaneTracker.DEFAULT_MAX_HOST_SIGMA_NS``, the bar the judge
  already sets for a host clock disciplined well enough to measure the
  T6 plane against.  One definition of "disciplined host" serves both
  uses.  A LAN server that relays WAN time carries its upstream's root
  delay and fails here even though its address looks local.
* **Stratum at most 2.**  The host sits one stratum below its source:
  1 for a local refclock, 2 for a LAN stratum-1 server.
* **Not circular.**  A refclock fed from the receiver's own samples
  (HPPS is T6, FUSE is T3) cannot vouch for T6 (MEASUREMENT_MODEL.md
  §7.1.1).
* **Fresh.**  The judge polls chrony every tick; a reading older than
  ``T4_REFERENCE_MAX_AGE_S`` describes a clock that may have moved.

The host clock enters only as the bootstrap reference CLAUDE.md allows
T4: it places one edge once, at acquisition, and the T6 authority's own
anchor supersedes the capture within a fold or two.
"""
from __future__ import annotations

from typing import Optional

from .label_plane import LabelPlaneTracker

__all__ = [
    "T4_REFERENCE_MAX_SIGMA_NS",
    "T4_REFERENCE_MAX_STRATUM",
    "T4_REFERENCE_MAX_AGE_S",
    "t4_reference_refusal",
]

#: The judge's own "disciplined host" bar (label_plane.py).
T4_REFERENCE_MAX_SIGMA_NS = LabelPlaneTracker.DEFAULT_MAX_HOST_SIGMA_NS

#: 1 = local refclock, 2 = a LAN stratum-1 server.
T4_REFERENCE_MAX_STRATUM = 2

#: The judge ticks every 10 s; six missed ticks means chrony stopped
#: answering.
T4_REFERENCE_MAX_AGE_S = 60.0

#: chrony refclock names that carry the receiver's own samples.
_SAMPLE_DERIVED_REFCLOCKS = ("HPPS", "FUSE")


def t4_reference_refusal(reading, mono_now: float) -> Optional[str]:
    """None when ``reading`` qualifies as a T4 reference, else the reason.

    ``reading`` is an ``offset_judge.BenchReading`` from ``ChronyBench``
    (or None when the judge has none).  The reason reads as a clause, so
    a caller can log it verbatim.
    """
    if reading is None:
        return "no host-clock reading from the offset judge"
    tier = getattr(reading, "tier", None)
    if tier != "T4":
        return (f"host clock is {tier}, not T4 (chrony's selected source "
                f"is not a LAN peer or local refclock)")
    try:
        age = float(mono_now) - float(reading.mono)
        sigma_ns = float(reading.sigma_ns)
    except (TypeError, ValueError, AttributeError):
        return "host-clock reading is malformed"
    if age > T4_REFERENCE_MAX_AGE_S:
        return f"host-clock reading is {age:.0f} s old"
    if not (sigma_ns <= T4_REFERENCE_MAX_SIGMA_NS):
        return (f"host-clock sigma {sigma_ns / 1e6:.3f} ms exceeds "
                f"{T4_REFERENCE_MAX_SIGMA_NS / 1e6:.3f} ms")
    detail = getattr(reading, "detail", None) or {}
    stratum = detail.get("stratum")
    if isinstance(stratum, int) and stratum > T4_REFERENCE_MAX_STRATUM:
        return (f"host stratum {stratum}: not disciplined by a stratum-1 "
                f"source")
    ref_id = str(detail.get("reference_id") or "").upper()
    for name in _SAMPLE_DERIVED_REFCLOCKS:
        if name in ref_id:
            return (f"chrony follows {name}, a refclock recovered from the "
                    f"receiver's own samples; it cannot vouch for T6")
    return None
