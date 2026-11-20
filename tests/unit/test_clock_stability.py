"""Clock stability from T3's own sample-plane phase (task A2).

Every statistical case draws its noise from ``numpy.random.default_rng(seed)``
(PCG64) with the seed the brief names, so the TDEV values hold from run to run.
"""
from __future__ import annotations

from fractions import Fraction

import numpy as np
import pytest

from hf_timestd.core.clock_stability import (
    HOLD_MIN_S, STABILITY_HISTORY_MIN, STABILITY_MIN_POINTS, TAU0_S,
    WINDOW_MAX_S, WINDOW_MIN_S, ClockStability, ClockStabilityEstimator,
    PhasePoint, phase_point_from_summary,
)
from synth_origins import F_IN, ILEN, counter_map_for, olen_for, rtp_at

T0 = 1_800_000_000 // 60 * 60       # a minute stamp, epoch seconds
RUN_A = 1_484_035_174.0
RUN_B = 1_484_099_999.0


def _estimator(phase_ms, run_ids=None) -> ClockStabilityEstimator:
    est = ClockStabilityEstimator()
    for i, x in enumerate(phase_ms):
        run = RUN_A if run_ids is None else run_ids[i]
        est.add(PhasePoint(utc_minute=T0 + TAU0_S * i, phase_ms=float(x), run_id=run))
    return est


def test_builder_constants():
    assert (TAU0_S, STABILITY_HISTORY_MIN, STABILITY_MIN_POINTS) == (60, 1440, 120)
    assert (WINDOW_MIN_S, WINDOW_MAX_S, HOLD_MIN_S) == (1800, 7200, 300)


def test_white_phase_noise_alone_never_turns_up():
    rng = np.random.default_rng(1)
    est = _estimator(rng.normal(0.0, 0.5, 1440))
    assert est.stability().tau_cross_s >= 64 * 60


def test_white_phase_plus_random_walk_crosses_near_theory():
    # sigma_w 0.5 ms white PM + phase random walk 0.2 ms/step, seed 2, 1440 points.
    # TDEV^2 ~ sigma_w^2/m + s^2*m/6, minimum at m* = sqrt(6)*sigma_w/s ~ 6 min.
    rng = np.random.default_rng(2)
    white = rng.normal(0.0, 0.5, 1440)
    walk = np.cumsum(rng.normal(0.0, 0.2, 1440))
    est = _estimator(white + walk)
    s = est.stability()
    assert 120 <= s.tau_cross_s <= 960
    assert s.window_s == 1800 and s.hold_s == 450


def test_a_constant_rate_is_not_wander_but_counts_as_drift():
    # phase ramps 1 us per minute, no noise
    est = _estimator(1e-3 * np.arange(1440))
    s = est.stability()
    assert s.tau_cross_s == max(est.tdev_by_tau())
    assert s.drift_ns_per_s == pytest.approx(1000 / 60, rel=1e-3)


def test_run_boundaries_split_the_series():
    # white PM seed 1, plus a +300 ms step where run_id changes at point 700
    rng = np.random.default_rng(1)
    x = rng.normal(0.0, 0.5, 1440)
    x[700:] += 300.0
    runs = [RUN_A] * 700 + [RUN_B] * 740
    est = _estimator(x, runs)
    assert est.stability().tau_cross_s >= 64 * 60
    # the step itself would dominate every tau if the series were one run
    one_run = _estimator(x)
    assert max(one_run.tdev_by_tau().values()) > 10 * max(est.tdev_by_tau().values())


def test_too_few_points_reads_unmeasured():
    rng = np.random.default_rng(1)
    est_with_119_points = _estimator(rng.normal(0.0, 0.5, 119))
    s = est_with_119_points.stability()
    assert (s.tau_cross_s, s.drift_ns_per_s, s.window_s, s.hold_s) == (None, None, 1800, 300)
    assert s.n_points == 119


def test_a_gpsdo_class_clock_reaches_the_window_cap():
    # white PM 0.3 ms only, seed 3: tau_cross >= 7200 s, so window caps
    rng = np.random.default_rng(3)
    est = _estimator(rng.normal(0.0, 0.3, 1440))
    s = est.stability()
    assert s.tau_cross_s >= 7200
    assert (s.window_s, s.hold_s) == (7200, 1800)


def test_history_keeps_one_day_and_drops_stale_or_repeated_minutes():
    est = _estimator(np.zeros(1500))
    assert est.stability().n_points == STABILITY_HISTORY_MIN
    est.add(PhasePoint(utc_minute=T0, phase_ms=99.0, run_id=RUN_A))     # older
    est.add(PhasePoint(utc_minute=T0 + TAU0_S * 1499, phase_ms=99.0, run_id=RUN_A))
    assert est.stability().n_points == STABILITY_HISTORY_MIN
    assert max(est.tdev_by_tau().values()) < 1e-6


