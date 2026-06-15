"""WWVB enhanced (post-2012) phase-modulation DSP and frame decoder.

Layer-3 DSP for hf-timestd's WWVB receive chain.  Consumes a complex64
IQ stream (centered on the 60 kHz carrier; typically produced by radiod
at 24 kHz sample rate) and yields decoded `WwvbTimeFrame` objects from
`wwvb_protocol`.  No service plumbing, no SQLite writer — those are
Layer 4.

For the full architectural picture (where this module fits in the WWVB
receive chain, the validation/reception-monitoring tap, what is and
isn't done), see ``docs/WWVB-INTEGRATION.md``.

Pipeline stages:

  1. **Coarse carrier-offset estimate** (FFT peak) and correction.
     RX888 TCXO accuracy is ~1 ppm → expected offset ≤ ~0.1 Hz at
     60 kHz, easily seen in a 2-second FFT.

  2. **AM envelope detection.**  |IQ| with a short moving-average
     smoother (~5 ms).  WWVB drops the carrier amplitude at the
     start of every second for 200/500/800 ms (encoding the legacy
     AM bit value); we use the *leading edge* of each drop as the
     on-time mark per NIST §2.2.

  3. **Second-boundary recovery.**  Find amplitude-drop edges
     spaced ~1 s apart; fit those to a per-frame timing model.

  4. **Per-second PM bit extraction.**  For each second, average the
     complex IQ over the guaranteed-high-amplitude window
     [850 ms, 990 ms] (200 ms is the worst-case AM-high overlap; we
     trim to avoid transients).  The mean phase clusters around two
     values (0 and π); threshold against the median.

  5. **Sync correlation.**  Slide the 13-bit sync_T pattern against
     the per-second bit stream; high match (≥ 12/13) marks a minute
     boundary.  Bit-polarity ambiguity from the absolute-phase
     reference is resolved here: if inverted sync_T matches better
     than upright, flip all bits.

  6. **Frame parse.**  Hand 60-bit windows from each detected
     minute boundary to `wwvb_protocol.parse_time_frame`.

Out of scope (Layer 4):
- Costas-loop tracking for slow carrier drift over 10+ min frames
  (this MVP relies on FFT-only coarse estimate; fine for short
  records, may need PLL refinement for live operation)
- AM bit value decoding (200/500/800 ms duration thresholding) —
  not needed for the PM time-frame channel, but needed if we ever
  want to cross-check against the legacy AM time code
- Chain-delay calibration for absolute UTC tier
- SQLite writer / metrology service envelope
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import numpy as np

from .wwvb_protocol import (
    FRAME_BITS,
    SYNC_T_BITS,
    WwvbTimeFrame,
    parse_time_frame,
)

__all__ = [
    "DemodResult",
    "DetectedFrame",
    "amplitude_envelope",
    "decode_iq",
    "estimate_carrier_offset",
    "extract_pm_bits",
    "find_second_boundaries",
    "find_sync_positions",
    "phases_to_bits",
    "synthesize_wwvb_iq",
]

logger = logging.getLogger(__name__)


# =============================================================================
# Carrier offset
# =============================================================================

def estimate_carrier_offset(
    iq: np.ndarray, sample_rate: float, max_seconds: float = 60.0,
) -> float:
    """Estimate the residual carrier offset (Hz) via squared-signal FFT.

    The WWVB PM signal is antipodal BPSK on a 60 kHz carrier centered
    at DC in the IQ stream.  Direct FFT of the IQ doesn't show a clean
    carrier line — BPSK suppresses the carrier and spreads energy.
    But **squaring** the IQ removes the ±1 BPSK modulation and
    doubles the carrier frequency: a perfectly-centered carrier
    becomes a strong tone at DC of `iq²`, and a small residual offset
    `f` shows up as a tone at `2f`.  The peak of `|FFT(iq²)|` then
    gives `2f`, and we report `f = peak / 2`.

    Args:
        iq: complex64 IQ array.
        sample_rate: samples per second.
        max_seconds: cap the FFT length (longer = finer resolution at
            the cost of CPU; 60 s gives ~17 mHz resolution, much
            tighter than RX888 TCXO drift).

    Returns:
        Carrier offset in Hz (positive: carrier above DC).
    """
    nsamp = min(iq.size, int(max_seconds * sample_rate))
    if nsamp < int(sample_rate):
        raise ValueError(
            f"need ≥ 1 s of IQ for carrier estimate; got {nsamp} samples"
        )
    squared = (iq[:nsamp].astype(np.complex128)) ** 2
    spectrum = np.fft.fftshift(np.fft.fft(squared))
    freqs = np.fft.fftshift(np.fft.fftfreq(nsamp, d=1.0 / sample_rate))
    peak = int(np.argmax(np.abs(spectrum)))
    return float(freqs[peak]) / 2.0


def correct_carrier(
    iq: np.ndarray, offset_hz: float, sample_rate: float,
) -> np.ndarray:
    """Mix `iq` by exp(-2πj·offset·t) to shift the carrier to DC."""
    if offset_hz == 0.0:
        return iq
    t = np.arange(iq.size, dtype=np.float64) / sample_rate
    return (iq * np.exp(-2j * np.pi * offset_hz * t)).astype(np.complex64)


# =============================================================================
# AM envelope + second-boundary detection
# =============================================================================

def amplitude_envelope(
    iq: np.ndarray, sample_rate: float, smooth_ms: float = 5.0,
) -> np.ndarray:
    """Return a smoothed |IQ| envelope at the same rate as `iq`.

    The smoother is a centered boxcar of `smooth_ms` width — enough
    to suppress sample-to-sample noise without blurring the
    200 ms-class AM amplitude transitions.
    """
    mag = np.abs(iq).astype(np.float32)
    n = max(1, int(round(smooth_ms * 1e-3 * sample_rate)))
    if n <= 1:
        return mag
    # SciPy/NumPy moving average via convolution.
    kernel = np.ones(n, dtype=np.float32) / n
    return np.convolve(mag, kernel, mode="same")


def find_second_boundaries(
    envelope: np.ndarray,
    sample_rate: float,
    drop_factor: float = 0.7,
    min_spacing_s: float = 0.85,
) -> np.ndarray:
    """Locate sample indices of the AM amplitude-drop edges.

    WWVB drops carrier amplitude at the start of every second for
    200/500/800 ms.  The leading falling edge is the on-time mark.
    We find samples where the envelope crosses below
    `drop_factor × median(envelope)` from above, enforce a minimum
    spacing to suppress duplicates, then **extrapolate backward**
    to fill in second boundaries missed at the start of the array
    (the very first edge is unobservable when the signal begins in
    the low-amplitude window).

    Args:
        envelope: smoothed |IQ| array (output of `amplitude_envelope`).
        sample_rate: samples per second.
        drop_factor: threshold as a fraction of the median envelope
            (0.7 ≈ midway between high and low levels).
        min_spacing_s: refractory window after each detected edge.

    Returns:
        1-D int array of sample indices, one per second boundary,
        starting from the earliest second boundary that fits within
        the array (including any inferred initial boundaries).
    """
    threshold = float(np.median(envelope) * drop_factor)
    # Falling-edge crossings (envelope[k-1] > threshold ≥ envelope[k]).
    above = envelope > threshold
    falling = np.where(above[:-1] & ~above[1:])[0] + 1
    if falling.size == 0:
        return np.array([], dtype=np.int64)

    # Refractory: keep edges spaced at least min_spacing_s apart.
    refractory = int(min_spacing_s * sample_rate)
    kept: List[int] = [int(falling[0])]
    for idx in falling[1:]:
        if int(idx) - kept[-1] >= refractory:
            kept.append(int(idx))

    # Extrapolate backward: if the first detected edge is more than
    # half a second from sample 0, there are second boundaries we
    # missed before the array began with high amplitude.  Project
    # backward at the inferred period (median spacing — robust to
    # the occasional skipped/spurious edge).
    period = int(sample_rate)
    if len(kept) >= 2:
        diffs = np.diff(kept)
        # Only use diffs that look like ~1 s (within ±5 %).
        good = diffs[(diffs > 0.95 * period) & (diffs < 1.05 * period)]
        if good.size:
            period = int(round(float(np.median(good))))
    extrapolated: List[int] = []
    prev = kept[0]
    while prev - period >= 0:
        prev -= period
        extrapolated.append(prev)
    return np.asarray(extrapolated[::-1] + kept, dtype=np.int64)


# =============================================================================
# Per-second PM phase + bits
# =============================================================================

def extract_pm_bits(
    iq: np.ndarray,
    second_boundaries: np.ndarray,
    sample_rate: float,
    window_start_ms: float = 850.0,
    window_end_ms: float = 990.0,
) -> np.ndarray:
    """For each second, compute the mean complex IQ over the guaranteed
    high-amplitude window, returning a 1-D complex array of length
    (len(second_boundaries) - 1).

    NIST §2.2: "receivers extract [phase] only from the high amplitude
    portion of the symbol."  The window [850, 990] ms after a second
    boundary is always high-amplitude regardless of AM bit value
    (markers extend the low-amp window to 800 ms), and avoids the
    transients at AM rising/falling edges.

    The returned values are the per-second mean IQ; the phase
    (np.angle) and magnitude both carry information — phase is the
    PM bit, magnitude indicates the SNR / decoder confidence.
    """
    if second_boundaries.size < 2:
        return np.array([], dtype=np.complex64)
    start_off = int(window_start_ms * 1e-3 * sample_rate)
    end_off = int(window_end_ms * 1e-3 * sample_rate)
    out = np.zeros(second_boundaries.size - 1, dtype=np.complex64)
    for k, edge in enumerate(second_boundaries[:-1]):
        s = int(edge) + start_off
        e = int(edge) + end_off
        if e > iq.size:
            out[k] = 0
            continue
        out[k] = iq[s:e].mean()
    return out


def phases_to_bits(
    mean_iq: np.ndarray, polarity: int = 0,
) -> Tuple[np.ndarray, float]:
    """Cluster the per-second mean IQ into binary PM bits.

    Phase ambiguity: the absolute phase reference is unknown until
    the sync word is found.  We choose a reference (the angle of
    the mean of all observations rotated to put their mean on the
    positive real axis), threshold each second's phase against that,
    and emit 0/1.  The framing layer will resolve polarity by
    comparing both the bit-stream and its inversion to sync_T.

    Args:
        mean_iq: per-second mean IQ from `extract_pm_bits`.
        polarity: external polarity flag — 0 keeps bits as-is, 1
            inverts them.  Used by the framing layer after sync
            disambiguation.

    Returns:
        (bits, reference_phase_rad) — bits is uint8 array of {0, 1},
        reference_phase_rad is the inferred reference orientation.
    """
    if mean_iq.size == 0:
        return np.array([], dtype=np.uint8), 0.0
    # The reference direction is the angle of the unrotated mean — this
    # collapses the bimodal distribution to one cluster on the positive
    # real axis (because antipodal symbols cancel in the mean only when
    # they're balanced; with WWVB's ~50/50 bit distribution we instead
    # square the signal to remove the BPSK and recover 2× the carrier
    # angle).
    squared = mean_iq ** 2
    ref_angle = float(np.angle(squared.mean()) / 2.0)
    rotated = mean_iq * np.exp(-1j * ref_angle)
    bits = (rotated.real < 0).astype(np.uint8)
    if polarity:
        bits ^= 1
    return bits, ref_angle


# =============================================================================
# Sync correlation + framing
# =============================================================================

def find_sync_positions(
    bits: np.ndarray,
    sync_word: Sequence[int] = SYNC_T_BITS,
    max_errors: int = 1,
) -> List[Tuple[int, int, bool]]:
    """Sliding correlation of `sync_word` against `bits`.

    Returns positions in `bits` where the 13-bit sync_word matches
    with ≤ max_errors mismatches, evaluated both upright and inverted.

    Returns:
        list of (start_index, error_count, inverted) tuples, sorted
        by start_index.
    """
    n = bits.size
    m = len(sync_word)
    if n < m:
        return []
    sync = np.asarray(sync_word, dtype=np.uint8)
    inv = 1 - sync
    out: List[Tuple[int, int, bool]] = []
    for i in range(n - m + 1):
        window = bits[i:i + m]
        upright = int(np.sum(window != sync))
        inverted = int(np.sum(window != inv))
        if upright <= max_errors:
            out.append((i, upright, False))
        elif inverted <= max_errors:
            out.append((i, inverted, True))
    return out


# =============================================================================
# Decoder dataclasses + pipeline
# =============================================================================

@dataclass(frozen=True)
class DetectedFrame:
    """A single minute frame located in the bit stream."""

    second_index: int
    """Index in the per-second bit stream where this frame starts."""

    inverted_polarity: bool
    """True if the bit stream had to be inverted to match sync_T."""

    sync_errors: int
    """Bit mismatches between received sync window and sync_T."""

    frame: WwvbTimeFrame
    """Parsed protocol-layer result."""

    boundary_sample: float = float("nan")
    """Sample index, into the decoded IQ array, of this frame's minute-
    boundary on-time mark (the falling edge of second 0).  This is what the
    Layer-4 Fusion writer needs: combined with the RTP timestamp of the IQ
    array's first sample it yields the on-time mark's RTP timestamp, hence its
    receiver UTC.  Sample resolution at 24 kHz is ~42 us (already sub-ms); a
    future sub-sample interpolation of the envelope crossing can tighten it
    without changing this field's meaning.  NaN if not recorded."""


