#!/usr/bin/env python3
"""
Buffer Timing: Sample-to-UTC Mapping
=====================================

A buffer is a contiguous sequence of IQ samples recorded at a GPSDO-locked
sample rate (exactly 24000 Hz).  This module answers one question:

    What UTC time does sample N correspond to?

The answer comes from the RTP timestamp chain — the sole timing authority:

    sample0_utc = GPS_TIME_unix + (start_rtp - RTP_TIMESNAP) / sample_rate

Every buffer's metadata contains:
  - start_rtp_timestamp: RTP timestamp of sample 0
  - gps_time_ns: GPS_TIME (ns since GPS epoch) — authoritative, from the writer
  - rtp_timesnap: RTP counter at GPS_TIME — authoritative, from the writer
  - timing_snapshots[]: GPS_TIME / RTP_TIMESNAP pairs (legacy, used as fallback)

GPS_TIME is the GPSDO-disciplined ground truth.  RTP_TIMESNAP is the
RTP counter value at the moment GPS_TIME was sampled.  Both are in the
same counter space (input_sample_index / decimation).  The formula above
gives the exact UTC of any RTP timestamp.

start_system_time is NEVER used for timing.  It is logged for diagnostics
only.  The writer computes it from its own (possibly stale) GPS/RTP
mapping, which can be wrong by seconds or more after a radiod restart.
"""

import logging
from dataclasses import dataclass
from typing import List, Optional, Dict, Any

logger = logging.getLogger(__name__)

# GPS epoch: 1980-01-06 00:00:00 UTC as Unix timestamp
GPS_EPOCH_UNIX = 315964800

# M-M4: resolve the GPS-UTC offset per buffer (keyed off its own GPS time)
# rather than capturing a module-level constant at import. A multi-week
# daemon that crossed a leap-second insertion would otherwise carry a 1 s
# error on every buffer recorded after the boundary.
from .leap_second import gps_leap_seconds_at_gps_time

BILLION = 1_000_000_000


@dataclass
class BufferTiming:
    """Maps sample indices to UTC for a buffer of IQ samples.

    The GPSDO guarantees the sample clock is exact, so the mapping is
    a simple linear function:

        utc(sample) = sample0_utc + sample / sample_rate

    Usage:
        timing = resolve_buffer_timing(metadata)
        utc = timing.sample_to_utc(12345)
        idx = timing.utc_to_sample(1770515405.123)
    """
    # UTC time of sample 0 of this buffer
    sample0_utc: float

    # Sample rate (Hz) — exact, GPSDO-locked
    sample_rate: int

    # Which timing source produced sample0_utc
    source: str  # 'rtp_gps' or 'no_timing'

    # Quality metrics
    n_snapshots_used: int
    jitter_ms: float

    def sample_to_utc(self, sample_index: float) -> float:
        """Convert a sample index to a UTC timestamp."""
        return self.sample0_utc + sample_index / self.sample_rate

    def utc_to_sample(self, utc: float) -> float:
        """Convert a UTC timestamp to a (fractional) sample index."""
        return (utc - self.sample0_utc) * self.sample_rate


def resolve_buffer_timing(
    metadata: Dict[str, Any],
    sample_rate: int = 24000
) -> BufferTiming:
    """Determine the sample-to-UTC mapping for a buffer.

    The RTP stream is the sole timing authority.  We compute sample0_utc
    from start_rtp_timestamp and the GPS_TIME / RTP_TIMESNAP snapshots.

    If snapshots span a radiod restart (different RTP counter spaces),
    we use the most recent snapshot — that's the counter space the
    buffer's start_rtp_timestamp was computed in.

    Args:
        metadata: Buffer metadata dict (from the JSON sidecar file)
        sample_rate: Samples per second (default 24000, GPSDO-locked)

    Returns:
        BufferTiming mapping for this buffer
    """
    start_rtp = metadata.get('start_rtp_timestamp')

    if start_rtp is None:
        logger.error("No start_rtp_timestamp in metadata — cannot determine buffer timing")
        return BufferTiming(
            sample0_utc=0.0,
            sample_rate=sample_rate,
            source='no_timing',
            n_snapshots_used=0,
            jitter_ms=float('inf')
        )

    start_rtp = int(start_rtp)

    # Primary: top-level gps_time_ns / rtp_timesnap written by the archive
    # writer from its authoritative GPS/RTP mapping.  Always present when
    # timing is locked — no dependency on the timing poll thread.
    top_gps_ns = metadata.get('gps_time_ns')
    top_rtp_snap = metadata.get('rtp_timesnap')
    if top_gps_ns is not None and top_rtp_snap is not None:
        top_gps_ns = int(top_gps_ns)
        leap = gps_leap_seconds_at_gps_time(top_gps_ns)
        gps_utc = top_gps_ns / BILLION + GPS_EPOCH_UNIX - leap
        delta = _rtp_delta_signed(start_rtp, int(top_rtp_snap))
        sample0_utc = gps_utc + delta / sample_rate

        sst = float(metadata.get('start_system_time', 0))
        if sst > 0 and abs(sample0_utc - sst) > 1.0:
            logger.warning(
                f"BufferTiming: RTP authority gives sample0_utc={sample0_utc:.3f}, "
                f"writer's start_system_time={sst:.3f} (off by {sample0_utc - sst:+.1f}s)"
            )

        logger.debug(f"BufferTiming (top-level): sample0_utc={sample0_utc:.6f}")
        return BufferTiming(
            sample0_utc=sample0_utc,
            sample_rate=sample_rate,
            source='rtp_gps',
            n_snapshots_used=1,
            jitter_ms=0.0
        )

    # Fallback: timing_snapshots[] array (for files written before this change)
    snapshots = metadata.get('timing_snapshots', [])
    if not snapshots:
        logger.error("No RTP timing in metadata — cannot determine buffer timing")
        return BufferTiming(
            sample0_utc=0.0,
            sample_rate=sample_rate,
            source='no_timing',
            n_snapshots_used=0,
            jitter_ms=float('inf')
        )

    return _from_rtp_gps(start_rtp, snapshots, sample_rate, metadata)


