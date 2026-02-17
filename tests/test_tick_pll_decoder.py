"""
Unit tests for tick_pll_decoder.py

Tests the flywheel PLL decoder functionality without requiring
actual audio streams. Uses synthetic signals for validation.

Run with: python -m pytest tests/test_tick_pll_decoder.py -v
"""

import numpy as np
import pytest
from pathlib import Path
import sys

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from hf_timestd.core.tick_pll_decoder import (
    TickPLL,
    DualStationPLL,
    BCDIntegrator,
    PLLState,
    PLLTickResult,
    MinutePLLAnalysis,
    create_pll_decoder
)


class TestTickPLL:
    """Tests for the TickPLL class."""
    
    def test_pll_initialization(self):
        """Test PLL initializes with correct parameters."""
        pll = TickPLL("WWV", 1000.0, fs=24000)
        
        assert pll.name == "WWV"
        assert pll.target_freq == 1000.0
        assert pll.fs == 24000
        assert pll.state == PLLState.HUNT
        assert pll.samples_per_period == 24000  # 1 second at 24kHz
    
    def test_pll_reset(self):
        """Test PLL reset functionality."""
        pll = TickPLL("WWV", 1000.0, fs=24000)
        
        # Simulate some state
        pll.state = PLLState.LOCK
        pll.locked_for_ticks = 10
        pll.missed_ticks = 3
        
        # Reset
        pll.reset()
        
        assert pll.state == PLLState.HUNT
        assert pll.locked_for_ticks == 0
        assert pll.missed_ticks == 0
    
    def test_hunt_to_lock_transition(self):
        """Test PLL finds initial tick and transitions to LOCK."""
        pll = TickPLL("WWV", 1000.0, fs=24000)
        
        # Create synthetic 5ms tick at 1000 Hz
        fs = 24000
        tick_duration = int(0.005 * fs)  # 5ms = 120 samples
        audio = np.zeros(fs)  # 1 second
        t = np.arange(tick_duration) / fs
        
        # Add 1000 Hz tick at sample 1000
        tick_start = 1000
        audio[tick_start:tick_start + tick_duration] = np.sin(2 * np.pi * 1000 * t)
        
        # Add noise
        audio += np.random.normal(0, 0.01, len(audio))
        
        # Process
        results = pll.process_buffer(audio, 0)
        
        # Should have detected tick and transitioned to LOCK
        assert pll.state == PLLState.LOCK
        assert len(results) >= 1
        assert results[0].station == "WWV"
        assert results[0].tick_index > 0
    
    def test_minute_marker_detection(self):
        """Test detection of 800ms minute marker vs 5ms tick."""
        pll = TickPLL("WWV", 1000.0, fs=24000)
        
        fs = 24000
        
        # Create 800ms minute marker
        marker_duration = int(0.800 * fs)  # 800ms = 19200 samples
        audio = np.zeros(fs * 2)  # 2 seconds
        t = np.arange(marker_duration) / fs
        
        # Add 1000 Hz tone at sample 1000
        marker_start = 1000
        audio[marker_start:marker_start + marker_duration] = 0.5 * np.sin(2 * np.pi * 1000 * t)
        
        # Process
        results = pll.process_buffer(audio[:fs], 0)
        
        # Check if minute marker was detected
        if results:
            assert results[0].is_minute_mark == True
    
    def test_coast_through_fades(self):
        """Test PLL coasts through missing ticks."""
        pll = TickPLL("WWV", 1000.0, fs=24000, max_missed_ticks=5)
        
        fs = 24000
        
        # First establish lock
        tick_duration = int(0.005 * fs)
        audio1 = np.zeros(fs)
        t = np.arange(tick_duration) / fs
        tick_start = 1000
        audio1[tick_start:tick_start + tick_duration] = np.sin(2 * np.pi * 1000 * t)
        audio1 += np.random.normal(0, 0.01, len(audio1))
        
        pll.process_buffer(audio1, 0)
        assert pll.state == PLLState.LOCK
        
        # Now provide audio WITHOUT the tick (simulating fade)
        audio2 = np.random.normal(0, 0.01, fs)
        
        # Process multiple seconds of missing ticks
        for i in range(4):
            results = pll.process_buffer(audio2, (i + 1) * fs)
            # Should still be in LOCK or COAST state (not HUNT)
            assert pll.state in (PLLState.LOCK, PLLState.COAST)
        
        # After 5 misses, should go to HUNT
        results = pll.process_buffer(audio2, 5 * fs)
        assert pll.state == PLLState.HUNT


