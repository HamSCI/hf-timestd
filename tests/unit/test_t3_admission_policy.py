"""T3 admission policy (task A5): route 1, modes, jumps, reasons.

Minutes sit 60 s apart from 0.  With no stability history the clock reads
unmeasured, so the window spans 1800 s (30 minutes), the hold 300 s, and the
fill needs 20 pairs.
"""
import json
import logging

import pytest

from hf_timestd.core.clock_stability import (
    HOLD_MIN_S, WINDOW_MIN_S, ClockStability, PhasePoint)
from hf_timestd.core.t3_admission import AdmissionMode
from hf_timestd.core.t3_admission_policy import (
    FILL_MIN_PAIRS, JUMP_MS, AdmissionDecision, MinuteEvidence,
    T3AdmissionPolicy, clock_stability_from_block)
from hf_timestd.core.t3_reference_errors import ReferencePair
from hf_timestd.core.t3_sigma_honesty import SigmaHonesty

_MISSING = object()


def ev(i, e3=0.3, e2=_MISSING, *, ref="T6", phase=None, run_id=1.0,
       unresolved=False, no_pair=False, minute=None):
    """Minute ``i`` (utc_minute = 60 i) with T3 error ``e3`` and T2 error
    ``e2`` (default 0.6; None means the row had no T2 error)."""
    e2 = 0.6 if e2 is _MISSING else e2
    m = 60 * i if minute is None else minute
    pair = None if no_pair else ReferencePair(ref, e3, e2)
    ph = None if phase is None else PhasePoint(m, phase, run_id)
    return MinuteEvidence(utc_minute=m, pair=pair, phase=ph, t3_sigma_ms=0.5,
                          t2_sigma_ms=20.0, whole_second_unresolved=unresolved)


def run(evidence, mode=AdmissionMode.AUTO):
    p = T3AdmissionPolicy(mode)
    return p, [p.observe(e) for e in evidence]


def first_index(decisions, admitted, offset=0):
    for k, d in enumerate(decisions):
        if d.admitted is admitted:
            return k + offset
    return None


def test_day_admits_after_fill_and_hold():
    _, decisions = run(ev(i) for i in range(40))
    assert first_index(decisions, admitted=True) == 24
    assert decisions[24].reason == "admitted by reference (T6)"
    assert decisions[24].route == "reference"
    assert decisions[24].reference == "T6"
    assert decisions[24].since_utc_minute == 24 * 60
    assert decisions[24].n_pairs == 25
    assert decisions[24].t3_median_ms == pytest.approx(0.3)
    assert decisions[24].t2_p95_ms == pytest.approx(0.6)


def test_filling_and_pending_reasons():
    _, decisions = run(ev(i) for i in range(25))
    assert decisions[0].reason == "withheld: window filling (1 of 20 minutes)"
    assert decisions[18].reason == "withheld: window filling (19 of 20 minutes)"
    assert decisions[19].admitted is False
    # The standing state keeps its reason and names the pending change.
    assert decisions[19].reason == (
        "withheld: window filling (19 of 20 minutes); change pending 0 of 300 s")
    assert decisions[23].reason.endswith("; change pending 240 of 300 s")


def night_series():
    # day 0..59, night 60..89, day again from 90
    return [ev(i, 1.2 if 60 <= i < 90 else 0.3) for i in range(150)]


def test_night_withdraws_after_the_p95_fails_and_the_hold_passes():
    _, decisions = run(night_series())
    assert decisions[60].admitted is True
    assert decisions[61].reason.startswith("admitted by reference (T6); change pending 0 of 300 s")
    assert first_index(decisions[60:], admitted=False, offset=60) == 66
    assert decisions[66].reason.startswith("withheld: T2 tracks T6 better")
    assert decisions[66].route is None


def test_withheld_reason_names_the_statistics():
    _, decisions = run(ev(i, 1.2) for i in range(30))
    assert decisions[-1].reason == (
        "withheld: T2 tracks T6 better (median 1.20 vs 0.60 ms, "
        "p95 1.20 vs 0.60 ms)")


def test_wrong_second_withdraws_at_once_and_refills():
    _, decisions = run(ev(i, 600.0 if i == 40 else 0.3) for i in range(80))
    assert decisions[39].admitted is True
    assert decisions[40].admitted is False
    assert decisions[40].reason == "withdrawn: T3 named the wrong second (600 ms against T6)"
    assert decisions[40].n_pairs == 0
    assert first_index(decisions[41:], admitted=True, offset=41) == 65


