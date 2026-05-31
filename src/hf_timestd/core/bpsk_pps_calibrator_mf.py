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

Costas lock-quality gate (Layer A of the TSL3 Costas-drift fix, see
docs/TSL3_COSTAS_DRIFT_2026-05-18.md): the carrier-recovery loop makes
intermittent ~10-15 s phase excursions during which the matched filter
throws strong phantom peaks.  ``_update_costas_lock`` tracks whether the
loop is quiescent; once acquired, edge acceptance is gated on it, so an
excursion makes the calibrator coast on the last-good chain delay (a
brief holdover) instead of re-locking against a phantom.

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

import logging
import time
from pathlib import Path
from typing import List, Optional

import numpy as np

from hf_timestd.core.bpsk_pps_calibrator import PpsCalibrationResult


logger = logging.getLogger(__name__)

__all__ = ['BpskPpsCalibratorMF']


# Costas lock-quality detector — Layer A of the TSL3 Costas-drift fix
# (see docs/TSL3_COSTAS_DRIFT_2026-05-18.md).  The LB-1421 → TS1 → RX-888
# chain shares one GPSDO, so the residual carrier offset is sub-Hz and a
# healthy Costas phase is near-stationary; any *sustained* phase motion
# is a carrier-recovery excursion, during which the matched filter throws
# phantom peaks that can corrupt the PPS lock.
#
# Thresholds are derived from the 2026-05-08 debug capture
# (/var/lib/timestd/debug/bpsk_mf_capture.npz, 3287 batches, one
# excursion): in normal operation the per-batch |Δφ| EMA is ≪1e-3 rad and
# φ tracks its EMA to ~1e-4 rad; through the excursion the |Δφ| EMA sat at
# ~0.012-0.015 rad and φ swung >5 rad off.  The two regimes are separated
# by 5-10×, so the exact values are not critical — a sweep across them all
# reproduced the same clean separation.  They are physically motivated
# (sub-Hz carrier ⇒ stationary phase), not operator knobs, so they are
# module constants rather than config; Layer B may revisit them.
# The φ-EMA tracks legitimate sub-Hz drift but is frozen while the loop
# is in motion, so it cannot chase φ into an excursion (see
# _update_costas_lock).
COSTAS_TAU_PHASE_EMA_S = 10.0   # φ-EMA time constant (s).
COSTAS_TAU_DPHASE_EMA_S = 0.5   # |Δφ|-EMA time constant (s).
# 2026-05-21: raised from 0.004 → 0.008 → 0.020 in two passes.
#
# The original 0.004 was set against a single observed excursion that
# sat at 0.012-0.015, giving 3-4× separation from the real-event band.
# Later bee1 runs found the carrier-recovery loop's steady-state |Δφ|
# EMA had drifted into 0.005-0.009 — well below any real excursion
# but enough to repeatedly trip the gate.  An intermediate 0.008
# step cut the false-positive rate but the regime kept climbing into
# 0.008-0.009 and the gate flapped again within minutes.
#
# 2026-05-21 (fourth pass) raised to 0.050.  Even 0.030 was tripping —
# observed peaks reached 0.0309 within minutes of deploy.  The
# dphase test is now essentially a *redundant* detector since real
# excursions are reliably caught by the BAND test (they swing |φ| > 5
# rad, far above any sane band threshold).  0.050 means the dphase
# test only fires on truly aggressive loop motion (faster than any
# observed steady-state regime) while otherwise staying out of the way.
COSTAS_DPHASE_MAX_RAD = 0.050   # |Δφ| EMA above this ⇒ loop in motion.
# 2026-05-21 (second pass) raised 1.0 → 2.0.  Steady-state |φ - φ_EMA|
# observed at 1.02 — just over the previous threshold.  Real excursions
# per the original characterisation swing |φ| > 5 rad — 2.5× above the
# new threshold, so we still catch them reliably.  Chain_delay during
# every borderline band trip has stayed stable to ±100 ns, confirming
# that what the gate is rejecting is NOT a measurement-quality event.
# This is the primary detector for real excursions; tune it conservatively.
COSTAS_PHASE_BAND_RAD = 2.0     # |φ − φ_EMA| above this ⇒ φ wandered off.
COSTAS_RELOCK_S = 0.5           # φ quiescent this long (s) before re-lock.


