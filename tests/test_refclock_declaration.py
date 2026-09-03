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

`trust` exempts FUSE from that vote.  It is safe only because the authority
polices itself: fusion publishes exclusively when
`quality_ok and multi_station and consistent and discontinuity_ok`, and logs
`Chrony feed GATED` with reasons when it will not.  These tests hold both ends
of that bargain — the trust flag, and the self-gate that justifies it.
"""

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CONF = REPO / "config" / "chrony-timestd-refclocks.conf"
FUSION = REPO / "src" / "hf_timestd" / "core" / "multi_broadcast_fusion.py"


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


class FuseDeclarationTest(unittest.TestCase):

    def test_fuse_is_trusted_so_the_network_cannot_outvote_it(self):
        line = _refclock_lines()["FUSE"]
        self.assertIn(" trust", line,
                      "without `trust`, four loose internet sources outvote "
                      "the station's own metrology (AC0G-ND 2026-09-03)")

    def test_fuse_precision_matches_its_own_uncertainty_budget(self):
        # The comment above the line promises +/-0.3-1.0 ms and the writer
        # stamps log2(measured uncertainty) per sample.  The line used to
        # assert 1e-4 — ten times better than both.
        line = _refclock_lines()["FUSE"]
        m = re.search(r"precision\s+(\S+)", line)
        self.assertIsNotNone(m, line)
        self.assertLessEqual(
            float(m.group(1)), 1e-3 + 1e-12,
            "precision must not claim better than the uncertainty budget")
        self.assertGreaterEqual(
            float(m.group(1)), 1e-4,
            "and must not swing so wide it stops being a refclock")

    def test_hpps_is_NOT_trusted(self):
        # ⛔ Guards the guard.  T6 took authority on B4 2026-08-15 and the
        # offset judge screamed 83 ms later.  A tier does not inherit trust
        # from the file it shares; HPPS keeps earning selection on the numbers.
        line = _refclock_lines()["HPPS"]
        self.assertNotIn(" trust", line,
                         "HPPS (T6) must keep earning selection, vote included")

    def test_both_feeds_are_still_declared(self):
        got = set(_refclock_lines())
        self.assertEqual(got, {"FUSE", "HPPS"}, f"unexpected feeds: {got}")


class SelfGateStillJustifiesTrustTest(unittest.TestCase):
    """`trust` is only defensible while fusion refuses to publish junk.

    If the gate is ever removed, `trust` becomes "believe the station no
    matter what", which is precisely the failure the falseticker test exists
    to prevent.  So the gate and the trust flag are tested together, and
    deleting the gate must fail this file rather than quietly widen trust.
    """

    def test_publication_is_gated_on_quality_and_consistency(self):
        src = FUSION.read_text()
        self.assertIn(
            "if quality_ok and multi_station and consistent and discontinuity_ok:",
            src,
            "fusion must publish to chrony only when it believes itself")

    def test_the_gate_reports_why_it_refused(self):
        src = FUSION.read_text()
        self.assertIn("Chrony feed GATED", src,
                      "a gate that refuses silently cannot be diagnosed")

    def test_precision_is_derived_from_measured_uncertainty(self):
        src = FUSION.read_text()
        self.assertIn("np.log2(uncertainty_sec_l2)", src,
                      "the per-sample precision must come from the measured "
                      "uncertainty, not a constant")


if __name__ == "__main__":
    unittest.main()