# ── internal helpers ─────────────────────────────────────────────────

def _rtp_delta_signed(rtp: int, rtp_start: int) -> int:
    """Signed 32-bit difference (rtp - rtp_start), handling wraparound."""
    delta = (rtp - rtp_start) & 0xFFFFFFFF
    if delta > 0x7FFFFFFF:
        delta -= 0x100000000
    return delta



def _gps_snapshot_to_utc(snapshot: Dict) -> Optional[float]:
    """Convert a GPS_TIME snapshot to Unix UTC seconds.

    The GPS-UTC offset is looked up against the snapshot's own GPS time,
    so a snapshot recorded before a leap second uses the pre-insertion
    offset and one recorded after uses the post-insertion offset — even
    when both snapshots coexist in the same buffer's metadata.
    """
    gps_ns = snapshot.get('gps_time_ns')
    if gps_ns is None:
        return None
    gps_ns = int(gps_ns)
    return gps_ns / BILLION + GPS_EPOCH_UNIX - gps_leap_seconds_at_gps_time(gps_ns)


def _from_rtp_gps(
    start_rtp: int,
    snapshots: List[Dict],
    sample_rate: int,
    metadata: Dict[str, Any],
) -> BufferTiming:
    """Compute sample0_utc from start_rtp_timestamp and GPS snapshots.

    Use the most recent snapshot.  It is always in the same RTP counter
    as start_rtp_timestamp because the writer updates its mapping on
    every new GPS_TIME/RTP_TIMESNAP pair from radiod.

        sample0_utc = gps_utc + (start_rtp - rtp_timesnap) / sample_rate
    """
    # Find the most recent snapshot (highest gps_time_ns)
    best = None
    for s in snapshots:
        gps_utc = _gps_snapshot_to_utc(s)
        rtp_snap = s.get('rtp_timesnap')
        gps_ns = s.get('gps_time_ns', 0)
        if gps_utc is None or rtp_snap is None:
            continue
        if best is None or gps_ns > best[0]:
            best = (gps_ns, gps_utc, rtp_snap)

    if best is None:
        logger.error("No valid GPS snapshots in metadata")
        return BufferTiming(
            sample0_utc=0.0,
            sample_rate=sample_rate,
            source='no_timing',
            n_snapshots_used=0,
            jitter_ms=float('inf')
        )

    _, gps_utc, rtp_snap = best
    delta = _rtp_delta_signed(start_rtp, rtp_snap)
    sample0_utc = gps_utc + delta / sample_rate

    # Diagnostic: log if this disagrees with start_system_time
    sst = float(metadata.get('start_system_time', 0))
    if sst > 0:
        diff_s = sample0_utc - sst
        if abs(diff_s) > 1.0:
            logger.warning(
                f"BufferTiming: RTP authority gives sample0_utc={sample0_utc:.3f}, "
                f"writer's start_system_time={sst:.3f} (off by {diff_s:+.1f}s)"
            )

    logger.debug(
        f"BufferTiming (rtp_gps): sample0_utc={sample0_utc:.6f}"
    )
    return BufferTiming(
        sample0_utc=sample0_utc,
        sample_rate=sample_rate,
        source='rtp_gps',
        n_snapshots_used=1,
        jitter_ms=0.0
    )