# Genuine chain-delay step detection — TSL3 displaced-reference fix
# (see docs/TSL3_COSTAS_DRIFT_2026-05-18.md).  Once acquired, an
# off-position edge is treated as a phantom and held inert; the lock is
# never moved by a transient.  A *real* chain-delay step (an RF/DSP
# change — a cable, a radiod filter reconfig) is rare and moves the true
# edge to a new fixed sample-of-second; it is distinguished from a
# phantom burst purely by persistence.  Only after this many consecutive
# off-position edges all agree on one new position does the calibrator
# re-home its lock.  At 1 PPS this is ~60 s — comfortably longer than the
# ~10-15 s Costas excursions, so excursion phantoms can never be mistaken
# for a step.
STEP_CONFIRM_EDGES = 60


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
        debug_dump_path: Optional[str] = None,
        debug_dump_seconds: float = 60.0,
        debug_dump_subthreshold_factor: float = 0.2,
        phase_log_period_batches: int = 0,
        use_magnitude_correlation: bool = False,
    ):
        """
        Args:
            use_magnitude_correlation: If True, peak-pick on |MF(s_rot)|
                (complex matched filter, take magnitude) instead of
                MF(Re(s_rot)) (legacy).  Costas is still used to remove
                residual carrier *frequency* — without that derotation
                a sub-Hz offset accumulates >N·ω·Δt phase rotation over
                the N=SR/2 boxcar window and the sums cancel toward
                zero; a 2026-05-22 deploy that tried to drop Costas
                entirely walked chain_delay to a 185 ms sidelobe within
                seconds.  What we DO drop is the costas_locked GATE —
                |MF(s_rot)| is rotation-invariant to any small residual
                phase error θ (|e^(jθ)|=1), so we no longer need to
                refuse edges during brief Costas excursions.  This
                eliminates the per-restart chain_delay disambiguation
                drift inherent to the Re(s_rot) path's amplitude
                dependence on θ.  See docs/HF-PPS-CHRONY-TUNING.md §5.2.
        """
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

        # Costas lock-quality state (Layer A TSL3 fix — see
        # _update_costas_lock and the COSTAS_* module constants).  The
        # EMA coefficients and the relock-debounce length are pinned
        # lazily alongside _alpha (they depend on the per-batch dt).
        # _phase_ema is the band reference; it is frozen whenever the
        # loop is in motion so it cannot chase phase into an excursion.
        self._costas_phase_ema_alpha: Optional[float] = None
        self._costas_dphase_ema_alpha: Optional[float] = None
        self._costas_relock_batches: Optional[int] = None
        self._phase_ema: Optional[float] = None
        self._dphase_ema: float = 0.0
        self._costas_locked: bool = False
        self._costas_relock_counter: int = 0

        self._use_magnitude_correlation = bool(use_magnitude_correlation)

        # Real-path I buffer (Costas-rotated real signal).  Used when
        # use_magnitude_correlation == False.
        self._I_buf = np.zeros(0, dtype=np.float32)
        # Complex-path z buffer (raw IQ).  Used when
        # use_magnitude_correlation == True; lets the MF integrate the
        # COMPLEX signal so the polarity-flip energy is preserved
        # regardless of where Costas thinks the phase is.
        self._z_buf = np.zeros(0, dtype=np.complex64)
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
        # Off-position edges seen while acquired (phantoms — see
        # _detect_and_record_peaks).  Diagnostic only: a phantom is inert,
        # it does not touch the lock.
        self.pps_phantom: int = 0
        self.pps_consecutive: int = 0
        # ACQUIRING (False): every detected edge walks ``_last_edge_rtp``
        # so the bootstrap converges on whatever offset it can find
        # consistently.  TRACKING (True — set once ``pps_consecutive``
        # first reaches ``consecutive_required``, never cleared without
        # ``reset()``): the true PPS edge is GPSDO-pinned to a fixed
        # sample-of-second, so an edge more than ``edge_tolerance_samples``
        # off is a phantom and is held inert (it cannot reset the lock or
        # move the reference).  Only a persistent run of off-position
        # edges — a genuine chain-delay step — re-homes the lock; see
        # ``_note_step_candidate`` and ``STEP_CONFIRM_EDGES``.
        self._acquired: bool = False
        self._chain_delay_samples: Optional[float] = None
        # Genuine-step candidate tracker (TSL3 displaced-reference fix):
        # the RTP of the most recent off-position edge and how many
        # consecutive off-position edges have agreed on that position.
        self._step_candidate_rtp: Optional[int] = None
        self._step_candidate_count: int = 0

        # Diagnostic capture (opt-in via TOML).  When enabled, records
        # the matched-filter output ``y`` and every sub/above-threshold
        # peak the detector finds, then dumps a single NPZ when the
        # capture window closes.  Used to diagnose why the calibrator
        # sometimes re-locks against a candidate ~60 samples (= 2 ×
        # edge_tolerance_samples at 96 kHz) away from the original PPS
        # edge — the cluster pattern visible in production logs hints
        # at a deterministic phantom peak whose source we don't yet
        # know (Costas π-flip, MF sidelobe, multipath, dual-source).
        # Captured fields per batch: y_full slice, rtp_at_y slice, the
        # Costas phase, and a wall-clock timestamp.  Per peak: rtp,
        # |y|, sign, threshold, peak_running, accept/reject reason.
        self._debug_path: Optional[Path] = (
            Path(debug_dump_path) if debug_dump_path else None
        )
        self._debug_dump_seconds: float = float(debug_dump_seconds)
        self._debug_subthreshold_factor: float = float(
            debug_dump_subthreshold_factor
        )
        self._debug_started_wall: Optional[float] = None
        self._debug_done: bool = False
        self._debug_y_chunks: List[np.ndarray] = []
        self._debug_rtp_chunks: List[np.ndarray] = []
        self._debug_phase_per_batch: List[float] = []
        self._debug_batch_wall: List[float] = []
        # Each peak record: (batch_idx, peak_rtp, peak_rtp_frac, ay,
        # threshold, peak_running, sign_y, classification, gap_to_last,
        # offset_d, last_edge_rtp_at_eval).
        # classification: 0=accepted, 1=rejected_offset, 2=skip_short_gap,
        # 3=below_threshold (sub-threshold debug capture only),
        # 4=costas_unlocked (peak seen while the Costas-lock gate was
        # coasting — no lock state was touched).
        self._debug_peaks: List[tuple] = []

        # Periodic Costas-phase log emit (Phase 1 of the 2026-05-08
        # Costas-drift investigation).  When non-zero, every Nth batch
        # the calibrator emits a one-line ``logger.info`` with the
        # current Costas phase, peak_running, pps state, and batch_max
        # |y| so that a 24+ hour journal can be parsed offline to look
        # for periodicity, time-of-day correlation, or coupling to
        # other system events around the ~13-second phase excursions
        # seen in the diagnostic dump.
        self._phase_log_period_batches: int = int(phase_log_period_batches)
        self._phase_log_counter: int = 0
        self._phase_log_last_pps_noise: int = 0
        self._phase_log_last_pps_ok: int = 0

    @property
    def locked(self) -> bool:
        return self.pps_consecutive >= self.consecutive_required

    @property
    def costas_locked(self) -> bool:
        """True when the Costas carrier-recovery loop is quiescent — phase
        near-stationary and within band of its slow EMA.  False during a
        carrier-recovery excursion (see ``_update_costas_lock``).  Once
        ``_acquired``, edge acceptance is gated on this so an excursion
        coasts on the last-good chain delay (Layer A TSL3 fix)."""
        return self._costas_locked

    def reset(self) -> None:
        self._phase = 0.0
        self._phase_initialized = False
        self._phase_ema = None
        self._dphase_ema = 0.0
        self._costas_locked = False
        self._costas_relock_counter = 0
        self._I_buf = np.zeros(0, dtype=np.float32)
        self._z_buf = np.zeros(0, dtype=np.complex64)
        self._rtp_buf = np.zeros(0, dtype=np.int64)
        self._last_edge_rtp = None
        self._last_y_tail = np.zeros(0, dtype=np.float64)
        self._last_rtp_tail = np.zeros(0, dtype=np.int64)
        self._peak_running = None
        self.pps_ok = 0
        self.pps_noise = 0
        self.pps_phantom = 0
        self.pps_consecutive = 0
        self._acquired = False
        self._chain_delay_samples = None
        self._step_candidate_rtp = None
        self._step_candidate_count = 0
        self._phase_log_counter = 0
        self._phase_log_last_pps_noise = 0
        self._phase_log_last_pps_ok = 0

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
            # Costas lock-quality coefficients — pinned here because they
            # depend on the per-batch dt, exactly like _alpha.
            self._costas_phase_ema_alpha = float(
                1.0 - np.exp(-dt / COSTAS_TAU_PHASE_EMA_S)
            )
            self._costas_dphase_ema_alpha = float(
                1.0 - np.exp(-dt / COSTAS_TAU_DPHASE_EMA_S)
            )
            self._costas_relock_batches = max(
                1, int(np.ceil(COSTAS_RELOCK_S / dt))
            )

        # Costas: square + halve-angle gives carrier phase modulo π.
        sq_mean = np.mean(s.astype(np.complex128) ** 2)
        phi_estimate = float(0.5 * np.angle(sq_mean))
        if not self._phase_initialized:
            self._phase = phi_estimate
            self._phase_initialized = True
            phase_increment = 0.0
        else:
            # Wrap delta to [-π/2, π/2) — the squaring leaves a π
            # ambiguity, so phi_estimate may flip by π between batches
            # if the BPSK polarity at the batch boundary differs.
            delta = ((phi_estimate - self._phase) + np.pi / 2) % np.pi - np.pi / 2
            phase_increment = self._alpha * delta
            self._phase += phase_increment

        # Costas lock-quality update (Layer A TSL3 fix).  Runs every
        # batch — including the buffer-fill phase — so the detector is
        # warm by the time edge acceptance starts gating on it.
        self._update_costas_lock(phase_increment)

        rtp_batch = (np.arange(batch_size, dtype=np.int64)
                     + np.int64(rtp_timestamp)) & 0xFFFFFFFF
        self._rtp_buf = np.concatenate([self._rtp_buf, rtp_batch])

        if self._use_magnitude_correlation:
            # MAGNITUDE-CORRELATION PATH.  Keep the Costas rotation
            # (needed to remove residual carrier *frequency* so the
            # half-second boxcar integrates coherently — without it,
            # any sub-Hz offset accumulates >N·ω·Δt phase rotation
            # over N=SR/2 samples, the sums cancel toward zero, and a
            # carrier-induced sidelobe wins peak-pick at the wrong
            # position — observed live 2026-05-22 01:48 UTC: chain_delay
            # walked to a sidelobe 185 ms off true).  Drop only the
            # final ``Re()`` projection and the costas_locked gate:
            # |MF(s_rot)| is rotation-invariant to any small residual
            # phase error θ — |e^(jθ)| = 1 — so it stays robust through
            # Costas excursions without gating edges.  This is the
            # narrow fix to the Re-path's failure modes (per-restart
            # chain_delay disambiguation drift, threshold treadmill)
            # while keeping Costas's actual essential job (frequency
            # tracking).
            s_rot = s * np.exp(-1j * self._phase)
            self._z_buf = np.concatenate(
                [self._z_buf, s_rot.astype(np.complex64)]
            )
            if len(self._z_buf) < 2 * self._N + 1:
                return self._maybe_result()
            N = self._N
            csum = np.concatenate(
                ([0.0 + 0.0j], np.cumsum(self._z_buf, dtype=np.complex128))
            )
            idx = np.arange(N, len(self._z_buf) - N)
            y_complex = ((csum[idx + N + 1] - csum[idx + 1])
                         - (csum[idx] - csum[idx - N]))
            # Take magnitude: preserves all signal energy regardless of
            # residual phase, downstream code already operates on |y|.
            y = np.abs(y_complex).astype(np.float64)
            rtp_at_y = self._rtp_buf[idx]
        else:
            # REAL-PATH (legacy) — Costas rotates s into the frame
            # where the signal is real, then MF on Re(s_rot).
            s_rot = s * np.exp(-1j * self._phase)
            I_batch = s_rot.real.astype(np.float32)
            self._I_buf = np.concatenate([self._I_buf, I_batch])

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

        # Diagnostic capture: snapshot the per-batch y / rtp / phase
        # before peak detection (so the captured signal is exactly what
        # the detector saw).  Per-batch y has no overlap with previous
        # batches; the analyzer can simply concatenate by batch order.
        if (self._debug_path is not None
                and not self._debug_done
                and len(y) > 0):
            now = time.time()
            if self._debug_started_wall is None:
                self._debug_started_wall = now
                logger.info(
                    f"BPSK MF debug capture started: path={self._debug_path}, "
                    f"window={self._debug_dump_seconds:.1f}s, "
                    f"subthreshold_factor={self._debug_subthreshold_factor}"
                )
            self._debug_y_chunks.append(y.copy())
            self._debug_rtp_chunks.append(rtp_at_y.copy())
            self._debug_phase_per_batch.append(float(self._phase))
            self._debug_batch_wall.append(now)
            if (now - self._debug_started_wall) >= self._debug_dump_seconds:
                self._flush_debug()

        if len(y_full) >= 3:
            self._detect_and_record_peaks(y_full, rtp_full)

        # Carry over the trailing 2 samples for next call.
        if len(y) >= 2:
            self._last_y_tail = y[-2:].copy()
            self._last_rtp_tail = rtp_at_y[-2:].copy()

        # Slide buffer: keep last 2*N samples for next batch.
        if self._use_magnitude_correlation:
            self._z_buf = self._z_buf[-2 * N:]
        else:
            self._I_buf = self._I_buf[-2 * N:]
        self._rtp_buf = self._rtp_buf[-2 * N:]

        # Periodic phase log (Phase 1 of Costas-drift investigation).
        # Emitted at the end of process_samples so peak_running and
        # pps counters reflect any peaks detected this batch.
        if self._phase_log_period_batches > 0:
            self._phase_log_counter += 1
            if self._phase_log_counter >= self._phase_log_period_batches:
                self._phase_log_counter = 0
                d_noise = self.pps_noise - self._phase_log_last_pps_noise
                d_ok = self.pps_ok - self._phase_log_last_pps_ok
                self._phase_log_last_pps_noise = self.pps_noise
                self._phase_log_last_pps_ok = self.pps_ok
                batch_max_y = float(np.max(np.abs(y))) if len(y) > 0 else 0.0
                logger.info(
                    f"T6 MF phase_log: phase_rad={self._phase:+.4f} "
                    f"phase_deg={self._phase * 180.0 / np.pi:+.2f} "
                    f"peak_running={self._peak_running or 0:.2f} "
                    f"batch_max_y={batch_max_y:.2f} "
                    f"pps_consec={self.pps_consecutive} "
                    f"acquired={int(self._acquired)} "
                    f"costas_locked={int(self._costas_locked)} "
                    f"dphase_ema={self._dphase_ema:.5f} "
                    f"pps_phantom={self.pps_phantom} "
                    f"step_cand={self._step_candidate_count} "
                    f"d_ok={d_ok} d_noise={d_noise} "
                    f"chain_delay_ns={self._chain_delay_samples * 1e9 / self.sample_rate if self._chain_delay_samples else 0:.0f}"
                )

        return self._maybe_result()

    def _update_costas_lock(self, phase_increment: float) -> None:
        """Update the Costas lock-quality state from this batch's realised
        phase increment, and log any locked⇄unlocked transition.

        Two tests (see the COSTAS_* module constants and
        docs/TSL3_COSTAS_DRIFT_2026-05-18.md):

          motion — the |Δφ| EMA stays below ``COSTAS_DPHASE_MAX_RAD``.  A
            healthy loop barely moves (sub-Hz carrier); an excursion
            slews it.
          band   — φ stays within ``COSTAS_PHASE_BAND_RAD`` of
            ``_phase_ema``, a slow EMA of φ that is *frozen whenever the
            motion test fails*, so it cannot follow φ into an excursion
            and silently re-validate a wandered phase.

        ``_costas_locked`` is motion ∧ band, with a ``COSTAS_RELOCK_S``
        debounce on the unlocked→locked edge so edge acceptance only
        resumes once φ is fully settled.  Unlock is immediate — it is the
        protective direction.
        """
        self._dphase_ema += self._costas_dphase_ema_alpha * (
            abs(phase_increment) - self._dphase_ema
        )
        motion_ok = self._dphase_ema <= COSTAS_DPHASE_MAX_RAD

        # Band reference: track φ only while the loop is quiescent, so an
        # excursion cannot drag _phase_ema along with it.
        if self._phase_ema is None:
            self._phase_ema = self._phase
        elif motion_ok:
            self._phase_ema += self._costas_phase_ema_alpha * (
                self._phase - self._phase_ema
            )
        band_ok = abs(self._phase - self._phase_ema) <= COSTAS_PHASE_BAND_RAD

        quiescent = motion_ok and band_ok
        was_locked = self._costas_locked
        if self._costas_locked:
            if not quiescent:
                self._costas_locked = False
                self._costas_relock_counter = 0
        elif quiescent:
            self._costas_relock_counter += 1
            if self._costas_relock_counter >= self._costas_relock_batches:
                self._costas_locked = True
        else:
            self._costas_relock_counter = 0

        if was_locked and not self._costas_locked:
            logger.warning(
                "T6 Costas UNLOCKED (carrier-recovery excursion): "
                f"phase_rad={self._phase:+.4f} "
                f"dphase_ema={self._dphase_ema:.5f} "
                f"phase_ema={self._phase_ema:+.4f} "
                f"acquired={int(self._acquired)} — edge acceptance gated, "
                "coasting on last-good chain delay"
            )
        elif not was_locked and self._costas_locked:
            logger.info(
                f"T6 Costas re-locked: phase_rad={self._phase:+.4f} "
                f"dphase_ema={self._dphase_ema:.5f} — edge acceptance resumed"
            )

    def _note_step_candidate(self, edge_rtp_int: int) -> bool:
        """Track a run of off-position edges to tell a genuine chain-delay
        step from a transient phantom burst (TSL3 displaced-reference fix).

        Each off-position edge seen while acquired is fed here.  If it
        agrees (within ``edge_tolerance_samples``) with the running
        candidate's within-second position, the run extends; otherwise
        the run restarts at this edge.  Returns ``True`` once the run
        reaches ``STEP_CONFIRM_EDGES`` — the caller then re-homes the lock
        to the new position.  An accepted on-position edge clears the
        candidate (done by the caller), so a phantom burst — which is
        interleaved with real edges — can never reach the threshold; only
        a true step, where the old edge is simply gone, can.
        """
        if self._step_candidate_rtp is not None:
            cur_off = edge_rtp_int % self.sample_rate
            cand_off = self._step_candidate_rtp % self.sample_rate
            dc = (cur_off - cand_off) % self.sample_rate
            if dc >= self.sample_rate // 2:
                dc -= self.sample_rate
            if abs(dc) <= self.edge_tolerance_samples:
                self._step_candidate_count += 1
                self._step_candidate_rtp = edge_rtp_int
                return self._step_candidate_count >= STEP_CONFIRM_EDGES
        # First off-position edge, or one inconsistent with the running
        # candidate — (re)start the run here.
        self._step_candidate_rtp = edge_rtp_int
        self._step_candidate_count = 1
        return False

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

        # Costas-lock gate (Layer A TSL3 fix).  Once acquired, a
        # carrier-recovery excursion must never corrupt the lock: while
        # the Costas loop is unlocked the matched filter throws phantom
        # peaks (see docs/TSL3_COSTAS_DRIFT_2026-05-18.md), so we accept
        # no edges and touch none of _last_edge_rtp / pps_* /
        # _chain_delay_samples / _peak_running.  The calibrator coasts on
        # the last-good chain delay — T6 / HPPS holds, like a leap-
        # second hold — until the loop re-locks.  The gate is inert
        # during acquisition (_acquired False) so the bootstrap can
        # still walk the reference toward a consistent offset.
        #
        # 2026-05-31 update — gate now applies in MAGNITUDE-CORRELATION
        # mode too.  An earlier comment claimed |y_complex| was
        # Costas-invariant and bypassed the gate, but live data on
        # bee1 disproved that: magnitude is invariant to PHASE error
        # (|e^(jθ)| = 1) but NOT to FREQUENCY error accumulating across
        # the half-second integration.  During a Costas excursion the
        # loop briefly mistracks frequency; the half-second sum loses
        # coherence and a sidelobe peak wins.  Observed live 2026-05-31
        # 01:39:13 UTC: chain_delay snapped 423 ms → 369 ms in a single
        # batch where dphase_ema spiked 7× and pps_phantom jumped 284,
        # leading to a step-recovery cycle and biased re-lock.  Re-
        # enabling the gate restores the documented coast-through-
        # excursion behaviour.
        if self._acquired and not self._costas_locked:
            # Still record the phantoms for the debug capture (Layer B
            # analysis) — but mutate no lock state.  Uses the frozen
            # _peak_running / threshold from before the excursion.
            if (self._debug_path is not None
                    and not self._debug_done
                    and self._debug_started_wall is not None
                    and self._peak_running is not None):
                batch_idx = len(self._debug_y_chunks) - 1
                debug_threshold = (
                    self._debug_subthreshold_factor * self._peak_running
                )
                frozen_threshold = 0.5 * self._peak_running
                for pi in peak_idx:
                    if ay[pi] >= debug_threshold:
                        self._debug_peaks.append((
                            batch_idx, int(rtp_at_y[pi]), 0.0,
                            float(ay[pi]), float(frozen_threshold),
                            float(self._peak_running),
                            float(np.sign(y[pi])),
                            4,  # costas_unlocked
                            0, 0, self._last_edge_rtp or 0,
                            self.pps_consecutive,
                        ))
            return

        # Adaptive threshold from running peak estimate. Bootstrap on
        # the first observation (which may overshoot a real edge if
        # we hit one mid-batch); subsequent batches adapt slowly.
        batch_max = float(ay[peak_idx].max())
        # NaN-guard. If the upstream MF output is NaN-poisoned (e.g.
        # from a radiod gap or an RTP-wrap-math glitch) some |y|
        # values come back NaN, ay[peak_idx].max() returns NaN, and
        # an unguarded IIR update permanently corrupts _peak_running
        # — the threshold check `ay[pi] < threshold` then fails on
        # every subsequent batch, no PPS detections are accepted, and
        # TSL3 goes dark until the watchdog restarts the process.
        # Treat NaN as "no observation this batch" and additionally
        # self-heal if _peak_running itself is currently non-finite
        # (so a future clean batch can recover without a restart).
        if not np.isfinite(batch_max):
            return
        if self._peak_running is None or not np.isfinite(self._peak_running):
            self._peak_running = batch_max
        else:
            # IIR toward batch_max but clamped from below by 0.99×
            # previous (so a quiet batch doesn't drop the threshold).
            self._peak_running = max(
                self._peak_running * 0.99,
                self._peak_running * 0.95 + batch_max * 0.05,
            )
        threshold = 0.5 * self._peak_running
        debug_active = (
            self._debug_path is not None
            and not self._debug_done
            and self._debug_started_wall is not None
        )
        debug_threshold = (
            self._debug_subthreshold_factor * self._peak_running
            if debug_active else None
        )
        batch_idx = len(self._debug_y_chunks) - 1 if debug_active else -1

        for pi in peak_idx:
            # CLASSIFICATION constants:
            #   0 accepted, 1 rejected_offset, 2 skip_short_gap,
            #   3 below_threshold, 4 costas_unlocked.
            if ay[pi] < threshold:
                if debug_active and ay[pi] >= debug_threshold:
                    self._debug_peaks.append((
                        batch_idx, int(rtp_at_y[pi]), 0.0,
                        float(ay[pi]), float(threshold),
                        float(self._peak_running),
                        float(np.sign(y[pi])),
                        3,  # below_threshold
                        0, 0, self._last_edge_rtp or 0,
                        self.pps_consecutive,
                    ))
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
            gap_dbg = 0
            d_dbg = 0
            last_edge_at_eval = self._last_edge_rtp or 0

            if self._last_edge_rtp is not None:
                gap = (edge_rtp_int - self._last_edge_rtp) & 0xFFFFFFFF
                if gap > 0x7FFFFFFF:
                    gap -= 0x100000000
                gap_dbg = int(gap)
                # Reject sidelobes / spurious peaks <0.99 s away.
                if abs(gap) < int(0.99 * self.sample_rate):
                    if debug_active:
                        self._debug_peaks.append((
                            batch_idx, edge_rtp_int, edge_rtp_frac,
                            float(ay[pi]), float(threshold),
                            float(self._peak_running),
                            float(np.sign(y[pi])),
                            2,  # skip_short_gap
                            gap_dbg, 0, last_edge_at_eval,
                            self.pps_consecutive,
                        ))
                    continue

                cur_off = edge_rtp_int % self.sample_rate
                prev_off = self._last_edge_rtp % self.sample_rate
                d = (cur_off - prev_off) % self.sample_rate
                if d >= self.sample_rate // 2:
                    d -= self.sample_rate
                d_dbg = int(d)
                if abs(d) > self.edge_tolerance_samples:
                    if not self._acquired:
                        # ACQUIRING — no lock to protect yet; walk the
                        # reference freely so the bootstrap converges.
                        self.pps_noise += 1
                        self.pps_consecutive = 0
                        self._last_edge_rtp = edge_rtp_int
                        if debug_active:
                            self._debug_peaks.append((
                                batch_idx, edge_rtp_int, edge_rtp_frac,
                                float(ay[pi]), float(threshold),
                                float(self._peak_running),
                                float(np.sign(y[pi])),
                                1,  # rejected_offset
                                gap_dbg, d_dbg, last_edge_at_eval,
                                self.pps_consecutive,
                            ))
                        continue

                    # ACQUIRED — the true PPS edge is GPSDO-pinned to a
                    # fixed sample-of-second and cannot physically drift,
                    # so an edge this far off is a phantom (a Costas /
                    # matched-filter sidelobe on the ~100 ms grid), not
                    # the true edge.  A phantom is INERT: it does not
                    # reset ``pps_consecutive`` and does not walk
                    # ``_last_edge_rtp``, so a phantom burst can neither
                    # break the lock nor hop it to another grid cell —
                    # the calibrator coasts on its last-good chain delay.
                    # Only a persistent run of off-position edges that
                    # agree on one new position is a genuine chain-delay
                    # step; ``_note_step_candidate`` confirms that and the
                    # edge then falls through to be accepted at the new
                    # operating point.
                    self.pps_phantom += 1
                    stepped = self._note_step_candidate(edge_rtp_int)
                    if debug_active:
                        self._debug_peaks.append((
                            batch_idx, edge_rtp_int, edge_rtp_frac,
                            float(ay[pi]), float(threshold),
                            float(self._peak_running),
                            float(np.sign(y[pi])),
                            1,  # rejected_offset (phantom — lock held)
                            gap_dbg, d_dbg, last_edge_at_eval,
                            self.pps_consecutive,
                        ))
                    if not stepped:
                        continue
                    logger.warning(
                        f"T6 BPSK chain-delay step adopted: lock re-homed "
                        f"{d_dbg:+d} samples "
                        f"({d_dbg * 1e6 / self.sample_rate:+.1f} us) after "
                        f"{STEP_CONFIRM_EDGES} consistent off-position "
                        f"edges — a genuine RF/DSP chain-delay change"
                    )

            self.pps_ok += 1
            self.pps_consecutive += 1
            # An on-position edge (or a just-confirmed step) means the
            # lock is sound — clear any pending step candidate.
            self._step_candidate_rtp = None
            self._step_candidate_count = 0
            if (not self._acquired
                    and self.pps_consecutive >= self.consecutive_required):
                self._acquired = True

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
            if debug_active:
                self._debug_peaks.append((
                    batch_idx, edge_rtp_int, edge_rtp_frac,
                    float(ay[pi]), float(threshold),
                    float(self._peak_running),
                    float(np.sign(y[pi])),
                    0,  # accepted
                    gap_dbg, d_dbg, last_edge_at_eval,
                    self.pps_consecutive,
                ))

    def _flush_debug(self) -> None:
        """Write the captured y / peaks / phase to NPZ, then disable
        further capture for this process."""
        if self._debug_path is None or self._debug_done:
            return
        try:
            self._debug_path.parent.mkdir(parents=True, exist_ok=True)
            # Concatenate per-batch arrays.  Per-batch y and rtp_at_y
            # have no overlap, so straight concat gives a clean
            # continuous signal.
            y_concat = (np.concatenate(self._debug_y_chunks)
                        if self._debug_y_chunks else np.zeros(0))
            rtp_concat = (np.concatenate(self._debug_rtp_chunks)
                          if self._debug_rtp_chunks
                          else np.zeros(0, dtype=np.int64))
            # Per-batch indexing into the concatenated arrays.
            chunk_lens = np.array(
                [len(c) for c in self._debug_y_chunks], dtype=np.int64
            )
            batch_starts = np.concatenate(
                [[0], np.cumsum(chunk_lens)[:-1]]
            ) if len(chunk_lens) else np.zeros(0, dtype=np.int64)
            phase_per_batch = np.array(
                self._debug_phase_per_batch, dtype=np.float64
            )
            batch_wall = np.array(self._debug_batch_wall, dtype=np.float64)
            # Peaks as 2-D float64 array (lossy on rtp ints — keep the
            # int columns separate to preserve full precision).
            if self._debug_peaks:
                peaks_arr = np.array(
                    [
                        [
                            r[0], r[1], r[2], r[3], r[4], r[5], r[6],
                            r[7], r[8], r[9], r[10], r[11],
                        ]
                        for r in self._debug_peaks
                    ],
                    dtype=np.float64,
                )
            else:
                peaks_arr = np.zeros((0, 12), dtype=np.float64)
            np.savez_compressed(
                self._debug_path,
                y=y_concat,
                rtp=rtp_concat,
                batch_starts=batch_starts,
                phase_per_batch=phase_per_batch,
                batch_wall=batch_wall,
                peaks=peaks_arr,
                sample_rate=np.int64(self.sample_rate),
                edge_tolerance_samples=np.int64(self.edge_tolerance_samples),
                consecutive_required=np.int64(self.consecutive_required),
                # Column legend for `peaks` (for offline analyzers):
                # 0 batch_idx (into batch_starts/phase_per_batch),
                # 1 peak_rtp_int, 2 peak_rtp_frac (sub-sample),
                # 3 ay, 4 threshold, 5 peak_running, 6 sign_y,
                # 7 classification (0 acc, 1 rej_offset,
                #   2 skip_short_gap, 3 below_threshold,
                #   4 costas_unlocked),
                # 8 gap_to_last (samples), 9 offset_d (samples,
                #   within-second), 10 last_edge_rtp_at_eval,
                # 11 pps_consecutive_post.
                peaks_columns=np.array(
                    [
                        'batch_idx', 'peak_rtp_int', 'peak_rtp_frac',
                        'ay', 'threshold', 'peak_running', 'sign_y',
                        'classification', 'gap_to_last', 'offset_d',
                        'last_edge_rtp_at_eval', 'pps_consecutive_post',
                    ],
                    dtype=object,
                ),
            )
            logger.warning(
                f"BPSK MF debug capture COMPLETE: file={self._debug_path}, "
                f"batches={len(self._debug_y_chunks)}, "
                f"y_samples={len(y_concat)}, peaks={len(self._debug_peaks)}"
            )
        except Exception as e:
            logger.error(f"BPSK MF debug flush failed: {e}", exc_info=True)
        finally:
            # Drop references regardless so we don't keep megabytes of
            # arrays around if write failed mid-way.
            self._debug_y_chunks = []
            self._debug_rtp_chunks = []
            self._debug_phase_per_batch = []
            self._debug_batch_wall = []
            self._debug_peaks = []
            self._debug_done = True

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
