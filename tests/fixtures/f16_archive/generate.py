"""Regenerate the f16_archive fixture with the real BinaryArchiveWriter.

    uv run python tests/fixtures/f16_archive/generate.py [OUT_DIR]

Writes three 60 s, 24 kHz chunks of one channel into one day directory:

    <OUT>/TESTCHAN/20260906/1788729000.*   F16 chunk       (complex-f16, scale 4096)
    <OUT>/TESTCHAN/20260906/1788729060.*   plain F32 chunk (complex64)
    <OUT>/TESTCHAN/20260906/1788729120.*   overflow fallback (complex64, f16_fallback)

The samples are low-amplitude noise (RMS about 3.5e-6, like a real station)
rounded to a 2^-20 grid.  The grid keeps zstd small and makes the F16 chunk
exact: every value times 4096 is a multiple of 2^-8 and fits an F16 mantissa.
The overflow chunk carries one component of 20.0 (20 * 4096 > 65504).
"""
import sys
from pathlib import Path

import numpy as np

from hf_timestd.core.binary_archive_writer import BinaryArchiveConfig, BinaryArchiveWriter

CHANNEL = "TESTCHAN"
RATE = 24000
FIRST = 1788729000          # 2026-09-06 21:10:00Z
OVERFLOW_SAMPLE = 3
OVERFLOW_VALUE = 20.0
GRID = 2.0 ** -20
RMS = 3.5e-6


def noise(seed, n=RATE * 60):
    rng = np.random.default_rng(seed)
    x = RMS / np.sqrt(2) * rng.standard_normal((n, 2))
    x = np.round(x / GRID) * GRID
    return (x[:, 0] + 1j * x[:, 1]).astype(np.complex64)


def chunks():
    """(minute boundary, archive_dtype, samples) for the three chunks."""
    over = noise(2)
    over[OVERFLOW_SAMPLE] = OVERFLOW_VALUE + 0j
    return [(FIRST, "f16", noise(0)), (FIRST + 60, "complex64", noise(1)),
            (FIRST + 120, "f16", over)]


def write(out_dir: Path) -> None:
    for boundary, dtype, samples in chunks():
        cfg = BinaryArchiveConfig(
            channel_name=CHANNEL, frequency_hz=10e6, sample_rate=RATE,
            output_dir=Path(out_dir), file_duration_sec=60, compression="zstd",
            archive_dtype=dtype, use_tiered_storage=False)
        w = BinaryArchiveWriter(cfg)
        w._gps_time_unix = float(boundary)
        w._gps_time_ns_raw = int(boundary * 1e9)
        w._rtp_timesnap = 0
        w._check_disk_space = lambda *a, **k: True
        buf = w._start_new_minute(float(boundary), 0)
        buf.samples[:len(samples)] = samples
        buf.write_pos = len(samples)
        assert w._flush_minute(buf)


if __name__ == "__main__":
    write(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent)
