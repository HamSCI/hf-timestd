"""Cross-process shared-memory ring buffer — producer side.

Phase 1 of the ring-buffer refactor.  One writer (the core-recorder) writes
IQ samples into a per-channel SysV shared-memory segment so that multiple
readers (metrology workers, the archive writer, CHU FSK, future clients)
can consume the same stream without coupling to file I/O cadence.

Protocol summary
----------------
- One segment per channel, keyed deterministically from the channel name.
- Fixed 4 KiB header followed by a `complex64` sample region of length
  `sample_rate * ring_seconds`.
- The producer is the only writer.  Readers never block the producer.
- The producer publishes each batch by:
    1. Copying samples into the ring at `write_cursor % ring_size`.
    2. Publishing the per-batch RTP anchor (`batch_first_rtp`,
       `batch_cursor_pos`) so readers can map any sample index to an RTP
       timestamp.
    3. Performing a single atomic 8-byte store of the new
       `write_cursor_samples`.  On x86-64 this is the release point.
- The GPS_TIME / RTP_TIMESNAP anchor is seqlock-protected by
  `epoch_counter` and `anchor_epoch_counter_mirror`.  Producer bumps
  epoch_counter on every re-seed; readers retry while the two disagree.
- Platform gate: the seqlock assumes TSO memory ordering, so creation
  refuses non-x86 hosts.  aarch64 support would require an explicit
  release fence (deferred).

The full plan, including phasing and rollback notes, lives at
`~/.claude/plans/silly-squishing-parnas.md`.
"""

from __future__ import annotations

import hashlib
import logging
import os
import platform
import struct
import threading
import time
from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np

try:
    import sysv_ipc
except ImportError as exc:  # pragma: no cover - sysv_ipc is a hard dep
    raise ImportError(
        "hf_timestd.core.ring_buffer requires sysv_ipc "
        "(already declared in pyproject.toml)"
    ) from exc

logger = logging.getLogger(__name__)


# ─── header layout ──────────────────────────────────────────────────────────
HEADER_MAGIC = b"TRB1"
HEADER_VERSION = 1
HEADER_SIZE = 4096  # bytes, 4 KiB aligned; sample region begins here
CHANNEL_NAME_LEN = 64

DTYPE_COMPLEX64 = 1  # dtype_code
_BYTES_PER_SAMPLE = 8  # complex64

# Static header, packed once at create time and parsed at attach time.
# Layout: 4s magic | I version | 64s name | I sample_rate | I dtype_code
#         | Q ring_size_samples | Q header_size_bytes
_STATIC_HEADER_FMT = "<4sI64sIIQQ"
_STATIC_HEADER_SIZE = struct.calcsize(_STATIC_HEADER_FMT)
assert _STATIC_HEADER_SIZE <= 256, "static header outgrew its 256B budget"

# Hot fields live in a uint64 numpy view starting at _HOT_OFFSET.
# The gap between the static header and the hot region leaves room for
# future read-only fields without touching cache lines readers care about.
_HOT_OFFSET = 256
_HOT_COUNT = 32  # 256 bytes reserved for hot state
_HOT_REGION_END = _HOT_OFFSET + _HOT_COUNT * 8
assert _HOT_REGION_END <= HEADER_SIZE

# Indices into the uint64 hot view.  Keep these dense and do NOT renumber:
# the layout is the wire format.
HOT_PRODUCER_PID      = 0
HOT_HEARTBEAT_NS      = 1   # CLOCK_MONOTONIC, bumped every batch
HOT_PRODUCER_START_NS = 2   # CLOCK_REALTIME at create
HOT_EPOCH_COUNTER     = 3   # bumped on every anchor re-seed (0 = invalid)
HOT_WRITE_CURSOR      = 4   # monotonic sample count; release point
HOT_BATCH_FIRST_RTP   = 5   # RTP of first sample of most recent batch
HOT_BATCH_CURSOR_POS  = 6   # write_cursor at the start of that batch
HOT_ANCHOR_GPS_NS     = 7   # GPS_TIME ns (as i64 reinterpreted)
HOT_ANCHOR_RTP_SNAP   = 8   # RTP counter at GPS_TIME (low 32 bits used)
HOT_EPOCH_MIRROR      = 9   # equals HOT_EPOCH_COUNTER when anchor valid
HOT_TOTAL_GAP_SAMPLES = 10  # cumulative gap samples (informational)
HOT_LAST_BATCH_SIZE   = 11  # last batch sample count (informational)
HOT_QUALITY_FLAGS     = 12  # reserved for per-batch quality flags

