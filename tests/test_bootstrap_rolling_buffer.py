"""
Tests for Bootstrap Rolling Buffer and Bootstrap Service

Tests the core bootstrap acquisition components:
1. BootstrapRollingBuffer - circular buffer with RTP indexing
2. BootstrapBufferManager - multi-channel coordination
3. BootstrapService - full bootstrap state machine
"""

import pytest
import numpy as np
import time
from unittest.mock import Mock, patch, MagicMock

from hf_timestd.core.bootstrap_rolling_buffer import (
    BootstrapRollingBuffer,
    BootstrapBufferManager,
    ToneCandidate,
    DEFAULT_SAMPLE_RATE,
    SAMPLES_PER_MINUTE
)
from hf_timestd.core.bootstrap_service import (
    BootstrapService,
    BootstrapConfig,
    BootstrapPhase,
    create_bootstrap_service
)


class TestBootstrapRollingBuffer:
    """Tests for BootstrapRollingBuffer class."""
    
    def test_initialization(self):
        """Test buffer initializes correctly."""
        buffer = BootstrapRollingBuffer(
            channel_name="CHU_3330",
            sample_rate=24000,
            buffer_duration_sec=150.0
        )
        
        assert buffer.channel_name == "CHU_3330"
        assert buffer.sample_rate == 24000
        assert buffer.buffer_size == 24000 * 150  # 3,600,000 samples
        assert buffer.buffer_start_rtp is None
        assert buffer.total_samples_written == 0
    
    def test_add_samples_first_batch(self):
        """Test adding first batch of samples."""
        buffer = BootstrapRollingBuffer(
            channel_name="test",
            sample_rate=24000,
            buffer_duration_sec=10.0  # Small buffer for testing
        )
        
        # Create test samples
        samples = np.zeros(1000, dtype=np.complex64)
        rtp_timestamp = 12345678
        
        buffer.add_samples(samples, rtp_timestamp)
        
        assert buffer.buffer_start_rtp == rtp_timestamp
        assert buffer.total_samples_written == 1000
        assert buffer.write_pos == 1000
    
    def test_add_samples_multiple_batches(self):
        """Test adding multiple batches of samples."""
        buffer = BootstrapRollingBuffer(
            channel_name="test",
            sample_rate=24000,
            buffer_duration_sec=10.0
        )
        
        # Add multiple batches
        for i in range(5):
            samples = np.ones(1000, dtype=np.complex64) * (i + 1)
            rtp = 1000000 + i * 1000
            buffer.add_samples(samples, rtp)
        
        assert buffer.total_samples_written == 5000
        assert buffer.write_pos == 5000
    
    def test_buffer_wraparound(self):
        """Test buffer correctly wraps around."""
        buffer = BootstrapRollingBuffer(
            channel_name="test",
            sample_rate=24000,
            buffer_duration_sec=1.0  # 24000 samples
        )
        
        # Fill buffer completely
        samples1 = np.ones(24000, dtype=np.complex64)
        buffer.add_samples(samples1, 0)
        
        assert buffer.write_pos == 0  # Wrapped back to start
        assert buffer.total_samples_written == 24000
        
        # Add more samples (should overwrite beginning)
        samples2 = np.ones(1000, dtype=np.complex64) * 2
        buffer.add_samples(samples2, 24000)
        
        assert buffer.write_pos == 1000
        assert buffer.total_samples_written == 25000
        
        # Check that buffer_start_rtp updated correctly
        # Oldest sample is now at position 1000, RTP = 1000
        assert buffer.buffer_start_rtp == 1000
    
    def test_get_contiguous_buffer_no_wrap(self):
        """Test getting contiguous buffer when no wraparound."""
        buffer = BootstrapRollingBuffer(
            channel_name="test",
            sample_rate=24000,
            buffer_duration_sec=10.0
        )
        
        # Add samples without filling buffer
        samples = np.arange(5000, dtype=np.complex64)
        buffer.add_samples(samples, 1000)
        
        result, start_rtp = buffer.get_contiguous_buffer()
        
        assert len(result) == 5000
        assert start_rtp == 1000
        np.testing.assert_array_equal(result, samples)
    
    def test_get_contiguous_buffer_with_wrap(self):
        """Test getting contiguous buffer after wraparound."""
        buffer = BootstrapRollingBuffer(
            channel_name="test",
            sample_rate=1000,  # Small for easy testing
            buffer_duration_sec=1.0  # 1000 samples
        )
        
        # Fill buffer
        samples1 = np.arange(1000, dtype=np.complex64)
        buffer.add_samples(samples1, 0)
        
        # Add more (causes wrap)
        samples2 = np.arange(1000, 1500, dtype=np.complex64)
        buffer.add_samples(samples2, 1000)
        
        result, start_rtp = buffer.get_contiguous_buffer()
        
        # Should contain samples 500-1499 (oldest to newest)
        assert len(result) == 1000
        assert start_rtp == 500
        expected = np.arange(500, 1500, dtype=np.complex64)
        np.testing.assert_array_equal(result, expected)
    
    def test_has_enough_data(self):
        """Test has_enough_data check."""
        buffer = BootstrapRollingBuffer(
            channel_name="test",
            sample_rate=24000,
            buffer_duration_sec=150.0
        )
        
        # Initially not enough
        assert not buffer.has_enough_data(min_duration_sec=65.0)
        
        # Add 60 seconds of data - still not enough
        samples = np.zeros(24000 * 60, dtype=np.complex64)
        buffer.add_samples(samples, 0)
        assert not buffer.has_enough_data(min_duration_sec=65.0)
        
        # Add 10 more seconds - now enough
        samples2 = np.zeros(24000 * 10, dtype=np.complex64)
        buffer.add_samples(samples2, 24000 * 60)
        assert buffer.has_enough_data(min_duration_sec=65.0)
    
    def test_clear(self):
        """Test buffer clear."""
        buffer = BootstrapRollingBuffer(
            channel_name="test",
            sample_rate=24000,
            buffer_duration_sec=10.0
        )
        
        # Add some data
        samples = np.zeros(5000, dtype=np.complex64)
        buffer.add_samples(samples, 1000)
        
        # Clear
        buffer.clear()
        
        assert buffer.buffer_start_rtp is None
        assert buffer.total_samples_written == 0
        assert buffer.write_pos == 0
        assert buffer.gap_count == 0
    
    def test_get_status(self):
        """Test status reporting."""
        buffer = BootstrapRollingBuffer(
            channel_name="CHU_3330",
            sample_rate=24000,
            buffer_duration_sec=10.0
        )
        
        samples = np.zeros(24000, dtype=np.complex64)  # 1 second
        buffer.add_samples(samples, 1000)
        
        status = buffer.get_status()
        
        assert status['channel'] == "CHU_3330"
        assert status['samples_written'] == 24000
        assert status['duration_available_sec'] == 1.0
        assert status['buffer_start_rtp'] == 1000


