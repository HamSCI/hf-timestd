"""
Stability analysis service for Allan deviation and related metrics.

Note: The core ADEV algorithms are defined in hf_timestd.core.stability_analysis.
This service provides the API wrapper and data retrieval logic.
"""

import numpy as np
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import logging

from hf_timestd.io import make_data_product_reader
from config import config

# Import core stability functions - try from installed package first,
# fall back to local implementation if not available
try:
    from hf_timestd.core.stability_analysis import (
        compute_phase_adev,
        compute_stability_metrics as core_compute_stability,
        identify_noise_type,
        compute_stability_at_tau
    )
except ImportError:
    # Core library not installed - use local implementation
    from .stability_core import (
        compute_phase_adev,
        compute_stability_metrics as core_compute_stability,
        identify_noise_type,
        compute_stability_at_tau
    )

logger = logging.getLogger(__name__)


class StabilityService:
    """
    API service for computing oscillator stability metrics.
    
    This is a thin wrapper that handles data retrieval and delegates
    the actual ADEV calculations to hf_timestd.core.stability_analysis.
    """
    
    def __init__(self, data_root: Path):
        """
        Initialize stability service.
        
        Args:
            data_root: Root directory for data products
        """
        self.data_root = Path(data_root)
        self.fusion_dir = self.data_root / 'phase2' / 'fusion'
        
        # Initialize reader for fusion data
        self.reader = make_data_product_reader(
            data_dir=self.fusion_dir,
            product_level='L3',
            product_name='fusion_timing',
            channel='fusion',
            storage_config=config.storage
        )
    
    def compute_stability_metrics(
        self,
        start: datetime,
        end: datetime,
        min_points: int = 60
    ) -> Optional[Dict]:
        """
        Compute stability metrics from fusion timing data.
        
        Args:
            start: Start time
            end: End time
            min_points: Minimum number of data points required
        
        Returns:
            Dictionary with stability metrics or None if insufficient data
        """
        try:
            # Read fusion timing data
            start_str = start.strftime('%Y-%m-%dT%H:%M:%S.%fZ')
            end_str = end.strftime('%Y-%m-%dT%H:%M:%S.%fZ')
            
            measurements = self.reader.read_time_range(
                start=start_str,
                end=end_str
            )
            
            if len(measurements) < min_points:
                logger.warning(f"Insufficient data for stability analysis: {len(measurements)} < {min_points}")
                return None
            
            # Extract D_clock values (convert to fractional frequency)
            d_clock_ms = np.array([m['d_clock_fused_ms'] for m in measurements])
            
            # Parse timestamps to seconds for interval calculation
            # timestamp_utc is ISO format string
            timestamps_dt = [datetime.fromisoformat(m['timestamp_utc'].replace('Z', '+00:00')) for m in measurements]
            timestamps = np.array([dt.timestamp() for dt in timestamps_dt])
            
            # Calculate sample interval dynamically
            intervals = np.diff(timestamps)
            if len(intervals) > 0:
                sample_interval = float(np.median(intervals))
            else:
                sample_interval = 8.0  # Fallback to nominal 8s
                
            if sample_interval <= 0:
                logger.warning(f"Invalid sample interval calculated: {sample_interval}")
                sample_interval = 8.0

            # D_clock is phase data (time offset in ms)
            # Convert to seconds for ADEV calculation
            phase_seconds = d_clock_ms * 1e-3
            
            # Delegate to core library for ADEV calculation
            result = core_compute_stability(phase_seconds, sample_interval)
            
            if not result or len(result.get('tau_seconds', [])) == 0:
                logger.warning("No valid ADEV values computed")
                return None
            
            # Add API-specific fields
            result['n_points'] = len(measurements)
            result['sample_rate_hz'] = 1.0 / sample_interval
            result['time_span_hours'] = (end - start).total_seconds() / 3600
            
            return result
        
        except Exception as e:
            logger.error(f"Error computing stability metrics: {e}")
            return None
