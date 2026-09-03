#!/usr/bin/env python3
"""`trust` on a refclock must not blind the cross-check that polices it.

⛔ AC0G-ND, 2026-09-03.  The station's RX888 was not governed by its GPSDO
(the LBE-Mini's OUT1 drive sat at 8 mA), so it sampled ~350 ppm fast.
hf-timestd's FUSE derives from those samples and inherited the error.  `trust`
on the FUSE refclock then told chrony to believe it over the network, and chrony
walked the host clock TWELVE SECONDS off UTC:

    #* FUSE             -174us
    ^x 144.202.66.214  -12.3s
    ^x 72.87.88.202    -12.2s
    ^x 172.234.37.140  -12.0s

METROLOGY.md §4.5 already specifies the rule that should have caught this —
"asymmetric T3 ↔ T2 gross delta (> 1 s) → force T3 down" — and it was
implemented.  It never fired, because it is gated on `"T2" in witnesses` and the
witness set was EMPTY (`smd timing` reported `witnesses: -`).

The reason is the circularity these tests exist to prevent: `trust` makes chrony
mark the whole WAN pool `x`, the T2 probe requires state `*`/`+`, so T2 went
unavailable and stopped witnessing.  **`trust` disabled the very check written to
catch a wildly-wrong Fusion.**

The distinction that fixes it: a source can carry a good MEASUREMENT while
chrony refuses to SELECT it.  A falseticker verdict on a WAN server, when a
local refclock is trusted, records only that it disagrees with that refclock —
which is precisely the disagreement the rule needs.  So such a source witnesses
(`ProbeResult.witness_only`) and is never selectable.

This matters because on a station with no TS-1 (T6), no LBE-142x (T5) and no
stratum-1 LAN server (T4), T3 IS the top authority and must beat WAN NTP —
`trust` is correct there.  It stays correct only while something can still tell
us when T3 has gone wrong.
"""
import unittest
from dataclasses import dataclass

from hf_timestd.core.authority_manager import ProbeResult
from hf_timestd.core.chrony_tracking_probe import (
    ChronyTrackingProbe,
    match_any_server_not_in,
)


@dataclass
class _Completed:
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0


def _runner(stdout: str):
    def _run(*_a, **_kw):
        return _Completed(stdout=stdout)
    return _run


# `chronyc -n -c sources`: mode,state,name,stratum,poll,reach,lastrx,offset,error,...
# ND as found, with `trust` in force: FUSE selected, whole pool falsetickered.
ND_SOURCES = "\n".join([
    "#,*,FUSE,0,4,125,5,-0.000174,-0.000576,0.0015",
    "^,x,144.202.66.214,4,10,377,48,-12.3,-12.3,0.048",
    "^,x,72.87.88.202,2,9,375,407,-12.2,-12.2,0.135",
    "^,x,172.234.37.140,2,10,377,633,-12.0,-12.0,0.087",
])

HEALTHY_SOURCES = "\n".join([
    "#,*,FUSE,0,4,125,5,-0.000174,-0.000576,0.0015",
    "^,+,144.202.66.214,4,10,377,48,0.002,0.002,0.048",
])


def _t2_probe(stdout: str, **kw) -> ChronyTrackingProbe:
    return ChronyTrackingProbe(
        t_level="T2",
        source_matcher=match_any_server_not_in([]),
        runner=_runner(stdout),
        **kw,
    )


