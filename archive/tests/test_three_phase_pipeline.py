#!/usr/bin/env python3
"""
Tests for the Three-Phase Robust Time-Aligned Data Pipeline

This test suite validates:
- Phase 1: Raw Archive Writer
- Phase 2: Clock Offset Engine
- Phase 3: Corrected Product Generator
- Pipeline Orchestrator integration
"""

import numpy as np
import pytest
import tempfile
import time
from pathlib import Path
from datetime import datetime, timezone

# Skip all tests if digital_rf is not available
pytest.importorskip("digital_rf")


class TestPhase1RawArchive:
    """Tests for Phase 1: Immutable Raw Archive"""
    
    def test_raw_archive_config(self):
        """Test RawArchiveConfig creation"""
        from grape_recorder.grape import RawArchiveConfig
        
        config = RawArchiveConfig(
            output_dir=Path('/tmp/test_archive'),
            channel_name='WWV_10MHz',
            frequency_hz=10e6,
            sample_rate=20000,
            station_config={'callsign': 'TEST', 'grid_square': 'EM38ww'},
            compression='gzip',
            file_duration_sec=3600
        )
        
        assert config.sample_rate == 20000
        assert config.compression == 'gzip'
        assert config.file_duration_sec == 3600
    
    def test_system_time_reference(self):
        """Test SystemTimeReference calculations"""
        from grape_recorder.grape import SystemTimeReference
        
        ref = SystemTimeReference(
            rtp_timestamp=1000000,
            system_time=1733222400.0,
            ntp_offset_ms=5.0,
            sample_rate=20000
        )
        
        # Calculate time at 1 second later (20000 samples)
        time_at_sample = ref.calculate_time_at_sample(1000000 + 20000)
        assert abs(time_at_sample - 1733222401.0) < 0.001
        
        # Test serialization
        d = ref.to_dict()
        ref2 = SystemTimeReference.from_dict(d)
        assert ref2.rtp_timestamp == ref.rtp_timestamp
        assert ref2.system_time == ref.system_time
    
    def test_raw_archive_writer_creation(self):
        """Test RawArchiveWriter initialization"""
        from grape_recorder.grape import create_raw_archive_writer
        
        with tempfile.TemporaryDirectory() as tmpdir:
            writer = create_raw_archive_writer(
                output_dir=Path(tmpdir),
                channel_name='WWV_10MHz',
                frequency_hz=10e6,
                sample_rate=20000,
                station_config={'callsign': 'TEST'},
                compression='gzip'
            )
            
            assert writer.config.sample_rate == 20000
            assert writer.config.compression == 'gzip'
            assert writer.samples_written == 0
            
            writer.close()


class TestPhase2ClockOffset:
    """Tests for Phase 2: Clock Offset Series"""
    
    def test_clock_offset_measurement(self):
        """Test ClockOffsetMeasurement dataclass"""
        from grape_recorder.grape import ClockOffsetMeasurement, ClockOffsetQuality
        
        measurement = ClockOffsetMeasurement(
            system_time=1733222400.0,
            utc_time=1733222399.985,
            minute_boundary_utc=1733222400.0,
            clock_offset_ms=15.0,
            station='WWV',
            frequency_mhz=10.0,
            propagation_delay_ms=14.5,
            propagation_mode='1F',
            n_hops=1,
            confidence=0.92,
            uncertainty_ms=0.5,
            quality_grade=ClockOffsetQuality.EXCELLENT,
            snr_db=25.0,
            utc_verified=True
        )
        
        assert measurement.clock_offset_ms == 15.0
        assert measurement.quality_grade == ClockOffsetQuality.EXCELLENT
        
        # Test serialization
        d = measurement.to_dict()
        assert d['quality_grade'] == 'A'
        
        m2 = ClockOffsetMeasurement.from_dict(d)
        assert m2.clock_offset_ms == 15.0
    
    def test_clock_offset_series(self):
        """Test ClockOffsetSeries interpolation"""
        from grape_recorder.grape import ClockOffsetSeries, ClockOffsetMeasurement, ClockOffsetQuality
        
        series = ClockOffsetSeries(
            channel_name='WWV_10MHz',
            frequency_hz=10e6,
            receiver_grid='EM38ww'
        )
        
        # Add two measurements
        series.add_measurement(ClockOffsetMeasurement(
            system_time=1733222400.0,
            utc_time=1733222399.985,
            minute_boundary_utc=1733222400.0,
            clock_offset_ms=15.0,
            station='WWV',
            frequency_mhz=10.0,
            propagation_delay_ms=14.5,
            propagation_mode='1F',
            n_hops=1,
            confidence=0.92,
            uncertainty_ms=0.5,
            quality_grade=ClockOffsetQuality.EXCELLENT
        ))
        
        series.add_measurement(ClockOffsetMeasurement(
            system_time=1733222460.0,
            utc_time=1733222459.980,
            minute_boundary_utc=1733222460.0,
            clock_offset_ms=20.0,
            station='WWV',
            frequency_mhz=10.0,
            propagation_delay_ms=14.5,
            propagation_mode='1F',
            n_hops=1,
            confidence=0.90,
            uncertainty_ms=0.6,
            quality_grade=ClockOffsetQuality.GOOD
        ))
        
        # Test interpolation at midpoint
        result = series.get_offset_at_time(1733222430.0, interpolate=True)
        assert result is not None
        offset, uncertainty = result
        assert abs(offset - 17.5) < 0.1  # Midpoint between 15 and 20
        
        # Test bounds
        assert series.start_time == 1733222400.0
        assert series.end_time == 1733222460.0
    
    def test_clock_offset_quality_grades(self):
        """Test quality grade enum"""
        from grape_recorder.grape import ClockOffsetQuality
        
        assert ClockOffsetQuality.EXCELLENT.value == 'A'
        assert ClockOffsetQuality.GOOD.value == 'B'
        assert ClockOffsetQuality.FAIR.value == 'C'
        assert ClockOffsetQuality.POOR.value == 'D'


