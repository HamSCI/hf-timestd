"""WWVB enhanced (post-2012) phase-modulation 1-minute time-frame protocol.

Pure-Python encoder/decoder for the WWVB PM time code, layer 2 (protocol)
only — no DSP, no I/O, no hf-timestd plumbing.  Input is a 60-bit frame
(list of ints 0/1, transmission order: bit 0 first, bit 59 last);
output is a parsed `WwvbTimeFrame`.

Reference: John Lowe, "Enhanced WWVB Broadcast Format", NIST Time and
Frequency Services, Revision 1.01, 2013-11-06.  Sections 4 and 4.1–4.6
specify the bit allocation, sync word, Hamming(31,26) parity code,
DST/leap-second indication, and DST-next advance notification used here.

For the full architectural picture (where this module fits in the WWVB
receive chain, layer-by-layer roadmap, the "no archive" principle,
diurnal reception story) see ``docs/WWVB-INTEGRATION.md``.

Out of scope (covered in a follow-up):
- Section 7 extended (6-minute) message symbols (124 PRBS sequences from
  the 7-stage LFSR with g(x) = x^7 + x^6 + x^5 + x^2 + 1)
- Section 5 message frames (use sync_M; 42-bit payload)
- DSP layer (carrier recovery, BPSK demod, AM gating)
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from enum import IntEnum
from typing import List, Optional, Sequence, Tuple

__all__ = [
    "DstState",
    "FRAME_BITS",
    "LeapSecond",
    "MINUTE_COUNTER_MAX",
    "MINUTE_EPOCH",
    "SYNC_M_BITS",
    "SYNC_T_BITS",
    "WwvbTimeFrame",
    "encode_time_frame",
    "from_minute_counter",
    "hamming_decode",
    "hamming_parity",
    "minute_counter",
    "parse_time_frame",
    "sync_score",
]

# =============================================================================
# Constants
# =============================================================================

# Sync words (NIST spec, Table 3).  MSB-first as written; sync_T[12] is the
# first transmitted bit and lands at frame position 0.
SYNC_T_BITS: Tuple[int, ...] = (0, 0, 1, 1, 1, 0, 1, 1, 0, 1, 0, 0, 0)
SYNC_M_BITS: Tuple[int, ...] = (1, 1, 0, 1, 0, 0, 0, 1, 1, 1, 0, 1, 0)
FRAME_BITS: int = 60

# Minute-counter epoch (NIST spec §4.3): time[25:0] counts minutes since
# 00:00 UTC on January 1, 2000.  Wraps every 2**26 minutes ≈ 127.6 years.
MINUTE_EPOCH = _dt.datetime(2000, 1, 1, 0, 0, tzinfo=_dt.timezone.utc)
MINUTE_COUNTER_MAX: int = 1 << 26  # 67_108_864

# Parity equations (NIST spec §4.3).  Each entry is the list of time-word
# bit indices XORed to produce that parity bit.  Hamming(31,26): 26 data
# bits (time[25..0]) + 5 parity bits → corrects 1, detects 2.
_PARITY_TAPS: Tuple[Tuple[int, ...], ...] = (
    (23, 21, 20, 17, 16, 15, 14, 13, 9, 8, 6, 5, 4, 2, 0),   # time_par[0]
    (24, 22, 21, 18, 17, 16, 15, 14, 10, 9, 7, 6, 5, 3, 1),  # time_par[1]
    (25, 23, 22, 19, 18, 17, 16, 15, 11, 10, 8, 7, 6, 4, 2),  # time_par[2]
    (24, 21, 19, 18, 15, 14, 13, 12, 11, 7, 6, 4, 3, 2, 0),   # time_par[3]
    (25, 22, 20, 19, 16, 15, 14, 13, 12, 8, 7, 5, 4, 3, 1),   # time_par[4]
)


# =============================================================================
# Frame bit map (NIST spec Table 1)
# =============================================================================
# Maps each PM frame position (0–59) to a semantic label.  Used by the
# encoder to place bits and by the decoder to extract them.
#
# Field labels:
#   'sync_T[i]'      – sync word bit i (0–12), positions 0–12
#   'time_par[i]'    – Hamming parity bit i (0–4)
#   'time[i]'        – minute-counter bit i (0–25)
#   'time[0]_rep'    – the LSB of the time word, repeated at position 19
#                      (NIST §4.3); transmitted but redundant on decode
#   'R'              – reserved-for-future (positions 29, 39); ignored
#   'dst_ls[i]'      – DST/leap-second code-word bit i (0–4)
#   'notice'         – NIST notice bit (position 49)
#   'dst_next[i]'    – DST-next advance-notification bit i (0–5)
#   'zero'           – always 0 (position 59)
_FRAME_MAP: Tuple[str, ...] = (
    # 0–9
    "sync_T[12]", "sync_T[11]", "sync_T[10]", "sync_T[9]", "sync_T[8]",
    "sync_T[7]", "sync_T[6]", "sync_T[5]", "sync_T[4]", "sync_T[3]",
    # 10–19
    "sync_T[2]", "sync_T[1]", "sync_T[0]",
    "time_par[4]", "time_par[3]", "time_par[2]", "time_par[1]", "time_par[0]",
    "time[25]", "time[0]_rep",
    # 20–29
    "time[24]", "time[23]", "time[22]", "time[21]", "time[20]",
    "time[19]", "time[18]", "time[17]", "time[16]", "R",
    # 30–39
    "time[15]", "time[14]", "time[13]", "time[12]", "time[11]",
    "time[10]", "time[9]", "time[8]", "time[7]", "R",
    # 40–49
    "time[6]", "time[5]", "time[4]", "time[3]", "time[2]",
    "time[1]", "time[0]", "dst_ls[4]", "dst_ls[3]", "notice",
    # 50–59
    "dst_ls[2]", "dst_ls[1]", "dst_ls[0]",
    "dst_next[5]", "dst_next[4]", "dst_next[3]",
    "dst_next[2]", "dst_next[1]", "dst_next[0]", "zero",
)
assert len(_FRAME_MAP) == FRAME_BITS


# =============================================================================
# Enums
# =============================================================================

class DstState(IntEnum):
    """DST state derived from dst_ls bits (NIST spec Table 5)."""

    NOT_IN_EFFECT = 0b00       # standard time, no transition today
    STARTING_TODAY = 0b10      # 00:00 UTC of Sunday DST begins
    IN_EFFECT = 0b11           # DST has been in effect > 24 h
    ENDING_TODAY = 0b01        # 00:00 UTC of Sunday DST ends


class LeapSecond(IntEnum):
    """Leap-second advance notice derived from dst_ls bits (Table 6)."""

    NONE = 0b00                # no leap second scheduled this month
    NEGATIVE = 0b10            # negative leap second at end of month
    POSITIVE = 0b11            # positive leap second at end of month


# Decode tables — Table 4 maps 5-bit dst_ls to (dst_on[1:0], leap_sec[1:0]).
# The spec uses 12 valid combinations; everything else is invalid (parity
# violation from the redundant encoding).  Built from Table 4 row-by-row.
# Keys are the dst_ls 5-bit value with dst_ls[4] as MSB; values are
# (dst_state, leap_state).  None entries mean "leap_sec[0] is don't-care",
# resolved at parse time by checking both 0 and 1.
_DST_LS_DECODE: dict = {
    0b01000: (DstState.NOT_IN_EFFECT, LeapSecond.NONE),
    0b10110: (DstState.STARTING_TODAY, LeapSecond.NONE),
    0b00011: (DstState.IN_EFFECT, LeapSecond.NONE),
    0b10101: (DstState.ENDING_TODAY, LeapSecond.NONE),
    0b00100: (DstState.NOT_IN_EFFECT, LeapSecond.NEGATIVE),
    0b10001: (DstState.STARTING_TODAY, LeapSecond.NEGATIVE),
    0b01110: (DstState.IN_EFFECT, LeapSecond.NEGATIVE),
    0b01100: (DstState.ENDING_TODAY, LeapSecond.NEGATIVE),
    0b11001: (DstState.NOT_IN_EFFECT, LeapSecond.POSITIVE),
    0b11010: (DstState.STARTING_TODAY, LeapSecond.POSITIVE),
    0b11111: (DstState.IN_EFFECT, LeapSecond.POSITIVE),
    0b11100: (DstState.ENDING_TODAY, LeapSecond.POSITIVE),
}
# Reverse map for the encoder.
_DST_LS_ENCODE: dict = {v: k for k, v in _DST_LS_DECODE.items()}


# =============================================================================
# Hamming(31,26) error-correcting code
# =============================================================================

def hamming_parity(time_word: int) -> Tuple[int, int, int, int, int]:
    """Compute the 5 Hamming(31,26) parity bits for a 26-bit time word.

    Args:
        time_word: integer in [0, 2**26).  Bit 0 is LSB (time[0]).

    Returns:
        (time_par[0], time_par[1], time_par[2], time_par[3], time_par[4]).
    """
    if not 0 <= time_word < MINUTE_COUNTER_MAX:
        raise ValueError(f"time_word out of range: {time_word}")
    parity = []
    for taps in _PARITY_TAPS:
        bit = 0
        for t in taps:
            bit ^= (time_word >> t) & 1
        parity.append(bit)
    return tuple(parity)  # type: ignore[return-value]


def hamming_decode(
    time_word: int, parity_bits: Sequence[int]
) -> Tuple[int, int]:
    """Decode a Hamming(31,26) codeword: detect or correct errors.

    Args:
        time_word: received 26-bit time word.
        parity_bits: received 5 parity bits, parity_bits[i] = time_par[i].

    Returns:
        (corrected_time_word, errors_detected).
        errors_detected is 0 (no error), 1 (corrected single-bit error),
        or 2 (multi-bit error detected but uncorrectable — corrected_time_word
        is unreliable and equals the input).
    """
    if len(parity_bits) != 5:
        raise ValueError(f"expected 5 parity bits, got {len(parity_bits)}")

    # Compute expected parity from the received data and form the syndrome.
    expected = hamming_parity(time_word)
    syndrome = tuple(p ^ e for p, e in zip(parity_bits, expected))

    if all(s == 0 for s in syndrome):
        return time_word, 0

    # Build a syndrome → error-bit lookup on first use.  31 single-bit error
    # positions: bits 0–4 are the parity bits themselves (syndrome has a
    # single 1 at the parity position); bits 5–30 correspond to flipping
    # time[i] for i in 0..25 (syndrome lights up the parity equations that
    # include i).
    if not hasattr(hamming_decode, "_syn_table"):
        table: dict = {}
        # Parity-bit errors: flipping parity i lights up only syndrome bit i.
        for i in range(5):
            syn = tuple(1 if j == i else 0 for j in range(5))
            table[syn] = ("parity", i)
        # Data-bit errors: flipping time[i] lights up exactly the parity
        # equations whose tap list contains i.
        for i in range(26):
            syn = tuple(1 if i in taps else 0 for taps in _PARITY_TAPS)
            # Sanity: the Hamming(31,26) code constructed from these taps
            # should give unique non-zero syndromes per bit position.
            if syn in table or all(b == 0 for b in syn):
                raise RuntimeError(
                    f"Hamming syndrome collision at time[{i}]: {syn}"
                )
            table[syn] = ("data", i)
        hamming_decode._syn_table = table  # type: ignore[attr-defined]

    target = hamming_decode._syn_table.get(syndrome)  # type: ignore[attr-defined]
    if target is None:
        # Syndrome does not match any single-bit error → 2+ bit error.
        return time_word, 2
    kind, idx = target
    if kind == "data":
        return time_word ^ (1 << idx), 1
    # Parity-bit error: time word is fine, parity is just toggled.
    return time_word, 1


# =============================================================================
# Time-word <-> datetime
# =============================================================================

def minute_counter(when: _dt.datetime) -> int:
    """Return the WWVB minute counter for `when` (UTC).

    NIST spec §4.3: minutes elapsed since 00:00 UTC on 2000-01-01.  Wraps
    at MINUTE_COUNTER_MAX (≈ year 2127).
    """
    if when.tzinfo is None:
        raise ValueError("naive datetime; pass tzinfo=UTC")
    delta = when.astimezone(_dt.timezone.utc) - MINUTE_EPOCH
    minutes = int(delta.total_seconds()) // 60
    if minutes < 0:
        raise ValueError(f"datetime predates WWVB epoch: {when}")
    return minutes % MINUTE_COUNTER_MAX


def from_minute_counter(counter: int) -> _dt.datetime:
    """Inverse of `minute_counter`."""
    if not 0 <= counter < MINUTE_COUNTER_MAX:
        raise ValueError(f"counter out of range: {counter}")
    return MINUTE_EPOCH + _dt.timedelta(minutes=counter)


# =============================================================================
# Parsed-frame dataclass
# =============================================================================

@dataclass(frozen=True)
class WwvbTimeFrame:
    """Decoded contents of a 1-minute PM time frame."""

    minute_of_frame: _dt.datetime
    """UTC time at the *start* of this minute (the on-time mark)."""

    dst_state: Optional[DstState]
    leap_second: Optional[LeapSecond]
    dst_next_code: int  # raw 6-bit dst_next field (Table 8 entry index)
    notice: int  # NIST notice bit (1 = notice on nist.gov)
    sync_errors: int  # bit-mismatches against sync_T (0 = perfect match)
    parity_errors: int  # 0 (clean), 1 (corrected), 2 (uncorrectable)
    dst_ls_valid: bool  # True if dst_ls decoded to a Table-4 row


# =============================================================================
# Encoder
# =============================================================================

def encode_time_frame(
    when: _dt.datetime,
    dst_state: DstState = DstState.NOT_IN_EFFECT,
    leap_second: LeapSecond = LeapSecond.NONE,
    dst_next_code: int = 0,
    notice: int = 0,
    reserved_value: int = 0,
) -> List[int]:
    """Build a 60-bit PM time frame for the given UTC minute.

    Args:
        when: UTC datetime of the minute being encoded (the on-time mark).
        dst_state: DST state (Table 5).
        leap_second: leap-second notice (Table 6).
        dst_next_code: 6-bit dst_next field (Table 8 row index, 0–55);
            0 means "1st Sunday of March", which is the typical encoding
            when DST is not in effect; callers should consult Table 8.
        notice: NIST notice bit (0 = nothing, 1 = check station webpage).
        reserved_value: bit value for the two reserved-for-future positions
            (29 and 39).  NIST does not specify; transmitters can use 0 or 1.

    Returns:
        60-element list of {0, 1}, transmission order (bit 0 first).
    """
    if not 0 <= dst_next_code < 64:
        raise ValueError(f"dst_next_code out of 6-bit range: {dst_next_code}")
    if notice not in (0, 1):
        raise ValueError(f"notice must be 0 or 1: {notice}")
    if reserved_value not in (0, 1):
        raise ValueError(f"reserved_value must be 0 or 1: {reserved_value}")

    time_word = minute_counter(when)
    parity = hamming_parity(time_word)
    dst_ls = _DST_LS_ENCODE.get((dst_state, leap_second))
    if dst_ls is None:
        raise ValueError(
            f"no Table-4 encoding for (dst_state={dst_state}, "
            f"leap_second={leap_second})"
        )

    bits: List[int] = [0] * FRAME_BITS
    for pos, label in enumerate(_FRAME_MAP):
        if label.startswith("sync_T["):
            i = int(label[7:-1])
            bits[pos] = SYNC_T_BITS[12 - i]  # sync_T[12] is bit 0 of SYNC_T_BITS
        elif label.startswith("time_par["):
            i = int(label[9:-1])
            bits[pos] = parity[i]
        elif label.startswith("time[") and label.endswith("]_rep"):
            bits[pos] = time_word & 1  # repeated LSB
        elif label.startswith("time["):
            i = int(label[5:-1])
            bits[pos] = (time_word >> i) & 1
        elif label.startswith("dst_ls["):
            i = int(label[7:-1])
            bits[pos] = (dst_ls >> i) & 1
        elif label.startswith("dst_next["):
            i = int(label[9:-1])
            bits[pos] = (dst_next_code >> i) & 1
        elif label == "notice":
            bits[pos] = notice
        elif label == "R":
            bits[pos] = reserved_value
        elif label == "zero":
            bits[pos] = 0
        else:
            raise RuntimeError(f"unhandled frame label: {label!r}")
    return bits


# =============================================================================
# Decoder
# =============================================================================

def parse_time_frame(bits: Sequence[int]) -> WwvbTimeFrame:
    """Parse a 60-bit PM time frame.

    Args:
        bits: 60 ints in {0, 1}, transmission order (bit 0 first).

    Returns:
        A `WwvbTimeFrame` with the decoded fields and error flags.

    Raises:
        ValueError: if `bits` is not 60 entries of {0, 1}.
    """
    if len(bits) != FRAME_BITS:
        raise ValueError(f"expected {FRAME_BITS} bits, got {len(bits)}")
    for b in bits:
        if b not in (0, 1):
            raise ValueError(f"non-binary bit in frame: {b}")

    # Sync check (positions 0–12).
    sync_errors = sum(
        1 for i, b in enumerate(bits[:13]) if b != SYNC_T_BITS[i]
    )

    # Extract time word and parity bits.
    time_word = 0
    parity_bits = [0, 0, 0, 0, 0]
    dst_ls = 0
    dst_next_code = 0
    notice = 0
    for pos, label in enumerate(_FRAME_MAP):
        b = bits[pos]
        if label.startswith("time[") and label.endswith("]_rep"):
            pass  # ignore redundant LSB copy
        elif label.startswith("time["):
            i = int(label[5:-1])
            time_word |= b << i
        elif label.startswith("time_par["):
            i = int(label[9:-1])
            parity_bits[i] = b
        elif label.startswith("dst_ls["):
            i = int(label[7:-1])
            dst_ls |= b << i
        elif label.startswith("dst_next["):
            i = int(label[9:-1])
            dst_next_code |= b << i
        elif label == "notice":
            notice = b

    # Apply Hamming correction.
    corrected, errors = hamming_decode(time_word, parity_bits)
    minute_dt = from_minute_counter(corrected)

    # Decode DST/leap.
    decoded = _DST_LS_DECODE.get(dst_ls)
    dst_state, leap_second = (decoded if decoded else (None, None))

    return WwvbTimeFrame(
        minute_of_frame=minute_dt,
        dst_state=dst_state,
        leap_second=leap_second,
        dst_next_code=dst_next_code,
        notice=notice,
        sync_errors=sync_errors,
        parity_errors=errors,
        dst_ls_valid=decoded is not None,
    )


# =============================================================================
# Sync correlation helper (used by the upstream framing layer)
# =============================================================================

def sync_score(candidate: Sequence[int], sync_word: Sequence[int]) -> int:
    """Bit-match count between `candidate` and `sync_word`.

    Used by the upstream framing layer to align the 60-bit frame
    boundary by sliding-window correlation against SYNC_T_BITS (time
    frames) or SYNC_M_BITS (message frames).
    """
    if len(candidate) != len(sync_word):
        raise ValueError(
            f"length mismatch: candidate={len(candidate)}, "
            f"sync={len(sync_word)}"
        )
    return sum(1 for a, b in zip(candidate, sync_word) if a == b)
