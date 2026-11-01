"""What T6's acquisition path accepted -- an edge, not an anchor.

Until v3.85 the external-reference acquisition path built a coarse
"namer" anchor at named second + placement.  The placement reads
radiod's host-clock tie at the edge, so that anchor copied the tie into
T6's registration and overwrote the authority's own anchor until the
next fold block.  Anchors now come only from
``T6AnchorAuthority._build_anchor`` (named second + asserted delay -
subsample); the acquisition path records what it accepted here and the
recorder latches its gate once the authority's anchor exists.
"""
from __future__ import annotations

from dataclasses import dataclass

__all__ = ["T6AcquisitionAcceptance"]


@dataclass(frozen=True)
class T6AcquisitionAcceptance:
    """What the acquisition path accepted: the edge, its named second, and
    where the accepting reference placed it in that second.

    ``placement_ns`` reads radiod's host-clock tie (or T4) at the edge --
    a placement, not a delay, and never an anchor.  The anchor itself
    comes only from ``T6AnchorAuthority._build_anchor``.
    """
    named_second_utc_ns: int
    edge_rtp: int
    placement_ns: int
    reference: str
