#!/usr/bin/env python3
"""Build witness traces for the estimator's acceptance tests. One-time tool.

Reads a fixture's compressed IQ and sidecar, folds the tick envelope in short
blocks, and writes one phase observation per block per band. The absolute UTC
carries an unknown constant (propagation plus station identity), so the traces
support rate and behaviour tests and never an absolute accuracy claim.

Usage:
    python scripts/build_estimator_corpus.py FIXTURE_DIR OUT_DIR \\
        [--resample-ppm N]
"""

from __future__ import annotations

import argparse
import json
import pathlib
from typing import Any, Dict, List, Tuple

import numpy as np
import zstandard

from hf_timestd.core.iq_codec import decode_iq
from hf_timestd.core.registration_acquirer import (  # type: ignore
    band_envelopes,
    fold_envelope,
)

BLOCK_S = 20
MIN_SNR = 1.5
SIGMA_NS = 1_000_000.0  # 1 ms, the acquirer's own origin sigma floor


def read_iq(path: pathlib.Path, meta: dict | None = None) -> np.ndarray:
    """Decode a .bin.zst chunk to complex64 by its sidecar (F16 or F32)."""
    meta = meta or {}
    with open(path, "rb") as fh:
        raw = zstandard.ZstdDecompressor().stream_reader(fh).read()
    return decode_iq(raw, meta.get("dtype", "complex64"), meta.get("scale", 1.0))


def resample_ppm(iq: np.ndarray, ppm: float) -> np.ndarray:
    """Stretch the signal as a ruler error of ``ppm`` would, by interpolation.

    A converter running ``ppm`` fast produces this many more samples per real
    second, so the same real signal lands on a longer index axis.
    """
    n_out = int(round(len(iq) * (1.0 + ppm * 1e-6)))
    src = np.arange(len(iq), dtype=np.float64)
    dst = np.linspace(0.0, len(iq) - 1.0, n_out)
    real = np.interp(dst, src, iq.real)
    imag = np.interp(dst, src, iq.imag)
    return (real + 1j * imag).astype(np.complex64)


def _sample_of_second(second: int, label0: float, fs: int) -> int:
    """Absolute sample index (relative to sample 0) where ``second`` begins.

    The same nominal-rate arithmetic ``fold_envelope`` uses internally for
    its own ``first_sec``/``i0``, generalised to an arbitrary integer second
    so the caller needs no access to its internals.
    """
    return round((second - label0) * fs)


