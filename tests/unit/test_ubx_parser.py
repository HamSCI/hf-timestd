"""
Unit tests for hf_timestd.core.ubx_parser

Minimal UBX parser for ZED-F9P GNSS frames. Tests build canonical messages
byte-for-byte (preamble + class + ID + length + payload + checksum) and
verify the parser:
- Synchronizes on the 0xB5 0x62 preamble
- Validates the Fletcher-style checksum
- Decodes UBX-RXM-RAWX (0x02 0x15) and UBX-NAV-SAT (0x01 0x35)
- Handles partial-frame buffering across process_data() calls
- Skips garbage between frames
- Drops frames with bad checksum
"""

import struct
from typing import List

import pytest

from hf_timestd.core.ubx_parser import UBXParser


# =============================================================================
# Frame builder helpers
# =============================================================================


def _checksum(content: bytes) -> bytes:
    a = b = 0
    for byte in content:
        a = (a + byte) & 0xFF
        b = (b + a) & 0xFF
    return bytes([a, b])


def make_ubx_frame(msg_class: int, msg_id: int, payload: bytes) -> bytes:
    body = bytes([msg_class, msg_id]) + struct.pack('<H', len(payload)) + payload
    return UBXParser.PREAMBLE + body + _checksum(body)


def make_rxm_rawx(measurements: List[dict] = None,
                  rcv_tow: float = 12345.0,
                  week: int = 2300,
                  leap_s: int = 18,
                  rec_stat: int = 0) -> bytes:
    measurements = measurements or []
    # Header: rcvTow(d) week(H) leapS(b) numMeas(B) recStat(B) version(B) reserved(2B)
    header = struct.pack('<dHbBBB', rcv_tow, week, leap_s,
                         len(measurements), rec_stat, 1) + b'\x00\x00'
    # Each measurement is 32 bytes (see source comment)
    blocks = b''
    for m in measurements:
        blocks += struct.pack(
            '<ddfBBBBHBBBBBB',
            m.get('prMes', 0.0), m.get('cpMes', 0.0), m.get('doMes', 0.0),
            m.get('gnssId', 0), m.get('svId', 0), m.get('sigId', 0),
            m.get('freqId', 0), m.get('locktime', 0), m.get('cno', 0),
            m.get('prStdev', 0), m.get('cpStdev', 0), m.get('doStdev', 0),
            m.get('trkStat', 0), 0,  # reserved
        )
    return make_ubx_frame(0x02, 0x15, header + blocks)


def make_nav_sat(sats: List[dict] = None, itow: int = 100000) -> bytes:
    sats = sats or []
    # Header: iTOW(I) version(B) numSvs(B) reserved(2B)
    header = struct.pack('<IBB', itow, 1, len(sats)) + b'\x00\x00'
    blocks = b''
    for s in sats:
        blocks += struct.pack(
            '<BBBbhhi',
            s.get('gnssId', 0), s.get('svId', 0), s.get('cno', 0),
            s.get('elev', 0), s.get('azim', 0),
            s.get('prRes', 0), s.get('flags', 0),
        )
    return make_ubx_frame(0x01, 0x35, header + blocks)


# =============================================================================
# Construction
# =============================================================================


class TestConstruction:
    def test_empty_buffer(self):
        p = UBXParser()
        assert p.buffer == bytearray()


# =============================================================================
# Checksum
# =============================================================================


class TestChecksum:
    def test_known_checksum(self):
        # Verify against an external Fletcher-8 reference
        p = UBXParser()
        ck = p._calc_checksum(b'\x01\x02\x03\x04')
        # ck_a = 1+2+3+4 = 10; ck_b = 1 + 3 + 6 + 10 = 20
        assert ck == bytes([10, 20])


# =============================================================================
# RXM-RAWX
# =============================================================================


