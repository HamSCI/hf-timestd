#!/usr/bin/env python3
"""The shipped refclock declaration must not lie, and must not be outvoted.

⛔ AC0G-ND, 2026-09-03.  chrony rejected FUSE as a FALSETICKER while it was,
by chrony's own measurement, the best clock on the station::

    FUSE                    NP=6   Std Dev   348us   Offset  -360us
    208.70.148.101          NP=30  Std Dev  2661us   Offset   +19ms
    LAX1.CALTICK.NET        NP=29  Std Dev    13ms   Offset   +23ms
    149.248.12.167          NP=25  Std Dev    32ms   Offset   +53ms
    time-usw4.crimpac.net   NP=31  Std Dev    12ms   Offset +6240us

The falseticker test is a majority vote rather than a comparison of quality:
four loose internet sources agreeing around +20 ms outvoted one tight local
source at -0.36 ms, and the station threw away its own metrology in favour of
consensus among sources 40-100x worse.  `smd timing` said so directly —
`flags chrony-rejected-FUSE:state=x` — and WSPR/GRAPE ran on the loser.

`trust` exempted FUSE from that vote, on the argument that the authority
polices itself (`quality_ok and multi_station and consistent and
discontinuity_ok`).  ⛔ 2026-09-04 refuted the argument on both stations: the
tick detector searches +-20 ms around the second the HOST clock names, so once
the clock is more than 20 ms off it reports noise peaks as on-time ticks,
fusion never gates, and chrony under `trust` follows the clock wherever it
walks (B4 11.6 s, then 1.0 s more; ND 12 s on 09-03, 1.0 s on 09-04).  The
majority vote the flag disabled is the one thing that stopped it: chronyd
restarted without `trust` stepped both clocks back to their witnesses.  These
tests now hold the flag OFF, and keep the self-gate honest for what it can see.
"""

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CONF = REPO / "config" / "chrony-timestd-refclocks.conf"


def _refclock_lines() -> dict:
    lines = {}
    for raw in CONF.read_text().splitlines():
        raw = raw.strip()
        if not raw.startswith("refclock "):
            continue
        m = re.search(r"refid\s+(\S+)", raw)
        if m:
            lines[m.group(1)] = raw
    return lines


class RefclockDeclarationTest(unittest.TestCase):
    """FUSE RETIRED on 2026-10-22 (review-fusion-kalman-20261022.md
    section 15, ruling 5).  The `trust` history above stays as the reason
    FUSE never earned a vote; tests/test_fuse_retired.py now holds FUSE
    out of the file altogether, and the fusion self-gate that justified it
    left with the write."""

    def test_fuse_is_not_declared(self):
        self.assertNotIn("FUSE", _refclock_lines(),
                         "FUSE retired 2026-10-22; fusion no longer feeds it")

    def test_hpps_is_NOT_trusted(self):
        # ⛔ Guards the guard.  T6 took authority on B4 2026-08-15 and the
        # offset judge screamed 83 ms later.  A tier does not inherit trust
        # from the file it shares; HPPS keeps earning selection on the numbers.
        line = _refclock_lines()["HPPS"]
        self.assertNotIn(" trust", line,
                         "HPPS (T6) must keep earning selection, vote included")

    def test_only_hpps_is_declared(self):
        got = set(_refclock_lines())
        self.assertEqual(got, {"HPPS"}, f"unexpected feeds: {got}")


if __name__ == "__main__":
    unittest.main()
