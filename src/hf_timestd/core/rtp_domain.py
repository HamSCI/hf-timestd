"""One continuous RTP counter domain for the T6 signal chain.

The RTP counter is 32-bit and ``2**32`` is not a multiple of any of our
sample rates (``2**32 % 96000 == 23296``), so the mod-sample-rate PHASE
of the masked counter jumps by ``2**32 % sr`` samples at every counter
wrap — 23,296 samples = 242.667 ms at 96 kHz, once per ~12 h 26 m of
channel lifetime.  Every place that compares such phases (MF edge
tracking, the fine/coarse cross-check, chain-delay disambiguation and
its persisted store) breaks at the wrap unless all of them live in ONE
continuous counter domain.

Observed on AC0G-B4 2026-08-23 23:33Z and again 2026-08-24 06:50Z: the
wrap knocked T6 to DEGRADED→UNLOCKED and every re-disambiguation then
landed 242.7 ms out (16.6 + 242.7 ≈ 257 ms > the ±250 ms plausibility
guard), wedging re-acquisition until an operator restart re-based the
counter.

``RtpUnwrapper`` is that domain's single entry point: feed it every
declared batch RTP in arrival order and it returns a monotonic-ish
64-bit extension.  Each component that does phase arithmetic owns one
(created in ``__init__``), and its state deliberately SURVIVES the
component's ``reset()``: two components watching the same stream must
agree on the epoch forever, and a re-lock after ``reset()`` must land
in the same domain as the lock it replaces — that re-lock landing one
wrap out is exactly the B4 wedge.
"""

from __future__ import annotations

_WRAP = 1 << 32
_MASK = 0xFFFFFFFF


def wrapped_signed32(delta: int) -> int:
    """Map a mod-2^32 difference to signed [-2^31, 2^31)."""
    d = int(delta) & _MASK
    return d - _WRAP if d >= (1 << 31) else d


class RtpUnwrapper:
    """Extend the 32-bit RTP counter to a continuous 64-bit domain.

    Each value is interpreted as the representative nearest the
    previous one (signed 32-bit fold), which tolerates the measured
    ±60-sample label wobble and any forward gap under ~2^31 samples.
    A genuine counter re-base still appears as a large jump in the
    output — downstream restart detection keeps its meaning; nothing
    is hidden, only the artificial 2^32 seam is removed.
    """

    def __init__(self) -> None:
        self._last: int | None = None

    def unwrap(self, rtp: int) -> int:
        v = int(rtp) & _MASK
        if self._last is None:
            self._last = v
        else:
            self._last = self._last + wrapped_signed32(v - self._last)
        return self._last
