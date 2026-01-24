"""
Stability analysis module for Allan deviation and related metrics.

This module re-exports the core stability analysis functions from the
hf_timestd.core.stability_analysis module. All calculations are performed
in the core library to ensure consistency and avoid code duplication.

References:
    - IEEE Std 1139-2008: Standard Definitions of Physical Quantities for
      Fundamental Frequency and Time Metrology
    - NIST Special Publication 1065: Handbook of Frequency Stability Analysis
"""

# Re-export all functions from core stability_analysis module
# This ensures web-api uses the same implementation as the core library
from hf_timestd.core.stability_analysis import (
    compute_phase_adev,
    compute_frequency_adev,
    identify_noise_type,
    compute_stability_at_tau,
    compute_stability_metrics,
)
