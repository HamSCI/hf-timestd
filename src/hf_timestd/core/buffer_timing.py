#!/usr/bin/env python3
"""
Buffer Timing: Sample-to-UTC Mapping
=====================================

A buffer is a contiguous sequence of IQ samples recorded at a GPSDO-locked
sample rate (exactly 24000 Hz).  This module answers one question:

    What UTC time does sample N correspond to?

The answer is trivial:

    utc(sample) = start_system_time + sample / sample_rate

The recorder (BinaryArchiveWriter) already reconciled radiod's counter
spaces at runtime.  It timestamps samples at USB callback time, and the
decimation from raw ADC samples to RTP packets is deterministic arithmetic.
The writer used the GPS_TIME/RTP_TIMESNAP mapping (with counter-space
correction) to compute start_rtp_timestamp in packet space and set
start_system_time to the exact UTC of sample 0.

GPS timing snapshots in the metadata are available for cross-checking
but are NOT needed as the primary timing source — the writer already
did that work.
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
    source: str  # 'writer', 'local_snapshots', 'metadata_fallback'

    # Quality metrics
    n_snapshots_used: int
    jitter_ms: float  # 0.0 when using writer's start_system_time

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

    Primary path:
      The writer set start_system_time to the exact UTC of sample 0,
      using the GPS_TIME/RTP_TIMESNAP mapping with counter-space
      correction already applied.  We trust it directly.

    Fallback (no GPS timing in writer):
      Use local_receipt_time snapshots for an approximate mapping
      (~10-300ms accuracy, suitable as Fusion mode seed).

    Args:
        metadata: Buffer metadata dict (from the JSON sidecar file)
        sample_rate: Samples per second (default 24000, GPSDO-locked)

    Returns:
        BufferTiming mapping for this buffer
    """
    sst = float(metadata.get('start_system_time', 0))
    snapshots = metadata.get('timing_snapshots', [])

    # Primary: start_system_time from the writer.
    # When the writer has GPS timing, it sets start_system_time to the
    # minute boundary (= exact UTC of sample 0).  We detect this by
    # checking that sst is a round minute (integer divisible by 60).
    if sst > 0 and sst == int(sst) and int(sst) % 60 == 0:
        logger.debug(
            f"BufferTiming (writer): sample0_utc={sst:.1f}"
        )
        return BufferTiming(
            sample0_utc=sst,
            sample_rate=sample_rate,
            source='writer',
            n_snapshots_used=0,
            jitter_ms=0.0
        )

    # Fallback: local_receipt_time snapshots (Fusion mode seed)
    if snapshots and 'local_receipt_time' in snapshots[0]:
        timing = _from_local_snapshots(
            snapshots, int(metadata.get('start_rtp_timestamp', 0)),
            sample_rate
        )
        if timing is not None:
            return timing

    # Last resort
    if sst > 0:
        logger.warning(
            f"No GPS-locked timing — using start_system_time ({sst:.3f})"
        )
        return BufferTiming(
            sample0_utc=sst,
            sample_rate=sample_rate,
            source='metadata_fallback',
            n_snapshots_used=0,
            jitter_ms=float('inf')
        )

    logger.error("No timing information in metadata")
    return BufferTiming(
        sample0_utc=0.0,
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
