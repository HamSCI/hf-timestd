"""The judge gates T6 against the station's stored chain delay.

Spec: docs/superpowers/specs/2026-10-27-t6-chain-delay-learning-design.md
§4 (adoption against the stored value) and §6 (the gross-error backstop).

The stored value is a diagnostic: it enters only the cross-bench delta and
the band arithmetic, never a timestamp, the chrony feed or the T6 anchor.
"""
from hf_timestd.core import offset_judge
from hf_timestd.core.chain_delay_store import ChainDelayRecord
from hf_timestd.core.core_recorder_v2 import chain_delay_identity_for
from hf_timestd.core.offset_judge import BenchReading, OffsetJudge


class FakeClock:
    def __init__(self, wall=1_700_000_000.0, mono=1_000.0):
        self.wall = wall
        self.mono_v = mono

    def time(self):
        return self.wall

    def mono(self):
        return self.mono_v


def reading(tier, utc, mono, sigma_ns=1e6, plane=None):
    kw = {}
    if plane is not None:
        kw["plane"] = plane
    return BenchReading(tier=tier, utc=utc, sigma_ns=sigma_ns, mono=mono, **kw)


def make_judge(tmp_path, **cfg):
    """A judge on a FakeClock, reachable as ``j._test_clock`` so later
    tests can advance time."""
    clock = FakeClock()
    j = OffsetJudge(
        config={"enabled": True, "tick_seconds": 10.0, **cfg},
        benches=[],
        publish_path=tmp_path / "offset_judge.json",
        time_fn=clock.time,
        mono_fn=clock.mono,
    )
    j._test_clock = clock
    return j


T = 1_700_000_000.0
SEED = dict(offset_ns=-16_350_000.0, sigma_ns=1_100_000.0, spread_ns=1_100_000.0,
            n=783, n_eff=100.0, span_s=49_000.0, learned_from_utc="x",
            learned_to_utc="y", lower_tier="T4", state="seeded")


def seeded_judge(tmp_path, **cfg):
    j = make_judge(tmp_path, chain_delay_path=str(tmp_path / "cd.json"), **cfg)
    j.chain_delay_store.set_record(ChainDelayRecord(**SEED))
    return j


def test_record_in_force_admits_t6_at_its_chain_delay(tmp_path):
    j = seeded_judge(tmp_path)
    t6 = reading("T6", T - 0.0170, 1.0, sigma_ns=900_000.0, plane="label")
    t4 = reading("T4", T, 1.0, sigma_ns=200_000.0)
    assert j._cross_gate_ok_locked(t6, t4, 1.0)


def test_record_in_force_refuses_t6_far_from_it(tmp_path):
    j = seeded_judge(tmp_path)
    t6 = reading("T6", T - 0.0320, 1.0, sigma_ns=900_000.0, plane="label")
    assert not j._cross_gate_ok_locked(t6, reading("T4", T, 1.0, sigma_ns=200_000.0), 1.0)


def test_gross_bound_refuses_without_a_record(tmp_path):
    j = make_judge(tmp_path, chain_delay_path=str(tmp_path / "cd.json"))
    t6 = reading("T6", T - 0.298, 1.0, sigma_ns=900_000.0, plane="label")
    assert not j._t6_gross_ok(t6, reading("T4", T, 1.0, sigma_ns=200_000.0), -298e6)


def test_bootstrap_adopts_t6_with_a_record(tmp_path):
    j = seeded_judge(tmp_path)
    readings = [reading("T6", T - 0.0165, 1.0, sigma_ns=900_000.0, plane="label"),
                reading("T4", T, 1.0, sigma_ns=200_000.0)]
    assert j._select_bench_locked(readings).tier == "T6"


def test_record_survives_a_new_judge(tmp_path):
    seeded_judge(tmp_path)
    j2 = make_judge(tmp_path, chain_delay_path=str(tmp_path / "cd.json"))
    assert j2.chain_delay_store.record.offset_ns == SEED["offset_ns"]


def test_learn_off_is_todays_gate(tmp_path):
    j = make_judge(tmp_path, chain_delay_learn=False)
    assert j.chain_delay_store is None
    t6 = reading("T6", T - 0.0165, 1.0, sigma_ns=900_000.0, plane="label")
    assert not j._cross_gate_ok_locked(t6, reading("T4", T, 1.0, sigma_ns=200_000.0), 1.0)


def test_station_without_t6_writes_no_record(tmp_path):
    j = make_judge(tmp_path, chain_delay_path=str(tmp_path / "cd.json"))
    for _ in range(5):
        j._select_bench_locked([reading("T4", T, 1.0, sigma_ns=200_000.0),
                                reading("T3", T, 1.0, sigma_ns=2e6)])
    assert not (tmp_path / "cd.json").exists()


def test_snapshot_publishes_the_record(tmp_path):
    j = seeded_judge(tmp_path)
    snap = j._snapshot_locked(1.0)
    assert snap["chain_delay"]["state"] == "seeded"
    assert snap["chain_delay"]["record"]["offset_ns"] == SEED["offset_ns"]


# ── beyond the brief's list: the edges the rules name ──────────────────


