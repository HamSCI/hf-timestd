#!/usr/bin/env python3
"""Check label_record.py's recordings against what the signals say.

  check_record.py pilot  DIR/45375-<start>       # each second's TS-1 edge vs its label
  check_record.py wwv    DIR/285821745-<start>   # WWV/WWVH tick arrival vs the label
  check_record.py hflabel DIR/285821745-<start> DIR/206674315-<start> LEDGER.jsonl...
        [--cmap-json FILE]   # hf-timestd's CounterMap label for the same RTP

Each prints one JSON object.  Needs numpy (and hamsci_dsp for hflabel).

pilot: the TS-1 pilot is a carrier whose BPSK polarity flips at every UTC
second.  Around each second's predicted sample (where the label crosses the
whole second) it derotates by half the angle of mean(x^2), takes the real
part, and finds where a windowed-sinc interpolant of it crosses the midpoint
of the two plateaus.  The error is measured minus predicted, in ns.

wwv: the tick is a 5 ms burst of 1000 Hz (WWV) or 1200 Hz (WWVH) AM, starting
on the second at the transmitter.  The envelope |x| is folded over the
recording's seconds (leaving out :00, which carries the minute tone, and
:29 and :59, which carry no tick), and correlated with a 5 ms tone template at every lag; the lag of the
largest correlation magnitude is the arrival, ms after the labelled second.

hflabel: hf-timestd's way of placing UTC on a channel's RTP: its T6 anchor
(anchor_rtp on its T6 channel's counter, anchor_utc_ns) carried to the other
channel through both channels' CounterMaps (INPUT_SAMPLES/RTP_TIMESNAP),
``utc = anchor_utc_ns + (cm_wwv.input_index(rtp) - cm_t6.input_index(anchor_rtp)) / fs_in``,
the formula of Registration.in_domain.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

SKIP_SECONDS = {0, 29, 59}       # WWV: no tick at :29 and :59; :00 has the minute tone


def load(stem: str):
    side = json.loads(Path(stem + ".json").read_text())
    x = np.fromfile(Path(stem + ".cf32"), dtype="<c8").astype(np.complex128)
    return x, side


def predicted_index(side: dict, k_seconds: int) -> float:
    """Fractional sample index whose label is window start + k seconds + chain."""
    period = side["sample_period_ns"]
    after = side["first_sample"]["after_window_start_ns"]
    return (k_seconds * 1e9 + side["chain_delay_ns"] - after) / period


def _sinc_interp(y: np.ndarray, t: float, half: int = 256, beta: float = 8.0) -> float:
    n0 = int(math.floor(t))
    n = np.arange(n0 - half + 1, n0 + half + 1)
    n = n[(n >= 0) & (n < len(y))]
    d = t - n
    w = np.kaiser(2 * half + 1, beta)[np.clip(np.round(d).astype(int) + half, 0, 2 * half)]
    return float(np.sum(y[n] * np.sinc(d) * w))


def edge_position(x: np.ndarray, p: float, *, plateau=(40, 400), search=8,
                  half: int = 256) -> float | None:
    """Sub-sample position of the polarity flip nearest ``p``."""
    c = int(round(p))
    lo, hi = c - plateau[1] - half, c + plateau[1] + half
    if lo < 0 or hi > len(x):
        return None
    seg = x[c - plateau[1]:c + plateau[1]]
    ph = 0.5 * np.angle(np.mean(seg * seg))
    y = (x[lo:hi] * np.exp(-1j * ph)).real
    cc = c - lo
    pre = y[cc - plateau[1]:cc - plateau[0]].mean()
    post = y[cc + plateau[0]:cc + plateau[1]].mean()
    if pre > post:
        y, pre, post = -y, -pre, -post
    level = 0.5 * (pre + post)
    # integer bracket: the last sample at or below the level before the first above it
    for n in range(cc - search, cc + search):
        if y[n] <= level < y[n + 1]:
            a, b = float(n), float(n + 1)
            fa = _sinc_interp(y, a, half) - level
            fb = _sinc_interp(y, b, half) - level
            if fa > 0 or fb < 0:     # the interpolant disagrees with the samples: widen
                a, b = n - 1.0, n + 2.0
                fa = _sinc_interp(y, a, half) - level
                fb = _sinc_interp(y, b, half) - level
                if fa > 0 or fb < 0:
                    return None
            for _ in range(40):
                m = 0.5 * (a + b)
                fm = _sinc_interp(y, m, half) - level
                if fm <= 0:
                    a = m
                else:
                    b = m
            return lo + 0.5 * (a + b)
    return None


def check_pilot(stem: str) -> dict:
    x, side = load(stem)
    period = side["sample_period_ns"]
    rows = []
    for k in range(side["window"]["seconds"] + 1):
        p = predicted_index(side, k)
        e = edge_position(x, p)
        if e is None:
            continue
        rows.append({"second": k, "predicted": p, "measured": e, "err_ns": (e - p) * period})
    err = np.array([r["err_ns"] for r in rows])
    n = len(err)
    mad = 1.4826 * np.median(np.abs(err - np.median(err))) if n else None
    return {
        "file": side["file"], "n_edges": n, "seconds": side["window"]["seconds"],
        "err_ns_mean": float(err.mean()) if n else None,
        "err_ns_median": float(np.median(err)) if n else None,
        "err_ns_std": float(err.std(ddof=1)) if n > 1 else None,
        "err_ns_robust_sigma": float(mad) if n else None,
        "err_ns_min": float(err.min()) if n else None,
        "err_ns_max": float(err.max()) if n else None,
        "mean_standard_error_ns": float(err.std(ddof=1) / math.sqrt(n)) if n > 1 else None,
        "sample_period_ns": period,
        "pilot_report_sigma_ns": (side["pilot_report"]["pilot_edge_sigma"] or 0) * 1e9,
        "rows": rows,
    }


def tick_arrival(x: np.ndarray, side: dict, tone_hz: float, *, start_second_of_minute: int,
                 max_lag_ms: float = 60.0, tick_ms: float = 5.0,
                 search_ms: tuple | None = None) -> dict:
    fs = side["output_samprate"]
    env = np.abs(x)
    spp = int(fs)
    nsec = side["window"]["seconds"]
    span = int(round((max_lag_ms + 2 * tick_ms) * fs / 1000)) + int(fs * 0.010)
    pre = int(fs * 0.010)                          # 10 ms before the second
    acc = np.zeros(span)
    used = 0
    sub = None                                     # p - round(p): the same every second
    for k in range(nsec):
        if (start_second_of_minute + k) % 60 in SKIP_SECONDS:
            continue
        p = predicted_index(side, k)
        i0 = int(round(p)) - pre
        if i0 < 0 or i0 + span > len(env):
            continue
        sub = p - round(p)
        seg = env[i0:i0 + span]
        acc += seg - seg.mean()
        used += 1
    acc /= max(used, 1)
    m = int(round(tick_ms * fs / 1000))
    tmpl = np.exp(-2j * np.pi * tone_hz * np.arange(m) / fs)
    lags = np.arange(0, span - m)
    corr = np.array([abs(np.dot(acc[i:i + m], tmpl)) for i in lags])
    if search_ms is not None:
        lo = max(0, int(round(search_ms[0] * fs / 1000)) + pre)
        hi = min(len(corr), int(round(search_ms[1] * fs / 1000)) + pre)
        ib = lo + int(np.argmax(corr[lo:hi]))
    else:
        ib = int(np.argmax(corr))
    frac = _triangle_apex(corr, ib, m) - ib
    # index i0 sits at the labelled second minus pre, offset by round(p) - p
    lag_samples = ib + frac - pre - (sub or 0.0)
    noise = float(np.median(corr))
    return {"tone_hz": tone_hz, "seconds_folded": used, "search_ms": search_ms,
            "peak": float(corr[ib]),
            "arrival_ms_after_labelled_second": lag_samples * 1000.0 / fs,
            "peak_to_median": float(corr[ib] / noise) if noise else None,
            "resolution_note": "lag of the 5 ms template's best match, apex of the correlation "
                               "triangle by two flank fits, "
                               "measured from the labelled second (sub-sample offset removed)"}


def _triangle_apex(corr: np.ndarray, ib: int, m: int) -> float:
    """Apex of the correlation triangle around ``ib``: a template of m samples
    against a burst of m samples gives a triangle 2m wide.  Fit a line to each
    flank between 30 % and 90 % of the peak and intersect them; a parabola
    through the top three points is biased by up to half a sample on a
    triangle's sharp apex.  Falls back to ``ib`` when a flank is too short."""
    peak = corr[ib]
    lo, hi = max(0, ib - m), min(len(corr), ib + m + 1)
    idx = np.arange(lo, hi)
    sel = (corr[lo:hi] >= 0.3 * peak) & (corr[lo:hi] <= 0.9 * peak)
    left, right = idx[sel & (idx < ib)], idx[sel & (idx > ib)]
    if len(left) < 3 or len(right) < 3:
        return float(ib)
    a1, b1 = np.polyfit(left, corr[left], 1)
    a2, b2 = np.polyfit(right, corr[right], 1)
    if a1 <= 0 or a2 >= 0:
        return float(ib)
    return float((b2 - b1) / (a1 - a2))


