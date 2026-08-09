#!/usr/bin/env python3
"""T6 offline estimator harness.

Two modes:

``sweep`` (original) -- reads a raw int16 I/Q capture of the T6 BPSK-PPS
channel (as written by `pcmrecord -r`) and measures, for a range of
antisymmetric-MF half-lengths N, the sharpness of the correlation feature
(% of peak per ms) and the per-second scatter of the resulting edge
estimate.  Feature sharpness improves as 1/N while SNR degrades as
1/sqrt(N); the sweep locates where timing precision actually optimises.
The deployed calibrator uses N = sample_rate//2 (the first row).

``fine`` (spec §8 offline replay / acceptance harness) -- replays a raw
I/Q capture through the deployed `BpskEdgeFineStage` in service-sized
batches, folding `--fold-seconds` at a time, and reports per-window
`edge_offset_samples`, the spread across windows, and an independent
early-minus-late discriminant cross-check (harness-only; never shipped
in the service path).

Usage:
    t6_estimator_sweep.py <capture.raw> [sample_rate]        # legacy sweep
    t6_estimator_sweep.py --mode sweep --input <capture.raw> [--sample-rate 96000]
    t6_estimator_sweep.py --mode fine --input <capture.raw> \\
        --sample-rate 96000 --coarse <samples> --fold-seconds 8 \\
        [--format {c64,s16}] [--search-window-ms 6.0]
"""
import argparse
import sys

import numpy as np


# ---------------------------------------------------------------------------
# fine mode: replay harness (spec §8)
# ---------------------------------------------------------------------------

def replay_fine(iq, sample_rate, coarse_offset, fold_seconds, batch=1740,
                 search_window_ms=6.0):
    """Run BpskEdgeFineStage over an in-memory IQ array in service-sized
    batches with clean synthetic RTP labels.  Returns all estimates."""
    from hf_timestd.core.bpsk_edge_fine_stage import BpskEdgeFineStage
    stage = BpskEdgeFineStage(int(sample_rate), fold_seconds=int(fold_seconds),
                               search_window_ms=float(search_window_ms))
    stage.set_coarse_offset_samples(float(coarse_offset))
    out = []
    i = 0
    while i < len(iq):
        r = stage.process_samples(iq[i:i + batch], i & 0xFFFFFFFF)
        if r is not None:
            out.append(r)
        i += batch
    return out


def early_late_offset(avg_I, coarse, gate_ms, sample_rate):
    """Independent early-minus-late discriminant (cross-check ONLY --
    never shipped in the service path; spec §8).  Slides a two-gate
    window around ``coarse`` and returns the offset where the early
    and late gate means balance, by linear interpolation of the
    discriminant's zero crossing."""
    g = max(2, int(gate_ms * 1e-3 * sample_rate))
    p = len(avg_I)
    span = np.arange(-2 * g, 2 * g + 1)
    d = np.empty(len(span))
    for j, s in enumerate(span):
        c = (coarse + s) % p
        late = np.take(avg_I, np.arange(c, c + g) % p).mean()
        early = np.take(avg_I, np.arange(c - g, c) % p).mean()
        d[j] = late - early
    # |d| is maximal when the gates straddle the edge; the *derivative*
    # of d crosses zero there.  Use the extremum of d, refined by
    # parabolic interpolation.
    k = int(np.argmax(np.abs(d)))
    if 0 < k < len(d) - 1:
        y0, y1, y2 = np.abs(d[k - 1]), np.abs(d[k]), np.abs(d[k + 1])
        denom = (y0 - 2 * y1 + y2)
        frac = 0.5 * (y0 - y2) / denom if denom != 0 else 0.0
    else:
        frac = 0.0
    # `late` = indices [c, c+g) and `early` = indices [c-g, c) (c = coarse
    # + span[k]) share their boundary between discrete indices c-1 and c,
    # i.e. at continuous position c-0.5 -- so a true edge sitting exactly
    # on that boundary (where the gates are maximally unbalanced) reports
    # here as c, half a sample high. Correct by -0.5 so this cross-check
    # lands in the same continuous coordinate as the fine stage's own
    # zero-crossing (linear-fit x0 over integer sample indices). Verified
    # against BpskEdgeFineStage on synthetic data: before this correction
    # the discrepancy was a deterministic +0.5 samples (~5.2 us @ 96 kHz),
    # constant across edge positions and noise-independent; see the T6
    # Task 7 acceptance-gate report.
    return float((coarse + span[k] + frac - 0.5) % p)


