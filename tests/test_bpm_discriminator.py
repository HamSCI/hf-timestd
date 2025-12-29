import numpy as np
import pytest

from hf_timestd.core.bpm_discriminator import BPMDiscriminator


def _synthetic_bpm_minute_marker(sample_rate: int = 20000) -> np.ndarray:
    """
    Build a synthetic IQ buffer that includes a 300 ms, 1000 Hz minute marker
    followed by low-level noise. This exercises the marker detector end-to-end.

    The signal models AM reception: the magnitude envelope contains a 1000 Hz
    tone during the marker interval. We achieve this by amplitude-modulating
    a carrier with a 1000 Hz sinusoid so that np.abs(iq) yields the tone.
    """
    duration_sec = 0.6
    n_samples = int(sample_rate * duration_sec)
    t = np.arange(n_samples) / sample_rate

    rng = np.random.default_rng(0)
    # Low-level complex noise (models receiver noise floor)
    noise = (0.01 * (rng.standard_normal(n_samples) + 1j * rng.standard_normal(n_samples))).astype(
        np.complex64
    )

    marker_len = int(0.3 * sample_rate)
    t_marker = t[:marker_len]

    # AM modulation: magnitude = DC + tone at 1000 Hz
    # This ensures np.abs(signal) contains the 1000 Hz component the detector expects.
    modulation = 1.0 + 0.9 * np.sin(2 * np.pi * 1000 * t_marker)
    # Carrier phase can be arbitrary; use slow drift for realism
    carrier = np.exp(1j * 2 * np.pi * 50 * t_marker)
    tone = (5.0 * modulation * carrier).astype(np.complex64)

    signal = noise.copy()
    signal[:marker_len] += tone
    return signal


def test_detect_minute_marker_returns_expected_window():
    """Ensure the new minute marker detector finds the long tick near second zero."""
    sample_rate = 20000
    iq_samples = _synthetic_bpm_minute_marker(sample_rate)
    discriminator = BPMDiscriminator(expected_delay_ms=40.0)

    detection = discriminator._detect_minute_marker(iq_samples, sample_rate)

    assert detection is not None, "Minute marker should be detected in synthetic signal"
    # Marker begins near the buffer start and should last roughly 300 ms
    assert 0.0 <= detection["toa_ms"] <= 20.0
    assert 200.0 <= detection["duration_ms"] <= 350.0
    assert detection["snr_db"] > 0.0


def test_analyze_uses_minute_marker_delay(monkeypatch):
    """
    Verify analyze() overrides measured_delay_ms with the detected minute marker ToA
    and propagates that into the discrimination result.
    """
    sample_rate = 20000
    iq_samples = np.zeros(sample_rate, dtype=np.complex64)
    discriminator = BPMDiscriminator(expected_delay_ms=40.0)

    # Force deterministic helper behavior so we isolate marker usage.
    monkeypatch.setattr(discriminator, "_measure_tick_duration", lambda *args, **kwargs: 10.0)
    monkeypatch.setattr(discriminator, "_measure_snr", lambda *args, **kwargs: 0.0)
    monkeypatch.setattr(
        discriminator,
        "_detect_minute_marker",
        lambda *args, **kwargs: {"toa_ms": 47.0, "duration_ms": 280.0, "snr_db": 18.0},
    )

    result = discriminator.analyze(
        iq_samples=iq_samples,
        sample_rate=sample_rate,
        minute=0,
        measured_delay_ms=5.0,  # Should be ignored in favor of marker ToA
        snr_db=0.0,
        hour=0,
    )

    assert result.is_bpm_detected
    assert result.measured_delay_ms == pytest.approx(47.0, abs=1e-6)
    assert result.delay_residual_ms == pytest.approx(7.0, abs=1e-6)
    assert result.snr_db == pytest.approx(18.0, abs=1e-6)


def test_detect_minute_marker_rejects_noise_only():
    """Ensure detector returns None when given pure noise (no marker)."""
    sample_rate = 20000
    rng = np.random.default_rng(42)
    noise = (0.01 * (rng.standard_normal(sample_rate) + 1j * rng.standard_normal(sample_rate))).astype(
        np.complex64
    )
    discriminator = BPMDiscriminator(expected_delay_ms=40.0)

    detection = discriminator._detect_minute_marker(noise, sample_rate)

    assert detection is None, "Should not detect marker in pure noise"


def test_detect_minute_marker_rejects_short_pulse():
    """Ensure detector rejects pulses shorter than 150ms (not a minute marker)."""
    sample_rate = 20000
    n_samples = int(sample_rate * 0.6)
    t = np.arange(n_samples) / sample_rate

    rng = np.random.default_rng(0)
    noise = (0.01 * (rng.standard_normal(n_samples) + 1j * rng.standard_normal(n_samples))).astype(
        np.complex64
    )

    # Create a short 50ms pulse (regular tick, not minute marker)
    pulse_len = int(0.05 * sample_rate)
    t_pulse = t[:pulse_len]
    modulation = 1.0 + 0.9 * np.sin(2 * np.pi * 1000 * t_pulse)
    carrier = np.exp(1j * 2 * np.pi * 50 * t_pulse)
    pulse = (5.0 * modulation * carrier).astype(np.complex64)

    signal = noise.copy()
    signal[:pulse_len] += pulse

    discriminator = BPMDiscriminator(expected_delay_ms=40.0)
    detection = discriminator._detect_minute_marker(signal, sample_rate)

    assert detection is None, "Should reject short pulse (not a minute marker)"


def test_detect_minute_marker_empty_input():
    """Ensure detector handles empty input gracefully."""
    discriminator = BPMDiscriminator(expected_delay_ms=40.0)

    detection = discriminator._detect_minute_marker(np.array([], dtype=np.complex64), 20000)

    assert detection is None