def check_wwv(stem: str) -> dict:
    x, side = load(stem)
    s0 = (side["window"]["start_unix_ns"] // 10**9) % 60
    out = {"file": side["file"], "frequency_hz": side.get("frequency_hz"),
           "first_sample_after_start_ns": side["first_sample"]["after_window_start_ns"]}
    out["wwv_1000hz"] = tick_arrival(x, side, 1000.0, start_second_of_minute=s0)
    # WWVH's tick from Kauai lands ~22 ms after WWV's at a US-midwest station,
    # inside WWV's 40 ms quiet zone.  An unrestricted 1200 Hz search finds the
    # WWV tick's own spectral leakage instead (1200 Hz sits at the first null
    # of a 5 ms burst only when aligned), so search only after WWV's tick ends.
    w = out["wwv_1000hz"]["arrival_ms_after_labelled_second"]
    out["wwvh_1200hz"] = tick_arrival(x, side, 1200.0, start_second_of_minute=s0,
                                      search_ms=(w + 2 * 5.0, w + 26.0))
    out["wwvh_1200hz"]["peak_over_wwv_peak"] = (out["wwvh_1200hz"]["peak"]
                                                / out["wwv_1000hz"]["peak"])
    return out


def _polls(status_jsonl: str):
    from hamsci_dsp.counter_map import CounterPoll
    polls = []
    for line in Path(status_jsonl).read_text().splitlines():
        d = json.loads(line)
        if all(k in d for k in ("rtp_timesnap", "input_samples", "filter_blocksize",
                                "gps_time")):
            polls.append(CounterPoll(rtp_timesnap=d["rtp_timesnap"],
                                     input_samples=d["input_samples"],
                                     filter_blocksize=d["filter_blocksize"],
                                     input_samprate=d["input_samprate"],
                                     output_samprate=d["output_samprate"],
                                     gps_time_ns=d["gps_time"]))
    return polls


def check_hflabel(wwv_stem: str, t6_stem: str, ledgers, cmap_json: str | None) -> dict:
    from hamsci_dsp.counter_map import CounterMap
    side = json.loads(Path(wwv_stem + ".json").read_text())
    t6side = json.loads(Path(t6_stem + ".json").read_text())
    cm_w = CounterMap.from_polls(_polls(wwv_stem + ".status.jsonl"))
    cm_t = CounterMap.from_polls(_polls(t6_stem + ".status.jsonl"))
    cm_live = None
    if cmap_json:
        cm_live = CounterMap.from_dict(json.loads(Path(cmap_json).read_text()))
    t0 = side["window"]["start_unix_ns"] / 1e9
    rows = []
    for path in ledgers:
        for line in Path(path).read_text().splitlines():
            d = json.loads(line)
            if abs(d["named_second_utc_ns"] / 1e9 - t0) <= 120:
                rows.append(d)
    rtp0 = side["first_sample"]["rtp"]
    ours = (side["first_sample"]["utc_unix_ns"], side["first_sample"]["utc_frac_ns"])
    fs_in = side["input_samprate"]
    out_rows = []
    for d in rows:
        for name, cm in (("from_polls", cm_w), ("hf_timestd_live", cm_live)):
            if cm is None:
                continue
            delta_in = cm.input_index(rtp0) - cm_t.input_index(d["anchor_rtp"])
            # hf label minus ours, ns, without forming 1.8e18 floats
            diff = (d["anchor_utc_ns"] - ours[0]) + delta_in * 1e9 / fs_in - ours[1]
            out_rows.append({"anchor_named_utc_ns": d["named_second_utc_ns"], "cmap": name,
                             "chain_delay_ns": d.get("chain_delay_ns"),
                             "hf_minus_ours_ns": diff})
    # the one-block convention: CounterMap index minus the ANCHOR pair's index
    a_w = side["channel_status"]
    a_t = t6side["channel_status"]
    blk_w = cm_w.input_index(a_w["anchor_rtp"]) - a_w["anchor_input_sample"] if cm_w else None
    blk_t = cm_t.input_index(a_t["anchor_rtp"]) - a_t["anchor_input_sample"] if cm_t else None
    diffs = np.array([r["hf_minus_ours_ns"] for r in out_rows if r["cmap"] == "from_polls"])
    return {
        "rtp": rtp0, "our_label_utc_ns": ours,
        "our_chain_delay_ns": side["chain_delay_ns"],
        "cmap_wwv_from_polls": cm_w.to_dict() if cm_w else None,
        "cmap_t6_from_polls": cm_t.to_dict() if cm_t else None,
        "cmap_wwv_live": cm_live.to_dict() if cm_live else None,
        "cmap_minus_anchor_pair_input_samples": {"wwv": blk_w, "t6": blk_t},
        "n_ledger_rows": len(rows),
        "hf_minus_ours_ns_median": float(np.median(diffs)) if len(diffs) else None,
        "hf_minus_ours_ns_min": float(diffs.min()) if len(diffs) else None,
        "hf_minus_ours_ns_max": float(diffs.max()) if len(diffs) else None,
        "rows": out_rows,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pilot").add_argument("stem")
    sub.add_parser("wwv").add_argument("stem")
    h = sub.add_parser("hflabel")
    h.add_argument("wwv_stem")
    h.add_argument("t6_stem")
    h.add_argument("ledgers", nargs="+")
    h.add_argument("--cmap-json")
    a = ap.parse_args(argv)
    if a.cmd == "pilot":
        r = check_pilot(a.stem)
    elif a.cmd == "wwv":
        r = check_wwv(a.stem)
    else:
        r = check_hflabel(a.wwv_stem, a.t6_stem, a.ledgers, a.cmap_json)
    print(json.dumps(r, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
