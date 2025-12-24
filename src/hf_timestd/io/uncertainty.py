"""
ISO GUM Uncertainty Calculator

Implements ISO Guide to the Expression of Uncertainty in Measurement (GUM)
for propagating Type A and Type B uncertainties in timing measurements.

Reference: JCGM 100:2008 (GUM 1995 with minor corrections)
"""

import numpy as np
from dataclasses import dataclass
from typing import Dict, Optional, List
import logging

logger = logging.getLogger(__name__)


@dataclass
class UncertaintyBudget:
    """
    ISO GUM uncertainty budget for a measurement.
    
    Attributes:
        u_rtp_timestamp_ms: Type A - RTP timestamp resolution uncertainty
        u_ionospheric_ms: Type A - Ionospheric propagation variability
        u_multipath_ms: Type A - Multipath/delay spread uncertainty
        u_discrimination_ms: Type B - Station discrimination uncertainty
        u_gpsdo_ms: Type B - GPSDO stability uncertainty
        u_propagation_model_ms: Type B - Propagation model uncertainty
        coverage_factor: Coverage factor k (typically 2 for 95% confidence)
        confidence_level: Confidence level (typically 0.95 for k=2)
    """
    # Type A uncertainties (statistical)
    u_rtp_timestamp_ms: float
    u_ionospheric_ms: float
    u_multipath_ms: float
    
    # Type B uncertainties (systematic)
    u_discrimination_ms: float
    u_gpsdo_ms: float
    u_propagation_model_ms: float
    
    # Coverage parameters
    coverage_factor: float = 2.0
    confidence_level: float = 0.95
    
    def __post_init__(self):
        """Validate uncertainty components are non-negative."""
        components = [
            self.u_rtp_timestamp_ms,
            self.u_ionospheric_ms,
            self.u_multipath_ms,
            self.u_discrimination_ms,
            self.u_gpsdo_ms,
            self.u_propagation_model_ms,
        ]
        
        for i, component in enumerate(components):
            if component < 0:
                raise ValueError(f"Uncertainty component {i} is negative: {component}")
            if not np.isfinite(component):
                raise ValueError(f"Uncertainty component {i} is not finite: {component}")


