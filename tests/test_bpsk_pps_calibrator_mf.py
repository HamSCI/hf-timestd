"""Tests for the matched-filter BPSK PPS calibrator.

Focus on the algorithmic contract: given a synthetic polarity-flip BPSK
signal with a known sub-sample edge offset, does the calibrator
  (a) lock within a reasonable number of seconds,
  (b) report a chain_delay that matches the injected offset, and
  (c) stay robust under added noise representative of in-line TS1 SNR?

We don't exercise radiod / RTP transport — those are integration concerns.
"""

import numpy as np
import pytest

from hf_timestd.core.bpsk_pps_calibrator_mf import BpskPpsCalibratorMF


SR = 96_000  # design-point sample rate


def _make_bpsk_signal(
    duration_s: float,
    sample_rate: int = SR,
    edge_offset_samples: float = 0.0,
    amplitude: float = 1.0,
    noise_std: float = 0.0,
    carrier_phase: float = 0.0,
    carrier_freq_hz: float = 0.0,
    transition_width_samples: float = 2.0,
    seed: int = 42,
) -> np.ndarray:
    """Synthesize a band-limited polarity-flip BPSK signal at DC.

    Each PPS edge is a tanh transition of width ``transition_width_samples``
    samples (matched roughly to a ±25 kHz channel filter at 96 kHz, which
    has rise time ≈ 1/(2·BW) ≈ 20 µs ≈ 2 samples). The zero-crossing of
    the smoothed polarity sits precisely at ``edge_offset_samples`` modulo
    ``sample_rate``, so the calibrator's recovered chain_delay should
    match the injected offset to sub-sample precision (limited by noise
    and the parabolic-interp residual error).

    Without band-limiting, an instantaneous discrete-step flip places
    the discrete MF peak at a 2-sample flat-top whose centroid is at
    sub-sample 0.5, which would make sub-sample tests fragile.

    Returns complex64 IQ samples of length ``duration_s * sample_rate``.
    """
    rng = np.random.default_rng(seed)
    n = int(duration_s * sample_rate)
    t = np.arange(n)

    # Distance to the nearest PPS edge, with the sign of the polarity
    # transition at that edge: +1 for even-indexed edges (going - → +)
    # and -1 for odd (going + → -).
    nearest_k = np.round((t - edge_offset_samples) / sample_rate).astype(np.int64)
    nearest_edge = nearest_k * sample_rate + edge_offset_samples
    distance = t - nearest_edge
    sign = np.where(nearest_k % 2 == 0, +1.0, -1.0)
    polarity = sign * np.tanh(distance / transition_width_samples)

    if noise_std > 0:
        signal_real = amplitude * polarity + rng.normal(0, noise_std, size=n)
        noise_imag = rng.normal(0, noise_std, size=n)
    else:
        signal_real = amplitude * polarity
        noise_imag = np.zeros(n)
    # Static phase rotation, optional residual carrier frequency.  The
    # carrier_freq_hz parameter models the post-radiod-downmix offset
    # that a real BPSK signal carries (Costas's job is to track and
    # remove it before the boxcar MF integrates).
    omega = 2.0 * np.pi * carrier_freq_hz
    phase = carrier_phase + omega * t / sample_rate
    s = (signal_real + 1j * noise_imag) * np.exp(1j * phase)
    return s.astype(np.complex64)


def _feed_in_batches(cal, signal, rtp_start=0, batch_size=480):
    """Feed signal to the calibrator in batches, return final result."""
    last_result = None
    rtp = rtp_start
    for i in range(0, len(signal), batch_size):
        batch = signal[i : i + batch_size]
        r = cal.process_samples(batch, rtp)
        if r is not None:
            last_result = r
        rtp = (rtp + len(batch)) & 0xFFFFFFFF
    return last_result


def _modular_distance(a: float, b: float, modulus: float) -> float:
    """Smallest signed distance from ``b`` to ``a`` modulo ``modulus``.

    chain_delay_samples is a modular quantity in [0, SR). Comparing
    recovered vs injected via plain subtraction wrongly flags a
    near-zero-vs-near-SR pair (e.g. 95999.95 vs 0.00) as far apart
    when they are physically the same edge position.
    """
    d = (a - b) % modulus
    if d > modulus / 2:
        d -= modulus
    return abs(d)


