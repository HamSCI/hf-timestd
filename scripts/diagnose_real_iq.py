#!/usr/bin/env python3
"""
Diagnose real IQ signal structure at tick positions.
Reads a production raw buffer and examines what the IQ correlator sees.
"""

import sys
import json
import zstandard
import numpy as np
sys.path.insert(0, '/home/mjh/git/hf-timestd/src')

from scipy.signal import correlate
from scipy.signal.windows import tukey
from pathlib import Path


def load_raw_buffer(bin_path, json_path):
    """Load a raw IQ buffer from production."""
    with open(json_path) as f:
        metadata = json.load(f)
    
    with open(bin_path, 'rb') as f:
        compressed = f.read()
    
    dctx = zstandard.ZstdDecompressor()
    raw = dctx.decompress(compressed)
    iq = np.frombuffer(raw, dtype=np.complex64).copy()
    
    return iq, metadata


def analyze_tick_correlation(iq, sample_rate=20000, freq_hz=1000.0, 
                              tick_duration_ms=5.0, station_name="WWV"):
    """Analyze per-tick IQ correlation on real data."""
    tick_samples = int(tick_duration_ms * sample_rate / 1000.0)
    search_range_ms = 100.0
    search_samples = int(search_range_ms * sample_rate / 1000.0)
    
    # Build IQ template (complex exponential)
    t = np.arange(tick_samples) / sample_rate
    window = tukey(tick_samples, alpha=0.1)
    template_iq = np.exp(1j * 2 * np.pi * freq_hz * t) * window
    template_iq /= np.sqrt(np.sum(np.abs(template_iq)**2))
    
    # Build sin/cos templates for AM approach comparison
    template_sin = np.sin(2 * np.pi * freq_hz * t) * window
    template_cos = np.cos(2 * np.pi * freq_hz * t) * window
    energy = np.sqrt(np.sum(template_sin**2))
    template_sin /= energy
    template_cos /= energy
    
    # Also build a REAL sinusoid template (for AM signals, the tick
    # appears as a real modulation on the carrier)
    template_real = np.sin(2 * np.pi * freq_hz * t) * window
    template_real /= np.sqrt(np.sum(template_real**2))
    
    print(f"\n{'='*70}")
    print(f"REAL IQ ANALYSIS: {station_name} ({freq_hz} Hz, {tick_duration_ms}ms ticks)")
    print(f"{'='*70}")
    print(f"IQ buffer: {len(iq)} samples ({len(iq)/sample_rate:.1f}s)")
    print(f"Template: {tick_samples} samples ({tick_duration_ms}ms)")
    print(f"Search: ±{search_range_ms}ms ({search_samples} samples)")
    
    # Examine IQ at a few tick positions
    print(f"\n--- IQ Signal Structure at Tick Positions ---")
    for sec in [1, 5, 10, 15, 20]:
        tick_start = sec * sample_rate
        tick_end = tick_start + tick_samples
        if tick_end > len(iq):
            continue
        
        iq_tick = iq[tick_start:tick_end]
        mag = np.abs(iq_tick)
        real_part = np.real(iq_tick)
        imag_part = np.imag(iq_tick)
        
        # Check for tone at freq_hz in the IQ
        fft = np.fft.fft(iq_tick)
        freqs = np.fft.fftfreq(len(iq_tick), 1/sample_rate)
        
        # Find power at tone frequency
        tone_idx = np.argmin(np.abs(freqs - freq_hz))
        neg_tone_idx = np.argmin(np.abs(freqs + freq_hz))
        dc_idx = 0
        
        tone_power = np.abs(fft[tone_idx])**2
        neg_tone_power = np.abs(fft[neg_tone_idx])**2
        dc_power = np.abs(fft[dc_idx])**2
        total_power = np.sum(np.abs(fft)**2)
        
        print(f"  sec {sec:2d}: |IQ|={np.mean(mag):.4f}, "
              f"DC={dc_power/total_power*100:.1f}%, "
              f"+{freq_hz:.0f}Hz={tone_power/total_power*100:.1f}%, "
              f"-{freq_hz:.0f}Hz={neg_tone_power/total_power*100:.1f}%")
    
    # Also check a noise-only region (second 29 is silent for WWV)
    noise_start = 29 * sample_rate
    noise_end = noise_start + tick_samples
    if noise_end <= len(iq):
        iq_noise = iq[noise_start:noise_end]
        fft_noise = np.fft.fft(iq_noise)
        freqs_n = np.fft.fftfreq(len(iq_noise), 1/sample_rate)
        tone_idx_n = np.argmin(np.abs(freqs_n - freq_hz))
        tone_power_n = np.abs(fft_noise[tone_idx_n])**2
        total_power_n = np.sum(np.abs(fft_noise)**2)
        print(f"  sec 29 (SILENT): +{freq_hz:.0f}Hz={tone_power_n/total_power_n*100:.1f}%")
    
    # Per-tick correlation comparison: IQ vs AM vs AM+BP
    print(f"\n--- Per-Tick Correlation Results ---")
    
    offsets_iq = []
    offsets_am = []
    snrs_iq = []
    snrs_am = []
    
    from scipy.signal import butter, sosfiltfilt
    sos = butter(4, [freq_hz - 100, freq_hz + 100], btype='band', 
                 fs=sample_rate, output='sos')
    
    for sec in range(1, 58):
        if sec in {0, 29, 59}:
            continue
        
        slice_start = sec * sample_rate - search_samples
        slice_end = sec * sample_rate + tick_samples + search_samples
        if slice_start < 0 or slice_end > len(iq):
            continue
        
        iq_slice = iq[slice_start:slice_end]
        
        # IQ-domain correlation
        corr_iq = correlate(iq_slice, template_iq, mode='valid')
        env_iq = np.abs(corr_iq)
        if len(env_iq) == 0:
            continue
        peak_iq = int(np.argmax(env_iq))
        peak_val_iq = env_iq[peak_iq]
        
        # SNR for IQ
        noise_region = np.concatenate([env_iq[:max(0,peak_iq-tick_samples)], 
                                        env_iq[min(len(env_iq),peak_iq+tick_samples):]])
        if len(noise_region) > 10:
            noise_std = np.std(noise_region)
            snr_iq = 20*np.log10(peak_val_iq/noise_std) if noise_std > 0 else 40.0
        else:
            snr_iq = 0.0
        
        offset_iq = (peak_iq - search_samples) / sample_rate * 1000.0
        offsets_iq.append(offset_iq)
        snrs_iq.append(snr_iq)
        
        # AM-domain correlation (with bandpass)
        audio = np.abs(iq_slice) - np.mean(np.abs(iq_slice))
        audio_bp = sosfiltfilt(sos, audio)
        corr_s = correlate(audio_bp, template_sin, mode='valid')
        corr_c = correlate(audio_bp, template_cos, mode='valid')
        env_am = np.sqrt(corr_s**2 + corr_c**2)
        peak_am = int(np.argmax(env_am))
        peak_val_am = env_am[peak_am]
        
        noise_region_am = np.concatenate([env_am[:max(0,peak_am-tick_samples)],
                                           env_am[min(len(env_am),peak_am+tick_samples):]])
        if len(noise_region_am) > 10:
            noise_std_am = np.std(noise_region_am)
            snr_am = 20*np.log10(peak_val_am/noise_std_am) if noise_std_am > 0 else 40.0
        else:
            snr_am = 0.0
        
        offset_am = (peak_am - search_samples) / sample_rate * 1000.0
        offsets_am.append(offset_am)
        snrs_am.append(snr_am)
    
    offsets_iq = np.array(offsets_iq)
    offsets_am = np.array(offsets_am)
    snrs_iq = np.array(snrs_iq)
    snrs_am = np.array(snrs_am)
    
    print(f"\n  IQ-domain:  mean={np.mean(offsets_iq):+.3f}ms, std={np.std(offsets_iq):.3f}ms, "
          f"median={np.median(offsets_iq):+.3f}ms, SNR={np.mean(snrs_iq):.1f}dB")
    print(f"  AM+BP:      mean={np.mean(offsets_am):+.3f}ms, std={np.std(offsets_am):.3f}ms, "
          f"median={np.median(offsets_am):+.3f}ms, SNR={np.mean(snrs_am):.1f}dB")
    
    # Show per-tick offsets for first 20 ticks
    print(f"\n  Per-tick offsets (first 20):")
    print(f"  {'sec':>4s}  {'IQ offset':>10s}  {'AM offset':>10s}  {'IQ SNR':>8s}  {'AM SNR':>8s}")
    secs = [s for s in range(1, 58) if s not in {0, 29, 59}]
    for i in range(min(20, len(offsets_iq))):
        print(f"  {secs[i]:4d}  {offsets_iq[i]:+10.3f}ms  {offsets_am[i]:+10.3f}ms  "
              f"{snrs_iq[i]:8.1f}dB  {snrs_am[i]:8.1f}dB")
    
    # Examine the IQ correlation envelope for one tick in detail
    print(f"\n--- Detailed IQ Correlation Envelope (sec 5) ---")
    sec = 5
    slice_start = sec * sample_rate - search_samples
    slice_end = sec * sample_rate + tick_samples + search_samples
    iq_slice = iq[slice_start:slice_end]
    
    corr = correlate(iq_slice, template_iq, mode='valid')
    env = np.abs(corr)
    peak = int(np.argmax(env))
    peak_val = env[peak]
    
    from scipy.signal import find_peaks
    peaks, _ = find_peaks(env, height=peak_val * 0.5)
    print(f"  Peaks >50% of max ({len(peaks)} total):")
    for p in peaks[:15]:
        p_ms = (p - search_samples) / sample_rate * 1000.0
        print(f"    offset={p_ms:+.3f}ms, height={env[p]:.6f} ({env[p]/peak_val*100:.1f}%)")
    
    # Check: is the IQ signal AM (both sidebands) or SSB (one sideband)?
    print(f"\n--- Signal Type Analysis (1-second FFT at sec 5) ---")
    iq_1sec = iq[5*sample_rate:6*sample_rate]
    fft_1sec = np.fft.fft(iq_1sec)
    freqs_1sec = np.fft.fftfreq(len(iq_1sec), 1/sample_rate)
    
    # Power at key frequencies
    for f_check in [0, 500, 1000, 1200, -1000, -1200, -500]:
        idx = np.argmin(np.abs(freqs_1sec - f_check))
        power = np.abs(fft_1sec[idx])**2
        total = np.sum(np.abs(fft_1sec)**2)
        print(f"  {f_check:+6.0f} Hz: power={power/total*100:.2f}%")


def main():
    # Find the most recent raw buffer
    base = Path("/dev/shm/timestd/raw_buffer/SHARED_10000")
    dates = sorted(base.iterdir(), reverse=True)
    
    for date_dir in dates:
        files = sorted(date_dir.glob("*.bin.zst"), reverse=True)
        if files:
            bin_path = files[0]
            json_path = bin_path.with_suffix('').with_suffix('.json')
            if json_path.exists():
                print(f"Loading: {bin_path}")
                iq, metadata = load_raw_buffer(str(bin_path), str(json_path))
                print(f"Metadata: system_time={metadata.get('start_system_time', 'N/A')}")
                
                # Analyze WWV (1000 Hz, 5ms)
                analyze_tick_correlation(iq, freq_hz=1000.0, tick_duration_ms=5.0, 
                                        station_name="WWV")
                
                # Analyze WWVH (1200 Hz, 5ms)
                analyze_tick_correlation(iq, freq_hz=1200.0, tick_duration_ms=5.0,
                                        station_name="WWVH")
                return
    
    print("No raw buffer files found!")


if __name__ == '__main__':
    main()