def trace(
    iq: np.ndarray, meta: Dict[str, Any], fixture: str, block_s: int = BLOCK_S
) -> List[Dict[str, Any]]:
    fs = int(meta["sample_rate"])
    label0 = float(meta["start_system_time"])
    rtp0 = int(meta["start_rtp_timestamp"])
    channel = str(meta["channel_name"])
    out: List[Dict[str, Any]] = []
    block_len = block_s * fs
    # One (rtp, second) per band, carried across blocks so each new row
    # can be checked against the last for the whole-second ambiguity
    # below. ``second``, not ``utc_ns``: it is the trustworthy half of
    # the pair, and storing it lets the next row compare against a
    # reference that was never itself adjusted.
    last_by_band: Dict[str, Tuple[int, int]] = {}
    for b in range(len(iq) // block_len):
        start = b * block_len
        end = start + block_len
        seg = np.abs(iq[start:end]).astype(np.float64)
        seg -= seg.mean()
        label_s = label0 + b * block_s
        for band, env in band_envelopes(seg, fs).items():
            profile, rows = fold_envelope(env, fs, label_s, block_s)
            if rows == 0:
                continue
            peak = int(np.argmax(profile))
            snr = float(profile[peak] / (np.median(profile) + 1e-12))
            if snr < MIN_SNR:
                continue
            # ``peak`` names the intra-second SAMPLE this block's tick
            # folded onto -- which register of a nominal second the tick
            # occupies, averaged over every repeat this block covered. That
            # is information about the SAMPLE, not a correction to the
            # tick's own UTC: WWV, WWVH and BPM all mark the start of a
            # real second regardless of how fast or slow this receiver's
            # ruler happens to run, so a tick's UTC is exactly the integer
            # second it belongs to, nothing added. Folding ``peak`` into
            # utc_ns (as this generator used to) reported that same
            # sample-offset twice: once correctly, as where the tick sits
            # in the index, and a second time, with the opposite sign's
            # worth of consequence for a two-state (phase, rate) filter, as
            # a shift in when the tick happened. One nominal second inside
            # the block stands in for the whole block's fold -- the middle
            # one, since the average is symmetric around it -- and ``peak``
            # belongs entirely on that second's sample index.
            second = int(np.floor(label0 + b * block_s + block_s // 2))

            # ``second`` is a LABEL, read straight off the block's own
            # position and independent of ``peak`` -- it is reliable on its
            # own (block ``b`` always names its middle nominal second the
            # same way, drift or none). ``peak`` is the opposite: it is a
            # measurement, in [0, fs), of where the tick sits WITHIN
            # whichever second it belongs to, and that reduction mod one
            # second is exactly what makes it ambiguous BETWEEN seconds --
            # unable to say by itself whether the tick landed a moment
            # before ``second`` began or a moment after. At a governed
            # station the WWV/WWVH/BPM minute marker (an 800 ms pulse once
            # a minute) resolves that same ambiguity by giving a witness an
            # unambiguous anchor to count seconds from. A recording has no
            # marker to lean on, but it has something the live acquirer
            # never does: it is read end to end, so each new row can be
            # checked against the last one instead of a marker. A real
            # ruler drifts at most tens of parts per million, so the sample
            # count implied by the ALREADY-TRUSTED ``second`` labels of two
            # rows predicts the RTP advance between them to within a few
            # samples even across a 20 s gap -- nowhere near the roughly
            # ``fs`` samples (one whole second) a genuine one-cycle
            # ambiguity would move ``peak`` by. So a ``rtp`` that disagrees
            # with that prediction by close to a whole multiple of ``fs``
            # is not real drift, it is the SAME tick's ``peak`` measured
            # one cycle early or late. Only ``rtp`` moves to correct it,
            # by that whole multiple of ``fs`` -- ``second`` stays
            # exactly what the block already said, so the LABEL never
            # bleeds into how any later row is read. The CYCLE CHOICE is
            # a different matter: it is the corrected ``rtp`` that seeds
            # the next row's own prediction (below), so one wrong snap
            # here does not stay local -- it shifts every later row of
            # this band's series by that same whole number of seconds,
            # because each later row is compared against this one, not
            # against a fresh, independent reference.
            sample_naive = _sample_of_second(second, label0, fs)
            rtp = rtp0 + sample_naive + peak
            prev = last_by_band.get(band)
            if prev is not None:
                last_rtp, last_second = prev
                # ``second`` never moves -- it is read fresh from the
                # block every row, so THAT label never inherits an
                # earlier row's mistake. ``rtp``'s CYCLE is not the same
                # story: ``last_rtp`` below is the PRECEDING row's
                # already-corrected value, so a wrong snap on one row
                # becomes the reference the next row corrects against,
                # and propagates for the rest of this band's series
                # rather than washing out. (Measured on the auxiliary,
                # low-SNR WWV channels this generator also emits: several
                # adjacent steps of 450-472 ms, where a boundary snap is
                # close to a coin flip.) The four rows the acceptance
                # table actually selects sit at 2 ms and under -- see the
                # per-series worst-step line ``main`` prints below.
                expected_delta = (second - last_second) * fs
                wraps = round((rtp - last_rtp - expected_delta) / fs)
                rtp -= wraps * fs

            utc_ns = second * 1_000_000_000
            last_by_band[band] = (rtp, second)
            out.append(
                {
                    "tier": "T3",
                    "rtp": int(rtp),
                    "utc_ns": int(utc_ns),
                    "sigma_ns": SIGMA_NS,
                    "plane": "label",
                    "source": "fold-peak",
                    "band": band,
                    "channel": channel,
                    "snr": snr,
                    "fixture": fixture,
                }
            )
    return out


def _worst_adjacent_steps(
    rows: List[Dict[str, Any]], fs: int
) -> Dict[str, float]:
    """Worst adjacent-row jump in each band's implied correction, in ns.

    The implied correction is ``utc_ns - rtp * 1e9 / fs``: a real ruler's
    own drift moves it smoothly, so a single large step signals a wrong
    whole-second cycle choice rather than genuine rate. Printed per band
    so the risk from FINDING 3 above -- a cycle choice that turns out
    wrong propagating through the rest of that band's series -- is
    visible at generation time rather than only discoverable by re-reading
    the corpus afterwards.
    """
    by_band: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        by_band.setdefault(r["band"], []).append(r)
    worst: Dict[str, float] = {}
    for band, band_rows in by_band.items():
        band_rows = sorted(band_rows, key=lambda r: r["rtp"])
        if len(band_rows) < 2:
            worst[band] = 0.0
            continue
        corrections = [
            r["utc_ns"] - r["rtp"] * 1_000_000_000.0 / fs for r in band_rows
        ]
        steps = [abs(b - a) for a, b in zip(corrections, corrections[1:])]
        worst[band] = max(steps)
    return worst


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("fixture_dir", type=pathlib.Path)
    ap.add_argument("out_dir", type=pathlib.Path)
    ap.add_argument("--resample-ppm", type=float, default=None)
    args = ap.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    for sidecar in sorted(args.fixture_dir.glob("*.json")):
        binary = sidecar.with_suffix(".bin.zst")
        if not binary.exists():
            continue
        meta = json.loads(sidecar.read_text())
        # A gap is a hole in the sample stream, and this generator cannot
        # see one. It walks the index axis at the nominal rate from block 0
        # -- ``_sample_of_second`` and the per-band unwrap both assume that
        # the sample count between two seconds is what the nominal rate
        # says -- so a dropped block shifts every LATER block's rtp by the
        # samples the recorder never wrote. The whole-second unwrap then
        # snaps that shift onto the second lattice and mislabels the rest
        # of the series without a murmur. Refuse the fixture rather than
        # emit a corpus whose provenance nobody can reconstruct. The
        # committed traces all came from sidecars reporting zero, so this
        # guards future runs rather than repairing past ones.
        if "gap_count" not in meta:
            raise SystemExit(
                f"{sidecar.name}: no gap_count, so completeness cannot be"
                " checked; refusing to trace it"
            )
        if int(meta["gap_count"]) != 0:
            raise SystemExit(
                f"{sidecar.name}: gap_count={meta['gap_count']}, and this"
                " generator extrapolates across a gap, mislabelling every"
                " block after it; refusing to trace it"
            )
        iq = read_iq(binary, meta)
        suffix = ""
        if args.resample_ppm is not None:
            iq = resample_ppm(iq, args.resample_ppm)
            suffix = f"-resampled{args.resample_ppm:+g}ppm"
        fixture = f"{args.fixture_dir.name}{suffix}"
        rows = trace(iq, meta, fixture)
        name = f"{fixture}-{meta['channel_name']}.jsonl"
        out_text = "".join(json.dumps(r) + "\n" for r in rows)
        (args.out_dir / name).write_text(out_text)
        print(f"{name}: {len(rows)} observations")
        for band, worst_ns in sorted(
            _worst_adjacent_steps(rows, int(meta["sample_rate"])).items()
        ):
            print(
                f"  band {band}: worst adjacent step"
                f" {worst_ns / 1e6:.3f} ms"
            )


if __name__ == "__main__":
    main()
