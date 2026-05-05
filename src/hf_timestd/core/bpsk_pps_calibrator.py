"""
BPSK PPS chain-delay calibrator (hf-timestd metrology)

Detects Pulse-Per-Second edges in a BPSK-modulated IQ signal injected
into the RF front-end by a local GPS-disciplined transmitter (e.g.,
Turn Island Systems TS1 + LeoBodnar LB-1421 GPSDO).  The measured edge
positions quantify the end-to-end latency through the
RF -> ADC -> DSP -> RTP chain, producing a chain_delay_ns correction.

This calibrator was originally located in ka9q-python's
``ka9q.pps_calibrator`` (ported there from Scott Newell\'s
``wd-record.c bpsk_state_machine()``).  It moved to hf-timestd in
2026-05 because chain-delay calibration is timing-payload semantics
that doesn\'t belong in a generic RTP transport library; ka9q-python
should remain a stream-container manager for radiod\'s multicast
output, with all metrology semantics (calibration, fusion, propagation
correction) living in hf-timestd.

Local additions vs. the original Newell port:
  * Fractional-sample interpolation: at each integer-sample edge
    detection, linear-interpolate the zero-crossing of the rotated
    in-phase component between samples i-1 and i.  Removes the
    1/2-sample quantization floor (62.5 us at 16 kHz); new floor is
    SNR-bounded.  Opt-in via ``enable_fractional_interpolation``
    (default True).

The reported ``chain_delay_ns`` is consumed by hf-timestd metrology
code; how / whether to apply it to wall-time computations and archive
timestamps is a metrology decision (not a transport-library decision).

Usage:
    from hf_timestd.core.bpsk_pps_calibrator import BpskPpsCalibrator

    cal = BpskPpsCalibrator(sample_rate=16000)

    def on_samples(samples, quality):
        result = cal.process_samples(samples, quality.last_rtp_timestamp)
        if result is not None and result.locked:
            # result.chain_delay_ns is the measured RF chain delay
            ...

Requires: numpy
"""

from dataclasses import dataclass
from typing import Optional

import numpy as np

__all__ = ['BpskPpsCalibrator', 'PpsCalibrationResult', 'NotchFilter500Hz']


@dataclass
class PpsCalibrationResult:
    """Result from a successful PPS calibration measurement."""
    chain_delay_ns: int        # Measured chain delay (nanoseconds)
    chain_delay_samples: float # Measured chain delay (fractional samples)
    pps_ok: int                # Cumulative valid edge count
    pps_noise: int             # Cumulative noise/rejected edge count
    pps_consecutive: int       # Current consecutive valid edge streak
    locked: bool               # True when consecutive >= lock threshold


