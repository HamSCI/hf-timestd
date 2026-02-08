#!/usr/bin/env python3
"""
Buffer Timing: Sample-to-UTC Mapping
=====================================

A buffer is a contiguous sequence of IQ samples recorded at a GPSDO-locked
sample rate (exactly 24000 Hz).  The buffer has no timing authority — it is
just samples.  This module answers one question:

    What UTC time does sample N correspond to?

Two sources can answer this:

  RTP mode (L4/L5/L6):
    timing_snapshots in the metadata map RTP counter values to GPS time.
    Since the sample clock is GPSDO-locked, every sample between snapshots
    is exactly 1/sample_rate seconds apart.  This gives sub-microsecond
    sample-to-UTC accuracy.

  Fusion mode (L1/L2/L3):
    timing_snapshots map RTP counters to local_receipt_time (NTP-derived).
    This gives ~10-300ms accuracy — good enough as an initial estimate.
    The metrology engine then refines the offset by detecting timing tones
    at known UTC seconds.  The tones are the timing authority.

In both cases the buffer's file-level "minute_boundary" label is irrelevant
to timing.  It is a file-organization convenience, not a timing input.
"""

import logging
from dataclasses import dataclass
from typing import List, Optional, Dict, Any

logger = logging.getLogger(__name__)

# GPS epoch: 1980-01-06 00:00:00 UTC as Unix timestamp
GPS_EPOCH_UNIX = 315964800

# GPS-UTC leap seconds as of 2026
GPS_LEAP_SECONDS = 18


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
    source: str  # 'gps_snapshots', 'local_snapshots', 'metadata_fallback'

    # Quality metrics
    n_snapshots_used: int
    jitter_ms: float  # robust std of per-snapshot estimates

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

    Tries, in order:
      1. GPS snapshots  (gps_time_ns)       — sub-ms, authoritative
      2. Local snapshots (local_receipt_time) — ~10-300ms, initial estimate
      3. start_system_time from metadata     — last resort, unreliable

    Args:
        metadata: Buffer metadata dict (from the JSON sidecar file)
        sample_rate: Samples per second (default 24000, GPSDO-locked)

    Returns:
        BufferTiming mapping for this buffer
    """
    rtp_start = int(metadata.get('start_rtp_timestamp', 0))
    snapshots = metadata.get('timing_snapshots', [])

    # 1. GPS snapshots (highest authority)
    if snapshots and 'gps_time_ns' in snapshots[0]:
        timing = _from_gps_snapshots(snapshots, rtp_start, sample_rate)
        if timing is not None:
            return timing

    # 2. Local receipt-time snapshots
    if snapshots and 'local_receipt_time' in snapshots[0]:
        timing = _from_local_snapshots(snapshots, rtp_start, sample_rate)
        if timing is not None:
            return timing

    # 3. Fallback — start_system_time (often set to the file's minute
    #    boundary label, which is NOT the UTC time of sample 0)
    sst = float(metadata.get('start_system_time', 0))
    logger.warning(
        f"No usable timing snapshots — falling back to start_system_time "
        f"({sst:.3f}).  Sample-to-UTC mapping will be approximate."
    )
    return BufferTiming(
        sample0_utc=sst,
        sample_rate=sample_rate,
        source='metadata_fallback',
        n_snapshots_used=0,
        jitter_ms=float('inf')
    )


# ── internal helpers ─────────────────────────────────────────────────

def _rtp_delta_signed(rtp: int, rtp_start: int) -> int:
    """Signed 32-bit difference (rtp - rtp_start), handling wraparound."""
    delta = (rtp - rtp_start) & 0xFFFFFFFF
    if delta > 0x7FFFFFFF:
        delta -= 0x100000000
    return delta


def _median_and_mad(values: List[float]):
    """Return (median, MAD-based sigma) of a list of floats."""
    values = sorted(values)
    n = len(values)
    median = values[n // 2]
    deviations = sorted(abs(v - median) for v in values)
    mad = deviations[n // 2]
    sigma = mad * 1.4826  # MAD -> Gaussian sigma
    return median, sigma


def _from_gps_snapshots(
    snapshots: List[Dict],
    rtp_start: int,
    sample_rate: int
) -> Optional[BufferTiming]:
    """Compute sample0_utc from GPS timing snapshots.

    Each snapshot maps (rtp_timesnap -> gps_time_ns).
    For each: utc0 = gps_to_unix(gps_time_ns) - rtp_delta / sample_rate
    Median gives a robust estimate.
    """
    estimates = []
    for s in snapshots:
        gps_ns = s.get('gps_time_ns')
        rtp = s.get('rtp_timesnap')
        if gps_ns is None or rtp is None:
            continue
        unix_sec = gps_ns / 1e9 + GPS_EPOCH_UNIX - GPS_LEAP_SECONDS
        delta = _rtp_delta_signed(rtp, rtp_start)
        estimates.append(unix_sec - delta / sample_rate)

    if len(estimates) < 3:
        logger.warning(f"Only {len(estimates)} GPS snapshots — too few")
        return None

    median_utc0, jitter = _median_and_mad(estimates)
    jitter_ms = jitter * 1000

    logger.info(
        f"BufferTiming (GPS): sample0_utc={median_utc0:.6f}, "
        f"jitter={jitter_ms:.2f}ms, snapshots={len(estimates)}"
    )
    return BufferTiming(
        sample0_utc=median_utc0,
        sample_rate=sample_rate,
        source='gps_snapshots',
        n_snapshots_used=len(estimates),
        jitter_ms=jitter_ms
    )


def _from_local_snapshots(
    snapshots: List[Dict],
    rtp_start: int,
    sample_rate: int
) -> Optional[BufferTiming]:
    """Compute sample0_utc from local_receipt_time snapshots.

    Lower accuracy than GPS (~10-300ms jitter from kernel scheduling
    and network buffering).  Usable as an initial estimate in Fusion mode.
    """
    estimates = []
    for s in snapshots:
        local = s.get('local_receipt_time')
        rtp = s.get('rtp_timesnap')
        if local is None or rtp is None:
            continue
        delta = _rtp_delta_signed(rtp, rtp_start)
        estimates.append(local - delta / sample_rate)

    if len(estimates) < 3:
        return None

    median_utc0, jitter = _median_and_mad(estimates)
    jitter_ms = jitter * 1000

    logger.info(
        f"BufferTiming (local): sample0_utc={median_utc0:.6f}, "
        f"jitter={jitter_ms:.1f}ms, snapshots={len(estimates)}"
    )
    return BufferTiming(
        sample0_utc=median_utc0,
        sample_rate=sample_rate,
        source='local_snapshots',
        n_snapshots_used=len(estimates),
        jitter_ms=jitter_ms
    )