def test_a_phase_step_within_a_run_is_a_jump():
    _, decisions = run(ev(i, phase=700.0 if i >= 40 else 0.0) for i in range(50))
    assert decisions[39].admitted is True
    assert decisions[40].admitted is False
    assert decisions[40].reason == "withdrawn: T3 jumped 700 ms"


def test_a_run_change_is_not_a_jump():
    _, decisions = run(ev(i, phase=700.0 if i >= 40 else 0.0,
                          run_id=2.0 if i >= 40 else 1.0) for i in range(50))
    assert decisions[40].admitted is True
    assert all(d.admitted for d in decisions[24:])


def test_a_phase_step_below_the_limit_is_no_jump():
    _, decisions = run(ev(i, phase=499.0 if i >= 40 else 0.0) for i in range(50))
    assert decisions[40].admitted is True
    assert JUMP_MS == 500.0


def test_reference_loss_holds_one_window():
    _, decisions = run([ev(i) for i in range(31)]
                       + [ev(i, no_pair=True) for i in range(31, 70)])
    assert decisions[30].admitted is True
    assert decisions[59].admitted is True
    assert decisions[59].reason == "admitted by reference (T6), held: no reference for 29 min"
    assert decisions[60].admitted is False
    assert decisions[60].reason == "withheld: no reference, and no fleet calibration yet"


def test_no_t2_says_so():
    _, decisions = run(ev(i, e2=None) for i in range(30))
    assert not any(d.admitted for d in decisions)
    assert decisions[-1].reason == "withheld: no T2 to compare against"
    assert decisions[-1].n_pairs == 0


def test_no_reference_at_all():
    _, decisions = run(ev(i, no_pair=True) for i in range(30))
    assert not any(d.admitted for d in decisions)
    assert decisions[-1].reason == "withheld: no reference, and no fleet calibration yet"


def test_whole_second_unresolved_withholds_at_once():
    _, decisions = run(ev(i, unresolved=(i == 40)) for i in range(60))
    assert decisions[39].admitted is True
    assert decisions[40].admitted is False
    assert decisions[40].reason == "withheld: T3 has not named the second"
    # The window survives (minute 40 adds no pair); admission returns after
    # one hold.
    assert decisions[39].n_pairs == 30 and decisions[41].n_pairs == 29
    assert first_index(decisions[41:], admitted=True, offset=41) == 46


def test_off_never_admits_and_on_admits_until_a_jump():
    _, off_decisions = run((ev(i) for i in range(40)), AdmissionMode.OFF)
    assert all(not d.admitted for d in off_decisions)
    assert off_decisions[-1].reason == 'off ([timing.t3] admission = "off")'
    assert off_decisions[-1].mode is AdmissionMode.OFF
    _, on_decisions = run((ev(i, 600.0 if i == 10 else 0.3) for i in range(20)),
                          AdmissionMode.ON)
    assert on_decisions[0].admitted and on_decisions[0].route == "forced"
    assert on_decisions[0].reason == (
        'forced on ([timing.t3] admission = "on"), for testing only')
    # jump at 10: withdrawn 10..14, forced again at 15
    assert [d.admitted for d in on_decisions[10:16]] == [False] * 5 + [True]
    assert on_decisions[12].reason == "withdrawn: T3 named the wrong second (600 ms against T6)"
    assert on_decisions[-1].transitions == 2


def test_initial_decisions():
    auto = AdmissionDecision.initial(AdmissionMode.AUTO)
    assert auto.admitted is False and auto.route is None
    assert auto.reason == f"withheld: window filling (0 of {FILL_MIN_PAIRS} minutes)"
    assert auto.stability.window_s == WINDOW_MIN_S
    assert auto.stability.hold_s == HOLD_MIN_S
    assert auto.transitions == 0 and auto.since_utc_minute is None
    assert AdmissionDecision.initial(AdmissionMode.OFF).admitted is False
    on = AdmissionDecision.initial(AdmissionMode.ON)
    assert on.admitted is True and on.route == "forced"
    assert T3AdmissionPolicy(AdmissionMode.AUTO).decision == auto


def test_a_repeated_minute_adds_no_sample():
    policy = T3AdmissionPolicy(AdmissionMode.AUTO)
    for i in range(9):
        policy.observe(ev(i))
    d1 = policy.observe(ev(10))
    d2 = policy.observe(ev(10))
    assert d2 == d1 and policy.decision.n_pairs == d1.n_pairs
    # An older minute changes nothing either.
    assert policy.observe(ev(5)) == d1


def test_transitions_count_once_each(caplog):
    caplog.set_level(logging.WARNING)
    _, decisions = run(night_series())
    # day -> night -> day
    assert decisions[-1].admitted is True
    assert decisions[-1].transitions == 3
    assert sum("T3 admission" in r.message for r in caplog.records
               if r.levelname == "WARNING") == 3