_SAMPLE_REGION_OFFSET = HEADER_SIZE


# ─── errors ────────────────────────────────────────────────────────────────
class RingBufferError(Exception):
    """Base class for ring-buffer failures."""


class RingBufferOverrunError(RingBufferError):
    """The producer lapped a reader; the requested window has been overwritten."""


class RingBufferIncompatibleError(RingBufferError):
    """An existing segment has incompatible magic/version/shape."""


class RingBufferOwnershipError(RingBufferError):
    """A foreign-owned SysV segment blocks our ring key and cannot be reclaimed.

    SysV shm removal requires the owner uid (or root); group membership does
    NOT grant it.  So a segment created by another user (e.g. a stale ``radio``
    segment at an hf-timestd ring key) is a permanent landmine: the producer
    cannot remove it to recreate a correctly-sized ring, and the metrology
    consumer silently starves.  Raised so the caller alarms instead.  Cleared
    by ``hf-timestd clean-stale-rings`` run as root (recorder ExecStartPre).
    """


# ─── helpers ────────────────────────────────────────────────────────────────
def _require_x86() -> None:
    """Refuse to run on platforms whose memory model breaks the seqlock."""
    mach = platform.machine().lower()
    if mach not in ("x86_64", "amd64"):
        raise RuntimeError(
            f"ring_buffer: seqlock protocol requires x86-64 TSO memory model "
            f"(got platform.machine()={mach!r}); aarch64 support is deferred"
        )


def ring_key_for_channel(channel_name: str) -> int:
    """Deterministic 31-bit SysV key for a channel's ring segment.

    SHA-256 of the channel name, truncated to 31 bits so the key stays
    within the positive range of Linux's ``key_t``.
    """
    h = hashlib.sha256(f"hf-timestd:ring:{channel_name}".encode("utf-8")).digest()
    return int.from_bytes(h[:4], "big") & 0x7FFFFFFF


def segment_size_bytes(ring_size_samples: int) -> int:
    """Total segment size for a given sample region length."""
    return HEADER_SIZE + ring_size_samples * _BYTES_PER_SAMPLE


# ─── config ────────────────────────────────────────────────────────────────
@dataclass
class RingBufferConfig:
    channel_name: str
    sample_rate: int
    ring_seconds: int
    mode: int = 0o660