class ISOGUMCalculator:
    """
    ISO GUM uncertainty propagation calculator.
    
    Implements the standard uncertainty propagation formulas from ISO GUM:
    - Type A: u_A = sqrt(u_rtp² + u_ionospheric² + u_multipath²)
    - Type B: u_B = sqrt(u_discrimination² + u_gpsdo² + u_propagation_model²)
    - Combined: u_c = sqrt(u_A² + u_B²)
    - Expanded: U = k × u_c
    """
    
    @staticmethod
    def calculate_combined_uncertainty(budget: UncertaintyBudget) -> Dict[str, float]:
        """
        Calculate combined standard uncertainty from uncertainty budget.
        
        Args:
            budget: UncertaintyBudget with all components
            
        Returns:
            Dictionary with:
                - u_type_a_ms: Type A combined uncertainty
                - u_type_b_ms: Type B combined uncertainty
                - u_combined_ms: Combined standard uncertainty u_c
                - u_expanded_ms: Expanded uncertainty U = k × u_c
                - coverage_factor: Coverage factor k
                - confidence_level: Confidence level
                - degrees_of_freedom: Effective degrees of freedom
                
        Raises:
            ValueError: If any uncertainty component is invalid
        """
        # Type A: statistical uncertainties (root sum of squares)
        u_type_a = np.sqrt(
            budget.u_rtp_timestamp_ms**2 +
            budget.u_ionospheric_ms**2 +
            budget.u_multipath_ms**2
        )
        
        # Type B: systematic uncertainties (root sum of squares)
        u_type_b = np.sqrt(
            budget.u_discrimination_ms**2 +
            budget.u_gpsdo_ms**2 +
            budget.u_propagation_model_ms**2
        )
        
        # Combined standard uncertainty (ISO GUM Eq. 10)
        u_combined = np.sqrt(u_type_a**2 + u_type_b**2)
        
        # Expanded uncertainty (ISO GUM Eq. 13)
        u_expanded = budget.coverage_factor * u_combined
        
        # Effective degrees of freedom (Welch-Satterthwaite formula, ISO GUM G.3)
        # For simplicity, assume large sample sizes (ν_eff ≈ ∞)
        # In practice, this would be calculated from individual component DoF
        degrees_of_freedom = 1000  # Effectively infinite
        
        return {
            'u_type_a_ms': float(u_type_a),
            'u_type_b_ms': float(u_type_b),
            'u_combined_ms': float(u_combined),
            'u_expanded_ms': float(u_expanded),
            'coverage_factor': budget.coverage_factor,
            'confidence_level': budget.confidence_level,
            'degrees_of_freedom': degrees_of_freedom,
        }
    
    @staticmethod
    def assign_quality_grade(u_expanded_ms: float) -> str:
        """
        Assign quality grade based on expanded uncertainty.
        
        Quality grades (from L2 schema):
        - A: U < 1.0 ms
        - B: 1.0 ms <= U < 2.0 ms
        - C: 2.0 ms <= U < 3.0 ms
        - D: U >= 3.0 ms
        
        Args:
            u_expanded_ms: Expanded uncertainty in milliseconds
            
        Returns:
            Quality grade ('A', 'B', 'C', or 'D')
        """
        if u_expanded_ms < 1.0:
            return 'A'
        elif u_expanded_ms < 2.0:
            return 'B'
        elif u_expanded_ms < 3.0:
            return 'C'
        else:
            return 'D'
    
    @staticmethod
    def assign_quality_flag(
        quality_grade: str,
        discrimination_confidence: float,
        gpsdo_locked: bool
    ) -> str:
        """
        Assign overall quality flag based on multiple criteria.
        
        Quality flags (from L2 schema):
        - GOOD: Grade A or B, GPSDO locked, discrimination confidence > 0.7
        - MARGINAL: Grade C, or discrimination confidence 0.5-0.7
        - BAD: Grade D, or discrimination confidence < 0.5, or GPSDO unlocked
        - MISSING: No valid timing measurement
        
        Args:
            quality_grade: Quality grade ('A', 'B', 'C', 'D')
            discrimination_confidence: Station discrimination confidence (0-1)
            gpsdo_locked: GPSDO lock status
            
        Returns:
            Quality flag ('GOOD', 'MARGINAL', 'BAD', 'MISSING')
        """
        # BAD: GPSDO unlocked or very low discrimination confidence
        if not gpsdo_locked or discrimination_confidence < 0.5:
            return 'BAD'
        
        # BAD: Grade D
        if quality_grade == 'D':
            return 'BAD'
        
        # MARGINAL: Grade C or moderate discrimination confidence
        if quality_grade == 'C' or discrimination_confidence < 0.7:
            return 'MARGINAL'
        
        # GOOD: Grade A or B, good discrimination, GPSDO locked
        return 'GOOD'
    
    @staticmethod
    def validate_measurement(value: float, field_name: str) -> None:
        """
        Validate that a measurement value is finite (not NaN or inf).
        
        Args:
            value: Measurement value to validate
            field_name: Name of the field (for error messages)
            
        Raises:
            ValueError: If value is NaN or inf
        """
        if np.isnan(value):
            raise ValueError(f"{field_name} is NaN (not allowed)")
        if np.isinf(value):
            raise ValueError(f"{field_name} is inf (not allowed)")
    
    @staticmethod
    def create_default_budget(
        snr_db: float = 10.0,
        gpsdo_locked: bool = True,
        discrimination_confidence: float = 0.8
    ) -> UncertaintyBudget:
        """
        Create a default uncertainty budget with typical values.
        
        This is useful for testing or when detailed uncertainty analysis
        is not yet available.
        
        Args:
            snr_db: Signal-to-noise ratio in dB
            gpsdo_locked: GPSDO lock status
            discrimination_confidence: Station discrimination confidence
            
        Returns:
            UncertaintyBudget with typical values
        """
        # Type A uncertainties (scale with SNR)
        snr_linear = 10**(snr_db / 10)
        u_rtp_timestamp_ms = 0.05  # 50 µs RTP resolution at 20 kHz
        u_ionospheric_ms = 1.0 / np.sqrt(snr_linear)  # Scales with SNR
        u_multipath_ms = 0.5 / np.sqrt(snr_linear)  # Scales with SNR
        
        # Type B uncertainties (systematic)
        u_discrimination_ms = 0.5 * (1.0 - discrimination_confidence)  # Lower confidence = higher uncertainty
        u_gpsdo_ms = 0.001 if gpsdo_locked else 10.0  # 1 µs locked, 10 ms unlocked
        u_propagation_model_ms = 0.3  # Typical model uncertainty
        
        return UncertaintyBudget(
            u_rtp_timestamp_ms=u_rtp_timestamp_ms,
            u_ionospheric_ms=u_ionospheric_ms,
            u_multipath_ms=u_multipath_ms,
            u_discrimination_ms=u_discrimination_ms,
            u_gpsdo_ms=u_gpsdo_ms,
            u_propagation_model_ms=u_propagation_model_ms,
            coverage_factor=2.0,
            confidence_level=0.95,
        )
