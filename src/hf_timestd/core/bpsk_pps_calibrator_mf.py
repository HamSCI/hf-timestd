"""BPSK PPS calibrator — matched-filter implementation.

Drop-in replacement for ``bpsk_pps_calibrator.BpskPpsCalibrator`` with a
textbook signal-processing chain instead of the per-sample-Δφ heuristic
inherited from Scott Newell's wd-record port:

  1. Single-shot Costas-style carrier phase recovery per batch
     (square-and-halve-angle, low-pass filtered across batches as the
     loop filter — LB-1421 → TS1 → RX-888 are reference-locked through
     a common 27 MHz GPSDO clock chain, so residual carrier offset is
     sub-Hz; a slow loop is plenty).
  2. Boxcar matched filter — y[n] = Σ I_rot[n+1:n+N+1] − Σ I_rot[n−N:n]
     where N = sample_rate/2. For a polarity-flip-once-per-second BPSK
     signal this is the optimal MF: the signal is rectangular within
     each half-second, so a rectangular template maximises SNR. The
     output peaks at each PPS transition with magnitude N·A.
  3. Peak detection via three-point local-max test on |y|.
  4. Parabolic sub-sample interpolation around the peak — Cramér-Rao-
     limited timing precision in the band-limited-step regime.

Avoids the cascade / amplitude-gate / acquisition-vs-tracking state
machinery in the legacy calibrator: the matched filter integrates
noise out coherently rather than relying on tolerance windows around
an evolving reference, so noise-edge classification is unnecessary.

Recommended radiod channel configuration: ±25 kHz filter at 96 kHz
sample rate. See sweep_bpsk_filter_widths.py for the verification
(σ_t scales as 1/B in the band-limited-step regime; wider is better
up to the receiver / aliasing limit).

Expected timing precision at the recommended config: per-edge raw
σ_t ~30 ns; chrony-combined ~5–10 ns (below LB-1421's PPS jitter
floor, so we end up measuring the GPSDO).

The output dataclass is the same ``PpsCalibrationResult`` exported
from the legacy module — a downstream consumer can switch between the
two implementations via a config flag.

Requires: numpy
"""

from __future__ import annotations

from typing import Optional

import numpy as np

from hf_timestd.core.bpsk_pps_calibrator import PpsCalibrationResult

__all__ = ['BpskPpsCalibratorMF']


