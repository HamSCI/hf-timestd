"""CounterEpochTracker — the live-ring twin of the recorder's
``binary_archive_writer._note_counter_epoch``.

radiod's (GPS_TIME, RTP_TIMESNAP) pair is re-adopted at every radiod or
recorder restart.  Within one counter space every pair implies the same
RTP→UTC mapping to within the pair's own skew; a jump past that means the
RTP counter space itself changed, and every registration held in the old
RTP frame is void (T3 self-registration spec §5, "step on counter-epoch
change").

This tracker exists because the live ring (metrology_engine) needs the
same open/keep decision the recorder's archive writer already makes,
so an ``acquired`` origin (BufferTiming.origin_source) is discarded the
instant the epoch it was registered in closes.

**The epoch is a MEASURED QUANTITY, not a name** (final review, C1).
``epoch_offset_s`` — ``gps_time_ns/1e9 − rtp_timesnap/sample_rate``, the
UTC of RTP sample 0 in THIS channel's counter.  Channels on one radiod do
not share an origin: since ka9q-radio e5f790dd each starts its own RTP
counter (ND, 2026-10-06: one 24 kHz channel 20 ms off the rest), so two
channels' offsets need not agree, and ``registration_store`` fusion no
longer clusters by this value; it converts through each channel's counter
map instead (Task 6).  The id string is informational: metrology runs one process per channel, each
tracker samples the ring's anchor at its own phase, so two channels in one
physical epoch can name it differently and nothing downstream may depend
on the strings matching.

**Lateness is not a new epoch** (final review, I3).  radiod samples
GPS_TIME live and RTP_TIMESNAP from a cached block field, so consecutive
pairs are independent draws from a ONE-SIDED lateness distribution — 232 ms
on AC0G-ND, 701 ms with a tail to 816 ms on AC0G-B4 (spec §1).  Predicting
from the LAST pair therefore opened a spurious epoch on B4 roughly whenever
a late sample followed an early one, and every spurious epoch throws the
registration away.  This tracker keeps a RUNNING MINIMUM of the implied
offset instead: skew only makes GPS_TIME late, so the least-late pair seen
is the truest mapping.  A new epoch opens when a pair implies an offset
BELOW that minimum by more than ``COUNTER_EPOCH_STEP_S`` (impossible within
one epoch) or ABOVE it by more than ``COUNTER_EPOCH_SKEW_ALLOWANCE_S``.

Two consequences worth stating plainly:

* **The recorder's own tracker is unchanged.**  ``binary_archive_writer``
  still predicts from the last adopted pair and still names its epochs
  ``pair-<gps>``, where this one names them ``ep-<int(offset_s)>``.  The
  two also observe different subsamples of the pair stream (per-adoption
  there, per-minute here), so their boundaries can fall at different times
  and their ids never match.  Do not try to correlate
  ``registration.counter_epoch_id`` with a chunk sidecar's.
* **A genuine re-anchor smaller than ``COUNTER_EPOCH_SKEW_ALLOWANCE_S``
  would be missed.**  Real re-anchors move the mapping by hours; the
  allowance buys immunity to a 0.8 s skew at the cost of blindness to a
  hypothetical sub-2-second one.

The prediction arithmetic — including RTP wrap handling, which is why the
running minimum is carried as a PAIR rather than as a bare float — is the
recorder's.
"""

from __future__ import annotations

from typing import Optional, Tuple

# Same value as binary_archive_writer.COUNTER_EPOCH_STEP_S — pinned equal
# by test_counter_epoch_tracker.test_step_constant_matches_the_recorder.
COUNTER_EPOCH_STEP_S = 0.5
# How much LATER than the running minimum a pair may imply before the
# tracker calls it a new counter space.  B4's measured pair skew reaches
# 816 ms and the distribution is one-sided, so the bound must clear that
# with margin; real re-anchors move by hours (final review, I3).
COUNTER_EPOCH_SKEW_ALLOWANCE_S = 2.0