def _fold_block_I(iq_block, sample_rate, fold_seconds):
    """Reproduce BpskEdgeFineStage's internal fold + derotation for a
    single fold block (exactly `fold_seconds * sample_rate` samples,
    aligned to the block start) so the harness can feed the independent
    early_late_offset cross-check the same derotated in-phase profile
    the fine stage itself computed.  Harness-only (mirrors the fine
    stage's private `_compute_estimate` math; not imported from it so
    this file has no dependency on stage internals)."""
    n = fold_seconds * sample_rate
    sec = np.arange(n) // sample_rate
    sign = 1.0 - 2.0 * (sec & 1)
    avg = (iq_block[:n].astype(np.complex128) * sign).reshape(
        fold_seconds, sample_rate).mean(axis=0)
    phi = 0.5 * float(np.angle(np.mean(avg ** 2)))
    return np.real(avg * np.exp(-1j * phi))


def _load_s16le_iq(path):
    """Load s16 little-endian interleaved I/Q as complex64."""
    raw = np.fromfile(path, dtype='<i2')
    return (raw[0::2].astype(np.float32)
            + 1j * raw[1::2].astype(np.float32)).astype(np.complex64)


def _load_c64_iq(path):
    """Load raw complex64 (float32 I/Q interleaved) I/Q, as written by the
    ka9q pcmrecord capture path -- see /root/appliance/t6-offline/rawiq.py,
    which reads t6-rawiq.bin this way (verified: gives the documented
    24.72 s duration and a clean single BPSK edge/sec; the s16-interleaved
    reading of the same file does not)."""
    return np.fromfile(path, dtype=np.complex64)


_LOADERS = {"s16": _load_s16le_iq, "c64": _load_c64_iq}


def run_fine(input_path, sample_rate, coarse, fold_seconds, fmt="c64",
             search_window_ms=6.0, gate_ms=2.0):
    iq = _LOADERS[fmt](input_path)
    print(f"loaded {len(iq)} samples = {len(iq)/sample_rate:.2f} s "
          f"@ {sample_rate} Hz (format={fmt})")

    ests = replay_fine(iq, sample_rate, coarse_offset=coarse,
                        fold_seconds=fold_seconds,
                        search_window_ms=search_window_ms)
    if not ests:
        print("no fine-stage estimates produced (no crossing found in "
              "window?) -- widen --search-window-ms or re-derive --coarse "
              "via mf_probe.py", file=sys.stderr)
        return ests

    block_len = int(fold_seconds) * int(sample_rate)
    c = int(round(coarse)) % sample_rate
    print(f"\n{'win':>3} {'edge_offset(samp)':>18} {'edge_offset(us)':>16} "
          f"{'fit_rms':>10} {'plateau_amp':>12} {'EL cross-check(us)':>19} "
          f"{'EL diff(us)':>12}")
    print("-" * 96)
    for w, est in enumerate(ests):
        edge_us = est.edge_offset_samples / sample_rate * 1e6
        block = iq[w * block_len:(w + 1) * block_len]
        el_diff_us = float("nan")
        el_us = float("nan")
        if len(block) == block_len:
            I = _fold_block_I(block, sample_rate, fold_seconds)
            el = early_late_offset(I, c, gate_ms=gate_ms,
                                    sample_rate=sample_rate)
            el_us = el / sample_rate * 1e6
            el_diff_us = abs(el - est.edge_offset_samples) / sample_rate * 1e6
        print(f"{w:>3} {est.edge_offset_samples:>18.3f} {edge_us:>16.3f} "
              f"{est.fit_rms:>10.4f} {est.plateau_amplitude:>12.4f} "
              f"{el_us:>19.3f} {el_diff_us:>12.3f}")

    if len(ests) > 1:
        offsets = np.array([e.edge_offset_samples for e in ests])
        spread_us = (offsets.max() - offsets.min()) / sample_rate * 1e6
        print(f"\nspread across {len(ests)} windows: {spread_us:.3f} us "
              f"(gate: <= 1.0 us)")
    return ests


# ---------------------------------------------------------------------------
# sweep mode (original tool, unchanged behaviour)
# ---------------------------------------------------------------------------

