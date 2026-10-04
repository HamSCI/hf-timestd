"""GPS broadcast ephemeris from RINEX 3 navigation files, and the geometry
that turns it into elevation and azimuth.

Used by :class:`hf_timestd.core.rtcm3_adapter.RTCM3Adapter` for receivers
whose RTCM stream carries no ephemeris (the Quectel LG290P on AC0G-ND).
Orbit propagation follows IS-GPS-200 §20.3.3.4.3; stdlib only.
"""
from __future__ import annotations

import gzip
import logging
import math
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

C = 299_792_458.0
F_L1 = 1575.42e6
F_L2 = 1227.60e6
F_L5 = 1176.45e6
GPS_EPOCH = datetime(1980, 1, 6, tzinfo=timezone.utc)
GPS_UTC_LEAP_S = 18                      # GPS - UTC since 2017-01-01
SECONDS_PER_WEEK = 604_800

# WGS-84 / IS-GPS-200
GM = 3.986005e14
OMEGA_E = 7.2921151467e-5
WGS84_A = 6_378_137.0
WGS84_F = 1.0 / 298.257223563


# ─── geometry ──────────────────────────────────────────────────────────────

def ecef_to_geodetic(x: float, y: float, z: float) -> Tuple[float, float, float]:
    """WGS-84 ECEF (m) -> (lat rad, lon rad, height m), Bowring iteration."""
    e2 = WGS84_F * (2 - WGS84_F)
    lon = math.atan2(y, x)
    p = math.hypot(x, y)
    lat = math.atan2(z, p * (1 - e2))
    for _ in range(6):
        n = WGS84_A / math.sqrt(1 - e2 * math.sin(lat) ** 2)
        h = p / math.cos(lat) - n
        lat = math.atan2(z, p * (1 - e2 * n / (n + h)))
    n = WGS84_A / math.sqrt(1 - e2 * math.sin(lat) ** 2)
    h = p / math.cos(lat) - n
    return lat, lon, h


def elevation_azimuth(station: Tuple[float, float, float],
                      sat: Tuple[float, float, float]) -> Tuple[float, float]:
    """Elevation and azimuth (degrees) of ``sat`` seen from ``station`` (ECEF m)."""
    lat, lon, _ = ecef_to_geodetic(*station)
    dx, dy, dz = (sat[i] - station[i] for i in range(3))
    sl, cl, so, co = math.sin(lat), math.cos(lat), math.sin(lon), math.cos(lon)
    e = -so * dx + co * dy
    n = -sl * co * dx - sl * so * dy + cl * dz
    u = cl * co * dx + cl * so * dy + sl * dz
    el = math.degrees(math.atan2(u, math.hypot(e, n)))
    az = math.degrees(math.atan2(e, n)) % 360.0
    return el, az


# ─── broadcast ephemeris (GPS, RINEX 3 navigation) ─────────────────────────

@dataclass
class GpsEphemeris:
    prn: int
    toc_gps_s: float      # clock reference, continuous GPS seconds
    week: int
    toe: float            # seconds of week
    sqrt_a: float
    e: float
    i0: float
    omega0: float
    omega: float
    m0: float
    delta_n: float
    omega_dot: float
    idot: float
    cuc: float
    cus: float
    crc: float
    crs: float
    cic: float
    cis: float
    health: int
    af0: float = 0.0      # satellite clock bias (s), drift (s/s), drift rate (s/s^2)
    af1: float = 0.0
    af2: float = 0.0

    def clock_bias(self, t_gps_s: float) -> float:
        """Satellite clock offset (s) at ``t_gps_s`` (relativistic term omitted)."""
        dt = t_gps_s - self.toc_gps_s
        return self.af0 + self.af1 * dt + self.af2 * dt * dt

    @property
    def toe_gps_s(self) -> float:
        return self.week * SECONDS_PER_WEEK + self.toe

    def position(self, t_gps_s: float) -> Tuple[float, float, float]:
        """Satellite ECEF (m) at continuous GPS time ``t_gps_s`` (IS-GPS-200 §20.3.3.4.3)."""
        a = self.sqrt_a ** 2
        tk = t_gps_s - self.toe_gps_s
        n = math.sqrt(GM / a ** 3) + self.delta_n
        m = self.m0 + n * tk
        ek = m
        for _ in range(12):
            ek = m + self.e * math.sin(ek)
        nu = math.atan2(math.sqrt(1 - self.e ** 2) * math.sin(ek), math.cos(ek) - self.e)
        phi = nu + self.omega
        du = self.cus * math.sin(2 * phi) + self.cuc * math.cos(2 * phi)
        dr = self.crs * math.sin(2 * phi) + self.crc * math.cos(2 * phi)
        di = self.cis * math.sin(2 * phi) + self.cic * math.cos(2 * phi)
        u = phi + du
        r = a * (1 - self.e * math.cos(ek)) + dr
        i = self.i0 + di + self.idot * tk
        xp, yp = r * math.cos(u), r * math.sin(u)
        om = self.omega0 + (self.omega_dot - OMEGA_E) * tk - OMEGA_E * self.toe
        return (xp * math.cos(om) - yp * math.cos(i) * math.sin(om),
                xp * math.sin(om) + yp * math.cos(i) * math.cos(om),
                yp * math.sin(i))


