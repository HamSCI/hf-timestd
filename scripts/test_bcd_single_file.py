#!/usr/bin/env python3
"""
Test BCD discrimination on a single NPZ file to debug why it's failing
"""
import sys
import numpy as np
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from grape_recorder.grape.analytics_service import NPZArchive
from grape_recorder.grape.wwvh_discrimination import WWVHDiscriminator

def main():
    # Find a recent NPZ file
    archive_dir = Path('/tmp/timestd-test/archives/WWV_10_MHz')
    
    if not archive_dir.exists():
        print(f"Archive directory not found: {archive_dir}")
        return
    
    npz_files = sorted(archive_dir.glob('20251119T1[45]*.npz'))
    
    if not npz_files:
        print("No NPZ files found for 20251119")
        return
    
    # Use the first file with full data
    npz_file = npz_files[0]
    print(f"Testing with: {npz_file.name}")
    print()
    
    # Load archive
    archive = NPZArchive.load(npz_file)
    print(f"Sample rate: {archive.sample_rate} Hz")
    print(f"IQ samples: {len(archive.iq_samples)}")
    print(f"Timestamp: {archive.unix_timestamp}")
    print()
    
    # Create discriminator
    discriminator = WWVHDiscriminator("WWV 10 MHz")
    
    # Test BCD discrimination directly
    print("=" * 70)
    print("TESTING BCD DISCRIMINATION")
    print("=" * 70)
    
    # Enable debug logging
    import logging
    logging.basicConfig(level=logging.DEBUG, format='%(levelname)s: %(message)s')
    
    try:
        result = discriminator.detect_bcd_discrimination(
            iq_samples=archive.iq_samples,
            sample_rate=archive.sample_rate,
            minute_timestamp=archive.unix_timestamp
        )
        
        wwv_amp, wwvh_amp, delay, quality, windows = result
        
        print(f"Result: {result}")
        print()
        
        if wwv_amp is None:
            print("❌ BCD discrimination returned None")
            print()
            print("Possible causes:")
            print("1. Template generation failed")
            print("2. No valid correlation peaks found")
            print("3. Peaks outside 5-30ms delay range")
            print("4. Signal too weak")
        else:
            print(f"✅ BCD discrimination successful!")
            print(f"   WWV amplitude: {wwv_amp:.1f}")
            print(f"   WWVH amplitude: {wwvh_amp:.1f}")
            print(f"   Differential delay: {delay:.2f} ms")
            print(f"   Correlation quality: {quality:.1f}")
            print(f"   Windows found: {len(windows) if windows else 0}")
            
            if windows and len(windows) > 0:
                print()
                print("First 3 windows:")
                for i, w in enumerate(windows[:3]):
                    print(f"   Window {i+1}: start={w['window_start_sec']:.1f}s, "
                          f"WWV={w['wwv_amplitude']:.1f}, WWVH={w['wwvh_amplitude']:.1f}, "
                          f"delay={w['differential_delay_ms']:.2f}ms, quality={w['correlation_quality']:.1f}")
    
    except Exception as e:
        print(f"❌ Exception during BCD discrimination:")
        print(f"   {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()

if __name__ == '__main__':
    main()