def run_sweep(path, sr):
    i16 = np.fromfile(path, dtype=np.int16)
    x = i16[0::2].astype(np.float64) + 1j * i16[1::2].astype(np.float64)
    x -= x.mean()
    print(f"loaded {len(x)} samples = {len(x)/sr:.2f} s @ {sr} Hz")

    # BPSK carrier recovery: square strips the +-1 modulation, leaving 2f
    seg = x[: min(len(x), sr * 8)]
    sq = seg ** 2
    sq -= sq.mean()
    fr = np.fft.fftshift(np.fft.fftfreq(len(sq), 1.0 / sr))
    f0 = fr[int(np.argmax(np.fft.fftshift(np.abs(np.fft.fft(sq)))))] / 2.0
    print(f"residual carrier: {f0:+.4f} Hz -> derotating")
    x *= np.exp(-2j * np.pi * f0 * np.arange(len(x)) / sr)

    csum = np.concatenate(([0.0 + 0.0j], np.cumsum(x)))

    def mf(N):
        """Antisymmetric MF, same form as the deployed calibrator:
           y[i] = sum(x[i+1:i+N+1]) - sum(x[i-N:i])  -> magnitude"""
        idx = np.arange(N, len(x) - N)
        yc = (csum[idx + N + 1] - csum[idx + 1]) - (csum[idx] - csum[idx - N])
        return idx, np.abs(yc)

    # Coarse anchors from the deployed configuration (N = sr/2), one per second
    Nc = sr // 2
    idc, yc = mf(Nc)
    anchors = []
    for k in range(len(yc) // sr):
        w = yc[k * sr:(k + 1) * sr]
        if len(w) == sr:
            anchors.append(idc[k * sr + int(np.argmax(w))])
    anchors = np.array(anchors)
    # keep only anchors that sit ~1 s apart (drop the aliased/edge ones)
    if len(anchors) > 2:
        dt = np.diff(anchors)
        good = np.concatenate(([True], np.abs(dt - sr) < 0.2 * sr))
        anchors = anchors[good]
    print(f"coarse anchors: {len(anchors)}\n")

    print(f"{'N':>8} {'span':>10} {'slope %/ms':>12} {'scatter us':>12} {'jitter us':>11}")
    print("-" * 58)
    GUARD = int(0.005 * sr)
    for N in (sr // 2, sr // 10, sr // 100, sr // 500, sr // 1000, sr // 5000, sr // 20000):
        if N < 4:
            continue
        idx, y = mf(N)
        base = idx[0]
        pos = []
        for a in anchors:
            lo, hi = a - GUARD - base, a + GUARD - base
            if lo < 0 or hi >= len(y):
                continue
            w_ = y[lo:hi]
            j = int(np.argmax(w_))
            # quadratic (3-point) interpolation for the sub-sample peak position
            frac = 0.0
            if 0 < j < len(w_) - 1:
                y0, y1, y2 = w_[j-1], w_[j], w_[j+1]
                den = y0 - 2*y1 + y2
                if den != 0:
                    frac = 0.5 * (y0 - y2) / den
            pos.append(lo + j + frac + base - a)
        if len(pos) < 3:
            continue
        pos = np.array(pos, dtype=np.float64)
        a = anchors[len(anchors) // 2] - base
        w = int(0.002 * sr)
        g = float("nan")
        if a - w > 0 and a + w < len(y):
            s = y[a - w:a + w]
            xs = (np.arange(len(s)) - w) / sr * 1000.0
            h = len(s) // 2
            g = abs(np.polyfit(xs[:h], s[:h], 1)[0]) / s.max() * 100
        print(f"{N:>8} {2*N/sr*1000:>8.2f}ms {g:>12.3f} "
              f"{np.std(pos)/sr*1e6:>12.1f} {np.std(np.diff(pos))/sr*1e6:>11.1f}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv

    # Legacy positional form: `t6_estimator_sweep.py <capture.raw> [sample_rate]`
    # (no --mode/--input flags at all) -> sweep mode, unchanged.
    if argv and not argv[0].startswith("-"):
        path = argv[0]
        sr = int(argv[1]) if len(argv) > 1 else 96000
        run_sweep(path, sr)
        return

    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=["sweep", "fine"], default="sweep")
    parser.add_argument("--input", required=True, help="raw I/Q capture path")
    parser.add_argument("--sample-rate", type=int, default=96000)
    parser.add_argument("--coarse", type=float,
                         help="fine mode: coarse edge offset in samples "
                              "within the second")
    parser.add_argument("--fold-seconds", type=int, default=8)
    parser.add_argument("--format", choices=sorted(_LOADERS), default="c64",
                         help="fine mode: raw I/Q sample format "
                              "(default c64 = float32 I/Q interleaved, "
                              "as written by the pcmrecord capture path; "
                              "s16 = int16 LE interleaved)")
    parser.add_argument("--search-window-ms", type=float, default=6.0,
                         help="fine mode: BpskEdgeFineStage search_window_ms "
                              "(widen if 'no crossing in window')")
    parser.add_argument("--gate-ms", type=float, default=2.0,
                         help="fine mode: early-late cross-check gate width")
    args = parser.parse_args(argv)

    if args.mode == "sweep":
        run_sweep(args.input, args.sample_rate)
    else:
        if args.coarse is None:
            parser.error("--mode fine requires --coarse")
        run_fine(args.input, args.sample_rate, args.coarse,
                  args.fold_seconds, fmt=args.format,
                  search_window_ms=args.search_window_ms,
                  gate_ms=args.gate_ms)


if __name__ == "__main__":
    main()
