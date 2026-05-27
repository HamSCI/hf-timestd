#!/usr/bin/env python3
"""Live WWVB demod validation tap — subscribes to the radiod WWVB_60 channel,
accumulates IQ, periodically runs `wwvb_demod.decode_iq`, and prints the
decoded frames to stdout.

This is a **development / validation tool**, not a service.  It is the
fastest answer to "does our WWVB demod chain work on real signal?"
without writing any IQ to disk.  Mirrors the eventual in-process
metrology consumer's input plumbing (dedicated `RadiodStream`, in-process
sample callback, rolling IQ buffer) but discards the output instead of
writing L2 broadcast_measurements rows.

Architecture matches the T6 (BPSK PPS) pattern in `_start_t6_stream()`:
dedicated socket / dedicated reader thread / in-process consumer.

For the full architectural picture, the output-line field reference,
and how to use this tool for long-term diurnal reception monitoring at
your site (not just decode validation), see ``docs/WWVB-INTEGRATION.md``.

Usage:

    .venv/bin/python scripts/wwvb_live_tap.py \\
        --radiod bee1-status.local \\
        --window-s 90 \\
        --decode-interval-s 30

Press Ctrl-C to stop.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import logging
import signal
import sys
import threading
from collections import deque
from typing import Deque, Optional

import numpy as np


UTC = _dt.timezone.utc


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--radiod", default="bee1-status.local",
        help="mDNS status hostname of the radiod instance (default: %(default)s)",
    )
    p.add_argument(
        "--frequency-hz", type=int, default=60_000,
        help="WWVB carrier frequency in Hz (default: %(default)s)",
    )
    p.add_argument(
        "--sample-rate", type=int, default=24_000,
        help="IQ sample rate in samples/sec (default: %(default)s)",
    )
    p.add_argument(
        "--window-s", type=float, default=90.0,
        help="rolling IQ buffer length in seconds — must exceed 60 so each "
        "decode pass has at least one full minute frame (default: %(default)s)",
    )
    p.add_argument(
        "--decode-interval-s", type=float, default=30.0,
        help="seconds between decode attempts (default: %(default)s)",
    )
    p.add_argument(
        "--min-buffer-s", type=float, default=65.0,
        help="minimum buffered seconds before the first decode attempt "
        "(default: %(default)s)",
    )
    p.add_argument(
        "-v", "--verbose", action="store_true",
        help="enable INFO logging from ka9q-python",
    )
    return p.parse_args()


def setup_logging(verbose: bool) -> None:
    level = logging.INFO if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def fmt_utc(dt: _dt.datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def main() -> int:
    args = parse_args()
    setup_logging(args.verbose)

    # Local imports so --help works without a populated ka9q-python install.
    from hf_timestd.core.wwvb_demod import decode_iq
    from ka9q import Encoding, RadiodControl, RadiodStream

    print(f"WWVB live tap — connecting to {args.radiod} ...")
    control = RadiodControl(args.radiod)
    try:
        channel_info = control.ensure_channel(
            frequency_hz=args.frequency_hz,
            preset="iq",
            sample_rate=args.sample_rate,
            encoding=Encoding.F32,
            agc_enable=0,
            gain=0.0,
            timeout=15.0,
        )
    except Exception as exc:
        print(f"FAIL: could not subscribe to channel: {exc}", file=sys.stderr)
        return 1

    print(
        f"  subscribed: SSRC={getattr(channel_info, 'ssrc', '?')} "
        f"freq={args.frequency_hz} Hz sr={args.sample_rate} "
        f"multicast={getattr(channel_info, 'multicast_address', '?')}"
    )

    # Rolling IQ buffer of (window_s * sample_rate) samples.  deque of
    # numpy arrays keeps the FIFO ops O(1); we concatenate only when
    # actually decoding.
    window_samples = int(args.window_s * args.sample_rate)
    min_decode_samples = int(args.min_buffer_s * args.sample_rate)
    buf_lock = threading.Lock()
    buf: Deque[np.ndarray] = deque()
    buf_samples = 0
    total_packets = 0
    last_rtp_ts: Optional[int] = None

    def on_samples(samples: np.ndarray, quality) -> None:
        nonlocal buf_samples, total_packets, last_rtp_ts
        with buf_lock:
            buf.append(samples)
            buf_samples += len(samples)
            total_packets += 1
            last_rtp_ts = quality.last_rtp_timestamp
            while buf_samples > window_samples and len(buf) > 1:
                buf_samples -= len(buf[0])
                buf.popleft()

    stream = RadiodStream(
        channel=channel_info,
        on_samples=on_samples,
        samples_per_packet=200,
        # Matches T6's tuning: 256 packets ≈ 3.2 s tolerance for jitter
        # before the resequencer fills in zeros (see _start_t6_stream
        # docstring).
        resequence_buffer_size=256,
    )
    stream.start()
    print(f"  stream started; will decode every {args.decode_interval_s:.0f}s "
          f"once buffer ≥ {args.min_buffer_s:.0f}s\n")

    stop_event = threading.Event()

    def handle_signal(signum, frame):  # noqa: ARG001
        print("\nstopping...", flush=True)
        stop_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        while not stop_event.is_set():
            if stop_event.wait(args.decode_interval_s):
                break
            now = _dt.datetime.now(UTC)
            with buf_lock:
                have = buf_samples
                if have < min_decode_samples:
                    print(
                        f"[{fmt_utc(now)}] buffering "
                        f"{have / args.sample_rate:5.1f}s / "
                        f"{args.min_buffer_s:.0f}s; pkts={total_packets} "
                        f"last_rtp={last_rtp_ts}"
                    )
                    continue
                iq = np.concatenate(list(buf))

            mean_amp = float(np.abs(iq).mean())
            try:
                result = decode_iq(iq, sample_rate=float(args.sample_rate))
            except Exception as exc:
                print(f"[{fmt_utc(now)}] decode error: {exc}")
                continue

            print(
                f"[{fmt_utc(now)}] iq={have / args.sample_rate:.1f}s "
                f"mean|iq|={mean_amp:.3e} "
                f"carrier_offset={result.carrier_offset_hz:+.3f} Hz "
                f"secs={result.seconds_detected} "
                f"bits={result.bits.size} "
                f"frames={len(result.frames)}"
            )
            for f in result.frames:
                diff_s = (f.frame.minute_of_frame - now).total_seconds()
                dst = f.frame.dst_state.name if f.frame.dst_state else "?"
                pol = "INV" if f.inverted_polarity else "OK "
                print(
                    f"  → minute={fmt_utc(f.frame.minute_of_frame)} "
                    f"DST={dst:14s} "
                    f"par_err={f.frame.parity_errors} "
                    f"sync_err={f.sync_errors} "
                    f"pol={pol} "
                    f"vs_wallclock={diff_s:+.0f}s"
                )
    finally:
        stream.stop()
        print("done.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
