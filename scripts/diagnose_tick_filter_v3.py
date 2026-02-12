#!/usr/bin/env python3
"""
Diagnostic v3: Test IQ-domain correlation (no AM demod) vs AM-domain.
Also test the real root cause: the synthetic signal generation itself.
"""

import numpy as np
import sys
sys.path.insert(0, '/home/mjh/git/hf-timestd/src')

from scipy.signal import correlate, butter, sosfiltfilt
from scipy.signal.windows import tukey


def test_am_demod_ambiguity():
    """
    Demonstrate the AM demod ambiguity problem.
    
    When a tone sin(2πft) is added to complex IQ noise and we take |IQ|,
    the resulting envelope has the tone modulated by the carrier.
    The matched filter sees the tone but the correlation peak has
    ambiguity at multiples of the tone period.
    """
    sample_rate = 20000
    freq_hz = 1000.0
    tick_duration_ms = 5.0
    known_offset_ms = 3.0
    
    tick_samples = int(tick_duration_ms * sample_rate / 1000.0)
    offset_samples = int(known_offset_ms * sample_rate / 1000.0)
    
    # Generate a single second of IQ with one tick
    n = sample_rate  # 1 second
    noise_level = 0.01
    iq = noise_level * (np.random.randn(n) + 1j * np.random.randn(n))
    
    # Add tick as AM modulation: carrier * (1 + m*sin(2πft))
    # In IQ domain, this means the tick adds a sideband
    t_tick = np.arange(tick_samples) / sample_rate
    tick_signal = np.sin(2 * np.pi * freq_hz * t_tick)
    iq[offset_samples:offset_samples + tick_samples] += tick_signal * 1.0
    
    # AM demod
    audio = np.abs(iq) - np.mean(np.abs(iq))
    
    # Build template
    t = np.arange(tick_samples) / sample_rate
    window = tukey(tick_samples, alpha=0.1)
    template_sin = np.sin(2 * np.pi * freq_hz * t) * window
    template_cos = np.cos(2 * np.pi * freq_hz * t) * window
    energy = np.sqrt(np.sum(template_sin**2))
    template_sin /= energy
    template_cos /= energy
    
    # Correlate
    corr_sin = correlate(audio, template_sin, mode='valid')
    corr_cos = correlate(audio, template_cos, mode='valid')
    envelope = np.sqrt(corr_sin**2 + corr_cos**2)
    
    peak_idx = np.argmax(envelope)
    detected_ms = (peak_idx / sample_rate) * 1000.0
    
    print("AM DEMOD CORRELATION (single tick)")
    print(f"  Known offset: {known_offset_ms:.3f}ms")
    print(f"  Detected: {detected_ms:.3f}ms")
    print(f"  Error: {detected_ms - known_offset_ms:.3f}ms")
    
    # Show peaks near the expected position
    from scipy.signal import find_peaks
    peaks, _ = find_peaks(envelope, height=envelope[peak_idx] * 0.5)
    print(f"  Peaks >50% of max:")
    for p in peaks:
        p_ms = (p / sample_rate) * 1000.0
        print(f"    {p_ms:.3f}ms (height={envelope[p]:.4f}, "
              f"{envelope[p]/envelope[peak_idx]*100:.1f}%)")
    
    # Now try IQ-domain correlation
    print(f"\nIQ-DOMAIN CORRELATION (single tick)")
    
    # Complex template at tone frequency
    template_complex = np.exp(1j * 2 * np.pi * freq_hz * t) * window
    template_complex /= np.sqrt(np.sum(np.abs(template_complex)**2))
    
    corr_complex = correlate(iq, template_complex, mode='valid')
    envelope_iq = np.abs(corr_complex)
    
    peak_idx_iq = np.argmax(envelope_iq)
    detected_ms_iq = (peak_idx_iq / sample_rate) * 1000.0
    
    print(f"  Known offset: {known_offset_ms:.3f}ms")
    print(f"  Detected: {detected_ms_iq:.3f}ms")
    print(f"  Error: {detected_ms_iq - known_offset_ms:.3f}ms")
    
    peaks_iq, _ = find_peaks(envelope_iq, height=envelope_iq[peak_idx_iq] * 0.5)
    print(f"  Peaks >50% of max:")
    for p in peaks_iq:
        p_ms = (p / sample_rate) * 1000.0
        print(f"    {p_ms:.3f}ms (height={envelope_iq[p]:.6f}, "
              f"{envelope_iq[p]/envelope_iq[peak_idx_iq]*100:.1f}%)")