def _rnx_floats(line: str) -> List[float]:
    out = []
    for k in range(4):
        f = line[4 + 19 * k: 4 + 19 * (k + 1)].strip().replace('D', 'E').replace('d', 'e')
        out.append(float(f) if f else 0.0)
    return out


def parse_rinex3_gps_nav(text: str) -> List[GpsEphemeris]:
    """GPS records of a RINEX 3.x (mixed) navigation file."""
    lines = text.splitlines()
    try:
        i = next(k for k, l in enumerate(lines) if 'END OF HEADER' in l) + 1
    except StopIteration:
        return []
    out: List[GpsEphemeris] = []
    while i < len(lines):
        head = lines[i]
        sys_ = head[:1]
        nrec = {'G': 8, 'E': 8, 'C': 8, 'J': 8, 'I': 8, 'R': 4, 'S': 4}.get(sys_, 1)
        if sys_ == 'G' and i + 7 < len(lines):
            try:
                prn = int(head[1:3])
                y, mo, d, hh, mi, ss = (int(head[4:8]), int(head[9:11]), int(head[12:14]),
                                        int(head[15:17]), int(head[18:20]), int(head[21:23]))
                toc = datetime(y, mo, d, hh, mi, ss, tzinfo=timezone.utc)
                o = [_rnx_floats(lines[i + k]) for k in range(1, 8)]
                af = [float(head[23 + 19 * k: 42 + 19 * k].strip().replace('D', 'E') or 0)
                      for k in range(3)]
                out.append(GpsEphemeris(
                    prn=prn, toc_gps_s=(toc - GPS_EPOCH).total_seconds(),
                    week=int(o[4][2]), toe=o[2][0], sqrt_a=o[1][3], e=o[1][1],
                    i0=o[3][0], omega0=o[2][2], omega=o[3][2], m0=o[0][3],
                    delta_n=o[0][2], omega_dot=o[3][3], idot=o[4][0],
                    cuc=o[1][0], cus=o[1][2], crc=o[3][1], crs=o[0][1],
                    cic=o[2][1], cis=o[2][3], health=int(o[5][1]),
                    af0=af[0], af1=af[1], af2=af[2]))
            except (ValueError, IndexError) as exc:
                logger.debug("RINEX nav: skipping GPS record at line %d: %s", i + 1, exc)
        i += nrec
    return out


class BroadcastEphemeris:
    """The healthy GPS ephemeris nearest in time, per PRN, from loaded files."""

    MAX_AGE_S = 4 * 3600          # IS-GPS-200 fit interval is 4 h

    def __init__(self):
        self._by_prn: Dict[int, List[GpsEphemeris]] = {}
        self._lock = threading.Lock()

    def load_text(self, text: str) -> int:
        recs = [r for r in parse_rinex3_gps_nav(text) if r.health == 0]
        with self._lock:
            for r in recs:
                lst = self._by_prn.setdefault(r.prn, [])
                if all(abs(x.toe_gps_s - r.toe_gps_s) > 1 for x in lst):
                    lst.append(r)
        return len(recs)

    def load_file(self, path: Path) -> int:
        opener = gzip.open if str(path).endswith('.gz') else open
        with opener(path, 'rt', errors='replace') as f:
            return self.load_text(f.read())

    def select(self, prn: int, t_gps_s: float) -> Optional[GpsEphemeris]:
        with self._lock:
            cands = self._by_prn.get(prn, [])
            best = min(cands, key=lambda r: abs(r.toe_gps_s - t_gps_s), default=None)
        if best is None or abs(best.toe_gps_s - t_gps_s) > self.MAX_AGE_S:
            return None
        return best

    def __len__(self) -> int:
        with self._lock:
            return sum(len(v) for v in self._by_prn.values())