class TestPhase3Product:
    """Tests for Phase 3: Corrected Telemetry Product"""
    
    def test_product_config(self):
        """Test ProductConfig creation"""
        from grape_recorder.grape import ProductConfig
        
        config = ProductConfig(
            raw_archive_dir=Path('/tmp/raw'),
            clock_offset_dir=Path('/tmp/clock'),
            output_dir=Path('/tmp/output'),
            channel_name='WWV_10MHz',
            frequency_hz=10e6,
            station_config={'callsign': 'TEST'},
            input_sample_rate=20000,
            output_sample_rate=10
        )
        
        assert config.input_sample_rate == 20000
        assert config.output_sample_rate == 10


class TestPipelineOrchestrator:
    """Tests for Pipeline Orchestrator"""
    
    def test_pipeline_config(self):
        """Test PipelineConfig creation"""
        from grape_recorder.grape import PipelineConfig
        
        config = PipelineConfig(
            data_dir=Path('/tmp/grape'),
            channel_name='WWV_10MHz',
            frequency_hz=10e6,
            sample_rate=20000,
            receiver_grid='EM38ww',
            station_config={'callsign': 'TEST'}
        )
        
        assert config.raw_archive_dir == Path('/tmp/grape/raw_archive')
        assert config.clock_offset_dir == Path('/tmp/grape/clock_offset')
        assert config.processed_dir == Path('/tmp/grape/processed')
    
    def test_pipeline_state_enum(self):
        """Test PipelineState enum"""
        from grape_recorder.grape import PipelineState
        
        assert PipelineState.IDLE.value == 'idle'
        assert PipelineState.RUNNING.value == 'running'
        assert PipelineState.STOPPING.value == 'stopping'


