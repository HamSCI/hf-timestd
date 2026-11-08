#!/usr/bin/env python3
"""Compare radiod's pilot edges with hf-timestd's T6 edges, live.

  compare_live.py LEDGER_A ANCHORS... [--ledger-b LEDGER_B]
                  [--hf-ssrc 206674315] [--fold-s 30] [--out DIR]

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
   pair radiod sent for that channel (refused when the anchors around it
   disagree on the tie), and is compared with radiod's edge projected to
   the same named second (the GPSDO clocks the ADC, so one second is
   exactly INPUT_SAMPRATE samples).  Both channels pass the same kind of
   radiod filter, so both raw positions carry the same filter delay; it
   is reported beside, never subtracted.
2. Scatter: the first differences of edge position minus whole seconds,
   over sqrt(2), between edges no more than two folds apart.  The pass
   flag compares both sides over the SAME paired seconds, on the robust
   sigma.  The two estimates share common-mode noise (one pilot, one
   ADC), so their difference understates neither side's own error.
3. The battery (``t6_battery.T6Battery.evaluate``) over every edge radiod
   marks locked.  radiod does not send its search mode; it is inferred as
   ``bootstrap`` for the first three locked edges of an epoch, as the
   detector confirms its self-seed, and ``tracking`` after.  The implied
   chain delay judged by criterion 7 is radiod's host-clock gap,
   PILOT_HOST_TIME - PILOT_EDGE_UTC.
4. Two receivers, one clock: B1-a's and B1-b's edges, each as its
   counter's offset from whole seconds, differenced pairwise and taken
   about the median per epoch pair.
5. The current pair against the new tie, decomposed per status packet
   (see ``old_pair_gaps``).
"""
import argparse
import bisect
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
FOLD_S = 30
PILOT_KEYS = ("input_epoch", "pilot_edge_sample", "pilot_edge_frac", "pilot_edge_utc",
              "pilot_host_time", "input_samprate", "output_samprate")


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
    xs = [x for x in xs if x is not None and math.isfinite(x)]
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


def drift_by_group(items) -> dict:
    """items: (group key, t, y) -> {str(key): slope per hour}; one fit per group."""
    g = defaultdict(lambda: ([], []))
    for k, t, y in items:
        g[k][0].append(t)
        g[k][1].append(y)
    return {str(k): drift_per_hour(ts, ys) for k, (ts, ys) in g.items()}


def load_jsonl(paths):
    """All rows of all files, sorted by logged_at_unix (stable)."""
    rows = []
    for p in paths:
        with open(p) as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except ValueError:       # a line cut short by a running writer
                    continue
    rows.sort(key=lambda r: r.get("logged_at_unix", 0.0))
    return rows


def groups_by_ssrc(rows) -> dict:
    g = defaultdict(set)
    for r in rows:
        if "group" in r and "ssrc" in r:
            g[r["ssrc"]].add(r["group"])
    return {str(k): sorted(v) for k, v in g.items()}


