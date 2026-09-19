"""A worse sigma must not buy a wider licence."""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hf_timestd.core import offset_judge


def _bound_ms(sigma_c_ms, sigma_l_ms, k=5.0, cand_tier="T6", ref_tier="T5"):
    """The bound as the gate computes it, after per-tier capping.

    ⚠ This mirrors the formula and therefore cannot notice the cap being
    wired into the wrong expression.  TestRealGatePath below is the one
    with power; these cases pin the CONSTANTS and the historical premise.
    """
    c = min(sigma_c_ms, offset_judge.cross_bench_sigma_ceiling_ms(cand_tier))
    l = min(sigma_l_ms, offset_judge.cross_bench_sigma_ceiling_ms(ref_tier))
    return k * math.sqrt(c * c + l * l)


class TestCeiling(unittest.TestCase):

    def test_the_ceiling_exists(self):
        self.assertTrue(
            hasattr(offset_judge, "CROSS_BENCH_SIGMA_CEILING_MS_BY_TIER"))
        self.assertTrue(hasattr(offset_judge, "cross_bench_sigma_ceiling_ms"))

    def test_477_ms_no_longer_admits_284_ms(self):
        """The AI6VN case of 2026-09-18.  Uncapped, the bound reached
        ~2.4 s and 284 ms sailed through."""
        uncapped = 5.0 * math.sqrt(477.0 ** 2 + 2.0 ** 2)
        self.assertGreater(uncapped, 284.0, "premise: it used to pass")
        self.assertLess(_bound_ms(477.0, 2.0), 284.0)

    def test_an_honest_narrow_sigma_is_untouched(self):
        """T6 at 1 us against T5 at 1 ms.  The cap must not tighten a
        pair that was already inside it."""
        capped = _bound_ms(0.001, 1.0)
        uncapped = 5.0 * math.sqrt(0.001 ** 2 + 1.0 ** 2)
        self.assertAlmostEqual(capped, uncapped, places=6)

    def test_a_legitimately_wide_bench_still_gets_its_room(self):
        """T5 over USB is bus-jitter floored near 1 ms; the §4.5 table
        allows 5 ms for the T6/T5 pair.  The ceiling must not fall
        below that."""
        self.assertGreaterEqual(
            offset_judge.cross_bench_sigma_ceiling_ms("T5"), 5.0)