class BpskPpsCalibratorMF:
    """Matched-filter BPSK PPS calibrator.

    Parameters
    ----------
    sample_rate : int
        Sample rate of the BPSK IQ channel (Hz).
    consecutive_required : int
        Number of consecutive valid edges required before declaring
        lock. Default 10 matches the legacy calibrator.
    edge_tolerance_samples : int
        Maximum deviation (samples) of a detected edge from the
        previous edge's position within the second. Default 10.
    costas_loop_bw_hz : float
        Loop bandwidth of the carrier-phase tracking filter, in Hz.
        Default 1.0 — slow because the GPSDO topology means residual
        offset is sub-Hz; setting this too high lets noise into the
        phase estimate.
    """

    def __init__(
        self,
        sample_rate: int,
        consecutive_required: int = 10,
        edge_tolerance_samples: int = 10,
        costas_loop_bw_hz: float = 1.0,
    ):
        if sample_rate < 8000:
            raise ValueError(
                f"sample_rate must be ≥ 8000 Hz "
                f"(MF needs SR/2 ≥ 4000 samples per half-second); "
                f"got {sample_rate}"
            )
        self.sample_rate = int(sample_rate)
        self.consecutive_required = int(consecutive_required)
        self.edge_tolerance_samples = int(edge_tolerance_samples)
        self._N = self.sample_rate // 2

        # Costas state. Loop coefficient α = 1 - exp(-2π·BW·dt), where
        # dt is per-batch; we don't know the batch size until the
        # first call, so α is pinned lazily there.
        self._costas_loop_bw_hz = float(costas_loop_bw_hz)
        self._phase: float = 0.0
        self._phase_initialized = False
        self._alpha: Optional[float] = None

        self._I_buf = np.zeros(0, dtype=np.float32)
        self._rtp_buf = np.zeros(0, dtype=np.int64)

        self._last_edge_rtp: Optional[int] = None
        self._last_y_tail = np.zeros(0, dtype=np.float64)
        self._last_rtp_tail = np.zeros(0, dtype=np.int64)

        # Adaptive peak-height tracker — bootstrapped from the largest
        # |y| in the first y-eligible batch and updated thereafter
        # toward observed peaks. Threshold = 0.5 × _peak_running.
        # Median-based thresholds don't work here: a clean triangular-
        # wave MF output has median ≈ half-peak (the median is on the
        # ramp, not in noise), so 3×median > peak. Real noisy data
        # happens to make median-based work because the noise dominates
        # the median, but the synthetic case breaks it.
        self._peak_running: Optional[float] = None

        self.pps_ok: int = 0
        self.pps_noise: int = 0
        self.pps_consecutive: int = 0
        self._chain_delay_samples: Optional[float] = None

    @property
    def locked(self) -> bool:
        return self.pps_consecutive >= self.consecutive_required

    def reset(self) -> None:
        self._phase = 0.0
        self._phase_initialized = False
        self._I_buf = np.zeros(0, dtype=np.float32)
        self._rtp_buf = np.zeros(0, dtype=np.int64)
        self._last_edge_rtp = None
        self._last_y_tail = np.zeros(0, dtype=np.float64)
        self._last_rtp_tail = np.zeros(0, dtype=np.int64)
        self._peak_running = None
        self.pps_ok = 0
        self.pps_noise = 0
        self.pps_consecutive = 0
        self._chain_delay_samples = None

    def process_samples(
        self, iq_samples: np.ndarray, rtp_timestamp: int,
    ) -> Optional[PpsCalibrationResult]:
        if len(iq_samples) == 0:
            return None
        s = iq_samples.astype(np.complex64)
        batch_size = len(s)

        if self._alpha is None:
            dt = batch_size / self.sample_rate
            self._alpha = float(
                1.0 - np.exp(-2.0 * np.pi * self._costas_loop_bw_hz * dt)
            )

        # Costas: square + halve-angle gives carrier phase modulo π.
        sq_mean = np.mean(s.astype(np.complex128) ** 2)
        phi_estimate = float(0.5 * np.angle(sq_mean))
        if not self._phase_initialized:
            self._phase = phi_estimate
            self._phase_initialized = True
        else:
            # Wrap delta to [-π/2, π/2) — the squaring leaves a π
            # ambiguity, so phi_estimate may flip by π between batches
            # if the BPSK polarity at the batch boundary differs.
            delta = ((phi_estimate - self._phase) + np.pi / 2) % np.pi - np.pi / 2
            self._phase += self._alpha * delta

        s_rot = s * np.exp(-1j * self._phase)
        I_batch = s_rot.real.astype(np.float32)
        rtp_batch = (np.arange(batch_size, dtype=np.int64)
                     + np.int64(rtp_timestamp)) & 0xFFFFFFFF

        self._I_buf = np.concatenate([self._I_buf, I_batch])
        self._rtp_buf = np.concatenate([self._rtp_buf, rtp_batch])

        if len(self._I_buf) < 2 * self._N + 1:
            return self._maybe_result()

        # MF: y[i] = sum(buf[i+1:i+N+1]) - sum(buf[i-N:i]).
        # Computed via cumsum for O(L) per batch.
        N = self._N
        csum = np.concatenate(
            ([0.0], np.cumsum(self._I_buf, dtype=np.float64))
        )
        idx = np.arange(N, len(self._I_buf) - N)
        y = ((csum[idx + N + 1] - csum[idx + 1])
             - (csum[idx] - csum[idx - N]))
        rtp_at_y = self._rtp_buf[idx]

        # Splice carryover from previous batch so peaks straddling the
        # boundary aren't lost (three-point peak detection needs 1
        # sample on each side of the candidate).
        if len(self._last_y_tail) > 0:
            y_full = np.concatenate([self._last_y_tail, y])
            rtp_full = np.concatenate([self._last_rtp_tail, rtp_at_y])
        else:
            y_full = y
            rtp_full = rtp_at_y

        if len(y_full) >= 3:
            self._detect_and_record_peaks(y_full, rtp_full)

        # Carry over the trailing 2 samples for next call.
        if len(y) >= 2:
            self._last_y_tail = y[-2:].copy()
            self._last_rtp_tail = rtp_at_y[-2:].copy()

        # Slide buffer: keep last 2*N samples for next batch.
        self._I_buf = self._I_buf[-2 * N:]
        self._rtp_buf = self._rtp_buf[-2 * N:]

        return self._maybe_result()

    def _detect_and_record_peaks(
        self, y: np.ndarray, rtp_at_y: np.ndarray,
    ) -> None:
        ay = np.abs(y)
        # Local-max test: ≥ on the left side, > on the right. The
        # asymmetry handles flat-top peaks (where a polarity flip
        # falls exactly between integer samples, the discrete MF can
        # report identical |y| at two adjacent samples) by picking
        # the leading edge of the flat region.
        is_peak = (ay[1:-1] >= ay[:-2]) & (ay[1:-1] > ay[2:])
        peak_idx = np.where(is_peak)[0] + 1
        if len(peak_idx) == 0:
            return

        # Adaptive threshold from running peak estimate. Bootstrap on
        # the first observation (which may overshoot a real edge if
        # we hit one mid-batch); subsequent batches adapt slowly.
        batch_max = float(ay[peak_idx].max())
        if self._peak_running is None:
            self._peak_running = batch_max
        else:
            # IIR toward batch_max but clamped from below by 0.99×
            # previous (so a quiet batch doesn't drop the threshold).
            self._peak_running = max(
                self._peak_running * 0.99,
                self._peak_running * 0.95 + batch_max * 0.05,
            )
        threshold = 0.5 * self._peak_running

        for pi in peak_idx:
            if ay[pi] < threshold:
                continue

            # Parabolic sub-sample interp on |y|.
            denom = ay[pi - 1] - 2.0 * ay[pi] + ay[pi + 1]
            if denom == 0:
                delta = 0.0
            else:
                delta = (ay[pi - 1] - ay[pi + 1]) / (2.0 * denom)
                if not (-1.0 < delta < 1.0):
                    delta = 0.0  # parabola didn't fit; fall back to integer

            edge_rtp_int = int(rtp_at_y[pi])
            edge_rtp_frac = float(delta)

            if self._last_edge_rtp is not None:
                gap = (edge_rtp_int - self._last_edge_rtp) & 0xFFFFFFFF
                if gap > 0x7FFFFFFF:
                    gap -= 0x100000000
                # Reject sidelobes / spurious peaks <0.99 s away.
                if abs(gap) < int(0.99 * self.sample_rate):
                    continue

                cur_off = edge_rtp_int % self.sample_rate
                prev_off = self._last_edge_rtp % self.sample_rate
                d = (cur_off - prev_off) % self.sample_rate
                if d >= self.sample_rate // 2:
                    d -= self.sample_rate
                if abs(d) > self.edge_tolerance_samples:
                    self.pps_noise += 1
                    self.pps_consecutive = 0
                    self._last_edge_rtp = edge_rtp_int
                    continue

            self.pps_ok += 1
            self.pps_consecutive += 1

            edge_rtp_full = edge_rtp_int + edge_rtp_frac
            # chain_delay = "where in the second the edge arrived,"
            # in [0, SR) sample units. We deliberately don't wrap to
            # [-SR/2, SR/2) — chain_delay is a latency (always ≥0), and
            # at SR=96 kHz with a ±25 kHz channel the actual radiod
            # filter group delay can exceed 500 ms, which the legacy
            # symmetric wrap would silently flip to a negative value
            # and confuse the downstream subtraction. Downstream's
            # disambiguation logic anchors the absolute reference; the
            # calibrator's job is to report the raw modular position.
            chain_delay_samples = edge_rtp_full % self.sample_rate
            self._chain_delay_samples = float(chain_delay_samples)
            self._last_edge_rtp = edge_rtp_int

    def _maybe_result(self) -> Optional[PpsCalibrationResult]:
        if self.locked and self._chain_delay_samples is not None:
            delay_seconds = self._chain_delay_samples / self.sample_rate
            delay_ns = int(round(delay_seconds * 1_000_000_000))
            return PpsCalibrationResult(
                chain_delay_ns=delay_ns,
                chain_delay_samples=self._chain_delay_samples,
                pps_ok=self.pps_ok,
                pps_noise=self.pps_noise,
                pps_consecutive=self.pps_consecutive,
                locked=True,
            )
        return None
