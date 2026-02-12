#!/usr/bin/env python3
"""
Diagnostic v2: Test with and without bandpass filter to confirm
the bandpass is causing the correlation ambiguity.
"""

import numpy as np
import sys
sys.path.insert(0, '/home/mjh/git/hf-timestd/src')

from scipy.signal import correlate, butter, sosfiltfilt
from scipy.signal.windows import tukey


def generate_clean_minute(freq_hz=1000.0, tick_duration_ms=5.0, 
                          timing_offset_ms=3.0, snr_db=30.0,
                          sample_rate=20000):
    """Generate 60s IQ with ticks at known positions."""
    n_samples = 60 * sample_rate
    noise_level = 0.01
    iq = noise_level * (np.random.randn(n_samples) + 1j * np.random.randn(n_samples))
    
    tick_samples = int(tick_duration_ms * sample_rate / 1000.0)
    offset_samples = int(timing_offset_ms * sample_rate / 1000.0)
    
    for sec in range(60):
        if sec in {0, 29, 59}:
            continue
        tick_start = sec * sample_rate + offset_samples
        if tick_start < 0 or tick_start + tick_samples > n_samples:
            continue
        t = np.arange(tick_samples) / sample_rate
        tick = np.sin(2 * np.pi * freq_hz * t)
        iq[tick_start:tick_start + tick_samples] += tick * 1.0
    
    return iq


def per_tick_test(use_bandpass, bandwidth_hz=100.0, sample_rate=20000):
    """Test per-tick correlation with/without bandpass."""
    known_offset_ms = 3.0
    freq_hz = 1000.0
    tick_duration_ms = 5.0
    
    iq = generate_clean_minute(timing_offset_ms=known_offset_ms, snr_db=30.0)
    
    tick_samples = int(tick_duration_ms * sample_rate / 1000.0)
    
    # Build single-tick template
    t = np.arange(tick_samples) / sample_rate
    window = tukey(tick_samples, alpha=0.1)
    template_sin = np.sin(2 * np.pi * freq_hz * t) * window
    template_cos = np.cos(2 * np.pi * freq_hz * t) * window
    energy = np.sqrt(np.sum(template_sin**2))
    template_sin /= energy
    template_cos /= energy
    
    if use_bandpass:
        sos = butter(4, [freq_hz - bandwidth_hz, freq_hz + bandwidth_hz], 
                     btype='band', fs=sample_rate, output='sos')
    
    search_range_ms = 50.0
    search_samples = int(search_range_ms * sample_rate / 1000.0)
    
    all_offsets = []
    
    for sec in range(1, 58):
        if sec in {0, 29, 59}:
            continue
        
        # Extract slice around expected tick position
        slice_start = sec * sample_rate - search_samples
        slice_end = sec * sample_rate + tick_samples + search_samples
        
        if slice_start < 0 or slice_end > len(iq):
            continue
        
        audio_slice = np.abs(iq[slice_start:slice_end])
        audio_slice = audio_slice - np.mean(audio_slice)
        
        if use_bandpass:
            audio_slice = sosfiltfilt(sos, audio_slice)
        
        # Correlate single tick template against slice using mode='valid'
        corr_sin = correlate(audio_slice, template_sin, mode='valid')
        corr_cos = correlate(audio_slice, template_cos, mode='valid')
        envelope = np.sqrt(corr_sin**2 + corr_cos**2)
        
        if len(envelope) == 0:
            continue
        
        peak_idx = np.argmax(envelope)
        
        # Sub-sample interpolation
        if 0 < peak_idx < len(envelope) - 1:
            y0, y1, y2 = envelope[peak_idx-1], envelope[peak_idx], envelope[peak_idx+1]
            denom = 2 * (y0 - 2*y1 + y2)
            if abs(denom) > 1e-10:
                delta = (y0 - y2) / denom
                delta = np.clip(delta, -0.5, 0.5)
                peak_idx_refined = peak_idx + delta
            else:
                peak_idx_refined = float(peak_idx)
        else:
            peak_idx_refined = float(peak_idx)
        
        expected_peak = search_samples
        offset_samples_val = peak_idx_refined - expected_peak
        offset_ms = (offset_samples_val / sample_rate) * 1000.0
        
        all_offsets.append(offset_ms)
    
    all_offsets = np.array(all_offsets)
    return all_offsets


