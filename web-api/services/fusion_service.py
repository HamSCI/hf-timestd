"""
Fusion timing data service.

Provides access to L3B fusion timing estimates using DataProductReader.
"""

import sys
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
import logging

# Add parent directory to path for hf_timestd imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'src'))

from hf_timestd.io.hdf5_reader import DataProductReader

logger = logging.getLogger(__name__)


class FusionService:
    """Service for accessing fusion timing data."""
    
    def __init__(self, fusion_dir: Path):
        """
        Initialize fusion service.
        
        Args:
            fusion_dir: Path to fusion data directory
        """
        self.fusion_dir = Path(fusion_dir)
        self.reader = DataProductReader(
            data_dir=self.fusion_dir,
            product_level='L3',
            product_name='fusion_timing',
            channel='fusion'
        )
    
    def get_latest(self) -> Optional[Dict[str, Any]]:
        """
        Get latest fusion estimate.
        
        Returns:
            Dictionary with latest fusion data or None if unavailable
        """
        try:
            # Get today's date
            today = datetime.utcnow().strftime('%Y%m%d')
            
            # Try to read from today's file
            end_time = datetime.utcnow().isoformat() + 'Z'
            start_time = (datetime.utcnow() - timedelta(hours=1)).isoformat() + 'Z'
            
            measurements = self.reader.read_time_range(
                start=start_time,
                end=end_time
            )
            
            if not measurements:
                logger.warning("No fusion measurements found in last hour")
                return None
            
            # Get most recent measurement
            latest = measurements[-1]
            
            # Parse stations_used
            stations_used = latest.get('stations_used', '')
            if isinstance(stations_used, str) and stations_used:
                stations_list = [s.strip() for s in stations_used.split(',') if s.strip()]
            else:
                stations_list = []
            
            # Helper to convert NaN/Inf to None for JSON serialization
            import math
            def clean_float(val):
                if val is None:
                    return None
                if isinstance(val, (int, float)) and (math.isnan(val) or math.isinf(val)):
                    return None
                return val
            
            # Return full detailed data for expert metrics
            return {
                'timestamp': latest.get('timestamp_utc'),
                'd_clock_ms': clean_float(latest.get('d_clock_fused_ms')),
                'd_clock_raw_ms': clean_float(latest.get('d_clock_raw_ms')),
                'uncertainty_ms': clean_float(latest.get('uncertainty_ms')),
                'statistical_uncertainty_ms': clean_float(latest.get('statistical_uncertainty_ms')),
                'systematic_uncertainty_ms': clean_float(latest.get('systematic_uncertainty_ms')),
                'propagation_uncertainty_ms': clean_float(latest.get('propagation_uncertainty_ms')),
                'quality_grade': latest.get('quality_grade'),
                'quality_flag': latest.get('quality_flag'),
                'n_broadcasts': latest.get('n_broadcasts'),
                'n_stations': latest.get('n_stations'),
                'stations_used': stations_list,
                'inter_station_spread_ms': clean_float(latest.get('inter_station_spread_ms')),
                'consistency_flag': latest.get('consistency_flag'),
                'outliers_rejected': latest.get('outliers_rejected'),
                'kalman_state': latest.get('kalman_state'),
                'reference_station': latest.get('reference_station'),
                'calibration_applied': latest.get('calibration_applied'),
                'processing_version': latest.get('processing_version'),
                'global_solve_verified': latest.get('global_solve_verified'),
                'global_solve_consistency_ms': clean_float(latest.get('global_solve_consistency_ms')),
                'global_solve_n_obs': latest.get('global_solve_n_obs'),
                # Per-station means
                'wwv_mean_ms': clean_float(latest.get('wwv_mean_ms')),
                'wwvh_mean_ms': clean_float(latest.get('wwvh_mean_ms')),
                'chu_mean_ms': clean_float(latest.get('chu_mean_ms')),
                'bpm_mean_ms': clean_float(latest.get('bpm_mean_ms')),
                # Per-station counts
                'wwv_count': latest.get('wwv_count'),
                'wwvh_count': latest.get('wwvh_count'),
                'chu_count': latest.get('chu_count'),
                'bpm_count': latest.get('bpm_count'),
                # Per-station intra-station std
                'wwv_intra_std_ms': clean_float(latest.get('wwv_intra_std_ms')),
                'wwvh_intra_std_ms': clean_float(latest.get('wwvh_intra_std_ms')),
                'chu_intra_std_ms': clean_float(latest.get('chu_intra_std_ms')),
                # L1 vs L2 comparison (v6.2 metrological tracking)
                'd_clock_l1_ms': clean_float(latest.get('d_clock_l1_ms')),
                'd_clock_l2_ms': clean_float(latest.get('d_clock_l2_ms')),
                'l1_l2_difference_ms': clean_float(latest.get('l1_l2_difference_ms')),
            }
        
        except Exception as e:
            logger.error(f"Error getting latest fusion: {e}")
            return None
    
    def get_history(
        self,
        start: datetime,
        end: datetime,
        min_quality_grade: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Get fusion timing history.
        
        Args:
            start: Start datetime
            end: End datetime
            min_quality_grade: Minimum quality grade filter
            
        Returns:
            Dictionary with time series data
        """
        try:
            # Convert to ISO8601
            start_iso = start.isoformat() + 'Z'
            end_iso = end.isoformat() + 'Z'
            
            # Read measurements
            measurements = self.reader.read_time_range(
                start=start_iso,
                end=end_iso,
                min_quality_grade=min_quality_grade
            )
            
            # Extract arrays
            timestamps = []
            d_clock_ms = []
            uncertainty_ms = []
            quality_grades = []
            n_broadcasts = []
            
            for m in measurements:
                timestamps.append(m.get('timestamp_utc', ''))
                d_clock_ms.append(float(m.get('d_clock_fused_ms', 0.0)))
                uncertainty_ms.append(float(m.get('uncertainty_ms', 0.0)))
                quality_grades.append(m.get('quality_grade', 'D'))
                n_broadcasts.append(int(m.get('n_broadcasts', 0)))
            
            return {
                'timestamps': timestamps,
                'd_clock_ms': d_clock_ms,
                'uncertainty_ms': uncertainty_ms,
                'quality_grade': quality_grades,
                'n_broadcasts': n_broadcasts,
                'count': len(measurements)
            }
        
        except Exception as e:
            logger.error(f"Error getting fusion history: {e}")
            return {
                'timestamps': [],
                'd_clock_ms': [],
                'uncertainty_ms': [],
                'quality_grade': [],
                'n_broadcasts': [],
                'count': 0
            }
