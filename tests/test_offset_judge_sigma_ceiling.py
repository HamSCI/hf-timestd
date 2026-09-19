"""A worse sigma must not buy a wider licence."""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hf_timestd.core import offset_judge


def _bound_ms(sigma_c_ms, sigma_l_ms, k=5.0):
    """The bound as the gate computes it, after capping."""
    c = min(sigma_c_ms, offset_judge.CROSS_BENCH_SIGMA_CEILING_MS)
    l = min(sigma_l_ms, offset_judge.CROSS_BENCH_SIGMA_CEILING_MS)
    return k * math.sqrt(c * c + l * l)


class TestCeiling(unittest.TestCase):

    def test_the_ceiling_exists(self):
        self.assertTrue(hasattr(offset_judge, "CROSS_BENCH_SIGMA_CEILING_MS"))

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
        self.assertGreaterEqual(offset_judge.CROSS_BENCH_SIGMA_CEILING_MS, 5.0)


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


if __name__ == "__main__":
    unittest.main()
