#!/usr/bin/env python3
"""Turn NOAA's GOES flare list into recording orders for sigmond stations.

Stdlib only: this file runs on wd30 as a plain script, outside the
hf-timestd venv:

    python3 flare_orders.py --stations stations.json --out orders.json

A flare earns an order when it is M1.0 or stronger, has ended, and struck a
sunlit station: the sun stands above the horizon at the station and at the
great-circle midpoint of at least one monitored path, at the flare's peak.
"""
import argparse
import json
import math
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

FEED_URL = "https://services.swpc.noaa.gov/json/goes/primary/xray-flares-7-day.json"
MIN_FLARE_WM2 = 1e-5                 # M1.0 on the GOES scale
PRE_MIN = 30                         # window opens this long before begin_time
POST_MIN = 90                        # and closes this long after end_time
_CLASS_SCALE = {"A": 1e-8, "B": 1e-7, "C": 1e-6, "M": 1e-5, "X": 1e-4}
_TS = "%Y-%m-%dT%H:%M:%SZ"

# Copied from hf_timestd.core.broadcast_specs.STATION_COORDINATES, because this
# file may not import hf_timestd.  tests/unit/test_flare_orders.py fails if the
# copies drift.
WWV = (40.6807, -105.0407)
WWVH = (21.9872, -159.7636)


def flare_class_value(cls: str) -> float:
    """GOES class string to peak flux in W/m^2: "M1.0" -> 1e-5."""
    return _CLASS_SCALE[cls[0].upper()] * float(cls[1:])


def solar_elevation_deg(when: datetime, lat: float, lon: float) -> float:
    """Sun elevation in degrees at a UTC time and a point (east longitude +).

    NOAA's low-precision solar position, the same math as
    hamsci_dsp.ionosphere.solar.solar_position.
    """
    when = when.astimezone(timezone.utc)
    a = (14 - when.month) // 12
    y = when.year + 4800 - a
    m = when.month + 12 * a - 3
    jd = when.day + (153 * m + 2) // 5 + 365 * y + y // 4 - y // 100 + y // 400 - 32045
    jd += (when.hour - 12) / 24 + when.minute / 1440 + when.second / 86400
    t = (jd - 2451545.0) / 36525.0
    l0 = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360
    mean_anom = math.radians(357.52911 + t * (35999.05029 - 0.0001537 * t))
    ecc = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    center = (math.sin(mean_anom) * (1.914602 - t * (0.004817 + 0.000014 * t))
              + math.sin(2 * mean_anom) * (0.019993 - 0.000101 * t)
              + math.sin(3 * mean_anom) * 0.000289)
    omega = 125.04 - 1934.136 * t
    app_lon = l0 + center - 0.00569 - 0.00478 * math.sin(math.radians(omega))
    obliq = (23 + (26 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60) / 60
             + 0.00256 * math.cos(math.radians(omega)))
    obliq_rad = math.radians(obliq)
    decl = math.asin(math.sin(obliq_rad) * math.sin(math.radians(app_lon)))
    var_y = math.tan(obliq_rad / 2) ** 2
    l0_rad = math.radians(l0)
    eq_time = 4 * math.degrees(
        var_y * math.sin(2 * l0_rad)
        - 2 * ecc * math.sin(mean_anom)
        + 4 * ecc * var_y * math.sin(mean_anom) * math.cos(2 * l0_rad)
        - 0.5 * var_y * var_y * math.sin(4 * l0_rad)
        - 1.25 * ecc * ecc * math.sin(2 * mean_anom))
    solar_min = (when.hour * 60 + when.minute + when.second / 60 + eq_time + 4 * lon) % 1440
    hour_angle = math.radians(solar_min / 4 - 180)
    lat_rad = math.radians(lat)
    cos_zen = (math.sin(lat_rad) * math.sin(decl)
               + math.cos(lat_rad) * math.cos(decl) * math.cos(hour_angle))
    return 90.0 - math.degrees(math.acos(max(-1.0, min(1.0, cos_zen))))


