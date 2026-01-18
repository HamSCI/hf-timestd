"""
Stability analysis module for Allan deviation and related metrics.

This module provides core metrology functions for computing oscillator
stability metrics from phase or frequency data. These are fundamental
measurement science calculations that belong in the core library.

References:
    - IEEE Std 1139-2008: Standard Definitions of Physical Quantities for
      Fundamental Frequency and Time Metrology
    - NIST Special Publication 1065: Handbook of Frequency Stability Analysis
"""

import numpy as np
from typing import Dict, List, Optional, Tuple
import logging

logger = logging.getLogger(__name__)


def compute_phase_adev(
    phase: np.ndarray,
    tau0: float,
    taus: Optional[np.ndarray] = None,
    overlapping: bool = True
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Compute Allan deviation from phase (time offset) data.
    
    For phase data x(t), ADEV is computed using second differences:
    σ_y(τ) = sqrt(1/(2τ²) * <(x[i+2m] - 2*x[i+m] + x[i])²>)
    
    This is the standard formula from IEEE 1139-2008.
    
    Args:
        phase: Phase (time offset) data in seconds, uniformly sampled
        tau0: Basic sampling interval in seconds
        taus: Averaging times to compute. If None, uses octave spacing.
        overlapping: If True, use overlapping estimator (recommended)
    
    Returns:
        Tuple of (tau_values, adev_values) in seconds and dimensionless
    
    Example:
        >>> phase = np.cumsum(np.random.randn(1000) * 1e-9)  # Random walk phase
        >>> taus, adev = compute_phase_adev(phase, tau0=1.0)
        >>> # For white frequency noise, ADEV ~ 1/sqrt(tau)
    """
    n = len(phase)
    if n < 3:
        return np.array([]), np.array([])
    
    if taus is None:
        # Default: octave-spaced tau values from tau0 to N/3 * tau0
        max_m = n // 3
        if max_m < 1:
            return np.array([]), np.array([])
        # Octave spacing: 1, 2, 4, 8, 16, ...
        m_values = 2 ** np.arange(0, int(np.log2(max_m)) + 1)
        m_values = m_values[m_values <= max_m]
        taus = m_values * tau0
    
    tau_out = []
    adev_out = []
    
    for tau in taus:
        m = int(round(tau / tau0))  # Number of samples per tau
        
        if m < 1 or 2 * m >= n:
            continue
        
        if overlapping:
            # Overlapping Allan deviation (more statistically efficient)
            # Second difference: x[i+2m] - 2*x[i+m] + x[i]
            second_diffs = phase[2*m:] - 2*phase[m:-m] + phase[:-2*m]
        else:
            # Non-overlapping (classical) Allan deviation
            # Use only non-overlapping triplets
            n_triplets = (n - 2*m) // m
            if n_triplets < 1:
                continue
            indices = np.arange(n_triplets) * m
            second_diffs = phase[indices + 2*m] - 2*phase[indices + m] + phase[indices]
        
        if len(second_diffs) == 0:
            continue
        
        # Allan variance from phase: σ²_y(τ) = 1/(2τ²) * mean(second_diff²)
        tau_actual = m * tau0
        allan_var = np.mean(second_diffs**2) / (2 * tau_actual**2)
        adev = np.sqrt(allan_var)
        
        tau_out.append(tau_actual)
        adev_out.append(adev)
    
    return np.array(tau_out), np.array(adev_out)


def compute_frequency_adev(
    frequency: np.ndarray,
    tau0: float,
    taus: Optional[np.ndarray] = None,
    overlapping: bool = True
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Compute Allan deviation from fractional frequency data.
    
    For frequency data y(t), ADEV is computed using first differences
    of averaged frequency:
    σ_y(τ) = sqrt(0.5 * <(ȳ[i+1] - ȳ[i])²>)
    
    where ȳ[i] is the average frequency over interval i.
    
    Args:
        frequency: Fractional frequency data (dimensionless), uniformly sampled
        tau0: Basic sampling interval in seconds
        taus: Averaging times to compute. If None, uses octave spacing.
        overlapping: If True, use overlapping estimator (recommended)
    
    Returns:
        Tuple of (tau_values, adev_values) in seconds and dimensionless
    """
    n = len(frequency)
    if n < 2:
        return np.array([]), np.array([])
    
    if taus is None:
        max_m = n // 2
        if max_m < 1:
            return np.array([]), np.array([])
        m_values = 2 ** np.arange(0, int(np.log2(max_m)) + 1)
        m_values = m_values[m_values <= max_m]
        taus = m_values * tau0
    
    tau_out = []
    adev_out = []
    
    for tau in taus:
        m = int(round(tau / tau0))
        
        if m < 1 or 2 * m > n:
            continue
        
        if overlapping:
            # Compute m-sample averages at each position
            # Using convolution for efficiency
            kernel = np.ones(m) / m
            avg_freq = np.convolve(frequency, kernel, mode='valid')
            # First differences of averages
            diffs = avg_freq[m:] - avg_freq[:-m]
        else:
            # Non-overlapping averages
            n_pairs = n // (2 * m)
            if n_pairs < 1:
                continue
            avg1 = np.array([np.mean(frequency[i*m:(i+1)*m]) for i in range(0, 2*n_pairs, 2)])
            avg2 = np.array([np.mean(frequency[i*m:(i+1)*m]) for i in range(1, 2*n_pairs, 2)])
            diffs = avg2 - avg1
        
        if len(diffs) == 0:
            continue
        
        allan_var = 0.5 * np.mean(diffs**2)
        adev = np.sqrt(allan_var)
        
        tau_out.append(m * tau0)
        adev_out.append(adev)
    
    return np.array(tau_out), np.array(adev_out)


def identify_noise_type(taus: np.ndarray, adev: np.ndarray) -> str:
    """
    Identify dominant noise type from ADEV slope.
    
    The slope of log(ADEV) vs log(tau) indicates noise type:
        -1.0: White phase noise (WPM)
        -0.5: Flicker phase noise (FPM)  
         0.0: White frequency noise (WFM)
        +0.5: Flicker frequency noise (FFM)
        +1.0: Random walk frequency noise (RWFM)
    
    Args:
        taus: Averaging times in seconds
        adev: Allan deviation values (dimensionless)
    
    Returns:
        String describing dominant noise type
    """
    if len(taus) < 3 or len(adev) < 3:
        return "Insufficient data"
    
    # Filter out any invalid values
    valid = (taus > 0) & (adev > 0) & np.isfinite(taus) & np.isfinite(adev)
    taus = taus[valid]
    adev = adev[valid]
    
    if len(taus) < 3:
        return "Insufficient valid data"
    
    # Fit line to log-log plot
    log_tau = np.log10(taus)
    log_adev = np.log10(adev)
    slope, _ = np.polyfit(log_tau, log_adev, 1)
    
    # Classify based on slope
    if slope < -0.75:
        return "White Phase Noise"
    elif slope < -0.25:
        return "Flicker Phase Noise"
    elif slope < 0.25:
        return "White Frequency Noise"
    elif slope < 0.75:
        return "Flicker Frequency Noise"
    else:
        return "Random Walk Frequency"


def compute_stability_at_tau(
    taus: np.ndarray,
    adev: np.ndarray,
    target_tau: float
) -> Optional[float]:
    """
    Get ADEV at a specific tau by interpolation.
    
    Args:
        taus: Array of tau values
        adev: Array of ADEV values
        target_tau: Desired tau value
    
    Returns:
        Interpolated ADEV value, or None if out of range
    """
    if len(taus) == 0 or len(adev) == 0:
        return None
    
    # Check if target is within range
    if target_tau < taus[0] * 0.5 or target_tau > taus[-1] * 2.0:
        return None
    
    # Find closest value
    idx = np.argmin(np.abs(taus - target_tau))
    if np.abs(taus[idx] - target_tau) / target_tau < 0.3:
        return float(adev[idx])
    
    # Interpolate in log-log space
    if target_tau < taus[0] or target_tau > taus[-1]:
        return None
    
    log_taus = np.log10(taus)
    log_adev = np.log10(adev)
    log_target = np.log10(target_tau)
    interp_log_adev = np.interp(log_target, log_taus, log_adev)
    
    return float(10 ** interp_log_adev)


def compute_stability_metrics(
    phase_data: np.ndarray,
    sample_interval: float,
    taus: Optional[np.ndarray] = None
) -> Dict:
    """
    Compute comprehensive stability metrics from phase data.
    
    This is the main entry point for stability analysis.
    
    Args:
        phase_data: Phase (time offset) data in seconds
        sample_interval: Sampling interval in seconds
        taus: Optional specific tau values to compute
    
    Returns:
        Dictionary containing:
            - tau_seconds: Array of tau values
            - adev: Array of ADEV values
            - dominant_noise: Identified noise type
            - adev_1s, adev_10s, etc.: ADEV at standard tau values
    """
    taus_out, adev_out = compute_phase_adev(
        phase_data, 
        tau0=sample_interval,
        taus=taus
    )
    
    if len(taus_out) == 0:
        return {
            'tau_seconds': [],
            'adev': [],
            'dominant_noise': 'Insufficient data',
            'n_points': len(phase_data),
            'sample_interval': sample_interval
        }
    
    noise_type = identify_noise_type(taus_out, adev_out)
    
    return {
        'tau_seconds': taus_out.tolist(),
        'adev': adev_out.tolist(),
        'dominant_noise': noise_type,
        'n_points': len(phase_data),
        'sample_interval': sample_interval,
        'adev_1s': compute_stability_at_tau(taus_out, adev_out, 1.0),
        'adev_10s': compute_stability_at_tau(taus_out, adev_out, 10.0),
        'adev_60s': compute_stability_at_tau(taus_out, adev_out, 60.0),
        'adev_100s': compute_stability_at_tau(taus_out, adev_out, 100.0),
        'adev_1000s': compute_stability_at_tau(taus_out, adev_out, 1000.0),
        'adev_10000s': compute_stability_at_tau(taus_out, adev_out, 10000.0),
    }
