"""Relate one radiod channel's RTP counter to another's.

hf-timestd#42 wants tick arrival measured against the T6 PPS edge in the
same recording rather than against a labelled minute boundary, so that
the anchor's absolute error cancels.  The ticks and the PPS live in
DIFFERENT channels, so this module answers the precondition: what is the
constant relating two channels' counters?

MEASURED ON AC0G-B4 2026-08-25 (six sidecars, one minute_boundary):

* The six 24 kHz metrology channels share ONE counter space.  Their
  implied epochs (``gps_time - rtp_timesnap/fs``) agree to **1.937 ms** —
  which is the (GPS_TIME, RTP_TIMESNAP) non-atomicity, not an origin
  difference.  Randomised per-channel origins would differ by hours.
* The T6 channel (96 kHz) does NOT simply scale.  Against WWV_20000 the
  residual after removing one 2**32 wrap is 362,095,021 samples ≈ 3772 s
  — neither zero nor a wrap multiple.

⇒ Same-rate sibling channels can be related by counter arithmetic alone.
T6 cannot: it carries a distinct offset that must be MEASURED.

## Why a floor, not a mean

The only cross-channel evidence radiod offers is the
``(GPS_TIME, RTP_TIMESNAP)`` pair, and that pair is read at status
EMISSION: ``GPS_TIME`` live, ``RTP_TIMESNAP`` cached.  Its error is
therefore one-sided LATENESS, never earliness — B4 measured 20.8 M
updates with ~37 % at exactly 0 disagreement and a tail to +816 ms.

Averaging a one-sided error converges to the wrong number.  The
least-late observation is the truest one, so the epoch estimate is a
running MINIMUM — the same reasoning ``t6_arrival_floor`` applies to
sample arrivals, and the reason this is usable at all despite ms-class
pair noise: the offset is CONSTANT within a radiod session (both
counters derive from the same ADC clock), so more observations only
sharpen it.

⚠ This is Class A evidence (``TIMING_AUTHORITY_TWO_AXIS.md``) and is used
here for exactly what Class A is entitled to do: relate two counter
spaces.  It never touches a sub-second placement.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

_TWO32 = 1 << 32


@dataclass(frozen=True)
class PairObservation:
    """One radiod ``(GPS_TIME, RTP_TIMESNAP)`` snapshot for one channel."""

    gps_time_ns: int
    rtp_timesnap: int
    sample_rate_hz: int


def epoch_offset_s(obs: PairObservation) -> float:
    """The channel's implied counter epoch: ``utc - rtp/fs``.

    Biased LATE by whatever the pair snapshot was late by; never early.
    """
    return (
        float(obs.gps_time_ns) / 1e9
        - float(obs.rtp_timesnap) / float(obs.sample_rate_hz)
    )


class ChannelEpoch:
    """Running least-late estimate of one channel's counter epoch."""

    def __init__(self) -> None:
        self._min: Optional[float] = None
        self.n = 0

    def observe(self, obs: PairObservation) -> None:
        e = epoch_offset_s(obs)
        self.n += 1
        if self._min is None or e < self._min:
            self._min = e

    @property
    def epoch_s(self) -> Optional[float]:
        return self._min


def rtp_in_other_channel(
    rtp: int,
    src_epoch_s: float,
    src_rate_hz: int,
    dst_epoch_s: float,
    dst_rate_hz: int,
    near_utc_s: float,
) -> int:
    """Map ``rtp`` from the source channel's counter into the target's.

    ``near_utc_s`` resolves the 2**32 ambiguity: a counter only fixes UTC
    modulo ``2**32 / fs`` (49.7 h at 24 kHz, 12.4 h at 96 kHz), so the
    caller must say roughly when the sample was — seconds of accuracy is
    ample against a window of hours.
    """
    period_s = _TWO32 / float(src_rate_hz)
    utc = src_epoch_s + (int(rtp) & 0xFFFFFFFF) / float(src_rate_hz)
    # lift into the wrap epoch containing near_utc_s
    utc += round((float(near_utc_s) - utc) / period_s) * period_s
    return int(round((utc - dst_epoch_s) * float(dst_rate_hz))) & 0xFFFFFFFF


def same_counter_space(
    a: PairObservation, b: PairObservation, tolerance_s: float = 0.010
) -> bool:
    """Whether two channels' epochs agree within the pair noise.

    True for sibling channels created together (B4: 1.937 ms across six);
    False for a channel carrying its own origin, such as T6 against the
    metrology set.  The default tolerance sits above the observed
    non-atomicity and far below any real origin difference.
    """
    return abs(epoch_offset_s(a) - epoch_offset_s(b)) <= float(tolerance_s)
