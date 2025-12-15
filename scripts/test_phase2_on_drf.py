#!/usr/bin/env python3
"""
Test Phase 2 Temporal Engine on live Digital RF data.

Reads raw 20 kHz IQ data from Phase 1 Digital RF archive and processes
through the Phase 2 Temporal Analysis Engine.
"""

import sys
import time
import numpy as np
import h5py
from pathlib import Path
from datetime import datetime, timezone
from glob import glob

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from grape_recorder.grape.phase2_temporal_engine import Phase2TemporalEngine, create_phase2_engine


def read_drf_direct(channel_dir: Path, sample_rate: int = 20000) -> tuple:
    """
    Read IQ data directly from DRF H5 files (bypasses digital_rf library).
    
    This is more robust when files are actively being written.
    
    Args:
        channel_dir: Channel directory (e.g., '.../WWV_10_MHz')
        sample_rate: Expected sample rate
        
    Returns:
        (iq_samples, system_time, rtp_timestamp) or (None, None, None)
    """
    # Find date directories
    date_dirs = sorted(channel_dir.glob('20*'))
    if not date_dirs:
        print(f"No date directories in {channel_dir}")
        return None, None, None
    
    # Use latest date directory
    date_dir = date_dirs[-1]
    
    # Find subdirectories with data
    subdirs = sorted(date_dir.glob('2025-*'))
    if not subdirs:
        print(f"No data subdirectories in {date_dir}")
        return None, None, None
    
    # Use latest subdir
    data_subdir = subdirs[-1]
    
    # Find H5 files
    h5_files = sorted(data_subdir.glob('*.h5'))
    if not h5_files:
        print(f"No H5 files in {data_subdir}")
        return None, None, None
    
    print(f"Found {len(h5_files)} H5 files in {data_subdir.name}")
    
    # Read from the latest complete file (not the one being written)
    # Try files from newest to oldest until we find one that works
    all_samples = []
    start_time = None
    
    for h5_file in h5_files:
        try:
            with h5py.File(h5_file, 'r', swmr=True) as f:
                if 'rf_data' not in f:
                    continue
                
                rf_data = f['rf_data'][:]
                
                # Extract timestamp from filename (e.g., tmp.rf@1764802800.000.h5)
                name = h5_file.name
                if '@' in name:
                    ts_str = name.split('@')[1].split('.h5')[0]
                    file_time = float(ts_str)
                    if start_time is None or file_time < start_time:
                        start_time = file_time
                
                all_samples.append(rf_data)
                print(f"  Read {len(rf_data)} samples from {h5_file.name}")
                
        except Exception as e:
            print(f"  Skipping {h5_file.name}: {e}")
            continue
    
    if not all_samples:
        print("No readable data found")
        return None, None, None
    
    # Concatenate all samples and squeeze to 1D
    iq_samples = np.concatenate(all_samples).squeeze().astype(np.complex64)
    print(f"  Data shape after squeeze: {iq_samples.shape}")
    
    # Use current time if we couldn't extract from filename
    if start_time is None:
        start_time = time.time() - len(iq_samples) / sample_rate
    
    # For RTP timestamp, use sample index (would be actual RTP in real use)
    rtp_timestamp = int(start_time * sample_rate)
    
    print(f"Total: {len(iq_samples)} samples ({len(iq_samples)/sample_rate:.1f}s)")
    print(f"  Amplitude: min={np.min(np.abs(iq_samples)):.4f}, "
          f"max={np.max(np.abs(iq_samples)):.4f}, "
          f"mean={np.mean(np.abs(iq_samples)):.4f}")
    
    return iq_samples, start_time, rtp_timestamp


def test_phase2_engine():
    """Test Phase 2 engine on real DRF data."""
    
    # Configuration
    drf_base = Path('/tmp/timestd-test/phase1_raw/raw_archive/raw_archive')
    output_dir = Path('/tmp/timestd-test/phase2_test')
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Test channels
    test_channels = [
        ('WWV_10_MHz', 10e6),
        ('WWV_15_MHz', 15e6),
        ('WWV_5_MHz', 5e6),
    ]
    
    receiver_grid = 'EM38ww'  # From config
    sample_rate = 20000
    
    print("=" * 70)
    print("Phase 2 Temporal Engine Test on Live Digital RF Data")
    print("=" * 70)
    print(f"DRF Base: {drf_base}")
    print(f"Output: {output_dir}")
    print(f"Receiver: {receiver_grid}")
    print()
    
    # Get current minute boundary (round down to minute)
    now = time.time()
    # Use the PREVIOUS minute to ensure we have complete data
    minute_boundary = ((int(now) // 60) - 1) * 60
    
    print(f"Current time: {datetime.now(tz=timezone.utc)}")
    print()
    
    for channel_name, frequency_hz in test_channels:
        print("-" * 70)
        print(f"Channel: {channel_name} ({frequency_hz/1e6:.1f} MHz)")
        print("-" * 70)
        
        # Read DRF data directly from H5 files
        channel_dir = drf_base / channel_name
        iq_samples, system_time, rtp_timestamp = read_drf_direct(
            channel_dir, sample_rate
        )
        
        if iq_samples is None:
            print(f"  ⚠️ No data available for {channel_name}")
            continue
        
        if len(iq_samples) < sample_rate * 30:  # Need at least 30 seconds
            print(f"  ⚠️ Insufficient data: {len(iq_samples)} samples ({len(iq_samples)/sample_rate:.1f}s)")
            continue
        
        # Create Phase 2 engine
        try:
            engine = Phase2TemporalEngine(
                raw_archive_dir=drf_base,
                output_dir=output_dir / channel_name,
                channel_name=channel_name,
                frequency_hz=frequency_hz,
                receiver_grid=receiver_grid,
                sample_rate=sample_rate
            )
            
            # Process the minute
            print(f"  Processing {len(iq_samples)} samples...")
            result = engine.process_minute(
                iq_samples=iq_samples,
                system_time=system_time,
                rtp_timestamp=rtp_timestamp
            )
            
            if result:
                print(f"  ✅ Phase 2 Result:")
                print(f"     D_clock: {result.d_clock_ms:+.2f} ms")
                print(f"     Quality: {result.quality_grade}")
                print(f"     Station: {result.solution.station}")
                print(f"     Mode: {result.solution.propagation_mode}")
                print(f"     Confidence: {result.solution.confidence:.2f}")
                print(f"     Uncertainty: {result.solution.uncertainty_ms:.2f} ms")
                
                # Time snap details
                ts = result.time_snap
                print(f"     Time Snap:")
                print(f"       Anchor: {ts.anchor_station} (confidence={ts.anchor_confidence:.2f})")
                print(f"       Timing error: {ts.timing_error_ms:+.2f} ms")
                print(f"       WWV detected: {ts.wwv_detected}, WWVH detected: {ts.wwvh_detected}")
                
                # Channel characterization
                ch = result.channel
                if ch.bcd_differential_delay_ms is not None:
                    print(f"     BCD delay: {ch.bcd_differential_delay_ms:.2f} ms")
                if ch.doppler_wwv_hz is not None:
                    print(f"     Doppler WWV: {ch.doppler_wwv_hz:+.4f} Hz")
                if ch.ground_truth_station:
                    print(f"     Ground truth: {ch.ground_truth_station} ({ch.ground_truth_source})")
            else:
                print(f"  ❌ No result from Phase 2 engine")
                
        except Exception as e:
            print(f"  ❌ Error: {e}")
            import traceback
            traceback.print_exc()
        
        print()
    
    print("=" * 70)
    print("Test complete")
    print("=" * 70)


if __name__ == '__main__':
    test_phase2_engine()
