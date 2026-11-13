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


# ---------------------------------------------------------------------------
# Final review fix wave: rule 2 refuses only what T6 vouches against
# (ruling R6); persistence tolerance 0.2 s (R7); prediction None paths.
# ---------------------------------------------------------------------------
from hf_timestd.core.pair_step_arbiter import (
    PERSIST_AGREE_S, t6_anchor_in_map_run, t6_contradicts_both)


def test_t6_contradicting_both_falls_to_persistence():
    # radiod restarted; its pair arrives 0.4 s late, the mapping is the
    # old run's (3641 s away).  T6 agrees with neither.
    a = PairStepArbiter(24000)
    assert t6_contradicts_both(5000.4, 1359.0, 5000.0)
    assert a.decide(3641.4, 5000.4, 1359.0, 8.4, 5000.0) is Verdict.HOLD
    assert a.decide(3641.4, 5060.4, 1419.0, 8.4, 5060.0) is Verdict.ADOPT


def test_t6_refuse_only_when_mapping_agrees():
    a = PairStepArbiter(24000)
    assert not t6_contradicts_both(101.0, 100.1, 100.0)
    assert a.decide(0.9, 101.0, 100.1, 5.0, 100.0) is Verdict.REFUSE_T6


def test_t6_rule_clears_pending_step():
    a = PairStepArbiter(24000)
    assert a.decide(3.2, 103.2, 100.0, 8.2, None) is Verdict.HOLD
    # T6 refuses the same step: the pending step is dropped ...
    assert a.decide(3.2, 163.2, 160.0, 8.2, 160.0) is Verdict.REFUSE_T6
    # ... so the next T6-less sighting starts over.
    assert a.decide(3.2, 223.2, 220.0, 8.2, None) is Verdict.HOLD


def test_t6_adopt_clears_pending_step():
    a = PairStepArbiter(24000)
    assert a.decide(3.2, 103.2, 100.0, 8.2, None) is Verdict.HOLD
    assert a.decide(-1.0, 159.0, 160.0, 3.0, 159.0) is Verdict.ADOPT
    assert a.decide(3.2, 223.2, 220.0, 8.2, None) is Verdict.HOLD


def test_persistence_tolerance_is_0p2_s():
    assert PERSIST_AGREE_S == 0.2
    a = PairStepArbiter(24000)
    assert a.decide(3.2, 103.2, 100.0, 8.2, None) is Verdict.HOLD
    # Two honest late pairs 0.15 s apart are one step ...
    assert a.decide(3.35, 163.35, 160.0, 8.35, None) is Verdict.ADOPT
    b = PairStepArbiter(24000)
    assert b.decide(3.2, 103.2, 100.0, 8.2, None) is Verdict.HOLD
    # ... but 0.3 s apart they are not.
    assert b.decide(3.5, 163.5, 160.0, 8.5, None) is Verdict.HOLD


def test_t6_prediction_none_when_a_map_is_missing():
    t6_map = _map(1_000_000, 1000.0)
    ref = _ref(t6_map)
    assert t6_predicted_utc_s(1_000_000, None, ref) is None
    assert t6_predicted_utc_s(1_000_000, t6_map, None) is None
    assert t6_predicted_utc_s(
        1_000_000, t6_map, T6Reference(anchor=ref.anchor, t6_map=None)) is None


def test_t6_prediction_none_when_t6_index_is_none():
    t6_map = _map(1_000_000, 1000.0)
    ref = _ref(t6_map)
    # Anchor at 96 kHz through a 24 kHz map: no index.
    from dataclasses import replace
    wrong_rate = T6Reference(anchor=replace(ref.anchor, sample_rate_hz=96000),
                             t6_map=t6_map)
    assert t6_predicted_utc_s(1_000_000, t6_map, wrong_rate) is None
    # Anchor RTP far beyond the map's reach (half a wrap away).
    far = T6Reference(anchor=replace(ref.anchor,
                                     anchor_rtp=(t6_map.rtp_ref + 2**31) % 2**32),
                      t6_map=t6_map)
    assert t6_predicted_utc_s(1_000_000, t6_map, far) is None


def test_t6_anchor_in_map_run():
    # Identity map: epoch is the GPS second of RTP 0.
    utc_minus_gps = 315964800 - 18
    gps_epoch = 1_450_000_000.0
    cmap = CounterMap(rtp_ref=1_000_000, input_ref=1_000_000, ilen=1, olen=1,
                      input_samprate=24000, input_epoch_gps_s=gps_epoch,
                      n_agree=3)

    def anchor_at(rtp, gps_s):
        utc_ns = int(round((gps_s + utc_minus_gps) * 1e9))
        return NativeAnchor(anchor_rtp=rtp, anchor_utc_ns=utc_ns,
                            sample_rate_hz=24000, chain_delay_ns=0,
                            captured_at_utc_ns=utc_ns, captured_via_tier="T6")

    good = anchor_at(1_024_000, gps_epoch + 1_024_000 / 24000)
    assert t6_anchor_in_map_run(T6Reference(good, cmap), utc_minus_gps)
    # An anchor from a run that started an hour earlier.
    old = anchor_at(1_024_000, gps_epoch - 3600 + 1_024_000 / 24000)
    assert not t6_anchor_in_map_run(T6Reference(old, cmap), utc_minus_gps)
    assert not t6_anchor_in_map_run(None, utc_minus_gps)