class TestRealGatePath(unittest.TestCase):
    """Drives OffsetJudge._cross_gate_ok_locked itself -- the production
    comparison -- rather than a re-implementation of the arithmetic.
    TestCeiling above would pass even if the cap were computed
    correctly but never wired into the gate; this would not.
    """

    def _make_judge(self, tmp_path: str) -> "offset_judge.OffsetJudge":
        return offset_judge.OffsetJudge(
            config={"enabled": True},
            benches=[],
            publish_path=Path(tmp_path) / "offset_judge.json",
            time_fn=lambda: 1_800_000_000.0,
            mono_fn=lambda: 1000.0,
        )

    def test_477ms_sigma_candidate_284ms_off_is_now_refused(self):
        """DASI-009.AI6VN, 2026-09-18: a 477 ms-sigma T6 candidate
        disagreed with a tight T5 reference by 284 ms and the gate
        let it through (uncapped bound ~2.4 s).  Called against the
        real method, this must now come back False."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            judge = self._make_judge(tmp)
            mono_now = 1000.0
            ref = offset_judge.BenchReading(
                tier="T5", utc=1_800_000_000.000, sigma_ns=2_000_000.0,
                mono=mono_now,
            )
            cand = offset_judge.BenchReading(
                tier="T6", utc=1_800_000_000.284, sigma_ns=477_000_000.0,
                mono=mono_now,
            )
            self.assertFalse(
                judge._cross_gate_ok_locked(cand, ref, mono_now),
                "477 ms of candidate sigma must not buy room for a "
                "284 ms disagreement through the real gate",
            )

    def test_a_genuinely_agreeing_wide_candidate_still_passes(self):
        """Sanity counterpart: the cap must not turn the gate into a
        blanket refusal of every wide-sigma candidate -- one that
        actually agrees (here, exactly at UTC) still passes."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            judge = self._make_judge(tmp)
            mono_now = 1000.0
            ref = offset_judge.BenchReading(
                tier="T5", utc=1_800_000_000.000, sigma_ns=2_000_000.0,
                mono=mono_now,
            )
            cand = offset_judge.BenchReading(
                tier="T6", utc=1_800_000_000.000, sigma_ns=477_000_000.0,
                mono=mono_now,
            )
            self.assertTrue(judge._cross_gate_ok_locked(cand, ref, mono_now))

    def test_a_t3_station_keeps_the_room_a_flat_ceiling_took(self):
        """The regression a flat 5 ms ceiling would have caused.

        The gate is tier-agnostic: it caps whatever two benches it is
        handed, so a station with no T6 at all still meets it.  On a
        T3-fusion station the reference bench is chrony, which reports
        root_dispersion + root_delay/2 + |rms_offset| -- routinely well
        past 5 ms.  Measured effect of a flat ceiling there:

            T3 4.3 ms vs chrony 20 ms: bound 102.3 ms -> 33.0 ms, 3.1x

        A fusion offset disagreeing with chrony by 50 ms was adopted
        before and refused after -- on AC0G-ND, whose fusion is
        quorum-starved by construction and whose host clock has run
        150 ms out.  Both benches sit inside their own tier budgets
        (T3 <= 20 ms, T2 <= 100 ms), so neither may be trimmed.
        """
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            judge = self._make_judge(tmp)
            mono_now = 1000.0
            ref = offset_judge.BenchReading(
                tier="T2", utc=1_800_000_000.000, sigma_ns=20_000_000.0,
                mono=mono_now,
            )
            cand = offset_judge.BenchReading(
                tier="T3", utc=1_800_000_000.050, sigma_ns=4_300_000.0,
                mono=mono_now,
            )
            self.assertTrue(
                judge._cross_gate_ok_locked(cand, ref, mono_now),
                "a T3 and a chrony bench both inside their tier budgets "
                "must keep the bound they had before any capping",
            )

    def test_a_t3_claiming_more_than_its_tier_can_deliver_is_trimmed(self):
        """The cap still bites where it should: a T3 reporting 400 ms
        claims an uncertainty its tier cannot produce (METROLOGY §4.5
        puts T3 at 0.5-2 ms on A1, 5-10 ms on A0), so it is trimmed to
        the T3 budget and cannot buy itself a 300 ms licence."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            judge = self._make_judge(tmp)
            mono_now = 1000.0
            ref = offset_judge.BenchReading(
                tier="T2", utc=1_800_000_000.000, sigma_ns=20_000_000.0,
                mono=mono_now,
            )
            cand = offset_judge.BenchReading(
                tier="T3", utc=1_800_000_000.300, sigma_ns=400_000_000.0,
                mono=mono_now,
            )
            self.assertFalse(
                judge._cross_gate_ok_locked(cand, ref, mono_now))

    def test_every_tier_budget_matches_metrology_4_5(self):
        """The budgets are read off METROLOGY.md §4.5's uncertainty
        columns with margin, not invented.  Pinning them here means a
        later edit to either has to face the other."""
        c = offset_judge.cross_bench_sigma_ceiling_ms
        # T6 is ns-class; past a few ms it is broken, not wide.
        self.assertLessEqual(c("T6"), 5.0)
        # T5/T4 sit at "a few ms"; T3 at 0.5-2 ms on A1, 5-10 on A0.
        self.assertGreaterEqual(c("T5"), 5.0)
        self.assertGreaterEqual(c("T4"), 5.0)
        self.assertGreaterEqual(c("T3"), 10.0)
        # T2 is NTP-dominated at 1-50 ms and must not be trimmed for it.
        self.assertGreaterEqual(c("T2"), 50.0)
        # Every tier is looser than T6, which is the whole point.
        for tier in ("T5", "T4", "T3", "T2", "T1"):
            self.assertGreater(c(tier), c("T6"), tier)
        # An unknown tier is generous, not trimmed to nothing.
        self.assertGreaterEqual(c("T9"), 50.0)


if __name__ == "__main__":
    unittest.main()
