"""The archive-drive archiver: copy closed raw IQ to the drive sigmond attached.

Design: sigmond docs/superpowers/specs/2026-11-12-archive-drive-design.md,
part 2.  sigmond owns the drive (`smd archive add` writes
/etc/sigmond/archive.toml and mounts it); this module owns the data.  It copies
every closed ``raw_buffer/<CH>/<YYYYMMDD>/<start>.bin.zst`` with its sidecar to
``<mount>/timestd/<station>/raw_buffer/...``, converting complex64 to F16 at
the codec's fixed scale, verifies every file before it counts, and keeps the
drive below its high-water mark by deleting the oldest whole day.

Recording never depends on it: it reads the SSD buffer, never writes there,
and a drive that errors ends the run.
"""
from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import socket
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

import numpy as np

from .core import iq_codec

logger = logging.getLogger("hf_timestd.archiver")

ARCHIVE_TOML = Path("/etc/sigmond/archive.toml")
DATA_ROOT = Path("/var/lib/timestd")
STATUS_PATH = Path("/var/lib/timestd/archive-status.json")
LOCK_PATH = Path("/run/timestd-archive.lock")
MIN_AGE_S = 120
ZSTD_LEVEL = 3                       # the recorder's (BinaryArchiveConfig.compression_level)
MAX_FAILURES = 3
_DAY_RE = re.compile(r"^(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])$")
_DRIVE_ERRNOS = {errno.EIO, errno.ENODEV, errno.ENXIO, errno.ENOSPC, errno.EROFS,
                 errno.ENOENT, errno.ESTALE}


class ArchiveIOError(Exception):
    """The drive failed (unplugged, I/O error, full): the run ends."""


class VerifyError(Exception):
    """A written file did not match its source; it was removed."""


# ---------------------------------------------------------------------------
# selecting
# ---------------------------------------------------------------------------

def closed_files(raw_root: Path, now: float, min_age_s: float = MIN_AGE_S
                 ) -> Iterator[Tuple[Path, Path]]:
    """(bin, sidecar) for every closed chunk, oldest day first, then channel,
    then start.  Closed: the sidecar exists and both files are unmodified for
    ``min_age_s``."""
    found: List[Tuple[str, str, str, Path, Path]] = []
    try:
        channels = sorted(p for p in raw_root.iterdir() if p.is_dir())
    except OSError:
        return
    for ch in channels:
        try:
            days = [d for d in ch.iterdir() if d.is_dir() and _DAY_RE.match(d.name)]
        except OSError:
            continue
        for d in days:
            try:
                bins = list(d.glob("*.bin.zst"))
            except OSError:
                continue
            for b in bins:
                j = b.with_name(b.name[:-len(".bin.zst")] + ".json")
                try:
                    if (now - b.stat().st_mtime < min_age_s
                            or now - j.stat().st_mtime < min_age_s):
                        continue
                except OSError:
                    continue          # no sidecar yet (or vanished): not closed
                found.append((d.name, ch.name, b.name, b, j))
    for _, _, _, b, j in sorted(found, key=lambda t: (t[0], t[1], t[2])):
        yield b, j


# ---------------------------------------------------------------------------
# verifying
# ---------------------------------------------------------------------------

def f16_within_rounding(src: np.ndarray, dec: np.ndarray) -> bool:
    """Every finite component of ``dec`` is within F16 rounding of ``src``,
    in scaled units: |d - s| <= 2**-10 * |s| + 2**-24.  NaN and infinity must
    sit at the same positions (and infinities share a sign)."""
    s = np.ascontiguousarray(src, dtype=np.complex64).view(np.float32).astype(np.float64)
    d = np.ascontiguousarray(dec, dtype=np.complex64).view(np.float32).astype(np.float64)
    if s.shape != d.shape:
        return False
    s *= iq_codec.F16_SCALE
    d *= iq_codec.F16_SCALE
    if not np.array_equal(np.isnan(s), np.isnan(d)):
        return False
    s_inf, d_inf = np.isinf(s), np.isinf(d)
    if not np.array_equal(s_inf, d_inf) or not np.array_equal(s[s_inf], d[d_inf]):
        return False
    fin = np.isfinite(s)
    return bool(np.all(np.abs(d[fin] - s[fin]) <= 2.0 ** -10 * np.abs(s[fin]) + 2.0 ** -24))