@dataclass(frozen=True)
class DemodResult:
    """Full demod result for one IQ chunk."""

    carrier_offset_hz: float
    """Estimated residual carrier offset (Hz, positive = above DC)."""

    seconds_detected: int
    """Number of AM-second boundaries found."""

    per_second_iq: np.ndarray
    """Mean IQ per second (complex64); for downstream SNR / diagnostics."""

    bits: np.ndarray
    """Per-second PM bits (uint8) — *upright* polarity (sync correction
    already applied if a sync_T was found)."""

    frames: List[DetectedFrame]
    """All minute frames detected in the chunk."""


def decode_iq(
    iq: np.ndarray,
    sample_rate: float = 24000.0,
    max_sync_errors: int = 1,
) -> DemodResult:
    """End-to-end DSP + frame decode on an IQ array.

    Args:
        iq: complex64 IQ array, centered nominally on the WWVB carrier.
        sample_rate: samples per second.
        max_sync_errors: tolerance for sync_T match (out of 13 bits).

    Returns:
        A `DemodResult` with carrier offset, per-second IQ/bits, and
        decoded `DetectedFrame` list.
    """
    offset = estimate_carrier_offset(iq, sample_rate)
    corrected = correct_carrier(iq, offset, sample_rate)
    envelope = amplitude_envelope(corrected, sample_rate)
    boundaries = find_second_boundaries(envelope, sample_rate)
    mean_iq = extract_pm_bits(corrected, boundaries, sample_rate)
    bits, _ = phases_to_bits(mean_iq, polarity=0)

    # Sync detection.  If most hits are inverted, flip the whole bit
    # stream once so downstream framing operates on upright bits.
    hits = find_sync_positions(bits, SYNC_T_BITS, max_errors=max_sync_errors)
    if hits and sum(1 for _, _, inv in hits if inv) > len(hits) / 2:
        bits = bits ^ 1
        hits = find_sync_positions(
            bits, SYNC_T_BITS, max_errors=max_sync_errors,
        )

    frames: List[DetectedFrame] = []
    for start, err, inverted in hits:
        if start + FRAME_BITS > bits.size:
            continue
        frame_bits = bits[start:start + FRAME_BITS].tolist()
        try:
            parsed = parse_time_frame(frame_bits)
        except ValueError:
            continue
        frames.append(DetectedFrame(
            second_index=start,
            inverted_polarity=inverted,
            sync_errors=err,
            frame=parsed,
            # boundaries[start] is the on-time-mark sample of the second where
            # this frame's sync_T begins — i.e. the minute boundary.  bits has
            # length boundaries.size - 1, and framing guarantees
            # start < bits.size, so this index is always valid.
            boundary_sample=float(boundaries[start]),
        ))

    return DemodResult(
        carrier_offset_hz=offset,
        seconds_detected=int(boundaries.size),
        per_second_iq=mean_iq,
        bits=bits,
        frames=frames,
    )