def main():
    np.random.seed(42)
    known_offset_ms = 3.0
    
    print("=" * 80)
    print("BANDPASS FILTER IMPACT ON TIMING PRECISION")
    print("=" * 80)
    
    # Test 1: With bandpass ±100 Hz (current implementation)
    offsets_bp100 = per_tick_test(use_bandpass=True, bandwidth_hz=100.0)
    print(f"\nWith bandpass ±100 Hz (current):")
    print(f"  Mean: {np.mean(offsets_bp100):.3f}ms (expected: {known_offset_ms:.3f}ms)")
    print(f"  Std:  {np.std(offsets_bp100):.3f}ms")
    print(f"  Error: {np.mean(offsets_bp100) - known_offset_ms:+.3f}ms")
    
    # Test 2: With wider bandpass ±500 Hz
    offsets_bp500 = per_tick_test(use_bandpass=True, bandwidth_hz=500.0)
    print(f"\nWith bandpass ±500 Hz:")
    print(f"  Mean: {np.mean(offsets_bp500):.3f}ms (expected: {known_offset_ms:.3f}ms)")
    print(f"  Std:  {np.std(offsets_bp500):.3f}ms")
    print(f"  Error: {np.mean(offsets_bp500) - known_offset_ms:+.3f}ms")
    
    # Test 3: Without bandpass (matched filter only)
    offsets_nobp = per_tick_test(use_bandpass=False)
    print(f"\nWithout bandpass (matched filter only):")
    print(f"  Mean: {np.mean(offsets_nobp):.3f}ms (expected: {known_offset_ms:.3f}ms)")
    print(f"  Std:  {np.std(offsets_nobp):.3f}ms")
    print(f"  Error: {np.mean(offsets_nobp) - known_offset_ms:+.3f}ms")
    
    # Test 4: Without bandpass, different SNR levels
    print(f"\n{'=' * 80}")
    print("SNR SWEEP (no bandpass, per-tick)")
    print(f"{'=' * 80}")
    
    for snr_db in [40, 30, 20, 15, 10, 6]:
        np.random.seed(42)
        iq = generate_clean_minute(timing_offset_ms=known_offset_ms, snr_db=snr_db)
        
        # Quick per-tick test without bandpass
        freq_hz = 1000.0
        tick_duration_ms = 5.0
        sample_rate = 20000
        tick_samples = int(tick_duration_ms * sample_rate / 1000.0)
        
        t = np.arange(tick_samples) / sample_rate
        window = tukey(tick_samples, alpha=0.1)
        template_sin = np.sin(2 * np.pi * freq_hz * t) * window
        template_cos = np.cos(2 * np.pi * freq_hz * t) * window
        energy = np.sqrt(np.sum(template_sin**2))
        template_sin /= energy
        template_cos /= energy
        
        search_samples = int(50.0 * sample_rate / 1000.0)
        offsets = []
        
        for sec in range(1, 58):
            if sec in {0, 29, 59}:
                continue
            slice_start = sec * sample_rate - search_samples
            slice_end = sec * sample_rate + tick_samples + search_samples
            if slice_start < 0 or slice_end > len(iq):
                continue
            
            audio_slice = np.abs(iq[slice_start:slice_end])
            audio_slice = audio_slice - np.mean(audio_slice)
            
            corr_sin = correlate(audio_slice, template_sin, mode='valid')
            corr_cos = correlate(audio_slice, template_cos, mode='valid')
            envelope = np.sqrt(corr_sin**2 + corr_cos**2)
            
            if len(envelope) == 0:
                continue
            
            peak_idx = np.argmax(envelope)
            if 0 < peak_idx < len(envelope) - 1:
                y0, y1, y2 = envelope[peak_idx-1], envelope[peak_idx], envelope[peak_idx+1]
                denom = 2 * (y0 - 2*y1 + y2)
                if abs(denom) > 1e-10:
                    delta = (y0 - y2) / denom
                    delta = np.clip(delta, -0.5, 0.5)
                    peak_idx += delta
            
            offset_ms = ((peak_idx - search_samples) / sample_rate) * 1000.0
            offsets.append(offset_ms)
        
        offsets = np.array(offsets)
        print(f"  SNR={snr_db:2d}dB: mean={np.mean(offsets):.3f}ms, "
              f"std={np.std(offsets):.3f}ms, "
              f"error={np.mean(offsets)-known_offset_ms:+.3f}ms")
    
    # Test 5: Shared channel (WWV 1000 Hz + WWVH 1200 Hz) — need bandpass?
    print(f"\n{'=' * 80}")
    print("SHARED CHANNEL: WWV 1000 Hz + WWVH 1200 Hz")
    print(f"{'=' * 80}")
    
    np.random.seed(42)
    n_samples = 60 * 20000
    noise_level = 0.01
    iq = noise_level * (np.random.randn(n_samples) + 1j * np.random.randn(n_samples))
    
    tick_samples_wwv = int(5.0 * 20000 / 1000.0)
    offset_samples = int(known_offset_ms * 20000 / 1000.0)
    
    for sec in range(60):
        if sec in {0, 29, 59}:
            continue
        tick_start = sec * 20000 + offset_samples
        if tick_start < 0 or tick_start + tick_samples_wwv > n_samples:
            continue
        t = np.arange(tick_samples_wwv) / 20000
        # WWV at 1000 Hz
        iq[tick_start:tick_start + tick_samples_wwv] += np.sin(2 * np.pi * 1000.0 * t) * 1.0
        # WWVH at 1200 Hz (slightly different offset)
        wwvh_offset = int(22.0 * 20000 / 1000.0)  # 22ms propagation
        wwvh_start = sec * 20000 + wwvh_offset
        if wwvh_start >= 0 and wwvh_start + tick_samples_wwv <= n_samples:
            iq[wwvh_start:wwvh_start + tick_samples_wwv] += np.sin(2 * np.pi * 1200.0 * t) * 0.7
    
    # Test WWV detection with and without bandpass on shared channel
    freq_hz = 1000.0
    tick_samples = int(5.0 * 20000 / 1000.0)
    t = np.arange(tick_samples) / 20000
    window = tukey(tick_samples, alpha=0.1)
    template_sin = np.sin(2 * np.pi * freq_hz * t) * window
    template_cos = np.cos(2 * np.pi * freq_hz * t) * window
    energy = np.sqrt(np.sum(template_sin**2))
    template_sin /= energy
    template_cos /= energy
    
    search_samples = int(50.0 * 20000 / 1000.0)
    
    for label, use_bp in [("No bandpass", False), ("±100 Hz bandpass", True)]:
        if use_bp:
            sos = butter(4, [900, 1100], btype='band', fs=20000, output='sos')
        
        offsets = []
        for sec in range(1, 58):
            if sec in {0, 29, 59}:
                continue
            slice_start = sec * 20000 - search_samples
            slice_end = sec * 20000 + tick_samples + search_samples
            if slice_start < 0 or slice_end > len(iq):
                continue
            
            audio_slice = np.abs(iq[slice_start:slice_end])
            audio_slice = audio_slice - np.mean(audio_slice)
            if use_bp:
                audio_slice = sosfiltfilt(sos, audio_slice)
            
            corr_sin = correlate(audio_slice, template_sin, mode='valid')
            corr_cos = correlate(audio_slice, template_cos, mode='valid')
            envelope = np.sqrt(corr_sin**2 + corr_cos**2)
            
            if len(envelope) == 0:
                continue
            peak_idx = np.argmax(envelope)
            offset_ms = ((peak_idx - search_samples) / 20000) * 1000.0
            offsets.append(offset_ms)
        
        offsets = np.array(offsets)
        print(f"  {label:20s}: mean={np.mean(offsets):.3f}ms, "
              f"std={np.std(offsets):.3f}ms, "
              f"error={np.mean(offsets)-known_offset_ms:+.3f}ms")


if __name__ == '__main__':
    main()
