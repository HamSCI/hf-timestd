"""RTCM3 input for the GNSS VTEC pipeline: observations from the stream,
elevations from broadcast ephemeris.

The VTEC analyzer (:class:`hf_timestd.core.gnss_tec.GNSSTECAnalyzer`) was
written for a u-blox receiver, which hands it two things in UBX:
``RXM-RAWX`` (per-signal pseudorange and carrier phase) and ``NAV-SAT``
(per-satellite elevation and azimuth).  A receiver that speaks open standards
instead — the Quectel LG290P on AC0G-ND, 2026-10-04 — streams RTCM 3.3:
MSM7 observations (1077 GPS, 1087 GLONASS, 1097 Galileo, 1127 BeiDou), the
antenna position (1005) and a receiver description (1033).  Its base-station
engine encodes NO broadcast ephemeris, so nothing in the stream says where a
satellite is.  Elevation therefore comes from the daily multi-GNSS broadcast
navigation file that BKG publishes from live IGS streams, refreshed every 15
minutes (``BRDC00WRD_S``), combined with the 1005 antenna position.

:class:`RTCM3Adapter` presents the same ``process_data()`` interface as
:class:`hf_timestd.core.ubx_parser.UBXParser` and yields the same
``(msg_class, msg_id, payload)`` triples — ``(0x01, 0x35, nav_sat_like)`` and
``(0x02, 0x15, rawx_like)`` — so ``live_vtec`` dispatches both receivers
through one code path and the analyzer never learns which one it is reading.

Scope: GPS only, because the analyzer is GPS L1/L2 only.  Legacy 1004 and
MSM7/MSM4 (1077/1074) are decoded; the other constellations' MSM pass by.

RTCM decoding uses ``pyrtcm``, which every station venv already carries as a
dependency of ``pyubx2``; the orbit and geometry code here is stdlib-only.
"""
from __future__ import annotations

import logging
import math
import time
from typing import Dict, Iterator, List, Optional, Tuple

from hf_timestd.core.broadcast_ephemeris import (
    C, F_L1, F_L2, F_L5, GPS_EPOCH, GPS_UTC_LEAP_S, SECONDS_PER_WEEK,
    BroadcastEphemeris, elevation_azimuth,
)

logger = logging.getLogger(__name__)

LIGHT_MS = C / 1000.0                    # metres per light-millisecond

# MSM signal code -> the RAWX sigId the analyzer groups on (u-blox numbering):
# 0 = L1 C/A, 3 = L2 CL, 4 = L2 CM.  An L2C (M+L) "2X" observation is the
# same carrier as 2L; it rides as 3.  L5 is carried as sigId 7 (u-blox L5 Q)
# and ignored by the current L1/L2 analyzer.
_GPS_SIG = {'1C': 0, '2L': 3, '2S': 4, '2X': 3, '5Q': 7, '5I': 6, '5X': 7}
_GPS_SIG_FREQ = {0: F_L1, 3: F_L2, 4: F_L2, 6: F_L5, 7: F_L5}

# MSM7 "invalid" sentinels after pyrtcm's scaling (the most negative raw value
# times the field's resolution): DF405 20 bits x 2^-29 ms, DF406 24 bits x
# 2^-31 ms.  MSM4 uses DF400/DF401 with 15/22 bits at 2^-24 / 2^-29 ms.
_INVALID = {
    'DF405': -(2 ** 19) * 2 ** -29,
    'DF406': -(2 ** 23) * 2 ** -31,
    'DF400': -(2 ** 14) * 2 ** -24,
    'DF401': -(2 ** 21) * 2 ** -29,
}


def _valid(name: str, v) -> bool:
    return v is not None and not math.isclose(v, _INVALID.get(name, float('nan')),
                                              rel_tol=0, abs_tol=1e-15)


# ─── the RTCM3 stream adapter ──────────────────────────────────────────────

def _crc24q(data: bytes) -> int:
    crc = 0
    for b in data:
        crc ^= b << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= 0x1864CFB
    return crc & 0xFFFFFF


