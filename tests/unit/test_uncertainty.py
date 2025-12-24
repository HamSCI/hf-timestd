"""
Unit tests for ISO GUM uncertainty calculator
"""

import pytest
import numpy as np

from hf_timestd.io.uncertainty import (
    ISOGUMCalculator,
    UncertaintyBudget
)


class TestUncertaintyBudget:
    """Test UncertaintyBudget dataclass."""
    
    def test_valid_budget(self):
        """Test creating a valid uncertainty budget."""
        budget = UncertaintyBudget(
            u_rtp_timestamp_ms=0.05,
            u_ionospheric_ms=1.0,
            u_multipath_ms=0.5,
            u_discrimination_ms=0.3,
            u_gpsdo_ms=0.001,
            u_propagation_model_ms=0.3,
            coverage_factor=2.0,
            confidence_level=0.95
        )
        
        assert budget.u_rtp_timestamp_ms == 0.05
        assert budget.coverage_factor == 2.0
        assert budget.confidence_level == 0.95
    
    def test_negative_uncertainty_rejected(self):
        """Test that negative uncertainties are rejected."""
        with pytest.raises(ValueError, match="negative"):
            UncertaintyBudget(
                u_rtp_timestamp_ms=-0.05,  # Negative!
                u_ionospheric_ms=1.0,
                u_multipath_ms=0.5,
                u_discrimination_ms=0.3,
                u_gpsdo_ms=0.001,
                u_propagation_model_ms=0.3
            )
    
    def test_nan_uncertainty_rejected(self):
        """Test that NaN uncertainties are rejected."""
        with pytest.raises(ValueError, match="not finite"):
            UncertaintyBudget(
                u_rtp_timestamp_ms=0.05,
                u_ionospheric_ms=np.nan,  # NaN!
                u_multipath_ms=0.5,
                u_discrimination_ms=0.3,
                u_gpsdo_ms=0.001,
                u_propagation_model_ms=0.3
            )
    
    def test_inf_uncertainty_rejected(self):
        """Test that inf uncertainties are rejected."""
        with pytest.raises(ValueError, match="not finite"):
            UncertaintyBudget(
                u_rtp_timestamp_ms=0.05,
                u_ionospheric_ms=1.0,
                u_multipath_ms=np.inf,  # Inf!
                u_discrimination_ms=0.3,
                u_gpsdo_ms=0.001,
                u_propagation_model_ms=0.3
            )


