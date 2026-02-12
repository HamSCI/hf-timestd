#!/usr/bin/env python3
"""
Diagnostic script: Visualize the correlation envelope from the tick matched filter
to understand why timing scatter is ±50ms across overlapping windows.

Generates a synthetic signal with known tick positions, runs the composite
correlation, and prints the envelope structure around the search region.
"""

import numpy as np
import sys
sys.path.insert(0, '/home/mjh/git/hf-timestd/src')

from scipy.signal import correlate, butter, sosfiltfilt
from scipy.signal.windows import tukey
from hf_timestd.core.tick_matched_filter import (
    TickMatchedFilter, StationType, create_tick_filter
)


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
        # AM: add tick as real modulation on carrier
        iq[tick_start:tick_start + tick_samples] += tick * 1.0
    
    return iq


def analyze_correlation_envelope(sample_rate=20000):
    """Reproduce the exact correlation logic from _correlate_window and analyze."""
    
    print("=" * 80)
    print("TICK MATCHED FILTER CORRELATION ENVELOPE DIAGNOSTIC")
    print("=" * 80)
    
    known_offset_ms = 3.0  # Known timing offset
    iq = generate_clean_minute(timing_offset_ms=known_offset_ms, snr_db=40.0)
    
    f = create_tick_filter('WWV', sample_rate=sample_rate)
    
    # Test two adjacent windows that share 4/5 of their data
    windows = [(1, 6), (2, 7), (3, 8)]
    
    for start_sec, end_sec in windows:
        print(f"\n{'─' * 60}")
        print(f"Window: seconds {start_sec}-{end_sec-1}")
        print(f"{'─' * 60}")
        
        # Extract window IQ
        start_sample = start_sec * sample_rate
        end_sample = end_sec * sample_rate
        window_iq = iq[start_sample:end_sample]
        
        # AM demod (same as process_window)
        magnitude = np.abs(window_iq)
        audio = magnitude - np.mean(magnitude)
        
        # Bandpass filter (same as process_window)
        tick_freq = 1000.0
        bandwidth = 100.0
        nyquist = sample_rate / 2
        sos = butter(4, [tick_freq - bandwidth, tick_freq + bandwidth], 
                     btype='band', fs=sample_rate, output='sos')
        audio = sosfiltfilt(sos, audio)
        
        # Build composite template (same as _build_composite_template)
        template_sin, template_cos, valid_seconds = f._build_composite_template(
            start_sec, end_sec, minute=0
        )
        
        print(f"  Valid seconds: {valid_seconds}")
        print(f"  Audio length: {len(audio)} samples ({len(audio)/sample_rate:.1f}s)")
        print(f"  Template length: {len(template_sin)} samples ({len(template_sin)/sample_rate:.1f}s)")
        
        # Correlate (same as _correlate_window)
        corr_sin = correlate(audio, template_sin, mode='same')
        corr_cos = correlate(audio, template_cos, mode='same')
        envelope = np.sqrt(corr_sin**2 + corr_cos**2)
        
        center = len(envelope) // 2
        search_samples = int(100.0 * sample_rate / 1000.0)  # ±100ms
        search_start = max(0, center - search_samples)
        search_end = min(len(envelope), center + search_samples)
        
        search_region = envelope[search_start:search_end]
        peak_idx_local = np.argmax(search_region)
        peak_idx = search_start + peak_idx_local
        peak_value = envelope[peak_idx]
        
        offset_samples = peak_idx - center
        detected_offset_ms = (offset_samples / sample_rate) * 1000.0
        
        print(f"\n  Correlation output length: {len(envelope)}")
        print(f"  Center index: {center}")
        print(f"  Search range: [{search_start}, {search_end}] ({search_end - search_start} samples = {(search_end - search_start)/sample_rate*1000:.1f}ms)")
        print(f"  Peak index: {peak_idx} (local: {peak_idx_local})")
        print(f"  Peak offset from center: {offset_samples} samples = {detected_offset_ms:.3f}ms")
        print(f"  Known offset: {known_offset_ms:.3f}ms")
        print(f"  Error: {detected_offset_ms - known_offset_ms:.3f}ms")
        
        # Analyze the envelope structure within the search region
        # Look for multiple peaks
        from scipy.signal import find_peaks
        peaks, properties = find_peaks(search_region, height=peak_value * 0.5)
        print(f"\n  Peaks within search region (>50% of max):")
        for p in peaks:
            p_global = search_start + p
            p_offset_ms = ((p_global - center) / sample_rate) * 1000.0
            p_height = search_region[p]
            print(f"    idx={p_global}, offset={p_offset_ms:+.3f}ms, "
                  f"height={p_height:.4f} ({p_height/peak_value*100:.1f}% of peak)")
        
        # Show envelope statistics in the search region
        print(f"\n  Search region envelope stats:")
        print(f"    Max: {np.max(search_region):.6f}")
        print(f"    Mean: {np.mean(search_region):.6f}")
        print(f"    Std: {np.std(search_region):.6f}")
        print(f"    Max/Mean ratio: {np.max(search_region)/np.mean(search_region):.2f}")
        
        # Show the full envelope — where are the REAL peaks?
        full_peaks, _ = find_peaks(envelope, height=peak_value * 0.3)
        print(f"\n  All peaks in full envelope (>30% of max):")
        for p in full_peaks[:20]:  # Limit output
            p_offset_ms = ((p - center) / sample_rate) * 1000.0
            p_height = envelope[p]
            print(f"    idx={p}, offset={p_offset_ms:+.3f}ms ({p_offset_ms/1000:.3f}s), "
                  f"height={p_height:.4f} ({p_height/peak_value*100:.1f}%)")
    
    # Now test what the actual process_minute produces
    print(f"\n{'=' * 80}")
    print("FULL MINUTE PROCESSING")
    print(f"{'=' * 80}")
    
    result = f.process_minute(iq, minute_number=0, min_snr_db=3.0)
    
    offsets = [r.timing_offset_ms for r in result.window_results]
    print(f"\n  Valid windows: {result.valid_windows}/{result.total_windows}")
    print(f"  Mean offset: {result.mean_timing_offset_ms:.3f}ms (expected: {known_offset_ms:.3f}ms)")
    print(f"  Std offset: {result.std_timing_offset_ms:.3f}ms (should be < 1ms)")
    print(f"  Mean SNR: {result.mean_snr_db:.1f}dB")
    if result.drift_rate_ms_per_sec is not None:
        print(f"  Drift rate: {result.drift_rate_ms_per_sec:.4f}ms/s (should be ~0)")
    
    # Show per-window offsets for first 20 windows
    print(f"\n  Per-window offsets (first 20):")
    for r in result.window_results[:20]:
        error = r.timing_offset_ms - known_offset_ms
        print(f"    sec {r.window_start_second:2d}-{r.window_end_second-1:2d}: "
              f"offset={r.timing_offset_ms:+7.3f}ms, "
              f"error={error:+7.3f}ms, "
              f"SNR={r.snr_db:.1f}dB")
    
    print(f"\n  Offset histogram (ms):")
    hist, bin_edges = np.histogram(offsets, bins=20)
    for i in range(len(hist)):
        bar = '#' * (hist[i] * 2)
        print(f"    [{bin_edges[i]:+7.2f}, {bin_edges[i+1]:+7.2f}): {hist[i]:3d} {bar}")


