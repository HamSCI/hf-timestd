"""The judge publishes the T3 shadow block its provider hands it (task 14).

The provider runs outside the judge's lock, as the T6 rate provider does.
A provider that raises publishes ``t3_shadow: null`` and the tick completes.
"""
from __future__ import annotations

import json

from hf_timestd.core.offset_judge import BenchReading, OffsetJudge


class _Clock:
    wall = 1_800_000_000.0
    mono = 1000.0


class _Bench:
    def __init__(self, clock):
        self.clock = clock

    def poll(self):
        return BenchReading(tier="T4", utc=self.clock.wall, sigma_ns=1e5,
                            mono=self.clock.mono)


def _judge(tmp_path):
    clock = _Clock()
    return OffsetJudge(
        config={"enabled": True, "chain_delay_learn": False},
        benches=[_Bench(clock)],
        publish_path=tmp_path / "offset_judge.json",
        time_fn=lambda: clock.wall,
        mono_fn=lambda: clock.mono,
    )


def _published(tmp_path):
    return json.loads((tmp_path / "offset_judge.json").read_text())


BLOCK = {
    "t3_minus_t6_ms": 0.3, "t6_chain_delay_ms": 1.25,
    "t6_anchor_input_index": 132_451_200.0, "t6_anchor_utc_ns": 1_800_000_002_044_000_000,
    "t3_rtp_domain": "SHARED_10000", "t3_state": "ACQUIRED", "t3_sigma_ms": 0.8,
}


def test_judge_publishes_the_provider_block(tmp_path):
    judge = _judge(tmp_path)
    judge.set_t3_shadow_provider(lambda: dict(BLOCK))
    judge.tick()
    assert _published(tmp_path)["t3_shadow"] == BLOCK


def test_no_provider_publishes_null(tmp_path):
    judge = _judge(tmp_path)
    judge.tick()
    data = _published(tmp_path)
    assert "t3_shadow" in data and data["t3_shadow"] is None


def test_a_failing_provider_publishes_null_and_ticks_on(tmp_path):
    judge = _judge(tmp_path)
    calls = []

    def provider():
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("summary unreadable")
        return dict(BLOCK)

    judge.set_t3_shadow_provider(provider)
    judge.tick()
    assert _published(tmp_path)["t3_shadow"] == BLOCK
    judge.tick()                                  # must not raise
    data = _published(tmp_path)
    assert data["t3_shadow"] is None              # a stale block never lingers
    assert data["judge"]["tier"] == "T4"          # the tick ran to the end
    judge.tick()
    assert _published(tmp_path)["t3_shadow"] == BLOCK


def test_the_provider_runs_outside_the_lock(tmp_path):
    judge = _judge(tmp_path)
    seen = []

    def provider():
        got = judge._lock.acquire(blocking=False)
        seen.append(got)
        if got:
            judge._lock.release()
        return None

    judge.set_t3_shadow_provider(provider)
    judge.tick()
    assert seen == [True]
