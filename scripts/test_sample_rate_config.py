#!/usr/bin/env python3
"""
Test script to verify config-driven sample rate architecture.

Tests:
1. GrapeConfig computed properties (samples_per_packet, max_gap_samples)
2. SessionConfig auto-calculation
3. Decimation for supported rates
4. Config file parsing
"""

import sys
import os
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

import numpy as np
import toml

def test_grape_config():
    """Test GrapeConfig computed properties"""
    print("\n=== Testing GrapeConfig ===")
    
    from grape_recorder.grape.grape_recorder import GrapeConfig
    
    # Test 20 kHz with 20ms blocktime
    config_20k = GrapeConfig(
        ssrc=10000000,
        frequency_hz=10e6,
        sample_rate=24000,
        description="Test 20 kHz",
        output_dir=Path("/tmp"),
        station_config={},
        blocktime_ms=20.0,
        max_gap_seconds=60.0
    )
    
    assert config_20k.samples_per_packet == 400, f"Expected 400, got {config_20k.samples_per_packet}"
    assert config_24k.max_gap_samples == 1_440_000, f"Expected 1440000, got {config_24k.max_gap_samples}"
    print(f"✅ 20 kHz @ 20ms: samples_per_packet={config_20k.samples_per_packet}, max_gap_samples={config_20k.max_gap_samples}")
    
    # Test 16 kHz with 20ms blocktime
    config_16k = GrapeConfig(
        ssrc=10000000,
        frequency_hz=10e6,
        sample_rate=16000,
        description="Test 16 kHz",
        output_dir=Path("/tmp"),
        station_config={},
        blocktime_ms=20.0,
        max_gap_seconds=60.0
    )
    
    assert config_16k.samples_per_packet == 320, f"Expected 320, got {config_16k.samples_per_packet}"
    assert config_16k.max_gap_samples == 960_000, f"Expected 960000, got {config_16k.max_gap_samples}"
    print(f"✅ 16 kHz @ 20ms: samples_per_packet={config_16k.samples_per_packet}, max_gap_samples={config_16k.max_gap_samples}")
    
    # Test 24 kHz (hypothetical) with 20ms blocktime
    config_24k = GrapeConfig(
        ssrc=10000000,
        frequency_hz=10e6,
        sample_rate=24000,
        description="Test 24 kHz",
        output_dir=Path("/tmp"),
        station_config={},
        blocktime_ms=20.0,
        max_gap_seconds=60.0
    )
    
    assert config_24k.samples_per_packet == 480, f"Expected 480, got {config_24k.samples_per_packet}"
    assert config_24k.max_gap_samples == 1_440_000, f"Expected 1440000, got {config_24k.max_gap_samples}"
    print(f"✅ 24 kHz @ 20ms: samples_per_packet={config_24k.samples_per_packet}, max_gap_samples={config_24k.max_gap_samples}")
    
    print("✅ All GrapeConfig tests passed!")


def test_session_config():
    """Test SessionConfig auto-calculation"""
    print("\n=== Testing SessionConfig ===")
    
    from grape_recorder.core.recording_session import SessionConfig
    
    # Test with explicit values
    config_explicit = SessionConfig(
        ssrc=10000000,
        sample_rate=24000,
        samples_per_packet=400,
        max_gap_samples=1_200_000
    )
    assert config_explicit.samples_per_packet == 400
    assert config_explicit.max_gap_samples == 1_200_000
    print(f"✅ Explicit values preserved: samples_per_packet={config_explicit.samples_per_packet}")
    
    # Test with auto-calculation (no explicit values)
    config_auto = SessionConfig(
        ssrc=10000000,
        sample_rate=24000,
        blocktime_ms=20.0,
        max_gap_seconds=60.0
    )
    assert config_auto.samples_per_packet == 400, f"Expected 400, got {config_auto.samples_per_packet}"
    assert config_auto.max_gap_samples == 1_440_000, f"Expected 1440000, got {config_auto.max_gap_samples}"
    print(f"✅ Auto-calculated: samples_per_packet={config_auto.samples_per_packet}, max_gap_samples={config_auto.max_gap_samples}")
    
    # Test 16 kHz auto-calculation
    config_16k = SessionConfig(
        ssrc=10000000,
        sample_rate=16000,
        blocktime_ms=20.0,
        max_gap_seconds=60.0
    )
    assert config_16k.samples_per_packet == 320
    assert config_16k.max_gap_samples == 960_000
    print(f"✅ 16 kHz auto-calculated: samples_per_packet={config_16k.samples_per_packet}")
    
    print("✅ All SessionConfig tests passed!")


