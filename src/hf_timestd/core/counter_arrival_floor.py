"""Least-delayed-arrival filter for one archive channel's RTP counter.

The T3 bench (``offset_judge.HfAcquiredBench``) projects the acquired
registration onto an arrived sample and hands that label to the monotonic
clock at the arrival instant, as the T6 bench does.  The label of an arrived
sample sits behind true UTC by however long the sample took to reach us, and
T6's bench removes that with ``ArrivalFloorTracker``.  T3's bench took the
latest arrival instead, so its reading carried the delivery latency: on
AC0G-B4 (shadow record 2026-10-31/11-01) the judge's T3-vs-T6 residual ran
below the direct label comparison by a median 6.6 ms, p25 11.7, p5 18, on
every channel.

The registration moves every minute (corroboration), so this filter cannot
track ``label_utc - mono`` the way T6's does against a fixed anchor.  It
tracks the counter instead: ``counter_s - mono``, where ``counter_s`` is the
channel's RTP counter unwrapped from its first arrival, in seconds.  The
largest such offset in the window marks the least-delayed arrival, exactly
as in ``t6_arrival_floor``, whose tracker does the filtering here too.  The
bench then asks how far that floor puts the counter ahead of the arrival it
projects: the lead, to add to the arrival's label.

Thread use: the recorder's tap notes arrivals on the stream thread while
the judge reads on its own tick, so a lock guards the tracker.
"""
from __future__ import annotations

import threading
from typing import Optional, Tuple

from .t6_arrival_floor import ArrivalFloorTracker, FloorEstimate

_WRAP = 1 << 32


def _delta_signed(rtp: int, ref: int) -> int:
    """Signed 32-bit counter difference ``rtp - ref``."""
    d = (int(rtp) - int(ref)) & 0xFFFFFFFF
    return d - _WRAP if d > 0x7FFFFFFF else d


class CounterArrivalFloor:
    """Rolling least-delayed arrival of one channel's RTP counter."""

    # An offset step this large between consecutive arrivals is no
    # latency: radiod renumbered the counter (a restart), or the stream
    # stopped and resumed somewhere else.  Delivery latency on B4 sweeps
    # 0..19.2 ms; a stall that buffers a second of packets is already a
    # fault the stream-stall hooks report.
    DISCONTINUITY_S = 1.0

    def __init__(self, sample_rate: int, window_s: float = 2.0):
        self.sample_rate = int(sample_rate)
        self._tracker = ArrivalFloorTracker(window_s=window_s)
        self._lock = threading.Lock()
        self._last_rtp: Optional[int] = None
        self._unwrapped = 0  # samples from the first arrival
        self._last_offset: Optional[float] = None

    def note(self, rtp: int, mono: float) -> None:
        """Record one delivered batch: its newest sample's RTP and the
        monotonic instant it arrived."""
        with self._lock:
            if self._last_rtp is None:
                self._unwrapped = 0
            else:
                self._unwrapped += _delta_signed(rtp, self._last_rtp)
            self._last_rtp = int(rtp) & 0xFFFFFFFF
            offset = self._unwrapped / self.sample_rate - float(mono)
            if (self._last_offset is not None
                    and abs(offset - self._last_offset) > self.DISCONTINUITY_S):
                # A new counter: the old frame's arrivals mean nothing in it.
                self._tracker.reset("counter discontinuity")
                self._unwrapped = 0
                offset = -float(mono)
            self._last_offset = offset
            self._tracker.note(offset, float(mono))

    def lead(self, arrival_rtp: int, arrival_mono: float, mono_now: float
             ) -> Optional[Tuple[float, FloorEstimate]]:
        """``(lead_s, estimate)``: how far the window's least-delayed
        arrival puts the counter ahead of ``arrival_rtp`` at
        ``arrival_mono``, and the tracker's estimate (its sigma measured
        from the floor's own history, ``ArrivalFloorTracker``).  None while
        the window is empty.  Records the estimate in the sigma history:
        call it at the judge's tick only."""
        with self._lock:
            if self._last_rtp is None:
                return None
            est = self._tracker.estimate(float(mono_now), record=True)
            if est is None:
                return None
            counter_s = (self._unwrapped
                         + _delta_signed(arrival_rtp, self._last_rtp)) / self.sample_rate
            return float(arrival_mono) + est.offset_s - counter_s, est