class TestTransmissionTimeSolver:
    """Tests for TransmissionTimeSolver (UTC back-calculation)"""
    
    def test_grid_to_latlon(self):
        """Test grid square to lat/lon conversion"""
        from grape_recorder.grape import grid_to_latlon
        
        # EM38ww is approximately Kansas City area
        lat, lon = grid_to_latlon('EM38ww')
        assert 38 < lat < 40
        assert -95 < lon < -93
    
    def test_solver_creation(self):
        """Test TransmissionTimeSolver creation from grid"""
        from grape_recorder.grape import create_solver_from_grid
        
        solver = create_solver_from_grid('EM38ww', sample_rate=20000)
        
        # Check distances are calculated
        assert 'WWV' in solver.station_distances
        assert 'WWVH' in solver.station_distances
        
        # WWV is ~800 km from Kansas
        assert 500 < solver.station_distances['WWV'] < 1500
        
        # WWVH is ~5500 km from Kansas
        assert 4000 < solver.station_distances['WWVH'] < 7000
    
    def test_solver_propagation_modes(self):
        """Test propagation mode calculation"""
        from grape_recorder.grape import create_solver_from_grid, PropagationMode
        
        solver = create_solver_from_grid('EM38ww', sample_rate=20000)
        
        # Solve for WWV at 10 MHz
        result = solver.solve(
            station='WWV',
            frequency_mhz=10.0,
            arrival_rtp=1000000,
            delay_spread_ms=0.5,
            doppler_std_hz=0.1,
            fss_db=-1.0,
            expected_second_rtp=999700  # ~15ms before arrival
        )
        
        # Should identify a valid mode
        assert result.mode != PropagationMode.UNKNOWN
        assert result.propagation_delay_ms > 0
        assert 0 <= result.confidence <= 1
    
    def test_multi_station_solver(self):
        """Test MultiStationSolver for cross-channel correlation"""
        from grape_recorder.grape import create_multi_station_solver
        
        solver = create_multi_station_solver('EM38ww', sample_rate=20000)
        
        # Add observations
        solver.add_observation(
            station='WWV',
            frequency_mhz=10.0,
            arrival_rtp=1000000,
            expected_second_rtp=999700,
            snr_db=25.0,
            delay_spread_ms=0.5
        )
        
        # Should be able to solve (even with single observation)
        result = solver.solve_combined()
        
        assert result.n_measurements >= 0
        assert result.quality_grade in ['A', 'B', 'C', 'D']


class TestPipelineRecorder:
    """Tests for PipelineRecorder (drop-in replacement)"""
    
    def test_pipeline_recorder_config(self):
        """Test PipelineRecorderConfig creation"""
        from grape_recorder.grape import PipelineRecorderConfig
        
        config = PipelineRecorderConfig(
            ssrc=20100,
            frequency_hz=10e6,
            sample_rate=20000,
            description='WWV_10MHz',
            output_dir=Path('/tmp/grape'),
            receiver_grid='EM38ww',
            station_config={'callsign': 'TEST'}
        )
        
        assert config.samples_per_packet == 400  # 20ms * 20000 Hz
        assert config.max_gap_samples == 1200000  # 60s * 20000 Hz
    
    def test_pipeline_recorder_state(self):
        """Test PipelineRecorderState enum"""
        from grape_recorder.grape import PipelineRecorderState
        
        assert PipelineRecorderState.IDLE.value == 'idle'
        assert PipelineRecorderState.RECORDING.value == 'recording'


class TestBatchReprocessor:
    """Tests for BatchReprocessor"""
    
    def test_batch_reprocessor_creation(self):
        """Test BatchReprocessor initialization"""
        from grape_recorder.grape.pipeline_orchestrator import BatchReprocessor
        
        with tempfile.TemporaryDirectory() as tmpdir:
            reprocessor = BatchReprocessor(
                data_dir=Path(tmpdir),
                channel_name='WWV_10MHz',
                frequency_hz=10e6,
                receiver_grid='EM38ww',
                station_config={'callsign': 'TEST'}
            )
            
            assert reprocessor.channel_name == 'WWV_10MHz'


class TestIntegration:
    """Integration tests for complete pipeline"""
    
    def test_synthetic_data_pipeline(self):
        """Test pipeline with synthetic IQ data"""
        from grape_recorder.grape import (
            ClockOffsetSeries,
            ClockOffsetMeasurement,
            ClockOffsetQuality
        )
        
        # Create synthetic clock offset series
        series = ClockOffsetSeries(
            channel_name='WWV_10MHz',
            frequency_hz=10e6,
            receiver_grid='EM38ww'
        )
        
        # Add measurements for 5 minutes
        base_time = 1733222400.0
        for i in range(5):
            series.add_measurement(ClockOffsetMeasurement(
                system_time=base_time + i * 60,
                utc_time=base_time + i * 60 - 0.015,  # 15ms offset
                minute_boundary_utc=base_time + i * 60,
                clock_offset_ms=15.0 + np.random.normal(0, 0.5),
                station='WWV',
                frequency_mhz=10.0,
                propagation_delay_ms=14.5,
                propagation_mode='1F',
                n_hops=1,
                confidence=0.9,
                uncertainty_ms=0.5,
                quality_grade=ClockOffsetQuality.GOOD
            ))
        
        # Verify we can query any time in range
        for t in np.linspace(base_time, base_time + 240, 10):
            result = series.get_offset_at_time(t)
            assert result is not None
            offset, _ = result
            assert 10 < offset < 20  # Should be around 15ms
        
        # Test quality summary
        summary = series.get_quality_summary()
        assert summary['B'] == 5  # All measurements are GOOD


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
