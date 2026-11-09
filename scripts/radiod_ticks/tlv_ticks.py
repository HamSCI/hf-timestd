"""Decode radiod status packets that carry the tick-detector group.

Wire format (ka9q-radio src/status.h values 218-227, src/tick_status.c):

  TICK_SECOND_UTC   227  int64   UTC second measured, ns since the GPS epoch
  TICK_WINDOW_START 226  int64   input-counter index of the profile's first sample
  TICK_STEP         225  int     input samples per profile step
  TICK_FLAGS        224  int     bit0 complete, bit1 used_pilot, bits 4-6 template mask
  TICK_PROFILE_WWV  223  bytes   one big-endian float scale, then int16 (re, im) pairs
  TICK_PROFILE_WWVH 222  bytes   same
  TICK_PROFILE_BPM  221  bytes   same
  TICK_PEAKS        220  6 floats  peak offset per template (wwv, wwvh, bpm) from the
                                   window start in INPUT samples (NaN: none), then SNR dB
  TICK_MARKERS      219  3 floats  energy at 1000, 1200, 1500 Hz over 0-800 ms
  TICK_ENVELOPE     218  bytes   float scale, then 1000 int16 (1 kHz AM envelope);
                                 here the scale is already per count: value = q * scale

A scaled value is q * scale / 32767.  The tick group is the last thing in
the packet, so a packet cut short loses the tail first; decoding keeps
everything before the cut.  A template outside the channel's mask sends
no profile, and its key is absent from the result.

``decode_ticks`` returns what ``radiod_pilot.tlv.decode`` returns, plus:
  tick_second_utc, tick_window_start, tick_step, tick_flags (ints),
  tick_complete, tick_used_pilot (bools), tick_templates (int mask),
  tick_profile_{wwv,wwvh,bpm} (complex64 [n]),
  tick_peaks (float32 [3, 2]: per template [offset in input samples, SNR dB]),
  tick_markers (float32 [3]), tick_envelope (float32 [1000]).
"""
import struct
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from radiod_pilot import tlv  # noqa: E402

T_TICK_SECOND_UTC = 227
T_TICK_WINDOW_START = 226
T_TICK_STEP = 225
T_TICK_FLAGS = 224
T_TICK_PROFILE = {223: "wwv", 222: "wwvh", 221: "bpm"}
T_TICK_PEAKS = 220
T_TICK_MARKERS = 219
T_TICK_ENVELOPE = 218

TEMPLATES = ("wwv", "wwvh", "bpm")
ENVELOPE_LEN = 1000
MAX_PROFILE = 1050
GPS_TO_UNIX_NS = (315964800 - 18) * 10**9    # ka9q-radio timing.h; 18 leap seconds


def _scaled(b: bytes, n_values: int):
    """(scale, int16 array) from a scale-then-int16 body, or None if malformed."""
    if len(b) != 4 + 2 * n_values:
        return None
    scale = struct.unpack(">f", b[:4])[0]
    q = np.frombuffer(b, dtype=">i2", offset=4, count=n_values).astype(np.float32)
    return np.float32(scale), q


def _profile(b: bytes):
    if len(b) < 4 or (len(b) - 4) % 4 or (len(b) - 4) // 4 > MAX_PROFILE:
        return None
    n = (len(b) - 4) // 4
    scale, q = _scaled(b, 2 * n)
    v = q * (scale / np.float32(32767))
    return (v[0::2] + 1j * v[1::2]).astype(np.complex64)


def _tlvs(packet: bytes):
    """Yield (type, value bytes); stop at EOL or a TLV that runs past the end."""
    n = len(packet)
    cp = 1
    while cp < n:
        t = packet[cp]
        cp += 1
        if t == 0 or cp >= n:
            return
        optlen = packet[cp]
        cp += 1
        if optlen & 0x80:
            lol = optlen & 0x7F
            if cp + lol > n:
                return
            optlen = int.from_bytes(packet[cp:cp + lol], "big")
            cp += lol
        if cp + optlen > n:
            return
        yield t, packet[cp:cp + optlen]
        cp += optlen


def decode_ticks(packet: bytes) -> dict:
    """Decode one status packet, tick group included.  See the module doc.

    Raises ValueError when the packet is not a status packet.
    """
    out = tlv.decode(packet)
    peaks = None
    for t, b in _tlvs(packet):
        if t == T_TICK_SECOND_UTC and len(b) <= 8:
            out["tick_second_utc"] = tlv._sint64(b)
        elif t == T_TICK_WINDOW_START and len(b) <= 8:
            out["tick_window_start"] = tlv._sint64(b)
        elif t == T_TICK_STEP and len(b) <= 8:
            out["tick_step"] = tlv._uint(b)
        elif t == T_TICK_FLAGS and len(b) <= 8:
            f = tlv._uint(b)
            out["tick_flags"] = f
            out["tick_complete"] = bool(f & 1)
            out["tick_used_pilot"] = bool(f & 2)
            out["tick_templates"] = (f >> 4) & 7
        elif t in T_TICK_PROFILE:
            p = _profile(b)
            if p is not None:
                out[f"tick_profile_{T_TICK_PROFILE[t]}"] = p
        elif t == T_TICK_PEAKS and len(b) == 24:
            peaks = np.frombuffer(b, dtype=">f4").astype(np.float32)
            out["tick_peaks"] = np.ascontiguousarray(peaks.reshape(2, 3).T)
        elif t == T_TICK_MARKERS and len(b) == 12:
            out["tick_markers"] = np.frombuffer(b, dtype=">f4").astype(np.float32)
        elif t == T_TICK_ENVELOPE:
            s = _scaled(b, ENVELOPE_LEN)
            if s is not None:
                if s[0] > 0 and np.isfinite(s[0]):
                    out["tick_envelope"] = (s[1] * s[0]).astype(np.float32)
                else:       # scale 0: incomplete report (or silence); absent, not a quiet second
                    out["tick_envelope"] = np.full(ENVELOPE_LEN, np.nan, np.float32)
    return out