def test_per_tick_iq_domain():
    """Test per-tick correlation in IQ domain across a full minute."""
    sample_rate = 20000
    freq_hz = 1000.0
    tick_duration_ms = 5.0
    known_offset_ms = 3.0
    
    tick_samples = int(tick_duration_ms * sample_rate / 1000.0)
    offset_samples = int(known_offset_ms * sample_rate / 1000.0)
    
    n_samples = 60 * sample_rate
    noise_level = 0.01
    iq = noise_level * (np.random.randn(n_samples) + 1j * np.random.randn(n_samples))
    
    for sec in range(60):
        if sec in {0, 29, 59}:
            continue
        tick_start = sec * sample_rate + offset_samples
        if tick_start < 0 or tick_start + tick_samples > n_samples:
            continue
        t = np.arange(tick_samples) / sample_rate
        iq[tick_start:tick_start + tick_samples] += np.sin(2 * np.pi * freq_hz * t) * 1.0
    
    # Build complex template
    t = np.arange(tick_samples) / sample_rate
    window = tukey(tick_samples, alpha=0.1)
    template_complex = np.exp(1j * 2 * np.pi * freq_hz * t) * window
    template_complex /= np.sqrt(np.sum(np.abs(template_complex)**2))
    
    # Also build sin/cos templates for AM approach
    template_sin = np.sin(2 * np.pi * freq_hz * t) * window
    template_cos = np.cos(2 * np.pi * freq_hz * t) * window
    energy = np.sqrt(np.sum(template_sin**2))
    template_sin /= energy
    template_cos /= energy
    
    search_range_ms = 50.0
    search_samples = int(search_range_ms * sample_rate / 1000.0)
    
    offsets_iq = []
    offsets_am = []
    offsets_am_bp = []
    
    sos = butter(4, [freq_hz - 100, freq_hz + 100], btype='band', 
                 fs=sample_rate, output='sos')
    
    for sec in range(1, 58):
        if sec in {0, 29, 59}:
            continue
        
        slice_start = sec * sample_rate - search_samples
        slice_end = sec * sample_rate + tick_samples + search_samples
        if slice_start < 0 or slice_end > n_samples:
            continue
        
        iq_slice = iq[slice_start:slice_end]
        
        # IQ-domain correlation
        corr_iq = correlate(iq_slice, template_complex, mode='valid')
        env_iq = np.abs(corr_iq)
        peak_iq = np.argmax(env_iq)
        # Sub-sample
        if 0 < peak_iq < len(env_iq) - 1:
            y0, y1, y2 = env_iq[peak_iq-1], env_iq[peak_iq], env_iq[peak_iq+1]
            d = 2*(y0 - 2*y1 + y2)
            if abs(d) > 1e-10:
                peak_iq_r = peak_iq + np.clip((y0-y2)/d, -0.5, 0.5)
            else:
                peak_iq_r = float(peak_iq)
        else:
            peak_iq_r = float(peak_iq)
        offsets_iq.append((peak_iq_r - search_samples) / sample_rate * 1000.0)
        
        # AM-domain correlation (no bandpass)
        audio = np.abs(iq_slice) - np.mean(np.abs(iq_slice))
        corr_s = correlate(audio, template_sin, mode='valid')
        corr_c = correlate(audio, template_cos, mode='valid')
        env_am = np.sqrt(corr_s**2 + corr_c**2)
        peak_am = np.argmax(env_am)
        if 0 < peak_am < len(env_am) - 1:
            y0, y1, y2 = env_am[peak_am-1], env_am[peak_am], env_am[peak_am+1]
            d = 2*(y0 - 2*y1 + y2)
            if abs(d) > 1e-10:
                peak_am_r = peak_am + np.clip((y0-y2)/d, -0.5, 0.5)
            else:
                peak_am_r = float(peak_am)
        else:
            peak_am_r = float(peak_am)
        offsets_am.append((peak_am_r - search_samples) / sample_rate * 1000.0)
        
        # AM-domain with bandpass
        audio_bp = sosfiltfilt(sos, audio)
        corr_s = correlate(audio_bp, template_sin, mode='valid')
        corr_c = correlate(audio_bp, template_cos, mode='valid')
        env_am_bp = np.sqrt(corr_s**2 + corr_c**2)
        peak_am_bp = np.argmax(env_am_bp)
        if 0 < peak_am_bp < len(env_am_bp) - 1:
            y0, y1, y2 = env_am_bp[peak_am_bp-1], env_am_bp[peak_am_bp], env_am_bp[peak_am_bp+1]
            d = 2*(y0 - 2*y1 + y2)
            if abs(d) > 1e-10:
                peak_am_bp_r = peak_am_bp + np.clip((y0-y2)/d, -0.5, 0.5)
            else:
                peak_am_bp_r = float(peak_am_bp)
        else:
            peak_am_bp_r = float(peak_am_bp)
        offsets_am_bp.append((peak_am_bp_r - search_samples) / sample_rate * 1000.0)
    
    print(f"\n{'=' * 80}")
    print("PER-TICK COMPARISON: IQ vs AM vs AM+BP (full minute)")
    print(f"{'=' * 80}")
    print(f"Known offset: {known_offset_ms:.3f}ms")
    
    for label, offsets in [("IQ-domain", offsets_iq), 
                           ("AM (no BP)", offsets_am),
                           ("AM + ±100Hz BP", offsets_am_bp)]:
        o = np.array(offsets)
        print(f"\n  {label:20s}: mean={np.mean(o):.3f}ms, std={np.std(o):.3f}ms, "
              f"error={np.mean(o)-known_offset_ms:+.3f}ms, "
              f"max_err={np.max(np.abs(o-known_offset_ms)):.3f}ms")


