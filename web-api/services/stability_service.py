"""
Stability analysis service for Allan deviation and related metrics.
"""

import numpy as np
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import logging

from hf_timestd.io.hdf5_reader import DataProductReader

logger = logging.getLogger(__name__)


class StabilityService:
    """Service for computing oscillator stability metrics."""
    
    def __init__(self, data_root: Path):
        """
        Initialize stability service.
        
        Args:
            data_root: Root directory for data products
        """
        self.data_root = Path(data_root)
        self.fusion_dir = self.data_root / 'phase2' / 'fusion'
        
        # Initialize reader for fusion data
        self.reader = DataProductReader(
            data_dir=self.fusion_dir,
            product_level='L3',
            product_name='fusion_timing',
            channel='fusion'
        )
    
    def compute_overlapping_adev(
        self,
        data: np.ndarray,
        rate: float,
        taus: Optional[List[float]] = None
    ) -> Tuple[np.ndarray, np.ndarray]:
        """
        Compute overlapping Allan deviation.
        
        Args:
            data: Fractional frequency data
            rate: Sample rate in Hz
            taus: Tau values to compute (in seconds). If None, use default set.
        
        Returns:
            Tuple of (tau_values, adev_values)
        """
        if taus is None:
            # Default tau values: 1 sample to N/2 samples
            max_m = len(data) // 2
            taus = np.logspace(0, np.log10(max_m), num=20, dtype=int)
            taus = np.unique(taus)  # Remove duplicates
            taus = taus / rate  # Convert to seconds
        
        adev_values = []
        
        for tau in taus:
            m = int(tau * rate)  # Number of samples per tau
            
            if m < 1 or m >= len(data) // 2:
                adev_values.append(np.nan)
                continue
            
            # Overlapping Allan deviation
            max_i = len(data) - 2 * m
            if max_i < 1:
                adev_values.append(np.nan)
                continue
            
            diffs = []
            for i in range(max_i):
                avg1 = np.mean(data[i:i+m])
                avg2 = np.mean(data[i+m:i+2*m])
                diffs.append(avg2 - avg1)
            
            diffs = np.array(diffs)
            adev = np.sqrt(0.5 * np.mean(diffs**2))
            adev_values.append(adev)
        
        return np.array(taus), np.array(adev_values)
    
    def compute_stability_metrics(
        self,
        start: datetime,
        end: datetime,
        min_points: int = 100
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
            start_str = start.isoformat() + 'Z'
            end_str = end.isoformat() + 'Z'
            
            measurements = self.reader.read_time_range(
                start=start_str,
                end=end_str
            )
            
            if len(measurements) < min_points:
                logger.warning(f"Insufficient data for stability analysis: {len(measurements)} < {min_points}")
                return None
            
            # Extract D_clock values (convert to fractional frequency)
            d_clock_ms = np.array([m['d_clock_fused_ms'] for m in measurements])
            timestamps = np.array([m['timestamp_utc'] for m in measurements])
            
            # Convert D_clock (time offset in ms) to fractional frequency
            # Assuming 1-minute sampling: f_frac = Δt / τ where τ = 60s
            # For small offsets: y ≈ Δt / τ
            sample_interval = 60.0  # seconds
            fractional_freq = d_clock_ms * 1e-3 / sample_interval  # dimensionless
            
            # Compute sample rate (should be ~1/60 Hz for 1-minute data)
            rate = 1.0 / sample_interval
            
            # Compute overlapping Allan deviation
            taus, adev = self.compute_overlapping_adev(fractional_freq, rate)
            
            # Filter out NaN values
            valid_mask = ~np.isnan(adev)
            taus = taus[valid_mask]
            adev = adev[valid_mask]
            
            if len(taus) == 0:
                logger.warning("No valid ADEV values computed")
                return None
            
            # Identify noise types based on slope
            # log(ADEV) vs log(tau) slope:
            #   -1: white phase noise
            #   -0.5: flicker phase noise
            #    0: white frequency noise
            #   +0.5: flicker frequency noise
            #   +1: random walk frequency noise
            
            noise_type = "Unknown"
            if len(taus) >= 3:
                # Fit line to log-log plot
                log_tau = np.log10(taus)
                log_adev = np.log10(adev)
                slope = np.polyfit(log_tau, log_adev, 1)[0]
                
                if slope < -0.75:
                    noise_type = "White Phase Noise"
                elif slope < -0.25:
                    noise_type = "Flicker Phase Noise"
                elif slope < 0.25:
                    noise_type = "White Frequency Noise"
                elif slope < 0.75:
                    noise_type = "Flicker Frequency Noise"
                else:
                    noise_type = "Random Walk Frequency"
            
            # Find ADEV at specific tau values by interpolation
            def get_adev_at_tau(target_tau):
                """Get ADEV at specific tau by finding closest value or interpolating."""
                if len(taus) == 0:
                    return None
                # Find closest tau value
                idx = np.argmin(np.abs(taus - target_tau))
                if np.abs(taus[idx] - target_tau) / target_tau < 0.3:  # Within 30%
                    return float(adev[idx])
                # Try linear interpolation in log space if we have bracketing values
                if target_tau < taus[0] or target_tau > taus[-1]:
                    return None
                # Interpolate in log-log space
                log_taus = np.log10(taus)
                log_adev = np.log10(adev)
                log_target = np.log10(target_tau)
                interp_log_adev = np.interp(log_target, log_taus, log_adev)
                return float(10 ** interp_log_adev)
            
            return {
                'tau_seconds': taus.tolist(),
                'adev': adev.tolist(),
                'n_points': len(measurements),
                'sample_rate_hz': rate,
                'time_span_hours': (end - start).total_seconds() / 3600,
                'dominant_noise': noise_type,
                'adev_1s': get_adev_at_tau(1.0),
                'adev_10s': get_adev_at_tau(10.0),
                'adev_60s': get_adev_at_tau(60.0),
                'adev_100s': get_adev_at_tau(100.0),
                'adev_1000s': get_adev_at_tau(1000.0),
                'adev_10000s': get_adev_at_tau(10000.0),
            }
        
        except Exception as e:
            logger.error(f"Error computing stability metrics: {e}")
            return None
