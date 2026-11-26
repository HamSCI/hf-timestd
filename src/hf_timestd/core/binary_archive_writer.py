#!/usr/bin/env python3
"""
Binary Archive Writer - Simple, robust raw IQ storage

Writes raw complex64 binary files with JSON metadata sidecars.
Designed for maximum reliability - append-only, no HDF5 complexity.

Architecture:
- One binary file per minute per channel
- JSON sidecar with timestamps and metadata
- Memory-mappable for zero-copy Phase 2 reading
- Optional async compression of completed minutes

File structure:
    raw_buffer/{CHANNEL}/YYYYMMDD/
        1765031100.bin      # Raw complex64 samples
        1765031100.json     # Metadata sidecar
        1765031040.bin.zst  # Compressed older minute (optional)

Timing provenance (docs/design/MEASUREMENT_MODEL.md §7.2): the sidecar's
start time descends from radiod's GPS_TIME/RTP_TIMESNAP pair, so its
origin is ``sysclock`` in the model's word.  The registration that
corrects it lives in the anchor ledger and the Offset Judge, not here.
"""

import errno
import json
import logging
import numpy as np
import os
import queue
import shutil
import threading
import time
from collections import deque as _deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass, field

from . import f16_check
from .chunk_segments import (SegmentJoiner, chunk_stem, join_metadata, parse_chunk_name,
                             runs_from_mask)
from .iq_codec import (DTYPE_F16, DTYPE_F32, F16_SCALE, F16Overflow, bytes_per_sample,
                       decode_iq, iter_f16_blocks)

logger = logging.getLogger(__name__)

#: MEASUREMENT_MODEL §3 — an adopted radiod pair this far from the mapping in
#: force means the counter was re-based (a restart renumbers by seconds).
COUNTER_EPOCH_STEP_S = 0.5

# Constants
BYTES_PER_SAMPLE = 8  # complex64 = 2 x float32


@dataclass
class TimingSnapshot:
    """
    A GPS_TIME/RTP_TIMESNAP pair from radiod status packets.
    
    These snapshots enable post-hoc RTP-to-UTC conversion using radiod's
    authoritative timing (when GPS+PPS disciplined, L4/L5 accuracy).
    
    Capture frequency: ~2 Hz (radiod's default status update rate)
    Metrological justification:
    - In L4/L5: Documents stable GPS-disciplined mapping for verification
    - In L3/L2/L1: Captures NTP slew/step events for post-hoc correction
    
    Attributes:
        gps_time_ns: radiod's GPS_TIME (ns since GPS epoch, from CLOCK_REALTIME)
        rtp_timesnap: RTP timestamp at the moment GPS_TIME was sampled
        local_receipt_time: When hf-timestd received this status packet (Unix time)
    """
    gps_time_ns: int
    rtp_timesnap: int  
    local_receipt_time: float
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            'gps_time_ns': self.gps_time_ns,
            'rtp_timesnap': self.rtp_timesnap,
            'local_receipt_time': self.local_receipt_time
        }


ARCHIVE_DTYPES = ('complex64', 'f16')
# F16 passed its B4/ND trial (2026-11-15) and became the fleet default on
# 2026-11-18: half the raw bytes, every reader still gets complex64.
DEFAULT_ARCHIVE_DTYPE = 'f16'


def validate_archive_dtype(value: Any) -> str:
    """Return ``value`` if it names a supported archive dtype, else raise."""
    if value not in ARCHIVE_DTYPES:
        raise ValueError(
            f"[recorder] archive_dtype = {value!r} is not supported; "
            f"use one of {', '.join(repr(v) for v in ARCHIVE_DTYPES)}"
        )
    return value


def f16_compression_warning(archive_dtype: str, compression: Optional[str]) -> Optional[str]:
    """The startup warning for archive_dtype = "f16" without zstd, else None.

    F16 applies only to .bin.zst chunks; with any other compression every
    chunk stays complex64, which an operator who asked for F16 should hear.
    """
    comp = (compression or 'none').lower()
    if archive_dtype != 'f16' or comp == 'zstd':
        return None
    return (f'[recorder] archive_dtype = "f16" applies only to zstd chunks; '
            f'compression = "{comp}" stores every chunk as complex64')


@dataclass
class BinaryArchiveConfig:
    """Configuration for binary archive writer."""
    channel_name: str
    frequency_hz: float
    sample_rate: int = 20000
    output_dir: Path = Path('/tmp/timestd-test/raw_buffer')
    station_config: Dict[str, Any] = field(default_factory=dict)
    compress_completed: bool = False  # Async compression of old minutes
    compression: str = 'none'  # 'none', 'zstd', or 'lz4' - reduces disk I/O by ~2-3x
    compression_level: int = 3  # zstd: 1-22 (3 = good balance), lz4: 1-12
    # Root for the open chunk's memmap (<scratch_root>/<channel_dir>/<YYYYMMDD>/),
    # e.g. a RAM disk.  None puts the scratch beside the chunk.  Finished
    # chunks always land under output_dir.
    scratch_root: Optional[Path] = None
    radiod_snr_db: Optional[float] = None  # SNR from radiod (updated periodically)
    # |old-mapping vs new-snapshot| beyond this (s) ⇒ radiod stepped its
    # RTP↔GPS offset: flush + adopt the new mapping (re-anchor).  Matches
    # the fleet-wide trigger in ka9q-python ChannelInfo (sigmond, 2026-06-07).
    anchor_step_threshold_sec: float = 0.25
    # ⛔ Place chunk boundaries on the detected TS-1 pulse instead of on
    # radiod's GPS_TIME/RTP_TIMESNAP arithmetic.  DEFAULT OFF, and it must
    # stay off until B4's shadow measurement says the anchor error is worth
    # correcting — two boxes leave for McMurdo and none should meet this
    # first.  Off still computes the pulse position and records the
    # difference, which is the whole point of shipping it off.
    # See docs/design/ARCHIVE_BOUNDARY_ON_THE_EDGE.md §5.
    boundary_on_pulse: bool = False

    # Archive sample format.  "f16" (default): zstd chunks store interleaved
    # float16 at the fixed iq_codec.F16_SCALE, half the bytes; other
    # compressions ignore it.  "complex64" keeps F32.  Set by [recorder]
    # archive_dtype; see docs/superpowers/specs/2026-11-11-f16-archive-design.md.
    archive_dtype: str = DEFAULT_ARCHIVE_DTYPE
    # While F16 runs, measure the rounding's fine-detail cost on each chunk's
    # first minute and record it in the sidecar (one Welch PSD per chunk,
    # in the flush worker).
    f16_check: bool = True

    # File duration: how many seconds of IQ data per file.
    # 600s (10 minutes) reduces filesystem overhead 10x vs 60s (1 minute),
    # improves compression ratios, and reduces GRAPE pipeline I/O.
    # Downstream consumers (GRAPE raw_reader) handle both cadences transparently.
    file_duration_sec: int = 600  # 10 minutes per file (was 60)

    # Pre-roll: Start buffer before minute boundary to capture full minute markers.
    # The minute marker tone starts at second 0, so we need samples BEFORE the
    # minute boundary to capture the full tone onset. NTP is used as a hint for
    # where to look, not as ground truth - the bootstrap establishes timing from
    # the tones themselves.
    pre_roll_seconds: float = 2.0  # Start buffer 2s before minute boundary

    def __post_init__(self):
        validate_archive_dtype(self.archive_dtype)


@dataclass
class MinuteBuffer:
    """Buffer for accumulating one chunk of samples.

    ``samples`` is an ``np.memmap`` backed by ``scratch_path`` on disk
    rather than a heap-allocated ``np.zeros`` array.  Writes go through
    kernel page cache + writeback, so the daemon's anonymous RSS stays
    near zero during the long fill window (~10 min at default
    ``file_duration_sec``) instead of the prior ~115 MB-per-channel
    sawtooth that looked like a leak in ``ps``/``top``.  At flush time
    the same memmap is read back to feed compression / direct write,
    then closed; the scratch file is unlinked after successful flush
    (or on abandon after ``MAX_FLUSH_RETRIES``).
    """
    minute_boundary: int  # Unix timestamp of minute start
    samples: np.ndarray   # np.memmap backed by scratch_path
    write_pos: int = 0    # Current write position
    gap_count: int = 0    # Number of gaps in this minute
    gap_samples: int = 0  # Total gap samples
    start_rtp: Optional[int] = None
    start_system_time: Optional[float] = None
    timing_snapshots: List[TimingSnapshot] = field(default_factory=list)  # Snapshots for this minute
    flush_attempts: int = 0  # Number of failed flush attempts
    scratch_path: Optional[Path] = None  # backing file for the memmap; unlinked on success
    # Offset Judge provenance captured at chunk start (spec §8 "timing"
    # block).  None when no judge verdict was applied — the sidecar then
    # omits the block entirely (legacy, raw-radiod-mapping chunk).
    judge_timing: Optional[Dict[str, Any]] = None
    # Task 14b: the (gps_time_ns, rtp_timesnap) pair this chunk's LABELS
    # were placed with, frozen at chunk start.  Set only while a
    # label-plane anchor is in force, in which case it is the ANCHOR's
    # plane restated at radiod's snap counter; None means the sidecar
    # carries radiod's raw pair as before.
    label_pair: Optional[tuple] = None
    # First chunk after an anchor round trip put back the mapping a pair
    # step had replaced (StreamRecorderV2 revalidation).
    anchor_restored: bool = False
    # Sample positions actually written, as sorted, merged [start, end)
    # runs.  write_pos only records the highest position, so a hole
    # inside the chunk used to read as complete.  Batches arrive almost
    # always in order, so this stays one or a few runs long.
    covered_runs: List[List[int]] = field(default_factory=list)
    # Samples the writer refused for this chunk, by cause: labelled onto
    # positions already written (kept the earlier ones), or labelled before
    # the chunk start (an earlier chunk is never reopened).
    overlap_samples: int = 0
    dropped_before_start: int = 0
    # "first_sample" (the resequencer's delivered_rtp_start), "last_packet"
    # (the pre-3.21 ka9q-python fallback), or "mixed".
    batch_label: Optional[str] = None

    # Positions holding ka9q-python's zero gap fill, as [start, end) runs in
    # chunk sample positions.  Every fill position is also in covered_runs.
    fill_runs: List[List[int]] = field(default_factory=list)

    def note_fill(self, start: int, mask: np.ndarray) -> int:
        """Record the True runs of ``mask``, placed at ``start``, as fill.

        A run that continues the last one merges into it.  Returns the
        number of positions added."""
        added = 0
        for a, b in runs_from_mask(mask, start):
            if self.fill_runs and self.fill_runs[-1][1] == a:
                self.fill_runs[-1][1] = b
            else:
                self.fill_runs.append([a, b])
            added += b - a
        return added

    def free_spans(self, start: int, end: int) -> List[tuple]:
        """The parts of [start, end) not yet written, in order."""
        runs = self.covered_runs
        if not runs or start >= runs[-1][1]:
            return [(start, end)] if end > start else []
        spans = []
        pos = start
        for a, b in runs:
            if b <= pos:
                continue
            if a >= end:
                break
            if a > pos:
                spans.append((pos, a))
            pos = max(pos, b)
            if pos >= end:
                break
        if pos < end:
            spans.append((pos, end))
        return spans

    def note_written(self, start: int, end: int) -> None:
        """Record that positions [start, end) now hold samples."""
        if end <= start:
            return
        runs = self.covered_runs
        if not runs or start > runs[-1][1]:
            runs.append([start, end])          # the usual case: in order
            return
        if start >= runs[-1][0]:
            runs[-1][1] = max(runs[-1][1], end)
            return
        merged = []
        for a, b in sorted(runs + [[start, end]]):
            if merged and a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        self.covered_runs = merged

    @property
    def samples_covered(self) -> int:
        return sum(b - a for a, b in self.covered_runs)

    @property
    def hole_count(self) -> int:
        """Unwritten runs inside [0, write_pos): lead hole plus interior holes."""
        runs = self.covered_runs
        if not runs:
            return 1 if self.write_pos > 0 else 0
        return len(runs) - 1 + (1 if runs[0][0] > 0 else 0)

    @property
    def is_complete(self) -> bool:
        return self.write_pos >= len(self.samples)
    
    @property
    def samples_remaining(self) -> int:
        return max(0, len(self.samples) - self.write_pos)


#: complex64 samples per block when a chunk streams into zstd (4 MiB).
_STREAM_BLOCK_SAMPLES = 1 << 19

#: One memory-heavy flush step at a time across every writer in the
#: process: a zstd compression or an f16_check.  Every channel's chunk
#: closes on the same boundary.  A threads=1 zstd context is ZSTDMT with
#: one worker and holds ~50-60 MB of job buffers whatever the input size;
#: the check holds ~50 MB at 24 kHz.  Six at once cost ~0.4-0.6 GB that one
#: at a time does not.  Compression is bulk work with minutes of slack.
_FLUSH_MEMORY_LOCK = threading.Lock()


_malloc_trim = None


def _release_freed_heap() -> None:
    """Hand freed heap back to the kernel (glibc ``malloc_trim``).  Best-effort.

    The f16_check's minute-long arrays (~23 MB each at 24 kHz) fall under
    glibc's dynamic mmap threshold once an earlier large free has raised it,
    so they come from the flush thread's own arena, and glibc keeps that
    memory resident after the free.  Six flush threads each kept ~50 MB
    between boundaries even with the checks run one at a time (measured:
    RssAnon 310 MB after a six-channel F16 flush, 86 MB before).  Trimming
    after each heavy step returns it.  A no-op off glibc.
    """
    global _malloc_trim
    if _malloc_trim is None:
        try:
            import ctypes
            _malloc_trim = ctypes.CDLL("libc.so.6").malloc_trim
        except (OSError, AttributeError):
            _malloc_trim = False
    if _malloc_trim:
        try:
            _malloc_trim(0)
        except Exception:  # noqa: BLE001 — never let a trim fail a flush
            pass


#: Nice increment for the archive flush workers, relative to the recorder.
#: +10 keeps them clearly behind capture and decode without risking the
#: starvation that SCHED_IDLE would (see _flush_worker_loop).
_FLUSH_WORKER_NICE = 10