class NotchFilter500Hz:
    """
    Biquad IIR notch filter at 500 Hz.

    Direct form II transposed, matching wd-record's notch500 implementation.
    Pole radius 0.99 gives a narrow notch (~10 Hz at 24 kHz sample rate).
    """

    def __init__(self, sample_rate: int, pole_radius: float = 0.99):
        w0 = 2.0 * np.pi * 500.0 / sample_rate
        c = np.cos(w0)

        # Numerator (zeros on unit circle at w0)
        self.b0 = 1.0
        self.b1 = -2.0 * c
        self.b2 = 1.0

        # Denominator (poles at radius r, angle w0)
        self.a1 = -2.0 * pole_radius * c
        self.a2 = pole_radius * pole_radius

        # Filter state (I and Q processed independently)
        self._state_i = np.zeros(2, dtype=np.float64)  # [x1, x2] not needed; use y1,y2
        self._xi1 = 0.0
        self._xi2 = 0.0
        self._yi1 = 0.0
        self._yi2 = 0.0
        self._xq1 = 0.0
        self._xq2 = 0.0
        self._yq1 = 0.0
        self._yq2 = 0.0

    def process(self, iq_samples: np.ndarray) -> np.ndarray:
        """
        Apply notch filter to complex IQ samples.

        Filters I and Q channels independently, matching wd-record behavior.
        Processes sample-by-sample to maintain state across calls.
        """
        out = np.empty_like(iq_samples)
        i_in = iq_samples.real.astype(np.float64)
        q_in = iq_samples.imag.astype(np.float64)

        b0, b1, b2 = self.b0, self.b1, self.b2
        a1, a2 = self.a1, self.a2

        # Unpack state for speed in the loop
        xi1, xi2, yi1, yi2 = self._xi1, self._xi2, self._yi1, self._yi2
        xq1, xq2, yq1, yq2 = self._xq1, self._xq2, self._yq1, self._yq2

        i_out = np.empty(len(iq_samples), dtype=np.float64)
        q_out = np.empty(len(iq_samples), dtype=np.float64)

        for n in range(len(iq_samples)):
            x_i = i_in[n]
            y_i = b0 * x_i + b1 * xi1 + b2 * xi2 - a1 * yi1 - a2 * yi2
            xi2, xi1 = xi1, x_i
            yi2, yi1 = yi1, y_i
            i_out[n] = y_i

            x_q = q_in[n]
            y_q = b0 * x_q + b1 * xq1 + b2 * xq2 - a1 * yq1 - a2 * yq2
            xq2, xq1 = xq1, x_q
            yq2, yq1 = yq1, y_q
            q_out[n] = y_q

        # Save state
        self._xi1, self._xi2, self._yi1, self._yi2 = xi1, xi2, yi1, yi2
        self._xq1, self._xq2, self._yq1, self._yq2 = xq1, xq2, yq1, yq2

        out = (i_out + 1j * q_out).astype(np.complex64)
        return out