class TestNoiseFreeLock:
    """In an idealized noise-free signal, the MF should lock within
    a handful of edges and report chain_delay matching the injected
    sub-sample offset to within ~0.05 samples."""

    def test_lock_at_zero_offset(self):
        cal = BpskPpsCalibratorMF(
            sample_rate=SR, consecutive_required=5, edge_tolerance_samples=20,
        )
        signal = _make_bpsk_signal(duration_s=10.0, edge_offset_samples=0.0)
        result = _feed_in_batches(cal, signal)
        assert result is not None, "MF calibrator failed to lock noise-free"
        assert result.locked
        # Edge at sample-of-second 0. chain_delay is in [0, SR), so
        # the recovered value is either ~0 or ~SR-ε; modular distance
        # collapses both to "near zero".
        err = _modular_distance(result.chain_delay_samples, 0.0, SR)
        assert err < 0.5, \
            f"chain_delay={result.chain_delay_samples} samples, expected ~0"

    def test_chain_delay_matches_injected_offset(self):
        """Inject a 12.3-sample edge offset; calibrator should recover
        it to within ~0.05 samples (parabolic interp limit on noise-free
        signal). At SR=96 kHz, 0.05 sample = ~520 ps — well below the
        physical timing precision."""
        injected = 12.3
        cal = BpskPpsCalibratorMF(
            sample_rate=SR, consecutive_required=5, edge_tolerance_samples=20,
        )
        signal = _make_bpsk_signal(
            duration_s=10.0, edge_offset_samples=injected,
        )
        result = _feed_in_batches(cal, signal)
        assert result is not None
        recovered = result.chain_delay_samples
        err = _modular_distance(recovered, injected, SR)
        assert err < 0.1, \
            f"recovered={recovered}, injected={injected}, err={err}"


class TestNoiseRobustness:
    """At signal-dominated SNRs (≥20 dB per sample) the calibrator
    should still lock and report chain_delay within a few sample
    fractions of the truth.

    bee1's TS1 in-line install has SNR_per_sample of ≥40 dB; we test
    at 20 dB to validate robustness, not the operating point."""

    def test_lock_at_20db_snr(self):
        injected = 5.7
        # SNR_per_sample = (A/σ)² → 20 dB = 100 ratio → σ = A/10
        cal = BpskPpsCalibratorMF(
            sample_rate=SR, consecutive_required=5, edge_tolerance_samples=50,
        )
        signal = _make_bpsk_signal(
            duration_s=15.0, edge_offset_samples=injected,
            amplitude=1.0, noise_std=0.1,
        )
        result = _feed_in_batches(cal, signal)
        assert result is not None, "Failed to lock at 20 dB per-sample SNR"
        recovered = result.chain_delay_samples
        # At 20 dB SNR over a half-second integration, σ_t per edge is
        # ≪ 1 sample. Allow 2 samples of slack for robustness.
        err = _modular_distance(recovered, injected, SR)
        assert err < 2.0, \
            f"recovered={recovered}, injected={injected}, err={err}"


class TestStreamingBehaviour:
    """Locking should not depend on how the input is sliced into
    batches — the streaming buffer should handle PPS edges that
    straddle batch boundaries."""

    def test_small_batches(self):
        cal = BpskPpsCalibratorMF(
            sample_rate=SR, consecutive_required=5, edge_tolerance_samples=20,
        )
        signal = _make_bpsk_signal(duration_s=10.0, edge_offset_samples=3.0)
        # Use 100-sample batches — about 1 ms per batch, much smaller
        # than the typical 200-sample radiod packet.
        result = _feed_in_batches(cal, signal, batch_size=100)
        assert result is not None
        assert _modular_distance(result.chain_delay_samples, 3.0, SR) < 0.1

    def test_one_giant_batch(self):
        cal = BpskPpsCalibratorMF(
            sample_rate=SR, consecutive_required=5, edge_tolerance_samples=20,
        )
        signal = _make_bpsk_signal(duration_s=10.0, edge_offset_samples=3.0)
        result = cal.process_samples(signal, rtp_timestamp=0)
        assert result is not None
        assert _modular_distance(result.chain_delay_samples, 3.0, SR) < 0.1