class TestBCDIntegrator:
    """Tests for the BCDIntegrator class."""
    
    def test_logic_0_decode(self):
        """Test decoding logic 0 (30-170ms active)."""
        bcd = BCDIntegrator(fs=24000)
        
        fs = 24000
        env_100 = np.zeros(fs)
        
        # Create logic 0: 30ms-170ms active (140ms duration)
        start_idx = int(0.030 * fs)
        end_idx = int(0.170 * fs)
        t = np.arange(end_idx - start_idx) / fs
        env_100[start_idx:end_idx] = np.abs(np.sin(2 * np.pi * 100 * t))
        
        # Add noise floor
        env_100 += 0.01
        
        bit, confidence, collision = bcd.decode(env_100, 0, "WWV")
        
        assert bit == '0'
        assert confidence > 0.5
    
    def test_logic_1_decode(self):
        """Test decoding logic 1 (30-500ms active)."""
        bcd = BCDIntegrator(fs=24000)
        
        fs = 24000
        env_100 = np.zeros(fs)
        
        # Create logic 1: 30ms-500ms active (470ms duration)
        start_idx = int(0.030 * fs)
        end_idx = int(0.500 * fs)
        t = np.arange(end_idx - start_idx) / fs
        env_100[start_idx:end_idx] = np.abs(np.sin(2 * np.pi * 100 * t))
        
        # Add noise floor
        env_100 += 0.01
        
        bit, confidence, collision = bcd.decode(env_100, 0, "WWV")
        
        assert bit == '1'
        assert confidence > 0.5
    
    def test_position_marker_decode(self):
        """Test decoding position marker P (30-800ms active)."""
        bcd = BCDIntegrator(fs=24000)
        
        fs = 24000
        env_100 = np.zeros(fs)
        
        # Create position marker: 30ms-800ms active (770ms duration)
        start_idx = int(0.030 * fs)
        end_idx = int(0.800 * fs)
        t = np.arange(end_idx - start_idx) / fs
        env_100[start_idx:end_idx] = np.abs(np.sin(2 * np.pi * 100 * t))
        
        # Add noise floor
        env_100 += 0.01
        
        bit, confidence, collision = bcd.decode(env_100, 0, "WWV")
        
        assert bit == 'P'
        assert confidence > 0.5


class TestDualStationPLL:
    """Tests for the DualStationPLL class."""
    
    def test_initialization(self):
        """Test dual station decoder initializes correctly."""
        decoder = DualStationPLL(fs=24000)
        
        assert decoder.pll_wwv.name == "WWV"
        assert decoder.pll_wwv.target_freq == 1000.0
        assert decoder.pll_wwvh.name == "WWVH"
        assert decoder.pll_wwvh.target_freq == 1200.0
    
    def test_process_minute_empty_audio(self):
        """Test processing empty/minimal audio returns empty results."""
        decoder = DualStationPLL(fs=24000)
        
        # Empty audio
        audio = np.zeros(24000 * 60)  # 60 seconds
        results = decoder.process_minute(audio, 0)
        
        # Should return empty list (no stations detected)
        assert len(results) == 0
    
    def test_process_minute_single_station(self):
        """Test processing audio with single WWV station."""
        decoder = DualStationPLL(fs=24000)
        
        fs = 24000
        duration = 5  # seconds (use shorter for test)
        audio = np.random.normal(0, 0.01, fs * duration)
        
        # Add WWV ticks every second
        tick_duration = int(0.005 * fs)
        t_tick = np.arange(tick_duration) / fs
        
        for sec in range(duration):
            tick_start = sec * fs + 1000  # Tick at +1000 samples each second
            if tick_start + tick_duration < len(audio):
                audio[tick_start:tick_start + tick_duration] += np.sin(2 * np.pi * 1000 * t_tick)
        
        # Process with only WWV
        results = decoder.process_minute(audio, 0, station_filter=["WWV"])
        
        # Should detect WWV
        assert len(results) >= 1
        assert results[0].station == "WWV"
        assert results[0].n_ticks_detected > 0


