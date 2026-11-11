#!/usr/bin/env python3
# scripts/replay-registration.py
"""Replay a raw chunk through the RegistrationAcquirer with a label shift.

    scripts/replay-registration.py CHUNK.bin.zst SIDECAR.json [--shift-ms X ...]

DEVBOX ONLY.  This is the sweep that loaded two station recorders to their
watchdog on 2026-09-06; scp the chunk here and run it here.
"""

import argparse
import dataclasses
import json
import sys
import tempfile
from pathlib import Path

import numpy as np
import zstandard

from hf_timestd.core import registration_acquirer as ra
from hf_timestd.core.buffer_timing import resolve_buffer_timing
from hf_timestd.core.iq_codec import decode_iq
from hf_timestd.core.metrology_engine import MetrologyEngine
from hf_timestd.core.registration_acquirer import RegistrationAcquirer
from hf_timestd.core.tick_edge_detector import TickEdgeDetector


def _engine(meta: dict, sr: int) -> MetrologyEngine:
    """Build the engine the way metrology_service.py does (same helper as
    tests/unit/test_replay_nd_acquisition.py -- kept local here on purpose,
    nothing shared between the operator tool and the test)."""
    st = meta["station"]
    out_dir = Path(tempfile.mkdtemp(prefix="hf_timestd_replay_"))
    return MetrologyEngine(
        raw_buffer_dir=Path("/dev/null"),
        output_dir=out_dir,
        channel_name=meta["channel_name"],
        frequency_hz=float(meta["frequency_hz"]),
        receiver_grid=st.get("grid_square", ""),
        sample_rate=sr,
        precise_lat=float(st["latitude"]),
        precise_lon=float(st["longitude"]),
        enable_physics_products=False,
    )


