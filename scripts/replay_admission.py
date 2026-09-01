#!/usr/bin/env python3
"""Replay the three-key admission cascade against an archived timestd.db.

Read-only.  Never connects to a station.

    scripts/replay_admission.py /path/to/copy-of-timestd.db \
        --channel SHARED_10000 --floor-snr-db 10.0

⚠ The thresholds below are PROVISIONAL.  Their values are what this harness
exists to determine; do not treat the defaults as settled policy.
"""

import argparse
import sys

from hf_timestd.replay.report import summarise
from hf_timestd.replay.runner import replay
from hf_timestd.replay.window_source import WindowSource

# AC0G / B4, Columbia MO.
DEFAULT_LAT, DEFAULT_LON = 38.9187497, -92.1277207


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("db_path")
    p.add_argument("--channel", default=None)
    p.add_argument("--start-utc", type=int, default=None)
    p.add_argument("--end-utc", type=int, default=None)
    p.add_argument("--floor-snr-db", type=float, default=10.0)
    p.add_argument("--tolerance-ms", type=float, default=1.0)
    p.add_argument("--lookback", type=int, default=10)
    p.add_argument("--reacquire-after", type=int, default=3)
    p.add_argument("--reference-sigma-ms", type=float, default=0.7)
    p.add_argument("--lat", type=float, default=DEFAULT_LAT)
    p.add_argument("--lon", type=float, default=DEFAULT_LON)
    args = p.parse_args(argv)

    source = WindowSource(receiver_lat=args.lat, receiver_lon=args.lon,
                          reference_sigma_ms=args.reference_sigma_ms)
    filters = {}
    if args.channel:
        filters["channel"] = args.channel
    if args.start_utc is not None:
        filters["start_utc"] = args.start_utc
    if args.end_utc is not None:
        filters["end_utc"] = args.end_utc

    report = summarise(replay(
        args.db_path, source, floor_snr_db=args.floor_snr_db,
        tolerance_ms=args.tolerance_ms, lookback=args.lookback,
        reacquire_after=args.reacquire_after, **filters))

    if report.minutes == 0:
        # A gap in the data must never look like a clean, quiet result: say
        # plainly what was asked for and fail loudly rather than print a
        # full-looking report of zeroes.
        asked = [f"channel={args.channel}" if args.channel else "channel=<all>"]
        if args.start_utc is not None:
            asked.append(f"start_utc={args.start_utc}")
        if args.end_utc is not None:
            asked.append(f"end_utc={args.end_utc}")
        print(f"no minutes matched this selection ({', '.join(asked)}) "
              f"in {args.db_path} — 0 minutes replayed", file=sys.stderr)
        return 1

    print(report.render())
    return 0


if __name__ == "__main__":
    sys.exit(main())
