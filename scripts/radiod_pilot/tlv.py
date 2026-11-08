"""Decode radiod status packets, including the TS-1 pilot-detector TLVs.

Wire format (ka9q-radio src/status.c, src/pilot_status.c; ka9q-python
status.py decode loop): a type byte 0, then TLVs of type byte, length
byte (high bit set: the low 7 bits count following length bytes), value.
Integers are big-endian with leading zero bytes suppressed, zero being
zero length.  Doubles and floats travel as their IEEE bit patterns in
that same integer form.  Signed int64 fields are two's-complement casts
to uint64, so decoding reinterprets the 64 bits.  Type 0 inside the list
ends it.

Reimplemented here rather than imported from ka9q-python, which stays
untouched and carries a heavy import chain for four small helpers.
"""
import struct

# Existing radiod types (ka9q-radio src/status.h enum status_type)
T_GPS_TIME = 3
T_RTP_TIMESNAP = 8
T_INPUT_SAMPRATE = 10
T_INPUT_SAMPLES = 13
T_OUTPUT_SSRC = 18
T_OUTPUT_SAMPRATE = 20
T_RADIO_FREQUENCY = 33

# Pilot detector types (status.h 228..240)
T_INPUT_EPOCH = 240
T_ANCHOR_RTP = 239
T_ANCHOR_INPUT_SAMPLE = 238
T_PILOT_EDGE_SAMPLE = 237
T_PILOT_EDGE_FRAC = 236
T_PILOT_EDGE_UTC = 235
T_PILOT_HOST_TIME = 234
T_PILOT_EDGE_SIGMA = 233
T_PILOT_SNR = 232
T_PILOT_LOCK = 231
T_PILOT_FOLD_COUNT = 230
T_PILOT_FILTER_DELAY = 229
T_PILOT_BATTERY = 228

# Order of the eight floats in PILOT_BATTERY
BATTERY_FIELDS = ("plateau_amplitude", "fit_rms", "fold_retention", "split_half_delta",
                  "peak_prominence", "transition_width", "apex_distance",
                  "edge_offset_samples")


def _uint(b):
    return int.from_bytes(b, "big")


def _sint64(b):
    return struct.unpack(">q", _uint(b).to_bytes(8, "big"))[0]


def _double(b):
    return struct.unpack(">d", _uint(b).to_bytes(8, "big"))[0] if b else 0.0


# type -> (name, decoder); decoders take the value bytes (length <= 8 checked)
_FIELDS = {
    T_OUTPUT_SSRC: ("ssrc", _uint),
    T_GPS_TIME: ("gps_time", _sint64),
    T_RTP_TIMESNAP: ("rtp_timesnap", _uint),
    T_INPUT_SAMPRATE: ("input_samprate", _uint),
    T_INPUT_SAMPLES: ("input_samples", _sint64),
    T_RADIO_FREQUENCY: ("frequency", _double),
    T_OUTPUT_SAMPRATE: ("output_samprate", _uint),
    T_INPUT_EPOCH: ("input_epoch", _uint),
    T_ANCHOR_RTP: ("anchor_rtp", _uint),
    T_ANCHOR_INPUT_SAMPLE: ("anchor_input_sample", _sint64),
    T_PILOT_EDGE_SAMPLE: ("pilot_edge_sample", _sint64),
    T_PILOT_EDGE_FRAC: ("pilot_edge_frac", _double),
    T_PILOT_EDGE_UTC: ("pilot_edge_utc", _sint64),
    T_PILOT_HOST_TIME: ("pilot_host_time", _sint64),
    T_PILOT_EDGE_SIGMA: ("pilot_edge_sigma", _double),
    T_PILOT_SNR: ("pilot_snr", _double),
    T_PILOT_LOCK: ("pilot_lock", lambda b: _uint(b) != 0),
    T_PILOT_FOLD_COUNT: ("pilot_fold_count", _uint),
    T_PILOT_FILTER_DELAY: ("pilot_filter_delay", _double),
}


def decode(packet: bytes) -> dict:
    """Decode one status packet to a dict of the fields present.

    Raises ValueError when the packet is not a status packet (first byte
    not 0).  Unknown types are skipped by length; a TLV that runs past
    the end of the packet ends decoding with what came before it.
    """
    if not packet or packet[0] != 0:
        raise ValueError("not a radiod status packet")
    out = {}
    n = len(packet)
    cp = 1
    while cp < n:
        t = packet[cp]
        cp += 1
        if t == 0 or cp >= n:       # EOL
            break
        optlen = packet[cp]
        cp += 1
        if optlen & 0x80:
            lol = optlen & 0x7F
            if cp + lol > n:
                break
            optlen = _uint(packet[cp:cp + lol])
            cp += lol
        if cp + optlen > n:
            break
        data = packet[cp:cp + optlen]
        cp += optlen
        if t == T_PILOT_BATTERY:
            if optlen == 32:
                out["pilot_battery"] = list(struct.unpack(">8f", data))
        elif t in _FIELDS and optlen <= 8:
            name, fn = _FIELDS[t]
            out[name] = fn(data)
    return out


def rtp_to_input(rtp: int, anchor_rtp: int, anchor_input: int, decim: int) -> int:
    """Map an RTP timestamp to the input sample counter.

    The RTP clock counts output samples modulo 2**32; the difference to
    the anchor is a signed 32-bit quantity, so the conversion survives
    the wrap in either direction.  One output sample is ``decim`` input
    samples.
    """
    d = (rtp - anchor_rtp) & 0xFFFFFFFF
    if d >= 0x80000000:
        d -= 0x100000000
    return anchor_input + d * decim
