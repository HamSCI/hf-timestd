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
  - gps_time_ns: GPS_TIME (ns since GPS epoch) — from the writer.  ⚠ On
    AC0G-B4 2026-08-25 this field and rtp_timesnap were FROZEN across
    five consecutive sidecars while start_rtp_timestamp advanced by
    exactly 7,200,000 samples each: the writer captures the pair once and
    never refreshes it, so a reader mapping RTP→UTC from these two fields
    can be hours out.  Prefer the anchor ledger.
  - rtp_timesnap: RTP counter at GPS_TIME — from the writer; see above
  - timing_snapshots[]: GPS_TIME / RTP_TIMESNAP pairs (legacy, used as fallback)

⚠ GPS_TIME is NOT GPSDO-disciplined ground truth, RTP_TIMESNAP is NOT
"the RTP counter at the moment GPS_TIME was sampled", and the two are NOT
in the same counter space (hf-timestd#37).  Per ka9q-radio source:

  * GPS_TIME is ``gps_time_ns()`` evaluated as the status packet is built
    (radio_status.c:718-719); ``gps_time_ns()`` is
    ``clock_gettime(CLOCK_TAI)`` offset to the GPS epoch (misc.c:546-563)
    — the HOST SYSTEM CLOCK, not a sample index.
  * RTP_TIMESNAP is ``chan->output.rtp.timestamp`` (radio_status.c:859),
    the next RTP timestamp to be sent, advanced per emitted block
    (audio.c:49-51, 177-179) — quantised to the 20 ms block grid plus
    that emission's lateness.
  * The sample index the old claim was reaching for is INPUT_SAMPLES =
    ``chan->filter.out.sample_index`` (radio_status.c:720).

Measured on AC0G-B4 2026-08-25: 20,813,617 pair updates, max
disagreement +816.4 ms, only ~37 % at exactly 0.  The error is one-sided
lateness — GPS_TIME is read live, RTP_TIMESNAP reflects the last
emission.  So the formula below does NOT give the exact UTC of an RTP
timestamp; it gives it to within that pair error.

⛔ Do not use this mapping where precision matters.  T6 sets the RTP→UTC
mapping and keeps the host clock out of it
(docs/design/T6_ANCHOR_INVERSION_DESIGN.md,
docs/design/TIMING_AUTHORITY_TWO_AXIS.md §2).

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
    # UTC time of sample 0 of this buffer.  When ``source == 'no_timing'``
    # this is a *sentinel* 0.0 (Unix epoch) -- consumers MUST check
    # ``source != 'no_timing'`` before treating it as real UTC.  The
    # sentinel can't be NaN because downstream code does
    # ``int(sample0_utc)`` and we'd lose deterministic behaviour.
    sample0_utc: float

    # Sample rate (Hz) — exact, GPSDO-locked
    sample_rate: int

    # Which timing source produced sample0_utc.  Possible values:
    #   'rtp_gps'   -- authoritative timing from the RTP/GPS snapshot.
    #   'no_timing' -- sentinel; sample0_utc is meaningless (0.0).
    source: str

    # Quality metrics — currently placeholders (§3.4 Low).  Today these
    # are always 0/1 and 0.0/inf because per-buffer timing is sourced
    # from a single snapshot (top-level gps_time_ns + rtp_timesnap);
    # they're retained on the dataclass so a future multi-snapshot
    # estimator can populate them without a schema change.
    n_snapshots_used: int
    jitter_ms: float

    # Offset Judge provenance (docs/OFFSET-JUDGE-SPEC-2026-08-05.md §8).
    # When the sidecar carries a "timing" block, sample0_utc above is the
    # CORRECTED UTC (raw radiod mapping + offset_applied_ns) and these
    # fields record the pedigree.  Legacy sidecars: 0.0 / None (raw
    # radiod mapping, exactly as before).
    offset_applied_ns: float = 0.0
    judge_tier: Optional[str] = None
    #: The judge's own uncertainty on that correction.  Carried because
    #: a consumer that reasons about WHERE something arrived needs to
    #: know how well the ruler is known -- the arrival gate widens its
    #: windows by this and abstains when they would overlap, so losing
    #: T6 produces an abstention rather than a confident wrong answer.
    offset_sigma_ns: float = 0.0

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
    # Offset Judge provenance block (OFFSET-JUDGE-SPEC-2026-08-05 §8).
    # When present, the writer applied offset_ns to this chunk's labels
    # (boundary placement + start_system_time); the raw radiod pair in
    # the sidecar stays uncorrected, so the correction is re-applied
    # here to reconstruct the corrected UTC downstream.  Legacy sidecars
    # (no block) get offset 0 — behavior identical to before.
    judge_block = metadata.get('timing') or {}
    try:
        judge_offset_ns = float(judge_block.get('offset_ns') or 0.0)
        judge_sigma_ns = float(judge_block.get('offset_sigma_ns') or 0.0)
    except (TypeError, ValueError):
        judge_offset_ns = 0.0
        judge_sigma_ns = 0.0
    judge_tier = judge_block.get('judge_tier')

    top_gps_ns = metadata.get('gps_time_ns')
    top_rtp_snap = metadata.get('rtp_timesnap')
    if top_gps_ns is not None and top_rtp_snap is not None:
        top_gps_ns = int(top_gps_ns)
        leap = gps_leap_seconds_at_gps_time(top_gps_ns)
        gps_utc = top_gps_ns / BILLION + GPS_EPOCH_UNIX - leap
        delta = _rtp_delta_signed(start_rtp, int(top_rtp_snap))
        sample0_utc = gps_utc + delta / sample_rate + judge_offset_ns / BILLION

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
            jitter_ms=0.0,
            offset_applied_ns=judge_offset_ns,
            judge_tier=judge_tier,
            offset_sigma_ns=judge_sigma_ns,
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

    bt = _from_rtp_gps(start_rtp, snapshots, sample_rate, metadata)
    if bt.source != 'no_timing' and judge_offset_ns:
        bt.sample0_utc += judge_offset_ns / BILLION
        bt.offset_applied_ns = judge_offset_ns
        bt.judge_tier = judge_tier
    return bt


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
