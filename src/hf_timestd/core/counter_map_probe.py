"""CounterMapProbe: learn each channel's counter map from its own status pair.

Since ka9q-radio e5f790dd every channel starts its own RTP counter.  radiod
reports each channel's (INPUT_SAMPLES, RTP_TIMESNAP) pair in that channel's
status stream.  This probe polls it, keeps the last HISTORY polls per
channel, and builds a CounterMap from them by majority (3 of 5).

A map steps when the same radiod run reports a different intercept: that is
a fault, logged at ERROR once and counted.  A new radiod run replaces the
map quietly.  The probe never raises, and it never returns a map that may
alias: RTP is a 32-bit counter, so a held map reaches only half the range
(24 h 51 m at 24 kHz).  The probe rolls the map to the newest RTP it has
seen on every refresh, and drops it after MAX_STALE_S without a fresh reply.

The monotonic clock only measures how long the probe has gone without a
reply.  No wall clock is read and nothing here reaches chrony.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field, replace
from typing import Deque, Dict, Optional

from hamsci_dsp.counter_map import CounterMap, CounterPoll

logger = logging.getLogger(__name__)


def counter_poll_from_reply(reply, output_samprate: int) -> Optional[CounterPoll]:
    """One status reply -> one poll, or None.

    radiod answers a ghost SSRC with frontend status alone.  A reply without
    a gps_time, an RTP_TIMESNAP or INPUT_SAMPLES, or from another output
    rate, is not a poll.
    """
    fe = getattr(reply, "frontend", None)
    gps = getattr(reply, "gps_time", None)
    rtp = getattr(reply, "rtp_timesnap", None)
    n_in = getattr(fe, "input_samples", None)
    f_in = getattr(fe, "input_samprate", None)
    ilen = getattr(reply, "filter_blocksize", None)
    if not gps or rtp is None or n_in is None or not f_in or not ilen:
        return None
    if getattr(reply, "output_samprate", None) != output_samprate:
        return None
    return CounterPoll(rtp_timesnap=int(rtp), input_samples=int(n_in),
                       filter_blocksize=int(ilen), input_samprate=int(f_in),
                       output_samprate=int(output_samprate), gps_time_ns=int(gps))


def _epoch(p: CounterPoll) -> float:
    return p.gps_time_ns / 1e9 - p.input_samples / p.input_samprate


@dataclass
class _Channel:
    polls: Deque[CounterPoll]
    held: Optional[CounterMap] = None
    last_rtp: Optional[int] = None
    last_fresh_mono: Optional[float] = None
    started: bool = False
    stale_s: float = 0.0
    dropped_logged: bool = False
    failing: bool = False


class CounterMapProbe:
    POLLS_AT_START = 5
    HISTORY = 5              # three of five is a strict majority; never widen
    MIN_AGREE = 3
    # A held map drops after half the signed-32 reach, 2**31 / rate seconds
    # (24 h 51 m at 24 kHz, 6.2 h at 96 kHz), so it never aliases.
    STALE_REACH_FRACTION = 0.5
    # The run decision comes from the counters alone.  A restart resets
    # INPUT_SAMPLES; no plausible step moves the grid this far.
    RUN_MISS_BLOCKS = 1000

    @classmethod
    def stale_limit_s(cls, output_samprate: int) -> float:
        return cls.STALE_REACH_FRACTION * (2**31 / output_samprate)

    def __init__(self, control, poll_timeout: float = 1.0, mono_fn=time.monotonic):
        self._control = control
        self._timeout = poll_timeout
        self._mono = mono_fn
        self._ch: Dict[str, _Channel] = {}
        self._steps: Dict[str, int] = {}

    @property
    def steps(self) -> Dict[str, int]:
        return dict(self._steps)

    def refresh(self, channel: str, ssrc: int, output_samprate: int,
                time_left_s: Optional[float] = None) -> Optional[CounterMap]:
        """`time_left_s` bounds the polling: a burst stops when it runs out and
        the partial history completes on later ticks."""
        try:
            return self._refresh(channel, ssrc, output_samprate, time_left_s)
        except Exception:  # noqa: BLE001 - the recorder must never lose a loop pass
            logger.exception("counter map refresh for %s failed", channel)
            return None

    def _refresh(self, channel, ssrc, rate, time_left_s=None) -> Optional[CounterMap]:
        st = self._ch.get(channel)
        if st is None:
            st = self._ch[channel] = _Channel(polls=deque(maxlen=self.HISTORY))
        now = self._mono()
        deadline = None if time_left_s is None else now + time_left_s
        remaining = self.POLLS_AT_START if not st.started else 1
        st.stale_s = self.stale_limit_s(rate)
        st.started = True       # stays True after a drop: relearning costs one poll a tick
        burst_extended = False
        fresh = 0
        while remaining > 0:
            if deadline is not None and self._mono() >= deadline:
                break
            remaining -= 1
            try:
                reply = self._control.poll_status(ssrc, self._timeout)
            except Exception as exc:  # noqa: BLE001
                if not st.failing:
                    logger.warning("counter map poll of %s failed: %s "
                                   "(further failures this streak are silent)",
                                   channel, exc)
                    st.failing = True
                continue
            poll = counter_poll_from_reply(reply, rate)
            if poll is None:
                continue
            if st.failing:
                logger.info("counter map poll of %s recovered", channel)
                st.failing = False
            if self._is_new_run(st, poll):
                logger.info("counter map for %s: new radiod run, map replaced "
                            "(epoch %.3f s)", channel, _epoch(poll))
                st.polls.clear()
                st.held = None
                st.last_rtp = None
                if not burst_extended:        # gather the new run's polls at once
                    burst_extended = True
                    remaining = max(remaining, self.POLLS_AT_START - 1)
            st.polls.append(poll)
            st.last_rtp = poll.rtp_timesnap
            fresh += 1
        if fresh:
            st.last_fresh_mono = now
            st.dropped_logged = False
        elif st.last_fresh_mono is None:
            st.last_fresh_mono = now          # start the staleness clock

        if now - st.last_fresh_mono > st.stale_s:
            if st.held is not None or not st.dropped_logged:
                logger.error("counter map for %s dropped: no fresh RTP for over "
                             "%.1f h, a held map could alias", channel,
                             st.stale_s / 3600)
            st.held = None
            st.polls.clear()
            st.last_rtp = None
            st.dropped_logged = True
            return None

        if fresh:
            new = CounterMap.from_polls(list(st.polls), min_agree=self.MIN_AGREE)
            if new is not None:
                self._adopt(channel, st, new)
        if st.held is not None and st.last_rtp is not None:
            st.held = st.held.rolled_forward(st.last_rtp)
        return st.held

    def _is_new_run(self, st: "_Channel", poll: CounterPoll) -> bool:
        """A new radiod run resets the counters.  The decision uses them alone.

        GPS_TIME is read live while INPUT_SAMPLES is cached, so a late status
        packet moves the epoch and nothing else (B4 logged up to 816 ms);
        the epoch therefore never decides.  A poll starts a new run when
        INPUT_SAMPLES went backward past the newest poll's (by more than one
        block, so a torn pair is not a restart), or when it misses the held
        map's grid (else the newest poll's) by more than RUN_MISS_BLOCKS.
        Anything nearer is the same run, and `_adopt` counts it as a step.
        """
        newest = st.polls[-1] if st.polls else None
        if newest is not None and poll.input_samples < newest.input_samples - newest.filter_blocksize:
            return True
        if st.held is not None:
            m = st.held
            if (poll.filter_blocksize, poll.input_samprate) != (m.ilen, m.input_samprate):
                return True
            return abs(m.input_index(poll.rtp_timesnap) - poll.input_samples) > \
                self.RUN_MISS_BLOCKS * m.ilen
        if newest is None:
            return False
        ilen = newest.filter_blocksize
        if (poll.filter_blocksize, poll.input_samprate) != (ilen, newest.input_samprate):
            return True
        olen = newest.output_samprate * ilen // newest.input_samprate
        if olen <= 0:
            return True
        d = (poll.rtp_timesnap - newest.rtp_timesnap + 2**31) % 2**32 - 2**31
        expect = newest.input_samples + d * ilen / olen
        return abs(expect - poll.input_samples) > self.RUN_MISS_BLOCKS * ilen

    def _adopt(self, channel: str, st: _Channel, new: CounterMap) -> None:
        held = st.held
        if held is None:
            st.held = new
            return
        rolled = held.rolled_forward(new.rtp_ref)
        if ((new.ilen, new.olen, new.input_samprate)
                != (held.ilen, held.olen, held.input_samprate)
                or abs(new.input_ref - rolled.input_index(new.rtp_ref))
                > self.RUN_MISS_BLOCKS * new.ilen):
            st.held = new                     # another run; the poll path logged it
            return
        # Same run: keep the least-late epoch, so lateness never creeps in.
        epoch = min(held.input_epoch_gps_s, new.input_epoch_gps_s)
        new = replace(new, input_epoch_gps_s=epoch)
        blocks = (new.input_ref - rolled.input_index(new.rtp_ref)) / new.ilen
        if abs(blocks) < 1e-6:
            st.held = new
            return
        self._steps[channel] = self._steps.get(channel, 0) + 1
        logger.error("COUNTER MAP STEP %s: intercept moved %.3f block(s) within one "
                     "radiod run (step %d)", channel, blocks, self._steps[channel])
        st.held = new