def counter_map_run_mismatch_s(cmap, raw_gps_time_ns: int,
                               raw_rtp_timesnap: int) -> float:
    """How far, in seconds, radiod's raw pair puts its run's epoch from
    ``cmap``'s ``input_epoch_gps_s``.  Zero for one run; hours for two.

    The raw pair's own epoch is ``gps_s - input_index(rtp) / f_in``, the
    quantity the map's epoch names.  RTP is a 32-bit counter, and a
    steady raw pair is never refreshed, so ``input_index`` may read the
    pair's RTP one wrap away; the difference is taken modulo that wrap
    (``2**32`` output samples), which cannot hide a second run unless it
    started a whole number of wraps (49.7 h at 24 kHz) after the first,
    to within the tolerance.  Final review I-4, 2026-10-30."""
    wrap_s = 2**32 * cmap.ilen / cmap.olen / cmap.input_samprate
    implied = (int(raw_gps_time_ns) / 1e9
               - cmap.input_index(int(raw_rtp_timesnap)) / cmap.input_samprate)
    d = implied - cmap.input_epoch_gps_s
    return (d + wrap_s / 2.0) % wrap_s - wrap_s / 2.0


def map_fits_raw_pair(cmap, raw_gps_time_ns, raw_rtp_timesnap) -> bool:
    """Do ``cmap`` and radiod's raw pair name one radiod run (within
    ``COUNTER_EPOCH_SKEW_ALLOWANCE_S``)?  False when either is missing."""
    if cmap is None or raw_gps_time_ns is None or raw_rtp_timesnap is None:
        return False
    return (abs(counter_map_run_mismatch_s(cmap, raw_gps_time_ns,
                                           raw_rtp_timesnap))
            <= COUNTER_EPOCH_SKEW_ALLOWANCE_S)


class CounterEpochTracker:
    """Live-ring twin of ``binary_archive_writer``'s per-channel epoch state."""

    def __init__(self) -> None:
        self._id: Optional[str] = None
        # (gps_time_ns, rtp_timesnap, sample_rate) of the LEAST-LATE pair
        # seen in this epoch — the running minimum of the implied offset.
        self._pair: Optional[Tuple[int, int, int]] = None

    @property
    def epoch_id(self) -> str:
        return self._id or "unregistered"

    @property
    def epoch_offset_s(self) -> float:
        """UTC of RTP sample 0 under this epoch's least-late pair, i.e.
        ``gps_time_ns/1e9 − rtp_timesnap/sample_rate``.

        NaN before any valid pair.  This is the quantity two processes on
        one radiod agree on; ``epoch_id`` is not (C1)."""
        if self._pair is None:
            return float("nan")
        gps, rtp, sr = self._pair
        return gps / 1e9 - rtp / sr

    def observe(self, gps_time_ns: int, rtp_timesnap: int, sample_rate: int) -> str:
        """Adopt a new (GPS_TIME, RTP_TIMESNAP) pair.

        Keeps the current epoch while the new pair's implied offset sits
        within ``COUNTER_EPOCH_STEP_S`` below, or
        ``COUNTER_EPOCH_SKEW_ALLOWANCE_S`` above, the running minimum
        already in force; otherwise opens a new epoch, named for the
        implied offset of its first pair.  Returns the (possibly new)
        epoch id.

        A malformed pair (``None``, or anything that doesn't convert to
        ``int`` -- a present-but-null metadata field, say) is not adopted;
        the tracker leaves the epoch it already holds unchanged and returns
        that id rather than raising (review I1: a live-ring caller must
        never wedge on a bad pair).  A non-positive sample rate is
        malformed in the same sense -- it would divide by zero below.
        """
        try:
            gps = int(gps_time_ns)
            rtp = int(rtp_timesnap)
            sr = int(sample_rate)
        except (TypeError, ValueError):
            return self.epoch_id
        if sr <= 0:
            return self.epoch_id
        prev = self._pair
        if prev is not None:
            p_gps, p_rtp, p_sr = prev
            # Wrap-safe RTP delta, the recorder's arithmetic verbatim.
            delta = (rtp - p_rtp) & 0xFFFFFFFF
            if delta > 0x7FFFFFFF:
                delta -= 0x1_0000_0000
            predicted_ns = p_gps + 1_000_000_000 * delta // p_sr
            # > 0: this pair implies a LATER origin than the running
            # minimum, i.e. it is the later (skewed) draw.
            lateness_s = (gps - predicted_ns) / 1e9
            if (
                -COUNTER_EPOCH_STEP_S <= lateness_s
                and lateness_s <= COUNTER_EPOCH_SKEW_ALLOWANCE_S
            ):
                if lateness_s < 0.0:
                    # a less-late pair: the running minimum moves down
                    self._pair = (gps, rtp, sr)
                return self.epoch_id
        self._pair = (gps, rtp, sr)
        self._id = f"ep-{int(self.epoch_offset_s)}"
        return self._id
