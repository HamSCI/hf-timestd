#!/usr/bin/env python3
"""
Detailed BCD discrimination debugging - step through the algorithm
"""
import sys
import numpy as np
from pathlib import Path
from scipy import signal as scipy_signal

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from grape_recorder.grape.analytics_service import NPZArchive
from grape_recorder.wwv_bcd_encoder import WWVBCDEncoder

def main():
    # Find a recent NPZ file
    archive_dir = Path('/tmp/timestd-test/archives/WWV_10_MHz')
    npz_files = sorted(archive_dir.glob('20251119T1[45]*.npz'))
    
    if not npz_files:
        print("No NPZ files found")
        return
    
    npz_file = npz_files[0]
    print(f"Testing with: {npz_file.name}")
    
    # Load archive
    archive = NPZArchive.load(npz_file)
    print(f"Sample rate: {archive.sample_rate} Hz")
    print(f"IQ samples: {len(archive.iq_samples)}")
    print()
    
    # Step 1: AM demodulation
    print("Step 1: AM demodulation")
    envelope = np.abs(archive.iq_samples)
    envelope = envelope - np.mean(envelope)
    print(f"  Envelope shape: {envelope.shape}")
    print(f"  Envelope range: [{np.min(envelope):.2f}, {np.max(envelope):.2f}]")
    print(f"  Envelope std: {np.std(envelope):.2f}")
    print()
    
    # Step 2: Low-pass filter
    print("Step 2: Low-pass filter (0-150 Hz)")
    nyquist = archive.sample_rate / 2
    cutoff_norm = 150 / nyquist
    sos = scipy_signal.butter(6, cutoff_norm, 'low', output='sos')
    bcd_signal = scipy_signal.sosfilt(sos, envelope)
    print(f"  BCD signal shape: {bcd_signal.shape}")
    print(f"  BCD signal range: [{np.min(bcd_signal):.2f}, {np.max(bcd_signal):.2f}]")
    print(f"  BCD signal std: {np.std(bcd_signal):.2f}")
    print()
    
    # Step 3: Generate BCD template
    print("Step 3: Generate BCD template")
    encoder = WWVBCDEncoder(sample_rate=archive.sample_rate)
    try:
        bcd_template = encoder.encode_minute(archive.unix_timestamp)
        print(f"  ✅ Template generated")
        print(f"  Template shape: {bcd_template.shape}")
        print(f"  Template range: [{np.min(bcd_template):.2f}, {np.max(bcd_template):.2f}]")
        print(f"  Template std: {np.std(bcd_template):.2f}")
    except Exception as e:
        print(f"  ❌ Template generation failed: {e}")
        return
    print()
    
    # Step 4: Try single 15-second window correlation
    print("Step 4: Sliding window correlation (15s windows)")
    window_seconds = 15
    step_seconds = 1
    window_samples = window_seconds * archive.sample_rate
    step_samples = step_seconds * archive.sample_rate
    
    total_samples = len(bcd_signal)
    num_windows = (total_samples - window_samples) // step_samples + 1
    print(f"  Window size: {window_samples} samples ({window_seconds}s)")
    print(f"  Step size: {step_samples} samples ({step_seconds}s)")
    print(f"  Total windows: {num_windows}")
    print()
    
    # Test first window in detail
    print("Testing first window (0-15s):")
    start_sample = 0
    end_sample = window_samples
    
    signal_window = bcd_signal[start_sample:end_sample]
    template_window = bcd_template[start_sample:end_sample]
    
    print(f"  Signal window shape: {signal_window.shape}")
    print(f"  Template window shape: {template_window.shape}")
    
    # Cross-correlate (mode='full' to get all lags)
    correlation = scipy_signal.correlate(signal_window, template_window, mode='full')
    correlation = np.abs(correlation)
    
    print(f"  Correlation shape: {correlation.shape}")
    print(f"  Correlation range: [{np.min(correlation):.2f}, {np.max(correlation):.2f}]")
    print(f"  Correlation mean: {np.mean(correlation):.2f}")
    print(f"  Correlation std: {np.std(correlation):.2f}")
    print()
    
    # Find peaks
    min_peak_distance = int(0.005 * archive.sample_rate)  # 5ms
    max_peak_distance = int(0.030 * archive.sample_rate)  # 30ms
    
    mean_corr = np.mean(correlation)
    std_corr = np.std(correlation)
    threshold = mean_corr + 2 * std_corr
    
    print(f"Peak detection:")
    print(f"  Threshold: {threshold:.2f} (mean + 2σ)")
    print(f"  Min peak distance: {min_peak_distance} samples (5ms)")
    print(f"  Max peak distance: {max_peak_distance} samples (30ms)")
    
    peaks, properties = scipy_signal.find_peaks(
        correlation, 
        height=threshold,
        distance=min_peak_distance,
        prominence=std_corr * 0.5
    )
    
    print(f"  Peaks found: {len(peaks)}")
    
    if len(peaks) > 0:
        print(f"  Peak indices: {peaks[:5]}")
        print(f"  Peak heights: {properties['peak_heights'][:5]}")
        
        if len(peaks) >= 2:
            # Find two strongest peaks
            peak_heights = properties['peak_heights']
            sorted_indices = np.argsort(peak_heights)[-2:]
            sorted_indices = np.sort(sorted_indices)
            
            peak1_idx = sorted_indices[0]
            peak2_idx = sorted_indices[1]
            
            peak1_amp = float(peak_heights[peak1_idx])
            peak2_amp = float(peak_heights[peak2_idx])
            peak1_time = peaks[peak1_idx] / archive.sample_rate
            peak2_time = peaks[peak2_idx] / archive.sample_rate
            
            delay_ms = (peak2_time - peak1_time) * 1000
            
            print()
            print(f"Two strongest peaks:")
            print(f"  Peak 1: amplitude={peak1_amp:.2f}, time={peak1_time:.6f}s")
            print(f"  Peak 2: amplitude={peak2_amp:.2f}, time={peak2_time:.6f}s")
            print(f"  Differential delay: {delay_ms:.2f} ms")
            
            if 5 <= delay_ms <= 30:
                print(f"  ✅ Delay in valid range!")
            else:
                print(f"  ❌ Delay OUTSIDE valid range (5-30ms)")
        else:
            print(f"  ❌ Need at least 2 peaks, only found {len(peaks)}")
    else:
        print(f"  ❌ No peaks found above threshold")
        print(f"     Try lowering threshold or checking signal quality")

if __name__ == '__main__':
    main()