def test_to_block_is_strict_json():
    _, decisions = run(ev(i) for i in range(30))
    decision = decisions[-1]
    block = decision.to_block()
    json.dumps(block, allow_nan=False)
    assert block["mode"] == "auto"
    assert block["admitted"] is True
    assert set(block) == {
        "mode", "admitted", "route", "reason", "reference", "n_pairs",
        "t3_median_ms", "t3_p95_ms", "t2_median_ms", "t2_p95_ms",
        "stability", "since_utc_minute", "transitions"}
    assert set(block["stability"]) == {
        "tau_cross_s", "window_s", "hold_s", "drift_ns_per_s", "n_points"}
    json.dumps(AdmissionDecision.initial(AdmissionMode.ON).to_block(),
               allow_nan=False)


def test_clock_stability_round_trips_through_the_block():
    s = ClockStability(tau_cross_s=3840, window_s=3840, hold_s=960,
                       drift_ns_per_s=2.5, n_points=500)
    d = AdmissionDecision.initial(AdmissionMode.AUTO)
    d = AdmissionDecision(**{**d.__dict__, "stability": s})
    assert clock_stability_from_block(d.to_block()["stability"]) == s
    assert clock_stability_from_block(None) is None
    assert clock_stability_from_block({"window_s": "x"}) is None
    assert clock_stability_from_block({"window_s": True, "hold_s": 300,
                                       "n_points": 0}) is None


def _row(i, e3=0.3, t4=0.0, t2=-0.6, phase=0.0, run_id=1.0, old=False,
         unresolved=None):
    row = {
        "kind": "minute",
        "utc_minute": 60 * i,
        "t3": {"sigma_ms": 0.5, "whole_second_unresolved": unresolved},
        "t3_shadow": {"t3_minus_t6_ms": e3},
        "t4_offset_ms": t4,
        "t2_offset_ms": t2,
        "t2_sigma_ms": 20.0,
    }
    if not old:
        row["t3_phase_ms"] = phase
        row["t3_run_id"] = run_id
        row["t2_vs_adopted"] = None
    return row


def test_from_row_reads_a_minute_row():
    e = MinuteEvidence.from_row(_row(3, unresolved=True))
    assert e.utc_minute == 180
    assert e.pair == ReferencePair("T6", 0.3, pytest.approx(0.6))
    assert e.phase == PhasePoint(180, 0.0, 1.0)
    assert e.t3_sigma_ms == 0.5 and e.t2_sigma_ms == 20.0
    assert e.whole_second_unresolved is True
    assert MinuteEvidence.from_row(_row(3)).whole_second_unresolved is False


@pytest.mark.parametrize("phase,run_id", [
    (None, 1.0), (1.0, None), (float("nan"), 1.0), (1.0, float("inf")),
    (True, 1.0), (1.0, False), ("1.0", 1.0)])
def test_from_row_phase_needs_both_finite_numbers(phase, run_id):
    assert MinuteEvidence.from_row(_row(1, phase=phase, run_id=run_id)).phase is None


@pytest.mark.parametrize("row", [
    None, [], {}, {"utc_minute": "x"}, {"utc_minute": True, "t3": 5},
    {"utc_minute": 60, "t3": {"sigma_ms": float("nan")}, "t2_sigma_ms": True}])
def test_from_row_never_raises(row):
    e = MinuteEvidence.from_row(row)
    assert e.pair is None and e.phase is None
    assert e.t3_sigma_ms is None and e.t2_sigma_ms is None
    assert e.whole_second_unresolved is False


def test_a_malformed_minute_changes_nothing():
    policy = T3AdmissionPolicy(AdmissionMode.AUTO)
    d0 = policy.decision
    assert policy.observe(MinuteEvidence.from_row({"utc_minute": "x"})) == d0


def test_seed_skips_old_rows_and_restores_the_window():
    rows = [_row(i, old=(i < 10)) for i in range(30)]
    policy = T3AdmissionPolicy(AdmissionMode.AUTO)
    assert policy.seed(rows) == 20
    assert policy.decision.n_pairs == 20
    assert policy.seed(iter([None, 5, "x"])) == 0
    assert policy.seed(None) == 0


def test_seed_logs_no_warning(caplog):
    caplog.set_level(logging.INFO)
    policy = T3AdmissionPolicy(AdmissionMode.AUTO)
    assert policy.seed(_row(i) for i in range(40)) == 40
    assert policy.decision.admitted is True
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


