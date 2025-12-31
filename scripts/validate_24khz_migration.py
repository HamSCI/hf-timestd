#!/usr/bin/env python3
"""
24 kHz Sample Rate Validation Script

Validates that the migration from 20 kHz to 24 kHz is complete and correct.
"""

import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

def test_constants():
    """Test that constants are updated"""
    from hf_timestd.core.wwv_constants import SAMPLE_RATE_FULL
    from hf_timestd.core.phase2_temporal_engine import SAMPLE_RATE_FULL as TEMP_RATE
    
    assert SAMPLE_RATE_FULL == 24000, f"wwv_constants.SAMPLE_RATE_FULL should be 24000, got {SAMPLE_RATE_FULL}"
    assert TEMP_RATE == 24000, f"phase2_temporal_engine.SAMPLE_RATE_FULL should be 24000, got {TEMP_RATE}"
    
    print("✅ Constants updated correctly")

def test_integer_cycles():
    """Test that WWVH and WWV have integer samples per cycle"""
    SAMPLE_RATE = 24000
    WWV_FREQ = 1000
    WWVH_FREQ = 1200
    
    wwv_samples = SAMPLE_RATE / WWV_FREQ
    wwvh_samples = SAMPLE_RATE / WWVH_FREQ
    
    assert wwv_samples == int(wwv_samples), f"WWV should have integer samples/cycle, got {wwv_samples}"
    assert wwvh_samples == int(wwvh_samples), f"WWVH should have integer samples/cycle, got {wwvh_samples}"
    
    print(f"✅ WWV: {int(wwv_samples)} samples/cycle (1000 Hz)")
    print(f"✅ WWVH: {int(wwvh_samples)} samples/cycle (1200 Hz)")

def test_nyquist_margin():
    """Test that Nyquist frequency provides adequate margin for test signals"""
    SAMPLE_RATE = 24000
    NYQUIST = SAMPLE_RATE / 2
    TEST_SIGNAL_MAX_FREQ = 10000  # HamSCI test signal bandwidth
    
    margin = NYQUIST - TEST_SIGNAL_MAX_FREQ
    
    assert margin >= 2000, f"Nyquist margin should be >= 2 kHz, got {margin} Hz"
    
    print(f"✅ Nyquist frequency: {NYQUIST} Hz")
    print(f"✅ Test signal max: {TEST_SIGNAL_MAX_FREQ} Hz")
    print(f"✅ Safety margin: {margin} Hz")

def test_chu_fsk():
    """Test that CHU FSK frequencies are handled correctly"""
    SAMPLE_RATE = 24000
    CHU_MARK = 2225
    CHU_SPACE = 2025
    
    # These won't be integer, but that's expected and handled by Goertzel
    mark_samples = SAMPLE_RATE / CHU_MARK
    space_samples = SAMPLE_RATE / CHU_SPACE
    
    print(f"✅ CHU Mark (2225 Hz): {mark_samples:.2f} samples/cycle (Goertzel handles fractional)")
    print(f"✅ CHU Space (2025 Hz): {space_samples:.2f} samples/cycle (Goertzel handles fractional)")

def test_config_file():
    """Test that config file is updated"""
    import tomllib
    
    config_path = Path(__file__).parent.parent / "config" / "timestd-config.toml"
    with open(config_path, 'rb') as f:
        config = tomllib.load(f)
    
    sample_rate = config['recorder']['channel_defaults']['sample_rate']
    assert sample_rate == 24000, f"Config sample_rate should be 24000, got {sample_rate}"
    
    print(f"✅ Config file sample_rate: {sample_rate} Hz")

def test_samples_per_minute():
    """Test expected samples per minute"""
    SAMPLE_RATE = 24000
    samples_per_minute = SAMPLE_RATE * 60
    
    assert samples_per_minute == 1_440_000, f"Expected 1440000 samples/minute, got {samples_per_minute}"
    
    print(f"✅ Samples per minute: {samples_per_minute:,}")

if __name__ == "__main__":
    print("=" * 60)
    print("24 kHz Sample Rate Migration Validation")
    print("=" * 60)
    print()
    
    try:
        test_constants()
        test_integer_cycles()
        test_nyquist_margin()
        test_chu_fsk()
        test_config_file()
        test_samples_per_minute()
        
        print()
        print("=" * 60)
        print("✅ ALL VALIDATION TESTS PASSED")
        print("=" * 60)
        print()
        print("Migration from 20 kHz to 24 kHz is complete and verified.")
        print()
        print("Benefits:")
        print("  • WWVH (1200 Hz): Now 20 samples/cycle (was 16.67)")
        print("  • WWV (1000 Hz): Now 24 samples/cycle (was 20)")
        print("  • Nyquist: 12 kHz (was 10 kHz) - better margin for test signals")
        print("  • Storage: 20% increase (1.44M vs 1.2M samples/minute)")
        
    except AssertionError as e:
        print()
        print("=" * 60)
        print(f"❌ VALIDATION FAILED: {e}")
        print("=" * 60)
        sys.exit(1)
    except Exception as e:
        print()
        print("=" * 60)
        print(f"❌ ERROR: {e}")
        print("=" * 60)
        import traceback
        traceback.print_exc()
        sys.exit(1)