def iso(s) -> str:
    return datetime.fromtimestamp(s, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- radiod's side ----------------------------------------------------------

def old_pair_gaps(r: dict, named_ns: int, host_gap_ns: int, filter_delay_s) -> dict:
    """The existing status pair's label of the pilot edge, decomposed (ns).

    What the fields name (ka9q-radio, pilot-detector branch; the demod loop
    is linear.c:48-315, and response() runs first in each pass, from
    handle_commands, radio.c:1507):
      GPS_TIME      the host clock read while encoding (radio_status.c:718),
                    after block k went out and before block k+1 is waited for;
      RTP_TIMESNAP  chan->output.rtp.timestamp (radio_status.c:888), which
                    send_output already advanced past block k (audio.c:52,
                    187): the sample AFTER the last one sent, block k+1's first;
      INPUT_SAMPLES filter.out.sample_index (radio_status.c:760): the input
                    index of block k's FIRST sample, one block (1920 output
                    samples) before RTP_TIMESNAP's sample;
      ANCHOR pair   block k's first sample (pilot_chan.c:242-243).
    Raw input positions run one filter delay f LATE: the first input sample
    lands at window position M-1 (filter.c:281,295), so output m of a block
    holds input sample_index + m*decim - f.

    Terms, with h the host clock's offset from UTC:
      gap_rtp      GPS_TIME + (edge - RTP_TIMESNAP) - named second
                   = f + h + L_status, where L_status = how long after the ADC
                   took the newest consumed sample the status read happened;
      host_gap     PILOT_HOST_TIME - PILOT_EDGE_UTC = f + h + L_usb, with L_usb
                   the same lag measured against frontend->samples, the
                   arrival counter (pilot.c:788, pilot_chan.c:302-307);
      host_term    host_gap - f = h + L_usb (h, plus USB arrival lag >= 0);
      residual     gap_rtp - host_gap = L_status - L_usb: the pair's own
                   labelling error, the latency between a sample's arrival
                   and the GPS_TIME read, once the host clock is taken out;
      block_offset gap_input_derived - gap_rtp: the definitional 20 ms that
                   pairing GPS_TIME with INPUT_SAMPLES instead adds.
    """
    decim = r["input_samprate"] // r["output_samprate"]
    base = r["gps_time"] + GPS_TO_UNIX_NS - named_ns          # exact int
    frac = r["pilot_edge_frac"]
    d_out = signed32(r["anchor_rtp"] - r["rtp_timesnap"]) + (
        (r["pilot_edge_sample"] - r["anchor_input_sample"]) + frac) / decim
    gap_rtp = base + d_out * BILLION / r["output_samprate"]
    gap_in = base + ((r["pilot_edge_sample"] - r["input_samples"]) + frac) * BILLION / r["input_samprate"]
    f_ns = (filter_delay_s or 0.0) * 1e9
    return {"logged_at_unix": r.get("logged_at_unix"),
            "gap_rtp_ns": gap_rtp, "gap_rtp_net_ns": gap_rtp - f_ns,
            "gap_input_derived_ns": gap_in, "block_offset_ns": gap_in - gap_rtp,
            "filter_delay_ns": f_ns, "host_term_ns": host_gap_ns - f_ns,
            "residual_ns": gap_rtp - host_gap_ns}


def radiod_edges(rows) -> list:
    """One entry per distinct pilot report, in the order first seen.

    Rows lacking any field the comparison needs are skipped."""
    out, seen = [], {}
    for r in rows:
        if r.get("kind", "pilot") != "pilot" or not all(k in r for k in PILOT_KEYS):
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
        e = seen[key]
        if all(k in r for k in ("gps_time", "rtp_timesnap", "input_samples",
                                "anchor_rtp", "anchor_input_sample")):
            e["packets"].append(old_pair_gaps(r, named_ns, e["host_gap_ns"], e["filter_delay_s"]))
    return out


def _radiod_step(prev, cur) -> float:
    """Edge-to-edge change minus whole seconds, ns."""
    d = ((cur["edge_in"] - prev["edge_in"]) - (cur["named_s"] - prev["named_s"]) * cur["in_rate"]
         + (cur["frac"] - prev["frac"]))
    return d * BILLION / cur["in_rate"]


def _hf_step(prev, cur) -> float:
    d = (signed32(cur["rtp"] - prev["rtp"]) + (cur["sub"] - prev["sub"])
         - (cur["named_s"] - prev["named_s"]) * cur["sr"])
    return d * BILLION / cur["sr"]


def _scatter(diffs_ns, n) -> dict:
    root2 = math.sqrt(2.0)
    return {"n": n, "n_diffs": len(diffs_ns),
            "diff_rms_ns": rms(diffs_ns) / root2,
            "diff_sigma_ns": robust_sigma(diffs_ns) / root2,
            "max_abs_diff_ns": max((abs(x) for x in diffs_ns), default=float("nan"))}


def radiod_scatter(edges, fold_s=FOLD_S) -> dict:
    """Within each epoch, locked edges no more than two folds apart."""
    diffs = [_radiod_step(p, c) for p, c in zip(edges, edges[1:])
             if p["epoch"] == c["epoch"] and p["lock"] and c["lock"]
             and 0 < c["named_s"] - p["named_s"] <= 2 * fold_s]
    return _scatter(diffs, len(edges))


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
        if not all(k in r for k in ("anchor_rtp", "anchor_utc_ns", "named_second_utc_ns",
                                    "sample_rate_hz", "chain_delay_ns")):
            continue
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


def hf_scatter(edges, fold_s=FOLD_S) -> dict:
    diffs = [_hf_step(p, c) for p, c in zip(edges, edges[1:])
             if 0 < c["named_s"] - p["named_s"] <= 2 * fold_s]
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


def hf_to_input_checked(hf: dict, anchors, window_s: float):
    """(status, (epoch, input index, fraction in input samples) or None).

    The nearest anchor on the RTP counter among those logged within
    ``window_s`` of the ledger row (the RTP counter wraps every 12.4 h at
    96 kHz, so time first, then RTP).  Status "no_anchor" when none is in
    the window, "tie_disagrees" when anchors of the chosen epoch in the
    window map the edge to different input indices (a missed block moved
    the tie), else "ok".
    """
    t = hf["logged_at_unix"]
    times = [a["logged_at_unix"] for a in anchors]
    lo, hi = bisect.bisect_left(times, t - window_s), bisect.bisect_right(times, t + window_s)
    cands = anchors[lo:hi]
    if not cands:
        return "no_anchor", None
    a = min(cands, key=lambda a: abs(signed32(hf["rtp"] - a["rtp"])))
    mapped = {tlv.rtp_to_input(hf["rtp"], c["rtp"], c["input"], c["decim"])
              for c in cands if c["epoch"] == a["epoch"]}
    if len(mapped) != 1:
        return "tie_disagrees", None
    return "ok", (a["epoch"], mapped.pop(), hf["sub"] * a["decim"])


def hf_to_input(hf: dict, anchors, window_s: float):
    return hf_to_input_checked(hf, anchors, window_s)[1]


def pair_hf_with_radiod(hf, anchors, r_edges, max_dn=60, window_s=600.0, counts=None) -> list:
    counts = counts if counts is not None else {}
    by_epoch = defaultdict(list)
    for e in r_edges:
        if e["lock"]:
            by_epoch[e["epoch"]].append(e)
    pairs = []
    for h in hf:
        status, conv = hf_to_input_checked(h, anchors, window_s)
        if conv is None:
            counts[status] = counts.get(status, 0) + 1
            continue
        epoch, hi, hfrac = conv
        cands = [e for e in by_epoch.get(epoch, ()) if abs(e["named_s"] - h["named_s"]) <= max_dn]
        if not cands:
            counts["no_radiod_edge"] = counts.get("no_radiod_edge", 0) + 1
            continue
        r = min(cands, key=lambda e: abs(e["named_s"] - h["named_s"]))
        dn = r["named_s"] - h["named_s"]
        d = (hi - r["edge_in"] + dn * r["in_rate"]) + (hfrac - r["frac"])
        diff_ns = d * BILLION / r["in_rate"]
        pairs.append({"named_s": h["named_s"], "dn": dn, "epoch": epoch, "diff_ns": diff_ns,
                      "naming_mismatch": abs(diff_ns) > 0.5 * BILLION,
                      "hf_sub": h["sub"], "sub_source": h["sub_source"],
                      "radiod_sigma_ns": (r["sigma_s"] or float("nan")) * 1e9,
                      "filter_delay_s": r["filter_delay_s"], "_hf": h, "_r": r})
    return pairs


def paired_scatter(pairs, fold_s=FOLD_S) -> dict:
    """Both sides' scatter over the same paired seconds.

    A difference is taken between consecutive pairs only when hf-timestd's
    two edges AND radiod's two edges are each more than zero and at most
    two folds apart, so both sides difference the same span of time.
    """
    hfd, rd = [], []
    for p, c in zip(pairs, pairs[1:]):
        if p["naming_mismatch"] or c["naming_mismatch"] or p["epoch"] != c["epoch"]:
            continue
        hp, hc, rp, rc = p["_hf"], c["_hf"], p["_r"], c["_r"]
        if not (0 < hc["named_s"] - hp["named_s"] <= 2 * fold_s
                and 0 < rc["named_s"] - rp["named_s"] <= 2 * fold_s):
            continue
        hfd.append(_hf_step(hp, hc))
        rd.append(_radiod_step(rp, rc))
    return {"hf_timestd": _scatter(hfd, len(pairs)), "radiod_a": _scatter(rd, len(pairs)),
            "caveat": "both sides measure one pilot through one ADC, so common-mode "
                      "noise is shared and neither figure isolates its method's own error"}


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

OLDPAIR_FIELDS = ("gap_rtp_ns", "gap_rtp_net_ns", "gap_input_derived_ns", "block_offset_ns",
                  "filter_delay_ns", "host_term_ns", "residual_ns")


def current_pair_summary(edges) -> dict:
    pk = [p for e in edges for p in e["packets"]]
    out = {"packets": len(pk)}
    for k in OLDPAIR_FIELDS:
        out[k] = stats([p[k] for p in pk])
    out["note"] = ("gap_rtp = filter delay + host_term + residual.  host_term = "
                   "PILOT_HOST_TIME - PILOT_EDGE_UTC - filter delay (host clock offset plus "
                   "USB arrival lag); residual = the pair's own labelling latency.  "
                   "gap_input_derived pairs GPS_TIME with INPUT_SAMPLES, which names a sample "
                   "one block before RTP_TIMESNAP's (block_offset), so it is derived, not a "
                   "field radiod states as a pair")
    return out


def receiver_summary(edges, fold_s=FOLD_S) -> dict:
    offs = offsets_ns(edges)
    return {
        "reports": len(edges), "locked": sum(e["lock"] for e in edges),
        "epochs": sorted({e["epoch"] for e in edges}),
        "first_named_utc": iso(edges[0]["named_s"]) if edges else None,
        "last_named_utc": iso(edges[-1]["named_s"]) if edges else None,
        "scatter": radiod_scatter(edges, fold_s),
        "offset_from_whole_seconds_ns": stats([o for _, o in offs]),
        "offset_drift_ns_per_hour_by_epoch": drift_by_group(
            (e["epoch"], e["named_s"], o) for e, o in offs),
        "sigma_reported_ns": stats([e["sigma_s"] * 1e9 for e in edges if e["sigma_s"] is not None]),
        "snr_db": stats([e["snr"] for e in edges if e["snr"] is not None]),
        "filter_delay_s": sorted({e["filter_delay_s"] for e in edges if e["filter_delay_s"] is not None}),
        "host_gap_ns": stats([e["host_gap_ns"] for e in edges]),
        "current_pair_vs_pilot": current_pair_summary(edges),
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
        rs = [p["residual_ns"] for p in e["packets"]]
        out.append({"named_utc": iso(e["named_s"]), "named_s": e["named_s"], "epoch": e["epoch"],
                    "lock": int(e["lock"]), "offset_ns": offs.get(id(e), ""),
                    "sigma_ns": (e["sigma_s"] or 0) * 1e9, "snr_db": e["snr"],
                    "host_gap_ns": e["host_gap_ns"],
                    "battery_mode": v.get("mode", ""), "battery_passed": v.get("passed", ""),
                    "battery_accumulating": v.get("accumulating", ""),
                    "battery_failures": "|".join(v.get("failures", [])),
                    "oldpair_gap_rtp_ns_median": statistics.median(gr) if gr else "",
                    "oldpair_residual_ns_median": statistics.median(rs) if rs else ""})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("ledger_a", metavar="LEDGER_A")
    ap.add_argument("anchors", nargs="+", metavar="ANCHORS")
    ap.add_argument("--ledger-b")
    ap.add_argument("--hf-ssrc", type=int, default=206674315)
    ap.add_argument("--max-dn", type=int, default=60, help="max named-second gap when pairing")
    ap.add_argument("--fold-s", type=int, default=FOLD_S, help="fold length, both sides (s)")
    ap.add_argument("--out", default="compare-live-out", metavar="DIR")
    a = ap.parse_args(argv)

    rows_a = load_jsonl([a.ledger_a])
    rows_b = load_jsonl([a.ledger_b]) if a.ledger_b else []
    groups = {"a": groups_by_ssrc(rows_a), "b": groups_by_ssrc(rows_b)}
    for tag, g in groups.items():
        for ssrc, gs in g.items():
            if len(gs) > 1:
                print(f"compare_live: WARNING ledger {tag}: SSRC {ssrc} arrived on "
                      f"{len(gs)} groups {gs}; rows from different radiods may be mixed",
                      file=sys.stderr)
    ea = radiod_edges(rows_a)
    anchors = channel_anchors(rows_a, a.hf_ssrc)
    hf_all = hf_edges(load_jsonl(a.anchors))
    t0 = min((r["logged_at_unix"] for r in rows_a if "logged_at_unix" in r), default=0.0)
    hf = [h for h in hf_all if (h["logged_at_unix"] or 0) >= t0 - 60]
    eb = radiod_edges(rows_b)

    counts = {}
    pairs = pair_hf_with_radiod(hf, anchors, ea, max_dn=a.max_dn, counts=counts)
    good = [p for p in pairs if not p["naming_mismatch"]]
    va, vb = battery_verdicts(ea), battery_verdicts(eb)
    ab = pair_receivers(ea, eb) if eb else []
    subdiff = [abs(h["sub"] - h["sub_derived"]) * BILLION / h["sr"] for h in hf]

    sa = receiver_summary(ea, a.fold_s)
    ps = paired_scatter(good, a.fold_s)
    diff = stats([p["diff_ns"] for p in good])
    summary = {
        "inputs": {"ledger_a": a.ledger_a, "ledger_b": a.ledger_b, "anchors": a.anchors,
                   "hf_ssrc": a.hf_ssrc, "groups_by_ssrc": groups},
        "hf_vs_radiod_a": {
            "hf_edges_in_window": len(hf), "hf_anchor_rows": len(anchors), "pairs": len(pairs),
            "unpaired": counts,
            "naming_mismatches": len(pairs) - len(good),
            "dn_seconds": dict(Counter(p["dn"] for p in good)),
            "diff_ns": diff,
            "note": "hf-timestd minus radiod, raw input-counter positions; both carry the "
                    "same channel-filter delay, reported in radiod_a.filter_delay_s",
            "hf_sub_quality_vs_derived_max_ns": max(subdiff, default=float("nan")),
            "hf_asserted_chain_delay_ns": sorted({h["chain_delay_ns"] for h in hf}),
        },
        "scatter_same_seconds": ps,
        "radiod_a": sa, "hf_timestd": {"scatter": hf_scatter(hf, a.fold_s)},
        "radiod_b": receiver_summary(eb, a.fold_s) if eb else None,
        "battery_a": battery_summary(va), "battery_b": battery_summary(vb) if eb else None,
        "two_receivers_one_clock": {
            "pairs": len(ab), "dn_seconds": dict(Counter(p["dn"] for p in ab)),
            "rel_ns": stats([p["rel_ns"] for p in ab]),
            "drift_ns_per_hour_by_epoch_pair": drift_by_group(
                ((p["epoch_a"], p["epoch_b"]), p["named_s"], p["rel_ns"]) for p in ab),
        },
    }
    md = diff.get("median")
    rs, hs = ps["radiod_a"], ps["hf_timestd"]
    summary["pass"] = {
        "median_abs_diff_le_50ns": None if md is None else abs(md) <= 50.0,
        "radiod_scatter_le_hf_robust_same_seconds": (
            rs["diff_sigma_ns"] <= hs["diff_sigma_ns"] if rs["n_diffs"] else None),
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
              "battery_failures", "oldpair_gap_rtp_ns_median", "oldpair_residual_ns_median"]
    for tag, edges, verdicts in (("a", ea, va), ("b", eb, vb)):
        if not edges:
            continue
        write_csv(os.path.join(a.out, f"edges_{tag}.csv"), edge_rows(edges, verdicts), fields)
        write_csv(os.path.join(a.out, f"oldpair_{tag}.csv"),
                  [dict(p, named_s=e["named_s"]) for e in edges for p in e["packets"]],
                  ["logged_at_unix", "named_s", *OLDPAIR_FIELDS])
    if eb:
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