def test_honesty_sees_every_pair():
    policy = T3AdmissionPolicy(AdmissionMode.OFF)
    for i in range(30):
        policy.observe(ev(i))
    assert isinstance(policy.honesty, SigmaHonesty)
    assert policy.honesty.by_hour()[0].n == 30


def test_a_sparse_good_pair_during_reference_loss_does_not_end_the_hold():
    # Pairs 0..30 with T3 winning, then none except one good pair at 50.
    # Favourable evidence must not withdraw sooner than no evidence at all,
    # and one sparse pair must not stretch the grace either.
    _, decisions = run([ev(i) for i in range(31)]
                       + [ev(i) if i == 50 else ev(i, no_pair=True)
                          for i in range(31, 70)])
    assert decisions[50].admitted is True
    assert decisions[50].route == "reference"
    assert decisions[59].admitted is True
    assert decisions[60].admitted is False
    assert decisions[60].reason == "withheld: no reference, and no fleet calibration yet"


def test_a_jump_ends_the_reference_loss_grace():
    _, decisions = run([ev(i) for i in range(31)]
                       + [ev(i, phase=0.0, no_pair=True) for i in range(31, 45)]
                       + [ev(45, phase=700.0, no_pair=True)]
                       + [ev(i, phase=700.0, no_pair=True) for i in range(46, 60)])
    assert decisions[44].admitted is True
    assert all(not d.admitted for d in decisions[45:])
    assert decisions[-1].reason == "withheld: no reference, and no fleet calibration yet"


def test_seed_counts_no_transitions():
    policy = T3AdmissionPolicy(AdmissionMode.AUTO)
    assert policy.seed(_row(i) for i in range(40)) == 40
    d = policy.decision
    assert d.admitted is True and d.n_pairs == 30
    assert d.transitions == 0 and d.since_utc_minute is None
    # Live minutes count from process start.
    d = policy.observe(ev(40, 600.0))
    assert d.admitted is False and d.transitions == 1
    assert d.since_utc_minute == 40 * 60


def test_jump_reasons_name_the_magnitude():
    _, decisions = run(ev(i, phase=-700.0 if i >= 40 else 0.0) for i in range(41))
    assert decisions[40].reason == "withdrawn: T3 jumped 700 ms"
    _, decisions = run(ev(i, -600.0 if i == 40 else 0.3) for i in range(41))
    assert decisions[40].reason == "withdrawn: T3 named the wrong second (600 ms against T6)"


# ── final review I-1: a failing policy closes T3 ─────────────────────


def _break_stability(p):
    def boom():
        raise RuntimeError("stability broke")
    p._estimator.stability = boom
    p._stability_at = None      # recompute on the next minute


def test_a_failing_policy_withdraws_at_once_and_logs_once(caplog):
    caplog.set_level(logging.INFO)
    p = T3AdmissionPolicy(AdmissionMode.AUTO)
    decisions = [p.observe(ev(i)) for i in range(30)]
    assert decisions[24].admitted is True and decisions[29].transitions == 1
    real = p._estimator.stability
    _break_stability(p)
    caplog.clear()
    failed = [p.observe(ev(i)) for i in range(30, 60)]
    assert all(not d.admitted for d in failed)
    assert failed[0].reason == "withheld: admission policy failed (RuntimeError)"
    assert failed[0].since_utc_minute == 30 * 60
    assert failed[-1].transitions == 2
    assert sum(r.levelname == "ERROR" for r in caplog.records) == 1
    assert sum(r.levelname == "WARNING" for r in caplog.records) == 1
    # Recovery: the window starts again, as after a jump.
    p._estimator.stability = real
    caplog.clear()
    back = [p.observe(ev(i)) for i in range(60, 100)]
    assert back[0].admitted is False and back[0].n_pairs <= 1
    assert first_index(back, admitted=True) == 24
    # A new failure streak logs again.
    _break_stability(p)
    caplog.clear()
    p.observe(ev(100))
    p.observe(ev(101))
    assert sum(r.levelname == "ERROR" for r in caplog.records) == 1


def test_a_failing_policy_closes_on_too_and_off_stays_off():
    on = T3AdmissionPolicy(AdmissionMode.ON)
    on.observe(ev(0))
    _break_stability(on)
    d = on.observe(ev(1))
    assert d.admitted is False
    assert d.reason == "withheld: admission policy failed (RuntimeError)"
    off = T3AdmissionPolicy(AdmissionMode.OFF)
    _break_stability(off)
    d = off.observe(ev(0))
    assert d.admitted is False
    assert d.reason == 'off ([timing.t3] admission = "off")'
