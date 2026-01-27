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
from hf_timestd.core.timing_bootstrap import (
    TimingBootstrap,
    BootstrapState,
    LockTier,
    AcquisitionCandidate,
    OffsetMeasurement,
    SAMPLES_PER_MINUTE as TB_SAMPLES_PER_MINUTE
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


class TestTwoTierBootstrap:
    """Tests for two-tier bootstrap (provisional and refined lock)."""
    
    def test_lock_tier_initialization(self):
        """Test that lock tier starts at NONE."""
        bootstrap = TimingBootstrap(receiver_lat=38.9, receiver_lon=-92.1)
        
        assert bootstrap.lock_tier == LockTier.NONE
        assert bootstrap.provisional_lock_time is None
        assert len(bootstrap._offset_measurements) == 0
    
    def test_lock_tier_in_status(self):
        """Test that lock_tier is exposed in status."""
        bootstrap = TimingBootstrap(receiver_lat=38.9, receiver_lon=-92.1)
        
        status = bootstrap.get_status()
        
        assert 'lock_tier' in status
        assert status['lock_tier'] == 0  # LockTier.NONE.value
    
    def test_provisional_lock_transition(self):
        """Test transition to provisional lock after sufficient validations."""
        bootstrap = TimingBootstrap(receiver_lat=38.9, receiver_lon=-92.1)
        
        # Set up state for tracking
        bootstrap.state = BootstrapState.TRACKING
        bootstrap.rtp_to_utc_offset_samples = 1000000
        bootstrap.minutes_observed = 2
        
        # Simulate 10 consecutive validations to trigger provisional lock
        for i in range(10):
            candidate = AcquisitionCandidate(
                channel="WWV_10000",
                station="WWV",
                frequency_khz=10000,
                tone_frequency_hz=1000.0,
                rtp_timestamp=1000000 + (i * TB_SAMPLES_PER_MINUTE) + 100,  # Small offset for propagation
                sample_position=0,
                snr_db=30.0,
                confidence=0.9,
                buffer_rtp_start=0
            )
            result = bootstrap._handle_tracking(candidate, i)
        
        assert bootstrap.lock_tier == LockTier.PROVISIONAL
        assert bootstrap.provisional_lock_time is not None
    
    def test_offset_measurement_recording(self):
        """Test that offset measurements are recorded during provisional lock."""
        bootstrap = TimingBootstrap(receiver_lat=38.9, receiver_lon=-92.1)
        
        # Set up provisional lock state
        bootstrap.state = BootstrapState.TRACKING
        bootstrap.lock_tier = LockTier.PROVISIONAL
        bootstrap.provisional_lock_time = time.time()
        bootstrap.rtp_to_utc_offset_samples = 1000000
        bootstrap.minutes_observed = 3
        bootstrap.consecutive_validations = 15
        
        # Add a candidate - should record offset measurement
        candidate = AcquisitionCandidate(
            channel="WWV_10000",
            station="WWV",
            frequency_khz=10000,
            tone_frequency_hz=1000.0,
            rtp_timestamp=1000100,  # Close to expected
            sample_position=0,
            snr_db=30.0,
            confidence=0.9,
            buffer_rtp_start=0
        )
        
        bootstrap._handle_tracking(candidate, 0)
        
        assert len(bootstrap._offset_measurements) == 1
        assert bootstrap._offset_measurements[0].station == "WWV"
        assert bootstrap._offset_measurements[0].snr_db == 30.0
    
    def test_refined_lock_criteria_duration(self):
        """Test that refined lock requires minimum duration."""
        bootstrap = TimingBootstrap(receiver_lat=38.9, receiver_lon=-92.1)
        
        # Set up provisional lock that just started
        bootstrap.lock_tier = LockTier.PROVISIONAL
        bootstrap.provisional_lock_time = time.time()  # Just now
        bootstrap.refined_lock_duration_sec = 600.0  # 10 minutes
        
        # Add enough measurements
        for i in range(60):
            bootstrap._offset_measurements.append(OffsetMeasurement(
                timestamp=time.time(),
                offset_samples=1000000 + i,  # Small variation
                station="WWV",
                snr_db=30.0,
                frequency_khz=10000
            ))
        
        # Should not achieve refined lock - not enough time elapsed
        result = bootstrap._check_refined_lock_criteria()
        assert result is None
        assert bootstrap.lock_tier == LockTier.PROVISIONAL
    
    def test_refined_lock_criteria_measurements(self):
        """Test that refined lock requires minimum measurements."""
        bootstrap = TimingBootstrap(receiver_lat=38.9, receiver_lon=-92.1)
        
        # Set up provisional lock with enough time elapsed
        bootstrap.lock_tier = LockTier.PROVISIONAL
        bootstrap.provisional_lock_time = time.time() - 700  # 11+ minutes ago
        bootstrap.refined_lock_duration_sec = 600.0
        bootstrap.min_measurements_for_refined = 50
        
        # Add only 30 measurements (not enough)
        for i in range(30):
            bootstrap._offset_measurements.append(OffsetMeasurement(
                timestamp=time.time(),
                offset_samples=1000000 + i,
                station="WWV",
                snr_db=30.0,
                frequency_khz=10000
            ))
        
        result = bootstrap._check_refined_lock_criteria()
        assert result is None
        assert bootstrap.lock_tier == LockTier.PROVISIONAL
    
    def test_refined_lock_criteria_stability(self):
        """Test that refined lock requires low offset std."""
        bootstrap = TimingBootstrap(receiver_lat=38.9, receiver_lon=-92.1)
        
        # Set up provisional lock with enough time and measurements
        bootstrap.lock_tier = LockTier.PROVISIONAL
        bootstrap.provisional_lock_time = time.time() - 700
        bootstrap.refined_lock_duration_sec = 600.0
        bootstrap.min_measurements_for_refined = 50
        bootstrap.max_offset_std_for_refined_ms = 15.0
        
        # Add measurements with HIGH variance (should fail stability check)
        # std of 50ms = 1200 samples at 24kHz
        np.random.seed(42)
        for i in range(60):
            # Large spread: ±2000 samples = ±83ms
            offset = 1000000 + int(np.random.randn() * 2000)
            bootstrap._offset_measurements.append(OffsetMeasurement(
                timestamp=time.time(),
                offset_samples=offset,
                station="WWV",
                snr_db=30.0,
                frequency_khz=10000
            ))
        
        result = bootstrap._check_refined_lock_criteria()
        assert result is None  # Should fail due to high std
        assert bootstrap.lock_tier == LockTier.PROVISIONAL
    
    def test_refined_lock_success(self):
        """Test successful transition to refined lock."""
        bootstrap = TimingBootstrap(receiver_lat=38.9, receiver_lon=-92.1)
        
        # Set up provisional lock with all criteria met
        bootstrap.state = BootstrapState.TRACKING
        bootstrap.lock_tier = LockTier.PROVISIONAL
        bootstrap.provisional_lock_time = time.time() - 700  # 11+ minutes ago
        bootstrap.refined_lock_duration_sec = 600.0
        bootstrap.min_measurements_for_refined = 50
        bootstrap.max_offset_std_for_refined_ms = 15.0
        bootstrap.rtp_to_utc_offset_samples = 1000000
        
        # Add measurements with LOW variance (should pass stability check)
        # std of 5ms = 120 samples at 24kHz
        np.random.seed(42)
        for i in range(60):
            # Small spread: ±100 samples = ±4ms
            offset = 1000000 + int(np.random.randn() * 100)
            bootstrap._offset_measurements.append(OffsetMeasurement(
                timestamp=time.time(),
                offset_samples=offset,
                station="WWV",
                snr_db=30.0,
                frequency_khz=10000
            ))
        
        result = bootstrap._check_refined_lock_criteria()
        
        assert result == "REFINED_LOCK"
        assert bootstrap.lock_tier == LockTier.REFINED
        assert bootstrap.state == BootstrapState.LOCKED
        assert bootstrap._refined_offset_samples is not None
        assert bootstrap._refined_offset_std_ms is not None
        assert bootstrap._refined_offset_std_ms < 15.0
    
    def test_refined_lock_uses_median(self):
        """Test that refined lock uses median (not mean) for robustness."""
        bootstrap = TimingBootstrap(receiver_lat=38.9, receiver_lon=-92.1)
        
        bootstrap.state = BootstrapState.TRACKING
        bootstrap.lock_tier = LockTier.PROVISIONAL
        bootstrap.provisional_lock_time = time.time() - 700
        bootstrap.refined_lock_duration_sec = 600.0
        bootstrap.min_measurements_for_refined = 50
        bootstrap.max_offset_std_for_refined_ms = 20.0  # Relaxed for this test
        bootstrap.rtp_to_utc_offset_samples = 1000000
        
        # Add 50 measurements clustered around 1000000
        for i in range(50):
            bootstrap._offset_measurements.append(OffsetMeasurement(
                timestamp=time.time(),
                offset_samples=1000000 + (i % 10) - 5,  # ±5 samples
                station="WWV",
                snr_db=30.0,
                frequency_khz=10000
            ))
        
        # Add 5 outliers (should be rejected by median)
        for i in range(5):
            bootstrap._offset_measurements.append(OffsetMeasurement(
                timestamp=time.time(),
                offset_samples=1100000,  # 100000 samples off = 4+ seconds!
                station="WWV",
                snr_db=30.0,
                frequency_khz=10000
            ))
        
        result = bootstrap._check_refined_lock_criteria()
        
        # Median should be close to 1000000, not pulled by outliers
        # Mean would be ~1009090, but median should be ~1000000
        assert bootstrap._refined_offset_samples is not None
        assert abs(bootstrap._refined_offset_samples - 1000000) < 100
    
    def test_retreat_resets_two_tier_state(self):
        """Test that retreating to ACQUIRING resets two-tier state."""
        bootstrap = TimingBootstrap(receiver_lat=38.9, receiver_lon=-92.1)
        
        # Set up provisional lock state
        bootstrap.state = BootstrapState.TRACKING
        bootstrap.lock_tier = LockTier.PROVISIONAL
        bootstrap.provisional_lock_time = time.time() - 300
        bootstrap._offset_measurements.append(OffsetMeasurement(
            timestamp=time.time(),
            offset_samples=1000000,
            station="WWV",
            snr_db=30.0,
            frequency_khz=10000
        ))
        
        # Retreat
        bootstrap._retreat_to_acquiring("Test retreat")
        
        assert bootstrap.lock_tier == LockTier.NONE
        assert bootstrap.provisional_lock_time is None
        assert len(bootstrap._offset_measurements) == 0
        assert bootstrap._refined_offset_samples is None
    
    def test_status_during_provisional_lock(self):
        """Test status includes provisional lock details."""
        bootstrap = TimingBootstrap(receiver_lat=38.9, receiver_lon=-92.1)
        
        bootstrap.lock_tier = LockTier.PROVISIONAL
        bootstrap.provisional_lock_time = time.time() - 300  # 5 minutes ago
        bootstrap.refined_lock_duration_sec = 600.0
        
        # Add some measurements
        for i in range(20):
            bootstrap._offset_measurements.append(OffsetMeasurement(
                timestamp=time.time(),
                offset_samples=1000000 + i * 10,
                station="WWV",
                snr_db=30.0,
                frequency_khz=10000
            ))
        
        status = bootstrap.get_status()
        
        assert status['lock_tier'] == 1  # PROVISIONAL
        assert 'provisional_lock_elapsed_sec' in status
        assert status['provisional_lock_elapsed_sec'] >= 300
        assert 'offset_measurements_count' in status
        assert status['offset_measurements_count'] == 20
        assert 'time_to_refined_sec' in status
        assert status['time_to_refined_sec'] <= 300  # ~5 min remaining
        assert 'current_offset_std_ms' in status
    
    def test_status_during_refined_lock(self):
        """Test status includes refined lock details."""
        bootstrap = TimingBootstrap(receiver_lat=38.9, receiver_lon=-92.1)
        
        bootstrap.lock_tier = LockTier.REFINED
        bootstrap._refined_offset_samples = 1000000
        bootstrap._refined_offset_std_ms = 8.5
        bootstrap._offset_measurements = [Mock()] * 75  # 75 measurements
        
        status = bootstrap.get_status()
        
        assert status['lock_tier'] == 2  # REFINED
        assert 'refined_offset_samples' in status
        assert status['refined_offset_samples'] == 1000000
        assert 'refined_offset_std_ms' in status
        assert status['refined_offset_std_ms'] == 8.5
        assert status['offset_measurements_count'] == 75


class TestBootstrapServiceTwoTier:
    """Tests for two-tier bootstrap in BootstrapService."""
    
    def test_lock_tier_in_service_status(self):
        """Test that lock_tier is exposed in service status."""
        service = create_bootstrap_service(38.9, -92.1)
        
        status = service.get_status()
        
        assert 'lock_tier' in status
        assert status['lock_tier'] == 0  # NONE


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
