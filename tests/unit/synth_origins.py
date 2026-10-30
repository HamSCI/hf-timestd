"""Synthetic radiod channel origins, shared by the T3 foundation tests.

B4 and ND run 64.8 Msps with a 20 ms block, so ilen = 1_296_000 input samples
per block and olen = f_out / 50 output samples (480 at 24 kHz, 1920 at 96 kHz).
Every channel of one radiod run starts its own RTP counter at its own block
`k0`.  Input block `b` then carries RTP `(2**32 - olen + (b + 1 - k0) * olen)
mod 2**32` in that channel's counter, the inverse of the memory's
`k0 = INPUT_SAMPLES/L + 1 - ((RTP_TIMESNAP - (2^32 - olen)) mod 2^32)/olen`.
"""
from __future__ import annotations

import math
from typing import Optional, Sequence, Tuple

from hamsci_dsp.counter_map import CounterMap, CounterPoll

F_IN = 64_800_000
ILEN = 1_296_000
ND_K0 = (51, 51, 51, 51, 50)     # ND's measured start blocks
EPOCH_GPS_S = 1_000_000.0        # synthetic radiod start, in GPS seconds
DEFAULT_BLOCKS = (100, 101, 102, 103, 104)


def olen_for(f_out: int) -> int:
    return f_out * ILEN // F_IN


def rtp_at(block: int, k0: int, olen: int) -> int:
    """RTP, in a channel that started at block k0, at the start of input block `block`."""
    return (2**32 - olen + (block + 1 - k0) * olen) % 2**32


def polls_for(k0: int, blocks: Sequence[int], f_out: int,
              epoch_gps_s: float = EPOCH_GPS_S):
    """One status poll per input block, as that channel would report it."""
    olen = olen_for(f_out)
    return [CounterPoll(rtp_timesnap=rtp_at(b, k0, olen), input_samples=b * ILEN,
                        filter_blocksize=ILEN, input_samprate=F_IN,
                        output_samprate=f_out,
                        gps_time_ns=int(round((epoch_gps_s + b * ILEN / F_IN) * 1e9)))
            for b in blocks]


def counter_map_for(k0: int, f_out: int, blocks: Sequence[int] = DEFAULT_BLOCKS,
                    epoch_gps_s: float = EPOCH_GPS_S) -> Optional[CounterMap]:
    return CounterMap.from_polls(polls_for(k0, blocks, f_out, epoch_gps_s))


def edge_at(cm: CounterMap, input_index: float) -> Tuple[int, float]:
    """(edge_rtp, edge_subsample) that place an edge at absolute input-sample
    `input_index` through map `cm`."""
    out = (input_index - cm.input_ref) * cm.olen / cm.ilen
    whole = math.floor(out)
    return (cm.rtp_ref + whole) % 2**32, out - whole
