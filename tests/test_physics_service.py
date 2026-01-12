
import pytest
import shutil
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from hf_timestd.core.physics_service import PhysicsService
from hf_timestd.models import StationID

@pytest.fixture
def temp_dirs(tmp_path):
    l1_dir = tmp_path / "l1"
    out_dir = tmp_path / "out"
    l1_dir.mkdir()
    out_dir.mkdir()
    return l1_dir, out_dir

@pytest.fixture
def service(temp_dirs):
    l1, out = temp_dirs
    # Mock config
    config = {}
    return PhysicsService(
        config=config,
        l1_data_dir=l1,
        output_dir=out,
        receiver_lat=40.0,
        receiver_lon=-105.0
    )

def test_process_single_measurement_valid(service):
    # Mock Solver
    # We cheat and just verify it calls solver and returns something
    # But real solver is instantiated. Let's try to use real solver if possible, 
    # but it might require IRI/network.
    # TransmissionTimeSolver defaults to static if simple?
    # We enabled dynamic ionosphere in service init.
    # We should mock solver.solve to avoid external dependencies and speed up test.
    
    mock_result = MagicMock()
    mock_result.confidence = 0.95
    mock_result.propagation_delay_ms = 5.5
    mock_result.mode_name = "1F"
    
    service.solver.solve = MagicMock(return_value=mock_result)
    
    # Create dummy L1 data (dict as returned by Reader)
    l1_meas = {
        'station_id': 'WWV',
        'frequency_mhz': 10.0,
        'raw_toa_ms': 6.0, # 6ms observed delay
        'timestamp_utc': '2025-01-01T12:00:00Z',
        'snr_db': 20.0
    }
    
    res = service._process_single_measurement(l1_meas)
    
    assert res is not None
    assert res.station_id == StationID.WWV
    assert res.propagation_delay_ms == 5.5
    assert res.propagation_mode == "1F"
    assert res.model_confidence == 0.95
    
    # Verify Solver called with correct args
    # arrival_rtp = (6.0 / 1000.0) * 24000 = 144
    service.solver.solve.assert_called_once()
    kwargs = service.solver.solve.call_args[1]
    assert kwargs['station'] == 'WWV'
    assert kwargs['arrival_rtp'] == 144
    assert kwargs['expected_second_rtp'] == 0

def test_process_single_measurement_invalid_station(service):
    l1_meas = {
        'station_id': 'UNKNOWN',
        'frequency_mhz': 10.0,
        'raw_toa_ms': 6.0,
        'timestamp_utc': '2025-01-01T12:00:00Z'
    }
    res = service._process_single_measurement(l1_meas)
    assert res is None

def test_process_single_measurement_nan_toa(service):
    l1_meas = {
        'station_id': 'WWV',
        'frequency_mhz': 10.0,
        'raw_toa_ms': float('nan'),
        'timestamp_utc': '2025-01-01T12:00:00Z'
    }
    res = service._process_single_measurement(l1_meas)
    assert res is None
