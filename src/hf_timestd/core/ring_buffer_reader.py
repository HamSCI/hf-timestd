"""Cross-process shared-memory ring buffer — consumer side.

Attaches to a SysV segment created by ``RingBuffer`` and extracts
intervals of samples in terms of UTC or sample counts.  Safe under
concurrent writes from a single producer via the seqlock protocol
documented in ``ring_buffer``.

Readers never modify shared state.  Every extract method performs a
cursor-before / cursor-after comparison so the caller can distinguish
"data still valid" from "producer lapped the ring".
"""

from __future__ import annotations

import logging
import struct
import time
from typing import Optional, Tuple

import numpy as np

try:
    import sysv_ipc
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "hf_timestd.core.ring_buffer_reader requires sysv_ipc"
    ) from exc

from .leap_second import get_current_gps_leap_seconds
from .ring_buffer import (
    DTYPE_COMPLEX64,
    HEADER_MAGIC,
    HEADER_SIZE,
    HEADER_VERSION,
    HOT_ANCHOR_GPS_NS,
    HOT_ANCHOR_RTP_SNAP,
    HOT_BATCH_CURSOR_POS,
    HOT_BATCH_FIRST_RTP,
    HOT_EPOCH_COUNTER,
    HOT_EPOCH_MIRROR,
    HOT_HEARTBEAT_NS,
    HOT_PRODUCER_PID,
    HOT_WRITE_CURSOR,
    RingBufferError,
    RingBufferIncompatibleError,
    RingBufferOverrunError,
    _HOT_COUNT,
    _HOT_OFFSET,
    _SAMPLE_REGION_OFFSET,
    _STATIC_HEADER_FMT,
    _STATIC_HEADER_SIZE,
    _require_x86,
    ring_key_for_channel,
)

logger = logging.getLogger(__name__)

# GPS epoch (Unix seconds at 1980-01-06 00:00:00 UTC)
GPS_EPOCH_UNIX = 315964800
BILLION = 1_000_000_000

# Seqlock retry budget — the producer holds the anchor invalid for only
# four stores, so 128 retries is enormous slack.
_MAX_SEQLOCK_RETRIES = 128

# Safety margin for overrun detection.  When the producer's cursor
# advances enough that (w2 - s_start) > (ring_size - margin), the copy
# may be torn and we declare overrun.  The margin is set per-reader at
# attach time (see _compute_overrun_margin) so tiny rings used by unit
# tests do not trip overruns on every read, while production rings keep
# several radiod packets of slack.
_OVERRUN_MARGIN_MAX = 4096
_OVERRUN_MARGIN_MIN = 64