class BpskPpsCalibrator:
    """
    Detects PPS edges in a BPSK IQ stream and measures RF chain delay.

    The injector produces a BPSK signal whose phase flips 180 degrees on
    each UTC second boundary.  This class detects those phase transitions
    in the IQ sample stream and measures where they fall relative to the
    RTP timestamp grid, yielding the end-to-end chain delay.

    Parameters
    ----------
    sample_rate : int
        Sample rate of the BPSK IQ channel (Hz).
    consecutive_required : int
        Number of consecutive valid PPS edges required before declaring
        lock and reporting a calibration result.  Default 10 matches
        wd-record.
    edge_tolerance_samples : int
        Maximum deviation (samples) of a detected edge from its expected
        position within the second.  Default 10 matches wd-record.
    min_pulse_fraction : float
        Minimum fraction of one second between consecutive edges.
        Edges closer than this are rejected as noise.  Default 0.99.
    enable_notch_500hz : bool
        Apply a 500 Hz notch filter before edge detection.
    enable_fractional_interpolation : bool
        Estimate sub-sample edge position by linear-interpolating the
        zero-crossing of the rotated in-phase component between the two
        samples bracketing the BPSK transition. Removes the 1/2-sample
        quantization floor; new floor is SNR-bounded. Default True.
        PATCH (hf-timestd): added by local override; pending upstream PR.
    amplitude_gate_fraction : float
        Per-batch amplitude gate: only consider phase-jump events for
        edge detection when both bracketing samples have ``|s|`` above
        this fraction of the batch's median amplitude.  Default 0.5
        is permissive but eliminates the noise-edge floor we observe
        at higher sample rates (15-33 % noise rate without the gate
        on bee1; <1 % with).  Set to 0.0 to disable.
        PATCH (hf-timestd): added by local override; pending upstream PR.
    """

    def __init__(
        self,
        sample_rate: int,
        consecutive_required: int = 10,
        edge_tolerance_samples: int = 10,
        min_pulse_fraction: float = 0.99,
        enable_notch_500hz: bool = False,
        enable_fractional_interpolation: bool = True,
        amplitude_gate_fraction: float = 0.05,
    ):
        self.sample_rate = sample_rate
        self.consecutive_required = consecutive_required
        self.edge_tolerance_samples = edge_tolerance_samples
        self.min_pulse_samples = int(sample_rate * min_pulse_fraction)
        self.enable_fractional_interpolation = bool(enable_fractional_interpolation)
        self.amplitude_gate_fraction = float(amplitude_gate_fraction)
        # Cross-batch amplitude tracking for the gate: we need the
        # previous sample's amplitude when i == 0 of a new batch.
        self._last_amp_sq: Optional[float] = None

        # Edge detection state
        self._last_angle: Optional[float] = None
        self._last_edge_offset: Optional[int] = None  # sample offset within second
        self._last_edge_rtp: Optional[int] = None      # absolute RTP timestamp of last edge
        self._sample_counter: int = 0                   # running count for RTP reconstruction

        # Counters (match wd-record globals)
        self.pps_ok: int = 0
        self.pps_noise: int = 0
        self.pps_consecutive: int = 0

        # Result: measured chain delay as fractional samples from second boundary
        self._chain_delay_samples: Optional[float] = None

        # Optional notch filter
        self._notch: Optional[NotchFilter500Hz] = None
        if enable_notch_500hz:
            self._notch = NotchFilter500Hz(sample_rate)

    @property
    def locked(self) -> bool:
        """True when enough consecutive valid edges have been detected."""
        return self.pps_consecutive >= self.consecutive_required

    def reset(self):
        """Reset all state. Call if the stream is restarted."""
        self._last_angle = None
        self._last_edge_offset = None
        self._last_edge_rtp = None
        self._sample_counter = 0
        self.pps_ok = 0
        self.pps_noise = 0
        self.pps_consecutive = 0
        self._chain_delay_samples = None
        self._last_amp_sq = None
        if self._notch is not None:
            self._notch = NotchFilter500Hz(self.sample_rate)

    def process_samples(
        self, iq_samples: np.ndarray, rtp_timestamp: int
    ) -> Optional[PpsCalibrationResult]:
        """
        Process a batch of complex IQ samples and detect PPS edges.

        Parameters
        ----------
        iq_samples : np.ndarray
            Complex64 IQ samples from the BPSK channel.
        rtp_timestamp : int
            RTP timestamp of the first sample in this batch.

        Returns
        -------
        PpsCalibrationResult or None
            Returns a result when locked (consecutive valid edges >=
            threshold).  Returns None while still acquiring.
        """
        if len(iq_samples) == 0:
            return None

        samples = iq_samples.astype(np.complex64)

        # Apply optional notch filter
        if self._notch is not None:
            samples = self._notch.process(samples)

        # Compute per-sample phase angle in degrees
        angles = np.degrees(np.angle(samples))

        # PATCH (hf-timestd): pre-compute |s|^2 per sample for the
        # amplitude gate.  Using batch median as a robust signal-level
        # estimate — most samples are at signal amplitude; transitions
        # and noise dips are a small minority that the median rejects.
        amps_sq = (samples.real.astype(np.float64) ** 2
                   + samples.imag.astype(np.float64) ** 2)
        if self.amplitude_gate_fraction > 0.0 and len(amps_sq) > 0:
            amp_threshold = self.amplitude_gate_fraction * float(np.median(amps_sq))
        else:
            amp_threshold = 0.0

        sr = self.sample_rate

        for i in range(len(angles)):
            angle = angles[i]
            # RTP timestamp for this specific sample
            ts = (rtp_timestamp + i) & 0xFFFFFFFF

            # PATCH (hf-timestd): amplitude gate.  Phase angle of a
            # low-amplitude sample is essentially random; gating the
            # edge classification on both bracketing samples having
            # signal-level amplitude eliminates the noise-edge floor
            # that otherwise cascades through ``_last_edge_rtp``.
            cur_amp_sq = float(amps_sq[i])
            prev_amp_sq = (float(amps_sq[i - 1]) if i > 0
                           else (self._last_amp_sq if self._last_amp_sq is not None else 0.0))
            amplitude_ok = (
                amp_threshold == 0.0
                or (cur_amp_sq >= amp_threshold and prev_amp_sq >= amp_threshold)
            )

            if self._last_angle is not None and amplitude_ok:
                # Phase difference between consecutive samples
                angle_diff = angle - self._last_angle
                # Wrap to [-360, 360] -- not strictly necessary but matches
                # the C code's fabs() comparison range
                if angle_diff > 360.0:
                    angle_diff -= 360.0
                elif angle_diff < -360.0:
                    angle_diff += 360.0

                abs_diff = abs(angle_diff)

                # Edge detected: phase transition between 90 and 270 degrees
                if 90.0 < abs_diff < 270.0:
                    noisy = False

                    # Check 1: edge position within the second should be
                    # consistent (within tolerance of last edge's position)
                    if self._last_edge_rtp is not None:
                        current_offset = ts % sr
                        expected_offset = self._last_edge_rtp % sr
                        delta = _signed_mod(
                            current_offset - expected_offset, sr
                        )
                        if abs(delta) > self.edge_tolerance_samples:
                            noisy = True

                        # Check 2: edges must be at least min_pulse_fraction
                        # of one second apart
                        rtp_gap = (ts - self._last_edge_rtp) & 0xFFFFFFFF
                        if rtp_gap > 0x7FFFFFFF:
                            rtp_gap -= 0x100000000
                        if abs(rtp_gap) < self.min_pulse_samples:
                            noisy = True

                    if noisy:
                        self.pps_noise += 1
                        self.pps_consecutive = 0
                    else:
                        self.pps_ok += 1
                        self.pps_consecutive += 1

                        # The chain delay is how far into the second the
                        # edge actually arrives.  At the sample rate, the
                        # PPS edge *should* land exactly on a second
                        # boundary (ts % sr == 0).  The measured offset is
                        # the chain delay.
                        #
                        # PATCH (hf-timestd): if fractional interpolation
                        # is enabled, refine the integer-sample edge index
                        # by linear-interpolating the zero-crossing of the
                        # rotated in-phase component between samples[i-1]
                        # and samples[i]. Falls back to integer when
                        # interpolation is not possible.
                        ts_fractional = float(ts)
                        if self.enable_fractional_interpolation:
                            sub = self._interpolate_edge(samples, i)
                            if sub is not None:
                                ts_fractional = float(ts) - 1.0 + sub

                        ts_int = int(ts_fractional)
                        ts_frac = ts_fractional - ts_int
                        self._chain_delay_samples = float(ts_int % sr) + ts_frac
                        # Handle wrap: if offset > sr/2 it's negative
                        if self._chain_delay_samples > sr / 2:
                            self._chain_delay_samples -= sr

                    self._last_edge_rtp = ts

            self._last_angle = angle
            self._last_amp_sq = float(amps_sq[i])

        self._sample_counter += len(angles)

        # Return result when locked
        if self.locked and self._chain_delay_samples is not None:
            delay_seconds = self._chain_delay_samples / sr
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


    def _interpolate_edge(self, samples, i):
        """PATCH (hf-timestd): linear-interpolate sub-sample edge position.

        At a BPSK transition, the in-phase component (after rotating to
        align the pre-transition phase with the real axis) flips sign.
        The zero-crossing between samples[i-1] and samples[i] gives the
        true edge position with sub-sample precision.

        Returns offset in [0, 1] from sample i-1 to sample i, or None if
        interpolation is not possible (edge near batch start, near-zero
        reference amplitude, or no sign change).
        """
        if i < 3:
            return None
        ref = samples[i - 3]
        ref_amp = abs(ref)
        if ref_amp < 1e-9:
            return None
        rot = np.conj(ref) / ref_amp
        i_prev = (samples[i - 1] * rot).real
        i_cur = (samples[i] * rot).real
        if (i_prev >= 0) == (i_cur >= 0):
            return None
        denom = i_prev - i_cur
        if denom == 0:
            return None
        offset = i_prev / denom
        if not (0.0 <= offset <= 1.0):
            return None
        return float(offset)


def _signed_mod(value: int, modulus: int) -> int:
    """Signed modular distance -- returns value in [-modulus/2, modulus/2)."""
    result = value % modulus
    if result >= modulus // 2:
        result -= modulus
    return result