class FalsetickerStillWitnessesTest(unittest.TestCase):

    def test_without_the_opt_in_a_falsetickered_pool_stops_witnessing(self):
        """The behaviour that hid the fault — held here so the gap is explicit."""
        r = _t2_probe(ND_SOURCES).poll()
        self.assertFalse(r.available)
        self.assertFalse(r.witness_only)
        self.assertIsNone(r.offset_ms)
        self.assertIn("unhealthy", r.reason or "")

    def test_with_the_opt_in_it_witnesses_and_carries_the_measurement(self):
        r = _t2_probe(ND_SOURCES, witness_state_chars="x-").poll()
        self.assertTrue(r.witness_only)
        self.assertFalse(r.available, "a falseticker must never be selectable")
        self.assertIsNotNone(r.offset_ms)
        self.assertAlmostEqual(r.offset_ms, -12300.0, places=0)

    def test_a_healthy_pool_is_unaffected_by_the_opt_in(self):
        r = _t2_probe(HEALTHY_SOURCES, witness_state_chars="x-").poll()
        self.assertTrue(r.available)
        self.assertFalse(r.witness_only)

    def test_an_unreachable_falseticker_still_says_nothing(self):
        # reach 0 means chrony has no measurement at all.
        unreached = "^,x,a,2,10,0,0,0.0,0.0,0.0\n^,x,b,2,10,0,0,0.0,0.0,0.0"
        r = _t2_probe(unreached, witness_state_chars="x-").poll()
        self.assertFalse(r.available)
        self.assertFalse(r.witness_only)

    def test_state_question_mark_also_witnesses(self):
        """`trust` yields `x` when the pool DISAGREES with the refclock and `?`
        when it merely loses to it.  ND showed `x` at 12 s and `?` once the
        offsets came back to milliseconds — both still carry a measurement."""
        q = "\n".join([
            "#,*,FUSE,0,4,125,16,0.001749,0.000924,0.0015",
            "^,?,23.186.168.131,2,6,177,9,-0.000813,-0.001638,0.079",
            "^,?,162.254.225.151,2,6,177,10,0.009126,0.008301,0.065",
        ])
        r = _t2_probe(q, witness_state_chars="x-?").poll()
        self.assertTrue(r.witness_only)
        self.assertFalse(r.available)
        self.assertIsNotNone(r.offset_ms)

    def test_a_witness_whose_last_poll_failed_is_dropped(self):
        """reach is an OCTAL shift register with the newest poll in the low bit.
        A witness-only source is not selected by chrony, so nothing else vouches
        for its freshness; a stale offset must not cross-check a live tier.
        `376` = seven good polls then a miss."""
        stale = "^,?,a,2,6,376,300,-12.0,-12.0,0.05"
        r = _t2_probe(stale, witness_state_chars="x-?").poll()
        self.assertFalse(r.witness_only)
        self.assertFalse(r.available)

    def test_a_selectable_source_is_not_held_to_the_stricter_bar(self):
        """`377` and `376` both select fine; only witness-only rows tighten."""
        ok = "^,+,a,2,6,376,9,0.002,0.002,0.05"
        r = _t2_probe(ok, witness_state_chars="x-?").poll()
        self.assertTrue(r.available)
        self.assertFalse(r.witness_only)


class GrossErrorRuleFiresAgainTest(unittest.TestCase):
    """The §4.5 asymmetric T3↔T2 rule, against ND's actual numbers."""

    @staticmethod
    def _results(t2: ProbeResult) -> dict:
        base = {lvl: ProbeResult(lvl, available=False)
                for lvl in ("T6", "T5", "T4", "T3", "T2", "T1", "T0")}
        # Fusion, confident and wrong — as ND published it.
        base["T3"] = ProbeResult("T3", available=True, offset_ms=-0.087,
                                 sigma_ms=3.2, frame="rtp")
        base["T2"] = t2
        return base

    def _cross_check(self, results):
        import tempfile
        from pathlib import Path
        from hf_timestd.core.authority_manager import AuthorityManager
        with tempfile.TemporaryDirectory() as d:
            mgr = AuthorityManager(
                probes=[],
                output_path=Path(d) / "authority.json",
                a_level_provider=lambda: "A1",
            )
            return mgr._cross_check("T3", results)

    def test_a_witness_only_t2_still_forces_t3_down(self):
        t2 = ProbeResult("T2", available=False, witness_only=True,
                         offset_ms=-12200.0, sigma_ms=20.0)
        active, witnesses, flags = self._cross_check(self._results(t2))
        self.assertIn("T2", witnesses, "the witness set must not be empty")
        self.assertTrue(any("asymmetric-T3-T2" in f for f in flags))
        self.assertNotEqual(active, "T3", "a 12 s wrong Fusion must lose")

    def test_it_does_not_promote_a_source_chrony_will_not_steer_to(self):
        t2 = ProbeResult("T2", available=False, witness_only=True,
                         offset_ms=-12200.0, sigma_ms=20.0)
        active, _w, flags = self._cross_check(self._results(t2))
        self.assertNotEqual(active, "T2")
        self.assertTrue(any("witness-only-T2" in f for f in flags))

    def test_a_selectable_t2_is_still_promoted_as_before(self):
        t2 = ProbeResult("T2", available=True, offset_ms=-12200.0, sigma_ms=20.0)
        active, _w, flags = self._cross_check(self._results(t2))
        self.assertEqual(active, "T2")
        self.assertTrue(any("asymmetric-T3-T2" in f for f in flags))

    def test_an_agreeing_t2_leaves_t3_active(self):
        """T3 outranks T2 and must keep the clock when the two agree."""
        t2 = ProbeResult("T2", available=False, witness_only=True,
                         offset_ms=-0.5, sigma_ms=20.0)
        active, witnesses, flags = self._cross_check(self._results(t2))
        self.assertEqual(active, "T3")
        self.assertIn("T2", witnesses)
        self.assertFalse(any("asymmetric-T3-T2" in f for f in flags))


if __name__ == "__main__":
    unittest.main()