def test_per_tick_approach(sample_rate=20000):
    """Test alternative: correlate each tick individually."""
    
    print(f"\n\n{'=' * 80}")
    print("PER-TICK CORRELATION APPROACH (ALTERNATIVE)")
    print(f"{'=' * 80}")
    
    known_offset_ms = 3.0
    iq = generate_clean_minute(timing_offset_ms=known_offset_ms, snr_db=30.0)
    
    freq_hz = 1000.0
    tick_duration_ms = 5.0
    tick_samples = int(tick_duration_ms * sample_rate / 1000.0)
    
    # Build single-tick template
    t = np.arange(tick_samples) / sample_rate
    window = tukey(tick_samples, alpha=0.1)
    template_sin = np.sin(2 * np.pi * freq_hz * t) * window
    template_cos = np.cos(2 * np.pi * freq_hz * t) * window
    energy = np.sqrt(np.sum(template_sin**2))
    template_sin /= energy
    template_cos /= energy
    
    # Bandpass filter
    sos = butter(4, [freq_hz - 100, freq_hz + 100], 
                 btype='band', fs=sample_rate, output='sos')
    
    search_range_ms = 50.0  # ±50ms search per tick
    search_samples = int(search_range_ms * sample_rate / 1000.0)
    
    all_offsets = []
    
    for sec in range(1, 58):  # Skip 0, 29, 59
        if sec in {0, 29, 59}:
            continue
        
        # Extract 1-second slice centered on expected tick position
        # Tick expected at sec * sample_rate, search ±50ms
        slice_start = sec * sample_rate - search_samples
        slice_end = sec * sample_rate + tick_samples + search_samples
        
        if slice_start < 0 or slice_end > len(iq):
            continue
        
        audio_slice = np.abs(iq[slice_start:slice_end])
        audio_slice = audio_slice - np.mean(audio_slice)
        audio_slice = sosfiltfilt(sos, audio_slice)
        
        # Correlate single tick template against slice
        corr_sin = correlate(audio_slice, template_sin, mode='valid')
        corr_cos = correlate(audio_slice, template_cos, mode='valid')
        envelope = np.sqrt(corr_sin**2 + corr_cos**2)
        
        if len(envelope) == 0:
            continue
        
        peak_idx = np.argmax(envelope)
        
        # The expected peak position: at search_samples (where the tick starts)
        expected_peak = search_samples
        offset_samples = peak_idx - expected_peak
        offset_ms = (offset_samples / sample_rate) * 1000.0
        
        all_offsets.append(offset_ms)
    
    all_offsets = np.array(all_offsets)
    print(f"\n  Ticks measured: {len(all_offsets)}")
    print(f"  Mean offset: {np.mean(all_offsets):.3f}ms (expected: {known_offset_ms:.3f}ms)")
    print(f"  Std offset: {np.std(all_offsets):.3f}ms")
    print(f"  Min: {np.min(all_offsets):.3f}ms, Max: {np.max(all_offsets):.3f}ms")
    
    print(f"\n  Per-tick offsets (first 20):")
    for i, offset in enumerate(all_offsets[:20]):
        error = offset - known_offset_ms
        print(f"    tick {i+1:2d}: offset={offset:+7.3f}ms, error={error:+7.3f}ms")


if __name__ == '__main__':
    np.random.seed(42)
    analyze_correlation_envelope()
    test_per_tick_approach()
