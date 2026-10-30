"""T3 against T6 in the sample plane (T3 foundations, task 14).

On a station with a TS-1, T6 serves as T3's primary reference, because both
live in the sample plane.  They live in different counters, though.  T3's
registration names the UTC of one sample in one 24 kHz channel's RTP counter
(``rtp_ref``, ``utc_ref``).  T6's native anchor names the UTC of one sample in
the T6 channel's 96 kHz counter (``anchor_rtp``, ``anchor_utc_ns``).  Every
radiod channel starts its own counter at its own input block, so the two RTP
values never compare directly.

The comparator relates them only through each channel's ``CounterMap``, which
maps that channel's RTP to radiod's input-sample index, a coordinate every
channel of one radiod run shares.  It reads no clock and no host offset, and
it feeds nothing: the judge publishes the answer for people to read.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Optional

from hamsci_dsp.counter_map import CounterMap

from .counter_epoch_tracker import COUNTER_EPOCH_SKEW_ALLOWANCE_S
from .native_anchor import NativeAnchor
from .registration_acquirer import COUNTER_MAP_REACH, _rtp_gap


class T3ShadowComparator:
    """Pure arithmetic: T3's label minus T6's label at one input sample."""

    @staticmethod
    def _index_near(cmap: CounterMap, rtp: int) -> Optional[float]:
        """``rtp``'s input-sample index through ``cmap``, or None when ``rtp``
        lies beyond the map's reach.  A map read past 2**31 samples aliases
        by a whole counter wrap, so the comparator refuses well short of it
        rather than publish a number off by hours."""
        rtp = int(rtp) & 0xFFFFFFFF
        if _rtp_gap(rtp, cmap.rtp_ref) > COUNTER_MAP_REACH:
            return None
        return cmap.rolled_forward(rtp).input_index(rtp)

    @staticmethod
    def _one_run(t3_map: CounterMap, t6_map: CounterMap) -> bool:
        """Both maps come from one radiod run.

        ``same_run`` also demands equal ``olen``, which a 24 kHz and a 96 kHz
        channel never share, so the T6 map borrows T3's ``olen`` for the
        check alone.  The tolerance is ``COUNTER_EPOCH_SKEW_ALLOWANCE_S``
        (2 s), not the default 0.5 s: radiod's GPS_TIME arrives up to 816 ms
        late on B4, and a late packet must not split one run in two."""
        return replace(t6_map, olen=t3_map.olen).same_run(
            t3_map, tol_s=COUNTER_EPOCH_SKEW_ALLOWANCE_S)

    @staticmethod
    def _t6_index(t6_anchor: Optional[NativeAnchor],
                  t6_map: Optional[CounterMap]) -> Optional[float]:
        """The input-sample index of T6's anchor sample, or None."""
        if t6_anchor is None or t6_map is None:
            return None
        # The anchor ticks at its own rate; the map must describe that same
        # counter (a 24 kHz anchor through a 96 kHz map lands elsewhere).
        if (int(t6_anchor.sample_rate_hz) * t6_map.ilen
                != t6_map.olen * t6_map.input_samprate):
            return None
        return T3ShadowComparator._index_near(t6_map, t6_anchor.anchor_rtp)

    @staticmethod
    def t3_minus_t6_ms(summary: Optional[dict], t6_anchor: Optional[NativeAnchor],
                       t6_map: Optional[CounterMap]) -> Optional[float]:
        """T3's UTC label minus T6's UTC label for the same input sample, in ms.

        Positive when T3 labels the sample LATER than T6 does; negative when
        T3 labels it earlier.

        The arithmetic, in words: the T3 summary's own counter map turns
        ``rtp_ref`` into an input-sample index.  The T6 map turns the T6
        anchor's ``anchor_rtp`` into another.  The difference of those two
        indices, divided by radiod's input sample rate, carries T6's anchor
        UTC forward to T3's sample, exactly as ``utc_ns_at_rtp`` carries it
        along T6's own counter: at the nominal, GPSDO-disciplined rate, with
        no rate correction.  T3's ``utc_ref`` minus that UTC gives the answer.

        The T6 chain delay stays out: ``anchor_utc_ns`` already labels the
        sample, and whether the HF path carries the same delay remains an
        open question for Stage 0.  The judge publishes the delay beside
        this number, raw.

        None when either side lacks a map or an anchor, when the maps come
        from different radiod runs, when the anchor's rate or the summary's
        ``sample_rate`` does not match its map, or when either RTP lies beyond its map's reach.
        """
        if not summary:
            return None
        try:
            rtp_ref = int(summary["rtp_ref"])
            utc_ref = float(summary["utc_ref"])
        except (KeyError, TypeError, ValueError):
            return None
        t3_map = CounterMap.from_dict(summary.get("counter_map"))
        if t3_map is None or t6_map is None or t6_anchor is None:
            return None
        # The summary's rtp_ref ticks at its own rate; its map must describe
        # that counter, as the T6 anchor's must describe T6's.
        try:
            t3_rate = int(summary["sample_rate"])
        except (KeyError, TypeError, ValueError):
            return None
        if t3_rate * t3_map.ilen != t3_map.olen * t3_map.input_samprate:
            return None
        if not T3ShadowComparator._one_run(t3_map, t6_map):
            return None
        idx3 = T3ShadowComparator._index_near(t3_map, rtp_ref)
        idx6 = T3ShadowComparator._t6_index(t6_anchor, t6_map)
        if idx3 is None or idx6 is None:
            return None
        # Split the anchor's ns into whole seconds and a remainder, so the
        # subtraction from utc_ref runs between nearby numbers and keeps
        # sub-microsecond precision in float.
        whole_s, rem_ns = divmod(int(t6_anchor.anchor_utc_ns), 10**9)
        diff_s = ((utc_ref - whole_s) - rem_ns / 1e9
                  - (idx3 - idx6) / t3_map.input_samprate)
        return diff_s * 1e3

    @staticmethod
    def block(summary: Optional[dict], t6_anchor: Optional[NativeAnchor],
              t6_map: Optional[CounterMap],
              chain_delay_ms: Optional[float]) -> dict:
        """The ``t3_shadow`` block of ``offset_judge.json``.

        ``t3_minus_t6_ms`` and ``t6_chain_delay_ms`` stand side by side,
        both raw; nothing here subtracts one from the other."""
        s = summary or {}
        diff = T3ShadowComparator.t3_minus_t6_ms(summary, t6_anchor, t6_map)
        return {
            "t3_minus_t6_ms": diff,
            "t6_chain_delay_ms": (float(chain_delay_ms)
                                  if chain_delay_ms is not None else None),
            # Only beside an answer: across runs, out of reach or at the
            # wrong rate, the index means nothing and must not read as data.
            "t6_anchor_input_index": (
                T3ShadowComparator._t6_index(t6_anchor, t6_map)
                if diff is not None else None),
            "t6_anchor_utc_ns": (int(t6_anchor.anchor_utc_ns)
                                 if t6_anchor is not None else None),
            "t3_rtp_domain": s.get("rtp_domain"),
            "t3_state": s.get("state"),
            "t3_sigma_ms": s.get("sigma_ms"),
        }
