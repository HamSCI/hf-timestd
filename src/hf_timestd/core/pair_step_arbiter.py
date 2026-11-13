"""Decide whether a radiod (gps_time, rtp_timesnap) pair that disagrees with
the recorder's mapping may be adopted.

radiod's status stream sometimes delivers a torn pair: a gps_time and an
rtp_timesnap read at different moments.  Re-basing the sample-to-UTC mapping
on one such pair leaves it wrong by a fraction of a second.  This module holds
the pure decision, with no I/O and no clock.

Where the T6 tier (a GPSDO-driven pilot measured in the sample stream) is
authoritative, the GPSDO decides what it can vouch for: it refuses a pair when
it agrees with the current mapping, and adopts one when it agrees with the
pair and not the mapping.  When it agrees with neither (radiod restarted and
its GPS_TIME arrived late, say), it vouches for nothing and the step falls to
the persistence rule.  Without T6 a step must persist across two checks with
the same counter offset, so a one-off torn read never lands.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Optional

from hamsci_dsp.counter_map import CounterMap

from .counter_epoch_tracker import (COUNTER_EPOCH_SKEW_ALLOWANCE_S,
                                    map_fits_raw_pair)
from .native_anchor import NativeAnchor
from .t3_shadow_comparator import t6_anchor_input_index

# A pair within this of the mapping is ordinary status jitter.
STEADY_LIMIT_S = 0.75
# How near the GPSDO a pair or a mapping must sit to count as agreeing.
T6_AGREE_S = 0.25
# How near two consecutive pending offsets must sit to count as one step.
# radiod's GPS_TIME arrives late by a one-sided amount (up to ~0.8 s on B4),
# so two honest pairs from one run can differ by a few hundred ms; torn
# reads differ by 0.45-1 s (2026-11-13 final review, I2).
PERSIST_AGREE_S = 0.2

_RTP_WRAP = 2 ** 32


@dataclass(frozen=True)
class T6Reference:
    """T6's native anchor with the counter map of the channel that carries it."""
    anchor: NativeAnchor
    t6_map: CounterMap


class Verdict(enum.Enum):
    STEADY = "steady"          # within jitter; leave the mapping alone
    ADOPT = "adopt"            # re-base on the fresh pair
    HOLD = "hold"              # unconfirmed; keep the mapping, wait a tick
    REFUSE_T6 = "refuse_t6"    # the GPSDO contradicts the fresh pair


def pair_disagreement_s(gps_time_ns: int, rtp_timesnap: int,
                        ref_gps_time_ns: int, ref_rtp_timesnap: int,
                        sample_rate: int) -> float:
    """Fresh pair's UTC minus the UTC the reference pair implies at the same
    counter value, in seconds.  Both pairs are GPS ns, so no leap-second
    conversion enters.  The RTP delta is signed and wrap-safe, as in
    ``BinaryArchiveWriter.evaluate_pair``."""
    delta = int((int(rtp_timesnap) - int(ref_rtp_timesnap)) & 0xFFFFFFFF)
    if delta > 0x7FFFFFFF:
        delta -= 0x100000000
    implied = int(ref_gps_time_ns) / 1e9 + delta / sample_rate
    return int(gps_time_ns) / 1e9 - implied


def t6_predicted_utc_s(rtp: int, chan_map: Optional[CounterMap],
                       ref: Optional[T6Reference]) -> Optional[float]:
    """UTC of channel sample ``rtp`` according to the GPSDO, or None when
    either map is missing, T6's anchor has no index, or the two maps name
    different radiod runs."""
    if chan_map is None or ref is None or ref.t6_map is None:
        return None
    t6_index = t6_anchor_input_index(ref.anchor, ref.t6_map)
    if t6_index is None:
        return None
    if (abs(chan_map.input_epoch_gps_s - ref.t6_map.input_epoch_gps_s)
            > COUNTER_EPOCH_SKEW_ALLOWANCE_S):
        return None
    chan_index = chan_map.input_index(rtp)
    return (ref.anchor.anchor_utc_ns / 1e9
            + (chan_index - t6_index) / ref.t6_map.input_samprate)