def path_midpoint(lat1: float, lon1: float, lat2: float, lon2: float):
    """Great-circle midpoint, (lat, lon) in degrees."""
    p1, l1, p2, l2 = map(math.radians, (lat1, lon1, lat2, lon2))
    bx = math.cos(p2) * math.cos(l2 - l1)
    by = math.cos(p2) * math.sin(l2 - l1)
    lat = math.atan2(math.sin(p1) + math.sin(p2),
                     math.hypot(math.cos(p1) + bx, by))
    lon = l1 + math.atan2(by, math.cos(p1) + bx)
    return math.degrees(lat), (math.degrees(lon) + 540) % 360 - 180


def flare_strikes(peak: datetime, station: dict) -> bool:
    """True if the station and at least one path midpoint are sunlit at peak."""
    if solar_elevation_deg(peak, station["lat"], station["lon"]) <= 0:
        return False
    for plat, plon in station["paths"]:
        mlat, mlon = path_midpoint(station["lat"], station["lon"], plat, plon)
        if solar_elevation_deg(peak, mlat, mlon) > 0:
            return True
    return False


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, _TS).replace(tzinfo=timezone.utc)


def orders_from_flares(flares: list, stations: list, now: datetime) -> list:
    """One order per qualifying flare; the first of a duplicated flare wins."""
    orders, seen = [], set()
    for fl in flares:
        try:
            if flare_class_value(fl["max_class"]) < MIN_FLARE_WM2:
                continue
            if not fl.get("end_time"):
                continue                      # still in progress; wait
            peak = _parse(fl["max_time"])
            begin, end = _parse(fl["begin_time"]), _parse(fl["end_time"])
        except (KeyError, ValueError, TypeError, IndexError):
            continue                          # malformed record
        event_id = "%s-flare-%s" % (peak.strftime("%Y%m%dT%H%MZ"), fl["max_class"])
        if event_id in seen:
            continue
        struck = [s for s in stations if flare_strikes(peak, s)]
        if not struck:
            continue
        seen.add(event_id)
        names = ", ".join(s["id"].split("-")[-1] + "'s" if len(struck) == 1
                          else s["id"] for s in struck)
        orders.append({
            "event_id": event_id,
            "name": "GOES %s flare, peak %s" % (fl["max_class"], peak.strftime("%Y-%m-%d %H:%MZ")),
            "kind": "flare",
            "ordered_by": "goes-trigger",
            "reason": "GOES %s, %s paths sunlit at peak" % (fl["max_class"], names),
            "start_utc": (begin - timedelta(minutes=PRE_MIN)).strftime(_TS),
            "end_utc": (end + timedelta(minutes=POST_MIN)).strftime(_TS),
            "stations": [s["id"] for s in struck],
            "record": [{"client": "hf-timestd", "keep": "time-channels"}],
            "destination": "wd30",
            "created_utc": now.astimezone(timezone.utc).strftime(_TS),
            "source": fl,
        })
    return orders


def merge_orders(existing: dict, new: list) -> dict:
    """Add orders whose event_id is new; existing entries stay untouched."""
    orders = list(existing.get("orders", []))
    have = {o["event_id"] for o in orders}
    for o in new:
        if o["event_id"] not in have:
            orders.append(o)
            have.add(o["event_id"])
    return {"version": 1, "orders": orders}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stations", required=True, help="JSON list of stations")
    ap.add_argument("--out", required=True, help="orders.json to merge into")
    ap.add_argument("--url", default=FEED_URL)
    args = ap.parse_args(argv)
    try:
        with open(args.stations) as f:
            stations = json.load(f)
        with urllib.request.urlopen(args.url, timeout=60) as r:
            flares = json.load(r)
        try:
            with open(args.out) as f:
                existing = json.load(f)
        except FileNotFoundError:
            existing = {"version": 1, "orders": []}
    except Exception as exc:                  # leave the orders file alone
        print("flare_orders: %s" % exc, file=sys.stderr)
        return 1
    merged = merge_orders(existing, orders_from_flares(flares, stations,
                                                       datetime.now(timezone.utc)))
    tmp = args.out + ".tmp"
    with open(tmp, "w") as f:
        json.dump(merged, f, indent=2)
        f.write("\n")
    os.replace(tmp, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