# ─── producer ──────────────────────────────────────────────────────────────
class RingBuffer:
    """Single-producer shared-memory ring buffer.

    Typical use (from the recorder):

        rb = RingBuffer.create("WWV_20000", sample_rate=24000, ring_seconds=600)
        rb.update_anchor(channel_info.gps_time, channel_info.rtp_timesnap)
        ...
        rb.write_samples(batch_samples, batch_first_rtp)
        ...
        rb.destroy()

    All mutating methods are safe from a single producer thread.  `destroy()`
    removes the SysV segment — callers must not touch the buffer afterward.
    """

    def __init__(self, config: RingBufferConfig, shm, created: bool) -> None:
        self._config = config
        self._shm = shm
        self._created_by_us = created
        self._lock = threading.Lock()
        self._closed = False

        ring_size = config.sample_rate * config.ring_seconds
        self._ring_size_samples = ring_size

        # Numpy views share the same underlying memory via the buffer
        # protocol.  Dropping both references in destroy() is what releases
        # the mapping so shm.detach() is safe.
        self._hot = np.frombuffer(
            shm, dtype=np.uint64, count=_HOT_COUNT, offset=_HOT_OFFSET
        )
        self._samples = np.frombuffer(
            shm,
            dtype=np.complex64,
            count=ring_size,
            offset=_SAMPLE_REGION_OFFSET,
        )

        # Internal producer bookkeeping — NOT shared with readers.
        self._expected_next_rtp: Optional[int] = None
        self._first_write_done = False

    # ─── lifecycle ─────────────────────────────────────────────────────
    @classmethod
    def create(
        cls,
        channel_name: str,
        sample_rate: int,
        ring_seconds: int,
        mode: int = 0o660,
    ) -> "RingBuffer":
        """Create (or adopt a compatible) ring segment for ``channel_name``.

        If a pre-existing segment has the wrong size, magic, version, or
        sample-rate shape, it is destroyed and recreated — stale consumers
        attached to it will see an epoch/cursor inversion and resync via
        ``RingBufferOverrunError``.
        """
        _require_x86()
        if sample_rate <= 0:
            raise ValueError(f"sample_rate={sample_rate} must be positive")
        if ring_seconds <= 0:
            raise ValueError(f"ring_seconds={ring_seconds} must be positive")

        ring_size_samples = sample_rate * ring_seconds
        seg_size = segment_size_bytes(ring_size_samples)
        key = ring_key_for_channel(channel_name)

        shm, created = cls._open_or_recreate(key, seg_size, mode)

        if created:
            cls._write_static_header(
                shm, channel_name, sample_rate, ring_size_samples
            )
        else:
            try:
                cls._verify_static_header(
                    shm, channel_name, sample_rate, ring_size_samples
                )
            except RingBufferIncompatibleError as exc:
                logger.warning(
                    f"RingBuffer[{channel_name}]: adopted segment incompatible "
                    f"({exc}); destroying and recreating"
                )
                shm.detach()
                shm.remove()
                shm = sysv_ipc.SharedMemory(
                    key,
                    flags=sysv_ipc.IPC_CREAT | sysv_ipc.IPC_EXCL,
                    size=seg_size,
                    mode=mode,
                )
                created = True
                cls._write_static_header(
                    shm, channel_name, sample_rate, ring_size_samples
                )

        config = RingBufferConfig(
            channel_name=channel_name,
            sample_rate=sample_rate,
            ring_seconds=ring_seconds,
            mode=mode,
        )
        rb = cls(config, shm, created=created)
        if created:
            rb._init_hot_fields()

        logger.info(
            f"RingBuffer[{channel_name}]: SysV key=0x{key:08x} "
            f"size={seg_size / 1024 / 1024:.1f} MiB "
            f"({ring_seconds}s @ {sample_rate} Hz) "
            f"[{'created' if created else 'adopted'}]"
        )
        return rb

    @staticmethod
    def _open_or_recreate(key: int, size: int, mode: int):
        """Open segment, handling size-mismatch and stale-segment recovery."""
        for _attempt in range(2):
            try:
                shm = sysv_ipc.SharedMemory(
                    key,
                    flags=sysv_ipc.IPC_CREAT | sysv_ipc.IPC_EXCL,
                    size=size,
                    mode=mode,
                )
                return shm, True
            except sysv_ipc.ExistentialError:
                existing = sysv_ipc.SharedMemory(key, flags=0)
                if existing.size == size:
                    return existing, False
                # Size mismatch: must recreate.  We can only remove a segment
                # we own (or as root) — group membership does NOT grant removal.
                my_uid = os.getuid()
                can_remove = (my_uid == 0) or (existing.uid == my_uid)
                if not can_remove:
                    raise RingBufferOwnershipError(
                        f"SysV ring key=0x{key:08x} exists with size "
                        f"{existing.size} != expected {size} and is owned by "
                        f"uid={existing.uid} (we are uid={my_uid}); cannot "
                        f"reclaim a foreign-owned segment. Remove it as root "
                        f"(`hf-timestd clean-stale-rings`) before recreating."
                    )
                logger.warning(
                    f"RingBuffer: SysV key=0x{key:08x} exists with size "
                    f"{existing.size} != expected {size}; removing stale segment"
                )
                existing.detach()
                try:
                    existing.remove()
                except (sysv_ipc.PermissionsError, OSError) as exc:
                    raise RingBufferOwnershipError(
                        f"SysV ring key=0x{key:08x} (owner uid={existing.uid}) "
                        f"could not be removed: {exc}. Remove it as root "
                        f"(`hf-timestd clean-stale-rings`) before recreating."
                    ) from exc
        raise RingBufferError(
            f"RingBuffer: unable to create segment key=0x{key:08x} after retry"
        )

    @staticmethod
    def _write_static_header(
        shm,
        channel_name: str,
        sample_rate: int,
        ring_size_samples: int,
    ) -> None:
        name_bytes = channel_name.encode("utf-8")[: CHANNEL_NAME_LEN - 1]
        name_padded = name_bytes.ljust(CHANNEL_NAME_LEN, b"\x00")
        packed = struct.pack(
            _STATIC_HEADER_FMT,
            HEADER_MAGIC,
            HEADER_VERSION,
            name_padded,
            sample_rate,
            DTYPE_COMPLEX64,
            ring_size_samples,
            HEADER_SIZE,
        )
        shm.write(packed, 0)

    @staticmethod
    def _verify_static_header(
        shm,
        channel_name: str,
        sample_rate: int,
        ring_size_samples: int,
    ) -> None:
        raw = shm.read(_STATIC_HEADER_SIZE, 0)
        (
            magic,
            version,
            name_padded,
            sr,
            dtype_code,
            ring_size,
            hdr_size,
        ) = struct.unpack(_STATIC_HEADER_FMT, raw)
        name = name_padded.rstrip(b"\x00").decode("utf-8", errors="replace")
        if magic != HEADER_MAGIC:
            raise RingBufferIncompatibleError(
                f"magic mismatch: got {magic!r} expected {HEADER_MAGIC!r}"
            )
        if version != HEADER_VERSION:
            raise RingBufferIncompatibleError(
                f"version mismatch: got {version} expected {HEADER_VERSION}"
            )
        if sr != sample_rate:
            raise RingBufferIncompatibleError(
                f"sample_rate mismatch: segment={sr} expected={sample_rate}"
            )
        if dtype_code != DTYPE_COMPLEX64:
            raise RingBufferIncompatibleError(
                f"dtype mismatch: segment={dtype_code} expected={DTYPE_COMPLEX64}"
            )
        if ring_size != ring_size_samples:
            raise RingBufferIncompatibleError(
                f"ring size mismatch: segment={ring_size} expected={ring_size_samples}"
            )
        if hdr_size != HEADER_SIZE:
            raise RingBufferIncompatibleError(
                f"header size mismatch: segment={hdr_size} expected={HEADER_SIZE}"
            )
        if name != channel_name:
            raise RingBufferIncompatibleError(
                f"channel name mismatch: segment={name!r} expected={channel_name!r}"
            )

    def _init_hot_fields(self) -> None:
        """Zero the hot region and stamp producer identity (create path only)."""
        self._hot[:] = 0
        self._hot[HOT_PRODUCER_PID] = os.getpid()
        self._hot[HOT_PRODUCER_START_NS] = time.time_ns()
        self._hot[HOT_HEARTBEAT_NS] = time.monotonic_ns()
        # Epoch 0 is the "anchor invalidated" sentinel; start at 1.
        self._hot[HOT_EPOCH_COUNTER] = 1
        self._hot[HOT_EPOCH_MIRROR] = 1

    # ─── producer API ──────────────────────────────────────────────────
    def write_samples(
        self,
        samples: np.ndarray,
        batch_first_rtp: int,
    ) -> None:
        """Copy one batch into the ring.

        ``batch_first_rtp`` is the 32-bit unsigned RTP timestamp of the
        first sample of this batch, as produced by radiod.  The ring uses
        it to keep its (cursor ↔ RTP) relation published per-batch so
        readers can map any still-in-window sample index back to UTC.
        """
        if self._closed:
            return
        n = int(samples.shape[0])
        if n == 0:
            return
        if samples.dtype != np.complex64:
            samples = samples.astype(np.complex64, copy=False)

        ring_size = self._ring_size_samples
        if n > ring_size:
            # Pathological: batch larger than the whole ring.  Keep only
            # the most recent `ring_size` samples so the ring stays
            # internally consistent; warn loudly.
            logger.error(
                f"RingBuffer[{self._config.channel_name}]: batch of {n} samples "
                f"exceeds ring size {ring_size}; truncating"
            )
            samples = samples[-ring_size:]
            n = ring_size

        with self._lock:
            if self._closed:
                return
            cursor = int(self._hot[HOT_WRITE_CURSOR])
            start = cursor % ring_size

            if start + n <= ring_size:
                np.copyto(self._samples[start : start + n], samples)
            else:
                first = ring_size - start
                np.copyto(self._samples[start:], samples[:first])
                np.copyto(self._samples[: n - first], samples[first:])

            # Detect radiod restart / resequencer reset via an RTP jump
            # that doesn't match our running expectation.  We only warn;
            # anchor re-seeding is the caller's responsibility via
            # update_anchor() (StreamRecorderV2 calls it from _create_channel
            # after every recovery).
            if (
                self._expected_next_rtp is not None
                and (batch_first_rtp & 0xFFFFFFFF)
                != (self._expected_next_rtp & 0xFFFFFFFF)
            ):
                logger.warning(
                    f"RingBuffer[{self._config.channel_name}]: RTP discontinuity "
                    f"expected={self._expected_next_rtp & 0xFFFFFFFF} "
                    f"got={batch_first_rtp & 0xFFFFFFFF}"
                )
            self._expected_next_rtp = (batch_first_rtp + n) & 0xFFFFFFFF

            # Publish per-batch RTP anchor BEFORE the cursor so readers
            # that observe the new cursor value also see the matching
            # batch_first_rtp / batch_cursor_pos.  On x86-64, CPython's
            # single-threaded producer cannot reorder these stores past
            # the final cursor store.
            self._hot[HOT_BATCH_FIRST_RTP] = batch_first_rtp & 0xFFFFFFFF
            self._hot[HOT_BATCH_CURSOR_POS] = cursor
            self._hot[HOT_LAST_BATCH_SIZE] = n

            # Release: single 8-byte atomic store of the new cursor.
            self._hot[HOT_WRITE_CURSOR] = cursor + n

            # Heartbeat — informational, no synchronization required.
            self._hot[HOT_HEARTBEAT_NS] = time.monotonic_ns()
            self._first_write_done = True

    def update_anchor(self, gps_time_ns: int, rtp_timesnap: int) -> None:
        """Install a fresh (GPS_TIME, RTP_TIMESNAP) pair via seqlock.

        Called from the recorder whenever a new authoritative timing
        snapshot arrives (channel creation, radiod restart recovery).
        The update sequence is:

            1. mirror ← 0             (invalidate: readers will retry)
            2. anchor fields ← new
            3. epoch_counter ← epoch_counter + 1
            4. mirror ← epoch_counter (anchor valid again)

        Readers loop until ``epoch_counter == mirror`` and both are non-zero.
        """
        if self._closed:
            return
        with self._lock:
            if self._closed:
                return
            # Step 1: invalidate
            self._hot[HOT_EPOCH_MIRROR] = 0
            # Step 2: publish new anchor values
            # gps_time_ns is signed ns since GPS epoch — store as uint64
            # bit pattern (negative values would be far future anyway).
            self._hot[HOT_ANCHOR_GPS_NS] = gps_time_ns & 0xFFFFFFFFFFFFFFFF
            self._hot[HOT_ANCHOR_RTP_SNAP] = rtp_timesnap & 0xFFFFFFFF
            # Step 3: bump epoch (skip zero)
            new_epoch = int(self._hot[HOT_EPOCH_COUNTER]) + 1
            if new_epoch == 0:
                new_epoch = 1
            self._hot[HOT_EPOCH_COUNTER] = new_epoch
            # Step 4: restore mirror
            self._hot[HOT_EPOCH_MIRROR] = new_epoch
            logger.debug(
                f"RingBuffer[{self._config.channel_name}]: anchor updated "
                f"gps_ns={gps_time_ns} rtp_snap={rtp_timesnap} epoch={new_epoch}"
            )

    def bump_heartbeat(self) -> None:
        """Publish a fresh heartbeat without advancing the cursor.

        Used by supervision threads to distinguish "producer alive but
        idle" from "producer wedged" — readers that see stale heartbeats
        and an unchanging cursor should report the producer as dead.
        """
        if self._closed:
            return
        self._hot[HOT_HEARTBEAT_NS] = time.monotonic_ns()

    def record_gap(self, gap_samples: int) -> None:
        """Accumulate a gap-sample telemetry counter (informational)."""
        if self._closed or gap_samples <= 0:
            return
        self._hot[HOT_TOTAL_GAP_SAMPLES] = (
            int(self._hot[HOT_TOTAL_GAP_SAMPLES]) + int(gap_samples)
        )

    # ─── introspection ─────────────────────────────────────────────────
    @property
    def channel_name(self) -> str:
        return self._config.channel_name

    @property
    def sample_rate(self) -> int:
        return self._config.sample_rate

    @property
    def ring_seconds(self) -> int:
        return self._config.ring_seconds

    @property
    def ring_size_samples(self) -> int:
        return self._ring_size_samples

    @property
    def segment_key(self) -> int:
        return ring_key_for_channel(self._config.channel_name)

    def write_cursor(self) -> int:
        """Current monotonic write_cursor_samples (for tests / diagnostics)."""
        if self._closed:
            return 0
        return int(self._hot[HOT_WRITE_CURSOR])

    # ─── shutdown ──────────────────────────────────────────────────────
    def destroy(self) -> None:
        """Detach and remove the SysV segment."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            # Release numpy views BEFORE detaching so the buffer protocol
            # unmaps the memory cleanly.
            self._hot = None
            self._samples = None
        try:
            self._shm.detach()
        except Exception as exc:
            logger.warning(
                f"RingBuffer[{self._config.channel_name}]: detach failed: {exc}"
            )
        try:
            self._shm.remove()
        except Exception as exc:
            logger.warning(
                f"RingBuffer[{self._config.channel_name}]: remove failed: {exc}"
            )
        logger.info(f"RingBuffer[{self._config.channel_name}]: destroyed")
