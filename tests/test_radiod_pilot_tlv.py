"""Decoder for radiod's pilot-detector status TLVs.

Two packet sources:
* a packet built here by hand from the TLV layout of ka9q-python
  status.py (independent of the decoder under test);
* packets built by radiod's own C encoder (ka9q-radio pilot-detector head
  02d73783: status.o + pilot_status.o via a throwaway helper), kept as hex.
"""
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from radiod_pilot import tlv  # noqa: E402

BATTERY = [1.5, 0.25, 0.5, -2.0, 4.0, 0.125, 0.0, -0.75]


def _len(n):
    return bytes([n]) if n < 128 else bytes([0x80 | 2, n >> 8, n & 0xFF])


def _uint(t, x):
    """Big-endian, leading zero bytes suppressed, zero is zero length."""
    b = x.to_bytes(8, "big").lstrip(b"\0")
    return bytes([t, len(b)]) + b


def _int64(t, x):
    return _uint(t, x & (2**64 - 1))


def _dbl(t, x):
    return _uint(t, struct.unpack(">Q", struct.pack(">d", x))[0])


def _vec(t, xs):
    body = b"".join(struct.pack(">f", x) for x in xs)
    return bytes([t]) + _len(len(body)) + body


def _packet(edge, anchor_input):
    p = bytes([0])
    p += _uint(tlv.T_OUTPUT_SSRC, 10000)
    p += _int64(tlv.T_GPS_TIME, 1478000000123456789)
    p += _uint(tlv.T_RTP_TIMESNAP, 4294967196)
    p += _int64(tlv.T_INPUT_SAMPLES, 123456789012)
    p += _dbl(tlv.T_RADIO_FREQUENCY, 10e6)
    p += _uint(tlv.T_OUTPUT_SAMPRATE, 96000)
    p += bytes([200, 3, 1, 2, 3])              # unknown type: skipped by length
    p += _int64(240, 3)
    p += _uint(239, 4294967196)
    p += _int64(238, anchor_input)
    p += _int64(237, edge)
    p += _dbl(236, 0.375)
    p += _int64(235, 1478000000000000000)
    p += _int64(234, 1478000000000012345)
    p += _dbl(233, 1.5e-6)
    p += _dbl(232, -3.25)
    p += _uint(231, 1)
    p += _uint(230, 30)
    p += _dbl(229, 2.5e-5)
    p += _vec(228, BATTERY)
    p += bytes([200, 0])                       # unknown, zero length
    p += bytes([0])                            # EOL
    p += _uint(240, 99)                        # after EOL: must not be read
    return p


def _check(d, edge, anchor_input):
    assert d["ssrc"] == 10000
    assert d["gps_time"] == 1478000000123456789
    assert d["rtp_timesnap"] == 4294967196
    assert d["input_samples"] == 123456789012
    assert d["frequency"] == 10e6
    assert d["output_samprate"] == 96000
    assert d["input_epoch"] == 3
    assert d["anchor_rtp"] == 4294967196
    assert d["anchor_input_sample"] == anchor_input
    assert d["pilot_edge_sample"] == edge
    assert d["pilot_edge_frac"] == 0.375
    assert d["pilot_edge_utc"] == 1478000000000000000
    assert d["pilot_host_time"] == 1478000000000012345
    assert d["pilot_edge_sigma"] == 1.5e-6
    assert d["pilot_snr"] == -3.25
    assert d["pilot_lock"] is True
    assert d["pilot_fold_count"] == 30
    assert d["pilot_filter_delay"] == 2.5e-5
    assert d["pilot_battery"] == BATTERY
    assert not any(k in d for k in (200, "200", "unknown"))


def test_decode_handcrafted_packet():
    _check(tlv.decode(_packet(987654321012, 555000000000)), 987654321012, 555000000000)


def test_negative_signed_values():
    _check(tlv.decode(_packet(-5000, -77)), -5000, -77)


def test_not_a_status_packet():
    with pytest.raises(ValueError):
        tlv.decode(bytes([1]) + _uint(240, 1))
    with pytest.raises(ValueError):
        tlv.decode(b"")


def test_anchor_only_has_no_pilot_group():
    p = bytes([0]) + _int64(240, 4) + _uint(239, 5) + _int64(238, 6) + bytes([0])
    d = tlv.decode(p)
    assert d["input_epoch"] == 4 and d["anchor_rtp"] == 5 and d["anchor_input_sample"] == 6
    assert not any(k.startswith("pilot_") for k in d)


def test_truncated_tlv_stops_cleanly():
    p = bytes([0]) + _int64(240, 4) + bytes([237, 8, 1, 2])
    d = tlv.decode(p)
    assert d["input_epoch"] == 4 and "pilot_edge_sample" not in d


def test_zero_values_and_short_double():
    # zero is zero length; 0.0 double and false bool both encode that way
    p = bytes([0]) + bytes([240, 0]) + bytes([232, 0]) + bytes([231, 0]) + bytes([0])
    d = tlv.decode(p)
    assert d["input_epoch"] == 0 and d["pilot_snr"] == 0.0 and d["pilot_lock"] is False


# Packets from radiod's own encoder (encode_pilot_status), head 02d73783
C_POSITIVE = bytes.fromhex(
    "001202271003081482e92b5962cd150804ffffff9c0d051cbe991a142108416312d0000000001403017700c80107"
    "f00103ef04ffffff9cee05813893ee00ed05e5f4c8f374ec083fd8000000000000eb081482e92b52070000ea0814"
    "82e92b52073039e9083eb92a737110e454e808c00a000000000000e70101e6011ee5083efa36e2eb1c432de4203f"
    "c000003e8000003f000000c0000000408000003e00000000000000bf40000000")
C_NEGATIVE = bytes.fromhex(
    "001202271003081482e92b5962cd150804ffffff9c0d051cbe991a142108416312d0000000001403017700c80107"
    "f00103ef04ffffff9cee08ffffffffffffffb3ed08ffffffffffffec78ec083fd8000000000000eb081482e92b52"
    "070000ea081482e92b52073039e9083eb92a737110e454e808c00a000000000000e70101e6011ee5083efa36e2eb"
    "1c432de4203fc000003e8000003f000000c0000000408000003e00000000000000bf40000000")


def test_decode_packet_from_c_encoder():
    _check(tlv.decode(C_POSITIVE), 987654321012, 555000000000)


def test_decode_negative_from_c_encoder():
    _check(tlv.decode(C_NEGATIVE), -5000, -77)


def test_rtp_wrap_conversion():
    # anchor 100 below the 32-bit wrap, packet 50 past it: 150 output samples
    assert tlv.rtp_to_input(50, 2**32 - 100, 1_000_000, 500) == 1_000_000 + 150 * 500


def test_rtp_conversion_no_wrap_and_backwards():
    assert tlv.rtp_to_input(1100, 1000, 7, 10) == 7 + 1000
    assert tlv.rtp_to_input(900, 1000, 7, 10) == 7 - 1000
    # backwards across the wrap
    assert tlv.rtp_to_input(2**32 - 10, 5, 0, 2) == -30