# ---------------------------------------------------------------------------
# copying one file
# ---------------------------------------------------------------------------

def _drive_error(exc: OSError) -> ArchiveIOError:
    return ArchiveIOError(f"{type(exc).__name__}: {exc}")


def _write_part(dst_dir: Path, name: str, data: bytes) -> Path:
    """Write ``.<name>.part`` in dst_dir and fsync it."""
    part = dst_dir / f".{name}.part"
    with open(part, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    return part


def _fsync_dir(d: Path) -> None:
    fd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _unlink_quiet(*paths: Path) -> None:
    for p in paths:
        try:
            p.unlink()
        except OSError:
            pass


def copy_one(src_bin: Path, src_json: Path, dst_dir: Path, archive_dtype: str) -> str:
    """Copy one chunk to ``dst_dir``.  Returns 'copied' (byte for byte),
    'converted' (complex64 -> F16), 'kept_f32' (F16 would overflow) or
    'skipped' (already there, or the source is unreadable).  Raises
    ArchiveIOError on a drive error and VerifyError on a mismatch; neither
    leaves a partial file behind."""
    import zstandard as zstd

    dst_bin, dst_json = dst_dir / src_bin.name, dst_dir / src_json.name
    try:
        if dst_bin.exists() and dst_json.exists():
            return "skipped"
    except OSError as exc:
        raise _drive_error(exc)
    try:
        meta = json.loads(src_json.read_text())
        if not isinstance(meta, dict):
            raise ValueError("sidecar is not an object")
        blob = src_bin.read_bytes()
    except (OSError, ValueError) as exc:
        logger.warning("archiver: skipping %s: %s", src_bin, exc)
        return "skipped"
    src_dtype = meta.get("dtype", iq_codec.DTYPE_F32)

    outcome, out_blob, out_meta, samples = "copied", blob, meta, None
    if archive_dtype == "f16" and src_dtype == iq_codec.DTYPE_F32:
        try:
            raw = zstd.ZstdDecompressor().decompress(blob)
            samples = iq_codec.decode_iq(raw, iq_codec.DTYPE_F32, 1.0)
        except (zstd.ZstdError, ValueError) as exc:
            logger.warning("archiver: skipping %s: cannot decode: %s", src_bin, exc)
            return "skipped"
        enc = iq_codec.encode_f16(samples)
        if enc is None:
            outcome = "kept_f32"
        else:
            outcome = "converted"
            out_blob = zstd.ZstdCompressor(level=ZSTD_LEVEL).compress(enc)
            out_meta = {k: v for k, v in meta.items() if k != "f16_check"}
            out_meta["dtype"] = iq_codec.DTYPE_F16
            out_meta["scale"] = int(iq_codec.F16_SCALE)
            out_meta["converted_from"] = iq_codec.DTYPE_F32

    part_bin = part_json = None
    try:
        dst_dir.mkdir(parents=True, exist_ok=True)
        part_bin = _write_part(dst_dir, src_bin.name, out_blob)
        written = part_bin.read_bytes()
        if outcome == "converted":
            dec = iq_codec.decode_iq(zstd.ZstdDecompressor().decompress(written),
                                     iq_codec.DTYPE_F16, iq_codec.F16_SCALE)
            if dec.size != samples.size or not f16_within_rounding(samples, dec):
                _unlink_quiet(part_bin)
                raise VerifyError(f"{src_bin}: F16 copy is not within rounding of the source")
        elif hashlib.sha256(written).digest() != hashlib.sha256(blob).digest():
            _unlink_quiet(part_bin)
            raise VerifyError(f"{src_bin}: copy differs from the source")
        part_json = _write_part(dst_dir, src_json.name,
                                (json.dumps(out_meta, indent=2) + "\n").encode())
        os.replace(part_bin, dst_bin)
        os.replace(part_json, dst_json)            # sidecar last: it marks the file archived
        _fsync_dir(dst_dir)
    except VerifyError:
        raise
    except OSError as exc:
        _unlink_quiet(*(p for p in (part_bin, part_json) if p is not None))
        if dst_bin.exists() and not dst_json.exists():
            _unlink_quiet(dst_bin)
        raise _drive_error(exc)
    return outcome