def looks_like_rtcm3(data: bytes, need: int = 2) -> bool:
    """True when ``data`` holds at least ``need`` CRC-valid RTCM3 frames."""
    i = good = 0
    while good < need:
        i = data.find(b'\xd3', i)
        if i < 0 or i + 6 > len(data):
            return False
        ln = ((data[i + 1] & 0x03) << 8) | data[i + 2]
        end = i + 3 + ln + 3
        if end <= len(data) and _crc24q(data[i:i + 3 + ln]) == int.from_bytes(data[i + 3 + ln:end], 'big'):
            good += 1
            i = end
        else:
            i += 1
    return True


class RTCM3Adapter:
    """RTCM3 bytes in, UBX-shaped ``(class, id, payload)`` triples out.

    For each GPS observation epoch (1077 / 1074 / 1004) it yields, in order:
    ``(0x01, 0x35, nav_sat_like)`` with elevations computed from broadcast
    ephemeris and the latest 1005 antenna position, then
    ``(0x02, 0x15, rawx_like)``.  Without a position or ephemeris it yields
    the observations alone — the analyzer then skips them (no elevation), and
    :meth:`status` says why.
    """

    def __init__(self, ephemeris: Optional[BroadcastEphemeris] = None,
                 now=time.time, leap_s: int = GPS_UTC_LEAP_S):
        from pyrtcm import RTCMReader  # station venvs carry it via pyubx2
        self._parse = RTCMReader.parse
        self.ephem = ephemeris or BroadcastEphemeris()
        self._now = now
        self.leap_s = leap_s
        self.buffer = b''
        self.station_ecef: Optional[Tuple[float, float, float]] = None
        self.counts: Dict[str, int] = {}
        self.no_ephemeris: set = set()

    # The analyzer and live_vtec only need process_data(); status() is for logs.
    def status(self) -> str:
        pos = 'known' if self.station_ecef else 'NOT YET (needs RTCM 1005/1006)'
        return (f"RTCM types seen {dict(sorted(self.counts.items()))}; antenna position "
                f"{pos}; {len(self.ephem)} GPS ephemerides"
                + (f"; no ephemeris for G{sorted(self.no_ephemeris)}" if self.no_ephemeris else ''))

    def _frames(self, data: bytes) -> Iterator[bytes]:
        self.buffer += data
        while True:
            i = self.buffer.find(b'\xd3')
            if i < 0:
                self.buffer = b''
                return
            if i:
                self.buffer = self.buffer[i:]
            if len(self.buffer) < 6:
                return
            ln = ((self.buffer[1] & 0x03) << 8) | self.buffer[2]
            end = 3 + ln + 3
            if len(self.buffer) < end:
                return
            frame = self.buffer[:end]
            if _crc24q(frame[:3 + ln]) != int.from_bytes(frame[3 + ln:end], 'big'):
                self.buffer = self.buffer[1:]          # resync past a false preamble
                continue
            self.buffer = self.buffer[end:]
            yield frame

    def _gps_time(self, tow_s: float) -> Tuple[int, float]:
        """(week, continuous GPS seconds) for a time-of-week, from the host clock."""
        now_gps = self._now() - GPS_EPOCH.timestamp() + self.leap_s
        week = int(now_gps // SECONDS_PER_WEEK)
        t = week * SECONDS_PER_WEEK + tow_s
        if t - now_gps > SECONDS_PER_WEEK / 2:      # TOW from last week, just past rollover
            week -= 1
        elif now_gps - t > SECONDS_PER_WEEK / 2:
            week += 1
        return week, week * SECONDS_PER_WEEK + tow_s

    def process_data(self, data: bytes) -> Iterator[Tuple[int, int, dict]]:
        for frame in self._frames(data):
            try:
                msg = self._parse(frame)
            except Exception as exc:  # noqa: BLE001 — a malformed frame must not kill the stream
                logger.debug("RTCM parse error: %s", exc)
                continue
            ident = getattr(msg, 'identity', '?')
            self.counts[ident] = self.counts.get(ident, 0) + 1
            if ident in ('1005', '1006'):
                self.station_ecef = (msg.DF025, msg.DF026, msg.DF027)
            elif ident in ('1077', '1074'):
                yield from self._epoch(*self._from_msm(msg))
            elif ident == '1004':
                yield from self._epoch(*self._from_1004(msg))

    def _from_msm(self, msg):
        from pyrtcm import parse_msm
        meta, sats, cells = parse_msm(msg)
        tow_s = meta['epoch'] / 1000.0
        rough = {}
        for s in sats:
            if s.get('DF397') in (None, 255):
                continue
            rough[int(s['PRN'])] = s['DF397'] + (s.get('DF398') or 0.0)
        fine_pr, fine_cp = ('DF405', 'DF406') if meta['identity'] == '1077' else ('DF400', 'DF401')
        meas = []
        for c in cells:
            prn = int(c['CELLPRN'])
            sig = _GPS_SIG.get(c['CELLSIG'])
            if sig is None or prn not in rough:
                continue
            pr = cp = None
            if _valid(fine_pr, c.get(fine_pr)):
                pr = (rough[prn] + c[fine_pr]) * LIGHT_MS
            if _valid(fine_cp, c.get(fine_cp)):
                cp = (rough[prn] + c[fine_cp]) * LIGHT_MS / (C / _GPS_SIG_FREQ[sig])
            if pr is None or cp is None:
                continue
            meas.append({'gnssId': 0, 'svId': prn, 'sigId': sig, 'prMes': pr,
                         'cpMes': cp, 'doMes': 0.0, 'cno': c.get('DF408', c.get('DF403', 0)),
                         'locktime': c.get('DF407', c.get('DF402', 0)), 'trkStat': 0})
        return tow_s, meas

    def _from_1004(self, msg):
        tow_s = msg.DF004 / 1000.0
        meas = []
        for k in range(1, msg.DF006 + 1):
            g = lambda f: getattr(msg, f'{f}_{k:02d}', None)   # noqa: E731
            prn, p1_mod, amb = g('DF009'), g('DF011'), g('DF014')
            if None in (prn, p1_mod, amb):
                continue
            p1 = p1_mod + amb * LIGHT_MS
            l1, d2p, d2l = g('DF012'), g('DF017'), g('DF018')
            if l1 is not None:
                meas.append({'gnssId': 0, 'svId': prn, 'sigId': 0, 'prMes': p1,
                             'cpMes': (p1 + l1) / (C / F_L1), 'doMes': 0.0,
                             'cno': g('DF015') or 0, 'locktime': g('DF013') or 0, 'trkStat': 0})
            if d2p is not None and d2l is not None:
                meas.append({'gnssId': 0, 'svId': prn, 'sigId': 3, 'prMes': p1 + d2p,
                             'cpMes': (p1 + d2l) / (C / F_L2), 'doMes': 0.0,
                             'cno': g('DF020') or 0, 'locktime': g('DF019') or 0, 'trkStat': 0})
        return tow_s, meas

    def _epoch(self, tow_s: float, meas: List[dict]):
        week, t_gps = self._gps_time(tow_s)
        if self.station_ecef is not None:
            sats = []
            for prn in sorted({m['svId'] for m in meas}):
                eph = self.ephem.select(prn, t_gps)
                if eph is None:
                    self.no_ephemeris.add(prn)
                    continue
                self.no_ephemeris.discard(prn)
                el, az = elevation_azimuth(self.station_ecef, eph.position(t_gps))
                sats.append({'gnssId': 0, 'svId': prn, 'elev': el, 'azim': az,
                             'prRes': 0, 'flags': 0})
            yield 0x01, 0x35, {'iTOW': int(tow_s * 1000), 'sats': sats}
        yield 0x02, 0x15, {'rcvTow': tow_s, 'week': week, 'leapS': self.leap_s,
                           'recStat': 0, 'measurements': meas}