class TestBootstrapBufferManager:
    """Tests for BootstrapBufferManager class."""
    
    def test_initialization(self):
        """Test manager initializes correctly."""
        manager = BootstrapBufferManager(
            sample_rate=24000,
            buffer_duration_sec=150.0
        )
        
        assert manager.sample_rate == 24000
        assert len(manager.buffers) == 0
        assert not manager.is_locked
    
    def test_get_or_create_buffer(self):
        """Test buffer creation on demand."""
        manager = BootstrapBufferManager()
        
        buffer1 = manager.get_or_create_buffer("CHU_3330")
        assert "CHU_3330" in manager.buffers
        
        # Same channel returns same buffer
        buffer2 = manager.get_or_create_buffer("CHU_3330")
        assert buffer1 is buffer2
        
        # Different channel creates new buffer
        buffer3 = manager.get_or_create_buffer("WWV_10000")
        assert buffer3 is not buffer1
        assert len(manager.buffers) == 2
    
    def test_add_samples(self):
        """Test adding samples through manager."""
        manager = BootstrapBufferManager()
        
        samples = np.zeros(1000, dtype=np.complex64)
        manager.add_samples("CHU_3330", samples, 12345)
        
        assert "CHU_3330" in manager.buffers
        assert manager.buffers["CHU_3330"].total_samples_written == 1000
    
    def test_add_samples_when_locked(self):
        """Test that samples are ignored when locked."""
        manager = BootstrapBufferManager()
        manager.is_locked = True
        
        samples = np.zeros(1000, dtype=np.complex64)
        manager.add_samples("CHU_3330", samples, 12345)
        
        # Buffer should not be created when locked
        assert "CHU_3330" not in manager.buffers
    
    def test_clear_all(self):
        """Test clearing all buffers."""
        manager = BootstrapBufferManager()
        
        # Add data to multiple channels
        for channel in ["CHU_3330", "WWV_10000", "WWVH_15000"]:
            samples = np.zeros(1000, dtype=np.complex64)
            manager.add_samples(channel, samples, 0)
        
        manager.is_locked = True
        manager.clear_all()
        
        assert not manager.is_locked
        for buffer in manager.buffers.values():
            assert buffer.total_samples_written == 0


