"""Stage-0 analysis: where the HF chain delay lies, per configuration."""
import pytest

from hf_timestd.core.hf_chain_delay_analysis import (
    EdgeObservation, ProbeChannelCapture, analyze, derived_filter_delay_s,
    format_report)

from synth_origins import F_IN, counter_map_for, edge_at

FIR = 324_001
P = 12_345_600 + 1000.5            # true edge phase within the input second
SECONDS = (100, 101, 102, 103, 104)
CLONES = (("SHARED_5000", 51), ("SHARED_10000", 51), ("WWV_2500", 51),
          ("WWV_5000", 51), ("WWV_10000", 50), ("WWV_25000", 120))
PPS_S = 2000                       # the GPS second the reference edge belongs to


def _capture(label, k0, f_out, late=0, drop_map=False):
    cm = counter_map_for(k0, f_out)
    edges = []
    for s in SECONDS:
        rtp, sub = edge_at(cm, s * F_IN + P + late)
        edges.append(EdgeObservation(rtp, sub, 0.0))
    return ProbeChannelCapture(
        label=label, preset="iq", output_samprate=f_out,
        low_edge_hz=-100.0 if f_out == 24_000 else -400.0,
        high_edge_hz=100.0 if f_out == 24_000 else 400.0,
        counter_map=None if drop_map else cm, edges=tuple(edges),
        fir_length=FIR, input_samprate=F_IN)


def _caps(late=None, drop_map=()):
    late = late or {}
    caps = [_capture("T6_REF", 40, 96_000, drop_map="T6_REF" in drop_map)]
    for label, k0 in CLONES:
        caps.append(_capture(label, k0, 24_000, late.get(label, 0), label in drop_map))
    return caps


def _pair_epochs():
    x = PPS_S - P / F_IN
    return [x + 0.0161, x + 0.0170, x + 0.0300]


def by_label(r, label):
    return next(row for row in r.rows if row.label == label)


def test_identical_clones_with_different_origins_agree():
    r = analyze(_caps(), "T6_REF", _pair_epochs(), learned_chain_delay_ms=None)
    others = [row for row in r.rows if row.label != "T6_REF"]
    assert len(others) == 6
    assert all(abs(row.delay_vs_ref_ms) < 1e-6 for row in others)
    assert r.clone_agreement_input_samples <= 1.0


def test_a_late_configuration_is_measured_and_fails_the_derivation():
    r = analyze(_caps(late={"WWV_25000": 16_200}), "T6_REF", _pair_epochs(), None)
    row = by_label(r, "WWV_25000")
    assert row.delay_vs_ref_ms == pytest.approx(0.25, abs=1e-3)
    assert row.derived_vs_ref_ms == 0.0 and row.verdict == "derivation_fails"
    assert r.clone_agreement_input_samples == pytest.approx(16_200, abs=1.0)


def test_matching_configuration_reports_derivation_holds():
    r = analyze(_caps(late={"WWV_25000": 16_200}), "T6_REF", _pair_epochs(), None)
    assert by_label(r, "SHARED_10000").verdict == "derivation_holds"


def test_a_clone_with_a_longer_filter_is_judged_against_its_own_derived_delay():
    caps = _caps(late={"WWV_5000": 16_200})
    # 16_200 input samples = 0.25 ms; a FIR 32_400 taps longer derives 0.25 ms more
    caps[4] = ProbeChannelCapture(**{**caps[4].__dict__, "fir_length": FIR + 32_400})
    r = analyze(caps, "T6_REF", _pair_epochs(), None)
    row = by_label(r, "WWV_5000")
    assert row.derived_vs_ref_ms == pytest.approx(0.25, abs=1e-9)
    assert row.verdict == "derivation_holds"


def test_derived_delay_is_half_the_fir_span_at_the_input_rate():
    assert derived_filter_delay_s(324_001, 64_800_000) == pytest.approx(0.0025)


def test_second_filter_adds_half_its_span_at_the_output_rate():
    d = derived_filter_delay_s(324_001, 64_800_000, filter2_fir_length=241,
                               output_samprate=24_000)
    assert d == pytest.approx(0.0025 + 120 / 24_000)
    with pytest.raises(ValueError):
        derived_filter_delay_s(324_001, 64_800_000, filter2_fir_length=241)


def test_missing_counter_map_refuses_the_comparison():
    r = analyze(_caps(drop_map=("SHARED_5000",)), "T6_REF", _pair_epochs(), None)
    row = by_label(r, "SHARED_5000")
    assert row.delay_vs_ref_ms is None and row.verdict == "no_counter_map"


def test_no_edges_is_reported_as_such():
    caps = _caps()
    caps[1] = ProbeChannelCapture(**{**caps[1].__dict__, "edges": ()})
    r = analyze(caps, "T6_REF", _pair_epochs(), None)
    assert by_label(r, "SHARED_5000").verdict == "no_edges"


def test_unusable_reference_leaves_every_clone_unjudged():
    r = analyze(_caps(drop_map=("T6_REF",)), "T6_REF", _pair_epochs(), None)
    assert by_label(r, "T6_REF").verdict == "no_counter_map"
    assert by_label(r, "WWV_5000").verdict == "no_reference"
    assert by_label(r, "WWV_5000").delay_vs_ref_ms is None
    assert r.link_ms is None


def test_link_is_the_pair_floor_minus_the_pps_epoch():
    r = analyze(_caps(), "T6_REF", _pair_epochs(), None)
    assert r.link_ms == pytest.approx(16.1, abs=1e-6) and r.n_pair_polls == 3
    assert r.link_minus_derived_ms == pytest.approx(16.1 - 2.5, abs=1e-6)


def test_report_prints_one_row_per_clone_and_the_link():
    r = analyze(_caps(), "T6_REF", _pair_epochs(), learned_chain_delay_ms=15.9)
    text = format_report(r)
    assert text.count("\n") >= len(r.rows) + 2
    assert "T6_REF" in text and "link (radiod pair − PPS edge)" in text
    assert "learned T6 chain delay" in text