class TestCarrierPhaseRecovery:
    """The carrier-phase tracker must recover lock regardless of the
    initial carrier phase."""

    @pytest.mark.parametrize("phi", [0.0, np.pi / 4, np.pi / 2, np.pi, 1.234])
    def test_locks_at_arbitrary_phase(self, phi):
        cal = BpskPpsCalibratorMF(
            sample_rate=SR, consecutive_required=5, edge_tolerance_samples=20,
        )
        signal = _make_bpsk_signal(
            duration_s=10.0, edge_offset_samples=2.0, carrier_phase=phi,
        )
        result = _feed_in_batches(cal, signal)
        assert result is not None, f"Failed to lock at carrier phase {phi}"
        # chain_delay should still be ~2.0 samples regardless of phase.
        assert _modular_distance(result.chain_delay_samples, 2.0, SR) < 0.2


class TestResetClearsState:
    def test_reset_clears_lock(self):
        cal = BpskPpsCalibratorMF(
            sample_rate=SR, consecutive_required=5, edge_tolerance_samples=20,
        )
        signal = _make_bpsk_signal(duration_s=10.0, edge_offset_samples=0.0)
        _feed_in_batches(cal, signal)
        assert cal.locked
        cal.reset()
        assert not cal.locked
        assert cal.pps_consecutive == 0
        assert cal.pps_ok == 0


class TestConstructorValidation:
    def test_rejects_low_sample_rate(self):
        with pytest.raises(ValueError, match="sample_rate"):
            BpskPpsCalibratorMF(sample_rate=4000)

    def test_accepts_minimum_sample_rate(self):
        cal = BpskPpsCalibratorMF(sample_rate=8000)
        assert cal.sample_rate == 8000


# ---------------------------------------------------------------------------
# Magnitude-correlation detection path (rotation-invariant; skips Costas).
# ---------------------------------------------------------------------------