def test_realistic_am_signal():
    """
    Test with a more realistic AM signal: carrier + AM modulation.
    In real HF reception, WWV is an AM station. The IQ baseband has:
    - DC carrier component
    - ±1000 Hz sidebands from tick modulation
    
    |IQ| recovers the envelope = carrier * (1 + m*tick)
    """
    sample_rate = 20000
    freq_hz = 1000.0
    tick_duration_ms = 5.0
    known_offset_ms = 3.0
    
    tick_samples = int(tick_duration_ms * sample_rate / 1000.0)
    offset_samples = int(known_offset_ms * sample_rate / 1000.0)
    
    n_samples = 60 * sample_rate
    t_full = np.arange(n_samples) / sample_rate
    
    # Carrier (DC in baseband) with some phase
    carrier_amplitude = 1.0
    carrier_phase = 0.5
    carrier = carrier_amplitude * np.exp(1j * carrier_phase) * np.ones(n_samples)
    
    # Add noise
    noise_level = 0.05
    noise = noise_level * (np.random.randn(n_samples) + 1j * np.random.randn(n_samples))
    
    # Add ticks as AM sidebands
    modulation = np.zeros(n_samples)
    for sec in range(60):
        if sec in {0, 29, 59}:
            continue
        tick_start = sec * sample_rate + offset_samples
        if tick_start < 0 or tick_start + tick_samples > n_samples:
            continue
        t_tick = np.arange(tick_samples) / sample_rate
        modulation[tick_start:tick_start + tick_samples] = 0.5 * np.sin(2 * np.pi * freq_hz * t_tick)
    
    # IQ = carrier * (1 + modulation) + noise
    # This is proper AM: the sidebands are at ±1000 Hz from carrier
    iq = carrier * (1.0 + modulation) + noise
    iq = iq.astype(np.complex64)
    
    # Build templates
    t = np.arange(tick_samples) / sample_rate
    window = tukey(tick_samples, alpha=0.1)
    template_sin = np.sin(2 * np.pi * freq_hz * t) * window
    template_cos = np.cos(2 * np.pi * freq_hz * t) * window
    energy = np.sqrt(np.sum(template_sin**2))
    template_sin /= energy
    template_cos /= energy
    
    # IQ template at sideband frequency
    template_complex = np.exp(1j * 2 * np.pi * freq_hz * t) * window
    template_complex /= np.sqrt(np.sum(np.abs(template_complex)**2))
    
    search_samples = int(50.0 * sample_rate / 1000.0)
    sos = butter(4, [freq_hz - 100, freq_hz + 100], btype='band', 
                 fs=sample_rate, output='sos')
    
    offsets_iq = []
    offsets_am = []
    offsets_am_bp = []
    
    for sec in range(1, 58):
        if sec in {0, 29, 59}:
            continue
        
        slice_start = sec * sample_rate - search_samples
        slice_end = sec * sample_rate + tick_samples + search_samples
        if slice_start < 0 or slice_end > n_samples:
            continue
        
        iq_slice = iq[slice_start:slice_end]
        
        # IQ-domain
        corr_iq = correlate(iq_slice, template_complex, mode='valid')
        env_iq = np.abs(corr_iq)
        peak_iq = np.argmax(env_iq)
        if 0 < peak_iq < len(env_iq) - 1:
            y0, y1, y2 = env_iq[peak_iq-1], env_iq[peak_iq], env_iq[peak_iq+1]
            d = 2*(y0 - 2*y1 + y2)
            if abs(d) > 1e-10:
                peak_iq = peak_iq + np.clip((y0-y2)/d, -0.5, 0.5)
        offsets_iq.append((peak_iq - search_samples) / sample_rate * 1000.0)
        
        # AM-domain
        audio = np.abs(iq_slice) - np.mean(np.abs(iq_slice))
        corr_s = correlate(audio, template_sin, mode='valid')
        corr_c = correlate(audio, template_cos, mode='valid')
        env_am = np.sqrt(corr_s**2 + corr_c**2)
        peak_am = np.argmax(env_am)
        if 0 < peak_am < len(env_am) - 1:
            y0, y1, y2 = env_am[peak_am-1], env_am[peak_am], env_am[peak_am+1]
            d = 2*(y0 - 2*y1 + y2)
            if abs(d) > 1e-10:
                peak_am = peak_am + np.clip((y0-y2)/d, -0.5, 0.5)
        offsets_am.append((peak_am - search_samples) / sample_rate * 1000.0)
        
        # AM + bandpass
        audio_bp = sosfiltfilt(sos, audio)
        corr_s = correlate(audio_bp, template_sin, mode='valid')
        corr_c = correlate(audio_bp, template_cos, mode='valid')
        env_bp = np.sqrt(corr_s**2 + corr_c**2)
        peak_bp = np.argmax(env_bp)
        if 0 < peak_bp < len(env_bp) - 1:
            y0, y1, y2 = env_bp[peak_bp-1], env_bp[peak_bp], env_bp[peak_bp+1]
            d = 2*(y0 - 2*y1 + y2)
            if abs(d) > 1e-10:
                peak_bp = peak_bp + np.clip((y0-y2)/d, -0.5, 0.5)
        offsets_am_bp.append((peak_bp - search_samples) / sample_rate * 1000.0)
    
    print(f"\n{'=' * 80}")
    print("REALISTIC AM SIGNAL (carrier + modulation)")
    print(f"{'=' * 80}")
    print(f"Known offset: {known_offset_ms:.3f}ms")
    
    for label, offsets in [("IQ sideband", offsets_iq),
                           ("AM envelope", offsets_am),
                           ("AM + ±100Hz BP", offsets_am_bp)]:
        o = np.array(offsets)
        print(f"  {label:20s}: mean={np.mean(o):.3f}ms, std={np.std(o):.3f}ms, "
              f"error={np.mean(o)-known_offset_ms:+.3f}ms")
    
    # Now test with SNR sweep for the best approach
    print(f"\n  SNR sweep (IQ sideband approach, realistic AM):")
    for snr_db in [40, 30, 20, 15, 10, 6]:
        np.random.seed(42)
        noise_level = carrier_amplitude / (10 ** (snr_db / 20))
        noise = noise_level * (np.random.randn(n_samples) + 1j * np.random.randn(n_samples))
        iq_snr = (carrier * (1.0 + modulation) + noise).astype(np.complex64)
        
        offsets = []
        for sec in range(1, 58):
            if sec in {0, 29, 59}:
                continue
            slice_start = sec * sample_rate - search_samples
            slice_end = sec * sample_rate + tick_samples + search_samples
            if slice_start < 0 or slice_end > n_samples:
                continue
            
            iq_slice = iq_snr[slice_start:slice_end]
            corr = correlate(iq_slice, template_complex, mode='valid')
            env = np.abs(corr)
            peak = np.argmax(env)
            if 0 < peak < len(env) - 1:
                y0, y1, y2 = env[peak-1], env[peak], env[peak+1]
                d = 2*(y0 - 2*y1 + y2)
                if abs(d) > 1e-10:
                    peak = peak + np.clip((y0-y2)/d, -0.5, 0.5)
            offsets.append((peak - search_samples) / sample_rate * 1000.0)
        
        o = np.array(offsets)
        print(f"    SNR={snr_db:2d}dB: mean={np.mean(o):.3f}ms, std={np.std(o):.3f}ms, "
              f"error={np.mean(o)-known_offset_ms:+.3f}ms")


if __name__ == '__main__':
    np.random.seed(42)
    test_am_demod_ambiguity()
    test_per_tick_iq_domain()
    test_realistic_am_signal()
