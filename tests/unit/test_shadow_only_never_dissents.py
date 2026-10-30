"""A shadow-only bench never convicts T6 and never reaches chrony.

Final review of the T3 foundations branch, finding I-1 (2026-10-30).
``HfAcquiredBench`` (the stand-alone T3 registration) stays shadow-only
until step 7.  Its readings reached ``shadow_residuals``, and witness
dissent treated every shadow entry as a witness.  T3 joined by one more
witness at a similar offset could then convict T6, and the recorder's
HPPS push would widen its ``precision`` to chrony on T3's word.

Michael's ruling: T3 votes nowhere until step 7 admits it.  The reading
stays in ``shadow_residuals`` for the record, and nothing else.
"""

from types import SimpleNamespace

from hf_timestd.core.core_recorder_v2 import CoreRecorderV2
from hf_timestd.core.offset_judge import BenchReading, OffsetJudge

SR = 24000
WALL0 = 1_800_000_000.0
KEY = ("hf-status.local", 0xABCD1234)
OFF_S = 0.010          # 10 ms from T6: far past 5 sigma for T3 and T4


class _Clock:
    def __init__(self):
        self.mono = 1000.0
        # The witnesses' common departure from T6.  Zero while T6 earns
        # adoption through the cross-bench gate, then OFF_S.
        self.departure_s = 0.0

    def wall(self):
        return WALL0 + (self.mono - 1000.0)


class _Bench:
    """A bench that reads ``offset_s`` from true UTC, now."""

    def __init__(self, clock, tier, sigma_ns, bench, offset_s=0.0,
                 plane="host", authoritative=None):
        self.clock, self.tier, self.sigma_ns = clock, tier, sigma_ns
        self.bench, self.offset_s, self.plane = bench, offset_s, plane
        self.authoritative = authoritative

    def poll(self):
        detail = {"bench": self.bench}
        if self.authoritative is not None:
            detail["authoritative"] = self.authoritative
        return BenchReading(tier=self.tier,
                            utc=(self.clock.wall()
                                 + self.offset_s * self.clock.departure_s
                                 / OFF_S),
                            sigma_ns=self.sigma_ns, mono=self.clock.mono,
                            detail=detail, plane=self.plane)


def _unix_to_gps_ns(unix_s):
    from hf_timestd.core.buffer_timing import unix_ns_to_gps_time_ns

    return unix_ns_to_gps_time_ns(int(round(unix_s * 1e9)))


def _run(tmp_path, witnesses, seconds=200, step=10.0):
    clock = _Clock()
    t6 = _Bench(clock, "T6", 10_000.0, "native_anchor", plane="label")
    benches = [t6] + [w(clock) for w in witnesses]
    judge = OffsetJudge(
        config={"enabled": True, "upgrade_polls": 1},
        benches=benches,
        publish_path=tmp_path / "offset_judge.json",
        time_fn=clock.wall,
        mono_fn=lambda: clock.mono,
    )
    judge.register_radiod_pair(KEY, _unix_to_gps_ns(WALL0), 0, SR)
    for _ in range(3):              # every witness agrees: T6 adopted
        judge.tick()
        clock.mono += step
    assert judge._best.tier == "T6"
    clock.departure_s = OFF_S       # then T6 walks 10 ms off
    t = 0.0
    while t <= seconds:
        judge.tick()
        clock.mono += step
        t += step
    return judge


def _t4(clock):
    return _Bench(clock, "T4", 200_000.0, "chrony_lan", offset_s=OFF_S)


def _t5(clock):
    return _Bench(clock, "T5", 200_000.0, "peer", offset_s=OFF_S)


def _hf_acquired(clock):
    return _Bench(clock, "T3", 1_000_000.0, "hf_acquired", offset_s=OFF_S,
                  plane="label", authoritative=False)


def _hpps_floor(judge):
    return CoreRecorderV2._t6_dissent_sigma_floor_ns(
        SimpleNamespace(_offset_judge=judge))


def test_two_real_witnesses_convict_t6(tmp_path):
    """Control: the harness can convict, so the next test has power."""
    judge = _run(tmp_path, [_t4, _t5])
    assert judge._best.tier == "T6"
    assert judge._dissent is not None
    assert _hpps_floor(judge) is not None


def test_far_off_hf_acquired_neither_dissents_nor_widens_hpps(tmp_path):
    """T3 and T4 agree 10 ms from T6 for 200 s: no dissent, no floor."""
    judge = _run(tmp_path, [_t4, _hf_acquired])
    assert judge._best.tier == "T6"
    # The record keeps T3's reading ...
    assert "T3" in judge._shadow_residuals
    assert abs(judge._shadow_residuals["T3"]["shadow_residual_ns"]
               - OFF_S * 1e9) < 1e6
    # ... and nothing acts on it.
    assert judge._dissent is None
    assert _hpps_floor(judge) is None


def test_hf_acquired_adds_nothing_to_a_standing_dissent(tmp_path):
    """With T4 and T5 convicting, T3 is not named among the witnesses."""
    judge = _run(tmp_path, [_t4, _t5, _hf_acquired])
    assert judge._dissent is not None
    assert "T3" not in judge._dissent.tiers