class TestRXMRAWX:
    def test_short_payload_returns_none(self):
        p = UBXParser()
        # Build a frame with payload length 8 bytes (under the 16-byte minimum)
        short_payload = b'\x00' * 8
        frame = make_ubx_frame(0x02, 0x15, short_payload)
        msgs = list(p.process_data(frame))
        # The parser ran, the checksum matched, but the payload was rejected
        # → no messages yielded
        assert msgs == []

    def test_zero_measurements_decoded(self):
        p = UBXParser()
        frame = make_rxm_rawx(measurements=[], rcv_tow=42.0,
                              week=2300, leap_s=18)
        msgs = list(p.process_data(frame))
        assert len(msgs) == 1
        cls, mid, payload = msgs[0]
        assert (cls, mid) == (0x02, 0x15)
        assert payload['rcvTow'] == pytest.approx(42.0)
        assert payload['week'] == 2300
        assert payload['leapS'] == 18
        assert payload['measurements'] == []

    def test_one_measurement_decoded(self):
        p = UBXParser()
        meas = {
            'prMes': 1.234e7, 'cpMes': 5.6e6, 'doMes': -250.0,
            'gnssId': 0, 'svId': 7, 'sigId': 0, 'freqId': 0,
            'locktime': 1500, 'cno': 42, 'prStdev': 1, 'cpStdev': 2,
            'doStdev': 3, 'trkStat': 0x0F,
        }
        frame = make_rxm_rawx([meas])
        msgs = list(p.process_data(frame))
        assert len(msgs) == 1
        payload = msgs[0][2]
        assert len(payload['measurements']) == 1
        decoded = payload['measurements'][0]
        for key in ('gnssId', 'svId', 'sigId', 'cno', 'locktime', 'trkStat'):
            assert decoded[key] == meas[key]
        assert decoded['prMes'] == pytest.approx(meas['prMes'])
        assert decoded['cpMes'] == pytest.approx(meas['cpMes'])
        assert decoded['doMes'] == pytest.approx(meas['doMes'])

    def test_multiple_measurements_decoded(self):
        p = UBXParser()
        ms = [
            {'gnssId': 0, 'svId': 1, 'cno': 40},
            {'gnssId': 2, 'svId': 7, 'cno': 35},
            {'gnssId': 3, 'svId': 12, 'cno': 30},
        ]
        frame = make_rxm_rawx(ms)
        payload = list(p.process_data(frame))[0][2]
        assert len(payload['measurements']) == 3
        # Order preserved
        for orig, decoded in zip(ms, payload['measurements']):
            assert decoded['svId'] == orig['svId']
            assert decoded['cno'] == orig['cno']


# =============================================================================
# NAV-SAT
# =============================================================================


class TestNAVSAT:
    def test_short_payload_returns_none(self):
        p = UBXParser()
        # Payload too short (< 8 bytes minimum)
        frame = make_ubx_frame(0x01, 0x35, b'\x00' * 4)
        assert list(p.process_data(frame)) == []

    def test_zero_satellites(self):
        p = UBXParser()
        frame = make_nav_sat(sats=[], itow=12345)
        msgs = list(p.process_data(frame))
        assert len(msgs) == 1
        cls, mid, payload = msgs[0]
        assert (cls, mid) == (0x01, 0x35)
        assert payload['iTOW'] == 12345
        assert payload['sats'] == []

    def test_satellite_decoded_with_signed_elevation(self):
        p = UBXParser()
        sat = {'gnssId': 0, 'svId': 5, 'cno': 38, 'elev': -10,
               'azim': 270, 'prRes': 50, 'flags': 0x01}
        frame = make_nav_sat([sat])
        payload = list(p.process_data(frame))[0][2]
        assert len(payload['sats']) == 1
        s = payload['sats'][0]
        assert s['gnssId'] == 0
        assert s['svId'] == 5
        # elev is int8 → signed
        assert s['elev'] == -10
        assert s['azim'] == 270
        assert s['prRes'] == 50
        assert s['flags'] == 0x01


# =============================================================================
# Stream-handling behavior
# =============================================================================


class TestStreamHandling:
    def test_unknown_message_class_yields_nothing(self):
        p = UBXParser()
        # Class 0xFF is not handled by _parse_payload
        frame = make_ubx_frame(0xFF, 0x00, b'\x00' * 8)
        # Frame parses + checksum is valid, but payload returns None
        # → no yielded messages.
        assert list(p.process_data(frame)) == []

    def test_partial_frame_buffered(self):
        p = UBXParser()
        frame = make_rxm_rawx([{'gnssId': 0, 'svId': 1}])
        # Feed the first half — nothing yielded
        msgs1 = list(p.process_data(frame[:20]))
        assert msgs1 == []
        # Feed the rest — message arrives
        msgs2 = list(p.process_data(frame[20:]))
        assert len(msgs2) == 1
        assert msgs2[0][0:2] == (0x02, 0x15)

    def test_garbage_before_frame_skipped(self):
        p = UBXParser()
        garbage = b'\x00\x11\x22\x33\xFF'
        frame = make_nav_sat([])
        msgs = list(p.process_data(garbage + frame))
        assert len(msgs) == 1
        assert msgs[0][0:2] == (0x01, 0x35)

    def test_two_frames_back_to_back(self):
        p = UBXParser()
        frame1 = make_nav_sat(sats=[], itow=100)
        frame2 = make_rxm_rawx([])
        msgs = list(p.process_data(frame1 + frame2))
        assert len(msgs) == 2
        assert msgs[0][0:2] == (0x01, 0x35)
        assert msgs[1][0:2] == (0x02, 0x15)

    def test_bad_checksum_dropped(self):
        p = UBXParser()
        frame = bytearray(make_nav_sat([]))
        # Corrupt the last byte (checksum tail)
        frame[-1] = (frame[-1] ^ 0xFF) & 0xFF
        msgs = list(p.process_data(bytes(frame)))
        assert msgs == []

    def test_no_preamble_in_buffer(self):
        # All garbage with no UBX preamble → buffer keeps trimming itself
        p = UBXParser()
        # Force entry into the parsing loop (≥6 bytes)
        list(p.process_data(b'\x00' * 100))
        # All but last byte should have been discarded
        assert len(p.buffer) <= 1