def _renice_current_thread(nice: int) -> None:
    """Lower the calling THREAD's priority. Best-effort.

    Nice is per-task on Linux, so this affects only this worker, not the
    recorder's capture threads. Failure is not worth an exception in a
    daemon worker — the flush still works, just at the old priority — so
    it is logged once and swallowed.
    """
    try:
        os.setpriority(os.PRIO_PROCESS, 0, os.getpriority(os.PRIO_PROCESS, 0) + nice)
    except (OSError, PermissionError, AttributeError) as exc:
        logger.warning("could not renice flush worker (+%d): %r — "
                       "compression will run at the recorder's priority", nice, exc)


class BinaryArchiveWriter:
    """
    Simple binary archive writer for Phase 1 raw IQ data.
    
    Key features:
    - Append-only binary files (cannot fail like HDF5)
    - One file per minute (easy for Phase 2 to read)
    - Memory-mappable output
    - No complex library dependencies
    """

    # Maximum flush retries before abandoning a minute buffer.  At 1 retry
    # per minute-crossing (~60 s), 3 retries gives ~3 minutes to recover
    # from a transient I/O error (e.g. NFS stall, tmpfs pressure).
    MAX_FLUSH_RETRIES = 3

    def __init__(self, config: BinaryArchiveConfig,
                 offset_judge: Optional[Any] = None,
                 source_key: Optional[tuple] = None):
        self.config = config

        # Offset Judge wiring (docs/OFFSET-JUDGE-SPEC-2026-08-05.md).
        # When present, labels become radiod_mapping(rtp) + offset and
        # every sidecar carries the spec §8 "timing" provenance block.
        # When absent, behavior is exactly the pre-judge writer.
        # source_key = (status_stream, ssrc); usually late-bound via
        # set_offset_judge() because the SSRC is only known after
        # ensure_channel().
        self._offset_judge = offset_judge
        self._judge_source_key = source_key
        # MEASUREMENT_MODEL §3 — the counter epoch.  radiod renumbers samples
        # on restart; a registration carried across that errs by seconds.
        # A new epoch opens when an adopted pair disagrees with the mapping
        # in force by more than COUNTER_EPOCH_STEP_S.
        self._counter_epoch_id: Optional[str] = None
        self._counter_epoch_pair: Optional[tuple] = None   # (gps_time_ns, rtp_timesnap, sample_rate)
        # Task 14b: the label-plane anchor provider (the recorder's
        # `_label_anchor_state`), or None on a station with no label
        # plane.  See `_label_anchor` / `_label_correction_s`.
        self._label_anchor_provider = None
        # TIMING_PROVENANCE_MODEL §3.1 — the per-chunk timing block publishes
        # the registration in force.  Late-bound by the recorder.
        self._time_map_provider = None
        self._time_map_counter_space: Optional[str] = None
        
        from ..paths import channel_name_to_dir
        self.archive_dir = config.output_dir / channel_name_to_dir(config.channel_name)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.scratch_dir: Optional[Path] = None
        if config.scratch_root is not None:
            self.scratch_dir = Path(config.scratch_root) / channel_name_to_dir(config.channel_name)
            self.scratch_dir.mkdir(parents=True, exist_ok=True)
        
        # Buffer sizing — one file per file_duration_sec (default 600s = 10 min)
        self.file_duration_sec = config.file_duration_sec
        self.samples_per_chunk = int(config.sample_rate * self.file_duration_sec)
        # Keep samples_per_minute for per-minute metadata (unchanged)
        self.samples_per_minute = int(config.sample_rate * 60)
        
        # Current minute buffer
        self.current_buffer: Optional[MinuteBuffer] = None
        self._lock = threading.Lock()

        # Async flush worker.  _flush_minute does zstd compression +
        # fsync of a ~73 MB chunk; running it inline on the receive
        # thread blocked packet reads for 3-5 s at every 10-min
        # boundary, overflowed the kernel UDP buffer, and dropped
        # 0.4-1% of samples per channel per day across all 9
        # archive channels.  The receive thread now enqueues
        # completed buffers and returns immediately; this daemon
        # thread drains the queue off the hot path.  Bounded queue
        # gives the receive thread bounded latency on enqueue (no
        # blocking) — if the worker can't keep up we log + drop
        # rather than backpressure the network reader.
        self._flush_queue: queue.Queue = queue.Queue(maxsize=4)
        self._flush_stop = threading.Event()
        self._flush_thread = threading.Thread(
            target=self._flush_worker_loop,
            name=f"flush-{config.channel_name}",
            daemon=True,
        )
        self._flush_thread.start()
        
        # Statistics
        self.minutes_written = 0
        self.samples_written = 0
        self.total_gaps = 0
        self.write_errors = 0
        self.fill_samples = 0  # Zero gap-fill positions stored (sidecar fill_runs)
        self.stale_drops = 0  # Samples dropped by the split detector (BACKLOG verdicts)
        self.timing_drops = 0  # Samples dropped waiting for GPS_TIME
        self.anchor_fault_events = 0  # Split-detector ANCHOR FAULT classifications (data kept)

        # Split-detector observation window (spec §6): a short history of
        # (wallclock, lag, unwrapped_rtp) points sampled at most once per
        # second, spanning SPLIT_WINDOW_SECONDS.  From it we derive
        # d(lag)/dt and the arrival rate that separate pipeline BACKLOG
        # (shed load) from radiod ANCHOR FAULT (keep data — the judge's
        # offset already corrects the label).
        self._lag_window: 'deque' = _deque()
        self._lag_last_append: float = 0.0
        self._unwrapped_rtp: int = 0
        self._last_raw_rtp: Optional[int] = None
        self._last_split_log: float = 0.0

        # BPSK PPS chain-delay metadata (set externally by core_recorder).
        # As of 2026-05, archive wall_times are RAW RTP-derived values —
        # the chain_delay is reported in metadata but NOT applied to the
        # stored timestamps.  Downstream consumers apply chain_delay (if
        # they want UTC-aligned timing) using the value here.  Pre-2026-05
        # archives have these fields absent and an implied "applied=True"
        # convention (BPSK was applied via ka9q's rtp_to_wallclock).
        self._bpsk_chain_delay_ns: Optional[int] = None
        self._bpsk_chain_delay_applied: bool = False

        # Most recent TS-1 pulse, in THIS channel's counter space, set
        # externally by core_recorder.  None on a station without a TS-1, and
        # before T6 acquires — in which case the boundary stays on the anchor
        # whatever the flag says.
        self._pulse_edge_rtp: Optional[int] = None
        # The last placement decision, for the sidecar's timing block.
        self._last_boundary_placement = None
        
        # Time reference - GPS_TIME/RTP_TIMESNAP from radiod
        # In RTP mode, the GPSDO-disciplined RTP clock IS the timing authority.
        # GPS_TIME/RTP_TIMESNAP gives us UTC directly:
        #   UTC = gps_time_unix + (rtp - rtp_timesnap) / sample_rate
        # Both RTP_TIMESNAP and packet RTP timestamps are in the same
        # counter space (input_sample_index / decimation). No pipeline
        # offset correction is needed — the timestamps are authoritative.
        self._gps_time_unix: Optional[float] = None  # GPS_TIME converted to Unix time
        self._gps_time_ns_raw: Optional[int] = None   # GPS_TIME in original ns (for metadata)
        self._rtp_timesnap: Optional[int] = None     # RTP timestamp at GPS_TIME
        self._timing_locked: bool = False

        # RTP↔GPS offset-step threshold (re-anchor trigger).  Lowered
        # from the historical 1.0 s to 0.25 s to match the fleet-wide
        # re-anchor trigger in ka9q-python's ChannelInfo.  Overridable
        # via config.anchor_step_threshold_sec or TIMESTD_ANCHOR_STEP_SEC.
        self._anchor_step_threshold_sec: float = float(
            os.environ.get(
                "TIMESTD_ANCHOR_STEP_SEC",
                getattr(config, "anchor_step_threshold_sec", 0.25),
            )
        )
        
        # RTP just past the newest sample handed to the writer (label +
        # batch length).  _restored_pair_age_s measures from it.
        self.last_rtp_timestamp: Optional[int] = None
        self.cumulative_samples: int = 0  # Total samples processed

        # Label faults (ka9q-python >= 3.28.0 re-bases the label after a
        # backward RTP jump or a sequence re-sync).  Every refused sample
        # lands in one of these counters; see _hold_batch and
        # _write_samples_inner.
        self._expected_label: Optional[int] = None
        self._hold_active = False
        self._held: "_deque" = _deque()   # (samples, rtp, gap_samples, label_source)
        self._held_n = 0
        self._last_label_fault_log = 0.0
        self._scratch_seq = 0
        self._host_time = time.time       # for a log line only; never decides
        self._pair_provider = None
        self._step_request: Optional[tuple] = None
        self.abandoned_chunks = 0
        self.abandoned_samples = 0
        self.label_discontinuities = 0
        self.unplaced_samples = 0
        self.overlap_samples = 0
        self.dropped_before_start = 0
        
        # Timing snapshot tracking for radiod GPS_TIME/RTP_TIMESNAP pairs
        # Deduplicated by rtp_timesnap to avoid storing duplicates
        self._last_rtp_timesnap: Optional[int] = None
        # Set by restore_timing_mapping; the next chunk started carries it.
        self._anchor_restored_pending = False
        self._pending_snapshots: List[TimingSnapshot] = []  # Snapshots waiting for minute assignment
        
        logger.info(f"BinaryArchiveWriter initialized for {config.channel_name}")
        logger.info(f"  Output: {self.archive_dir}")
        compression = config.compression.lower()
        stored = (DTYPE_F16 if config.archive_dtype == 'f16' and compression == 'zstd'
                  else DTYPE_F32)
        logger.info(f"  Format: {stored} I/Q, compression={compression} + JSON metadata")

        # Clean up orphaned .tmp files from a prior crash
        self._cleanup_orphaned_tmp_files()

    def _cleanup_orphaned_tmp_files(self) -> None:
        """Remove .tmp / .scratch files left behind by a prior crash.

        These are partial writes (``.tmp``) or memmap scratch files
        (``.scratch``) that were never atomically renamed or unlinked.
        Safe to delete at startup since no other writer instance should
        be active for this channel.
        """
        try:
            count = 0
            # Both .tmp (compressed-write half-success) and .scratch
            # (memmap backing files from a crashed fill) need cleaning.
            scans = [(self.archive_dir, '*.tmp'), (self.archive_dir, '*.scratch')]
            if self.scratch_dir is not None:
                scans.append((self.scratch_dir, '*.scratch'))
            for root, pattern in scans:
                for tmp_file in root.rglob(pattern):
                    try:
                        tmp_file.unlink()
                        count += 1
                    except OSError as e:
                        logger.warning(f"Failed to remove orphaned {pattern} file {tmp_file}: {e}")
            if count > 0:
                logger.info(f"{self.config.channel_name}: cleaned up {count} orphaned .tmp/.scratch file(s)")
        except Exception as e:
            logger.warning(f"Error scanning for orphaned .tmp files: {e}")

    def set_bpsk_metadata(self, chain_delay_ns: Optional[int],
                          applied: bool = False) -> None:
        """Record the current BPSK PPS chain_delay value for archive metadata.

        Called by core_recorder when the BPSK calibrator publishes a new
        accepted chain_delay (post wrap-rejection + disambiguation).  The
        value is written into the next sidecar JSON's
        ``bpsk_chain_delay_ns`` and ``bpsk_chain_delay_applied`` fields,
        so downstream consumers know whether the stored wall_times have
        the chain_delay correction applied (legacy: True; current: False).
        """
        self._bpsk_chain_delay_ns = chain_delay_ns
        self._bpsk_chain_delay_applied = bool(applied)

    def set_pulse_edge_rtp(self, edge_rtp: Optional[int]) -> None:
        """Record the latest TS-1 pulse position for boundary placement.

        ⛔ ``edge_rtp`` must already sit in THIS channel's counter space.  The
        TS-1 channel runs at 96 kHz while the archived WWV channels run at
        24 kHz, so the caller owns the conversion; passing the TS-1 channel's
        raw value here would place boundaries four times too far out.

        Passing None returns the writer to anchor placement, which is also
        what happens on a station with no TS-1 and before T6 acquires.
        """
        self._pulse_edge_rtp = None if edge_rtp is None else int(edge_rtp)

    def set_label_anchor_provider(self, provider) -> None:
        """Install the label-plane anchor provider (task 14b).

        ``provider() -> Optional[LabelAnchor]``, the SAME object the ring
        and authority.json §18 label from.  While it answers for this
        channel's counter domain, the sidecar's pair and this chunk's
        labels come from the anchor and radiod's host-stamped pair does
        not enter.  Silence restores the judged-pair behaviour exactly.
        """
        self._label_anchor_provider = provider

    def _label_anchor(self):
        """The anchor valid for THIS channel's counter domain, or None.

        An anchor is a ruler for one counter (``cross_channel_rtp.py``),
        and it can only restate its plane at radiod's snap once a snap
        exists — before timing lock there is nothing to restate.
        """
        provider = getattr(self, '_label_anchor_provider', None)
        if provider is None:
            return None
        if self._gps_time_unix is None or self._rtp_timesnap is None:
            return None
        try:
            label = provider()
        except Exception as exc:  # noqa: BLE001 — never disturb recording
            logger.debug(
                f"{self.config.channel_name}: label-anchor provider "
                f"failed: {exc}"
            )
            return None
        if label is None or not label.matches_rate(self.config.sample_rate):
            return None
        return label

    def _label_correction_s(self, verdict) -> float:
        """The correction added to radiod's raw mapping to get labels.

        With a label-plane anchor in force this is NOT the judge's
        verdict: it is the constant that carries radiod's plane onto the
        anchor's, measured at radiod's own snap counter by pure counter
        arithmetic.

            correction = anchor.utc(rtp_timesnap) − gps_unix(rtp_timesnap)

        Both terms name the SAME sample, so the difference is a plane
        offset and nothing else — no clock is read to compute it, and it
        is constant across the chunk because both planes advance on the
        one RTP ruler.  Audit G6: the ring is anchored on the same object
        (``StreamRecorderV2._anchor_ring_from_label_plane``), so
        ring-resolved and sidecar-resolved UTC agree exactly.

        Without an anchor: the judge's offset, exactly as before.
        """
        label = self._label_anchor()
        if label is not None:
            return (label.utc_ns_at(self._rtp_timesnap) / 1e9
                    - float(self._gps_time_unix))
        return (verdict.offset_ns / 1e9) if verdict is not None else 0.0

    def _label_pair(self, label) -> Optional[tuple]:
        """The sidecar pair for an anchor in force: its plane at radiod's
        snap, in radiod's GPS-epoch units so every existing reader
        resolves it unchanged (``buffer_timing.resolve_buffer_timing``).
        """
        if label is None or self._rtp_timesnap is None:
            return None
        from .buffer_timing import unix_ns_to_gps_time_ns
        return (
            unix_ns_to_gps_time_ns(int(label.utc_ns_at(self._rtp_timesnap))),
            int(self._rtp_timesnap),
        )

    def set_offset_judge(self, offset_judge: Any, source_key: tuple) -> None:
        """Late-bind the Offset Judge + per-source key.

        Called by the recorder after ensure_channel() has produced the
        SSRC (the source_key = (status_stream, ssrc) can't exist before
        that).  If a radiod pair was already adopted, register it with
        the judge immediately so measurement starts without waiting for
        the next anchor adoption.
        """
        with self._lock:
            self._offset_judge = offset_judge
            self._judge_source_key = source_key
            gps_ns = self._gps_time_ns_raw
            rtp_snap = self._rtp_timesnap
        if gps_ns is not None and rtp_snap is not None:
            self._judge_register_pair(gps_ns, rtp_snap)


    @property
    def counter_epoch_id(self) -> str:
        return self._counter_epoch_id or "unregistered"

    def _note_counter_epoch(self, gps_time_ns: int, rtp_timesnap: int, sample_rate: int) -> str:
        """Open a new counter epoch when the adopted pair disagrees with the
        mapping in force by more than COUNTER_EPOCH_STEP_S; else keep it."""
        prev = self._counter_epoch_pair
        if prev is not None:
            p_gps, p_rtp, p_sr = prev
            delta = (int(rtp_timesnap) - int(p_rtp)) & 0xFFFFFFFF
            if delta > 0x7FFFFFFF:
                delta -= 0x1_0000_0000
            predicted_ns = int(p_gps) + 1_000_000_000 * delta // int(p_sr)
            if abs(int(gps_time_ns) - predicted_ns) <= COUNTER_EPOCH_STEP_S * 1e9:
                self._counter_epoch_pair = (int(gps_time_ns), int(rtp_timesnap), int(sample_rate))
                return self.counter_epoch_id
            logger.warning(
                f"{getattr(self.config, 'channel_name', '?')}: adopted pair sits "
                f"{(int(gps_time_ns) - predicted_ns) / 1e9:+.3f} s from the mapping in force — "
                f"counter re-based; opening a new counter epoch")
        self._counter_epoch_pair = (int(gps_time_ns), int(rtp_timesnap), int(sample_rate))
        self._counter_epoch_id = f"pair-{int(gps_time_ns)}"
        return self._counter_epoch_id

    @staticmethod
    def _radiod_gps_ns_to_utc_ns(gps_time_ns: int) -> int:
        """radiod's GPS_TIME counts nanoseconds from the GPS epoch
        (1980-01-06) with no leap seconds; the TimeMap's t0 is UTC
        nanoseconds from the Unix epoch.  Same conversion the labels use
        (the adoption site above).  On 2026-09-05 the first v2 sidecars on
        AC0G-B4 carried the raw value as t0_utc_ns and read as 2016."""
        from .leap_second import get_current_gps_leap_seconds
        GPS_EPOCH_UNIX = 315964800
        return int(gps_time_ns) + 1_000_000_000 * (GPS_EPOCH_UNIX - get_current_gps_leap_seconds())

    def set_time_map_provider(self, provider, counter_space: str) -> None:
        """Late-bind the TimeMap provider (a callable TimeMapInputs -> TimeMap)
        and this channel's counter-space name."""
        self._time_map_provider = provider
        self._time_map_counter_space = str(counter_space)

    def _legacy_timing_keys(self, verdict, label=None) -> dict:
        """The spec §8 `timing` keys.

        `offset_ns` is the correction a reader RE-APPLIES to the sidecar's
        pair (`buffer_timing.resolve_buffer_timing`), so with a
        label-plane anchor in force it must be 0.0: the pair written
        alongside is ALREADY the anchor's plane, and a non-zero offset
        there would apply the correction twice.  The judge's own reading
        is preserved under `judge_offset_ns` -- it is the witness value,
        the disagreement between radiod's pair and the plane in force,
        and losing it would erase the evidence.
        """
        anchored = label is not None
        return {
            'radiod_gps_time_ns': self._gps_time_ns_raw,
            'radiod_rtp_timesnap': self._rtp_timesnap,
            'offset_ns': 0.0 if anchored else float(verdict.offset_ns),
            'offset_sigma_ns': (float(label.sigma_ns) if anchored
                                else float(verdict.sigma_ns)),
            'judge_tier': label.tier if anchored else verdict.tier,
            # Additive (task 14b): which plane produced this chunk's
            # labels, and the anchor that defines it.
            'plane_source': (
                ('t6_native' if label.tier == 'T6' else 't3_registration')
                if anchored else 'radiod_pair_judged'
            ),
            'anchor_rtp': int(label.anchor.anchor_rtp) if anchored else None,
            'anchor_utc_ns': (int(label.anchor.anchor_utc_ns)
                              if anchored else None),
            'anchor_epoch_id': label.epoch_id if anchored else None,
            # The judge as WITNESS: what it measured against radiod's
            # pair, whether or not it defined the plane.
            'judge_offset_ns': (float(verdict.offset_ns)
                                if verdict is not None else None),
            'judge_offset_sigma_ns': (float(verdict.sigma_ns)
                                      if verdict is not None else None),
            'judge_witness_tier': (verdict.tier if verdict is not None
                                   else None),
            'judge_age_s': float(verdict.judge_age_s),
            'segment_id': int(verdict.segment_id),
            # The stored T6 chain delay in force (diagnostic only; never
            # applied to a label).  None without a record.
            'chain_delay_ns': getattr(verdict, 'chain_delay_ns', None),
            'chain_delay_state': getattr(verdict, 'chain_delay_state', None),
            # P3 (spec §10): the source's current segment rate estimate —
            # RECORDED metadata only, never resampled, never folded into
            # the labels (spec §11, audit G7).  None until the estimator
            # reaches its minimum span.
            'rate_ppm': (float(verdict.rate_ppm)
                         if getattr(verdict, 'rate_ppm', None) is not None else None),
        }

    def _boundary_fields(self) -> dict:
        """The boundary-placement fields for a chunk's `timing` block.

        Empty when no placement was recorded — an older chunk, or a failure
        that fell back to the anchor.  Never raises; provenance must not
        disturb recording.
        """
        # ⚠ getattr, not attribute access.  Tests and older pickles build this
        # object by paths that skip __init__, and a provenance helper that
        # raises AttributeError would take recording down with it — which is
        # precisely what the "never disturbs recording" rule exists to stop.
        placement = getattr(self, '_last_boundary_placement', None)
        if placement is None:
            return {}
        try:
            return placement.sidecar_fields()
        except Exception:  # noqa: BLE001
            return {}

    def _chunk_timing_block(self, verdict, chunk_boundary_utc_ns: int,
                            label=None) -> Optional[dict]:
        """The `timing` block of a chunk's JSON sidecar.

        With a provider: the schema v2 `state` record (TIMING_PROVENANCE_MODEL
        §3.1) with the legacy Offset Judge keys mirrored at top level for one
        release, so hamsci-physics' timing_from_sidecar keeps reading until
        it moves to u_epoch_ns.  Without a provider: the legacy block alone.
        Never raises.

        `label` (task 14b) names the label-plane anchor this chunk's
        labels were placed with, if any: the block then records the
        anchor's plane and a zero re-applied offset, and keeps the
        judge's reading as a witness."""
        legacy = (self._legacy_timing_keys(verdict, label)
                  if verdict is not None else None)
        provider = self._time_map_provider
        if provider is None:
            # ⛔ Return exactly what today returns, including None.  I tried
            # adding boundary provenance here and it made a `timing` block
            # appear on chunks that have none today — test_offset_judge's
            # "judge absent behaves as today" and "none verdict identical to
            # no judge" both caught it.  Step 1's whole premise is that the
            # flag off changes nothing, and a sidecar gaining a block it never
            # had is a change.  Provenance for these chunks waits for the
            # step that turns the flag on.
            return legacy
        from hf_timestd.core.time_map_producer import TimeMapInputs
        from hamsci_dsp.timing_map import null_map
        eng = dict(legacy) if legacy is not None else {
            'radiod_gps_time_ns': self._gps_time_ns_raw,
            'radiod_rtp_timesnap': self._rtp_timesnap}
        # The pair enters the map in UTC nanoseconds; engineering keeps
        # radiod's raw GPS-epoch value under its legacy name, and the
        # counter epoch id stays keyed on the raw value (an opaque name).
        gps_utc_ns = (self._radiod_gps_ns_to_utc_ns(self._gps_time_ns_raw)
                      if self._gps_time_ns_raw is not None else None)
        inputs = TimeMapInputs(
            counter_space=self._time_map_counter_space or self.config.channel_name,
            counter_epoch_id=self.counter_epoch_id,
            f_s_hz=int(self.config.sample_rate),
            measured_at_utc_ns=int(chunk_boundary_utc_ns),
            gps_time_ns=gps_utc_ns, rtp_timesnap=self._rtp_timesnap,
            judge_tier=(
                label.tier if label is not None
                else (verdict.tier if verdict is not None else None)
            ),
            engineering=eng,
        )
        try:
            tmap = provider(inputs)
        except Exception as exc:  # noqa: BLE001 — provenance never disturbs recording
            logger.warning(f"{self.config.channel_name}: time map provider failed: {exc}")
            tmap = null_map(counter_space=inputs.counter_space, counter_epoch_id=inputs.counter_epoch_id,
                            f_s_hz=inputs.f_s_hz, measured_at_utc_ns=inputs.measured_at_utc_ns,
                            reason=f"provider error: {exc}", engineering=eng)
        block = tmap.to_state_record(int(chunk_boundary_utc_ns))
        if legacy is not None:
            block.update(legacy)      # top-level mirror, one release
        block.update(self._boundary_fields())
        return block

    def _judge_register_pair(self, gps_time_ns: int, rtp_timesnap: int,
                             pair_age_s: float = 0.0) -> None:
        """Forward an adopted radiod pair to the judge (never raises).

        ``pair_age_s`` > 0 tells the judge the pair is not fresh (a
        round-trip restore); 0 keeps the judge's fresh-pair reading."""
        judge, key = self._offset_judge, self._judge_source_key
        if judge is None or key is None:
            return
        try:
            if pair_age_s > 0:
                judge.register_radiod_pair(
                    key, gps_time_ns, rtp_timesnap, self.config.sample_rate,
                    pair_age_s=pair_age_s,
                )
            else:
                judge.register_radiod_pair(
                    key, gps_time_ns, rtp_timesnap, self.config.sample_rate
                )
        except Exception as e:  # noqa: BLE001 — judge trouble must never disturb recording
            logger.warning(
                f"{self.config.channel_name}: offset-judge pair registration "
                f"failed (recording continues uncorrected): {e}"
            )

    def _restored_pair_age_s(self, rtp_timesnap: int) -> float:
        """How long ago the counter stood at ``rtp_timesnap``: the newest
        packet's RTP minus the snap, on the sample clock.  0 when no packet
        has arrived or the snap lies ahead of it."""
        last = self.last_rtp_timestamp
        if last is None:
            return 0.0
        delta = int((int(last) - int(rtp_timesnap)) & 0xFFFFFFFF)
        if delta > 0x7FFFFFFF:
            delta -= 0x100000000
        return max(0.0, delta / self.config.sample_rate)

    def _judge_verdict(self, rtp_timestamp: int) -> Optional[Any]:
        """Current judge verdict for this source, or None (raw mapping).

        Cheap (lock + dict lookup inside the judge; no I/O) — safe on
        the per-batch hot path.  Never raises.
        """
        judge, key = self._offset_judge, self._judge_source_key
        if judge is None or key is None:
            return None
        try:
            return judge.offset_for(key, rtp_timestamp)
        except Exception as e:  # noqa: BLE001
            logger.debug(
                f"{self.config.channel_name}: offset_for failed: {e}"
            )
            return None

    def evaluate_pair(self, gps_time_ns: int, rtp_timesnap: int) -> Optional[float]:
        """UTC disagreement (seconds) between a freshly observed radiod
        pair and this writer's currently adopted mapping.

        Returns ``fresh_utc − mapping_implied_utc`` at the fresh pair's
        counter value, or None when no mapping has been adopted yet.
        Used by the P2 revalidation tick (StreamRecorderV2.
        revalidate_radiod_pair) to decide whether a re-observed pair is
        steady-consistent status jitter (leave the steel-ruler mapping
        alone) or a genuine discontinuity (adopt via
        add_timing_snapshot).  Read-only — never mutates the mapping.
        """
        with self._lock:
            if self._gps_time_unix is None or self._rtp_timesnap is None:
                return None
            delta = int((int(rtp_timesnap) - self._rtp_timesnap) & 0xFFFFFFFF)
            if delta > 0x7FFFFFFF:
                delta -= 0x100000000
            implied_utc = self._gps_time_unix + delta / self.config.sample_rate
        GPS_EPOCH_UNIX = 315964800
        from .leap_second import get_current_gps_leap_seconds
        fresh_utc = (
            int(gps_time_ns) / 1_000_000_000
            + GPS_EPOCH_UNIX - get_current_gps_leap_seconds()
        )
        return fresh_utc - implied_utc

    def add_timing_snapshot(self, gps_time_ns: int, rtp_timesnap: int) -> bool:
        """
        Record a GPS_TIME/RTP_TIMESNAP pair from radiod status.
        
        Called at ~2 Hz (radiod's status update rate). Deduplicated by rtp_timesnap
        to avoid storing duplicate snapshots when status hasn't changed.
        
        CRITICAL: This is the AUTHORITATIVE time reference in RTP mode.
        GPS_TIME comes from radiod's GPS+PPS and is the ground truth for UTC.
        We use this to establish the RTP-to-UTC mapping, NOT local system time.
        
        Args:
            gps_time_ns: radiod's GPS_TIME (ns since GPS epoch)
            rtp_timesnap: RTP timestamp at the moment GPS_TIME was sampled
            
        Returns:
            True if snapshot was stored (new), False if deduplicated
        """
        with self._lock:
            return self._adopt_pair_locked(gps_time_ns, rtp_timesnap)

    def current_mapping(self) -> Optional[tuple]:
        """The adopted ``(gps_time_ns, rtp_timesnap)`` exactly as
        ``add_timing_snapshot`` received it, or None before the first."""
        with self._lock:
            if self._gps_time_ns_raw is None or self._rtp_timesnap is None:
                return None
            return int(self._gps_time_ns_raw), int(self._rtp_timesnap)

    def restore_timing_mapping(self, gps_time_ns: int, rtp_timesnap: int) -> bool:
        """Re-adopt a mapping that a pair step replaced (anchor round trip).

        Does what ``add_timing_snapshot`` does -- flush the in-progress
        chunk under the mapping it was labelled with, adopt the pair,
        relay it to the judge -- but flushes whatever the step size, and
        skips the dedupe, since the restored pair may be one this writer
        has seen before.  The next chunk's sidecar carries
        ``"anchor_restored": true``.
        """
        with self._lock:
            if self.current_buffer is not None:
                if self._try_flush(self.current_buffer):
                    self.current_buffer = None
            self._last_rtp_timesnap = None
            self._anchor_restored_pending = True
            return self._adopt_pair_locked(gps_time_ns, rtp_timesnap,
                                           restoring=True)

    def _adopt_pair_locked(self, gps_time_ns: int, rtp_timesnap: int,
                           restoring: bool = False) -> bool:
        """Body of ``add_timing_snapshot``; the caller holds ``_lock``.

        ``restoring`` marks a round-trip restore: the step back is our own
        undo, which the recorder already logged at ERROR, not a radiod
        restart, so the counter-space-changed warning and the watchdog's
        mapping-jump capture stay quiet."""
        # Deduplicate: only store if rtp_timesnap has changed.  The pair in
        # force still confirms a held line it lies on.
        if rtp_timesnap == self._last_rtp_timesnap:
            if self._hold_active and self._pair_on_held_line(rtp_timesnap):
                self._release_hold("the pair in force lies on the held line")
            return False
        
        self._last_rtp_timesnap = rtp_timesnap
        
        # Convert GPS_TIME to Unix time
        # GPS epoch is Jan 6, 1980. GPS_TIME is ns since GPS epoch.
        GPS_EPOCH_UNIX = 315964800  # Unix timestamp of GPS epoch
        from .leap_second import get_current_gps_leap_seconds
        GPS_LEAP_SECONDS = get_current_gps_leap_seconds()
        BILLION = 1_000_000_000
        
        gps_unix_ns = gps_time_ns + BILLION * (GPS_EPOCH_UNIX - GPS_LEAP_SECONDS)
        gps_unix_sec = gps_unix_ns / BILLION
        
        # Detect RTP counter-space discontinuity (wraparound or radiod restart).
        #
        # The 32-bit RTP counter at 24 kHz wraps every ~49.7 hours — this is a
        # routine event, not an error.  A radiod restart resets the counter to
        # near zero at an arbitrary wall-clock moment.
        #
        # In both cases the correct action is identical: flush the in-progress
        # minute buffer (so it is written with the OLD mapping) and then adopt
        # the NEW GPS_TIME/RTP_TIMESNAP.  GPS_TIME is always authoritative; we
        # never need to second-guess it.
        #
        # Distinguishing wraparound from restart for logging purposes:
        #   - gps_unix_sec advances smoothly from _gps_time_unix  → wraparound
        #   - gps_unix_sec is close to time.time()                → either case
        #   The clearest signal is the magnitude of UTC disagreement relative to
        #   one full wrap period (~178957 s).
        WRAP_PERIOD = (0x100000000) / self.config.sample_rate  # ~178957 s at 24 kHz
        if self._gps_time_unix is not None and self._rtp_timesnap is not None:
            # What UTC does the OLD mapping give for the NEW rtp_timesnap?
            old_delta = int((rtp_timesnap - self._rtp_timesnap) & 0xFFFFFFFF)
            if old_delta > 0x7FFFFFFF:
                old_delta -= 0x100000000
            old_utc = self._gps_time_unix + old_delta / self.config.sample_rate
            utc_diff = old_utc - gps_unix_sec
            if abs(utc_diff) > self._anchor_step_threshold_sec:
                is_wraparound = abs(abs(utc_diff) - WRAP_PERIOD) < 60  # within 1 min of wrap period
                if restoring:
                    pass  # our own undo; logged by the recorder
                elif is_wraparound:
                    logger.info(
                        f"{self.config.channel_name}: RTP counter wrapped "
                        f"(32-bit rollover at {WRAP_PERIOD/3600:.1f}h). "
                        f"Adopting new GPS_TIME={gps_unix_sec:.3f}. Flushing current buffer."
                    )
                else:
                    logger.warning(
                        f"{self.config.channel_name}: RTP counter space CHANGED "
                        f"(likely radiod restart) — "
                        f"old mapping gives UTC={old_utc:.3f} but new GPS_TIME={gps_unix_sec:.3f} "
                        f"(diff={utc_diff:+.1f}s). Flushing current buffer."
                    )
                    # Feed the host-wide watchdog: it ignores the ordinary
                    # re-anchor/jitter regime (this branch also trips on
                    # ~0.45 s status jitter) and, on a GROSS jump, captures a
                    # gpsd/chrony evidence bundle + GPS-source-vs-radiod
                    # verdict.  Diagnostic only — never touches timing.
                    try:
                        from .radiod_timing_watchdog import get_watchdog
                        get_watchdog().on_mapping_jump(
                            channel=self.config.channel_name,
                            gps_time_ns=gps_time_ns,
                            rtp_timesnap=rtp_timesnap,
                            radiod_utc=gps_unix_sec,
                            old_utc=old_utc,
                            delta_sec=utc_diff,
                            sample_rate=self.config.sample_rate,
                        )
                    except Exception:  # noqa: BLE001 — never disturb recording
                        pass
                # In both cases: flush and adopt new mapping
                if self.current_buffer is not None:
                    if self._try_flush(self.current_buffer):
                        self.current_buffer = None
                    # On failure _try_flush keeps buffer for retry;
                    # new mapping is adopted below regardless so the
                    # retained buffer will flush with stale timing —
                    # but partial data is better than none.
        
        # Store GPS_TIME/RTP_TIMESNAP mapping directly — no correction needed.
        self._gps_time_unix = gps_unix_sec
        self._gps_time_ns_raw = gps_time_ns
        self._rtp_timesnap = rtp_timesnap
        self._note_counter_epoch(gps_time_ns, rtp_timesnap, self.config.sample_rate)

        # Anchor adoption point: register the pair with the Offset
        # Judge (spec §3 — estimated "at every radiod anchor
        # adoption").  The judge dedupes identical pairs and
        # fractures its segment on implausible steps / RTP wraps.
        # A restored pair is old: tell the judge how old, or it reads the
        # pair's age as radiod's offset (AB4EJ 2026-11-19).
        age_s = self._restored_pair_age_s(rtp_timesnap) if restoring else 0.0
        self._judge_register_pair(gps_time_ns, rtp_timesnap, pair_age_s=age_s)
        if not self._timing_locked:
            self._timing_locked = True
            wait_dur = ''
            if hasattr(self, '_waiting_since'):
                wait_dur = f' (after {time.time() - self._waiting_since:.1f}s wait)'
                del self._waiting_since
            logger.info(f"{self.config.channel_name}: RTP timing LOCKED{wait_dur} - GPS_TIME={gps_unix_sec:.6f}, RTP_TIMESNAP={rtp_timesnap}")
        
        snapshot = TimingSnapshot(
            gps_time_ns=gps_time_ns,
            rtp_timesnap=rtp_timesnap,
            local_receipt_time=time.time()  # For diagnostics only, not used for timing
        )
        
        # Add to current buffer if available, otherwise to pending list
        if self.current_buffer is not None:
            self.current_buffer.timing_snapshots.append(snapshot)
        else:
            self._pending_snapshots.append(snapshot)

        # A line held since a label break gets placed under this pair, if
        # the pair describes that line (a restored old pair does not).
        if self._hold_active and self._pair_on_held_line(rtp_timesnap):
            self._release_hold("pair adopted on the held line")
        
        return True
    
    def _sanitize_channel_name(self) -> str:
        """Convert channel name to filesystem-safe format.
        
        Preserves dots in frequency (e.g., WWV_2.5_MHz) for consistency
        with analytics scripts and web UI.
        """
        return self.config.channel_name.replace(' ', '_')
    
    def _get_minute_dir(self, minute_boundary: int) -> Path:
        """Get directory for a specific minute."""
        dt = datetime.fromtimestamp(minute_boundary, tz=timezone.utc)
        date_str = dt.strftime('%Y%m%d')
        day_dir = self.archive_dir / date_str
        day_dir.mkdir(parents=True, exist_ok=True)
        return day_dir
    
    def _scratch_day_dir(self, boundary: int) -> Path:
        """Day directory for the open chunk's memmap: under scratch_dir
        when set, else beside the chunk."""
        if self.scratch_dir is None:
            return self._get_minute_dir(boundary)
        dt = datetime.fromtimestamp(boundary, tz=timezone.utc)
        day_dir = self.scratch_dir / dt.strftime('%Y%m%d')
        day_dir.mkdir(parents=True, exist_ok=True)
        return day_dir

    def _start_new_minute(self, rtp_derived_time: float, rtp_timestamp: int,
                          verdict: Optional[Any] = None) -> MinuteBuffer:
        """Start a new chunk buffer.

        Args:
            rtp_derived_time: Unix time derived from RTP timestamp
                (GPSDO-disciplined; judge-corrected when a verdict was applied)
            rtp_timestamp: RTP timestamp of the packet that triggered this new chunk
            verdict: Offset Judge verdict applied to rtp_derived_time (or
                None for the raw radiod mapping)

        The RTP stream tells us the exact time. When a packet's RTP-derived UTC
        crosses a chunk boundary, we start a new buffer.

        CRITICAL: We calculate the RTP timestamp that corresponds to the exact
        chunk boundary using the GPS_TIME/RTP_TIMESNAP mapping. This ensures
        sample position 0 = chunk boundary, regardless of when the first packet
        actually arrives.
        """
        chunk_boundary = (int(rtp_derived_time) // self.file_duration_sec) * self.file_duration_sec

        # Calculate RTP timestamp at the exact chunk boundary using the mapping:
        #   UTC = GPS_TIME + (rtp - RTP_TIMESNAP) / sample_rate + offset
        #   rtp = RTP_TIMESNAP + (UTC - offset - GPS_TIME) * sample_rate
        # Note: _rtp_timesnap is already in packet counter space
        # (see counter-space reconciliation in write_samples).
        # When a judge verdict corrected rtp_derived_time, the same
        # offset must be removed here so sample position 0 lands on the
        # chunk boundary in CORRECTED time.
        # Task 14b: with a label-plane anchor in force the correction is
        # the plane offset, not the judge's verdict, so sample position 0
        # lands on the chunk boundary in the ANCHOR's time.
        label = self._label_anchor()
        offset_s = self._label_correction_s(verdict)
        time_delta = chunk_boundary - offset_s - self._gps_time_unix
        rtp_delta = int(time_delta * self.config.sample_rate)
        chunk_boundary_rtp = (self._rtp_timesnap + rtp_delta) & 0xFFFFFFFF

        # Boundary placement (ARCHIVE_BOUNDARY_ON_THE_EDGE.md).  With the flag
        # off — the default, and the fleet's state — this returns
        # chunk_boundary_rtp unchanged and merely RECORDS what the detected
        # pulse would have said.  That shadow number measures radiod's anchor
        # error without altering a stored file.  Never raises: a boundary is
        # not the place to discover a new exception.
        try:
            from hf_timestd.core.archive_boundary import place_boundary
            placement = place_boundary(
                chunk_boundary_rtp,
                int(self.config.sample_rate),
                edge_rtp=self._pulse_edge_rtp,
                use_pulse=bool(getattr(self.config, 'boundary_on_pulse', False)),
                chain_delay_ns=self._bpsk_chain_delay_ns,
            )
            self._last_boundary_placement = placement
            chunk_boundary_rtp = placement.chunk_boundary_rtp
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"{self.config.channel_name}: boundary placement failed, "
                f"keeping the anchor value: {exc}")
            self._last_boundary_placement = None

        # Allocate the chunk buffer as a memmap backed by a scratch
        # file in the chunk's destination directory.  Mode ``'w+'``
        # creates the file at full size (kernel-zeroed via SIGBUS-safe
        # sparse allocation on supporting filesystems) and lets us
        # write through it.  At ``_flush_minute`` we read this back
        # to drive compression / direct write, then unlink the scratch
        # file.  Anonymous heap stays near zero for the whole fill.
        #
        # Per-channel scratch name (``.<channel>.scratch``) so a
        # concurrent flush from another channel can't collide, and a
        # per-writer sequence number so a chunk reopened at the same
        # boundary (a pair step mid-chunk) never unlinks the scratch of
        # the earlier buffer still waiting in the flush queue.
        minute_dir = self._scratch_day_dir(chunk_boundary)
        self._scratch_seq += 1
        scratch_path = minute_dir / (
            f"{chunk_boundary}.{self._sanitize_channel_name()}"
            f".{self._scratch_seq}.scratch"
        )
        # Best-effort: clean up any leftover scratch from a previous
        # process crash before we mmap a fresh one.  We hold the writer
        # lock, no concurrent producer for this channel exists.
        if scratch_path.exists():
            try:
                scratch_path.unlink()
            except OSError as e:
                logger.warning(
                    f"could not unlink stale scratch {scratch_path}: {e} "
                    f"(continuing with fresh memmap allocation)"
                )
        try:
            samples = np.memmap(
                str(scratch_path),
                dtype=np.complex64,
                mode='w+',
                shape=(self.samples_per_chunk,),
            )
        except OSError as e:
            # Fall back to heap allocation if memmap creation fails
            # (read-only fs, ENOSPC, etc.).  Worse memory profile but
            # preserves the old behaviour rather than crashing the
            # recorder.  Log noisily so the operator sees it.
            logger.error(
                f"memmap scratch alloc failed for {self.config.channel_name} "
                f"({scratch_path}): {e}.  Falling back to heap np.zeros — "
                f"daemon RSS will sawtooth during this chunk's fill."
            )
            samples = np.zeros(self.samples_per_chunk, dtype=np.complex64)
            scratch_path = None

        # Offset Judge provenance (spec §8), frozen at chunk start: the
        # verdict here is the one that positioned this chunk's boundary.
        # The raw radiod pair is captured alongside so the sidecar is
        # fully self-describing (raw mapping + applied correction).
        judge_timing = self._chunk_timing_block(
            verdict, chunk_boundary_utc_ns=int(round(float(chunk_boundary) * 1e9)),
            label=label)

        buffer = MinuteBuffer(
            minute_boundary=chunk_boundary,
            samples=samples,
            write_pos=0,
            start_rtp=chunk_boundary_rtp,  # RTP at actual chunk boundary
            start_system_time=float(chunk_boundary),  # Exactly on chunk boundary
            timing_snapshots=[],
            scratch_path=scratch_path,
            judge_timing=judge_timing,
            label_pair=self._label_pair(label),
            anchor_restored=bool(getattr(self, '_anchor_restored_pending', False)),
        )
        self._anchor_restored_pending = False

        # Transfer any pending timing snapshots to this buffer
        if self._pending_snapshots:
            buffer.timing_snapshots.extend(self._pending_snapshots)
            logger.debug(f"Transferred {len(self._pending_snapshots)} pending timing snapshots to new chunk")
            self._pending_snapshots = []

        logger.debug(f"Started new chunk buffer: {chunk_boundary} ({self.file_duration_sec}s)")
        return buffer
    
    def _check_disk_space(self, path: Path, required_bytes: int) -> bool:
        """Refuse a flush when free space runs short.  Never delete a file.

        True when at least required_bytes + 100 MiB is free.  Otherwise this
        logs an ERROR and returns False.  Retention belongs to sigmond's
        ledger and the prune timer, not to the writer.
        """
        try:
            stat = shutil.disk_usage(path)
            min_free = required_bytes + 100 * 1024 * 1024
            if stat.free < min_free:
                logger.error(
                    f"Insufficient disk space: {stat.free / 1024 / 1024:.1f}MB free, "
                    f"need {min_free / 1024 / 1024:.1f}MB"
                )
                return False
            return True
        except OSError as e:
            logger.warning(f"Could not check disk space: {e}")
            return True  # Proceed anyway, let write fail if needed

    def _try_flush(self, buffer: MinuteBuffer) -> bool:
        """Hand the buffer off to the async flush worker.

        Returns True unconditionally — ownership transfers to the worker
        (or to scratch-cleanup on queue overflow), so the caller must
        clear ``current_buffer`` either way.

        The worker handles compression + fsync + retries off the
        receive thread.  The bounded queue ensures the receive thread
        never blocks on enqueue: if the worker has fallen behind
        (queue full), we cleanly abandon the oldest buffer with a
        loud error rather than back-pressuring packet reads.
        """
        try:
            # ``put_nowait`` so we never block the receive thread on
            # the queue.  Worker is sized for the steady-state load;
            # if it ever falls behind by ``maxsize`` chunks we have a
            # bigger problem than this one buffer.
            self._flush_queue.put_nowait(buffer)
            return True
        except queue.Full:
            logger.error(
                f"{self.config.channel_name}: flush queue full "
                f"(maxsize={self._flush_queue.maxsize}) — ABANDONING "
                f"minute {buffer.minute_boundary} "
                f"({buffer.write_pos} samples LOST)"
            )
            self._count_abandoned(buffer)
            self._release_scratch(buffer)
            return True

    def _flush_worker_loop(self) -> None:
        """Daemon worker: pull buffers from the queue and flush them.

        One worker thread per archive channel — the channels write
        independent files so there's no inter-channel ordering need;
        the parallelism gives us 9× concurrent compression+fsync
        instead of the old serial-on-the-receive-thread behavior.

        The worker runs at LOWER priority than the rest of the recorder.
        It inherited the unit's ``Nice=-10``, which meant zstd ran at
        *elevated* priority — every channel's chunk closes on the same
        300 s wall-clock epoch, so six 57.6 MB compressions fired
        simultaneously, phase-locked to the minute-aligned metrology and
        2-minute WSPR cycles, and outranked the capture path while doing
        it. Compression is bulk work with seconds of slack; capture has
        none.

        Deliberately nice rather than SCHED_IDLE. ``_flush_queue`` is
        bounded and the overflow policy is to log and DROP rather than
        backpressure the network reader, so a worker that never gets
        scheduled loses archive chunks. Nice yields to everything that
        matters without that cliff — and it needs no model of when the
        bursts are, which is the part that would go stale.
        """
        _renice_current_thread(_FLUSH_WORKER_NICE)
        while not self._flush_stop.is_set():
            try:
                buffer = self._flush_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if buffer is None:
                # Sentinel from close() — drain done.
                self._flush_queue.task_done()
                return
            try:
                self._flush_one_buffer(buffer)
            finally:
                self._flush_queue.task_done()

    def _flush_one_buffer(self, buffer: MinuteBuffer) -> bool:
        """Synchronously flush a single buffer with retry.  Returns
        True on success, False if all retries were exhausted (in which
        case scratch has been unlinked and samples are lost).

        Extracted from the worker loop for direct test access — the
        retry logic is the unit of behavior worth verifying.
        """
        for attempt in range(self.MAX_FLUSH_RETRIES):
            if self._flush_minute(buffer):
                return True
            buffer.flush_attempts = attempt + 1
            logger.warning(
                f"{self.config.channel_name}: flush failed for minute "
                f"{buffer.minute_boundary} (attempt {buffer.flush_attempts}/"
                f"{self.MAX_FLUSH_RETRIES}) — will retry"
            )
            # Exponential backoff capped at 5 s.  attempt=0 → 0.1 s,
            # attempt=1 → 0.2 s, … attempt=5 → 3.2 s, attempt=6+ → 5 s.
            time.sleep(min(0.1 * (2 ** attempt), 5.0))
        logger.error(
            f"{self.config.channel_name}: ABANDONING minute "
            f"{buffer.minute_boundary} after {self.MAX_FLUSH_RETRIES} "
            f"flush failures — {buffer.write_pos} samples LOST"
        )
        self._count_abandoned(buffer)
        self._release_scratch(buffer)
        return False

    @staticmethod
    def _free_chunk_stem(minute_dir: Path, boundary: int) -> tuple:
        """(stem, segment) for a chunk at ``boundary`` that replaces no file."""
        segment = 1
        while True:
            stem = str(boundary) if segment == 1 else f"{boundary}.seg{segment}"
            if not any((minute_dir / f"{stem}{ext}").exists()
                       for ext in ('.bin', '.bin.zst', '.bin.lz4', '.json')):
                return stem, segment
            segment += 1

    def _cleanup_partial_write(self, *paths: Path) -> None:
        """Clean up partial files after a failed write."""
        for path in paths:
            if path is None:
                continue
            try:
                if path.exists():
                    path.unlink()
                    logger.debug(f"Cleaned up partial file: {path}")
            except OSError as e:
                logger.warning(f"Failed to clean up {path}: {e}")

    def _count_abandoned(self, buffer: MinuteBuffer) -> None:
        """An abandoned buffer's written samples never reach disk: count them."""
        self.abandoned_chunks += 1
        self.abandoned_samples += buffer.samples_covered

    def _release_scratch(self, buffer: MinuteBuffer) -> None:
        """Release the memmap and unlink its backing scratch file.

        Called on flush success (success path of _flush_minute) and on
        abandon after MAX_FLUSH_RETRIES (_try_flush).  Idempotent: safe
        to call on a buffer that fell back to heap allocation
        (``scratch_path is None``) or that has already been released.
        Errors are logged at warn level rather than raised — they imply
        a stranded scratch file at most, not data loss.
        """
        if buffer.samples is not None:
            # numpy ≥1.21: np.memmap has a no-op _mmap when uninitialised;
            # del-ing the array drops the kernel mapping ref.  We also
            # zero out the dataclass field so any later access raises
            # AttributeError loudly instead of writing into a freed page.
            buffer.samples = None  # type: ignore[assignment]
        if buffer.scratch_path is not None:
            try:
                if buffer.scratch_path.exists():
                    buffer.scratch_path.unlink()
            except OSError as e:
                logger.warning(
                    f"failed to unlink scratch {buffer.scratch_path}: {e} "
                    f"(stranded; the next chunk's _start_new_minute will retry)"
                )
            buffer.scratch_path = None
    
    @staticmethod
    def _iter_raw_blocks(chunk: np.ndarray):
        """Yield ``chunk`` as zero-copy complex64 slices of the memmap."""
        step = _STREAM_BLOCK_SAMPLES
        for start in range(0, len(chunk), step):
            yield chunk[start:start + step]

    def _write_zstd_streaming(self, path: Path, blocks, total_bytes: int) -> None:
        """Compress ``blocks`` into ``path`` as one zstd frame, then fsync.

        The compressor gets the total size up front (``size=``), exactly as
        the one-shot ``compress()`` learned it from its argument, so the
        frame header and the compressed bytes match the one-shot write
        (tests/unit/test_archive_flush_streaming.py).  Memory stays at a
        block in and a block out, where the one-shot write held the whole
        chunk uncompressed AND compressed — ~2x 115 MB per channel at
        24 kHz, six channels at once on the same boundary.  The context
        itself (~50-60 MB, see _FLUSH_MEMORY_LOCK) lives only inside the
        lock; the fsync runs outside it.

        CRITICAL FIX (2026-01-12), kept: threads=1.  Multi-threaded
        compression across 9 channels simultaneously stalled the recorder.
        threads=1 also fixes the bytes: threads=0 writes a different frame.
        """
        import zstandard as zstd
        with open(path, 'wb') as f:
            with _FLUSH_MEMORY_LOCK:
                cctx = zstd.ZstdCompressor(level=self.config.compression_level, threads=1)
                cobj = cctx.compressobj(size=total_bytes)
                for block in blocks:
                    out = cobj.compress(block)
                    if out:
                        f.write(out)
                f.write(cobj.flush())
                del cobj, cctx   # free the context's job buffers inside the lock
                _release_freed_heap()
            f.flush()
            os.fsync(f.fileno())
            written = f.tell()
        if written:
            logger.debug(f"zstd compression: {total_bytes} -> {written} "
                         f"({total_bytes / written:.1f}x)")

    def _run_f16_check(self, chunk: np.ndarray, minute_boundary: int,
                       f16_extra: Dict[str, Any]) -> None:
        """Measure the F16 rounding on the chunk's first minute, into f16_extra.

        Under ``_FLUSH_MEMORY_LOCK``: one heavy step at a time across every
        channel in the process.  Never blocks the write.
        """
        try:
            n = min(len(chunk), int(self.config.sample_rate) * 60)
            head = chunk[:n]
            with _FLUSH_MEMORY_LOCK:
                f16_extra['f16_check'] = f16_check.measure(
                    np.asarray(head),
                    decode_iq(b''.join(iter_f16_blocks(head)), DTYPE_F16, F16_SCALE),
                    self.config.sample_rate)
                _release_freed_heap()
        except Exception as e:  # the check never blocks the write
            logger.warning(f"f16_check failed for chunk {minute_boundary}: {e}")

    def _flush_minute(self, buffer: MinuteBuffer) -> bool:
        """Write completed minute buffer to disk with disk full handling."""
        bin_path = None
        json_path = None
        temp_json = None
        bin_path_tmp = None
        
        try:
            minute_dir = self._get_minute_dir(buffer.minute_boundary)
            
            # Binary file path - extension depends on compression.  Never
            # replace a chunk already on disk: a chunk reopened at the same
            # boundary (a pair step mid-chunk, a recorder restart, a
            # re-based label) used to overwrite the earlier file.  The later
            # one becomes a segment, ``<boundary>.seg<N>``; the readers join
            # a boundary's segments (chunk_segments).
            compression = self.config.compression.lower()
            stem, segment = self._free_chunk_stem(minute_dir, buffer.minute_boundary)
            if compression == 'zstd':
                bin_path = minute_dir / f"{stem}.bin.zst"
            elif compression == 'lz4':
                bin_path = minute_dir / f"{stem}.bin.lz4"
            else:
                bin_path = minute_dir / f"{stem}.bin"
            json_path = minute_dir / f"{stem}.json"
            
            # Write binary data (just the filled portion).  The zstd path
            # streams the chunk out of the scratch memmap in blocks: never
            # a whole-chunk copy, never the whole compressed output in
            # memory (see _write_zstd_streaming).
            actual_samples = min(buffer.write_pos, self.samples_per_chunk)
            chunk = buffer.samples[:actual_samples]
            dtype_name = 'complex64'
            f16_extra: Dict[str, Any] = {}
            want_f16 = compression == 'zstd' and self.config.archive_dtype == 'f16'
            stored_bytes = actual_samples * bytes_per_sample(
                DTYPE_F16 if want_f16 else DTYPE_F32)

            # Check disk space before writing (raw size + some overhead)
            if not self._check_disk_space(minute_dir, stored_bytes + 10000):
                self.write_errors += 1
                return False

            # Atomic write: write to temp file first
            bin_path_tmp = bin_path.with_suffix(bin_path.suffix + '.tmp')

            # Apply compression if configured
            if compression == 'zstd':
                try:
                    import zstandard  # noqa: F401  (fallback below if absent)
                    zstd_ok = True
                except ImportError:
                    zstd_ok = False
                if zstd_ok:
                    wrote_f16 = False
                    if want_f16:
                        try:
                            self._write_zstd_streaming(
                                bin_path_tmp, iter_f16_blocks(chunk), stored_bytes)
                            wrote_f16 = True
                        except F16Overflow:
                            self._cleanup_partial_write(bin_path_tmp)
                            f16_extra['f16_fallback'] = 'overflow'
                            logger.warning(
                                f"F16 overflow in chunk {buffer.minute_boundary} "
                                f"({self.config.channel_name}): storing complex64"
                            )
                    if wrote_f16:
                        dtype_name = DTYPE_F16
                        f16_extra['scale'] = F16_SCALE
                        if self.config.f16_check and actual_samples > 0:
                            self._run_f16_check(chunk, buffer.minute_boundary, f16_extra)
                    else:
                        self._write_zstd_streaming(
                            bin_path_tmp, self._iter_raw_blocks(chunk),
                            actual_samples * bytes_per_sample(DTYPE_F32))
                else:
                    logger.warning("zstandard not installed, falling back to uncompressed")
                    bin_path = minute_dir / f"{stem}.bin"
                    bin_path_tmp = bin_path.with_suffix('.bin.tmp')
                    chunk.tofile(bin_path_tmp)
                    dtype_name = 'complex64'   # the fallback file holds F32
                    f16_extra = {}
            elif compression == 'lz4':
                try:
                    import lz4.frame
                    raw_data = chunk.tobytes()
                    compressed_data = lz4.frame.compress(raw_data, compression_level=self.config.compression_level)
                    with open(bin_path_tmp, 'wb') as f:
                        f.write(compressed_data)
                        f.flush()
                        os.fsync(f.fileno())
                    compression_ratio = len(raw_data) / len(compressed_data)
                    logger.debug(f"lz4 compression: {len(raw_data)} -> {len(compressed_data)} ({compression_ratio:.1f}x)")
                    del raw_data, compressed_data
                except ImportError:
                    logger.warning("lz4 not installed, falling back to uncompressed")
                    bin_path = minute_dir / f"{stem}.bin"
                    bin_path_tmp = bin_path.with_suffix('.bin.tmp')
                    chunk.tofile(bin_path_tmp)
            else:
                # No compression - direct write
                chunk.tofile(bin_path_tmp)
            del chunk

            # Write metadata sidecar.  The sidecar lands BEFORE the bin is
            # renamed into place: a crash between the two must never leave
            # a bin without the sidecar that names its dtype (an F16 chunk
            # read as complex64 is garbage).  A sidecar without a bin is
            # harmless — every reader looks for the bin first.
            metadata = {
                'minute_boundary': buffer.minute_boundary,
                'channel_name': self.config.channel_name,
                'frequency_hz': self.config.frequency_hz,
                'sample_rate': self.config.sample_rate,
                'samples_written': actual_samples,
                'samples_expected': self.samples_per_chunk,
                'file_duration_sec': self.file_duration_sec,
                'completeness_pct': 100.0 * actual_samples / self.samples_per_chunk,
                'gap_count': buffer.gap_count,
                'gap_samples': buffer.gap_samples,
                # The archive stores ka9q-python's gap fill as the zeros it
                # received; fill_runs names where they lie.  Known limit
                # (Ruling F1): inside a batch that reports a gap, a sample
                # the radio really received as exact zero also lands in
                # fill_runs.  GRAPE counts those positions as lost.
                'gap_fill': 'zeros',
                'fill_runs': [list(r) for r in buffer.fill_runs],
                # Coverage.  samples_written above is the highest position
                # written (the bin's length), and completeness_pct follows
                # it; these count what inside it the writer actually wrote.
                # samples_covered includes the fill, and fill_runs names
                # where it lies, so received samples = samples_covered
                # minus the fill count (the sum of fill_runs).  gap_samples
                # stays what ka9q-python reported, so the two can differ.
                # hole_* covers
                # [0, samples_written) only: a short tail already shows as
                # samples_written < samples_expected.
                'samples_covered': buffer.samples_covered,
                'hole_samples': actual_samples - buffer.samples_covered,
                'hole_count': buffer.hole_count,
                # The [start, end) runs behind samples_covered.  A reader
                # joining this chunk's segments (<boundary>.seg<N>) takes
                # each segment's samples from its own runs.
                'covered_runs': [list(r) for r in buffer.covered_runs],
                # Each batch was placed by its FIRST delivered sample
                # (resequencer delivered_rtp_start), and a batch crossing
                # the chunk end continued into the next chunk.  A sidecar
                # without this field is an older chunk: its F32 batches sit
                # late by the label error (1440 samples at 24 kHz on B4).
                # "last_packet" marks the fallback for ka9q-python without
                # delivered_rtp_start (placement as old).  overlap_samples
                # and dropped_before_start count samples refused because
                # their label fell on positions already written or before
                # this chunk's start.
                'batch_label': buffer.batch_label or 'first_sample',
                'overlap_samples': buffer.overlap_samples,
                'dropped_before_start': buffer.dropped_before_start,
                'start_rtp_timestamp': buffer.start_rtp,
                'start_system_time': buffer.start_system_time,
                # Authoritative GPS/RTP mapping from the writer — always present
                # when timing is locked.  buffer_timing.py uses these directly.
                # Task 14b: with a label-plane anchor in force this is
                # the ANCHOR's plane restated at radiod's snap counter,
                # frozen at chunk start alongside the labels it placed;
                # radiod's raw pair stays in the `timing` block under
                # radiod_gps_time_ns.  Audit G6: the ring is anchored on
                # the same object, so ring-resolved and sidecar-resolved
                # UTC are equal by construction.
                'gps_time_ns': (buffer.label_pair[0]
                                if buffer.label_pair is not None
                                else self._gps_time_ns_raw),
                'rtp_timesnap': (buffer.label_pair[1]
                                 if buffer.label_pair is not None
                                 else self._rtp_timesnap),
                'dtype': dtype_name,
                'byte_order': 'little',
                'compression': compression if compression != 'none' else None,
                'radiod_snr_db': self.config.radiod_snr_db,  # SNR from radiod
                'written_at': datetime.now(timezone.utc).isoformat(),
                'station': self.config.station_config,
                # Counter-space correction: timing_snapshots[].rtp_timesnap is in
                # RTP_TIMESNAP and packet RTP timestamps are in the same counter
                # space (both derived from input_sample_index / decimation).
                # No pipeline offset correction is needed.
                'pipeline_offset_samples': 0,
                # Timing snapshots: GPS_TIME/RTP_TIMESNAP pairs from radiod (~2 Hz)
                # Enables post-hoc RTP-to-UTC conversion and timing validation
                'timing_snapshots': [s.to_dict() for s in buffer.timing_snapshots],
                # BPSK PPS chain-delay metadata.  As of 2026-05 archives
                # store RAW RTP-derived wall_times (chain_delay is in
                # metadata but NOT applied).  Pre-2026-05 archives lack
                # these fields and have an implied "applied=True"
                # convention.  Downstream readers can apply the value
                # here if they want UTC-aligned timing.
                'bpsk_chain_delay_ns': self._bpsk_chain_delay_ns,
                'bpsk_chain_delay_applied': self._bpsk_chain_delay_applied,
            }

            # Offset Judge provenance block (spec §8, additive).  A chunk
            # without a "timing" block is legacy (raw radiod mapping); a
            # chunk with one is fully self-describing: raw pair + the
            # correction applied to this chunk's labels.
            if buffer.judge_timing is not None:
                metadata['timing'] = buffer.judge_timing
            if buffer.anchor_restored:
                metadata['anchor_restored'] = True
            if segment > 1:
                metadata['segment'] = segment
                logger.warning(
                    f"{self.config.channel_name}: chunk {buffer.minute_boundary} "
                    f"already on disk; writing this one as segment {segment} "
                    f"({stem})")
            metadata.update(f16_extra)
            
            # Atomic write: write to temp file, fsync, then rename
            temp_json = json_path.with_suffix('.tmp')
            with open(temp_json, 'w') as f:
                json.dump(metadata, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            temp_json.replace(json_path)

            # Rename the bin into place last (atomic)
            if bin_path_tmp.exists():
                bin_path_tmp.replace(bin_path)

            self.minutes_written += 1
            logger.info(
                f"📁 Wrote chunk {buffer.minute_boundary}: "
                f"{actual_samples}/{self.samples_per_chunk} samples "
                f"({metadata['completeness_pct']:.1f}%) "
                f"[{bin_path.name}]"
            )

            # Release the memmap + unlink the scratch file.  Done last
            # so a failure here doesn't roll back the successful write —
            # the worst case is a stranded scratch file that the next
            # _start_new_minute call will clean up.  Setting samples to
            # None drops the only reference; np.memmap frees pages and
            # closes the underlying fd on GC.
            self._release_scratch(buffer)

            return True

        except OSError as e:
            # Handle disk full specifically
            if e.errno == errno.ENOSPC:
                logger.error(
                    f"DISK FULL: Failed to write minute {buffer.minute_boundary}. "
                    "Consider freeing disk space or enabling compression."
                )
            else:
                logger.error(f"OS error writing minute {buffer.minute_boundary}: {e}")
            # Clean up any partial files
            # The compressed temp file holds SSD space on a full disk too.
            self._cleanup_partial_write(bin_path, json_path, temp_json, bin_path_tmp)
            self.write_errors += 1
            return False
        except Exception as e:
            logger.error(f"Failed to write minute {buffer.minute_boundary}: {e}", exc_info=True)
            # Clean up any partial files
            # The compressed temp file holds SSD space on a full disk too.
            self._cleanup_partial_write(bin_path, json_path, temp_json, bin_path_tmp)
            self.write_errors += 1
            return False
    
    def _rtp_to_unix_time(self, rtp_timestamp: int) -> float:
        """
        Convert RTP timestamp to Unix time. In RTP mode the GPSDO provides UTC
        directly via GPS_TIME/RTP_TIMESNAP — no offset discovery needed.
        
        RTP_TIMESNAP has been corrected to the packet counter space (see
        counter-space reconciliation in write_samples).
        
        Formula: UTC = GPS_TIME + (rtp - RTP_TIMESNAP) / sample_rate
        """
        if self._gps_time_unix is None or self._rtp_timesnap is None:
            # Not initialized yet - return 0 (will use system_time fallback)
            return 0.0
        
        # Handle 32-bit RTP wrap-around
        rtp_delta = int((rtp_timestamp - self._rtp_timesnap) & 0xFFFFFFFF)
        if rtp_delta > 0x7FFFFFFF:
            rtp_delta -= 0x100000000
        
        return self._gps_time_unix + rtp_delta / self.config.sample_rate
    
    def write_samples(
        self,
        samples: np.ndarray,
        rtp_timestamp: int,
        system_time: Optional[float] = None,
        gap_samples: int = 0,
        label_source: str = "first_sample",
        rebased: bool = False,
    ) -> int:
        """
        Write IQ samples to the archive.
        
        Args:
            samples: Complex64 IQ samples
            rtp_timestamp: RTP timestamp of first sample
            system_time: System wall clock time (only used for initial sync)
            gap_samples: Number of gap samples (for statistics)
            label_source: what ``rtp_timestamp`` names: "first_sample"
                (delivered_rtp_start) or "last_packet" (the fallback for
                ka9q-python without it).  Recorded in the sidecar.
            rebased: the stream reported a STREAM_INTERRUPTION for this
                batch (ka9q-python >= 3.28.0): its label opens a new line.
            
        Returns:
            Number of samples written
        """
        with self._lock:
            # GPS_TIME/RTP_TIMESNAP must be established before we can write.
            # Every sample arriving here without timing is SILENTLY LOST.
            if self._gps_time_unix is None or self._rtp_timesnap is None:
                now = time.time()
                if not hasattr(self, '_waiting_since'):
                    self._waiting_since = now
                wait_secs = now - self._waiting_since
                if not hasattr(self, '_last_waiting_log') or now - self._last_waiting_log > 5.0:
                    if wait_secs > 60:
                        logger.error(
                            f"{self.config.channel_name}: NO GPS_TIME for {wait_secs:.0f}s — "
                            f"samples are being DROPPED. Check radiod GPS+PPS lock."
                        )
                    elif wait_secs > 15:
                        logger.warning(
                            f"{self.config.channel_name}: Waiting for GPS_TIME from radiod "
                            f"({wait_secs:.0f}s, samples dropped)"
                        )
                    else:
                        logger.info(
                            f"{self.config.channel_name}: Waiting for GPS_TIME from radiod..."
                        )
                    self._last_waiting_log = now
                self.timing_drops += len(samples)
                return 0  # Cannot write until we have authoritative timing

            n = len(samples)
            rtp = int(rtp_timestamp)
            self.last_rtp_timestamp = (rtp + n) & 0xFFFFFFFF
            expected = self._expected_label
            self._expected_label = (rtp + n) & 0xFFFFFFFF
            jump = 0
            if expected is not None and rtp != expected:
                jump = (rtp - expected) & 0xFFFFFFFF
                if jump > 0x7FFFFFFF:
                    jump -= 0x100000000
                self.label_discontinuities += 1
            # A real break: the stream said so, the label went backward, or
            # it jumped further forward than any ka9q-python zero-fills.  A
            # smaller forward jump stays on the line the mapping describes
            # and places at once; its hole shows in hole_*.  The fallback
            # label jumps whenever a batch's size changes and places as
            # before, so it never holds.  Nor does a writer with no pair
            # source: nothing could release the hold.
            broke = rebased or jump < 0 or jump > self.FORWARD_BREAK_SAMPLES
            if (broke and not self._hold_active
                    and label_source != "last_packet"
                    and self._pair_provider is not None):
                self._begin_hold(rtp, jump, rebased)
            if self._hold_active:
                self._hold_batch(samples, rtp, gap_samples, label_source)
                return self._poll_held_line()
            self._label_source = label_source
            return self._write_samples_inner(samples, rtp_timestamp, gap_samples)

    # A label that breaks its line may sit on a new RTP counter the pair in
    # force does not describe: after a radiod restart the old pair maps it
    # up to 24.8 h away.  The writer holds such batches, in order, and on
    # each held batch reads radiod's current pair through the provider the
    # recorder wires (the listener-refreshed ChannelInfo; no adopted pair
    # reaches this writer in steady state).  A pair on the held line that
    # continues the mapping in force releases the hold under that mapping
    # (a re-sync or an outage on the old line).  One that steps is handed
    # back to the recorder (take_step_request), whose revalidation and
    # PairStepArbiter adopt it; the adoption releases the hold.  No host
    # clock enters.  The hold keeps at most LABEL_HOLD_SECONDS of samples;
    # only the overflow is dropped (unplaced_samples).  20 s leaves room
    # for radiod's first post-restart pair plus the arbiter's
    # CONFIRM_MIN_SPAN_S; it costs ~3.8 MB per channel at 24 kHz.
    LABEL_HOLD_SECONDS = 20.0

    # The smallest gap any supported ka9q-python leaves unfilled: 3.27.0
    # fills up to 960 000 samples, 3.28.0 up to 120 s (or a 64 MiB budget).
    # A forward label jump up to this stays on its line.
    FORWARD_BREAK_SAMPLES = 960_000

    # A pair this close to the mapping in force continues it: the
    # PairStepArbiter's STEADY limit (radiod's GPS_TIME arrives late by up
    # to ~0.8 s, so honest pairs scatter by a few hundred ms).
    HOLD_CONTINUE_LIMIT_S = 0.75

    def set_pair_provider(self, provider) -> None:
        """``provider()`` returns radiod's current (gps_time_ns,
        rtp_timesnap) or None.  Without one the writer never holds."""
        self._pair_provider = provider

    @property
    def hold_active(self) -> bool:
        return self._hold_active

    def take_step_request(self) -> Optional[tuple]:
        """The stepped pair a held line waits on, once; else None.

        The recorder runs its revalidation on it, outside this writer's
        lock (adoption re-enters through add_timing_snapshot)."""
        with self._lock:
            req, self._step_request = self._step_request, None
            return req

    def _pair_on_held_line(self, rtp_timesnap: int) -> bool:
        """Does a pair's snap lie on the held line?  radiod samples a fresh
        pair near the newest sample, so a pair for the held line sits
        within the hold span plus 2 s; an old-line pair, or a restored pair
        taken minutes before, does not.  Wrap-aware, RTP only."""
        if not self._held:
            return False
        first = self._held[0][1]
        d = (int(rtp_timesnap) - first) & 0xFFFFFFFF
        if d > 0x7FFFFFFF:
            d -= 0x100000000
        sr = self.config.sample_rate
        return (-int(self.LABEL_HOLD_SECONDS * sr) <= d
                <= self._held_n + 2 * sr)

    def _pair_diff_s(self, gps_time_ns: int, rtp_timesnap: int) -> float:
        """evaluate_pair's disagreement, for a caller holding the lock."""
        delta = int((int(rtp_timesnap) - self._rtp_timesnap) & 0xFFFFFFFF)
        if delta > 0x7FFFFFFF:
            delta -= 0x100000000
        implied = self._gps_time_unix + delta / self.config.sample_rate
        from .leap_second import get_current_gps_leap_seconds
        fresh = (int(gps_time_ns) / 1e9 + 315964800
                 - get_current_gps_leap_seconds())
        return fresh - implied

    def _poll_held_line(self) -> int:
        """Read radiod's current pair for the held line; release or ask
        for a step.  Returns the samples a release placed."""
        try:
            pair = self._pair_provider()
        except Exception:  # noqa: BLE001 -- a provider fault only delays release
            pair = None
        if pair is None or not self._pair_on_held_line(pair[1]):
            return 0
        if abs(self._pair_diff_s(*pair)) <= self.HOLD_CONTINUE_LIMIT_S:
            return self._release_hold("radiod's pair continues the mapping")
        self._step_request = (int(pair[0]), int(pair[1]))
        return 0

    def _begin_hold(self, rtp: int, jump: int, rebased: bool) -> None:
        self._hold_active = True
        if jump:
            what = f"label jumped {jump:+d} samples"
            if rebased:
                what += " (STREAM_INTERRUPTION)"
        else:
            what = "STREAM_INTERRUPTION"
        # The host lag goes in the log line only: it decides nothing.
        try:
            verdict = self._judge_verdict(rtp)
            lag = self._host_time() - (self._rtp_to_unix_time(rtp)
                                       + self._label_correction_s(verdict))
            lag_txt = f"; the pair in force maps it {lag:+.1f}s off the host clock"
        except Exception:  # noqa: BLE001 -- diagnostics only
            lag_txt = ""
        self._label_fault_log(
            f"{self.config.channel_name}: {what} at rtp={rtp}; holding the "
            f"line until a pair places it{lag_txt}")

    def _hold_batch(self, samples: np.ndarray, rtp: int, gap_samples: int,
                    label_source: str) -> None:
        self._held.append((np.array(samples, dtype=np.complex64, copy=True),
                           rtp, gap_samples, label_source))
        self._held_n += len(samples)
        bound = int(self.LABEL_HOLD_SECONDS * self.config.sample_rate)
        while self._held_n > bound and self._held:
            old, _, _, _ = self._held.popleft()
            self._held_n -= len(old)
            self.unplaced_samples += len(old)
            self._label_fault_log(
                f"{self.config.channel_name}: no pair within "
                f"{self.LABEL_HOLD_SECONDS:.0f}s of held samples; dropped the "
                f"oldest {len(old)} (unplaced_samples={self.unplaced_samples})")

    def _release_hold(self, why: str) -> int:
        """Replay the held batches, in order, under the mapping now in force."""
        held, self._held = self._held, _deque()
        n, self._held_n = self._held_n, 0
        self._hold_active = False
        self._step_request = None
        if held:
            logger.info(
                f"{self.config.channel_name}: {why}; replaying "
                f"{len(held)} held batch(es), {n} samples")
        placed = 0
        for samples, rtp, gap_samples, label_source in held:
            self._label_source = label_source
            placed += self._write_samples_inner(samples, rtp, gap_samples)
        return placed

    def _drop_held(self, why: str) -> None:
        """Count whatever is still held as unplaced (no pair came)."""
        if self._held_n:
            logger.warning(
                f"{self.config.channel_name}: {self._held_n} held samples "
                f"never got a pair ({why}); counted in unplaced_samples")
        self.unplaced_samples += self._held_n
        self._held, self._held_n = _deque(), 0
        self._hold_active = False
        self._step_request = None

    def _label_fault_log(self, message: str) -> None:
        """Rate-limited WARNING for refused samples; the counters hold the totals."""
        now = time.monotonic()
        if now - self._last_label_fault_log > 10.0:
            logger.warning(message)
            self._last_label_fault_log = now
    
    # Split-detector trigger (spec §6, replacing the old one-sided
    # wallclock staleness guard): when |wallclock - label| exceeds this,
    # the detector classifies the discrepancy — it does NOT drop on the
    # threshold alone.  Both signs are watched (future-dated labels are
    # just as suspect as stale ones).
    MAX_STALENESS_SECONDS = 120.0

    # Split-detector observation window and classification thresholds.
    SPLIT_WINDOW_SECONDS = 30.0    # history span for d(lag)/dt + arrival rate
    SPLIT_MIN_SPAN_SECONDS = 5.0   # min evidence span before classifying
    SPLIT_BACKLOG_DLAG_MIN = 0.5   # d(lag)/dt above this ⇒ lag is growing
    SPLIT_BACKLOG_ARRIVAL_MAX = 0.9  # arrival rate below this ⇒ sub-real-time

    def _split_detector_should_drop(
        self,
        wallclock_now: float,
        lag: float,
        rtp_timestamp: int,
        sample_unix_time: float,
    ) -> bool:
        """Spec §6 split detector.  Returns True ⇒ drop (BACKLOG only).

        Observables at the drop site (both lag signs watched):
          d(lag)/dt ≈ 1 s/s and arrival < 1× real-time  ⇒ BACKLOG:
              the pipeline is genuinely behind — shed load (drop),
              CRITICAL log.  This is the only timing-adjacent drop that
              remains legitimate.
          d(lag)/dt ≈ 0 and arrival ≈ 1× real-time      ⇒ ANCHOR FAULT:
              radiod's epoch (or an uncorrected mapping) is wrong but
              data flows at real-time — KEEP the samples (the judge's
              offset corrects the label; absent a judge the label
              pedigree is degraded, not the data), CRITICAL log +
              judge violation flag.
          insufficient evidence                          ⇒ KEEP
              (never drop on a bare threshold).
        """
        # Maintain unwrapped RTP progression for the arrival-rate leg.
        if self._last_raw_rtp is not None:
            delta = int((rtp_timestamp - self._last_raw_rtp) & 0xFFFFFFFF)
            if delta > 0x7FFFFFFF:
                delta -= 0x100000000
            self._unwrapped_rtp += delta
        self._last_raw_rtp = rtp_timestamp

        # Sample the observation window at most once per second.
        if wallclock_now - self._lag_last_append >= 1.0 or not self._lag_window:
            self._lag_window.append((wallclock_now, lag, self._unwrapped_rtp))
            self._lag_last_append = wallclock_now
            while (self._lag_window
                   and wallclock_now - self._lag_window[0][0] > self.SPLIT_WINDOW_SECONDS):
                self._lag_window.popleft()

        if abs(lag) <= self.MAX_STALENESS_SECONDS:
            return False

        # Classify.  Need enough evidence span first — until then, keep
        # the data (a wrong label is recoverable; a dropped sample is not).
        w0, lag0, rtp0 = self._lag_window[0]
        span = wallclock_now - w0
        if span < self.SPLIT_MIN_SPAN_SECONDS or len(self._lag_window) < 3:
            self._split_log(
                wallclock_now,
                f"{self.config.channel_name}: label {lag:+.1f}s vs wallclock "
                f"(limit {self.MAX_STALENESS_SECONDS}s) — gathering evidence "
                f"({span:.1f}s span) before classifying; KEEPING data."
            )
            return False

        dlag_dt = (lag - lag0) / span
        arrival_rate = ((self._unwrapped_rtp - rtp0) / self.config.sample_rate) / span

        backlog = (
            lag > 0
            and dlag_dt > self.SPLIT_BACKLOG_DLAG_MIN
            and arrival_rate < self.SPLIT_BACKLOG_ARRIVAL_MAX
        )
        if backlog:
            self._split_log(
                wallclock_now,
                f"{self.config.channel_name}: PIPELINE BACKLOG — DROPPING data. "
                f"lag={lag:+.1f}s growing at {dlag_dt:+.2f} s/s, arrival rate "
                f"{arrival_rate:.2f}x real-time. Load shedding is legitimate; "
                f"this is NOT an anchor fault."
            )
            return True

        # Anchor fault (constant lag at real-time arrival) or ambiguous:
        # never drop.  Flag the judge so offset_judge.json shows the
        # violation (spec §9 step 1).
        self.anchor_fault_events += 1
        self._split_log(
            wallclock_now,
            f"{self.config.channel_name}: ANCHOR FAULT signature — "
            f"lag={lag:+.1f}s (d(lag)/dt={dlag_dt:+.2f} s/s, arrival "
            f"{arrival_rate:.2f}x real-time). KEEPING data: the offset judge "
            f"corrects the label (or stamps the degraded pedigree). "
            f"radiod's advertised epoch is suspect."
        )
        judge, key = self._offset_judge, self._judge_source_key
        if judge is not None and key is not None:
            try:
                judge.flag_anchor_fault(key, lag)
            except Exception:  # noqa: BLE001 — never disturb recording
                pass
        return False

    def _split_log(self, wallclock_now: float, message: str) -> None:
        """Rate-limited CRITICAL logging for the split detector."""
        if wallclock_now - self._last_split_log > 10.0:
            logger.critical(message)
            self._last_split_log = wallclock_now

    def _write_samples_inner(
        self,
        samples: np.ndarray,
        rtp_timestamp: int,
        gap_samples: int = 0,
        fill_mask: Optional[np.ndarray] = None,
    ) -> int:
        """Write samples to the buffer (called with lock held, offset calibrated).

        ``fill_mask`` marks the zero-filled positions of ``samples`` and
        is passed only when this call writes the remainder of a batch
        split at a chunk end: the mask tells this chunk which share of the
        gap fill it holds.
        """
        # Ensure complex64
        if samples.dtype != np.complex64:
            samples = samples.astype(np.complex64)
        
        # ka9q-python fills gaps with exact zeros; the archive keeps them and
        # marks where they lie (fill_runs).  Only scan for gaps if the stream told us it inserted some.  The
        # zero mask, taken first, says where the fill lies, so a batch split
        # across two chunks gives each chunk its own share of the gap.
        # Ruling F1: a received sample that is exactly zero, inside a batch
        # with a reported gap, counts as fill too.  GRAPE counts it as lost.
        if fill_mask is None and gap_samples > 0:
            fill_mask = samples == 0
        
        # Determine which chunk this belongs to FROM RTP TIMESTAMP (GPSDO-disciplined)
        # This avoids wall clock jitter from NTP/chrony adjustments.
        # When the Offset Judge has a verdict for this source, the label
        # is the radiod mapping PLUS the judge's offset (spec §3:
        # label(rtp) = UTC_radiod(rtp) + offset).  Judge absent ⇒ raw
        # mapping, exactly the pre-judge behavior.
        verdict = self._judge_verdict(rtp_timestamp)
        offset_s = self._label_correction_s(verdict)
        sample_unix_time = self._rtp_to_unix_time(rtp_timestamp) + offset_s
        sample_minute = (int(sample_unix_time) // self.file_duration_sec) * self.file_duration_sec

        # Split detector (spec §6, replacing the old staleness guard):
        # classifies a large wallclock-vs-label discrepancy as pipeline
        # BACKLOG (drop — legitimate load shedding) or ANCHOR FAULT
        # (KEEP — the judge's offset already corrects the label; samples
        # are never dropped for timing reasons).
        wallclock_now = time.time()
        lag = wallclock_now - sample_unix_time
        if self._split_detector_should_drop(
                wallclock_now, lag, rtp_timestamp, sample_unix_time):
            self.stale_drops += len(samples)
            return 0

        # Start new buffer if needed
        if self.current_buffer is None:
            self.current_buffer = self._start_new_minute(
                sample_unix_time, rtp_timestamp, verdict=verdict)

        # Check if we've crossed into a new minute
        if sample_minute > self.current_buffer.minute_boundary:
            # Hand off to async worker; _try_flush always returns True
            # (either enqueued or abandoned on queue overflow).
            self._try_flush(self.current_buffer)
            self.current_buffer = self._start_new_minute(
                sample_unix_time, rtp_timestamp, verdict=verdict)
        
        # Write to buffer at correct position based on RTP timestamp
        # In RTP mode, samples are positioned by their RTP offset from minute boundary
        buffer = self.current_buffer
        source = getattr(self, '_label_source', 'first_sample')
        if buffer.batch_label is None:
            buffer.batch_label = source
        elif buffer.batch_label != source:
            buffer.batch_label = 'mixed'
        
        # Calculate position in buffer DIRECTLY from RTP timestamp
        # This is authoritative - RTP is GPSDO-disciplined
        # Handle 32-bit RTP wrap-around
        rtp_delta = int((rtp_timestamp - buffer.start_rtp) & 0xFFFFFFFF)
        if rtp_delta > 0x7FFFFFFF:
            rtp_delta -= 0x100000000
        sample_position = rtp_delta
        
        # Clamp to valid range
        if sample_position < 0:
            # Samples before the chunk start.  The writer never reopens an
            # earlier chunk (its file may be on disk already), so they are
            # dropped, but counted: a backward label (ka9q-python re-bases
            # after a backward RTP jump) or a mapping nudge at the seam.
            skip_count = min(-sample_position, len(samples))
            buffer.dropped_before_start += skip_count
            self.dropped_before_start += skip_count
            self._label_fault_log(
                f"{self.config.channel_name}: {skip_count} samples labelled "
                f"before chunk {buffer.minute_boundary}'s start; dropped "
                f"(counted in dropped_before_start)")
            if skip_count >= len(samples):
                return 0
            samples = samples[skip_count:]
            if fill_mask is not None:
                fill_mask = fill_mask[skip_count:]
            sample_position = 0
        
        samples_to_write = max(0, min(len(samples), self.samples_per_chunk - sample_position))

        if samples_to_write > 0:
            # Never overwrite a sample already written: write only the
            # positions still empty and count the rest.  Batches arrive in
            # order, so this is one free span in the normal case.
            end = sample_position + samples_to_write
            overlap = samples_to_write
            for a, b in buffer.free_spans(sample_position, end):
                buffer.samples[a:b] = samples[a - sample_position:b - sample_position]
                buffer.note_written(a, b)
                if fill_mask is not None:
                    self.fill_samples += buffer.note_fill(
                        a, fill_mask[a - sample_position:b - sample_position])
                overlap -= b - a
                self.samples_written += b - a
            if overlap:
                buffer.overlap_samples += overlap
                self.overlap_samples += overlap
                self._label_fault_log(
                    f"{self.config.channel_name}: {overlap} samples labelled "
                    f"onto positions chunk {buffer.minute_boundary} already "
                    f"holds; kept the earlier samples (overlap_samples)")
            # Update write_pos to track highest written position
            buffer.write_pos = max(buffer.write_pos, end)
        
        # Track gaps: this chunk's share of the fill
        if gap_samples > 0:
            here = gap_samples
            if fill_mask is not None and len(samples) > samples_to_write:
                n_fill = int(np.count_nonzero(fill_mask))
                if n_fill:
                    here = int(round(gap_samples * int(np.count_nonzero(
                        fill_mask[:samples_to_write])) / n_fill))
            if here > 0:
                buffer.gap_count += 1
                buffer.gap_samples += here
                self.total_gaps += 1
            gap_samples -= here

        # A batch that runs past the chunk end: its head went into this
        # chunk, the remainder belongs at the start of the next.  The
        # writer used to drop the remainder, so each chunk opened with a
        # zero run as long as the straddling batch's tail.
        remainder = samples[samples_to_write:]
        next_boundary = buffer.minute_boundary + self.file_duration_sec

        # Check if minute is complete
        if buffer.is_complete or len(remainder):
            self._try_flush(buffer)
            self.current_buffer = None

        if len(remainder):
            # The remainder's first sample sits at position samples_per_chunk
            # of this chunk, which is the next chunk's boundary by
            # definition.  Open that chunk by its boundary rather than by
            # re-deriving time from the RTP label, so rounding in the
            # mapping cannot reopen this chunk.
            rest_rtp = (buffer.start_rtp + sample_position + samples_to_write) & 0xFFFFFFFF
            self.current_buffer = self._start_new_minute(
                float(next_boundary), rest_rtp, verdict=verdict)
            rest_mask = None
            if fill_mask is not None:
                rest_mask = fill_mask[samples_to_write:]
            elif gap_samples > 0:
                rest_mask = np.zeros(len(remainder), dtype=bool)
            samples_to_write += self._write_samples_inner(
                remainder, rest_rtp, gap_samples, fill_mask=rest_mask)

        return samples_to_write

    def flush(self):
        """Enqueue any pending buffer + wait for the worker to drain.

        Use this when you need durability — e.g. on shutdown or before
        a downstream consumer must see the latest chunk.  In the steady
        state the worker drains asynchronously and you don't need to
        call this.
        """
        with self._lock:
            if self._hold_active:
                self._drop_held("flush")
            if self.current_buffer and self.current_buffer.write_pos > 0:
                self._try_flush(self.current_buffer)
                self.current_buffer = None
        # Block until the worker has processed everything enqueued so far.
        # join() returns when every put() has been task_done()-d, which is
        # how we know zstd+fsync has finished on the last buffer.
        self._flush_queue.join()

    def close(self):
        """Close the writer, flushing any pending data."""
        self.flush()
        # Give an in-flight watchdog capture (a diagnostic) ~2 s to finish.
        try:
            from . import radiod_timing_watchdog
            radiod_timing_watchdog.close_watchdog(timeout=2.0)
        except Exception:  # noqa: BLE001 -- never block or fail a close
            pass
        # Stop the worker — send sentinel, then signal stop so the
        # worker exits even if the queue is empty.
        self._flush_stop.set()
        try:
            self._flush_queue.put_nowait(None)
        except queue.Full:
            pass  # sentinel optional; the stop event covers it
        self._flush_thread.join(timeout=10.0)
        logger.info(
            f"BinaryArchiveWriter closed: {self.minutes_written} minutes, "
            f"{self.samples_written} samples, {self.write_errors} errors"
        )
    
    def get_stats(self) -> Dict[str, Any]:
        """Get writer statistics."""
        return {
            'channel_name': self.config.channel_name,
            'minutes_written': self.minutes_written,
            'samples_written': self.samples_written,
            'total_gaps': self.total_gaps,
            'write_errors': self.write_errors,
            'stale_drops': self.stale_drops,
            'timing_drops': self.timing_drops,
            'anchor_fault_events': self.anchor_fault_events,
            'label_discontinuities': self.label_discontinuities,
            'unplaced_samples': self.unplaced_samples,
            'overlap_samples': self.overlap_samples,
            'fill_samples': self.fill_samples,
            'dropped_before_start': self.dropped_before_start,
            'held_samples': self._held_n,
            'abandoned_chunks': self.abandoned_chunks,
            'abandoned_samples': self.abandoned_samples,
            'current_buffer_pos': self.current_buffer.write_pos if self.current_buffer else 0,
            'flush_queue_depth': self._flush_queue.qsize(),
        }

    def loss_report(self) -> Dict[str, Any]:
        """What this writer placed and what it lost, by cause (A18).

        ``samples_placed`` counts samples the writer put into a chunk
        buffer.  Samples of an abandoned chunk count in both
        ``samples_placed`` and ``abandoned_samples``: the buffer took them,
        and the flush queue then lost the whole chunk.  ``fill_samples``
        counts zeros stored for reported gaps; they sit inside
        ``samples_placed`` and are not a loss.  ``held_samples`` waits for
        a timing pair and is not yet a loss.  ``samples_lost`` sums
        ``lost_by_cause``.
        """
        lost = {
            'timing_drops': self.timing_drops,
            'stale_drops': self.stale_drops,
            'unplaced_samples': self.unplaced_samples,
            'overlap_samples': self.overlap_samples,
            'dropped_before_start': self.dropped_before_start,
            'abandoned_samples': self.abandoned_samples,
        }
        return {
            'samples_placed': self.samples_written,
            'fill_samples': self.fill_samples,
            'samples_lost': sum(lost.values()),
            'lost_by_cause': lost,
            'abandoned_chunks': self.abandoned_chunks,
            'write_errors': self.write_errors,
            'held_samples': self._held_n,
            'flush_queue_depth': self._flush_queue.qsize(),
        }


class BinaryArchiveReader:
    """
    Reader for binary archive files.

    Provides memory-mapped access for zero-copy reading by Phase 2.
    Handles both legacy 1-minute files and multi-minute chunk files.
    """

    def __init__(self, archive_dir: Path, channel_name: str):
        # Use channel_name_to_dir for consistent path format (preserves dots)
        from ..paths import channel_name_to_dir
        self.archive_dir = archive_dir / channel_name_to_dir(channel_name)
        self.channel_name = channel_name
        self.sample_rate = 20000
        self._warned_unreadable: set = set()
    
    def get_available_minutes(self, date_str: Optional[str] = None) -> List[int]:
        """Get list of available minute boundaries.

        A boundary counts when any of its segments is on disk (see
        chunk_segments)."""
        if date_str is None:
            date_str = datetime.now(timezone.utc).strftime('%Y%m%d')
        
        day_dir = self.archive_dir / date_str
        if not day_dir.exists():
            return []
        
        minutes = set()
        # Match both uncompressed and compressed files
        for bin_file in day_dir.glob('*.bin*'):
            parsed = parse_chunk_name(bin_file.name)
            if parsed is not None and parsed[2] != '.json':
                minutes.add(parsed[0])
        
        return sorted(minutes)
    
    def read_minute(self, minute_boundary: int) -> Optional[np.ndarray]:
        """Samples for one chunk as complex64 (see read_chunk)."""
        return self.read_chunk(minute_boundary)[0]

    def _day_dir(self, minute_boundary: int) -> Path:
        dt = datetime.fromtimestamp(minute_boundary, tz=timezone.utc)
        return self.archive_dir / dt.strftime('%Y%m%d')

    def _segments(self, minute_boundary: int) -> List[int]:
        """The segment numbers on disk at this boundary, a .bin* or a .json, in order."""
        day_dir = self._day_dir(minute_boundary)
        found = set()
        for f in day_dir.glob(f"{minute_boundary}*"):
            parsed = parse_chunk_name(f.name)
            if parsed is not None and parsed[0] == minute_boundary:
                found.add(parsed[1])
        return sorted(found)

    def read_chunk(self, minute_boundary: int) -> Tuple[Optional[np.ndarray], Optional[Dict]]:
        """
        Read one chunk: (samples, sidecar).

        Samples come back as complex64 at the original amplitude.  A .bin.zst
        chunk decodes by its sidecar's "dtype" and "scale" (F16 chunks divide
        the fixed scale back out); .bin and .bin.lz4 are always complex64.
        The sidecar's sample_rate, when present, replaces self.sample_rate.

        A chunk reopened at its boundary has later segments,
        ``<boundary>.seg<N>``.  They are joined by position, the earlier
        segment kept where two wrote the same place, and the sidecar comes
        back with a ``segments`` list (chunk_segments.join_segments).  A
        segment that cannot be read is left out; with none readable the
        chunk reads as missing.
        """
        day_dir = self._day_dir(minute_boundary)
        joiner = SegmentJoiner()
        first_meta = None
        for n, segment in enumerate(self._segments(minute_boundary)):
            stem = chunk_stem(minute_boundary, segment)
            meta = self._read_sidecar(day_dir / f"{stem}.json")
            if n == 0:
                first_meta = meta
            samples = self._read_file(day_dir / stem, meta)
            if samples is not None:
                joiner.add(segment, samples, meta)
            del samples          # the joiner holds the joined chunk, nothing more
        samples, meta = joiner.result()
        if samples is None:
            return None, first_meta
        if meta and meta.get('sample_rate'):
            self.sample_rate = int(meta['sample_rate'])
        return samples, meta

    def _read_file(self, base_path: Path, meta: Optional[Dict]) -> Optional[np.ndarray]:
        """One chunk file's samples, by its own sidecar, or None."""
        # Try uncompressed first (fastest - memory-mappable)
        bin_path = Path(f"{base_path}.bin")
        if bin_path.exists():
            mm = np.memmap(bin_path, dtype=np.complex64, mode='r')
            arr = np.array(mm)
            del mm
            return arr

        # Try zstd compressed
        zst_path = Path(f"{base_path}.bin.zst")
        if zst_path.exists():
            # Only the sidecar names a .bin.zst chunk's dtype.  Without it,
            # or with an F16 sidecar that lacks its scale, the chunk counts
            # as missing: guessing complex64 would reinterpret half-floats.
            if meta is None:
                self._warn_unreadable(zst_path, "has no JSON sidecar")
                return None
            dtype = meta.get('dtype', DTYPE_F32)
            if dtype == DTYPE_F16 and 'scale' not in meta:
                self._warn_unreadable(zst_path, "has a complex-f16 sidecar without 'scale'")
                return None
            try:
                import zstandard as zstd
                with open(zst_path, 'rb') as f:
                    dctx = zstd.ZstdDecompressor()
                    decompressed = dctx.decompress(f.read())
                return decode_iq(decompressed, dtype, meta.get('scale', 1.0))
            except ImportError:
                logger.warning("zstandard not installed, cannot read .bin.zst files")
                return None

        # Try lz4 compressed
        lz4_path = Path(f"{base_path}.bin.lz4")
        if lz4_path.exists():
            try:
                import lz4.frame
                with open(lz4_path, 'rb') as f:
                    decompressed = lz4.frame.decompress(f.read())
                return np.frombuffer(decompressed, dtype=np.complex64)
            except ImportError:
                logger.warning("lz4 not installed, cannot read .bin.lz4 files")
                return None

        return None

    def _warn_unreadable(self, path: Path, why: str) -> None:
        """Warn once per file that a chunk cannot be decoded."""
        if path in self._warned_unreadable:
            return
        self._warned_unreadable.add(path)
        logger.warning(f"Skipping {path}: it {why}; treating the chunk as missing")

    @staticmethod
    def _read_sidecar(json_path: Path) -> Optional[Dict]:
        if not json_path.exists():
            return None
        try:
            with open(json_path) as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            logger.warning(f"Corrupted metadata file {json_path}: {e}")
            return None

    def read_metadata(self, minute_boundary: int) -> Optional[Dict]:
        """Read metadata for a specific minute.

        With later segments on disk, the earliest segment's sidecar plus a
        ``segments`` list (chunk_segments.join_metadata).  read_chunk's
        sidecar adds what it took from each segment."""
        day_dir = self._day_dir(minute_boundary)
        return join_metadata([
            (segment, self._read_sidecar(day_dir / f"{chunk_stem(minute_boundary, segment)}.json"))
            for segment in self._segments(minute_boundary)])
    
    def get_latest_complete_minute(self) -> Optional[int]:
        """Get the most recent complete minute boundary."""
        # Scan available minutes and return second-to-last (last might be incomplete)
        # This avoids using wall clock time
        minutes = self.get_available_minutes()
        if minutes:
            return minutes[-2] if len(minutes) > 1 else minutes[-1]
        return None
