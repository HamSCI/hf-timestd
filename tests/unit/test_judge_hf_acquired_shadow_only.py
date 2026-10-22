"""The tick registration stays out of the judge's adoption until step 7.

Review of T3 steps 2-5, finding C1 (proofs/review-t3-steps2-5.md,
2026-10-22).  Step 5 took FusionBench out of the judge's default benches.
That left HfAcquiredBench, the tick registration, alone in the judge's T3
slot.  The judge ranks T3 above T2, so on a WAN-only station it would
adopt the registration and correct ring and archive labels by it while
authority.json says T2.  Michael's ruling 4 lets the registration win T3
only after its shadow run beside T6 on B4 (step 7 of the ruled order).

So, until step 7:
  * an ``hf_acquired`` reading is never adopted.  It still feeds the
    shadow residuals, so the shadow run has its numbers;
  * any reading whose ``detail["authoritative"]`` is False (a WITNESS-
    state plane) is never adopted, whatever its bench.
"""

import json

from hf_timestd.core.offset_judge import BenchReading, OffsetJudge

SR = 24000
WALL0 = 1_800_000_000.0
KEY = ("hf-status.local", 0xABCD1234)


class _Fixed:
    def __init__(self, reading):
        self.reading = reading

    def poll(self):
        return self.reading


def _unix_to_gps_ns(unix_s):
    from hf_timestd.core.buffer_timing import unix_ns_to_gps_time_ns

    return unix_ns_to_gps_time_ns(int(round(unix_s * 1e9)))


def _t2():
    return BenchReading(tier="T2", utc=WALL0, sigma_ns=20_000_000.0,
                        mono=1000.0, detail={"bench": "chrony"})


def _hf_acquired(authoritative=True):
    return BenchReading(
        tier="T3", utc=WALL0, sigma_ns=1_000_000.0, mono=1000.0,
        detail={"bench": "hf_acquired", "authoritative": authoritative},
        plane="label")


def _t6():
    return BenchReading(tier="T6", utc=WALL0, sigma_ns=10_000.0,
                        mono=1000.0, detail={"bench": "native_anchor"},
                        plane="label")


def _judge(tmp_path, readings, ticks=6):
    judge = OffsetJudge(
        config={"enabled": True, "upgrade_polls": 3},
        benches=[_Fixed(r) for r in readings],
        publish_path=tmp_path / "offset_judge.json",
        time_fn=lambda: WALL0,
        mono_fn=lambda: 1000.0,
    )
    judge.register_radiod_pair(KEY, _unix_to_gps_ns(WALL0), 0, SR)
    for _ in range(ticks):
        judge.tick()
    return judge


def _published(tmp_path):
    return json.loads((tmp_path / "offset_judge.json").read_text())


def test_wan_only_station_adopts_t2_not_the_registration(tmp_path):
    judge = _judge(tmp_path, [_t2(), _hf_acquired()])
    verdict = judge.offset_for(KEY, 0)
    assert verdict is not None and verdict.tier == "T2"
    # The registration still witnesses: the shadow run keeps its numbers.
    assert "T3" in _published(tmp_path)["shadow_residuals"]


def test_registration_alone_is_never_adopted(tmp_path):
    judge = _judge(tmp_path, [_hf_acquired()])
    verdict = judge.offset_for(KEY, 0)
    assert verdict is None or verdict.tier != "T3"


def test_t6_station_still_adopts_t6(tmp_path):
    judge = _judge(tmp_path, [_t2(), _hf_acquired(), _t6()])
    verdict = judge.offset_for(KEY, 0)
    assert verdict is not None and verdict.tier == "T6"


def test_a_witness_bench_is_never_adopted(tmp_path):
    """Any bench, not only hf_acquired: authoritative False means witness."""
    witness_t4 = BenchReading(
        tier="T4", utc=WALL0, sigma_ns=200_000.0, mono=1000.0,
        detail={"bench": "some_future_bench", "authoritative": False})
    judge = _judge(tmp_path, [_t2(), witness_t4])
    verdict = judge.offset_for(KEY, 0)
    assert verdict is not None and verdict.tier == "T2"


def test_an_authoritative_higher_bench_still_wins(tmp_path):
    """Control: the refusal must not refuse everything."""
    t4 = BenchReading(tier="T4", utc=WALL0, sigma_ns=200_000.0, mono=1000.0,
                      detail={"bench": "chrony_lan", "authoritative": True})
    judge = _judge(tmp_path, [_t2(), t4])
    verdict = judge.offset_for(KEY, 0)
    assert verdict is not None and verdict.tier == "T4"
