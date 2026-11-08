#!/usr/bin/env python3
"""Compare radiod's pilot edges with hf-timestd's T6 edges, live.

  compare_live.py LEDGER_A ANCHORS... [--ledger-b LEDGER_B]
                  [--hf-ssrc 206674315] [--out DIR]

LEDGER_A/LEDGER_B: listen.py output for B1-a (pilot rows plus anchor rows
for hf-timestd's own T6 channel) and B1-b (pilot rows).  ANCHORS:
hf-timestd's ``t6-anchors-*.jsonl``.

What it reports (JSON summary on stdout and in DIR/summary.json, CSV
series beside it):

1. hf-timestd against radiod, on B1-a's input counter.  hf-timestd's fine
   edge sits on its own channel's RTP counter: ``anchor_rtp`` plus the
   fine stage's sub-sample, taken from the ledger's
   ``quality.edge_subsample`` or, failing that, recovered as
   ``sub_ns = named_second_utc_ns + chain_delay_ns - anchor_utc_ns``
   (``t6_anchor_ledger``).  The asserted chain delay drops out of that
   recovery, so neither side carries an asserted term.  The edge moves
   to the input counter through the nearest ANCHOR_RTP/ANCHOR_INPUT_SAMPLE
   pair radiod sent for that channel, and is compared with radiod's edge
   projected to the same named second (the GPSDO clocks the ADC, so one
   second is exactly INPUT_SAMPRATE samples).  Both channels pass the
   same kind of radiod filter, so both raw positions carry the same
   filter delay; it is reported beside, never subtracted.
2. Each side's own scatter: the RMS and robust sigma of the first
   differences of edge position minus whole seconds, over sqrt(2).
3. The battery (``t6_battery.T6Battery.evaluate``) over every edge radiod
   marks locked.  radiod does not send its search mode; it is inferred as
   ``bootstrap`` for the first three locked edges of an epoch, as the
   detector confirms its self-seed, and ``tracking`` after.  The implied
   chain delay judged by criterion 7 is radiod's host-clock gap,
   PILOT_HOST_TIME - PILOT_EDGE_UTC.
4. Two receivers, one clock: B1-a's and B1-b's edges, each as its
   counter's offset from whole seconds, differenced pairwise and taken
   about the median per epoch pair.  One GPSDO clocks both ADCs and the
   TS-1, so the difference should hold still.
5. The current pair against the new tie: for every pilot status packet,
   where radiod's existing GPS_TIME/RTP_TIMESNAP pair (and the
   GPS_TIME/INPUT_SAMPLES form) labels the pilot's edge sample, minus the
   named second.
"""
import argparse
import csv
import json
import math
import os
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))
from radiod_pilot import tlv  # noqa: E402

try:
    from hf_timestd.core import t6_battery  # noqa: E402
except ImportError:                          # in-tree run without the venv
    sys.path.insert(0, str(_HERE.parent.parent / "src"))
    from hf_timestd.core import t6_battery  # noqa: E402

# ka9q-radio timing.h: UNIX_EPOCH 315964800 (the GPS epoch on the unix
# scale), GPS_UTC_OFFSET 18 s.  unix_ns = gps_ns + GPS_TO_UNIX_NS.
GPS_TO_UNIX_NS = (315964800 - 18) * 10**9
BILLION = 10**9
BOOTSTRAP_BLOCKS = 3          # pilot.c BOOTSTRAP_CONFIRM_BLOCKS


# --- small helpers ----------------------------------------------------------

def signed32(x: int) -> int:
    x &= 0xFFFFFFFF
    return x - 0x100000000 if x >= 0x80000000 else x


def robust_sigma(xs) -> float:
    xs = [x for x in xs if math.isfinite(x)]
    if not xs:
        return float("nan")
    m = statistics.median(xs)
    return 1.4826 * statistics.median(abs(x - m) for x in xs)


def rms(xs) -> float:
    xs = list(xs)
    return math.sqrt(sum(x * x for x in xs) / len(xs)) if xs else float("nan")