def test_decimation():
    """Test decimation for supported rates"""
    print("\n=== Testing Decimation ===")
    
    from grape_recorder.grape.decimation import (
        decimate_for_upload,
        get_supported_rates,
        is_rate_supported,
        SUPPORTED_INPUT_RATES
    )
    
    # Check supported rates
    print(f"Supported rates: {list(SUPPORTED_INPUT_RATES.keys())}")
    
    assert is_rate_supported(24000), "24 kHz should be supported"
    assert is_rate_supported(20000), "20 kHz should still be supported (legacy)"
    assert is_rate_supported(16000), "16 kHz should be supported"
    assert not is_rate_supported(24000), "24 kHz should not be supported (yet)"
    print("✅ Rate support checks passed")
    
    # Test 20 kHz decimation
    print("\nTesting 20 kHz decimation...")
    samples_20k = np.random.randn(1_200_000) + 1j * np.random.randn(1_200_000)  # 60 seconds
    result_24k = decimate_for_upload(samples_24k.astype(np.complex64), input_rate=24000, output_rate=10)
    assert result_20k is not None, "Decimation failed"
    assert len(result_20k) == 600, f"Expected 600 samples, got {len(result_20k)}"
    print(f"✅ 20 kHz: {len(samples_20k)} samples → {len(result_20k)} samples (factor 2000)")
    
    # Test 16 kHz decimation
    print("\nTesting 16 kHz decimation...")
    samples_16k = np.random.randn(960_000) + 1j * np.random.randn(960_000)  # 60 seconds
    result_16k = decimate_for_upload(samples_16k.astype(np.complex64), input_rate=16000, output_rate=10)
    assert result_16k is not None, "Decimation failed"
    assert len(result_16k) == 600, f"Expected 600 samples, got {len(result_16k)}"
    print(f"✅ 16 kHz: {len(samples_16k)} samples → {len(result_16k)} samples (factor 1600)")
    
    # Test unsupported rate error
    print("\nTesting unsupported rate error...")
    try:
        decimate_for_upload(samples_20k.astype(np.complex64), input_rate=24000, output_rate=10)
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "Unsupported input rate" in str(e)
        assert "SUPPORTED_INPUT_RATES" in str(e)
        print(f"✅ Proper error for unsupported rate: {str(e)[:80]}...")
    
    print("✅ All decimation tests passed!")


def test_config_file():
    """Test config file parsing"""
    print("\n=== Testing Config File ===")
    
    config_path = Path(__file__).parent.parent / 'config' / 'timestd-config.toml'
    
    if not config_path.exists():
        print(f"⚠️  Config file not found: {config_path}")
        return
    
    with open(config_path, 'r') as f:
        config = toml.load(f)
    
    recorder = config.get('recorder', {})
    blocktime_ms = recorder.get('blocktime_ms', 20.0)
    max_gap_seconds = recorder.get('max_gap_seconds', 60.0)
    
    print(f"Config file: {config_path}")
    print(f"  blocktime_ms: {blocktime_ms}")
    print(f"  max_gap_seconds: {max_gap_seconds}")
    
    channels = recorder.get('channels', [])
    print(f"  Channels: {len(channels)}")
    
    for ch in channels[:3]:  # Show first 3
        sample_rate = ch.get('sample_rate', 'NOT SET')
        desc = ch.get('description', 'unknown')
        expected_spp = int(sample_rate * blocktime_ms / 1000) if isinstance(sample_rate, int) else 'N/A'
        print(f"    - {desc}: {sample_rate} Hz → {expected_spp} samples/packet")
    
    if len(channels) > 3:
        print(f"    ... and {len(channels) - 3} more channels")
    
    print("✅ Config file parsed successfully!")


def test_packet_resequencer():
    """Test PacketResequencer with configurable max_gap_samples"""
    print("\n=== Testing PacketResequencer ===")
    
    from grape_recorder.core.packet_resequencer import PacketResequencer
    
    # Test with default
    reseq_default = PacketResequencer(samples_per_packet=400)
    assert reseq_default.max_gap_samples == 1_440_000, f"Expected default 1440000, got {reseq_default.max_gap_samples}"
    print(f"✅ Default max_gap_samples: {reseq_default.max_gap_samples}")
    
    # Test with explicit value
    reseq_custom = PacketResequencer(samples_per_packet=320, max_gap_samples=960_000)
    assert reseq_custom.max_gap_samples == 960_000
    assert reseq_custom.samples_per_packet == 320
    print(f"✅ Custom 16 kHz: samples_per_packet={reseq_custom.samples_per_packet}, max_gap_samples={reseq_custom.max_gap_samples}")
    
    # Test with 24 kHz hypothetical
    reseq_24k = PacketResequencer(samples_per_packet=480, max_gap_samples=1_440_000)
    assert reseq_24k.max_gap_samples == 1_440_000
    print(f"✅ Hypothetical 24 kHz: samples_per_packet={reseq_24k.samples_per_packet}, max_gap_samples={reseq_24k.max_gap_samples}")
    
    print("✅ All PacketResequencer tests passed!")


def main():
    print("=" * 60)
    print("Sample Rate Configuration Test Suite")
    print("=" * 60)
    
    try:
        test_grape_config()
        test_session_config()
        test_packet_resequencer()
        test_decimation()
        test_config_file()
        
        print("\n" + "=" * 60)
        print("🎉 ALL TESTS PASSED!")
        print("=" * 60)
        print("\nThe config-driven sample rate architecture is working correctly.")
        print("To change sample rate:")
        print("  1. Edit sample_rate in timestd-config.toml")
        print("  2. If new rate, add to SUPPORTED_INPUT_RATES in decimation.py")
        print("  3. Configure radiod for matching rate")
        
    except Exception as e:
        print(f"\n❌ TEST FAILED: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()