# =============================================================================
# Test-signal synthesis (for unit tests)
# =============================================================================

def synthesize_wwvb_iq(
    frame_bits: Sequence[int],
    sample_rate: float = 24000.0,
    am_high: float = 1.0,
    am_low: float = 0.4,
    am_bit_durations_ms: Optional[Sequence[float]] = None,
    snr_db: Optional[float] = None,
    seed: int = 0,
) -> np.ndarray:
    """Generate a synthetic baseband IQ stream for a 60-second PM frame.

    Used by unit tests to exercise the demod pipeline against a known
    input.  The synthesis applies the spec's AM envelope (low at start
    of each second, high for the rest) plus carrier-phase inversion
    according to `frame_bits` (0 = phase 0, 1 = phase π).

    Args:
        frame_bits: 60 ints in {0, 1}.
        sample_rate: samples per second of the generated IQ.
        am_high: high-amplitude level of the AM envelope.
        am_low: low-amplitude level during the 200/500/800 ms drops.
        am_bit_durations_ms: per-second low-amp durations (default:
            all 200 ms = AM "0" bits; not relevant to PM decoding).
        snr_db: if not None, add complex Gaussian noise at this SNR.
        seed: RNG seed for the noise.

    Returns:
        complex64 array of length sample_rate * 60.
    """
    if len(frame_bits) != FRAME_BITS:
        raise ValueError(f"need {FRAME_BITS} bits, got {len(frame_bits)}")
    if am_bit_durations_ms is None:
        am_bit_durations_ms = [200.0] * FRAME_BITS
    if len(am_bit_durations_ms) != FRAME_BITS:
        raise ValueError("am_bit_durations_ms must have 60 entries")

    n_per_second = int(round(sample_rate))
    n_total = n_per_second * FRAME_BITS
    iq = np.zeros(n_total, dtype=np.complex64)
    for k, bit in enumerate(frame_bits):
        s = k * n_per_second
        e = s + n_per_second
        # AM envelope: low for am_bit_durations_ms[k] at start, then high.
        low_samples = int(round(am_bit_durations_ms[k] * 1e-3 * sample_rate))
        amp = np.full(n_per_second, am_high, dtype=np.float32)
        amp[:low_samples] = am_low
        # PM phase: 0 for bit 0, π for bit 1.
        phase = math.pi if bit else 0.0
        iq[s:e] = (amp * (math.cos(phase) + 1j * math.sin(phase))).astype(
            np.complex64
        )
    if snr_db is not None:
        rng = np.random.default_rng(seed)
        sig_power = float(np.mean(np.abs(iq) ** 2))
        noise_power = sig_power / (10.0 ** (snr_db / 10.0))
        sigma = math.sqrt(noise_power / 2.0)
        noise = (rng.standard_normal(n_total)
                 + 1j * rng.standard_normal(n_total)) * sigma
        iq = (iq + noise.astype(np.complex64)).astype(np.complex64)
    return iq