def test_stability_is_plain_and_frozen():
    s = _estimator(np.zeros(200)).stability()
    assert isinstance(s, ClockStability)
    with pytest.raises(Exception):
        s.window_s = 1
    for v in (s.tau_cross_s, s.window_s, s.hold_s, s.n_points):
        assert type(v) is int
    assert type(s.drift_ns_per_s) is float


# --- phase_point_from_summary ------------------------------------------------

T3_K0 = 51
O3 = olen_for(24_000)


def _summary(cmap, err_s: float = 0.0003, block: int = 103) -> dict:
    """A T3 summary 10 ms (240 samples at 24 kHz) into ``block``, labelling
    that sample ``err_s`` after the run epoch plus its index over F_IN."""
    rtp = (rtp_at(block, T3_K0, O3) + 240) % 2**32
    idx = block * ILEN + 240 * ILEN // O3
    return {
        "state": "ACQUIRED", "sigma_ms": 0.8,
        "rtp_ref": rtp,
        "utc_ref": cmap.input_epoch_gps_s + idx / F_IN + err_s,
        "sample_rate": 24_000,
        "counter_map": cmap.to_dict(),
        "_idx": idx,                         # test-only
    }


def test_phase_point_reads_the_summary_through_its_counter_map():
    cmap = counter_map_for(T3_K0, 24_000)
    s = _summary(cmap)
    p = phase_point_from_summary(s, T0)
    assert p is not None
    assert p.utc_minute == T0
    assert p.run_id == cmap.input_epoch_gps_s
    hand = float((Fraction(s["utc_ref"]) - Fraction(cmap.input_epoch_gps_s)
                  - Fraction(s["_idx"], F_IN)) * 1000)
    assert p.phase_ms == pytest.approx(hand, abs=1e-6)
    assert p.phase_ms == pytest.approx(0.3, abs=1e-3)


@pytest.mark.parametrize("mutate", [
    lambda s: s.pop("utc_ref"),
    lambda s: s.pop("rtp_ref"),
    lambda s: s.pop("counter_map"),
    lambda s: s.update(counter_map=None),
    lambda s: s.update(counter_map={"rtp_ref": 1}),
    lambda s: s.update(state="SEEKING"),
    lambda s: s.pop("state"),
    lambda s: s.update(utc_ref="not a number"),
    lambda s: s.update(utc_ref=float("nan")),
    lambda s: s.update(sample_rate=96_000),
    lambda s: s.update(rtp_ref=(s["rtp_ref"] + 2**31) % 2**32),   # beyond reach
])
def test_phase_point_is_none_when_the_summary_cannot_say(mutate):
    s = _summary(counter_map_for(T3_K0, 24_000))
    mutate(s)
    assert phase_point_from_summary(s, T0) is None


def test_phase_point_is_none_without_a_summary():
    assert phase_point_from_summary(None, T0) is None
    assert phase_point_from_summary({}, T0) is None
    assert phase_point_from_summary("garbage", T0) is None


# --- fix round 1: drift only from a long current run -------------------------

def test_a_short_current_run_leaves_drift_unmeasured():
    # a long first run, then a 3-point current run after a restart
    rng = np.random.default_rng(1)
    x = rng.normal(0.0, 0.5, 1440)
    runs = [RUN_A] * 1437 + [RUN_B] * 3
    s = _estimator(x, runs).stability()
    assert s.tau_cross_s is not None
    assert s.drift_ns_per_s is None
    # one point short of STABILITY_MIN_POINTS still reads unmeasured
    runs = [RUN_A] * (1440 - 119) + [RUN_B] * 119
    assert _estimator(x, runs).stability().drift_ns_per_s is None
    runs = [RUN_A] * (1440 - 120) + [RUN_B] * 120
    assert _estimator(x, runs).stability().drift_ns_per_s is not None


def test_drift_ignores_the_previous_runs_slope():
    # first run ramps 1 us per minute; the long current run sits flat
    x = np.concatenate([1e-3 * np.arange(700), np.zeros(740)])
    runs = [RUN_A] * 700 + [RUN_B] * 740
    s = _estimator(x, runs).stability()
    # TDEV is ~0 everywhere, so sigma_y ~ 0 and the flat run's slope is 0
    assert s.drift_ns_per_s == pytest.approx(0.0, abs=1e-6)


def test_a_gps_sized_phase_constant_costs_no_precision():
    # live phases sit near 3e11 ms; a ramp there must read as cleanly as at 0
    est = _estimator(3.1e11 + 1e-3 * np.arange(1440))
    s = est.stability()
    assert s.drift_ns_per_s == pytest.approx(1000 / 60, rel=1e-6)
    # the float PhasePoint itself rounds to one ulp of 3.1e11 (~6e-5 ms);
    # TDEV stays at that input quantisation, adding nothing of its own
    assert max(est.tdev_by_tau().values()) < float(np.spacing(3.1e11))