def _dump_minute_diagnostics(acq: RegistrationAcquirer, delays: dict, sr: int) -> None:
    """Replicate ``RegistrationAcquirer._try_acquire``'s internals and
    print the peak/hypothesis dump an operator needs to see WHY a station
    chunk did or didn't acquire: fold peaks per band (position, SNR,
    width), the ``arbitrate_bands`` survivors, every ``Hypothesis``
    (correction, support, assignments, unambiguous), the minute-marker
    result, and the resulting integer-second correction.  Read-only --
    does not touch ``acq``'s state."""
    if not acq._buf:
        return
    a0, s0, rtp0, _ = acq._buf[0]
    pieces = []
    for audio, s_lbl, rtp, _mu in acq._buf:
        want = rtp - rtp0
        have = sum(len(p) for p in pieces)
        if want > have:
            pieces.append(np.zeros(want - have))
        elif want < have:
            audio = audio[have - want :]
        pieces.append(audio)
    audio_all = np.concatenate(pieces)
    n_sec = min(180, len(audio_all) // sr)
    by_band = {}
    for band in ra.TONE_BANDS_HZ:
        profile, rows = ra.fold_tick_train(audio_all, sr, s0, band, n_sec)
        if rows == 0:
            continue
        peaks = ra.find_fold_peaks(profile, sr, band)
        by_band[band] = peaks
        print(f"    band {band}: {rows} rows folded, {len(peaks)} peaks:")
        for p in peaks:
            print(
                f"      pos={p.position_s * 1000:8.3f} ms  snr={p.snr_db:6.2f} dB  "
                f"width={p.width_ms:5.2f} ms"
            )
    best = [
        p
        for p in ra.arbitrate_bands(by_band)
        if any(ra.BAND_OF_STATION.get(s) == p.band for s in delays)
    ]
    print(
        "    arbitrate_bands survivors:",
        [(p.band, round(p.position_s * 1000, 3), round(p.snr_db, 2)) for p in best],
    )
    hyps = ra.fit_template(best, delays)
    print(f"    hypotheses ({len(hyps)}):")
    for h in hyps:
        print(
            f"      corr={h.correction_s * 1000:+9.3f} ms  sigma={h.sigma_ms:6.3f}  "
            f"support={h.support}  unambiguous={h.unambiguous}  "
            f"assignments={[(x[0], round(x[2] * 1000, 2), round(x[3], 1)) for x in h.assignments]}"
        )
    winners = [h for h in hyps if h.unambiguous]
    if winners:
        h = winners[0]
        st0 = h.assignments[0][0]
        a_last, _s_last, rtp_last, minute_last = acq._buf[-1]
        s_last_in_frame0 = s0 + (rtp_last - rtp0) / sr
        mk = ra.locate_minute_marker(
            a_last, sr, s_last_in_frame0, ra.BAND_OF_STATION[st0], minute_last
        )
        print(f"    winning hyp station: {st0}  marker result: {mk}")
        k_int = 0
        if mk is not None:
            k_int = ra.integer_second_correction(mk[0], delays[st0], h.correction_s)
        corr = h.correction_s + k_int
        print(f"    k_int={k_int:+d}  total correction={corr * 1000:+.3f} ms")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("chunk", type=Path)
    ap.add_argument("sidecar", type=Path)
    ap.add_argument("--shift-ms", type=float, nargs="*", default=[0.0])
    ap.add_argument("--channel", default="SHARED_10000")
    ap.add_argument(
        "--verbose",
        action="store_true",
        help="print the per-minute fold-peak/hypothesis/marker dump (how "
        "an operator diagnoses a chunk that doesn't acquire cleanly)",
    )
    args = ap.parse_args()
    meta = json.loads(args.sidecar.read_text())
    sr = int(meta["sample_rate"])
    iq = decode_iq(
        zstandard.ZstdDecompressor().decompress(
            args.chunk.read_bytes(), max_output_size=1 << 31
        ),
        meta.get("dtype", "complex64"),
        meta.get("scale", 1.0),
    )
    bt_true = resolve_buffer_timing(meta, sample_rate=sr)
    det = TickEdgeDetector(sample_rate=sr)
    print(
        f"{'shift_ms':>9} {'minutes':>7} {'recovered_ms':>13} {'sigma_ms':>9} "
        f"{'fine_sigma1':>11} {'fine_err_ms':>11}"
    )
    for shift in args.shift_ms:
        eng = _engine(meta, sr)
        acq = RegistrationAcquirer(args.channel, sr)
        got, k, a = None, -1, 0
        if args.verbose:
            print(f"=== shift={shift:+.1f} ms ===")
        for k in range(min(3, len(iq) // (60 * sr) - 1)):
            a = k * 60 * sr
            seg = iq[a : a + 62 * sr]
            minute_utc = int(meta["minute_boundary"]) + 60 * k
            label = dataclasses.replace(
                bt_true, sample0_utc=bt_true.sample0_utc + a / sr + shift / 1000.0
            )
            audio = eng.prepare_audio(seg)
            delays = eng.expected_delays_s(label.sample0_utc, minute_utc)
            got = acq.offer_minute(
                audio,
                label,
                int(meta["start_rtp_timestamp"]) + a,
                minute_utc,
                delays,
                "ep-replay",
            )
            if args.verbose:
                print(
                    f"  minute {k}: delays(ms)={ {s: round(d * 1000, 3) for s, d in delays.items()} }"
                )
                _dump_minute_diagnostics(acq, delays, sr)
                print(
                    f"  state={acq.state}  got={'ACQUIRED' if got is not None else None}"
                )
            if got is not None:
                break
        if got is None:
            print(f"{shift:>9.1f} {k + 1:>7} {'BOOTSTRAP':>13}")
            continue
        s0 = got.sample0_utc_for(int(meta["start_rtp_timestamp"]) + a)
        rec = (s0 - (bt_true.sample0_utc + a / sr)) * 1000.0
        bt_acq = dataclasses.replace(
            bt_true,
            sample0_utc=s0,
            origin_source="acquired",
            origin_sigma_ms=got.sigma_ms,
        )
        res = det.detect_edges(
            audio_signal=audio,
            station="WWV",
            minute_number=minute_utc,
            buffer_timing=bt_acq,
            expected_delay_sec=delays.get("WWV", 0.0),
            is_dedicated_channel=False,
            iq_samples=None,
        )
        fs = (
            f"{res.sigma_single_ms:11.2f} {res.ensemble_timing_error_ms:11.2f}"
            if res
            else f"{'none':>11} {'':>11}"
        )
        print(f"{shift:>9.1f} {k + 1:>7} {rec:>13.2f} {got.sigma_ms:>9.2f} {fs}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