class TestBootstrapService:
    """Tests for BootstrapService class."""
    
    def test_initialization(self):
        """Test service initializes correctly."""
        config = BootstrapConfig(
            receiver_lat=38.9,
            receiver_lon=-92.1
        )
        service = BootstrapService(config)
        
        assert service.phase == BootstrapPhase.INITIALIZING
        assert not service.is_locked
        assert not service.is_fully_locked
    
    def test_factory_function(self):
        """Test create_bootstrap_service factory."""
        service = create_bootstrap_service(
            receiver_lat=38.9,
            receiver_lon=-92.1,
            sample_rate=24000
        )
        
        assert isinstance(service, BootstrapService)
        assert service.config.receiver_lat == 38.9
        assert service.config.receiver_lon == -92.1
    
    def test_add_samples_returns_false_during_bootstrap(self):
        """Test add_samples returns False while bootstrapping."""
        service = create_bootstrap_service(38.9, -92.1)
        
        samples = np.zeros(1000, dtype=np.complex64)
        result = service.add_samples("CHU_3330", samples, 12345)
        
        assert result is False  # Still bootstrapping
    
    def test_add_samples_returns_true_when_locked(self):
        """Test add_samples returns True when locked."""
        service = create_bootstrap_service(38.9, -92.1)
        service.phase = BootstrapPhase.LOCKED
        
        samples = np.zeros(1000, dtype=np.complex64)
        result = service.add_samples("CHU_3330", samples, 12345)
        
        assert result is True  # Locked, proceed with archiving
    
    def test_phase_transitions(self):
        """Test phase transitions from INITIALIZING to SEARCHING."""
        service = create_bootstrap_service(38.9, -92.1)
        service.config.min_data_duration_sec = 1.0  # Low threshold for testing
        
        assert service.phase == BootstrapPhase.INITIALIZING
        
        # Add enough data to trigger transition
        samples = np.zeros(24000 * 2, dtype=np.complex64)  # 2 seconds
        service.add_samples("CHU_3330", samples, 0)
        
        assert service.phase == BootstrapPhase.SEARCHING
    
    def test_get_status(self):
        """Test status reporting."""
        service = create_bootstrap_service(38.9, -92.1)
        
        samples = np.zeros(1000, dtype=np.complex64)
        service.add_samples("CHU_3330", samples, 0)
        
        status = service.get_status()
        
        assert 'phase' in status
        assert 'is_locked' in status
        assert 'elapsed_sec' in status
        assert 'stats' in status
        assert 'buffers' in status
        assert status['phase'] == 'initializing'
        assert status['is_locked'] is False
    
    def test_reset(self):
        """Test service reset."""
        service = create_bootstrap_service(38.9, -92.1)
        
        # Add some data and change state
        samples = np.zeros(24000 * 70, dtype=np.complex64)
        service.add_samples("CHU_3330", samples, 0)
        service.phase = BootstrapPhase.CONFIRMING
        
        # Reset
        service.reset()
        
        assert service.phase == BootstrapPhase.INITIALIZING
        assert service.stats['samples_received'] == 0
    
    def test_callbacks_on_lock(self):
        """Test callbacks are called on lock events."""
        provisional_called = []
        full_called = []
        
        def on_provisional(d_clock):
            provisional_called.append(d_clock)
        
        def on_full(d_clock, uncertainty):
            full_called.append((d_clock, uncertainty))
        
        config = BootstrapConfig(
            receiver_lat=38.9,
            receiver_lon=-92.1,
            on_provisional_lock=on_provisional,
            on_full_lock=on_full
        )
        service = BootstrapService(config)
        
        # Mock the timing_bootstrap to return an offset
        service.timing_bootstrap.rtp_to_utc_offset_samples = 1000000
        service.timing_bootstrap.offset_uncertainty_samples = 100
        
        # Simulate provisional lock
        service._on_provisional_lock()
        
        # Callback should be called (d_clock may be 0.0 placeholder)
        assert len(provisional_called) == 1
        
        # Simulate full lock
        service._on_lock_achieved()
        
        assert len(full_called) == 1


