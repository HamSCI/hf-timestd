#!/usr/bin/env python3
"""
Test Physics-Based Fusion Service Integration
"""

import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path
import json
import shutil
import tempfile
import time
from datetime import datetime, timezone

from hf_timestd.core.physics_fusion_service import PhysicsFusionService
from hf_timestd.core.tec_estimator import TECResult

class TestPhysicsFusionService(unittest.TestCase):
    
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.data_root = Path(self.test_dir) / 'data'
        self.output_dir = Path(self.test_dir) / 'output'
        
        self.data_root.mkdir(parents=True)
        self.output_dir.mkdir(parents=True)
        
        # Mock phase2 directory structure
        (self.data_root / 'phase2' / 'WWV_10000').mkdir(parents=True)
        (self.data_root / 'phase2' / 'WWV_20000').mkdir(parents=True)
        
        self.service = PhysicsFusionService(
            data_root=self.data_root,
            output_dir=self.output_dir
        )
        
    def tearDown(self):
        shutil.rmtree(self.test_dir)
        
    @patch('hf_timestd.core.physics_fusion_service.DataProductReader')
    def test_process_minute_tec_logic(self, MockReader):
        """Test that TEC estimation is triggered when multi-freq data exists."""
        
        # Create synthetic L2 data for minute 1000
        obs_10 = [{
            'station': 'WWV', 
            'frequency_mhz': 10.0, 
            'tof_kalman_ms': 10.4,
            'tof_uncertainty_ms': 0.01
        }]
        
        obs_20 = [{
            'station': 'WWV', 
            'frequency_mhz': 20.0, 
            'tof_kalman_ms': 10.1,
            'tof_uncertainty_ms': 0.01
        }]
        
        # Mock class constructor to return different instances based on channel arg
        def reader_factory(*args, **kwargs):
            channel = kwargs.get('channel', '')
            mock_instance = MagicMock()
            mock_instance.channel = channel
            mock_instance._get_hdf5_path.return_value.exists.return_value = True
            
            if 'WWV_10000' in channel:
                mock_instance.read_time_range.return_value = obs_10
            elif 'WWV_20000' in channel:
                 mock_instance.read_time_range.return_value = obs_20
            else:
                 mock_instance.read_time_range.return_value = []
            return mock_instance
            
        MockReader.side_effect = reader_factory
        
        # Mock discovery to specific channels
        self.service.channels = ['WWV_10000', 'WWV_20000']
        
        # Spy on writer
        self.service.l3_writer = MagicMock()
        
        # Run
        self.service.process_minute(1000)
        
        # Verify L3 write
        self.service.l3_writer.write_measurement.assert_called_once()
        call_args = self.service.l3_writer.write_measurement.call_args[0][0]
        
        print(f"\nGenerared L3 Record: {json.dumps(call_args, default=str, indent=2)}")
        
        self.assertIn('WWV', call_args['stations_used'])
        self.assertTrue(call_args['utc_consistency_flag'])
        
        # We don't check exact TEC float value due to estimator details, 
        # but check it ran
        
    @patch('hf_timestd.core.physics_fusion_service.DataProductReader')
    def test_process_minute_insufficient_data(self, MockReader):
        """Test graceful handling of single frequency (no TEC)."""
         # Setup mocks
        mock_reader_instance = MockReader.return_value
        mock_reader_instance._get_hdf5_path.return_value = MagicMock(exists=lambda: True)
        
        # Only 10 MHz
        obs_10 = [{
            'station': 'WWV', 
            'frequency_mhz': 10.0, 
            'tof_kalman_ms': 10.4,
            'tof_uncertainty_ms': 0.01
        }]
        
        mock_reader_instance.read_time_range.return_value = obs_10
        self.service.channels = ['WWV_10000']
        
        self.service.l3_writer = MagicMock()
        
        self.service.process_minute(1000)
        
        # Should still write L3 but with empty/false values
        self.service.l3_writer.write_measurement.assert_called_once()
        record = self.service.l3_writer.write_measurement.call_args[0][0]
        
        self.assertEqual(len(record['stations_used']), 0) # No successful TEC stations
        self.assertFalse(record['utc_consistency_flag'])

if __name__ == '__main__':
    unittest.main()