def test_gross_bound_admits_a_plausible_chain_delay(tmp_path):
    j = make_judge(tmp_path, chain_delay_path=str(tmp_path / "cd.json"))
    t6 = reading("T6", T - 0.0165, 1.0, sigma_ns=900_000.0, plane="label")
    assert j._t6_gross_ok(t6, reading("T4", T, 1.0, sigma_ns=200_000.0), -16.5e6)


def test_gross_bound_refuses_before_the_band(tmp_path):
    """A record centred far out cannot buy admission past the gross bound."""
    j = make_judge(tmp_path, chain_delay_path=str(tmp_path / "cd.json"))
    j.chain_delay_store.set_record(
        ChainDelayRecord(**{**SEED, "offset_ns": -298_000_000.0}))
    t6 = reading("T6", T - 0.298, 1.0, sigma_ns=900_000.0, plane="label")
    assert not j._cross_gate_ok_locked(t6, reading("T4", T, 1.0, sigma_ns=200_000.0), 1.0)


# Deltas compare to 1 us: a float holds T = 1.7e9 s only to ~240 ns.


def test_without_a_record_the_gate_keeps_todays_plane_term(tmp_path):
    j = make_judge(tmp_path, chain_delay_path=str(tmp_path / "cd.json"),
                   label_plane_offset_ns=-16_500_000.0)
    t6 = reading("T6", T - 0.0165, 1.0, sigma_ns=900_000.0, plane="label")
    t4 = reading("T4", T, 1.0, sigma_ns=200_000.0)
    assert abs(j._cross_bench_delta_ns(t6, t4, 1.0)) < 1_000.0
    assert j._cross_gate_ok_locked(t6, t4, 1.0)


def test_record_supplies_the_plane_term_for_t6_only(tmp_path):
    j = seeded_judge(tmp_path, label_plane_offset_ns=-5_000_000.0)
    t4 = reading("T4", T, 1.0, sigma_ns=200_000.0)
    t6 = reading("T6", T - 0.0170, 1.0, sigma_ns=900_000.0, plane="label")
    t3 = reading("T3", T - 0.0170, 1.0, sigma_ns=900_000.0, plane="label")
    assert abs(j._cross_bench_delta_ns(t6, t4, 1.0) - (-650_000.0)) < 1_000.0
    # A non-T6 label bench keeps today's term (the config value here).
    assert abs(j._cross_bench_delta_ns(t3, t4, 1.0) - (-12_000_000.0)) < 1_000.0


def test_band_widens_with_stored_sigma_and_spread(tmp_path):
    j = seeded_judge(tmp_path)
    t6 = reading("T6", T, 1.0, sigma_ns=900_000.0, plane="label")
    t4 = reading("T4", T, 1.0, sigma_ns=200_000.0)
    expected = 5.0 * (0.9e6 ** 2 + 0.2e6 ** 2 + 1.1e6 ** 2 + 1.1e6 ** 2) ** 0.5
    assert abs(j._chain_band_ns(t6, t4) - expected) < 1.0


def test_band_caps_each_bench_sigma_at_its_tier(tmp_path):
    j = seeded_judge(tmp_path)
    t6 = reading("T6", T, 1.0, sigma_ns=50e6, plane="label")    # cap 5 ms
    t4 = reading("T4", T, 1.0, sigma_ns=50e6)                    # cap 10 ms
    expected = 5.0 * (5e6 ** 2 + 10e6 ** 2 + 1.1e6 ** 2 + 1.1e6 ** 2) ** 0.5
    assert abs(j._chain_band_ns(t6, t4) - expected) < 1.0


def test_snapshot_without_a_record_says_none(tmp_path):
    j = make_judge(tmp_path, chain_delay_path=str(tmp_path / "cd.json"))
    block = j._snapshot_locked(1.0)["chain_delay"]
    assert block == {"state": "none", "record": None, "candidate": None,
                     "changed": None, "learning": None}


def test_default_path_is_redirected_away_from_var_lib(tmp_path):
    """The autouse fixture keeps a default-path judge off /var/lib."""
    assert not offset_judge.DEFAULT_CHAIN_DELAY_PATH.startswith("/var/lib")
    j = make_judge(tmp_path)
    assert str(j.chain_delay_store.path) == offset_judge.DEFAULT_CHAIN_DELAY_PATH


def test_recorder_builds_identity_from_t6_section():
    ident = chain_delay_identity_for({"t6_pps": {
        "frequency_hz": 45_375_000, "sample_rate": 96_000,
        "low_edge_hz": -25_000, "high_edge_hz": 25_000}})
    assert ident == {"t6_frequency_hz": 45_375_000.0, "t6_sample_rate": 96_000.0,
                     "low_edge_hz": -25_000.0, "high_edge_hz": 25_000.0}


def test_recorder_identity_falls_back_to_legacy_section():
    ident = chain_delay_identity_for({"l6_pps": {"frequency_hz": 10e6}})
    assert ident == {"t6_frequency_hz": 10e6, "t6_sample_rate": 0.0,
                     "low_edge_hz": 0.0, "high_edge_hz": 0.0}
