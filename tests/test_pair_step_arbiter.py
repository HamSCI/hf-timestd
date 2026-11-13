from hamsci_dsp.counter_map import CounterMap

from hf_timestd.core.native_anchor import NativeAnchor
from hf_timestd.core.pair_step_arbiter import (
    PairStepArbiter, T6Reference, Verdict, pair_disagreement_s,
    t6_predicted_utc_s)


def test_small_disagreement_is_steady():
    assert PairStepArbiter(24000).decide(0.3, 100.3, 100.0, 5.0, None) is Verdict.STEADY


def test_t6_refuses_step_that_gpsdo_contradicts():
    # fresh pair 1 s off the GPSDO, mapping on it: the ND case
    assert PairStepArbiter(24000).decide(0.998, 101.0, 100.002, 5.0, 100.0) is Verdict.REFUSE_T6


def test_t6_adopts_step_that_gpsdo_confirms():
    assert PairStepArbiter(24000).decide(-0.97, 100.01, 100.98, 5.0, 100.0) is Verdict.ADOPT


def test_no_t6_single_step_held():
    assert PairStepArbiter(24000).decide(0.998, 101.0, 100.0, 6.0, None) is Verdict.HOLD


def test_no_t6_round_trip_never_adopted():
    a = PairStepArbiter(24000)
    assert a.decide(0.998, 101.0, 100.0, 6.0, None) is Verdict.HOLD
    assert a.decide(0.001, 160.0, 160.0, 5.0, None) is Verdict.STEADY


def test_no_t6_genuine_step_adopted_on_second_tick():
    a = PairStepArbiter(24000)
    assert a.decide(3.2, 103.2, 100.0, 8.2, None) is Verdict.HOLD
    assert a.decide(3.2, 163.2, 160.0, 8.21, None) is Verdict.ADOPT


def test_persistence_wrap_safe():
    wrap = 2**32 / 24000
    a = PairStepArbiter(24000)
    a.decide(3.2, 103.2, 100.0, 8.2, None)
    assert a.decide(3.2, 163.2, 160.0, 8.2 + wrap, None) is Verdict.ADOPT


def test_reset_clears_pending_step():
    a = PairStepArbiter(24000)
    assert a.decide(3.2, 103.2, 100.0, 8.2, None) is Verdict.HOLD
    a.reset()
    assert a.decide(3.2, 163.2, 160.0, 8.2, None) is Verdict.HOLD


def _map(rtp_ref, epoch, ilen=1000, olen=1000, f_in=24000):
    return CounterMap(rtp_ref=rtp_ref, input_ref=5_000_000, ilen=ilen,
                      olen=olen, input_samprate=f_in,
                      input_epoch_gps_s=epoch, n_agree=3)


def _ref(t6_map):
    anchor = NativeAnchor(anchor_rtp=t6_map.rtp_ref + 24000,
                          anchor_utc_ns=1_700_000_000_000_000_000,
                          sample_rate_hz=24000, chain_delay_ns=0,
                          captured_at_utc_ns=1_700_000_000_000_000_000,
                          captured_via_tier="T6")
    return T6Reference(anchor=anchor, t6_map=t6_map)


def test_t6_prediction_none_across_runs():
    ref = _ref(_map(1_000_000, 1000.0))
    assert t6_predicted_utc_s(1_000_000, _map(2_000_000, 1002.0 + 1e-3), ref) is None


def test_t6_prediction_matches_anchor():
    t6_map = _map(1_000_000, 1000.0)
    ref = _ref(t6_map)
    # A channel on the same run whose counter sits 3000 samples ahead.
    chan = CounterMap(rtp_ref=7_000_000, input_ref=5_000_000 + 3000,
                      ilen=1000, olen=1000, input_samprate=24000,
                      input_epoch_gps_s=1000.0, n_agree=3)
    # Input index of T6's anchor is input_ref + 24000; channel rtp for it:
    rtp = 7_000_000 + 24000 - 3000
    got = t6_predicted_utc_s(rtp, chan, ref)
    assert abs(got - ref.anchor.anchor_utc_ns / 1e9) < 1e-6


def test_pair_disagreement_zero_for_consistent_pair():
    g = 1_700_000_000_000_000_000
    assert pair_disagreement_s(g + 500_000_000, 1000 + 12000, g, 1000, 24000) == 0.0


def test_pair_disagreement_across_rtp_wrap():
    g = 1_700_000_000_000_000_000
    ref_rtp = 2**32 - 6000
    # 0.5 s later = 12000 samples: rtp wraps to 6000.  Fresh is 20 ms late.
    got = pair_disagreement_s(g + 520_000_000, 6000, g, ref_rtp, 24000)
    assert abs(got - 0.02) < 1e-6