class TestMagnitudeCorrelation:
    """The magnitude-correlation path computes the MF on the COMPLEX
    signal and peak-picks on |y|.  It should:
      * lock as fast as the legacy path on a clean signal,
      * recover chain_delay to the same sub-sample precision,
      * remain locked regardless of carrier phase (the whole point),
      * still respect the chain_delay step-detection / phantom-gating
        logic that does not depend on Costas.
    """

    def test_lock_at_zero_offset_magnitude_mode(self):
        cal = BpskPpsCalibratorMF(
            sample_rate=SR, consecutive_required=5,
            edge_tolerance_samples=20,
            use_magnitude_correlation=True,
        )
        signal = _make_bpsk_signal(duration_s=10.0, edge_offset_samples=0.0)
        result = _feed_in_batches(cal, signal)
        assert result is not None, "magnitude-mode failed to lock noise-free"
        assert result.locked
        err = _modular_distance(result.chain_delay_samples, 0.0, SR)
        assert err < 0.5, \
            f"chain_delay={result.chain_delay_samples}, expected ~0"

    def test_chain_delay_matches_injected_offset(self):
        injected = 12.3
        cal = BpskPpsCalibratorMF(
            sample_rate=SR, consecutive_required=5,
            edge_tolerance_samples=20,
            use_magnitude_correlation=True,
        )
        signal = _make_bpsk_signal(
            duration_s=10.0, edge_offset_samples=injected,
        )
        result = _feed_in_batches(cal, signal)
        assert result is not None
        err = _modular_distance(result.chain_delay_samples, injected, SR)
        assert err < 0.1, \
            f"recovered={result.chain_delay_samples}, injected={injected}, err={err}"

    @pytest.mark.parametrize("phi", [0.0, np.pi / 4, np.pi / 2, np.pi, 1.234])
    def test_invariant_under_carrier_phase(self, phi):
        """Magnitude correlation is designed to be carrier-phase
        invariant — this is the WHOLE POINT vs the legacy path that
        needed a Costas loop to rotate phase out before peak-picking."""
        cal = BpskPpsCalibratorMF(
            sample_rate=SR, consecutive_required=5,
            edge_tolerance_samples=20,
            use_magnitude_correlation=True,
        )
        signal = _make_bpsk_signal(
            duration_s=10.0, edge_offset_samples=2.0, carrier_phase=phi,
        )
        result = _feed_in_batches(cal, signal)
        assert result is not None, f"failed to lock at carrier phase {phi}"
        assert _modular_distance(result.chain_delay_samples, 2.0, SR) < 0.2

    def test_lock_at_20db_snr_magnitude_mode(self):
        injected = 5.7
        cal = BpskPpsCalibratorMF(
            sample_rate=SR, consecutive_required=5,
            edge_tolerance_samples=50,
            use_magnitude_correlation=True,
        )
        signal = _make_bpsk_signal(
            duration_s=15.0, edge_offset_samples=injected,
            amplitude=1.0, noise_std=0.1,
        )
        result = _feed_in_batches(cal, signal)
        assert result is not None, "magnitude-mode failed to lock at 20 dB SNR"
        err = _modular_distance(result.chain_delay_samples, injected, SR)
        assert err < 2.0, f"recovered={result.chain_delay_samples}, err={err}"

    def test_reset_clears_complex_buffer(self):
        cal = BpskPpsCalibratorMF(
            sample_rate=SR, consecutive_required=5,
            edge_tolerance_samples=20,
            use_magnitude_correlation=True,
        )
        signal = _make_bpsk_signal(duration_s=10.0, edge_offset_samples=0.0)
        _feed_in_batches(cal, signal)
        assert cal.locked
        assert len(cal._z_buf) > 0  # complex buffer was populated
        cal.reset()
        assert not cal.locked
        assert len(cal._z_buf) == 0
        assert len(cal._I_buf) == 0

    def test_legacy_mode_default(self):
        """Default behavior (no flag) must remain the Costas+Re path —
        no surprise switch-over for existing deployments."""
        cal = BpskPpsCalibratorMF(sample_rate=SR)
        assert cal._use_magnitude_correlation is False

    @pytest.mark.parametrize("carrier_hz", [0.0, 0.2, 0.5])
    def test_locks_with_residual_carrier_frequency(self, carrier_hz):
        """Real BPSK signal has residual carrier offset after radiod's
        downmix — sub-Hz from GPSDO + RX-888 LO mismatch in normal
        operation.  Costas tracks and removes it (1 Hz loop BW); the
        magnitude path must still produce correct chain_delay through
        that tracking.

        A 2026-05-22 deploy that ran |MF(s)| WITHOUT Costas derotation
        walked to a 185 ms sidelobe in seconds — the boxcar over
        N=SR/2 samples cancels rotating signal — proving Costas's
        carrier-frequency removal is essential even though its
        lock-state gate is not.

        Limit: carrier offsets above ~1 Hz exceed the Costas loop
        bandwidth and BOTH detection paths degrade (legacy Re cancels
        in cos·polarity averaging the same way magnitude does).  For
        the GPSDO-fed TS1 install on bee1 the residual is well below
        0.5 Hz, so we test 0-0.5 Hz here; higher offsets are a
        Costas-loop tuning concern, separate from detection-path
        choice."""
        injected = 7.5
        cal = BpskPpsCalibratorMF(
            sample_rate=SR, consecutive_required=5,
            edge_tolerance_samples=20,
            use_magnitude_correlation=True,
        )
        signal = _make_bpsk_signal(
            duration_s=15.0, edge_offset_samples=injected,
            carrier_freq_hz=carrier_hz,
        )
        result = _feed_in_batches(cal, signal)
        assert result is not None, \
            f"failed to lock at carrier_freq_hz={carrier_hz}"
        err = _modular_distance(result.chain_delay_samples, injected, SR)
        # 2 samples (~21 µs at 96 kHz) of slack for Costas tracking error.
        assert err < 2.0, \
            f"carrier_hz={carrier_hz} recovered={result.chain_delay_samples} "\
            f"injected={injected} err={err}"
