"""BPSK PPS edge detector via per-sample magnitude derivative.

Algorithm
=========

For a BPSK signal with a once-per-second polarity flip:

  Between PPS edges (constant polarity):
    s[n] ≈ ±A · e^(j·2π·Δf·n/SR)
    s[n] − s[n−1] ≈ s[n−1] · (j·2π·Δf/SR)
    |s[n] − s[n−1]| ≈ A · 2π·|Δf|/SR
  At the polarity flip:
    s[n] = +A·e^(jθ), s[n−1] = −A·e^(jθ)  (ignoring 1-sample carrier rotation)
    s[n] − s[n−1] = 2A·e^(jθ)
    |s[n] − s[n−1]| = 2A

The ratio "spike at flip" / "background between flips" is
2A / (A · 2π·|Δf|/SR) = SR / (π·|Δf|).
At SR = 96 kHz and the GPSDO + RX-888 LO mismatch's typical sub-Hz
residual, that's ~30 000:1 = ~90 dB of margin.  The polarity flip is
essentially unmissable.

What this DOESN'T need: a Costas loop, a half-second boxcar matched
filter, or any of the threshold-tuning machinery that bit the legacy
detector.  This is an oscilloscope edge-trigger applied to the complex
envelope — find where the signal is changing fastest, report that
position to sub-sample precision via parabolic interpolation.

For a band-limited polarity flip (TS1's signal is filtered through
radiod's ±25 kHz channel filter), the transition smears over the
filter's rise time of ~1/(2B) = 20 µs ≈ 2 samples at 96 kHz.  The
|d[n]| spike becomes a Gaussian-like pulse of width ~2 samples.
Parabolic interpolation on 3 samples around the peak resolves the
edge to a fraction of a sample.

Sidecar mode
============

This class is intended to run ALONGSIDE the existing matched-filter
calibrator, dumping its per-PPS edge timestamps to a CSV for offline
A/B comparison.  It DOES NOT push to chrony, modify SHM, or touch
any other state in the system.  Use ``BpskPpsCalibratorMF`` (the
legacy + opt-in magnitude paths) for the operational chain_delay.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


# Threshold for "this is a polarity flip" vs background: |d[n]| must
# exceed THRESHOLD_FACTOR × running median of |d|.  With the 90 dB
# theoretical margin, anything above ~10× the median is decisively a
# flip.  Conservative 100× keeps us comfortably above any expected
# carrier-induced jitter while well below the actual flip magnitude.
DIFF_THRESHOLD_FACTOR = 100.0

# Running-median IIR alpha — slow enough that one big spike (the
# flip we want to detect) doesn't pollute the median estimate.
DIFF_MEDIAN_IIR_ALPHA = 0.01

# Minimum gap between accepted edges, as a fraction of one second.
# PPS edges are exactly 1 s apart; anything closer is a sidelobe or
# spurious peak.
DIFF_MIN_EDGE_GAP_FRAC = 0.99


class BpskPpsCalibratorDiff:
    """Per-sample magnitude-derivative PPS edge detector.

    Sidecar / offline-analysis use only.  Does NOT push to chrony.

    Parameters
    ----------
    sample_rate : int
        IQ sample rate in Hz (typically 96000).
    output_path : Optional[str | Path]
        CSV file to append edge events to.  Header:
        ``timestamp_unix,edge_rtp_int,edge_rtp_frac,d_magnitude,median_d,chain_delay_samples``.
        If None, edges are only counted in self.pps_ok (useful for tests).
    threshold_factor : float
        |d[n]| ≥ threshold_factor × running_median(|d|) qualifies as a peak.
    """

    def __init__(
        self,
        sample_rate: int,
        output_path: Optional[str | Path] = None,
        threshold_factor: float = DIFF_THRESHOLD_FACTOR,
    ):
        self.sample_rate = int(sample_rate)
        self.threshold_factor = float(threshold_factor)
        self.output_path = Path(output_path) if output_path is not None else None

        # State across batches
        self._last_sample: Optional[np.complex64] = None
        self._last_edge_rtp: Optional[int] = None
        self._median_d: Optional[float] = None

        # Counters
        self.pps_ok: int = 0
        self.peaks_rejected_gap: int = 0
        self.peaks_rejected_threshold: int = 0
        # The last chain_delay we resolved, in [0, SR) sample units.
        # Modular like the legacy calibrator's chain_delay_samples.
        self.chain_delay_samples: Optional[float] = None

        # CSV append handle (line-buffered so external readers see new
        # rows promptly without a flush call).
        self._csv_fp = None
        if self.output_path is not None:
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
            write_header = not self.output_path.exists()
            self._csv_fp = open(self.output_path, "a", buffering=1)
            if write_header:
                self._csv_fp.write(
                    "timestamp_unix,edge_rtp_int,edge_rtp_frac,"
                    "d_magnitude,median_d,chain_delay_samples\n"
                )

        logger.info(
            f"BpskPpsCalibratorDiff initialised: sample_rate={sample_rate}, "
            f"threshold_factor={threshold_factor}, "
            f"output_path={output_path}"
        )

    def process_samples(self, iq_samples: np.ndarray, rtp_timestamp: int) -> None:
        """Process one batch of IQ samples.  Detects polarity-flip
        edges and appends them to the CSV (if open).

        Parameters
        ----------
        iq_samples : np.ndarray (complex)
            Batch of IQ samples.
        rtp_timestamp : int
            RTP timestamp of the FIRST sample in this batch.
        """
        if len(iq_samples) == 0:
            return

        s = iq_samples.astype(np.complex64)

        # Build the diff array, splicing in last_sample from previous
        # batch so a flip that straddles the boundary isn't missed.
        if self._last_sample is not None:
            s_full = np.concatenate([[self._last_sample], s])
            # diff_full[i] = s_full[i+1] - s_full[i].
            # We want d[i] = s[i] - s[i-1] (or s[0] - last_sample for i=0).
            # diff_full has len(s) entries — exactly s.shape after the splice.
            d = np.abs(np.diff(s_full)).astype(np.float64)
            # d[i] corresponds to s[i] (the "after" sample of the diff).
            rtp_at_d = (
                np.arange(len(d), dtype=np.int64) + np.int64(rtp_timestamp)
            ) & 0xFFFFFFFF
        else:
            # First call — no carryover.  Skip the first sample entirely.
            d = np.abs(np.diff(s)).astype(np.float64)
            rtp_at_d = (
                np.arange(1, len(s), dtype=np.int64) + np.int64(rtp_timestamp)
            ) & 0xFFFFFFFF

        # Save the last sample for the next batch's diff.
        self._last_sample = s[-1]

        if len(d) < 3:
            return  # need 3 samples for local-max test + parabolic interp

        # Update running median (robust to the flip spikes themselves —
        # they're rare and dwarfed by the median of ordinary samples).
        batch_median = float(np.median(d))
        if self._median_d is None:
            self._median_d = batch_median
        else:
            self._median_d = (
                (1.0 - DIFF_MEDIAN_IIR_ALPHA) * self._median_d
                + DIFF_MEDIAN_IIR_ALPHA * batch_median
            )

        threshold = self.threshold_factor * self._median_d

        # Local-max test on the interior of d (need neighbours on
        # both sides for the parabolic interp).  Asymmetric `>=` on
        # the LEFT side handles flat-top peaks: a 1-sample-wide
        # polarity transition through the channel filter produces
        # TWO equal-magnitude adjacent samples in |d|, and the strict
        # `>` test would reject both.  With `>=` on the left, the
        # LATER of the pair wins, and parabolic interp on
        # [equal, equal, smaller] resolves the true edge between them.
        interior = d[1:-1]
        is_peak = (
            (interior >= d[:-2])
            & (interior > d[2:])
            & (interior > threshold)
        )
        peak_idx = np.where(is_peak)[0] + 1
        if len(peak_idx) == 0:
            return

        now = time.time()
        for pi in peak_idx:
            # Parabolic interpolation around the peak.
            # f(x) ≈ a·x² + b·x + c, vertex at -b/(2a).
            denom = d[pi - 1] - 2.0 * d[pi] + d[pi + 1]
            if denom == 0:
                frac = 0.0
            else:
                frac = (d[pi - 1] - d[pi + 1]) / (2.0 * denom)
                if not (-1.0 < frac < 1.0):
                    frac = 0.0  # parabola didn't fit, fall back to integer

            edge_rtp_int = int(rtp_at_d[pi])
            edge_rtp_frac = float(frac)

            # Inter-edge-gap reject: PPS edges are exactly 1 s apart.
            # Anything closer is a sidelobe / spurious peak from noise.
            if self._last_edge_rtp is not None:
                gap = (edge_rtp_int - self._last_edge_rtp) & 0xFFFFFFFF
                if gap > 0x7FFFFFFF:
                    gap -= 0x100000000
                if abs(gap) < int(DIFF_MIN_EDGE_GAP_FRAC * self.sample_rate):
                    self.peaks_rejected_gap += 1
                    continue

            edge_rtp_full = edge_rtp_int + edge_rtp_frac
            chain_delay_samples = edge_rtp_full % self.sample_rate
            self.chain_delay_samples = float(chain_delay_samples)
            self._last_edge_rtp = edge_rtp_int
            self.pps_ok += 1

            if self._csv_fp is not None:
                self._csv_fp.write(
                    f"{now:.6f},{edge_rtp_int},{edge_rtp_frac:.6f},"
                    f"{d[pi]:.6f},{self._median_d:.6g},"
                    f"{chain_delay_samples:.6f}\n"
                )

    def close(self) -> None:
        """Close the CSV file handle if open."""
        if self._csv_fp is not None:
            try:
                self._csv_fp.flush()
                self._csv_fp.close()
            finally:
                self._csv_fp = None