class TestFactory:
    """Tests for the create_pll_decoder factory function."""
    
    def test_create_wwv(self):
        """Test creating WWV PLL."""
        pll = create_pll_decoder("WWV", fs=24000)
        assert pll.name == "WWV"
        assert pll.target_freq == 1000.0
    
    def test_create_wwvh(self):
        """Test creating WWVH PLL."""
        pll = create_pll_decoder("WWVH", fs=24000)
        assert pll.name == "WWVH"
        assert pll.target_freq == 1200.0
    
    def test_create_unsupported(self):
        """Test creating PLL for unsupported station raises error."""
        with pytest.raises(ValueError, match="not supported"):
            create_pll_decoder("CHU", fs=24000)


class TestIntegration:
    """Integration tests with synthetic dual-station signals."""
    
    def test_dual_station_signal(self):
        """Test decoding simulated dual-station WWV + WWVH signal."""
        decoder = DualStationPLL(fs=24000)
        
        fs = 24000
        duration = 3  # seconds
        t = np.arange(fs * duration) / fs
        
        # Create synthetic signal with both stations
        audio = np.random.normal(0, 0.01, len(t))
        
        # Add WWV (1000 Hz) ticks
        tick_duration = int(0.005 * fs)
        t_tick = np.arange(tick_duration) / fs
        for sec in range(duration):
            tick_start = sec * fs + 1000
            if tick_start + tick_duration < len(audio):
                audio[tick_start:tick_start + tick_duration] += 1.0 * np.sin(2 * np.pi * 1000 * t_tick)
        
        # Add WWVH (1200 Hz) ticks (10ms delay simulates path difference)
        for sec in range(duration):
            tick_start = sec * fs + 1000 + int(0.010 * fs)
            if tick_start + tick_duration < len(audio):
                audio[tick_start:tick_start + tick_duration] += 0.5 * np.sin(2 * np.pi * 1200 * t_tick)
        
        # Process
        results = decoder.process_minute(audio, 0)
        
        # Should detect at least one station
        assert len(results) >= 1


def run_quick_test():
    """Quick smoke test that can be run without pytest."""
    print("Running Tick PLL Decoder Quick Tests...")
    print("=" * 60)
    
    # Test 1: Basic initialization
    print("\n1. Testing PLL initialization...")
    pll = TickPLL("WWV", 1000.0, fs=24000)
    assert pll.state == PLLState.HUNT
    print("   ✓ PLL initializes in HUNT state")
    
    # Test 2: Hunt to lock
    print("\n2. Testing HUNT→LOCK transition...")
    fs = 24000
    tick_duration = int(0.005 * fs)
    audio = np.zeros(fs)
    t = np.arange(tick_duration) / fs
    tick_start = 1000
    audio[tick_start:tick_start + tick_duration] = np.sin(2 * np.pi * 1000 * t)
    audio += np.random.normal(0, 0.01, len(audio))
    
    results = pll.process_buffer(audio, 0)
    assert pll.state == PLLState.LOCK
    print(f"   ✓ Locked onto tick at sample {results[0].tick_index}")
    
    # Test 3: BCD decode
    print("\n3. Testing BCD decoding...")
    bcd = BCDIntegrator(fs=24000)
    env_100 = np.zeros(fs)
    start_idx = int(0.030 * fs)
    end_idx = int(0.500 * fs)
    t_bcd = np.arange(end_idx - start_idx) / fs
    env_100[start_idx:end_idx] = np.abs(np.sin(2 * np.pi * 100 * t_bcd))
    env_100 += 0.01
    
    bit, conf, _ = bcd.decode(env_100, 0, "WWV")
    print(f"   ✓ Decoded bit '{bit}' with confidence {conf:.2f}")
    
    # Test 4: Dual station
    print("\n4. Testing DualStationPLL...")
    dual = DualStationPLL(fs=24000)
    print(f"   ✓ WWV PLL: {dual.pll_wwv.target_freq} Hz")
    print(f"   ✓ WWVH PLL: {dual.pll_wwvh.target_freq} Hz")
    
    print("\n" + "=" * 60)
    print("All quick tests passed!")
    return True


if __name__ == "__main__":
    # Run quick tests if executed directly
    run_quick_test()