def t6_anchor_in_map_run(ref: Optional[T6Reference],
                         utc_minus_gps_s: float) -> bool:
    """Does T6's anchor belong to the radiod run its counter map describes?

    The anchor is itself an (RTP, UTC) pair, so it passes through
    ``map_fits_raw_pair`` like any raw radiod pair once its UTC is put back
    on the GPS timescale (``utc_minus_gps_s`` is ``315964800 - leap``).
    Right after a radiod restart the map may be refreshed while the anchor
    still comes from the old run; the two then sit hours apart."""
    if ref is None or ref.anchor is None or ref.t6_map is None:
        return False
    anchor_gps_ns = (int(ref.anchor.anchor_utc_ns)
                     - int(round(utc_minus_gps_s * 1e9)))
    return map_fits_raw_pair(ref.t6_map, anchor_gps_ns,
                             int(ref.anchor.anchor_rtp))


def t6_contradicts_both(fresh_utc_s: float, implied_utc_s: float,
                        t6_utc_s: Optional[float]) -> bool:
    """T6 agrees with neither the fresh pair nor the current mapping."""
    return (t6_utc_s is not None
            and abs(fresh_utc_s - t6_utc_s) > T6_AGREE_S
            and abs(implied_utc_s - t6_utc_s) > T6_AGREE_S)


class PairStepArbiter:
    """Remembers at most one pending step between checks."""

    def __init__(self, sample_rate: int) -> None:
        self._sample_rate = int(sample_rate)
        self._pending_offset_s: Optional[float] = None

    def reset(self) -> None:
        self._pending_offset_s = None

    def _offsets_agree(self, a: float, b: float) -> bool:
        """Equal within PERSIST_AGREE_S modulo one RTP wrap period."""
        period = _RTP_WRAP / self._sample_rate
        d = (a - b) % period
        return min(d, period - d) <= PERSIST_AGREE_S

    def decide(self, diff_s: float, fresh_utc_s: float, implied_utc_s: float,
               fresh_offset_s: float, t6_utc_s: Optional[float]) -> Verdict:
        """Rules, in order:

        1. ``|diff_s| <= STEADY_LIMIT_S``: STEADY; clear any pending step.
        2. T6 present and within ``T6_AGREE_S`` of the current mapping:
           REFUSE_T6.  Else T6 within ``T6_AGREE_S`` of the fresh pair:
           ADOPT.  Either way clear any pending step.  T6 present but
           agreeing with neither: fall through to rule 3.
        3. Nothing pending: store ``fresh_offset_s`` and HOLD.
        4. A step pending: ADOPT (and clear it) when the two offsets agree
           within ``PERSIST_AGREE_S``, wrap-safe; else replace it and HOLD.
        """
        if abs(diff_s) <= STEADY_LIMIT_S:
            self._pending_offset_s = None
            return Verdict.STEADY
        if t6_utc_s is not None:
            if abs(implied_utc_s - t6_utc_s) <= T6_AGREE_S:
                # The GPSDO vouches for the current mapping.
                self._pending_offset_s = None
                return Verdict.REFUSE_T6
            if abs(fresh_utc_s - t6_utc_s) <= T6_AGREE_S:
                # The GPSDO vouches for the fresh pair.
                self._pending_offset_s = None
                return Verdict.ADOPT
            # The GPSDO contradicts both: it vouches for nothing, so the
            # step must persist like any other (the caller logs ERROR).
        pending = self._pending_offset_s
        if pending is not None and self._offsets_agree(pending, fresh_offset_s):
            self._pending_offset_s = None
            return Verdict.ADOPT
        self._pending_offset_s = fresh_offset_s
        return Verdict.HOLD
