"""Where a stored chunk begins: on the anchor, or on the detected pulse.

Design: ``docs/design/ARCHIVE_BOUNDARY_ON_THE_EDGE.md``.

Today every chunk starts where ``binary_archive_writer`` puts it, derived from
radiod's ``GPS_TIME``/``RTP_TIMESNAP`` pair.  That pair does not update
atomically — B4 logged 83.952 ms of self-disagreement over 1.5 M updates on
2026-09-22, the known bound reaches 816 ms, and that same afternoon its
advertised epoch ran 320.793 ms wrong for hours.  So the boundary of every
archived file inherits an error we call a fault everywhere else.

The TS-1 pulse enters ahead of the RX888, carries no propagation, and cannot
move.  T6's shipped anchor inversion already says the edge registers the ruler
and the cascade only names the second; the archive boundary is the last
consumer still deriving its position from the anchor instead of reading it off
the edge.

⛔ This module DECIDES NOTHING BY DEFAULT.  ``use_pulse=False`` returns the
anchor's own value unchanged, so the writer stores byte-identical boundaries
to today while still reporting what the pulse would have said.  That shadow
number is the point of the first deployment: it measures the size of the
problem without altering a single stored file, and can falsify the whole
premise cheaply.

⚠ Wrap safety.  ``2**32 % 96000 == 23296``, so comparing two mod-rate phases
across a counter wrap jumps 23,296 samples with the physical edge unmoved —
the fault ``T6AnchorAuthority._period_deviation_samples`` documents having
suffered once per wrap.  So the distance here comes from a SIGNED 32-BIT
DELTA of the two counter values, never from two phases.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# The plane a pulse-placed boundary sits on.  The TS-1 injects ahead of the
# RX888, so the edge marks the injection point, not the antenna terminals.
PLANE_INJECTION = "ts1_injection_point"
PLANE_ANCHOR = "radiod_anchor"

SOURCE_ANCHOR = "anchor"
SOURCE_PULSE = "pulse"


def wrapped_signed32(delta: int) -> int:
    """A 32-bit counter difference as a signed value.

    The RTP counter wraps at 2**32.  Subtracting two raw values gives a
    number that may sit near +2**32 when the truth is a small negative step,
    so fold it into (-2**31, +2**31].
    """
    d = int(delta) & 0xFFFFFFFF
    return d - 0x100000000 if d >= 0x80000000 else d


@dataclass(frozen=True)
class BoundaryPlacement:
    """What the writer should use, and what the other method would have said."""
    chunk_boundary_rtp: int
    source: str                              # SOURCE_ANCHOR | SOURCE_PULSE
    plane: str                               # PLANE_ANCHOR | PLANE_INJECTION
    anchor_boundary_rtp: int
    pulse_boundary_rtp: Optional[int]
    # pulse minus anchor, in samples, wrap-safe and reduced to the nearest
    # second.  None when no edge was available.  THIS is the shadow number.
    shadow_delta_samples: Optional[int]
    chain_delay_ns: Optional[int]
    chain_delay_applied: bool = False        # ⛔ always False — see below
    reason: str = ""

    def sidecar_fields(self) -> dict:
        """The `timing` block's boundary fields, beside judge_tier.

        ⛔ ``chain_delay_applied`` stays False.  A correction folded into a
        stored boundary cannot be undone by a reader who disagrees with the
        calibration, and MEASUREMENT_MODEL.md treats a correction and a
        measurement as different objects.  The archive already keeps this
        rule for wall_times (``_bpsk_chain_delay_applied``); the boundary
        follows it.
        """
        d = {
            "boundary_source": self.source,
            "boundary_plane": self.plane,
            "boundary_rtp": int(self.chunk_boundary_rtp),
            "boundary_anchor_rtp": int(self.anchor_boundary_rtp),
            "boundary_chain_delay_applied": bool(self.chain_delay_applied),
        }
        if self.pulse_boundary_rtp is not None:
            d["boundary_pulse_rtp"] = int(self.pulse_boundary_rtp)
        if self.shadow_delta_samples is not None:
            d["boundary_pulse_minus_anchor_samples"] = int(self.shadow_delta_samples)
        if self.chain_delay_ns is not None:
            d["boundary_chain_delay_ns"] = int(self.chain_delay_ns)
        if self.reason:
            d["boundary_reason"] = self.reason
        return d


def place_boundary(anchor_boundary_rtp: int,
                   sample_rate: int,
                   *,
                   edge_rtp: Optional[int] = None,
                   use_pulse: bool = False,
                   chain_delay_ns: Optional[int] = None) -> BoundaryPlacement:
    """Decide where this chunk begins, and report what the other method said.

    Args:
        anchor_boundary_rtp: today's value, from the GPS_TIME/RTP_TIMESNAP
            mapping.  Returned unchanged whenever ``use_pulse`` is False.
        sample_rate: this CHANNEL's rate.  ⛔ The archived WWV channels run at
            24 kHz while the TS-1 channel runs at 96 kHz; the caller owns the
            conversion and must pass an ``edge_rtp`` already in THIS channel's
            counter space.
        edge_rtp: RTP timestamp of a detected pulse, any second.  None when no
            edge exists — on a station without a TS-1, or before acquisition.
        use_pulse: place the cut on the pulse.  Default False.
        chain_delay_ns: calibrated offset from the injection point to the
            antenna terminals.  Recorded, never applied.
    """
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    anchor = int(anchor_boundary_rtp) & 0xFFFFFFFF

    if edge_rtp is None:
        return BoundaryPlacement(
            chunk_boundary_rtp=anchor, source=SOURCE_ANCHOR, plane=PLANE_ANCHOR,
            anchor_boundary_rtp=anchor, pulse_boundary_rtp=None,
            shadow_delta_samples=None, chain_delay_ns=chain_delay_ns,
            reason="no pulse available")

    # Distance from the anchor's boundary to the nearest pulse, reduced to
    # within half a second either way.  Signed 32-bit first — see the module
    # note on 2**32 % 96000.
    d = wrapped_signed32(int(edge_rtp) - anchor)
    half = sample_rate // 2
    r = ((d + half) % sample_rate) - half
    pulse = (anchor + r) & 0xFFFFFFFF

    if not use_pulse:
        return BoundaryPlacement(
            chunk_boundary_rtp=anchor, source=SOURCE_ANCHOR, plane=PLANE_ANCHOR,
            anchor_boundary_rtp=anchor, pulse_boundary_rtp=pulse,
            shadow_delta_samples=r, chain_delay_ns=chain_delay_ns,
            reason="shadow: pulse computed, anchor used")

    return BoundaryPlacement(
        chunk_boundary_rtp=pulse, source=SOURCE_PULSE, plane=PLANE_INJECTION,
        anchor_boundary_rtp=anchor, pulse_boundary_rtp=pulse,
        shadow_delta_samples=r, chain_delay_ns=chain_delay_ns,
        reason="placed on the detected pulse")
