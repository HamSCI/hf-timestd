"""BinaryArchiveWriter — memmap-backed MinuteBuffer behaviour.

Locks in the np.memmap rewrite of MinuteBuffer.samples (2026-05-14):

  * Fill uses an np.memmap backed by a per-chunk ``.scratch`` file in
    archive_dir.  Anonymous heap stays near zero during the long fill.
  * On flush success, the memmap is released and the scratch file is
    unlinked — no stranded files in archive_dir.
  * On flush abandon (MAX_FLUSH_RETRIES), same cleanup.
  * Startup orphan cleanup also removes ``.scratch`` files left by a
    crashed prior run.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from hf_timestd.core.binary_archive_writer import (
    BinaryArchiveConfig,
    BinaryArchiveWriter,
    MinuteBuffer,
)


def _writer(tmp_path: Path, *, sample_rate: int = 100,
            file_duration_sec: int = 2, compression: str = 'none'
            ) -> BinaryArchiveWriter:
    """Minimal writer with small numbers — fast tests."""
    cfg = BinaryArchiveConfig(
        channel_name="testchan",
        frequency_hz=10_000_000.0,
        sample_rate=sample_rate,
        output_dir=tmp_path / "raw_buffer",
        file_duration_sec=file_duration_sec,
        compression=compression,
        use_tiered_storage=False,
    )
    w = BinaryArchiveWriter(cfg)
    # Inject the GPS_TIME mapping (writer refuses to flush without it).
    w._gps_time_unix = 1_000_000.0  # arbitrary epoch
    w._gps_time_ns_raw = int(w._gps_time_unix * 1e9)
    w._rtp_timesnap = 0
    return w


def _drive_one_chunk(w: BinaryArchiveWriter, *, rtp_start: int = 0) -> tuple[MinuteBuffer, int]:
    """Start a fresh buffer, write enough samples to fill one chunk,
    return the buffer (before flush) plus chunk_boundary."""
    chunk_boundary = (int(w._gps_time_unix) // w.file_duration_sec) * w.file_duration_sec
    buf = w._start_new_minute(float(chunk_boundary), rtp_start)
    samples = (np.arange(w.samples_per_chunk) + 1).astype(np.complex64)
    buf.samples[:] = samples
    buf.write_pos = w.samples_per_chunk
    return buf, chunk_boundary


# ---- memmap allocation -------------------------------------------------------


def test_minute_buffer_uses_memmap(tmp_path: Path):
    w = _writer(tmp_path)
    buf, _ = _drive_one_chunk(w)
    assert isinstance(buf.samples, np.memmap), \
        "MinuteBuffer.samples must be np.memmap, not heap np.zeros"
    assert buf.scratch_path is not None
    assert buf.scratch_path.exists()
    assert buf.scratch_path.suffix == ".scratch"
    # Channel name is in the scratch name so concurrent channels don't collide.
    assert "testchan" in buf.scratch_path.name


def test_scratch_file_size_matches_chunk_bytes(tmp_path: Path):
    w = _writer(tmp_path)
    buf, _ = _drive_one_chunk(w)
    expected = w.samples_per_chunk * 8  # complex64 = 8 bytes
    assert buf.scratch_path.stat().st_size == expected


# ---- flush success cleans up -------------------------------------------------


def test_flush_success_unlinks_scratch_and_releases_memmap(tmp_path: Path):
    w = _writer(tmp_path)
    buf, boundary = _drive_one_chunk(w)
    scratch = buf.scratch_path

    ok = w._flush_minute(buf)
    assert ok is True
    assert not scratch.exists(), "scratch file must be unlinked on success"
    assert buf.scratch_path is None
    assert buf.samples is None  # memmap reference dropped


def test_flush_writes_final_bin_with_correct_content(tmp_path: Path):
    w = _writer(tmp_path, compression='none')
    buf, boundary = _drive_one_chunk(w)
    expected = (np.arange(w.samples_per_chunk) + 1).astype(np.complex64)

    w._flush_minute(buf)

    # Find the produced .bin and read back
    bins = list(w.archive_dir.rglob(f"{boundary}.bin"))
    assert len(bins) == 1, f"expected one .bin, got {bins}"
    actual = np.fromfile(bins[0], dtype=np.complex64)
    assert np.array_equal(actual, expected)


def test_no_scratch_or_tmp_files_after_clean_flush(tmp_path: Path):
    """Belt-and-suspenders: no scratch/.tmp residue anywhere under archive_dir."""
    w = _writer(tmp_path)
    buf, _ = _drive_one_chunk(w)
    w._flush_minute(buf)

    leftovers = list(w.archive_dir.rglob('*.scratch')) + \
                list(w.archive_dir.rglob('*.tmp'))
    assert leftovers == [], f"stranded files: {leftovers}"


# ---- flush failure / abandon cleans up ---------------------------------------


def test_abandon_after_max_retries_unlinks_scratch(tmp_path: Path):
    """After MAX_FLUSH_RETRIES the buffer is dropped — scratch must go too."""
    w = _writer(tmp_path)
    buf, _ = _drive_one_chunk(w)
    scratch = buf.scratch_path

    # Force every _flush_minute attempt to fail.
    with patch.object(BinaryArchiveWriter, '_flush_minute', return_value=False):
        for _ in range(w.MAX_FLUSH_RETRIES):
            done = w._try_flush(buf)
        # Last call returned True (abandon path).
        assert done is True
    assert buf.flush_attempts == w.MAX_FLUSH_RETRIES
    assert not scratch.exists(), \
        "scratch file must be unlinked on MAX_FLUSH_RETRIES abandon"
    assert buf.scratch_path is None


# ---- crash-recovery: startup picks up stale scratch files --------------------


def test_orphan_cleanup_removes_scratch_files(tmp_path: Path):
    archive = tmp_path / "raw_buffer" / "testchan"
    archive.mkdir(parents=True)
    stale_scratch = archive / "12345.testchan.scratch"
    stale_tmp = archive / "12345.bin.tmp"
    stale_scratch.write_bytes(b"\x00" * 1024)
    stale_tmp.write_bytes(b"\x00" * 1024)

    _writer(tmp_path)  # constructor calls _cleanup_orphaned_tmp_files()

    assert not stale_scratch.exists()
    assert not stale_tmp.exists()


# ---- fallback when memmap allocation fails -----------------------------------


def test_memmap_failure_falls_back_to_heap_with_warning(tmp_path: Path, caplog):
    w = _writer(tmp_path)
    with patch('hf_timestd.core.binary_archive_writer.np.memmap',
               side_effect=OSError("simulated")):
        with caplog.at_level('ERROR'):
            chunk_boundary = int(w._gps_time_unix) // w.file_duration_sec * w.file_duration_sec
            buf = w._start_new_minute(float(chunk_boundary), 0)

    # Fallback path: np.zeros + scratch_path=None.  Buffer must still
    # be writable so the recorder degrades gracefully instead of
    # crashing.
    assert isinstance(buf.samples, np.ndarray)
    assert not isinstance(buf.samples, np.memmap)
    assert buf.scratch_path is None
    assert any("memmap scratch alloc failed" in r.message
               for r in caplog.records)
    # The fallback buffer is still usable.
    buf.samples[0] = 1.0 + 2.0j
    assert buf.samples[0] == 1.0 + 2.0j