def _compute_overrun_margin(ring_size_samples: int) -> int:
    """Slack between s_start and eviction point; scales with ring size."""
    return max(_OVERRUN_MARGIN_MIN, min(_OVERRUN_MARGIN_MAX, ring_size_samples // 16))


def _rtp_delta_signed(rtp: int, rtp_start: int) -> int:
    """Signed 32-bit RTP delta (rtp - rtp_start)."""
    delta = (rtp - rtp_start) & 0xFFFFFFFF
    if delta > 0x7FFFFFFF:
        delta -= 0x100000000
    return delta


class RingBufferReader:
    """Attach to a producer's ring segment and copy out sample windows.

    Lifecycle::

        reader = RingBufferReader.attach("WWV_20000")
        cursor = reader.write_cursor()
        head_utc = reader.head_utc(cursor)
        samples, metadata = reader.extract_interval(utc_start, 60.0)
        reader.close()

    ``metadata`` is in the exact shape
    ``hf_timestd.core.buffer_timing.resolve_buffer_timing()`` already
    consumes — so downstream code (metrology, CHU FSK, etc.) can treat a
    ring-buffer extract the same as a decoded ``.bin`` file.
    """

    def __init__(
        self,
        channel_name: str,
        shm,
        sample_rate: int,
        ring_size_samples: int,
    ) -> None:
        self._channel_name = channel_name
        self._shm = shm
        self._sample_rate = int(sample_rate)
        self._ring_size_samples = int(ring_size_samples)
        self._leap = get_current_gps_leap_seconds()
        self._closed = False

        self._hot = np.frombuffer(
            shm, dtype=np.uint64, count=_HOT_COUNT, offset=_HOT_OFFSET
        )
        self._samples = np.frombuffer(
            shm,
            dtype=np.complex64,
            count=self._ring_size_samples,
            offset=_SAMPLE_REGION_OFFSET,
        )
        self._overrun_margin = _compute_overrun_margin(self._ring_size_samples)

    # ─── attach ────────────────────────────────────────────────────────
    @classmethod
    def attach(cls, channel_name: str) -> "RingBufferReader":
        """Attach to an existing ring segment for ``channel_name``.

        Raises ``RingBufferError`` if no segment is present (i.e. the
        producer has not started yet) and
        ``RingBufferIncompatibleError`` if the segment's static header is
        unexpected.
        """
        _require_x86()
        key = ring_key_for_channel(channel_name)
        try:
            shm = sysv_ipc.SharedMemory(key, flags=0)
        except sysv_ipc.ExistentialError as exc:
            raise RingBufferError(
                f"RingBufferReader[{channel_name}]: no segment found "
                f"(key=0x{key:08x}); is the producer running?"
            ) from exc

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
                f"RingBufferReader[{channel_name}]: bad magic {magic!r}"
            )
        if version != HEADER_VERSION:
            raise RingBufferIncompatibleError(
                f"RingBufferReader[{channel_name}]: version={version} "
                f"(reader expects {HEADER_VERSION})"
            )
        if dtype_code != DTYPE_COMPLEX64:
            raise RingBufferIncompatibleError(
                f"RingBufferReader[{channel_name}]: dtype_code={dtype_code}"
            )
        if hdr_size != HEADER_SIZE:
            raise RingBufferIncompatibleError(
                f"RingBufferReader[{channel_name}]: header size={hdr_size}"
            )
        if name != channel_name:
            raise RingBufferIncompatibleError(
                f"RingBufferReader: segment name={name!r} != requested "
                f"{channel_name!r}"
            )

        reader = cls(
            channel_name=channel_name,
            shm=shm,
            sample_rate=int(sr),
            ring_size_samples=int(ring_size),
        )
        logger.info(
            f"RingBufferReader[{channel_name}]: attached key=0x{key:08x} "
            f"sr={sr} ring_size={ring_size}"
        )
        return reader

    # ─── header snapshots ──────────────────────────────────────────────
    def write_cursor(self) -> int:
        """Return the monotonic write_cursor_samples (single 8-byte read)."""
        return int(self._hot[HOT_WRITE_CURSOR])

    def heartbeat_ns(self) -> int:
        return int(self._hot[HOT_HEARTBEAT_NS])

    def producer_pid(self) -> int:
        return int(self._hot[HOT_PRODUCER_PID])

    def batch_anchor(self) -> Tuple[int, int]:
        """Return ``(batch_first_rtp, batch_cursor_pos)``."""
        return (
            int(self._hot[HOT_BATCH_FIRST_RTP]),
            int(self._hot[HOT_BATCH_CURSOR_POS]),
        )

    def _read_gps_anchor(self) -> Tuple[int, int]:
        """Seqlock-protected read of ``(gps_time_ns, rtp_timesnap)``.

        Retries while the anchor is being re-seeded or the mirror
        disagrees with ``epoch_counter``.
        """
        for _ in range(_MAX_SEQLOCK_RETRIES):
            e1 = int(self._hot[HOT_EPOCH_COUNTER])
            if e1 == 0:
                time.sleep(0.0001)
                continue
            gps_ns_u = int(self._hot[HOT_ANCHOR_GPS_NS])
            rtp_snap = int(self._hot[HOT_ANCHOR_RTP_SNAP]) & 0xFFFFFFFF
            e2 = int(self._hot[HOT_EPOCH_MIRROR])
            if e1 == e2:
                # Reinterpret as signed int64 for negative GPS_TIME (would
                # indicate a pre-GPS-epoch time — shouldn't happen, but be
                # faithful to the storage shape).
                if gps_ns_u >= (1 << 63):
                    gps_ns = gps_ns_u - (1 << 64)
                else:
                    gps_ns = gps_ns_u
                return gps_ns, rtp_snap
        raise RingBufferError(
            f"RingBufferReader[{self._channel_name}]: anchor seqlock stuck "
            f"after {_MAX_SEQLOCK_RETRIES} retries"
        )

    def get_anchor(self) -> dict:
        """Snapshot of all timing fields (for diagnostics / tests)."""
        gps_ns, rtp_snap = self._read_gps_anchor()
        batch_rtp, batch_pos = self.batch_anchor()
        return {
            "gps_time_ns": gps_ns,
            "rtp_timesnap": rtp_snap,
            "batch_first_rtp": batch_rtp,
            "batch_cursor_pos": batch_pos,
            "epoch": int(self._hot[HOT_EPOCH_COUNTER]),
        }

    # ─── sample ↔ UTC mapping ──────────────────────────────────────────
    def _sample_to_rtp(
        self,
        sample_index: int,
        batch_rtp: int,
        batch_pos: int,
    ) -> int:
        """Map monotonic sample index → 32-bit RTP timestamp."""
        return (batch_rtp + (sample_index - batch_pos)) & 0xFFFFFFFF

    def _rtp_to_utc(
        self,
        rtp: int,
        gps_ns: int,
        rtp_snap: int,
    ) -> float:
        gps_utc = gps_ns / BILLION + GPS_EPOCH_UNIX - self._leap
        return gps_utc + _rtp_delta_signed(rtp, rtp_snap) / self._sample_rate

    def _utc_to_sample(
        self,
        utc: float,
        gps_ns: int,
        rtp_snap: int,
        batch_rtp: int,
        batch_pos: int,
    ) -> int:
        gps_utc = gps_ns / BILLION + GPS_EPOCH_UNIX - self._leap
        target_rtp = int(round(rtp_snap + (utc - gps_utc) * self._sample_rate))
        target_rtp &= 0xFFFFFFFF
        return batch_pos + _rtp_delta_signed(target_rtp, batch_rtp)

    def head_utc(self, cursor: Optional[int] = None) -> Optional[float]:
        """UTC of the sample one-past the current write head.

        Returns ``None`` while the producer has not yet written any
        samples or installed a valid anchor — the caller should poll
        again.
        """
        if cursor is None:
            cursor = self.write_cursor()
        if cursor == 0:
            return None
        try:
            gps_ns, rtp_snap = self._read_gps_anchor()
        except RingBufferError:
            return None
        if gps_ns == 0 and rtp_snap == 0:
            return None
        batch_rtp, batch_pos = self.batch_anchor()
        last_rtp = self._sample_to_rtp(cursor - 1, batch_rtp, batch_pos)
        return self._rtp_to_utc(last_rtp, gps_ns, rtp_snap) + 1.0 / self._sample_rate

    # ─── extract ───────────────────────────────────────────────────────
    def extract_interval(
        self,
        utc_start: float,
        duration_sec: float,
    ) -> Tuple[np.ndarray, dict]:
        """Copy out ``duration_sec`` of samples starting at ``utc_start``.

        Returns ``(samples, metadata)``.  ``metadata`` has the shape that
        :func:`hf_timestd.core.buffer_timing.resolve_buffer_timing`
        already accepts, so metrology / CHU FSK / future consumers do not
        need a second timing path.

        Raises
        ------
        RingBufferError
            The requested interval is in the future (not yet written) or
            the anchor has not been installed yet.
        RingBufferOverrunError
            The producer has overwritten the requested window, or the
            producer lapped us mid-copy, or the epoch changed during the
            read.
        """
        if duration_sec <= 0:
            raise ValueError(f"duration_sec={duration_sec} must be positive")
        n_req = int(round(duration_sec * self._sample_rate))
        if n_req <= 0:
            raise ValueError(f"n_req={n_req} must be positive")
        if n_req > self._ring_size_samples:
            raise ValueError(
                f"extract_interval: requested {n_req} samples "
                f"> ring size {self._ring_size_samples}"
            )

        epoch_before = int(self._hot[HOT_EPOCH_COUNTER])
        w1 = self.write_cursor()
        if w1 == 0:
            raise RingBufferError(
                f"RingBufferReader[{self._channel_name}]: no samples yet"
            )
        gps_ns, rtp_snap = self._read_gps_anchor()
        if gps_ns == 0 and rtp_snap == 0:
            raise RingBufferError(
                f"RingBufferReader[{self._channel_name}]: anchor not installed"
            )
        batch_rtp, batch_pos = self.batch_anchor()

        s_start = self._utc_to_sample(
            utc_start, gps_ns, rtp_snap, batch_rtp, batch_pos
        )
        s_end = s_start + n_req

        if s_end > w1:
            raise RingBufferError(
                f"RingBufferReader[{self._channel_name}]: interval not yet "
                f"written (need up to sample {s_end}, cursor={w1})"
            )
        min_safe_start = w1 - self._ring_size_samples + self._overrun_margin
        if s_start < min_safe_start:
            raise RingBufferOverrunError(
                f"RingBufferReader[{self._channel_name}]: requested window "
                f"overwritten (s_start={s_start}, min_safe={min_safe_start}, "
                f"cursor={w1})"
            )

        out = self._copy_out(s_start, n_req)

        w2 = self.write_cursor()
        epoch_after = int(self._hot[HOT_EPOCH_COUNTER])
        if epoch_after != epoch_before or epoch_after == 0:
            raise RingBufferOverrunError(
                f"RingBufferReader[{self._channel_name}]: epoch changed during "
                f"read ({epoch_before} → {epoch_after})"
            )
        # Post-check: the sample at s_start is overwritten when cursor
        # reaches s_start + ring_size.  Require a margin of slack so the
        # race window between w2 read and caller consumption is covered.
        if (w2 - s_start) > self._ring_size_samples - self._overrun_margin:
            raise RingBufferOverrunError(
                f"RingBufferReader[{self._channel_name}]: producer lapped read "
                f"window (s_start={s_start}, w2={w2}, ring={self._ring_size_samples})"
            )

        start_rtp = self._sample_to_rtp(s_start, batch_rtp, batch_pos)
        metadata = {
            "start_rtp_timestamp": int(start_rtp),
            "gps_time_ns": int(gps_ns),
            "rtp_timesnap": int(rtp_snap),
            "sample_rate": self._sample_rate,
            "channel": self._channel_name,
            "n_samples": n_req,
            "start_system_time": self._rtp_to_utc(start_rtp, gps_ns, rtp_snap),
            "source": "ring_buffer",
        }
        return out, metadata

    def extract_samples(self, count: int) -> Tuple[np.ndarray, dict]:
        """Copy out the most recent ``count`` samples.

        Returned ``metadata`` is in the same ``resolve_buffer_timing`` shape
        as :meth:`extract_interval`.
        """
        if count <= 0:
            raise ValueError(f"count={count} must be positive")
        if count > self._ring_size_samples:
            raise ValueError(
                f"count={count} > ring size {self._ring_size_samples}"
            )

        epoch_before = int(self._hot[HOT_EPOCH_COUNTER])
        w1 = self.write_cursor()
        if w1 < count:
            raise RingBufferError(
                f"RingBufferReader[{self._channel_name}]: only {w1} samples "
                f"written, cannot extract {count}"
            )
        gps_ns, rtp_snap = self._read_gps_anchor()
        batch_rtp, batch_pos = self.batch_anchor()

        s_start = w1 - count
        out = self._copy_out(s_start, count)

        w2 = self.write_cursor()
        epoch_after = int(self._hot[HOT_EPOCH_COUNTER])
        if epoch_after != epoch_before or epoch_after == 0:
            raise RingBufferOverrunError(
                f"RingBufferReader[{self._channel_name}]: epoch changed during "
                f"extract_samples"
            )
        if (w2 - s_start) > self._ring_size_samples - self._overrun_margin:
            raise RingBufferOverrunError(
                f"RingBufferReader[{self._channel_name}]: producer lapped read "
                f"window during extract_samples "
                f"(s_start={s_start}, w2={w2}, ring={self._ring_size_samples})"
            )

        start_rtp = self._sample_to_rtp(s_start, batch_rtp, batch_pos)
        metadata = {
            "start_rtp_timestamp": int(start_rtp),
            "gps_time_ns": int(gps_ns),
            "rtp_timesnap": int(rtp_snap),
            "sample_rate": self._sample_rate,
            "channel": self._channel_name,
            "n_samples": count,
            "start_system_time": self._rtp_to_utc(start_rtp, gps_ns, rtp_snap),
            "source": "ring_buffer",
        }
        return out, metadata

    def _copy_out(self, s_start: int, n: int) -> np.ndarray:
        """Copy ``n`` samples starting at monotonic index ``s_start``."""
        ring_size = self._ring_size_samples
        out = np.empty(n, dtype=np.complex64)
        start_mod = s_start % ring_size
        if start_mod + n <= ring_size:
            np.copyto(out, self._samples[start_mod : start_mod + n])
        else:
            first = ring_size - start_mod
            np.copyto(out[:first], self._samples[start_mod:])
            np.copyto(out[first:], self._samples[: n - first])
        return out

    # ─── shutdown ──────────────────────────────────────────────────────
    def close(self) -> None:
        """Detach from the shared segment.  Does NOT remove it."""
        if self._closed:
            return
        self._closed = True
        self._hot = None
        self._samples = None
        try:
            self._shm.detach()
        except Exception as exc:
            logger.debug(
                f"RingBufferReader[{self._channel_name}]: detach: {exc}"
            )