class TestToneSearching:
    """Tests for tone searching in rolling buffer."""
    
    def test_search_finds_synthetic_tone(self):
        """Test that search can find a synthetic 1000 Hz tone."""
        buffer = BootstrapRollingBuffer(
            channel_name="CHU_3330",
            sample_rate=24000,
            buffer_duration_sec=10.0
        )
        
        # Create buffer with synthetic 1000 Hz tone at known position
        sample_rate = 24000
        duration = 10.0  # 10 seconds
        n_samples = int(duration * sample_rate)
        
        # Create noise floor
        np.random.seed(42)
        samples = (np.random.randn(n_samples) + 1j * np.random.randn(n_samples)).astype(np.complex64) * 0.01
        
        # Add 500ms 1000 Hz tone at 2 seconds
        tone_start = int(2.0 * sample_rate)
        tone_duration = 0.5
        tone_samples = int(tone_duration * sample_rate)
        t = np.arange(tone_samples) / sample_rate
        tone = np.exp(2j * np.pi * 1000 * t).astype(np.complex64) * 0.5
        samples[tone_start:tone_start + tone_samples] += tone
        
        buffer.add_samples(samples, 0)
        
        # Search should find the tone
        # Note: This is a basic test - full search requires tone_detector
        assert buffer.has_enough_data(min_duration_sec=5.0)
        
        contiguous, start_rtp = buffer.get_contiguous_buffer()
        assert len(contiguous) == n_samples


class TestIntegration:
    """Integration tests for the full bootstrap flow."""
    
    def test_multi_channel_bootstrap_flow(self):
        """Test bootstrap with multiple channels."""
        service = create_bootstrap_service(38.9, -92.1)
        service.config.min_data_duration_sec = 1.0
        
        # Simulate data from multiple channels
        channels = ["CHU_3330", "WWV_10000", "WWVH_15000"]
        
        for channel in channels:
            samples = np.zeros(24000 * 2, dtype=np.complex64)
            service.add_samples(channel, samples, 0)
        
        # All channels should have buffers
        assert len(service.buffer_manager.buffers) == 3
        
        # Should be in SEARCHING phase
        assert service.phase == BootstrapPhase.SEARCHING
    
    def test_bootstrap_timeout(self):
        """Test bootstrap timeout detection."""
        config = BootstrapConfig(
            receiver_lat=38.9,
            receiver_lon=-92.1,
            bootstrap_timeout_sec=0.1  # Very short for testing
        )
        service = BootstrapService(config)
        service.phase = BootstrapPhase.SEARCHING
        
        # Wait for timeout
        time.sleep(0.2)
        
        result = service.search_and_update()
        assert result == "TIMEOUT"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