def stats(xs) -> dict:
    xs = [x for x in xs if math.isfinite(x)]
    if not xs:
        return {"n": 0}
    return {"n": len(xs), "median": statistics.median(xs), "mean": statistics.fmean(xs),
            "robust_sigma": robust_sigma(xs),
            "stdev": statistics.stdev(xs) if len(xs) > 1 else float("nan"),
            "min": min(xs), "max": max(xs), "p2p": max(xs) - min(xs)}


def drift_per_hour(ts, ys) -> float:
    """Least-squares slope, units of y per hour."""
    if len(ts) < 3:
        return float("nan")
    tm, ym = statistics.fmean(ts), statistics.fmean(ys)
    den = sum((t - tm) ** 2 for t in ts)
    return float("nan") if den == 0 else 3600.0 * sum(
        (t - tm) * (y - ym) for t, y in zip(ts, ys)) / den


def load_jsonl(paths):
    rows = []
    for p in paths:
        with open(p) as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except ValueError:       # a line cut short by a running writer
                    continue
    return rows


def iso(s) -> str:
    return datetime.fromtimestamp(s, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- radiod's side ----------------------------------------------------------

def old_pair_gaps(r: dict, named_ns: int) -> dict:
    """Where the existing status pair labels the edge sample, minus its name (ns)."""
    decim = r["input_samprate"] // r["output_samprate"]
    base = r["gps_time"] + GPS_TO_UNIX_NS - named_ns          # exact int
    frac = r["pilot_edge_frac"]
    d_out = signed32(r["anchor_rtp"] - r["rtp_timesnap"]) + (
        (r["pilot_edge_sample"] - r["anchor_input_sample"]) + frac) / decim
    gap_rtp = base + d_out * BILLION / r["output_samprate"]
    gap_in = base + ((r["pilot_edge_sample"] - r["input_samples"]) + frac) * BILLION / r["input_samprate"]
    return {"logged_at_unix": r.get("logged_at_unix"), "gap_rtp_ns": gap_rtp, "gap_input_ns": gap_in}


def radiod_edges(rows) -> list:
    """One entry per distinct pilot report, in the order first seen."""
    out, seen = [], {}
    for r in rows:
        if r.get("kind", "pilot") != "pilot" or "pilot_edge_sample" not in r:
            continue
        key = (r["input_epoch"], r["pilot_edge_sample"], r["pilot_edge_frac"])
        named_ns = r["pilot_edge_utc"] + GPS_TO_UNIX_NS
        if key not in seen:
            e = {
                "epoch": r["input_epoch"], "edge_in": r["pilot_edge_sample"],
                "frac": r["pilot_edge_frac"], "named_s": named_ns // BILLION,
                "in_rate": r["input_samprate"], "out_rate": r["output_samprate"],
                "decim": r["input_samprate"] // r["output_samprate"],
                "sigma_s": r.get("pilot_edge_sigma"), "snr": r.get("pilot_snr"),
                "lock": bool(r.get("pilot_lock")), "fold_count": r.get("pilot_fold_count"),
                "filter_delay_s": r.get("pilot_filter_delay"),
                "battery": r.get("pilot_battery"),
                "host_gap_ns": r["pilot_host_time"] - r["pilot_edge_utc"],
                "first_logged": r.get("logged_at_unix"), "packets": [],
            }
            seen[key] = e
            out.append(e)
        if all(k in r for k in ("gps_time", "rtp_timesnap", "input_samples")):
            seen[key]["packets"].append(old_pair_gaps(r, named_ns))
    return out


def radiod_scatter(edges) -> dict:
    """Scatter of radiod's edges from whole seconds, within each epoch."""
    diffs = []
    for prev, cur in zip(edges, edges[1:]):
        if prev["epoch"] != cur["epoch"] or not (prev["lock"] and cur["lock"]):
            continue
        d = ((cur["edge_in"] - prev["edge_in"]) - (cur["named_s"] - prev["named_s"]) * cur["in_rate"]
             + (cur["frac"] - prev["frac"]))
        diffs.append(d * BILLION / cur["in_rate"])
    return _scatter(diffs, len(edges))


def _scatter(diffs_ns, n) -> dict:
    root2 = math.sqrt(2.0)
    return {"n": n, "n_diffs": len(diffs_ns),
            "diff_rms_ns": rms(diffs_ns) / root2,
            "diff_sigma_ns": robust_sigma(diffs_ns) / root2,
            "max_abs_diff_ns": max((abs(x) for x in diffs_ns), default=float("nan"))}


def offsets_ns(edges) -> list:
    """Each locked edge's counter offset from whole seconds, relative to the
    first edge of its epoch, in ns."""
    first, out = {}, []
    for e in edges:
        if not e["lock"]:
            continue
        f = first.setdefault(e["epoch"], e)
        o = ((e["edge_in"] - f["edge_in"]) - (e["named_s"] - f["named_s"]) * e["in_rate"]
             + (e["frac"] - f["frac"]))
        out.append((e, o * BILLION / e["in_rate"]))
    return out


# --- hf-timestd's side ------------------------------------------------------

def hf_edges(rows) -> list:
    out, seen = [], set()
    for r in rows:
        key = (r["anchor_rtp"], r["anchor_utc_ns"])
        if key in seen:
            continue
        seen.add(key)
        sr = int(r["sample_rate_hz"])
        sub_ns = r["named_second_utc_ns"] + r["chain_delay_ns"] - r["anchor_utc_ns"]
        derived = sub_ns * sr / BILLION
        q = (r.get("quality") or {}).get("edge_subsample")
        out.append({
            "rtp": int(r["anchor_rtp"]) & 0xFFFFFFFF,
            "sub": float(q) if q is not None else derived,
            "sub_source": "quality" if q is not None else "derived",
            "sub_derived": derived,
            "named_s": r["named_second_utc_ns"] // BILLION, "sr": sr,
            "chain_delay_ns": r["chain_delay_ns"],
            "authority_state": r.get("authority_state"),
            "logged_at_unix": r.get("logged_at_unix"),
        })
    return out


def hf_scatter(edges) -> dict:
    diffs = []
    for prev, cur in zip(edges, edges[1:]):
        d = (signed32(cur["rtp"] - prev["rtp"]) + (cur["sub"] - prev["sub"])
             - (cur["named_s"] - prev["named_s"]) * cur["sr"])
        diffs.append(d * BILLION / cur["sr"])
    return _scatter(diffs, len(edges))


def channel_anchors(rows, ssrc) -> list:
    out = []
    for r in rows:
        if r.get("ssrc") != ssrc or not all(
                k in r for k in ("input_epoch", "anchor_rtp", "anchor_input_sample",
                                 "input_samprate", "output_samprate")):
            continue
        out.append({"epoch": r["input_epoch"], "rtp": r["anchor_rtp"],
                    "input": r["anchor_input_sample"],
                    "decim": r["input_samprate"] // r["output_samprate"],
                    "logged_at_unix": r.get("logged_at_unix", 0.0)})
    out.sort(key=lambda a: a["logged_at_unix"])
    return out


def hf_to_input(hf: dict, anchors, window_s: float):
    """(epoch, input index, fraction in input samples) or None.

    The nearest anchor on the RTP counter among those logged within
    ``window_s`` of the ledger row (the RTP counter wraps every 12.4 h at
    96 kHz, so time first, then RTP).
    """
    t = hf["logged_at_unix"]
    cands = [a for a in anchors if abs(a["logged_at_unix"] - t) <= window_s]
    if not cands:
        return None
    a = min(cands, key=lambda a: abs(signed32(hf["rtp"] - a["rtp"])))
    return a["epoch"], tlv.rtp_to_input(hf["rtp"], a["rtp"], a["input"], a["decim"]), hf["sub"] * a["decim"]


def pair_hf_with_radiod(hf, anchors, r_edges, max_dn=60, window_s=600.0) -> list:
    by_epoch = defaultdict(list)
    for e in r_edges:
        if e["lock"]:
            by_epoch[e["epoch"]].append(e)
    pairs = []
    for h in hf:
        conv = hf_to_input(h, anchors, window_s)
        if conv is None:
            continue
        epoch, hi, hfrac = conv
        cands = [e for e in by_epoch.get(epoch, ()) if abs(e["named_s"] - h["named_s"]) <= max_dn]
        if not cands:
            continue
        r = min(cands, key=lambda e: abs(e["named_s"] - h["named_s"]))
        dn = r["named_s"] - h["named_s"]
        d = (hi - r["edge_in"] + dn * r["in_rate"]) + (hfrac - r["frac"])
        diff_ns = d * BILLION / r["in_rate"]
        pairs.append({"named_s": h["named_s"], "dn": dn, "epoch": epoch, "diff_ns": diff_ns,
                      "naming_mismatch": abs(diff_ns) > 0.5 * BILLION,
                      "hf_sub": h["sub"], "sub_source": h["sub_source"],
                      "radiod_sigma_ns": (r["sigma_s"] or float("nan")) * 1e9,
                      "filter_delay_s": r["filter_delay_s"]})
    return pairs


# --- two receivers, one clock -----------------------------------------------

def pair_receivers(a, b, max_dt_s=60) -> list:
    """Counter offsets from whole seconds, a minus b, about the median per epoch pair."""
    bl = [e for e in b if e["lock"]]
    raw = []
    for ea in a:
        if not ea["lock"]:
            continue
        cands = [e for e in bl if abs(e["named_s"] - ea["named_s"]) <= max_dt_s]
        if not cands:
            continue
        eb = min(cands, key=lambda e: abs(e["named_s"] - ea["named_s"]))
        oa = ea["edge_in"] - ea["named_s"] * ea["in_rate"]
        ob = eb["edge_in"] - eb["named_s"] * eb["in_rate"]
        d = (oa - ob) + (ea["frac"] - eb["frac"])        # input samples, both 129.6 Msps
        raw.append((ea, eb, d))
    groups = defaultdict(list)
    for ea, eb, d in raw:
        groups[(ea["epoch"], eb["epoch"])].append(d)
    med = {k: statistics.median(v) for k, v in groups.items()}
    return [{"named_s": ea["named_s"], "dn": eb["named_s"] - ea["named_s"],
             "epoch_a": ea["epoch"], "epoch_b": eb["epoch"],
             "rel_ns": (d - med[(ea["epoch"], eb["epoch"])]) * BILLION / ea["in_rate"]}
            for ea, eb, d in raw]


# --- the battery ------------------------------------------------------------

def battery_verdicts(edges) -> list:
    out, battery, epoch, n_in_epoch = [], None, None, 0
    for e in edges:
        if not e["lock"] or not e["battery"] or len(e["battery"]) != 8:
            continue
        if e["epoch"] != epoch or battery is None:
            battery = t6_battery.T6Battery(e["out_rate"], int(e["fold_count"] or 30))
            epoch, n_in_epoch = e["epoch"], 0
        b = dict(zip(tlv.BATTERY_FIELDS, e["battery"]))
        q, rem = divmod(e["edge_in"], e["decim"])
        est = SimpleNamespace(
            edge_rtp=q + int(round((rem + e["frac"]) / e["decim"])),
            edge_offset_samples=b["edge_offset_samples"],
            fold_retention=b["fold_retention"], peak_prominence=b["peak_prominence"],
            apex_distance_samples=b["apex_distance"],
            transition_width_samples=b["transition_width"],
            split_half_delta_samples=b["split_half_delta"])
        mode = "bootstrap" if n_in_epoch < BOOTSTRAP_BLOCKS else "tracking"
        n_in_epoch += 1
        v = battery.evaluate(est, implied_chain_delay_ns=e["host_gap_ns"],
                             reported_sigma_ms=(e["sigma_s"] if e["sigma_s"] is not None
                                                else float("nan")) * 1e3,
                             cn0_db_hz=None, search_mode=mode)
        out.append({"named_s": e["named_s"], "epoch": e["epoch"], "mode": mode,
                    "passed": v.passed, "failures": list(v.failures),
                    "accumulating": t6_battery.is_still_accumulating(v)})
    return out


def battery_summary(v) -> dict:
    refused = [x for x in v if not x["passed"] and not x["accumulating"]]
    return {"n_locked_judged": len(v), "n_passed": sum(x["passed"] for x in v),
            "n_accumulating": sum(x["accumulating"] for x in v), "n_refused": len(refused),
            "refusal_criteria": dict(Counter(c for x in refused for c in x["failures"])),
            "refused_seconds": [x["named_s"] for x in refused][:50]}


# --- report -----------------------------------------------------------------

def receiver_summary(edges) -> dict:
    pk = [p for e in edges for p in e["packets"]]
    offs = offsets_ns(edges)
    return {
        "reports": len(edges), "locked": sum(e["lock"] for e in edges),
        "epochs": sorted({e["epoch"] for e in edges}),
        "first_named_utc": iso(edges[0]["named_s"]) if edges else None,
        "last_named_utc": iso(edges[-1]["named_s"]) if edges else None,
        "scatter": radiod_scatter(edges),
        "offset_from_whole_seconds_ns": stats([o for _, o in offs]),
        "offset_drift_ns_per_hour": drift_per_hour([e["named_s"] for e, _ in offs],
                                                   [o for _, o in offs]),
        "sigma_reported_ns": stats([e["sigma_s"] * 1e9 for e in edges if e["sigma_s"] is not None]),
        "snr_db": stats([e["snr"] for e in edges if e["snr"] is not None]),
        "filter_delay_s": sorted({e["filter_delay_s"] for e in edges}),
        "host_gap_ns": stats([e["host_gap_ns"] for e in edges]),
        "current_pair_vs_pilot": {
            "packets": len(pk),
            "gps_time_rtp_timesnap_gap_ns": stats([p["gap_rtp_ns"] for p in pk]),
            "gps_time_input_samples_gap_ns": stats([p["gap_input_ns"] for p in pk]),
        },
    }


def write_csv(path, rows, fields):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def edge_rows(edges, verdicts):
    vmap = {(v["epoch"], v["named_s"]): v for v in verdicts}
    offs = {id(e): o for e, o in offsets_ns(edges)}
    out = []
    for e in edges:
        v = vmap.get((e["epoch"], e["named_s"]), {})
        gr = [p["gap_rtp_ns"] for p in e["packets"]]
        out.append({"named_utc": iso(e["named_s"]), "named_s": e["named_s"], "epoch": e["epoch"],
                    "lock": int(e["lock"]), "offset_ns": offs.get(id(e), ""),
                    "sigma_ns": (e["sigma_s"] or 0) * 1e9, "snr_db": e["snr"],
                    "host_gap_ns": e["host_gap_ns"],
                    "battery_mode": v.get("mode", ""), "battery_passed": v.get("passed", ""),
                    "battery_accumulating": v.get("accumulating", ""),
                    "battery_failures": "|".join(v.get("failures", [])),
                    "oldpair_gap_rtp_ns_median": statistics.median(gr) if gr else ""})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("ledger_a", metavar="LEDGER_A")
    ap.add_argument("anchors", nargs="+", metavar="ANCHORS")
    ap.add_argument("--ledger-b")
    ap.add_argument("--hf-ssrc", type=int, default=206674315)
    ap.add_argument("--max-dn", type=int, default=60, help="max named-second gap when pairing")
    ap.add_argument("--out", default="compare-live-out", metavar="DIR")
    a = ap.parse_args(argv)

    rows_a = load_jsonl([a.ledger_a])
    ea = radiod_edges(rows_a)
    anchors = channel_anchors(rows_a, a.hf_ssrc)
    hf_all = hf_edges(load_jsonl(a.anchors))
    t0 = min((r["logged_at_unix"] for r in rows_a), default=0.0)
    hf = [h for h in hf_all if h["logged_at_unix"] >= t0 - 60]
    eb = radiod_edges(load_jsonl([a.ledger_b])) if a.ledger_b else []

    pairs = pair_hf_with_radiod(hf, anchors, ea, max_dn=a.max_dn)
    good = [p for p in pairs if not p["naming_mismatch"]]
    va, vb = battery_verdicts(ea), battery_verdicts(eb)
    ab = pair_receivers(ea, eb) if eb else []
    subdiff = [abs(h["sub"] - h["sub_derived"]) * BILLION / h["sr"] for h in hf]

    sa, shf = receiver_summary(ea), hf_scatter(hf)
    diff = stats([p["diff_ns"] for p in good])
    summary = {
        "inputs": {"ledger_a": a.ledger_a, "ledger_b": a.ledger_b, "anchors": a.anchors,
                   "hf_ssrc": a.hf_ssrc},
        "hf_vs_radiod_a": {
            "hf_edges_in_window": len(hf), "hf_anchor_rows": len(anchors), "pairs": len(pairs),
            "naming_mismatches": len(pairs) - len(good),
            "dn_seconds": dict(Counter(p["dn"] for p in good)),
            "diff_ns": diff,
            "note": "hf-timestd minus radiod, raw input-counter positions; both carry the "
                    "same channel-filter delay, reported in radiod_a.filter_delay_s",
            "hf_sub_quality_vs_derived_max_ns": max(subdiff, default=float("nan")),
            "hf_asserted_chain_delay_ns": sorted({h["chain_delay_ns"] for h in hf}),
        },
        "radiod_a": sa, "hf_timestd": {"scatter": shf},
        "radiod_b": receiver_summary(eb) if eb else None,
        "battery_a": battery_summary(va), "battery_b": battery_summary(vb) if eb else None,
        "two_receivers_one_clock": {
            "pairs": len(ab), "dn_seconds": dict(Counter(p["dn"] for p in ab)),
            "rel_ns": stats([p["rel_ns"] for p in ab]),
            "drift_ns_per_hour": drift_per_hour([p["named_s"] for p in ab], [p["rel_ns"] for p in ab]),
        },
    }
    md = diff.get("median")
    summary["pass"] = {
        "median_abs_diff_le_50ns": None if md is None else abs(md) <= 50.0,
        "radiod_scatter_le_hf": (sa["scatter"]["diff_rms_ns"] <= shf["diff_rms_ns"]
                                 if shf["n_diffs"] and sa["scatter"]["n_diffs"] else None),
        "zero_locked_edges_refused": summary["battery_a"]["n_refused"] == 0 and
            (summary["battery_b"] is None or summary["battery_b"]["n_refused"] == 0),
        "no_epoch_change": len(sa["epochs"]) <= 1 and (not eb or len(summary["radiod_b"]["epochs"]) <= 1),
    }

    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2, default=str)
    write_csv(os.path.join(a.out, "hf_vs_a.csv"),
              [dict(p, named_utc=iso(p["named_s"])) for p in pairs],
              ["named_utc", "named_s", "dn", "epoch", "diff_ns", "naming_mismatch",
               "hf_sub", "sub_source", "radiod_sigma_ns", "filter_delay_s"])
    fields = ["named_utc", "named_s", "epoch", "lock", "offset_ns", "sigma_ns", "snr_db",
              "host_gap_ns", "battery_mode", "battery_passed", "battery_accumulating",
              "battery_failures", "oldpair_gap_rtp_ns_median"]
    write_csv(os.path.join(a.out, "edges_a.csv"), edge_rows(ea, va), fields)
    for tag, edges in (("a", ea), ("b", eb)):
        if edges:
            write_csv(os.path.join(a.out, f"oldpair_{tag}.csv"),
                      [dict(p, named_s=e["named_s"]) for e in edges for p in e["packets"]],
                      ["logged_at_unix", "named_s", "gap_rtp_ns", "gap_input_ns"])
    if eb:
        write_csv(os.path.join(a.out, "edges_b.csv"), edge_rows(eb, vb), fields)
        write_csv(os.path.join(a.out, "a_vs_b.csv"),
                  [dict(p, named_utc=iso(p["named_s"])) for p in ab],
                  ["named_utc", "named_s", "dn", "epoch_a", "epoch_b", "rel_ns"])
    write_csv(os.path.join(a.out, "hf_edges.csv"),
              [dict(h, named_utc=iso(h["named_s"])) for h in hf],
              ["named_utc", "named_s", "rtp", "sub", "sub_source", "sub_derived",
               "chain_delay_ns", "authority_state"])
    json.dump(summary, sys.stdout, indent=2, default=str)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
