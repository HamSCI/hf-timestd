"""compare_live's pure parts, over synthetic rows with a known answer.

The synthetic station: one input counter at 129.6 Msps, a 96 kHz channel
(decimation 1350), a pilot edge at a fixed phase on the counter every
UTC second.  hf-timestd's edge is placed DELTA_NS later than radiod's,
so the comparison must return DELTA_NS.
"""
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from radiod_pilot import compare_live as cl  # noqa: E402

IN_RATE = 129_600_000
OUT_RATE = 96_000
DECIM = IN_RATE // OUT_RATE
N0 = 1_794_178_800            # a UTC second (unix)
E0 = 128_000_000_000          # input index of the edge of second N0
PHI = 0.375                   # sub-sample phase of every edge
EPOCH = 1_478_213_294_638_026_024
GPS_OFF = cl.GPS_TO_UNIX_NS   # unix_ns = gps_ns + GPS_OFF
DELTA_NS = 37.0


def edge_at(n, phase=PHI):
    """True edge position on the input counter for second n: (int, frac)."""
    x = E0 + (n - N0) * IN_RATE
    return x, phase


def pilot_row(n, *, epoch=EPOCH, lock=True, phase=PHI, battery=None,
              gps_late_ns=3_000_000, host_gap_ns=3_800_000, logged=None):
    ei, fr = edge_at(n, phase)
    # status packet sent a few seconds after the edge; anchor at block start
    a_in = ei + 5 * IN_RATE
    a_rtp = (a_in // DECIM) & 0xFFFFFFFF
    in_samples = a_in
    rtp_snap = (a_rtp + 1920) & 0xFFFFFFFF
    # the true UTC of rtp_snap's sample, plus a late reading of gps_time
    t_snap_ns = n * 10**9 + round(((a_in + 1920 * DECIM - ei) - fr) * 1e9 / IN_RATE)
    gps_time = t_snap_ns - GPS_OFF + gps_late_ns
    return {
        "kind": "pilot", "ssrc": 45375, "input_epoch": epoch,
        "anchor_rtp": a_rtp, "anchor_input_sample": a_in,
        "input_samprate": IN_RATE, "output_samprate": OUT_RATE,
        "input_samples": in_samples, "rtp_timesnap": rtp_snap, "gps_time": gps_time,
        "pilot_edge_sample": ei, "pilot_edge_frac": fr,
        "pilot_edge_utc": n * 10**9 - GPS_OFF,
        "pilot_host_time": n * 10**9 - GPS_OFF + host_gap_ns,
        "pilot_edge_sigma": 2.5e-8, "pilot_snr": 60.0, "pilot_lock": lock,
        "pilot_fold_count": 30, "pilot_filter_delay": 0.0025,
        "pilot_battery": battery or [4e-4, 1e-14, 0.9999, 0.004, 2e-5, 1.0, 0.01, 32865.98],
        "logged_at_unix": logged if logged is not None else n + 5.0,
    }


# the hf-timestd channel's tie: input = A_IN + (rtp - A_RTP) * DECIM
A_RTP, A_IN = 4_000_000_000, E0 - 7 * IN_RATE + 123 * DECIM


def anchor_row(t_logged, n_for_anchor, epoch=EPOCH, ssrc=206674315):
    a_in = E0 + (n_for_anchor - N0) * IN_RATE + 999 * DECIM
    a_in = A_IN + ((a_in - A_IN) // DECIM) * DECIM
    a_rtp = (A_RTP + (a_in - A_IN) // DECIM) & 0xFFFFFFFF
    return {"kind": "anchor", "ssrc": ssrc, "input_epoch": epoch,
            "anchor_rtp": a_rtp, "anchor_input_sample": a_in,
            "input_samprate": IN_RATE, "output_samprate": OUT_RATE,
            "logged_at_unix": t_logged}


def hf_row(n, delta_ns=DELTA_NS, chain_delay_ns=200, with_quality=True):
    ei, fr = edge_at(n)
    pos = ei + fr + delta_ns * IN_RATE / 1e9          # hf's edge, input samples
    rtp_float = A_RTP + (pos - A_IN) / DECIM
    rtp = int(round(rtp_float))
    sub = rtp_float - rtp
    sub_ns = int(round(sub * 1e9 / OUT_RATE))
    row = {"anchor_rtp": rtp & 0xFFFFFFFF,
           "anchor_utc_ns": n * 10**9 + chain_delay_ns - sub_ns,
           "named_second_utc_ns": n * 10**9, "sample_rate_hz": OUT_RATE,
           "chain_delay_ns": chain_delay_ns, "authority_state": "AUTHORITATIVE",
           "logged_at_unix": n + 1.0}
    if with_quality:
        row["quality"] = {"edge_subsample": sub}
    return row


# --- conversions --------------------------------------------------------

def test_gps_to_unix_offset():
    # GPS epoch 1980-01-06 = unix 315964800, GPS ahead of UTC by 18 s
    assert cl.GPS_TO_UNIX_NS == (315964800 - 18) * 10**9


def test_signed32():
    assert cl.signed32(5) == 5
    assert cl.signed32(0xFFFFFFFF) == -1
    assert cl.signed32(0x80000000) == -0x80000000


def test_robust_sigma_of_gaussian_like():
    xs = [-1.0] * 50 + [0.0] + [1.0] * 49 + [100.0]
    assert cl.robust_sigma(xs) == pytest.approx(1.4826, rel=1e-3)
    assert math.isnan(cl.robust_sigma([]))


def test_radiod_edges_dedupes_repeated_reports():
    rows = [pilot_row(N0), pilot_row(N0, logged=N0 + 5.5), pilot_row(N0 + 30)]
    edges = cl.radiod_edges(rows)
    assert [e["named_s"] for e in edges] == [N0, N0 + 30]
    assert len(edges[0]["packets"]) == 2
    assert edges[0]["decim"] == DECIM and edges[0]["frac"] == PHI


def test_hf_edge_subsample_from_quality_and_derived_agree():
    e = cl.hf_edges([hf_row(N0)])[0]
    e2 = cl.hf_edges([hf_row(N0, with_quality=False)])[0]
    assert abs(e["sub"] - e2["sub"]) * 1e9 / OUT_RATE <= 0.5    # ns rounding
    assert e2["sub_source"] == "derived" and e["sub_source"] == "quality"


def test_hf_edge_derivation_independent_of_asserted_delay():
    a = cl.hf_edges([hf_row(N0, chain_delay_ns=200, with_quality=False)])[0]
    b = cl.hf_edges([hf_row(N0, chain_delay_ns=2_500_000, with_quality=False)])[0]
    assert a["sub"] == pytest.approx(b["sub"], abs=1e-9)


def test_hf_to_input_through_nearest_anchor():
    anchors = cl.channel_anchors([anchor_row(N0 - 3, N0 - 3), anchor_row(N0 + 2, N0 + 2),
                                  anchor_row(N0 + 900, N0 + 900)], 206674315)
    hf = cl.hf_edges([hf_row(N0)])[0]
    got = cl.hf_to_input(hf, anchors, window_s=600)
    assert got is not None
    epoch, ei, fr = got
    ti, tf = edge_at(N0)
    assert epoch == EPOCH
    assert ((ei - ti) + (fr - tf)) * 1e9 / IN_RATE == pytest.approx(DELTA_NS, abs=0.01)


def test_hf_to_input_none_without_anchor_in_window():
    anchors = cl.channel_anchors([anchor_row(N0 + 5000, N0 + 5000)], 206674315)
    assert cl.hf_to_input(cl.hf_edges([hf_row(N0)])[0], anchors, window_s=600) is None


# --- pairing ------------------------------------------------------------

def test_pair_hf_with_radiod_projects_across_seconds():
    r = cl.radiod_edges([pilot_row(N0 - 30), pilot_row(N0 + 7), pilot_row(N0 + 37)])
    anchors = cl.channel_anchors([anchor_row(N0, N0)], 206674315)
    hf = cl.hf_edges([hf_row(N0), hf_row(N0 + 30)])
    pairs = cl.pair_hf_with_radiod(hf, anchors, r, max_dn=60)
    assert len(pairs) == 2
    # dn = radiod's named second minus hf-timestd's
    assert pairs[0]["dn"] == 7 and pairs[1]["dn"] == 7
    for p in pairs:
        assert p["diff_ns"] == pytest.approx(DELTA_NS, abs=0.01)
        assert not p["naming_mismatch"]


def test_pair_flags_a_second_named_differently():
    r = cl.radiod_edges([pilot_row(N0)])
    # hf names the same edge one second later
    row = hf_row(N0)
    row["named_second_utc_ns"] += 10**9
    row["anchor_utc_ns"] += 10**9
    anchors = cl.channel_anchors([anchor_row(N0, N0)], 206674315)
    pairs = cl.pair_hf_with_radiod(cl.hf_edges([row]), anchors, r, max_dn=60)
    assert pairs[0]["naming_mismatch"]


def test_pair_refuses_other_epoch():
    r = cl.radiod_edges([pilot_row(N0, epoch=EPOCH + 1)])
    anchors = cl.channel_anchors([anchor_row(N0, N0)], 206674315)
    assert cl.pair_hf_with_radiod(cl.hf_edges([hf_row(N0)]), anchors, r, max_dn=60) == []


# --- scatter, a vs b, the old pair -------------------------------------

def test_scatter_of_a_perfect_ruler_is_zero_and_sees_jitter():
    perfect = cl.radiod_edges([pilot_row(N0 + 30 * k) for k in range(10)])
    s = cl.radiod_scatter(perfect)
    assert s["diff_sigma_ns"] == pytest.approx(0.0, abs=1e-6)
    jit = cl.radiod_edges([pilot_row(N0 + 30 * k, phase=PHI + (0.01 if k % 2 else -0.01))
                           for k in range(11)])
    s = cl.radiod_scatter(jit)
    # +-0.01 input samples alternating: first differences +-0.02 samples,
    # so the per-edge scatter is 0.02 / sqrt(2) samples
    assert s["diff_rms_ns"] == pytest.approx(0.02 * 1e9 / IN_RATE / math.sqrt(2), rel=1e-6)
    assert s["n"] == 11


def test_hf_scatter_in_its_own_rtp_domain_survives_wrap():
    rows = [hf_row(N0 + 30 * k) for k in range(5)]
    s = cl.hf_scatter(cl.hf_edges(rows))
    assert s["diff_sigma_ns"] == pytest.approx(0.0, abs=0.01)
    assert s["n"] == 5


def test_two_receivers_one_clock():
    a = cl.radiod_edges([pilot_row(N0 + 30 * k) for k in range(6)])
    # receiver b: another counter, other origin, a fixed phase, other seconds
    b_rows = []
    for k in range(6):
        r = pilot_row(N0 + 30 * k + 11)
        r["pilot_edge_sample"] += 987_654_321
        r["pilot_edge_frac"] = 0.125 + (0.001 if k == 3 else 0.0)
        b_rows.append(r)
    b = cl.radiod_edges(b_rows)
    pairs = cl.pair_receivers(a, b, max_dt_s=60)
    assert len(pairs) == 6
    devs = [p["rel_ns"] for p in pairs]
    assert max(abs(d) for d in devs if d != max(devs, key=abs)) < 1e-6
    assert max(abs(d) for d in devs) == pytest.approx(0.001 * 1e9 / IN_RATE, rel=1e-3)


def test_old_pair_gap_measures_the_late_gps_time():
    e = cl.radiod_edges([pilot_row(N0, gps_late_ns=3_000_000)])[0]
    g = e["packets"][0]
    assert g["gap_rtp_ns"] == pytest.approx(3_000_000, abs=2)
    # the INPUT_SAMPLES form: here INPUT_SAMPLES sits one block (20 ms)
    # before RTP_TIMESNAP's sample, as B1 sends them, so it reads 20 ms later
    assert g["gap_input_ns"] == pytest.approx(3_000_000 + 1920 * 1e9 / OUT_RATE, abs=2)


# --- the battery --------------------------------------------------------

def test_battery_passes_a_healthy_run_and_refuses_a_bad_shape():
    rows = [pilot_row(N0 + 30 * k) for k in range(5)]
    bad = list(rows[4]["pilot_battery"])
    bad[4] = 0.9                               # peak_prominence residual far too high
    rows[4]["pilot_battery"] = bad
    v = cl.battery_verdicts(cl.radiod_edges(rows))
    assert [x["mode"] for x in v] == ["bootstrap"] * 3 + ["tracking"] * 2
    # the first two lack history (ruler, unimodality): accumulating, not refused
    assert v[0]["accumulating"] and v[1]["accumulating"]
    assert v[2]["passed"] and v[3]["passed"]
    assert not v[4]["passed"] and "shape" in v[4]["failures"]


def test_battery_resets_on_epoch_change():
    rows = [pilot_row(N0 + 30 * k) for k in range(3)] + \
           [pilot_row(N0 + 30 * k, epoch=EPOCH + 1) for k in range(3, 5)]
    v = cl.battery_verdicts(cl.radiod_edges(rows))
    assert v[3]["accumulating"] and v[3]["mode"] == "bootstrap"