class TestISOGUMCalculator:
    """Test ISO GUM uncertainty calculator."""
    
    def test_calculate_combined_uncertainty(self):
        """Test combined uncertainty calculation."""
        budget = UncertaintyBudget(
            u_rtp_timestamp_ms=0.05,
            u_ionospheric_ms=1.0,
            u_multipath_ms=0.5,
            u_discrimination_ms=0.3,
            u_gpsdo_ms=0.001,
            u_propagation_model_ms=0.3,
            coverage_factor=2.0,
            confidence_level=0.95
        )
        
        result = ISOGUMCalculator.calculate_combined_uncertainty(budget)
        
        # Check Type A (sqrt(0.05² + 1.0² + 0.5²) ≈ 1.118)
        assert abs(result['u_type_a_ms'] - 1.118) < 0.01
        
        # Check Type B (sqrt(0.3² + 0.001² + 0.3²) ≈ 0.424)
        assert abs(result['u_type_b_ms'] - 0.424) < 0.01
        
        # Check combined (sqrt(1.118² + 0.424²) ≈ 1.196)
        assert abs(result['u_combined_ms'] - 1.196) < 0.01
        
        # Check expanded (2.0 × 1.196 ≈ 2.392)
        assert abs(result['u_expanded_ms'] - 2.392) < 0.01
        
        # Check metadata
        assert result['coverage_factor'] == 2.0
        assert result['confidence_level'] == 0.95
        assert result['degrees_of_freedom'] == 1000
    
    def test_quality_grade_assignment(self):
        """Test quality grade assignment based on expanded uncertainty."""
        # Grade A: U < 1.0 ms
        assert ISOGUMCalculator.assign_quality_grade(0.5) == 'A'
        assert ISOGUMCalculator.assign_quality_grade(0.99) == 'A'
        
        # Grade B: 1.0 ms <= U < 2.0 ms
        assert ISOGUMCalculator.assign_quality_grade(1.0) == 'B'
        assert ISOGUMCalculator.assign_quality_grade(1.5) == 'B'
        assert ISOGUMCalculator.assign_quality_grade(1.99) == 'B'
        
        # Grade C: 2.0 ms <= U < 3.0 ms
        assert ISOGUMCalculator.assign_quality_grade(2.0) == 'C'
        assert ISOGUMCalculator.assign_quality_grade(2.5) == 'C'
        assert ISOGUMCalculator.assign_quality_grade(2.99) == 'C'
        
        # Grade D: U >= 3.0 ms
        assert ISOGUMCalculator.assign_quality_grade(3.0) == 'D'
        assert ISOGUMCalculator.assign_quality_grade(10.0) == 'D'
    
    def test_quality_flag_assignment(self):
        """Test quality flag assignment."""
        # GOOD: Grade A/B, GPSDO locked, high discrimination confidence
        assert ISOGUMCalculator.assign_quality_flag('A', 0.9, True) == 'GOOD'
        assert ISOGUMCalculator.assign_quality_flag('B', 0.8, True) == 'GOOD'
        
        # MARGINAL: Grade C or moderate discrimination confidence
        assert ISOGUMCalculator.assign_quality_flag('C', 0.9, True) == 'MARGINAL'
        assert ISOGUMCalculator.assign_quality_flag('B', 0.6, True) == 'MARGINAL'
        
        # BAD: Grade D
        assert ISOGUMCalculator.assign_quality_flag('D', 0.9, True) == 'BAD'
        
        # BAD: GPSDO unlocked
        assert ISOGUMCalculator.assign_quality_flag('A', 0.9, False) == 'BAD'
        
        # BAD: Low discrimination confidence
        assert ISOGUMCalculator.assign_quality_flag('A', 0.4, True) == 'BAD'
    
    def test_validate_measurement_accepts_valid(self):
        """Test that valid measurements are accepted."""
        # Should not raise
        ISOGUMCalculator.validate_measurement(1.5, 'test_field')
        ISOGUMCalculator.validate_measurement(0.0, 'test_field')
        ISOGUMCalculator.validate_measurement(-1.5, 'test_field')
    
    def test_validate_measurement_rejects_nan(self):
        """Test that NaN measurements are rejected."""
        with pytest.raises(ValueError, match="NaN"):
            ISOGUMCalculator.validate_measurement(np.nan, 'test_field')
    
    def test_validate_measurement_rejects_inf(self):
        """Test that inf measurements are rejected."""
        with pytest.raises(ValueError, match="inf"):
            ISOGUMCalculator.validate_measurement(np.inf, 'test_field')
        
        with pytest.raises(ValueError, match="inf"):
            ISOGUMCalculator.validate_measurement(-np.inf, 'test_field')
    
    def test_create_default_budget(self):
        """Test creating default uncertainty budget."""
        budget = ISOGUMCalculator.create_default_budget(
            snr_db=10.0,
            gpsdo_locked=True,
            discrimination_confidence=0.8
        )
        
        # Check all components are positive
        assert budget.u_rtp_timestamp_ms > 0
        assert budget.u_ionospheric_ms > 0
        assert budget.u_multipath_ms > 0
        assert budget.u_discrimination_ms > 0
        assert budget.u_gpsdo_ms > 0
        assert budget.u_propagation_model_ms > 0
        
        # Check GPSDO locked has small uncertainty
        assert budget.u_gpsdo_ms < 0.01
        
        # Check unlocked GPSDO has large uncertainty
        budget_unlocked = ISOGUMCalculator.create_default_budget(
            snr_db=10.0,
            gpsdo_locked=False,
            discrimination_confidence=0.8
        )
        assert budget_unlocked.u_gpsdo_ms > 1.0
    
    def test_snr_scaling(self):
        """Test that uncertainties scale with SNR."""
        budget_high_snr = ISOGUMCalculator.create_default_budget(snr_db=20.0)
        budget_low_snr = ISOGUMCalculator.create_default_budget(snr_db=5.0)
        
        # Lower SNR should have higher ionospheric uncertainty
        assert budget_low_snr.u_ionospheric_ms > budget_high_snr.u_ionospheric_ms
        assert budget_low_snr.u_multipath_ms > budget_high_snr.u_multipath_ms
